#!/usr/bin/env python3
"""
Steam Offline Archiver - Tkinter GUI 启动器（零依赖）
用法: python archiver_gui.py
"""
import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "gui_config.json")

# (key, 显示名, 脚本文件名, 默认勾选)
STEPS = [
    ("appdetails",  "多语言游戏详情 (appdetails)",  "fetch_appdetails.py",  True),
    ("appreviews",  "玩家评论 + 头像 (appreviews)", "fetch_appreviews.py",  True),
    ("playercount", "在线人数快照 (playercount)",   "fetch_playercount.py", True),
    ("news",         "游戏新闻 RSS (news)",              "fetch_news.py",         True),
    ("news_details", "新闻详情（中文正文，多语言）",     "fetch_news_details.py", True),
    ("steamspy",    "SteamSpy 数据 (拥有量/标签)",   "fetch_steamspy.py",    True),
    ("storebrowse", "IStoreBrowseService (官方标签)","fetch_storebrowse.py", True),
    ("protondb",    "ProtonDB (Steam Deck 兼容性)",  "fetch_protondb.py",    True),
    ("achievements","全球成就解锁率 (无 Key)",       "fetch_achievements_global.py", True),
    ("reviewhistogram","评论时间分布 (舆论生命周期)","fetch_reviewhistogram.py", True),
    ("market",       "社区市场物品 (卡牌/表情/背景)", "fetch_market_items.py", True),
    ("pcgamingwiki", "PCGamingWiki (DRM/引擎/云同步)","fetch_pcgamingwiki.py", True),
    ("html",        "生成离线 HTML",                 "steam_archiver.py",    True),
]

LANGS_ALL = ["english", "schinese", "tchinese", "japanese", "koreana",
             "russian", "french", "german", "spanish", "brazilian",
             "italian", "thai", "vietnamese"]
LANGS_MAIN = ["english", "schinese", "tchinese", "japanese"]


def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def open_folder(path):
    """跨平台打开文件夹"""
    if not path or not os.path.exists(path):
        return
    try:
        if sys.platform == "win32":
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception as e:
        print(f"⚠️ 打开目录失败: {e}")


class ArchiverGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Steam Offline Archiver · 归档启动器")
        # 尺寸兼容矩阵：
        #   1024×768 屏（Win 任务栏 ~48px，可用高约 720px）→ 默认 1000×680 稳妥不被挡
        #   最小 800×560（老笔记本兼容 + 任务栏留空间）
        #   有滚动条兜底，内容再长也能滚
        self.root.geometry("1000x680")
        self.root.minsize(800, 560)
        self.config = load_config()
        self.current_proc = None
        self.log_queue = queue.Queue()
        self.running = False
        self.abort_flag = False
        self.start_time = 0
        self._build_ui()
        self._load_from_config()
        self._poll_log_queue()

    def _build_ui(self):
        pad = 8

        # 滚动容器：Canvas + Vertical Scrollbar，内部放真正的 main Frame
        # 兼容 1024×768 及以下低分辨率，让内容长度不受窗口高度限制
        container = ttk.Frame(self.root)
        container.pack(fill=tk.BOTH, expand=True)

        canvas = tk.Canvas(container, highlightthickness=0, bg="#2a475e")
        vscroll = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vscroll.set)

        vscroll.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        main = ttk.Frame(canvas, padding=pad)
        main_window = canvas.create_window((0, 0), window=main, anchor="nw", tags="main_frame")

        # main 内容变化时，更新 canvas 的可滚动区域
        def _on_main_configure(event):
            canvas.configure(scrollregion=canvas.bbox("all"))
        main.bind("<Configure>", _on_main_configure)

        # canvas 尺寸变化时，让内部 main Frame 宽度跟随，避免横向滚动条
        def _on_canvas_configure(event):
            canvas.itemconfig("main_frame", width=event.width)
        canvas.bind("<Configure>", _on_canvas_configure)

        # 鼠标滚轮支持：Windows / macOS 用 <MouseWheel>，Linux 用 <Button-4>/<Button-5>
        def _on_mousewheel(event):
            # event.delta 在 Windows 是 ±120，macOS 是 ±1
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        def _on_mousewheel_linux_up(event):
            canvas.yview_scroll(-1, "units")
        def _on_mousewheel_linux_down(event):
            canvas.yview_scroll(1, "units")

        # 用 bind_all 让整个窗口任意位置滚轮都能滚
        self.root.bind_all("<MouseWheel>", _on_mousewheel)
        self.root.bind_all("<Button-4>", _on_mousewheel_linux_up)
        self.root.bind_all("<Button-5>", _on_mousewheel_linux_down)

        # AppID
        row1 = ttk.Frame(main)
        row1.pack(fill=tk.X, pady=(0, pad))
        ttk.Label(row1, text="AppID:", width=12).pack(side=tk.LEFT)
        self.appid_var = tk.StringVar()
        ttk.Entry(row1, textvariable=self.appid_var, width=20).pack(side=tk.LEFT, padx=(0, pad))
        ttk.Button(row1, text="📋 剪贴板", command=self._paste_appid).pack(side=tk.LEFT)
        ttk.Label(row1, text=" (例: 2710380)", foreground="#888").pack(side=tk.LEFT)

        # 输出目录
        row2 = ttk.Frame(main)
        row2.pack(fill=tk.X, pady=(0, pad))
        ttk.Label(row2, text="数据输出目录:", width=12).pack(side=tk.LEFT)
        self.out_var = tk.StringVar()
        ttk.Entry(row2, textvariable=self.out_var).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, pad))
        ttk.Button(row2, text="浏览", command=self._browse_out).pack(side=tk.LEFT)

        # 数据源勾选（三列分组网格）
        src = ttk.LabelFrame(main, text=" 数据源选择 ", padding=pad)
        src.pack(fill=tk.X, pady=(0, pad))
        self.step_vars = {}
        for key, label, script, default in STEPS:
            self.step_vars[key] = tk.BooleanVar(value=default)

        # 分组定义（key 顺序决定 STEPS 里的执行顺序，这里只负责视觉分组）
        groups = [
            ("📋 官方基础数据", ["appdetails", "appreviews", "playercount"]),
            ("📊 平台/生态数据", ["news", "news_details", "steamspy", "storebrowse", "protondb"]),
            ("⭐ 深度归档 (灰色 API)", ["achievements", "reviewhistogram", "market", "pcgamingwiki"]),
        ]
        step_label_map = {k: lbl for k, lbl, _, _ in STEPS}

        cols = ttk.Frame(src)
        cols.pack(fill=tk.X)
        for col_idx, (group_title, keys) in enumerate(groups):
            col_frame = ttk.LabelFrame(cols, text=" " + group_title + " ", padding=(6, 4))
            col_frame.grid(row=0, column=col_idx, sticky="nsew", padx=(0, 6) if col_idx < len(groups) - 1 else 0)
            cols.grid_columnconfigure(col_idx, weight=1, uniform="src_col")
            for k in keys:
                if k in self.step_vars:
                    ttk.Checkbutton(col_frame, text=step_label_map.get(k, k),
                                    variable=self.step_vars[k]).pack(anchor=tk.W, pady=1)

        # 输出：html 单独强调
        out_row = ttk.Frame(src)
        out_row.pack(fill=tk.X, pady=(6, 0))
        if "html" in self.step_vars:
            ttk.Separator(out_row, orient="horizontal").pack(fill=tk.X, pady=(0, 4))
            ttk.Checkbutton(out_row, text="🎨 " + step_label_map.get("html", "生成离线 HTML"),
                            variable=self.step_vars["html"]).pack(anchor=tk.W)

        # 细分参数
        param = ttk.LabelFrame(main, text=" 细分参数 ", padding=pad)
        param.pack(fill=tk.X, pady=(0, pad))

        row_l = ttk.Frame(param); row_l.pack(fill=tk.X, pady=2)
        ttk.Label(row_l, text="appdetails 语言:", width=18).pack(side=tk.LEFT)
        self.lang_vars = {}
        for lg in LANGS_MAIN:
            v = tk.BooleanVar(value=True)
            self.lang_vars[lg] = v
            ttk.Checkbutton(row_l, text=lg, variable=v).pack(side=tk.LEFT, padx=2)
        self.lang_all_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row_l, text="--all (13种)", variable=self.lang_all_var).pack(side=tk.LEFT, padx=(10, 0))

        row_r = ttk.Frame(param); row_r.pack(fill=tk.X, pady=2)
        ttk.Label(row_r, text="评论选项:", width=18).pack(side=tk.LEFT)
        self.reviews_all_var = tk.BooleanVar(value=True)
        self.reviews_avatar_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(row_r, text="拉全部 --all", variable=self.reviews_all_var).pack(side=tk.LEFT, padx=2)
        ttk.Checkbutton(row_r, text="下头像 --with-avatars", variable=self.reviews_avatar_var).pack(side=tk.LEFT, padx=2)

        row_n = ttk.Frame(param); row_n.pack(fill=tk.X, pady=2)
        ttk.Label(row_n, text="news 语言:", width=18).pack(side=tk.LEFT)
        self.news_lang_var = tk.StringVar(value="schinese")
        ttk.Combobox(row_n, textvariable=self.news_lang_var, values=LANGS_ALL, width=15, state="readonly").pack(side=tk.LEFT)
        # 🆕 GetNewsForApp API 支持翻页拿全部历史（默认勾选，符合归档哲学）
        self.news_all_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(row_n, text="拉全部新闻 --all (翻页)", variable=self.news_all_var).pack(side=tk.LEFT, padx=8)

        row_h = ttk.Frame(param); row_h.pack(fill=tk.X, pady=2)
        ttk.Label(row_h, text="HTML 主展示语言:", width=18).pack(side=tk.LEFT)
        self.html_lang_var = tk.StringVar(value="schinese")
        ttk.Combobox(row_h, textvariable=self.html_lang_var, values=LANGS_MAIN, width=15, state="readonly").pack(side=tk.LEFT)
        self.remote_assets_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            row_h,
            text="🌐 浏览器直链资源（不下载）",
            variable=self.remote_assets_var,
        ).pack(side=tk.LEFT, padx=(10, 0))

        # SteamSpy 手动 JSON 输入框（Cloudflare 兜底方案）
        row_ss = ttk.Frame(param); row_ss.pack(fill=tk.X, pady=(6, 2))
        ttk.Label(row_ss, text="SteamSpy 手动 JSON:", width=18).pack(side=tk.LEFT, anchor=tk.N)
        ss_right = ttk.Frame(row_ss); ss_right.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.steamspy_manual_text = tk.Text(ss_right, height=4, font=("Consolas", 9),
                                             bg="#1b2838", fg="#cfe6ff", insertbackground="#fff",
                                             wrap=tk.NONE)
        self.steamspy_manual_text.pack(fill=tk.X, expand=True)
        ss_btns = ttk.Frame(ss_right); ss_btns.pack(fill=tk.X, pady=(2, 0))
        ttk.Button(ss_btns, text="📋 粘贴", command=self._steamspy_paste).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(ss_btns, text="🗑 清空", command=self._steamspy_clear).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(ss_btns, text="🌐 打开 API", command=self._steamspy_open_api).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Label(ss_btns, text="留空则自动请求；Cloudflare 挡时贴 JSON 兜底",
                  foreground="#8f98a0", font=("", 8)).pack(side=tk.LEFT, padx=(8, 0))

        # 社区市场手动 JSON 输入框（429 兜底方案，复用 SteamSpy 的 UI 模式）
        row_mk = ttk.Frame(param); row_mk.pack(fill=tk.X, pady=(6, 2))
        ttk.Label(row_mk, text="社区市场手动 JSON:", width=18).pack(side=tk.LEFT, anchor=tk.N)
        mk_right = ttk.Frame(row_mk); mk_right.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.market_manual_text = tk.Text(mk_right, height=4, font=("Consolas", 9),
                                          bg="#1b2838", fg="#cfe6ff", insertbackground="#fff",
                                          wrap=tk.NONE)
        self.market_manual_text.pack(fill=tk.X, expand=True)
        mk_btns = ttk.Frame(mk_right); mk_btns.pack(fill=tk.X, pady=(2, 0))
        ttk.Button(mk_btns, text="📋 粘贴", command=self._market_paste).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(mk_btns, text="🗑 清空", command=self._market_clear).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(mk_btns, text="🌐 打开 API", command=self._market_open_api).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Label(mk_btns, text="留空则自动请求；429 时贴 JSON 兜底",
                  foreground="#8f98a0", font=("", 8)).pack(side=tk.LEFT, padx=(8, 0))

        row_ho = ttk.Frame(param); row_ho.pack(fill=tk.X, pady=2)
        ttk.Label(row_ho, text="HTML 输出目录:", width=18).pack(side=tk.LEFT)
        self.html_out_var = tk.StringVar()
        ttk.Entry(row_ho, textvariable=self.html_out_var).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, pad))
        ttk.Label(row_ho, text="(空=<数据目录>-html)", foreground="#888").pack(side=tk.LEFT)

        # 老版路径输入框已移除，使用上方新的多行 Text 输入框（配 📋/🗑/🌐 三按钮）

        row_ao = ttk.Frame(param); row_ao.pack(fill=tk.X, pady=2)
        self.auto_open_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(row_ao, text="完成后自动打开 HTML 输出目录", variable=self.auto_open_var).pack(side=tk.LEFT)

        # 按钮
        btns = ttk.Frame(main); btns.pack(fill=tk.X, pady=(0, pad))
        self.start_btn = ttk.Button(btns, text="🚀 开始归档", command=self._on_start)
        self.start_btn.pack(side=tk.LEFT, padx=(0, pad))
        ttk.Button(btns, text="📂 打开数据目录", command=lambda: open_folder(self.out_var.get())).pack(side=tk.LEFT, padx=(0, pad))
        ttk.Button(btns, text="🌐 打开 HTML", command=self._open_html).pack(side=tk.LEFT, padx=(0, pad))
        self.abort_btn = ttk.Button(btns, text="❌ 中止", command=self._on_abort, state=tk.DISABLED)
        self.abort_btn.pack(side=tk.LEFT)

        # 日志
        logf = ttk.LabelFrame(main, text=" 执行日志 ", padding=pad)
        logf.pack(fill=tk.BOTH, expand=True, pady=(0, pad))
        self.log_text = scrolledtext.ScrolledText(logf, height=12, font=("Consolas", 9), bg="#1b2838", fg="#cfe6ff", insertbackground="#fff")
        self.log_text.pack(fill=tk.BOTH, expand=True)

        # 状态栏
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(main, textvariable=self.status_var, foreground="#5c7e10").pack(fill=tk.X)

    # ────── 配置读写 ──────
    def _reload_manual_inputs(self):
        """根据当前 AppID + 输出目录，自动回填两个手动 JSON Text 框。
        规则：只在 Text 框当前为空时才回填，避免覆盖用户新输入。"""
        try:
            appid = self.appid_var.get().strip().strip('"').strip("'")
            out_base = self.out_var.get().strip().strip('"').strip("'")
        except Exception:
            return
        if not appid or not appid.isdigit():
            return
        # 数据目录：优先 out_base 本身；如果 out_base 不含 appid 则拼一层
        if out_base and os.path.isdir(out_base):
            out_dir = out_base
        elif out_base:
            out_dir = os.path.join(out_base, appid)
        else:
            return
        if not os.path.isdir(out_dir):
            return

        # SteamSpy 手动 JSON 回填
        ss_path = os.path.join(out_dir, "steamspy_manual_input.json")
        if os.path.exists(ss_path) and hasattr(self, "steamspy_manual_text"):
            try:
                with open(ss_path, "r", encoding="utf-8") as f:
                    content = f.read()
                current = self.steamspy_manual_text.get("1.0", tk.END).strip()
                if not current and content.strip():
                    self.steamspy_manual_text.insert("1.0", content)
                    self._log(f"📥 自动回填 SteamSpy 手动 JSON（{len(content)} 字符 · {ss_path}）\n")
            except Exception:
                pass

        # 社区市场手动 JSON 回填
        mk_path = os.path.join(out_dir, "market_manual_input.json")
        if os.path.exists(mk_path) and hasattr(self, "market_manual_text"):
            try:
                with open(mk_path, "r", encoding="utf-8") as f:
                    content = f.read()
                current = self.market_manual_text.get("1.0", tk.END).strip()
                if not current and content.strip():
                    self.market_manual_text.insert("1.0", content)
                    self._log(f"📥 自动回填社区市场手动 JSON（{len(content)} 字符 · {mk_path}）\n")
            except Exception:
                pass

    def _load_from_config(self):
        c = self.config
        if c.get("appid"):
            self.appid_var.set(c["appid"])
        if c.get("out"):
            self.out_var.set(c["out"])
        if c.get("html_out"):
            self.html_out_var.set(c["html_out"])
        if c.get("news_lang"):
            self.news_lang_var.set(c["news_lang"])
        if "news_all" in c:
            self.news_all_var.set(c["news_all"])
        if c.get("html_lang"):
            self.html_lang_var.set(c["html_lang"])
        for k, v in (c.get("steps") or {}).items():
            if k in self.step_vars:
                self.step_vars[k].set(v)
        # 启动时根据当前 AppID + out_dir 自动回填手动 JSON
        # 注意：这里用 after() 延迟到 UI 完全构造好，因为 Text 组件可能还没生成
        self.root.after(100, self._reload_manual_inputs)
        # 给 AppID 和 out_dir 输入框绑定 FocusOut，用户切换后自动重新回填
        if hasattr(self, "appid_var"):
            try:
                self.appid_var.trace_add("write", lambda *_: self.root.after(200, self._reload_manual_inputs))
            except Exception:
                pass
        if hasattr(self, "out_var"):
            try:
                self.out_var.trace_add("write", lambda *_: self.root.after(200, self._reload_manual_inputs))
            except Exception:
                pass
        for k, v in (c.get("langs") or {}).items():
            if k in self.lang_vars:
                self.lang_vars[k].set(v)
        if "lang_all" in c:
            self.lang_all_var.set(c["lang_all"])
        if "reviews_all" in c:
            self.reviews_all_var.set(c["reviews_all"])
        if "reviews_avatar" in c:
            self.reviews_avatar_var.set(c["reviews_avatar"])
        if "auto_open" in c:
            self.auto_open_var.set(c["auto_open"])

    def _save_current_config(self):
        cfg = {
            "appid": self.appid_var.get().strip(),
            "out": self.out_var.get().strip(),
            "html_out": self.html_out_var.get().strip(),
            "news_lang": self.news_lang_var.get(),
            "news_all": self.news_all_var.get() if hasattr(self, "news_all_var") else True,
            "html_lang": self.html_lang_var.get(),
            "steps": {k: v.get() for k, v in self.step_vars.items()},
            "langs": {k: v.get() for k, v in self.lang_vars.items()},
            "lang_all": self.lang_all_var.get(),
            "reviews_all": self.reviews_all_var.get(),
            "reviews_avatar": self.reviews_avatar_var.get(),
            "auto_open": self.auto_open_var.get(),
        }
        save_config(cfg)

    # ────── UI 交互工具 ──────
    def _paste_appid(self):
        try:
            txt = self.root.clipboard_get().strip()
            # 支持 URL 里提取 AppID: https://store.steampowered.com/app/2710380/xxx
            import re
            m = re.search(r"/app/(\d+)", txt)
            if m:
                txt = m.group(1)
            if txt.isdigit():
                self.appid_var.set(txt)
                self._log(f"📋 从剪贴板贴入 AppID: {txt}\n")
            else:
                messagebox.showwarning("剪贴板内容无效", f"剪贴板不是数字 AppID，也不是 Steam URL:\n{txt[:100]}")
        except tk.TclError:
            messagebox.showwarning("剪贴板为空", "剪贴板里没有内容")

    def _browse_steamspy_manual(self):
        """选择 SteamSpy 手动 JSON 文件"""
        from tkinter import filedialog
        path = filedialog.askopenfilename(
            title="选择 SteamSpy 手动 JSON",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if path:
            self.steamspy_manual_var.set(path)

    def _open_steamspy_url(self):
        """在浏览器打开 SteamSpy API URL（用当前 AppID）"""
        import webbrowser
        appid = self.appid_var.get().strip().strip('"').strip("'")
        if not appid.isdigit():
            from tkinter import messagebox
            messagebox.showwarning("AppID 无效", "请先填入有效的 Steam AppID")
            return
        url = f"https://steamspy.com/api.php?request=appdetails&appid={appid}"
        webbrowser.open(url)
        self._log(f"🌐 已在浏览器打开: {url}\n💡 页面显示 JSON 后，Ctrl+S 保存，然后在上面的输入框选择该文件\n")

    def _browse_out(self):
        d = filedialog.askdirectory(title="选择数据输出目录", initialdir=self.out_var.get() or SCRIPT_DIR)
        if d:
            self.out_var.set(d)

    def _open_html(self):
        html_dir = self.html_out_var.get().strip() or self._default_html_dir()
        index = os.path.join(html_dir, "index.html")
        if os.path.exists(index):
            open_folder(html_dir)
        else:
            messagebox.showinfo("未找到", f"没找到 {index}\n请先跑一次归档")

    def _steamspy_paste(self):
        """从剪贴板一键读入 SteamSpy JSON"""
        try:
            content = self.root.clipboard_get()
            self.steamspy_manual_text.delete("1.0", tk.END)
            self.steamspy_manual_text.insert("1.0", content)
            self._log(f"📋 已从剪贴板粘贴 {len(content)} 字符\n")
        except Exception as e:
            self._log(f"⚠️ 剪贴板读取失败: {e}\n")

    def _steamspy_clear(self):
        """清空 SteamSpy 手动 JSON 输入框"""
        self.steamspy_manual_text.delete("1.0", tk.END)

    def _steamspy_open_api(self):
        """用默认浏览器打开当前 AppID 的 SteamSpy API URL"""
        import webbrowser
        appid = self.appid_var.get().strip().strip('"').strip("'")
        if not appid.isdigit():
            self._log("⚠️ 请先填入合法 AppID\n")
            return
        url = f"https://steamspy.com/api.php?request=appdetails&appid={appid}"
        webbrowser.open(url)
        self._log(f"🌐 已打开: {url}\n💡 等 Cloudflare 验证通过后，复制页面里的 JSON 回来粘贴\n")

    def _market_paste(self):
        """从剪贴板一键读入社区市场 JSON"""
        try:
            content = self.root.clipboard_get()
            self.market_manual_text.delete("1.0", tk.END)
            self.market_manual_text.insert("1.0", content)
            self._log(f"📋 已从剪贴板粘贴 {len(content)} 字符（社区市场）\n")
        except Exception as e:
            self._log(f"⚠️ 剪贴板读取失败: {e}\n")

    def _market_clear(self):
        """清空社区市场手动 JSON 输入框"""
        self.market_manual_text.delete("1.0", tk.END)

    def _market_open_api(self):
        """用默认浏览器打开当前 AppID 的社区市场 API URL"""
        import webbrowser
        appid = self.appid_var.get().strip().strip('"').strip("'")
        if not appid.isdigit():
            self._log("⚠️ 请先填入合法 AppID\n")
            return
        url = (f"https://steamcommunity.com/market/search/render/"
               f"?appid=753&category_753_Game[]=tag_app_{appid}&norender=1&count=100")
        webbrowser.open(url)
        self._log(f"🌐 已打开: {url}\n💡 等页面显示 JSON 后，复制回来粘贴\n")

    def _default_html_dir(self):
        base = self.out_var.get().strip().strip('"').strip("'") or f"./AppID_{self.appid_var.get().strip()}"
        return base.rstrip("/\\") + "-html"

    # ────── 日志 ──────
    def _log(self, text):
        self.log_queue.put(text)

    def _poll_log_queue(self):
        try:
            while True:
                text = self.log_queue.get_nowait()
                self.log_text.insert(tk.END, text)
                self.log_text.see(tk.END)
        except queue.Empty:
            pass
        self.root.after(80, self._poll_log_queue)

    def _set_status(self, msg, color="#5c7e10"):
        self.status_var.set(msg)

    # ────── 命令构建 ──────
    def _build_cmd(self, step_key, appid, out_dir):
        """根据勾选和参数构建每个步骤的命令行参数列表"""
        py = sys.executable or "python"
        if step_key == "appdetails":
            cmd = [py, os.path.join(SCRIPT_DIR, "fetch_appdetails.py"), appid, "--out", out_dir]
            if self.lang_all_var.get():
                cmd.append("--all")
            else:
                selected = [lg for lg, v in self.lang_vars.items() if v.get()]
                if selected:
                    cmd += ["--langs", ",".join(selected)]
            return cmd
        if step_key == "appreviews":
            cmd = [py, os.path.join(SCRIPT_DIR, "fetch_appreviews.py"), appid, "--out", out_dir]
            if self.reviews_all_var.get():
                cmd.append("--all")
            if self.reviews_avatar_var.get():
                cmd.append("--with-avatars")
            return cmd
        if step_key == "playercount":
            return [py, os.path.join(SCRIPT_DIR, "fetch_playercount.py"), appid, "--out", out_dir]
        if step_key == "news_details":
            # 🌏 中文新闻详情：ajaxgeteventdetails 拉多语言正文（复用 news 语言下拉）
            # 输出 news_details_{lang_tag}.json，供 steam_archiver 展示层中文覆盖使用
            return [py, os.path.join(SCRIPT_DIR, "fetch_news_details.py"), appid,
                    "--lang", self.news_lang_var.get(), "--out", out_dir]
        if step_key == "news":
            cmd = [py, os.path.join(SCRIPT_DIR, "fetch_news.py"), appid,
                   "--lang", self.news_lang_var.get(), "--out", out_dir]
            if hasattr(self, "news_all_var") and self.news_all_var.get():
                cmd.append("--all")
            return cmd
        if step_key == "steamspy":
            cmd = [py, os.path.join(SCRIPT_DIR, "fetch_steamspy.py"), appid, "--out", out_dir]

            # 优先：Text 输入框里的 JSON 内容（直贴模式）
            manual_json = ""
            if hasattr(self, "steamspy_manual_text"):
                try:
                    manual_json = self.steamspy_manual_text.get("1.0", tk.END).strip()
                except Exception:
                    manual_json = ""

            if manual_json:
                try:
                    import json as _json
                    _json.loads(manual_json)  # 校验合法性
                    os.makedirs(out_dir, exist_ok=True)
                    # 用真名字保存到数据目录，跟着 AppID 走，下次能回填
                    manual_path = os.path.join(out_dir, "steamspy_manual_input.json")
                    with open(manual_path, "w", encoding="utf-8") as f:
                        f.write(manual_json)
                    self._log(f"💾 SteamSpy 手动 JSON 已保存到: {manual_path}\n")
                    cmd += ["--manual", manual_path]
                    return cmd
                except Exception as e:
                    self._log(f"⚠️ SteamSpy 手动 JSON 无效，回退自动模式: {e}\n")

            return cmd
        if step_key == "storebrowse":
            return [py, os.path.join(SCRIPT_DIR, "fetch_storebrowse.py"), appid,
                    "--lang", self.news_lang_var.get() if hasattr(self, "news_lang_var") else "schinese",
                    "--out", out_dir]
        if step_key == "protondb":
            return [py, os.path.join(SCRIPT_DIR, "fetch_protondb.py"), appid, "--out", out_dir]
        if step_key == "achievements":
            return [py, os.path.join(SCRIPT_DIR, "fetch_achievements_global.py"), appid, "--out", out_dir]
        if step_key == "reviewhistogram":
            return [py, os.path.join(SCRIPT_DIR, "fetch_reviewhistogram.py"), appid,
                    "--lang", self.news_lang_var.get() if hasattr(self, "news_lang_var") else "schinese",
                    "--out", out_dir]
        if step_key == "pcgamingwiki":
            return [py, os.path.join(SCRIPT_DIR, "fetch_pcgamingwiki.py"), appid, "--out", out_dir]
        if step_key == "market":
            cmd = [py, os.path.join(SCRIPT_DIR, "fetch_market_items.py"), appid,
                   "--with-icons", "--out", out_dir]
            manual_json = ""
            if hasattr(self, "market_manual_text"):
                try:
                    manual_json = self.market_manual_text.get("1.0", tk.END).strip()
                except Exception:
                    manual_json = ""
            if manual_json:
                try:
                    import json as _json
                    _json.loads(manual_json)
                    os.makedirs(out_dir, exist_ok=True)
                    manual_path = os.path.join(out_dir, "market_manual_input.json")
                    with open(manual_path, "w", encoding="utf-8") as f:
                        f.write(manual_json)
                    self._log(f"💾 社区市场手动 JSON 已保存到: {manual_path}\n")
                    cmd += ["--manual", manual_path]
                except Exception as e:
                    self._log(f"⚠️ 社区市场手动 JSON 无效，回退自动模式: {e}\n")
            return cmd
        if step_key == "html":
            html_lang = self.html_lang_var.get()
            in_json = os.path.join(out_dir, f"appdetails_{html_lang}.json")
            html_out = self.html_out_var.get().strip() or self._default_html_dir()
            cmd = [py, os.path.join(SCRIPT_DIR, "steam_archiver.py"), in_json, html_out]
            if self.remote_assets_var.get():
                cmd.append("--remote-assets")
            return cmd
        return None

    # ────── 开始 / 中止 ──────
    def _on_start(self):
        if self.running:
            return
        appid = self.appid_var.get().strip().strip('"').strip("'")
        self.appid_var.set(appid)
        if not appid.isdigit():
            messagebox.showerror("AppID 无效", "请输入纯数字 AppID，例如 2710380")
            return
        out_dir = (self.out_var.get().strip().strip('"').strip("'")) or f"./AppID_{appid}"
        self.out_var.set(out_dir)
        # 同时清理 html_out 里可能带的引号
        html_out_raw = self.html_out_var.get().strip().strip('"').strip("'")
        self.html_out_var.set(html_out_raw)
        selected = [(k, label, script) for k, label, script, _ in STEPS if self.step_vars[k].get()]
        if not selected:
            messagebox.showwarning("没有勾选", "请至少勾选一个数据源")
            return

        # 检查脚本存在性
        missing = []
        for k, label, script in selected:
            if not os.path.exists(os.path.join(SCRIPT_DIR, script)):
                missing.append(script)
        if missing:
            messagebox.showerror("脚本缺失", "找不到以下脚本:\n" + "\n".join(missing))
            return

        self._save_current_config()
        self.log_text.delete("1.0", tk.END)
        self._log(f"🎯 AppID: {appid}\n")
        self._log(f"📁 数据输出: {os.path.abspath(out_dir)}\n")
        self._log(f"📋 计划执行 {len(selected)} 步\n")
        self._log("─" * 60 + "\n")

        self.running = True
        self.abort_flag = False
        self.start_time = time.time()
        self.start_btn.configure(state=tk.DISABLED)
        self.abort_btn.configure(state=tk.NORMAL)
        self._set_status("⏳ 运行中...", "#f0ad4e")

        t = threading.Thread(target=self._run_pipeline, args=(appid, out_dir, selected), daemon=True)
        t.start()

    def _on_abort(self):
        if not self.running:
            return
        self.abort_flag = True
        if self.current_proc:
            try:
                self.current_proc.terminate()
            except Exception:
                pass
        self._log("\n⚠️  用户请求中止...\n")

    def _run_pipeline(self, appid, out_dir, selected):
        """后台线程：依次跑每个步骤"""
        ok = fail = 0
        for i, (key, label, script) in enumerate(selected, 1):
            if self.abort_flag:
                self._log(f"\n❌ 中止：跳过剩余步骤\n")
                break
            self._log(f"\n📦 [{i}/{len(selected)}] {label} — {script}\n")
            cmd = self._build_cmd(key, appid, out_dir)
            if not cmd:
                self._log(f"  ⚠️ 未知步骤 {key}，跳过\n")
                continue
            rc = self._run_one_step(cmd)
            if rc == 0:
                ok += 1
                self._log(f"  ✅ 完成\n")
            else:
                fail += 1
                self._log(f"  ❌ 失败（退出码 {rc}）\n")
                # HTML 生成失败通常是致命的，其他失败可以继续
                if key == "html":
                    break

        elapsed = time.time() - self.start_time
        self._log("\n" + "─" * 60 + "\n")
        self._log(f"✨ 全部完成 · 耗时 {elapsed:.1f}s · 成功 {ok} 失败 {fail}\n")

        # 完成回调（回主线程）
        self.root.after(0, self._on_pipeline_done, ok, fail)

    def _run_one_step(self, cmd):
        """跑单个子进程，实时把 stdout 塞进日志"""
        try:
            # Windows 用 CREATE_NO_WINDOW 隐藏黑窗口
            kwargs = {}
            if sys.platform == "win32":
                kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW

            # 强制子进程用 UTF-8 输出 + 无缓冲，避免"跑完才一次性出"
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUTF8"] = "1"
            env["PYTHONUNBUFFERED"] = "1"

            # 在 python 后加 -u 强制无缓冲（双保险）
            cmd_unbuf = list(cmd)
            if cmd_unbuf and ("python" in os.path.basename(cmd_unbuf[0]).lower()):
                cmd_unbuf.insert(1, "-u")

            self.current_proc = subprocess.Popen(
                cmd_unbuf,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                bufsize=1, env=env, **kwargs
            )
            # 按字符读而不是按行读，让实时性最大化
            for line in iter(self.current_proc.stdout.readline, ""):
                if self.abort_flag:
                    break
                self._log(line)
            rc = self.current_proc.wait()
            self.current_proc = None
            return rc
        except Exception as e:
            self._log(f"  💥 启动失败: {e}\n")
            self.current_proc = None
            return -1

    def _on_pipeline_done(self, ok, fail):
        self.running = False
        self.abort_flag = False
        self.current_proc = None
        self.start_btn.configure(state=tk.NORMAL)
        self.abort_btn.configure(state=tk.DISABLED)
        if fail == 0:
            self._set_status(f"✅ 完成：成功 {ok} 步", "#5c7e10")
        else:
            self._set_status(f"⚠️  完成：成功 {ok}，失败 {fail}", "#c94a4a")

        # 自动打开 HTML 目录
        if self.auto_open_var.get() and self.step_vars.get("html") and self.step_vars["html"].get():
            html_dir = self.html_out_var.get().strip() or self._default_html_dir()
            if os.path.exists(os.path.join(html_dir, "index.html")):
                open_folder(html_dir)


if __name__ == "__main__":
    root = tk.Tk()
    app = ArchiverGUI(root)
    root.mainloop()
