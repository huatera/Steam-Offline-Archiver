#!/usr/bin/env python3
"""
Steam IStoreBrowseService/GetItems 一键下载器
用法: python fetch_storebrowse.py <appid> [--lang <语言>] [--cc <国家>] [--out <目录>]

数据来源：https://api.steampowered.com/IStoreBrowseService/GetItems/v1/
无需 API Key，公开可用。这是 store.steampowered.com 前端在用的私有 API，
返回比 appdetails 更丰富的元数据：真实用户标签（带权重）、Steam Deck 官方评级、
抢先体验标记、评论汇总、Library Hero 大图、完整 Trailer 编码等。

保存格式（和其他 fetch 脚本一致）:
{
  "appid": "1035760",
  "fetched_at_iso": "...",
  "raw": { ... IStoreBrowseService 原始返回 ... }
}
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

API_URL = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/"
USER_AGENT = "Mozilla/5.0 (Steam-Offline-Archiver/1.0)"
MAX_RETRIES = 3
RETRY_INTERVAL = 2.0
TIMEOUT = 15

# Steam Deck 兼容分类映射（打印用）
DECK_LABELS = {
    0: "❓ 未评级",
    1: "🚫 不支持",
    2: "⚠️ 可玩 (Playable)",
    3: "✅ 已验证 (Verified)",
}


def build_input_json(appid, language, country_code):
    """构造 input_json 参数"""
    payload = {
        "ids": [{"appid": int(appid)}],
        "context": {
            "language": language,
            "country_code": country_code,
        },
        "data_request": {
            "include_assets": True,
            "include_release": True,
            "include_platforms": True,
            "include_all_purchase_options": True,
            "include_screenshots": True,
            "include_trailers": True,
            "include_ratings": True,
            "include_reviews": True,
            "include_tag_count": 20,
            "include_basic_info": True,
        },
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def fetch_storebrowse(appid, language, country_code):
    """拉取 IStoreBrowseService 数据"""
    input_json = build_input_json(appid, language, country_code)
    query = urllib.parse.urlencode({"input_json": input_json})
    url = f"{API_URL}?{query}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            print(f"❌ 第 {attempt}/{MAX_RETRIES} 次请求失败: HTTP {e.code}")
        except (urllib.error.URLError, TimeoutError) as e:
            print(f"❌ 第 {attempt}/{MAX_RETRIES} 次请求失败: {e}")
        except json.JSONDecodeError as e:
            print(f"❌ JSON 解析失败: {e}")
            return None

        if attempt < MAX_RETRIES:
            time.sleep(RETRY_INTERVAL)

    return None


def main():
    parser = argparse.ArgumentParser(
        description="Steam IStoreBrowseService/GetItems 数据下载器"
    )
    parser.add_argument("appid", help="Steam AppID，例如 1035760")
    parser.add_argument("--lang", default="schinese", help="语言（默认 schinese）")
    parser.add_argument("--cc", default="CN", help="国家代码（默认 CN）")
    parser.add_argument("--out", default=".", help="输出目录（默认当前目录）")
    args = parser.parse_args()

    appid = args.appid.strip()
    if not appid.isdigit():
        print(f"❌ AppID 必须是数字：{appid}")
        sys.exit(1)

    out_dir = args.out.strip().strip('"').strip("'")
    os.makedirs(out_dir, exist_ok=True)

    print(f"🎯 目标 AppID: {appid}")
    print(f"📁 输出目录: {os.path.abspath(out_dir)}")
    print(f"🌍 语言/地区: {args.lang} / {args.cc}")
    print(f"🌐 数据来源: IStoreBrowseService (Steam 私有 API)")
    print("─" * 60)

    print(f"📡 拉取 IStoreBrowseService 数据...")
    data = fetch_storebrowse(appid, args.lang, args.cc)

    if data is None:
        print("─" * 60)
        print("❌ 未获取到数据（网络异常或 API 结构变更）")
        sys.exit(2)

    # 提取关键字段用于展示
    items = data.get("response", {}).get("store_items", [])
    if not items:
        print("❌ 返回中没有 store_items")
        sys.exit(3)

    item = items[0]
    name = item.get("name", "?")
    is_ea = item.get("is_early_access", False)
    tags = item.get("tags", []) or []
    reviews = item.get("reviews", {}).get("summary_filtered", {}) or {}
    platforms = item.get("platforms", {}) or {}
    deck_cat = platforms.get("steam_deck_compat_category", 0)
    basic = item.get("basic_info", {}) or {}
    franchises = basic.get("franchises", []) or []

    print(f"✅ 数据获取成功")
    print(f"   游戏名称：{name}")
    if is_ea:
        print(f"   状态：🚧 抢先体验中")
    print(f"   Steam Deck 官方评级：{DECK_LABELS.get(deck_cat, '?')}")
    if reviews:
        print(f"   评论汇总：{reviews.get('review_count', 0)} 条 · "
              f"{reviews.get('percent_positive', 0)}% 好评 · "
              f"{reviews.get('review_score_label', '')}")
    print(f"   用户标签：{len(tags)} 个（带权重）")
    if franchises:
        print(f"   所属系列：{', '.join(f.get('name','') for f in franchises)}")

    # 保存到 storebrowse.json
    output = {
        "appid": appid,
        "fetched_at_iso": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "language": args.lang,
        "country_code": args.cc,
        "raw": data,
    }

    out_path = os.path.join(out_dir, "storebrowse.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print("─" * 60)
    print(f"💾 已保存: {out_path}")
    print(f"✨ 完成")


if __name__ == "__main__":
    main()
