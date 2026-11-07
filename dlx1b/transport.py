"""传输层：把「往打印机写字节 / 从打印机读字节」抽象出来。

DL-X1B 走**经典蓝牙 SPP**（Serial Port Profile，服务 UUID
``00001101-0000-1000-8000-00805F9B34FB``），不是 BLE。App 里
``cpcl/BTOperator.OpenPort`` 用的是 ``createInsecureRfcommSocketToServiceRecord``，
失败再反射 ``createRfcommSocket(1)``。

三种实现：

* :class:`RfcommTransport` —— 原生 RFCOMM socket：Linux 用 ``AF_BLUETOOTH``，
  Windows 自动改用 winsock（复用 ``deliprint.winbt``），都无需第三方库，推荐。
* :class:`SerialTransport` —— 已配对的 SPP 虚拟串口（Windows ``COM5``、
  macOS ``/dev/cu.*``），需要 ``pyserial``。
* :class:`SocketTransport` —— 任意 TCP 端点（自建蓝牙网关、抓包代理、单元测试）。
"""

from __future__ import annotations

import abc
import socket
import sys
import time
from typing import Optional

SPP_UUID = "00001101-0000-1000-8000-00805F9B34FB"


class Transport(abc.ABC):
    """传输基类。"""

    @property
    @abc.abstractmethod
    def is_open(self) -> bool:
        ...

    @abc.abstractmethod
    def open(self) -> None:
        ...

    @abc.abstractmethod
    def write(self, data: bytes) -> int:
        ...

    @abc.abstractmethod
    def read(self, timeout: float = 0.5) -> bytes:
        """读一段数据；超时返回 ``b""``；连接断开抛 :class:`ConnectionError`。"""

    @abc.abstractmethod
    def close(self) -> None:
        ...

    def __enter__(self) -> "Transport":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class RfcommTransport(Transport):
    """原生 RFCOMM：Linux 用 ``socket.AF_BLUETOOTH``，Windows 用 winsock。

    >>> t = RfcommTransport("AA:BB:CC:DD:EE:FF")   # 默认 channel 1 = SPP
    >>> t.open()
    """

    def __init__(self, mac: str, channel: int = 1, timeout: float = 10.0) -> None:
        self.mac = mac.strip().upper()
        self.channel = channel
        self.timeout = timeout
        self._sock: Optional[socket.socket] = None

    @property
    def is_open(self) -> bool:
        return self._sock is not None

    def open(self) -> None:
        if len(self.mac) != 17 or ":" not in self.mac:
            raise ValueError(f"蓝牙地址格式应为 XX:XX:XX:XX:XX:XX，收到 {self.mac!r}")
        if sys.platform.startswith("win"):
            # Windows 的 Python 没有 AF_BLUETOOTH，借用 deliprint 的 winsock 实现
            from deliprint.winbt import open_rfcomm_socket

            self._sock = open_rfcomm_socket(self.mac, self.channel, timeout=self.timeout)
            return
        if not hasattr(socket, "AF_BLUETOOTH"):
            raise RuntimeError(
                "当前平台不支持 AF_BLUETOOTH：Windows 请用官方 CPython（会自动走 winsock），"
                "macOS 请改用 SerialTransport（已配对串口）"
            )
        sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
        sock.settimeout(self.timeout)
        try:
            sock.connect((self.mac, self.channel))
        except OSError:
            sock.close()
            raise
        sock.settimeout(None)
        self._sock = sock

    def write(self, data: bytes) -> int:
        if self._sock is None:
            raise ConnectionError("端口未打开")
        # App 侧 cpcl/BTOperator.WriteData 是每 0x400=1024 字节 write + flush；
        # 这里交给上层分片，传输层保证整段写完。
        self._sock.sendall(data)
        return len(data)

    def read(self, timeout: float = 0.5) -> bytes:
        if self._sock is None:
            raise ConnectionError("端口未打开")
        self._sock.settimeout(timeout)
        try:
            chunk = self._sock.recv(4096)
        except socket.timeout:
            return b""
        except OSError as exc:
            raise ConnectionError(f"蓝牙连接已断开: {exc}") from exc
        if not chunk:
            raise ConnectionError("对端关闭了连接")
        return chunk

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None


class SerialTransport(Transport):
    """已配对 SPP 虚拟串口（Windows / macOS / 蓝牙串口桥）。"""

    def __init__(self, port: str, baudrate: int = 115200, timeout: float = 0.5) -> None:
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self._ser = None

    @property
    def is_open(self) -> bool:
        return self._ser is not None

    def open(self) -> None:
        try:
            import serial  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("需要 pyserial：pip install pyserial") from exc
        self._ser = serial.Serial(self.port, self.baudrate, timeout=self.timeout)
        time.sleep(0.2)

    def write(self, data: bytes) -> int:
        if self._ser is None:
            raise ConnectionError("端口未打开")
        n = self._ser.write(data)
        self._ser.flush()
        return int(n)

    def read(self, timeout: float = 0.5) -> bytes:
        if self._ser is None:
            raise ConnectionError("端口未打开")
        self._ser.timeout = timeout
        data = self._ser.read(4096)
        return bytes(data)

    def close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            finally:
                self._ser = None


class SocketTransport(Transport):
    """TCP 传输（蓝牙网关 / 抓包代理 / 无硬件回环测试）。"""

    def __init__(self, host: str = "127.0.0.1", port: int = 9100, timeout: float = 5.0) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock: Optional[socket.socket] = None
        self._buffer = bytearray()

    @property
    def is_open(self) -> bool:
        return self._sock is not None

    def open(self) -> None:
        self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self._sock.settimeout(None)

    def write(self, data: bytes) -> int:
        if self._sock is None:
            raise ConnectionError("端口未打开")
        self._sock.sendall(data)
        return len(data)

    def read(self, timeout: float = 0.5) -> bytes:
        if self._sock is None:
            raise ConnectionError("端口未打开")
        if self._buffer:
            data = bytes(self._buffer)
            self._buffer.clear()
            return data
        self._sock.settimeout(timeout)
        try:
            chunk = self._sock.recv(4096)
        except socket.timeout:
            return b""
        except OSError as exc:
            raise ConnectionError(f"连接已断开: {exc}") from exc
        if not chunk:
            raise ConnectionError("对端关闭了连接")
        return chunk

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None


class LoopbackTransport(Transport):
    """本地回环：写入的字节被 :class:`dlx1b.printer.DeliPrinter` 用于离线验证帧编码。"""

    def __init__(self) -> None:
        self.written = bytearray()
        self._open = False
        self._pending = bytearray()

    @property
    def is_open(self) -> bool:
        return self._open

    def open(self) -> None:
        self._open = True

    def write(self, data: bytes) -> int:
        self.written.extend(data)
        return len(data)

    def read(self, timeout: float = 0.5) -> bytes:
        if self._pending:
            data = bytes(self._pending)
            self._pending.clear()
            return data
        time.sleep(min(timeout, 0.02))
        return b""

    def close(self) -> None:
        self._open = False
