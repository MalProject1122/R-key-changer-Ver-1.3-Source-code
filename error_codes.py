"""エラーコード（E2xx など）を、表示用の文字列へ乗せる小さなヘルパー。

このアプリの例外クラスには ``code`` というクラス属性が付いている
（例: ``LiveMonitorError.code == "E202"``）。エラーダイアログのタイトルに
この番号を乗せることで、利用者が問題を報告するときに「E202が出た」の
ように一言で伝えられるようにする。
"""

from __future__ import annotations


def with_code(exc: BaseException, title: str) -> str:
    """例外にエラーコードが付いていれば、ダイアログのタイトルへ乗せる。

    ``code`` 属性を持たない例外が渡ってきても、元のタイトルをそのまま
    返すだけで安全に使える。

    Args:
        exc: 発生した例外。
        title: 元々表示するつもりだったダイアログのタイトル。

    Returns:
        コードがあれば ``"[E202] 元のタイトル"``、無ければ元のタイトル。
    """
    code = getattr(exc, "code", None)
    return f"[{code}] {title}" if code else title
