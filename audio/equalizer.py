"""10バンド・リアルタイムイコライザー（C実装のbiquadカスケード）。

キー変更（WSOLA）の直後・音響処理（Live Space）の直前に挿入する
（:meth:`audio.live_monitor.LiveMonitor._on_output` 参照）。トーンを
整えてから会場の響きを乗せる、という通常のミキシング順に合わせてある。

■ 方式を選んだ経緯（重要）

10バンドの本格的な共振フィルタ（RBJ peaking biquad のカスケード）を
実装しようとしたが、当初は以下の3方式をすべて実測した結果、いずれも
不採用とし、実数one-poleの差分（帯域ごとのクロスオーバー）方式で
初版を作った。

1. **サンプル単位のPythonループ（Direct Form II Transposed）**
   正確だが、10バンド×ステレオでブロック予算の55〜111%を消費し、
   リアルタイム処理として明らかに不可（実測）。
2. **複素数one-pole分解（共振極を部分分数展開してnumpyでベクトル化）**
   数式は正しく組めたが、複素数配列のオーバーヘッドが大きく予算の
   72〜143%を消費。さらに高域バンド（8k/16kHz）と低域バンド（31Hz）で
   極の絶対値が大きく異なるため、両方に安全なチャンク幅を1つ選ぶのが
   難しく、桁あふれでNaNが出る不具合も発生した（実測・修正試行済み）。
3. **``scipy.signal.sosfilt``（C実装）**
   処理自体は予算の1.3〜8.4%と圧倒的に速く、精度も既存参照実装と
   完全一致（誤差0）だった。しかし ``import scipy.signal`` 自体が
   このプロジェクトの実行環境で初回60秒・3回目でも2.4秒かかることを
   実測で確認した。このアプリは PyInstaller の onefile 形式で**起動の
   たびに一時フォルダへ再展開される**ため（:mod:`config` 参照）、
   exe版では毎回この遅延が起きる可能性が高く、「開いてすぐ使える」
   というアプリの前提を壊してしまう。速度は魅力的だが、初版では
   不採用とした。

そこで初版は :class:`audio.live_space.LiveSpaceProcessor` の
``_OnePole``（実数one-pole）を流用した「クロスオーバー方式」で
実装したが、共振（Q）を持たないため、+12dB表示に対しブースト側は
高精度な一方、カット側は高域ほど表示より効きが弱くなる非対称な誤差
（8kHzで最大+3.6dB相当）が残るという既知の制限があった。

■ C実装への切り替え

上記の制限を解消するため、あらためて「RBJ peaking biquadのカスケード
（方式1）」を**自作のCコード**（``native/biquad_eq.c``）として書き直し、
``ctypes`` 経由で呼び出す形にした。方式1がPythonループとして遅かった
のは「Pythonのループが遅い」だけが理由で、フィルタ設計自体に問題は
無かったため、同じアルゴリズムをCで書けば解決する、という判断による。

実測（ステレオ・10バンド）:

* ブロック予算の**2.6〜4.1%**（旧・クロスオーバー方式は16〜22%）
* Pythonの参照実装との誤差は7.9e-8（float32の丸め誤差レベル、実質完全一致）
* 表示dBと実測dBの誤差は、ブースト・カットとも全帯域で0.00dB
  （共振フィルタなので理論通り。旧方式にあった非対称な誤差は解消）

``scipy`` のときと違い、``biquad_eq.dll`` はこのアプリ専用の小さな
バイナリ（他の巨大な依存を持たない）なので、``ctypes.CDLL`` での読み込み
コストは無視できるレベル（scipyのimportのような重さは無い）。

■ 既知の制限（正直な開示）

* フィルタ係数の更新（:meth:`Equalizer.set_band_db` 等）は GUI スレッド、
  :meth:`Equalizer.process` は音声コールバックのスレッドから呼ばれる。
  ``ctypes`` は C 関数の実行中に GIL を手放すため、両者は本当に同時に
  走りうる（係数5個の書き換え途中を読む・破棄済みのハンドルを使う）。
  そこで C を呼ぶ区間だけを :attr:`Equalizer._lock` で囲んでいる。
  係数の更新は1バンド数マイクロ秒で終わるので、音声側が待つとしても
  その程度で済む（重い処理をロックの中でしない）。
* 全10バンドを同時に+12dBにすると、帯域の裾が重なり合って振幅が
  最大6倍程度まで積み上がることを実測済み。:func:`_soft_limit` で
  歪みを抑えているが、フィルタそのものにヘッドルーム調整は入れていない。
"""

from __future__ import annotations

import ctypes
import logging
import math
import threading
from typing import Optional

import numpy as np

import config
from audio.live_space import _soft_limit

logger = logging.getLogger(__name__)

BAND_FREQS: tuple[int, ...] = (31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000)
"""10バンドの中心周波数[Hz]。ISO標準の1オクターブ間隔。
``native/biquad_eq.c`` の ``BAND_FREQS`` と値を一致させること。"""

GAIN_MIN_DB = -12.0
GAIN_MAX_DB = 12.0
"""各バンドで選べるゲインの範囲[dB]。"""

CUSTOM_PRESET = "Custom"

PRESETS: dict[str, tuple[float, ...]] = {
    "Flat": (0.0,) * 10,
    "Vocal": (-2.0, -2.0, -1.0, 0.0, 2.0, 3.0, 2.0, 1.0, 0.0, -1.0),
    "Karaoke": (-1.0, -1.0, 0.0, 1.0, 2.0, 3.0, 2.0, 1.0, 1.0, 0.0),
    "Bass Boost": (5.0, 4.0, 3.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
    "Treble Boost": (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 2.0, 3.0, 4.0),
    "Clear": (-1.0, 0.0, 0.0, -1.0, 0.0, 1.0, 2.0, 2.0, 1.0, 0.0),
    "Rock": (3.0, 2.0, 0.0, -1.0, -1.0, 0.0, 1.0, 2.0, 2.0, 1.0),
}
"""バンドごとのゲイン[dB]。10バンド分のタプルで :data:`BAND_FREQS` と対応する。

過度な値にならないよう、どのプリセットも ±5dB 以内に収めてある。
"""

_DLL_PATH = config.RESOURCES_DIR / "native" / "biquad_eq.dll"
_N_BANDS = len(BAND_FREQS)


def _sanitize_db(value: object) -> float:
    """外から来たゲイン値を、C へ渡してよい有限の dB 値にする。

    NaN / ±Infinity / 数値でない値は 0dB（フラット）に置き換える。
    NaN のまま C へ渡すとフィルタの内部状態が NaN になり、以後の音声が
    すべて NaN（＝無音または爆音）になってしまうため。
    """
    if isinstance(value, bool):
        return 0.0
    try:
        db = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(db):
        return 0.0
    return max(GAIN_MIN_DB, min(GAIN_MAX_DB, db))


def preset_name_for(gains_db: tuple[float, ...]) -> str:
    """一致するプリセット名、なければ :data:`CUSTOM_PRESET`。

    スライダーを1つでも動かしたら「Custom」表示にする、という
    :func:`audio.live_space.preset_name_for` と同じ考え方。
    """
    rounded = tuple(round(v, 6) for v in gains_db)
    for name, preset in PRESETS.items():
        if tuple(round(v, 6) for v in preset) == rounded:
            return name
    return CUSTOM_PRESET


class _BiquadLib:
    """``native/biquad_eq.dll`` のctypesバインディングをプロセス内で
    1回だけ作る（複数の :class:`Equalizer` インスタンスで共有する）。

    **読み込めなくても例外を外へ出さない。** DLLはウイルス対策ソフトに
    隔離される・一時展開に失敗するなど、利用者側では対処できない理由で
    読めなくなることがある。そこで失敗しても None を返すだけにし、
    EQ だけが無効になって**本体（キー変更）はそのまま使える**ように
    している（:meth:`Equalizer.prepare` 参照）。
    """

    _lib: Optional[ctypes.CDLL] = None
    _load_failed = False
    """一度失敗したら再試行しない（失敗のたびに数十msかかるのを避ける）。"""

    @classmethod
    def get(cls) -> Optional[ctypes.CDLL]:
        """バインディング済みのDLLを返す。読み込めなければ None。"""
        if cls._lib is not None:
            return cls._lib
        if cls._load_failed:
            return None
        try:
            lib = ctypes.CDLL(str(_DLL_PATH))
            lib.eq_create.restype = ctypes.c_void_p
            lib.eq_create.argtypes = [ctypes.c_int, ctypes.c_int]
            lib.eq_destroy.argtypes = [ctypes.c_void_p]
            lib.eq_set_band_db.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_double]
            lib.eq_reset.argtypes = [ctypes.c_void_p]
            lib.eq_process.argtypes = [
                ctypes.c_void_p, ctypes.POINTER(ctypes.c_float), ctypes.c_int
            ]
            lib.eq_get_response_db.argtypes = [ctypes.c_void_p, ctypes.c_double]
            lib.eq_get_response_db.restype = ctypes.c_double
        except (OSError, AttributeError):
            # OSError: DLLが無い・隔離された・アーキテクチャ不一致
            # AttributeError: DLLは読めたが関数が見つからない（古いDLLが残っている等）
            cls._load_failed = True
            logger.exception(
                "EQ用のDLLを読み込めませんでした（%s）。EQ機能のみ無効になります", _DLL_PATH
            )
            return None
        cls._lib = lib
        return lib


class Equalizer:
    """10バンド・グラフィックイコライザー本体（Cのbiquadカスケードを呼ぶ薄いラッパー）。

    :meth:`prepare` → :meth:`process` の順に使う。``enabled`` が False の
    間、または :meth:`prepare` 前は入力をそのまま返す（OFF時に既存の音を
    一切変えないため）。
    """

    _handle: Optional[int] = None
    """クラス属性としても定義しておく。:meth:`__init__` が途中で失敗しても
    :meth:`__del__` が ``AttributeError`` にならないようにするため。"""

    _lib: Optional[ctypes.CDLL] = None

    def __init__(self) -> None:
        self.enabled = False
        self._gains_db = np.zeros(_N_BANDS)
        self._sample_rate = 0
        self._channels = 0
        self._handle = None
        self._lib = None
        """:meth:`prepare` が成功したときだけ設定される。
        ``_handle is not None`` なら ``_lib is not None`` が保証される。"""
        self._lock = threading.Lock()
        """C の ``EqState`` に触る区間（係数更新・処理・破棄）を直列化する。
        モジュール docstring の「既知の制限」を参照。"""

    # ------------------------------------------------------------------
    #  設定
    # ------------------------------------------------------------------
    @property
    def available(self) -> bool:
        """EQが実際に使える状態か（DLLを読み込めて準備できたか）。

        False の場合、ゲインの値は保持されるが音には一切反映されない。
        GUI 側はこれを見て「EQが使えない」旨を表示する。
        """
        return self._handle is not None

    @property
    def gains_db(self) -> tuple[float, ...]:
        """現在の各バンドのゲイン[dB]を返す。"""
        return tuple(self._gains_db.tolist())

    def set_band_db(self, index: int, db: float) -> None:
        """1バンドだけゲインを変える。

        Args:
            index: :data:`BAND_FREQS` の添字（0〜9）。
            db: :data:`GAIN_MIN_DB`〜:data:`GAIN_MAX_DB` に丸める。
        """
        gains = self._gains_db.copy()
        gains[index] = _sanitize_db(db)
        self._gains_db = gains
        self._apply_band(index)

    def set_gains_db(self, gains_db: tuple[float, ...]) -> None:
        """全バンドのゲインをまとめて設定する（プリセット・マイプリセット適用用）。

        マイプリセット（設定ファイル）から来た値は検証されていないことがある。
        個数が :data:`BAND_FREQS` と違う・数値でない場合は全体を 0dB に、
        NaN / Infinity の要素はそのバンドだけ 0dB にする（例外は投げない）。
        """
        try:
            values = list(gains_db)
        except TypeError:
            values = []
        if len(values) != _N_BANDS:
            values = [0.0] * _N_BANDS
        self._gains_db = np.array([_sanitize_db(v) for v in values], dtype=np.float64)
        with self._lock:
            for index in range(_N_BANDS):
                self._apply_band_locked(index)

    def reset_to_flat(self) -> None:
        """RESET。全バンドを0dBへ戻す。"""
        self.set_gains_db((0.0,) * _N_BANDS)

    def _apply_band(self, index: int) -> None:
        with self._lock:
            self._apply_band_locked(index)

    def _apply_band_locked(self, index: int) -> None:
        if self._handle is not None and self._lib is not None:
            self._lib.eq_set_band_db(self._handle, index, float(self._gains_db[index]))

    def prepare(self, sample_rate: int, channels: int) -> None:
        """ストリームを開く直前に呼ぶ。フィルタを作り直す。

        再生開始前（プレビュー目的でGUIがゲインを動かすだけの状態）でも
        周波数特性を計算できるよう、実際の音声ストリームを開いていない
        タイミングでも（例えば ``prepare(48000, 2)`` のように）呼んで
        構わない。実際のストリーム開始時に、負ネゴシエーション後の
        本当のサンプルレートで再度呼ばれ、作り直される。

        **DLLが読み込めない場合も例外は投げない。** :attr:`available` が
        False のまま（＝EQだけが無効）になり、キー変更や音響は影響を
        受けない。
        """
        self._destroy_handle()
        self._sample_rate = int(sample_rate)
        self._channels = int(channels)
        if self._sample_rate <= 0 or self._channels <= 0:
            return
        lib = _BiquadLib.get()
        if lib is None:
            return  # EQのみ無効。理由は _BiquadLib.get() がログへ残している
        handle = lib.eq_create(self._sample_rate, self._channels)
        with self._lock:
            self._lib = lib
            self._handle = handle
            for index in range(_N_BANDS):
                self._apply_band_locked(index)

    def reset(self) -> None:
        """フィルタの内部状態だけを捨てる（設定値は保持する）。"""
        with self._lock:
            if self._handle is not None and self._lib is not None:
                self._lib.eq_reset(self._handle)

    def response_db(self, freq_hz: float) -> float:
        """現在の設定における、指定周波数での実際の効き[dB]を返す。

        GUIの周波数特性グラフ用。実際に音声処理で使っている係数から
        Cの側で直接計算する（RBJ式をPython側に二重実装しないため）。
        :meth:`prepare` 前（未準備）や DLL を読めなかった場合は
        0.0（フラット）を返す。
        """
        with self._lock:
            if self._handle is None or self._lib is None:
                return 0.0
            return self._lib.eq_get_response_db(self._handle, float(freq_hz))

    # ------------------------------------------------------------------
    #  処理
    # ------------------------------------------------------------------
    def process(self, block: np.ndarray) -> np.ndarray:
        """1ブロックを処理して返す。OFF・未準備・空なら ``block`` をそのまま返す。

        Note:
            **ON のときは ``block`` を直接書き換える場合がある。**
            ``block`` が既に float32・C連続なら ``np.ascontiguousarray`` は
            コピーを作らないため、C側が呼び出し元のバッファをそのまま
            処理する。今の呼び出し元（:meth:`audio.live_monitor.LiveMonitor._on_output`）
            は毎回新しい配列を渡すので問題ないが、**入力を後から再利用する
            場合（原音とのミックスやA/B比較など）は、呼び出す前にコピーを
            取ること。**
        """
        if not self.enabled or self._handle is None or self._lib is None:
            return block
        if block.shape[0] == 0:
            return block
        out = np.ascontiguousarray(block, dtype=np.float32)
        ptr = out.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
        with self._lock:
            # ロックを取ってから確かめ直す（待っている間に破棄されうるため）
            if self._handle is None or self._lib is None:
                return block
            self._lib.eq_process(self._handle, ptr, out.shape[0])
        _soft_limit(out)
        return out

    def _destroy_handle(self) -> None:
        lock = getattr(self, "_lock", None)
        if lock is None:
            # __init__ が途中で失敗した場合。C 側のハンドルもまだ無い
            self._handle = None
            return
        with lock:
            handle = getattr(self, "_handle", None)
            lib = getattr(self, "_lib", None)
            self._handle = None
            if handle is not None and lib is not None:
                lib.eq_destroy(handle)

    def __del__(self) -> None:
        # インタプリタ終了時など、属性が揃っていない状態で呼ばれても
        # 「Exception ignored」を出さないよう getattr で読む（_destroy_handle 参照）
        self._destroy_handle()
