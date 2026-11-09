#!/usr/bin/env python3
"""
PCGamingWiki 技术资料一键下载器（DRM/引擎/云同步/平台等）
用法: python fetch_pcgamingwiki.py <appid> [--out <目录>]

数据来源：https://www.pcgamingwiki.com/w/api.php (MediaWiki Cargo API)
无需 API Key，纯 GET 请求。

三次查询组合：
  1. Infobox_game  → 引擎/上架平台/系列/玩法元数据
  2. Availability  → 各平台 DRM 情况（Steam / GOG / Epic ...）
  3. Cloud         → 各平台云同步支持

保存格式:
{
  "appid": "1035760",
  "fetched_at_iso": "...",
  "page_name": "Glimmer in Mirror",
  "infobox": {...},
  "availability": {...},
  "cloud": {...}
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
import re
from datetime import datetime

API_URL = "https://www.pcgamingwiki.com/w/api.php"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
MAX_RETRIES = 3
RETRY_INTERVAL = 2.0
TIMEOUT = 20

# 三次查询的字段清单
INFOBOX_FIELDS = [
    "_pageName=Page", "Steam_AppID", "Engines", "Available_on",
    "Modes", "Pacing", "Perspectives", "Controls", "Genres", "Themes",
    "Series", "Developers", "Publishers", "Monetization", "Microtransactions",
    "Wrappers_Windows", "Wrappers_Linux", "Wrappers_OS_X",
    "GOGcom_ID", "Wikipedia", "License",
]
AVAILABILITY_FIELDS = [
    "Uses_DRM", "Removed_DRM",
    "Steam_DRM", "GOGcom_DRM", "Epic_Games_Store_DRM",
    "Ubisoft_Store_DRM", "EA_app_DRM", "Battlenet_DRM",
    "Microsoft_Store_DRM", "Humble_Store_DRM", "itchio_DRM",
    "Xbox_Game_Pass", "EA_Play", "Ubisoft_Plus_Premium",
]
CLOUD_FIELDS = [
    "Steam", "Epic_Games_Launcher", "GOG_Galaxy", "EA_app",
    "Ubisoft_Connect", "Xbox", "Discord", "iCloud", "OneDrive",
]


def cargoquery(table, fields, where):
    """执行一次 cargoquery"""
    params = {
        "action": "cargoquery",
        "tables": table,
        "fields": ",".join(fields),
        "where": where,
        "format": "json",
    }
    query = urllib.parse.urlencode(params)
    url = f"{API_URL}?{query}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
            if "error" in data:
                print(f"  ⚠️ {table} 查询错误: {data['error'].get('info', '?')}")
                return None
            return data.get("cargoquery", [])
        except urllib.error.HTTPError as e:
            print(f"  ❌ 第 {attempt}/{MAX_RETRIES} 次 {table} 失败: HTTP {e.code}")
        except (urllib.error.URLError, TimeoutError) as e:
            print(f"  ❌ 第 {attempt}/{MAX_RETRIES} 次 {table} 失败: {e}")
        except json.JSONDecodeError as e:
            print(f"  ❌ JSON 解析失败: {e}")
            return None

        if attempt < MAX_RETRIES:
            time.sleep(RETRY_INTERVAL)

    return None


def search_page_by_appid(appid):
    """Cargo 不可用时，通过 MediaWiki 全文搜索反查页面名。"""
    search_queries = [
        f'insource:"steam appid" {appid}',
        appid,
    ]

    for search_text in search_queries:
        params = {
            "action": "query",
            "list": "search",
            "srsearch": search_text,
            "srnamespace": "0",
            "srlimit": "10",
            "format": "json",
        }
        url = f"{API_URL}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
            results = (data.get("query", {}) or {}).get("search", []) or []
            if results:
                return results[0].get("title", "")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            print(f"  ⚠️ MediaWiki 页面搜索失败: {e}")
            return ""

    return ""


def load_steam_english_name(appid, out_dir):
    """从 appdetails_english.json 提取 Steam 官方英文名。"""
    path = os.path.join(out_dir, "appdetails_english.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError):
        return ""

    candidates = []
    direct = payload.get(str(appid))
    if isinstance(direct, dict):
        candidates.append(direct)
    candidates.extend(v for v in payload.values() if isinstance(v, dict))

    for entry in candidates:
        data = entry.get("data", {}) or {}
        if str(data.get("steam_appid", "")) == str(appid):
            return str(data.get("name", "") or "").strip()
    return ""


def search_page_by_name(game_name):
    """用 MediaWiki 搜索根据 Steam 英文名寻找真实标题和 pageid。"""
    params = {
        "action": "query",
        "list": "search",
        "srsearch": game_name,
        "srnamespace": "0",
        "srlimit": "10",
        "format": "json",
    }
    url = f"{API_URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))

        results = (data.get("query", {}) or {}).get("search", []) or []
        if results:
            first = results[0]
            return (
                str(first.get("title", "") or ""),
                str(first.get("pageid", "") or ""),
            )
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError,
            json.JSONDecodeError, AttributeError) as e:
        print(f"  ⚠️ MediaWiki 页面搜索失败: {e}")
    return "", ""


def fetch_page_wikitext(page_name="", page_id=""):
    """通过 MediaWiki parse API 获取原始 Wikitext，优先使用 pageid。"""
    params = {
        "action": "parse",
        "prop": "wikitext",
        "format": "json",
    }
    if page_id:
        params["pageid"] = str(page_id)
    else:
        params["page"] = page_name
    url = f"{API_URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        return (payload.get("parse", {}) or {}).get("wikitext", {}).get("*", "")
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError,
            json.JSONDecodeError) as e:
        print(f"  ⚠️ 页面 parse 请求失败: {e}")
        return ""


def wiki_field(wikitext, field):
    """提取 Infobox 中形如 |field = value 的简单字段。"""
    pattern = rf"(?mi)^\|\s*{re.escape(field)}\s*=\s*(.*?)\s*$"
    match = re.search(pattern, wikitext or "")
    return match.group(1).strip() if match else ""


def infobox_from_wikitext(wikitext, appid, page_name):
    """把页面 Wikitext 的基础字段转换成现有 JSON 结构。"""
    fields = {
        "Page": page_name,
        "Steam_AppID": wiki_field(wikitext, "steam appid") or appid,
        "Developers": wiki_field(wikitext, "developers"),
        "Publishers": wiki_field(wikitext, "publishers"),
        "Available_on": wiki_field(wikitext, "available on"),
        "Modes": wiki_field(wikitext, "modes"),
        "Pacing": wiki_field(wikitext, "pacing"),
        "Perspectives": wiki_field(wikitext, "perspectives"),
        "Controls": wiki_field(wikitext, "controls"),
        "Genres": wiki_field(wikitext, "genres"),
        "Themes": wiki_field(wikitext, "themes"),
        "Series": wiki_field(wikitext, "series"),
        "License": wiki_field(wikitext, "license"),
    }
    return {k: clean_value(v) for k, v in fields.items() if clean_value(v)}


def clean_value(v):
    """清洗字段值：剥掉 Engine: 前缀、去掉尾部逗号、null → ''"""
    if v is None:
        return ""
    s = str(v).strip().rstrip(",").strip()
    # 剥掉 wiki 页面前缀（如 "Engine:Unity" → "Unity"）
    if ":" in s and s.count(":") == 1:
        prefix, rest = s.split(":", 1)
        if prefix in ("Engine", "Company", "Series"):
            s = rest.strip()
    return s


def clean_row(row):
    """清洗一行数据：过滤空值 + 剥前缀"""
    if not row:
        return {}
    title = row.get("title", {}) or {}
    return {k: clean_value(v) for k, v in title.items() if clean_value(v)}


def main():
    parser = argparse.ArgumentParser(description="PCGamingWiki 技术资料下载器")
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
    print(f"🌐 数据来源: PCGamingWiki (MediaWiki Cargo API)")
    print("─" * 60)

    # 第 1 步：主表用 Steam_AppID 反查
    print("📡 [1/3] 查询 Infobox_game（引擎/平台/玩法）...")
    infobox_rows = cargoquery(
        "Infobox_game", INFOBOX_FIELDS,
        f'Steam_AppID HOLDS "{appid}"'
    )
    page_wikitext = ""
    page_id_fallback = ""
    page_name_fallback = ""

    if infobox_rows is None:
        print("⚠️ Cargo 查询不可用，改用 Steam 英文名 → OpenSearch → parse...")
        steam_name = load_steam_english_name(appid, out_dir)
        if steam_name:
            print(f"🎮 Steam 英文名: {steam_name}")
            page_name_fallback, page_id_fallback = search_page_by_name(steam_name)
            if page_name_fallback:
                print(
                    f"✅ OpenSearch 命中: {page_name_fallback} "
                    f"(pageid={page_id_fallback or '?'})"
                )
                page_wikitext = fetch_page_wikitext(
                    page_name_fallback, page_id_fallback
                )

        if page_wikitext:
            print(f"✅ MediaWiki 页面读取成功: {page_name_fallback}")
            infobox_rows = [{
                "title": infobox_from_wikitext(
                    page_wikitext, appid, page_name_fallback
                )
            }]
        else:
            print("⚠️ 未能通过 Steam 名称读取 PCGamingWiki 页面")
            infobox_rows = []

    if not infobox_rows:
        print("─" * 60)
        print(f"⚠️ PCGamingWiki 未找到匹配页面或页面读取失败: AppID {appid}")
        print("ℹ️ 这不会影响 Steam 主归档和 HTML 生成")
        sys.exit(2)

    # 从主表拿到 page 名，用它反查次级表（Availability/Cloud 表没有 Steam_AppID 字段）
    first_row = infobox_rows[0].get("title", {}) or {}
    page_name_raw = first_row.get("Page", "")
    if not page_name_raw:
        print("⚠️ Infobox_game 命中但拿不到页面名，跳过次级表查询")
        availability_rows = []
        cloud_rows = []
    else:
        # 页面名里可能有引号，双引号转义成 \" 用于 SQL
        page_esc = page_name_raw.replace('"', '\\"')
        page_where = f'_pageName="{page_esc}"'

        time.sleep(0.5)
        print(f"📡 [2/3] 查询 Availability（DRM / 各平台上架）_pageName={page_name_raw}...")
        availability_rows = cargoquery("Availability", AVAILABILITY_FIELDS, page_where)

        time.sleep(0.5)
        print(f"📡 [3/3] 查询 Cloud（各平台云同步）_pageName={page_name_raw}...")
        cloud_rows = cargoquery("Cloud", CLOUD_FIELDS, page_where)

    infobox = clean_row(infobox_rows[0]) if infobox_rows else {}
    availability = clean_row(availability_rows[0]) if availability_rows else {}
    cloud = clean_row(cloud_rows[0]) if cloud_rows else {}

    page_name = infobox.get("Page", "")

    print(f"✅ 数据获取成功")
    print(f"   Wiki 页面: {page_name}")
    if infobox.get("Engines"):
        print(f"   🎮 引擎: {infobox['Engines']}")
    if infobox.get("Available_on"):
        print(f"   🌍 上架平台: {infobox['Available_on']}")
    if infobox.get("Series"):
        print(f"   📚 系列: {infobox['Series']}")

    # DRM 汇总
    drm_lines = []
    for k, v in availability.items():
        if k.endswith("_DRM") and v:
            platform = k.replace("_DRM", "").replace("_", " ")
            drm_lines.append(f"      {platform}: {v}")
    if drm_lines:
        print(f"   🔒 DRM 情况:")
        for line in drm_lines[:5]:
            print(line)
        if len(drm_lines) > 5:
            print(f"      ...（共 {len(drm_lines)} 个平台）")

    # 云同步汇总
    cloud_supported = [k for k, v in cloud.items() if v and v.lower() in ("true", "yes", "limited")]
    if cloud_supported:
        print(f"   ☁️ 云同步: {', '.join(cloud_supported)}")

    # 保存
    output = {
        "appid": appid,
        "fetched_at_iso": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "page_name": page_name,
        "page_id": page_id_fallback if page_wikitext else "",
        "infobox": infobox,
        "availability": availability,
        "cloud": cloud,
        "source": "cargoquery" if not page_wikitext else "mediawiki_search_parse",
        "wikitext": page_wikitext,
    }
    out_path = os.path.join(out_dir, "pcgamingwiki.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print("─" * 60)
    print(f"💾 已保存: {out_path}")
    print(f"✨ 完成")


if __name__ == "__main__":
    main()
