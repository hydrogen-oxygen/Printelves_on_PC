#!/usr/bin/env python3
"""把抓到的/导出的 DL-X1B 字节流（hex 文本）解析成帧表。

用途：
  * 拿 ``print_text_cli.py --protocol cpcl --dump out.hex`` 的结果自检 cpcl 帧格式；
  * 真机抓包（蓝牙 HCI log、或把打印机接到 TCP 网关）后，核对命令与应答；
  * 排查 CRC 密钥：密钥不对时所有帧都会显示 crc=BAD。

用法：
    python3 demo/decode_dump.py frames.hex
    python3 demo/decode_dump.py frames.hex --key 0      # 协商后的会话密钥
    cat frames.hex | python3 demo/decode_dump.py -      # 从 stdin 读
"""

from __future__ import annotations

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dlx1b.frame import CMD, DEFAULT_CRC_KEY, STATUS, FrameParser  # noqa: E402


def load_hex(text: str) -> bytes:
    """接受 "02 19 01 ..." / "0x02,0x19" / 连续十六进制，忽略注释。"""
    text = re.sub(r"#[^\n]*", " ", text)          # 去掉行内注释
    text = re.sub(r"0[xX]", " ", text)
    digits = re.sub(r"[^0-9a-fA-F]", "", text)
    if len(digits) % 2:
        digits = digits[:-1]
    return bytes.fromhex(digits)


def describe(frame) -> str:
    """把 payload 解释成人能读的语义（尽力而为，不认识的原样返回）。"""
    cmd, data = frame.cmd1, frame.payload
    if cmd == CMD.IMAGE:
        lines = len(data) // 48
        return f"点阵 {len(data)}B ≈ {lines} 行"
    if cmd in (CMD.VERSION, CMD.MODEL, CMD.BT_MAC, CMD.SN,
               CMD.ELECTRICITY, CMD.TEMP, CMD.BOARD_VER) and data:
        return repr(data.decode("ascii", "replace").strip("\x00 "))
    if cmd in (CMD.STATUS, CMD.STATUS + 1) and data:
        code = STATUS.decode(data[0])
        return f"0x{data[0]:02X} → {STATUS.name(code)}"
    if cmd in (CMD.DENSITY, CMD.DENSITY_Q, CMD.DENSITY_Q + 1) and data:
        return f"浓度 {data[0]}"
    if cmd == CMD.FEED_LINE and len(data) >= 2:
        points = int.from_bytes(data[:2], "little")
        return f"{points} 点 ≈ {points / 24:.0f} 行"
    if cmd == CMD.HEAD_LINE and len(data) >= 2:
        points = int.from_bytes(data[:2], "little")
        return f"留白 {points} 点 ≈ {points / 24:.0f} 行"
    if cmd == CMD.OFF_TIME and len(data) >= 2:
        return f"{int.from_bytes(data[:2], 'little')} 秒"
    if cmd in (CMD.CRC_KEY, CMD.CRC_KEY + 1) and data:
        code = data[0]
        return f"crc=le32(0x{int.from_bytes(data[:4], 'little'):08X}) 应答码 {code}" \
            if len(data) >= 4 else f"应答码 {code}"
    if cmd == CMD.DEFAULT_PARA:
        return "恢复默认参数"
    if cmd == CMD.SELF_TEST:
        return "打印自检页"
    return ""


def main() -> None:
    ap = argparse.ArgumentParser(description="解析 DL-X1B 帧字节流")
    ap.add_argument("path", help="hex 文本文件，'-' 表示 stdin")
    ap.add_argument("--key", default=hex(DEFAULT_CRC_KEY),
                    help="CRC 会话密钥（默认 0x35769521；协商后一般传 0）")
    args = ap.parse_args()

    raw_text = sys.stdin.read() if args.path == "-" else open(args.path, encoding="utf-8", errors="ignore").read()
    data = load_hex(raw_text)
    key = int(args.key, 0) & 0xFFFFFFFF
    frames = FrameParser(key).feed(data)

    print(f"输入 {len(data)} 字节，解析出 {len(frames)} 帧，CRC key=0x{key:08X}")
    print(f"{'#':>3} {'cmd1':>6} {'cmd2':>5} {'len':>5}  {'crc':>4}  {'命令':<24} 语义")
    bad = 0
    for i, f in enumerate(frames):
        crc = "ok" if f.crc_ok else "BAD"
        bad += 0 if f.crc_ok else 1
        print(f"{i:>3} 0x{f.cmd1:02X}   {f.cmd2:>5} {len(f.payload):>5}  {crc:>4}  "
              f"{CMD.name(f.cmd1):<24} {describe(f)}")
    if not frames and data:
        print("一帧都没解析出来：检查是否 hex 文本、是否被截断，"
              "或换 --key（真机密钥协商后通常为 0）。")
    if bad:
        print(f"\n注意：{bad} 帧 CRC 校验失败 —— 多半是 --key 不对（真机协商后通常为 0）。")
    total = sum(len(f.payload) for f in frames if f.cmd1 == CMD.IMAGE)
    if total:
        print(f"图像数据合计 {total} 字节 = {total / 48:.0f} 行 = {total / 48 / 30:.2f} 个 30 行 band")


if __name__ == "__main__":
    main()
