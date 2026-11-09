#!/usr/bin/env python3
"""
SteamSpy appdetails 一键下载器
用法:
  python fetch_steamspy.py <AppID>
  python fetch_steamspy.py <AppID> --out ./AppID_2710380

输出:
  <out_dir>/steamspy.json
    {
      "appid": "2710380",
      "fetched_at_iso": "...",
      "raw": { ... 原始返回 ... },
      "owners": "50,000 .. 100,000",
      "ccu": 123,
      "tags": {"Roguelite": 523, "美少女": 410, ...},
      "positive": 65,
      "negative": 13,
      "languages": "English, Simplified Chinese, ..."
    }

说明:
  - 调用 SteamSpy 官方 API（无需 API Key）
  - 限流: 1 request per second（本脚本单请求，不受影响）
  - 数据是 SteamSpy 估算，不是 Steam 官方真值
  - playtime 字段现已废弃（返回 0），本脚本不再展示
  - 归档意义: tags 是玩家投票标签，Steam 官方不提供，独家价值
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
TIMEOUT = 20
# 用较新的完整浏览器 UA + Referer + Accept，绕过 SteamSpy 的反爬过滤
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
BROWSER_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://steamspy.com/",
    "Accept-Encoding": "gzip, deflate",
}


def fetch_steamspy(appid):
    import gzip
    url = f"https://steamspy.com/api.php?request=appdetails&appid={appid}"
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers=BROWSER_HEADERS)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read()
                # 处理 gzip 压缩
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as e:
            wait = 5 * attempt if e.code == 429 else 2
            # 403 时打印响应体前 200 字帮助诊断
            try:
                body = e.read()[:200].decode("utf-8", errors="replace")
                print(f"  ❌ HTTP {e.code}，{wait}s 后重试 ({attempt}/{MAX_RETRIES}) · body={body!r}")
            except Exception:
                print(f"  ❌ HTTP {e.code}，{wait}s 后重试 ({attempt}/{MAX_RETRIES})")
            time.sleep(wait)
        except Exception as e:
            print(f"  ❌ 错误: {e}，重试 ({attempt}/{MAX_RETRIES})")
            time.sleep(2)
    return None


def main():
    parser = argparse.ArgumentParser(description="SteamSpy appdetails 一键下载器")
    parser.add_argument("appid", type=str, help="Steam AppID，例如 2710380")
    parser.add_argument("--out", type=str, default=None, help="输出目录（默认 ./AppID_<id>/）")
    parser.add_argument("--manual", type=str, default=None,
                        help="手动模式：从本地 JSON 文件读取 SteamSpy 原始返回，绕过 Cloudflare。"
                             "浏览器访问 https://steamspy.com/api.php?request=appdetails&appid=<ID> 保存即可")
    args = parser.parse_args()

    appid = args.appid
    out_dir = args.out or f"./AppID_{appid}"
    out_dir = out_dir.strip().strip('"').strip("'")
    os.makedirs(out_dir, exist_ok=True)

    print(f"🎯 AppID: {appid}")
    print(f"📁 输出: {os.path.abspath(out_dir)}")

    if args.manual:
        # 手动模式：直接读本地 JSON
        manual_path = args.manual.strip().strip('"').strip("'")
        print(f"📥 数据源: 手动模式（从 {manual_path} 读取）")
        print("─" * 60)
        try:
            with open(manual_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            print("✅ 手动 JSON 读取成功")
        except Exception as e:
            print(f"💥 读取失败: {e}")
            sys.exit(3)
    else:
        print(f"🌐 数据源: SteamSpy（第三方估算，非 Steam 官方）")
        print("─" * 60)
        raw = fetch_steamspy(appid)
        if raw is None:
            print("💥 请求失败，已放弃")
            print("💡 提示: SteamSpy 可能被 Cloudflare 拦截。可用手动模式绕过：")
            print(f"   1. 浏览器访问 https://steamspy.com/api.php?request=appdetails&appid={appid}")
            print(f"   2. 保存为 raw.json")
            print(f"   3. 重跑: python fetch_steamspy.py {appid} --out {out_dir} --manual raw.json")
            sys.exit(2)

    # SteamSpy 找不到游戏时会返回 {"appid":0, "name":null, ...}
    if not raw.get("name"):
        print(f"⚠️  SteamSpy 未收录该 AppID（可能太新或已下架）")
        # 仍然保存原始返回，方便排查
        # 但不 sys.exit，让上层流程继续

    now = int(time.time())
    now_iso = datetime.datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S")

    # 提炼有价值的字段
    tags = raw.get("tags", {}) or {}
    # tags 有时候是 [] 空 list（无标签数据）
    if isinstance(tags, list):
        tags = {}

    payload = {
        "appid": appid,
        "fetched_at": now,
        "fetched_at_iso": now_iso,
        "raw": raw,
        "name": raw.get("name") or "",
        "owners": raw.get("owners") or "",
        "ccu": raw.get("ccu", 0) or 0,
        "tags": tags,
        "positive": raw.get("positive", 0) or 0,
        "negative": raw.get("negative", 0) or 0,
        "languages": raw.get("languages") or "",
    }

    out_file = os.path.join(out_dir, "steamspy.json")
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    # 友好输出
    print(f"🎮 游戏名: {payload['name'] or '(未收录)'}")
    print(f"👥 拥有量估算: {payload['owners'] or '—'}")
    print(f"📡 24h 峰值同时在线 (CCU): {payload['ccu']:,} 人")
    print(f"👍 好评: {payload['positive']:,} · 👎 差评: {payload['negative']:,}")
    if tags:
        top_tags = sorted(tags.items(), key=lambda x: -x[1])[:5]
        tags_preview = ", ".join(f"{k}({v})" for k, v in top_tags)
        print(f"🏷  热门玩家标签 (Top 5): {tags_preview}")
        print(f"   全部标签数: {len(tags)}")
    else:
        print(f"🏷  玩家标签: 无数据")
    print(f"⏰ 归档时间: {now_iso}")
    print(f"📄 保存到: {out_file}")


if __name__ == "__main__":
    main()
