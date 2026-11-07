"""Deli SDK 的传输层：经典蓝牙 SPP（RFCOMM）。

原厂实现 com.deli.sdk.a.connect()：
  * 等 BluetoothAdapter 状态 = STATE_ON（12），超时 3s；
  * `createRfcommSocketToServiceRecord(UUID 00001101-0000-1000-8000-00805F9B34FB)`；
  * 失败后每 200ms 重试一次直到超时；
  * 成功后开 InputStream/OutputStream，起一个读线程。

所以这里不需要 BLE（服务/特征值/MTU 都不存在），也不需要配对密码，
只要系统里该设备已配对（或内核允许 rfcomm connect）即可。

注意 Android/Python 的差异：原厂用 *secure* `createRfcommSocketToServiceRecord`，
靠 SDP 解析出 RFCOMM 信道；Python 标准库的 AF_BLUETOOTH 只能按信道号连接，
没有 SDP 查询接口，所以这里默认用 SPP 的标准信道 1（可用 channel 覆盖），
并复刻原厂「每 200ms 重连一次、总共 3s 超时」的行为。

Windows 的 Python 没有 socket.AF_BLUETOOTH：同样的重试语义改由
deliprint.winbt 用 winsock（WSASocketW + 非阻塞 connect）实现，对外接口不变。
"""

from __future__ import annotations

import select
import socket
import sys
import threading
import time
from typing import Optional

SPP_UUID = "00001101-0000-1000-8000-00805F9B34FB"
DEFAULT_CHANNEL = 1  # 多数国产打印机 SPP 服务在 channel 1


class Transport:
    def open(self) -> None:  # pragma: no cover - 抽象
        raise NotImplementedError

    def write(self, data: bytes) -> int:  # pragma: no cover - 抽象
        raise NotImplementedError

    def read(self, timeout: float = 0.0) -> bytes:  # pragma: no cover - 抽象
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover - 抽象
        raise NotImplementedError

    @property
    def is_open(self) -> bool:  # pragma: no cover - 抽象
        raise NotImplementedError

    def drain(self) -> None:
        """丢弃输入缓冲里的残留字节（原厂 a.e()）。"""
        try:
            while self.read(timeout=0.0):
                pass
        except Exception:
            pass


class BtSppTransport(Transport):
    """直连经典蓝牙 SPP：Linux/Android 用 AF_BLUETOOTH，Windows 用 winsock。"""

    def __init__(
        self,
        mac: str,
        channel: int = DEFAULT_CHANNEL,
        timeout: float = 3.0,
        retry_interval: float = 0.2,
    ):
        self.mac = mac.upper()
        self.channel = channel
        self.timeout = timeout
        self.retry_interval = retry_interval
        self._sock: Optional[socket.socket] = None

    def open(self) -> None:
        """按原厂语义重试：失败每 200ms 再来一次，直到 timeout 秒。"""
        deadline = time.monotonic() + self.timeout
        while True:
            sock: Optional[socket.socket] = None
            try:
                sock = self._open_socket(max(0.5, self.retry_interval * 2.5))
                # 连接成功后切回阻塞模式：写大图时由内核做流控（否则 50ms 的读超时
                # 会让 sendall 半路超时，长图打印就断在半路）
                sock.settimeout(None)
                self._sock = sock
                return
            except OSError:
                # winbt 的失败路径会自己 closesocket，这里只管标准库建出来的那条
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
                if time.monotonic() >= deadline:
                    raise
                time.sleep(self.retry_interval)

    def _open_socket(self, timeout: float) -> socket.socket:
        """建一条已连上的 RFCOMM socket；失败抛 OSError（Windows 走 winsock）。"""
        if sys.platform.startswith("win"):
            from .winbt import open_rfcomm_socket

            return open_rfcomm_socket(self.mac, self.channel, timeout=timeout)
        if not hasattr(socket, "AF_BLUETOOTH"):
            raise RuntimeError(
                "当前平台没有 socket.AF_BLUETOOTH：Linux 请用官方 CPython，"
                "macOS 请改用 SerialTransport（已配对串口），"
                "Windows 上 BtSppTransport 会自动走 winsock"
            )
        sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
        try:
            sock.settimeout(timeout)
            sock.connect((self.mac, self.channel))
        except OSError:
            sock.close()
            raise
        return sock

    def write(self, data: bytes) -> int:
        if self._sock is None:
            raise OSError("蓝牙 socket 未打开")
        view = memoryview(data)
        total = 0
        while total < len(view):
            try:
                n = self._sock.send(view[total:])
            except InterruptedError:
                continue
            if n <= 0:
                raise OSError("蓝牙 socket 发送失败")
            total += n
        return total

    def read(self, timeout: float = 0.0) -> bytes:
        # 用 select 等可读，而不是改 socket 超时：socket 超时是共享状态，
        # 读线程频繁 settimeout 会把并发的大块写也一起打断。
        if self._sock is None:
            return b""
        wait = timeout if timeout and timeout > 0 else 0.05
        try:
            ready, _, _ = select.select([self._sock], [], [], wait)
        except (OSError, ValueError):
            return b""
        if not ready:
            return b""
        try:
            return self._sock.recv(4096)
        except (BlockingIOError, socket.timeout, InterruptedError):
            return b""

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    @property
    def is_open(self) -> bool:
        return self._sock is not None


class SocketTransport(Transport):
    """TCP 透传（例如 ESP32 蓝牙串口桥、USB 串口服务器）。"""

    def __init__(self, host: str, port: int, timeout: float = 10.0):
        self.host, self.port, self.timeout = host, port, timeout
        self._sock: Optional[socket.socket] = None

    def open(self) -> None:
        self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self._sock.settimeout(0.2)

    def write(self, data: bytes) -> int:
        assert self._sock is not None
        self._sock.sendall(data)
        return len(data)

    def read(self, timeout: float = 0.0) -> bytes:
        assert self._sock is not None
        self._sock.settimeout(timeout or 0.2)
        try:
            return self._sock.recv(4096)
        except (socket.timeout, BlockingIOError):
            return b""

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    @property
    def is_open(self) -> bool:
        return self._sock is not None


class SerialTransport(Transport):
    """已配对串口 / USB 转 TTL 直连（pyserial）。"""

    def __init__(self, port: str, baudrate: int = 115200, timeout: float = 0.2):
        self.port, self.baudrate, self.timeout = port, baudrate, timeout
        self._ser = None

    def open(self) -> None:
        import serial

        self._ser = serial.Serial(self.port, self.baudrate, timeout=self.timeout)

    def write(self, data: bytes) -> int:
        assert self._ser is not None
        return self._ser.write(data)

    def read(self, timeout: float = 0.0) -> bytes:
        assert self._ser is not None
        self._ser.timeout = timeout or self.timeout
        return self._ser.read(self._ser.in_waiting or 1)

    def close(self) -> None:
        if self._ser is not None:
            self._ser.close()
            self._ser = None

    @property
    def is_open(self) -> bool:
        return self._ser is not None


class LoopbackTransport(Transport):
    """虚拟打印机：记录写入的字节，并模拟固件应答（FF 00 / OK）。

    用于离线验证字节流、跑单元测试、以及在没有真机时演示 GUI。
    """

    def __init__(self, ack: bool = True):
        self.written = bytearray()
        self.chunks = []  # 每次 write() 的独立块，便于验证 1024 分片
        self._open = False
        self._inbox = bytearray()
        self.ack = ack
        self._lock = threading.Lock()

    def open(self) -> None:
        self._open = True

    def write(self, data: bytes) -> int:
        with self._lock:
            self.written.extend(data)
            self.chunks.append(bytes(data))
            if self.ack and data:
                # 简化模拟：任何命令都回状态正常；打印结束时回 "OK"
                if data[:1] == b"\x1d" and len(data) > 8:
                    self._inbox.extend(b"OK")
                else:
                    self._inbox.extend(bytes([0xFF, 0x00]))
        return len(data)

    def feed(self, data: bytes) -> None:
        """外部注入应答（模拟缺纸/低压等）。"""
        with self._lock:
            self._inbox.extend(data)

    def read(self, timeout: float = 0.0) -> bytes:
        deadline = time.time() + max(0.0, timeout)
        while True:
            with self._lock:
                if self._inbox:
                    out = bytes(self._inbox)
                    self._inbox.clear()
                    return out
            if time.time() >= deadline:
                return b""
            time.sleep(0.005)

    def close(self) -> None:
        self._open = False

    @property
    def is_open(self) -> bool:
        return self._open
