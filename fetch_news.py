#!/usr/bin/env python3
"""
Steam 游戏新闻一键下载器（GetNewsForApp API 版）
用法:
  python fetch_news.py <AppID>                    # 默认拉 500 条
  python fetch_news.py <AppID> --max 100
  python fetch_news.py <AppID> --all              # 翻页拿全部历史
  python fetch_news.py <AppID> --lang english
  python fetch_news.py <AppID> --out ./AppID_2710380

说明:
  - API: api.steampowered.com/ISteamNews/GetNewsForApp/v2/
  - 无需 API Key
  - 相比旧 RSS Feed（~10 条硬性封顶），此 API 一次可拉 500 条并支持翻页
  - 保留 BBCode 富文本（[p][img][dynamiclink]），转成基础 HTML
"""
import argparse
import datetime
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import urllib.error

MAX_RETRIES = 3
TIMEOUT = 30
PAGE_SIZE = 500
REQUEST_INTERVAL = 1.5
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


def fetch_page(appid, count, enddate=None, feeds=None, lang="schinese"):
    """调用 GetNewsForApp API 拉一页"""
    params = {
        "appid": str(appid),
        "count": str(count),
        "maxlength": "0",
        "format": "json",
        "l": lang,
    }
    if enddate:
        params["enddate"] = str(enddate)
    if feeds:
        params["feeds"] = feeds
    url = (
        "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/?"
        + urllib.parse.urlencode(params)
    )
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw)
        except urllib.error.HTTPError as e:
            wait = 5 * attempt if e.code == 429 else 2
            print(f"  ❌ HTTP {e.code}，{wait}s 后重试 ({attempt}/{MAX_RETRIES})")
            time.sleep(wait)
        except Exception as e:
            print(f"  ❌ 错误: {e}，重试 ({attempt}/{MAX_RETRIES})")
            time.sleep(2)
    return None


def bbcode_to_html(bbcode):
    """将 Steam BBCode 富文本转成基础 HTML（保留归档价值，安全离线可用）"""
    if not bbcode:
        return ""
    s = bbcode
    # 🔧 Bug 修复 #1：Steam 图片占位符替换
    # Steam BBCode 里的图片用 {STEAM_CLAN_IMAGE} 作为 CDN 前缀占位符
    # 实际 URL = https://clan.fastly.steamstatic.com/images/
    s = s.replace("{STEAM_CLAN_IMAGE}", "https://clan.fastly.steamstatic.com/images")
    # 段落 & 换行
    s = re.sub(r'\[p\](.*?)\[/p\]', r'<p>\1</p>', s, flags=re.DOTALL)
    s = re.sub(r'\[br\]|\[/br\]', '<br>', s)
    # 图片：兼容 [img src="..."][/img] 和 [img]...[/img] 两种格式
    s = re.sub(r'\[img\s+src=\\?"([^"\\]+)\\?"[^\]]*\]\[/img\]',
               r'<img src="\1" style="max-width:100%;">', s)
    s = re.sub(r'\[img\](.*?)\[/img\]',
               r'<img src="\1" style="max-width:100%;">', s, flags=re.DOTALL)
    # 视频/链接
    s = re.sub(r'\[dynamiclink\s+href=\\?"([^"\\]+)\\?"[^\]]*\]\[/dynamiclink\]',
               r'<a href="\1" target="_blank">🔗 \1</a>', s)
    s = re.sub(r'\[previewyoutube=\\?"([^;"\\]+)[^\]]*\]\[/previewyoutube\]',
               r'<a href="https://www.youtube.com/watch?v=\1" target="_blank">▶️ YouTube 视频</a>', s)
    # 列表：先转 [list] → <ul>，再转 [*]...[/*] → <li>...</li>
    s = re.sub(r'\[list\](.*?)\[/list\]', r'<ul>\1</ul>', s, flags=re.DOTALL)
    s = re.sub(r'\[olist\](.*?)\[/olist\]', r'<ol>\1</ol>', s, flags=re.DOTALL)
    # 🔧 Bug 修复 #2：[*]...[/*] 完整包裹（Steam 用 [/*] 显式关闭列表项）
    s = re.sub(r'\[\*\]\s*(.*?)\[/\*\]', r'<li>\1</li>', s, flags=re.DOTALL)
    # 兜底：没有 [/*] 的裸 [*] 也要处理
    s = re.sub(r'\[\*\]\s*(.*?)(?=\[\*\]|</ul>|</ol>|$)', r'<li>\1</li>', s, flags=re.DOTALL)
    # 基础格式
    s = re.sub(r'\[b\](.*?)\[/b\]', r'<b>\1</b>', s, flags=re.DOTALL)
    s = re.sub(r'\[i\](.*?)\[/i\]', r'<i>\1</i>', s, flags=re.DOTALL)
    s = re.sub(r'\[u\](.*?)\[/u\]', r'<u>\1</u>', s, flags=re.DOTALL)
    # 🆕 Steam BBCode 扩展标签：标题 / 删除线 / 引用 / 分隔线 / 代码 / 剧透折叠
    s = re.sub(r'\[h1\](.*?)\[/h1\]', r'<div class="bb-h1">\1</div>', s, flags=re.DOTALL)
    s = re.sub(r'\[h2\](.*?)\[/h2\]', r'<div class="bb-h2">\1</div>', s, flags=re.DOTALL)
    s = re.sub(r'\[h3\](.*?)\[/h3\]', r'<div class="bb-h3">\1</div>', s, flags=re.DOTALL)
    s = re.sub(r'\[strike\](.*?)\[/strike\]', r'<s>\1</s>', s, flags=re.DOTALL)
    s = re.sub(r'\[s\](.*?)\[/s\]', r'<s>\1</s>', s, flags=re.DOTALL)
    s = re.sub(r'\[hr\]\s*(?:\[/hr\])?', '<hr class="bb-hr">', s)
    s = re.sub(r'\[code\](.*?)\[/code\]', r'<pre class="bb-code">\1</pre>', s, flags=re.DOTALL)
    s = re.sub(r'\[quote=([^\]]+)\]', r'<blockquote class="bb-quote"><small>\1</small><br>', s)
    s = re.sub(r'\[quote\](.*?)\[/quote\]', r'<blockquote class="bb-quote">\1</blockquote>', s, flags=re.DOTALL)
    s = re.sub(r'\[quote\]', '<blockquote class="bb-quote">', s)
    s = re.sub(r'\[/quote\]', '</blockquote>', s)
    # 剧透折叠：Steam 上默认隐藏、点击展开 → 离线页保留同样交互
    s = re.sub(r'\[spoiler\](.*?)\[/spoiler\]',
               r'<details class="bb-spoiler"><summary>⚠️ 剧透内容 · 点击展开</summary><div class="bb-spoiler-body">\1</div></details>',
               s, flags=re.DOTALL | re.IGNORECASE)
    # 表格（Steam [table][tr][th][td] 语法）
    s = re.sub(r'\[table\]', '<table class="bb-table">', s)
    s = re.sub(r'\[/table\]', '</table>', s)
    s = re.sub(r'\[tr\]', '<tr>', s)
    s = re.sub(r'\[/tr\]', '</tr>', s)
    s = re.sub(r'\[th\]', '<th>', s)
    s = re.sub(r'\[/th\]', '</th>', s)
    s = re.sub(r'\[td\]', '<td>', s)
    s = re.sub(r'\[/td\]', '</td>', s)
    # [noparse] 包裹的内容不需要 BBCode 解析 → 直接去掉标签保留原文
    s = re.sub(r'\[/?noparse\]', '', s)
    # URL：Steam 允许带引号的参数，先去掉参数外层引号再转义 HTML 属性。
    def url_link(match):
        import html
        url = html.unescape(match.group(1)).strip()
        url = url.replace('\\"', '"').replace("\\'", "'")
        if len(url) >= 2 and url[0] in ('"', "'") and url[-1] == url[0]:
            url = url[1:-1].strip()
        label = match.group(2) if match.lastindex == 2 else html.escape(url)
        return f'<a href="{html.escape(url, quote=True)}" target="_blank">{label}</a>'

    s = re.sub(r'\[url=([^\]]+)\](.*?)\[/url\]', url_link, s, flags=re.DOTALL)
    s = re.sub(r'\[url\](.*?)\[/url\]', url_link, s, flags=re.DOTALL)
    # 清理剩余的未识别 BBCode 标签（含 [/*]、[/list] 等）
    s = re.sub(r'\[/?\*\]', '', s)
    s = re.sub(r'\[/?[a-zA-Z][^\]]*\]', '', s)
    return s


def normalize_item(item):
    """把 GetNewsForApp 单条转成统一格式（与旧 RSS 版本 JSON 结构兼容）"""
    date_ts = item.get("date", 0) or 0
    pub_date_iso = ""
    pub_date_rfc = ""
    if date_ts:
        try:
            dt = datetime.datetime.fromtimestamp(date_ts)
            pub_date_iso = dt.strftime("%Y-%m-%d")
            pub_date_rfc = dt.strftime("%a, %d %b %Y %H:%M:%S +0000")
        except Exception:
            pass

    contents = item.get("contents", "") or ""
    # 🏛️ 架构重构：归档层只存原始 BBCode，渲染层负责转 HTML
    # 好处：以后修 BBCode 转换 bug 只需重渲染，无需重新联网 fetch
    # description_html 保留是为了向后兼容旧 news.json 消费者

    return {
        "gid": item.get("gid", ""),
        "title": item.get("title", "") or "",
        "link": item.get("url", "") or "",
        "pub_date": pub_date_rfc,
        "pub_date_iso": pub_date_iso,
        "contents_bbcode": contents,  # 🏛️ 原汁原味 BBCode 归档（无损）
        "description_html": bbcode_to_html(contents),  # 兼容字段（有损转换）
        "guid": item.get("gid", ""),
        # GetNewsForApp 独有字段
        "author": item.get("author", "") or "",
        "feedlabel": item.get("feedlabel", "") or "",
        "feedname": item.get("feedname", "") or "",
        "tags": item.get("tags", []) or [],
        "is_external_url": item.get("is_external_url", False),
        "date_ts": date_ts,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Steam GetNewsForApp API 一键下载器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("appid", type=str, help="Steam AppID")
    parser.add_argument("--lang", type=str, default="schinese",
                        help="新闻语言（默认 schinese）")
    parser.add_argument("--cc", type=str, default="CN",
                        help="国家代码（兼容旧参数，API 实际不使用）")
    parser.add_argument("--max", type=int, default=PAGE_SIZE,
                        help=f"最多条数（默认 {PAGE_SIZE}）")
    parser.add_argument("--all", action="store_true",
                        help="翻页拿全部历史（覆盖 --max）")
    parser.add_argument("--feeds", type=str, default=None,
                        help="新闻源过滤（逗号分隔），如 steam_community_announcements,steam_updates")
    parser.add_argument("--out", type=str, default=None,
                        help="输出目录（默认 ./AppID_<id>/）")
    args = parser.parse_args()

    appid = args.appid
    out_dir = args.out or f"./AppID_{appid}"
    os.makedirs(out_dir, exist_ok=True)

    print(f"🎯 AppID: {appid}")
    print(f"📁 输出: {os.path.abspath(out_dir)}")
    print(f"🌍 语言: {args.lang}")
    print(f"🔢 目标: {'全部（翻页）' if args.all else args.max}")
    if args.feeds:
        print(f"📚 过滤: {args.feeds}")
    print("─" * 60)

    # 🔁 断点续传：读旧 news.json 拿已有 gid + 最新一条时间戳
    out_file = os.path.join(out_dir, "news.json")
    old_items = []
    old_gids = set()
    old_latest_ts = 0
    if os.path.exists(out_file):
        try:
            with open(out_file, "r", encoding="utf-8") as f:
                old_data = json.load(f)
            old_items = old_data.get("items", []) or []
            for it in old_items:
                gid = it.get("gid") or it.get("guid") or ""
                if gid:
                    old_gids.add(gid)
                ts = it.get("date_ts", 0) or 0
                if ts > old_latest_ts:
                    old_latest_ts = ts
            if old_items:
                print(f"🔁 断点续传：已有 {len(old_items)} 条，最新 {datetime.datetime.fromtimestamp(old_latest_ts).strftime('%Y-%m-%d')}，只拉更新增量")
        except Exception as e:
            print(f"⚠️  读旧 news.json 失败，走全量拉取: {e}")
            old_items = []
            old_gids = set()
            old_latest_ts = 0

    all_items = []
    seen_gids = set()
    enddate = None
    page = 0
    hit_history = False  # 命中历史即停止翻页
    t0 = time.time()

    while True:
        page += 1
        page_count = PAGE_SIZE if args.all else min(PAGE_SIZE, args.max - len(all_items))
        if page_count <= 0:
            break
        end_str = f" (enddate={enddate})" if enddate else ""
        print(f"📄 第 {page} 页{end_str}")
        data = fetch_page(appid, page_count, enddate=enddate,
                          feeds=args.feeds, lang=args.lang)
        if data is None:
            print("  💥 本页失败，中止")
            break

        appnews = data.get("appnews", {}) or {}
        items = appnews.get("newsitems", []) or []
        if not items:
            print("  ℹ️  0 条，翻页结束")
            break

        new_count = 0
        dup_count = 0
        hist_count = 0
        oldest_date = None
        for it in items:
            gid = it.get("gid", "")
            # 本次会话内去重
            if gid and gid in seen_gids:
                dup_count += 1
                continue
            # 🔁 断点续传：命中旧库里的 gid = 追上历史，标记停翻页
            if gid and gid in old_gids:
                hist_count += 1
                hit_history = True
                continue
            if gid:
                seen_gids.add(gid)
            norm = normalize_item(it)
            all_items.append(norm)
            new_count += 1
            d = norm.get("date_ts", 0)
            if d and (oldest_date is None or d < oldest_date):
                oldest_date = d

        note_parts = []
        if dup_count:
            note_parts.append(f"去重 {dup_count}")
        if hist_count:
            note_parts.append(f"命中历史 {hist_count}")
        note = f"（{', '.join(note_parts)}）" if note_parts else ""
        print(f"  ✅ 本页 {len(items)} 条{note}，累计新增 {len(all_items)} 条")

        # 🔁 已追上历史，无需继续翻页
        if hit_history:
            print("  🔁 已追上历史记录，停止翻页")
            break

        # 达到上限
        if not args.all and len(all_items) >= args.max:
            all_items = all_items[:args.max]
            print(f"  🎯 达到上限 {args.max}，停止翻页")
            break

        # 翻页：本页没新增说明翻到头了
        if new_count == 0:
            print("  ℹ️  本页无新增，翻页结束")
            break

        # 翻页需要 --all 才继续
        if not args.all:
            break

        # 用最老一条的时间戳 - 1 作为下一页 enddate
        if oldest_date is None:
            break
        enddate = oldest_date - 1
        time.sleep(REQUEST_INTERVAL)

    # 🔁 断点续传：merge 新增 + 旧库，按 gid 去重后按时间降序
    new_fetched = len(all_items)
    merged = list(all_items)
    merged_gids = set(seen_gids)
    for it in old_items:
        gid = it.get("gid") or it.get("guid") or ""
        if gid and gid in merged_gids:
            continue
        if gid:
            merged_gids.add(gid)
        merged.append(it)
    merged.sort(key=lambda x: x.get("date_ts", 0), reverse=True)
    all_items = merged

    now = int(time.time())
    now_iso = datetime.datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S")

    payload = {
        "appid": appid,
        "language": args.lang,
        "cc": args.cc,
        "fetched_at": now,
        "fetched_at_iso": now_iso,
        "count": len(all_items),
        "items": all_items,
        "_meta": {
            "api": "GetNewsForApp/v2",
            "pages": page,
            "new_fetched": new_fetched,
            "old_kept": len(all_items) - new_fetched,
            "total_fetched": len(all_items),
        },
    }

    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    elapsed = time.time() - t0
    print("─" * 60)
    print(f"✨ 完成，耗时 {elapsed:.1f}s")
    print(f"   ✅ 拉取新闻: {len(all_items)} 条")
    if all_items:
        latest = all_items[0]
        oldest = all_items[-1]
        print(f"   📅 时间跨度: {oldest.get('pub_date_iso', '?')} → {latest.get('pub_date_iso', '?')}")
    print(f"   📄 保存到: {out_file}")

    # 打印前 10 条标题
    print("\n📰 最新 10 条：")
    for i, it in enumerate(all_items[:10], 1):
        title = it["title"][:60] + ("..." if len(it["title"]) > 60 else "")
        tags = f" [{','.join(it['tags'])}]" if it.get('tags') else ""
        print(f"  {i}. [{it.get('pub_date_iso', '?')}] {title}{tags}")
    if len(all_items) > 10:
        print(f"  ... 及其他 {len(all_items) - 10} 条")

    if not all_items:
        sys.exit(2)


if __name__ == "__main__":
    main()
