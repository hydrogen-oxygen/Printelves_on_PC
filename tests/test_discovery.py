"""自动扫描与匹配的离线测试（不需要蓝牙硬件）。"""

from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from deliprint.discovery import (
    BluezCtlScanner,
    Device,
    ScanError,
    find_printer,
    is_x1b,
    passes_app_filter,
    pick_device,
    rank_devices,
    scan_devices,
    score_device,
)


SAMPLE_SCAN = """
[CHG] Controller 10:4A:7D:68:A7:3F Discovering: yes
[NEW] Device C8:48:8A:53:26:7C X1B_267C
[CHG] Device C8:48:8A:53:26:7C RSSI: 0xffffffbf (-65)
[NEW] Device C9:48:8A:53:26:7C X1B_267C_BLE
[NEW] Device 13:05:AA:00:08:94 BT5.2 Mouse
[NEW] Device 20:74:54:E0:74:8E vivo S6
Device C8:48:8A:53:26:7C (public)
\tName: X1B_267C
\tAlias: X1B_267C
\tClass: 0x00800604 (8390148)
\tPaired: yes
"""

SAMPLE_INFO_S501 = """
Device AA:BB:CC:00:11:22 (public)
\tName: S501-0001
\tClass: 0x000680 (1664)
\tPaired: no
"""


def test_is_x1b():
    assert is_x1b("X1B_267C")
    assert is_x1b("DL-X1B")
    assert is_x1b("dl x1b")  # 大小写/分隔符不敏感
    assert not is_x1b("X1B_267C_BLE")
    assert not is_x1b("XiangYinBao_X1")


def test_parse_scan_output_skips_pseudo_names():
    devs = BluezCtlScanner.parse_scan_output(SAMPLE_SCAN)
    printer = devs["C8:48:8A:53:26:7C"]
    assert printer.name == "X1B_267C"  # 不是 "(public)"
    assert printer.paired is True
    assert printer.is_imaging
    assert printer.rssi == -65
    assert devs["13:05:AA:00:08:94"].name == "BT5.2 Mouse"


def test_app_filter_rejects_ble_and_non_printer():
    ble = Device("C9:48:8A:53:26:7C", "X1B_267C_BLE")
    mouse = Device("13:05:AA:00:08:94", "BT5.2 Mouse")
    good = Device("C8:48:8A:53:26:7C", "X1B_267C", paired=True, device_class=0x00800604)
    assert not passes_app_filter(ble)
    assert not passes_app_filter(mouse)
    assert passes_app_filter(good)


def test_scoring_prefers_x1b_paired_imaging():
    best = Device("C8:48:8A:53:26:7C", "X1B_267C", paired=True,
                  rssi=-60, device_class=0x00800604)
    ble = Device("C9:48:8A:53:26:7C", "X1B_267C_BLE", rssi=-50)
    other = Device("11:22:33:44:55:66", "Some Speaker")
    assert score_device(ble) == 0
    assert score_device(other) == 0
    assert score_device(best) > 100
    assert pick_device([ble, other, best]) is best


def test_rank_devices_order_and_dl_x1b_bonus():
    plain = Device("AA:BB:CC:DD:EE:01", "X1B_A")
    dl = Device("AA:BB:CC:DD:EE:02", "DL-X1B-A")
    ranked = rank_devices([plain, dl])
    assert ranked[0].name == "DL-X1B-A"


def test_scan_devices_uses_custom_scanner():
    class FakeScanner:
        def __init__(self, devices):
            self.devices = devices
            self.calls = []

        def scan(self, duration=8.0):
            self.calls.append(duration)
            return self.devices

    fake = FakeScanner([Device("C8:48:8A:53:26:7C", "X1B_267C", paired=True),
                        Device("13:05:AA:00:08:94", "Mouse")])
    devs = scan_devices(3.0, scanner=fake)
    assert fake.calls == [3.0]
    assert len(devs) == 2
    assert find_printer(3.0, scanner=fake).name == "X1B_267C"
    # patterns 是「额外名字模式」，不是白名单；X1B 永远命中
    assert find_printer(3.0, patterns=("NOTHING",), scanner=fake).name == "X1B_267C"


def test_find_printer_respects_custom_patterns():
    class FakeScanner:
        def scan(self, duration=8.0):
            return [Device("AA:BB:CC:DD:EE:FF", "FOO-123", device_class=0x00800604)]

    assert find_printer(2.0, scanner=FakeScanner()) is None
    assert find_printer(2.0, patterns=("FOO",), scanner=FakeScanner()).name == "FOO-123"


def test_missing_bluetoothctl_raises_scan_error():
    scanner = BluezCtlScanner(ctl="/nonexistent/bluetoothctl")
    with pytest.raises(ScanError):
        scanner.scan(1.0)


def test_patterns_none_defaults_to_x1b():
    """auto_connect 会把 patterns=None 传进来，不能炸。"""
    from deliprint.discovery import Device, rank_devices, score_device, DEFAULT_PATTERNS

    dev = Device(mac="C8:48:8A:53:26:7C", name="X1B_267C", paired=True,
                 rssi=-51, device_class=0x00800604)
    assert score_device(dev, None) > 0
    assert score_device(dev, DEFAULT_PATTERNS) == score_device(dev, None)
    assert rank_devices([dev], None) == [dev]
