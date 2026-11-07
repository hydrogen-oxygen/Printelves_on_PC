"""帧编码 / 解码。

帧格式（由 cpcl/Tools.getBytes 反汇编确认）::

    0x02 | cmd1 | cmd2 | len_lo | len_hi | payload(len 字节) | crc32(key,payload) 4B | 0x03

* 所有多字节整数均为**小端**（cpcl/Tools.intTo2Bytes / intTo4Bytes）。
* crc32 是标准 IEEE CRC-32（反射多项式 0xEDB88320），但初值与终值异或的是
  ``key ^ 0xFFFFFFFF``，``key`` 是会话密钥：默认 0x35769521，
  连接后的密钥协商帧（0x18）成功后改为 0 或 1。
* 应答帧与请求帧格式完全相同，但 **cmd1 = 请求 cmd1 + 1**。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Iterable, Iterator, List, Optional, Tuple

HEAD = 0x02
TAIL = 0x03
#: 未协商时的默认 CRC 会话密钥（cpcl/Tools 静态字段 crcKey 的初值）
DEFAULT_CRC_KEY = 0x35769521

# CRC-32/IEEE 查表（反射，poly 0xEDB88320），运行时由下表生成
_CRC_TABLE: List[int] = []


def _ensure_table() -> List[int]:
    if _CRC_TABLE:
        return _CRC_TABLE
    for i in range(256):
        c = i
        for _ in range(8):
            c = (c >> 1) ^ 0xEDB88320 if c & 1 else c >> 1
        _CRC_TABLE.append(c)
    return _CRC_TABLE


def crc32_keyed(data: bytes, key: int = DEFAULT_CRC_KEY) -> int:
    """带会话密钥的 CRC-32。

    与 zlib.crc32 的区别仅在初值：``crc = key ^ 0xFFFFFFFF``，末尾再异或 0xFFFFFFFF。
    ``key == 0`` 时结果等价于 ``zlib.crc32(data)``。
    """
    table = _ensure_table()
    crc = (key ^ 0xFFFFFFFF) & 0xFFFFFFFF
    for b in data:
        crc = ((crc >> 8) ^ table[(crc ^ b) & 0xFF]) & 0xFFFFFFFF
    return (crc ^ 0xFFFFFFFF) & 0xFFFFFFFF


# ---------------------------------------------------------------------------
# 命令码（cpcl/Config$Print 静态字段，名字为反编译后的短名）
# ---------------------------------------------------------------------------
class CMD:
    IMAGE = 0x00          # R  —— 图像光栅数据（payload = 原始 1bpp 点阵）
    OTA_DATA = 0x02       # S  —— 固件升级数据
    VERSION = 0x04        # U  —— 查询版本
    MODEL = 0x06          # aa —— 查询型号
    BT_MAC = 0x08         # ca —— 查询蓝牙 MAC
    SN = 0x0A             # ea —— 查询 SN
    STATUS = 0x0C         # ga —— 查询状态（应答 0x0D）
    ELECTRICITY = 0x10    # ia —— 查询电量
    TEMP = 0x12           # ja? —— 查询温度（请求码为 0x12 时应答 0x13）
    CRC_KEY = 0x18        # la —— 会话 CRC 密钥协商
    DENSITY = 0x19        # ma —— 设置打印浓度
    FEED_LINE = 0x1A      # na —— 设置走纸（尾部留白）
    SELF_TEST = 0x1B      # oa —— 打印自检页
    DENSITY_Q = 0x1C      # pa —— 查询浓度
    OFF_TIME = 0x1E       # ra —— 设置自动关机时间
    OFF_TIME_Q = 0x1F     # sa —— 查询自动关机时间
    HEAD_LINE = 0x21      # ua —— 设置打印头起始位置（顶部留白）
    DEFAULT_PARA = 0x22   # va —— 恢复默认参数
    BOARD_VER = 0x23      # wa —— 查询板卡版本（应答 0x24）
    LINE_NUM = 0x34       # za —— 查询已打印行数
    LINE_NUM_Q = 0x35     # Aa —— 应答 0x35

    #: 名称表（请求码；应答码 = 请求码 + 1）
    NAMES = {
        0x00: "IMAGE 图像数据",
        0x02: "OTA_DATA 升级数据",
        0x04: "VERSION 查询版本",
        0x06: "MODEL 查询型号",
        0x08: "BT_MAC 查询蓝牙MAC",
        0x0A: "SN 查询SN",
        0x0C: "STATUS 查询状态",
        0x10: "ELECTRICITY 查询电量",
        0x12: "TEMP 查询温度",
        0x18: "CRC_KEY 密钥协商",
        0x19: "DENSITY 设置浓度",
        0x1A: "FEED_LINE 设置走纸",
        0x1B: "SELF_TEST 自检页",
        0x1C: "DENSITY_Q 查询浓度",
        0x1E: "OFF_TIME 设置关机",
        0x1F: "OFF_TIME_Q 查询关机",
        0x21: "HEAD_LINE 设置留白",
        0x22: "DEFAULT_PARA 恢复默认",
        0x23: "BOARD_VER 查询板卡版本",
        0x34: "LINE_NUM 查询已打行数",
    }

    @classmethod
    def name(cls, code: int) -> str:
        if code in cls.NAMES:
            return cls.NAMES[code]
        if (code - 1) in cls.NAMES:
            return cls.NAMES[code - 1].split()[0] + "_ACK 应答"
        return f"未命名(0x{code:02X})"


class STATUS:
    """cpcl/Config$Status 常量（状态字节的位 → 状态码）。"""

    NORMAL = 0
    NO_PAPER = 1
    MEGATHERMAL = 2      # 过热
    MICROTHERM = 3       # 温度过低
    LOW_PRESSURE = 4     # 电压过低
    OPEN_COVER = 5       # 开盖
    OFF = 6              # 关机

    NAMES = {
        0: "正常",
        1: "缺纸",
        2: "过热",
        3: "温度过低",
        4: "电压过低",
        5: "盖子未关",
        6: "关机",
    }

    #: 状态字节位 → 状态码（cpcl/PrinterHelper.l([B)）
    BITS = (
        (0x01, 1),
        (0x02, 2),
        (0x04, 3),
        (0x08, 4),
        (0x10, 5),
        (0x20, 6),
    )

    @classmethod
    def decode(cls, byte: int) -> int:
        byte &= 0xFF
        for mask, code in cls.BITS:
            if byte & mask:
                return code
        return cls.NORMAL if byte == 0 else cls.NORMAL

    @classmethod
    def name(cls, code: int) -> str:
        return cls.NAMES.get(code, f"未知({code})")


# ---------------------------------------------------------------------------
# 编解码
# ---------------------------------------------------------------------------
def build_frame(cmd1: int, cmd2: int, payload: bytes = b"", key: int = DEFAULT_CRC_KEY) -> bytes:
    """按 cpcl/Tools.getBytes 组装一帧。"""
    payload = bytes(payload)
    if len(payload) > 0xFFFF:
        raise ValueError("payload 超过 65535 字节")
    crc = crc32_keyed(payload, key)
    return (
        bytes((HEAD, cmd1 & 0xFF, cmd2 & 0xFF))
        + struct.pack("<H", len(payload))
        + payload
        + struct.pack("<I", crc)
        + bytes((TAIL,))
    )


@dataclass
class Frame:
    """一个解析出来的完整帧。"""

    cmd1: int
    cmd2: int
    payload: bytes
    crc: int
    crc_ok: bool

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"Frame(cmd1=0x{self.cmd1:02X}, cmd2={self.cmd2}, len={len(self.payload)}, crc_ok={self.crc_ok})"

    def hex(self, limit: int = 64) -> str:
        p = self.payload[:limit].hex(" ")
        return f"{self.prefix_hex} | {p}{' ...' if len(self.payload) > limit else ''}"

    @property
    def prefix_hex(self) -> str:
        return f"02 {self.cmd1:02x} {self.cmd2:02x} {len(self.payload) & 0xFF:02x} {len(self.payload) >> 8:02x}"


class FrameParser:
    """增量解析字节流。

    - 自动重同步：找 0x02 帧头，长度/CRC 不符就丢 1 字节继续找。
    - 同时支持一次到达多帧与半帧分片到达（蓝牙 SPP 是流）。
    """

    def __init__(self, key: int = DEFAULT_CRC_KEY, verify_crc: bool = True) -> None:
        self.buf = bytearray()
        self.key = key
        self.verify_crc = verify_crc

    def feed(self, data: bytes) -> List[Frame]:
        self.buf.extend(data)
        return list(self._parse())

    def _parse(self) -> Iterator[Frame]:
        while True:
            if not self.buf:
                return
            if self.buf[0] != HEAD:
                idx = self.buf.find(bytes((HEAD,)))
                if idx < 0:
                    self.buf.clear()
                    return
                del self.buf[:idx]
            if len(self.buf) < 5:
                return
            length = self.buf[3] | (self.buf[4] << 8)
            total = 5 + length + 5  # 头5 + payload + crc4 + tail1
            if len(self.buf) < total:
                return
            if self.buf[total - 1] != TAIL:
                del self.buf[:1]
                continue
            cmd1 = self.buf[1]
            cmd2 = self.buf[2]
            payload = bytes(self.buf[5 : 5 + length])
            (crc,) = struct.unpack("<I", bytes(self.buf[5 + length : 9 + length]))
            ok = (not self.verify_crc) or (crc == crc32_keyed(payload, self.key))
            if self.verify_crc and not ok:
                del self.buf[:1]
                continue
            del self.buf[:total]
            yield Frame(cmd1, cmd2, payload, crc, ok)


def parse_frames(data: bytes, key: int = DEFAULT_CRC_KEY, verify_crc: bool = True) -> List[Frame]:
    """一次性解析整段数据（调试 / 抓包回放用）。"""
    return FrameParser(key, verify_crc).feed(data)
