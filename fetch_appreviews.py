#!/usr/bin/env python3
"""
Steam appreviews 玩家评论一键下载器
用法:
  python fetch_appreviews.py <AppID>                    # 默认拉前 20 条最有帮助
  python fetch_appreviews.py <AppID> --all              # 翻页拉取全部
  python fetch_appreviews.py <AppID> --max 100
  python fetch_appreviews.py <AppID> --lang schinese
  python fetch_appreviews.py <AppID> --sort recent      # helpful/recent/funny
  python fetch_appreviews.py <AppID> --with-avatars
  python fetch_appreviews.py <AppID> --out ./Reviews
说明: 旧 appreviews 接口已下线；优先用官方新接口 IUserReviewsService（匿名可用），失败自动兜底 steamcommunity.com 评论页
"""
import argparse
import hashlib
import html
import json
import os
import re
import shutil
import socket
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timedelta

# Windows 控制台/管道编码兜底：输出 emoji 遇到 GBK 等编码时不崩（降级为 ?）
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

PAGE_SIZE = 100
REQUEST_INTERVAL = 1.5
MAX_RETRIES = 3
TIMEOUT = 20
DEFAULT_MAX_REVIEWS = 20
MAX_COMMUNITY_PAGES = 500  # 社区兜底翻页安全上限（每页约 10 条）
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
# 请求头：部分代理/风控按头部特征过滤，补齐 Accept 系列减少空响应概率
API_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}
# 🐛 修复：Steam filter=all 只暴露"最有帮助的前 ~100 条"（硬性限制）
# 要拉全部评论，--all 模式下必须用 filter=recent 或 filter=updated
# helpful 模式保持 filter=all（少量最有帮助，适合默认 20 条场景）
SORT_MAP = {"helpful": "all", "recent": "recent", "funny": "funny", "updated": "updated"}

# ── 官方新接口（旧 store /appreviews 已于 2026-10-22 被 Valve 下线）──
# 文档: https://partner.steamgames.com/doc/webapi/IUserReviewsService
# 匿名可用（无需 key）；字段与旧接口基本一致，但不返回 personaname / avatar /
# num_games_owned → 稍后由社区评论页按 recommendationid 尽力补齐。
API2_URL = "https://api.steampowered.com/IUserReviewsService/GetAppReviews/v1/"
FILTER_MAP_V2 = {"helpful": 0, "recent": 1, "updated": 2, "funny": 3}

# 🔒 本次会话是否命中“接口被重定向/拦截”（302 → 商店首页）
# 命中后即便抓到 0 条，也绝不覆盖已有 appreviews.json
LAST_FETCH_BLOCKED = False

# ── 本地代理（Clash/V2Ray 等）自动探测 ──
# 场景：steamcommunity.com 等域名在部分网络下直连不稳，
# 本地代理（如 Clash 的 127.0.0.1:7897）通常可用；探测到就显式使用。
_LOCAL_PROXY_PORTS = (7897, 7890, 10809, 10808, 1080, 8889, 2080)


def _detect_local_proxy():
    for port in _LOCAL_PROXY_PORTS:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.35):
                return f"http://127.0.0.1:{port}"
        except OSError:
            continue
    return ""


def init_proxy(prefer=None, disable=False):
    """初始化 HTTP 代理（影响本进程所有 urllib 请求）。
    prefer: 显式指定的代理地址；disable: 禁用代理（纯直连）。
    未指定时自动探测本地常见代理端口。"""
    if disable:
        print("🌐 代理: 已禁用（纯直连）")
        return ""
    proxy = prefer or _detect_local_proxy()
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        urllib.request.install_opener(opener)
        print(f"🌐 代理: {proxy}")
    else:
        print("🌐 代理: 未检测到本地代理（使用系统默认）")
    return proxy


def fetch_page(appid, cursor, sort_filter, language):
    # 🐛 修复：cursor 里含 + / = 等特殊字符，必须用 quote(safe='') 精确编码
    # 用 urlencode 会导致 Steam 服务端"看不懂" cursor，退回第一页数据 → 翻页失效
    params = {
        "json": "1", "filter": sort_filter, "language": language,
        "num_per_page": str(PAGE_SIZE),
        "purchase_type": "all",
    }
    qs = urllib.parse.urlencode(params)
    # cursor 单独用 quote(safe='') 编码，确保 +/= 被正确保留
    qs += "&cursor=" + urllib.parse.quote(cursor, safe="")
    url = f"https://store.steampowered.com/appreviews/{appid}?{qs}"
    blocked = False
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers=API_HEADERS)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read()
            # 302 → 跟随到首页时 body 为空或直接是 HTML；这两种都说明接口不接受本次请求
            text = raw.decode("utf-8-sig", errors="replace").lstrip()
            if not text or text[:1] != "{":
                blocked = True
                snippet = text[:80].replace("\r", " ").replace("\n", " ")
                if attempt == 1:
                    print(f"  🔒 接口未返回 JSON（疑似被重定向/拦截）: {snippet or '(空响应)'}")
                    print(f"      2s 后快速重试一次...")
                    time.sleep(2)
                    continue
                print(f"  🔒 二次确认仍未返回 JSON → 判定为接口级拦截，稍后启用社区页兜底")
                break
            try:
                data = json.loads(text)
            except json.JSONDecodeError as e:
                snippet = text[:120].replace("\r", " ").replace("\n", " ")
                print(f"  ⚠️  JSON 解析失败: {e} | 片段: {snippet}")
                time.sleep(2)
                continue
            if data.get("success") != 1:
                print(f"  ⚠️  API 返回 success != 1")
                return None
            return data
        except urllib.error.HTTPError as e:
            wait = 5 * attempt if e.code == 429 else 2
            print(f"  ❌ HTTP {e.code}，{wait}s 后重试 ({attempt}/{MAX_RETRIES})")
            time.sleep(wait)
        except Exception as e:
            print(f"  ❌ 错误: {e}，重试 ({attempt}/{MAX_RETRIES})")
            time.sleep(2)
    if blocked:
        globals()["LAST_FETCH_BLOCKED"] = True
    return None


def fetch_page_v2(appid, cursor, sort_key, language):
    """官方新接口 IUserReviewsService.GetAppReviews（替代已下线的 store /appreviews）。
    匿名可用；返回 {"reviews": [...], "query_summary": {...}, "cursor": ...} 或 None。
    """
    payload = {
        "appid": int(appid),
        "filter": FILTER_MAP_V2.get(sort_key, 1),
        "purchase_type": 1,               # 1 = All（含非 Steam 购买，与旧 purchase_type=all 对齐）
        "num_per_page": 100,
        "cursor": cursor or "*",
        "languages": ["all"] if (not language or language == "all") else [language],
    }
    if FILTER_MAP_V2.get(sort_key, 1) == 0:
        # Helpful 过滤器默认仅查最近 30 天；0 = 不限时段（与旧接口 filter=all 行为对齐）
        payload["day_range"] = 0
    qs = urllib.parse.urlencode(
        {"input_json": json.dumps(payload, separators=(",", ":"))})
    url = f"{API2_URL}?{qs}"
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers=API_HEADERS)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read()
            text = raw.decode("utf-8-sig", errors="replace").lstrip()
            if not text or text[:1] != "{":
                snippet = text[:80].replace("\r", " ").replace("\n", " ")
                print(f"  ⚠️ 新接口未返回 JSON: {snippet or '(空响应)'}")
                time.sleep(3)
                continue
            data = json.loads(text)
            return data.get("response", {}) or {}
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = 10 * attempt
                print(f"  ⏳ 匿名接口限流（HTTP 429），{wait}s 后重试 ({attempt}/{MAX_RETRIES})")
            else:
                wait = 3
                print(f"  ❌ HTTP {e.code}，{wait}s 后重试 ({attempt}/{MAX_RETRIES})")
            time.sleep(wait)
        except Exception as e:
            print(f"  ❌ 错误: {e}，重试 ({attempt}/{MAX_RETRIES})")
            time.sleep(3)
    return None


def _normalize_v2_review(r):
    """新接口 review → 兼容旧结构（昵称/头像暂用占位，稍后由社区页补齐）"""
    r = dict(r)
    a = dict(r.get("author", {}) or {})
    steamid = str(a.get("steamid", "") or "")
    if not a.get("personaname"):
        a["personaname"] = f"用户{steamid[-6:]}" if steamid else "匿名"
    if not a.get("profile_url"):
        a["profile_url"] = f"https://steamcommunity.com/profiles/{steamid}" if steamid else ""
    a.setdefault("avatar", "")
    a.setdefault("num_games_owned", 0)
    r["author"] = a
    return r


# ────────────────────────────────────────────────
# 社区评论页兜底（官方 JSON 接口被 302 拦截时启用）
# ────────────────────────────────────────────────
COMMUNITY_BROWSEFILTER = {
    "helpful": "toprated",
    "recent":  "mostrecent",
    "funny":   "funny",
    "updated": "updated",
}


def _fetch_url(url, timeout=TIMEOUT):
    req = urllib.request.Request(url, headers=API_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8-sig", errors="replace")


def _community_filter_language(lang):
    if not lang or lang == "all":
        return "all"
    return lang


def _community_split_cards(page_html):
    marker = 'class="apphub_Card modalContentLink interactable"'
    parts = page_html.split(marker)
    return parts[1:] if len(parts) > 1 else []


def _community_clean_text(inner):
    """把卡片正文 HTML 清成纯文本（保留换行）"""
    s = inner
    s = re.sub(r'<div class="date_posted">.*?</div>', '', s, flags=re.S)
    s = re.sub(r'<div class="early_access_review">.*?</div>', '', s, flags=re.S)
    s = re.sub(r'<div class="received_compensation">.*?</div>', '', s, flags=re.S)
    s = re.sub(r'<div class="review_award[^"]*">.*?</div>', '', s, flags=re.S)
    s = re.sub(r'<br\s*/?>', '\n', s, flags=re.I)
    s = re.sub(r'</(?:div|p|li|ul|ol|h[1-6])>', '\n', s, flags=re.I)
    s = re.sub(r'<[^>]+>', '', s)
    s = html.unescape(s)
    s = '\n'.join(ln.strip() for ln in s.split('\n'))
    s = re.sub(r'\n{3,}', '\n\n', s).strip()
    return s


_MONTH_NAMES = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}


def _community_parse_date(raw):
    """'6 November' / '6 November, 2024' → Unix 时间戳（精确到天）。
    不用 strptime 解析无年份日期，避免 Python 3.15 行为的 DeprecationWarning。"""
    s = (raw or "").strip().rstrip(".,")
    if not s:
        return 0

    def _mk(year, month, day):
        try:
            return int(datetime(year, month, day).timestamp())
        except (ValueError, OverflowError):
            return 0

    m = re.match(r'^(\d{1,2})\s+([A-Za-z]+),?\s+(\d{4})$', s)
    if m:
        return _mk(int(m.group(3)), _MONTH_NAMES.get(m.group(2).lower(), 0), int(m.group(1)))
    m = re.match(r'^([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})$', s)
    if m:
        return _mk(int(m.group(3)), _MONTH_NAMES.get(m.group(1).lower(), 0), int(m.group(2)))
    m = re.match(r'^(\d{1,2})\s+([A-Za-z]+)$', s)
    if m:
        day, mon = int(m.group(1)), _MONTH_NAMES.get(m.group(2).lower(), 0)
        if not mon:
            return 0
        now = datetime.now()
        try:
            dt = datetime(now.year, mon, day)
        except ValueError:
            return 0
        if dt > now + timedelta(days=2):
            try:
                dt = datetime(now.year - 1, mon, day)
            except ValueError:
                return 0
        return int(dt.timestamp())
    return 0


def _community_parse_card(card, language, rec_id):
    """把单张社区评论卡片解析成 appreviews 兼容结构"""
    def find1(pat, default=""):
        m = re.search(pat, card, re.S)
        return m.group(1).strip() if m else default

    prof_url = find1(r'data-modal-content-url="([^"]+)"')
    author_url = find1(r'apphub_CardContentAuthorName[^>]*>\s*<a href="([^"]*)"')
    # 🐛 昵称里的 HTML 实体（&amp; 等）需要反转义，否则会原样进档案
    name = html.unescape(find1(r'apphub_CardContentAuthorName[^>]*>\s*<a href="[^"]*"[^>]*>([^<]+)</a>'))
    avatar = find1(r'<img src="https://avatars[^"]*/([0-9a-f]+)\.jpg')
    products = find1(r'apphub_CardContentMoreLink[^>]*>\s*([\d,]+) products in account')
    title = find1(r'<div class="title">([^<]+)</div>')
    hours = find1(r'<div class="hours">([\d.,]+) hrs on record')
    cc = find1(r'apphub_CardCommentCount[^>]*>(\d+)<')

    voted_up = "Not Recommended" not in title

    try:
        minutes = int(float(hours.replace(",", "")) * 60)
    except (TypeError, ValueError):
        minutes = 0

    votes_up = 0
    m = re.search(r'([\d,]+)\s+(?:person|people)\s+found this review helpful', card)
    if m:
        votes_up = int(m.group(1).replace(",", ""))
    votes_funny = 0
    m = re.search(r'([\d,]+)\s+(?:person|people)\s+found this review funny', card)
    if m:
        votes_funny = int(m.group(1).replace(",", ""))

    received_free = "received_compensation" in card
    early = 'class="early_access_review"' in card

    date_raw = find1(r'date_posted">\s*Posted:\s*([^<]+)<')
    ts = _community_parse_date(date_raw)

    text = ""
    i0 = card.find('apphub_CardTextContent">')
    i1 = card.find("UserReviewCardContent_Footer", i0 + 1)
    if i0 >= 0 and i1 > i0:
        text = _community_clean_text(card[i0 + len('apphub_CardTextContent">'):i1])

    steamid = ""
    for _src in (author_url, prof_url):
        m = re.search(r'/profiles/(\d+)', _src or "")
        if m:
            steamid = m.group(1)
            break
    if not steamid:
        m = re.search(r'/id/([^/]+)', author_url or prof_url or "")
        if m:
            steamid = m.group(1)

    try:
        num_games = int(products.replace(",", ""))
    except (TypeError, ValueError):
        num_games = 0

    if not rec_id:
        rec_id = "c" + hashlib.md5(
            (name + "|" + date_raw + "|" + text).encode("utf-8", "ignore")
        ).hexdigest()[:12]

    return {
        "recommendationid": rec_id,
        "author": {
            "steamid": steamid,
            "personaname": name or "匿名",
            "persona_status": "",
            "profile_url": author_url or prof_url,
            "num_games_owned": num_games,
            "num_reviews": 0,
            "playtime_forever": minutes,
            "playtime_last_two_weeks": 0,
            "playtime_at_review": minutes,
            "last_played": 0,
            "avatar": avatar,
        },
        "language": "" if (not language or language == "all") else language,
        "review": text,
        "timestamp_created": ts,
        "timestamp_updated": ts,
        "voted_up": voted_up,
        "votes_up": votes_up,
        "votes_funny": votes_funny,
        "weighted_vote_score": 0.0,
        "comment_count": int(cc) if cc.isdigit() else 0,
        "steam_purchase": False,
        "received_for_free": received_free,
        "refunded": False,
        "written_during_early_access": early,
        "primarily_steam_deck": False,
        "reactions": {},
        "app_release_date": "",
    }


def _community_next_url(page_html, appid, browsefilter, language):
    """从 MoreContentForm{N} 提取隐藏字段，构造下一页 homecontent URL。
    ⚠️ 表单编号每翻一页递增（第 1 页 MoreContentForm1 → 第 2 页 MoreContentForm2 …），
    必须用 \\d+ 匹配，否则第 2 页之后翻不动。"""
    mform = re.search(r'<form[^>]*id="MoreContentForm\d+"[^>]*>(.*?)</form>', page_html, re.S)
    if not mform:
        return None, ""
    fields = dict(re.findall(r'name="([^"]+)" value="([^"]*)"', mform.group(1)))
    if "userreviewscursor" not in fields:
        return None, ""
    fields["l"] = "english"
    fields["filterLanguage"] = _community_filter_language(language)
    fields.setdefault("userreviews", "1")
    fields.setdefault("appHubSubSection", "10")
    fields.setdefault("browsefilter", browsefilter)
    fields.setdefault("numperpage", "10")
    fields.setdefault("forceanon", "1")
    url = f"https://steamcommunity.com/app/{appid}/homecontent/?" + urllib.parse.urlencode(fields)
    return url, fields.get("userreviewscursor", "")


def _community_query_summary(appid, reviews):
    """商店页抓总数/评级/百分比；失败则用本地计数"""
    total = len(reviews)
    positive = sum(1 for r in reviews if r.get("voted_up"))
    negative = total - positive
    desc = ""
    try:
        s = _fetch_url(f"https://store.steampowered.com/app/{appid}/?l=english")
        m = re.search(r'itemprop="reviewCount" content="(\d+)"', s)
        if m:
            total = int(m.group(1))
        m = re.search(r'game_review_summary[^>]*>([^<]+)</span>', s)
        if m:
            desc = m.group(1).strip()
        m = re.search(r'(\d+)% of the ([\d,]+) user reviews', s)
        if m and total:
            pct = int(m.group(1))
            positive = round(total * pct / 100)
            negative = total - positive
    except Exception as e:
        print(f"  ⚠️ 商店页汇总抓取失败（使用本地计数）: {e}")
    if not desc:
        desc = "未知（社区兜底归档）"
    return {
        "num_reviews": total,
        "review_score": 0,
        "review_score_desc": desc,
        "total_positive": positive,
        "total_negative": negative,
        "total_reviews": total,
    }


def fetch_reviews_via_community(appid, sort_key, language, max_reviews):
    """官方 appreviews 不可用时的兜底：抓 steamcommunity.com 评论页。
    返回 (reviews, query_summary)；失败返回 None。
    """
    browsefilter = COMMUNITY_BROWSEFILTER.get(sort_key, "mostrecent")
    if max_reviews is None and sort_key == "helpful":
        print("💡 社区兜底：--all 模式使用 mostrecent 翻页")
        browsefilter = "mostrecent"

    fl = _community_filter_language(language)
    url = (f"https://steamcommunity.com/app/{appid}/reviews/"
           f"?browsefilter={browsefilter}&numperpage=10&p=1&l=english"
           f"&filterLanguage={urllib.parse.quote(fl)}")

    max_pages = MAX_COMMUNITY_PAGES
    if max_reviews is not None:
        max_pages = min(MAX_COMMUNITY_PAGES, (max_reviews + 9) // 10 + 2)

    reviews = []
    seen_ids = set()
    seen_cursors = set()
    page_no = 0
    while url and page_no < max_pages:
        page_no += 1
        page_html = None
        for attempt in range(1, 3):
            try:
                page_html = _fetch_url(url)
                break
            except Exception as e:
                print(f"  ⚠️ 社区页第 {page_no} 页请求失败: {e}（重试 {attempt}/2）")
                time.sleep(3)
        if not page_html:
            break

        cards = _community_split_cards(page_html)
        if not cards:
            print(f"  ℹ️ 社区页第 {page_no} 页没有评论卡片，结束")
            break

        ids = []
        m = re.search(r'RequestCurrentUserRecommendationVotes\(\s*\[([^\]]*)\]', page_html)
        if m:
            ids = [x.strip().strip('"') for x in m.group(1).split(",") if x.strip()]

        new_on_page = 0
        for i, card in enumerate(cards):
            rec = _community_parse_card(card, language, ids[i] if i < len(ids) else "")
            rid = rec.get("recommendationid") or ""
            if rid and rid in seen_ids:
                continue
            if rid:
                seen_ids.add(rid)
            reviews.append(normalize_review(rec))
            new_on_page += 1

        print(f"  ✅ 社区页 {page_no}: 本页 {new_on_page} 条，累计 {len(reviews)} 条")

        if max_reviews is not None and len(reviews) >= max_reviews:
            reviews = reviews[:max_reviews]
            print(f"  🎯 达到上限 {max_reviews}，停止翻页")
            break

        nxt, cur = _community_next_url(page_html, appid, browsefilter, language)
        if not nxt or not cur or cur in seen_cursors:
            break
        seen_cursors.add(cur)
        url = nxt
        time.sleep(REQUEST_INTERVAL)

    if not reviews:
        return None
    summary = _community_query_summary(appid, reviews)
    return reviews, summary


def enrich_via_community(appid, reviews, sort_key="recent", max_pages=120, on_checkpoint=None):
    """官方新接口不返回昵称/头像 → 用社区评论页按 recommendationid 互补填充。
    on_checkpoint(page_no, filled_so_far) 可选：每页回调（用于断点保存）。
    返回已补齐条数。

    断点续跑兼容：把数据集里已补过的评论也视为「已知」，
    熔断只在连续多页「连一条已知评论都遇不到」时才触发（真正越界），
    避免从第 1 页重扫时被"已补区域"误判熔断。
    """
    need = {}
    all_ids = set()
    for r in reviews:
        rid = str(r.get("recommendationid", "") or "")
        if rid:
            all_ids.add(rid)
        a = r.get("author", {}) or {}
        nick = str(a.get("personaname", "") or "")
        if (not nick) or nick.startswith("用户") or not a.get("avatar"):
            if rid:
                need[rid] = r
    if not need:
        return 0

    browsefilter = COMMUNITY_BROWSEFILTER.get(sort_key, "mostrecent")
    url = (f"https://steamcommunity.com/app/{appid}/reviews/"
           f"?browsefilter={browsefilter}&numperpage=10&p=1&l=english&filterLanguage=all")

    budget = min(max_pages, max(8, len(need) // 2 + 8))
    filled = 0
    seen_cursors = set()
    page_no = 0
    empty_streak = 0
    while url and page_no < budget and need:
        page_no += 1
        page_html = None
        for attempt in range(1, 4):
            try:
                page_html = _fetch_url(url)
                break
            except Exception as e:
                print(f"    ⚠️ 补齐第 {page_no} 页失败: {e}（重试 {attempt}/3）")
                time.sleep(2 + attempt)
        if not page_html:
            break
        cards = _community_split_cards(page_html)
        if not cards:
            break
        ids = []
        m = re.search(r'RequestCurrentUserRecommendationVotes\(\s*\[([^\]]*)\]', page_html)
        if m:
            ids = [x.strip().strip('"') for x in m.group(1).split(",") if x.strip()]
        hits_on_page = 0
        known_on_page = 0
        for i, card in enumerate(cards):
            rid = ids[i] if i < len(ids) else ""
            if not rid:
                continue
            if rid in need:
                rec = _community_parse_card(card, "all", rid)
                target = need.pop(rid)
                ta = target.setdefault("author", {})
                ra = rec.get("author", {}) or {}
                if ra.get("personaname"):
                    ta["personaname"] = ra["personaname"]
                if ra.get("avatar"):
                    ta["avatar"] = ra["avatar"]
                if ra.get("profile_url"):
                    ta["profile_url"] = ra["profile_url"]
                if ra.get("steamid"):
                    ta["steamid"] = ra["steamid"]
                if ra.get("num_games_owned"):
                    ta["num_games_owned"] = ra["num_games_owned"]
                filled += 1
                hits_on_page += 1
                known_on_page += 1
            elif rid in all_ids:
                # 已补过的评论 → 属于「已知区域」，不算熔断
                known_on_page += 1
        print(f"    🔗 补齐进度: {filled} 条已匹配 · {len(need)} 条待找（第 {page_no} 页）")
        if on_checkpoint is not None:
            try:
                on_checkpoint(page_no, filled)
            except Exception:
                pass
        # 熔断：连续多页连一条已知评论都遇不到 → 已越过社区页可达范围，停止空转
        if known_on_page == 0:
            empty_streak += 1
            if empty_streak >= 6:
                print(f"    ℹ️ 连续 {empty_streak} 页无任何已知评论，停止补齐（跳过剩余 {len(need)} 条）")
                break
        else:
            empty_streak = 0
        nxt, cur = _community_next_url(page_html, appid, browsefilter, "all")
        if not nxt or not cur or cur in seen_cursors:
            break
        seen_cursors.add(cur)
        url = nxt
        if need:
            time.sleep(REQUEST_INTERVAL)
    return filled


def normalize_review(r):
    """统一 weighted_vote_score 类型 + 换行符"""
    r = dict(r)
    try:
        r["weighted_vote_score"] = float(r.get("weighted_vote_score", 0))
    except (TypeError, ValueError):
        r["weighted_vote_score"] = 0.0
    txt = r.get("review", "") or ""
    r["review"] = txt.replace("\r\n", "\n").replace("\r", "\n")
    return r


def download_avatar(avatar_hash, out_dir):
    if not avatar_hash:
        return False
    filepath = os.path.join(out_dir, f"{avatar_hash}_full.jpg")
    if os.path.exists(filepath):
        return True
    url = f"https://avatars.steamstatic.com/{avatar_hash}_full.jpg"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = resp.read()
        with open(filepath, "wb") as f:
            f.write(data)
        return True
    except Exception as e:
        print(f"     ⚠️  头像 {avatar_hash[:8]}... 下载失败: {e}")
        return False


def parse_args():
    p = argparse.ArgumentParser(
        description="Steam appreviews 玩家评论一键下载器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("appid", type=str)
    p.add_argument("--all", action="store_true", help="翻页拉取全部")
    p.add_argument("--max", type=int, default=None, help=f"最多条数（默认 {DEFAULT_MAX_REVIEWS}）")
    p.add_argument("--lang", type=str, default="all", help="语言过滤，默认 all")
    p.add_argument("--sort", type=str, default="helpful", choices=list(SORT_MAP.keys()))
    p.add_argument("--with-avatars", action="store_true", help="顺便下载头像")
    p.add_argument("--avatars-only", action="store_true",
                   help="只补下头像，不重新拉取评论（读取已有 appreviews.json）")
    p.add_argument("--enrich-only", action="store_true",
                   help="只补昵称/头像（不重新拉取评论，读取已有 appreviews.json）")
    p.add_argument("--enrich-pages", type=int, default=260,
                   help="--enrich-only 的社区页扫描上限（每页约10条，默认 260）")
    p.add_argument("--proxy", type=str, default=None,
                   help="显式指定 HTTP 代理，如 http://127.0.0.1:7897")
    p.add_argument("--no-proxy", action="store_true", help="禁用代理（纯直连）")
    p.add_argument("--out", type=str, default=None, help="输出目录（默认 ./AppID_<id>/）")
    return p.parse_args()


def _avatars_only_mode(appid, out_dir):
    """只补下头像，不重新拉取评论 —— 读取已有 appreviews.json"""
    out_file = os.path.join(out_dir, "appreviews.json")
    if not os.path.exists(out_file):
        print(f"❌ 找不到 {out_file}，无法补头像（请先拉取评论）")
        sys.exit(1)
    with open(out_file, encoding="utf-8") as f:
        data = json.load(f)
    reviews = data.get("reviews", []) if isinstance(data, dict) else []
    if not reviews:
        print(f"❌ {out_file} 中没有评论数据")
        sys.exit(1)
    avatar_dir = os.path.join(out_dir, "avatars")
    os.makedirs(avatar_dir, exist_ok=True)
    print(f"🎯 AppID: {appid}")
    print(f"📁 输出: {os.path.abspath(out_dir)}")
    print(f"🖼  --avatars-only 模式：只补头像，不动评论数据")
    print(f"📄 现有评论: {len(reviews)} 条")
    print("─" * 60)
    download_avatars_batch(reviews, avatar_dir)
    print("─" * 60)
    print(f"✅ 头像补完 · 评论数据未改动")


def download_avatars_batch(reviews, avatar_dir, workers=8):
    """并发下载评论里出现过的头像（按 hash 去重）。"""
    from concurrent.futures import ThreadPoolExecutor

    hashes = []
    seen = set()
    no_hash = 0
    for r in reviews:
        h = (r.get("author", {}) or {}).get("avatar", "")
        if not h:
            no_hash += 1
            continue
        if h not in seen:
            seen.add(h)
            hashes.append(h)

    todo = [h for h in hashes
            if not os.path.exists(os.path.join(avatar_dir, f"{h}_full.jpg"))]
    print(f"   🖼 头像: 唯一 {len(hashes)} 个 · 待下载 {len(todo)} 个 · 已存在 {len(hashes)-len(todo)} 个")
    ok = fail = 0
    if todo:
        def _one(h):
            return download_avatar(h, avatar_dir)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for i, res in enumerate(ex.map(_one, todo), 1):
                if res:
                    ok += 1
                else:
                    fail += 1
                if i % 25 == 0 or i == len(todo):
                    print(f"   进度 {i}/{len(todo)} (✅{ok} / ❌{fail})")
    print(f"   ✅ 头像完成: 成功 {ok} / 失败 {fail} / 无头像跳过 {no_hash}")
    return ok, fail, no_hash


def _enrich_only_mode(appid, out_dir, enrich_pages=260):
    """只补昵称/头像：读取已有 appreviews.json → 社区页补齐（带断点保存）→ 回写 → 并发下载头像。"""
    out_file = os.path.join(out_dir, "appreviews.json")
    if not os.path.exists(out_file):
        print(f"❌ 找不到 {out_file}，无法补齐（请先拉取评论）")
        sys.exit(1)
    with open(out_file, encoding="utf-8") as f:
        data = json.load(f)
    reviews = data.get("reviews", []) if isinstance(data, dict) else []
    if not reviews:
        print(f"❌ {out_file} 中没有评论数据")
        sys.exit(1)

    print(f"🎯 AppID: {appid}")
    print(f"📁 输出: {os.path.abspath(out_dir)}")
    print(f"🔧 --enrich-only 模式：只补昵称/头像，不重新拉取评论")
    print(f"📄 现有评论: {len(reviews)} 条 · 扫描上限: {enrich_pages} 页")

    def _needs_enrich(r):
        a = r.get("author", {}) or {}
        nick = str(a.get("personaname") or "")
        return (not a.get("avatar")) or (not nick) or nick.startswith("用户")

    need_before = sum(1 for r in reviews if _needs_enrich(r))
    print(f"🔍 待补齐: {need_before} 条")
    print("─" * 60)

    state = {"backed_up": False}

    def _persist(note=""):
        """原子保存当前进度（首次保存前先备份原文件）"""
        if not state["backed_up"] and os.path.exists(out_file):
            bak_name = f"appreviews.json.bak_{int(time.time())}"
            try:
                os.rename(out_file, os.path.join(out_dir, bak_name))
                print(f"💾 旧文件已备份: {bak_name}")
            except Exception as e:
                print(f"⚠️ 备份失败（继续写入）: {e}")
            state["backed_up"] = True
        merged = dict(data)
        merged["reviews"] = reviews
        tmp = out_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
        os.replace(tmp, out_file)
        if note:
            print(f"   💾 进度已保存: {note}")

    filled = 0
    if need_before:
        sort_key = (data.get("_meta") or {}).get("sort") or "recent"

        def _ckpt(page_no, filled_so_far):
            if page_no % 20 == 0:
                _persist(f"第 {page_no} 页 · 已补 {filled_so_far} 条")

        try:
            filled = enrich_via_community(appid, reviews, sort_key,
                                          max_pages=enrich_pages, on_checkpoint=_ckpt)
        except Exception as e:
            print(f"⚠️ 补齐中断: {e}")
        if filled:
            _persist(f"最终 · 已补 {filled} 条")
            print(f"📄 已保存: {out_file}")
    else:
        print("✅ 昵称/头像已是完整状态，无需补齐")

    print("─" * 60)
    # 头像批量下载（使用补齐后的数据）
    avatar_dir = os.path.join(out_dir, "avatars")
    os.makedirs(avatar_dir, exist_ok=True)
    download_avatars_batch(reviews, avatar_dir)
    print("─" * 60)
    still = sum(1 for r in reviews if _needs_enrich(r))
    print(f"✨ 补齐完成: 本次补 {filled} 条 · 仍待补 {still} 条")


def main():
    args = parse_args()
    init_proxy(prefer=args.proxy, disable=args.no_proxy)
    appid = args.appid
    out_dir = args.out or f"./AppID_{appid}"
    os.makedirs(out_dir, exist_ok=True)

    # ═══ --avatars-only 快捷路径：只补头像，完全不碰评论 ═══
    if args.avatars_only:
        _avatars_only_mode(appid, out_dir)
        return

    # ═══ --enrich-only 快捷路径：只补昵称/头像，不重拉评论 ═══
    if args.enrich_only:
        _enrich_only_mode(appid, out_dir, enrich_pages=args.enrich_pages)
        return

    max_reviews = None if args.all else (args.max if args.max is not None else DEFAULT_MAX_REVIEWS)
    avatar_dir = os.path.join(out_dir, "avatars")
    if args.with_avatars:
        os.makedirs(avatar_dir, exist_ok=True)

    # 🎯 --all 模式下如果用户没显式指定 --sort，自动切到 recent
    # （旧端点 filter=all 有 ~100 条封顶；新端点翻全量需 Recent/Updated 才能走到尽头）
    effective_sort = args.sort
    if args.all and args.sort == "helpful":
        print(f"💡 --all 模式自动切换排序：helpful → recent（翻页需 Recent/Updated 才能走到尽头）")
        effective_sort = "recent"

    print(f"🎯 AppID: {appid}")
    print(f"📁 输出: {os.path.abspath(out_dir)}")
    print(f"🌍 语言: {args.lang} | 📊 排序: {effective_sort}")
    print(f"🔢 目标: {'全部' if max_reviews is None else max_reviews} | 🖼 头像: {'是' if args.with_avatars else '否'}")
    print("─" * 60)

    all_reviews = []
    seen_cursors = set()
    seen_rec_ids = set()  # 按 recommendationid 兜底去重
    cursor = "*"
    query_summary_final = None
    page = 0
    used_community_fallback = False
    used_v2 = False
    t0 = time.time()

    # ═══ 首选：官方新接口 IUserReviewsService（匿名可用） ═══
    while True:
        page += 1
        print(f"📄 第 {page} 页 (cursor={cursor[:20]}{'...' if len(cursor) > 20 else ''})")
        data = fetch_page_v2(appid, cursor, effective_sort, args.lang)
        if data is None:
            print(f"  🔁 新官方接口不可用，启用社区页兜底...")
            break

        used_v2 = True
        if query_summary_final is None:
            query_summary_final = data.get("query_summary", {}) or {}
            total = query_summary_final.get("total_reviews", "?")
            desc = query_summary_final.get("review_score_desc", "?")
            print(f"  📊 全网评论总数: {total} | 评级: {desc}")

        reviews = data.get("reviews", []) or []
        if not reviews:
            print(f"  ℹ️  0 条，翻页结束")
            break

        new_count = 0
        dup_count = 0
        for r in reviews:
            rec_id = r.get("recommendationid")
            if rec_id and rec_id in seen_rec_ids:
                dup_count += 1
                continue
            if rec_id:
                seen_rec_ids.add(rec_id)
            all_reviews.append(normalize_review(_normalize_v2_review(r)))
            new_count += 1

        got = len(reviews)
        dup_note = f"（去重 {dup_count} 条）" if dup_count else ""
        print(f"  ✅ 本页 {got} 条{dup_note}，累计 {len(all_reviews)} 条")

        # 达到上限
        if max_reviews is not None and len(all_reviews) >= max_reviews:
            all_reviews = all_reviews[:max_reviews]
            print(f"  🎯 达到上限 {max_reviews}，停止翻页")
            break

        seen_cursors.add(cursor)
        next_cursor = data.get("cursor", "")
        if not next_cursor or next_cursor in seen_cursors or next_cursor == cursor:
            print(f"  ℹ️  cursor 重复或为空，翻页结束")
            break
        cursor = next_cursor
        time.sleep(REQUEST_INTERVAL)

    # ═══ 兜底：官方新接口不可用 → 社区评论页 ═══
    if not used_v2 and not all_reviews:
        print(f"  🔁 启用社区评论页兜底...")
        result = fetch_reviews_via_community(appid, effective_sort, args.lang, max_reviews)
        if result:
            all_reviews, query_summary_final = result
            used_community_fallback = True
            total = query_summary_final.get("total_reviews", "?")
            desc = query_summary_final.get("review_score_desc", "?")
            print(f"  📊 商店页汇总: 全网 {total} 条 | 评级: {desc}")
        else:
            print(f"  💥 社区页兜底也失败，中止")

    # ═══ 昵称/头像补齐：官方新接口不返回这些 → 社区页按 ID 互补 ═══
    if used_v2 and all_reviews:
        try:
            filled = enrich_via_community(appid, all_reviews, effective_sort, max_pages=200)
            if filled:
                print(f"  🔗 昵称/头像补齐: {filled} 条")
        except Exception as e:
            print(f"  ⚠️ 昵称/头像补齐失败（不影响主数据）: {e}")

    # 保存合并 JSON（带三层防护：防降级 + 自动备份 + 头像独立化）
    out_file = os.path.join(out_dir, "appreviews.json")
    reviews_for_avatars = all_reviews  # 默认用新抓的做头像源
    write_json = True

    # 🛡️ 防降级检查：如果旧文件存在，且旧的评论数 > 新抓的评论数 → 保护旧数据
    old_reviews_count = 0
    if os.path.exists(out_file):
        try:
            with open(out_file, "r", encoding="utf-8") as f:
                old_data = json.load(f)
            old_reviews = old_data.get("reviews", []) if isinstance(old_data, dict) else []
            old_reviews_count = len(old_reviews)
        except Exception as e:
            print(f"⚠️  读取旧 appreviews.json 失败: {e}")
            old_reviews = []

        if old_reviews_count > len(all_reviews):
            print("─" * 60)
            print(f"🛡️  防降级保护触发！")
            print(f"   旧文件: {old_reviews_count} 条 · 本次抓取: {len(all_reviews)} 条")
            print(f"   → 拒绝用小样本覆盖大样本，评论 JSON 保持不变")
            if args.with_avatars:
                print(f"   → 头像仍会补下（基于旧文件的 {old_reviews_count} 条）")
                reviews_for_avatars = old_reviews
            write_json = False

    if write_json:
        # 🛡️ 自动备份：写入前把旧文件重命名（如果存在）
        if os.path.exists(out_file):
            bak_name = f"appreviews.json.bak_{int(time.time())}"
            bak_path = os.path.join(out_dir, bak_name)
            try:
                os.rename(out_file, bak_path)
                print(f"💾 旧文件已备份: {bak_name}")
            except Exception as e:
                print(f"⚠️  备份失败（继续写入新文件）: {e}")

        merged = {
            "success": 1,
            "query_summary": query_summary_final or {},
            "reviews": all_reviews,
            "_meta": {
                "appid": appid,
                "fetched_at": int(time.time()),
                "fetched_count": len(all_reviews),
                "sort": effective_sort,
                "language": args.lang,
                "source": ("community_fallback" if used_community_fallback
                           else ("official_api_v2" if used_v2 else "official_api")),
            },
        }
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)

    elapsed = time.time() - t0
    print("─" * 60)
    print(f"✨ 完成，耗时 {elapsed:.1f}s")
    if used_community_fallback:
        print(f"   🌐 数据来源: steamcommunity.com 评论页（官方接口被拦截时的兜底）")
    elif used_v2:
        print(f"   🌐 数据来源: 官方新接口 IUserReviewsService（匿名）")
    print(f"   ✅ 拉取评论: {len(all_reviews)} 条")
    if write_json:
        print(f"   📄 保存到: {out_file}")
    else:
        print(f"   🛡️  评论 JSON 未变更（保留旧 {old_reviews_count} 条）")

    # 头像下载（使用 reviews_for_avatars，防降级触发时用旧数据）
    if args.with_avatars and reviews_for_avatars:
        print(f"🖼  开始下载头像（去重并发）→ avatars/")
        download_avatars_batch(reviews_for_avatars, avatar_dir)

    if not all_reviews and not write_json:
        pass  # 防降级触发但保留了旧数据，不算失败
    elif not all_reviews:
        sys.exit(2)


if __name__ == "__main__":
    main()
