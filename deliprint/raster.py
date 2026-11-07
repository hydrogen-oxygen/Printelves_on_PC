"""图像 → 打印机点阵：得利 SDK 的 1bpp 打包 + 半色调选项。

原厂路径（com.deli.sdk.e）：
  1. App 先把图片缩放到 384 宽（BitmapFlex.compressPic：device_type 含
     "X1B" 时用 Contants.X1_RADIO = 384），并可选做抖动
     （HPRTPrinterHelper.getBWjitterBitmap，HPRT 侧半色调模式 1）。
  2. SDK 私有 e.a(Bitmap,w,h) 打包：bpl = ceil(w/8)，逐行 getPixels，
     像素全白(-1)跳过，bit = 1 << (8 - i)，判黑条件 (R+G+B)/3 < 128。
  3. SDK 发送 1D 76 30 <mode> bplLo bplHi hLo hHi + 点阵。

所以这里提供两件事：
  * to_1bpp()：按原厂判据（均值灰度 < 128、MSB 在前、1=黑）打包；
  * 可选抖动（threshold / floyd / bayer），用来模拟 App 里 getBWjitterBitmap
    那一步（App 传的样式是 1，HPRT 侧的具体算法属 HPRT 实现，此处用
    Floyd–Steinberg 作为等价近似，见 API.md 的「未确认」一节）。
"""

from __future__ import annotations

from typing import Optional, Tuple

PRINTER_WIDTH = 384  # X1B：2 英寸 @ 203dpi = 384 点（Contants.X1_RADIO）
BYTES_PER_LINE = PRINTER_WIDTH // 8  # 48

# 8x8 Bayer 有序抖动矩阵（0..63）
_BAYER8 = [
    [0, 32, 8, 40, 2, 34, 10, 42],
    [48, 16, 56, 24, 50, 18, 58, 26],
    [12, 44, 4, 36, 14, 46, 6, 38],
    [60, 28, 52, 20, 62, 30, 54, 22],
    [3, 35, 11, 43, 1, 33, 9, 41],
    [51, 19, 59, 27, 49, 17, 57, 25],
    [15, 47, 7, 39, 13, 45, 5, 37],
    [63, 31, 55, 23, 61, 29, 53, 21],
]


def _require_pil():
    try:
        from PIL import Image  # noqa: F401
    except ImportError as exc:  # pragma: no cover - 依赖缺失时给出明确提示
        raise ImportError("需要 Pillow：pip install pillow") from exc
    return Image


def _gray_mask(img, threshold: int) -> Tuple[int, int, bytearray]:
    """原厂 mode 0 判据：bpl=ceil(w/8)，MSB 在前，1=黑，(R+G+B)/3 < 128。"""
    width, height = img.size
    bpl = (width + 7) // 8
    data = bytearray(bpl * height)
    pixels = img.convert("RGB").tobytes()
    for y in range(height):
        base = y * width * 3
        row = y * bpl
        for x in range(width):
            o = base + 3 * x
            if (pixels[o] + pixels[o + 1] + pixels[o + 2]) // 3 < threshold:
                data[row + (x >> 3)] |= 0x80 >> (x & 7)
    return width, height, data


def _floyd_steinberg(img, threshold: int = 128) -> Tuple[int, int, bytearray]:
    """误差扩散（整数系数 7/16、3/16、5/16、1/16），近似 App 的抖动步骤。"""
    width, height = img.size
    gray = img.convert("L")
    px = list(gray.getdata())
    bpl = (width + 7) // 8
    out = bytearray(bpl * height)
    for y in range(height):
        for x in range(width):
            i = y * width + x
            old = px[i]
            new = 255 if old > threshold else 0
            if new == 0:
                out[y * bpl + (x >> 3)] |= 0x80 >> (x & 7)
            err = old - new
            if x + 1 < width:
                px[i + 1] += int(err * 0.4375)
            if y + 1 < height:
                if x > 0:
                    px[i + width - 1] += int(err * 0.1875)
                px[i + width] += int(err * 0.3125)
                if x + 1 < width:
                    px[i + width + 1] += int(err * 0.0625)
    return width, height, out


def _bayer(img, threshold: int = 128) -> Tuple[int, int, bytearray]:
    """8x8 有序抖动（Bayer），适合文字/线稿。"""
    width, height = img.size
    gray = img.convert("L")
    px = list(gray.getdata())
    bpl = (width + 7) // 8
    out = bytearray(bpl * height)
    for y in range(height):
        for x in range(width):
            level = _BAYER8[y & 7][x & 7]  # 0..63
            # 把 gray 与抖动阈值比较：等价于 (gray) < (level * 4 + 2)
            if px[y * width + x] < level * 4 + 2:
                out[y * bpl + (x >> 3)] |= 0x80 >> (x & 7)
    return width, height, out


def to_1bpp(img, dither: str = "threshold", threshold: int = 128):
    """PIL 图像 → (width, height, bytes)。

    dither: ``threshold``（原厂 SDK 判据，默认）| ``floyd`` | ``bayer``。
    返回的点阵按打印机要求：每行 ceil(w/8) 字节、MSB 在前、1 表示黑。
    """
    if dither == "threshold":
        return _gray_mask(img, threshold)
    if dither == "floyd":
        return _floyd_steinberg(img, threshold)
    if dither == "bayer":
        return _bayer(img, threshold)
    raise ValueError("dither 只能是 threshold / floyd / bayer")


def fit_width(img, width: int = PRINTER_WIDTH):
    """等比缩放到打印机宽度（不放大，超过则缩）。"""
    w, h = img.size
    if w == width:
        return img
    new_h = max(1, int(round(h * width / w)))
    return img.resize((width, new_h))


# --- 文本渲染（demo 用，核心协议不依赖字体） --------------------------------
_CJK_FONTS = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "C:/Windows/Fonts/msyh.ttc",
]


def find_cjk_font() -> Optional[str]:
    import os

    for p in ([os.environ.get("DELIPRINT_FONT")] if os.environ.get("DELIPRINT_FONT") else []) + _CJK_FONTS:
        if p and os.path.exists(p):
            return p
    return None


def text_image(text: str, font_size: int = 24, width: int = PRINTER_WIDTH, line_spacing: int = 4,
               padding: int = 8, font_path: Optional[str] = None, align: str = "left"):
    """把文本渲染成 width 宽的 PIL 灰度图（自动折行、裁掉多余白边）。"""
    Image = _require_pil()
    from PIL import ImageDraw, ImageFont

    path = font_path or find_cjk_font()
    font = ImageFont.truetype(path, font_size) if path else ImageFont.load_default()

    probe = Image.new("L", (10, 10), 255)
    draw = ImageDraw.Draw(probe)
    usable = width - 2 * padding
    lines = []
    for para in text.split("\n"):
        cur = ""
        for ch in para:
            if draw.textlength(cur + ch, font=font) <= usable:
                cur += ch
            else:
                lines.append(cur)
                cur = ch
        lines.append(cur)

    heights = []
    widths = []
    for line in lines:
        box = draw.textbbox((0, 0), line, font=font)
        heights.append(box[3] - box[1])
        widths.append(box[2] - box[0])
    line_h = max(heights) if heights else font_size
    img = Image.new("L", (width, line_h * len(lines) + line_spacing * (len(lines) - 1) + 2 * padding), 255)
    d = ImageDraw.Draw(img)
    y = padding
    for line, w in zip(lines, widths):
        x = padding
        if align == "center":
            x = max(padding, (width - w) // 2)
        elif align == "right":
            x = max(padding, width - padding - w)
        d.text((x, y), line, fill=0, font=font)
        y += line_h + line_spacing

    bbox = img.getbbox()
    if bbox:
        img = img.crop((0, max(0, bbox[1] - 2), width, min(img.height, bbox[3] + 2)))
    return img
