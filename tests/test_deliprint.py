"""deliprint 离线自测：点阵打包、GS v 0 帧、完整打印序列。

运行：python3 -m pytest tests/test_deliprint.py -q   或   python3 tests/test_deliprint.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from deliprint import (  # noqa: E402
    BYTES_PER_LINE,
    PRINTER_WIDTH,
    DeliPrinter,
    LoopbackTransport,
    raster_header,
    text_image,
    to_1bpp,
)

ENABLE = bytes([0x10, 0xFF, 0xFE, 0x01])
STOP = bytes([0x10, 0xFF, 0xFE, 0x45])
PAPER_2 = bytes([0x10, 0xFF, 0x84, 0x03])
FEED_0 = bytes([0x1B, 0x4A, 0x00])
FEED_140 = bytes([0x1B, 0x4A, 0x8C])


def _img(rows, width):
    from PIL import Image

    img = Image.new("L", (width, len(rows)), 255)
    px = img.load()
    for y, bits in enumerate(rows):
        for x, v in enumerate(bits):
            px[x, y] = 0 if v else 255
    return img


def test_raster_header():
    assert raster_header(48, 68, 0) == bytes([0x1D, 0x76, 0x30, 0, 48, 0, 68, 0])
    assert raster_header(384, 300, 3) == bytes([0x1D, 0x76, 0x30, 3, 0x80, 0x01, 0x2C, 0x01])


def test_pack_msb_black_is_one():
    # 第一行像素 1,0,0,0,0,0,0,0 -> 0x80；第二行全黑 -> 0xFF
    img = _img([[1, 0, 0, 0, 0, 0, 0, 0], [1] * 8], 8)
    w, h, data = to_1bpp(img)
    assert (w, h) == (8, 2)
    assert data == bytes([0x80, 0xFF])


def test_pack_avg_threshold():
    from PIL import Image

    # (100+100+100)/3=100 <128 -> 黑； (200,0,0) 均值 66 <128 -> 黑（均值判据而非加权）
    img = Image.new("RGB", (2, 1), (255, 255, 255))
    img.putpixel((0, 0), (100, 100, 100))
    img.putpixel((1, 0), (0, 200, 200))  # 均值 133 -> 白
    _, _, data = to_1bpp(img)
    assert data == bytes([0x80])


def test_text_image_width():
    img = text_image("DL-X1B 打印测试", font_size=24, width=PRINTER_WIDTH)
    assert img.width == PRINTER_WIDTH
    assert img.height > 0


def test_full_sequence_bytes():
    t = LoopbackTransport(ack=False)
    p = DeliPrinter(t)
    p.connect()
    p._finished.set()
    p.print_text_page("111\n222", font_size=24, dither="threshold")
    p.disconnect()
    raw = bytes(t.written)
    # 顺序
    assert raw.startswith(ENABLE)
    assert raw[4:4 + 1024] == bytes(1024)          # printerWakeup：1024 个 0
    off = 4 + 1024
    assert raw[off:off + 4] == PAPER_2             # choosePaperType(3)
    off += 4
    assert raw[off:off + 3] == FEED_0              # printLinedots(0)
    off += 3
    assert raw[off:off + 3] == b"\x1d\x76\x30"     # GS v 0
    bpl = raw[off + 4] | (raw[off + 5] << 8)
    h = raw[off + 6] | (raw[off + 7] << 8)
    assert bpl == BYTES_PER_LINE
    data_end = off + 8 + bpl * h
    assert data_end <= len(raw)
    assert raw[data_end:data_end + 3] == FEED_140
    assert raw.endswith(STOP)


def test_chunk_size_1024():
    t = LoopbackTransport(ack=False)
    p = DeliPrinter(t)
    p.connect()
    big = bytes(range(256)) * 16  # 4096 字节
    p.print_raster(big, 48, 4096 // 48)
    p.disconnect()
    # 头 8 字节一块，然后数据按 1024 分块
    assert all(len(c) <= 1024 for c in t.chunks)
    assert sum(len(c) for c in t.chunks) == len(bytes(t.written))


def test_status_dispatch():
    t = LoopbackTransport(ack=False)
    p = DeliPrinter(t)
    seen = []
    p.on_status = seen.append
    p.connect()
    t.feed(bytes([0xFF, 0x04]))
    import time
    time.sleep(0.1)
    p.disconnect()
    assert seen and (seen[0] & 0x04)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("all passed")


def test_query_bytes_match_app_state_machine():
    """App 连上后按 SN→电量→关机时间→版本→状态 查询（MainActivity.java:385-465）。"""
    from deliprint import LoopbackTransport
    from deliprint.printer import DeliPrinter

    t = LoopbackTransport()
    p = DeliPrinter(t)
    p.connect()
    p.read_all_info()
    assert bytes(t.written) == (
        bytes([0x10, 0xFF, 0x20, 0xF2])   # SN
        + bytes([0x10, 0xFF, 0x50, 0xF1])  # 电量
        + bytes([0x10, 0xFF, 0x13])        # 关机时间
        + bytes([0x10, 0xFF, 0x20, 0xF1])  # 版本
        + bytes([0x10, 0xFF, 0x40])        # 状态
    )


def test_paper_mode_bytes():
    from deliprint import commands as C

    assert C.set_paper_mode(True) == b"\x10\xff\x60\x01"
    assert C.set_paper_mode(False) == b"\x10\xff\x60\x00"


# ---------------------------------------------------------------- 真机回包语义
def test_aa_bb_marks_job_finished():
    from deliprint import DeliPrinter, LoopbackTransport

    p = DeliPrinter(LoopbackTransport(ack=False))
    done = []
    p.on_print_finished = lambda: done.append(True)
    p._dispatch(b"\xaa")
    assert not p._finished.is_set()          # 只收到 AA 还不算完成
    p._dispatch(b"\xbb")                      # BB 分两次读上来
    assert p._finished.is_set() and done == [True]


def test_ok_ack_still_works():
    from deliprint import DeliPrinter, LoopbackTransport

    p = DeliPrinter(LoopbackTransport(ack=False))
    p._dispatch(b"OK")
    assert p._finished.is_set()


class _CannedTransport:
    """write 时按命令回一段固定应答，用于测试 request/read_*。"""

    def __init__(self, replies):
        self.replies = replies      # dict[bytes, bytes]
        self._inbox = bytearray()
        self.written = bytearray()
        self._open = True

    @property
    def is_open(self):
        return self._open

    def open(self):
        self._open = True

    def write(self, data):
        self.written.extend(data)
        for cmd, resp in self.replies.items():
            if bytes(data).startswith(cmd):
                self._inbox.extend(resp)
        return len(data)

    def read(self, timeout=0.0):
        import time as _t

        end = _t.time() + max(0.0, timeout)
        while True:
            if self._inbox:
                out = bytes(self._inbox)
                self._inbox.clear()
                return out
            if _t.time() >= end:
                return b""
            _t.sleep(0.005)

    def drain(self):
        self._inbox.clear()

    def close(self):
        self._open = False


def test_request_and_typed_reads_real_device_formats():
    from deliprint import DeliPrinter
    from deliprint import commands as C

    t = _CannedTransport({
        C.QUERY_VERSION: b"3.16YC",
        C.QUERY_SN: b"65414018456",
        C.QUERY_MODEL: b"DL-X1B",
        C.QUERY_BATTERY: b"\x00\x64",
        C.QUERY_SHUT_TIME: b"\x00\x14",
        C.STATUS: b"\x00",
    })
    p = DeliPrinter(t, auto_read=False)
    assert p.read_version() == "3.16YC"
    assert p.read_sn() == "65414018456"
    assert p.read_model() == "DL-X1B"
    assert p.read_battery() == 100
    assert p.read_shut_time() == 20
    assert p.read_status() == 0


def test_request_timeout_returns_empty():
    from deliprint import DeliPrinter

    t = _CannedTransport({})
    p = DeliPrinter(t, auto_read=False)
    assert p.request(b"\x10\xff\x20\xf2", timeout=0.1) == b""


def test_bb_alone_marks_finished_when_aa_arrived_early():
    """大图时 AA 会在 stop() 之前到达（stop 会重置 job_started），BB 仍须算完成。"""
    from deliprint import DeliPrinter, LoopbackTransport

    p = DeliPrinter(LoopbackTransport(ack=False))
    p.connect()
    p._dispatch(b"\xaa")           # 图还没发完就收到 AA
    p.stop()                        # 等价于 stop() 重置了 job 状态
    p._finished.clear()             # stop() 自己会清一次完成标志
    p._dispatch(b"\xbb")
    assert p._finished.is_set()


def test_print_image_page_full_sequence():
    """图片打印必须走完整一页时序（enable→wakeup→paper→feed0→GS v 0→feed140→stop）。"""
    from PIL import Image
    from deliprint import DeliPrinter, LoopbackTransport

    t = LoopbackTransport(ack=False)
    p = DeliPrinter(t)
    p.connect()
    p.print_image_page(Image.new("L", (384, 10), 255))

    raw = bytes(t.written)
    assert raw[:4] == bytes([0x10, 0xFF, 0xFE, 0x01])          # enablePrinter
    assert raw[4:4 + 1024] == bytes(1024)                       # printerWakeup
    assert raw[1028:1032] == bytes([0x10, 0xFF, 0x84, 0x03])    # choosePaperType(3)
    assert raw[1032:1035] == bytes([0x1B, 0x4A, 0x00])          # 走纸 0
    assert raw[1035:1038] == bytes([0x1D, 0x76, 0x30])          # GS v 0
    assert raw[-7:-4] == bytes([0x1B, 0x4A, 0x8C])              # 走纸 140
    assert raw[-4:] == bytes([0x10, 0xFF, 0xFE, 0x45])          # stopPrintJob
