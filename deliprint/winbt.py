"""Windows 平台经典蓝牙（SPP / RFCOMM）支持，只用 ctypes + 标准库。

Windows 的 Python 构建通常没有 socket.AF_BTH（官方安装包、Espressif 等第三方
构建都可能缺），所以这里直接调系统 API，不需要 PyBluez / pywin32：

* 设备发现：bthprops.cpl 的 BluetoothFindFirstDevice 系列，可枚举已配对
  （remembered/authenticated）设备，也能发起一次 inquiry 找在范围内但没配对的设备；
  返回的 ulClassofDevice 就是 BlueZ 的 Class（Imaging 类 = 0x600），
  所以 deliprint.discovery 的匹配规则可以原样复用。
* 数据通道：ws2_32.dll 的 socket(AF_BTH, SOCK_STREAM, BTHPROTO_RFCOMM)，
  连接成功后把原生 SOCKET 用 socket.socket(..., fileno=...) 包装成普通 socket
  对象，读写/select/超时语义与 Linux 版 BtSppTransport 完全一致。

这个模块在非 Windows 平台也能 import（模块级只定义结构体，不加载 DLL），
任何实际功能调用都会抛 WinBtError，方便单元测试和跨平台代码引用。
"""

from __future__ import annotations

import ctypes
import math
import re
import socket
import sys
import threading
from dataclasses import dataclass, replace
from typing import Dict, List

_IS_WINDOWS = sys.platform.startswith("win")

#: Windows 的 AF_BTH / BTHPROTO_RFCOMM（老构建的 socket 模块里没有这俩常量）
AF_BTH: int = int(getattr(socket, "AF_BTH", 32))
BTHPROTO_RFCOMM: int = int(getattr(socket, "BTHPROTO_RFCOMM", 3))

INVALID_SOCKET = ctypes.c_size_t(-1).value
SOCKET_ERROR = -1

FIONBIO = 0x8004667E       # ioctlsocket：阻塞开关
SOL_SOCKET = 0xFFFF
SO_ERROR = 0x1007

#: Winsock 里 connect 正在进行时返回的错误（此时要 select 等可写）
_CONNECT_PENDING = (10035, 10036, 10037)  # WSAEWOULDBLOCK / WSAEINPROGRESS / WSAEALREADY

ERROR_NO_MORE_ITEMS = 259


class WinBtError(RuntimeError):
    """Windows 蓝牙 API 不可用 / 调用失败（非 socket 层错误）。"""


# --------------------------------------------------------------------- 结构体
class SYSTEMTIME(ctypes.Structure):
    _fields_ = [
        ("wYear", ctypes.c_uint16),
        ("wMonth", ctypes.c_uint16),
        ("wDayOfWeek", ctypes.c_uint16),
        ("wDay", ctypes.c_uint16),
        ("wHour", ctypes.c_uint16),
        ("wMinute", ctypes.c_uint16),
        ("wSecond", ctypes.c_uint16),
        ("wMilliseconds", ctypes.c_uint16),
    ]


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _BLUETOOTH_ADDRESS_VALUE(ctypes.Union):
    _fields_ = [("ullLong", ctypes.c_uint64), ("rgBytes", ctypes.c_ubyte * 6)]


class BLUETOOTH_ADDRESS(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("u", _BLUETOOTH_ADDRESS_VALUE)]


class BLUETOOTH_FIND_RADIO_PARAMS(ctypes.Structure):
    _fields_ = [("dwSize", ctypes.c_uint32)]


class BLUETOOTH_RADIO_INFO(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("address", BLUETOOTH_ADDRESS),
        ("szName", ctypes.c_wchar * 248),
        ("ulClassofDevice", ctypes.c_uint32),
        ("lmpSubversion", ctypes.c_uint16),
        ("manufacturer", ctypes.c_uint16),
    ]


class BLUETOOTH_DEVICE_INFO(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("Address", BLUETOOTH_ADDRESS),
        ("ulClassofDevice", ctypes.c_uint32),
        ("fConnected", ctypes.c_int),
        ("fRemembered", ctypes.c_int),
        ("fAuthenticated", ctypes.c_int),
        ("stLastSeen", SYSTEMTIME),
        ("stLastUsed", SYSTEMTIME),
        ("szName", ctypes.c_wchar * 248),
    ]


class BLUETOOTH_DEVICE_SEARCH_PARAMS(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("fReturnAuthenticated", ctypes.c_int),
        ("fReturnRemembered", ctypes.c_int),
        ("fReturnUnknown", ctypes.c_int),
        ("fReturnConnected", ctypes.c_int),
        ("fIssueInquiry", ctypes.c_int),
        ("cTimeoutMultiplier", ctypes.c_ubyte),
        ("hRadio", ctypes.c_void_p),
    ]


class SOCKADDR_BTH(ctypes.Structure):
    # 必须 packed：MSVC 下这个结构是 30 字节；默认对齐会多出 2 字节填充，
    # connect() 就会以 WSAEADDRNOTAVAIL(10049) 失败（真机踩过）。
    _pack_ = 1
    _fields_ = [
        ("addressFamily", ctypes.c_uint16),
        ("btAddr", ctypes.c_uint64),
        ("serviceClassId", GUID),
        ("port", ctypes.c_uint32),
    ]


class WSADATA(ctypes.Structure):
    _fields_ = [
        ("wVersion", ctypes.c_uint16),
        ("wHighVersion", ctypes.c_uint16),
        ("szDescription", ctypes.c_char * 257),
        ("szSystemStatus", ctypes.c_char * 129),
        ("iMaxSockets", ctypes.c_uint16),
        ("iMaxUdpDg", ctypes.c_uint16),
        ("lpVendorInfo", ctypes.c_void_p),
    ]


class TIMEVAL(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_long), ("tv_usec", ctypes.c_long)]


class FD_SET(ctypes.Structure):
    _fields_ = [("fd_count", ctypes.c_uint), ("fd_array", ctypes.c_size_t * 64)]


# --------------------------------------------------------------------- 数据类
@dataclass(frozen=True)
class WinDevice:
    """Windows 枚举到的一台经典蓝牙设备。"""

    mac: str
    name: str = ""
    device_class: int = 0
    connected: bool = False
    remembered: bool = False
    authenticated: bool = False

    @property
    def paired(self) -> bool:
        return bool(self.remembered or self.authenticated)

    def __str__(self) -> str:  # 便于日志
        tags = []
        if self.paired:
            tags.append("已配对")
        if self.connected:
            tags.append("已连接")
        if self.device_class & 0x1F00 == 0x600:
            tags.append("Imaging")
        return f"{self.mac} {self.name}".strip() + (f" [{','.join(tags)}]" if tags else "")


@dataclass(frozen=True)
class WinRadio:
    """本机蓝牙适配器。"""

    mac: str = ""
    name: str = ""
    device_class: int = 0
    lmp_subversion: int = 0
    manufacturer: int = 0

    def __str__(self) -> str:
        return f"{self.name or '蓝牙适配器'} {self.mac}".strip()


# --------------------------------------------------------------------- 地址工具
def normalize_mac(mac: str) -> str:
    """把 aabbccddeeff / AA-BB-.. / AA:BB:.. 统一成 AA:BB:CC:DD:EE:FF。"""
    raw = re.sub(r"[^0-9A-Fa-f]", "", str(mac or ""))
    if len(raw) != 12:
        raise ValueError(f"蓝牙地址格式应为 XX:XX:XX:XX:XX:XX，收到 {mac!r}")
    return ":".join(raw[i:i + 2] for i in range(0, 12, 2)).upper()


def mac_to_ull(mac: str) -> int:
    """MAC → BTH_ADDR（例 C8:48:8A:53:26:7C → 0xC8488A53267C）。"""
    return int(normalize_mac(mac).replace(":", ""), 16)


def ull_to_mac(value: int) -> str:
    """BTH_ADDR → MAC 字符串（高位在前，保留前导 0）。"""
    number = int(value) & 0xFFFFFFFFFFFF
    return ":".join(f"{(number >> shift) & 0xFF:02X}" for shift in range(40, -8, -8))


def inquiry_multiplier(duration: float) -> int:
    """秒 → BluetoothFindFirstDevice 的 1.28s 单位（0 表示不发起 inquiry）。"""
    seconds = float(duration or 0.0)
    if seconds <= 0:
        return 0
    return max(1, min(48, int(math.ceil(seconds / 1.28))))


# --------------------------------------------------------------------- DLL 加载
_bth_lock = threading.Lock()
_bth_dll = None
_ws2_lock = threading.Lock()
_ws2_dll = None
_k32_dll = None


def _require_windows() -> None:
    if not _IS_WINDOWS:
        raise WinBtError(f"Windows 专用功能，当前平台是 {sys.platform!r}")


def _load(name: str):
    loader = getattr(ctypes, "WinDLL", None)
    if loader is None:  # pragma: no cover - 非 Windows
        raise WinBtError(f"当前平台无法加载 {name}")
    try:
        return loader(name, use_last_error=True)
    except OSError as exc:
        raise WinBtError(f"加载 {name} 失败：{exc}") from exc


def _bthprops():
    """bthprops.cpl（蓝牙属性页 DLL，Windows 自带）。"""
    global _bth_dll
    if _bth_dll is not None:
        return _bth_dll
    with _bth_lock:
        if _bth_dll is not None:
            return _bth_dll
        _require_windows()
        bth = _load("bthprops.cpl")
        handle_p = ctypes.POINTER(ctypes.c_void_p)
        bth.BluetoothFindFirstRadio.argtypes = [
            ctypes.POINTER(BLUETOOTH_FIND_RADIO_PARAMS), handle_p
        ]
        bth.BluetoothFindFirstRadio.restype = ctypes.c_void_p
        bth.BluetoothFindNextRadio.argtypes = [ctypes.c_void_p, handle_p]
        bth.BluetoothFindNextRadio.restype = ctypes.c_int
        bth.BluetoothFindRadioClose.argtypes = [ctypes.c_void_p]
        bth.BluetoothFindRadioClose.restype = ctypes.c_int
        bth.BluetoothGetRadioInfo.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(BLUETOOTH_RADIO_INFO)
        ]
        bth.BluetoothGetRadioInfo.restype = ctypes.c_int
        bth.BluetoothFindFirstDevice.argtypes = [
            ctypes.POINTER(BLUETOOTH_DEVICE_SEARCH_PARAMS),
            ctypes.POINTER(BLUETOOTH_DEVICE_INFO),
        ]
        bth.BluetoothFindFirstDevice.restype = ctypes.c_void_p
        bth.BluetoothFindNextDevice.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(BLUETOOTH_DEVICE_INFO)
        ]
        bth.BluetoothFindNextDevice.restype = ctypes.c_int
        bth.BluetoothFindDeviceClose.argtypes = [ctypes.c_void_p]
        bth.BluetoothFindDeviceClose.restype = ctypes.c_int
        _bth_dll = bth
    return bth


def _kernel32():
    global _k32_dll
    if _k32_dll is not None:
        return _k32_dll
    k32 = _load("kernel32")
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    k32.CloseHandle.restype = ctypes.c_int
    _k32_dll = k32
    return k32


def _winsock():
    global _ws2_dll
    if _ws2_dll is not None:
        return _ws2_dll
    with _ws2_lock:
        if _ws2_dll is not None:
            return _ws2_dll
        _require_windows()
        ws2 = _load("ws2_32")
        ws2.socket.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int]
        ws2.socket.restype = ctypes.c_size_t
        ws2.connect.argtypes = [ctypes.c_size_t, ctypes.c_void_p, ctypes.c_int]
        ws2.connect.restype = ctypes.c_int
        ws2.closesocket.argtypes = [ctypes.c_size_t]
        ws2.closesocket.restype = ctypes.c_int
        ws2.ioctlsocket.argtypes = [
            ctypes.c_size_t, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)
        ]
        ws2.ioctlsocket.restype = ctypes.c_int
        ws2.select.argtypes = [
            ctypes.c_int,
            ctypes.POINTER(FD_SET),
            ctypes.POINTER(FD_SET),
            ctypes.POINTER(FD_SET),
            ctypes.POINTER(TIMEVAL),
        ]
        ws2.select.restype = ctypes.c_int
        ws2.getsockopt.argtypes = [
            ctypes.c_size_t, ctypes.c_int, ctypes.c_int,
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_int),
        ]
        ws2.getsockopt.restype = ctypes.c_int
        ws2.WSAStartup.argtypes = [ctypes.c_uint16, ctypes.POINTER(WSADATA)]
        ws2.WSAStartup.restype = ctypes.c_int
        data = WSADATA()
        if ws2.WSAStartup(0x0202, ctypes.byref(data)) != 0:
            raise WinBtError("Winsock 初始化失败（WSAStartup 返回非 0）")
        _ws2_dll = ws2
    return ws2


# --------------------------------------------------------------------- 适配器
def _read_radio_info(bth, handle) -> WinRadio:
    info = BLUETOOTH_RADIO_INFO()
    info.dwSize = ctypes.sizeof(BLUETOOTH_RADIO_INFO)
    if not bth.BluetoothGetRadioInfo(handle, ctypes.byref(info)):
        return WinRadio()
    return WinRadio(
        mac=ull_to_mac(info.address.ullLong),
        name=info.szName.strip(),
        device_class=int(info.ulClassofDevice),
        lmp_subversion=int(info.lmpSubversion),
        manufacturer=int(info.manufacturer),
    )


def list_radios() -> List[WinRadio]:
    """本机蓝牙适配器列表（空列表 = 没有蓝牙硬件/未开启）。"""
    _require_windows()
    bth = _bthprops()
    params = BLUETOOTH_FIND_RADIO_PARAMS()
    params.dwSize = ctypes.sizeof(BLUETOOTH_FIND_RADIO_PARAMS)
    handle = ctypes.c_void_p()
    find = bth.BluetoothFindFirstRadio(ctypes.byref(params), ctypes.byref(handle))
    if not find:
        err = ctypes.get_last_error()
        if err in (0, ERROR_NO_MORE_ITEMS):
            return []
        raise WinBtError(f"查询蓝牙适配器失败（Win32 错误 {err}）")

    radios: List[WinRadio] = []
    try:
        while True:
            radios.append(_read_radio_info(bth, handle))
            nxt = ctypes.c_void_p()
            if not bth.BluetoothFindNextRadio(find, ctypes.byref(nxt)):
                break
            if handle.value:
                _kernel32().CloseHandle(handle)
            handle = nxt
    finally:
        bth.BluetoothFindRadioClose(find)
        if handle.value:
            _kernel32().CloseHandle(handle)
    return radios


def available() -> bool:
    """本机是否能用经典蓝牙（Windows + 至少一个适配器）。"""
    if not _IS_WINDOWS:
        return False
    try:
        return bool(list_radios())
    except (WinBtError, OSError):
        return False


# --------------------------------------------------------------------- 设备枚举
def _new_device_info() -> BLUETOOTH_DEVICE_INFO:
    info = BLUETOOTH_DEVICE_INFO()
    info.dwSize = ctypes.sizeof(BLUETOOTH_DEVICE_INFO)
    return info


def _to_device(info: BLUETOOTH_DEVICE_INFO) -> WinDevice:
    return WinDevice(
        mac=ull_to_mac(info.Address.ullLong),
        name=info.szName.strip(),
        device_class=int(info.ulClassofDevice),
        connected=bool(info.fConnected),
        remembered=bool(info.fRemembered),
        authenticated=bool(info.fAuthenticated),
    )


def _merge(a: WinDevice, b: WinDevice) -> WinDevice:
    """同一设备被两轮枚举看到时合并字段（空名字/0 类型不覆盖已有值）。"""
    return replace(
        a,
        name=a.name or b.name,
        device_class=a.device_class or b.device_class,
        connected=a.connected or b.connected,
        remembered=a.remembered or b.remembered,
        authenticated=a.authenticated or b.authenticated,
    )


def _collect(bth, found: Dict[str, WinDevice], *, inquiry: bool, multiplier: int) -> None:
    params = BLUETOOTH_DEVICE_SEARCH_PARAMS()
    params.dwSize = ctypes.sizeof(BLUETOOTH_DEVICE_SEARCH_PARAMS)
    params.fReturnAuthenticated = True
    params.fReturnRemembered = True
    params.fReturnUnknown = True
    params.fReturnConnected = True
    params.fIssueInquiry = bool(inquiry)
    params.cTimeoutMultiplier = multiplier
    params.hRadio = None  # NULL = 搜索所有本地适配器

    info = _new_device_info()
    find = bth.BluetoothFindFirstDevice(ctypes.byref(params), ctypes.byref(info))
    if not find:
        err = ctypes.get_last_error()
        if err in (0, ERROR_NO_MORE_ITEMS):
            return
        raise WinBtError(f"枚举蓝牙设备失败（Win32 错误 {err}）")
    try:
        while True:
            dev = _to_device(info)
            prev = found.get(dev.mac)
            found[dev.mac] = dev if prev is None else _merge(prev, dev)
            info = _new_device_info()
            if not bth.BluetoothFindNextDevice(find, ctypes.byref(info)):
                break
    finally:
        bth.BluetoothFindDeviceClose(find)


def enumerate_devices(duration: float = 0.0) -> List[WinDevice]:
    """枚举经典蓝牙设备。

    duration > 0 时额外发起一次 inquiry（约 1.28s 的整数倍），否则只读系统
    缓存——已配对设备即使关机也会出现在缓存里，所以快速模式也能找到打印机。
    """
    _require_windows()
    bth = _bthprops()
    found: Dict[str, WinDevice] = {}
    _collect(bth, found, inquiry=False, multiplier=0)
    multiplier = inquiry_multiplier(duration)
    if multiplier:
        _collect(bth, found, inquiry=True, multiplier=multiplier)
    if not found and not list_radios():
        raise WinBtError(
            "没有可用的蓝牙适配器：请在「设置 → 蓝牙和其他设备」里打开蓝牙，"
            "并确认本机有蓝牙硬件"
        )
    return list(found.values())


# --------------------------------------------------------------------- RFCOMM
_ERR_HINTS = {
    10013: "权限不足：请在「设置 → 隐私 → 蓝牙」里允许桌面应用使用蓝牙",
    10049: "无法连接：请先在「设置 → 蓝牙和其他设备」里配对该打印机，"
           "并确认打印机已开机、在范围内",
    10051: "网络不可达：蓝牙适配器可能已关闭",
    10060: "连接超时：打印机可能已关机、不在范围内，或已被其它程序占用",
    10061: "连接被拒绝：打印机可能已被其它程序占用，或配对信息失效（删除配对后重试）",
    10065: "主机不可达：打印机不在范围内",
    10093: "Winsock 未初始化（请重试）",
}


def _socket_error(err: int, action: str) -> OSError:
    err = int(err)
    hint = _ERR_HINTS.get(err)
    text = f"{action}失败（Winsock {err}）"
    if hint:
        text = f"{action}失败：{hint}（Winsock {err}）"
    return OSError(err, text)


def _set_blocking(ws2, raw: int, blocking: bool) -> None:
    mode = ctypes.c_uint32(0 if blocking else 1)
    if ws2.ioctlsocket(ctypes.c_size_t(raw), FIONBIO, ctypes.byref(mode)) == SOCKET_ERROR:
        raise _socket_error(ctypes.get_last_error(), "设置蓝牙 socket 模式")


def _so_error(ws2, raw: int) -> int:
    value = ctypes.c_int(0)
    length = ctypes.c_int(ctypes.sizeof(value))
    rc = ws2.getsockopt(
        ctypes.c_size_t(raw), SOL_SOCKET, SO_ERROR, ctypes.byref(value), ctypes.byref(length)
    )
    if rc == SOCKET_ERROR:
        raise _socket_error(ctypes.get_last_error(), "读取蓝牙 socket 状态")
    return int(value.value)


def _connect(ws2, raw: int, mac: str, channel: int, timeout: float) -> None:
    addr = SOCKADDR_BTH()
    addr.addressFamily = AF_BTH
    addr.btAddr = mac_to_ull(mac)
    addr.port = int(channel)

    _set_blocking(ws2, raw, False)
    if ws2.connect(ctypes.c_size_t(raw), ctypes.byref(addr), ctypes.sizeof(addr)) == 0:
        return
    err = ctypes.get_last_error()
    if err not in _CONNECT_PENDING:
        raise _socket_error(err, f"连接 {mac}（信道 {channel}）")

    # 非阻塞 connect 已发起：select 等可写，再用 SO_ERROR 取最终结果
    wait = max(0.0, float(timeout))
    fdset = FD_SET()
    fdset.fd_count = 1
    fdset.fd_array[0] = raw
    tv = TIMEVAL(int(wait), int(round((wait - int(wait)) * 1_000_000)))
    n = ws2.select(0, None, ctypes.byref(fdset), None, ctypes.byref(tv))
    if n == SOCKET_ERROR:
        raise _socket_error(ctypes.get_last_error(), "等待蓝牙连接")
    if n == 0:
        raise _socket_error(10060, f"连接 {mac}（信道 {channel}）")
    final = _so_error(ws2, raw)
    if final:
        raise _socket_error(final, f"连接 {mac}（信道 {channel}）")


def _wrap_socket(raw: int) -> socket.socket:
    """把原生 SOCKET 包装成标准 socket 对象（读写/select/close 都能用）。"""
    try:
        sock = socket.socket(AF_BTH, socket.SOCK_STREAM, BTHPROTO_RFCOMM, fileno=raw)
    except OSError as exc:  # pragma: no cover - 正常 CPython 都能包装
        raise WinBtError(
            f"无法把蓝牙 SOCKET 包装成 socket 对象：{exc}；"
            f"当前 Python {sys.version.split()[0]} 可能过旧"
        ) from exc
    sock.settimeout(None)
    return sock


def open_rfcomm_socket(
    mac: str, channel: int = 1, timeout: float = 3.0
) -> socket.socket:
    """连接经典蓝牙 SPP（RFCOMM），返回可直接读写的 socket 对象。

    注意：Windows 上未配对的设备通常连不上，请先在系统设置里配对打印机。
    信道默认 1（SPP 标准信道）——Windows 的 RFCOMM 不返回 SDP 解析结果。
    """
    _require_windows()
    target = normalize_mac(mac)
    ws2 = _winsock()
    raw = int(ws2.socket(AF_BTH, socket.SOCK_STREAM, BTHPROTO_RFCOMM))
    if raw in (0, INVALID_SOCKET):
        raise _socket_error(ctypes.get_last_error(), "创建蓝牙 socket")
    try:
        _connect(ws2, raw, target, channel, timeout)
        _set_blocking(ws2, raw, True)
        return _wrap_socket(raw)
    except BaseException:
        ws2.closesocket(ctypes.c_size_t(raw))
        raise


# --------------------------------------------------------------------- 配对提示
def is_paired(mac: str) -> bool:
    """设备是否已在 Windows 里配对（用于给出更准确的报错）。"""
    try:
        target = normalize_mac(mac)
    except ValueError:
        return False
    for dev in enumerate_devices(0.0):
        if dev.mac == target:
            return dev.paired
    return False
