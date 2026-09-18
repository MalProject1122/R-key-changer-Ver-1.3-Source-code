"""R-key changer のエントリポイント。

実行方法::

    pip install -r requirements.txt
    python main.py

このアプリは「再生中の音をキー変更して聴く」機能だけの単体版です。
パソコンで鳴っている音を、その場で半音単位に移調して聴きます。
録音・保存は一切しません。
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path

# スクリプトのあるフォルダを import パスに加える
# （どのカレントディレクトリから起動しても動くようにするため）
_BASE_DIR = Path(__file__).resolve().parent
if str(_BASE_DIR) not in sys.path:
    sys.path.insert(0, str(_BASE_DIR))

logger = logging.getLogger("r_key_changer")


def _setup_logging() -> Path | None:
    """ログを %LOCALAPPDATA%\\R-key changer\\logs へ書き出す設定をする。

    exe 版は console=False でコンソールが無く、例外のスタックトレースが
    どこにも残らないため、不具合の原因を追えるようファイルへ残す。
    書き込めない環境でもアプリ本体は動かしたいので、失敗したら諦める。

    Returns:
        ログファイルのパス。設定できなかったら None。
    """
    base = os.environ.get("LOCALAPPDATA")
    log_dir = Path(base) / "R-key changer" / "logs" if base else _BASE_DIR / "logs"
    log_path = log_dir / "r-key-changer.log"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
    except OSError:
        return None
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    )
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    # comtypes は COM を使うたびにキャッシュの INFO を出してログを埋めるため
    logging.getLogger("comtypes").setLevel(logging.WARNING)

    def log_uncaught(exc_type, exc, tb) -> None:  # noqa: ANN001
        if not issubclass(exc_type, KeyboardInterrupt):
            logger.critical("未処理の例外", exc_info=(exc_type, exc, tb))
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = log_uncaught
    return log_path


def main() -> int:
    """キー変更ウィンドウを単体で起動する。

    Returns:
        終了コード。正常終了なら 0。
    """
    try:
        import customtkinter as ctk
    except ImportError as exc:
        print(f"必要なライブラリが不足しています: {exc}", file=sys.stderr)
        print("  pip install -r requirements.txt", file=sys.stderr)
        return 1

    import config
    from audio.app_settings import load_settings
    from gui.live_monitor_window import LiveMonitorWindow

    log_path = _setup_logging()
    logger.info(
        "起動 %s (Python %s, frozen=%s, log=%s)",
        config.APP_VERSION,
        sys.version.split()[0],
        getattr(sys, "frozen", False),
        log_path,
    )

    ctk.set_appearance_mode("dark")

    # 見えないルートウィンドウを1枚だけ作り、キー変更画面をその子として
    # 開く。LiveMonitorWindow は元々「アプリ本体の一部として開く別
    # ウィンドウ」として作られており、``master.settings`` だけを読む
    # ので、ここに settings を持たせれば無改造のまま流用できる。
    root = ctk.CTk()
    root.withdraw()
    root.settings = load_settings()

    def report_callback_exception(exc_type, exc, tb) -> None:  # noqa: ANN001
        # ボタン等のコールバック内の例外。Tk の既定は stderr へ出すだけで、
        # exe（コンソール無し）では誰にも見えないためログへ残す。
        logger.error("GUI コールバックで例外", exc_info=(exc_type, exc, tb))

    root.report_callback_exception = report_callback_exception

    window = LiveMonitorWindow(root)

    def on_window_destroy(event: object) -> None:
        # ウィンドウ自身の破棄イベントだけを見る（子ウィジェットの
        # 破棄でも <Destroy> は伝播するため）。
        if getattr(event, "widget", None) is window:
            root.destroy()

    window.bind("<Destroy>", on_window_destroy)

    try:
        root.mainloop()
    except KeyboardInterrupt:
        return 0
    finally:
        logger.info("終了")
    return 0


if __name__ == "__main__":
    sys.exit(main())
