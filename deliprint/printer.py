"""DL-X1B 打印驱动（得利 Deli SDK 协议）。

用法（离线验证，不需要真机）::

    from deliprint import DeliPrinter, LoopbackTransport, text_to_raster
    t = LoopbackTransport()
    p = DeliPrinter(t)
    p.connect()
    p.print_text("你好 DL-X1B", dither="floyd")
    p.stop()
    print(t.written.hex(" "))

真机（系统里已配对的蓝牙，Linux / Windows 均可）::

    p = DeliPrinter.connect_bt("AA:BB:CC:DD:EE:FF")
    p.print_text("hello")
    p.stop()

打印序列与 App 完全一致（PrePrintImgActivity.java:665-681）::

    enable() -> wakeup() -> choose_paper_type(3) -> feed(0)
             -> print_bitmap(bitmap, 0) -> feed(140) -> stop()

状态：打印机主动推 2 字节帧 ``FF xx``（xx & 0x04 缺纸、xx & 0x08 低压、
xx == 0 正常），整页打完回 ASCII "OK"。
"""

from __future__ import annotations

import queue
import sys
import threading
import time
from typing import Callable, Optional

from . import commands as C
from .raster import PRINTER_WIDTH, to_1bpp
from .transport import Transport

CHUNK = 1024  # 原厂分块大小（com.deli.sdk.e.a(byte[],boolean)）
CHUNK_DELAY = 0.001  # 块间 sleep 1ms


class PrinterError(Exception):
    pass


class DeliPrinter:
    def __init__(self, transport: Transport, auto_read: bool = True, debug: bool = False,
                 auto_reconnect: bool = True):
        self.transport = transport
        self.debug = debug
        self._read_thread: Optional[threading.Thread] = None
        self._stop_read = threading.Event()
        self._lock = threading.Lock()
        self._finished = threading.Event()
        self._rxq: "queue.Queue[bytes]" = queue.Queue()
        self._job_started = False
        # 回调
        self.on_status: Optional[Callable[[int], None]] = None
        self.on_receive: Optional[Callable[[bytes], None]] = None
        self.on_print_finished: Optional[Callable[[], None]] = None
        self._auto_read = auto_read
        self.last_status: Optional[int] = None
        self.device = None  # auto_connect / 扫描后附带的 Device 信息
        self._awaiting = False               # request() 等待应答时不再把回包丢给 on_receive
        self._mac: Optional[str] = None      # connect_bt 记下来，断线可自动重连
        self._channel: int = 1
        self.auto_reconnect = auto_reconnect

    # ---------------------------------------------------------------- 连接
    @classmethod
    def connect_bt(cls, mac: str, channel: int = 1, **kw) -> "DeliPrinter":
        from .transport import BtSppTransport

        t = BtSppTransport(mac, channel)
        p = cls(t, **kw)
        p._mac, p._channel = mac.upper(), channel
        p.connect()
        return p

    @classmethod
    def auto_connect(
        cls,
        duration: float = 8.0,
        patterns=None,
        channel: int = 1,
        scanner=None,
        **kw,
    ) -> "DeliPrinter":
        """扫描 → 按 App 规则匹配 DL-X1B → 连接。找不到时抛 PrinterError。"""
        from .discovery import find_printer

        dev = find_printer(duration, patterns, scanner)
        if dev is None:
            hint = (
                "，并先在「设置 → 蓝牙和其他设备」里配对它"
                if sys.platform.startswith("win")
                else ""
            )
            raise PrinterError(
                f"扫描 {duration:g}s 未发现匹配的 DL-X1B（名字应含 X1B、不含 BLE）；"
                f"确认打印机开机、在范围内{hint}，或用 --mac 手动指定"
            )
        p = cls.connect_bt(dev.mac, channel, **kw)
        p.device = dev
        return p

    def reconnect(self) -> None:
        """断开重连（打印机休眠/链路被系统回收后恢复）。"""
        if self._mac is None:
            raise PrinterError("这个实例不是 connect_bt 创建的，无法自动重连")
        from .transport import BtSppTransport

        try:
            self.transport.close()
        except Exception:
            pass
        t = BtSppTransport(self._mac, self._channel)
        t.open()
        self.transport = t
        self._finished.clear()
        self._job_started = False

    def connect(self) -> None:
        self.transport.open()
        if self._auto_read:
            self._stop_read.clear()
            self._read_thread = threading.Thread(target=self._read_loop, daemon=True)
            self._read_thread.start()

    def disconnect(self) -> None:
        self._stop_read.set()
        if self._read_thread is not None:
            self._read_thread.join(timeout=1.0)
            self._read_thread = None
        self.transport.close()

    def __enter__(self):
        if not self.transport.is_open:
            self.connect()
        return self

    def __exit__(self, *exc):
        self.disconnect()

    @property
    def is_open(self) -> bool:
        return self.transport.is_open

    # ---------------------------------------------------------------- 底层
    def _raw(self, data: bytes) -> None:
        if not self.transport.is_open:
            if not (self.auto_reconnect and self._mac):
                raise PrinterError("未连接")
            self.reconnect()
        if self.debug:
            print(f">> {data[:16].hex(' ')}{' ...' if len(data) > 16 else ''}")
        try:
            self.transport.write(data)
        except OSError:
            # 真机上链路可能被对端回收（Errno 107/104）；重连一次再重发
            if not (self.auto_reconnect and self._mac):
                raise
            self.reconnect()
            self.transport.write(data)

    def _send_chunked(self, data: bytes, chunk: int = CHUNK) -> None:
        """对齐原厂：每块 1024 字节，块间 sleep 1ms。"""
        for off in range(0, len(data), chunk):
            self._raw(data[off:off + chunk])
            if off + chunk < len(data):
                time.sleep(CHUNK_DELAY)

    # ---------------------------------------------------------------- 读线程
    def _read_loop(self) -> None:
        while not self._stop_read.is_set():
            try:
                data = self.transport.read(timeout=0.05)
            except Exception:
                time.sleep(0.2)
                continue
            if not data:
                continue
            if self.debug:
                print(f"<< {data.hex(' ')}")
            self._dispatch(data)

    def _dispatch(self, data: bytes) -> None:
        i = 0
        while i < len(data):
            # 状态帧 FF xx
            if data[i] == 0xFF and i + 1 < len(data):
                code = data[i + 1]
                self.last_status = code
                if self.on_status:
                    self.on_status(code)
                if code & C.STATUS_LOW_BATTERY and self.on_status:
                    pass
                i += 2
                continue
            # 打印完成 "OK"
            if data[i:i + 2] == C.ACK_PRINT_FINISHED:
                self._finished.set()
                if self.on_print_finished:
                    self.on_print_finished()
                i += 2
                continue
            # 真机固件：任务开始回 AA、结束回 BB（两者可能分两次读上来）。
            # 注意：大图时 AA 可能在 stop() 之前就到达，所以 BB 无条件算完成。
            if data[i] == C.JOB_START_ACK and not self._job_started:
                self._job_started = True
                i += 1
                continue
            if data[i] == C.JOB_DONE_ACK:
                self._job_started = False
                self._finished.set()
                if self.on_print_finished:
                    self.on_print_finished()
                i += 1
                continue
            # 其它（查询应答等）整段交给上层
            rest = data[i:]
            self._rxq.put(rest)
            if self.on_receive and not self._awaiting:
                self.on_receive(rest)
            return

    # ---------------------------------------------------------------- 打印流程
    def enable(self) -> None:
        self._raw(C.ENABLE_PRINTER)

    def wakeup(self) -> None:
        """原厂 printerWakeup：发 1024 个 0x00。"""
        self._send_chunked(C.WAKEUP_PADDING)

    def choose_paper_type(self, paper_type: int = 3) -> None:
        """App 对 2 英寸机型传 3（4 英寸传 4）。"""
        self._raw(C.choose_paper_type(paper_type))

    def feed(self, lines: int) -> None:
        """走纸 n 点行（ESC J n）。App 打印前 0，打印后 140。"""
        self._raw(C.feed_lines(lines))

    def set_thickness(self, level: int) -> None:
        self._raw(C.set_thickness(level))

    def set_shut_time(self, seconds: int) -> None:
        self._raw(C.set_shut_time(seconds))

    def learn_label_gap(self) -> None:
        self._raw(C.LEARN_LABEL_GAP)

    def backoff_paper(self) -> None:
        self._raw(C.BACKOFF_PAPER)

    def stop(self) -> None:
        self._finished.clear()
        self._job_started = False
        self._raw(C.STOP_PRINT_JOB)

    def flush_input(self) -> None:
        """清掉输入缓冲（原厂 e.c() 的 drain，防止旧应答串到下一次打印）。"""
        self.transport.drain()

    # ---------------------------------------------------------------- 图像
    def print_raster(self, data: bytes, bytes_per_line: int, height: int, mode: int = 0) -> None:
        """发送已打包好的 1bpp 点阵（GS v 0）。"""
        self.flush_input()
        self._raw(C.raster_header(bytes_per_line, height, mode))
        self._send_chunked(data)

    def print_image(self, img, mode: int = 0, dither: str = "threshold", threshold: int = 128) -> None:
        """打印任意 PIL 图像（自动缩放到 384 宽、按需要抖动）。"""
        from .raster import fit_width

        img = fit_width(img, PRINTER_WIDTH)
        w, h, data = to_1bpp(img, dither=dither, threshold=threshold)
        self.print_raster(data, (w + 7) // 8, h, mode)

    def print_text(self, text: str, dither: str = "floyd", font_size: int = 24,
                   font_path: Optional[str] = None, **text_kw) -> None:
        from .raster import text_image

        img = text_image(text, font_size=font_size, font_path=font_path, **text_kw)
        self.print_image(img, dither=dither)

    def print_image_page(self, img, paper_type: int = 3, **kw) -> None:
        """按 App 的顺序完整打一张图片：enable/wakeup/paper/feed0/图/feed140/stop。"""
        self.enable()
        self.wakeup()
        self.choose_paper_type(paper_type)
        self.feed(0)
        self.print_image(img, **kw)
        self.feed(140)
        self.stop()

    def print_text_page(self, text: str, **kw) -> None:
        """按 App 的顺序完整打一张：enable/wakeup/paper/feed0/图/feed140/stop。"""
        self.enable()
        self.wakeup()
        self.choose_paper_type(kw.pop("paper_type", 3))
        self.feed(0)
        self.print_text(text, **kw)
        self.feed(140)
        self.stop()

    # ---------------------------------------------------------------- 查询
    def query_status(self) -> None:
        """发状态查询；真正的结果显示在 on_status 回调里（FF xx）。"""
        self._raw(C.STATUS)

    # App 连接后的握手是 SN → 电量 → 关机时间 → 版本 → 状态（MainActivity.java:385-465），
    # 这里按同一顺序提供 5 个单发查询；应答都从 on_receive/on_status 异步回来。
    def get_sn(self) -> None:
        self._raw(C.QUERY_SN)

    def get_battery(self) -> None:
        self._raw(C.QUERY_BATTERY)

    def get_shut_time(self) -> None:
        self._raw(C.QUERY_SHUT_TIME)

    def get_version(self) -> None:
        self._raw(C.QUERY_VERSION)

    def get_model(self) -> None:
        self._raw(C.QUERY_MODEL)

    def get_bt_name(self) -> None:
        self._raw(C.QUERY_BT_NAME)

    def get_bt_mac(self) -> None:
        self._raw(C.QUERY_BT_MAC)

    def get_bt_version(self) -> None:
        self._raw(C.QUERY_BT_VERSION)

    def get_paper_type(self) -> None:
        self._raw(C.GET_PAPER_TYPE)

    def printer_position(self) -> None:
        self._raw(C.PRINTER_POSITION)

    def set_paper_mode(self, continuous: bool = True) -> None:
        """10 FF 60 01 连续纸 / 00 标签纸（原厂 App 未调用）。"""
        self._raw(C.set_paper_mode(continuous))

    def read_all_info(self) -> None:
        """按 App 顺序把 SN/电量/关机时间/版本/状态各发一次（应答异步回来）。"""
        self.get_sn()
        self.get_battery()
        self.get_shut_time()
        self.get_version()
        self._raw(C.STATUS)

    def query(self, cmd: bytes) -> None:
        self._raw(cmd)

    # ------------------------------------------------------- 同步查询（收发一问一答）
    def _drain_rx(self) -> None:
        while True:
            try:
                self._rxq.get_nowait()
            except queue.Empty:
                return

    def request(self, cmd: bytes, timeout: float = 2.0) -> bytes:
        """发一条查询并同步等应答；超时返回已收到的部分（可能为空）。

        应答不发 ``FF`` 前缀，也不回 "OK"，与原厂 App 的异步回调等价，
        只是这里把「发出去 → 收回包」封装成一次调用。
        """
        self._drain_rx()
        self._awaiting = True
        try:
            self._raw(cmd)
        except Exception:
            self._awaiting = False
            raise
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                self._awaiting = False
                return b""
            try:
                data = self._rxq.get(timeout=min(left, 0.05))
                self._awaiting = False
                return data
            except queue.Empty:
                if self._auto_read:
                    continue
                # 没有读线程时自己收：先收一段再回队列
                data = self.transport.read(0.02)
                if data:
                    self._dispatch(data)

    # 真机实测（DL-X1B / 3.16YC）：
    #   version   -> "3.16YC"
    #   SN        -> "65414018456"
    #   model     -> "DL-X1B"
    #   battery   -> 00 64（byte1 = 电量%）
    #   shut_time -> 00 14（byte1 = 分钟）
    def read_version(self, timeout: float = 2.0) -> str:
        return self.request(C.QUERY_VERSION, timeout).decode("gbk", "replace").strip()

    def read_sn(self, timeout: float = 2.0) -> str:
        return self.request(C.QUERY_SN, timeout).decode("gbk", "replace").strip()

    def read_model(self, timeout: float = 2.0) -> str:
        return self.request(C.QUERY_MODEL, timeout).decode("gbk", "replace").strip()

    def read_bt_name(self, timeout: float = 2.0) -> str:
        return self.request(C.QUERY_BT_NAME, timeout).decode("gbk", "replace").strip()

    def read_battery(self, timeout: float = 2.0) -> int:
        """电量百分比；-1 表示无应答。"""
        buf = self.request(C.QUERY_BATTERY, timeout)
        if not buf:
            return -1
        return buf[1] if len(buf) >= 2 else buf[0]

    def read_shut_time(self, timeout: float = 2.0) -> int:
        """自动关机时间（分钟）；-1 表示无应答。"""
        buf = self.request(C.QUERY_SHUT_TIME, timeout)
        if not buf:
            return -1
        return buf[1] if len(buf) >= 2 else buf[0]

    def read_paper_type(self, timeout: float = 2.0) -> int:
        buf = self.request(C.GET_PAPER_TYPE, timeout)
        return buf[0] if buf else -1

    def read_status(self, timeout: float = 2.0) -> int:
        """同步读状态字节（真机只回 1 字节，例如 0x00 正常）。"""
        buf = self.request(C.STATUS, timeout)
        return buf[0] if buf else -1

    def wait_print_finished(self, timeout: float = 30.0) -> bool:
        return self._finished.wait(timeout)
