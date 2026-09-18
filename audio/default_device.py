"""Windows の既定オーディオ出力デバイスを読み書きするモジュール（Windows専用）。

**用途はただ一つ**: :mod:`gui.live_monitor_window` で、利用者が手動で
「既定の出力を CABLE Input にする」（VB-CABLE のセットアップ手順②）を
行った後、キー変更を終えたら**自動でスピーカー等へ戻す**ため。

``sounddevice``（PortAudio）には「OS の既定デバイスを変更する」機能が
無い。PortAudio は既定デバイスを**読む**だけで、書き込みは OS 固有の
API が必要になる。Windows では ``IPolicyConfig`` という**非公開
（undocumented）COM インターフェース**を使うのが定番の方法で、
Windows の「サウンド」設定パネル自体もこれと同じ仕組みで既定デバイスを
切り替えている。公式ドキュメントには載っていないが、Windows 7 の頃から
広く使われており、多くの音声切り替えツール（NirCmd 等）も同じ手法を
採っている。

失敗しても実害が無いよう、**すべての関数は例外を投げず、失敗時は
None / False を返す**。この機能はあくまで利便性のためのもので、これが
使えなくてもキー変更そのものは支障なく動く。
"""

from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import (
    POINTER,
    Structure,
    c_float,
    c_int,
    c_ulong,
    c_ulonglong,
    c_ushort,
    c_void_p,
    c_wchar_p,
)
from typing import Any

logger = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform.startswith("win")

_ERENDER = 0
_ECONSOLE = 0
_EMULTIMEDIA = 1
_ECOMMUNICATIONS = 2
_DEVICE_STATE_ACTIVE = 0x1
_STGM_READ = 0
_CLSCTX_ALL = 23


def is_available() -> bool:
    """この機能が使える環境かを返す。

    Windows かつ ``comtypes`` が入っている場合のみ True。それ以外の
    環境（Mac/Linux、``comtypes`` 未導入）では常に False を返し、
    呼び出し側は静かに機能をスキップできる。

    Returns:
        使えれば True。
    """
    if not _IS_WINDOWS:
        return False
    try:
        import comtypes  # noqa: F401, PLC0415
        import comtypes.client  # noqa: F401, PLC0415
    except Exception:  # noqa: BLE001 - 未導入・壊れている場合を区別しない
        logger.warning("comtypes を読み込めないため既定デバイスの自動切り替えを使いません", exc_info=True)
        return False
    return True


def _build_interfaces() -> Any:
    """COM インターフェースの定義一式を組み立てる。

    毎回定義し直すのは無駄に見えるが、この機能はごくまれにしか
    呼ばれない（キー変更の開始・終了時だけ）ので、モジュール読み込み時
    の固定コストにしない方を優先した（``comtypes`` を持たない環境でも
    import だけは安全に通したいため）。

    Returns:
        ``(enumerator_cls, policy_cls, propstore_cls, propertykey_cls,
        propvariant_cls)`` のタプル。
    """
    from comtypes import COMMETHOD, GUID, HRESULT, IUnknown  # noqa: PLC0415

    class _PROPERTYKEY(Structure):
        _fields_ = [("fmtid", GUID), ("pid", c_ulong)]

    class _PROPVARIANT(Structure):
        _fields_ = [
            ("vt", c_ushort),
            ("wReserved1", c_ushort),
            ("wReserved2", c_ushort),
            ("wReserved3", c_ushort),
            ("data", c_ulonglong),
        ]

    class _IPropertyStore(IUnknown):
        _iid_ = GUID("{886d8eeb-8cf2-4446-8d02-cdba1dbdcf99}")
        _methods_ = [
            COMMETHOD([], HRESULT, "GetCount", (["out"], POINTER(c_ulong), "cProps")),
            COMMETHOD(
                [], HRESULT, "GetAt",
                (["in"], c_ulong, "iProp"),
                (["out"], POINTER(_PROPERTYKEY), "pkey"),
            ),
            COMMETHOD(
                [], HRESULT, "GetValue",
                (["in"], POINTER(_PROPERTYKEY), "key"),
                (["out"], POINTER(_PROPVARIANT), "pv"),
            ),
        ]

    class _IMMDevice(IUnknown):
        _iid_ = GUID("{D666063F-1587-4E43-81F1-B948E807363F}")
        _methods_ = [
            COMMETHOD(
                [], HRESULT, "Activate",
                (["in"], POINTER(GUID), "iid"),
                (["in"], c_ulong, "dwClsCtx"),
                (["in"], c_void_p, "pActivationParams"),
                (["out"], POINTER(c_void_p), "ppInterface"),
            ),
            COMMETHOD(
                [], HRESULT, "OpenPropertyStore",
                (["in"], c_ulong, "stgmAccess"),
                (["out"], POINTER(POINTER(_IPropertyStore)), "ppProperties"),
            ),
            COMMETHOD([], HRESULT, "GetId", (["out"], POINTER(c_wchar_p), "ppstrId")),
            COMMETHOD([], HRESULT, "GetState", (["out"], POINTER(c_ulong), "pdwState")),
        ]

    class _IMMDeviceCollection(IUnknown):
        _iid_ = GUID("{0BD7A1BE-7A1A-44DB-8397-CC5392387B5E}")
        _methods_ = [
            COMMETHOD([], HRESULT, "GetCount", (["out"], POINTER(c_ulong), "pcDevices")),
            COMMETHOD(
                [], HRESULT, "Item",
                (["in"], c_ulong, "nDevice"),
                (["out"], POINTER(POINTER(_IMMDevice)), "ppDevice"),
            ),
        ]

    class _IMMDeviceEnumerator(IUnknown):
        _iid_ = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
        _methods_ = [
            COMMETHOD(
                [], HRESULT, "EnumAudioEndpoints",
                (["in"], c_int, "dataFlow"),
                (["in"], c_ulong, "dwStateMask"),
                (["out"], POINTER(POINTER(_IMMDeviceCollection)), "ppDevices"),
            ),
            COMMETHOD(
                [], HRESULT, "GetDefaultAudioEndpoint",
                (["in"], c_int, "dataFlow"),
                (["in"], c_int, "role"),
                (["out"], POINTER(POINTER(_IMMDevice)), "ppEndpoint"),
            ),
        ]

    class _IPolicyConfig(IUnknown):
        # SetDefaultEndpoint 以外のメソッドは使わないが、vtable の順番を
        # 合わせるために宣言だけしておく必要がある（COM は vtable の
        # 位置でメソッドを呼ぶため、間を飛ばすと別のメソッドを叩いて
        # しまう）。
        _iid_ = GUID("{f8679f50-850a-41cf-9c72-430f290290c8}")
        _methods_ = [
            COMMETHOD([], HRESULT, "GetMixFormat"),
            COMMETHOD([], HRESULT, "GetDeviceFormat"),
            COMMETHOD([], HRESULT, "ResetDeviceFormat"),
            COMMETHOD([], HRESULT, "SetDeviceFormat"),
            COMMETHOD([], HRESULT, "GetProcessingPeriod"),
            COMMETHOD([], HRESULT, "SetProcessingPeriod"),
            COMMETHOD([], HRESULT, "GetShareMode"),
            COMMETHOD([], HRESULT, "SetShareMode"),
            COMMETHOD([], HRESULT, "GetPropertyValue"),
            COMMETHOD([], HRESULT, "SetPropertyValue"),
            COMMETHOD(
                [], HRESULT, "SetDefaultEndpoint",
                (["in"], c_wchar_p, "wszDeviceId"),
                (["in"], c_int, "role"),
            ),
            COMMETHOD([], HRESULT, "SetEndpointVisibility"),
        ]

    class _IAudioEndpointVolume(IUnknown):
        """デバイス（エンドポイント）そのものの音量を読み書きするための
        インターフェース。Windows の音量ミキサーで「そのデバイスに対して」
        表示される音量と同じもの（アプリ単位のセッション音量とは別）。

        呼ばない先頭のメソッドも、vtable の位置合わせのため引数なしで
        宣言だけしてある（:class:`_IPolicyConfig` と同じ理由）。
        """

        _iid_ = GUID("{5CDF2C82-841E-4546-9722-0CF74078229A}")
        _methods_ = [
            COMMETHOD([], HRESULT, "RegisterControlChangeNotify"),
            COMMETHOD([], HRESULT, "UnregisterControlChangeNotify"),
            COMMETHOD([], HRESULT, "GetChannelCount"),
            COMMETHOD([], HRESULT, "SetMasterVolumeLevel"),
            COMMETHOD(
                [], HRESULT, "SetMasterVolumeLevelScalar",
                (["in"], c_float, "fLevel"),
                (["in"], POINTER(GUID), "pguidEventContext"),
            ),
            COMMETHOD([], HRESULT, "GetMasterVolumeLevel"),
            COMMETHOD(
                [], HRESULT, "GetMasterVolumeLevelScalar",
                (["out"], POINTER(c_float), "pfLevel"),
            ),
        ]

    return (
        _IMMDeviceEnumerator, _IPolicyConfig, _IPropertyStore,
        _PROPERTYKEY, _PROPVARIANT, _IAudioEndpointVolume,
    )


def _friendly_name(device: Any, propstore_cls: Any, key_cls: Any) -> str | None:
    """IMMDevice の表示名（コントロールパネルに出るのと同じ名前）を返す。

    Args:
        device: 対象の ``IMMDevice``。
        propstore_cls: :class:`_IPropertyStore` 相当のクラス。
        key_cls: :class:`_PROPERTYKEY` 相当のクラス。

    Returns:
        表示名。取得できなければ None。
    """
    from comtypes import GUID  # noqa: PLC0415

    try:
        store = device.OpenPropertyStore(_STGM_READ)
        key = key_cls()
        key.fmtid = GUID("{A45C254E-DF1C-4EFD-8020-67D146A850E0}")  # PKEY_Device_FriendlyName
        key.pid = 14
        value = store.GetValue(key)
        if value.vt != 31:  # VT_LPWSTR 以外は想定していない
            return None
        name = ctypes.cast(value.data, c_wchar_p).value
        ctypes.OleDLL("ole32").PropVariantClear(ctypes.byref(value))
        return name
    except Exception:  # noqa: BLE001 - 名前が取れないだけなら諦める
        logger.debug("デバイス名の取得に失敗", exc_info=True)
        return None


def get_default_output_name(role: int = _ECONSOLE) -> str | None:
    """今の既定の出力デバイス名を返す（``sounddevice`` の名前と同じ表記）。

    Args:
        role: 見る役割（既定は eConsole）。Windows は既定出力デバイスを
            eConsole/eMultimedia/eCommunications の3役割それぞれ独立に
            持っており、食い違うことがある
            （:func:`is_any_role_still` 参照）。

    Returns:
        デバイス名。取得できなければ None。
    """
    if not is_available():
        return None

    import comtypes  # noqa: PLC0415
    import comtypes.client  # noqa: PLC0415

    try:
        comtypes.CoInitialize()
    except OSError:
        pass  # 既に初期化済みのスレッドで呼ばれた場合はこれで問題ない

    try:
        enumerator_cls, _, propstore_cls, key_cls, _, _ = _build_interfaces()
        enumerator = comtypes.client.CreateObject(
            comtypes.GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}"),
            interface=enumerator_cls,
        )
        device = enumerator.GetDefaultAudioEndpoint(_ERENDER, role)
        return _friendly_name(device, propstore_cls, key_cls)
    except Exception:  # noqa: BLE001 - 取得できなくても致命的ではない
        logger.warning("既定の出力デバイス名を取得できません (role=%s)", role, exc_info=True)
        return None


def is_any_role_still(name_fragment: str) -> bool:
    """既定出力の3つの役割のうち、どれか1つでも ``name_fragment`` を
    含むデバイス名になっているかを返す。

    :func:`get_default_output_name` は eConsole 役割しか見ないため、
    それだけで「もう元に戻っている」と判定すると、**eConsole だけ
    正しく戻り、eMultimedia/eCommunications が古いデバイス（CABLE
    Input 等）に取り残されているケースを見逃す**（実機で確認済み。
    このケースだと一部のアプリだけ音が出ない）。復元が必要かどうかの
    判定には、3役割まとめて確認するこちらを使うこと。

    Args:
        name_fragment: 判定したい文字列（例: ``"CABLE"``）。

    Returns:
        いずれかの役割の既定デバイス名に ``name_fragment`` が含まれていれば
        True。何も取得できなければ False（判定できないだけで、実害を
        避けるため「戻っている」扱いにはしない＝呼び出し側は必要なら
        復元処理を試みてよい）。
    """
    for role in (_ECONSOLE, _EMULTIMEDIA, _ECOMMUNICATIONS):
        name = get_default_output_name(role)
        if name and name_fragment in name:
            return True
    return False


def set_default_output_by_name(name: str) -> bool:
    """指定した名前の出力デバイスを既定にする。

    メイン・マルチメディア・通信の 3 つの役割すべてを同じデバイスへ
    向ける（コントロールパネルの「既定値に設定」と同じ挙動にするため。
    片方だけ変えると、アプリによって既定デバイスの認識が食い違う）。

    Args:
        name: :func:`get_default_output_name` や ``sounddevice`` が返す
            のと同じ表記のデバイス名。前方一致で探す（切り詰められた
            表示名からの呼び出しにも対応するため）。

    Returns:
        切り替えられたら True。デバイスが見つからない・失敗した場合は False。
    """
    if not is_available() or not name:
        return False

    import comtypes  # noqa: PLC0415
    import comtypes.client  # noqa: PLC0415

    try:
        comtypes.CoInitialize()
    except OSError:
        pass

    try:
        enumerator_cls, policy_cls, propstore_cls, key_cls, _, _ = _build_interfaces()
        enumerator = comtypes.client.CreateObject(
            comtypes.GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}"),
            interface=enumerator_cls,
        )
        policy = comtypes.client.CreateObject(
            comtypes.GUID("{870af99c-171d-4f9e-af0d-e63df40c2bc9}"),
            interface=policy_cls,
        )

        collection = enumerator.EnumAudioEndpoints(_ERENDER, _DEVICE_STATE_ACTIVE)
        count = collection.GetCount()
        target_id = None
        for i in range(count):
            device = collection.Item(i)
            device_name = _friendly_name(device, propstore_cls, key_cls)
            if device_name and (device_name == name or device_name.startswith(name)):
                target_id = device.GetId()
                break

        if target_id is None:
            logger.warning("既定にしたい出力デバイスが見つかりません: %s", name)
            return False

        # **3つの役割それぞれを独立した try/except で切り替える。**
        # 以前はまとめて1つの try の中で呼んでおり、途中の役割
        # （例: eConsole）で例外が起きると、まだ手を付けていない残りの
        # 役割（eMultimedia/eCommunications）が古いデバイス（CABLE
        # Input）を向いたまま取り残されていた。:func:`get_default_output_name`
        # は eConsole しか見ないため、eConsole だけ元に戻っていれば
        # 「正常に戻った」と誤判定してしまい、実際には他の役割向けの
        # アプリ（一部の再生・通信アプリ）だけ音が出ない、という
        # 「たまに音が出ない・アプリを開き直すと直る」不具合の原因に
        # なっていた（開き直すと CABLE Input への切り替え → 復元が
        # もう一度フルで実行され、たまたま今度は全役割成功するため
        # 直って見えていた）。
        succeeded_any = False
        for role in (_ECONSOLE, _EMULTIMEDIA, _ECOMMUNICATIONS):
            try:
                policy.SetDefaultEndpoint(target_id, role)
                succeeded_any = True
            except Exception:  # noqa: BLE001 - 1つの役割の失敗で残りを諦めない
                logger.warning("既定デバイスの切り替えに失敗 (role=%s, %s)", role, name, exc_info=True)
                continue
        return succeeded_any
    except Exception:  # noqa: BLE001 - 切り替えに失敗しても致命的ではない
        logger.warning("既定デバイスを %s へ切り替えられません", name, exc_info=True)
        return False


def _find_device_by_name(
    enumerator: Any, name: str, propstore_cls: Any, key_cls: Any
) -> Any | None:
    """出力デバイスを名前（前方一致）で探す。

    Args:
        enumerator: ``IMMDeviceEnumerator``。
        name: 探すデバイス名。
        propstore_cls: :class:`_IPropertyStore` 相当のクラス。
        key_cls: :class:`_PROPERTYKEY` 相当のクラス。

    Returns:
        見つかった ``IMMDevice``。無ければ None。
    """
    collection = enumerator.EnumAudioEndpoints(_ERENDER, _DEVICE_STATE_ACTIVE)
    for i in range(collection.GetCount()):
        device = collection.Item(i)
        device_name = _friendly_name(device, propstore_cls, key_cls)
        if device_name and (device_name == name or device_name.startswith(name)):
            return device
    return None


def _activate_endpoint_volume(device: Any, endpoint_cls: Any) -> Any | None:
    """``IMMDevice`` を ``IAudioEndpointVolume`` として開く。

    ``IMMDevice.Activate`` は汎用の ``void**`` でしか返せないため、
    戻ってきた生ポインタを目的のインターフェース型へ手動でキャストする。

    Args:
        device: 対象の ``IMMDevice``。
        endpoint_cls: :class:`_IAudioEndpointVolume` 相当のクラス。

    Returns:
        開けた ``IAudioEndpointVolume``。失敗すれば None。
    """
    raw = device.Activate(endpoint_cls._iid_, _CLSCTX_ALL, None)
    if not raw:
        return None
    return ctypes.cast(ctypes.c_void_p(raw), POINTER(endpoint_cls))


def get_output_volume(name: str) -> float | None:
    """指定した出力デバイスの、Windows側エンドポイント音量を読む。

    アプリ単位の音量（音量ミキサーの各アプリのスライダー）ではなく、
    そのデバイス自体の音量（音量ミキサーの一番下・デバイス名の欄や、
    タスクバーのスピーカーアイコンで既定デバイスとして触るときの音量）
    を指す。

    Args:
        name: :func:`get_default_output_name` と同じ表記のデバイス名。

    Returns:
        0.0〜1.0のスカラー音量。取得できなければ None
        （Windows以外、``comtypes`` 未導入、デバイスが見つからない等）。
    """
    if not is_available() or not name:
        return None

    import comtypes  # noqa: PLC0415
    import comtypes.client  # noqa: PLC0415

    try:
        comtypes.CoInitialize()
    except OSError:
        pass

    try:
        enumerator_cls, _, propstore_cls, key_cls, _, endpoint_cls = (
            _build_interfaces()
        )
        enumerator = comtypes.client.CreateObject(
            comtypes.GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}"),
            interface=enumerator_cls,
        )
        device = _find_device_by_name(enumerator, name, propstore_cls, key_cls)
        if device is None:
            return None
        endpoint = _activate_endpoint_volume(device, endpoint_cls)
        if endpoint is None:
            return None
        # comtypes の規約により、out引数が1個だけのメソッドは引数なしで
        # 呼ぶと戻り値としてその値が返る（byrefで自分で受け皿を渡すのは
        # 誤り＝実機で "call takes exactly 1 arguments (2 given)" になる）。
        return float(endpoint.GetMasterVolumeLevelScalar())
    except Exception:  # noqa: BLE001 - 取得できなくても致命的ではない
        logger.debug("出力音量の取得に失敗: %s", name, exc_info=True)
        return None


def set_output_volume(name: str, level: float) -> bool:
    """指定した出力デバイスの、Windows側エンドポイント音量を設定する。

    :func:`get_output_volume` と対になる書き込み版。CABLE Input の
    ような仮想デバイスは音量変更を受け付けないことがあるため、
    :class:`gui.live_monitor_window.LiveMonitorWindow` では**実際に
    音が出る出力デバイス（スピーカー等）側**をこれで操作する
    （アプリ内蔵の音量調整とは別に、Windows側の音量そのものも操作
    できるようにするため）。

    Args:
        name: :func:`get_default_output_name` と同じ表記のデバイス名。
        level: 設定する音量（0.0〜1.0）。範囲外は丸める。

    Returns:
        設定できたら True。失敗したら False。
    """
    if not is_available() or not name:
        return False

    level = max(0.0, min(1.0, float(level)))

    import comtypes  # noqa: PLC0415
    import comtypes.client  # noqa: PLC0415

    try:
        comtypes.CoInitialize()
    except OSError:
        pass

    try:
        enumerator_cls, _, propstore_cls, key_cls, _, endpoint_cls = (
            _build_interfaces()
        )
        enumerator = comtypes.client.CreateObject(
            comtypes.GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}"),
            interface=enumerator_cls,
        )
        device = _find_device_by_name(enumerator, name, propstore_cls, key_cls)
        if device is None:
            return False
        endpoint = _activate_endpoint_volume(device, endpoint_cls)
        if endpoint is None:
            return False
        endpoint.SetMasterVolumeLevelScalar(level, None)
        return True
    except Exception:  # noqa: BLE001 - 失敗しても致命的ではない
        logger.debug("出力音量の設定に失敗: %s", name, exc_info=True)
        return False
