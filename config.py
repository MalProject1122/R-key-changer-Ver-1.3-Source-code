"""アプリの設定値。

Agastia（元のカラオケ練習アプリ）の ``config.py`` から、この単体アプリ
（再生中の音のキー変更）に実際に必要な値だけを抜き出したもの。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Final

if getattr(sys, "frozen", False):
    # PyInstaller の onefile 版として実行されている場合。
    # ``__file__`` は実行のたびに作られる一時展開フォルダを指してしまい、
    # そこは終了時に消えるため、**設定ファイルの保存先には使えない**
    # （毎回初期状態に戻ってしまう）。実行ファイル（.exe）自身がある
    # フォルダを使うことで、次回起動時も同じ場所を読み書きできるように
    # する。
    BASE_DIR: Final[Path] = Path(sys.executable).resolve().parent
    RESOURCES_DIR: Final[Path] = Path(getattr(sys, "_MEIPASS", str(BASE_DIR)))
    """アイコン等、実行ファイルに同梱した読み取り専用リソースの場所。
    onefile 版では一時展開フォルダ（``sys._MEIPASS``）を指す。
    書き込み先（:data:`BASE_DIR`）とはあえて分けてある。"""
else:
    BASE_DIR: Final[Path] = Path(__file__).resolve().parent
    """このアプリのフォルダ。設定ファイルの保存先に使う。"""
    RESOURCES_DIR: Final[Path] = BASE_DIR

APP_VERSION: Final[str] = "Ver 1.3"

APP_FONT: Final[str] = "BIZ UDGothic"
"""表示フォント。無料の日本語UDフォント。入っていない環境では
Tkinter が既定フォントへ自動でフォールバックする。"""

SAMPLE_RATE: Final[int] = 48000
"""既定のサンプリングレート[Hz]。デバイス側と合わなければ自動調整される。"""

CHANNELS: Final[int] = 1

BLOCK_SIZE: Final[int] = 1536
"""音声コールバック1回あたりのサンプル数。"""

ASSETS_DIR: Final[Path] = RESOURCES_DIR / "assets"
"""アイコン等の静的ファイル（読み取り専用）を置くフォルダ。"""

DEVELOPER_NAME: Final[str] = "Malta"
DEVELOPER_ICON_PATH: Final[Path] = ASSETS_DIR / "developer_icon.png"
DEVELOPER_GITHUB_URL: Final[str] = "https://github.com/MalProject1122/"
"""？ボタンの「開発者情報」タブに出す、問い合わせ先の情報。"""
