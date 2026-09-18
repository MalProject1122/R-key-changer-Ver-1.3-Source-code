"""ヘッドホン用のバイノーラル描画（HRTF）モジュール。

:mod:`audio.live_space` の初期反射・残響を「その方向から両耳に届く音」に
する。直接音には掛けない（ボーカルの音色を変えないため）。

■ 球体頭部モデル（Brown & Duda 1998）

実測 HRTF（MIT KEMAR 等）の代わりに、頭を半径 8.75cm の球とみなして
**両耳の時間差（ITD）** と **頭の影による高域減衰（ILD）** を計算する。

* ITD: Woodworth の式 ``a/c · (θ + sin θ)``（θ は正中面からの角度）
* ILD: 遠い側の耳ほど高域が落ちる 1 次シェルフ
  ``H(ω) = (1 + jαω/2ω0) / (1 + jω/2ω0)``、``ω0 = c/a``、
  ``α(θ) = 1.05 + 0.95·cos(θ_ear · 180/150)``

個人の耳介形状を含まないので上下の定位は出ないが、左右・前後の
広がりと「頭の外で鳴っている」感覚の大半はこれで得られ、個人差で
「合わない」人が少ない。データ同梱も不要。

■ 設計

方位を :data:`AZIMUTH_BINS` 段階に量子化し、方位ごとに L/R の短い FIR
（:data:`KERNEL_TAPS` タップ、周波数サンプリング法で設計）を持つ。
同じ方位の音はまとめてから畳み込むので、初期反射が何本あっても
畳み込みは「方位数 × 2」で済む。

将来、実測 HRTF を使う場合は :class:`HrtfSet` と同じ ``kernels(azimuth)``
を返すクラスを用意して :class:`BinauralRenderer` に渡すだけでよい。
"""

from __future__ import annotations

import math

import numpy as np

HEAD_RADIUS_M = 0.0875
SPEED_OF_SOUND = 343.0
KERNEL_TAPS = 96
"""FIR の長さ。ITD 最大 ~0.7ms（48kHz で 34 サンプル）を含めて余裕を持たせる。"""

AZIMUTH_BINS: tuple[float, ...] = (-90.0, -60.0, -30.0, 0.0, 30.0, 60.0, 90.0)
"""量子化する方位[度]（負 = 左、正 = 右、0 = 正面）。"""


def nearest_bin(azimuth_deg: float) -> int:
    """方位[度]に最も近い :data:`AZIMUTH_BINS` の添字を返す。"""
    azimuth = max(-90.0, min(90.0, azimuth_deg))
    return min(range(len(AZIMUTH_BINS)), key=lambda i: abs(AZIMUTH_BINS[i] - azimuth))


class HrtfSet:
    """球体頭部モデルから方位ごとの L/R FIR を作る。"""

    def __init__(self, sample_rate: int, taps: int = KERNEL_TAPS) -> None:
        self.sample_rate = int(sample_rate)
        self.taps = taps
        self._cache: dict[int, tuple[np.ndarray, np.ndarray]] = {}

    def kernels(self, azimuth_deg: float) -> tuple[np.ndarray, np.ndarray]:
        """方位に対応する ``(左耳 FIR, 右耳 FIR)`` を返す（量子化してキャッシュ）。"""
        index = nearest_bin(azimuth_deg)
        if index not in self._cache:
            azimuth = AZIMUTH_BINS[index]
            left = self._ear_response(azimuth, ear=-1.0)
            right = self._ear_response(azimuth, ear=+1.0)
            # 両耳の合計パワーが方位によらず一定になるよう正規化する
            # （横方向の音だけ大きく／明るくならないように）。ILD の比は保たれる。
            power = np.sqrt((np.abs(left) ** 2 + np.abs(right) ** 2) / 2.0)
            norm = float(np.sqrt((power**2).mean()))
            self._cache[index] = (
                self._to_kernel(left / norm),
                self._to_kernel(right / norm),
            )
        return self._cache[index]

    def _ear_response(self, azimuth_deg: float, ear: float) -> np.ndarray:
        """片耳分の周波数応答（ITD の位相を含む）を返す。

        Args:
            azimuth_deg: 音源の方位[度]（右が正）。
            ear: 左耳 -1.0、右耳 +1.0。
        """
        theta_src = math.radians(azimuth_deg)
        # 音源から見た「その耳」の角度。0 = 耳の正面（同側）、180 = 反対側
        theta_ear = math.acos(max(-1.0, min(1.0, math.sin(theta_src) * ear)))
        theta_ear_deg = math.degrees(theta_ear)

        # ITD（Woodworth）: 両耳差 = a/c · (θ + sin θ)。同側を早く、反対側を
        # 遅くし、その中央を基準にする。
        theta_abs = abs(theta_src)
        itd = HEAD_RADIUS_M / SPEED_OF_SOUND * (theta_abs + math.sin(theta_abs))
        same_side = (azimuth_deg < 0) == (ear < 0) or azimuth_deg == 0.0
        delay = -itd / 2.0 if same_side else itd / 2.0
        if azimuth_deg == 0.0:
            delay = 0.0
        delay_samples = delay * self.sample_rate

        # ILD シェルフ（Brown & Duda）。低域は 1.0、高域は同側 ~2 倍・反対側 ~0.1 倍。
        alpha = 1.05 + 0.95 * math.cos(math.radians(theta_ear_deg * 180.0 / 150.0))
        w0 = SPEED_OF_SOUND / HEAD_RADIUS_M
        w = 2.0 * math.pi * np.fft.rfftfreq(2 * self.taps, 1.0 / self.sample_rate)
        response = (1.0 + 1j * alpha * w / (2.0 * w0)) / (1.0 + 1j * w / (2.0 * w0))
        center = self.taps / 2.0 + delay_samples
        return response * np.exp(-1j * w * center / self.sample_rate)

    def _to_kernel(self, response: np.ndarray) -> np.ndarray:
        kernel = np.fft.irfft(response, 2 * self.taps)[: self.taps]
        kernel *= np.hanning(self.taps)
        return kernel.astype(np.float32)


class _FirState:
    def __init__(self, taps: int) -> None:
        self.history = np.zeros(taps - 1, dtype=np.float32)

    def process(self, signal: np.ndarray, kernel: np.ndarray) -> np.ndarray:
        padded = np.concatenate((self.history, signal))
        self.history = padded[-self.history.shape[0] :]
        return np.convolve(padded, kernel, mode="valid").astype(np.float32, copy=False)


class BinauralRenderer:
    """方位ごとにまとめたモノ信号を、両耳のステレオへ描画する。

    :meth:`render` に「方位ビン → その方位の音（1 次元）」の辞書を渡すと、
    各方位を L/R の FIR で畳み込んで足し合わせた ``(左, 右)`` を返す。
    """

    def __init__(self, hrtf: HrtfSet) -> None:
        self.hrtf = hrtf
        self._states: dict[int, tuple[_FirState, _FirState]] = {}

    def render(self, sources: dict[int, np.ndarray], frames: int) -> tuple[np.ndarray, np.ndarray]:
        left = np.zeros(frames, dtype=np.float32)
        right = np.zeros(frames, dtype=np.float32)
        for index, signal in sources.items():
            kl, kr = self.hrtf.kernels(AZIMUTH_BINS[index])
            if index not in self._states:
                self._states[index] = (_FirState(self.hrtf.taps), _FirState(self.hrtf.taps))
            sl, sr = self._states[index]
            left += sl.process(signal, kl)
            right += sr.process(signal, kr)
        return left, right

    @property
    def latency_samples(self) -> int:
        """FIR の中央遅延[サンプル]（反射・残響にだけ掛かる。直接音は不変）。"""
        return self.hrtf.taps // 2
