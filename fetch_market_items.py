#!/usr/bin/env python3
"""
Steam 社区市场物品一键下载器（集换式卡牌 / 闪亮卡牌 / 表情 / 个人资料背景）
用法: python fetch_market_items.py <appid> [--out <目录>] [--with-icons] [--max <数量>]

数据来源：https://steamcommunity.com/market/search/render/
  ?appid=753&category_753_Game[]=tag_app_<appid>&norender=1&count=100&start=<offset>

无需 API Key，纯 GET 请求。
保存 market_items.json + 可选 market_icons/ 目录（离线化的图标）。
"""
import sys
import os
import json
import time
import argparse
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime

API_URL = ("https://steamcommunity.com/market/search/render/"
           "?appid=753&category_753_Game%5B%5D=tag_app_{appid}"
           "&norender=1&count={count}&start={start}")
ICON_URL_FMT = "https://community.akamai.steamstatic.com/economy/image/{icon_url}"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
BROWSER_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://steamcommunity.com/market/",
    "Accept-Encoding": "gzip, deflate",
    "X-Requested-With": "XMLHttpRequest",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
}
MAX_RETRIES = 3
RETRY_INTERVAL = 2.0
TIMEOUT = 20
PAGE_SIZE = 100
REQUEST_INTERVAL = 1.5


def fetch_page(appid, start, count=PAGE_SIZE):
    """拉取一页市场物品"""
    import gzip
    url = API_URL.format(appid=appid, count=count, start=start)
    req = urllib.request.Request(url, headers=BROWSER_HEADERS)

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return json.loads(raw.decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                print(f"⏳ 429 限流，等 5 秒重试...")
                time.sleep(5)
                continue
            print(f"❌ 第 {attempt}/{MAX_RETRIES} 次请求失败: HTTP {e.code}")
        except (urllib.error.URLError, TimeoutError) as e:
            print(f"❌ 第 {attempt}/{MAX_RETRIES} 次请求失败: {e}")
        except json.JSONDecodeError as e:
            print(f"❌ JSON 解析失败: {e}")
            return None

        if attempt < MAX_RETRIES:
            time.sleep(RETRY_INTERVAL)

    return None


def download_icon(icon_url_hash, dest_dir):
    """下载单张图标到本地，返回相对路径"""
    if not icon_url_hash:
        return ""

    # 用 hash 前 20 字符做文件名，避免 URL 编码问题
    safe_name = "".join(c if c.isalnum() else "_" for c in icon_url_hash[:60])
    dst = os.path.join(dest_dir, safe_name + ".png")

    if os.path.exists(dst):
        return os.path.basename(dst)  # 已存在，跳过

    url = ICON_URL_FMT.format(icon_url=icon_url_hash)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                data = resp.read()
            with open(dst, "wb") as f:
                f.write(data)
            return os.path.basename(dst)
        except Exception as e:
            if attempt < MAX_RETRIES:
                time.sleep(1.0)
            else:
                print(f"   ⚠️ 图标下载失败: {e}")
                return ""

    return ""


def main():
    parser = argparse.ArgumentParser(description="Steam 社区市场物品下载器")
    parser.add_argument("appid", help="Steam AppID，例如 1035760")
    parser.add_argument("--out", default=".", help="输出目录（默认当前目录）")
    parser.add_argument("--with-icons", action="store_true",
                        help="同时下载所有物品图标到 market_icons/ 子目录")
    parser.add_argument("--max", type=int, default=500,
                        help="最多拉取物品数量（默认 500）")
    parser.add_argument("--manual", type=str, default=None,
                        help="手动模式：从本地 JSON 文件读取市场原始返回，绕过 429")
    args = parser.parse_args()

    appid = args.appid.strip()
    if not appid.isdigit():
        print(f"❌ AppID 必须是数字：{appid}")
        sys.exit(1)

    out_dir = args.out.strip().strip('"').strip("'")
    os.makedirs(out_dir, exist_ok=True)

    print(f"🎯 目标 AppID: {appid}")
    print(f"📁 输出目录: {os.path.abspath(out_dir)}")
    print(f"🌐 数据来源: Steam 社区市场（market/search/render）")
    print(f"🖼️ 离线化图标: {'是' if args.with_icons else '否'}")
    print("─" * 60)

    all_items = []
    total_reported = None

    if args.manual:
        manual_path = args.manual.strip().strip('"').strip("'")
        print(f"📥 数据源: 手动模式（从 {manual_path} 读取）")
        print("─" * 60)
        try:
            with open(manual_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            total_reported = data.get("total_count", 0)
            all_items = data.get("results", []) or []
            print(f"✅ 手动 JSON 读取成功: {len(all_items)} 条物品（total={total_reported}）")
        except Exception as e:
            print(f"💥 读取失败: {e}")
            sys.exit(3)

    # 翻页拉取（自动模式）
    start = 0
    page = 0

    while not args.manual and start < args.max:
        page += 1
        print(f"📄 第 {page} 页 (start={start})...")
        data = fetch_page(appid, start)

        if data is None:
            print("❌ 网络异常，中止")
            break

        if not data.get("success"):
            print("⚠️ 返回 success=false，可能没有市场物品")
            break

        if total_reported is None:
            total_reported = data.get("total_count", 0)
            print(f"📊 全网市场物品总数: {total_reported}")

        items = data.get("results", []) or []
        if not items:
            print("✅ 无更多物品")
            break

        all_items.extend(items)
        got = len(items)
        print(f"   本页 {got} 条，累计 {len(all_items)} 条")

        if got < PAGE_SIZE:
            break  # 最后一页
        if len(all_items) >= args.max:
            print(f"⏹ 达到上限 {args.max}")
            break

        start += PAGE_SIZE
        time.sleep(REQUEST_INTERVAL)

    if not all_items:
        print("❌ 没有拉到任何市场物品")
        sys.exit(2)

    # 按类型分类统计
    type_count = {}
    for it in all_items:
        t = it.get("asset_description", {}).get("type", "未知")
        type_count[t] = type_count.get(t, 0) + 1
    print(f"\n📊 物品类型分布：")
    for t, c in sorted(type_count.items(), key=lambda x: -x[1]):
        print(f"   {t}: {c} 个")

    # 下载图标
    if args.with_icons:
        icons_dir = os.path.join(out_dir, "market_icons")
        os.makedirs(icons_dir, exist_ok=True)
        print(f"\n🖼️ 下载图标到 {icons_dir}/ ...")
        downloaded = 0
        skipped = 0
        for i, it in enumerate(all_items, 1):
            icon_hash = it.get("asset_description", {}).get("icon_url", "")
            if not icon_hash:
                skipped += 1
                continue
            local = download_icon(icon_hash, icons_dir)
            if local:
                # 把本地文件名写回 item，供归档 HTML 引用
                it["_local_icon"] = f"market_icons/{local}"
                downloaded += 1
            else:
                skipped += 1
            if i % 5 == 0:
                print(f"   已处理 {i}/{len(all_items)}...")
            time.sleep(0.3)  # 温柔一点
        print(f"✅ 图标下载完成: {downloaded} 张 (跳过 {skipped})")

    # 保存
    output = {
        "appid": appid,
        "fetched_at_iso": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_reported": total_reported,
        "total_fetched": len(all_items),
        "type_count": type_count,
        "with_icons": args.with_icons,
        "items": all_items,
    }
    out_path = os.path.join(out_dir, "market_items.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print("─" * 60)
    print(f"💾 已保存: {out_path}")
    print(f"✨ 完成")


if __name__ == "__main__":
    main()
