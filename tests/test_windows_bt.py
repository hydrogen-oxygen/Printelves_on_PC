"""Windows 蓝牙后端的离线测试（不需要蓝牙硬件，也不需要真的运行在 Windows 上）。

覆盖：MAC 与 BTH_ADDR 的互转、inquiry 时长换算、x64 结构体布局、
WindowsScanner 的 Device 映射与异常包装、default_scanner 的平台分派。
"""

from __future__ import annotations

import ctypes
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from deliprint import winbt
from deliprint.discovery import (
    ScanError,
    WindowsScanner,
    default_scanner,
    scan_devices,
)

PRINTER_MAC = "C8:48:8A:53:26:7C"
BLE_TWIN_MAC = "C9:48:8A:53:26:7C"


# ------------------------------------------------------------------ MAC 转换
def test_mac_ull_roundtrip():
    assert winbt.mac_to_ull(PRINTER_MAC) == 0xC8488A53267C
    assert winbt.ull_to_mac(0xC8488A53267C) == PRINTER_MAC
    # 前导零不能丢（Mosidun 那台的本地地址就是 00:00:00:03:0F:02）
    assert winbt.ull_to_mac(0x000000030F02) == "00:00:00:03:0F:02"


@pytest.mark.parametrize(
    "raw",
    ["c8-48-8a-53-26-7c", "c8488a53267c", " C8:48:8A:53:26:7C ", "C8 48 8A 53 26 7C"],
)
def test_normalize_mac_accepts_common_forms(raw):
    assert winbt.normalize_mac(raw) == PRINTER_MAC


@pytest.mark.parametrize("bad", ["", "C8:48:8A", "C8:48:8A:53:26", "ZZ:48:8A:53:26:7C"])
def test_normalize_mac_rejects_bad(bad):
    with pytest.raises(ValueError):
        winbt.normalize_mac(bad)


# ------------------------------------------------------------ inquiry 时长换算
@pytest.mark.parametrize(
    "duration, expected",
    [(0.0, 0), (-1.0, 0), (0.5, 1), (1.28, 1), (3.0, 3), (10.0, 8), (61.44, 48), (1000.0, 48)],
)
def test_inquiry_multiplier(duration, expected):
    assert winbt.inquiry_multiplier(duration) == expected


# ------------------------------------------------------------------ 结构体布局
@pytest.mark.skipif(ctypes.sizeof(ctypes.c_void_p) != 8, reason="按 x64 布局断言")
def test_struct_sizes_match_windows_abi():
    assert ctypes.sizeof(winbt.SOCKADDR_BTH) == 30  # 必须 packed，否则 32 → WSAEADDRNOTAVAIL
    assert ctypes.sizeof(winbt.BLUETOOTH_DEVICE_INFO) == 560
    assert ctypes.sizeof(winbt.BLUETOOTH_DEVICE_SEARCH_PARAMS) == 40
    assert ctypes.sizeof(winbt.BLUETOOTH_RADIO_INFO) == 520


def test_sockaddr_bth_field_offsets():
    assert winbt.SOCKADDR_BTH.addressFamily.offset == 0
    assert winbt.SOCKADDR_BTH.btAddr.offset == 2
    assert winbt.SOCKADDR_BTH.port.offset == 26


# ---------------------------------------------------------- WindowsScanner 映射
def _fake_devices():
    return [
        winbt.WinDevice(BLE_TWIN_MAC, "X1B_267C", 0, False, False, False),
        winbt.WinDevice(PRINTER_MAC, "X1B_267C", 0x00800604, False, True, True),
        winbt.WinDevice("48:BD:A7:8F:94:27", "荣耀畅玩20", 0x005A020C, True, True, True),
    ]


def test_windows_scanner_maps_device_fields(monkeypatch):
    monkeypatch.setattr(winbt, "enumerate_devices", lambda duration=0.0: _fake_devices())
    devs = WindowsScanner().scan(5.0)

    by_mac = {d.mac: d for d in devs}
    printer = by_mac[PRINTER_MAC]
    assert printer.name == "X1B_267C"
    assert printer.paired is True
    assert printer.device_class == 0x00800604
    assert printer.is_imaging is True
    assert printer.rssi is None
    assert printer.source == "paired"

    twin = by_mac[BLE_TWIN_MAC]
    assert twin.paired is False
    assert twin.device_class is None
    assert twin.source == "scan"

    phone = by_mac["48:BD:A7:8F:94:27"]
    assert phone.major_class == 0x0200  # Phone，不是 Imaging


def test_windows_scanner_sorts_best_first(monkeypatch):
    monkeypatch.setattr(winbt, "enumerate_devices", lambda duration=0.0: _fake_devices())
    devs = WindowsScanner().scan(0.0)
    assert devs[0].mac == PRINTER_MAC
    assert devs[-1].mac == "48:BD:A7:8F:94:27"


def test_windows_scanner_passes_duration(monkeypatch):
    seen = []

    def fake(duration=0.0):
        seen.append(duration)
        return []

    monkeypatch.setattr(winbt, "enumerate_devices", fake)
    WindowsScanner().scan(4.0)
    assert seen == [4.0]

    seen.clear()
    WindowsScanner(inquiry=False).scan(4.0)
    assert seen == [0.0]  # 不在范围内搜索时只读系统缓存


def test_windows_scanner_wraps_errors(monkeypatch):
    def boom(duration=0.0):
        raise winbt.WinBtError("没有可用的蓝牙适配器")

    monkeypatch.setattr(winbt, "enumerate_devices", boom)
    with pytest.raises(ScanError) as info:
        WindowsScanner().scan(1.0)
    assert "Windows 蓝牙扫描失败" in str(info.value)


def test_windows_scanner_wraps_oserror(monkeypatch):
    def boom(duration=0.0):
        raise OSError(10093, "WSAStartup 未调用")

    monkeypatch.setattr(winbt, "enumerate_devices", boom)
    with pytest.raises(ScanError):
        WindowsScanner().scan(1.0)


def test_scan_devices_accepts_windows_scanner(monkeypatch):
    monkeypatch.setattr(winbt, "enumerate_devices", lambda duration=0.0: _fake_devices())
    devs = scan_devices(2.0, scanner=WindowsScanner())
    assert len(devs) == 3
    assert devs[0].mac == PRINTER_MAC


# ------------------------------------------------------------- 平台默认后端
def test_default_scanner_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    scanner = default_scanner()
    assert isinstance(scanner, WindowsScanner)


def test_default_scanner_linux(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    from deliprint.discovery import BluezCtlScanner

    scanner = default_scanner()
    assert isinstance(scanner, BluezCtlScanner)
    assert not isinstance(scanner, WindowsScanner)
