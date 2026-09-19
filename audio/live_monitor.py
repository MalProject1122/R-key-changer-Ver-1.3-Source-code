"""パソコンで再生中の音を、リアルタイムにキー変更して聴くモジュール。

Apple Music・YouTube・ブラウザなど、**手元に音声ファイルが無い音源**を
半音単位で移調しながら聴くためのもの。録音も保存もダウンロードもしない
（すでに復号されて鳴っている PCM をその場で加工し、鳴らし直すだけ）。

■ 前提となる配線（VB-CABLE 方式）

::

    再生アプリ ─▶ CABLE Input（＝Windows の既定の出力）
                        │  （仮想ケーブル）
                        ▼
                  CABLE Output ─▶ このモジュール ─▶ ヘッドホン/スピーカー
                    （録音側）      キー変更            （実機）

ポイントは **既定の出力を CABLE Input にする**こと。こうすると元の音は
スピーカーへ直接届かなくなり、耳に入るのはこのモジュールが出した
加工後の音だけになる。

ステレオミキサー方式（鳴っている音をコピーして横取りする）も試したが、
あちらは元のスピーカー再生を止めないため、「元の音」と「キー変更後の
音」が同時に鳴ってしまい、移調が聞き取れない。VB-CABLE 方式なら
この二重再生が構造的に起こらない。

■ 実装上の落とし穴（実機で踏んだもの）

1. ``sounddevice`` 0.5.5 の ``WasapiSettings`` には ``loopback``
   引数が無い。WASAPI ループバックで直接横取りする経路は使えない。
2. 入力と出力を 1 本の ``sounddevice.Stream`` にまとめると
   ``Illegal combination of I/O devices [PaErrorCode -9993]`` で
   開けないことがある（入力が WDM-KS 方式のときに頻出）。
   そのため **入力用と出力用のストリームを別々に開き**、
   :class:`_RingPitchShifter` を挟んで橋渡しする。
3. 同じデバイスが MME / DirectSound / WASAPI / WDM-KS の 4 方式で
   重複して列挙される。WDM-KS は単体でも ``Invalid device`` で
   開けないことがあるため、自動選択では後回しにする。

■ 遅延について

WSOLA は「これから鳴る波形」を少し先まで見ないと継ぎ目を選べないので、
:data:`_LEAD` サンプル分（48kHz で約 85ms）だけ先読みの遅延が出る。
これに出力バッファの遅延が加わる。音楽を聴く用途では気にならないが、
自分の声と重ねて聴く用途には向かない。
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any

import numpy as np

import config
from audio.audio_input import AudioInputError
from audio.audio_input import _import_sounddevice as _load_sounddevice
from audio.equalizer import Equalizer
from audio.live_space import LiveSpaceProcessor
from audio.time_stretch import TimeStretcher

logger = logging.getLogger(__name__)

KEY_SHIFT_MIN = -12
KEY_SHIFT_MAX = 12
"""キー変更で選べる半音数の範囲（0 = 原曲）。

:mod:`video.video_analysis` の同名の定数と同じ幅（カラオケ機と同じ
±12）にそろえてある。片方だけ広げると、動画とライブで操作感が
食い違ってしまう。
"""

MAX_CHANNELS = 2
"""同時に処理するチャンネル数の上限。音楽を聴く用途なのでステレオまで。"""

_BLOCK_SIZE = 2048
"""コールバック 1 回あたりのサンプル数。48kHz で約 43ms。

小さくすると遅延は減るが、コールバックの回数が増えて取りこぼしやすい。
:data:`config.BLOCK_SIZE`（解析用）とは目的が違うので別に持つ。

Developer版での検証で、1024→2048にすると音飛びが明確に減り
（平均9〜10回→6回）、2048→4096ではわずかな改善（6→5回）に留まる
一方で遅延が約43ms→85msへ倍増したため、2048を既定にしている。
環境（PC・オーディオデバイス）によって最適値は変わるため、⚙ 詳細設定
から選び直せる（:data:`BLOCK_SIZE_OPTIONS` 参照）。
"""

BLOCK_SIZE_OPTIONS: tuple[int, ...] = (1024, 2048, 4096)
"""設定画面（⚙ 詳細設定）で選べるコールバックサイズの候補。

環境（PC・オーディオデバイス）によって音飛びの起きやすさが変わるため、
既定の :data:`_BLOCK_SIZE` だけでなく利用者が選べるようにしてある。
値の意味・トレードオフは :data:`_BLOCK_SIZE` のdocstring参照。
"""

_GRAIN = 2048
_SEARCH = 256
"""WSOLA のグレイン長と継ぎ目の探索範囲[サンプル]。

:class:`audio.time_stretch.TimeStretcher` の既定値と同じ。動画のキー変更
で音質を確認済みの値なので、ライブでもそのまま使う。
"""

_LEAD = _GRAIN * 2
"""出力より先に溜めておく入力サンプル数（＝先読み遅延）。

WSOLA は読み取り位置から ``grain + search`` 先までを覗く。足りないまま
処理すると無音で埋められて「プチッ」と鳴るので、余裕を見てグレイン
2 個分を常に確保する。48kHz なら約 85ms。
"""

_MAX_LAG = _LEAD * 4
"""許容する入力の溜まりすぎ[サンプル]。

入力と出力は別々のストリーム＝**別々のクロック**で動くため、長く回すと
少しずつずれて入力側が溜まっていく。これを超えたら古い音を捨てて
遅延を戻す（:meth:`_RingPitchShifter._resync`）。
"""

_INBOX_MAX_BLOCKS = 64
"""入力キューに溜めておける最大ブロック数。

出力側が止まる（ヘッドホンを抜いた等）と入力だけが積まれ続け、上限が無いと
1 時間で 1GB を超える（実測 22MB/分）。通常は出力コールバックのたびに空に
なるので、ここに届くのは異常時だけ。4096 サンプルでも約 5 秒・約 2MB。
"""

_STALL_TIMEOUT_S = 2.0
"""入出力どちらかのコールバックがこの秒数届かなければ、デバイスとの接続が
切れたとみなして停止する（:meth:`LiveMonitor.check_stalled`）。最大の
コールバック間隔（4096 サンプル / 44.1kHz ≒ 93ms）より十分長くしてある。
"""

_INPUT_KEYWORDS = (
    "CABLE Output",
    "ステレオ ミキサー",
    "ステレオミキサー",
    "Stereo Mix",
)
"""入力デバイスを自動で選ぶときに優先する名前（前ほど優先）。"""

_LOOPBACK_MARK = "CABLE"
"""仮想ケーブルのデバイス名に必ず含まれる文字列。

出力先の自動選択から除外するのと、``CABLE Input`` へ出しながら
``CABLE Output`` から録る（＝音が回り続ける）配線を弾くのに使う。
"""

_WDM_KS = "WDM-KS"
"""後回しにするホスト API 名の断片（落とし穴 3 を参照）。"""

_HOSTAPI_PRIORITY = ("WASAPI", "DirectSound", "MME")
"""自動選択で優先するホスト API の順（前のほうを優先）。

WASAPI が最も低遅延で安定しており、実機でも一貫して動作した。
WDM-KS はここに含めない＝一覧に無いので最下位（:func:`_hostapi_rank`
参照）として扱われ、落とし穴 3 の対策を兼ねる。同じデバイス名が複数の
方式で重複列挙されたとき、毎回同じ方式を選ぶようにして選択結果が
起動のたびにぶれないようにする。
"""


def _hostapi_rank(hostapi: str) -> int:
    """ホスト API 名を、自動選択での優先順位（小さいほど優先）に直す。

    Args:
        hostapi: ``sounddevice`` が返すホスト API 名。

    Returns:
        :data:`_HOSTAPI_PRIORITY` の中での順位。該当が無ければ
        （WDM-KS 含む）リストの長さ＝最下位を返す。
    """
    for rank, name in enumerate(_HOSTAPI_PRIORITY):
        if name in hostapi:
            return rank
    return len(_HOSTAPI_PRIORITY)


class LiveMonitorError(RuntimeError):
    """再生中の音のキー変更まわりの失敗を表す例外。"""

    code = "E202"


def _import_sounddevice() -> Any:
    """sounddevice を遅延 import する。

    :mod:`audio.audio_input` と同じ判定をそのまま使い、例外だけこの
    モジュールのものに詰め替える（GUI 側が 1 種類の例外を受け止めれば
    済むようにするため）。

    Returns:
        sounddevice モジュール。

    Raises:
        LiveMonitorError: 未導入の場合。
    """
    try:
        return _load_sounddevice()
    except AudioInputError as exc:
        raise LiveMonitorError(str(exc)) from exc


# ----------------------------------------------------------------------
#  デバイス一覧
# ----------------------------------------------------------------------
def _host_api_names(sounddevice: Any) -> list[str]:
    """ホスト API 番号 → 名前の対応表を返す。

    Args:
        sounddevice: sounddevice モジュール。

    Returns:
        ホスト API 名のリスト（添字がホスト API 番号）。
    """
    try:
        return [str(api.get("name", "")) for api in sounddevice.query_hostapis()]
    except Exception:  # noqa: BLE001 - 表示用のため失敗しても続行
        return []


def _list_devices(kind: str) -> list[dict[str, Any]]:
    """入力または出力デバイスの一覧を返す。

    Args:
        kind: ``"input"`` か ``"output"``。

    Returns:
        ``{"index", "name", "hostapi", "channels", "is_default", "label"}``
        の辞書のリスト。取得に失敗した場合は空リスト。
    """
    try:
        sounddevice = _import_sounddevice()
        devices = sounddevice.query_devices()
        default_index = sounddevice.default.device[0 if kind == "input" else 1]
    except Exception:  # noqa: BLE001 - 一覧取得は失敗しても続行
        return []

    api_names = _host_api_names(sounddevice)
    channel_key = f"max_{kind}_channels"
    result: list[dict[str, Any]] = []
    for index, info in enumerate(devices):
        channels = int(info.get(channel_key, 0))
        if channels <= 0:
            continue
        api_index = int(info.get("hostapi", -1))
        hostapi = api_names[api_index] if 0 <= api_index < len(api_names) else ""
        name = str(info.get("name", f"device {index}"))
        result.append(
            {
                "index": index,
                "name": name,
                "hostapi": hostapi,
                "channels": channels,
                "is_default": index == default_index,
                "label": f"{name}  [{hostapi}]" if hostapi else name,
            }
        )
    return result


def rescan_devices() -> None:
    """PortAudio にデバイス一覧を取り直させる。**停止中にだけ**呼ぶこと。

    PortAudio はデバイス一覧を初期化時に一度だけ作るため、起動後に
    抜き差ししたデバイス（USB ヘッドホン等）は、再初期化しないと一覧に
    現れない・古い番号のまま残る。開いているストリームがあると再初期化で
    壊れるので、呼び出し側（「一覧を更新」）が停止中であることを保証する。
    失敗しても一覧が古いままになるだけなので、例外は外へ出さない。
    """
    try:
        sounddevice = _import_sounddevice()
        sounddevice._terminate()  # noqa: SLF001 - 公開 API に再スキャンが無いため
        sounddevice._initialize()  # noqa: SLF001
    except Exception:  # noqa: BLE001
        logger.warning("デバイス一覧の再スキャンに失敗しました", exc_info=True)


def list_input_devices() -> list[dict[str, Any]]:
    """録音（横取り）に使えるデバイスの一覧を返す。

    Returns:
        :func:`_list_devices` と同じ形式のリスト。
    """
    return _list_devices("input")


def list_output_devices() -> list[dict[str, Any]]:
    """再生に使えるデバイスの一覧を返す。

    Returns:
        :func:`_list_devices` と同じ形式のリスト。
    """
    return _list_devices("output")


def suggest_input_device(devices: list[dict[str, Any]] | None = None) -> int | None:
    """入力に使いそうなデバイスを推測する。

    ``CABLE Output``（VB-CABLE の録音側）を最優先で探す。見つからない
    ときはステレオミキサーを探し、それも無ければ None を返して利用者に
    選ばせる。同じ名前が複数のホスト API で重複列挙されている場合は
    :data:`_HOSTAPI_PRIORITY` の順で選ぶ（WASAPI優先、WDM-KSは最下位。
    起動のたびに選択がぶれないようにするため）。

    Args:
        devices: 候補。None なら :func:`list_input_devices` で取得する。

    Returns:
        デバイス番号。候補が無ければ None。
    """
    candidates = list_input_devices() if devices is None else devices
    for keyword in _INPUT_KEYWORDS:
        matched = [
            device for device in candidates if keyword.lower() in device["name"].lower()
        ]
        if not matched:
            continue
        matched.sort(key=lambda device: _hostapi_rank(device["hostapi"]))
        return int(matched[0]["index"])
    return None


def suggest_output_device(devices: list[dict[str, Any]] | None = None) -> int | None:
    """出力に使いそうなデバイスを推測する。

    **既定の出力デバイスは当てにできない。** VB-CABLE 方式では既定の
    出力が ``CABLE Input`` になっているので、そのまま使うと音が仮想
    ケーブルへ戻ってしまう。名前に ``CABLE`` を含むものは必ず外し、
    実機のスピーカー／ヘッドホンを選ぶ。

    Args:
        devices: 候補。None なら :func:`list_output_devices` で取得する。

    Returns:
        デバイス番号。候補が無ければ None。
    """
    candidates = [
        device
        for device in (list_output_devices() if devices is None else devices)
        if _LOOPBACK_MARK.lower() not in device["name"].lower()
    ]
    if not candidates:
        return None
    candidates.sort(
        key=lambda device: (
            _hostapi_rank(device["hostapi"]),
            not device["is_default"],
            device["index"],
        )
    )
    return int(candidates[0]["index"])


# ----------------------------------------------------------------------
#  ライブ用のピッチシフター
# ----------------------------------------------------------------------
class _RecordingStretcher(TimeStretcher):
    """継ぎ目として選んだ位置を記録する :class:`TimeStretcher`。

    ステレオを左右バラバラに WSOLA すると、継ぎ目の探索結果が左右で
    食い違い、定位がふらつく。そこで片方（左）で選んだ位置を記録し、
    もう片方に同じ位置を使わせる（:class:`_FollowingStretcher`）。

    Attributes:
        offsets: 本来の位置からのずれ[サンプル]。グレイン 1 個につき 1 個。
    """

    def __init__(self, **kwargs: Any) -> None:
        """_RecordingStretcher を初期化する。

        Args:
            **kwargs: :class:`TimeStretcher` へそのまま渡す。
        """
        super().__init__(**kwargs)
        self.offsets: list[int] = []

    def begin(self) -> None:
        """記録を捨てる。``pull()`` を呼ぶ直前に呼ぶ。"""
        self.offsets.clear()

    def _best_offset(self, audio: np.ndarray, center: int) -> int:
        """継ぎ目を探し、選んだずれを記録してから返す。

        Args:
            audio: 元の波形。
            center: 本来読むべき位置。

        Returns:
            :class:`TimeStretcher` が選んだ位置。
        """
        best = super()._best_offset(audio, center)
        self.offsets.append(best - center)
        return best


class _FollowingStretcher(TimeStretcher):
    """他チャンネルが選んだ継ぎ目をそのまま使う :class:`TimeStretcher`。

    自分では探索しないので、左右で同じだけ波形がずれる（＝定位が
    保たれる）。ずれの列が足りない場合は探索なし（素朴な OLA）に
    落ちるだけで、音が途切れることはない。
    """

    def __init__(self, **kwargs: Any) -> None:
        """_FollowingStretcher を初期化する。

        Args:
            **kwargs: :class:`TimeStretcher` へそのまま渡す。
        """
        super().__init__(**kwargs)
        self._deltas: list[int] = []
        self._cursor = 0

    def follow(self, deltas: list[int]) -> None:
        """次の ``pull()`` で使うずれの列を受け取る。

        Args:
            deltas: :attr:`_RecordingStretcher.offsets`。
        """
        self._deltas = deltas
        self._cursor = 0

    def _best_offset(self, audio: np.ndarray, center: int) -> int:  # noqa: ARG002
        """記録されたずれを順に適用する。

        Args:
            audio: 元の波形（探索しないので使わない）。
            center: 本来読むべき位置。

        Returns:
            ずれを足した位置。列を使い切っていれば ``center`` のまま。
        """
        if self._cursor >= len(self._deltas):
            return center
        delta = self._deltas[self._cursor]
        self._cursor += 1
        return center + delta


class _RingPitchShifter:
    """流れ続ける音を、溜めながらキー変更するバッファ。

    :class:`TimeStretcher` は「波形全体」を渡す前提なので、そのままでは
    終わりの無いライブ入力に使えない。そこで直近の入力だけを保持する
    スライド窓を持ち、読み取り位置が窓の中に収まるよう面倒を見る。

    * 先読みが :data:`_LEAD` に満たない間は無音を返す（起動直後と、
      入力が途切れたとき）
    * 溜まりすぎたら古い音を捨てて遅延を戻す（入出力のクロック差）

    Attributes:
        channels: 扱うチャンネル数。
        starved: 先読み不足で無音を返した回数。
        resyncs: 溜まりすぎて古い音を捨てた回数。
    """

    def __init__(
        self,
        channels: int = 1,
        grain: int = _GRAIN,
        search: int = _SEARCH,
        lead: int = _LEAD,
        max_lag: int = _MAX_LAG,
    ) -> None:
        """_RingPitchShifter を初期化する。

        Args:
            channels: 扱うチャンネル数（1 か 2）。
            grain: WSOLA のグレイン長[サンプル]。
            search: 継ぎ目の探索範囲[サンプル]。
            lead: 常に確保する先読み量[サンプル]。
            max_lag: 許容する溜まりすぎ[サンプル]。
        """
        self.channels = max(1, channels)
        self.starved = 0
        self.resyncs = 0

        self._grain = grain
        self._lead = lead
        self._max_lag = max_lag
        # 読み取り位置より手前に残しておく長さ。継ぎ目の探索が
        # center - search まで遡るので、それを下回らせない。
        self._history = grain + search

        self._leader = _RecordingStretcher(grain=grain, search=search)
        self._followers = [
            _FollowingStretcher(grain=grain, search=search)
            for _ in range(self.channels - 1)
        ]
        self._buffers = [np.zeros(0, dtype=np.float32) for _ in range(self.channels)]
        self._base = 0
        """``_buffers[*][0]`` が入力全体の何サンプル目かを表す絶対位置。"""
        self._written = 0
        """受け取った入力の総サンプル数（絶対位置）。"""
        self._read = 0.0
        """次に読む絶対位置。小数になるのはキー変更で再サンプルするため。"""

    @property
    def lag(self) -> float:
        """まだ出力していない入力の量[サンプル]を返す。

        Returns:
            入力の絶対位置と読み取り位置の差。
        """
        return self._written - self._read

    def push(self, block: np.ndarray) -> None:
        """入力ブロックを溜める。

        Args:
            block: 形 ``(サンプル数, チャンネル数)`` の波形。チャンネルが
                足りない場合は最後のチャンネルを複製して埋める。
        """
        frames = block.shape[0]
        if frames == 0:
            return
        last = block.shape[1] - 1
        for channel in range(self.channels):
            source = np.asarray(block[:, min(channel, last)], dtype=np.float32)
            self._buffers[channel] = np.concatenate((self._buffers[channel], source))
        self._written += frames
        self._trim()

    def pull(self, frames: int, pitch_ratio: float) -> np.ndarray:
        """出力を ``frames`` サンプル取り出す。

        Args:
            frames: 欲しいサンプル数。
            pitch_ratio: 音程の倍率（1.0 = 原曲。半音 n なら ``2**(n/12)``）。

        Returns:
            形 ``(frames, channels)`` の波形。先読みが足りなければ無音。
        """
        out = np.zeros((frames, self.channels), dtype=np.float32)
        if self.lag < self._lead + frames:
            self.starved += 1
            return out
        if self.lag > self._max_lag:
            self._resync()

        position = self._read - self._base
        self._leader.begin()
        block, advanced = self._leader.pull(
            self._buffers[0], position, frames, 1.0, pitch_ratio
        )
        out[:, 0] = block
        for channel, follower in enumerate(self._followers, start=1):
            follower.follow(self._leader.offsets)
            chunk, _ = follower.pull(
                self._buffers[channel], position, frames, 1.0, pitch_ratio
            )
            out[:, channel] = chunk

        # 進んだ量は全チャンネル同じ（同じ frames・同じ比率で回すため、
        # グレインを取る回数まで一致する）
        self._read = advanced + self._base
        self._trim()
        return out

    def _resync(self) -> None:
        """溜まりすぎた入力を捨て、遅延を :data:`_LEAD` へ戻す。

        入力と出力は別のクロックで動くので、放っておくと遅延がじわじわ
        伸びる。捨てた瞬間は音が飛ぶが、起きる頻度は低いので実用上は
        気にならない。
        """
        self._read = float(self._written - self._lead)
        self._leader.reset()
        for follower in self._followers:
            follower.reset()
        self.resyncs += 1

    def _trim(self) -> None:
        """読み終えた古い部分をまとめて捨てる。

        1 サンプルごとに詰めると毎回コピーが走るので、グレイン 1 個分
        溜まってからまとめて切る。
        """
        drop = int(self._read) - self._history - self._base
        if drop < self._grain:
            return
        self._buffers = [buffer[drop:] for buffer in self._buffers]
        self._base += drop


# ----------------------------------------------------------------------
#  本体
# ----------------------------------------------------------------------
class LiveMonitor:
    """入力デバイスの音をキー変更して、出力デバイスへ流し続ける。

    入力用と出力用のストリームを**別々に**開き、入力コールバックは
    ブロックをキューへ積むだけにしてある（落とし穴 2 と、重い処理を
    コールバックでやらないという :mod:`audio.audio_input` と同じ方針）。
    キー変更の計算は出力コールバックの中で行う。

    Attributes:
        input_device: 入力デバイス番号。None なら OS の既定。
        output_device: 出力デバイス番号。None なら OS の既定。
        block_size: コールバック 1 回あたりのサンプル数。
        sample_rate: 実際に開けたサンプリング周波数[Hz]。停止中は 0。
        channels: 実際に開けたチャンネル数。
    """

    def __init__(
        self,
        input_device: int | None = None,
        output_device: int | None = None,
        block_size: int = _BLOCK_SIZE,
    ) -> None:
        """LiveMonitor を初期化する。

        Args:
            input_device: 入力デバイス番号。None なら OS の既定。
            output_device: 出力デバイス番号。None なら OS の既定。
            block_size: コールバック 1 回あたりのサンプル数。
        """
        self.input_device = input_device
        self.output_device = output_device
        self.block_size = block_size
        self.sample_rate = 0
        self.channels = 1

        self._pitch_ratio = 1.0
        self._key_shift = 0
        self._shifter: _RingPitchShifter | None = None
        self.equalizer = Equalizer()
        """キー変更の直後・Live Space の直前に通す 10 バンド EQ。
        OFF の間は :meth:`_on_output` の結果は従来と同一になる。"""
        self.live_space = LiveSpaceProcessor()
        """キー変更のあと・出力の直前に通す音場（Live Space）処理。
        OFF の間は :meth:`_on_output` の結果は従来と同一になる。"""
        self._inbox: queue.SimpleQueue[np.ndarray] = queue.SimpleQueue()
        self._input_stream: Any | None = None
        self._output_stream: Any | None = None
        self._error: str | None = None
        self._dropped_blocks = 0
        """入力キューが上限に達して捨てたブロック数（出力が止まっている兆候）。"""
        self._nonfinite_logged = False
        """NaN/Inf の検出をログへ残したか（毎ブロック出してログを埋めないため）。"""
        self._last_input_at = 0.0
        self._last_output_at = 0.0
        """入出力コールバックが最後に呼ばれた時刻（time.monotonic）。
        音声スレッドが float を代入するだけなのでロックは要らない。"""
        self._input_finished = False
        self._output_finished = False
        """PortAudio がストリームを終わらせたか（finished_callback で立つ）。"""
        self._lock = threading.Lock()
        """``start()`` / ``stop()`` の同時呼び出しを防ぐだけのロック。

        音声コールバックはこのロックを取らない（取ると入力側が出力側の
        計算を待たされ、取りこぼしの原因になる）。
        """

    # ------------------------------------------------------------------
    #  状態
    # ------------------------------------------------------------------
    @property
    def is_running(self) -> bool:
        """動作中かどうかを返す。

        Returns:
            入出力どちらのストリームも開いていれば True。
        """
        return self._input_stream is not None and self._output_stream is not None

    @property
    def error(self) -> str | None:
        """直近のエラーを返す。

        Returns:
            エラー文。異常がなければ None。
        """
        return self._error

    @property
    def key_shift(self) -> int:
        """現在のキー変更量[半音]を返す。

        Returns:
            半音数（0 = 原曲）。
        """
        return self._key_shift

    @property
    def latency_ms(self) -> float:
        """先読みによる遅延の目安[ミリ秒]を返す。

        出力バッファ側の遅延は含まない（ドライバ依存のため）。

        Returns:
            遅延[ミリ秒]。停止中は 0.0。
        """
        if self.sample_rate <= 0:
            return 0.0
        return _LEAD / self.sample_rate * 1000.0

    @property
    def health(self) -> dict[str, int]:
        """取りこぼしの回数をまとめて返す。

        Returns:
            ``{"starved": 先読み不足, "resyncs": 遅延の切り詰め,
            "dropped": 入力キューが溢れて捨てたブロック}``。
        """
        shifter = self._shifter
        if shifter is None:
            return {"starved": 0, "resyncs": 0, "dropped": self._dropped_blocks}
        return {
            "starved": shifter.starved,
            "resyncs": shifter.resyncs,
            "dropped": self._dropped_blocks,
        }

    def check_stalled(self) -> bool:
        """デバイスとの接続が切れていないか調べ、切れていれば停止する。

        GUI の定期更新から呼ぶ。ヘッドホンを抜く等で片方のストリームだけが
        止まっても PortAudio は例外を投げないことがあり、放っておくと
        「稼働中」表示のまま無音になる。コールバックが一定時間届かない、
        またはストリームが終了していたら停止して :attr:`error` に理由を残す。

        GUI スレッドからのみ呼ぶこと（:meth:`stop` を呼ぶため）。

        Returns:
            今回停止させたら True。
        """
        input_stream = self._input_stream
        output_stream = self._output_stream
        if input_stream is None or output_stream is None:
            return False
        now = time.monotonic()
        reason: str | None = None
        if (
            self._output_finished
            or not _stream_active(output_stream)
            or now - self._last_output_at > _STALL_TIMEOUT_S
        ):
            reason = (
                "出力デバイス（ヘッドホン／スピーカー）との接続が切れたため"
                "停止しました。\n接続を確認し、「一覧を更新」してから開始し直して"
                "ください。"
            )
        elif (
            self._input_finished
            or not _stream_active(input_stream)
            or now - self._last_input_at > _STALL_TIMEOUT_S
        ):
            reason = (
                "入力デバイス（CABLE Output）から音が届かなくなったため停止"
                "しました。\nVB-CABLE が有効か確認し、「一覧を更新」してから開始し"
                "直してください。"
            )
        if reason is None:
            return False
        logger.warning("ストリーム停止を検出: %s", reason.splitlines()[0])
        self.stop()
        self._error = reason
        return True

    def set_key_shift(self, semitones: int) -> None:
        """キー変更量を設定する。動作中でも呼べる。

        Args:
            semitones: 半音数。:data:`KEY_SHIFT_MIN`〜:data:`KEY_SHIFT_MAX`
                に丸める。
        """
        semitones = max(KEY_SHIFT_MIN, min(KEY_SHIFT_MAX, int(semitones)))
        self._key_shift = semitones
        # float の代入だけなのでロックは要らない。出力コールバックは
        # 新旧どちらかの値を読むだけで、壊れた値は読めない。
        self._pitch_ratio = 2.0 ** (semitones / 12.0)

    # ------------------------------------------------------------------
    #  開始・停止
    # ------------------------------------------------------------------
    def start(self) -> None:
        """入出力ストリームを開き、キー変更を開始する。

        Raises:
            LiveMonitorError: sounddevice 未導入、デバイスの組み合わせが
                不正、共通のサンプリング周波数が無い、または
                ストリームを開けなかった場合。
        """
        with self._lock:
            if self.is_running:
                return
            self._error = None
            sounddevice = _import_sounddevice()

            input_info = self._device_info(sounddevice, self.input_device, "input")
            output_info = self._device_info(sounddevice, self.output_device, "output")
            self._reject_feedback_loop(input_info, output_info)

            channels = max(
                1,
                min(
                    MAX_CHANNELS,
                    int(input_info.get("max_input_channels", 1)),
                    int(output_info.get("max_output_channels", 1)),
                ),
            )
            sample_rate = self._negotiate_sample_rate(
                sounddevice, input_info, output_info, channels
            )

            self.channels = channels
            self.sample_rate = sample_rate
            self._shifter = _RingPitchShifter(channels)
            self.equalizer.prepare(sample_rate, channels)
            self.live_space.prepare(sample_rate, channels)
            self._drain_inbox()
            self._dropped_blocks = 0
            self._input_finished = False
            self._output_finished = False

            try:
                self._input_stream = sounddevice.InputStream(
                    samplerate=sample_rate,
                    blocksize=self.block_size,
                    channels=channels,
                    dtype="float32",
                    device=self.input_device,
                    callback=self._on_input,
                    finished_callback=self._on_input_finished,
                )
                self._output_stream = sounddevice.OutputStream(
                    samplerate=sample_rate,
                    blocksize=self.block_size,
                    channels=channels,
                    dtype="float32",
                    device=self.output_device,
                    callback=self._on_output,
                    finished_callback=self._on_output_finished,
                )
                # 開始直後はまだコールバックが来ていないので、今を起点にする
                self._last_input_at = self._last_output_at = time.monotonic()
                self._input_stream.start()
                self._output_stream.start()
            except Exception as exc:
                logger.exception("音声ストリームを開けませんでした")
                self._close_streams()
                self._shifter = None
                self.sample_rate = 0
                raise LiveMonitorError(
                    f"音声デバイスを開けませんでした: {exc}\n"
                    "・入力に「CABLE Output」、出力に実際のスピーカー／"
                    "ヘッドホンを選んでいるか\n"
                    "・同じデバイスが複数の方式で並んでいる場合、"
                    "WDM-KS 以外を選んでいるか\n"
                    "を確認してください。"
                ) from exc

    def stop(self) -> None:
        """ストリームを閉じて停止する。

        すでに停止済みなら何もしない。停止処理中の例外は握りつぶす
        （停止できないこと自体は呼び出し側で復旧できないため）。
        """
        with self._lock:
            self._close_streams()
            self._shifter = None
            self.sample_rate = 0
            self._drain_inbox()

    def _close_streams(self) -> None:
        """開いているストリームを出力側から閉じる。

        先に出力を止めるのは、入力だけ生きている状態のほうが
        （溜まるだけで音が出ないので）害が少ないため。
        """
        for name in ("_output_stream", "_input_stream"):
            stream = getattr(self, name)
            setattr(self, name, None)
            if stream is None:
                continue
            try:
                stream.stop()
            except Exception:  # noqa: BLE001 - 停止できなくても先へ進む
                logger.warning("ストリームの停止に失敗", exc_info=True)
            try:
                stream.close()
            except Exception:  # noqa: BLE001 - 同上
                logger.warning("ストリームのクローズに失敗", exc_info=True)

    def _drain_inbox(self) -> None:
        """入力キューを空にする。"""
        while True:
            try:
                self._inbox.get_nowait()
            except queue.Empty:
                return

    # ------------------------------------------------------------------
    #  デバイスの下調べ
    # ------------------------------------------------------------------
    @staticmethod
    def _device_info(sounddevice: Any, device: int | None, kind: str) -> Any:
        """デバイス情報を引く。

        Args:
            sounddevice: sounddevice モジュール。
            device: デバイス番号。None なら OS の既定。
            kind: ``"input"`` か ``"output"``。

        Returns:
            :func:`sounddevice.query_devices` が返す辞書。

        Raises:
            LiveMonitorError: デバイスが見つからない場合。
        """
        try:
            if device is None:
                return sounddevice.query_devices(kind=kind)
            return sounddevice.query_devices(device)
        except Exception as exc:
            label = "入力" if kind == "input" else "出力"
            raise LiveMonitorError(
                f"{label}デバイスが見つかりませんでした: {exc}\n"
                "一覧を更新してから選び直してください。"
            ) from exc

    @staticmethod
    def _reject_feedback_loop(input_info: Any, output_info: Any) -> None:
        """仮想ケーブルへ出しながら同じケーブルから録る配線を弾く。

        ``CABLE Input`` へ出した音は ``CABLE Output`` へ戻ってくるので、
        この組み合わせで開始すると音が回り続けて発振する。開く前に
        止めるのが唯一の対策（開いたあとでは耳を痛める）。

        Args:
            input_info: 入力デバイス情報。
            output_info: 出力デバイス情報。

        Raises:
            LiveMonitorError: 入力・出力ともに仮想ケーブルの場合。
        """
        mark = _LOOPBACK_MARK.lower()
        input_name = str(input_info.get("name", ""))
        output_name = str(output_info.get("name", ""))
        if mark in input_name.lower() and mark in output_name.lower():
            raise LiveMonitorError(
                f"出力先が仮想ケーブル（{output_name}）になっています。\n"
                "この組み合わせでは音が仮想ケーブルの中を回り続けてしまう"
                "ため開始できません。\n"
                "出力には実際のスピーカーかヘッドホンを選んでください。"
            )

    def _negotiate_sample_rate(
        self,
        sounddevice: Any,
        input_info: Any,
        output_info: Any,
        channels: int,
    ) -> int:
        """入力と出力の両方が受け付けるサンプリング周波数を探す。

        VB-CABLE 側の形式（既定では 44.1kHz のことが多い）と実機側の形式
        が食い違うことがよくあり、片方に合わせて開くともう片方が
        ``Invalid sample rate`` で失敗する。両方に問い合わせて共通の値を
        選ぶ。

        Args:
            sounddevice: sounddevice モジュール。
            input_info: 入力デバイス情報。
            output_info: 出力デバイス情報。
            channels: 開きたいチャンネル数。

        Returns:
            使えるサンプリング周波数[Hz]。

        Raises:
            LiveMonitorError: 共通の周波数が見つからない場合。
        """
        candidates: list[int] = []
        for info in (input_info, output_info):
            rate = int(float(info.get("default_samplerate", 0) or 0))
            if rate > 0:
                candidates.append(rate)
        candidates += [config.SAMPLE_RATE, 48000, 44100]

        for rate in dict.fromkeys(candidates):
            try:
                sounddevice.check_input_settings(
                    device=self.input_device,
                    channels=channels,
                    dtype="float32",
                    samplerate=rate,
                )
                sounddevice.check_output_settings(
                    device=self.output_device,
                    channels=channels,
                    dtype="float32",
                    samplerate=rate,
                )
            except Exception:  # noqa: BLE001 - 次の候補を試す
                continue
            return rate

        raise LiveMonitorError(
            "入力と出力で共通のサンプリング周波数が見つかりませんでした。\n"
            "Windows の「サウンドの詳細設定」で、VB-CABLE とスピーカーの"
            "形式（例: 48000 Hz・2 チャンネル）をそろえてください。"
        )

    # ------------------------------------------------------------------
    #  PortAudio のコールバック
    # ------------------------------------------------------------------
    def _on_input(
        self,
        indata: np.ndarray,
        frames: int,  # noqa: ARG002 - PortAudio が要求するシグネチャ
        time_info: Any,  # noqa: ARG002
        status: Any,  # noqa: ARG002
    ) -> None:
        """入力コールバック。ブロックをキューへ積むだけ。

        Args:
            indata: 取得した波形（shape=(frames, channels)）。
            frames: サンプル数。
            time_info: タイミング情報。
            status: ストリームの状態フラグ。
        """
        self._last_input_at = time.monotonic()
        # 出力が止まっていると溜まる一方になるので、上限を超えたら
        # 古いものから捨てる（_INBOX_MAX_BLOCKS 参照）
        if self._inbox.qsize() >= _INBOX_MAX_BLOCKS:
            try:
                self._inbox.get_nowait()
                self._dropped_blocks += 1
            except queue.Empty:
                pass
        # indata は PortAudio が使い回すバッファなので必ずコピーする
        self._inbox.put(np.array(indata, dtype=np.float32, copy=True))

    def _on_input_finished(self) -> None:
        """入力ストリームが終了したときに PortAudio から呼ばれる。"""
        self._input_finished = True

    def _on_output_finished(self) -> None:
        """出力ストリームが終了したときに PortAudio から呼ばれる。"""
        self._output_finished = True

    def _on_output(
        self,
        outdata: np.ndarray,
        frames: int,
        time_info: Any,  # noqa: ARG002
        status: Any,  # noqa: ARG002
    ) -> None:
        """出力コールバック。溜まった入力をキー変更して書き出す。

        Args:
            outdata: 書き込み先（shape=(frames, channels)）。
            frames: サンプル数。
            time_info: タイミング情報。
            status: ストリームの状態フラグ。
        """
        self._last_output_at = time.monotonic()
        shifter = self._shifter
        if shifter is None:
            outdata.fill(0.0)
            return
        try:
            while True:
                try:
                    shifter.push(self._inbox.get_nowait())
                except queue.Empty:
                    break
            block = shifter.pull(frames, self._pitch_ratio)
            block = self.equalizer.process(block)
            block = self.live_space.process(block)
            if not np.isfinite(block).all():
                # IIR/遅延線に一度 NaN・Inf が入ると、以後ずっと出続ける。
                # 爆音・ノイズを出さないよう無音にし、フィルタの状態を捨てる。
                if not self._nonfinite_logged:
                    self._nonfinite_logged = True
                    logger.error("音声データに NaN/Inf を検出したため無音にし、EQ と音響をリセットしました")
                self.equalizer.reset()
                self.live_space.reset()
                block = np.zeros_like(block)
            outdata[:] = block
        except Exception as exc:  # noqa: BLE001 - 音を止めずに理由を残す
            # ここで例外を投げると PortAudio がストリームごと落として
            # しまうので、無音を書いて GUI（error プロパティ）へ伝える。
            outdata.fill(0.0)
            if self._error is None:
                # 同じ例外がブロックごとに出続けるとログが溢れるので初回だけ
                logger.exception("音声処理でエラーが発生しました")
            self._error = f"音声処理でエラーが発生しました: {exc}"


def _stream_active(stream: Any) -> bool:
    """ストリームが動作中かを返す。問い合わせ自体が失敗したら止まっている扱い。"""
    try:
        return bool(stream.active)
    except Exception:  # noqa: BLE001 - デバイス消失時は属性参照でも例外になりうる
        return False
