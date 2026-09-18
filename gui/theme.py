"""アプリ全体の見た目（配色・フォント・共通ウィジェット）を集約するモジュール。

``config.py`` が「挙動の設定」を集めるのに対し、こちらは
**見た目だけ**を集める。ここを書き換えるだけでアプリ全体の
トーンを差し替えられるようにしてあり、解析側（``audio/``）や
GUI のロジックからは一切参照されない。

依存の向きは ``gui → theme → config`` の一方向のみ。

デザインの方針
--------------
ダークな紺〜黒のグラデーション地に、シアン／バイオレットのネオンを
差し色として置く「オーディオコンソール風」。値の表示は等幅フォント
（``FONT_NUM``）で桁を揃え、歌っている最中に数字が横揺れしないようにする。

Tkinter にはアルファ合成・影・グローが無いため、以下で代用している。

* グラデーション → 帯を細かく分割して少しずつ色を変えて塗る
  （:class:`GradientBackdrop` / :class:`AccentRule`）
* グロー → 同じ文字を少しずつずらし、背景側へ寄せた色で重ね描きする
  （:class:`GlowText`）
* 半透明 → 背景色との線形補間で「透けたように見える色」を作る
  （:func:`blend` / :func:`over`）
"""

from __future__ import annotations

import math
import tkinter as tk
from typing import Any

import customtkinter as ctk

import config

# ==============================
#  パレット
# ==============================
BG_DEEP: str = "#04060d"
"""ウィンドウ最下部の地色（ほぼ黒の紺）。"""

BG_BASE: str = "#080e1b"
"""ウィンドウ上部の地色。下へ向かって :data:`BG_DEEP` へ落ちる。"""

PANEL: str = "#0d1524"
"""カード（パネル）の地色。"""

PANEL_SOFT: str = "#121c2e"
"""カード内でさらに一段沈める領域の地色。"""

PANEL_EDGE: str = "#1d2941"
"""カードの輪郭線。"""

PANEL_EDGE_HI: str = "#2e4269"
"""強調したいカードの輪郭線。"""

TEXT: str = "#e9eefb"
"""通常の文字色。"""

TEXT_DIM: str = "#a4b3cd"
"""やや落とした文字色。"""

MUTED: str = "#66758f"
"""見出し・補助テキストの文字色。"""

CYAN: str = "#22d3ee"
"""主アクセント（地声・進行中の操作）。"""

CYAN_HOVER: str = "#4ae3f7"
"""シアンのホバー色。"""

VIOLET: str = "#c084fc"
"""副アクセント（裏声）。"""

VIOLET_HOVER: str = "#d4a6ff"
"""バイオレットのホバー色。"""

GREEN: str = "#34d399"
"""成功・ミックスボイス。"""

GREEN_HOVER: str = "#5ee7b5"
"""グリーンのホバー色。"""

AMBER: str = "#fbbf24"
"""警告。音量メーターの高音量域にも使う。"""

DANGER: str = "#fb3b64"
"""停止・危険。音量メーターのピーク域にも使う。"""

DANGER_HOVER: str = "#ff5c80"
"""ダンジャーのホバー色。"""

DANGER_DEEP: str = "#c22a4d"
"""塗りつぶしボタンの地色に使う、沈んだ赤。

``DANGER`` をそのまま面で使うと、``state="disabled"`` のときも彩度が
高いまま残って「押せそう」に見えてしまう（customtkinter は
``fg_color`` を無効状態で自動的には落とさない）。面には常にこちらを使う。
"""

# ==============================
#  フォント
# ==============================
FONT_UI: str = config.APP_FONT
"""日本語を含む通常の UI 文字。``config.APP_FONT`` に追従する。"""

FONT_NUM: str = "Consolas"
"""数値・英字用の等幅フォント。

Hz・dB・経過時間のように毎フレーム書き換わる値に使う。プロポーショナル
フォントだと桁が変わるたびに文字列の幅が変わって左右に揺れて見えるため。
**ASCII を表示する箇所にだけ使うこと**（日本語や記号は :data:`FONT_UI`）。
"""


# ==============================
#  色ユーティリティ
# ==============================
def _clamp255(value: float) -> int:
    """0〜255 の範囲に収めた整数を返す。

    Args:
        value: 変換前の値。

    Returns:
        0〜255 に収めた整数。
    """
    return max(0, min(255, round(value)))


def hex_to_rgb(color: str) -> tuple[int, int, int]:
    """``"#rrggbb"`` 形式の色を RGB のタプルへ変換する。

    Args:
        color: 16進数カラーコード。

    Returns:
        (R, G, B) の各 0〜255 のタプル。
    """
    color = color.lstrip("#")
    return (int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16))


def rgb_to_hex(rgb: tuple[int, int, int] | tuple[float, float, float]) -> str:
    """RGB のタプルを ``"#rrggbb"`` 形式の色へ変換する。

    Args:
        rgb: (R, G, B) の各 0〜255 のタプル。

    Returns:
        16進数カラーコード。
    """
    return "#{:02x}{:02x}{:02x}".format(*(_clamp255(c) for c in rgb))


def blend(color_a: str, color_b: str, ratio: float) -> str:
    """2 色を線形補間する。

    Args:
        color_a: 基準色（``ratio=0`` のときの色）。
        color_b: 混ぜる色（``ratio=1`` のときの色）。
        ratio: 混ぜる割合（0.0〜1.0）。

    Returns:
        補間後の 16進数カラーコード。
    """
    a = hex_to_rgb(color_a)
    b = hex_to_rgb(color_b)
    return rgb_to_hex(tuple(a[i] + (b[i] - a[i]) * ratio for i in range(3)))


def bring_to_front(window: Any, *, topmost_ms: int = 250) -> None:
    """新しく開いたウィンドウを、開いた瞬間だけ確実に前面へ出す。

    **単に ``lift()``/``focus_force()`` を呼ぶだけでは足りないことがある。**
    Windows では、フォーカスを奪う権限が OS の「フォーカス盗み防止」に
    阻まれ、同じプロセスの親ウィンドウの裏に新しい ``CTkToplevel`` が
    隠れたまま出てこないケースが複数の画面（設定・ヘルプ・動画解析
    ウィンドウ等）で確認された。一時的に ``-topmost`` 属性を立てて
    強制的に最前面へ引き上げ、:data:`topmost_ms` ミリ秒後に解除する
    ことで、**常に最前面固定にはせず**「開いた瞬間だけ必ず前へ出る」
    動作にする。

    呼び出し側であらかじめ ``transient()``/``grab_set()`` を設定して
    いても問題なく併用できる（``-topmost`` は別属性のため競合しない）。

    Args:
        window: 前面に出す対象（``CTkToplevel``/``tk.Toplevel``）。
        topmost_ms: ``-topmost`` を保つ時間[ミリ秒]。
    """
    try:
        window.lift()
        window.attributes("-topmost", True)
        window.focus_force()
    except tk.TclError:
        return

    def _release_topmost() -> None:
        try:
            window.attributes("-topmost", False)
        except tk.TclError:
            pass

    window.after(topmost_ms, _release_topmost)


class HoverTooltip:
    """カーソルを乗せると、少し遅れて項目名を出す小さな吹き出し。

    :func:`attach_tooltip` から使う。ウィジェット自体には手を入れず、
    ``<Enter>``/``<Leave>``/``<Motion>`` を bind するだけなので、
    既存のクリック処理（``command=`` や個別の ``<Button-1>`` bind）とは
    競合しない。
    """

    _DELAY_MS = 450
    """カーソルを乗せてから吹き出しが出るまでの遅延[ミリ秒]。

    0 にすると、単にボタンを押しに行く動きでもチラつくため、
    「少し迷ったら出る」程度の間を持たせている。
    """

    def __init__(self, widget: Any, text: str) -> None:
        """HoverTooltip を初期化する。

        Args:
            widget: 吹き出しを出す対象のウィジェット。
            text: 表示する文字列。
        """
        self._widget = widget
        self._text = text
        self._after_id: str | None = None
        self._popup: tk.Toplevel | None = None
        widget.bind("<Enter>", self._on_enter, add="+")
        widget.bind("<Leave>", self._on_leave, add="+")
        widget.bind("<Button-1>", self._on_leave, add="+")

    def _on_enter(self, event: Any) -> None:
        self._cancel_pending()
        self._after_id = self._widget.after(self._DELAY_MS, self._show, event.x_root, event.y_root)

    def _on_leave(self, _event: Any) -> None:
        self._cancel_pending()
        self._hide()

    def _cancel_pending(self) -> None:
        if self._after_id is not None:
            try:
                self._widget.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None

    def _show(self, x_root: int, y_root: int) -> None:
        self._hide()
        popup = tk.Toplevel(self._widget)
        popup.wm_overrideredirect(True)
        popup.wm_attributes("-topmost", True)
        popup.configure(bg=CYAN)
        label = tk.Label(
            popup,
            text=self._text,
            font=(config.APP_FONT, 10),
            bg=PANEL_SOFT,
            fg=TEXT,
            padx=8,
            pady=3,
        )
        label.pack(padx=1, pady=1)
        popup.geometry(f"+{x_root + 14}+{y_root + 18}")
        self._popup = popup

    def _hide(self) -> None:
        if self._popup is not None:
            try:
                self._popup.destroy()
            except tk.TclError:
                pass
            self._popup = None


def attach_tooltip(widget: Any, text: str) -> HoverTooltip:
    """``widget`` にホバーで項目名を出すツールチップを付ける。

    Args:
        widget: 対象のウィジェット。
        text: 表示する文字列。

    Returns:
        作成した :class:`HoverTooltip`（参照を保持したい場合に使う。
        保持しなくても widget 自身が bind を持つため動作は継続する）。
    """
    return HoverTooltip(widget, text)


def disable_widget_mousewheel(widget: Any) -> None:
    """スクロール可能な親の中でホイールに反応してしまうウィジェットを黙らせる。

    :class:`customtkinter.CTkSlider` は内部キャンバスへ直接
    ``<MouseWheel>``（Linux では ``<Button-4>``/``<Button-5>``）を
    バインドしており、:class:`customtkinter.CTkScrollableFrame` の中に
    置くと「パネルをスクロールしたいだけなのにスライダーの値が動く」
    事故が起きる（設定画面で実機確認済み）。ウィジェット固有のバインドを
    外し、親のスクロール処理（``bind_all`` 側）へ委ねる。

    Args:
        widget: バインドを外す対象（``_canvas`` 属性を持つ CTk ウィジェット）。
    """
    canvas = getattr(widget, "_canvas", None)
    if canvas is None:
        return
    for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
        canvas.unbind(sequence)


def over(color: str, background: str, alpha: float) -> str:
    """``color`` を ``alpha`` の不透明度で ``background`` に重ねた色を返す。

    Tkinter は半透明を扱えないため、あらかじめ合成済みの単色を作って
    「透けているように見せる」ために使う。

    Args:
        color: 前景色。
        background: 背景色。
        alpha: 前景の不透明度（0.0〜1.0）。

    Returns:
        合成後の 16進数カラーコード。
    """
    return blend(background, color, alpha)


def scaling_of(widget: tk.Misc) -> float:
    """ウィジェットに適用すべき DPI 倍率を返す。

    customtkinter は起動時に Windows の DPI 対応を有効化するが、これは
    「OS 側にぼやけた拡大をさせない」だけで、素の tkinter ウィジェットの
    フォント・ピクセルサイズを自動では拡大しない。CTk ウィジェットは内部で
    この倍率を掛けて描画しているため、素の tkinter で組む部品も同じ倍率を
    自分で掛けないと高 DPI 環境で小さくつぶれる。

    Args:
        widget: 対象のウィジェット。

    Returns:
        DPI 倍率（100% なら 1.0）。
    """
    try:
        return ctk.ScalingTracker.get_widget_scaling(widget)
    except Exception:  # noqa: BLE001 - 取得できない環境では等倍で妥協する
        return 1.0


# ==============================
#  共通ウィジェット
# ==============================
class GradientBackdrop(tk.Canvas):
    """ウィンドウ全体の背景になる、縦グラデーション＋上端グローの下地。

    ``place(relwidth=1, relheight=1)`` で親いっぱいに敷き、``lower()`` で
    最背面へ送って使う。grid で配置した他のウィジェットはこの上に描画される
    （親の ``fg_color`` を ``"transparent"`` にしておくこと）。
    """

    def __init__(
        self,
        master: Any,
        top: str = BG_BASE,
        bottom: str = BG_DEEP,
        accent: str = CYAN,
        accent_b: str = VIOLET,
        **kwargs: Any,
    ) -> None:
        """GradientBackdrop を初期化する。

        Args:
            master: 親ウィジェット。
            top: 上端の色。
            bottom: 下端の色。
            accent: 上端に走らせるネオンラインの左側の色。
            accent_b: 同じく右側の色。
            **kwargs: ``tk.Canvas`` に渡す追加引数。
        """
        super().__init__(
            master, highlightthickness=0, bd=0, bg=top, takefocus=0, **kwargs
        )
        self._top = top
        self._bottom = bottom
        self._accent = accent
        self._accent_b = accent_b
        self._scale = scaling_of(self)
        self._resize_job: str | None = None
        self.bind("<Configure>", self._on_configure)

    def send_to_back(self) -> None:
        """ウィジェットとしての重なり順を最背面へ送る。

        **``self.lower()`` は使えない。** ``tk.Canvas`` の ``lower()`` は
        「キャンバス内に描いたアイテム」を下げる ``tag_lower`` の別名として
        上書きされており、引数なしで呼ぶと ``wrong # args`` で落ちる
        （実機で確認済み）。ウィジェット自体の重なり順を変えるには、
        上書き前の :meth:`tk.Misc.lower` を明示的に呼ぶ必要がある。
        """
        tk.Misc.lower(self)

    def _on_configure(self, event: Any) -> None:
        """サイズが決まる／変わるたびに呼ばれる。実際の再描画は間引く。

        ウィンドウのドラッグリサイズ中は ``<Configure>`` が 1 秒間に
        何十回も飛ぶ。毎回 :meth:`_redraw`（約100個の矩形を描き直す）を
        呼ぶとカクつく（実機で確認済み）ため、**直近のイベントから
        一定時間イベントが来なかったときだけ**実際に描き直す
        （デバウンス）。ドラッグ中は前回サイズのまま描画が止まって
        見えるが、指を離した瞬間に正しいサイズへ描き直る。

        Args:
            event: Tkinter の Configure イベント。
        """
        width, height = event.width, event.height
        if self._resize_job is not None:
            self.after_cancel(self._resize_job)
        self._resize_job = self.after(60, self._on_resize_settled, width, height)

    def _on_resize_settled(self, width: int, height: int) -> None:
        """デバウンス後に実際の再描画を行う。

        Args:
            width: 確定した幅[px]。
            height: 確定した高さ[px]。
        """
        self._resize_job = None
        self._redraw(width, height)

    def _redraw(self, width: int, height: int) -> None:
        """縦グラデーションと上端のネオンラインを描く。

        Args:
            width: 現在の幅[px]。
            height: 現在の高さ[px]。
        """
        self.delete("all")
        if width <= 0 or height <= 0:
            return

        # 縦グラデーション本体。帯の数は「滑らかさ」と「描画コスト」の
        # 折り合いで 40 程度あれば段差は見えない。
        bands = 40
        for i in range(bands):
            y0 = height * i // bands
            y1 = height * (i + 1) // bands
            # 上 1/3 はゆっくり、下 2/3 は速く沈めると奥行きが出る
            t = (i / (bands - 1)) ** 1.35
            self.create_rectangle(
                -1, y0, width + 1, y1 + 1,
                fill=blend(self._top, self._bottom, t), outline="",
            )

        # 上端のネオンライン（左シアン → 右バイオレット）と、その下の淡い残光。
        line_h = max(2, round(2 * self._scale))
        glow_h = max(6, round(14 * self._scale))
        steps = max(8, width // max(1, round(10 * self._scale)))
        for i in range(steps):
            x0 = width * i // steps
            x1 = width * (i + 1) // steps
            t = i / max(1, steps - 1)
            neon = blend(self._accent, self._accent_b, t)
            # 残光: 上ほど濃く、下へ向けて背景へ溶ける
            for g in range(3):
                gy0 = line_h + glow_h * g // 3
                gy1 = line_h + glow_h * (g + 1) // 3
                self.create_rectangle(
                    x0, gy0, x1 + 1, gy1,
                    fill=over(neon, self._top, 0.16 - 0.05 * g), outline="",
                )
            self.create_rectangle(
                x0, 0, x1 + 1, line_h, fill=neon, outline=""
            )


class AccentRule(tk.Canvas):
    """左右にグラデーションする細いネオンの区切り線。

    見出しの下や、カードの上端に短く置いて使う。
    """

    def __init__(
        self,
        master: Any,
        colors: tuple[str, str] = (CYAN, VIOLET),
        bg: str = BG_BASE,
        thickness: int = 2,
        fade_edges: bool = True,
        **kwargs: Any,
    ) -> None:
        """AccentRule を初期化する。

        Args:
            master: 親ウィジェット。
            colors: (左端の色, 右端の色)。
            bg: 背景色（``fade_edges`` で溶かす先）。
            thickness: 線の太さ[px]（DPI 100% 基準）。
            fade_edges: True なら両端を背景色へフェードさせる。
            **kwargs: ``tk.Canvas`` に渡す追加引数。
        """
        scale = scaling_of(master)
        super().__init__(
            master,
            height=max(1, round(thickness * scale)),
            highlightthickness=0,
            bd=0,
            bg=bg,
            takefocus=0,
            **kwargs,
        )
        self._colors = colors
        self._bg = bg
        self._fade_edges = fade_edges
        self._resize_job: str | None = None
        self.bind("<Configure>", self._on_configure)

    def _on_configure(self, event: Any) -> None:
        """サイズが決まる／変わるたびに呼ばれる。実際の再描画は間引く。

        ウィンドウ幅いっぱいに伸びる線のため、リサイズ中に毎回
        （最大160個の矩形を）描き直すとカクつく。
        :class:`GradientBackdrop` と同じデバウンスを行う。

        Args:
            event: Tkinter の Configure イベント。
        """
        width, height = event.width, event.height
        if self._resize_job is not None:
            self.after_cancel(self._resize_job)
        self._resize_job = self.after(60, self._on_resize_settled, width, height)

    def _on_resize_settled(self, width: int, height: int) -> None:
        """デバウンス後に実際の再描画を行う。

        Args:
            width: 確定した幅[px]。
            height: 確定した高さ[px]。
        """
        self._resize_job = None
        self._draw(width, height)

    def _draw(self, width: int, height: int) -> None:
        """グラデーションの線を描く。

        Args:
            width: 現在の幅[px]。
            height: 現在の高さ[px]。
        """
        self.delete("all")
        if width <= 0 or height <= 0:
            return

        steps = max(8, min(160, width // 4))
        left, right = self._colors
        for i in range(steps):
            x0 = width * i // steps
            x1 = width * (i + 1) // steps
            t = i / max(1, steps - 1)
            color = blend(left, right, t)
            if self._fade_edges:
                # 中央 1.0、両端 0.0 の三角窓で背景へ溶かす
                alpha = 1.0 - abs(t - 0.5) * 2.0
                color = over(color, self._bg, alpha ** 0.6)
            self.create_rectangle(x0, 0, x1 + 1, height, fill=color, outline="")


class GlowText(tk.Canvas):
    """周囲にネオンの残光をまとった大きな文字。

    Tkinter に影・グローは無いため、同じ文字を少しずつずらして
    背景側へ寄せた色で重ね描きし、最後に本体を上へ乗せることで
    「にじんで光っている」ように見せている。

    :meth:`configure` が ``text`` / ``text_color`` / ``glow`` を受け取るので、
    ``CTkLabel`` と同じ感覚で ``configure(text=...)`` と書ける。
    """

    def __init__(
        self,
        master: Any,
        text: str = "",
        size: int = 54,
        family: str = FONT_UI,
        weight: str = "bold",
        color: str = TEXT,
        glow: str = CYAN,
        bg: str = PANEL,
        **kwargs: Any,
    ) -> None:
        """GlowText を初期化する。

        Args:
            master: 親ウィジェット。
            text: 初期の表示文字列。
            size: フォントサイズ（DPI 100% 基準）。
            family: フォントファミリ。
            weight: フォントの太さ（``"bold"`` など）。
            color: 文字本体の色。
            glow: 残光の色。
            bg: 背景色（残光を溶かす先）。
            **kwargs: ``tk.Canvas`` に渡す追加引数。
        """
        scale = scaling_of(master)
        self._size = max(1, round(size * scale))
        self._scale = scale
        super().__init__(
            master,
            height=round(self._size * 1.22),
            highlightthickness=0,
            bd=0,
            bg=bg,
            takefocus=0,
            **kwargs,
        )
        self._text = text
        self._font = (family, self._size, weight)
        self._color = color
        self._glow = glow
        self._bg = bg
        self._drawn: tuple[str, str, str, int] | None = None
        self.bind("<Configure>", self._on_configure)

    def configure(self, **kwargs: Any) -> Any:  # type: ignore[override]
        """表示内容・色を更新する（``CTkLabel`` と同じ呼び出し方をできるようにする）。

        Args:
            **kwargs: ``text`` / ``text_color`` / ``glow`` のほか、
                ``tk.Canvas`` が解釈するオプション。

        Returns:
            ``tk.Canvas.configure`` の戻り値（独自オプションだけの場合は None）。
        """
        text = kwargs.pop("text", None)
        color = kwargs.pop("text_color", None)
        glow = kwargs.pop("glow", None)
        if text is not None:
            self._text = text
        if color is not None:
            self._color = color
        if glow is not None:
            self._glow = glow
        result = super().configure(**kwargs) if kwargs else None
        if text is not None or color is not None or glow is not None:
            self._redraw()
        return result

    config = configure

    def _on_configure(self, event: Any) -> None:
        """サイズが決まる／変わるたびに描き直す。

        Args:
            event: Tkinter の Configure イベント。
        """
        self._drawn = None  # 幅が変わったら中央がずれるので必ず描き直す
        self._redraw()

    def _redraw(self) -> None:
        """残光つきの文字を描き直す。

        毎フレーム呼ばれても無駄が出ないよう、前回と同じ内容・同じ幅なら
        何もしない（音程表示は 20fps で更新されるため）。
        """
        width = self.winfo_width()
        height = self.winfo_height()
        if width <= 1 or height <= 1:
            return

        signature = (self._text, self._color, self._glow, width)
        if signature == self._drawn:
            return
        self._drawn = signature

        self.delete("all")
        cx, cy = width / 2, height / 2

        # 外側から内側へ 2 段の残光。オフセットが大きいほど背景へ寄せる。
        rings = (
            (max(2, round(3 * self._scale)), 0.20),
            (max(1, round(1 * self._scale)), 0.42),
        )
        for offset, alpha in rings:
            halo = over(self._glow, self._bg, alpha)
            for dx, dy in (
                (-offset, 0), (offset, 0), (0, -offset), (0, offset),
                (-offset, -offset), (offset, -offset),
                (-offset, offset), (offset, offset),
            ):
                self.create_text(
                    cx + dx, cy + dy, text=self._text,
                    font=self._font, fill=halo,
                )
        self.create_text(cx, cy, text=self._text, font=self._font, fill=self._color)


class SegmentMeter(tk.Canvas):
    """LED を並べたようなセグメント式の音量メーター。

    ``CTkProgressBar`` と同じく :meth:`set` に 0.0〜1.0 を渡して更新する
    （そのまま差し替えられる）。直近のピークだけを別色で保持し、
    呼ばれるたびに少しずつ落としてゆく（ピークホールド）。
    """

    def __init__(
        self,
        master: Any,
        segments: int = 32,
        height: int = 16,
        bg: str = PANEL,
        **kwargs: Any,
    ) -> None:
        """SegmentMeter を初期化する。

        Args:
            master: 親ウィジェット。
            segments: セグメントの分割数。
            height: メーターの高さ[px]（DPI 100% 基準）。
            bg: 背景色。
            **kwargs: ``tk.Canvas`` に渡す追加引数。
        """
        scale = scaling_of(master)
        super().__init__(
            master,
            height=max(6, round(height * scale)),
            highlightthickness=0,
            bd=0,
            bg=bg,
            takefocus=0,
            **kwargs,
        )
        self._segments = max(4, segments)
        self._bg = bg
        self._scale = scale
        self._value = 0.0
        self._peak = 0.0
        self.bind("<Configure>", lambda _event: self._redraw())

    def set(self, value: float) -> None:
        """メーターの値を更新する。

        Args:
            value: 0.0〜1.0 の比率。範囲外は丸める。
        """
        self._value = min(1.0, max(0.0, float(value)))
        # ピークは即座に追従し、下がるときだけゆっくり落とす。
        # 20fps 更新なので 1 回 0.012 なら約 1.4 秒で 0 まで戻る。
        self._peak = max(self._value, self._peak - 0.012)
        self._redraw()

    def get(self) -> float:
        """現在の値を返す（``CTkProgressBar`` との互換のため）。

        Returns:
            0.0〜1.0 の比率。
        """
        return self._value

    @staticmethod
    def _segment_color(position: float) -> str:
        """セグメントの位置に応じた点灯色を返す。

        Args:
            position: 左端 0.0、右端 1.0 の位置。

        Returns:
            16進数カラーコード。
        """
        if position < 0.55:
            return blend(CYAN, GREEN, position / 0.55)
        if position < 0.82:
            return blend(GREEN, AMBER, (position - 0.55) / 0.27)
        return blend(AMBER, DANGER, (position - 0.82) / 0.18)

    def _redraw(self) -> None:
        """セグメントを描き直す。"""
        width = self.winfo_width()
        height = self.winfo_height()
        if width <= 1 or height <= 1:
            return

        self.delete("all")
        gap = max(1, round(2 * self._scale))
        total_gap = gap * (self._segments - 1)
        seg_w = max(1.0, (width - total_gap) / self._segments)
        lit_count = int(self._value * self._segments + 0.5)
        peak_index = int(self._peak * self._segments + 0.5) - 1

        for i in range(self._segments):
            x0 = i * (seg_w + gap)
            x1 = x0 + seg_w
            position = i / max(1, self._segments - 1)
            if i < lit_count:
                color = self._segment_color(position)
            elif i == peak_index:
                # ピークホールドは点灯色を淡くしたもの
                color = over(self._segment_color(position), self._bg, 0.45)
            else:
                color = over(TEXT, self._bg, 0.07)
            self.create_rectangle(x0, 0, x1, height, fill=color, outline="")


class HistoryGraph(tk.Canvas):
    """直近N件の値を折れ線＋塗りつぶしで表示する、簡易な履歴グラフ。

    タスクマネージャーのメモリグラフのような「直近60秒の推移」を
    軽量に出したいだけの用途向け。:meth:`push` で新しい値
    （0.0〜1.0）を追加するたびに再描画する。折れ線チャート用の
    ライブラリを増やしたくないため、:class:`SegmentMeter` と同じ
    ``tk.Canvas`` 手描き方式にしてある。
    """

    def __init__(
        self,
        master: Any,
        max_points: int = 60,
        height: int = 110,
        color: str = CYAN,
        bg: str = PANEL,
        **kwargs: Any,
    ) -> None:
        """HistoryGraph を初期化する。

        Args:
            master: 親ウィジェット。
            max_points: 保持する点の数（例: 1秒ごとに:meth:`push`するなら60で60秒分）。
            height: グラフの高さ[px]（DPI 100% 基準）。
            color: 折れ線の色。
            bg: 背景色。
            **kwargs: ``tk.Canvas`` に渡す追加引数。
        """
        scale = scaling_of(master)
        super().__init__(
            master,
            height=max(20, round(height * scale)),
            highlightthickness=0,
            bd=0,
            bg=bg,
            takefocus=0,
            **kwargs,
        )
        self._max_points = max(2, max_points)
        self._values: list[float] = []
        self._color = color
        self._bg = bg
        self.bind("<Configure>", lambda _event: self._redraw())

    def push(self, value: float) -> None:
        """新しい値を右端に追加する（古い値は左へ押し出されて消える）。

        Args:
            value: 0.0〜1.0の比率。範囲外は丸める。
        """
        self._values.append(min(1.0, max(0.0, float(value))))
        if len(self._values) > self._max_points:
            del self._values[: len(self._values) - self._max_points]
        self._redraw()

    def _redraw(self) -> None:
        """グラフを描き直す。"""
        width = self.winfo_width()
        height = self.winfo_height()
        if width <= 1 or height <= 1:
            return

        self.delete("all")
        grid_color = over(TEXT, self._bg, 0.08)
        for frac in (0.25, 0.5, 0.75):
            y = height * (1 - frac)
            self.create_line(0, y, width, y, fill=grid_color)

        n = len(self._values)
        if n < 2:
            return
        step = width / (self._max_points - 1)
        offset = (self._max_points - n) * step
        points = [
            (offset + i * step, height * (1 - v)) for i, v in enumerate(self._values)
        ]

        fill_color = over(self._color, self._bg, 0.22)
        polygon = [(points[0][0], height), *points, (points[-1][0], height)]
        self.create_polygon(polygon, fill=fill_color, outline="")
        self.create_line(
            *[coord for point in points for coord in point],
            fill=self._color,
            width=2,
            joinstyle="round",
        )


class FrequencyCurve(tk.Canvas):
    """周波数特性（dB）を対数軸で描く、静的な特性カーブ。

    :class:`HistoryGraph` が「時間の経過で伸びる折れ線」なのに対し、
    こちらは :meth:`set_curve` を呼ぶたびに全体を描き直す「今の設定は
    こういうカーブになっている」という静的なスナップショット表示。
    EQのようにパラメータが変わるたびに形が変わるものに向く。
    """

    def __init__(
        self,
        master: Any,
        height: int = 90,
        color: str = CYAN,
        bg: str = PANEL,
        db_range: float = 15.0,
        **kwargs: Any,
    ) -> None:
        """FrequencyCurve を初期化する。

        Args:
            master: 親ウィジェット。
            height: グラフの高さ[px]（DPI 100% 基準）。
            color: 曲線の色。
            bg: 背景色。
            db_range: 縦軸の表示範囲[dB]（±この値で上下いっぱいになる）。
            **kwargs: ``tk.Canvas`` に渡す追加引数。
        """
        scale = scaling_of(master)
        super().__init__(
            master,
            height=max(20, round(height * scale)),
            highlightthickness=0,
            bd=0,
            bg=bg,
            takefocus=0,
            **kwargs,
        )
        self._color = color
        self._bg = bg
        self._db_range = max(1.0, db_range)
        self._freqs: list[float] = []
        self._values_db: list[float] = []
        self.bind("<Configure>", lambda _event: self._redraw())

    def set_curve(self, freqs_hz: list[float], values_db: list[float]) -> None:
        """描くカーブを差し替える。

        Args:
            freqs_hz: 昇順の周波数[Hz]の並び（対数軸の位置決めに使う）。
            values_db: 各周波数での値[dB]。``freqs_hz`` と同じ長さ。
        """
        self._freqs = list(freqs_hz)
        self._values_db = list(values_db)
        self._redraw()

    def set_color(self, color: str) -> None:
        """曲線の色を変える（ON/OFFに合わせたグレーアウト表示などに使う）。"""
        if color == self._color:
            return
        self._color = color
        self._redraw()

    def _redraw(self) -> None:
        """カーブを描き直す。"""
        width = self.winfo_width()
        height = self.winfo_height()
        if width <= 1 or height <= 1 or len(self._freqs) < 2:
            return

        self.delete("all")
        grid_color = over(TEXT, self._bg, 0.08)
        zero_y = height / 2
        self.create_line(0, zero_y, width, zero_y, fill=grid_color)
        for frac in (0.25, 0.75):
            self.create_line(0, height * frac, width, height * frac, fill=grid_color)

        log_min = math.log10(self._freqs[0])
        log_max = math.log10(self._freqs[-1])
        log_span = max(1e-9, log_max - log_min)

        def x_of(freq: float) -> float:
            return (math.log10(freq) - log_min) / log_span * width

        def y_of(db: float) -> float:
            clamped = max(-self._db_range, min(self._db_range, db))
            return height / 2 - (clamped / self._db_range) * (height / 2)

        points = [(x_of(f), y_of(v)) for f, v in zip(self._freqs, self._values_db)]
        fill_color = over(self._color, self._bg, 0.18)
        polygon = [(points[0][0], zero_y), *points, (points[-1][0], zero_y)]
        self.create_polygon(polygon, fill=fill_color, outline="")
        self.create_line(
            *[coord for point in points for coord in point],
            fill=self._color,
            width=2,
            joinstyle="round",
        )


class NeonCard(ctk.CTkFrame):
    """上端に短いネオンの光条が走る、角の丸いカード。

    光条は ``place()`` で置いているので、カードの中身を ``grid()`` で
    組んでも行番号がずれない（既存のレイアウトへそのまま差し替えられる）。
    """

    def __init__(
        self,
        master: Any,
        accent: str = CYAN,
        accent_b: str | None = None,
        rail_width: float = 0.34,
        fg_color: str = PANEL,
        border_color: str = PANEL_EDGE,
        corner_radius: int = 14,
        **kwargs: Any,
    ) -> None:
        """NeonCard を初期化する。

        Args:
            master: 親ウィジェット。
            accent: 光条の左側の色。
            accent_b: 光条の右側の色。None なら ``accent`` と同色。
            rail_width: 光条の幅（カード幅に対する比率）。0 で非表示。
            fg_color: カードの地色。
            border_color: カードの輪郭線の色。
            corner_radius: 角の丸み。
            **kwargs: ``CTkFrame`` に渡す追加引数。
        """
        super().__init__(
            master,
            corner_radius=corner_radius,
            fg_color=fg_color,
            border_width=1,
            border_color=border_color,
            **kwargs,
        )
        self._rail: AccentRule | None = None
        if rail_width > 0:
            self._rail = AccentRule(
                self,
                colors=(accent, accent_b or accent),
                bg=fg_color,
                thickness=3,
            )
            # 角の丸みに掛からないよう中央へ短く置く
            self._rail.place(relx=0.5, y=0, anchor="n", relwidth=rail_width)


def style_ghost_button(button: ctk.CTkButton, accent: str = CYAN) -> None:
    """枠線だけの「ゴーストボタン」へ見た目を揃える。

    Args:
        button: 対象のボタン。
        accent: 枠線・文字・ホバーに使う色。
    """
    button.configure(
        fg_color="transparent",
        border_width=1,
        border_color=over(accent, PANEL, 0.55),
        text_color=blend(accent, TEXT, 0.35),
        hover_color=over(accent, PANEL, 0.18),
        corner_radius=10,
    )


def style_solid_button(
    button: ctk.CTkButton, accent: str, hover: str, text_color: str = "#03080f"
) -> None:
    """塗りつぶしの主要ボタンへ見た目を揃える。

    Args:
        button: 対象のボタン。
        accent: 地色。
        hover: ホバー時の地色。
        text_color: 文字色。地色が明るいので既定は暗い色。
    """
    button.configure(
        fg_color=accent,
        hover_color=hover,
        text_color=text_color,
        corner_radius=10,
        border_width=0,
    )
