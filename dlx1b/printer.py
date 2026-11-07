"""打印机高层驱动：连接、参数设置、状态查询、图像/文本打印。

对应 App 里的 ``cpcl/Print`` + ``cpcl/PrinterHelper`` + ``cpcl/WriteThread``。
传输层可替换（见 :mod:`dlx1b.transport`），因此同一套帧逻辑既能走真实蓝牙，
也能走串口、TCP 网关或回环（离线自测）。
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

from .frame import (
    CMD,
    DEFAULT_CRC_KEY,
    STATUS,
    Frame,
    FrameParser,
    build_frame,
)
from .raster import (
    MAX_CHUNK,
    PRINTER_WIDTH,
    PrinterImage,
    RasterOptions,
    image_from_pil,
    text_image,
    to_raster,
)
from .transport import RfcommTransport, Transport

log = logging.getLogger("dlx1b")

#: 底层写分片大小（cpcl/BTOperator.WriteData 每 0x400 字节 write + flush）
WRITE_BLOCK = 0x400
#: 等待应答的超时（App 的 WriteThread 是 Future.get(5, SECONDS)）
ACK_TIMEOUT = 5.0
#: 帧序号循环上限（getPackage：> 0x7f 回到 1）
SEQ_MAX = 0x7F


class PrinterError(Exception):
    """打印 / 通讯错误。"""


@dataclass
class PrinterStatus:
    code: int

    @property
    def name(self) -> str:
        return STATUS.name(self.code)

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.name}({self.code})"

    @property
    def ok(self) -> bool:
        return self.code == STATUS.NORMAL


ProgressCb = Optional[Callable[[int], None]]


class DeliPrinter:
    """得力 DL-X1B（相印宝）打印机。

    典型用法::

        from dlx1b import DeliPrinter, text_image, to_raster

        p = DeliPrinter.connect_bt("AA:BB:CC:DD:EE:FF")     # 阻塞直到连接成功
        p.set_density(3)
        p.print_text("你好，相印宝！", font_size=24)
        print(p.status())
        p.close()
    """

    def __init__(
        self,
        transport: Transport,
        *,
        handshake: bool = True,
        crc_seed: int = 0,
        strict_handshake: bool = False,
        on_frame: Optional[Callable[[Frame], None]] = None,
    ) -> None:
        self.transport = transport
        self.handshake_enabled = handshake
        self.crc_seed = crc_seed & 1
        self.strict_handshake = strict_handshake
        self.on_frame = on_frame

        self._seq = 0
        self._crc_key = DEFAULT_CRC_KEY
        self._parser = FrameParser(self._crc_key)
        self._inbox: "queue.Queue[Frame]" = queue.Queue()
        self._reader: Optional[threading.Thread] = None
        self._running = False
        self._lock = threading.Lock()
        self.connected = False
        self.last_error: Optional[str] = None

    # ------------------------------------------------------------------ 连接
    @classmethod
    def connect_bt(
        cls,
        mac: str,
        channel: int = 1,
        *,
        handshake: bool = True,
        crc_seed: int = 0,
        strict_handshake: bool = False,
        timeout: float = 10.0,
    ) -> "DeliPrinter":
        """通过经典蓝牙 SPP 连接（原生 RFCOMM：Linux 走 AF_BLUETOOTH，Windows 走 winsock）。"""
        transport = RfcommTransport(mac, channel=channel, timeout=timeout)
        printer = cls(
            transport,
            handshake=handshake,
            crc_seed=crc_seed,
            strict_handshake=strict_handshake,
        )
        printer.open()
        return printer

    def open(self) -> None:
        """打开传输、启动读线程、完成 CRC 会话密钥协商。"""
        self.transport.open()
        self.connected = True
        self._running = True
        self._reader = threading.Thread(target=self._read_loop, name="dlx1b-reader", daemon=True)
        self._reader.start()
        if self.handshake_enabled:
            try:
                self._handshake()
            except Exception as exc:
                self.last_error = str(exc)
                if self.strict_handshake:
                    self.close()
                    raise
                log.warning("CRC 会话密钥协商失败（继续按默认密钥发送）: %s", exc)

    def close(self) -> None:
        self._running = False
        if self._reader is not None:
            self._reader.join(timeout=1.0)
            self._reader = None
        try:
            self.transport.close()
        finally:
            self.connected = False

    # ------------------------------------------------------------- 读写内核
    def _read_loop(self) -> None:
        while self._running:
            try:
                data = self.transport.read(0.5)
            except Exception as exc:  # 断开
                if self._running:
                    self.last_error = str(exc)
                    log.warning("读线程退出: %s", exc)
                self._running = False
                return
            if not data:
                continue
            for frame in self._parser.feed(data):
                log.debug("RX %s", frame.hex())
                if self.on_frame:
                    try:
                        self.on_frame(frame)
                    except Exception:  # pragma: no cover
                        pass
                self._inbox.put(frame)

    def _next_seq(self) -> int:
        self._seq += 1
        if self._seq > SEQ_MAX:
            self._seq = 1
        return self._seq

    def _write(self, data: bytes) -> None:
        """按 1024 字节分块写入（与 App 的 BTOperator.WriteData 一致）。"""
        for i in range(0, len(data), WRITE_BLOCK):
            self.transport.write(data[i : i + WRITE_BLOCK])

    def _send(
        self,
        cmd1: int,
        payload: bytes = b"",
        *,
        cmd2: Optional[int] = None,
        wait_ack: bool = False,
        timeout: float = ACK_TIMEOUT,
    ) -> Optional[bytes]:
        """发一帧；``wait_ack`` 时等待应答并返回其 payload。"""
        seq = self._next_seq() if cmd2 is None else cmd2
        frame = build_frame(cmd1, seq, payload, self._crc_key)
        log.debug("TX %s", frame.hex(" "))
        self._write(frame)
        if not wait_ack:
            return None
        return self._wait_ack(cmd1, timeout)

    def _wait_ack(self, req_cmd1: int, timeout: float = ACK_TIMEOUT) -> bytes:
        """等待 cmd1 == req_cmd1 + 1 的应答帧（丢弃其它帧）。"""
        want = (req_cmd1 + 1) & 0xFF
        deadline = time.monotonic() + timeout
        while True:
            remain = deadline - time.monotonic()
            if remain <= 0:
                raise TimeoutError(f"等待 0x{want:02X} 应答超时（请求 0x{req_cmd1:02X}）")
            try:
                frame = self._inbox.get(timeout=remain)
            except queue.Empty:
                raise TimeoutError(f"等待 0x{want:02X} 应答超时（请求 0x{req_cmd1:02X}）") from None
            if frame.cmd1 == want:
                return frame.payload

    # ------------------------------------------------------------ 密钥协商
    def _handshake(self) -> None:
        """CRC 会话密钥协商（cpcl/Print.setPrintCrcKey）。

        App：payload = intTo4Bytes(crc ^ 0x35769521)，crc 随机 0/1；
        应答 payload[0] == 0 表示成功，此后所有帧的 CRC 都用 key = crc。
        """
        self._seq = 0  # setPrintCrcKey 里 Fa 被清零
        payload = (self.crc_seed ^ DEFAULT_CRC_KEY).to_bytes(4, "little")
        for attempt in range(3):
            resp = self._send(CMD.CRC_KEY, payload, cmd2=0, wait_ack=True, timeout=3.0)
            assert resp is not None
            code = resp[0] if resp else 0xFF
            if code == 0:  # Config$Typedef.Ba
                self._crc_key = self.crc_seed
                self._parser.key = self._crc_key
                log.info("CRC 会话密钥协商成功，key=%d", self._crc_key)
                return
            if code == 1:  # Config$Typedef.Ca → 让写线程重发
                log.warning("打印机会要求重发密钥帧（第 %d 次）", attempt + 1)
                time.sleep(0.1)
                continue
            raise PrinterError(f"打印机拒绝 CRC 密钥（应答字节 0x{code:02X}）")
        raise PrinterError("CRC 密钥协商多次被要求重发")

    # -------------------------------------------------------------- 参数设置
    def set_density(self, density: int) -> None:
        """设置打印浓度（App 直接透传 1 字节，建议 0..4，越大越黑）。"""
        self._send(CMD.DENSITY, bytes((density & 0xFF,)))

    def set_feed_lines(self, n: int) -> None:
        """设置尾部走纸量（App 公式：payload = intTo2Bytes(n * 8 * 3)，点）。"""
        if n > 0xAAA:
            raise ValueError("走纸量超出范围（<= 0xAAA）")
        self._send(CMD.FEED_LINE, (n * 8 * 3).to_bytes(2, "little"))

    def set_head_lines(self, n: int) -> None:
        """设置打印头起始位置 / 顶部留白（payload = intTo2Bytes(n * 8 * 3)）。"""
        self._send(CMD.HEAD_LINE, (n * 8 * 3).to_bytes(2, "little"))

    def set_off_time(self, seconds: int) -> None:
        """设置自动关机时间。"""
        self._send(CMD.OFF_TIME, (seconds & 0xFFFF).to_bytes(2, "little"))

    def default_params(self) -> None:
        """恢复默认参数（cmd 0x22, payload 0x00）。"""
        self._send(CMD.DEFAULT_PARA, b"\x00")

    def self_test(self) -> None:
        """打印自检页。"""
        self._send(CMD.SELF_TEST, b"\x00")

    # -------------------------------------------------------------- 状态查询
    def query(self, cmd1: int, payload: bytes = b"\x00", timeout: float = ACK_TIMEOUT) -> bytes:
        return self._send(cmd1, payload, wait_ack=True, timeout=timeout) or b""

    def status(self, timeout: float = ACK_TIMEOUT) -> PrinterStatus:
        """查询打印机状态（缺纸/过热/开盖…）。"""
        data = self.query(CMD.STATUS, timeout=timeout)
        return PrinterStatus(STATUS.decode(data[0] if data else 0))

    def density(self) -> int:
        data = self.query(CMD.DENSITY_Q)
        return data[0] if data else -1

    def sn(self) -> str:
        return self.query(CMD.SN).decode("ascii", "replace").strip("\x00 ")

    def model(self) -> str:
        return self.query(CMD.MODEL).decode("ascii", "replace").strip("\x00 ")

    def version(self) -> str:
        return self.query(CMD.VERSION).decode("ascii", "replace").strip("\x00 ")

    def bt_mac(self) -> str:
        return self.query(CMD.BT_MAC).decode("ascii", "replace").strip("\x00 ")

    def board_version(self) -> str:
        return self.query(CMD.BOARD_VER).decode("ascii", "replace").strip("\x00 ")

    def electricity(self) -> str:
        return self.query(CMD.ELECTRICITY).decode("ascii", "replace").strip("\x00 ")

    def off_time(self) -> int:
        data = self.query(CMD.OFF_TIME_Q)
        return data[0] if data else -1

    def line_num(self) -> int:
        data = self.query(CMD.LINE_NUM)
        return data[0] if data else -1

    # ------------------------------------------------------------------ 打印
    def print_raster(
        self,
        image: PrinterImage,
        progress: ProgressCb = None,
        *,
        chunk: int = MAX_CHUNK,
        settle: float = 0.0,
    ) -> int:
        """把已转好的点阵按 1440 字节/帧推送。返回推送的帧数。

        * cmd1 = 0x00（图像数据），cmd2 = 帧序号（1..0x7f 循环）
        * 不等待应答（App 的 printBitmap 传 waitAck=false）
        """
        chunks = image.chunks(chunk)
        total = len(chunks)
        for i, part in enumerate(chunks):
            self._send(CMD.IMAGE, part, wait_ack=False)
            if settle:
                time.sleep(settle)
            if progress:
                progress(int((i + 1) * 100 / total))
        return total

    def print_image(self, pil_image, halftone: int = 0, progress: ProgressCb = None, **kw) -> int:
        """打印任意 PIL 图像（自动缩放到 384 宽）。"""
        img = image_from_pil(pil_image, width=PRINTER_WIDTH)
        raster = to_raster(img, RasterOptions(halftone=halftone))
        return self.print_raster(raster, progress=progress, **kw)

    def print_text(
        self, text: str, progress: ProgressCb = None, halftone: int = 0, **text_kw
    ) -> int:
        """打印文本。``text_kw`` 透传给 :func:`dlx1b.raster.text_image`。

        ``halftone`` 与原厂 ``PrintDataFormat`` 的三种模式一致（0 阈值 / 1 误差扩散 / 2 抖动）。
        """
        img = text_image(text, **text_kw)
        raster = to_raster(img, RasterOptions(halftone=halftone))
        return self.print_raster(raster, progress=progress)
