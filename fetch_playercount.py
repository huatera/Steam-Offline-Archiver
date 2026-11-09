#!/usr/bin/env python3
"""
Steam 当前在线玩家数一键下载器
用法:
  python fetch_playercount.py <AppID>
  python fetch_playercount.py <AppID> --out ./AppID_2710380

输出:
  <out_dir>/playercount.json
    {
      "appid": "2710380",
      "player_count": 0,
      "result": 1,
      "fetched_at": 1786800000,
      "fetched_at_iso": "2026-08-15 22:30:00"
    }

说明:
  - 调用 Steam 官方 ISteamUserStats/GetNumberOfCurrentPlayers（无需 API Key）
  - player_count=0 是正常现象（冷门/老游戏经常为 0）
  - 归档意义：时间胶囊，记录"归档时该游戏还有多少人在玩"
"""
import argparse
import datetime
import json
import os
import sys
import time
import urllib.request
import urllib.error

MAX_RETRIES = 3
TIMEOUT = 15
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def fetch_playercount(appid):
    url = f"https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/?appid={appid}"
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            response = data.get("response", {}) or {}
            return response
        except urllib.error.HTTPError as e:
            wait = 5 * attempt if e.code == 429 else 2
            print(f"  ❌ HTTP {e.code}，{wait}s 后重试 ({attempt}/{MAX_RETRIES})")
            time.sleep(wait)
        except Exception as e:
            print(f"  ❌ 错误: {e}，重试 ({attempt}/{MAX_RETRIES})")
            time.sleep(2)
    return None


def main():
    parser = argparse.ArgumentParser(description="Steam 当前在线玩家数一键下载器")
    parser.add_argument("appid", type=str, help="Steam AppID，例如 2710380")
    parser.add_argument("--out", type=str, default=None, help="输出目录（默认 ./AppID_<id>/）")
    args = parser.parse_args()

    appid = args.appid
    out_dir = args.out or f"./AppID_{appid}"
    os.makedirs(out_dir, exist_ok=True)

    print(f"🎯 AppID: {appid}")
    print(f"📁 输出: {os.path.abspath(out_dir)}")
    print("─" * 60)

    response = fetch_playercount(appid)
    if response is None:
        print("💥 请求失败，已放弃")
        sys.exit(2)

    result = response.get("result", 0)
    player_count = response.get("player_count", None)

    if result != 1:
        print(f"⚠️  API 返回 result={result}（非 1），可能是 AppID 无效或该游戏未上架")
    if player_count is None:
        print("⚠️  返回中不包含 player_count 字段")
        player_count = 0

    now = int(time.time())
    now_iso = datetime.datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S")

    payload = {
        "appid": appid,
        "player_count": player_count,
        "result": result,
        "fetched_at": now,
        "fetched_at_iso": now_iso,
    }

    out_file = os.path.join(out_dir, "playercount.json")
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    # 友好输出
    if player_count == 0:
        print(f"📡 当前在线人数: 0 人（冷门/老游戏常见）")
    elif player_count < 100:
        print(f"📡 当前在线人数: {player_count} 人（小众玩家群体）")
    elif player_count < 10000:
        print(f"📡 当前在线人数: {player_count:,} 人（活跃）")
    else:
        print(f"📡 当前在线人数: {player_count:,} 人 🔥（热门）")
    print(f"⏰ 归档时间: {now_iso}")
    print(f"📄 保存到: {out_file}")


if __name__ == "__main__":
    main()
