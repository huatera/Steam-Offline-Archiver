#!/usr/bin/env python3
"""
Steam appdetails 多语言一键下载器
用法:
  python fetch_appdetails.py <AppID>                     # 默认四语言 en/sc/tc/jp
  python fetch_appdetails.py <AppID> --all               # 全部 13 种主流语言
  python fetch_appdetails.py <AppID> --langs english,koreana,thai
  python fetch_appdetails.py <AppID> --out ./Game-Patoneko

输出:
  <out_dir>/
    ├── appdetails_english.json
    ├── appdetails_schinese.json
    ├── appdetails_tchinese.json
    └── appdetails_japanese.json

说明:
  - 直接调用 Steam 官方 store.steampowered.com/api/appdetails
  - 每种语言之间间隔 1.5 秒避免 429
  - 失败自动重试最多 3 次
  - success=false 会保留 JSON 文件但在控制台标红提示
"""
import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error

# ────────────────────────────────────────────────
# 语言 → 货币 映射表
# ────────────────────────────────────────────────
LANGUAGES = {
    # lang_code:    cc_code (影响 price_overview.currency)
    "english":    "us",   # USD
    "schinese":   "cn",   # CNY 简体中文
    "tchinese":   "tw",   # TWD 繁体中文
    "japanese":   "jp",   # JPY
    "koreana":    "kr",   # KRW
    "russian":    "ru",   # RUB
    "french":     "fr",   # EUR
    "german":     "de",   # EUR
    "spanish":    "es",   # EUR
    "brazilian":  "br",   # BRL 巴西葡语
    "italian":    "it",   # EUR
    "thai":       "th",   # THB
    "vietnamese": "vn",   # VND
}

# 默认四大常用语言（对应 sc/tc/en/jp）
DEFAULT_LANGS = ["english", "schinese", "tchinese", "japanese"]

# 请求间隔（秒），避免 Steam API 限流
REQUEST_INTERVAL = 1.5

# 每种语言最大重试次数
MAX_RETRIES = 3

# 请求超时（秒）
TIMEOUT = 20

# User-Agent：模拟浏览器，避免部分节点返回空
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def fetch_one(appid, lang, cc, out_dir):
    """
    拉取单个语言的 appdetails JSON。
    返回: (status, filepath)
      status ∈ {"ok", "success_false", "failed"}
    """
    url = (
        f"https://store.steampowered.com/api/appdetails"
        f"?appids={appid}&l={lang}&cc={cc}"
    )
    filename = f"appdetails_{lang}.json"
    filepath = os.path.join(out_dir, filename)

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read().decode("utf-8")

            # 校验 JSON 结构
            data = json.loads(raw)

            # Steam 偶尔会用 side AppID 作为顶层键；
            # 以 data.steam_appid 作为真正的目标 AppID 兜底匹配。
            entry = data.get(str(appid), {})
            if not entry:
                for candidate in data.values():
                    candidate_data = candidate.get("data", {}) or {}
                    if str(candidate_data.get("steam_appid", "")) == str(appid):
                        entry = candidate
                        break

            success = bool(entry.get("success", False))

            # 无论 success 真假都保存文件（方便排查）
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(raw)

            if success:
                name = entry.get("data", {}).get("name", "?")
                currency = (
                    entry.get("data", {})
                         .get("price_overview", {})
                         .get("currency", "—")
                )
                print(f"  ✅ {lang:12s} → {filename}  [{name} / {currency}]")
                return "ok", filepath
            else:
                print(f"  ⚠️  {lang:12s} → {filename}  [success=false, 该 AppID 在此语言下无数据]")
                return "success_false", filepath

        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = 5 * attempt
                print(f"  ⏳ {lang:12s} 触发限流 429，等待 {wait}s 后重试 ({attempt}/{MAX_RETRIES})")
                time.sleep(wait)
            else:
                print(f"  ❌ {lang:12s} HTTP {e.code}，重试 ({attempt}/{MAX_RETRIES})")
                time.sleep(2)
        except Exception as e:
            print(f"  ❌ {lang:12s} 错误: {e}，重试 ({attempt}/{MAX_RETRIES})")
            time.sleep(2)

    print(f"  💥 {lang:12s} 重试 {MAX_RETRIES} 次仍失败，跳过")
    return "failed", None


def parse_args():
    parser = argparse.ArgumentParser(
        description="Steam appdetails 多语言一键下载器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "可用语言代码:\n  " + ", ".join(LANGUAGES.keys())
        ),
    )
    parser.add_argument("appid", type=str, help="Steam AppID，例如 4267730")
    parser.add_argument(
        "--all", action="store_true",
        help=f"下载全部 {len(LANGUAGES)} 种语言（默认只下载 sc/tc/en/jp）",
    )
    parser.add_argument(
        "--langs", type=str, default=None,
        help="自定义语言列表，逗号分隔，例如 english,koreana,thai",
    )
    parser.add_argument(
        "--out", type=str, default=None,
        help="输出目录，默认 ./AppID_<id>/",
    )
    return parser.parse_args()


def resolve_languages(args):
    """根据参数决定要下载哪些语言"""
    if args.langs:
        langs = [x.strip() for x in args.langs.split(",") if x.strip()]
        # 校验
        invalid = [l for l in langs if l not in LANGUAGES]
        if invalid:
            print(f"❌ 未知语言代码: {', '.join(invalid)}")
            print(f"   可用代码: {', '.join(LANGUAGES.keys())}")
            sys.exit(1)
        return langs
    if args.all:
        return list(LANGUAGES.keys())
    return DEFAULT_LANGS


def main():
    args = parse_args()
    appid = args.appid
    langs = resolve_languages(args)
    out_dir = args.out or f"./AppID_{appid}"

    os.makedirs(out_dir, exist_ok=True)

    print(f"🎯 目标 AppID: {appid}")
    print(f"📁 输出目录: {os.path.abspath(out_dir)}")
    print(f"🌍 待下载语言 ({len(langs)}): {', '.join(langs)}")
    print(f"⏱️  请求间隔: {REQUEST_INTERVAL}s | 最大重试: {MAX_RETRIES}")
    print("─" * 60)

    stats = {"ok": 0, "success_false": 0, "failed": 0}
    t0 = time.time()

    for i, lang in enumerate(langs):
        cc = LANGUAGES[lang]
        status, _ = fetch_one(appid, lang, cc, out_dir)
        stats[status] += 1
        # 除最后一个外，请求间隔
        if i < len(langs) - 1:
            time.sleep(REQUEST_INTERVAL)

    elapsed = time.time() - t0
    print("─" * 60)
    print(f"✨ 完成，耗时 {elapsed:.1f}s")
    print(f"   ✅ 成功: {stats['ok']}")
    print(f"   ⚠️  无数据 (success=false): {stats['success_false']}")
    print(f"   ❌ 失败: {stats['failed']}")
    print(f"📁 文件保存在: {os.path.abspath(out_dir)}")

    # 如果全失败，返回非零退出码
    if stats["ok"] == 0:
        sys.exit(2)


if __name__ == "__main__":
    main()
