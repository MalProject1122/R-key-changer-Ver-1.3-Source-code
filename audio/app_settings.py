"""アプリの設定（1つだけ）を読み書きする。

このアプリで唯一覚えておく必要があるのは「キー変更画面の手順パネルを
畳んだかどうか」だけ（一度接続に成功したら、次回以降は手順を畳んで
おく。:class:`gui.live_monitor_window.LiveMonitorWindow` 参照）。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import config

_SETTINGS_FILE_NAME = "app_settings.json"


@dataclass
class AppSettings:
    """アプリの設定。"""

    live_monitor_guide_seen: bool = False
    """キー変更画面の手順パネルを一度でも開始（接続成功）まで進めたか。"""

    key_changer_block_size: int = 2048
    """キー変更画面の音声コールバックサイズ[サンプル]（⚙ 詳細設定から
    変更可能）。:data:`audio.live_monitor.BLOCK_SIZE_OPTIONS` のいずれか
    のはずだが、設定ファイルを直接編集された場合に備えて、読み込み側
    （:class:`gui.live_monitor_window.LiveMonitorWindow`）で候補外の値は
    既定へフォールバックする。値の意味は :data:`audio.live_monitor._BLOCK_SIZE`
    のdocstring参照。
    """

    live_space_enabled: bool = False
    """Live Space（会場の音場）を掛けるかどうか。"""

    live_space_preset: str = "Concert Hall"
    """選択中のプリセット名。:data:`audio.live_space.PRESETS` のキー、
    またはスライダーを手で動かした ``Custom``。"""

    live_space_output_mode: str = "headphone"
    """:data:`audio.live_space.OUTPUT_MODES` のどれか。"""

    live_space_binaural: bool = False
    """Headphone モードで反射・残響を HRTF（バイノーラル）描画するか。"""

    last_real_output_name: str = ""
    """最後に確認できた、CABLE 系ではない既定の出力デバイス名。

    起動時に既定の出力を CABLE Input へ切り替えるため、アプリが強制終了
    されると既定が CABLE のまま残る。次回起動時にそれを検出したら、この
    名前へ戻す（:meth:`gui.live_monitor_window.LiveMonitorWindow._resolve_original_output_name`）。
    """

    live_space_params: dict[str, Any] = field(default_factory=dict)
    """Custom のときに復元するパラメータ
    （:func:`audio.live_space.params_to_dict` の形）。プリセット選択中は
    無視される。範囲外・欠損は読み込み側（GUI）で丸める。"""

    eq_enabled: bool = False
    """10バンド EQ（イコライザー）を掛けるかどうか。"""

    eq_preset: str = "Flat"
    """選択中の EQ プリセット名。:data:`audio.equalizer.PRESETS` のキー、
    またはスライダーを手で動かした ``Custom``。"""

    eq_gains: list[float] = field(default_factory=lambda: [0.0] * 10)
    """10バンド分のゲイン[dB]（:data:`audio.equalizer.BAND_FREQS` の順）。
    範囲外・欠損・個数不一致は読み込み側（GUI）で丸める。"""

    sound_profiles: dict[str, dict[str, Any]] = field(default_factory=dict)
    """名前を付けて保存した「EQ + 音響」の組み合わせ（マイプリセット）。

    キー（半音）は含まない。曲によってキーは変わるが、好みの音質設定は
    曲に依らないことが多いため。1エントリは
    :meth:`gui.live_monitor_window.LiveMonitorWindow._snapshot_sound_profile`
    が作る辞書（``eq_enabled``/``eq_preset``/``eq_gains``/
    ``live_space_enabled``/``live_space_preset``/``live_space_output_mode``/
    ``live_space_binaural``/``live_space_params``）。壊れた・欠けた
    エントリは適用側（GUI）が個別に丸める。"""

    def to_dict(self) -> dict[str, Any]:
        """辞書に変換する。

        Returns:
            JSON へ書き出せる辞書。
        """
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AppSettings:
        """辞書から復元する。未知のキーは無視し、欠けたキーは既定値で補う。

        Args:
            data: :meth:`to_dict` が返した辞書。

        Returns:
            復元された設定。
        """
        defaults = cls()
        if not isinstance(data, dict):
            return defaults
        # 手で編集された・壊れた設定ファイルでも起動できるよう、型が
        # 合わない値は例外にせず既定値へ戻す（bool("false") が True に
        # なるような暗黙変換もしない）。
        return cls(
            live_monitor_guide_seen=_as_bool(
                data.get("live_monitor_guide_seen"), defaults.live_monitor_guide_seen
            ),
            key_changer_block_size=_as_int(
                data.get("key_changer_block_size"), defaults.key_changer_block_size
            ),
            live_space_enabled=_as_bool(
                data.get("live_space_enabled"), defaults.live_space_enabled
            ),
            live_space_preset=_as_str(
                data.get("live_space_preset"), defaults.live_space_preset
            ),
            live_space_output_mode=_as_str(
                data.get("live_space_output_mode"), defaults.live_space_output_mode
            ),
            live_space_binaural=_as_bool(
                data.get("live_space_binaural"), defaults.live_space_binaural
            ),
            last_real_output_name=_as_str(
                data.get("last_real_output_name"), defaults.last_real_output_name
            ),
            live_space_params=(
                dict(raw) if isinstance(raw := data.get("live_space_params"), dict) else {}
            ),
            eq_enabled=_as_bool(data.get("eq_enabled"), defaults.eq_enabled),
            eq_preset=_as_str(data.get("eq_preset"), defaults.eq_preset),
            eq_gains=_as_float_list(
                data.get("eq_gains"), defaults.eq_gains
            ),
            sound_profiles=_as_profile_dict(data.get("sound_profiles")),
        )


def _as_bool(value: Any, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _as_int(value: Any, default: int) -> int:
    # bool は int のサブクラスなので明示的に弾く
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return default


def _as_str(value: Any, default: str) -> str:
    return value if isinstance(value, str) else default


def _as_float_list(value: Any, default: list[float]) -> list[float]:
    """要素数10の数値リストとして妥当な場合のみ採用する。

    手編集された設定ファイルで個数が違う・数値でない要素が混ざる場合に
    備え、その場合は既定値（フラット=全0dB）へ丸める。
    """
    if not isinstance(value, list) or len(value) != len(default):
        return list(default)
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value):
        return list(default)
    return [float(v) for v in value]


def _as_profile_dict(value: Any) -> dict[str, dict[str, Any]]:
    """``{名前: 設定辞書}`` の形として妥当な要素だけを残す。

    キー（名前）が文字列でない、または値が辞書でないエントリは無視する。
    各プロフィール内部の細かい妥当性（範囲外の値など）は、適用する側
    （GUI）が :data:`audio.equalizer.PRESETS` 等と同じやり方で丸める。
    """
    if not isinstance(value, dict):
        return {}
    return {
        name: dict(profile)
        for name, profile in value.items()
        if isinstance(name, str) and isinstance(profile, dict)
    }


def _settings_path() -> Path:
    """設定ファイルの保存先パスを返す。

    Returns:
        アプリのフォルダ直下の固定ファイルパス。
    """
    return config.BASE_DIR / _SETTINGS_FILE_NAME


def load_settings() -> AppSettings:
    """保存済みの設定を読み込む。無ければ既定値を返す。

    Returns:
        読み込んだ（または既定の）設定。壊れたファイルでも例外は
        投げず、既定値へフォールバックする。
    """
    path = _settings_path()
    if not path.exists():
        return AppSettings()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # ValueError は JSONDecodeError と UnicodeDecodeError（UTF-8 でない
        # ファイル）の両方を含む
        return AppSettings()
    return AppSettings.from_dict(data)


def save_settings(settings: AppSettings) -> None:
    """設定をファイルへ保存する。

    Args:
        settings: 保存する設定。

    失敗しても例外は投げない（設定保存はあくまで利便性のためのもので、
    ここで落ちてアプリ本体が使えなくなる方が困る）。
    """
    path = _settings_path()
    temp = path.with_name(path.name + ".tmp")
    try:
        # 一時ファイルに書いてから置き換える。書き込み途中でアプリが
        # 落ちても、元の設定ファイルが半端な内容で壊れないようにするため。
        temp.write_text(
            json.dumps(settings.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temp, path)
    except OSError:
        pass
