#!/usr/bin/env python3
"""
ProtonDB Steam Deck / Linux 兼容性数据一键下载器
用法: python fetch_protondb.py <appid> [--out <目录>]

数据来源：https://www.protondb.com/api/v1/reports/summaries/<appid>.json
无需 API Key，单次 GET 请求。

保存格式（和其他 fetch 脚本一致）:
{
  "appid": "2710380",
  "fetched_at_iso": "2026-08-15 23:30:00",
  "raw": { ... ProtonDB 原始返回 ... }
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

# ────────────────────────────────
# 常量
# ────────────────────────────────
API_URL = "https://www.protondb.com/api/v1/reports/summaries/{appid}.json"
USER_AGENT = "Mozilla/5.0 (Steam-Offline-Archiver/1.0)"
MAX_RETRIES = 3
RETRY_INTERVAL = 2.0
TIMEOUT = 15

# tier → 中文映射（打印用）
TIER_LABELS = {
    "platinum": "🏅 Platinum（完美）",
    "gold": "🥇 Gold（优秀）",
    "silver": "🥈 Silver（良好）",
    "bronze": "🥉 Bronze（一般）",
    "borked": "💀 Borked（无法运行）",
    "pending": "⏳ Pending（样本不足）",
}

# ────────────────────────────────
# 网络请求
# ────────────────────────────────
def fetch_protondb(appid):
    """拉取 ProtonDB 的兼容性摘要"""
    url = API_URL.format(appid=appid)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                print(f"⚠️ ProtonDB 上没有 AppID {appid} 的任何报告（404）")
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

# ────────────────────────────────
# 主流程
# ────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="ProtonDB Steam Deck / Linux 兼容性数据下载器"
    )
    parser.add_argument("appid", help="Steam AppID，例如 2710380")
    parser.add_argument(
        "--out",
        default=".",
        help="输出目录（默认当前目录）",
    )
    args = parser.parse_args()

    appid = args.appid.strip()
    if not appid.isdigit():
        print(f"❌ AppID 必须是数字：{appid}")
        sys.exit(1)

    out_dir = args.out.strip().strip('"').strip("'")
    os.makedirs(out_dir, exist_ok=True)

    print(f"🎯 目标 AppID: {appid}")
    print(f"📁 输出目录: {os.path.abspath(out_dir)}")
    print(f"🌐 数据来源: ProtonDB")
    print("─" * 60)

    print(f"📡 拉取 ProtonDB 数据...")
    data = fetch_protondb(appid)

    if data is None:
        print("─" * 60)
        print("❌ 未获取到数据（游戏不在 ProtonDB 数据库，或网络异常）")
        sys.exit(2)

    # 提取关键字段用于展示
    tier = data.get("tier", "pending")
    provisional = data.get("provisionalTier", "pending")
    best = data.get("bestReportedTier", "pending")
    total = data.get("total", 0)
    confidence = data.get("confidence", "unknown")
    score = data.get("score", 0)

    print(f"✅ 数据获取成功")
    print(f"   官方评级：{TIER_LABELS.get(tier, tier)}")
    print(f"   暂定评级：{TIER_LABELS.get(provisional, provisional)}")
    print(f"   最佳报告：{TIER_LABELS.get(best, best)}")
    print(f"   报告总数：{total} 份")
    print(f"   置信度：{confidence}")
    print(f"   综合分：{score}")

    if total < 5:
        print(f"   ⚠️ 样本不足（< 5 份），结果仅供参考")

    # 保存到 protondb.json
    output = {
        "appid": appid,
        "fetched_at_iso": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "raw": data,
    }

    out_path = os.path.join(out_dir, "protondb.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print("─" * 60)
    print(f"💾 已保存: {out_path}")
    print(f"✨ 完成")


if __name__ == "__main__":
    main()
