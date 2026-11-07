"""离线自测：不接打印机也能验证协议实现是否正确。

覆盖：
1. 帧构造 ↔ 解析（多种密钥、全部命令码）
2. 增量解析：半帧、多帧粘连、垃圾字节重同步
3. 1bpp 点阵：位序、三种半色调、1440 字节分片
4. 端到端：用「虚拟打印机」跑完整连接（密钥协商）→ 参数 → 文本 → 状态查询

运行：``python3 -m pytest tests/ -v``（也支持 ``python3 tests/test_offline.py``）
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dlx1b import (  # noqa: E402
    CMD,
    DEFAULT_CRC_KEY,
    DeliPrinter,
    FrameParser,
    PrinterImage,
    RasterOptions,
    STATUS,
    Transport,
    build_frame,
    crc32_keyed,
    parse_frames,
    text_image,
    to_raster,
)
from dlx1b.raster import BYTES_PER_LINE, MAX_CHUNK, PRINTER_WIDTH  # noqa: E402


# ---------------------------------------------------------------------------
# 1. 帧格式
# ---------------------------------------------------------------------------
def test_frame_roundtrip_all_commands():
    for key in (0, 1, DEFAULT_CRC_KEY):
        for code in CMD.NAMES:
            frame = build_frame(code, 7, b"\x01\x02\x03", key)
            # 线上格式：02 cmd1 cmd2 len_lo len_hi payload crc4 03
            assert frame[0] == 0x02 and frame[-1] == 0x03
            assert frame[1] == code and frame[2] == 7
            assert frame[3] | (frame[4] << 8) == 3
            got = parse_frames(frame, key)
            assert len(got) == 1, code
            assert got[0].cmd1 == code and got[0].payload == b"\x01\x02\x03"
            assert got[0].crc_ok


def test_frame_crc_matches_zlib_when_key_zero():
    import zlib

    payload = bytes(range(200))
    assert crc32_keyed(payload, 0) == zlib.crc32(payload)


def test_frame_crc_detects_corruption():
    frame = bytearray(build_frame(CMD.DENSITY, 1, b"\x02", 0))
    frame[5] ^= 0xFF  # 破坏 payload
    assert parse_frames(bytes(frame), 0) == []


def test_parser_split_and_glued_frames():
    a = build_frame(CMD.STATUS + 1, 2, b"\x00", 0)
    b = build_frame(CMD.IMAGE, 3, b"\xAA" * 100, 0)
    parser = FrameParser(key=0)
    out = []
    for i in range(0, len(a) + len(b), 7):  # 每 7 字节喂一次，模拟串口碎片
        out.extend(parser.feed((a + b)[i : i + 7]))
    assert [f.cmd1 for f in out] == [CMD.STATUS + 1, CMD.IMAGE]
    assert out[1].payload == b"\xAA" * 100


def test_parser_resync_after_garbage():
    junk = b"\x00\x11\x22\x33"
    good = build_frame(CMD.MODEL + 1, 4, b"DL-X1B", 0)
    frames = parse_frames(junk + good + junk, 0)
    assert len(frames) == 1 and frames[0].payload == b"DL-X1B"


# ---------------------------------------------------------------------------
# 2. 状态码
# ---------------------------------------------------------------------------
def test_status_decode():
    assert STATUS.decode(0x00) == 0
    assert STATUS.decode(0x01) == 1
    assert STATUS.decode(0x20) == 6
    # 多位同置时按低位优先
    assert STATUS.decode(0x11) == 1
    assert STATUS.name(1) == "缺纸"


# ---------------------------------------------------------------------------
# 3. 点阵
# ---------------------------------------------------------------------------
def _bits(raster: PrinterImage, x: int, y: int) -> int:
    byte = raster.data[y * raster.bytes_per_line + (x >> 3)]
    return (byte >> (7 - (x & 7))) & 1


def test_raster_msb_first_black_is_one():
    from PIL import Image

    img = Image.new("RGB", (16, 2), "white")
    img.putpixel((0, 0), (0, 0, 0))    # 左上 → 第 0 字节最高位
    img.putpixel((7, 1), (0, 0, 0))    # 第 8 列 → 第 1 字节最高位
    r = to_raster(img, RasterOptions(halftone=0))
    assert r.width == 16 and r.bytes_per_line == 2
    assert r.data[0] == 0b10000000
    assert r.data[2] == 0b00000001  # 第二行第 7 列 → 低 1 位（MSB 在前）
    assert _bits(r, 0, 0) == 1 and _bits(r, 1, 0) == 0


def test_raster_threshold_128():
    from PIL import Image

    img = Image.new("RGB", (2, 1))
    img.putpixel((0, 0), (200, 200, 200))   # gray 200 → 白
    img.putpixel((1, 0), (120, 120, 120))   # gray 120 → 黑
    r = to_raster(img, RasterOptions(halftone=0))
    assert r.data[0] == 0b01000000


def test_raster_modes_384_width_and_chunks():
    img = text_image("相印宝 DL-X1B\nPython 逆向测试")
    assert img.width == PRINTER_WIDTH
    for mode in (0, 1, 2):
        r = to_raster(img, RasterOptions(halftone=mode))
        assert r.width == PRINTER_WIDTH
        assert r.bytes_per_line == BYTES_PER_LINE
        assert len(r.data) == r.bytes_per_line * r.height
        chunks = r.chunks()
        assert all(len(c) <= MAX_CHUNK for c in chunks)
        assert sum(len(c) for c in chunks) == len(r.data)
        # 除最后一块外都应正好 1440（整 30 行）
        assert all(len(c) == MAX_CHUNK for c in chunks[:-1])
        assert any(c != b"\x00" * len(c) for c in chunks)  # 不是全白


def test_raster_mode2_matches_matrix_rule():
    """mode 2: v=255-gray, 黑 ⇔ (v>>2) >= M[y&7][x&7] 且 (v>>2)!=0。"""
    from PIL import Image

    img = Image.new("RGB", (8, 8), "white")
    for y in range(8):
        for x in range(8):
            img.putpixel((x, y), (128, 128, 128))  # gray=128 → v=127 → v>>2=31
    r = to_raster(img, RasterOptions(halftone=2))
    from dlx1b.raster import _CLUSTER8

    for y in range(8):
        for x in range(8):
            expect = 1 if 31 >= _CLUSTER8[y][x] else 0
            assert _bits(r, x, y) == expect, (x, y)


# ---------------------------------------------------------------------------
# 4. 端到端：虚拟打印机
# ---------------------------------------------------------------------------
class FakePrinter(Transport):
    """模拟 DL-X1B：收到帧就按「应答 cmd1 = 请求 + 1」回包，并记录收到的帧。"""

    def __init__(self, status_byte: int = 0x00):
        self.status_byte = status_byte
        self.received: list = []
        self._tx = bytearray()
        self._open = False
        self._key = DEFAULT_CRC_KEY
        self._parser = FrameParser(self._key)

    @property
    def is_open(self) -> bool:
        return self._open

    def open(self) -> None:
        self._open = True

    def write(self, data: bytes) -> int:
        for frame in self._parser.feed(data):  # 解析器自己缓存半帧
            self.received.append(frame)
            self._reply(frame)
        return len(data)

    def _reply(self, frame) -> None:
        payload = b""
        reply_key = self._key          # 应答帧用「收到该帧时」的密钥
        if frame.cmd1 == CMD.CRC_KEY:  # 密钥协商
            self._key = int.from_bytes(frame.payload, "little") ^ DEFAULT_CRC_KEY
            payload = b"\x00"
        elif frame.cmd1 == CMD.STATUS:
            payload = bytes((self.status_byte,))
        elif frame.cmd1 in (CMD.VERSION,):
            payload = b"\x01\x02\x03"
        elif frame.cmd1 in (CMD.MODEL,):
            payload = b"X1B"
        elif frame.cmd1 in (CMD.SN,):
            payload = b"SN12345678"
        elif frame.cmd1 in (
            CMD.BT_MAC,
            CMD.BOARD_VER,
            CMD.DENSITY_Q,
            CMD.OFF_TIME_Q,
            CMD.LINE_NUM,
            CMD.ELECTRICITY,
        ):
            payload = b"\x03"
        elif frame.cmd1 == CMD.IMAGE:
            payload = b""
        else:
            payload = b"\x00"
        self._tx.extend(build_frame((frame.cmd1 + 1) & 0xFF, frame.cmd2, payload, reply_key))
        if frame.cmd1 == CMD.CRC_KEY:  # 应答发完之后才切换到新密钥
            self._parser.key = self._key

    def read(self, timeout: float = 0.5) -> bytes:
        if self._tx:
            data = bytes(self._tx)
            self._tx.clear()
            return data
        time.sleep(min(timeout, 0.01))
        return b""

    def close(self) -> None:
        self._open = False


def test_end_to_end_connect_print_status():
    fake = FakePrinter(status_byte=0x01)  # 缺纸
    p = DeliPrinter(fake, handshake=True, crc_seed=0)
    p.open()
    try:
        codes = [f.cmd1 for f in fake.received]
        assert codes[0] == CMD.CRC_KEY, "连接后第一帧必须是密钥协商"
        assert fake.received[0].cmd2 == 0, "密钥帧序号固定 0"

        p.set_density(2)
        p.set_feed_lines(4)
        n = p.print_text("你好 DL-X1B", font_size=24)
        assert n >= 1

        st = p.status()
        assert st.code == 1 and st.name == "缺纸" and not st.ok
        assert p.model() == "X1B"

        codes = [f.cmd1 for f in fake.received]
        assert CMD.DENSITY in codes and CMD.FEED_LINE in codes
        image_frames = [f for f in fake.received if f.cmd1 == CMD.IMAGE]
        assert len(image_frames) == n
        assert all(len(f.payload) <= MAX_CHUNK for f in image_frames)
        # 包序号逐帧 +1（从连接后的 1 开始，之前每发一帧也 +1）
        seqs = [f.cmd2 for f in image_frames]
        before = [f.cmd2 for f in fake.received if f.cmd1 in (CMD.DENSITY, CMD.FEED_LINE)]
        assert seqs == list(range(before[-1] + 1, before[-1] + 1 + n))
        # 图像帧不等待应答：发出后立刻能继续发下一帧
        dens = [f for f in fake.received if f.cmd1 == CMD.DENSITY][0]
        assert dens.payload == b"\x02"
        feed = [f for f in fake.received if f.cmd1 == CMD.FEED_LINE][0]
        assert feed.payload == (4 * 24).to_bytes(2, "little")
    finally:
        p.close()


def test_seq_wraps_at_0x7f():
    fake = FakePrinter()
    p = DeliPrinter(fake, handshake=True)
    p.open()
    try:
        seqs = []
        for _ in range(0x82):
            seqs.append(p._next_seq())
        assert seqs[:3] == [1, 2, 3]
        assert 0 not in seqs
        assert max(seqs) == 0x7F
        assert seqs[0x7F] == 1  # 0x7F 之后回到 1
    finally:
        p.close()


def main() -> int:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ok   {fn.__name__}")
        except Exception as exc:  # pragma: no cover
            failed += 1
            print(f"  FAIL {fn.__name__}: {exc!r}")
    print(f"\n{len(fns) - failed}/{len(fns)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
