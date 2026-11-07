"""deliprint —— 得力 DL-X1B（相印宝）蓝牙热敏打印机的 Python 驱动。

扫描/连接在 Windows 上不需要 PyBluez 或 pywin32：扫描用系统蓝牙 API
（bthprops），连接用 winsock 的 RFCOMM，见 `deliprint.winbt`。

这是**实际机型走的协议**：App 内部 `com.feioou.abroad.printutils.AYPrintUtil`
→ `com.deli.sdk.PrintPP`（得利自研 SDK），经典蓝牙 SPP + 裸字节命令流，
图像用标准 ESC/POS `GS v 0`（1D 76 30）发送未压缩 1bpp 点阵，384 点宽。

    from deliprint import DeliPrinter, LoopbackTransport
    p = DeliPrinter(LoopbackTransport()); p.connect()
    p.print_text_page("hello DL-X1B")

同仓库的 `dlx1b` 包实现的是同 App 中**旧机型 X1** 用的 cpcl 协议
（有 02…03 包封 + CRC32 + 会话密钥握手），DL-X1B 不用它。
"""

from .commands import (  # noqa: F401
    ACK_PRINT_FINISHED,
    STATUS_FRAME_PREFIX,
    STATUS_LOW_BATTERY,
    STATUS_PAPER_OUT,
    raster_header,
)
from .discovery import (  # noqa: F401
    APP_NAME_PATTERNS,
    BluezCtlScanner,
    Device,
    ScanError,
    Scanner,
    WindowsScanner,
    default_scanner,
    find_printer,
    is_x1b,
    pick_device,
    rank_devices,
    scan_devices,
    scan_printers,
    score_device,
)
from .printer import CHUNK, DeliPrinter, PrinterError  # noqa: F401
from .raster import (  # noqa: F401
    BYTES_PER_LINE,
    PRINTER_WIDTH,
    find_cjk_font,
    fit_width,
    text_image,
    to_1bpp,
)
from .transport import (  # noqa: F401
    BtSppTransport,
    LoopbackTransport,
    SerialTransport,
    SocketTransport,
    Transport,
)

__version__ = "1.0.0"

__all__ = [
    "DeliPrinter",
    "PrinterError",
    "Device",
    "Scanner",
    "BluezCtlScanner",
    "WindowsScanner",
    "default_scanner",
    "ScanError",
    "scan_devices",
    "scan_printers",
    "find_printer",
    "rank_devices",
    "pick_device",
    "score_device",
    "is_x1b",
    "APP_NAME_PATTERNS",
    "Transport",
    "BtSppTransport",
    "SocketTransport",
    "SerialTransport",
    "LoopbackTransport",
    "PRINTER_WIDTH",
    "BYTES_PER_LINE",
    "CHUNK",
    "to_1bpp",
    "text_image",
    "fit_width",
    "find_cjk_font",
    "raster_header",
    "STATUS_PAPER_OUT",
    "STATUS_LOW_BATTERY",
    "STATUS_FRAME_PREFIX",
    "ACK_PRINT_FINISHED",
]
