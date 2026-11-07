#!/usr/bin/env python3
"""命令行打印示例（默认 DL-X1B 实际协议 = 得利 Deli SDK）。

    # 真实打印（DL-X1B）
    python3 demo/print_text_cli.py --mac AA:BB:CC:DD:EE:FF --text "你好 DL-X1B"

    # 打印文件里的文字
    python3 demo/print_text_cli.py --mac AA:BB:CC:DD:EE:FF --text-file note.txt --font-size 24

    # 打印图片
    python3 demo/print_text_cli.py --mac AA:BB:CC:DD:EE:FF --image photo.png

    # 不连打印机，只把要发的字节 dump 成十六进制（离线验证协议）
    python3 demo/print_text_cli.py --text "hello" --dump frames.hex
    python3 demo/print_text_cli.py --text "hello" --dump frames.hex --protocol cpcl

    # 自动扫描并匹配 DL-X1B（不用手填 MAC）
    python3 demo/print_text_cli.py --scan                 # 只列出发现的设备
    python3 demo/print_text_cli.py --scan 10 --text "hello" --auto
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import deliprint  # noqa: E402
import dlx1b  # noqa: E402
from dlx1b.frame import parse_frames  # noqa: E402
from dlx1b.raster import RasterOptions  # noqa: E402


def read_text(args) -> str:
    text = args.text
    if args.text_file:
        with open(args.text_file, encoding="utf-8") as fh:
            text = fh.read()
    if not text:
        raise SystemExit("需要 --text / --text-file / --image 之一")
    return text


def scan_and_pick(seconds: float = 10.0):
    """扫描并打印设备表，返回最匹配的 DL-X1B（没有则 None）。"""
    try:
        devs = deliprint.scan_devices(seconds)
    except deliprint.ScanError as exc:
        raise SystemExit(f"扫描失败: {exc}")
    if not devs:
        print(f"扫描 {seconds:g}s 没有发现任何蓝牙设备。")
        return None
    print(f"发现 {len(devs)} 台设备（按匹配度排序）：")
    for dev in devs:
        print("  " + str(dev))
    best = deliprint.pick_device(devs)
    if best is not None:
        print(f"最佳匹配：{best}")
    else:
        print("没有匹配到 DL-X1B（规则：名字含 X1B、不含 BLE、Imaging 类）")
    return best


def deli_main(args) -> None:
    dither = {0: "threshold", 1: "floyd", 2: "bayer"}[args.halftone]

    if args.dump:
        t = deliprint.LoopbackTransport(ack=False)
        p = deliprint.DeliPrinter(t)
        p.connect()
        if args.density is not None:
            p.set_thickness(args.density)
        if args.image:
            from PIL import Image

            p.print_image_page(
                deliprint.fit_width(Image.open(args.image).convert("L")), dither=dither
            )
        else:
            p.print_text_page(read_text(args), dither=dither, font_size=args.font_size)
        if args.feed is not None:
            p.feed(args.feed * 24)
        p.disconnect()
        with open(args.dump, "w", encoding="ascii") as fh:
            fh.write(bytes(t.written).hex(" "))
            fh.write("\n")
        print(f"已写入 {args.dump}：{len(t.written)} 字节")
        _describe_deli(bytes(t.written))
        return

    if not args.mac:
        if not args.auto:
            raise SystemExit("真实打印需要 --mac，或使用 --auto 自动扫描匹配，或 --dump 离线模式")
        dev = scan_and_pick(args.scan if args.scan is not None else 10.0)
        if dev is None:
            raise SystemExit("未发现匹配的 DL-X1B，请确认打印机已开机/在范围内")
        args.mac = dev.mac
    print(f"连接 {args.mac} …")
    p = deliprint.DeliPrinter.connect_bt(args.mac, channel=args.channel)
    try:
        p.on_status = lambda code: print(f"\n状态 0x{code:02X}")
        if args.density is not None:
            p.set_thickness(args.density)
        if args.image:
            from PIL import Image

            p.print_image_page(
                deliprint.fit_width(Image.open(args.image).convert("L")), dither=dither
            )
        else:
            p.print_text_page(read_text(args), dither=dither, font_size=args.font_size)
        print("已发送，等待打印机完成 …")
        print("完成" if p.wait_print_finished(timeout=30) else "超时未收到完成信号（OK / AA+BB）")
    finally:
        p.disconnect()


def _describe_deli(raw: bytes) -> None:
    """打印离线字节流的结构摘要。"""
    i = 0
    steps = []
    while i < len(raw):
        if raw[i:i + 4] == bytes([0x10, 0xFF, 0xFE, 0x01]):
            steps.append(("enablePrinter", 4))
            i += 4
        elif raw[i:i + 4] == bytes([0x10, 0xFF, 0xFE, 0x45]):
            steps.append(("stopPrintJob", 4))
            i += 4
        elif raw[i:i + 2] == b"\x1b\x4a":
            steps.append((f"走纸 {raw[i + 2]} 点行", 3))
            i += 3
        elif raw[i:i + 4] == bytes([0x10, 0xFF, 0x84, 0x03]):
            steps.append(("choosePaperType(3) 2 英寸", 4))
            i += 4
        elif raw[i:i + 3] == b"\x1d\x76\x30":
            bpl = raw[i + 4] | raw[i + 5] << 8
            h = raw[i + 6] | raw[i + 7] << 8
            steps.append((f"GS v 0 模式 {raw[i + 3]}：{bpl * 8} 点宽 × {h} 行，{bpl * h} 字节", 8 + bpl * h))
            i += 8 + bpl * h
        elif raw[i:i + 1024] == bytes(1024):
            steps.append(("printerWakeup（1024 个 0x00）", 1024))
            i += 1024
        else:
            # 未知/填充字节，跳过 1 字节
            i += 1
    for n, (name, size) in enumerate(steps, 1):
        print(f"  {n:2d}. {name:<44} {size:>6d} B")


def cpcl_main(args) -> None:
    if args.image:
        img = dlx1b.image_from_file(args.image)
    else:
        img = dlx1b.text_image(read_text(args), font_size=args.font_size)
    raster = dlx1b.to_raster(img, RasterOptions(halftone=args.halftone))
    print(f"点阵: {raster.width}x{raster.height}, {len(raster.data)} 字节, {len(raster.chunks())} 帧")

    if args.dump:
        t = dlx1b.LoopbackTransport()
        p = dlx1b.DeliPrinter(t, handshake=not args.no_handshake)
        p.connected = True
        if args.density is not None:
            p.set_density(args.density)
        if args.feed is not None:
            p.set_feed_lines(args.feed)
        p.print_raster(raster)
        with open(args.dump, "w", encoding="ascii") as fh:
            fh.write(bytes(t.written).hex(" "))
            fh.write("\n")
        frames = parse_frames(bytes(t.written), key=0x35769521, verify_crc=False)
        print(f"已写入 {args.dump}：{len(t.written)} 字节 / {len(frames)} 帧")
        return

    if not args.mac:
        raise SystemExit("真实打印需要 --mac，或使用 --dump 离线模式")
    p = dlx1b.DeliPrinter.connect_bt(args.mac, channel=args.channel, handshake=not args.no_handshake)
    try:
        print("已连接，状态:", p.status())
        if args.density is not None:
            p.set_density(args.density)
        if args.feed is not None:
            p.set_feed_lines(args.feed)
        frames = p.print_raster(raster, progress=lambda v: print(f"\r打印中 {v}%", end="", flush=True))
        print(f"\r完成：{frames} 帧")
    finally:
        p.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="DL-X1B 打印示例")
    ap.add_argument("--protocol", default="deli", choices=["deli", "cpcl"], help="deli=DL-X1B 实际协议（默认）")
    ap.add_argument("--mac", default="", help="打印机 MAC（不填则用 --dump 离线模式）")
    ap.add_argument("--channel", type=int, default=1, help="RFCOMM 信道，默认 1（SPP）")
    ap.add_argument("--text", default="", help="要打印的文本")
    ap.add_argument("--text-file", default="", help="从文件读取要打印的文本")
    ap.add_argument("--image", default="", help="打印图片文件")
    ap.add_argument("--font-size", type=int, default=26)
    ap.add_argument("--halftone", type=int, default=1, choices=[0, 1, 2],
                    help="0=阈值 1=误差扩散 2=有序抖动")
    ap.add_argument("--density", type=int, default=None, help="deli=厚度(0-4) / cpcl=浓度")
    ap.add_argument("--feed", type=int, default=None, help="尾部走纸（deli=行数×24 点行 / cpcl=行）")
    ap.add_argument("--no-handshake", action="store_true", help="cpcl：跳过 CRC 会话密钥协商")
    ap.add_argument("--dump", default="", help="离线模式：把要发的字节写入该文件（十六进制）")
    ap.add_argument("--scan", nargs="?", const=10.0, type=float, default=None,
                    help="扫描蓝牙设备并列出（可跟秒数，默认 10）")
    ap.add_argument("--auto", action="store_true", help="自动匹配 DL-X1B 并连接（无需 --mac）")
    args = ap.parse_args()

    if args.auto and args.protocol != "deli":
        raise SystemExit("--auto 只支持 deli 协议（DL-X1B）；cpcl 是 X1 机型，请用 --mac")
    if args.scan is not None and not args.auto:
        scan_and_pick(args.scan)
        return
    if args.scan is not None and args.auto and not args.dump:
        # 先扫描；deli_main 里若仍没有 mac 会再扫一次，这里直接把结果写回
        dev = scan_and_pick(args.scan)
        if dev is None:
            raise SystemExit("未发现匹配的 DL-X1B")
        args.mac = dev.mac; args.auto = False

    if args.protocol == "deli":
        deli_main(args)
    else:
        cpcl_main(args)


if __name__ == "__main__":
    main()
