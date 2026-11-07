"""图像准备：任意图片 / 文本 → 384 宽 1bpp 点阵 + 1440 字节分片。

DL-X1B 是 2 英寸 203dpi 热敏打印机：打印头 384 点，即 48 字节/行。

位序（由 ``cpcl/PrinterDataCore.a(Bitmap,int)`` 反汇编确认）：

* 每行 48 字节，**MSB 在前**：``bit = 1 << (7 - (x & 7))``
* ``bit = 1`` 表示**黑点**（加热），``bit = 0`` 表示白
* 灰度 = ``0.29891*R + 0.58661*G + 0.11448*B``，``gray <= 128`` 判为黑
* 整幅图像按行拼接，不插入任何头/尾（height 只用于进度与分片）
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

#: 打印头宽度（点）
PRINTER_WIDTH = 384
#: 每行字节数
BYTES_PER_LINE = PRINTER_WIDTH // 8  # 48
#: 单帧图像 payload 上限 = 48 * 30 = 1440（App 的 30 行一个 band）
MAX_CHUNK = BYTES_PER_LINE * 30  # 1440

_GRAY_R, _GRAY_G, _GRAY_B = 0.29891, 0.58661, 0.11448
_THRESHOLD = 128

#: 常见中文字体候选（按顺序探测）
_FONT_CANDIDATES = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simsun.ttc",
)


def find_cjk_font() -> Optional[str]:
    """返回系统里第一个可用的中文字体路径（找不到返回 None，回退 PIL 默认位图字体）。"""
    env = os.environ.get("DLX1B_FONT")
    if env and os.path.exists(env):
        return env
    for path in _FONT_CANDIDATES:
        if os.path.exists(path):
            return path
    return None


@dataclass
class RasterOptions:
    """转点阵的参数。"""

    #: 0 = 固定阈值，1 = Floyd–Steinberg 误差扩散，2 = 8x8 cluster-dot 抖动
    #: （三者均为原厂 PrinterDataCore 的实现，已按 smali/jadx 逐行还原）
    halftone: int = 0
    #: 阈值法的判定阈值（仅 halftone=0 生效，原厂固定 128）
    threshold: int = _THRESHOLD
    #: 原厂 PrintDataFormat(bitmap, bias) 的偏移参数，App 恒传 0
    bias: int = 0
    #: 反转黑白（例如浅底深字的模板）
    invert: bool = False


@dataclass
class PrinterImage:
    """一幅已转成打印机点阵的图像。"""

    width: int
    height: int
    data: bytes                      # 1bpp、按行、MSB 在前
    bytes_per_line: int = BYTES_PER_LINE
    halftone: int = 0
    meta: dict = field(default_factory=dict)

    @property
    def size(self) -> int:
        return len(self.data)

    def chunks(self, chunk_size: int = MAX_CHUNK) -> List[bytes]:
        """按 App 的逻辑切片：整块 chunk_size（1440=30 行），余数单独一块。"""
        return chunk_payload(self.data, chunk_size)


def chunk_payload(data: bytes, chunk_size: int = MAX_CHUNK) -> List[bytes]:
    """等价于 ``cpcl/Tools.addBytesToList(data, chunk_size)``。"""
    if chunk_size <= 0:
        raise ValueError("chunk_size 必须为正")
    return [data[i : i + chunk_size] for i in range(0, len(data), chunk_size)]


# ---------------------------------------------------------------------------
# 位图 → 1bpp
# ---------------------------------------------------------------------------
def _pil_to_pixels(img):
    """返回 (w, h, rgb_bytes)：RGB 原始字节，按 ``3*(y*w+x)`` 取像素。"""
    img = img.convert("RGB")
    w, h = img.size
    return w, h, img.tobytes()


def to_raster(img, options: Optional[RasterOptions] = None) -> PrinterImage:
    """把 PIL Image 转成 :class:`PrinterImage`（1bpp、MSB 在前、黑=1）。

    三种半色调模式与原厂 ``cpcl/PrinterDataCore.PrintDataFormat`` 的
    ``HalftoneMode`` 一一对应（0 阈值 / 1 误差扩散 / 2 抖动）。
    宽度不是 8 的倍数时按 App 的做法向上补足到整字节，右侧补白。
    """
    options = options or RasterOptions()
    if options.halftone not in (0, 1, 2):
        raise ValueError("halftone 只支持 0/1/2")

    w, h, pixels = _pil_to_pixels(img)
    width = max(w, 1)
    bpl = (width + 7) // 8
    out = bytearray(bpl * h)
    thr = options.threshold
    bias = options.bias

    def set_black(x: int, y: int) -> None:
        out[y * bpl + (x >> 3)] |= 1 << (7 - (x & 7))

    if options.halftone == 0:
        # 原厂 a()：灰度取整数截断，<= 128 判黑（getThreshold 只打日志，不参与判定）
        for y in range(h):
            base = y * w
            for x in range(w):
                o = 3 * (base + x)
                gray = int(_GRAY_R * pixels[o] + _GRAY_G * pixels[o + 1] + _GRAY_B * pixels[o + 2])
                black = gray <= thr
                if options.invert:
                    black = not black
                if black:
                    set_black(x, y)
    elif options.halftone == 1:
        # 原厂 b()：先减偏移 i（printBitmap 传 0），再做 Floyd–Steinberg 误差扩散，
        # 阈值 128；右侧 7/16、左下 3/16、下方 5/16、右下 1/16，整数截断。
        v = [[0] * w for _ in range(h)]
        for y in range(h):
            base = y * w
            row = v[y]
            for x in range(w):
                o = 3 * (base + x)
                gray = int(
                    _GRAY_R * pixels[o] + _GRAY_G * pixels[o + 1] + _GRAY_B * pixels[o + 2]
                ) & 0xFF
                row[x] = gray - bias
        _floyd_steinberg(v, w, h)
        for y in range(h):
            row = v[y]
            for x in range(w):
                if row[x] <= 128:
                    set_black(x, y)
    else:
        # 原厂 c()：v = 255 - gray + bias，黑点条件为 (v>>2) >= 8x8 cluster-dot 矩阵值
        for y in range(h):
            base = y * w
            mat = _CLUSTER8[y & 7]
            for x in range(w):
                o = 3 * (base + x)
                gray = int(
                    _GRAY_R * pixels[o] + _GRAY_G * pixels[o + 1] + _GRAY_B * pixels[o + 2]
                ) & 0xFF
                v = 255 - gray + bias
                q = v >> 2
                if q != 0 and q >= mat[x & 7]:
                    set_black(x, y)

    return PrinterImage(width=w, height=h, data=bytes(out), bytes_per_line=bpl, halftone=options.halftone)


def _floyd_steinberg(v: List[List[int]], w: int, h: int) -> None:
    """原厂 b() 的误差扩散：结果原地变成 0 / 255（整数截断，同 Java 的 (int)）。"""
    for y in range(h):
        row = v[y]
        for x in range(w):
            old = row[x]
            if old > 128:
                new = 255
            else:
                new = 0
            row[x] = new
            err = old - new
            if x + 1 < w:
                row[x + 1] += int(err * 0.4375)     # 7/16
            if y + 1 < h:
                nxt = v[y + 1]
                if x > 0:
                    nxt[x - 1] += int(err * 0.1875)  # 3/16
                nxt[x] += int(err * 0.3125)          # 5/16
                if x + 1 < w:
                    nxt[x + 1] += int(err * 0.0625)  # 1/16


#: 原厂 c() 的 8x8 cluster-dot 抖动矩阵（smali fill-array-data 原始字节）
#: 24 10 12 26 35 47 49 37 / 8 0 2 14 45 59 61 51 / 22 6 4 16 43 57 63 53 / ...
_CLUSTER8 = (
    (24, 10, 12, 26, 35, 47, 49, 37),
    (8, 0, 2, 14, 45, 59, 61, 51),
    (22, 6, 4, 16, 43, 57, 63, 53),
    (30, 20, 18, 28, 33, 41, 55, 39),
    (34, 46, 48, 36, 25, 11, 13, 27),
    (44, 58, 60, 50, 9, 1, 3, 15),
    (42, 56, 62, 52, 23, 7, 5, 17),
    (32, 40, 54, 38, 31, 21, 19, 29),
)


def _load_font(size: int, font_path: Optional[str] = None):
    from PIL import ImageFont

    path = font_path or find_cjk_font()
    if path:
        try:
            return ImageFont.truetype(path, size)
        except Exception:  # pragma: no cover
            pass
    try:
        return ImageFont.load_default(size=size)  # Pillow >= 10.1
    except TypeError:  # pragma: no cover
        return ImageFont.load_default()


def text_image(
    text: str,
    font_size: int = 24,
    width: int = PRINTER_WIDTH,
    line_spacing: int = 4,
    padding: int = 8,
    bold: bool = False,
    font_path: Optional[str] = None,
    align: str = "left",
):
    """把一段文本渲染成 ``width`` 宽的 PIL 图像（白底黑字，高度自适应）。

    支持 ``\\n`` 换行与自动折行；中文字体自动探测（可用 ``DLX1B_FONT`` 覆盖）。
    """
    from PIL import Image, ImageDraw

    font = _load_font(font_size, font_path)
    pad = max(0, padding)
    usable = max(1, width - pad * 2)

    # 先做一个够高的画布，再裁掉底部空白
    probe = Image.new("L", (width, 1), 255)
    pd = ImageDraw.Draw(probe)
    lines: List[str] = []
    for raw in str(text).split("\n"):
        if raw == "":
            lines.append("")
            continue
        cur = ""
        for ch in raw:
            test = cur + ch
            if pd.textlength(test, font=font) <= usable or not cur:
                cur = test
            else:
                lines.append(cur)
                cur = ch
        if cur:
            lines.append(cur)

    line_h = int(font_size * 1.45) + line_spacing
    height = max(line_h * max(1, len(lines)) + pad * 2, font_size + pad * 2)
    img = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(img)
    y = pad
    for line in lines:
        if align == "center":
            w = pd.textlength(line, font=font)
            x = max(pad, int((width - w) / 2))
        elif align == "right":
            w = pd.textlength(line, font=font)
            x = max(pad, int(width - pad - w))
        else:
            x = pad
        draw.text((x, y), line, font=font, fill=0, stroke_width=1 if bold else 0)
        y += line_h

    # 裁掉底部多余白边
    bbox = img.getbbox()
    if bbox:
        img = img.crop((0, 0, width, min(height, bbox[3] + pad)))
    return img.convert("RGB")


def image_from_pil(img, width: int = PRINTER_WIDTH, keep_ratio: bool = True, pad_white: bool = True):
    """把任意 PIL 图像缩放到打印头宽度（默认等比缩放 + 白底补边）。"""
    from PIL import Image

    img = img.convert("RGB")
    if keep_ratio:
        w, h = img.size
        ratio = width / float(w)
        img = img.resize((width, max(1, int(round(h * ratio)))), Image.LANCZOS)
    else:
        img = img.resize((width, img.size[1]), Image.LANCZOS)
    if pad_white and img.width < width:
        canvas = Image.new("RGB", (width, img.height), (255, 255, 255))
        canvas.paste(img, (0, 0))
        img = canvas
    return img


def image_from_file(path: str, width: int = PRINTER_WIDTH, keep_ratio: bool = True):
    """打开图片文件并缩放到打印头宽度。"""
    from PIL import Image

    with Image.open(path) as im:
        return image_from_pil(im, width=width, keep_ratio=keep_ratio)
