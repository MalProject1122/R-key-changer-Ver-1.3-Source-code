"""ピッチを変えずに時間軸だけを伸縮させる（WSOLA）ための共通モジュール。

このモジュールは numpy 以外に何も依存しない**純粋な DSP** で、
入力が何であるか（動画ファイル／マイク／再生中の音）を知らない。

利用者は 2 つある。

* :mod:`video.video_analysis` -- 動画の倍速再生とキー変更
* :mod:`audio.live_monitor`   -- 再生中の音のリアルタイムキー変更

もともと :mod:`video.video_analysis` の中に private クラスとして
置いていたが、ライブ監視でも同じ DSP が要るようになったため、
**依存の向き（``video`` → ``audio`` → ``config``）を保ったまま
共有できるよう** こちらへ移した。DSP の中身は移動前と同一。
"""

from __future__ import annotations

import numpy as np


class TimeStretcher:
    """ピッチを変えずに再生速度だけを変える（WSOLA 方式）。

    単純に読み飛ばし／補間で速度を変えると、波形そのものが伸び縮み
    するため音の高さまで変わってしまう（2倍速で 1 オクターブ上がる）。
    歌の音程を耳で確かめながら聞きたい用途では困るので、
    **短い粒（グレイン）に切って重ね合わせる**ことで、波形の周期は
    保ったまま時間軸だけを伸縮させる。

    素朴な重ね合わせ（OLA）は継ぎ目で波形の位相がずれて金属的な
    響きになる。そこで次のグレインを取る位置を少し前後に探し、
    直前のグレインと最も滑らかにつながる場所を選ぶ（これが WSOLA）。

    Attributes:
        grain: 1 グレインのサンプル数。
        hop_out: 出力側のホップ幅（= ``grain`` の半分）。
        search: 継ぎ目を探す範囲[サンプル]。
    """

    def __init__(
        self, grain: int = 2048, search: int = 256, template: int = 512
    ) -> None:
        """TimeStretcher を初期化する。

        Args:
            grain: 1 グレインのサンプル数。長いほど音質は安定するが
                時間方向の追従が鈍る。
            search: 継ぎ目を探す前後の範囲[サンプル]。0 なら探索なし
                （素朴な OLA になる）。
            template: つながりの良さを測るのに使う長さ[サンプル]。
        """
        self.grain = grain
        self.hop_out = grain // 2
        self.search = search
        self.template = min(template, self.hop_out)
        # ハン窓は 50% 重ねで足すと 1.0 になる（等倍なら元の波形に戻る）
        self._window = np.hanning(grain).astype(np.float32)
        self._pending = np.zeros(0, dtype=np.float32)
        self._tail = np.zeros(self.hop_out, dtype=np.float32)
        self._next_template: np.ndarray | None = None
        self._resample_phase = 0.0
        """キー変更時の再サンプル位置の小数部（コールバックをまたいで持ち越す）。"""
        self._carry = np.zeros(0, dtype=np.float32)
        """伸縮済みで、まだ再サンプルに使い切っていない波形。

        補間には終端の 1 サンプル先まで要るので、毎回ほんの数サンプル
        だけ多く伸縮することになる。これを捨てると**捨てた分だけ音が
        先へ進み**、キー変更時に音程がわずかに上ずる（出力 1024 サンプル
        あたり 2〜3 サンプル ≒ 1 オクターブ下げで約 8 セント）。
        次回の先頭へ持ち越すことで、この誤差をゼロにする。
        """

    def reset(self) -> None:
        """内部状態を捨てる。シークや速度変更のあとに呼ぶ。"""
        self._pending = np.zeros(0, dtype=np.float32)
        self._tail = np.zeros(self.hop_out, dtype=np.float32)
        self._next_template = None
        self._resample_phase = 0.0
        self._carry = np.zeros(0, dtype=np.float32)

    def pull(
        self,
        audio: np.ndarray,
        source_pos: float,
        frames: int,
        speed: float,
        pitch_ratio: float = 1.0,
    ) -> tuple[np.ndarray, float]:
        """出力を ``frames`` サンプル取り出し、進んだ読み取り位置を返す。

        ``pitch_ratio`` を 1.0 以外にすると**キー変更**になる。
        時間伸縮（この関数）と再サンプルを組み合わせて実現する:

        * まず伸縮率 ``speed / pitch_ratio`` で時間だけを伸ばす
        * その結果を ``pitch_ratio`` 倍で読み飛ばす（＝音程が上がり、
          長さが元に戻る）

        両者を掛けると元の消費速度 ``speed`` に戻るため、**キーを変えても
        再生位置の進み方は変わらない**（映像との同期がずれない）。

        Args:
            audio: 元の波形（全体）。
            source_pos: 現在の読み取り位置[サンプル]。
            frames: 欲しい出力サンプル数。
            speed: 再生速度（1.0 = 等倍）。
            pitch_ratio: 音程の倍率（1.0 = 原曲。半音 n なら 2**(n/12)）。

        Returns:
            ``(出力ブロック, 進んだあとの読み取り位置)``。
        """
        if pitch_ratio != 1.0:
            return self._pull_pitch_shifted(
                audio, source_pos, frames, speed, pitch_ratio
            )
        return self._pull_plain(audio, source_pos, frames, speed)

    def _pull_pitch_shifted(
        self,
        audio: np.ndarray,
        source_pos: float,
        frames: int,
        speed: float,
        pitch_ratio: float,
    ) -> tuple[np.ndarray, float]:
        """キー変更つきで出力を取り出す。

        Args:
            audio: 元の波形（全体）。
            source_pos: 現在の読み取り位置[サンプル]。
            frames: 欲しい出力サンプル数。
            speed: 再生速度。
            pitch_ratio: 音程の倍率。

        Returns:
            ``(出力ブロック, 進んだあとの読み取り位置)``。
        """
        # 最後に補間する位置は phase + (frames-1)*比率。線形補間は
        # その 1 サンプル先まで読むので、+2 して個数にする。
        end = self._resample_phase + frames * pitch_ratio
        consumed = int(np.floor(end))
        # 比率が 2 倍を超えると「消費する数」のほうが多くなる。今の
        # 呼び出し元は ±12 半音（最大 2 倍）までだが、足りないまま
        # 進めると音が飛ぶので、多いほうに合わせておく。
        needed = max(int(np.floor(end - pitch_ratio)) + 2, consumed)
        if self._carry.size < needed:
            extra, source_pos = self._pull_plain(
                audio, source_pos, needed - self._carry.size, speed / pitch_ratio
            )
            self._carry = np.concatenate((self._carry, extra))
        stretched = self._carry

        positions = self._resample_phase + np.arange(frames) * pitch_ratio
        out = np.interp(
            positions, np.arange(stretched.size, dtype=np.float64), stretched
        ).astype(np.float32)

        # 使い切った整数分だけ捨て、端数と余りは次回へ持ち越す。
        # ここで余りを捨てると音程がずれる（_carry の説明を参照）。
        self._carry = stretched[consumed:]
        self._resample_phase = end - consumed
        return out, source_pos

    def _pull_plain(
        self, audio: np.ndarray, source_pos: float, frames: int, speed: float
    ) -> tuple[np.ndarray, float]:
        """時間伸縮だけを行って出力を取り出す（キー変更なし）。

        Args:
            audio: 元の波形（全体）。
            source_pos: 現在の読み取り位置[サンプル]。
            frames: 欲しい出力サンプル数。
            speed: 伸縮率。

        Returns:
            ``(出力ブロック, 進んだあとの読み取り位置)``。
        """
        while self._pending.size < frames:
            center = int(source_pos)
            if self._next_template is not None and self.search > 0:
                center = self._best_offset(audio, center)

            grain = self._slice(audio, center, self.grain)
            windowed = grain * self._window

            # 前のグレインの後ろ半分と、今のグレインの前半分を足し合わせる
            block = self._tail + windowed[: self.hop_out]
            self._tail = windowed[self.hop_out :].copy()
            self._pending = np.concatenate((self._pending, block))

            # 次に「自然につながるはずの波形」を控えておく（探索の的）
            self._next_template = self._slice(
                audio, center + self.hop_out, self.template
            )
            source_pos += self.hop_out * speed

            if source_pos >= audio.size + self.grain:
                break  # 終端。これ以上は無音が続くだけ

        take = min(frames, self._pending.size)
        out = np.zeros(frames, dtype=np.float32)
        out[:take] = self._pending[:take]
        self._pending = self._pending[take:]
        return out, source_pos

    def _best_offset(self, audio: np.ndarray, center: int) -> int:
        """直前のグレインと最も滑らかにつながる読み取り位置を探す。

        Args:
            audio: 元の波形。
            center: 本来読むべき位置。

        Returns:
            探索範囲の中で相関が最大になった位置。
        """
        template = self._next_template
        if template is None or template.size == 0:
            return center

        low = max(0, center - self.search)
        high = min(audio.size - template.size, center + self.search)
        if high <= low:
            return center

        segment = audio[low : high + template.size]
        if segment.size < template.size:
            return center
        correlation = np.correlate(segment, template, mode="valid")
        return low + int(np.argmax(correlation))

    @staticmethod
    def _slice(audio: np.ndarray, start: int, length: int) -> np.ndarray:
        """範囲外を無音で埋めつつ、波形を切り出す。

        Args:
            audio: 元の波形。
            start: 切り出し開始位置。
            length: 切り出す長さ。

        Returns:
            長さ ``length`` の波形（float32）。
        """
        start = max(0, start)
        stop = min(start + length, audio.size)
        chunk = audio[start:stop]
        if chunk.size < length:
            chunk = np.concatenate(
                (chunk, np.zeros(length - chunk.size, dtype=np.float32))
            )
        return chunk
