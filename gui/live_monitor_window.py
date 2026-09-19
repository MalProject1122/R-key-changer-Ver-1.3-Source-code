"""再生中の音をキー変更して聴く画面（🎚）。

:class:`audio.live_monitor.LiveMonitor` の操作パネル。仕組みそのものは
そちらのモジュール docstring を参照。この画面は

* VB-CABLE の配線手順を案内する
* 入力（横取り元）と出力（実際に鳴らす先）を選ばせる
* キーを ±12 半音で動かす
* 動いているかどうかを表示する

だけを受け持ち、音の処理には一切関わらない。

■ なぜ手順の案内を画面に置くのか

この機能は**アプリの外（Windows のサウンド設定）を先に直さないと
音が出ない**。手順書を別ファイルにすると、音が出ない人が必ず
「アプリの不具合」だと判断してしまうため、操作する場所のすぐ上に
手順を並べてある。
"""

from __future__ import annotations

import atexit
import logging
import math
import subprocess
import tkinter as tk
import urllib.parse
import webbrowser
import winreg
from pathlib import Path
from tkinter import messagebox
from typing import Any

import customtkinter as ctk

import config
from audio import default_device
from audio.app_settings import save_settings
from audio.equalizer import BAND_FREQS as EQ_BAND_FREQS
from audio.equalizer import CUSTOM_PRESET as EQ_CUSTOM_PRESET
from audio.equalizer import GAIN_MAX_DB, GAIN_MIN_DB
from audio.equalizer import PRESETS as EQ_PRESETS
from audio.equalizer import preset_name_for as eq_preset_name_for
from gui.sysinfo_monitor import SysInfoUnavailableError, SystemMonitor
from audio.live_monitor import (
    BLOCK_SIZE_OPTIONS,
    KEY_SHIFT_MAX,
    KEY_SHIFT_MIN,
    LiveMonitor,
    LiveMonitorError,
    list_input_devices,
    list_output_devices,
    rescan_devices,
    suggest_input_device,
    suggest_output_device,
)
from audio.live_space import (
    CUSTOM_PRESET,
    DECAY_MAX_S,
    DECAY_MIN_S,
    ECHO_TIME_MAX_MS,
    ECHO_TIME_MIN_MS,
    OUTPUT_MODES,
    PERCENT_MAX,
    PERCENT_MIN,
    PREDELAY_MAX_MS,
    PREDELAY_MIN_MS,
    PRESETS,
    WIDTH_MAX,
    WIDTH_MIN,
    params_from_dict,
    params_to_dict,
    preset_name_for,
)
from error_codes import with_code
from gui import theme

logger = logging.getLogger(__name__)

_DOCS: dict[str, str] = {
    "README": "README.md",
    "説明書": "説明書.md",
}
"""？ボタンのヘルプ画面で読み込むファイル。``config.BASE_DIR`` 直下から探す。"""

_GUIDE_HTML_PATH = config.ASSETS_DIR / "guide.html"
"""初めて使う人向けの、読みやすいHTML版説明書。:func:`_on_open_guide_html`
でブラウザから開く。README/説明書.md（開発者向けのそのままの原稿）とは別に
用意してあり、目次からのジャンプとブラウザの Ctrl+F 検索に対応する。"""

_SAVE_DEBOUNCE_MS = 500
"""音響の設定を変えてから保存するまで待つ時間[ミリ秒]。

スライダーをドラッグすると 1 秒に数十回値が変わるため、そのたびに保存すると
GUI スレッドでディスク書き込みが連続してしまう。動きが止まってから 1 回だけ
保存する（:meth:`LiveMonitorWindow._save_live_space_settings`）。
"""

_POLL_MS = 500
"""状態表示を更新する間隔[ミリ秒]。音の処理とは無関係の見た目だけの周期。"""

_DEFAULT_BLOCK_SIZE = 2048
"""⚙ 詳細設定の「初期設定に戻す」が使う既定値。
:data:`audio.live_monitor._BLOCK_SIZE` と同じ値にしてある。"""

_YOUTUBE_URL = "https://www.youtube.com"

_YOUTUBE_SEARCH_URL = "https://www.youtube.com/results?search_query={query}"
"""検索欄に文字が入っているときに開く先。``{query}`` は URL エンコード済みの
検索語。特定の1本の動画へ直接飛ばすのはYouTube側のAPI/スクレイピングが
必要で不安定になるため、検索結果ページに飛ぶだけに留めている。"""

_INCOGNITO_FLAGS: dict[str, str] = {
    "chrome.exe": "--incognito",
    "msedge.exe": "--inprivate",
    "brave.exe": "--incognito",
    "vivaldi.exe": "--incognito",
    "firefox.exe": "--private-window",
    "opera.exe": "--private",
}
"""ブラウザの実行ファイル名（小文字）→ シークレット/プライベートモードで
起動するためのコマンドライン引数。"""

_BROWSER_LABELS: dict[str, str] = {
    "Chrome": "chrome.exe",
    "Edge": "msedge.exe",
    "Firefox": "firefox.exe",
    "Brave": "brave.exe",
    "Vivaldi": "vivaldi.exe",
    "Opera": "opera.exe",
}
"""⇩ボタンの選択肢（表示名）→ 実行ファイル名。:data:`_INCOGNITO_FLAGS` の
キーと対応させてあるので、ここに無いブラウザを選ばせることはできない。"""

_DEFAULT_BROWSER_LABEL = "Chrome"


def find_browser_path(exe_name: str) -> str | None:
    """Windows の「App Paths」レジストリから、指定した実行ファイルの
    インストール場所を探す。

    ユーザーが⇩で選んだブラウザは既定のブラウザとは限らないため、
    「既定のアプリ」設定ではなく、Windowsのインストーラーが実行ファイル名
    ごとに登録するこの仕組みを使う（Chrome/Edge/Firefox等の主要ブラウザは
    インストール時に自動登録される）。

    Args:
        exe_name: 実行ファイル名（例: ``"chrome.exe"``）。

    Returns:
        実行ファイルの絶対パス。見つからなければ None
        （未インストール、または対応していないブラウザ）。
    """
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(
                hive,
                r"Software\Microsoft\Windows\CurrentVersion\App Paths"
                rf"\{exe_name}",
            ) as key:
                path, _ = winreg.QueryValueEx(key, "")
        except OSError:
            continue
        if path and Path(path).exists():
            return path
    return None

_SETUP_STEPS: tuple[tuple[str, str], ...] = (
    (
        "① VB-CABLE を入れる",
        "vb-audio.com の「VB-CABLE Virtual Audio Device」を入れて"
        "パソコンを再起動します（無償。1 回だけ）。",
    ),
    (
        "② 聴きたい音楽を再生する",
        "Apple Music・YouTube など、いつも通り再生するだけです。"
        "ファイルの保存やダウンロードは行いません。",
    ),
    (
        "③ 下でデバイスを選ぶ",
        "入力に CABLE Output、出力に実際のヘッドホン／スピーカー。"
        "出力に CABLE Input を選ぶと音が回るので選べません。",
    ),
    (
        "④ 開始を押してキーを動かす",
        "終わったら「停止」を押すか画面を閉じれば、既定の出力は"
        "自動で元に戻ります。",
    ),
)
"""画面上部に並べる手順。``(見出し, 説明)`` の並び。

**「既定の出力を CABLE Input にする」という手順が無いのは、この画面を
開いた時点でアプリが自動で切り替えるため**（:meth:`_auto_switch_to_cable`
参照）。``comtypes``/Windows が使えない環境など、自動切り替えに失敗した
場合だけ :meth:`_build_guide` が手動手順を追加で表示する。
"""

_MANUAL_STEP_2 = (
    "（自動切り替えに失敗した場合）既定の出力を CABLE Input にする",
    "設定 ＞ システム ＞ サウンド の「出力」を手動で CABLE Input に"
    "変えてください。ここでスピーカーから音が消えるのが正常です。",
)
"""自動切り替えができなかったときだけ :data:`_SETUP_STEPS` へ追加する手順。"""

_NO_DEVICE = "（見つかりません）"
"""デバイスが 1 つも無いときにメニューへ出す文字列。"""


class LiveMonitorWindow(ctk.CTkToplevel):
    """再生中の音をキー変更して聴くためのウィンドウ。

    Attributes:
        monitor: 音を横取りして鳴らし直す本体。
    """

    def __init__(self, master: Any) -> None:
        """LiveMonitorWindow を初期化する。

        Args:
            master: 親ウィンドウ。
        """
        super().__init__(master)

        self.monitor = LiveMonitor()

        self._settings = master.settings
        """設定の実体（:class:`gui.main_window.AnalyzerFrame` が持つもの）を
        直接参照する。手順パネルを畳んだ状態を次回起動へ引き継ぐため
        （:attr:`_guide_collapsed` 参照）。
        """
        self._output_devices: list[dict[str, Any]] = []

        self._original_output_name = self._resolve_original_output_name()
        """開いた時点の既定出力デバイス名。停止・終了時に、既定が
        まだ CABLE 系のままなら元へ戻すのに使う
        （:meth:`_restore_default_output` 参照）。取得できなくても
        None のまま進む（この機能自体が任意の利便性用途のため）。

        **CABLE Input へ自動切り替えする前に取得すること。** 切り替えた
        後に取得すると「元の値」が CABLE Input になってしまい、
        戻し先が分からなくなる。
        """
        self._original_output_ids = self._capture_original_output_ids()
        """役割ごとの「本来の」既定出力デバイスID。:meth:`_capture_original_output_ids`
        参照。これも CABLE Input へ切り替える前に取ること。"""
        self._auto_switch_succeeded = self._auto_switch_to_cable()

        # ここから先で失敗・異常終了すると、既定の出力が CABLE Input の
        # まま残り PC 全体が無音になる。未処理例外・Ctrl+C・sys.exit で
        # プロセスが終わる経路は atexit で、構築中の例外はその場で戻す。
        # （強制終了だけはコードで防げないため、_resolve_original_output_name
        # が次回起動時に戻す。）
        atexit.register(self._restore_default_output_quietly)
        try:
            self._build_window()
        except BaseException:
            self._restore_default_output_quietly()
            raise

    def _build_window(self) -> None:
        """既定出力を切り替えた後の初期化（ウィジェットの構築など）。"""
        self._guide_collapsed = self._settings.live_monitor_guide_seen

        # 保存されている値が候補（1024/2048/4096）から外れていたら
        # （設定ファイルを直接編集された等）既定へフォールバックする。
        if self._settings.key_changer_block_size in BLOCK_SIZE_OPTIONS:
            self.monitor.block_size = self._settings.key_changer_block_size
        else:
            self._settings.key_changer_block_size = self.monitor.block_size

        self._restore_live_space_settings()
        self._restore_eq_settings()

        self._font_title = ctk.CTkFont(family=config.APP_FONT, size=15, weight="bold")
        self._font_label = ctk.CTkFont(family=config.APP_FONT, size=13)
        self._font_small = ctk.CTkFont(family=config.APP_FONT, size=11)
        self._font_eyebrow = ctk.CTkFont(
            family=config.APP_FONT, size=11, weight="bold"
        )
        self._font_key = ctk.CTkFont(family=theme.FONT_NUM, size=34, weight="bold")
        self._font_mini = ctk.CTkFont(family=config.APP_FONT, size=10)
        """音響タブのグリッドカード内、スライダー名に使う小さめのフォント。
        ラベルが日本語（「フィードバック」等）でも 92px 幅に収まるように、
        :attr:`_font_small` よりひと回り小さくしてある。"""

        self._input_devices: list[dict[str, Any]] = []
        self._output_devices: list[dict[str, Any]] = []
        self._poll_id: str | None = None
        self._save_after_id: str | None = None
        self._help_window: ctk.CTkToplevel | None = None
        self._sysinfo_window: ctk.CTkToplevel | None = None
        self._sysinfo_after_id: str | None = None
        self._system_monitor: SystemMonitor | None = None
        """遅延初期化する（:meth:`_on_open_sysinfo` 参照）。CPU使用率は
        前回呼び出しとの差分でしか計算できないため、ポップアップを
        開き直しても同じインスタンスを使い続ける。"""
        self._developer_icon_photo: tk.PhotoImage | None = None
        """開発者情報タブのアイコン画像の参照保持用（手放すと表示が消える）。"""

        self.title(f"key changer {config.APP_VERSION}")
        self.geometry("960x860")
        self.minsize(800, 700)
        self.configure(fg_color=theme.BG_DEEP)
        # 設定画面と同じ理由で transient は付けない（□ を残すため）

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        self._build_header()

        # 「接続」「キー」「音響」「EQ」をタブで分ける。以前は 1 画面に
        # 全部積み上げていて、LIVE SPACE の詳細スライダーまで含めると
        # ウィンドウの高さを大きく超え、常にスクロールが必要だった。
        # タブに分け、音響タブの中身も切り替えではなくグリッドで
        # まとめることで、どのタブを開いてもウィンドウ内に収まる
        # ようにしてある（:meth:`_build_live_space_panel` 参照）。
        # タブの見出しは左上（検索欄のすぐ下）に来るよう anchor="w" にする
        # （既定の中央寄せだと視線が迷うという指摘を受けて変更）。
        # マイプリセットの登録・読み込みは、EQ・音響の両方にまたがる機能
        # だが、実際に操作するのはほぼ音響タブなので、そこに集約してある
        # （:meth:`_build_live_space_panel` の「マイプリセット」「保存」参照）。
        self._tabview = ctk.CTkTabview(
            self,
            anchor="w",
            fg_color=theme.BG_DEEP,
            segmented_button_fg_color=theme.PANEL,
            segmented_button_selected_color=theme.over(theme.CYAN, theme.PANEL, 0.45),
            segmented_button_selected_hover_color=theme.over(
                theme.CYAN, theme.PANEL, 0.6
            ),
            segmented_button_unselected_color=theme.PANEL,
            segmented_button_unselected_hover_color=theme.over(
                theme.CYAN, theme.PANEL, 0.2
            ),
            segmented_button_font=self._font_label,
            text_color=theme.TEXT_DIM,
        )
        self._tabview.grid(row=1, column=0, sticky="nsew", padx=20, pady=(12, 0))
        self._tab_conn = self._tabview.add("🔌 接続")
        self._tab_key = self._tabview.add("🎚 キー")
        self._tab_acoustic = self._tabview.add("🔊 音響")
        self._tab_eq = self._tabview.add("🎛 EQ")

        # タブに分けたことで、ほとんどの環境ではもうスクロールせずに
        # 収まる。ただし高 DPI・小さいノート PC 画面など、収まらない
        # 場合の保険として各タブの中身は個別にスクロール可能にしておく
        # （以前の :class:`ctk.CTkScrollableFrame` と同じ考え方）。
        self._conn_scroll = self._make_tab_scroll(self._tab_conn)
        self._key_scroll = self._make_tab_scroll(self._tab_key)
        self._acoustic_scroll = self._make_tab_scroll(self._tab_acoustic)
        self._eq_scroll = self._make_tab_scroll(self._tab_eq)

        self._build_guide()
        self._build_key_panel()
        self._build_live_space_panel()
        self._build_eq_panel()
        self._build_footer()
        self._build_advanced_settings_window()

        self.refresh_devices()
        self._update_state_label()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._schedule_poll()

        # **既定の出力デバイスを切り替える COM 呼び出し（上の
        # _auto_switch_to_cable）は、Windows 側で「音声デバイスが
        # 変わった」イベントとして扱われ、まれにこのウィンドウが背面へ
        # 回ってしまうことがある（実機で確認済み）。作り終えた直後に
        # 明示的に前面へ出しておく。
        theme.bring_to_front(self)

    # ------------------------------------------------------------------
    #  組み立て
    # ------------------------------------------------------------------
    @staticmethod
    def _make_tab_scroll(tab: Any) -> ctk.CTkScrollableFrame:
        """タブの中身を置く、そのタブいっぱいに広がるスクロール枠を作る。

        Args:
            tab: ``CTkTabview.add()`` が返すタブのフレーム。

        Returns:
            列0を伸縮させた :class:`ctk.CTkScrollableFrame`。
        """
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(0, weight=1)
        scroll = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        scroll.grid(row=0, column=0, sticky="nsew")
        scroll.grid_columnconfigure(0, weight=1)
        return scroll

    def _build_header(self) -> None:
        """見出しの帯を作る。タブの外（常に見える位置）に置く。"""
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=20, pady=(16, 0))
        header.grid_columnconfigure(0, weight=1)

        title_row = ctk.CTkFrame(header, fg_color="transparent")
        title_row.grid(row=0, column=0, sticky="w")

        ctk.CTkLabel(
            title_row,
            text="Key changer",
            font=self._font_title,
            text_color=theme.TEXT,
            anchor="w",
        ).pack(side="left")

        ctk.CTkLabel(
            title_row,
            text=config.APP_VERSION,
            font=self._font_small,
            text_color=theme.MUTED,
        ).pack(side="left", padx=(6, 0))

        # 入力・出力デバイスが揃っているかどうかを、一目で分かる●で示す
        # （詳しい理由は接続確認パネルの案内文へ譲り、ここは色だけ見れば
        # 済むようにする）。実際の判定は :meth:`_update_state_label` 参照。
        self._connection_dot = ctk.CTkLabel(
            title_row,
            text="●",
            font=self._font_label,
            text_color=theme.DANGER,
            width=18,
        )
        self._connection_dot.pack(side="left", padx=(12, 2))

        self._connection_status_label = ctk.CTkLabel(
            title_row,
            text="未接続",
            font=self._font_small,
            text_color=theme.MUTED,
        )
        self._connection_status_label.pack(side="left", padx=(0, 8))

        connection_button = ctk.CTkButton(
            title_row,
            text="接続設定",
            font=self._font_small,
            width=84,
            height=26,
            command=self._on_open_connection_settings,
        )
        connection_button.pack(side="left")
        theme.style_ghost_button(connection_button, theme.CYAN)

        # 音飛びの起きやすさ（コールバックサイズ）は環境（PC・オーディオ
        # デバイス）によって最適値が変わるため、詳細設定として調整可能に
        # してある（:meth:`_build_advanced_settings_window` 参照）。
        advanced_button = ctk.CTkButton(
            title_row,
            text="⚙",
            font=self._font_label,
            width=32,
            height=26,
            command=self._on_open_advanced_settings,
        )
        advanced_button.pack(side="left", padx=(6, 0))
        theme.style_ghost_button(advanced_button, theme.MUTED)

        # 今のPC全体のメモリ・CPU使用率を確認するボタン。⚙のすぐ隣に置く。
        sysinfo_button = ctk.CTkButton(
            title_row,
            text="📊",
            font=self._font_label,
            width=32,
            height=26,
            command=self._on_open_sysinfo,
        )
        sysinfo_button.pack(side="left", padx=(6, 0))
        theme.style_ghost_button(sysinfo_button, theme.MUTED)

        # README・説明書・問い合わせ先（開発者情報）をまとめて見られる
        # ヘルプ画面を開く。「サイトオーナーへの問い合わせ先が分からない」
        # という声を受けて追加した。⚙（詳細設定）のすぐ隣に置き、
        # 「設定まわりのボタンはここにまとまっている」と分かるようにする。
        help_button = ctk.CTkButton(
            title_row,
            text="？",
            font=self._font_label,
            width=32,
            height=26,
            command=self._on_open_help,
        )
        help_button.pack(side="left", padx=(6, 0))
        theme.style_ghost_button(help_button, theme.MUTED)

        self._build_youtube_row(header)

        rule = theme.AccentRule(header, colors=(theme.VIOLET, theme.CYAN))
        rule.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))

    # ------------------------------------------------------------------
    #  マイプリセット（EQ + 音響をまとめて名前で保存）
    # ------------------------------------------------------------------
    def _on_open_register_dialog(self) -> None:
        """「マイプリセット登録」ボタンの処理。

        名前入力欄の下に「音響の設定を保存」「EQの設定を保存」の
        チェックを並べた小さなウィンドウを開く。片方だけチェックを
        外せば、EQだけ・音響だけを保存することもできる。
        """
        dialog = ctk.CTkToplevel(self)
        dialog.title("マイプリセット登録")
        dialog.geometry("360x260")
        dialog.resizable(False, False)
        dialog.configure(fg_color=theme.BG_DEEP)
        dialog.transient(self)
        dialog.grab_set()

        ctk.CTkLabel(
            dialog, text="プリセット名", font=self._font_eyebrow, text_color=theme.MUTED, anchor="w"
        ).pack(anchor="w", padx=20, pady=(20, 4))
        name_entry = ctk.CTkEntry(
            dialog, font=self._font_label, height=32,
            fg_color=theme.BG_DEEP, border_color=theme.PANEL_EDGE,
        )
        name_entry.pack(fill="x", padx=20)
        name_entry.focus_set()

        save_live_space_var = tk.BooleanVar(value=True)
        save_eq_var = tk.BooleanVar(value=True)
        for text, var in (
            ("音響の設定を保存", save_live_space_var),
            ("EQの設定を保存", save_eq_var),
        ):
            ctk.CTkCheckBox(
                dialog, text=text, font=self._font_label, variable=var,
                checkbox_width=18, checkbox_height=18, corner_radius=4, border_width=1.5,
                fg_color=theme.VIOLET, hover_color=theme.VIOLET_HOVER,
                border_color=theme.over(theme.VIOLET, theme.PANEL, 0.55),
                checkmark_color=theme.BG_DEEP,
                text_color=theme.TEXT_DIM,
            ).pack(anchor="w", padx=20, pady=(14, 0))

        error_label = ctk.CTkLabel(
            dialog, text="", font=self._font_small, text_color=theme.DANGER
        )
        error_label.pack(anchor="w", padx=20, pady=(8, 0))

        def confirm() -> None:
            name = name_entry.get().strip()
            if not name:
                error_label.configure(text="名前を入力してください")
                return
            if not save_live_space_var.get() and not save_eq_var.get():
                error_label.configure(text="どちらか1つはチェックしてください")
                return
            if name in self._settings.sound_profiles and not messagebox.askyesno(
                "上書きの確認", f"「{name}」は既にあります。上書きしますか？", parent=dialog
            ):
                return
            self._settings.sound_profiles[name] = self._snapshot_sound_profile(
                include_live_space=save_live_space_var.get(),
                include_eq=save_eq_var.get(),
            )
            save_settings(self._settings)
            self._refresh_sound_profile_menu(select=name)
            dialog.destroy()

        name_entry.bind("<Return>", lambda _e: confirm())

        button_row = ctk.CTkFrame(dialog, fg_color="transparent")
        button_row.pack(fill="x", padx=20, pady=(16, 20), side="bottom")
        cancel_button = ctk.CTkButton(
            button_row, text="キャンセル", font=self._font_small, height=32,
            command=dialog.destroy,
        )
        cancel_button.pack(side="left", expand=True, fill="x", padx=(0, 6))
        theme.style_ghost_button(cancel_button, theme.MUTED)
        confirm_button = ctk.CTkButton(
            button_row, text="登録", font=self._font_small, height=32, command=confirm,
        )
        confirm_button.pack(side="left", expand=True, fill="x", padx=(6, 0))
        theme.style_solid_button(confirm_button, theme.CYAN, theme.CYAN_HOVER)

    def _refresh_sound_profile_menu(self, *, select: str | None = None) -> None:
        """保存済みのプリセット名一覧でドロップダウンを作り直す。

        Args:
            select: 更新後に選択状態にする名前。None なら「（未選択）」。
        """
        names = sorted(self._settings.sound_profiles)
        values = names if names else ["（未選択）"]
        self._sound_profile_menu.configure(values=values)
        self._sound_profile_var.set(select if select in names else "（未選択）")
        has_selection = select in names
        self._sound_profile_delete_button.configure(
            state="normal" if has_selection else "disabled"
        )

    def _snapshot_sound_profile(
        self, *, include_live_space: bool = True, include_eq: bool = True
    ) -> dict[str, Any]:
        """今の EQ・音響の状態を、保存用の辞書にまとめる。

        Args:
            include_live_space: 音響（Live Space）の項目を含めるか。
            include_eq: EQ の項目を含めるか。両方 False にはしない
                （呼び出し側の :meth:`_on_open_register_dialog` で防止済み）。
        """
        snapshot: dict[str, Any] = {}
        if include_eq:
            eq = self.monitor.equalizer
            snapshot.update(
                eq_enabled=bool(eq.enabled),
                eq_preset=eq_preset_name_for(eq.gains_db),
                eq_gains=list(eq.gains_db),
            )
        if include_live_space:
            space = self.monitor.live_space
            snapshot.update(
                live_space_enabled=bool(space.enabled),
                live_space_preset=preset_name_for(space.params),
                live_space_output_mode=space.output_mode,
                live_space_binaural=bool(space.binaural),
                live_space_params=params_to_dict(space.params),
            )
        return snapshot

    def _on_delete_sound_profile(self) -> None:
        """「🗑」の処理。選択中のプリセットを削除する。"""
        name = self._sound_profile_var.get()
        if name not in self._settings.sound_profiles:
            return
        if not messagebox.askyesno(
            "削除の確認", f"「{name}」を削除しますか？", parent=self
        ):
            return
        del self._settings.sound_profiles[name]
        save_settings(self._settings)
        self._refresh_sound_profile_menu()

    def _on_apply_sound_profile(self, name: str) -> None:
        """プリセット選択の処理。EQ・音響へまとめて反映する。

        壊れた・古い形式のエントリでも落ちないよう、各項目は個別に
        ``.get()`` で読み、無ければ何もしない（既存の値を保つ）。
        """
        profile = self._settings.sound_profiles.get(name)
        if profile is None:
            return

        eq = self.monitor.equalizer
        if "eq_gains" in profile:
            eq.set_gains_db(tuple(profile["eq_gains"]))
        eq.enabled = bool(profile.get("eq_enabled", eq.enabled))
        self._eq_var.set(eq.enabled)
        self._sync_eq_controls()
        self._eq_preset_var.set(eq_preset_name_for(eq.gains_db))
        self._apply_eq_enabled_look()
        self._save_eq_settings()

        space = self.monitor.live_space
        if isinstance(profile.get("live_space_params"), dict):
            space.set_params(params_from_dict(profile["live_space_params"]))
        if profile.get("live_space_output_mode") in OUTPUT_MODES:
            space.output_mode = profile["live_space_output_mode"]
        space.binaural = bool(profile.get("live_space_binaural", space.binaural))
        space.enabled = bool(profile.get("live_space_enabled", space.enabled))
        self._live_space_var.set(space.enabled)
        self._sync_live_space_controls()
        self._preset_var.set(preset_name_for(space.params))
        self._output_mode_var.set("スピーカー" if space.output_mode == "speaker" else "ヘッドホン")
        self._binaural_var.set(space.binaural)
        self._apply_binaural_availability()
        self._apply_live_space_enabled_look()
        self._save_live_space_settings()

        self._refresh_sound_profile_menu(select=name)

    def _build_youtube_row(self, master: Any) -> None:
        """YouTube検索の行（検索欄・🔍検索・👻シークレット切替・ブラウザ選択）を作る。

        歌ってみたい曲をすぐ探しに行けるようにするための、ただの
        ショートカット。このアプリ自体は音の横取り・再生にしか関わらない
        ので、ブラウザを開く操作それ自体はアプリの動作に一切影響しない。

        「シークレット」はボタンではなくチェックボックスにしてある
        （オンのまま何度も検索する使い方の方が自然なため。押すたびに
        別モードで開く2つのボタンより、チェックのオン/オフで済む方が
        分かりやすい）。「ブラウザを選択」で選んだブラウザが、検索・
        シークレットどちらにも使われる（Windows の既定のブラウザとは
        無関係。選択は :attr:`_selected_browser_label` に保持し、
        ウィンドウを開き直すと :data:`_DEFAULT_BROWSER_LABEL` へ戻る＝
        保存はしない）。

        Args:
            master: 置き先（ヘッダーの内部フレーム）。
        """
        self._selected_browser_label = _DEFAULT_BROWSER_LABEL
        self._incognito_var = tk.BooleanVar(value=False)

        # 手順パネル（_build_guide）と同じ「PANEL色のカード」の見た目に
        # 揃え、独立した機能だと分かるように区切る。
        panel = ctk.CTkFrame(
            master, fg_color=theme.PANEL, corner_radius=12, border_width=0
        )
        panel.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        panel.grid_columnconfigure(0, weight=1)

        # 見出しの行に「シークレット」「ブラウザを選択」を並べる
        # （以前は検索欄の下の別行にあったが、見出しのすぐ隣にある方が
        # まとまって見えて分かりやすいという要望による）。
        title_row = ctk.CTkFrame(panel, fg_color="transparent")
        title_row.grid(row=0, column=0, sticky="ew", padx=14, pady=(10, 4))

        ctk.CTkLabel(
            title_row,
            text="🎵 YouTubeで曲を探す",
            font=self._font_small,
            text_color=theme.MUTED,
        ).pack(side="left")

        incognito_check = ctk.CTkCheckBox(
            title_row,
            text="👻シークレットモード",
            font=self._font_small,
            variable=self._incognito_var,
            checkbox_width=18,
            checkbox_height=18,
            corner_radius=4,
            border_width=1.5,
            fg_color=theme.VIOLET,
            hover_color=theme.VIOLET_HOVER,
            border_color=theme.over(theme.VIOLET, theme.PANEL, 0.55),
            checkmark_color=theme.BG_DEEP,
            text_color=theme.blend(theme.VIOLET, theme.TEXT, 0.35),
        )
        incognito_check.pack(side="left", padx=(14, 10))

        self._browser_button = ctk.CTkButton(
            title_row,
            text=f"ブラウザを選択：{self._selected_browser_label} ⇩",
            font=self._font_small,
            width=170,
            height=28,
            command=self._on_open_browser_menu,
        )
        self._browser_button.pack(side="left")
        theme.style_ghost_button(self._browser_button, theme.CYAN)

        # 検索欄の右端に「🔍」ボタンを埋め込む形にする（検索欄のすぐ右に
        # ある方が「探す→押す」の動線が短く分かりやすいという要望による）。
        search_row = ctk.CTkFrame(panel, fg_color="transparent")
        search_row.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 12))
        search_row.grid_columnconfigure(0, weight=1)

        self._youtube_search_entry = ctk.CTkEntry(
            search_row,
            placeholder_text="曲名やアーティストで検索（空欄なら通常のトップページ）",
            font=self._font_small,
            height=28,
            fg_color=theme.BG_DEEP,
            border_color=theme.PANEL_EDGE,
        )
        self._youtube_search_entry.grid(row=0, column=0, sticky="ew")
        # Enter キーでも「🔍」ボタンと同じ動きにする（検索して即開ける）。
        self._youtube_search_entry.bind("<Return>", lambda _e: self._on_search())

        search_button = ctk.CTkButton(
            search_row,
            text="🔍",
            font=self._font_small,
            width=36,
            height=28,
            command=self._on_search,
        )
        search_button.grid(row=0, column=1, sticky="e", padx=(6, 0))
        theme.style_ghost_button(search_button, theme.MUTED)

    def _on_open_browser_menu(self) -> None:
        """「ブラウザを選択」ボタンの処理。開くブラウザを選ぶメニューを出す。"""
        menu = tk.Menu(self, tearoff=0)
        for label in _BROWSER_LABELS:
            menu.add_command(
                label=label, command=lambda label=label: self._set_browser(label)
            )
        x = self._browser_button.winfo_rootx()
        y = self._browser_button.winfo_rooty() + self._browser_button.winfo_height()
        menu.tk_popup(x, y)

    def _set_browser(self, label: str) -> None:
        """メニューでブラウザが選ばれたときの処理。"""
        self._selected_browser_label = label
        self._browser_button.configure(text=f"ブラウザを選択：{label} ⇩")

    def _resolve_youtube_url(self) -> str:
        """検索欄の入力を反映したYouTubeのURLを組み立てる。

        検索欄が空なら :data:`_YOUTUBE_URL`（トップページ）、文字が
        入っていれば :data:`_YOUTUBE_SEARCH_URL`（検索結果ページ）を返す。
        """
        query = self._youtube_search_entry.get().strip()
        if not query:
            return _YOUTUBE_URL
        return _YOUTUBE_SEARCH_URL.format(query=urllib.parse.quote(query))

    def _launch_browser(self, url: str, *, incognito: bool) -> None:
        """「ブラウザを選択」で選ばれているブラウザで ``url`` を開く。

        見つからなければ、既定のブラウザ（:mod:`webbrowser` 任せ）へ
        フォールバックする。その場合 ``incognito=True`` でも通常モードに
        なる（見つからないブラウザにシークレット起動の引数を渡す方法が
        無いため）。

        Args:
            url: 開くURL。
            incognito: True ならシークレット/プライベートモードで開く。
        """
        exe_name = _BROWSER_LABELS[self._selected_browser_label]
        exe_path = find_browser_path(exe_name)
        if exe_path is None:
            messagebox.showinfo(
                "ブラウザが見つかりませんでした",
                f"選択中の「{self._selected_browser_label}」が見つからな"
                "かったため、既定のブラウザで開きます。",
                parent=self,
            )
            webbrowser.open(url)
            return

        args = [exe_path]
        if incognito:
            flag = _INCOGNITO_FLAGS[exe_name]
            args.append(flag)
        args.append(url)
        try:
            subprocess.Popen(args)
        except OSError as exc:
            messagebox.showerror(
                "ブラウザを起動できません", str(exc), parent=self
            )

    def _on_search(self) -> None:
        """「🔍検索」ボタン（またはEnterキー）の処理。

        検索欄に文字が入っていれば、「ブラウザを選択」で選んだブラウザで
        その検索結果を開く。空欄ならトップページを開く。「👻シークレット」
        にチェックが入っていれば、シークレット/プライベートモードで開く。
        """
        self._launch_browser(
            self._resolve_youtube_url(), incognito=self._incognito_var.get()
        )

    def _on_open_help(self) -> None:
        """？ ボタンの処理。README・説明書・開発者情報をタブで見せる。

        「使い方が分からない」「問い合わせ先が分からない」という声を
        受けて追加した。多重起動は防ぐ（既に開いていれば前面へ）。
        """
        if self._help_window is not None:
            try:
                exists = self._help_window.winfo_exists()
            except tk.TclError:
                exists = False
            if exists:
                theme.bring_to_front(self._help_window)
                return
            self._help_window = None

        window = ctk.CTkToplevel(self)
        window.title("ヘルプ")
        window.geometry("560x600")
        window.minsize(420, 360)
        window.resizable(True, True)
        window.configure(fg_color=theme.BG_DEEP)
        self._help_window = window

        def on_destroy(event: Any) -> None:
            if event.widget is window:
                self._help_window = None

        window.bind("<Destroy>", on_destroy)

        # README・説明書.md は開発者向けの書き方（見出し記号や太字記号が
        # そのまま文字として見えてしまう）なので、初めて使う人向けには
        # 別に用意した読みやすいHTML版（:data:`_GUIDE_HTML_PATH`）を
        # ブラウザで開けるようにする。目次から各項目へ飛べ、ブラウザの
        # 検索（Ctrl+F）もそのまま使える。
        guide_button = ctk.CTkButton(
            window,
            text="📖 わかりやすい説明書を開く（ブラウザ）",
            font=self._font_label,
            height=38,
            command=self._on_open_guide_html,
        )
        guide_button.pack(fill="x", padx=16, pady=(16, 4))
        theme.style_solid_button(guide_button, theme.CYAN, theme.CYAN_HOVER)

        ctk.CTkLabel(
            window,
            text="README・説明書タブは、開発時のそのままの原稿（開発者向け）です",
            font=self._font_small,
            text_color=theme.MUTED,
        ).pack(anchor="w", padx=18, pady=(0, 8))

        tabview = ctk.CTkTabview(
            window,
            anchor="w",
            fg_color=theme.BG_DEEP,
            segmented_button_fg_color=theme.PANEL,
            segmented_button_selected_color=theme.over(theme.CYAN, theme.PANEL, 0.45),
            segmented_button_selected_hover_color=theme.over(
                theme.CYAN, theme.PANEL, 0.6
            ),
            segmented_button_unselected_color=theme.PANEL,
            segmented_button_unselected_hover_color=theme.over(
                theme.CYAN, theme.PANEL, 0.2
            ),
            segmented_button_font=self._font_label,
            text_color=theme.TEXT_DIM,
        )
        tabview.pack(fill="both", expand=True, padx=16, pady=(16, 8))

        for label, file_name in _DOCS.items():
            self._build_doc_tab(tabview.add(label), file_name)

        self._build_developer_tab(tabview.add("開発者情報"))

        ctk.CTkButton(
            window,
            text="閉じる",
            font=self._font_label,
            height=32,
            command=window.destroy,
        ).pack(anchor="e", padx=16, pady=(0, 16))

        theme.bring_to_front(window)

    def _on_open_guide_html(self) -> None:
        """「📖 わかりやすい説明書を開く」ボタンの処理。

        既定のブラウザで開く（「ブラウザを選択」の対象はYouTube検索専用
        なので、ここでは触らない＝常に :mod:`webbrowser` に任せる）。
        """
        path = _GUIDE_HTML_PATH
        if not path.exists():
            messagebox.showerror(
                "説明書が見つかりません",
                f"次の場所に説明書ファイルが見当たりませんでした:\n{path}",
                parent=self,
            )
            return
        webbrowser.open(path.as_uri())

    _SYSINFO_REFRESH_MS = 1000
    """メモリ・CPU使用率の自動更新間隔[ms]。開いている間だけ動かす。"""

    def _on_open_sysinfo(self) -> None:
        """「📊」ボタンの処理。PC全体のメモリ・CPU使用率を表示する。

        多重起動は防ぐ（既に開いていれば前面へ）。:class:`SystemMonitor`
        はDLL（``native/sysinfo.dll``）が無い・読み込めない環境でも
        アプリ本体は落とさず、ポップアップ内にエラー文言を出すだけに
        している。
        """
        if self._sysinfo_window is not None:
            try:
                exists = self._sysinfo_window.winfo_exists()
            except tk.TclError:
                exists = False
            if exists:
                theme.bring_to_front(self._sysinfo_window)
                return
            self._sysinfo_window = None

        window = ctk.CTkToplevel(self)
        window.title("メモリ・CPU使用率")
        window.geometry("320x420")
        window.resizable(False, False)
        window.configure(fg_color=theme.BG_DEEP)
        self._sysinfo_window = window

        def on_destroy(event: Any) -> None:
            if event.widget is window:
                if self._sysinfo_after_id is not None:
                    try:
                        self.after_cancel(self._sysinfo_after_id)
                    except tk.TclError:
                        pass
                    self._sysinfo_after_id = None
                self._sysinfo_window = None

        window.bind("<Destroy>", on_destroy)

        if self._system_monitor is None:
            try:
                self._system_monitor = SystemMonitor()
            except SysInfoUnavailableError as exc:
                ctk.CTkLabel(
                    window,
                    text=f"取得できませんでした\n({exc})",
                    font=self._font_small,
                    text_color=theme.MUTED,
                    justify="center",
                    wraplength=260,
                ).pack(expand=True, padx=16, pady=16)
                return

        header_row = ctk.CTkFrame(window, fg_color="transparent")
        header_row.pack(fill="x", padx=18, pady=(18, 0))
        header_row.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            header_row, text="メモリ", font=self._font_eyebrow, text_color=theme.MUTED, anchor="w"
        ).grid(row=0, column=0, sticky="w")
        memory_detail_label = ctk.CTkLabel(
            header_row, text="", font=self._font_small, text_color=theme.MUTED, anchor="e"
        )
        memory_detail_label.grid(row=0, column=1, sticky="e")

        memory_label = ctk.CTkLabel(
            window, text="―", font=self._font_key, text_color=theme.CYAN, anchor="w"
        )
        memory_label.pack(anchor="w", padx=18)

        # 直近60秒の推移（タスクマネージャーのメモリグラフの簡易版）。
        # 折れ線チャート用ライブラリを増やしたくないため、
        # tk.Canvas手描きの :class:`theme.HistoryGraph` を使う。
        memory_graph = theme.HistoryGraph(
            window, max_points=60, height=100, color=theme.CYAN, bg=theme.PANEL
        )
        memory_graph.pack(fill="x", padx=18, pady=(6, 8))

        # 使用中/利用可能の内訳バー（タスクマネージャーの「メモリ構成」に相当）。
        # 音量メーター用に既にある :class:`theme.SegmentMeter` を流用する
        # （0.0〜1.0を渡すと使用率に応じて緑→黄→赤に点灯するのが、
        # そのまま「メモリが逼迫してきた」の直感的な表示にもなるため）。
        memory_meter = theme.SegmentMeter(window, segments=32, height=14, bg=theme.PANEL)
        memory_meter.pack(fill="x", padx=18, pady=(0, 4))

        ctk.CTkLabel(
            window, text="CPU", font=self._font_eyebrow, text_color=theme.MUTED, anchor="w"
        ).pack(anchor="w", padx=18, pady=(14, 0))
        cpu_label = ctk.CTkLabel(
            window, text="―", font=self._font_key, text_color=theme.VIOLET, anchor="w"
        )
        cpu_label.pack(anchor="w", padx=18)
        cpu_graph = theme.HistoryGraph(
            window, max_points=60, height=80, color=theme.VIOLET, bg=theme.PANEL
        )
        cpu_graph.pack(fill="x", padx=18, pady=(6, 14))

        last_cpu_text = "計測中…"

        def refresh() -> None:
            nonlocal last_cpu_text
            try:
                exists = window.winfo_exists()
            except tk.TclError:
                exists = False
            if not exists or self._system_monitor is None:
                return
            used, total = self._system_monitor.memory()
            percent = 100.0 * used / total if total else 0.0
            memory_label.configure(text=f"{percent:.0f}%")
            memory_detail_label.configure(
                text=f"{used / 1024**3:.1f} / {total / 1024**3:.1f} GB"
            )
            memory_graph.push(percent / 100.0)
            memory_meter.set(percent / 100.0)

            cpu = self._system_monitor.cpu_percent()
            if cpu is not None:
                last_cpu_text = f"{cpu:.0f}%"
                cpu_graph.push(cpu / 100.0)
            cpu_label.configure(text=last_cpu_text)
            self._sysinfo_after_id = self.after(self._SYSINFO_REFRESH_MS, refresh)

        refresh()

    def _build_doc_tab(self, tab: Any, file_name: str) -> None:
        """README/説明書の内容をそのまま表示するタブを作る。

        Args:
            tab: 置き先（``CTkTabview.add()`` が返すフレーム）。
            file_name: :data:`config.BASE_DIR` 直下のファイル名。
        """
        text_box = ctk.CTkTextbox(
            tab,
            font=ctk.CTkFont(family=config.APP_FONT, size=12),
            wrap="word",
            fg_color=theme.PANEL,
            text_color=theme.TEXT,
        )
        text_box.pack(fill="both", expand=True)

        # README/説明書は読み取り専用の同梱リソースなので、書き込み先
        # （config.BASE_DIR）ではなく同梱リソースの場所（RESOURCES_DIR）
        # から読む。onefile 版の exe では別の場所を指す（config.py 参照）。
        path = config.RESOURCES_DIR / file_name
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            content = f"ファイルを読み込めませんでした: {exc}\n\n{path}"
        text_box.insert("1.0", content)
        text_box.configure(state="disabled")

    def _build_developer_tab(self, tab: Any) -> None:
        """開発者情報（アイコン・名前・GitHub・X）のタブを作る。

        「サイトオーナーに問い合わせるにはどこを見ればいいか分からない」
        という声を受けて追加した。GitHub・X はクリックで既定のブラウザを
        開く（外部サイトを開くだけで、アプリからは何も送信しない）。

        Args:
            tab: 置き先（``CTkTabview.add()`` が返すフレーム）。
        """
        content = ctk.CTkFrame(tab, fg_color="transparent")
        content.pack(expand=True)

        icon_path = config.DEVELOPER_ICON_PATH
        if icon_path.exists():
            try:
                photo = tk.PhotoImage(file=str(icon_path))
                self._developer_icon_photo = photo
                icon_label = tk.Label(
                    content, image=photo, bg=theme.BG_DEEP, borderwidth=0
                )
            except tk.TclError:
                icon_label = ctk.CTkLabel(
                    content, text="👤", font=self._font_key, text_color=theme.MUTED
                )
        else:
            icon_label = ctk.CTkLabel(
                content, text="👤", font=self._font_key, text_color=theme.MUTED
            )
        icon_label.pack(pady=(24, 12))

        ctk.CTkLabel(
            content,
            text=config.DEVELOPER_NAME,
            font=self._font_title,
            text_color=theme.AMBER,
        ).pack()

        def open_link(url: str) -> None:
            webbrowser.open(url)

        github_label = ctk.CTkLabel(
            content,
            text=f"GitHub: {config.DEVELOPER_GITHUB_URL}",
            font=self._font_small,
            text_color=theme.CYAN,
            cursor="hand2",
        )
        github_label.pack(pady=(16, 4))
        github_label.bind(
            "<Button-1>", lambda _e: open_link(config.DEVELOPER_GITHUB_URL)
        )

        ctk.CTkLabel(
            content,
            text="不具合の報告・お問い合わせはこちらまでどうぞ。",
            font=self._font_small,
            text_color=theme.MUTED,
        ).pack(pady=(16, 0))

    def _build_guide(self) -> None:
        """VB-CABLE の配線手順を並べる。

        既定出力への自動切り替え（:meth:`_auto_switch_to_cable`）が
        成功していれば、その手順自体をリストから省く（もう終わっている
        ことを案内しても混乱を招くだけのため）。失敗していた場合だけ
        :data:`_MANUAL_STEP_2` を手動手順として差し込む。
        """
        panel = ctk.CTkFrame(
            self._conn_scroll, fg_color=theme.PANEL, corner_radius=12, border_width=0
        )
        panel.grid(row=0, column=0, sticky="ew", padx=20, pady=(14, 0))
        panel.grid_columnconfigure(0, weight=1)

        # 見出し行。手順そのもの（self._guide_body）とは別の行に置き、
        # ここだけは折りたたんでも常に見えるようにする。接続状態の表示
        # （緑=デバイス接続中／赤=デバイスの接続に失敗）もここに置く。
        head = ctk.CTkFrame(panel, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=14, pady=(10, 6))
        head.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            head,
            text="接続設定・確認",
            font=self._font_eyebrow,
            text_color=theme.MUTED,
            anchor="w",
        ).grid(row=0, column=0, sticky="w")

        self._guide_status_label = ctk.CTkLabel(
            head,
            text="",
            font=self._font_small,
            text_color=theme.MUTED,
            anchor="e",
        )
        self._guide_status_label.grid(row=0, column=1, sticky="e", padx=(8, 8))

        self._guide_toggle_button = ctk.CTkButton(
            head,
            text="▲",
            font=self._font_small,
            width=28,
            height=22,
            fg_color="transparent",
            border_width=1,
            command=self._on_toggle_guide,
        )
        self._guide_toggle_button.grid(row=0, column=2, sticky="e")
        theme.style_ghost_button(self._guide_toggle_button, theme.MUTED)

        body = ctk.CTkFrame(panel, fg_color="transparent")
        body.grid(row=1, column=0, sticky="ew")
        body.grid_columnconfigure(1, weight=1)
        self._guide_body = body

        steps = list(_SETUP_STEPS)
        if not self._auto_switch_succeeded:
            steps.insert(1, _MANUAL_STEP_2)

        row_index = 0
        if self._auto_switch_succeeded:
            ctk.CTkLabel(
                body,
                text="✓ 既定の出力は自動でCABLE Inputへ切り替えました",
                font=self._font_small,
                text_color=theme.GREEN,
                anchor="w",
            ).grid(row=row_index, column=0, columnspan=2, sticky="w", padx=14, pady=(0, 6))
            row_index += 1

        ctk.CTkLabel(
            body,
            text="手順",
            font=self._font_eyebrow,
            text_color=theme.MUTED,
            anchor="w",
        ).grid(row=row_index, column=0, columnspan=2, sticky="w", padx=14, pady=(0, 4))
        row_index += 1

        for title, description in steps:
            ctk.CTkLabel(
                body,
                text=title,
                font=self._font_label,
                text_color=theme.TEXT_DIM,
                anchor="w",
            ).grid(row=row_index, column=0, sticky="nw", padx=(14, 10), pady=(0, 4))
            ctk.CTkLabel(
                body,
                text=description,
                font=self._font_small,
                text_color=theme.MUTED,
                anchor="w",
                justify="left",
                wraplength=340,
            ).grid(row=row_index, column=1, sticky="w", padx=(0, 14), pady=(0, 4))
            row_index += 1

        ctk.CTkLabel(
            body,
            text="",
            height=8,
        ).grid(row=row_index, column=0)
        row_index += 1

        # 入力・出力の選択も、別ウィンドウにせずこのパネルの中に置く
        # （画面遷移が無い方が分かりやすいという判断）。
        self._input_var = tk.StringVar(value=_NO_DEVICE)
        self._output_var = tk.StringVar(value=_NO_DEVICE)
        self._input_menu = self._add_device_row(
            body, row=row_index, text="入力", variable=self._input_var
        )
        row_index += 1
        self._output_menu = self._add_device_row(
            body, row=row_index, text="出力", variable=self._output_var
        )
        row_index += 1
        # 出力デバイスを選び直したら、Windows側の出力音量スライダーの
        # 表示もそのデバイスの値へ合わせ直す。
        self._output_var.trace_add(
            "write", lambda *_args: self._sync_windows_volume_slider()
        )

        self._refresh_button = ctk.CTkButton(
            body,
            text="一覧を更新",
            font=self._font_small,
            width=100,
            height=28,
            command=self.refresh_devices,
        )
        self._refresh_button.grid(
            row=row_index, column=1, sticky="e", padx=(0, 14), pady=(0, 12)
        )
        theme.style_ghost_button(self._refresh_button, theme.MUTED)

        # 前回すでに手順を読み終えている（一度でも接続に成功している）
        # 場合は、最初から畳んだ状態で表示する。まだ Toplevel が画面に
        # 出ていない段階では winfo_ismapped() が信用できない
        # （ヘッドレス環境で毎回確認済みの罠）ため、実際の表示状態は
        # :attr:`_guide_collapsed` というアプリ側の変数だけで管理する。
        self._apply_guide_collapsed()

    def _on_toggle_guide(self) -> None:
        """手順パネルの本体（self._guide_body）を隠す/出す。"""
        self._guide_collapsed = not self._guide_collapsed
        self._apply_guide_collapsed()

    def _apply_guide_collapsed(self) -> None:
        """:attr:`_guide_collapsed` の値に画面を合わせる。"""
        if self._guide_collapsed:
            self._guide_body.grid_remove()
            self._guide_toggle_button.configure(text="▼")
        else:
            self._guide_body.grid()
            self._guide_toggle_button.configure(text="▲")

    def _on_open_connection_settings(self) -> None:
        """見出しの「接続設定」ボタンの処理。

        「🔌 接続」タブへ切り替え、「接続設定・確認」パネルを開いた
        状態にする（既に開いていれば畳まない）。
        """
        self._tabview.set("🔌 接続")
        if self._guide_collapsed:
            self._guide_collapsed = False
            self._apply_guide_collapsed()

    def _build_advanced_settings_window(self) -> None:
        """⚙「詳細設定」画面（別ウィンドウ）を作る。

        今のところ中身は音声コールバックサイズ（音飛びの起きやすさに
        関わる）だけ。環境によって最適値が変わるため、利用者が選べる
        ようにしてある（:data:`audio.live_monitor.BLOCK_SIZE_OPTIONS`
        参照）。常時は表示せず、見出しの「⚙」ボタンから開く。閉じるのは
        破棄ではなく非表示にするだけ（接続設定と同じ理由）。
        """
        window = ctk.CTkToplevel(self)
        window.title("詳細設定")
        window.geometry("440x320")
        # サイズ固定は resizable(False, False) ではなく minsize/maxsize で行う。
        # Windows では resizable() を呼んだ Toplevel はその後 withdraw() →
        # deiconify() しても state が "withdrawn" のまま戻ってこなくなる
        # （実機・ヘッドレス双方で再現を確認済み）。
        window.minsize(440, 320)
        window.maxsize(440, 320)
        window.configure(fg_color=theme.BG_DEEP)
        window.protocol("WM_DELETE_WINDOW", window.withdraw)
        window.withdraw()
        self._advanced_settings_window = window

        panel = ctk.CTkFrame(window, fg_color="transparent")
        panel.pack(fill="both", expand=True, padx=20, pady=20)

        ctk.CTkLabel(
            panel,
            text="音声コールバックサイズ",
            font=self._font_eyebrow,
            text_color=theme.MUTED,
            anchor="w",
        ).pack(anchor="w")

        ctk.CTkLabel(
            panel,
            text=(
                "1回に処理する音の長さ。大きいほど音飛びしにくい代わりに、"
                "音が遅れて聞こえるようになります（PC・オーディオ"
                "デバイスによって最適値が変わります）。"
            ),
            font=self._font_small,
            text_color=theme.TEXT_DIM,
            anchor="w",
            justify="left",
            wraplength=390,
        ).pack(anchor="w", pady=(4, 12))

        self._block_size_var = tk.IntVar(value=self._settings.key_changer_block_size)
        labels = {
            1024: "1024（低遅延・約21ms／音飛びしやすい）",
            2048: "2048（推奨・約43ms）",
            4096: "4096（高遅延・約85ms／音飛びしにくい）",
        }
        for value in BLOCK_SIZE_OPTIONS:
            ctk.CTkRadioButton(
                panel,
                text=labels.get(value, str(value)),
                font=self._font_label,
                text_color=theme.TEXT,
                variable=self._block_size_var,
                value=value,
            ).pack(anchor="w", pady=4)

        self._advanced_settings_note = ctk.CTkLabel(
            panel,
            text="",
            font=self._font_small,
            text_color=theme.AMBER,
            anchor="w",
            justify="left",
            wraplength=390,
        )
        self._advanced_settings_note.pack(anchor="w", pady=(8, 0))

        button_row = ctk.CTkFrame(panel, fg_color="transparent")
        button_row.pack(fill="x", pady=(16, 0), side="bottom")

        reset_button = ctk.CTkButton(
            button_row,
            text="初期設定に戻す",
            font=self._font_small,
            width=110,
            height=28,
            command=self._on_reset_advanced_settings,
        )
        reset_button.pack(side="left")
        theme.style_ghost_button(reset_button, theme.MUTED)

        cancel_button = ctk.CTkButton(
            button_row,
            text="キャンセル",
            font=self._font_small,
            width=90,
            height=28,
            command=self._on_cancel_advanced_settings,
        )
        cancel_button.pack(side="right", padx=(6, 0))
        theme.style_ghost_button(cancel_button, theme.MUTED)

        apply_button = ctk.CTkButton(
            button_row,
            text="適用",
            font=self._font_small,
            width=90,
            height=28,
            command=self._on_apply_advanced_settings,
        )
        apply_button.pack(side="right")
        theme.style_solid_button(apply_button, theme.CYAN, theme.CYAN_HOVER)

    def _on_open_advanced_settings(self) -> None:
        """見出しの「⚙」ボタンの処理。詳細設定ウィンドウを表示する。

        開くたびに、今保存されている値へ選択肢を合わせ直す
        （前回キャンセルした未保存の選択が残らないようにするため）。
        """
        self._block_size_var.set(self._settings.key_changer_block_size)
        self._advanced_settings_note.configure(text="")
        self._advanced_settings_window.deiconify()
        theme.bring_to_front(self._advanced_settings_window)

    def _on_reset_advanced_settings(self) -> None:
        """「初期設定に戻す」ボタンの処理。

        その場で選択肢を既定値へ戻すだけで、保存も適用もしない
        （「適用」を押すまでは確定させないため）。
        """
        self._block_size_var.set(_DEFAULT_BLOCK_SIZE)
        self._advanced_settings_note.configure(text="")

    def _on_cancel_advanced_settings(self) -> None:
        """「キャンセル」ボタンの処理。何も変更せずに閉じる。"""
        self._advanced_settings_window.withdraw()

    def _on_apply_advanced_settings(self) -> None:
        """「適用」ボタンの処理。選んだコールバックサイズを保存・反映する。

        動作中は音声ストリームを開き直さないと反映できないため、
        安全のため**動作中は適用させない**（止めてからやり直してもらう）。
        """
        if self.monitor.is_running:
            self._advanced_settings_note.configure(
                text="⚠ 動作中は変更できません。「停止」してから適用してください。"
            )
            return

        value = self._block_size_var.get()
        self._settings.key_changer_block_size = value
        save_settings(self._settings)
        self.monitor.block_size = value
        self._advanced_settings_window.withdraw()

    def _add_device_row(
        self, parent: Any, row: int, text: str, variable: tk.StringVar
    ) -> ctk.CTkOptionMenu:
        """デバイス選択の 1 行を作る。

        Args:
            parent: 置き先。
            row: グリッドの行番号。
            text: 左側に出す見出し。
            variable: 選択中のラベルを保持する変数。

        Returns:
            出来上がったドロップダウン。
        """
        ctk.CTkLabel(
            parent,
            text=text,
            font=self._font_eyebrow,
            text_color=theme.MUTED,
            width=120,
            anchor="w",
        ).grid(row=row, column=0, sticky="w", padx=(14, 10), pady=(0, 8))

        # CTkOptionMenu 自体には枠線を付けられないため、枠線付きの
        # フレームで包んで境界を見せる（検索欄の入力枠と同じ考え方）。
        border = ctk.CTkFrame(
            parent,
            fg_color=theme.BG_DEEP,
            corner_radius=10,
            border_width=1,
            border_color=theme.PANEL_EDGE_HI,
        )
        border.grid(row=row, column=1, sticky="ew", padx=(0, 14), pady=(0, 8))
        border.grid_columnconfigure(0, weight=1)

        menu = ctk.CTkOptionMenu(
            border,
            variable=variable,
            values=[_NO_DEVICE],
            font=self._font_label,
            dynamic_resizing=False,
            corner_radius=8,
            height=32,
            fg_color=theme.BG_DEEP,
            button_color=theme.over(theme.CYAN, theme.PANEL, 0.35),
            button_hover_color=theme.over(theme.CYAN, theme.PANEL, 0.55),
            text_color=theme.TEXT_DIM,
            dropdown_fg_color=theme.PANEL_SOFT,
            dropdown_hover_color=theme.over(theme.CYAN, theme.PANEL_SOFT, 0.25),
            dropdown_text_color=theme.TEXT_DIM,
            dropdown_font=self._font_label,
        )
        menu.grid(row=0, column=0, sticky="ew", padx=1, pady=1)
        return menu

    def _build_key_panel(self) -> None:
        """キー変更の操作パネルを作る（「🎚 キー」タブの1枚目のカード）。"""
        panel = ctk.CTkFrame(
            self._key_scroll, fg_color=theme.PANEL, corner_radius=12, border_width=0
        )
        panel.grid(row=0, column=0, sticky="ew", padx=20, pady=(14, 0))
        panel.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            panel,
            text="キー",
            font=self._font_eyebrow,
            text_color=theme.MUTED,
            anchor="w",
        ).grid(row=0, column=0, columnspan=3, sticky="w", padx=14, pady=(10, 0))

        self._down_button = ctk.CTkButton(
            panel,
            text="▼",
            font=self._font_title,
            width=56,
            height=52,
            command=lambda: self._nudge_key(-1),
        )
        self._down_button.grid(row=1, column=0, padx=(14, 0), pady=(2, 6))
        theme.style_ghost_button(self._down_button, theme.CYAN)

        self._key_label = ctk.CTkLabel(
            panel,
            text="±0",
            font=self._font_key,
            text_color=theme.CYAN,
        )
        self._key_label.grid(row=1, column=1, pady=(2, 0))

        self._up_button = ctk.CTkButton(
            panel,
            text="▲",
            font=self._font_title,
            width=56,
            height=52,
            command=lambda: self._nudge_key(1),
        )
        self._up_button.grid(row=1, column=2, padx=(0, 14), pady=(2, 6))
        theme.style_ghost_button(self._up_button, theme.CYAN)

        self._key_hint = ctk.CTkLabel(
            panel,
            text=f"原曲キー（{KEY_SHIFT_MIN}〜+{KEY_SHIFT_MAX} 半音）",
            font=self._font_small,
            text_color=theme.MUTED,
        )
        self._key_hint.grid(row=2, column=0, columnspan=3, pady=(0, 4))

        self._reset_button = ctk.CTkButton(
            panel,
            text="原曲キー",
            font=self._font_small,
            width=110,
            height=26,
            command=lambda: self._set_key(0),
        )
        self._reset_button.grid(row=3, column=0, columnspan=3, pady=(0, 12))
        theme.style_ghost_button(self._reset_button, theme.MUTED)

        # ▲▼ ボタンを押しに行かなくても操作できるようにする
        self.bind("<Up>", lambda _event: self._nudge_key(1))
        self.bind("<Down>", lambda _event: self._nudge_key(-1))

        self._build_volume_panel()

    def _build_volume_panel(self) -> None:
        """🖥 出力音量 のカードを作る（「🎚 キー」タブの2枚目のカード）。

        CABLE Input 側の音量調整がOSの音量スライダーを受け付けない
        ことがある（VB-CABLEの実装依存）ため、Windows側の出力デバイス
        （実際に音が出るスピーカー等）そのものの音量を直接操作する
        （:mod:`audio.default_device` の
        :func:`get_output_volume`/:func:`set_output_volume` 参照）。
        """
        panel = ctk.CTkFrame(
            self._key_scroll, fg_color=theme.PANEL, corner_radius=12, border_width=0
        )
        panel.grid(row=1, column=0, sticky="ew", padx=20, pady=(10, 16))
        panel.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            panel,
            text="🖥 出力音量",
            font=self._font_eyebrow,
            text_color=theme.MUTED,
            anchor="w",
        ).grid(row=0, column=0, columnspan=3, sticky="w", padx=14, pady=(10, 6))

        self._windows_volume_value = 0.0
        """「🖥 出力音量」の実際の値（0.0〜1.0）。スライダーの
        ``get()`` は5%刻みに丸められてしまうため、－／＋ボタンでの
        1%刻みの増減はこちらを基準にする
        （:meth:`_on_windows_volume_changed` docstring参照）。"""

        self._windows_volume_slider = ctk.CTkSlider(
            panel,
            from_=0.0,
            to=1.0,
            number_of_steps=20,
            command=self._on_windows_volume_changed,
        )
        # **今の実際の音量を確認できるまでは、100%等の当てずっぽうの
        # 値を出さず、必ず無効状態で始める。** ここでうっかり100%を
        # 初期値にすると、確認が済む前に利用者がスライダーへ触れて
        # しまった場合に、実際の音量とは無関係な値（＝最悪フル音量）を
        # 書き込んでしまう恐れがある。実際の値は
        # :meth:`_sync_windows_volume_slider`（起動直後の
        # :meth:`refresh_devices` から必ず呼ばれる）が読み取れてから
        # 初めて有効化する。
        self._windows_volume_slider.configure(state="disabled")
        self._windows_volume_slider.grid(
            row=1, column=0, columnspan=2, sticky="ew", padx=(14, 8), pady=(0, 4)
        )
        # このスライダーは実際のスピーカー/ヘッドホンの音量そのものを
        # 動かす（audio.default_device.set_output_volume）。CTkSlider は
        # マウスホイールにも反応するため、キー タブをスクロールしようと
        # スライダーの上でホイールを回しただけで音量が変わってしまう
        # （気付かず大音量になる恐れがある）。他の操作と違って安全に
        # 関わるので、ホイール操作だけは無効化する（ドラッグ・－／＋
        # ボタンでの操作は今まで通りできる）。
        theme.disable_widget_mousewheel(self._windows_volume_slider)

        self._windows_volume_label = ctk.CTkLabel(
            panel,
            text="確認中…",
            font=self._font_small,
            text_color=theme.MUTED,
            width=56,
            anchor="e",
        )
        self._windows_volume_label.grid(row=1, column=2, sticky="e", padx=(0, 14), pady=(0, 4))

        # スライダーは5%刻み（20段階）にしてあるので、1%単位で微調整
        # したいときのために－／＋ボタンも並べておく。
        fine_row = ctk.CTkFrame(panel, fg_color="transparent")
        fine_row.grid(row=2, column=0, columnspan=3, sticky="e", padx=(0, 14), pady=(0, 4))

        self._windows_volume_minus_button = ctk.CTkButton(
            fine_row,
            text="－",
            font=self._font_small,
            width=28,
            height=24,
            command=lambda: self._on_windows_volume_step(-0.01),
        )
        self._windows_volume_minus_button.pack(side="left", padx=(0, 4))
        theme.style_ghost_button(self._windows_volume_minus_button, theme.CYAN)

        self._windows_volume_plus_button = ctk.CTkButton(
            fine_row,
            text="＋",
            font=self._font_small,
            width=28,
            height=24,
            command=lambda: self._on_windows_volume_step(0.01),
        )
        self._windows_volume_plus_button.pack(side="left")
        theme.style_ghost_button(self._windows_volume_plus_button, theme.CYAN)

        # スライダーと同じ理由（実際の値を確認できるまでは触らせない）で、
        # 最初は無効にしておく。:meth:`_set_windows_volume_controls_enabled`
        # がスライダーと一緒に有効/無効を切り替える。
        self._windows_volume_minus_button.configure(state="disabled")
        self._windows_volume_plus_button.configure(state="disabled")

        ctk.CTkLabel(
            panel,
            text="⚠ 音声はスピーカーやヘッドホンから再生されます。突然大きな音が出る場合があるため、使用前に音量を小さめに設定してください。",
            font=self._font_small,
            text_color=theme.AMBER,
            anchor="w",
            justify="left",
            wraplength=500,
        ).grid(row=3, column=0, columnspan=3, sticky="w", padx=14, pady=(0, 12))

        if not default_device.is_available():
            # Windows以外・comtypes未導入の環境では触れないので、
            # 触っても何も起きないふりをせず、操作自体を無効化する。
            self._windows_volume_label.configure(text="非対応")

    # ------------------------------------------------------------------
    #  音響（Live Space）
    # ------------------------------------------------------------------
    _LIVE_SPACE_SECTIONS: tuple[tuple[str, tuple[tuple[str, str, float, float, float, str], ...]], ...] = (
        ("ルーム", (
            ("room_size", "部屋の広さ", PERCENT_MIN, PERCENT_MAX, 5, "%"),
            ("depth", "奥行き", PERCENT_MIN, PERCENT_MAX, 5, "%"),
        )),
        ("反射", (
            ("early", "初期反射", PERCENT_MIN, PERCENT_MAX, 5, "%"),
            ("predelay_ms", "プリディレイ", PREDELAY_MIN_MS, PREDELAY_MAX_MS, 2, "ms"),
        )),
        ("残響", (
            ("reverb", "残響量", PERCENT_MIN, PERCENT_MAX, 5, "%"),
            ("decay_s", "減衰時間", DECAY_MIN_S, DECAY_MAX_S, 0.1, "s"),
            ("damping", "ダンピング", PERCENT_MIN, PERCENT_MAX, 5, "%"),
        )),
        ("エコー", (
            ("echo", "エコー量", PERCENT_MIN, PERCENT_MAX, 5, "%"),
            ("echo_time_ms", "エコー間隔", ECHO_TIME_MIN_MS, ECHO_TIME_MAX_MS, 10, "ms"),
            ("echo_feedback", "フィードバック", PERCENT_MIN, PERCENT_MAX, 5, "%"),
        )),
        ("ステレオ", (
            ("width", "広がり", WIDTH_MIN, WIDTH_MAX, 5, "%"),
            ("vocal_center", "ボーカル", WIDTH_MIN, WIDTH_MAX, 5, "%"),
            ("instrument_level", "楽器", WIDTH_MIN, WIDTH_MAX, 5, "%"),
        )),
    )
    """音響タブに並べる 5 カテゴリとスライダー
    ``(パラメータ名, 表示名, 最小, 最大, 刻み, 単位)``。
    切り替えず 2 列グリッドへ並べて常時表示する
    （:meth:`_build_live_space_panel` の ``_LIVE_SPACE_GRID_POSITIONS`` 参照）。
    以前あった「詳細 ▼」による開閉は廃止し、全項目を常に見える形にした。"""

    _LIVE_SPACE_GRID_POSITIONS: tuple[tuple[int, int], ...] = (
        (0, 0), (0, 1), (1, 0), (1, 1), (2, 0),
    )
    """:data:`_LIVE_SPACE_SECTIONS` の各カードを置く ``(行, 列)``。
    最後のカード（ステレオ）だけ最終行に列をまたいで置く
    （:meth:`_build_live_space_panel` 参照）。"""

    _AUDIENCE_LABELS: dict[str, str] = {"front": "前方", "middle": "中央", "back": "後方"}

    def _restore_live_space_settings(self) -> None:
        """保存された設定を :attr:`LiveMonitor.live_space` へ流し込む。

        プリセット名が有効ならその値、``Custom`` なら保存済みのパラメータ。
        壊れた設定ファイルに備えて範囲外の値は丸め、未知のプリセット名は
        既定へ戻す。
        """
        settings = self._settings
        space = self.monitor.live_space
        if settings.live_space_output_mode not in OUTPUT_MODES:
            settings.live_space_output_mode = OUTPUT_MODES[0]
        space.output_mode = settings.live_space_output_mode
        space.binaural = bool(settings.live_space_binaural)
        space.enabled = bool(settings.live_space_enabled)
        if settings.live_space_preset in PRESETS:
            space.set_params(PRESETS[settings.live_space_preset])
            return
        settings.live_space_preset = CUSTOM_PRESET
        space.set_params(params_from_dict(settings.live_space_params))

    @staticmethod
    def _format_live_space_value(value: float, unit: str) -> str:
        if unit == "s":
            return f"{value:.1f}s"
        if unit == "ms":
            return f"{value:.0f}ms"
        return f"{int(round(value))}%"

    def _build_live_space_panel(self) -> None:
        """音響（Live Space）の操作パネルを作る（「🔊 音響」タブの中身）。

        プリセット・客席・出力・バイノーラル・ON/OFF を上のカードにまとめ、
        ルーム／反射／残響／エコー／ステレオの5カテゴリは切り替えずに
        2列グリッドへ常時表示する（以前の「詳細 ▼」開閉は廃止した。
        タブ分けだけで既にウィンドウ内に収まるため、これ以上隠す必要が
        無くなったため）。値は :attr:`LiveMonitor.live_space` へ即時に
        渡す（動作中でも反映される）。
        """
        tab = self._acoustic_scroll

        head = ctk.CTkFrame(tab, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=20, pady=(14, 0))
        head.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            head, text="音響", font=self._font_eyebrow, text_color=theme.MUTED, anchor="w"
        ).grid(row=0, column=0, sticky="w")

        self._live_space_var = tk.BooleanVar(value=self._settings.live_space_enabled)
        self._live_space_switch = ctk.CTkSwitch(
            head,
            text="",
            font=self._font_small,
            variable=self._live_space_var,
            command=self._on_live_space_toggled,
            width=70,
            switch_width=38,
            switch_height=18,
            fg_color=theme.PANEL_EDGE_HI,
            progress_color=theme.VIOLET,
            button_color=theme.TEXT,
            button_hover_color=theme.VIOLET_HOVER,
            text_color=theme.TEXT_DIM,
        )
        self._live_space_switch.grid(row=0, column=1, sticky="e")

        controls = ctk.CTkFrame(
            tab, fg_color=theme.PANEL, corner_radius=12, border_width=0
        )
        controls.grid(row=1, column=0, sticky="ew", padx=20, pady=(8, 0))
        controls.grid_columnconfigure(1, weight=1)

        # プリセット
        ctk.CTkLabel(
            controls, text="プリセット", font=self._font_eyebrow, text_color=theme.MUTED, width=88, anchor="w"
        ).grid(row=0, column=0, sticky="w", padx=(14, 6), pady=(12, 4))
        self._preset_var = tk.StringVar(value=self._settings.live_space_preset)
        border = ctk.CTkFrame(
            controls, fg_color=theme.BG_DEEP, corner_radius=10, border_width=1, border_color=theme.PANEL_EDGE_HI
        )
        border.grid(row=0, column=1, columnspan=2, sticky="ew", padx=(0, 14), pady=(12, 4))
        border.grid_columnconfigure(0, weight=1)
        self._preset_menu = ctk.CTkOptionMenu(
            border,
            variable=self._preset_var,
            values=list(PRESETS),
            command=self._on_live_space_preset,
            font=self._font_label,
            dynamic_resizing=False,
            corner_radius=8,
            height=32,
            fg_color=theme.BG_DEEP,
            button_color=theme.over(theme.VIOLET, theme.PANEL, 0.35),
            button_hover_color=theme.over(theme.VIOLET, theme.PANEL, 0.55),
            text_color=theme.TEXT_DIM,
            dropdown_fg_color=theme.PANEL_SOFT,
            dropdown_hover_color=theme.over(theme.VIOLET, theme.PANEL_SOFT, 0.25),
            dropdown_text_color=theme.TEXT_DIM,
            dropdown_font=self._font_label,
        )
        self._preset_menu.grid(row=0, column=0, sticky="ew", padx=1, pady=1)

        # マイプリセット（読み込み）。組み込みの「プリセット」のすぐ下に
        # 置き、「プリセットは2種類ある（既定／自分の）」と分かるように
        # まとめる。登録（保存）はバイノーラルの下に別のボタンとして置く
        # （:meth:`_build_sound_profile_save_row` 参照）。
        ctk.CTkLabel(
            controls, text="マイプリセット", font=self._font_eyebrow, text_color=theme.MUTED, width=88, anchor="w"
        ).grid(row=1, column=0, sticky="w", padx=(14, 6), pady=(8, 4))
        self._sound_profile_var = tk.StringVar(value="")
        my_preset_border = ctk.CTkFrame(
            controls, fg_color=theme.BG_DEEP, corner_radius=10, border_width=1, border_color=theme.PANEL_EDGE_HI
        )
        my_preset_border.grid(row=1, column=1, sticky="ew", padx=(0, 8), pady=(8, 4))
        my_preset_border.grid_columnconfigure(0, weight=1)
        self._sound_profile_menu = ctk.CTkOptionMenu(
            my_preset_border,
            variable=self._sound_profile_var,
            values=["（未選択）"],
            command=self._on_apply_sound_profile,
            font=self._font_label,
            dynamic_resizing=False,
            corner_radius=8,
            height=32,
            fg_color=theme.BG_DEEP,
            button_color=theme.over(theme.VIOLET, theme.PANEL, 0.35),
            button_hover_color=theme.over(theme.VIOLET, theme.PANEL, 0.55),
            text_color=theme.TEXT_DIM,
            dropdown_fg_color=theme.PANEL_SOFT,
            dropdown_hover_color=theme.over(theme.VIOLET, theme.PANEL_SOFT, 0.25),
            dropdown_text_color=theme.TEXT_DIM,
            dropdown_font=self._font_label,
        )
        self._sound_profile_menu.grid(row=0, column=0, sticky="ew", padx=1, pady=1)
        self._sound_profile_delete_button = ctk.CTkButton(
            controls, text="🗑", font=self._font_small, width=32, height=32,
            command=self._on_delete_sound_profile,
        )
        self._sound_profile_delete_button.grid(row=1, column=2, sticky="e", padx=(0, 14), pady=(8, 4))
        theme.style_ghost_button(self._sound_profile_delete_button, theme.MUTED)
        self._refresh_sound_profile_menu()

        params = self.monitor.live_space.params

        # 客席 / 出力
        for row_index, (text, values, var_name, handler, initial) in enumerate(
            (
                (
                    "客席",
                    list(self._AUDIENCE_LABELS.values()),
                    "_audience_var",
                    self._on_live_space_audience,
                    self._AUDIENCE_LABELS.get(params.audience, "中央"),
                ),
                (
                    "出力",
                    ["ヘッドホン", "スピーカー"],
                    "_output_mode_var",
                    self._on_live_space_output_mode,
                    "ヘッドホン" if self._settings.live_space_output_mode == "headphone" else "スピーカー",
                ),
            ),
            start=2,
        ):
            ctk.CTkLabel(
                controls, text=text, font=self._font_eyebrow, text_color=theme.MUTED, width=88, anchor="w"
            ).grid(row=row_index, column=0, sticky="w", padx=(14, 6), pady=(8, 0))
            var = tk.StringVar(value=initial)
            setattr(self, var_name, var)
            ctk.CTkSegmentedButton(
                controls,
                values=values,
                variable=var,
                command=handler,
                font=self._font_small,
                height=26,
                corner_radius=8,
                fg_color=theme.BG_DEEP,
                selected_color=theme.over(theme.VIOLET, theme.PANEL, 0.45),
                selected_hover_color=theme.over(theme.VIOLET, theme.PANEL, 0.6),
                unselected_color=theme.BG_DEEP,
                unselected_hover_color=theme.over(theme.VIOLET, theme.PANEL, 0.2),
                text_color=theme.TEXT_DIM,
            ).grid(row=row_index, column=1, columnspan=2, sticky="w", padx=(0, 14), pady=(8, 0))

        # バイノーラル（ヘッドホンのときだけ有効）
        ctk.CTkLabel(
            controls, text="バイノーラル", font=self._font_eyebrow, text_color=theme.MUTED, width=88, anchor="nw"
        ).grid(row=4, column=0, sticky="nw", padx=(14, 6), pady=(8, 12))
        self._binaural_var = tk.BooleanVar(value=self._settings.live_space_binaural)
        self._binaural_check = ctk.CTkCheckBox(
            controls,
            text="HRTF で反射・残響を頭の外に定位（ヘッドホン専用）",
            font=self._font_small,
            variable=self._binaural_var,
            command=self._on_live_space_binaural,
            checkbox_width=18,
            checkbox_height=18,
            corner_radius=4,
            border_width=1.5,
            fg_color=theme.VIOLET,
            hover_color=theme.VIOLET_HOVER,
            border_color=theme.over(theme.VIOLET, theme.PANEL, 0.55),
            checkmark_color=theme.BG_DEEP,
            text_color=theme.TEXT_DIM,
        )
        self._binaural_check.grid(row=4, column=1, columnspan=2, sticky="w", padx=(0, 14), pady=(8, 12))
        self._apply_binaural_availability()

        # マイプリセット登録（保存）。バイノーラルのすぐ下に置く
        # （EQ・音響の設定一式が固まった、このカードの最後の行として）。
        save_profile_button = ctk.CTkButton(
            controls, text="💾 マイプリセットに保存", font=self._font_small, height=32,
            command=self._on_open_register_dialog,
        )
        save_profile_button.grid(
            row=5, column=0, columnspan=3, sticky="ew", padx=14, pady=(0, 14)
        )
        theme.style_ghost_button(save_profile_button, theme.CYAN)

        # ルーム／反射／残響／エコー／ステレオ を2列グリッドへ常時表示
        self._live_space_sliders: dict[str, ctk.CTkSlider] = {}
        self._live_space_value_labels: dict[str, ctk.CTkLabel] = {}
        self._live_space_units: dict[str, str] = {}
        value_font = ctk.CTkFont(family=theme.FONT_NUM, size=12, weight="bold")

        grid = ctk.CTkFrame(tab, fg_color="transparent")
        grid.grid(row=2, column=0, sticky="ew", padx=20, pady=(8, 16))
        grid.grid_columnconfigure(0, weight=1)
        grid.grid_columnconfigure(1, weight=1)

        last_position = self._LIVE_SPACE_GRID_POSITIONS[-1]
        for (grid_row, grid_col), (section, sliders) in zip(
            self._LIVE_SPACE_GRID_POSITIONS, self._LIVE_SPACE_SECTIONS
        ):
            span = 2 if (grid_row, grid_col) == last_position else 1
            padx = 0 if span == 2 else ((0, 4) if grid_col == 0 else (4, 0))
            card = ctk.CTkFrame(
                grid, fg_color=theme.PANEL, corner_radius=11, border_width=0
            )
            card.grid(
                row=grid_row, column=grid_col, columnspan=span,
                sticky="nsew", padx=padx, pady=(0, 8),
            )
            card.grid_columnconfigure(1, weight=1)

            ctk.CTkLabel(
                card,
                text=section,
                font=self._font_mini,
                text_color=theme.over(theme.VIOLET, theme.PANEL, 0.7),
                anchor="w",
            ).grid(row=0, column=0, columnspan=3, sticky="w", padx=12, pady=(9, 4))

            for slider_row, (name, label, low, high, step, unit) in enumerate(sliders, start=1):
                ctk.CTkLabel(
                    card, text=label, font=self._font_mini, text_color=theme.MUTED, width=92, anchor="w"
                ).grid(row=slider_row, column=0, sticky="w", padx=(12, 6), pady=(0, 9))
                slider = ctk.CTkSlider(
                    card,
                    from_=low,
                    to=high,
                    number_of_steps=int(round((high - low) / step)),
                    command=lambda value, name=name: self._on_live_space_slider(name, value),
                    fg_color=theme.PANEL_EDGE_HI,
                    progress_color=theme.VIOLET,
                    button_color=theme.VIOLET,
                    button_hover_color=theme.VIOLET_HOVER,
                    height=14,
                )
                slider.set(getattr(params, name))
                slider.grid(row=slider_row, column=1, sticky="ew", padx=(0, 8), pady=(0, 9))
                theme.disable_widget_mousewheel(slider)
                value_label = ctk.CTkLabel(
                    card,
                    text=self._format_live_space_value(getattr(params, name), unit),
                    font=value_font,
                    text_color=theme.VIOLET,
                    width=46,
                    anchor="e",
                )
                value_label.grid(row=slider_row, column=2, sticky="e", padx=(0, 12), pady=(0, 9))
                self._live_space_sliders[name] = slider
                self._live_space_value_labels[name] = value_label
                self._live_space_units[name] = unit

        self._apply_live_space_enabled_look()

    def _apply_live_space_enabled_look(self) -> None:
        """ON/OFF に合わせてスイッチの文字とスライダーの色を切り替える。"""
        enabled = self._live_space_var.get()
        self._live_space_switch.configure(text="ON" if enabled else "OFF")
        color = theme.VIOLET if enabled else theme.MUTED
        for slider in self._live_space_sliders.values():
            slider.configure(progress_color=color, button_color=color)
        for label in self._live_space_value_labels.values():
            label.configure(text_color=color)

    def _sync_live_space_controls(self) -> None:
        """スライダー・数値・Audience を、今のパラメータへ合わせる（プリセット選択後など）。"""
        params = self.monitor.live_space.params
        for name, slider in self._live_space_sliders.items():
            value = getattr(params, name)
            slider.set(value)
            self._live_space_value_labels[name].configure(
                text=self._format_live_space_value(value, self._live_space_units[name])
            )
        self._audience_var.set(self._AUDIENCE_LABELS.get(params.audience, "中央"))

    def _save_live_space_settings(self) -> None:
        """今の Live Space の状態を設定へ写し、少し待ってから保存する。

        設定オブジェクトへはすぐ写す（他の処理から最新値が見えるように）。
        ファイルへの書き込みだけを :data:`_SAVE_DEBOUNCE_MS` 遅らせ、その間に
        また変更があれば待ち直す。閉じるときは :meth:`_flush_pending_save`
        で待たずに書き出す。
        """
        space = self.monitor.live_space
        settings = self._settings
        settings.live_space_enabled = bool(space.enabled)
        settings.live_space_preset = preset_name_for(space.params)
        settings.live_space_output_mode = space.output_mode
        settings.live_space_binaural = bool(space.binaural)
        settings.live_space_params = params_to_dict(space.params)
        if self._save_after_id is not None:
            self.after_cancel(self._save_after_id)
        self._save_after_id = self.after(_SAVE_DEBOUNCE_MS, self._flush_pending_save)

    def _flush_pending_save(self) -> None:
        """保存待ちがあれば、今すぐ設定ファイルへ書き出す。"""
        if self._save_after_id is None:
            return
        try:
            self.after_cancel(self._save_after_id)
        except tk.TclError:
            pass
        self._save_after_id = None
        save_settings(self._settings)

    def _on_live_space_toggled(self) -> None:
        self.monitor.live_space.enabled = bool(self._live_space_var.get())
        self._apply_live_space_enabled_look()
        self._save_live_space_settings()

    def _on_live_space_preset(self, name: str) -> None:
        """プリセット選択の処理。全パラメータを差し替え、スライダーを合わせる。"""
        preset = PRESETS.get(name)
        if preset is None:
            return
        self.monitor.live_space.set_params(preset)
        self._preset_var.set(name)
        self._sync_live_space_controls()
        self._save_live_space_settings()

    def _on_live_space_slider(self, name: str, value: float) -> None:
        """スライダーの処理。1 項目だけ変え、プリセット表示を Custom にする。"""
        space = self.monitor.live_space
        unit = self._live_space_units[name]
        typed: float | int = round(float(value), 1) if unit == "s" else (
            float(round(value)) if unit == "ms" else int(round(value))
        )
        if getattr(space.params, name) == typed:
            return
        params = space.update(**{name: typed})
        self._live_space_value_labels[name].configure(
            text=self._format_live_space_value(typed, unit)
        )
        self._preset_var.set(preset_name_for(params))
        self._save_live_space_settings()

    def _on_live_space_audience(self, label: str) -> None:
        """前方 / 中央 / 後方 の切り替え。"""
        for key, text in self._AUDIENCE_LABELS.items():
            if text == label:
                params = self.monitor.live_space.update(audience=key)
                self._preset_var.set(preset_name_for(params))
                self._save_live_space_settings()
                return

    def _on_live_space_output_mode(self, label: str) -> None:
        """ヘッドホン / スピーカー の切り替え。"""
        self.monitor.live_space.output_mode = "speaker" if label == "スピーカー" else "headphone"
        self._apply_binaural_availability()
        self._save_live_space_settings()

    def _apply_binaural_availability(self) -> None:
        """バイノーラルはヘッドホンのときだけ操作できる（スピーカーでは無効表示）。"""
        headphone = self.monitor.live_space.output_mode == "headphone"
        self._binaural_check.configure(state="normal" if headphone else "disabled")

    def _on_live_space_binaural(self) -> None:
        self.monitor.live_space.binaural = bool(self._binaural_var.get())
        self._save_live_space_settings()

    # ------------------------------------------------------------------
    #  EQ（10バンド・グラフィックイコライザー）
    # ------------------------------------------------------------------
    _EQ_BAND_LABELS: tuple[str, ...] = (
        "31Hz", "62Hz", "125Hz", "250Hz", "500Hz",
        "1kHz", "2kHz", "4kHz", "8kHz", "16kHz",
    )
    """:data:`audio.equalizer.BAND_FREQS` に対応する表示ラベル。"""

    def _restore_eq_settings(self) -> None:
        """保存された設定を :attr:`LiveMonitor.equalizer` へ流し込む。

        プリセット名が有効ならその値、``Custom`` なら保存済みのゲイン列。
        壊れた設定ファイルに備え、個数や型が合わない値は
        :func:`audio.app_settings._as_float_list` 側で既に丸められている。
        """
        settings = self._settings
        eq = self.monitor.equalizer
        # C実装（native/biquad_eq.c）は係数がフィルタの内部状態
        # （EqState）側にあるため、実際の再生開始（LiveMonitor.start）
        # より前に周波数特性グラフを見せたい・スライダーを動かせるように
        # したい場合は、ここで一旦「仮の」サンプルレートで準備しておく
        # 必要がある。実際にストリームを開くと、そこで本当のサンプル
        # レートへ作り直される（:meth:`audio.live_monitor.LiveMonitor.start`
        # 参照）ので、ここでの値は表示用の近似でしかない。
        eq.prepare(48000, 2)
        eq.enabled = bool(settings.eq_enabled)
        if settings.eq_preset in EQ_PRESETS:
            eq.set_gains_db(EQ_PRESETS[settings.eq_preset])
            return
        settings.eq_preset = EQ_CUSTOM_PRESET
        eq.set_gains_db(tuple(settings.eq_gains))

    @staticmethod
    def _format_eq_value(db: float) -> str:
        return f"{db:+.1f} dB"

    def _build_eq_panel(self) -> None:
        """EQ の操作パネルを作る（「🎛 EQ」タブの中身）。

        プリセット・RESET・ON/OFF を上のカードにまとめ、10バンド分の
        スライダーは :attr:`_LIVE_SPACE_SECTIONS` と同じ「カード内に
        ラベル・スライダー・数値を並べる」形式で、低域5バンド・高域5
        バンドの2カードに分けて常時表示する。
        """
        tab = self._eq_scroll

        head = ctk.CTkFrame(tab, fg_color="transparent")
        # row=0 は「EQを利用できません」の案内用に空けてある
        # （:meth:`_apply_eq_availability` 参照。普段は何も置かれない）
        head.grid(row=1, column=0, sticky="ew", padx=20, pady=(14, 0))
        head.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            head, text="EQUALIZER", font=self._font_eyebrow, text_color=theme.MUTED, anchor="w"
        ).grid(row=0, column=0, sticky="w")

        self._eq_var = tk.BooleanVar(value=self._settings.eq_enabled)
        self._eq_switch = ctk.CTkSwitch(
            head,
            text="",
            font=self._font_small,
            variable=self._eq_var,
            command=self._on_eq_toggled,
            width=70,
            switch_width=38,
            switch_height=18,
            fg_color=theme.PANEL_EDGE_HI,
            progress_color=theme.CYAN,
            button_color=theme.TEXT,
            button_hover_color=theme.CYAN_HOVER,
            text_color=theme.TEXT_DIM,
        )
        self._eq_switch.grid(row=0, column=1, sticky="e")

        controls = ctk.CTkFrame(
            tab, fg_color=theme.PANEL, corner_radius=12, border_width=0
        )
        controls.grid(row=2, column=0, sticky="ew", padx=20, pady=(8, 0))
        controls.grid_columnconfigure(1, weight=1)

        # プリセット
        ctk.CTkLabel(
            controls, text="プリセット", font=self._font_eyebrow, text_color=theme.MUTED, width=88, anchor="w"
        ).grid(row=0, column=0, sticky="w", padx=(14, 6), pady=(12, 12))
        self._eq_preset_var = tk.StringVar(value=self._settings.eq_preset)
        border = ctk.CTkFrame(
            controls, fg_color=theme.BG_DEEP, corner_radius=10, border_width=1, border_color=theme.PANEL_EDGE_HI
        )
        border.grid(row=0, column=1, sticky="ew", padx=(0, 8), pady=(12, 12))
        border.grid_columnconfigure(0, weight=1)
        self._eq_preset_menu = ctk.CTkOptionMenu(
            border,
            variable=self._eq_preset_var,
            values=list(EQ_PRESETS),
            command=self._on_eq_preset,
            font=self._font_label,
            dynamic_resizing=False,
            corner_radius=8,
            height=32,
            fg_color=theme.BG_DEEP,
            button_color=theme.over(theme.CYAN, theme.PANEL, 0.35),
            button_hover_color=theme.over(theme.CYAN, theme.PANEL, 0.55),
            text_color=theme.TEXT_DIM,
            dropdown_fg_color=theme.PANEL_SOFT,
            dropdown_hover_color=theme.over(theme.CYAN, theme.PANEL_SOFT, 0.25),
            dropdown_text_color=theme.TEXT_DIM,
            dropdown_font=self._font_label,
        )
        self._eq_preset_menu.grid(row=0, column=0, sticky="ew", padx=1, pady=1)

        self._eq_reset_button = ctk.CTkButton(
            controls,
            text="RESET",
            font=self._font_small,
            width=64,
            height=32,
            command=self._on_eq_reset,
        )
        self._eq_reset_button.grid(row=0, column=2, sticky="e", padx=(0, 14), pady=(12, 12))
        theme.style_ghost_button(self._eq_reset_button, theme.MUTED)

        # 今のEQ設定がどんなカーブになっているかを見える化する
        # 周波数特性グラフ（31Hz〜16kHzを対数軸で表示）。
        self._eq_curve = theme.FrequencyCurve(
            tab, height=90, color=theme.CYAN, bg=theme.PANEL, db_range=15.0
        )
        self._eq_curve.grid(row=3, column=0, sticky="ew", padx=20, pady=(8, 0))

        # 10バンド分のスライダー（低域5・高域5の2カード）
        self._eq_sliders: dict[int, ctk.CTkSlider] = {}
        self._eq_value_labels: dict[int, ctk.CTkLabel] = {}
        value_font = ctk.CTkFont(family=theme.FONT_NUM, size=12, weight="bold")
        gains = self.monitor.equalizer.gains_db

        grid = ctk.CTkFrame(tab, fg_color="transparent")
        grid.grid(row=4, column=0, sticky="ew", padx=20, pady=(8, 16))
        grid.grid_columnconfigure(0, weight=1)
        grid.grid_columnconfigure(1, weight=1)

        half = len(EQ_BAND_FREQS) // 2
        for col, indices in enumerate((range(0, half), range(half, len(EQ_BAND_FREQS)))):
            card = ctk.CTkFrame(
                grid, fg_color=theme.PANEL, corner_radius=11, border_width=0
            )
            card.grid(
                row=0, column=col, sticky="nsew",
                padx=(0, 4) if col == 0 else (4, 0), pady=(0, 8),
            )
            card.grid_columnconfigure(1, weight=1)
            for slider_row, band_index in enumerate(indices):
                ctk.CTkLabel(
                    card,
                    text=self._EQ_BAND_LABELS[band_index],
                    font=self._font_mini,
                    text_color=theme.MUTED,
                    width=48,
                    anchor="w",
                ).grid(row=slider_row, column=0, sticky="w", padx=(12, 6), pady=(9 if slider_row == 0 else 0, 9))
                slider = ctk.CTkSlider(
                    card,
                    from_=GAIN_MIN_DB,
                    to=GAIN_MAX_DB,
                    number_of_steps=int(round((GAIN_MAX_DB - GAIN_MIN_DB) / 0.5)),
                    command=lambda value, i=band_index: self._on_eq_slider(i, value),
                    fg_color=theme.PANEL_EDGE_HI,
                    progress_color=theme.CYAN,
                    button_color=theme.CYAN,
                    button_hover_color=theme.CYAN_HOVER,
                    height=14,
                )
                slider.set(gains[band_index])
                slider.grid(
                    row=slider_row, column=1, sticky="ew", padx=(0, 8),
                    pady=(9 if slider_row == 0 else 0, 9),
                )
                theme.disable_widget_mousewheel(slider)
                value_label = ctk.CTkLabel(
                    card,
                    text=self._format_eq_value(gains[band_index]),
                    font=value_font,
                    text_color=theme.CYAN,
                    width=54,
                    anchor="e",
                )
                value_label.grid(
                    row=slider_row, column=2, sticky="e", padx=(0, 12),
                    pady=(9 if slider_row == 0 else 0, 9),
                )
                self._eq_sliders[band_index] = slider
                self._eq_value_labels[band_index] = value_label

        self._apply_eq_enabled_look()
        self._update_eq_curve()
        self._apply_eq_availability()

    def _apply_eq_availability(self) -> None:
        """EQ用DLLを読み込めなかった場合に、その旨を表示して操作を止める。

        無言で「動かすと表示は変わるのに音が変わらない」状態になるのが
        一番わかりにくいので、理由を出したうえで操作できないようにする
        （:attr:`audio.equalizer.Equalizer.available` 参照）。
        """
        if self.monitor.equalizer.available:
            return
        self._eq_var.set(False)
        self.monitor.equalizer.enabled = False
        self._eq_switch.configure(state="disabled")
        self._eq_preset_menu.configure(state="disabled")
        self._eq_reset_button.configure(state="disabled")
        for slider in self._eq_sliders.values():
            slider.configure(state="disabled")
        ctk.CTkLabel(
            self._eq_scroll,
            text=(
                "EQを利用できません。ウイルス対策ソフトに biquad_eq.dll が\n"
                "隔離された可能性があります（キー変更と音響は通常どおり使えます）。"
            ),
            font=self._font_small,
            text_color=theme.AMBER,
            justify="left",
            anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=20, pady=(14, 0))

    _EQ_CURVE_POINTS = 90
    """周波数特性グラフの描画点数（31Hz〜16kHzを対数間隔で区切る数）。"""

    def _update_eq_curve(self) -> None:
        """周波数特性グラフを、今のゲインに合わせて描き直す。"""
        eq = self.monitor.equalizer
        log_lo = math.log10(EQ_BAND_FREQS[0])
        log_hi = math.log10(EQ_BAND_FREQS[-1])
        freqs = [
            10 ** (log_lo + (log_hi - log_lo) * i / (self._EQ_CURVE_POINTS - 1))
            for i in range(self._EQ_CURVE_POINTS)
        ]
        values = [eq.response_db(f) for f in freqs]
        self._eq_curve.set_curve(freqs, values)

    def _apply_eq_enabled_look(self) -> None:
        """ON/OFF に合わせてスイッチの文字とスライダーの色を切り替える。"""
        enabled = self._eq_var.get()
        self._eq_switch.configure(text="ON" if enabled else "OFF")
        color = theme.CYAN if enabled else theme.MUTED
        self._eq_curve.set_color(color)
        for slider in self._eq_sliders.values():
            slider.configure(progress_color=color, button_color=color)
        for label in self._eq_value_labels.values():
            label.configure(text_color=color)

    def _sync_eq_controls(self) -> None:
        """スライダー・数値・周波数特性グラフを、今のゲインへ合わせる（プリセット選択・RESET後など）。"""
        gains = self.monitor.equalizer.gains_db
        for index, slider in self._eq_sliders.items():
            slider.set(gains[index])
            self._eq_value_labels[index].configure(text=self._format_eq_value(gains[index]))
        self._update_eq_curve()

    def _save_eq_settings(self) -> None:
        """今の EQ の状態を設定へ写し、少し待ってから保存する。

        :meth:`_save_live_space_settings` と同じ debounce タイマーを
        共有する（どちらも最終的に書き出すのは同じ設定ファイルのため）。
        """
        eq = self.monitor.equalizer
        settings = self._settings
        settings.eq_enabled = bool(eq.enabled)
        gains = eq.gains_db
        settings.eq_preset = eq_preset_name_for(gains)
        settings.eq_gains = list(gains)
        if self._save_after_id is not None:
            self.after_cancel(self._save_after_id)
        self._save_after_id = self.after(_SAVE_DEBOUNCE_MS, self._flush_pending_save)

    def _on_eq_toggled(self) -> None:
        self.monitor.equalizer.enabled = bool(self._eq_var.get())
        self._apply_eq_enabled_look()
        self._save_eq_settings()

    def _on_eq_preset(self, name: str) -> None:
        """プリセット選択の処理。全バンドを差し替え、スライダーを合わせる。"""
        preset = EQ_PRESETS.get(name)
        if preset is None:
            return
        self.monitor.equalizer.set_gains_db(preset)
        self._eq_preset_var.set(name)
        self._sync_eq_controls()
        self._save_eq_settings()

    def _on_eq_reset(self) -> None:
        """RESET。全バンドを0dBへ戻す。"""
        self.monitor.equalizer.reset_to_flat()
        self._eq_preset_var.set(eq_preset_name_for(self.monitor.equalizer.gains_db))
        self._sync_eq_controls()
        self._save_eq_settings()

    def _on_eq_slider(self, index: int, value: float) -> None:
        """スライダーの処理。1バンドだけ変え、プリセット表示を Custom にする。"""
        eq = self.monitor.equalizer
        typed = round(float(value) * 2) / 2  # 0.5dB刻みに丸める
        if eq.gains_db[index] == typed:
            return
        eq.set_band_db(index, typed)
        self._eq_value_labels[index].configure(text=self._format_eq_value(typed))
        self._eq_preset_var.set(eq_preset_name_for(eq.gains_db))
        self._update_eq_curve()
        self._save_eq_settings()

    def _build_footer(self) -> None:
        """開始／停止ボタンと状態表示を作る。

        タブの外（常に見える位置）に置く。「🖥 出力音量」は
        :meth:`_build_volume_panel`（「🎚 キー」タブ）へ移した。
        """
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.grid(row=2, column=0, sticky="ew", padx=20, pady=(10, 16))
        row.grid_columnconfigure(0, weight=1)

        self._toggle_button = ctk.CTkButton(
            row,
            text="▶ 開始",
            font=self._font_title,
            height=44,
            command=self._on_toggle,
        )
        self._toggle_button.grid(row=0, column=0, sticky="ew")
        theme.style_solid_button(self._toggle_button, theme.CYAN, theme.CYAN_HOVER)

        self._state_label = ctk.CTkLabel(
            row,
            text="停止中",
            font=self._font_small,
            text_color=theme.MUTED,
            anchor="w",
            justify="left",
            wraplength=560,
        )
        self._state_label.grid(row=1, column=0, sticky="w", pady=(8, 0))

    def _on_windows_volume_changed(self, value: float) -> None:
        """「🖥 出力音量」スライダーの処理。

        今選ばれている出力デバイス（実際に音が出る側）の、Windows側の
        エンドポイント音量を直接書き換える。

        **正確な値は :attr:`_windows_volume_value` に必ず記録しておく。**
        ``CTkSlider.set()`` はスライダー自体の刻み幅（5%）に丸め直して
        しまうため、``self._windows_volume_slider.get()`` を後から読むと
        1%刻みで動かしたつもりの値が5%刻みへ丸め戻ってしまい、
        （例: 11%→取得すると10%扱いになり、＋ボタンを押しても
        11%から先へ進めなくなる）。丸められないこの変数の方を
        「今の本当の値」として扱う。
        """
        self._windows_volume_value = value
        self._windows_volume_label.configure(text=f"{value * 100:.0f}%")
        name = self._get_selected_output_device_name()
        if name:
            default_device.set_output_volume(name, value)

    def _on_windows_volume_step(self, delta: float) -> None:
        """－／＋ボタンの処理。スライダーの5%刻みでは細かすぎるとき用に、
        今の値（:attr:`_windows_volume_value`）へ ``delta``（1%=0.01）を
        足し引きする。スライダーから ``get()`` した値は使わない
        （:meth:`_on_windows_volume_changed` のdocstring参照）。

        Args:
            delta: 増減量（-0.01 または 0.01）。
        """
        new_value = max(0.0, min(1.0, self._windows_volume_value + delta))
        self._windows_volume_slider.set(new_value)  # 見た目だけの近似表示
        self._on_windows_volume_changed(new_value)

    def _set_windows_volume_controls_enabled(self, enabled: bool) -> None:
        """スライダーと－／＋ボタンの有効/無効をまとめて切り替える。

        Args:
            enabled: True なら操作可能にする。
        """
        state = "normal" if enabled else "disabled"
        self._windows_volume_slider.configure(state=state)
        self._windows_volume_minus_button.configure(state=state)
        self._windows_volume_plus_button.configure(state=state)

    def _get_selected_output_device_name(self) -> str | None:
        """今選ばれている出力デバイスの、``sounddevice`` 表記の名前を返す。

        Returns:
            デバイス名。一覧未取得・未選択なら None。
        """
        output_index = self._selected_index(self._output_var, self._output_devices)
        if output_index is None:
            return None
        device = next(
            (d for d in self._output_devices if d["index"] == output_index), None
        )
        return device["name"] if device is not None else None

    def _sync_windows_volume_slider(self) -> None:
        """「🖥 出力音量」スライダーを、今の出力デバイスの実際の値に合わせる。

        出力デバイスを選び直したとき・一覧を更新したときに呼ぶ。

        **実際の値を読めたときだけスライダーを有効化する。** 読めない間は
        無効のままにしておく（:meth:`_build_volume_panel` 参照）。ここで
        「読めなかったから触れるままにしておく」を選ぶと、利用者が
        実際の音量とは無関係な位置からドラッグしてしまい、突然の大音量
        （またはその逆の無音）につながりかねないため。
        """
        if not default_device.is_available():
            return  # 「非対応」表示のまま。無効状態も維持する。

        name = self._get_selected_output_device_name()
        if not name:
            self._set_windows_volume_controls_enabled(False)
            self._windows_volume_label.configure(text="確認中…")
            return

        level = default_device.get_output_volume(name)
        if level is None:
            self._set_windows_volume_controls_enabled(False)
            self._windows_volume_label.configure(text="取得失敗")
            return

        self._set_windows_volume_controls_enabled(True)
        self._windows_volume_value = level
        self._windows_volume_slider.set(level)
        self._windows_volume_label.configure(text=f"{level * 100:.0f}%")

    # ------------------------------------------------------------------
    #  デバイス一覧
    # ------------------------------------------------------------------
    def refresh_devices(self) -> None:
        """デバイス一覧を取り直し、それらしいものを選んでおく。

        動作中は入力・出力を変えられないので、一覧の更新もしない
        （選び直しても反映されず、かえって混乱するため）。
        """
        if self.monitor.is_running:
            return

        # 抜き差ししたデバイスを反映するため、PortAudio に一覧を取り直させる
        rescan_devices()
        self._input_devices = list_input_devices()
        self._output_devices = list_output_devices()

        self._fill_menu(
            self._input_menu,
            self._input_var,
            self._input_devices,
            suggest_input_device(self._input_devices),
        )
        self._fill_menu(
            self._output_menu,
            self._output_var,
            self._output_devices,
            suggest_output_device(self._output_devices),
        )
        self._sync_windows_volume_slider()

    def _fill_menu(
        self,
        menu: ctk.CTkOptionMenu,
        variable: tk.StringVar,
        devices: list[dict[str, Any]],
        preferred: int | None,
    ) -> None:
        """ドロップダウンの中身を入れ替える。

        Args:
            menu: 対象のドロップダウン。
            variable: 選択中のラベルを保持する変数。
            devices: 並べるデバイス。
            preferred: 最初から選んでおくデバイス番号。None なら先頭。
        """
        labels = [str(device["label"]) for device in devices]
        if not labels:
            menu.configure(values=[_NO_DEVICE])
            variable.set(_NO_DEVICE)
            return

        menu.configure(values=labels)
        chosen = labels[0]
        if preferred is not None:
            for device, label in zip(devices, labels):
                if int(device["index"]) == preferred:
                    chosen = label
                    break
        variable.set(chosen)

    def _selected_index(
        self, variable: tk.StringVar, devices: list[dict[str, Any]]
    ) -> int | None:
        """選択中のラベルからデバイス番号を引く。

        Args:
            variable: 選択中のラベルを保持する変数。
            devices: 候補。

        Returns:
            デバイス番号。選ばれていなければ None。
        """
        label = variable.get()
        for device in devices:
            if str(device["label"]) == label:
                return int(device["index"])
        return None

    # ------------------------------------------------------------------
    #  操作
    # ------------------------------------------------------------------
    def _nudge_key(self, delta: int) -> None:
        """キーを ``delta`` 半音動かす。

        Args:
            delta: 動かす半音数（+1 / -1）。
        """
        self._set_key(self.monitor.key_shift + delta)

    def _set_key(self, semitones: int) -> None:
        """キーを設定して表示を更新する。

        Args:
            semitones: 半音数。範囲外は :class:`LiveMonitor` 側で丸められる。
        """
        self.monitor.set_key_shift(semitones)
        shift = self.monitor.key_shift
        self._key_label.configure(
            text="±0" if shift == 0 else f"{shift:+d}",
            text_color=theme.CYAN if shift == 0 else theme.VIOLET,
        )


    def _on_toggle(self) -> None:
        """開始／停止ボタンの処理。"""
        if self.monitor.is_running:
            self.monitor.stop()
            self._restore_default_output()
            # 既定デバイスの切り替え（COM呼び出し）でウィンドウが
            # 背面へ回ることがあるため、前面へ出し直す
            # （__init__ の同じ処理を参照）。
            theme.bring_to_front(self)
            self._update_state_label()
            return

        input_index = self._selected_index(self._input_var, self._input_devices)
        output_index = self._selected_index(self._output_var, self._output_devices)
        if input_index is None or output_index is None:
            messagebox.showwarning(
                "デバイスが選ばれていません",
                "入力と出力の両方を選んでから開始してください。\n"
                "一覧が空の場合は「一覧を更新」を押してください。",
                parent=self,
            )
            return

        # ウィンドウを開いた瞬間には自動でCABLE Inputへ切り替えているが、
        # 一度「停止」すると既定の出力を元のデバイスへ戻す
        # （:meth:`_restore_default_output`）。そのため、2回目以降の
        # 「開始」ではここでもう一度切り替え直さないと、曲の音がケーブルを
        # 通らないまま元のスピーカーへ直接流れてしまい、キーを変えても
        # 何も変わらないように見える（実機で確認済みの不具合）。
        self._auto_switch_to_cable()
        # 既定デバイスの切り替え（COM呼び出し）でウィンドウが背面へ
        # 回ることがあるため、前面へ出し直す（停止時の同じ処理を参照）。
        theme.bring_to_front(self)

        self.monitor.input_device = input_index
        self.monitor.output_device = output_index
        try:
            self.monitor.start()
        except LiveMonitorError as exc:
            # 直前に既定の出力を CABLE Input へ切り替えているので、開始に
            # 失敗したまま放置すると PC 全体が無音になる。停止時と同じく
            # 元へ戻しておく（次に「開始」を押せば再び切り替わる）。
            self._restore_default_output()
            messagebox.showerror(
                with_code(exc, "キー変更を開始できません"), str(exc), parent=self
            )
        else:
            # 一度でも開始（＝接続成功）まで進んだら「もう手順は分かって
            # いる人」とみなし、この場と次回起動以降で手順パネルを
            # 畳んでおく。
            if not self._settings.live_monitor_guide_seen:
                self._settings.live_monitor_guide_seen = True
                save_settings(self._settings)
            if not self._guide_collapsed:
                self._guide_collapsed = True
                self._apply_guide_collapsed()
        self._update_state_label()

    def _on_close(self) -> None:
        """ウィンドウを閉じる。動いていれば必ず止めてから閉じる。"""
        if self._poll_id is not None:
            try:
                self.after_cancel(self._poll_id)
            except tk.TclError:
                pass
            self._poll_id = None
        # 動かした直後に閉じても最後の設定が失われないよう、待たずに保存する
        self._flush_pending_save()
        self.monitor.stop()
        self._restore_default_output()
        self.destroy()

    def _resolve_original_output_name(self) -> str | None:
        """「本来の」既定出力デバイス名を決める。

        今の既定が CABLE 系でなければそれを本来の出力として設定にも
        覚えておく。今の既定が既に CABLE 系なら、前回アプリが強制終了
        などで既定を戻せなかった可能性が高いので、覚えておいた名前を
        戻し先として使う（強制終了は atexit でも拾えないため、次回起動時に
        復旧する唯一の手段）。

        Returns:
            戻し先に使うデバイス名。分からなければ None。
        """
        name = default_device.get_default_output_name()
        saved = self._settings.last_real_output_name
        if name and "CABLE" not in name:
            if saved != name:
                self._settings.last_real_output_name = name
                save_settings(self._settings)
            return name
        return saved or name

    def _capture_original_output_ids(self) -> dict[int, str]:
        """3つの役割それぞれの「本来の」既定出力デバイスIDを決め、設定へ残す。

        CABLE 系でない役割は今の ID を覚え直す。CABLE 系の役割は、前回
        強制終了などで戻せなかった可能性が高いので、前回保存した ID を
        そのまま戻し先に使う（:meth:`_resolve_original_output_name` と同じ考え方）。

        Returns:
            ``{役割: デバイスID}``。分からない役割は含まない。
        """
        saved = self._settings.original_output_ids
        updated = dict(saved)
        for role, (device_id, name) in default_device.get_default_outputs_by_role().items():
            key = default_device.ROLE_KEYS[role]
            if name and "CABLE" not in name:
                updated[key] = device_id
        if updated != saved:
            self._settings.original_output_ids = updated
            save_settings(self._settings)
        return {
            role: updated[key]
            for role, key in default_device.ROLE_KEYS.items()
            if key in updated
        }

    def _restore_default_output_quietly(self) -> None:
        """:meth:`_restore_default_output` を、例外を外へ出さずに呼ぶ。

        atexit やウィンドウ構築の失敗時など、Tk が既に壊れているかも
        しれない場面から呼ぶため。ここで例外を投げると元の例外（本当の
        原因）が見えなくなる。
        """
        try:
            self._restore_default_output()
        except Exception:  # noqa: BLE001 - 後始末の失敗で本来のエラーを隠さない
            logger.exception("既定の出力デバイスを元に戻せませんでした")

    def _auto_switch_to_cable(self) -> bool:
        """既定の出力を CABLE Input へ自動で切り替える。

        VB-CABLE のセットアップ手順②（「既定の出力を CABLE Input に
        する」）を、利用者が Windows のサウンド設定を開かずに済むよう
        代わりにやる。:meth:`__init__` の一番はじめ、
        :attr:`_original_output_name` を記録した**直後**に呼ぶこと
        （記録より先に切り替えると、元の値が分からなくなる）。

        Returns:
            切り替えられたら True。``comtypes``/Windows が無い環境や
            VB-CABLE 未導入で見つからない場合は False（このときは
            :meth:`_build_guide` が手動手順を案内する）。
        """
        if not default_device.is_available():
            return False
        if self._original_output_name and "CABLE" in self._original_output_name:
            return True  # 既に CABLE 系。切り替え済みとして扱う
        return default_device.set_default_output_by_name("CABLE Input")

    def _restore_default_output(self) -> None:
        """既定の出力が CABLE 系のままなら、実際のスピーカー等へ戻す。

        VB-CABLE のセットアップ手順②で「既定の出力を CABLE Input に
        する」を利用者に手動でやってもらっているため、キー変更を
        やめた後もそのままだと**システムの音がどこにも出なくなる**
        （CABLE Input で受け止めてくれる相手が居なくなるため）。
        これを利用者が毎回手動で戻さなくて済むようにする。

        戻し先は次の優先順で決める。

        1. この画面を開いた時点の既定出力（:attr:`_original_output_name`）
           ただし、それ自体が CABLE 系だった場合は使わない（開く前から
           CABLE のままだった＝どれが「本来の」出力か分からないため）
        2. 画面の「出力（鳴らす先）」欄で選んでいるデバイス
           （実在する現実のデバイスのはずなので、無音になるよりまし）

        Windows 以外の環境や ``comtypes`` 未導入では
        :func:`audio.default_device.is_available` が False を返すため、
        何もせず静かに終わる。
        """
        if not default_device.is_available():
            return

        # eConsole 役割だけでなく3役割すべてを見る。eConsole だけ既に
        # 元へ戻っていても、他の役割が CABLE Input に取り残されている
        # ことがあり（実機で確認済み: 「たまに音が出ない・アプリを
        # 開き直すと直る」不具合の原因だった）、1役割だけの判定だと
        # それを見逃して復元をスキップしてしまう。
        if not default_device.is_any_role_still("CABLE"):
            return  # 既に CABLE 以外（利用者が自分で戻した等）なら触らない

        # まず役割ごとに、起動前に覚えたデバイスIDへ個別に戻す（3役割が
        # 別々のデバイスを向いていた場合もそのまま再現する）。元の機器が
        # 抜かれている役割はスキップし、下の名前による復元に任せる。
        pending: list[int] = []
        original_ids = getattr(self, "_original_output_ids", {})
        current = default_device.get_default_outputs_by_role()
        if not current:
            # 役割ごとの状態を読めなかった。従来どおり3役割まとめて名前で戻す
            pending = list(default_device.ROLES)
        for role, (_, name) in current.items():
            if not name or "CABLE" not in name:
                continue  # この役割は CABLE を向いていない。触らない
            device_id = original_ids.get(role)
            if device_id and default_device.set_default_output_by_id(device_id, role):
                continue
            pending.append(role)
        if not pending:
            return
        roles = tuple(pending)

        if self._original_output_name and "CABLE" not in self._original_output_name:
            if default_device.set_default_output_by_name(self._original_output_name, roles):
                return
            # 元のデバイスが外された等で戻せなければ、選択中の出力へ戻す

        output_var = getattr(self, "_output_var", None)
        if output_var is None:
            return  # 画面の構築前に失敗した（出力欄がまだ無い）
        output_index = self._selected_index(output_var, self._output_devices)
        if output_index is None:
            return
        device = next(
            (d for d in self._output_devices if d["index"] == output_index), None
        )
        if device is not None:
            default_device.set_default_output_by_name(device["name"], roles)

    # ------------------------------------------------------------------
    #  状態表示
    # ------------------------------------------------------------------
    def _schedule_poll(self) -> None:
        """状態表示の更新を予約する。"""
        self._poll_id = self.after(_POLL_MS, self._poll)

    def _poll(self) -> None:
        """定期的に状態表示を更新する。"""
        if self.monitor.check_stalled():
            # デバイスが切れて自動停止した。手動で「停止」したときと同じく
            # 既定の出力を戻しておかないと PC 全体が無音のままになる。
            self._restore_default_output()
        self._update_state_label()
        self._schedule_poll()

    def _update_state_label(self) -> None:
        """稼働状態とボタンの見た目をそろえる。"""
        running = self.monitor.is_running
        self._toggle_button.configure(text="■ 停止" if running else "▶ 開始")
        if running:
            theme.style_solid_button(
                self._toggle_button, theme.DANGER, theme.DANGER_HOVER, theme.TEXT
            )
        else:
            theme.style_solid_button(self._toggle_button, theme.CYAN, theme.CYAN_HOVER)

        state = "normal" if not running else "disabled"
        self._input_menu.configure(state=state)
        self._output_menu.configure(state=state)
        self._refresh_button.configure(state=state)

        error = self.monitor.error
        self._update_connection_dot(error)
        if error:
            self._guide_status_label.configure(
                text="● デバイスの接続に失敗", text_color=theme.DANGER
            )
            self._state_label.configure(text=f"⚠ {error}", text_color=theme.DANGER)
            return
        if not running:
            self._guide_status_label.configure(text="", text_color=theme.MUTED)
            self._state_label.configure(
                text="停止中。手順②まで済ませてから開始してください。",
                text_color=theme.MUTED,
            )
            return

        self._guide_status_label.configure(
            text="● デバイス接続中", text_color=theme.GREEN
        )

        layout = "ステレオ" if self.monitor.channels >= 2 else "モノラル"
        health = self.monitor.health
        text = (
            f"● 稼働中  {self.monitor.sample_rate} Hz・{layout}・"
            f"遅延 約 {self.monitor.latency_ms:.0f} ms"
        )
        skips = health["starved"] + health["resyncs"] + health["dropped"]
        if health["resyncs"] or health["dropped"] or health["starved"] > 8:
            # 起動直後の数回は先読みを溜める分なので、そこは数えない
            text += f"（音飛び {skips} 回）"
        self._state_label.configure(text=text, text_color=theme.GREEN)

    def _update_connection_dot(self, error: str | None) -> None:
        """見出し横の●（接続状態）を更新する。

        入力・出力の両方が見つかっていて、かつ ``monitor.error`` が無ければ
        「接続済み」（緑）。片方でも見つからない、またはエラーがあれば
        「接続エラー」（赤）。稼働中かどうかは問わない
        （動かしていなくても、デバイスさえ揃っていれば緑でよい）。

        Args:
            error: :attr:`LiveMonitor.error` の現在値。
        """
        has_devices = (
            self._input_var.get() != _NO_DEVICE
            and self._output_var.get() != _NO_DEVICE
        )
        connected = has_devices and not error
        if connected:
            self._connection_dot.configure(text_color=theme.GREEN)
            self._connection_status_label.configure(
                text="接続済み", text_color=theme.GREEN
            )
        else:
            self._connection_dot.configure(text_color=theme.DANGER)
            self._connection_status_label.configure(
                text="接続エラー", text_color=theme.DANGER
            )
