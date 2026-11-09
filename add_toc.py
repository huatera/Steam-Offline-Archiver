#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
add_toc.py — 给「已经生成」的归档页面一键注入「目录导航」（悬浮 TOC）

用法:
  python add_toc.py <路径> [更多路径...]

  <路径> 可以是：
    - 某个 index.html 文件
    - 某个归档目录（含 index.html）
    - 顶层输出目录（自动递归扫描所有 index.html）

示例:
  python add_toc.py D:\\Project\\Steam-Offline-Archiver\\out\\4513880
  python add_toc.py D:\\Project\\Steam-Offline-Archiver\\out          # 批量全部

原理:
  - 写出 assets/toc.js（与 steam_archiver.py 的 TOC_JS_CODE 保持一致）
  - 在 </body> 前注入 <script src="assets/toc.js" defer></script>
  - 幂等：已注入过的页面自动跳过；随时可重复运行
  - 无需重新生成页面、无需联网
"""
import os
import sys

# ── 目录导航脚本源码（与 steam_archiver.py 内的 TOC_JS_CODE 一致）──
TOC_JS_CODE = None  # 运行期从 steam_archiver.py 读取，保证单一来源


def _load_toc_js():
    """从同目录的 steam_archiver.py 读取 TOC_JS_CODE（单一来源，避免双份维护）"""
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, here)
    try:
        import steam_archiver  # noqa: E402
        code = getattr(steam_archiver, "TOC_JS_CODE", None)
        if code:
            return code
    except Exception as e:
        print(f"⚠️  读取 steam_archiver.py 失败: {e}")
    # 兜底提示：把两个文件放同一目录即可
    print("❌ 无法从 steam_archiver.py 读取 TOC_JS_CODE。请确保两个文件在同一目录。")
    sys.exit(1)


def find_html_targets(path):
    """把用户给的路径展开成一串 index.html 文件路径"""
    results = []
    if os.path.isfile(path):
        if path.lower().endswith(".html"):
            results.append(path)
        return results
    if os.path.isdir(path):
        direct = os.path.join(path, "index.html")
        if os.path.isfile(direct):
            # 归档目录本身
            results.append(direct)
        else:
            # 顶层目录：递归扫描
            for root, dirs, files in os.walk(path):
                if "index.html" in files:
                    results.append(os.path.join(root, "index.html"))
    return sorted(results)


def inject_one(html_path, toc_js):
    """给单个 index.html 注入 TOC。返回 ('ok'|'skip'|'fail', 说明)"""
    d = os.path.dirname(os.path.abspath(html_path))
    toc_dir = os.path.join(d, "assets")
    toc_path = os.path.join(toc_dir, "toc.js")

    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()

    # 幂等：已有引用则只刷新 toc.js
    if "assets/toc.js" in html:
        os.makedirs(toc_dir, exist_ok=True)
        with open(toc_path, "w", encoding="utf-8") as f:
            f.write(toc_js)
        return ("skip", "已注入过（已刷新 toc.js）")

    tag = '  <script src="assets/toc.js" defer></script>\n'
    if "</body>" in html:
        html = html.replace("</body>", tag + "</body>", 1)
    else:
        html += "\n" + tag  # 极少见：没有 </body> 的残缺页面

    os.makedirs(toc_dir, exist_ok=True)
    with open(toc_path, "w", encoding="utf-8") as f:
        f.write(toc_js)
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    return ("ok", f"已注入（{os.path.relpath(toc_path)}）")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    toc_js = _load_toc_js()

    targets = []
    for p in sys.argv[1:]:
        if not os.path.exists(p):
            print(f"⚠️  路径不存在，跳过: {p}")
            continue
        targets.extend(find_html_targets(p))

    # 去重
    targets = sorted(set(os.path.abspath(t) for t in targets))
    if not targets:
        print("❌ 没有找到任何 index.html")
        sys.exit(1)

    print(f"🔍 找到 {len(targets)} 个页面，开始注入「目录导航」...")
    print("─" * 60)
    ok = skip = fail = 0
    for t in targets:
        try:
            status, msg = inject_one(t, toc_js)
        except Exception as e:
            status, msg = "fail", f"{type(e).__name__}: {e}"
        icon = {"ok": "✅", "skip": "⏭️", "fail": "❌"}[status]
        print(f"{icon} {os.path.relpath(t)}")
        print(f"   {msg}")
        if status == "ok":
            ok += 1
        elif status == "skip":
            skip += 1
        else:
            fail += 1
    print("─" * 60)
    print(f"✨ 完成：新注入 {ok} · 跳过 {skip} · 失败 {fail}")
    print("   打开页面后右上角即出现「目录导航」悬浮面板（可拖拽 / ✕ 收起 / Esc 收起）")


if __name__ == "__main__":
    main()
