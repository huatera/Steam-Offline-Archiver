#!/usr/bin/env python3
"""
Steam 评论时间分布直方图一键下载器
用法: python fetch_reviewhistogram.py <appid> [--out <目录>]

数据来源：https://store.steampowered.com/appreviewhistogram/<appid>
这是 Steam 商店页评论时间曲线用的内部 AJAX 接口，无需 API Key。
返回按月聚合的 rollups + 近 30 天的 recent 数据。

保存格式:
{
  "appid": "1035760",
  "fetched_at_iso": "...",
  "raw": { ... 原始返回 ... }
}
"""
import sys
import os
import json
import time
import argparse
import urllib.request
import urllib.error
from datetime import datetime

API_URL = "https://store.steampowered.com/appreviewhistogram/{appid}?l={lang}&review_score_preference=0"
USER_AGENT = "Mozilla/5.0 (Steam-Offline-Archiver/1.0)"
MAX_RETRIES = 3
RETRY_INTERVAL = 2.0
TIMEOUT = 15


def fetch_histogram(appid, lang="schinese"):
    """拉取评论直方图数据"""
    url = API_URL.format(appid=appid, lang=lang)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                print(f"⚠️ AppID {appid} 无直方图数据（404）")
                return None
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
    parser = argparse.ArgumentParser(description="Steam 评论历史直方图下载器")
    parser.add_argument("appid", help="Steam AppID，例如 1035760")
    parser.add_argument("--lang", default="schinese", help="语言（默认 schinese）")
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
    print(f"🌐 数据来源: appreviewhistogram (Steam 私有 API)")
    print("─" * 60)

    print(f"📡 拉取评论时间分布...")
    data = fetch_histogram(appid, args.lang)

    if not data or data.get("success") != 1:
        print("─" * 60)
        print("❌ 未获取到数据（游戏评论太少 / 网络异常）")
        sys.exit(2)

    results = data.get("results", {}) or {}
    rollups = results.get("rollups", []) or []
    recent = results.get("recent", []) or []
    rollup_type = results.get("rollup_type", "?")

    print(f"✅ 数据获取成功")
    print(f"   聚合类型：{rollup_type}")
    print(f"   聚合数据点：{len(rollups)} 个")
    print(f"   近期日度数据点：{len(recent)} 个")

    if rollups:
        first_ts = rollups[0].get("date", 0)
        last_ts = rollups[-1].get("date", 0)
        first_str = datetime.fromtimestamp(first_ts).strftime("%Y-%m") if first_ts else "?"
        last_str = datetime.fromtimestamp(last_ts).strftime("%Y-%m") if last_ts else "?"
        total_up = sum(r.get("recommendations_up", 0) or 0 for r in rollups)
        total_down = sum(r.get("recommendations_down", 0) or 0 for r in rollups)
        total = total_up + total_down
        pct = (total_up / total * 100) if total > 0 else 0
        print(f"   时间跨度：{first_str} → {last_str}")
        print(f"   累计好评：{total_up} | 累计差评：{total_down} | 好评率：{pct:.1f}%")

        # 找出差评波峰（差评 > 好评的月份）
        bad_months = []
        for r in rollups:
            up = r.get("recommendations_up", 0) or 0
            down = r.get("recommendations_down", 0) or 0
            if down > up and down >= 3:
                m = datetime.fromtimestamp(r.get("date", 0)).strftime("%Y-%m")
                bad_months.append(f"{m} ({up}↑/{down}↓)")
        if bad_months:
            print(f"   ⚠️ 差评波峰月份：{', '.join(bad_months)}")

    # 保存
    output = {
        "appid": appid,
        "fetched_at_iso": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "language": args.lang,
        "raw": data,
    }
    out_path = os.path.join(out_dir, "reviewhistogram.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print("─" * 60)
    print(f"💾 已保存: {out_path}")
    print(f"✨ 完成")


if __name__ == "__main__":
    main()
