"""マイクから音声ブロックを取得するモジュール。

このモジュールは「取得」だけを担当し、解析は一切行わない。
PortAudio のコールバックスレッド内で重い処理をすると
バッファアンダーラン（プチプチ音・取りこぼし）が発生するため、
コールバックではキューへ積むだけにしている。
"""

from __future__ import annotations

import queue
from types import TracebackType
from typing import Any

import numpy as np

import config


class AudioInputError(RuntimeError):
    """音声入力まわりの失敗を表す例外。"""

    code = "E201"


def _import_sounddevice() -> Any:
    """sounddevice を遅延 import する。

    Returns:
        sounddevice モジュール。

    Raises:
        AudioInputError: 未導入の場合。
    """
    try:
        import sounddevice  # noqa: PLC0415
    except ImportError as exc:
        raise AudioInputError(
            "sounddevice が導入されていません。\n"
            "  pip install sounddevice\n"
            "を実行してください。"
        ) from exc
    return sounddevice


class AudioInput:
    """Windows の既定の録音デバイスから音声ブロックを取得する。

    Attributes:
        sample_rate: サンプリング周波数[Hz]。
        block_size: コールバック 1 回あたりのサンプル数。
        device: 入力デバイス番号。None なら OS の既定デバイス。
        dropped_blocks: キュー溢れで破棄したブロック数。
        input_gain: 入力波形に掛ける倍率。マイク・機材ごとの音量差を
            ソフト側で補正するためのもの（:data:`config.MIC_GAIN_DB` 参照）。
    """

    def __init__(
        self,
        sample_rate: int = config.SAMPLE_RATE,
        block_size: int = config.BLOCK_SIZE,
        device: int | None = None,
        input_gain: float = 1.0,
    ) -> None:
        """AudioInput を初期化する。

        Args:
            sample_rate: サンプリング周波数[Hz]。
            block_size: コールバック 1 回あたりのサンプル数。
            device: 入力デバイス番号。None なら OS の既定デバイス。
            input_gain: 入力波形に掛ける倍率（線形値、1.0 で無補正）。
        """
        self.sample_rate = sample_rate
        self.block_size = block_size
        self.device = device
        self.input_gain = input_gain
        self.dropped_blocks = 0

        self._stream: Any | None = None
        self._sink: queue.Queue[np.ndarray] | None = None
        self._status_message: str | None = None

    # ------------------------------------------------------------------
    #  デバイス情報
    # ------------------------------------------------------------------
    @staticmethod
    def list_input_devices() -> list[dict[str, Any]]:
        """利用可能な入力デバイスの一覧を返す。

        Returns:
            ``{"index": int, "name": str, "channels": int, "is_default": bool}``
            の辞書のリスト。取得に失敗した場合は空リスト。
        """
        try:
            sounddevice = _import_sounddevice()
            devices = sounddevice.query_devices()
            default_index = sounddevice.default.device[0]
        except Exception:  # noqa: BLE001 - 一覧取得は失敗しても続行
            return []

        result: list[dict[str, Any]] = []
        for index, info in enumerate(devices):
            if int(info.get("max_input_channels", 0)) <= 0:
                continue
            result.append(
                {
                    "index": index,
                    "name": str(info.get("name", f"device {index}")),
                    "channels": int(info["max_input_channels"]),
                    "is_default": index == default_index,
                }
            )
        return result

    @staticmethod
    def default_device_name() -> str:
        """既定の入力デバイス名を返す。

        Returns:
            デバイス名。取得できない場合は ``"（不明）"``。
        """
        try:
            sounddevice = _import_sounddevice()
            info = sounddevice.query_devices(kind="input")
            return str(info.get("name", "（不明）"))
        except Exception:  # noqa: BLE001 - 表示用のため失敗しても続行
            return "（不明）"

    # ------------------------------------------------------------------
    #  ストリーム制御
    # ------------------------------------------------------------------
    @property
    def is_running(self) -> bool:
        """ストリームが動作中かどうかを返す。

        Returns:
            動作中なら True。
        """
        return self._stream is not None

    def start(self, sink: queue.Queue[np.ndarray]) -> None:
        """録音を開始し、取得したブロックを ``sink`` へ積む。

        Args:
            sink: 音声ブロックの投入先キュー。

        Raises:
            AudioInputError: sounddevice 未導入、またはストリーム開始に失敗した場合。
        """
        if self._stream is not None:
            return

        sounddevice = _import_sounddevice()
        self._sink = sink
        self.dropped_blocks = 0
        self._status_message = None

        try:
            self._stream = sounddevice.InputStream(
                samplerate=self.sample_rate,
                blocksize=self.block_size,
                channels=config.CHANNELS,
                dtype="float32",
                device=self.device,
                callback=self._on_audio,
            )
            self._stream.start()
        except Exception as exc:
            self._stream = None
            self._sink = None
            raise AudioInputError(
                f"マイクの録音を開始できませんでした: {exc}\n"
                "Windows の「設定 > プライバシー > マイク」でマイクへのアクセスが"
                "許可されているか確認してください。"
            ) from exc

    def stop(self) -> None:
        """録音を停止しストリームを解放する。

        すでに停止済みの場合は何もしない。停止処理中の例外は握りつぶす
        （停止できないこと自体は呼び出し側で復旧できないため）。
        """
        stream, self._stream = self._stream, None
        self._sink = None
        if stream is None:
            return
        try:
            stream.stop()
        finally:
            stream.close()

    def _on_audio(
        self,
        indata: np.ndarray,
        frames: int,  # noqa: ARG002 - PortAudio が要求するシグネチャ
        time_info: Any,  # noqa: ARG002
        status: Any,
    ) -> None:
        """PortAudio のコールバック。ブロックをキューへ積むだけ。

        Args:
            indata: 取得した波形（shape=(frames, channels)）。
            frames: サンプル数。
            time_info: タイミング情報。
            status: ストリームの状態フラグ。
        """
        if status:
            self._status_message = str(status)

        sink = self._sink
        if sink is None:
            return

        # indata は PortAudio が使い回すバッファなので必ずコピーする
        block = np.array(indata[:, 0], dtype=np.float32, copy=True)
        if self.input_gain != 1.0:
            # ゲインを掛けたあと、フルスケールを超えた分はクリップする
            # （そのまま渡すと dBFS 計算や YIN の自己相関が壊れるため）
            np.multiply(block, self.input_gain, out=block)
            np.clip(block, -1.0, 1.0, out=block)
        try:
            sink.put_nowait(block)
        except queue.Full:
            # 解析が追いつかない場合は最も古いブロックを捨てて遅延を防ぐ
            self.dropped_blocks += 1
            try:
                sink.get_nowait()
                sink.put_nowait(block)
            except (queue.Empty, queue.Full):
                pass

    @property
    def status_message(self) -> str | None:
        """直近のストリーム状態メッセージを返す。

        Returns:
            オーバーフロー等の警告文字列。異常がなければ None。
        """
        return self._status_message

    # ------------------------------------------------------------------
    #  コンテキストマネージャ
    # ------------------------------------------------------------------
    def __enter__(self) -> AudioInput:
        """with 文の開始。

        Returns:
            自分自身。
        """
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """with 文の終了時に録音を停止する。

        Args:
            exc_type: 例外の型。
            exc_value: 例外インスタンス。
            traceback: トレースバック。
        """
        self.stop()
