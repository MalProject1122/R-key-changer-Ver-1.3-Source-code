"""PC全体のメモリ使用率・CPU使用率を取得する（C実装のDLL経由）。

Windows APIを直接叩く部分だけをC（:mod:`native.sysinfo` のビルド元
``native/sysinfo.c``）へ切り出し、Pythonからは ``ctypes`` 越しに薄く
呼ぶだけにしてある。numpyのような重い依存も増やさず、開発環境
（Python未フリーズ時）と exe化後（PyInstaller onefile）の両方で同じ
相対パスから読み込めるよう :data:`config.RESOURCES_DIR` を使う。
"""

from __future__ import annotations

import ctypes
from typing import Optional

import config

_DLL_PATH = config.RESOURCES_DIR / "native" / "sysinfo.dll"


class SysInfoUnavailableError(Exception):
    """DLLが読み込めない、または呼び出しに失敗した場合。"""


class SystemMonitor:
    """PC全体のメモリ・CPU使用率を取得する。

    ``sysinfo_get_cpu_percent`` はWindowsの仕様上、瞬間値ではなく
    前回呼び出しとの差分からしか計算できない。そのため、このクラスの
    インスタンスを1つ保持し続け、一定間隔で :meth:`cpu_percent` を
    呼び出す想定（呼び出しごとに新しいインスタンスを作ると、毎回
    「初回はデータなし」の -1.0 が返ってしまう）。
    """

    _handle: Optional[int] = None
    """クラス属性としても定義しておく。:meth:`__init__` が最後まで進まなくても
    :meth:`__del__` → :meth:`close` が ``AttributeError`` にならないようにするため。"""

    _lib: Optional[ctypes.CDLL] = None

    def __init__(self) -> None:
        try:
            self._lib = ctypes.CDLL(str(_DLL_PATH))
            self._lib.sysinfo_create.restype = ctypes.c_void_p
            self._lib.sysinfo_destroy.argtypes = [ctypes.c_void_p]
            self._lib.sysinfo_get_memory.argtypes = [
                ctypes.POINTER(ctypes.c_ulonglong), ctypes.POINTER(ctypes.c_ulonglong)
            ]
            self._lib.sysinfo_get_memory.restype = ctypes.c_int
            self._lib.sysinfo_get_cpu_percent.argtypes = [ctypes.c_void_p]
            self._lib.sysinfo_get_cpu_percent.restype = ctypes.c_double
            self._handle = self._lib.sysinfo_create()
        except OSError as exc:
            # DLLが無い・隔離された・アーキテクチャ不一致
            raise SysInfoUnavailableError(f"sysinfo.dll を読み込めません: {exc}") from exc
        except AttributeError as exc:
            # DLLは読めたが関数が見つからない（古い・壊れたDLLが残っている等）
            raise SysInfoUnavailableError(
                f"sysinfo.dll の内容が想定と異なります: {exc}"
            ) from exc
        if not self._handle:
            raise SysInfoUnavailableError("sysinfo_create に失敗しました")

    def memory(self) -> tuple[int, int]:
        """PC全体の物理メモリ使用量を返す。

        Returns:
            ``(使用中バイト数, 合計バイト数)``。

        Raises:
            SysInfoUnavailableError: 取得に失敗した場合。
        """
        total = ctypes.c_ulonglong()
        used = ctypes.c_ulonglong()
        ok = self._lib.sysinfo_get_memory(ctypes.byref(total), ctypes.byref(used))
        if not ok:
            raise SysInfoUnavailableError("メモリ情報を取得できませんでした")
        return used.value, total.value

    def cpu_percent(self) -> Optional[float]:
        """PC全体のCPU使用率[%]を返す。

        Returns:
            0〜100の値。直前の呼び出しからの経過が無い最初の1回だけは
            比較対象が無いため ``None``（呼び出し側は前回値を維持する）。
        """
        value = self._lib.sysinfo_get_cpu_percent(self._handle)
        return None if value < 0 else value

    def close(self) -> None:
        """確保したハンドルを解放する。何度呼んでも安全。"""
        # __init__ が途中で失敗した場合や、インタプリタ終了時に属性が
        # 片付いた後から呼ばれても例外を出さないよう getattr で読む
        # （__del__ 内の例外は「Exception ignored」として標準エラーへ
        # 出続け、本当の不具合を追うときのノイズになるため）。
        handle = getattr(self, "_handle", None)
        lib = getattr(self, "_lib", None)
        if handle and lib is not None:
            lib.sysinfo_destroy(handle)
        self._handle = None

    def __del__(self) -> None:
        self.close()
