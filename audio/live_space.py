"""ライブ会場のような音場をリアルタイムに作る（Live Space）モジュール。

:mod:`audio.time_stretch` と同じく numpy 以外に依存しない純粋な DSP で、
キー変更のあと・出力デバイスへ書く直前に 1 ブロックずつ通す
（:meth:`audio.live_monitor.LiveMonitor._on_output` 参照）。

■ 段構成

::

    in ─┬─ Width（Mid/Side）── Audience の高域減衰 ── dry_gain ───────────────┐
        └─ mono ─ HPF ─┬─ Early Reflections（鏡像法・壁の吸音 FIR）───────────┼─ Σ ─ clip ─ out
                       ├─ Pre-delay ─ Diffuser(allpass×2) ─ FDN-8（HF減衰）───┤
                       └─ Echo（ステレオ・ピンポン、帰還路ローパス）───────────┘

* **Direct**: dry には Width と、Audience=BACK のときのわずかな高域減衰
  （空気吸収）以外は何も掛けない。
* **Early Reflections**: 会場を直方体（シューボックス）とみなし、鏡像法
  2 次までの反射（26 個）から遅延・ゲイン・方位を計算する。Room Size
  で会場の寸法、Audience で客席位置が決まるので、「壁までの距離」
  「音源との距離」が物理的に連動する。
* **Reverb**: 入口の 2 段 allpass（Diffusion）で密度を上げてから、
  遅延線 8 本を Hadamard 行列で混ぜる FDN。ループ内に HF 減衰（FIR）を
  持ち、Decay（T60）からゲインを求める。低域は送り側の HPF で抑える。
* **Echo**: 奥の壁からの単発の戻りを模したステレオ・ピンポン・ディレイ。
  120ms 未満はコム/フランジになるため下限固定。帰還路にローパスを
  入れて繰り返すたびに暗くなる。Feedback 上限 0.6 で発振しない。
* **Audience**: FRONT / MIDDLE / BACK。聴取位置（鏡像法の距離）と
  dry / ER / Reverb / 高域のバランスをまとめて動かす。

■ リアルタイム性

* Python のサンプル単位ループは使わない。遅延線は「遅延長 ≥ チャンク長」
  になるよう分割し、チャンクごとに numpy でまとめて処理する。
* ゲインはブロック内で直線補間（つまみを動かしてもクリックしない）。
* 遅延長そのものが変わる操作（Room Size / Pre-delay / Echo Time /
  Audience）は、値が :data:`_ROOM_SETTLE_BLOCKS` 静止したあと新しい
  エンジンを作り、1 ブロックでクロスフェードする（:class:`_RoomEngine`）。

■ 将来の拡張

各段は「(frames,) または (frames, 2) を受け取り同じ形を返す」規約。
:class:`_RoomEngine` を差し替えれば会場 IR による畳み込み残響に、
dry の出口に FIR を足せば HRTF に、鏡像法の音源位置を動かせば
Stage Position に、それぞれ拡張できる。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace, fields
from typing import Any

import numpy as np

from audio import hrtf

# ----------------------------------------------------------------------
#  公開パラメータ
# ----------------------------------------------------------------------
OUTPUT_MODES: tuple[str, ...] = ("headphone", "speaker")
AUDIENCE_POSITIONS: tuple[str, ...] = ("front", "middle", "back")


@dataclass(frozen=True)
class LiveSpaceParams:
    """Live Space の利用者向けパラメータ。frozen なので差し替えは原子的。"""

    width: int = 100
    """ステレオ幅[%] 0〜200。100 = 標準。"""
    room_size: int = 50
    """会場の大きさ[%]。寸法・残響の遅延長を決める。"""
    depth: int = 40
    """奥行き感[%]。dry を下げ反射を増やす（Audience と併用）。"""
    early: int = 40
    """初期反射の量[%]。"""
    predelay_ms: float = 20.0
    """残響の前の隙間[ms] 0〜80。"""
    reverb: int = 35
    """残響の量[%]。"""
    decay_s: float = 1.8
    """残響時間 T60[秒] 0.2〜6.0。"""
    damping: int = 50
    """高域減衰[%]。大きいほど暗く柔らかい。"""
    diffusion: int = 60
    """残響の密度[%]（プリセット内部値）。"""
    echo: int = 0
    """エコーの量[%]。"""
    echo_time_ms: float = 300.0
    """エコーの間隔[ms] 120〜800。"""
    echo_feedback: int = 20
    """エコーの繰り返し[%] 0〜100（内部で 0〜0.6 に丸める）。"""
    audience: str = "middle"
    """客席位置。:data:`AUDIENCE_POSITIONS`。"""
    dry_wet: int = 100
    """効果の全体量[%]（プリセット内部値。100 = 設計どおり）。"""
    wall_reflect: int = 72
    """壁 1 回あたりの反射率[%]（プリセット内部値）。野外は壁が無いので低い。"""
    vocal_center: int = 100
    """ボーカル（中央成分）の音量[%] 0〜200。100 = 何もしない。

    Width は Side（左右差＝楽器の広がり）だけを操作し、Mid（中央＝ボーカルが
    多い）には触れないため、広げてもボーカルの位置はズレない。ただし Depth や
    客席を「後方」にすると、ボーカルも周囲の音と一緒に音量が下がる。これは
    その下がり方とは無関係に、ボーカルだけを前後させるための独立したツマミ。
    """
    instrument_level: int = 100
    """楽器・伴奏（左右差の成分）の音量[%] 0〜200。100 = 何もしない。

    :attr:`vocal_center` が Mid（中央＝ボーカル）を操作するのに対し、こちらは
    Side（左右差＝ボーカル以外のほぼ全部）を操作する。Width（広がり）とは
    別軸で、「広さ」ではなく「音量」を独立に動かせる。
    """


PRESETS: dict[str, LiveSpaceParams] = {
    "Studio": LiveSpaceParams(
        width=95, room_size=8, depth=5, early=10, predelay_ms=6.0,
        reverb=8, decay_s=0.35, damping=55, diffusion=70,
        echo=0, echo_time_ms=200.0, echo_feedback=0, audience="front",
    ),
    "Small Live House": LiveSpaceParams(
        width=125, room_size=25, depth=20, early=55, predelay_ms=8.0,
        reverb=20, decay_s=0.8, damping=45, diffusion=55,
        echo=8, echo_time_ms=160.0, echo_feedback=15, audience="front",
    ),
    "Concert Hall": LiveSpaceParams(
        width=140, room_size=60, depth=40, early=40, predelay_ms=24.0,
        reverb=42, decay_s=2.2, damping=40, diffusion=75,
        echo=6, echo_time_ms=260.0, echo_feedback=15, audience="middle",
    ),
    "Arena": LiveSpaceParams(
        width=155, room_size=80, depth=55, early=38, predelay_ms=36.0,
        reverb=50, decay_s=3.2, damping=50, diffusion=70,
        echo=14, echo_time_ms=380.0, echo_feedback=25, audience="middle",
    ),
    "Stadium": LiveSpaceParams(
        width=170, room_size=100, depth=70, early=32, predelay_ms=50.0,
        reverb=55, decay_s=4.5, damping=65, diffusion=60,
        echo=22, echo_time_ms=520.0, echo_feedback=35, audience="back",
    ),
    "Arena (Best Seat)": LiveSpaceParams(
        width=150, room_size=80, depth=35, early=28, predelay_ms=45.0,
        reverb=42, decay_s=3.0, damping=45, diffusion=75,
        echo=12, echo_time_ms=375.0, echo_feedback=20, audience="middle",
    ),
    "Stadium (Best Seat)": LiveSpaceParams(
        width=165, room_size=100, depth=45, early=22, predelay_ms=70.0,
        reverb=48, decay_s=4.0, damping=60, diffusion=70,
        echo=18, echo_time_ms=500.0, echo_feedback=28, audience="middle",
    ),
    # --- 会場の規模別バリエーション（特定の実在会場を再現したものではない）
    "Live 1": LiveSpaceParams(
        # 小規模ライブハウス相当（30×25×10m、天井低め）
        width=125, room_size=22, depth=18, early=60, predelay_ms=8.0,
        reverb=18, decay_s=0.7, damping=50, diffusion=55,
        echo=5, echo_time_ms=150.0, echo_feedback=10, audience="front",
        wall_reflect=70,
    ),
    "Live 2": LiveSpaceParams(
        # 天井の高いホール相当（八角形・直径約90m・天井約40m）
        width=150, room_size=70, depth=40, early=30, predelay_ms=30.0,
        reverb=45, decay_s=2.6, damping=45, diffusion=75,
        echo=10, echo_time_ms=320.0, echo_feedback=20, audience="middle",
        wall_reflect=66,
    ),
    "Live 3": LiveSpaceParams(
        # 標準的なアリーナ相当（約100×80×30m）
        width=155, room_size=82, depth=45, early=34, predelay_ms=38.0,
        reverb=48, decay_s=3.0, damping=50, diffusion=72,
        echo=14, echo_time_ms=400.0, echo_feedback=25, audience="middle",
        wall_reflect=72,
    ),
    "Live 4": LiveSpaceParams(
        # 超大規模ドーム相当（直径約200m・天井約60m）。本物は残響5秒超・
        # エコー600ms超で濁るため、Best Seat と同じ考え方で抑えつつ
        # 長いエコーだけ残す。
        width=165, room_size=100, depth=50, early=25, predelay_ms=60.0,
        reverb=50, decay_s=4.2, damping=65, diffusion=65,
        echo=22, echo_time_ms=600.0, echo_feedback=35, audience="middle",
        wall_reflect=76,
    ),
    "Live 5": LiveSpaceParams(
        # 野外会場相当。壁が無い（反射率15%）ので初期反射・残響はごく薄く、
        # 遠くの建造物からの薄いエコーと、開けた広さだけがある。
        width=160, room_size=100, depth=35, early=8, predelay_ms=10.0,
        reverb=12, decay_s=1.2, damping=70, diffusion=40,
        echo=18, echo_time_ms=450.0, echo_feedback=15, audience="middle",
        wall_reflect=15,
    ),
}
"""プリセット名 → パラメータ（辞書順が UI のメニュー順）。

``Live 1``〜``Live 5`` は特定の実在会場を再現したものではなく、
「会場の規模が大きくなるほど音場がどう変わるか」を段階的に表した
バリエーション（各パラメータの根拠は定義の直上コメント参照）。

会場が大きいほど「反射が遅く届き、残響が長く、高域が空気で減り、
遠い壁からの単発の戻り（Echo）がはっきりする」という物理に沿わせ、
小さい会場ほど初期反射が濃く残響は短い。

``(Best Seat)`` 版は「物理的に正しい席」ではなく「音が一番良く聞こえる席」。
本物のアリーナ／スタジアムは残響 5〜8 秒・遅いエコーで濁った会場なので、
忠実に再現すると歌詞が聞き取れない。直接音を近く・クリアに保ちつつ
（MIDDLE 席、長い Pre-Delay、薄い初期反射）、会場感だけを後ろに大きく
置く設計にしてある。Echo Time は 120BPM の 1 拍（500ms）前後。
"""

CUSTOM_PRESET = "Custom"
DEFAULT_PRESET = "Concert Hall"

WIDTH_MIN, WIDTH_MAX = 0, 200
PERCENT_MIN, PERCENT_MAX = 0, 100
PREDELAY_MIN_MS, PREDELAY_MAX_MS = 0.0, 80.0
DECAY_MIN_S, DECAY_MAX_S = 0.2, 6.0
ECHO_TIME_MIN_MS, ECHO_TIME_MAX_MS = 120.0, 800.0

# ----------------------------------------------------------------------
#  内部の定数
# ----------------------------------------------------------------------
_SPEED_OF_SOUND = 343.0
_LOWPASS_CHUNK = 1024

_WIDTH_LOW_CUT_HZ = 150.0
_WIDTH_SIDE_GAIN_PER_100 = 1.0
_WIDTH_LOUDNESS_COMP = 0.25
"""広げた Side 側だけに掛ける音量補正。Mid（ボーカル）には掛けない。"""

_SEND_HIGHPASS_HZ = 160.0
"""ER・残響・エコーへ送る前に切る低域[Hz]。低音が膨らむのを防ぐ。"""

# --- 会場の寸法（Room Size 0%→100%）[m]
_ROOM_DIMS_MIN = (8.0, 12.0, 4.0)
_ROOM_DIMS_MAX = (60.0, 110.0, 28.0)
"""(幅, 奥行き, 高さ)。ライブハウス 8×12×4m 〜 スタジアム級 60×110×28m。"""

_ER_MAX_ORDER = 2
_ER_MAX_MS = 160.0
_ER_MIN_MS = 3.0
_ER_WALL_ABSORB = 0.72
"""壁 1 回あたりの残存率（吸音率 0.28 相当）。"""
_ER_LEVEL_MAX = 0.7
_ER_DAMP_TAPS = 7
_ER_DAMP_HZ = 6000.0

_AUDIENCE_DISTANCE = {"front": 0.12, "middle": 0.45, "back": 0.82}
"""ステージ前端から客席位置までの、会場奥行きに対する比率。"""
_AUDIENCE_TRIM = {
    # dry, er, wet, echo, dry 高域カットオフ[Hz]（None = 掛けない）
    "front": (1.00, 0.70, 0.65, 0.7, None),
    "middle": (1.00, 1.00, 1.00, 1.0, None),
    "back": (0.94, 1.10, 1.15, 1.10, 7500.0),
}

# --- FDN
_FDN_DELAYS_MS: tuple[float, ...] = (23.9, 29.3, 34.7, 41.3, 47.9, 55.1, 63.7, 73.1)
_FDN_DAMP_TAPS = 9
_FDN_DAMP_HZ_MIN, _FDN_DAMP_HZ_MAX = 2500.0, 9000.0
"""Damping 100% → 2.5kHz、0% → 9kHz。"""
"""低域の膨らみは送り側の HPF（:data:`_SEND_HIGHPASS_HZ`）で防ぐ。ループ内に
低域フィルタを置くと 8 本 × チャンク数の IIR 計算が最大の CPU 負荷になる
（実測 p99 9.6ms）ため、あえて持たない。"""
_DIFFUSER_MS: tuple[float, ...] = (5.3, 8.9)
_DIFFUSER_GAIN_MAX = 0.7
_REVERB_WET_MAX = 0.6

# --- Echo
_ECHO_FEEDBACK_MAX = 0.6
_ECHO_LP_HZ = 3500.0
_ECHO_LEVEL_MAX = 0.5
_ECHO_CROSS = 0.6
"""左右の交差量（ピンポン）。1.0 で完全に左右交互。"""

_DEPTH_DRY_MIN = 0.92
_ROOM_SETTLE_BLOCKS = 2


# ----------------------------------------------------------------------
#  小さな DSP 部品
# ----------------------------------------------------------------------
def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


_LIMIT_KNEE = 0.9


def _soft_limit(out: np.ndarray) -> None:
    """|x| が :data:`_LIMIT_KNEE` を超えた分だけ tanh で丸める（その場で書き換え）。

    ハードクリップの「バリッ」を避ける。通常の音量域（<0.9）には一切触れない。
    """
    mask = np.abs(out) > _LIMIT_KNEE
    if not mask.any():
        return
    over = out[mask]
    room = 1.0 - _LIMIT_KNEE
    out[mask] = np.sign(over) * (_LIMIT_KNEE + room * np.tanh((np.abs(over) - _LIMIT_KNEE) / room))


def _one_pole_alpha(cutoff_hz: float, sample_rate: int) -> float:
    return 1.0 - math.exp(-2.0 * math.pi * cutoff_hz / sample_rate)


class _OnePole:
    """1 次 IIR ローパスをベクトル化して掛ける（状態を持ち越す）。

    ``y[n] = decay^(n+1) y0 + a Σ_k decay^(n-k) x[k]`` を cumsum で計算し、
    ``decay^(-k)`` が発散しないよう :data:`_LOWPASS_CHUNK` ごとに区切る。
    低いカットオフ（数百 Hz）専用。高いカットオフは :class:`_Fir` を使う。
    """

    def __init__(self, cutoff_hz: float, sample_rate: int) -> None:
        self.alpha = _one_pole_alpha(cutoff_hz, sample_rate)
        self.state = 0.0

    def process(self, signal: np.ndarray) -> np.ndarray:
        decay = 1.0 - self.alpha
        out = np.empty(signal.shape[0], dtype=np.float32)
        state = self.state
        for start in range(0, signal.shape[0], _LOWPASS_CHUNK):
            chunk = signal[start : start + _LOWPASS_CHUNK].astype(np.float64)
            powers = np.power(decay, np.arange(chunk.shape[0], dtype=np.float64))
            y = self.alpha * np.cumsum(chunk / powers) * powers + state * powers * decay
            state = float(y[-1])
            out[start : start + _LOWPASS_CHUNK] = y
        self.state = state
        return out


def _fir_lowpass_kernel(cutoff_hz: float, taps: int, sample_rate: int) -> np.ndarray:
    n = np.arange(taps) - (taps - 1) / 2.0
    fc = _clamp(cutoff_hz / sample_rate, 0.001, 0.499)
    kernel = 2.0 * fc * np.sinc(2.0 * fc * n) * np.hanning(taps)
    kernel /= kernel.sum()
    return kernel.astype(np.float32)


class _Fir:
    """状態を持ち越す短い FIR（``np.convolve`` は C 実装で速い）。"""

    def __init__(self, kernel: np.ndarray) -> None:
        self.kernel = kernel
        self._history = np.zeros(kernel.shape[0] - 1, dtype=np.float32)

    def process(self, signal: np.ndarray) -> np.ndarray:
        padded = np.concatenate((self._history, signal))
        self._history = padded[-self._history.shape[0] :]
        return np.convolve(padded, self.kernel, mode="valid").astype(np.float32, copy=False)


class _DelayLine:
    """固定長の遅延線。最後に書いた ``length`` サンプルを配列で持つ。

    読み出しは先頭 ``count`` サンプル（＝ ``n - length``）、書き込みは末尾へ
    連結。``count ≤ length`` を守れば 1 チャンクをまとめて処理できる。
    """

    def __init__(self, length: int) -> None:
        self.length = max(1, int(length))
        self._buffer = np.zeros(self.length, dtype=np.float32)

    def read(self, count: int) -> np.ndarray:
        return self._buffer[:count]

    def write(self, chunk: np.ndarray) -> None:
        self._buffer = np.concatenate((self._buffer[chunk.shape[0] :], chunk))

    def process(self, signal: np.ndarray) -> np.ndarray:
        """任意長の入力を遅らせて返す（帰還なし）。"""
        combined = np.concatenate((self._buffer, signal))
        self._buffer = combined[signal.shape[0] :]
        return combined[: signal.shape[0]]


class _Allpass:
    """Schroeder allpass（拡散用）。``count ≤ length`` のチャンクで処理する。"""

    def __init__(self, length: int) -> None:
        self.line = _DelayLine(length)

    def process(self, signal: np.ndarray, gain: float) -> np.ndarray:
        out = np.empty_like(signal)
        step = self.line.length
        for start in range(0, signal.shape[0], step):
            x = signal[start : start + step]
            delayed = self.line.read(x.shape[0])
            v = x + delayed * gain
            self.line.write(v)
            out[start : start + step] = delayed - v * gain
        return out


class _TapHistory:
    """複数のタップ遅延を 1 本の履歴から読む（初期反射用）。"""

    def __init__(self, max_delay: int) -> None:
        self.max_delay = max(1, max_delay)
        self._history = np.zeros(self.max_delay, dtype=np.float32)

    def push(self, signal: np.ndarray) -> np.ndarray:
        history = np.concatenate((self._history, signal))
        self._history = history[-self.max_delay :]
        return history

    @staticmethod
    def tap(history: np.ndarray, frames: int, delay: int) -> np.ndarray:
        total = history.shape[0]
        return history[total - frames - delay : total - delay]


# ----------------------------------------------------------------------
#  鏡像法による初期反射の設計
# ----------------------------------------------------------------------
def _room_dims(room_size: int) -> tuple[float, float, float]:
    t = _clamp(room_size, PERCENT_MIN, PERCENT_MAX) / 100.0
    # 体積は非線形に増えるので、寸法は t^0.8 で伸ばす
    t = t ** 0.8
    return tuple(a + (b - a) * t for a, b in zip(_ROOM_DIMS_MIN, _ROOM_DIMS_MAX))  # type: ignore[return-value]


def _image_source_taps(
    dims: tuple[float, float, float],
    audience: str,
    sample_rate: int,
    wall_reflect: float = _ER_WALL_ABSORB,
) -> list[tuple[int, float, float, float]]:
    """鏡像法 2 次までの反射を ``(遅延[サンプル], ゲイン, パン -1〜+1, 方位[度])`` で返す。

    座標: x = 幅方向（中央 0）、y = 奥行き（ステージ側 0）、z = 高さ。
    音源はステージ中央の少し上、聴取者は Audience に応じた距離の中央。
    ゲインは直接音（距離 d0）を 1.0 とした相対値 ``d0/d × 残存率^次数``。
    """
    width, length, height = dims
    source = (0.0, 1.5, 1.6)
    ratio = _AUDIENCE_DISTANCE.get(audience, _AUDIENCE_DISTANCE["middle"])
    listener = (0.0, max(3.0, length * ratio), 1.4)
    d0 = math.dist(source, listener)

    taps: list[tuple[int, float, float]] = []
    orders = range(-_ER_MAX_ORDER, _ER_MAX_ORDER + 1)
    for ix in orders:
        for iy in orders:
            for iz in orders:
                order = abs(ix) + abs(iy) + abs(iz)
                if order == 0 or order > _ER_MAX_ORDER:
                    continue
                # 鏡像位置: 偶数回反射は平行移動、奇数回は反転
                sx = (ix * width) + (source[0] if ix % 2 == 0 else -source[0])
                sy = (iy * length) + (source[1] if iy % 2 == 0 else -source[1])
                sz = (iz * height) + (source[2] if iz % 2 == 0 else -source[2])
                dx, dy, dz = sx - listener[0], sy - listener[1], sz - listener[2]
                dist = math.sqrt(dx * dx + dy * dy + dz * dz)
                delay_ms = (dist - d0) / _SPEED_OF_SOUND * 1000.0
                if delay_ms < _ER_MIN_MS or delay_ms > _ER_MAX_MS:
                    continue
                gain = (d0 / dist) * (wall_reflect ** order)
                # 左右方位: 水平面の角度から -1〜+1（正面 0、真横 ±1）
                horiz = math.hypot(dx, dy)
                pan = 0.0 if horiz < 1e-6 else _clamp(dx / horiz * 1.2, -1.0, 1.0)
                # バイノーラル用の方位[度]。聴取者はステージ（y 小）を向いている。
                # 球体頭部モデルは前後対称なので、後方は前方へ折り返す。
                azimuth = math.degrees(math.atan2(dx, -dy)) if horiz >= 1e-6 else 0.0
                if abs(azimuth) > 90.0:
                    azimuth = math.copysign(180.0 - abs(azimuth), azimuth)
                taps.append((int(round(delay_ms * sample_rate / 1000.0)), gain, pan, azimuth))
    taps.sort()
    return taps


# ----------------------------------------------------------------------
#  遅延長で決まる部分をまとめた「部屋」エンジン
# ----------------------------------------------------------------------
class _RoomEngine:
    """ER・Diffuser・FDN・Echo。遅延長は生成時に固定、ゲインは毎ブロック外から渡す。"""

    def __init__(self, sample_rate: int, params: LiveSpaceParams) -> None:
        self.sample_rate = sample_rate
        ms = sample_rate / 1000.0

        # --- Early Reflections（鏡像法）
        dims = _room_dims(params.room_size)
        self.taps = _image_source_taps(
            dims,
            params.audience,
            sample_rate,
            _clamp(params.wall_reflect, 0, 100) / 100.0,
        )
        max_delay = max((d for d, _, _, _ in self.taps), default=1)
        self._er_history = _TapHistory(max_delay)
        self._er_damp = [
            _Fir(_fir_lowpass_kernel(_ER_DAMP_HZ, _ER_DAMP_TAPS, sample_rate)) for _ in range(3)
        ]
        # 反射の合計が直接音より大きくならないよう正規化する
        total = sum(g for _, g, _, _ in self.taps) or 1.0
        self._er_norm = 1.0 / max(1.0, total)
        # タップごとの L/R ゲイン（等パワーパン）
        self._er_lr = [
            (g * math.cos((p + 1.0) * 0.25 * math.pi), g * math.sin((p + 1.0) * 0.25 * math.pi))
            for _, g, p, _ in self.taps
        ]
        # バイノーラル用: 方位ビンごとに (遅延, ゲイン) をまとめる
        self._er_bins: dict[int, list[tuple[int, float]]] = {}
        for delay, g, _, azimuth in self.taps:
            self._er_bins.setdefault(hrtf.nearest_bin(azimuth), []).append((delay, g))

        # --- Reverb
        self._predelay = _DelayLine(max(1, int(round(_clamp(params.predelay_ms, PREDELAY_MIN_MS, PREDELAY_MAX_MS) * ms))))
        room_scale = 0.55 + 1.05 * (_clamp(params.room_size, 0, 100) / 100.0)
        self._diffusers = [_Allpass(max(8, int(round(t * ms)))) for t in _DIFFUSER_MS]
        self.fdn_lengths = [max(64, int(round(t * room_scale * ms))) for t in _FDN_DELAYS_MS]
        self._fdn_lines = [_DelayLine(n) for n in self.fdn_lengths]
        self._fdn_hf: list[_Fir] = []
        self._fdn_hf_hz = -1.0
        self._set_fdn_damping(params.damping)
        self._chunk = min(self.fdn_lengths)
        n = len(self.fdn_lengths)
        h = np.array([[1.0]])
        while h.shape[0] < n:
            h = np.block([[h, h], [h, -h]])
        self._mix = (h / math.sqrt(n)).astype(np.float32)

        # --- Echo（ピンポン）
        echo_samples = max(1, int(round(_clamp(params.echo_time_ms, ECHO_TIME_MIN_MS, ECHO_TIME_MAX_MS) * ms)))
        self._echo_lines = [_DelayLine(echo_samples), _DelayLine(echo_samples)]
        self._echo_lp = [_Fir(_fir_lowpass_kernel(_ECHO_LP_HZ, 9, sample_rate)) for _ in range(2)]
        self._echo_chunk = echo_samples

    # ------------------------------------------------------------------
    def _set_fdn_damping(self, damping: int) -> None:
        t = _clamp(damping, 0, 100) / 100.0
        hz = _FDN_DAMP_HZ_MAX + (_FDN_DAMP_HZ_MIN - _FDN_DAMP_HZ_MAX) * t
        if abs(hz - self._fdn_hf_hz) < 50.0:
            return
        self._fdn_hf_hz = hz
        kernel = _fir_lowpass_kernel(hz, _FDN_DAMP_TAPS, self.sample_rate)
        if self._fdn_hf:
            for f in self._fdn_hf:
                f.kernel = kernel
        else:
            self._fdn_hf = [_Fir(kernel) for _ in self.fdn_lengths]

    def feedback_gains(self, decay_s: float) -> np.ndarray:
        decay_samples = _clamp(decay_s, DECAY_MIN_S, DECAY_MAX_S) * self.sample_rate
        lengths = np.asarray(self.fdn_lengths, dtype=np.float64)
        return np.minimum(np.power(10.0, -3.0 * lengths / decay_samples), 0.985).astype(np.float32)

    # ------------------------------------------------------------------
    def early(self, send: np.ndarray, er_width: float) -> tuple[np.ndarray, np.ndarray]:
        frames = send.shape[0]
        history = self._er_history.push(send)
        left = np.zeros(frames, dtype=np.float32)
        right = np.zeros(frames, dtype=np.float32)
        for (delay, _, _, _), (gl, gr) in zip(self.taps, self._er_lr):
            tap = _TapHistory.tap(history, frames, delay)
            # er_width < 1 なら L/R ゲインを中央へ寄せる（スピーカー用）
            l_gain = gl * er_width + (gl + gr) * 0.5 * (1.0 - er_width)
            r_gain = gr * er_width + (gl + gr) * 0.5 * (1.0 - er_width)
            left += tap * l_gain
            right += tap * r_gain
        return (
            self._er_damp[0].process(left) * self._er_norm,
            self._er_damp[1].process(right) * self._er_norm,
        )

    def early_by_azimuth(self, send: np.ndarray) -> dict[int, np.ndarray]:
        """バイノーラル用。方位ビン → その方向から来る反射の合計（モノ）を返す。

        壁の吸音（ローパス）は線形なので、タップを取る前に送りへ 1 回だけ掛ける。
        """
        frames = send.shape[0]
        damped = self._er_damp[2].process(send) * self._er_norm
        history = self._er_history.push(damped)
        sources: dict[int, np.ndarray] = {}
        for index, taps in self._er_bins.items():
            acc = np.zeros(frames, dtype=np.float32)
            for delay, gain in taps:
                acc += _TapHistory.tap(history, frames, delay) * gain
            sources[index] = acc
        return sources

    def reverb(self, send: np.ndarray, gains: np.ndarray, diffusion: float, damping: int) -> tuple[np.ndarray, np.ndarray]:
        self._set_fdn_damping(damping)
        x_all = self._predelay.process(send)
        for ap in self._diffusers:
            x_all = ap.process(x_all, diffusion * _DIFFUSER_GAIN_MAX)
        frames = x_all.shape[0]
        out_l = np.empty(frames, dtype=np.float32)
        out_r = np.empty(frames, dtype=np.float32)
        for start in range(0, frames, self._chunk):
            stop = min(frames, start + self._chunk)
            x = x_all[start:stop]
            reads = []
            for line, hf, gain in zip(self._fdn_lines, self._fdn_hf, gains):
                reads.append(hf.process(line.read(stop - start)) * gain)
            reads_arr = np.stack(reads)
            mixed = self._mix @ reads_arr
            for i, line in enumerate(self._fdn_lines):
                line.write(mixed[i] + x)
            out_l[start:stop] = reads_arr[0] + reads_arr[2] + reads_arr[4] + reads_arr[6]
            out_r[start:stop] = reads_arr[1] + reads_arr[3] + reads_arr[5] + reads_arr[7]
        scale = 1.0 / math.sqrt(len(self.fdn_lengths) / 2.0)
        return out_l * scale, out_r * scale

    def echo(self, send: np.ndarray, feedback: float, cross: float) -> tuple[np.ndarray, np.ndarray]:
        frames = send.shape[0]
        out_l = np.empty(frames, dtype=np.float32)
        out_r = np.empty(frames, dtype=np.float32)
        for start in range(0, frames, self._echo_chunk):
            stop = min(frames, start + self._echo_chunk)
            x = send[start:stop]
            dl = self._echo_lp[0].process(self._echo_lines[0].read(stop - start))
            dr = self._echo_lp[1].process(self._echo_lines[1].read(stop - start))
            # 左は入力＋右からの戻り、右は左からの戻り（ピンポン）
            self._echo_lines[0].write(x + (dr * cross + dl * (1.0 - cross)) * feedback)
            self._echo_lines[1].write(x * (1.0 - cross) + (dl * cross + dr * (1.0 - cross)) * feedback)
            out_l[start:stop] = dl
            out_r[start:stop] = dr
        return out_l, out_r


# ----------------------------------------------------------------------
#  本体
# ----------------------------------------------------------------------
_ENGINE_KEY_FIELDS = ("room_size", "predelay_ms", "echo_time_ms", "audience")
"""変更すると遅延長が変わり、エンジンを作り直す必要があるパラメータ。"""


class LiveSpaceProcessor:
    """Live Space の本体。:meth:`prepare` → :meth:`process` の順に使う。"""

    def __init__(self) -> None:
        self.enabled = False
        self.output_mode = OUTPUT_MODES[0]
        self.binaural = False
        """True かつ Headphone モードのとき、反射・残響を HRTF で両耳へ描画する。
        直接音には掛けない。"""
        self._renderer_er: hrtf.BinauralRenderer | None = None
        self._renderer_rv: hrtf.BinauralRenderer | None = None
        self._binaural_state = False
        self._binaural_pending: bool | None = None
        self._params = PRESETS[DEFAULT_PRESET]
        self._sample_rate = 0
        self._channels = 0
        self._width_lp: _OnePole | None = None
        self._send_hp: _OnePole | None = None
        self._air_lp: list[_Fir] = []
        self._air_hz = 0.0
        self._engine: _RoomEngine | None = None
        self._engine_key: tuple[Any, ...] = ()
        self._pending: _RoomEngine | None = None
        self._settle = 0
        self._prev_gains: dict[str, float] | None = None

    # ------------------------------------------------------------------
    @property
    def params(self) -> LiveSpaceParams:
        return self._params

    def set_params(self, params: LiveSpaceParams) -> None:
        self._params = params

    def update(self, **changes: object) -> LiveSpaceParams:
        self._params = replace(self._params, **changes)
        return self._params

    def prepare(self, sample_rate: int, channels: int) -> None:
        self._sample_rate = int(sample_rate)
        self._channels = int(channels)
        self.reset()

    def reset(self) -> None:
        self._prev_gains = None
        self._pending = None
        self._settle = 0
        self._engine = None
        self._binaural_state = False
        self._binaural_pending = None
        if self._sample_rate > 0 and self._channels >= 2:
            self._width_lp = _OnePole(_WIDTH_LOW_CUT_HZ, self._sample_rate)
            self._send_hp = _OnePole(_SEND_HIGHPASS_HZ, self._sample_rate)
            self._air_lp = [_Fir(_fir_lowpass_kernel(9000.0, 7, self._sample_rate)) for _ in range(2)]
            self._air_hz = 9000.0
            hrtf_set = hrtf.HrtfSet(self._sample_rate)
            self._renderer_er = hrtf.BinauralRenderer(hrtf_set)
            self._renderer_rv = hrtf.BinauralRenderer(hrtf_set)
            self._engine_key = self._engine_key_of(self._params)
            self._engine = _RoomEngine(self._sample_rate, self._params)

    @property
    def binaural_active(self) -> bool:
        """今のブロックで実際にバイノーラル描画しているか。

        ``binaural`` / ``output_mode`` は GUI からの要求で、切り替えは
        :meth:`_binaural_transition` が 2 ブロックかけて行う（反射・残響を
        一度フェードアウトしてから新しい経路でフェードイン）。
        """
        return self._binaural_state

    def _binaural_transition(self, frames: int) -> np.ndarray | float:
        """要求と状態が違えば切り替え、反射・残響に掛けるフェードを返す。

        切り替えブロックは古い経路のままフェードアウトし、次のブロックで
        新しい経路に切り替えてフェードインする（クリック防止）。
        """
        if self._binaural_pending is not None:
            self._binaural_state = self._binaural_pending
            self._binaural_pending = None
            return np.linspace(0.0, 1.0, frames, dtype=np.float32)
        wanted = self.binaural and self.output_mode == "headphone" and self._renderer_er is not None
        if wanted != self._binaural_state:
            self._binaural_pending = wanted
            return np.linspace(1.0, 0.0, frames, dtype=np.float32)
        return 1.0

    # ------------------------------------------------------------------
    @staticmethod
    def _engine_key_of(p: LiveSpaceParams) -> tuple[Any, ...]:
        return (
            int(p.room_size),
            round(_clamp(p.predelay_ms, PREDELAY_MIN_MS, PREDELAY_MAX_MS), 1),
            round(_clamp(p.echo_time_ms, ECHO_TIME_MIN_MS, ECHO_TIME_MAX_MS), 0),
            p.audience if p.audience in AUDIENCE_POSITIONS else "middle",
            int(p.wall_reflect),
        )

    def _target_gains(self) -> dict[str, float]:
        p = self._params
        speaker = self.output_mode == "speaker"
        trim_dry, trim_er, trim_wet, trim_echo, air_hz = _AUDIENCE_TRIM.get(
            p.audience, _AUDIENCE_TRIM["middle"]
        )
        mix = _clamp(p.dry_wet, 0, 100) / 100.0

        width = _clamp(p.width, WIDTH_MIN, WIDTH_MAX) / 100.0 * (0.75 if speaker else 1.0)
        w = 1.0 + width * _WIDTH_SIDE_GAIN_PER_100
        comp = 1.0 / (1.0 + _WIDTH_LOUDNESS_COMP * (w - 1.0))

        depth = _clamp(p.depth, 0, 100) / 100.0
        dry = (1.0 - (1.0 - _DEPTH_DRY_MIN) * depth * mix) * trim_dry
        boost = 1.0 + 0.25 * depth

        er = _ER_LEVEL_MAX * (_clamp(p.early, 0, 100) / 100.0) * boost * trim_er * mix
        wet = _REVERB_WET_MAX * (_clamp(p.reverb, 0, 100) / 100.0) * boost * trim_wet * mix
        if speaker:
            wet *= 0.9
        echo = _ECHO_LEVEL_MAX * (_clamp(p.echo, 0, 100) / 100.0) * trim_echo * mix
        vocal = _clamp(p.vocal_center, WIDTH_MIN, WIDTH_MAX) / 100.0
        instrument = _clamp(p.instrument_level, WIDTH_MIN, WIDTH_MAX) / 100.0
        return {
            "side": w * comp,
            "mid": 1.0,
            "vocal": vocal,
            "instrument": instrument,
            "dry": dry,
            "er": er,
            "wet": wet,
            "echo": echo,
            "echo_fb": _ECHO_FEEDBACK_MAX * _clamp(p.echo_feedback, 0, 100) / 100.0,
            "diffusion": _clamp(p.diffusion, 0, 100) / 100.0,
            "er_width": 0.75 if speaker else 1.0,
            "echo_cross": _ECHO_CROSS * (0.6 if speaker else 1.0),
            "air_hz": air_hz or 0.0,
        }

    @staticmethod
    def _ramp(prev: float, target: float, frames: int) -> np.ndarray | float:
        if prev == target:
            return target
        return np.linspace(prev, target, frames, dtype=np.float32)

    # ------------------------------------------------------------------
    def process(self, block: np.ndarray) -> np.ndarray:
        """1 ブロックを処理して返す。OFF・モノラル・空なら ``block`` をそのまま返す。"""
        if not self.enabled or self._channels < 2 or self._engine is None or block.shape[0] == 0:
            self._prev_gains = None
            return block
        assert self._width_lp is not None and self._send_hp is not None

        frames = block.shape[0]
        target = self._target_gains()
        prev = self._prev_gains or target
        self._prev_gains = target
        ramp = {k: self._ramp(prev[k], target[k], frames) for k in ("side", "mid", "vocal", "instrument", "dry", "er", "wet", "echo")}

        left = block[:, 0]
        right = block[:, 1]
        mid = (left + right) * 0.5
        side = (left - right) * 0.5

        # ---- Direct（Width、Audience=BACK の空気吸収）
        side_low = self._width_lp.process(side)
        side_out = ((side - side_low) * ramp["side"] + side_low * ramp["mid"]) * ramp["instrument"]
        mid_out = mid * ramp["mid"] * ramp["vocal"]
        dry_l = mid_out + side_out
        dry_r = mid_out - side_out
        if target["air_hz"] > 0.0:
            if abs(target["air_hz"] - self._air_hz) > 50.0:
                kernel = _fir_lowpass_kernel(target["air_hz"], 7, self._sample_rate)
                for f in self._air_lp:
                    f.kernel = kernel
                self._air_hz = target["air_hz"]
            dry_l = self._air_lp[0].process(dry_l)
            dry_r = self._air_lp[1].process(dry_r)
        dry_l = dry_l * ramp["dry"]
        dry_r = dry_r * ramp["dry"]

        # ---- 送り（モノ化 → 低域カット）
        send = mid - self._send_hp.process(mid)

        engine = self._resolve_engine()
        binaural_fade = self._binaural_transition(frames)
        er_l, er_r, rv_l, rv_r, ec_l, ec_r = self._run_engine(engine, send, target)

        if self._pending is not None:
            n = self._run_engine(self._pending, send, target)
            fade_in = np.linspace(0.0, 1.0, frames, dtype=np.float32)
            fade_out = 1.0 - fade_in
            er_l = er_l * fade_out + n[0] * fade_in
            er_r = er_r * fade_out + n[1] * fade_in
            rv_l = rv_l * fade_out + n[2] * fade_in
            rv_r = rv_r * fade_out + n[3] * fade_in
            ec_l = ec_l * fade_out + n[4] * fade_in
            ec_r = ec_r * fade_out + n[5] * fade_in
            self._engine = self._pending
            self._pending = None

        er_gain = ramp["er"] * binaural_fade
        wet_gain = ramp["wet"] * binaural_fade
        out = np.empty_like(block)
        out[:, 0] = dry_l + er_l * er_gain + rv_l * wet_gain + ec_l * ramp["echo"]
        out[:, 1] = dry_r + er_r * er_gain + rv_r * wet_gain + ec_r * ramp["echo"]
        if self._channels > 2:
            out[:, 2:] = block[:, 2:]
        _soft_limit(out)
        return out

    def _run_engine(self, engine: _RoomEngine, send: np.ndarray, target: dict[str, float]) -> tuple[np.ndarray, ...]:
        p = self._params
        rv_l, rv_r = engine.reverb(send, engine.feedback_gains(p.decay_s), target["diffusion"], p.damping)
        ec_l, ec_r = engine.echo(send, target["echo_fb"], target["echo_cross"])
        if self.binaural_active:
            assert self._renderer_er is not None and self._renderer_rv is not None
            frames = send.shape[0]
            # 反射はそれぞれの方位から、残響は左右 ±90° から両耳へ描画する。
            # 各方位の音は FIR（線形）に通す前に足し合わせるので、畳み込みは
            # 方位数 × 2 回で済む。ゲイン（ramp）は process 側で従来どおり掛ける。
            er_l, er_r = self._renderer_er.render(engine.early_by_azimuth(send), frames)
            rv_l, rv_r = self._renderer_rv.render(
                {0: rv_l, len(hrtf.AZIMUTH_BINS) - 1: rv_r}, frames
            )
            return er_l, er_r, rv_l, rv_r, ec_l, ec_r
        er_l, er_r = engine.early(send, target["er_width"])
        return er_l, er_r, rv_l, rv_r, ec_l, ec_r

    def _resolve_engine(self) -> _RoomEngine:
        assert self._engine is not None
        key = self._engine_key_of(self._params)
        if key == self._engine_key:
            self._settle = 0
            return self._engine
        self._settle += 1
        if self._settle >= _ROOM_SETTLE_BLOCKS and self._pending is None:
            self._pending = _RoomEngine(self._sample_rate, self._params)
            self._engine_key = key
            self._settle = 0
        return self._engine


# ----------------------------------------------------------------------
#  設定の読み書き補助
# ----------------------------------------------------------------------
def preset_name_for(params: LiveSpaceParams) -> str:
    """一致するプリセット名、なければ ``Custom``。"""
    for name, preset in PRESETS.items():
        if preset == params:
            return name
    return CUSTOM_PRESET


def params_to_dict(params: LiveSpaceParams) -> dict[str, Any]:
    return {f.name: getattr(params, f.name) for f in fields(params)}


def params_from_dict(data: dict[str, Any], base: LiveSpaceParams | None = None) -> LiveSpaceParams:
    """辞書から復元する。未知キーは無視、欠けたキーと範囲外は ``base``/既定へ丸める。"""
    base = base or PRESETS[DEFAULT_PRESET]
    ranges: dict[str, tuple[float, float]] = {
        "width": (WIDTH_MIN, WIDTH_MAX),
        "vocal_center": (WIDTH_MIN, WIDTH_MAX),
        "instrument_level": (WIDTH_MIN, WIDTH_MAX),
        "predelay_ms": (PREDELAY_MIN_MS, PREDELAY_MAX_MS),
        "decay_s": (DECAY_MIN_S, DECAY_MAX_S),
        "echo_time_ms": (ECHO_TIME_MIN_MS, ECHO_TIME_MAX_MS),
    }
    values: dict[str, Any] = {}
    for f in fields(base):
        raw = data.get(f.name, getattr(base, f.name))
        if f.name == "audience":
            values[f.name] = raw if raw in AUDIENCE_POSITIONS else base.audience
            continue
        try:
            number = float(raw)
        except (TypeError, ValueError):
            number = float(getattr(base, f.name))
        if isinstance(raw, bool) or not math.isfinite(number):
            # NaN は _clamp を素通りして上限値に化けるため、既定へ戻す
            number = float(getattr(base, f.name))
        low, high = ranges.get(f.name, (PERCENT_MIN, PERCENT_MAX))
        number = _clamp(number, low, high)
        values[f.name] = float(number) if f.type == "float" else int(round(number))
    return LiveSpaceParams(**values)
