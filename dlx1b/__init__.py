"""dlx1b — 得力 DL-X1B（相印宝 / CORX1）蓝牙热敏打印机 Python 驱动。

核心功能（蓝牙传输 + 图像数据）全部集中在本包内，UI 只依赖本包公开 API。

协议来源：hwxyb.apk 的 smali_classes2/cpcl/ 包（com.feioou.abroad 自研 SDK），
所有常量与帧格式均由字节码还原，见同目录 ../API.md 与 ../逆向笔记。
"""

from .frame import (
    CMD,
    DEFAULT_CRC_KEY,
    Frame,
    FrameParser,
    STATUS,
    build_frame,
    crc32_keyed,
    parse_frames,
)
from .transport import (
    LoopbackTransport,
    RfcommTransport,
    SerialTransport,
    SocketTransport,
    Transport,
)
from .raster import (
    PrinterImage,
    RasterOptions,
    chunk_payload,
    image_from_file,
    image_from_pil,
    text_image,
    to_raster,
)
from .printer import DeliPrinter, PrinterError, PrinterStatus

__all__ = [
    "CMD",
    "STATUS",
    "DEFAULT_CRC_KEY",
    "Frame",
    "FrameParser",
    "build_frame",
    "crc32_keyed",
    "parse_frames",
    "Transport",
    "RfcommTransport",
    "SerialTransport",
    "SocketTransport",
    "LoopbackTransport",
    "PrinterImage",
    "RasterOptions",
    "chunk_payload",
    "image_from_file",
    "image_from_pil",
    "text_image",
    "to_raster",
    "DeliPrinter",
    "PrinterError",
    "PrinterStatus",
]

__version__ = "1.0.0"
