#!/usr/bin/env python3
"""
Steam 游戏新闻详情（含中文）一键下载器

数据源：
  1. news/app/{appid} HTML 首屏 → 拿 clanid + gid 索引
  2. ajaxgeteventdetails?lang_list=6_0 → 拿中文 announcement_body

归档层忠实搬运：event 原样保存到 news_details_zh.json
"""
import argparse
import datetime
import html as htmlmod
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error

MAX_RETRIES = 3
TIMEOUT = 30
REQUEST_INTERVAL = 1.5
BATCH_SIZE = 30  # 铁证来自 Steam 前端源码 chunk~beb94d585.js:
                 #   if (this.m_rgQueuedEventsClanIDs.length >= 30) ...
                 # 官方一批上限就是 30 条
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# Steam 内部语言编号（实测验证，2026 版）
# 铁证来源：ajaxgeteventdetails?lang_list={code}_0 返回的 announcement_body.language
LANG_CODE = {
    "english": 0,
    "french": 2,
    "italian": 3,
    "koreana": 4,
    "spanish": 5,
    "schinese": 6,
    "tchinese": 7,
    "russian": 8,
    "thai": 9,
    "japanese": 10,
    "portuguese": 11,
    "polish": 12,
    "danish": 13,
    "dutch": 14,
    "finnish": 15,
    "norwegian": 16,
    "swedish": 17,
    "hungarian": 18,
    "czech": 19,
    "romanian": 20,
    "turkish": 21,
    "brazilian": 22,
    "bulgarian": 23,
    "greek": 24,
    "ukrainian": 26,
    "latam": 27,
    "vietnamese": 28,
}

LANG_TAG = {
    "english": "en", "french": "fr", "italian": "it",
    "koreana": "ko", "spanish": "es",
    "schinese": "zh", "tchinese": "tw",
    "russian": "ru", "thai": "th", "japanese": "ja",
    "portuguese": "pt", "polish": "pl",
    "danish": "da", "dutch": "nl", "finnish": "fi",
    "norwegian": "no", "swedish": "sv", "hungarian": "hu",
    "czech": "cs", "romanian": "ro", "turkish": "tr",
    "brazilian": "pt-br", "bulgarian": "bg", "greek": "el",
    "ukrainian": "uk", "latam": "es-419", "vietnamese": "vi",
}


def http_get(url, headers=None, timeout=TIMEOUT):
    base_headers = {"User-Agent": USER_AGENT}
    if headers:
        base_headers.update(headers)
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers=base_headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            wait = 5 * attempt if e.code == 429 else 2
            print(f"  ❌ HTTP {e.code}，{wait}s 后重试 ({attempt}/{MAX_RETRIES})")
            time.sleep(wait)
        except Exception as e:
            print(f"  ❌ 错误: {e}，重试 ({attempt}/{MAX_RETRIES})")
            time.sleep(2)
    return None


def fetch_index_from_html(appid, lang="schinese"):
    """
    从 news/app/{appid} HTML 首屏抓公告索引
    返回: [{"clanid": int, "gid": str, "event_type": int, "start_time": int}, ...]
    """
    url = f"https://store.steampowered.com/news/app/{appid}?updates=true&l={lang}"
    print(f"📥 拉 HTML 首屏索引: {url}")
    body = http_get(url, headers={"Referer": "https://store.steampowered.com/"})
    if body is None:
        return []
    decoded = htmlmod.unescape(body)
    pattern = r'\{"clanid":(\d+),"unique_id":"(\d+)","event_type":(\d+),"appid":(\d+),"start_time":(\d+)'
    matches = re.findall(pattern, decoded)
    index = []
    seen = set()
    for clanid, gid, event_type, aid, start_time in matches:
        if gid in seen:
            continue
        seen.add(gid)
        index.append({
            "clanid": int(clanid),
            "gid": gid,
            "event_type": int(event_type),
            "start_time": int(start_time),
        })
    index.sort(key=lambda x: x["start_time"], reverse=True)
    return index


def fetch_details_batch(clanids, gids, lang_code=6, fallback_code=0):
    """
    调 ajaxgeteventdetails 拉一批详情
    lang_list=<lang_code>_<fallback_code>，例如 6_0 表示中文优先，无则英文
    返回 events 数组（原样，不做任何字段变形）
    """
    clan_str = ",".join(str(c) for c in clanids)
    gid_str = ",".join(gids)
    lang_str = f"{lang_code}_{fallback_code}"
    url = (
        f"https://store.steampowered.com/events/ajaxgeteventdetails/"
        f"?clanid_list={clan_str}"
        f"&uniqueid_list={gid_str}"
        f"&lang_list={lang_str}"
        f"&origin=https:%2F%2Fstore.steampowered.com"
    )
    body = http_get(url, headers={
        "Referer": "https://store.steampowered.com/",
        "X-Requested-With": "XMLHttpRequest",
    })
    if body is None:
        return None
    try:
        data = json.loads(body)
    except Exception as e:
        print(f"  ❌ JSON 解析失败: {e}")
        return None
    if not data.get("success"):
        print(f"  ⚠️ API success != 1，msg={data.get('msg', '?')}")
    return data.get("events", []) or []


def main():
    ap = argparse.ArgumentParser(description="Steam 新闻详情下载器（含中文）")
    ap.add_argument("appid", help="Steam AppID")
    ap.add_argument("--lang", default="schinese", help="首选语言（默认 schinese）")
    ap.add_argument("--fallback", default="english", help="回退语言（默认 english）")
    ap.add_argument("--max", type=int, default=0, help="最多拉取多少条（0=全部）")
    ap.add_argument("--batch", type=int, default=BATCH_SIZE, help=f"每批多少条（默认 {BATCH_SIZE}）")
    ap.add_argument("--out", default=None, help="输出目录（默认 <AppID>/）")
    args = ap.parse_args()

    appid = args.appid.strip()
    if not appid.isdigit():
        print(f"❌ AppID 必须是数字：{appid}")
        sys.exit(1)

    lang_code = LANG_CODE.get(args.lang.lower())
    fallback_code = LANG_CODE.get(args.fallback.lower(), 0)
    if lang_code is None:
        print(f"❌ 不支持的语言：{args.lang}")
        print(f"   支持: {', '.join(LANG_CODE.keys())}")
        sys.exit(1)

    out_dir = args.out or appid
    os.makedirs(out_dir, exist_ok=True)

    print("=" * 60)
    print(f"📰 Steam 新闻详情下载器")
    print(f"   AppID: {appid}")
    print(f"   语言: {args.lang}({lang_code}) → fallback: {args.fallback}({fallback_code})")
    print(f"   批次: {args.batch} · 输出: {out_dir}")
    print("─" * 60)

    t0 = time.time()

    # Step 1: 拿索引
    index = fetch_index_from_html(appid, lang=args.lang)
    if not index:
        print("💥 索引为空")
        sys.exit(2)
    print(f"✅ 拿到 {len(index)} 条公告索引")

    if args.max > 0:
        index = index[:args.max]
        print(f"🎯 应用 --max {args.max}，处理 {len(index)} 条")

    # Step 2: 分批拉详情
    print("─" * 60)
    print(f"📥 分批拉详情（每批 {args.batch} 条，间隔 {REQUEST_INTERVAL}s）")

    all_events = []
    total_batches = (len(index) + args.batch - 1) // args.batch
    lang_stats = {"target": 0, "fallback": 0, "other": 0}

    for i in range(0, len(index), args.batch):
        batch = index[i:i + args.batch]
        batch_no = i // args.batch + 1
        clanids = [item["clanid"] for item in batch]
        gids = [item["gid"] for item in batch]
        print(f"📄 批次 {batch_no}/{total_batches} · {len(batch)} 条")

        events = fetch_details_batch(clanids, gids,
                                     lang_code=lang_code,
                                     fallback_code=fallback_code)
        if events is None:
            print("  💥 本批失败，跳过")
            continue

        for ev in events:
            ann = ev.get("announcement_body", {}) or {}
            lang = ann.get("language")
            if lang == lang_code:
                lang_stats["target"] += 1
            elif lang == fallback_code:
                lang_stats["fallback"] += 1
            else:
                lang_stats["other"] += 1
            all_events.append(ev)

        print(f"  ✅ 拿到 {len(events)} 条，累计 {len(all_events)}")

        if batch_no < total_batches:
            time.sleep(REQUEST_INTERVAL)

    # Step 3: 写文件
    now = int(time.time())
    now_iso = datetime.datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S")
    lang_tag = LANG_TAG.get(args.lang.lower(), args.lang.lower())

    payload = {
        "appid": appid,
        "lang": args.lang,
        "lang_code": lang_code,
        "fallback": args.fallback,
        "fallback_code": fallback_code,
        "fetched_at": now,
        "fetched_at_iso": now_iso,
        "count": len(all_events),
        "lang_stats": lang_stats,
        "events": all_events,
        "_meta": {
            "api": "ajaxgeteventdetails",
            "index_source": "news/app/{appid} HTML documents[]",
            "batch_size": args.batch,
            "total_batches": total_batches,
        },
    }

    out_file = os.path.join(out_dir, f"news_details_{lang_tag}.json")
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    elapsed = time.time() - t0
    print("─" * 60)
    print(f"✨ 完成，耗时 {elapsed:.1f}s")
    print(f"   ✅ 拉取详情: {len(all_events)} 条")
    print(f"   🎯 目标语言({args.lang}): {lang_stats['target']} 条")
    print(f"   🔄 回退语言({args.fallback}): {lang_stats['fallback']} 条")
    print(f"   ❓ 其他语言: {lang_stats['other']} 条")
    print(f"   📄 保存到: {out_file}")

    # 打印前 5 条预览
    if all_events:
        print("\n📰 前 5 条预览：")
        for i, ev in enumerate(all_events[:5], 1):
            ann = ev.get("announcement_body", {}) or {}
            head = ann.get("headline", "")[:60]
            lang = ann.get("language", "?")
            print(f"  {i}. [lang={lang}] {head}")

    if not all_events:
        sys.exit(2)


if __name__ == "__main__":
    main()
