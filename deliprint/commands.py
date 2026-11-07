"""得利 (Deli) 打印机 SDK 命令表 —— 从 com.deli.sdk.e 逐字节还原。

所有命令都是"裸字节直接写进 SPP 串口"，没有包头/包尾/CRC（这与同 App 里
旧机型用的 cpcl 协议完全不同）。证据见 API.md 的「Deli SDK 命令」小节，
每条都标注了 smali/Java 出处。

传输层：经典蓝牙 SPP（UUID 00001101-0000-1000-8000-00805F9B34FB），
写满 1024 字节为一块、块间 sleep 1ms（com.deli.sdk.e.a(byte[],boolean)）。
"""

from __future__ import annotations

# --- 电源 / 打印作业 -------------------------------------------------------
ENABLE_PRINTER = bytes([0x10, 0xFF, 0xFE, 0x01])  # e.s()   PrintPP.enablePrinter()
STOP_PRINT_JOB = bytes([0x10, 0xFF, 0xFE, 0x45])  # e.t()   PrintPP.stopPrintJob()
BACKOFF_PAPER = bytes([0x10, 0xFF, 0xFE, 0x40])  # e.i()   PrintPP.backoffPaper()
SET_ENABLE = bytes([0x10, 0xFF, 0x60, 0x01])  # e.b(true)
SET_DISABLE = bytes([0x10, 0xFF, 0x60, 0x00])  # e.b(false)

# 唤醒：App 发 1024 个 0x00（e.g() -> b(new byte[1024])）
WAKEUP_PADDING = bytes(1024)

# --- 打印参数 -------------------------------------------------------------
HEADER_GS_V0 = 0x1D  # ESC/POS "GS v 0" 光栅位图命令头
GS_V0 = bytes([0x1D, 0x76, 0x30])  # 1D 76 30 <mode> xL xH yL yH + 数据
FEED_LINE = 0x1B  # ESC J n  (e.a(int)  -> {27,74,n})
ESC_J = bytes([0x1B, 0x4A])
PRINTER_POSITION = bytes([0x1D, 0x0C])  # e.k()   PrintPP.printerPosition()
LEARN_LABEL_GAP = bytes([0x10, 0xFF, 0x03])  # e.h()
CHECK_FLASH_BMP = bytes([0x10, 0xFF, 0x15, 0x06])  # e.l()
GET_PAPER_TYPE = bytes([0x10, 0xFF, 0x85])  # e.j()
RESET_1FB210 = bytes([0x1F, 0xB2, 0x10])  # e.r()

# --- 查询 -----------------------------------------------------------------
STATUS = bytes([0x10, 0xFF, 0x40])  # e.o()   PrintPP.printerStatus()
QUERY_VERSION = bytes([0x10, 0xFF, 0x20, 0xF1])  # e.A()
QUERY_SN = bytes([0x10, 0xFF, 0x20, 0xF2])  # e.B()
QUERY_MODEL = bytes([0x10, 0xFF, 0x20, 0xF0])  # e.C()
QUERY_BT_VERSION = bytes([0x10, 0xFF, 0x30, 0x10])  # e.z()
QUERY_BT_NAME = bytes([0x10, 0xFF, 0x30, 0x11])  # e.x()
QUERY_BT_MAC = bytes([0x10, 0xFF, 0x30, 0x12])  # e.y()
QUERY_BATTERY = bytes([0x10, 0xFF, 0x50, 0xF1])  # e.v()
QUERY_BATTERY_ALT = bytes([0x10, 0xFF, 0x50, 0xF0])  # e.w()
QUERY_SHUT_TIME = bytes([0x10, 0xFF, 0x13])  # e.u()，App 状态机里当"关机时间"读


def choose_paper_type(paper_type: int) -> bytes:
    """e.b(int) — PrintPP.choosePaperType。App 传 3（2 英寸）或 4。"""
    return bytes([0x10, 0xFF, 0x84, paper_type & 0xFF])


def set_paper_mode(continuous: bool = True) -> bytes:
    """e.b(boolean) — 10 FF 60 01 连续纸 / 10 FF 60 00 标签纸（App 未调用，保留完整性）。"""
    return SET_ENABLE if continuous else SET_DISABLE


def feed_lines(lines: int) -> bytes:
    """e.a(int) — PrintPP.printLinedots。ESC J n，n = 点行数。"""
    return bytes([0x1B, 0x4A, lines & 0xFF])


def set_shut_time(seconds: int) -> bytes:
    """e.d(int) — PrintPP.setShutTime，10 FF 12 hi lo。"""
    return bytes([0x10, 0xFF, 0x12, (seconds >> 8) & 0xFF, seconds & 0xFF])


def set_thickness(level: int) -> bytes:
    """e.e(int) — PrintPP.setThickness，10 FF 10 00 v。"""
    return bytes([0x10, 0xFF, 0x10, 0x00, level & 0xFF])


def set_current_time(y=2026, mo=1, d=1, h=0, mi=0, s=0) -> bytes:
    """e.a(7 个 int) — PrintPP.setCurrentTime，10 FF 14 02 y mo d h mi s。"""
    return bytes(
        [0x10, 0xFF, 0x14, 0x02, y & 0xFF, mo & 0xFF, d & 0xFF, h & 0xFF, mi & 0xFF, s & 0xFF]
    )


# --- 应答 -----------------------------------------------------------------
STATUS_FRAME_PREFIX = 0xFF  # 状态帧：FF xx
STATUS_PAPER_OUT = 0x04  # bit2 缺纸  -> onOutPaper()
STATUS_LOW_BATTERY = 0x08  # bit3 低压 -> onLowVal()
ACK_PRINT_FINISHED = b"OK"  # 4F 4B    -> onPrintFinish()


def raster_header(bytes_per_line: int, height: int, mode: int = 0) -> bytes:
    """GS v 0 头，e.d(Bitmap,int)/e.b(byte[],int,int,int) 的实际布局：

        1D 76 30 <mode> <bpl&0xff> <bpl>>8> <h&0xff> <h>>8>

    注意 x/y 的投影顺序与标准 ESC/POS 一致（x 低字节在前）。
    mode 取 0..3，App 恒传 0。
    """
    mode &= 0x03
    return bytes(
        [
            0x1D,
            0x76,
            0x30,
            mode,
            bytes_per_line & 0xFF,
            (bytes_per_line >> 8) & 0xFF,
            height & 0xFF,
            (height >> 8) & 0xFF,
        ]
    )

# 真机回包的另一种「任务完成」信号（本机 DL-X1B 固件 3.16YC 实测）：
# stopPrintJob 之后约 1.5s 回 0xAA，再约 0.5s 回 0xBB；与 App 期待的 ASCII "OK" 不同，
# 两种都当作「打印任务已被打印机处理完」。
JOB_START_ACK = 0xAA
JOB_DONE_ACK = 0xBB
