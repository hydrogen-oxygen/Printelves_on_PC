"""自动扫描与机型匹配 —— 复刻 App 的蓝牙发现规则。

App 的扫描过滤（`com/feioou/abroad/views/addequitment/MyEquitmentActivity.java:87`）：

1. 设备名 `contains` 任一：``XiangYinBao`` / ``X1B`` / ``X3B`` / ``S501`` /
   ``S502`` / ``THERMAL PRINTER``；
2. 且名字**不含** ``BLE``（DL-X1B 走经典 SPP，BLE 广播的是 OTA/别的设备）；
3. 且 ``BluetoothClass.getMajorDeviceClass() == 1536``（``0x600``，Imaging 类）。

然后按名字分支：``X1B`` → Deli SDK（本库）；``XiangYinBao_X1`` → cpcl；
``X3B``/``S501``/``S502`` → HPRT。本模块默认只匹配 **X1B**，
但把 App 的整条过滤规则和打分逻辑都实现了，便于真机现场排查。

扫描后端按平台自动选择：Linux 用 BlueZ 的 ``bluetoothctl``（自带，不需要
PyBluez），Windows 用系统 API（bthprops + winsock，不需要 pywin32）。
也接受任意实现 ``scan(duration) -> list[Device]`` 的自定义 scanner（测试用）。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Callable, Iterable, List, Optional, Sequence

# App 侧的过滤规则
APP_NAME_PATTERNS: tuple[str, ...] = (
    "XIANGYINBAO",
    "X1B",
    "X3B",
    "S501",
    "S502",
    "THERMAL PRINTER",
)
BLE_MARKER = "BLE"
IMAGING_MAJOR_CLASS = 0x600  # 1536，App 用的判据

# 本库默认目标：只认 X1B（X3B/S501 等是别的机型，协议不同）
DEFAULT_PATTERNS: tuple[str, ...] = ("X1B",)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_MAC_RE = re.compile(r"([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})")
_NAME_RE = re.compile(r"Device\s+([0-9A-Fa-f:]{17})\s+(.+?)\s*$")
_RSSI_RE = re.compile(r"RSSI:\s*0x[0-9A-Fa-f]+\s*\((-?\d+)\)")
_CLASS_RE = re.compile(r"Class:\s*(0x[0-9A-Fa-f]+)")
_PAIRED_RE = re.compile(r"Paired:\s*(yes|no)", re.IGNORECASE)
_INFO_KEYS = (
    "RSSI:",
    "CLASS:",
    "PAIRED:",
    "BONDED:",
    "TRUSTED:",
    "BLOCKED:",
    "CONNECTED:",
    "LEGACYPAIRING:",
    "WAKEALLOWED:",
    "NAME:",
    "ALIAS:",
    "UUID:",
    "MODALIAS:",
)


class ScanError(RuntimeError):
    """扫描失败（没有适配器、bluetoothctl 缺失、BlueZ 未启动等）。"""


@dataclass(frozen=True)
class Device:
    """一个蓝牙设备。``device_class`` 为 BlueZ 的 ``Class`` 值（可能为 None）。"""

    mac: str
    name: str = ""
    paired: bool = False
    rssi: Optional[int] = None
    device_class: Optional[int] = None
    source: str = "scan"  # scan / paired / manual

    @property
    def major_class(self) -> Optional[int]:
        """Class 的高位主设备类（0x600 = Imaging，打印机通常落在这里）。"""
        if self.device_class is None:
            return None
        return self.device_class & 0x1F00

    @property
    def is_imaging(self) -> bool:
        return self.major_class == IMAGING_MAJOR_CLASS

    @property
    def mac_display(self) -> str:
        return self.mac.upper()

    def __str__(self) -> str:  # 便于日志打印
        parts = [self.mac_display]
        if self.name:
            parts.append(self.name)
        tags = []
        if self.paired:
            tags.append("已配对")
        if self.is_imaging:
            tags.append("Imaging")
        if self.rssi is not None:
            tags.append(f"{self.rssi}dBm")
        if tags:
            parts.append("[" + ",".join(tags) + "]")
        return " ".join(parts)


# --------------------------------------------------------------------- 匹配
def is_x1b(name: str) -> bool:
    """设备名是否是 DL-X1B（大小写不敏感，忽略空格/下划线）。"""
    compact = re.sub(r"[\s_\-]+", "", (name or "")).upper()
    return "X1B" in compact and BLE_MARKER not in compact


def _norm(patterns: Optional[Sequence[str]]) -> tuple[str, ...]:
    """None → 默认模式；其它原样转成 tuple。"""
    if not patterns:
        return DEFAULT_PATTERNS
    return tuple(patterns)


def _patterns(extra: Optional[Sequence[str]]) -> tuple[str, ...]:
    """App 默认模式 + 调用方附加模式（附加不是替换）。"""
    if not extra:
        return APP_NAME_PATTERNS
    return tuple(dict.fromkeys([*APP_NAME_PATTERNS, *(str(p).upper() for p in extra)]))


def passes_app_filter(dev: Device, patterns: Optional[Sequence[str]] = None) -> bool:
    """复刻 App 的扫描过滤。类信息未知时放行（BlueZ 不一定给 Class）。"""
    name = (dev.name or "").strip().upper()
    if not name:
        return False
    if BLE_MARKER in name:
        return False
    if not any(p in name for p in _patterns(patterns)):
        return False
    if dev.device_class not in (None, 0) and not dev.is_imaging:
        return False
    return True


def score_device(dev: Device, patterns: Sequence[str] = DEFAULT_PATTERNS) -> int:
    """给设备打分；0 表示不匹配。分数只用于排序，无协议含义。"""
    patterns = _norm(patterns)
    if not passes_app_filter(dev, patterns):
        return 0
    name = (dev.name or "").upper()
    score = 0
    if "DL-X1B" in name:
        score += 120
    if any(p.upper() in name for p in patterns):
        score += 100
    if "X1B" in name:
        score += 30
    if dev.paired:
        score += 20  # 已配对的优先（App 在 MainActivity 里会自动重连已配对）
    if dev.is_imaging:
        score += 10
    if dev.rssi is not None:
        score += max(0, min(15, (dev.rssi + 100) // 4))  # -100..-40 → 0..15
    return score


def rank_devices(
    devices: Iterable[Device], patterns: Sequence[str] = DEFAULT_PATTERNS
) -> List[Device]:
    """过滤 + 按匹配度降序。"""
    patterns = _norm(patterns)
    scored = [(score_device(d, patterns), d) for d in devices]
    scored = [(s, d) for s, d in scored if s > 0]
    scored.sort(key=lambda item: (-item[0], item[1].name, item[1].mac))
    return [d for _, d in scored]


def pick_device(
    devices: Iterable[Device], patterns: Sequence[str] = DEFAULT_PATTERNS
) -> Optional[Device]:
    """从扫描结果里挑一台最合适的；没有匹配返回 None。"""
    ranked = rank_devices(devices, patterns)
    return ranked[0] if ranked else None


# --------------------------------------------------------------------- 扫描
class Scanner:
    """扫描后端接口（鸭子类型即可，测试里直接传 FakeScanner）。"""

    def scan(self, duration: float = 8.0) -> List[Device]:  # pragma: no cover - 抽象
        raise NotImplementedError


class BluezCtlScanner(Scanner):
    """Linux/BlueZ：调用 ``bluetoothctl`` 扫描，不依赖第三方蓝牙库。

    BlueZ 的行为补充：``scan on`` 只报告**正在广播**的设备；已配对但当前没广播的
    打印机不会出现，所以扫描后还会合并 ``bluetoothctl devices`` 的缓存列表，
    并对候选设备做一次 ``info`` 以拿到 ``Paired/Class/RSSI``。
    """

    def __init__(
        self,
        ctl: str = "bluetoothctl",
        info_timeout: float = 2.0,
        max_info_lookups: int = 30,
    ):
        self.ctl = ctl
        self.info_timeout = info_timeout
        self.max_info_lookups = max_info_lookups

    # -- 子进程小工具 ---------------------------------------------------
    def _run(self, args: Sequence[str], timeout: float) -> str:
        if shutil.which(self.ctl) is None:
            raise ScanError(
                f"未找到 {self.ctl}。Linux 请安装 bluez（apt install bluez），"
                "或在 GUI 里手动填 MAC 直接连接。"
            )
        proc = subprocess.run(
            [self.ctl, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        out = _ANSI_RE.sub("", (proc.stdout or "") + "\n" + (proc.stderr or ""))
        if proc.returncode != 0 and "No default controller" in out:
            raise ScanError(
                "BlueZ 没有可用的蓝牙适配器（或 bluetooth 服务未启动）。"
                "检查：`systemctl status bluetooth`、`bluetoothctl list`。"
            )
        return out

    # -- 解析 -----------------------------------------------------------
    @staticmethod
    def parse_scan_output(text: str) -> dict[str, Device]:
        """解析 ``scan on`` / ``devices`` / ``info`` 的混合输出。

        只依赖文本里出现的 ``Device <MAC> <name>``、``RSSI: 0x.. (-80)``、
        ``Class: 0x…``、``Paired: yes`` 这些行，因此对 BlueZ 版本差异较宽容。
        """
        found: dict[str, Device] = {}
        for raw in text.splitlines():
            line = _ANSI_RE.sub("", raw).strip()
            mac_match = _MAC_RE.search(line)
            if not mac_match or "Device" not in line:
                continue
            mac = mac_match.group(1).upper()
            name_match = _NAME_RE.search(line)
            name = name_match.group(2).strip() if name_match else ""
            # "(public)"/"(random)" 是地址类型；"RSSI: …"/"Class: …" 是 info 行，
            # 都不是设备名（bluetoothctl 的 [CHG] 行也以 "Device <MAC>" 开头）
            if name.startswith("(") or name.upper().startswith(_INFO_KEYS):
                name = ""
            prev = found.get(mac)
            if prev is None:
                found[mac] = Device(mac=mac, name=name)
            elif name and not prev.name:
                found[mac] = _replace(prev, name=name)

        # 第二遍补 info 字段（RSSI/Class/Paired 可能出现在独立的 [CHG] 行里）
        current: Optional[str] = None
        for raw in text.splitlines():
            line = _ANSI_RE.sub("", raw).strip()
            mac_hit = _MAC_RE.search(line)
            mac_here = mac_hit.group(1).upper() if mac_hit else None
            # `[CHG] Device <MAC> RSSI: 0x.. (-65)` 这类行自带 MAC，直接落到该设备
            if mac_here is not None and mac_here in found and not line.startswith("Device "):
                dev = found[mac_here]
                m = _RSSI_RE.search(line)
                if m:
                    found[mac_here] = _replace(dev, rssi=int(m.group(1)))
                m = _CLASS_RE.search(line)
                if m:
                    found[mac_here] = _replace(dev, device_class=int(m.group(1), 16))
                m = _PAIRED_RE.search(line)
                if m:
                    found[mac_here] = _replace(dev, paired=m.group(1).lower() == "yes")
                continue
            if line.startswith("Device ") and mac_here:
                current = mac_here
                continue
            if current is None:
                continue
            dev = found.get(current)
            if dev is None:
                continue
            if line.startswith("Name:") or line.startswith("Alias:"):
                value = line.split(":", 1)[1].strip()
                if value and not dev.name:
                    found[current] = _replace(dev, name=value)
                    dev = found[current]
            m = _RSSI_RE.search(line)
            if m:
                found[current] = _replace(dev, rssi=int(m.group(1)))
            m = _CLASS_RE.search(line)
            if m:
                found[current] = _replace(dev, device_class=int(m.group(1), 16))
            m = _PAIRED_RE.search(line)
            if m:
                found[current] = _replace(dev, paired=m.group(1).lower() == "yes")
        return found

    def _enrich(self, dev: Device) -> Device:
        try:
            out = self._run(["info", dev.mac], timeout=self.info_timeout)
        except (subprocess.TimeoutExpired, ScanError):
            return dev
        parsed = self.parse_scan_output(out)
        info = parsed.get(dev.mac)
        if info is None:
            return dev
        if info.name.startswith("("):
            info = _replace(info, name="")
        return Device(
            mac=dev.mac,
            name=info.name or dev.name,
            paired=info.paired or dev.paired,
            rssi=info.rssi if info.rssi is not None else dev.rssi,
            device_class=info.device_class if info.device_class is not None else dev.device_class,
            source=dev.source,
        )

    # -- 主流程 ---------------------------------------------------------
    def scan(self, duration: float = 8.0) -> List[Device]:
        duration = max(1.0, float(duration))
        out = self._run(
            ["--timeout", str(int(round(duration))), "scan", "on"],
            timeout=duration + 10,
        )
        devices = self.parse_scan_output(out)

        # 已配对/缓存设备（可能当前没有广播）
        try:
            cached = self._run(["devices"], timeout=10)
            for mac, dev in self.parse_scan_output(cached).items():
                devices.setdefault(mac, _replace(dev, source="paired"))
        except (subprocess.TimeoutExpired, ScanError):
            pass

        # 补 info（只对疑似打印机/已配对设备，避免几十次子进程）
        candidates = [
            d
            for d in devices.values()
            if d.paired or passes_app_filter(d) or not d.name
        ][: self.max_info_lookups]
        for dev in candidates:
            enriched = self._enrich(dev)
            devices[dev.mac] = enriched
        return sorted(devices.values(), key=lambda d: (-score_device(d), d.name, d.mac))


class WindowsScanner(Scanner):
    """Windows：ctypes 调系统蓝牙 API，不需要 PyBluez / pywin32。

    Windows 没有 bluetoothctl，改用 bthprops.cpl 的 BluetoothFindFirstDevice：

    * 已配对（remembered/authenticated）设备来自系统缓存，打印机休眠/没广播时
      也能看到，和 BlueZ 的 devices 缓存效果一样；
    * duration > 0 时再发起一次 inquiry，找出在范围内但尚未配对的设备
      （耗时约为 1.28s 的整数倍，由 winbt.inquiry_multiplier 换算）。

    设备类 ulClassofDevice 与 BlueZ 的 Class 含义相同，所以上面的过滤/打分规则
    原样复用。Windows 不返回 RSSI，Device.rssi 为 None（不影响匹配）。
    """

    def __init__(self, inquiry: bool = True):
        self.inquiry = inquiry

    def scan(self, duration: float = 8.0) -> List[Device]:
        from . import winbt

        seconds = max(0.0, float(duration)) if self.inquiry else 0.0
        try:
            raw = winbt.enumerate_devices(seconds)
        except (winbt.WinBtError, OSError) as exc:
            raise ScanError(f"Windows 蓝牙扫描失败：{exc}") from exc
        devices = [
            Device(
                mac=d.mac,
                name=d.name,
                paired=d.paired,
                rssi=None,
                device_class=d.device_class or None,
                source="paired" if d.paired else "scan",
            )
            for d in raw
        ]
        return sorted(devices, key=lambda d: (-score_device(d), d.name, d.mac))


def default_scanner() -> Scanner:
    """按平台选默认扫描后端：Windows → bthprops，其它 → bluetoothctl。"""
    if sys.platform.startswith("win"):
        return WindowsScanner()
    return BluezCtlScanner()


def _replace(dev: Device, **changes) -> Device:
    data = {
        "mac": dev.mac,
        "name": dev.name,
        "paired": dev.paired,
        "rssi": dev.rssi,
        "device_class": dev.device_class,
        "source": dev.source,
    }
    data.update(changes)
    return Device(**data)


# --------------------------------------------------------------------- 便捷函数
def scan_devices(
    duration: float = 8.0, scanner: Optional[Scanner] = None
) -> List[Device]:
    """扫描一轮，返回所有发现的设备（已按匹配度排序，未匹配的排最后）。"""
    return (scanner or default_scanner()).scan(duration)


def scan_printers(
    duration: float = 8.0,
    patterns: Sequence[str] = DEFAULT_PATTERNS,
    scanner: Optional[Scanner] = None,
) -> List[Device]:
    """扫描并只返回匹配的打印机（默认只匹配 X1B）。"""
    return rank_devices((scanner or default_scanner()).scan(duration), patterns)


def find_printer(
    duration: float = 8.0,
    patterns: Sequence[str] = DEFAULT_PATTERNS,
    scanner: Optional[Scanner] = None,
) -> Optional[Device]:
    """扫描一轮并返回最匹配的打印机；找不到返回 None（不抛异常）。"""
    return pick_device((scanner or default_scanner()).scan(duration), patterns)
