#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Steam Offline Archiver · 命令行一键归档（项目入口 __main__.py）

用法:
  python . <AppID> [选项]           # 在项目目录内执行（推荐）
  python <项目目录路径> <AppID> [选项]  # 从任意位置执行

示例:
  python . 1035760                              # 完整流水线（默认 13 步）
  python . 1035760 --only appdetails,html       # 只跑指定步骤
  python . 1035760 --skip market,pcgamingwiki   # 跳过指定步骤
  python . 1035760 --dry-run                    # 只打印将执行的命令，不运行
  python . 1035760 --remote-assets              # HTML 用浏览器直链模式
  python . --list                               # 列出所有步骤

说明:
  - 流水线与 GUI 版 (archiver_gui.py) 完全一致，参数一一对应
  - 每步实时流式输出日志；单步失败自动继续（HTML 步骤失败即中止）
  - 输出目录默认: <项目目录>/out/<AppID>
  - CLI 无法直贴 JSON 的兜底：用 --steamspy-manual / --market-manual 指定文件
"""
import argparse
import os
import subprocess
import sys
import time

# ── Windows 控制台/管道编码兜底：emoji 遇到 GBK 等编码时不崩（降级为 ?）──
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# (key, 显示名, 脚本文件名, 是否默认执行)
# 与 archiver_gui.py 的 STEPS 保持完全一致
STEPS = [
    ("appdetails",      "多语言游戏详情 (appdetails)",         "fetch_appdetails.py",          True),
    ("appreviews",      "玩家评论 + 头像 (appreviews)",        "fetch_appreviews.py",          True),
    ("playercount",     "在线人数快照 (playercount)",          "fetch_playercount.py",         True),
    ("news",            "游戏新闻 (news)",                     "fetch_news.py",                True),
    ("news_details",    "新闻详情（中文正文，多语言）",          "fetch_news_details.py",        True),
    ("steamspy",        "SteamSpy 数据 (拥有量/标签)",          "fetch_steamspy.py",            True),
    ("storebrowse",     "IStoreBrowseService (官方标签)",      "fetch_storebrowse.py",         True),
    ("protondb",        "ProtonDB (Steam Deck 兼容性)",        "fetch_protondb.py",            True),
    ("achievements",    "全球成就解锁率 (无 Key)",              "fetch_achievements_global.py", True),
    ("reviewhistogram", "评论时间分布 (舆论生命周期)",           "fetch_reviewhistogram.py",     True),
    ("market",          "社区市场物品 (卡牌/表情/背景)",         "fetch_market_items.py",        True),
    ("pcgamingwiki",    "PCGamingWiki (DRM/引擎/云同步)",      "fetch_pcgamingwiki.py",        True),
    ("html",            "生成离线 HTML（含目录导航）",           "steam_archiver.py",            True),
]

ALL_STEP_KEYS = [k for k, _, _, _ in STEPS]
STEP_META = {k: (label, script) for k, label, script, _ in STEPS}
DEFAULT_LANGS = "english,schinese,tchinese,japanese"


def _script(name):
    return os.path.join(SCRIPT_DIR, name)


def _py():
    return sys.executable or "python"


def build_cmd(key, appid, out_dir, opt):
    """构建单步命令（与 GUI 版 _build_cmd 的参数一一对应）"""
    py = _py()

    if key == "appdetails":
        cmd = [py, _script("fetch_appdetails.py"), appid, "--out", out_dir]
        if opt.appdetails_all_langs:
            cmd.append("--all")
        else:
            cmd += ["--langs", opt.appdetails_langs or DEFAULT_LANGS]
        return cmd

    if key == "appreviews":
        cmd = [py, _script("fetch_appreviews.py"), appid, "--out", out_dir]
        if opt.reviews_max is not None:
            cmd += ["--max", str(opt.reviews_max)]
        else:
            cmd.append("--all")
        if not opt.no_avatars:
            cmd.append("--with-avatars")
        if opt.proxy:
            cmd += ["--proxy", opt.proxy]
        if opt.no_proxy:
            cmd.append("--no-proxy")
        return cmd

    if key == "playercount":
        return [py, _script("fetch_playercount.py"), appid, "--out", out_dir]

    if key == "news":
        cmd = [py, _script("fetch_news.py"), appid, "--lang", opt.news_lang, "--out", out_dir]
        if opt.news_max is not None:
            cmd += ["--max", str(opt.news_max)]
        else:
            cmd.append("--all")
        return cmd

    if key == "news_details":
        return [py, _script("fetch_news_details.py"), appid,
                "--lang", opt.news_lang, "--out", out_dir]

    if key == "steamspy":
        cmd = [py, _script("fetch_steamspy.py"), appid, "--out", out_dir]
        if opt.steamspy_manual:
            cmd += ["--manual", opt.steamspy_manual]
        return cmd

    if key == "storebrowse":
        return [py, _script("fetch_storebrowse.py"), appid,
                "--lang", opt.news_lang, "--out", out_dir]

    if key == "protondb":
        return [py, _script("fetch_protondb.py"), appid, "--out", out_dir]

    if key == "achievements":
        return [py, _script("fetch_achievements_global.py"), appid, "--out", out_dir]

    if key == "reviewhistogram":
        return [py, _script("fetch_reviewhistogram.py"), appid,
                "--lang", opt.news_lang, "--out", out_dir]

    if key == "market":
        cmd = [py, _script("fetch_market_items.py"), appid, "--with-icons", "--out", out_dir]
        if opt.market_manual:
            cmd += ["--manual", opt.market_manual]
        return cmd

    if key == "pcgamingwiki":
        return [py, _script("fetch_pcgamingwiki.py"), appid, "--out", out_dir]

    if key == "html":
        html_out = opt.html_out or out_dir
        in_json = os.path.join(out_dir, "appdetails_" + opt.lang + ".json")
        if not os.path.exists(in_json):
            # 兜底：挑一个存在的 appdetails_*.json（排除 appreviews）
            try:
                cands = sorted(
                    f for f in os.listdir(out_dir)
                    if f.startswith("appdetails_") and f.endswith(".json")
                    and "appreviews" not in f
                )
            except OSError:
                cands = []
            if cands:
                in_json = os.path.join(out_dir, cands[0])
        cmd = [py, _script("steam_archiver.py"), in_json, html_out]
        if opt.remote_assets:
            cmd.append("--remote-assets")
        return cmd

    return None


def resolve_steps(opt):
    """解析要执行的步骤列表：--only/--steps 指定 → 再应用 --skip 剔除"""
    if opt.steps:
        wanted = [s.strip() for s in opt.steps.split(",") if s.strip()]
        for s in wanted:
            if s not in STEP_META:
                print("❌ 未知步骤: " + s)
                print("   可用步骤: " + ", ".join(ALL_STEP_KEYS))
                sys.exit(2)
    else:
        wanted = [k for k, _, _, default in STEPS if default]

    if opt.skip:
        skips = {s.strip() for s in opt.skip.split(",") if s.strip()}
        unknown = skips - set(ALL_STEP_KEYS)
        if unknown:
            print("❌ 未知步骤（--skip）: " + ", ".join(sorted(unknown)))
            sys.exit(2)
        wanted = [s for s in wanted if s not in skips]

    if not wanted:
        print("❌ 没有可执行的步骤")
        sys.exit(2)
    return wanted


def run_step(cmd):
    """跑单个子进程，实时把输出流到本进程 stdout。返回退出码。"""
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONUNBUFFERED"] = "1"

    cmd = list(cmd)
    if cmd and "python" in os.path.basename(cmd[0]).lower():
        cmd.insert(1, "-u")  # 双保险：强制无缓冲

    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            bufsize=1, env=env, **kwargs
        )
    except Exception as e:
        print("  💥 启动失败: " + str(e))
        return -1

    for line in iter(proc.stdout.readline, ""):
        sys.stdout.write(line)
    return proc.wait()


def main():
    ap = argparse.ArgumentParser(
        prog="python .",
        description="Steam Offline Archiver · 命令行一键归档（与 GUI 同构的 13 步流水线）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python . 1035760                            # 完整流水线\n"
            "  python . 1035760 --only appdetails,html     # 只跑指定步骤\n"
            "  python . 1035760 --skip market              # 跳过指定步骤\n"
            "  python . 1035760 --dry-run                  # 只打印命令\n"
            "  python . --list                             # 列出所有步骤\n"
        ),
    )
    ap.add_argument("appid", nargs="?", help="Steam AppID（纯数字，例如 1035760）")
    ap.add_argument("--out", default=None,
                    help="数据输出目录（默认 <项目目录>/out/<AppID>）")
    ap.add_argument("--html-out", default=None,
                    help="HTML 输出目录（默认与数据目录相同）")
    ap.add_argument("--only", dest="steps", default=None,
                    help="只执行这些步骤（逗号分隔，见 --list）")
    ap.add_argument("--skip", default=None,
                    help="跳过这些步骤（逗号分隔）")
    ap.add_argument("--list", action="store_true", dest="list_steps",
                    help="列出所有可用步骤并退出")
    ap.add_argument("--lang", default="schinese",
                    help="HTML 主展示语言（默认 schinese；也用于挑选 appdetails_<lang>.json）")
    ap.add_argument("--appdetails-langs", default=None,
                    help="appdetails 下载语言列表（逗号分隔，默认 english,schinese,tchinese,japanese）")
    ap.add_argument("--appdetails-all-langs", action="store_true",
                    help="appdetails 下载全部 13 种语言")
    ap.add_argument("--news-lang", default="schinese",
                    help="新闻语言（默认 schinese）")
    ap.add_argument("--news-max", type=int, default=None,
                    help="新闻最多条数（默认 --all 翻页拿全部）")
    ap.add_argument("--reviews-max", type=int, default=None,
                    help="评论最多条数（默认 --all 全量，推荐）")
    ap.add_argument("--no-avatars", action="store_true",
                    help="不下头像（默认下载）")
    ap.add_argument("--proxy", default=None,
                    help="点评级请求代理，如 http://127.0.0.1:7897")
    ap.add_argument("--no-proxy", action="store_true",
                    help="评论请求禁用代理（纯直连）")
    ap.add_argument("--remote-assets", action="store_true",
                    help="HTML 用浏览器直链模式（不下载资源）")
    ap.add_argument("--steamspy-manual", default=None,
                    help="SteamSpy 手动 JSON 文件路径（Cloudflare 兜底）")
    ap.add_argument("--market-manual", default=None,
                    help="社区市场手动 JSON 文件路径（429 兜底）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印将执行的命令，不实际运行")
    ap.add_argument("--open", action="store_true",
                    help="完成后自动打开 HTML 输出目录（Windows/macOS/Linux）")
    opt = ap.parse_args()

    # --list
    if opt.list_steps:
        print("📋 可用步骤（按流水线顺序）:")
        for k, label, script, default in STEPS:
            mark = "●" if default else "○"
            print("  " + mark + " " + k.ljust(16) + " " + label + "  [" + script + "]")
        print()
        print("  用法: python . <AppID> --only 步骤1,步骤2")
        return

    # AppID 校验
    if not opt.appid:
        ap.print_help()
        sys.exit(2)
    appid = opt.appid.strip().strip('"').strip("'")
    if not appid.isdigit():
        print("❌ AppID 必须是纯数字，例如 1035760（收到: " + appid + "）")
        sys.exit(2)

    # 输出目录
    out_dir = opt.out or os.path.join(SCRIPT_DIR, "out", appid)
    out_dir = os.path.abspath(out_dir)

    # 步骤解析
    selected = resolve_steps(opt)

    # 检查脚本存在性
    missing = []
    for k in selected:
        _, script = STEP_META[k]
        if not os.path.exists(_script(script)):
            missing.append(script)
    if missing:
        print("❌ 找不到以下脚本（请在项目目录内运行）:")
        for m in missing:
            print("   - " + m)
        sys.exit(2)

    print("🎯 AppID: " + appid)
    print("📁 数据输出: " + out_dir)
    print("📋 计划执行 " + str(len(selected)) + " 步")
    if opt.dry_run:
        print("🧪 dry-run：只打印命令，不实际执行")
    print("─" * 60)

    # dry-run：打印每步命令
    if opt.dry_run:
        for i, k in enumerate(selected, 1):
            label, script = STEP_META[k]
            cmd = build_cmd(k, appid, out_dir, opt)
            print("📦 [" + str(i) + "/" + str(len(selected)) + "] " + label + " — " + script)
            print("    $ " + subprocess.list2cmdline(cmd))
        print("─" * 60)
        print("✨ dry-run 完成：命令未执行")
        return

    os.makedirs(out_dir, exist_ok=True)

    # 主循环
    ok = fail = 0
    start = time.time()
    for i, k in enumerate(selected, 1):
        label, script = STEP_META[k]
        print()
        print("📦 [" + str(i) + "/" + str(len(selected)) + "] " + label + " — " + script)
        cmd = build_cmd(k, appid, out_dir, opt)
        if not cmd:
            print("  ⚠️ 未知步骤 " + k + "，跳过")
            continue
        t0 = time.time()
        rc = run_step(cmd)
        dt = time.time() - t0
        if rc == 0:
            ok += 1
            print("  ✅ 完成（" + ("%.1f" % dt) + "s）")
        else:
            fail += 1
            print("  ❌ 失败（退出码 " + str(rc) + "，" + ("%.1f" % dt) + "s）")
            if k == "html":
                print("  ⛔ HTML 生成失败，中止后续步骤")
                break

    elapsed = time.time() - start
    print()
    print("─" * 60)
    print("✨ 全部完成 · 耗时 " + ("%.1f" % elapsed) + "s · 成功 " + str(ok) + " 失败 " + str(fail))

    index_html = os.path.join(opt.html_out or out_dir, "index.html")
    if os.path.exists(index_html):
        print("💡 双击打开离线页面: " + index_html)

    # 可选：完成后自动打开目录
    if opt.open:
        target = os.path.dirname(os.path.abspath(index_html)) if os.path.exists(index_html) else out_dir
        try:
            if sys.platform == "win32":
                os.startfile(target)  # noqa
            elif sys.platform == "darwin":
                subprocess.Popen(["open", target])
            else:
                subprocess.Popen(["xdg-open", target])
        except Exception as e:
            print("⚠️ 打开目录失败: " + str(e))

    sys.exit(0 if fail == 0 else 1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        print("⚠️  用户中断（Ctrl+C）。已完成步骤的产物保留在磁盘上。")
        sys.exit(130)
