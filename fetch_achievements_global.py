#!/usr/bin/env python3
"""
Steam 全球成就解锁率一键下载器
用法: python fetch_achievements_global.py <appid> [--out <目录>]

数据来源：https://api.steampowered.com/ISteamUserStats/GetGlobalAchievementPercentagesForApp/v2/
无需 API Key，公开可用。返回该游戏所有成就的全球解锁百分比。

⚠️ 注意：这个 API 只返回成就的内部 API 名（如 ACH_TRUE_ENDING）和百分比，
不返回中文显示名、描述、图标。要拿完整成就资料需要 GetSchemaForGame（需 Key）。
主脚本会自动用 appdetails.achievements.highlighted 里的 name 做匹配桥梁。

保存格式（和其他 fetch 脚本一致）:
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

API_URL = "https://api.steampowered.com/ISteamUserStats/GetGlobalAchievementPercentagesForApp/v2/?gameid={appid}"
USER_AGENT = "Mozilla/5.0 (Steam-Offline-Archiver/1.0)"
MAX_RETRIES = 3
RETRY_INTERVAL = 2.0
TIMEOUT = 15


def fetch_achievements_global(appid):
    """拉取全球成就解锁率"""
    url = API_URL.format(appid=appid)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            if e.code == 400:
                print(f"⚠️ 游戏 AppID {appid} 没有全球成就数据（可能没有成就系统）")
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
    parser = argparse.ArgumentParser(
        description="Steam 全球成就解锁率下载器（无 Key）"
    )
    parser.add_argument("appid", help="Steam AppID，例如 1035760")
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
    print(f"🌐 数据来源: GetGlobalAchievementPercentagesForApp (无 Key)")
    print("─" * 60)

    print(f"📡 拉取全球成就解锁率...")
    data = fetch_achievements_global(appid)

    if data is None:
        print("─" * 60)
        print("❌ 未获取到数据（游戏无成就 / 网络异常）")
        sys.exit(2)

    achievements = data.get("achievementpercentages", {}).get("achievements", []) or []
    if not achievements:
        print("⚠️ 返回中没有成就数据")
        sys.exit(3)

    # 排序 + 统计
    parsed = []
    for a in achievements:
        try:
            pct = float(a.get("percent", 0))
        except (ValueError, TypeError):
            pct = 0.0
        parsed.append({"name": a.get("name", ""), "percent": pct})
    parsed.sort(key=lambda x: x["percent"])

    print(f"✅ 数据获取成功")
    print(f"   共 {len(parsed)} 个成就")

    # 分级统计
    tiers = {"💎 极稀 (<1%)": 0, "🔴 稀有 (<5%)": 0, "🟠 中等 (<20%)": 0,
             "🟡 常见 (<50%)": 0, "🟢 大众 (>=50%)": 0}
    for a in parsed:
        p = a["percent"]
        if p < 1: tiers["💎 极稀 (<1%)"] += 1
        elif p < 5: tiers["🔴 稀有 (<5%)"] += 1
        elif p < 20: tiers["🟠 中等 (<20%)"] += 1
        elif p < 50: tiers["🟡 常见 (<50%)"] += 1
        else: tiers["🟢 大众 (>=50%)"] += 1
    for t, c in tiers.items():
        if c > 0:
            print(f"   {t}: {c} 个")

    # 最稀有 Top 5
    print(f"\n   💎 最稀有 Top 5:")
    for a in parsed[:5]:
        print(f"      {a['percent']:5.2f}% · {a['name']}")

    # 保存
    output = {
        "appid": appid,
        "fetched_at_iso": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "raw": data,
    }
    out_path = os.path.join(out_dir, "achievements_global.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print("─" * 60)
    print(f"💾 已保存: {out_path}")
    print(f"✨ 完成")


if __name__ == "__main__":
    main()
