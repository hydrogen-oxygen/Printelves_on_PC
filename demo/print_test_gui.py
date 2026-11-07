#!/usr/bin/env python3
"""DL-X1B 打印测试界面（Tkinter）。

两种协议可选：
  * X1B Deli SDK —— DL-X1B 实际使用的协议（com.deli.sdk.PrintPP，默认）
  * X1 cpcl      —— 同 App 中旧机型 X1 的协议（包封 + CRC + 会话密钥）

用法：
    python3 demo/print_test_gui.py
    python3 demo/print_test_gui.py --mac AA:BB:CC:DD:EE:FF --protocol deli

只依赖标准库 + Pillow；协议字节全部在 deliprint/ 与 dlx1b/ 包里，UI 不拼命令。
"""

from __future__ import annotations

import argparse
import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import deliprint  # noqa: E402
import dlx1b  # noqa: E402
from dlx1b.raster import RasterOptions  # noqa: E402

PROTO_DELI = "X1B Deli SDK（实际协议）"
PROTO_CPCL = "X1 cpcl（旧机型）"


class App:
    def __init__(self, root: tk.Tk, mac: str = "", protocol: str = "deli",
                 auto_scan: bool = False) -> None:
        self.root = root
        self.printer = None           # deliprint.DeliPrinter 或 dlx1b.DeliPrinter
        self.logq: "queue.Queue[str]" = queue.Queue()
        self._devices: "list[deliprint.Device]" = []
        self._scanning = False

        root.title("DL-X1B 打印测试台（逆向复刻，非官方）")
        root.geometry("900x660")
        root.minsize(860, 560)
        self._build(mac, protocol)
        self._poll_log()
        if auto_scan:
            self.root.after(300, self.auto_connect)

    # ------------------------------------------------------------------ UI
    def _build(self, mac: str, protocol: str) -> None:
        pad = {"padx": 6, "pady": 4}

        top = ttk.LabelFrame(self.root, text="1. 连接（经典蓝牙 SPP）")
        top.pack(fill="x", **pad)
        ttk.Label(top, text="协议").grid(row=0, column=0, sticky="w")
        self.proto_var = tk.StringVar(value=PROTO_DELI if protocol == "deli" else PROTO_CPCL)
        cb = ttk.Combobox(top, width=24, state="readonly", textvariable=self.proto_var,
                          values=[PROTO_DELI, PROTO_CPCL])
        cb.grid(row=0, column=1, sticky="w", padx=(4, 12))
        cb.bind("<<ComboboxSelected>>", lambda _e: self._sync_protocol())

        ttk.Label(top, text="MAC").grid(row=0, column=2, sticky="e")
        self.mac_var = tk.StringVar(value=mac)
        ttk.Entry(top, textvariable=self.mac_var, width=20).grid(row=0, column=3, sticky="w")
        ttk.Label(top, text="信道").grid(row=0, column=4, sticky="e")
        self.chan_var = tk.StringVar(value="1")
        ttk.Spinbox(top, from_=1, to=30, width=4, textvariable=self.chan_var).grid(row=0, column=5, sticky="w")
        self.btn_conn = ttk.Button(top, text="连接", command=self.toggle_connect)
        self.btn_conn.grid(row=0, column=6, padx=8)
        self.handshake_var = tk.BooleanVar(value=True)
        self.cb_handshake = ttk.Checkbutton(top, text="cpcl 会话密钥握手", variable=self.handshake_var)
        self.cb_handshake.grid(row=0, column=7, sticky="w")

        scan = ttk.Frame(top)
        scan.grid(row=1, column=0, columnspan=8, sticky="we", pady=(4, 0))
        self.scan_btn = ttk.Button(scan, text="扫描设备", command=self.start_scan)
        self.scan_btn.pack(side="left")
        ttk.Label(scan, text="时长").pack(side="left", padx=(8, 2))
        self.scan_secs_var = tk.StringVar(value="10")
        ttk.Spinbox(scan, from_=2, to=30, width=4, textvariable=self.scan_secs_var).pack(side="left")
        ttk.Label(scan, text="秒").pack(side="left", padx=(0, 8))
        self.dev_var = tk.StringVar()
        self.dev_cb = ttk.Combobox(scan, width=42, state="readonly", textvariable=self.dev_var)
        self.dev_cb.pack(side="left", padx=4)
        self.dev_cb.bind("<<ComboboxSelected>>", self._on_device_selected)
        self.auto_btn = ttk.Button(scan, text="自动匹配并连接", command=self.auto_connect)
        self.auto_btn.pack(side="left", padx=4)

        self.status_var = tk.StringVar(value="未连接")
        ttk.Label(top, textvariable=self.status_var, foreground="#666").grid(
            row=2, column=0, columnspan=8, sticky="w"
        )

        mid = ttk.LabelFrame(self.root, text="2. 参数")
        mid.pack(fill="x", **pad)
        ttk.Label(mid, text="浓度/厚度").grid(row=0, column=0, sticky="w")
        self.density_var = tk.IntVar(value=2)
        ttk.Scale(mid, from_=0, to=4, orient="horizontal", variable=self.density_var, length=120).grid(
            row=0, column=1, sticky="w"
        )
        ttk.Label(mid, textvariable=self.density_var, width=3).grid(row=0, column=2, sticky="w")
        ttk.Button(mid, text="应用", command=self.apply_density).grid(row=0, column=3, padx=6)

        ttk.Label(mid, text="走纸").grid(row=0, column=4, sticky="e")
        self.feed_var = tk.IntVar(value=4)
        ttk.Spinbox(mid, from_=0, to=200, width=5, textvariable=self.feed_var).grid(row=0, column=5, sticky="w")
        ttk.Button(mid, text="应用走纸", command=self.apply_feed).grid(row=0, column=6, padx=6)

        ttk.Button(mid, text="查询状态", command=self.query_status).grid(row=1, column=0, pady=4, sticky="w")
        ttk.Button(mid, text="打印自检页", command=self.self_test).grid(row=1, column=1, columnspan=2, sticky="w")
        ttk.Button(mid, text="恢复默认参数", command=self.default_params).grid(row=1, column=3, sticky="w")
        ttk.Button(mid, text="读设备信息", command=self.read_info).grid(row=1, column=4, sticky="w")

        body = ttk.LabelFrame(self.root, text="3. 打印内容")
        body.pack(fill="both", expand=True, **pad)
        self.text = tk.Text(body, height=7, wrap="word")
        self.text.insert("1.0", "相印宝 DL-X1B\nPython 逆向复刻\n中英文混合 Test 123")
        self.text.pack(fill="both", expand=True, padx=6, pady=6)

        row = ttk.Frame(body)
        row.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Label(row, text="字号").pack(side="left")
        self.font_var = tk.IntVar(value=26)
        ttk.Spinbox(row, from_=10, to=64, width=5, textvariable=self.font_var).pack(side="left", padx=(2, 10))
        ttk.Label(row, text="半色调").pack(side="left")
        self.half_var = tk.StringVar(value="1-误差扩散")
        ttk.Combobox(row, width=12, state="readonly", textvariable=self.half_var,
                     values=["0-阈值", "1-误差扩散", "2-有序抖动"]).pack(side="left", padx=(2, 10))
        ttk.Button(row, text="打印文本", command=self.print_text).pack(side="left", padx=4)
        ttk.Button(row, text="打印图片…", command=self.print_file).pack(side="left", padx=4)
        ttk.Button(row, text="预览", command=self.preview).pack(side="left", padx=4)

        tail = ttk.LabelFrame(self.root, text="日志")
        tail.pack(fill="both", expand=True, **pad)
        self.log = tk.Text(tail, height=8, state="disabled")
        self.log.pack(fill="both", expand=True, padx=4, pady=4)

        self._sync_protocol()

    def _sync_protocol(self) -> None:
        deli = self._is_deli()
        self.cb_handshake.configure(state="disabled" if deli else "normal")

    def _is_deli(self) -> bool:
        return self.proto_var.get().startswith("X1B")

    # --------------------------------------------------------------- 日志
    def _log(self, msg: str) -> None:
        self.logq.put(msg)

    def _poll_log(self) -> None:
        try:
            while True:
                msg = self.logq.get_nowait()
                self.log.configure(state="normal")
                self.log.insert("end", msg + "\n")
                self.log.see("end")
                self.log.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(120, self._poll_log)

    # --------------------------------------------------------------- 扫描
    def _scan_seconds(self) -> float:
        try:
            return max(1.0, float(self.scan_secs_var.get()))
        except ValueError:
            return 10.0

    def start_scan(self) -> None:
        """后台扫描蓝牙设备，自动按 App 规则匹配 DL-X1B 并预选。"""
        if self._scanning:
            return
        self._scanning = True
        self.scan_btn.configure(state="disabled")
        self.auto_btn.configure(state="disabled")
        secs = self._scan_seconds()
        self._log(f"开始扫描（{secs:g}s）…过滤规则：名字含 X1B / 不含 BLE / Imaging 类")
        if sys.platform.startswith("win"):
            self._log(
                "Windows 扫描后端：系统蓝牙 API（无需 PyBluez）；若没扫到打印机，"
                "请先在「设置 → 蓝牙和其他设备」里配对它"
            )

        def work() -> None:
            try:
                devs = deliprint.scan_devices(secs)
                self._devices = devs
                self._log(f"扫描完成：发现 {len(devs)} 台设备")
                for d in devs:
                    self._log("  " + str(d))
                matched = deliprint.rank_devices(devs)
                self._log(f"匹配到 {len(matched)} 台 DL-X1B 系列打印机")
                self.root.after(0, lambda: self._fill_devices(devs))
            except Exception as exc:
                self._log(f"扫描失败: {exc}")
            finally:
                self._scanning = False
                self.root.after(0, self._scan_done)

        threading.Thread(target=work, daemon=True).start()

    def _scan_done(self) -> None:
        self.scan_btn.configure(state="normal")
        self.auto_btn.configure(state="normal")

    def _set_device(self, dev) -> None:
        """把设备填进下拉框与 MAC 输入框（下拉框显示 MAC + 名字）。"""
        label = f"{dev.mac_display}  {dev.name}".strip() if dev.name else dev.mac_display
        values = list(self.dev_cb.cget("values"))
        if label not in values:
            values.append(label)
            self.dev_cb.configure(values=values)
        self.dev_var.set(label)
        self.mac_var.set(dev.mac_display)

    def _fill_devices(self, devs) -> None:
        values = [
            f"{d.mac_display}  {d.name}".strip() if d.name else d.mac_display for d in devs
        ]
        self.dev_cb.configure(values=values)
        best = deliprint.pick_device(devs)
        if best is not None:
            self._set_device(best)
            self._log(f"自动预选：{best}")
        else:
            self.dev_var.set("")
            hint = (
                "；并先在「设置 → 蓝牙和其他设备」里配对它"
                if sys.platform.startswith("win")
                else ""
            )
            self._log("没有匹配到 DL-X1B：确认打印机已开机、在范围内、未被占用" + hint)

    def _on_device_selected(self, _event=None) -> None:
        text = self.dev_var.get().strip()
        mac = text.split()[0] if text else ""
        if mac:
            self.mac_var.set(mac)
            self._log(f"选中设备 {text}")

    def auto_connect(self) -> None:
        """扫描 → 匹配 DL-X1B → 直接连接（没有扫描结果时先扫一轮）。"""
        if self._connected():
            messagebox.showinfo("提示", "已经连接，先断开再自动连接")
            return
        text = self.dev_var.get().strip()
        mac = text.split()[0] if text else self.mac_var.get().strip()
        if mac:
            self._log(f"自动连接选中设备 {mac}")
            self._connect_async(mac)
            return

        secs = self._scan_seconds()
        self.status_var.set("扫描中…")
        self.auto_btn.configure(state="disabled")
        self._log(f"没有选中设备，先扫描 {secs:g}s 再自动匹配…")

        def work() -> None:
            try:
                dev = deliprint.find_printer(secs)
            except Exception as exc:
                self._log(f"扫描失败: {exc}")
                self.status_var.set("未连接")
                self.root.after(0, lambda: self.auto_btn.configure(state="normal"))
                return
            if dev is None:
                hint = (
                    "；并先在「设置 → 蓝牙和其他设备」里配对它"
                    if sys.platform.startswith("win")
                    else ""
                )
                self._log(
                    "未发现匹配的 DL-X1B：确认打印机已开机、在范围内、且本机蓝牙已开启" + hint
                )
                self.status_var.set("未发现打印机")
                self.root.after(0, lambda: self.auto_btn.configure(state="normal"))
                return
            self._log(f"匹配成功：{dev}")
            self.root.after(0, lambda: self._set_device(dev))
            self.root.after(0, lambda: self.auto_btn.configure(state="normal"))
            self._connect_worker(dev.mac, int(self.chan_var.get() or 1),
                                 self.handshake_var.get(), self._is_deli())

        threading.Thread(target=work, daemon=True).start()

    # --------------------------------------------------------------- 连接
    def _connected(self) -> bool:
        p = self.printer
        if p is None:
            return False
        if isinstance(p, deliprint.DeliPrinter):
            return p.is_open
        return bool(p.connected)

    def toggle_connect(self) -> None:
        if self._connected():
            try:
                if isinstance(self.printer, deliprint.DeliPrinter):
                    self.printer.disconnect()
                else:
                    self.printer.close()
            except Exception as exc:
                self._log(f"断开异常: {exc}")
            self.printer = None
            self.btn_conn.configure(text="连接")
            self.status_var.set("未连接")
            self._log("已断开")
            return

        mac = self.mac_var.get().strip()
        if not mac:
            messagebox.showwarning("提示", "请先点「扫描设备」或填写打印机蓝牙 MAC")
            return
        self._connect_async(mac)

    def _connect_async(self, mac: str) -> None:
        self.btn_conn.configure(state="disabled")
        self.status_var.set("连接中…")
        deli = self._is_deli()
        channel = int(self.chan_var.get() or 1)
        handshake = self.handshake_var.get()
        threading.Thread(
            target=self._connect_worker, args=(mac, channel, handshake, deli), daemon=True
        ).start()

    def _connect_worker(self, mac: str, channel: int, handshake: bool, deli: bool) -> None:
        try:
            if deli:
                p = deliprint.DeliPrinter.connect_bt(mac, channel=channel)
                p.on_status = lambda code: self._log(
                    f"← 状态 0x{code:02X}" + self._status_text(code)
                )
                p.on_print_finished = lambda: self._log("← 打印机报告打印完成 (OK)")
                p.on_receive = lambda d: self._log(f"← {d.hex(' ')}")
            else:
                p = dlx1b.DeliPrinter.connect_bt(mac, channel=channel, handshake=handshake)
                p.on_frame = lambda f: self._log(f"← {f.hex(24)}")
            self.printer = p
            self._log(f"已连接 {mac}（{'Deli SDK' if deli else 'cpcl'}）")
            self.status_var.set(f"已连接 {mac}")
            self.root.after(0, lambda: self.btn_conn.configure(text="断开", state="normal"))
            if not deli:
                self._query_status_async()
        except Exception as exc:
            self._log(f"连接失败: {exc}")
            self.status_var.set("连接失败")
            self.root.after(0, lambda: self.btn_conn.configure(state="normal"))

    @staticmethod
    def _status_text(code: int) -> str:
        parts = []
        if code == 0:
            return "（正常）"
        if code & deliprint.STATUS_PAPER_OUT:
            parts.append("缺纸")
        if code & deliprint.STATUS_LOW_BATTERY:
            parts.append("电池电量低")
        if code & 0x01:
            parts.append("正在打印")
        return "（" + "/".join(parts) + "）" if parts else "（未识别）"

    def _need_port(self) -> bool:
        if not self._connected():
            messagebox.showwarning("提示", "请先连接打印机")
            return False
        return True

    def _guarded(self, fn, *a, **kw) -> None:
        def work() -> None:
            try:
                fn(*a, **kw)
            except Exception as exc:
                self._log(f"操作失败: {exc}")

        threading.Thread(target=work, daemon=True).start()

    # --------------------------------------------------------------- 参数
    def apply_density(self) -> None:
        if not self._need_port():
            return
        v = int(self.density_var.get())
        self._log(f"设置浓度/厚度 {v}")
        if self._is_deli():
            self._guarded(self.printer.set_thickness, v)
        else:
            self._guarded(self.printer.set_density, v)

    def apply_feed(self) -> None:
        if not self._need_port():
            return
        n = int(self.feed_var.get())
        self._log(f"走纸 {n}")
        if self._is_deli():
            self._guarded(self.printer.feed, n * 24)  # n 行 ≈ n*24 点行
        else:
            self._guarded(self.printer.set_feed_lines, n)

    def _query_status_async(self) -> None:
        if not self._connected():
            return

        def work() -> None:
            try:
                st = self.printer.status()  # type: ignore[union-attr]
                self._log(f"状态: {st.name} (code={st.code})")
                self.status_var.set(f"已连接 — {st.name}")
            except Exception as exc:
                self._log(f"状态查询失败: {exc}")

        threading.Thread(target=work, daemon=True).start()

    def query_status(self) -> None:
        if not self._need_port():
            return
        if self._is_deli():
            self._log("查询状态 …")

            def work() -> None:
                try:
                    code = self.printer.read_status()
                    if code < 0:
                        self._log("状态查询：没有应答（打印机可能在休眠）")
                    else:
                        self._log(f"状态 0x{code:02X}{self._status_text(code)}")
                        self.status_var.set(f"已连接 {self.mac_var.get().strip()} — 0x{code:02X}")
                except Exception as exc:
                    self._log(f"状态查询失败: {exc}")

            self._guarded(work)
        else:
            self._query_status_async()

    def self_test(self) -> None:
        if not self._need_port():
            return
        self._log("打印自检页")
        if self._is_deli():
            txt = "DL-X1B 自检\n------------\n协议: Deli SDK\n宽度: 384 dots\n中英文 Test 123\n------------"
            self._guarded(self.printer.print_text_page, txt, dither=self._dither())
        else:
            self._guarded(self.printer.self_test)

    def read_info(self) -> None:
        """同步读型号/版本/SN/电量/关机时间（真机实测的 5 条查询）。"""
        if not self._need_port():
            return
        if not self._is_deli():
            self._log("读设备信息只支持 Deli 协议")
            return

        def work() -> None:
            try:
                self._log(f"型号: {self.printer.read_model() or '(无应答)'}")
                self._log(f"固件: {self.printer.read_version() or '(无应答)'}")
                self._log(f"SN:   {self.printer.read_sn() or '(无应答)'}")
                bat = self.printer.read_battery()
                self._log(f"电量: {bat}%" if bat >= 0 else "电量: (无应答)")
                shut = self.printer.read_shut_time()
                self._log(f"自动关机: {shut} 分钟" if shut >= 0 else "自动关机: (无应答)")
            except Exception as exc:
                self._log(f"读设备信息失败: {exc}")

        self._guarded(work)

    def default_params(self) -> None:
        if not self._need_port():
            return
        self._log("恢复默认参数")
        if self._is_deli():
            self._guarded(self._deli_defaults)
        else:
            self._guarded(self.printer.default_params)

    def _deli_defaults(self) -> None:
        self.printer.choose_paper_type(3)  # 2 英寸
        self.printer.set_thickness(2)
        self.printer.flush_input()

    # --------------------------------------------------------------- 打印
    def _half(self) -> int:
        return int(self.half_var.get().split("-")[0])

    def _paper_type(self) -> int:
        # 3 = 2 英寸（DL-X1B）；4 英寸机型传 4
        return 3

    def _dither(self) -> str:
        return {0: "threshold", 1: "floyd", 2: "bayer"}[self._half()]

    def print_text(self) -> None:
        if not self._need_port():
            return
        txt = self.text.get("1.0", "end").rstrip("\n")
        if not txt.strip():
            return
        size = int(self.font_var.get())
        self._log(f"打印文本（{len(txt)} 字，字号 {size}，半色调 {self.half_var.get()}）")
        if self._is_deli():
            self._guarded(self.printer.print_text_page, txt, dither=self._dither(), font_size=size)
            return

        def work() -> None:
            img = dlx1b.text_image(txt, font_size=size)
            raster = dlx1b.to_raster(img, RasterOptions(halftone=self._half()))
            frames = self.printer.print_raster(raster, progress=lambda v: self._log(f"进度 {v}%"))
            self._log(f"完成，共 {frames} 帧 / {len(raster.data)} 字节")

        self._guarded(work)

    def print_file(self) -> None:
        if not self._need_port():
            return
        path = filedialog.askopenfilename(
            title="选择图片", filetypes=[("图片", "*.png *.jpg *.jpeg *.bmp *.gif *.webp"), ("全部", "*.*")]
        )
        if not path:
            return
        self._log(f"打印图片 {path}（半色调 {self.half_var.get()}）")
        if self._is_deli():
            from PIL import Image

            def work() -> None:
                img = deliprint.fit_width(Image.open(path).convert("L"), deliprint.PRINTER_WIDTH)
                self.printer.print_image_page(
                    img, dither=self._dither(), paper_type=self._paper_type()
                )
                self._log(f"已发送图片点阵（{img.width}x{img.height}）")

            self._guarded(work)
            return

        def work_cpcl() -> None:
            img = dlx1b.image_from_file(path)
            raster = dlx1b.to_raster(img, RasterOptions(halftone=self._half()))
            frames = self.printer.print_raster(raster, progress=lambda v: self._log(f"进度 {v}%"))
            self._log(f"完成，共 {frames} 帧 / {len(raster.data)} 字节")

        self._guarded(work_cpcl)

    def preview(self) -> None:
        """把即将发送的点阵渲染成预览窗口（不发数据）。"""
        txt = self.text.get("1.0", "end").rstrip("\n")
        size = int(self.font_var.get())
        if self._is_deli():
            img = deliprint.text_image(txt, font_size=size)
            w, h, data = deliprint.to_1bpp(img, dither=self._dither())
            self._show_bits(w, h, (w + 7) // 8, data)
        else:
            img = dlx1b.text_image(txt, font_size=size)
            r = dlx1b.to_raster(img, RasterOptions(halftone=self._half()))
            self._show_bits(r.width, r.height, r.bytes_per_line, r.data)

    def _show_bits(self, w: int, h: int, bpl: int, data) -> None:
        from PIL import Image, ImageTk

        # 用 L 模式而不是 1 位模式：部分 Tk/Xwayland 环境把 "1" 模式渲染成黑屏
        preview = Image.new("L", (bpl * 8, h), 255)
        px = preview.load()
        for y in range(h):
            for x in range(w):
                bit = (data[y * bpl + (x >> 3)] >> (7 - (x & 7))) & 1
                px[x, y] = 0 if bit else 255
        scale = max(1, min(2, 700 // max(1, w)))
        preview = preview.resize((w * scale, h * scale), Image.NEAREST)
        # 只保留实际宽度；转 RGB，避免调色板/1 位图的显示问题
        preview = preview.crop((0, 0, w * scale, h * scale)).convert("RGB")

        win = tk.Toplevel(self.root)
        win.title(f"点阵预览 {w}x{h}（{len(data)} 字节，{w}×{h} 点）")
        win.configure(bg="white")
        photo = ImageTk.PhotoImage(preview)
        label = tk.Label(win, image=photo, bg="white", bd=1, relief="solid",
                         highlightthickness=0, padx=8, pady=8)
        label.image = photo
        label.pack(fill="both", expand=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mac", default="", help="打印机蓝牙 MAC，如 AA:BB:CC:DD:EE:FF")
    ap.add_argument("--protocol", default="deli", choices=["deli", "cpcl"], help="deli=DL-X1B 实际协议")
    ap.add_argument("--auto", action="store_true", help="启动后自动扫描、匹配 DL-X1B 并连接")
    args = ap.parse_args()
    root = tk.Tk()
    App(root, mac=args.mac, protocol=args.protocol, auto_scan=args.auto)
    root.mainloop()


if __name__ == "__main__":
    main()
