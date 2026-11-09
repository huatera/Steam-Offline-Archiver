#!/usr/bin/env python3
"""
Steam appdetails JSON → 完全离线的静态 HTML 页面
用法: python steam_json_to_offline_html.py <appdetails.json> <output_dir>

输出结构:
  output_dir/
    ├── index.html         ← 主页面
    └── assets/
        ├── header.jpg
        ├── screenshots/
        ├── movies/
        └── inline/        ← 描述里内嵌的图片/视频
"""
import json
import sys
import os
import re
import html as html_lib
import hashlib
import shutil
import subprocess
import urllib.request
import time
from urllib.parse import urlparse, quote as _url_quote
import urllib.parse
from datetime import datetime

# Windows 控制台/管道编码兜底：输出 emoji 遇到 GBK 等编码时不崩（降级为 ?）
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

# 🎯 Steam category loc key 兜底翻译字典
# 场景：Steam appdetails 上游偶尔返回裸 loc key（如 #category_playable_at_your_own_pace）
# 而不是本地化后的中文名 → 咱们在展示层兜底翻译成人话
CATEGORY_LOC_MAP = {
    "#category_playable_at_your_own_pace": "无需应对快速反应事件",
    "#category_playable_without_vision": "无需视觉即可游玩",
    "#category_playable_without_hearing": "无需听觉即可游玩",
    "#category_mouse_only_option": "可以仅用鼠标",
    "#category_touch_only_option": "可以仅用触控",
    "#category_keyboard_only_option": "可以仅用键盘",
    "#category_single_stick_option": "可以仅用单摇杆",
    "#category_chat_only_option": "可以仅用聊天",
    "#category_narrated_menus": "朗读游戏菜单",
    "#category_adjustable_difficulty": "可调整难度",
    "#category_save_anytime": "随时保存",
    "#category_subtitle_options": "字幕选项",
    "#category_surround_sound": "环绕声",
    "#category_adjustable_gameplay_speed": "可调整游戏速度",
    "#category_stereo_sound": "立体声",
}


def _translate_category(desc):
    """category description 兜底翻译"""
    if not desc:
        return ""
    if desc in CATEGORY_LOC_MAP:
        return CATEGORY_LOC_MAP[desc]
    if desc.startswith("#category_"):
        raw = desc[len("#category_"):].replace("_", " ").strip()
        return raw.title() if raw else desc
    return desc


# ────────────────────────────────────────────────
# main.json 清单管理
# ────────────────────────────────────────────────
MANIFEST_NAME = "main.json"

def load_manifest(output_dir):
    """加载已有的 main.json，不存在则返回空结构"""
    mpath = os.path.join(output_dir, MANIFEST_NAME)
    if os.path.exists(mpath):
        try:
            with open(mpath, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"generated_at": "", "files": {}}

def save_manifest(output_dir, manifest):
    """把清单写回 main.json（每次变更后调用，防 Ctrl+C 丢数据）"""
    manifest["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    mpath = os.path.join(output_dir, MANIFEST_NAME)
    tmp = mpath + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    os.replace(tmp, mpath)

def file_sha256(filepath):
    """计算文件的 SHA256（分块读取，大文件友好）"""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while True:
            chunk = f.read(1 << 20)  # 1 MB
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()

def register_file(manifest, output_dir, abs_path, url=""):
    """把一个已下载文件注册到清单里（计算 SHA256 + 记录元数据）"""
    if not abs_path or not os.path.isfile(abs_path):
        return
    rel = os.path.relpath(abs_path, output_dir).replace(os.path.sep, "/")
    # 如果清单里已经有这个文件且 SHA256 已记录，跳过重算
    if rel in manifest["files"]:
        existing = manifest["files"][rel]
        if existing.get("sha256") and existing.get("size") == os.path.getsize(abs_path):
            return
    manifest["files"][rel] = {
        "url": url,
        "sha256": file_sha256(abs_path),
        "size": os.path.getsize(abs_path),
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }

# ────────────────────────────────────────────────
# yt-dlp 支持检测
# ────────────────────────────────────────────────
YTDLP_BIN = shutil.which("yt-dlp") or shutil.which("yt-dlp.exe")

def download_via_ytdlp(manifest_url, dest_dir, filename_stem, ctx=None):
    """用 yt-dlp 下载 HLS/DASH 清单为 mp4。返回本地 mp4 路径，失败返回 None。
    ctx 可选：传入则下载成功后自动 register_file + save_manifest。
    浏览器直链模式下不下载，直接返回清单 URL 交给浏览器加载。
    """
    if ctx is not None and ctx.get("remote_assets"):
        return manifest_url
    if not YTDLP_BIN:
        return None
    os.makedirs(dest_dir, exist_ok=True)
    
    def _register_and_return(path):
        """成功后统一登记入口"""
        if ctx is not None:
            register_file(ctx["manifest"], ctx["output_dir"], path, url=manifest_url)
            save_manifest(ctx["output_dir"], ctx["manifest"])
        return path
    
    # ★ 先检查是否已有同 stem 的视频文件 → 跳过下载
    for ext in ("mp4", "mkv", "webm"):
        candidate = os.path.join(dest_dir, f"{filename_stem}.{ext}")
        if os.path.exists(candidate) and os.path.getsize(candidate) > 0:
            print(f"  ✅ 视频已存在，跳过: {filename_stem}.{ext}")
            return _register_and_return(candidate)
    
    out_template = os.path.join(dest_dir, f"{filename_stem}.%(ext)s")
    try:
        print(f"  🎬 yt-dlp 下载: {manifest_url[:80]}...")
        subprocess.run(
            [YTDLP_BIN,
             "--no-warnings",
             "--no-overwrites",
             "--concurrent-fragments", "8",   # 单个视频内 8 个分片并行下载，大幅提速
             "--merge-output-format", "mp4",
             "-o", out_template,
             manifest_url],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
        )
    except subprocess.CalledProcessError as e:
        print(f"  ⚠️  yt-dlp 下载失败: {e}")
        return None
    # 查找实际输出文件
    for ext in ("mp4", "mkv", "webm"):
        candidate = os.path.join(dest_dir, f"{filename_stem}.{ext}")
        if os.path.exists(candidate):
            return _register_and_return(candidate)
    return None

# ────────────────────────────────────────────────
# 下载工具
# ────────────────────────────────────────────────
def safe_filename(url):
    """根据 URL 生成一个安全的本地文件名。
    - 有扩展名的 URL：保留原扩展名（.jpg / .mp4 / .webm 等）
    - 没扩展名的 URL：不加任何后缀，只保留哈希
      （避免用 .bin 触发广告拦截插件的 ERR_BLOCKED_BY_CLIENT）
    浏览器加载本地资源时会通过内容嗅探识别 MIME，不依赖文件扩展名。
    """
    path = urlparse(url).path
    ext = os.path.splitext(path)[1]  # 没扩展名时是空字符串
    # 只保留常见的合法扩展名，避免奇怪后缀被拦截
    safe_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".svg",
                 ".mp4", ".webm", ".mkv", ".m4v", ".mov",
                 ".mp3", ".ogg", ".m4a", ".wav",
                 ".json", ".txt", ".css", ".js"}
    if ext.lower() not in safe_exts:
        ext = ""
    h = hashlib.md5(url.encode()).hexdigest()[:12]
    return f"{h}{ext}"

def scan_and_register_existing(output_dir, manifest):
    """扫描 output_dir 里所有已存在文件，用 register_file() 逐个登记。
    跳过 main.json 自己和 .part 临时文件。已在 manifest 中且 size 匹配的会自动跳过重算。
    """
    count_new = 0
    count_skipped = 0
    for root, dirs, filenames in os.walk(output_dir):
        for filename in filenames:
            if filename == MANIFEST_NAME or filename.endswith(".part"):
                continue
            full_path = os.path.join(root, filename)
            rel = os.path.relpath(full_path, output_dir).replace(os.path.sep, "/")
            # 已登记且 size 匹配 → register_file 内部会跳过
            already = rel in manifest["files"] and \
                      manifest["files"][rel].get("sha256") and \
                      manifest["files"][rel].get("size") == os.path.getsize(full_path)
            if already:
                count_skipped += 1
            else:
                register_file(manifest, output_dir, full_path, url="")
                count_new += 1
    if count_new or count_skipped:
        print(f"   📇 已登记 {count_new} 个新文件到清单，{count_skipped} 个已在清单中跳过")

def download(url, dest_dir, ctx):
    """下载 URL 到 dest_dir，返回本地绝对路径。
    ctx 是打包上下文：{"session_cache": dict, "output_dir": str, "manifest": dict}
    策略：
      1) session 缓存里已下过 → 直接返回
      2) 目标文件已存在且非空 → 视作已下载，跳过网络（但仍尝试登记进清单）
      3) 都没有 → 下载到 .part 临时文件，成功后再重命名
    每次成功产出文件后都会调用 register_file + save_manifest，
    这样中断不会丢清单，重跑也不会重下已完成的文件。
    """
    session_cache = ctx["session_cache"]
    output_dir    = ctx["output_dir"]
    manifest      = ctx["manifest"]

    # 浏览器直链模式：不下载、不登记，直接让 HTML 使用原始 CDN URL
    if ctx.get("remote_assets"):
        return url

    if not url or not url.startswith("http"):
        return url
    if url in session_cache:
        return session_cache[url]
    
    os.makedirs(dest_dir, exist_ok=True)
    fname = safe_filename(url)
    fpath = os.path.join(dest_dir, fname)
    part  = fpath + ".part"
    
    # 本目录已有完好文件 → 直接跳过（顺便补登记，方便清单完整）
    if os.path.exists(fpath) and os.path.getsize(fpath) > 0:
        session_cache[url] = fpath
        register_file(manifest, output_dir, fpath, url=url)
        save_manifest(output_dir, manifest)
        return fpath
    
    # 清理可能残留的 .part
    if os.path.exists(part):
        try: os.remove(part)
        except: pass
    
    # 真正下载（先写 .part，成功后重命名）
    try:
        print(f"  ↓ {url[:80]}...")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as r, open(part, "wb") as f:
            f.write(r.read())
        os.replace(part, fpath)  # 原子重命名
    except Exception as e:
        print(f"  ⚠️  下载失败: {e}")
        if os.path.exists(part):
            try: os.remove(part)
            except: pass
        session_cache[url] = url  # 失败就保留原 URL
        return url
    
    session_cache[url] = fpath
    register_file(manifest, output_dir, fpath, url=url)
    save_manifest(output_dir, manifest)
    return fpath

def rewrite_html_urls(html_str, ctx):
    """把 HTML 片段里所有 http(s) 的 URL 属性替换成本地下载路径。
    支持属性：src / href / poster / data-big-src / data-src / data-poster / data-image
    支持写法：attr="..."、attr='...'、以及 attr=...（无引号，Steam 的 data-big-src 就是这种）

    按媒体扩展名归档资源，支持 Steam CDN 和第三方图床。
    普通网页链接（YouTube/商店页/workshop/飞书等）保留原 URL。
    浏览器直链模式保留所有远程资源地址。
    """
    output_dir = ctx["output_dir"]
    inline_dir = os.path.join(output_dir, "assets", "inline")
    ATTR_NAMES = r"(src|href|poster|data-big-src|data-src|data-poster|data-image)"

    # 第三方图床也可归档；用 URL 路径的媒体扩展名区分资源与普通网页。
    # ✅ 只处理这些扩展名的资源（图片/视频/音频）
    ALLOWED_EXTS = (
        ".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".svg", ".bmp", ".ico",
        ".mp4", ".webm", ".mov", ".m4v",
        ".mp3", ".ogg", ".wav",
    )

    def _is_downloadable(url):
        """判断 URL 是否指向可归档的媒体资源"""
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
            return False
        return parsed.path.lower().endswith(ALLOWED_EXTS)

    def _to_local_rel(url):
        if not url.startswith("http"):
            return None
        # 普通网页链接保留原 URL；仅下载具有媒体扩展名的资源。
        if not _is_downloadable(url):
            return None
        local = download(url, inline_dir, ctx)
        if local.startswith("http"):
            return None
        return os.path.relpath(local, output_dir).replace(os.path.sep, "/")
    
    def replace_quoted(m):
        attr, quote, url = m.group(1), m.group(2), m.group(3)
        rel = _to_local_rel(url)
        return m.group(0) if rel is None else f'{attr}={quote}{rel}{quote}'
    
    def replace_unquoted(m):
        attr, url = m.group(1), m.group(2)
        rel = _to_local_rel(url)
        return m.group(0) if rel is None else f'{attr}="{rel}"'
    
    html_str = re.sub(rf'{ATTR_NAMES}=(["\'])([^"\']+)\2', replace_quoted, html_str)
    html_str = re.sub(rf'{ATTR_NAMES}=(https?://[^\s>]+)', replace_unquoted, html_str)
    return html_str

# ────────────────────────────────────────────────
# 玩家评论渲染
# ────────────────────────────────────────────────
def _format_playtime(minutes):
    """299 → '5.0 小时' / 45 → '45 分钟'"""
    try:
        m = int(minutes or 0)
    except (TypeError, ValueError):
        m = 0
    if m <= 0:
        return "0 分钟"
    if m < 60:
        return f"{m} 分钟"
    return f"{m/60:.1f} 小时"


def _fmt_ts(ts):
    """Unix 时间戳 → 'YYYY-MM-DD'"""
    try:
        import datetime
        return datetime.datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d")
    except Exception:
        return "?"


def _copy_avatar(avatar_hash, src_dir, dst_dir):
    """把 <src_dir>/<hash>_full.jpg 拷到 <dst_dir>/，返回相对路径或空串"""
    if not avatar_hash or not src_dir:
        return ""
    fname = f"{avatar_hash}_full.jpg"
    src = os.path.join(src_dir, fname)
    if not os.path.exists(src):
        return ""
    os.makedirs(dst_dir, exist_ok=True)
    dst = os.path.join(dst_dir, fname)
    if not os.path.exists(dst):
        try:
            shutil.copy2(src, dst)
        except Exception:
            return ""
    return f"assets/avatars/{fname}"


def bbcode_strip(text):
    """把评论里的 BBCode 富文本剥成纯文本（reviews.txt / 搜索 / 复制用）。
    [spoiler]xxx[/spoiler] → xxx；其余标签直接去掉，保留内容。"""
    if not text:
        return ""
    s = str(text)
    # 先保留包裹标签的内容
    s = re.sub(r'\[spoiler\](.*?)\[/spoiler\]', r'\1', s, flags=re.DOTALL | re.IGNORECASE)
    # 去掉剩下的所有 [xxx] / [/xxx] 标签
    s = re.sub(r'\[\*\]', '', s)
    s = re.sub(r'\[/?[a-zA-Z][^\]]*\]', '', s)
    return s


REVIEWS_JS_CODE = r"""
(function() {
  var raw = JSON.parse(document.getElementById("reviews-data").textContent);
  var listEl = document.getElementById("review-list");
  var pagerEl = document.getElementById("review-pager");
  var countEl = document.getElementById("review-count");
  var sortEl = document.getElementById("review-sort");
  var voteEl = document.getElementById("review-vote");
  var langEl = document.getElementById("review-lang");
  var searchEl = document.getElementById("review-search");
  var PAGE_SIZE = 50;
  var curPage = 0;

  function esc(s) {
    var str = String(s == null ? "" : s);
    var out = "";
    for (var i = 0; i < str.length; i++) {
      var c = str.charCodeAt(i);
      if (c === 38) out += "\u0026" + "amp;";
      else if (c === 60) out += "\u0026" + "lt;";
      else if (c === 62) out += "\u0026" + "gt;";
      else if (c === 34) out += "\u0026" + "quot;";
      else if (c === 39) out += "\u0026" + "#39;";
      else out += str.charAt(i);
    }
    return out;
  }
  // 🆕 BBCode → 纯文本（复制/搜索用）
  function bbStrip(text) {
    return String(text == null ? "" : text)
      .replace(/\[spoiler\]([\s\S]*?)\[\/spoiler\]/gi, "$1")
      .replace(/\[\/?\*\]/g, "")
      .replace(/\[\/?[a-zA-Z][^\]]*\]/g, "");
  }
  // 🆕 BBCode → HTML（评论正文渲染；spoiler 渲染成可展开折叠块，与 Steam 行为一致）
  function bbToHtml(text) {
    var s = esc(text);
    s = s.replace(/\[spoiler\]([\s\S]*?)\[\/spoiler\]/gi,
      '<details class="bb-spoiler"><summary>⚠️ 剧透内容 · 点击展开</summary><div class="bb-spoiler-body">$1</div></details>');
    s = s.replace(/\[h1\]([\s\S]*?)\[\/h1\]/gi, '<div class="bb-h1">$1</div>');
    s = s.replace(/\[h2\]([\s\S]*?)\[\/h2\]/gi, '<div class="bb-h2">$1</div>');
    s = s.replace(/\[h3\]([\s\S]*?)\[\/h3\]/gi, '<div class="bb-h3">$1</div>');
    s = s.replace(/\[strike\]([\s\S]*?)\[\/strike\]/gi, '<s>$1</s>');
    s = s.replace(/\[s\]([\s\S]*?)\[\/s\]/gi, '<s>$1</s>');
    s = s.replace(/\[b\]([\s\S]*?)\[\/b\]/gi, '<b>$1</b>');
    s = s.replace(/\[i\]([\s\S]*?)\[\/i\]/gi, '<i>$1</i>');
    s = s.replace(/\[u\]([\s\S]*?)\[\/u\]/gi, '<u>$1</u>');
    s = s.replace(/\[quote\]([\s\S]*?)\[\/quote\]/gi, '<blockquote class="bb-quote">$1</blockquote>');
    s = s.replace(/\[quote=([^\]]+)\]/gi, '<blockquote class="bb-quote"><small>$1</small><br>');
    s = s.replace(/\[\/quote\]/gi, "</blockquote>");
    s = s.replace(/\[code\]([\s\S]*?)\[\/code\]/gi, '<pre class="bb-code">$1</pre>');
    s = s.replace(/\[hr\]\s*(?:\[\/hr\])?/gi, '<hr class="bb-hr">');
    s = s.replace(/\[table\]/gi, '<table class="bb-table">');
    s = s.replace(/\[\/table\]/gi, '</table>');
    s = s.replace(/\[tr\]/gi, '<tr>');
    s = s.replace(/\[\/tr\]/gi, '</tr>');
    s = s.replace(/\[th\]/gi, '<th>');
    s = s.replace(/\[\/th\]/gi, '</th>');
    s = s.replace(/\[td\]/gi, '<td>');
    s = s.replace(/\[\/td\]/gi, '</td>');
    s = s.replace(/\[\/?noparse\]/gi, "");
    s = s.replace(/\[\/?\*\]/g, "");
    s = s.replace(/\[\/?[a-zA-Z][^\]]*\]/g, "");
    s = s.replace(/\n/g, "<br>");
    return s;
  }
  function fmtPlaytime(min) {
    var m = parseInt(min || 0);
    if (m <= 0) return "0 分钟";
    if (m < 60) return m + " 分钟";
    return (m / 60).toFixed(1) + " 小时";
  }
  function fmtTs(ts) {
    if (!ts) return "?";
    var d = new Date(ts * 1000);
    var y = d.getFullYear();
    var mo = String(d.getMonth() + 1).padStart(2, "0");
    var da = String(d.getDate()).padStart(2, "0");
    return y + "-" + mo + "-" + da;
  }
  function renderCard(r) {
    var avatar = r.avatar_rel
      ? '<img class="review-avatar" src="' + esc(r.avatar_rel) + '" alt="avatar">'
      : '<div class="review-avatar review-avatar-fallback">👤</div>';
    var name = r.profile_url
      ? '<a href="' + esc(r.profile_url) + '" target="_blank">' + esc(r.nick) + '</a>'
      : esc(r.nick);
    var thumb = r.voted_up
      ? '<span class="thumb-up">👍 推荐</span>'
      : '<span class="thumb-down">👎 不推荐</span>';
    var badges = [];
    if (r.purchased) badges.push('<span class="badge">Steam 购买</span>');
    if (r.received_free) badges.push('<span class="badge">免费获得</span>');
    if (r.refunded) badges.push('<span class="badge">已退款</span>');
    if (r.ea) badges.push('<span class="badge">抢先体验时期</span>');
    if (r.deck) badges.push('<span class="badge">Steam Deck</span>');
    if (r.language) badges.push('<span class="badge lang">' + esc(r.language) + '</span>');
    if (r.num_games_owned > 0) badges.push('<span class="badge">🎮 ' + r.num_games_owned + ' 款游戏</span>');
    if (r.num_reviews > 0) badges.push('<span class="badge">📝 ' + r.num_reviews + ' 条评论</span>');
    if (r.comment_count > 0) badges.push('<span class="badge">💬 ' + r.comment_count + ' 回复</span>');
    var textHtml = bbToHtml(r.review);
    var editedNote = (r.ts_updated && r.ts_updated !== r.ts_created)
      ? ' · 编辑于 ' + fmtTs(r.ts_updated) : '';
    var cardClass = "review-card" + (r.voted_up ? "" : " review-card-negative");
    return '<div class="' + cardClass + '">' + avatar
      + '<div class="review-body">'
      + '<div class="review-head"><b>' + name + '</b>'
      + '<span class="review-playtime">游玩 ' + fmtPlaytime(r.pt_review)
      + ' (总计 ' + fmtPlaytime(r.pt_forever) + ')</span>' + thumb + '</div>'
      + '<div class="review-badges">' + badges.join("") + '</div>'
      + '<div class="review-text">' + textHtml + '</div>'
      + '<div class="review-foot">👍 ' + r.votes_up + ' 有用 · 🎭 ' + r.votes_funny
      + ' 搞笑 · ' + fmtTs(r.ts_created) + editedNote + '</div>'
      + '</div></div>';
  }
  function applyFilters() {
    var sortMode = sortEl.value;
    var voteMode = voteEl.value;
    var langFilter = langEl.value;
    var kw = (searchEl.value || "").trim().toLowerCase();
    var arr = raw.slice();
    if (voteMode === "up") arr = arr.filter(function(r){ return r.voted_up; });
    else if (voteMode === "down") arr = arr.filter(function(r){ return !r.voted_up; });
    if (langFilter) arr = arr.filter(function(r){ return r.language === langFilter; });
    if (kw) arr = arr.filter(function(r){
      var body = bbStrip(r.review).toLowerCase();
      var nick = (r.nick || "").toLowerCase();
      return body.indexOf(kw) >= 0 || nick.indexOf(kw) >= 0;
    });
    if (sortMode === "helpful") arr.sort(function(a,b){ return b.wvs - a.wvs || b.votes_up - a.votes_up; });
    else if (sortMode === "funny") arr.sort(function(a,b){ return b.votes_funny - a.votes_funny; });
    else if (sortMode === "recent") arr.sort(function(a,b){ return b.ts_created - a.ts_created; });
    else if (sortMode === "oldest") arr.sort(function(a,b){ return a.ts_created - b.ts_created; });
    else if (sortMode === "playtime") arr.sort(function(a,b){ return b.pt_forever - a.pt_forever; });
    return arr;
  }
  function render() {
    var arr = applyFilters();
    var totalPages = Math.max(1, Math.ceil(arr.length / PAGE_SIZE));
    if (curPage >= totalPages) curPage = 0;
    var start = curPage * PAGE_SIZE;
    var page = arr.slice(start, start + PAGE_SIZE);
    listEl.innerHTML = page.map(renderCard).join("");
    countEl.textContent = "筛选后 " + arr.length + " 条 · 第 " + (curPage + 1) + "/" + totalPages + " 页";
    var pager = "";
    if (totalPages > 1) {
      pager += '<button class="pg-btn" data-p="prev"' + (curPage === 0 ? ' disabled' : '') + '>← 上一页</button>';
      pager += ' <span class="pg-info">第 ' + (curPage + 1) + ' / ' + totalPages + ' 页</span> ';
      pager += '<button class="pg-btn" data-p="next"' + (curPage >= totalPages - 1 ? ' disabled' : '') + '>下一页 →</button>';
    }
    pagerEl.innerHTML = pager;
    var btns = pagerEl.querySelectorAll(".pg-btn");
    for (var i = 0; i < btns.length; i++) {
      btns[i].addEventListener("click", function(e) {
        var p = e.currentTarget.getAttribute("data-p");
        if (p === "prev" && curPage > 0) curPage--;
        else if (p === "next") curPage++;
        render();
        listEl.scrollIntoView({behavior: "smooth", block: "start"});
      });
    }
  }
  sortEl.addEventListener("change", function(){ curPage = 0; render(); });
  voteEl.addEventListener("change", function(){ curPage = 0; render(); });
  langEl.addEventListener("change", function(){ curPage = 0; render(); });
  var searchTimer = null;
  searchEl.addEventListener("input", function(){
    if (searchTimer) clearTimeout(searchTimer);
    searchTimer = setTimeout(function(){ curPage = 0; render(); }, 200);
  });

  // 📋 复制全部（当前筛选结果的）评论为纯文本
  var copyBtn = document.getElementById("review-copy-all");
  if (copyBtn) {
    copyBtn.addEventListener("click", function() {
      var arr = applyFilters();
      var lines = [];
      lines.push("=== Steam 玩家评论 (筛选后 " + arr.length + " 条) ===");
      lines.push("");
      for (var i = 0; i < arr.length; i++) {
        var r = arr[i];
        var d = new Date((r.ts_created || 0) * 1000);
        var dateStr = d.getFullYear() + "-"
          + String(d.getMonth() + 1).padStart(2, "0") + "-"
          + String(d.getDate()).padStart(2, "0");
        var vote = r.voted_up ? "👍 好评" : "👎 差评";
        var pth = ((r.pt_forever || 0) / 60).toFixed(1);
        lines.push("--------------------------------------------------");
        lines.push("[" + (i + 1) + "] " + r.nick + " | " + vote
          + " | 游玩 " + pth + "h | " + dateStr
          + " | 语言: " + (r.language || "?"));
        lines.push("👍 有用 " + r.votes_up + " | 🎭 搞笑 " + r.votes_funny
          + " | 加权分 " + (parseFloat(r.wvs) || 0).toFixed(4));
        lines.push("");
        lines.push(bbStrip(r.review).trim() || "(空)");
        lines.push("");
      }
      var text = lines.join("\n");
      function fallbackCopy() {
        var ta = document.createElement("textarea");
        ta.value = text;
        ta.style.position = "fixed";
        ta.style.left = "-9999px";
        document.body.appendChild(ta);
        ta.select();
        try { document.execCommand("copy"); } catch (e) {}
        document.body.removeChild(ta);
      }
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(function() {
          copyBtn.textContent = "✅ 已复制 " + arr.length + " 条";
          setTimeout(function(){ copyBtn.textContent = "📋 复制全部评论"; }, 2000);
        }, function() {
          fallbackCopy();
          copyBtn.textContent = "✅ 已复制(降级) " + arr.length + " 条";
          setTimeout(function(){ copyBtn.textContent = "📋 复制全部评论"; }, 2000);
        });
      } else {
        fallbackCopy();
        copyBtn.textContent = "✅ 已复制(降级) " + arr.length + " 条";
        setTimeout(function(){ copyBtn.textContent = "📋 复制全部评论"; }, 2000);
      }
    });
  }

  render();
})();
"""


def _write_reviews_js(output_dir):
    """把 reviews.js 写到 output_dir/assets/reviews.js（幂等）"""
    assets_dir = os.path.join(output_dir, "assets")
    os.makedirs(assets_dir, exist_ok=True)
    js_path = os.path.join(assets_dir, "reviews.js")
    try:
        with open(js_path, "w", encoding="utf-8") as f:
            f.write(REVIEWS_JS_CODE)
    except Exception as e:
        print(f"⚠️  写入 reviews.js 失败: {e}")


# ── 目录导航（悬浮 TOC）脚本：注入每个归档页 ──
# 功能：点击跳转 / 滚动高亮 / 可拖拽 / 可收起（Esc 或 ✕ 收起为小圆钮）
# 收集规则：body 直接子级的「页面主标题 h1」+「无 class 的章节 h2」（排除正文里的 h2）
TOC_JS_CODE = r"""
/*!
 * Steam-Offline-Archiver · 目录导航（悬浮 TOC）
 * 点击跳转 / 滚动高亮 / 可拖拽 / 可收起 · 零依赖 · 纯离线可用
 * 由 steam_archiver.py 自动生成，或用 add_toc.py 注入到既有页面（幂等）
 */
(function () {
  "use strict";
  if (window.__soaTocLoaded) return;
  window.__soaTocLoaded = true;

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "\x26quot;");
  }

  function ready(fn) {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", fn);
    } else {
      fn();
    }
  }

  ready(function () {
    /* ── 1. 收集章节标题：页面主标题(h1) + 正文顶层章节(h2) ── */
    var heads = [];
    var title = document.querySelector("body > h1");
    if (title && (title.textContent || "").trim()) heads.push(title);
    var h2s = document.querySelectorAll("body > h2");
    for (var i = 0; i < h2s.length; i++) {
      var el = h2s[i];
      if (el.hasAttribute("class") || el.hasAttribute("style")) continue;
      if (!(el.textContent || "").trim()) continue;
      heads.push(el);
    }
    if (!heads.length) return;

    var items = [];
    for (var j = 0; j < heads.length; j++) {
      var h = heads[j];
      if (!h.id) h.id = "toc_sec_" + j;
      var num = parseInt(String(h.tagName).replace(/[^0-9]/g, ""), 10) || 2;
      var label = (h.textContent || "").trim();
      var badge = h.querySelector(".offline-badge");
      if (badge && badge.textContent) {
        label = label.replace(badge.textContent, "").trim();
      }
      items.push({ el: h, id: h.id, level: num, text: label });
    }

    /* 锚点滚动时留出上边距 */
    var padStyle = document.createElement("style");
    padStyle.textContent = "h1,h2{scroll-margin-top:14px;}";
    document.head.appendChild(padStyle);

    /* ── 2. 悬浮面板（Shadow DOM 隔离样式，不受页面 CSS 影响）── */
    var host = document.createElement("div");
    host.id = "soa-toc-root";
    var shadow = host.attachShadow({ mode: "open" });
    shadow.innerHTML =
      '<style>' +
      '#panel{position:fixed;top:96px;right:16px;width:232px;max-height:72vh;' +
      'display:flex;flex-direction:column;z-index:2147483647;box-sizing:border-box;' +
      'font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;' +
      'font-size:13px;line-height:1.4;color:#c7d5e0;background:rgba(22,32,45,0.96);' +
      'border:1px solid rgba(102,192,244,0.25);border-radius:8px;overflow:hidden;' +
      'box-shadow:0 6px 24px rgba(0,0,0,0.45);}' +
      '#panel-head{display:flex;align-items:center;justify-content:space-between;' +
      'padding:8px 10px;cursor:move;user-select:none;-webkit-user-select:none;' +
      'background:linear-gradient(180deg,rgba(42,71,94,0.9),rgba(27,40,56,0.9));' +
      'border-bottom:1px solid rgba(102,192,244,0.15);font-weight:600;color:#a3d4ff;}' +
      '#panel-head .t{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}' +
      '#panel-min{cursor:pointer;width:20px;height:20px;margin-left:8px;flex-shrink:0;' +
      'display:flex;align-items:center;justify-content:center;border-radius:50%;' +
      'color:#8f98a0;font-size:12px;}' +
      '#panel-min:hover{background:rgba(102,192,244,0.2);color:#fff;}' +
      '#panel-list{margin:0;padding:6px;list-style:none;overflow:auto;}' +
      '#panel-list::-webkit-scrollbar{width:8px;}' +
      '#panel-list::-webkit-scrollbar-thumb{background:rgba(102,192,244,0.25);border-radius:4px;}' +
      '#panel-list li{margin:0;padding:0;}' +
      '#panel-list a{display:block;padding:5px 8px;border-radius:5px;color:#c7d5e0;' +
      'text-decoration:none;font-size:12.5px;white-space:nowrap;overflow:hidden;' +
      'text-overflow:ellipsis;transition:background .12s,color .12s;}' +
      '#panel-list a:hover{background:rgba(102,192,244,0.16);color:#fff;}' +
      '#panel-list a.active{background:rgba(102,192,244,0.28);color:#fff;font-weight:600;}' +
      '#panel-list li.lv0 a{font-weight:700;color:#e8f4ff;}' +
      '#panel-list li.lv1 a{padding-left:16px;}' +
      '#panel-list li.lv2 a{padding-left:28px;}' +
      '#panel-list li.lv3 a{padding-left:40px;}' +
      '#pill{position:fixed;top:96px;right:16px;z-index:2147483647;display:none;' +
      'align-items:center;padding:7px 12px;border-radius:999px;cursor:pointer;' +
      'font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;' +
      'font-size:12.5px;user-select:none;-webkit-user-select:none;' +
      'background:rgba(22,32,45,0.96);color:#a3d4ff;' +
      'border:1px solid rgba(102,192,244,0.3);box-shadow:0 4px 16px rgba(0,0,0,0.4);}' +
      '#pill:hover{background:rgba(42,71,94,0.98);color:#fff;}' +
      '</style>' +
      '<div id="panel">' +
      '<div id="panel-head"><span class="t">▼ 目录导航</span><span id="panel-min">✕</span></div>' +
      '<ul id="panel-list"></ul>' +
      '</div>' +
      '<div id="pill">☰ 目录</div>';
    (document.body || document.documentElement).appendChild(host);

    var panel = shadow.querySelector("#panel");
    var pill = shadow.querySelector("#pill");
    var listEl = shadow.querySelector("#panel-list");
    var minBtn = shadow.querySelector("#panel-min");
    var headEl = shadow.querySelector("#panel-head");

    /* ── 3. 渲染列表（按标题层级缩进）── */
    var baseLv = items[0].level;
    var li = "";
    for (var k = 0; k < items.length; k++) {
      var it = items[k];
      var lv = it.level - baseLv;
      if (lv < 0) lv = 0;
      if (lv > 3) lv = 3;
      li += '<li class="lv' + lv + '"><a href="#' + it.id + '" data-i="' + k + '">' +
        esc(it.text) + '</a></li>';
    }
    listEl.innerHTML = li;
    var links = listEl.querySelectorAll("a");

    /* ── 4. 点击跳转 ── */
    for (var m = 0; m < links.length; m++) {
      links[m].addEventListener("click", function (ev) {
        ev.preventDefault();
        var t = items[parseInt(this.getAttribute("data-i"), 10)];
        if (t) t.el.scrollIntoView({ behavior: "smooth", block: "start" });
      });
    }

    /* ── 5. 滚动高亮当前章节 ── */
    function updateActive() {
      var idx = -1;
      for (var p = 0; p < items.length; p++) {
        if (items[p].el.getBoundingClientRect().top <= 140) idx = p;
        else break;
      }
      for (var q = 0; q < links.length; q++) {
        links[q].className = (q === idx) ? "active" : "";
      }
    }
    var ticking = false;
    window.addEventListener("scroll", function () {
      if (ticking) return;
      ticking = true;
      window.requestAnimationFrame(function () {
        ticking = false;
        updateActive();
      });
    }, { passive: true });
    updateActive();

    /* ── 6. 收起 / 展开（✕ 或 Esc 收起为小圆钮，点击圆钮展开）── */
    function collapse() {
      var r = panel.getBoundingClientRect();
      pill.style.left = r.left + "px";
      pill.style.top = r.top + "px";
      pill.style.right = "auto";
      panel.style.display = "none";
      pill.style.display = "flex";
    }
    function expand() {
      var r = pill.getBoundingClientRect();
      panel.style.left = r.left + "px";
      panel.style.top = r.top + "px";
      panel.style.right = "auto";
      pill.style.display = "none";
      panel.style.display = "flex";
    }
    minBtn.addEventListener("click", collapse);
    pill.addEventListener("click", expand);
    document.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape" && panel.style.display !== "none") collapse();
    });
    /* 便捷开关：控制台里可调用 window.__soaTocToggle() */
    window.__soaTocToggle = function () {
      if (pill.style.display === "flex") expand();
      else collapse();
    };

    /* ── 7. 拖拽移动（鼠标 + 触屏）── */
    var dragging = false;
    var ox = 0;
    var oy = 0;
    function dragStart(e) {
      if (e.target && e.target.id === "panel-min") return;
      var pt = e.touches ? e.touches[0] : e;
      dragging = true;
      var r = panel.getBoundingClientRect();
      ox = pt.clientX - r.left;
      oy = pt.clientY - r.top;
      if (e.cancelable) e.preventDefault();
    }
    function dragMove(e) {
      if (!dragging) return;
      var pt = e.touches ? e.touches[0] : e;
      var x = pt.clientX - ox;
      var y = pt.clientY - oy;
      if (x < 4) x = 4;
      if (y < 4) y = 4;
      panel.style.left = x + "px";
      panel.style.top = y + "px";
      panel.style.right = "auto";
      if (e.cancelable) e.preventDefault();
    }
    function dragEnd() { dragging = false; }
    headEl.addEventListener("mousedown", dragStart);
    document.addEventListener("mousemove", dragMove);
    document.addEventListener("mouseup", dragEnd);
    headEl.addEventListener("touchstart", dragStart, { passive: false });
    document.addEventListener("touchmove", dragMove, { passive: false });
    document.addEventListener("touchend", dragEnd);
  });
})();
"""


def _write_toc_js(output_dir):
    """把目录导航脚本写到 output_dir/assets/toc.js（幂等，每次覆盖为最新版）"""
    assets_dir = os.path.join(output_dir, "assets")
    os.makedirs(assets_dir, exist_ok=True)
    js_path = os.path.join(assets_dir, "toc.js")
    try:
        with open(js_path, "w", encoding="utf-8") as f:
            f.write(TOC_JS_CODE)
    except Exception as e:
        print(f"⚠️  写入 toc.js 失败: {e}")


def render_reviews_section(reviews_path, output_dir):
    """
    读取 appreviews.json，渲染玩家评论区块 HTML。
    找不到文件返回空串（调用方决定要不要用兜底提示）。
    """
    if not reviews_path or not os.path.exists(reviews_path):
        return ""
    try:
        with open(reviews_path, "r", encoding="utf-8") as f:
            rdata = json.load(f)
    except Exception as e:
        print(f"⚠️  读取评论文件失败: {e}")
        return ""

    summary = rdata.get("query_summary", {}) or {}
    reviews = rdata.get("reviews", []) or []
    total = summary.get("total_reviews", 0) or 0
    positive = summary.get("total_positive", 0) or 0
    negative = summary.get("total_negative", 0) or 0
    desc = summary.get("review_score_desc", "") or "—"
    pct = int(positive * 100 / total) if total > 0 else 0

    # 头像目录（fetch_appreviews.py 生成的位置）
    reviews_dir = os.path.dirname(os.path.abspath(reviews_path))
    avatar_src = os.path.join(reviews_dir, "avatars")
    avatar_dst = os.path.join(output_dir, "assets", "avatars")

    if not reviews:
        return (
            f'<h2>💬 玩家评论</h2>'
            f'<p style="color:#8f98a0;">暂无玩家评论 · 全网评论数：{total}</p>'
        )

    # 归档完整度健康检查
    fetched_count = len(reviews)
    coverage_pct = int(fetched_count * 100 / total) if total > 0 else 100
    meta = rdata.get("_meta", {}) or {}
    sort_mode = meta.get("sort", "?")
    if fetched_count < total and total > 0:
        # 归档不完整：显示醒目提示
        coverage_banner = (
            f'<div class="review-coverage-warn">'
            f'⚠️ 归档完整度 {coverage_pct}% · 本地 {fetched_count} / 全网 {total} 条 '
            f'（排序模式：{html_lib.escape(sort_mode)}）'
            f'<br><small>💡 想归档全部？请在 GUI 勾选"拉全部 --all"重跑玩家评论步骤（会自动切换到 recent 排序）</small>'
            f'</div>'
        )
    else:
        coverage_banner = (
            f'<div class="review-coverage-ok">'
            f'✅ 归档完整度 100% · 已归档全部 {total} 条评论'
            f'</div>'
        )

    # 精简字段 + 预处理头像本地路径 → 序列化 JSON 内嵌
    slim_reviews = []
    for r in reviews:
        author = r.get("author", {}) or {}
        avatar_hash = author.get("avatar", "") or ""
        avatar_rel = _copy_avatar(avatar_hash, avatar_src, avatar_dst)
        slim_reviews.append({
            "nick": author.get("personaname", "匿名") or "匿名",
            "profile_url": author.get("profile_url", "") or "",
            "avatar_rel": avatar_rel,
            "pt_review": author.get("playtime_at_review", 0) or 0,
            "pt_forever": author.get("playtime_forever", 0) or 0,
            "num_reviews": author.get("num_reviews", 0) or 0,
            "num_games_owned": author.get("num_games_owned", 0) or 0,
            "voted_up": bool(r.get("voted_up", False)),
            "review": r.get("review", "") or "",
            "votes_up": r.get("votes_up", 0) or 0,
            "votes_funny": r.get("votes_funny", 0) or 0,
            "wvs": float(r.get("weighted_vote_score", 0) or 0),
            "ts_created": r.get("timestamp_created", 0) or 0,
            "ts_updated": r.get("timestamp_updated", 0) or 0,
            "language": r.get("language", "") or "",
            "purchased": bool(r.get("steam_purchase", False)),
            "ea": bool(r.get("written_during_early_access", False)),
            "deck": bool(r.get("primarily_steam_deck", False)),
            "received_free": bool(r.get("received_for_free", False)),
            "refunded": bool(r.get("refunded", False)),
            "comment_count": r.get("comment_count", 0) or 0,
        })

    # 收集语言列表用于筛选下拉
    lang_set = sorted(set(sr["language"] for sr in slim_reviews if sr["language"]))
    lang_options = '<option value="">🌍 全部语言</option>' + "".join(
        f'<option value="{html_lib.escape(l)}">{html_lib.escape(l)}</option>' for l in lang_set
    )

    # </ 会破坏 <script> 结束标签，必须转义
    reviews_json = json.dumps(slim_reviews, ensure_ascii=False).replace("</", "<\\/")

    # 🎯 同时输出一份纯文本评论到 output_dir/reviews.txt（方便喂给 AI 做二次分析）
    try:
        txt_path = os.path.join(output_dir, "reviews.txt")
        lines = []
        lines.append(f"=== Steam 玩家评论归档 ===")
        lines.append(f"全网总数: {total} | 好评: {positive} | 差评: {negative} | 好评率: {pct}%")
        lines.append(f"本地归档: {len(reviews)} 条 | 归档完整度: {coverage_pct}% | 排序模式: {sort_mode}")
        lines.append(f"评价描述: {desc}")
        lines.append("")
        # 按 weighted_vote_score 降序（最有帮助优先），跟 JS 默认排序一致
        sorted_reviews = sorted(
            slim_reviews,
            key=lambda x: (x["wvs"], x["votes_up"]),
            reverse=True,
        )
        import datetime as _dt
        for idx, sr in enumerate(sorted_reviews, 1):
            ts = sr["ts_created"]
            date_str = _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else "?"
            pt_h = sr["pt_forever"] / 60.0
            vote = "👍 好评" if sr["voted_up"] else "👎 差评"
            badges = []
            if sr["ea"]: badges.append("抢先体验")
            if sr["deck"]: badges.append("Steam Deck")
            if sr["received_free"]: badges.append("免费获得")
            if sr["refunded"]: badges.append("已退款")
            badge_str = f" | {' · '.join(badges)}" if badges else ""
            lines.append("─" * 60)
            lines.append(
                f"[{idx}] {sr['nick']} · {vote} · 游玩 {pt_h:.1f}h · {date_str} "
                f"· 语言: {sr['language'] or '?'}"
            )
            lines.append(
                f"👍 有用 {sr['votes_up']} | 🎭 搞笑 {sr['votes_funny']} "
                f"| 加权分 {sr['wvs']:.4f}{badge_str}"
            )
            lines.append("")
            lines.append(bbcode_strip(sr["review"]).strip() or "(空)")
            lines.append("")
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    except Exception as e:
        print(f"⚠️  写入 reviews.txt 失败: {e}")

    # 把 reviews.js 写到 output_dir/assets/reviews.js（幂等，已存在会覆盖）
    _write_reviews_js(output_dir)

    section = f'''
  <h2>💬 玩家评论 ({total} 条 · {html_lib.escape(desc)} · 好评率 {pct}%)</h2>
  {coverage_banner}
  <div class="review-summary">
    <div class="review-bar"><span style="width:{pct}%"></span></div>
    <span class="review-stats">好评 {positive} / 差评 {negative} · 本地归档 {len(reviews)} 条</span>
  </div>

  <div class="review-toolbar">
    <label>📊 排序
      <select id="review-sort">
        <option value="helpful">🌟 最有帮助</option>
        <option value="funny">🎭 最搞笑</option>
        <option value="recent">🕒 最新</option>
        <option value="oldest">📜 最早</option>
        <option value="playtime">⏱️ 游玩最久</option>
      </select>
    </label>
    <label>👍 态度
      <select id="review-vote">
        <option value="all">全部</option>
        <option value="up">👍 只看好评</option>
        <option value="down">👎 只看差评</option>
      </select>
    </label>
    <label>🌍 语言
      <select id="review-lang">{lang_options}</select>
    </label>
    <input type="search" id="review-search" placeholder="🔍 搜索正文或昵称..." />
    <button id="review-copy-all" class="pg-btn" type="button">📋 复制全部评论</button>
    <span id="review-count" class="review-count"></span>
  </div>

  <div id="review-list" class="review-list"></div>
  <div id="review-pager" class="review-pager"></div>

  <script id="reviews-data" type="application/json">{reviews_json}</script>
  <script src="assets/reviews.js" defer></script>
'''
    return section


# ────────────────────────────────────────────────
# 全球成就解锁率渲染
# ────────────────────────────────────────────────
def _achievement_rarity(pct):
    """按百分比返回稀有度徽章（emoji, 中文名, 颜色）"""
    if pct < 1:
        return ("💎", "极稀", "#b565f5")
    elif pct < 5:
        return ("🔴", "稀有", "#c94f4f")
    elif pct < 20:
        return ("🟠", "中等", "#ff8c42")
    elif pct < 50:
        return ("🟡", "常见", "#e8c547")
    else:
        return ("🟢", "大众", "#5c9a4d")


def render_pcgamingwiki_section(pcgw_info):
    """
    渲染 PCGamingWiki 技术资料区块（DRM/引擎/云同步/系列/玩法）。
    """
    if not pcgw_info:
        return ""

    infobox = pcgw_info.get("infobox", {}) or {}
    availability = pcgw_info.get("availability", {}) or {}
    cloud = pcgw_info.get("cloud", {}) or {}
    page_name = pcgw_info.get("page_name", "") or infobox.get("Page", "")
    fetched_at = pcgw_info.get("fetched_at_iso", "")

    if not infobox and not availability and not cloud:
        return ""

    # 基础信息行
    info_lines = []
    field_map = [
        ("Engines", "🎮 引擎"),
        ("Available_on", "🌍 上架平台"),
        ("Series", "📚 系列"),
        ("Modes", "🎯 模式"),
        ("Perspectives", "👁️ 视角"),
        ("Genres", "🏷️ 类型"),
        ("Themes", "🎨 主题"),
        ("Pacing", "⏱️ 节奏"),
        ("Controls", "🎮 控制方式"),
        ("Monetization", "💰 商业化"),
        ("Microtransactions", "💳 内购"),
        ("Wrappers_Linux", "🐧 Linux 包装器"),
        ("Wrappers_OS_X", "🍎 macOS 包装器"),
        ("License", "📜 许可"),
    ]
    for key, label in field_map:
        val = infobox.get(key, "")
        if val:
            info_lines.append(
                f'<div><b>{label}：</b>{html_lib.escape(val)}</div>'
            )

    # DRM 汇总
    drm_chips = []
    for k, v in availability.items():
        if k.endswith("_DRM") and v:
            platform = k.replace("_DRM", "").replace("_", " ")
            # DRM 值颜色：无 DRM → 绿，Steam DRM → 蓝，Denuvo → 红
            v_lower = v.lower()
            if "no" in v_lower or v_lower == "false":
                color = "#5c9a4d"
            elif "denuvo" in v_lower:
                color = "#c94f4f"
            else:
                color = "#66c0f4"
            drm_chips.append(
                f'<span class="pcgw-drm-chip" style="background:{color};">'
                f'{html_lib.escape(platform)}: {html_lib.escape(v)}</span>'
            )

    drm_block = ""
    if drm_chips:
        drm_block = f'''
        <div><b>🔒 DRM 情况:</b></div>
        <div class="pcgw-drm-list">{"".join(drm_chips)}</div>
        '''

    # 云同步汇总
    cloud_chips = []
    for k, v in cloud.items():
        if v:
            v_lower = v.lower()
            if v_lower in ("true", "yes"):
                icon = "✅"
                color = "#5c9a4d"
            elif v_lower in ("limited",):
                icon = "⚠️"
                color = "#e8c547"
            elif v_lower in ("false", "no"):
                icon = "❌"
                color = "#c94f4f"
            else:
                icon = "❓"
                color = "#8f98a0"
            cloud_chips.append(
                f'<span class="pcgw-cloud-chip" style="border-color:{color};color:{color};">'
                f'{icon} {html_lib.escape(k.replace("_", " "))}</span>'
            )

    cloud_block = ""
    if cloud_chips:
        cloud_block = f'''
        <div style="margin-top:10px;"><b>☁️ 云同步支持:</b></div>
        <div class="pcgw-cloud-list">{"".join(cloud_chips)}</div>
        '''

    # 外部链接
    ext_links = []
    if page_name:
        wiki_url = f"https://www.pcgamingwiki.com/wiki/{urllib.parse.quote(page_name.replace(' ', '_'))}"
        ext_links.append(f'<a href="{wiki_url}" style="color:#66c0f4;">📖 PCGamingWiki 完整页面</a>')
    if infobox.get("Wikipedia"):
        wp = infobox["Wikipedia"]
        wp_url = f"https://en.wikipedia.org/wiki/{urllib.parse.quote(wp.replace(' ', '_'))}"
        ext_links.append(f'<a href="{wp_url}" style="color:#66c0f4;">🌐 Wikipedia</a>')

    ext_html = ""
    if ext_links:
        ext_html = f'<div style="margin-top:10px;color:#8f98a0;">{" · ".join(ext_links)}</div>'

    return f'''
    <h2>💾 PCGamingWiki 技术资料 <span style="font-size:0.6em;color:#8f98a0;">
        ({html_lib.escape(page_name)} · 快照于 {html_lib.escape(fetched_at)})</span></h2>
    <div class="pcgw-info">
        {"".join(info_lines) if info_lines else '<div style="color:#8f98a0;">— 无基础信息 —</div>'}
    </div>
    {drm_block}
    {cloud_block}
    {ext_html}
    '''


def render_market_section(market_info, json_path, output_dir):
    """
    渲染 Steam 社区市场物品区块（卡牌墙）。
    如果 fetch_market_items.py 用了 --with-icons，item 里会有 _local_icon 字段，
    此时把源目录的 market_icons/ 拷贝到 assets_dir，并用本地路径引用。
    """
    if not market_info:
        return ""

    items = market_info.get("items", []) or []
    if not items:
        return ""

    total_reported = market_info.get("total_reported", len(items))
    total_fetched = market_info.get("total_fetched", len(items))
    fetched_at = market_info.get("fetched_at_iso", "")
    type_count = market_info.get("type_count", {}) or {}
    with_icons = market_info.get("with_icons", False)

    # 如果有本地图标，把 market_icons/ 目录拷贝到 output_dir/market_icons/
    src_icons_dir = os.path.join(os.path.dirname(os.path.abspath(json_path)), "market_icons")
    dst_icons_dir = os.path.join(os.path.abspath(output_dir), "market_icons")
    if with_icons and os.path.isdir(src_icons_dir) and not os.path.isdir(dst_icons_dir):
        try:
            import shutil
            shutil.copytree(src_icons_dir, dst_icons_dir)
            print(f"   📋 已拷贝市场图标目录: {dst_icons_dir}")
        except Exception as e:
            print(f"   ⚠️ 拷贝图标目录失败: {e}")

    # 类型统计徽章
    type_chips = []
    for t, c in sorted(type_count.items(), key=lambda x: -x[1]):
        type_chips.append(
            f'<span class="mk-type-chip">{html_lib.escape(t)} × {c}</span>'
        )

    # 卡片
    cards = []
    for it in items:
        name = it.get("name", "?")
        asset = it.get("asset_description", {}) or {}
        item_type = asset.get("type", "未知")
        sell_price = it.get("sell_price_text", "—")
        sale_price = it.get("sale_price_text", "")
        sell_listings = it.get("sell_listings", 0) or 0

        # 类型识别（区分闪亮卡牌、普通卡、表情、背景）
        is_foil = "闪亮" in item_type or "Foil" in item_type
        is_emote = "表情" in item_type or "Emoticon" in item_type
        is_bg = "背景" in item_type or "Background" in item_type

        card_class = "mk-card"
        if is_foil:
            card_class += " mk-card-foil"

        # 图标：优先本地，否则用远程 CDN
        local_icon = it.get("_local_icon", "")
        if local_icon and with_icons:
            icon_src = html_lib.escape(local_icon)
        else:
            icon_hash = asset.get("icon_url", "")
            if icon_hash:
                icon_src = f'https://community.akamai.steamstatic.com/economy/image/{html_lib.escape(icon_hash)}'
            else:
                icon_src = ""

        if icon_src:
            icon_html = f'<img class="mk-icon" src="{icon_src}" alt="" loading="lazy">'
        else:
            icon_html = '<div class="mk-icon-fallback">🎴</div>'

        # 类型 emoji
        emoji = "🌟" if is_foil else ("😊" if is_emote else ("🖼️" if is_bg else "🎴"))

        # 简化类型显示（去掉游戏名前缀）
        short_type = item_type.replace(market_info.get("appid", ""), "").strip()

        cards.append(f'''
        <div class="{card_class}">
            {icon_html}
            <div class="mk-body">
                <div class="mk-name" title="{html_lib.escape(name)}">{emoji} {html_lib.escape(name)}</div>
                <div class="mk-type">{html_lib.escape(short_type)}</div>
                <div class="mk-price">{html_lib.escape(sell_price)}</div>
                <div class="mk-meta">📊 {sell_listings} 个挂单</div>
            </div>
        </div>
        ''')

    return f'''
    <h2>🎴 Steam 社区市场物品 <span style="font-size:0.6em;color:#8f98a0;">
        (共 {total_reported} 件 · 已归档 {total_fetched} 件{" · 含离线图标" if with_icons else ""} · 快照于 {html_lib.escape(fetched_at)})</span></h2>
    <div class="mk-type-summary">
        {"".join(type_chips)}
    </div>
    <div class="mk-grid">
        {"".join(cards)}
    </div>
    '''


def render_reviewhistogram_section(reviewhistogram_info):
    """
    渲染评论时间分布（舆论生命周期）区块。
    纯 SVG 双色柱状图，差评波峰月份金色描边高亮。零 JS。
    """
    if not reviewhistogram_info:
        return ""
    try:
        results = reviewhistogram_info.get("raw", {}).get("results", {}) or {}
        rollups = results.get("rollups", []) or []
    except Exception:
        return ""
    if not rollups:
        return ""

    rollup_type = results.get("rollup_type", "month")
    unit_labels = {"month": "月", "week": "周", "day": "日"}
    unit_label = unit_labels.get(rollup_type, "期")
    bad_threshold = {"month": 3, "week": 2, "day": 1}.get(rollup_type, 3)

    total_up = sum(r.get("recommendations_up", 0) or 0 for r in rollups)
    total_down = sum(r.get("recommendations_down", 0) or 0 for r in rollups)
    total = total_up + total_down
    pct = (total_up / total * 100) if total > 0 else 0
    pct_color = "#5c9a4d" if pct >= 70 else "#e8c547" if pct >= 40 else "#c94f4f"

    first_ts = rollups[0].get("date", 0)
    last_ts = rollups[-1].get("date", 0)
    first_str = datetime.fromtimestamp(first_ts).strftime("%Y-%m-%d") if first_ts else "?"
    last_str = datetime.fromtimestamp(last_ts).strftime("%Y-%m-%d") if last_ts else "?"

    n = len(rollups)
    bar_gap = 2
    bar_area_h = 160
    label_h = 20
    svg_h = bar_area_h + label_h
    svg_w = 800
    bar_w = max((svg_w - (n - 1) * bar_gap) / n, 1)

    max_count = max(
        ((r.get("recommendations_up", 0) or 0) + (r.get("recommendations_down", 0) or 0))
        for r in rollups
    ) or 1

    bars = []
    year_labels_seen = set()
    year_label_items = []
    for i, r in enumerate(rollups):
        up = r.get("recommendations_up", 0) or 0
        down = r.get("recommendations_down", 0) or 0
        cnt = up + down
        ts = r.get("date", 0)
        month_str = datetime.fromtimestamp(ts).strftime("%Y-%m") if ts else "?"
        year_str = datetime.fromtimestamp(ts).strftime("%Y") if ts else "?"

        h_total = (cnt / max_count) * bar_area_h if cnt > 0 else 0
        h_up = (up / cnt * h_total) if cnt > 0 else 0
        h_down = h_total - h_up

        x = i * (bar_w + bar_gap)
        y_up = bar_area_h - h_total
        y_down = bar_area_h - h_down

        is_bad = down > up and down >= bad_threshold
        stroke = ' stroke="#ffc82c" stroke-width="1"' if is_bad else ""
        period_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else "?"
        tooltip = f"{period_str} ({unit_label}): {up} 好评 / {down} 差评"

        if h_up > 0:
            bars.append(
                f'<rect x="{x:.1f}" y="{y_up:.1f}" width="{bar_w:.1f}" '
                f'height="{h_up:.1f}" fill="#5c9a4d"{stroke}>'
                f'<title>{tooltip}</title></rect>'
            )
        if h_down > 0:
            bars.append(
                f'<rect x="{x:.1f}" y="{y_down:.1f}" width="{bar_w:.1f}" '
                f'height="{h_down:.1f}" fill="#c94f4f"{stroke}>'
                f'<title>{tooltip}</title></rect>'
            )

        # 稀疏标签策略：按 rollup_type 选粒度
        if ts:
            if rollup_type == "day":
                key = datetime.fromtimestamp(ts).strftime("%Y-%m")   # 日度→每月标一次
            elif rollup_type == "week":
                dt = datetime.fromtimestamp(ts)
                key = f"{dt.year}-Q{(dt.month - 1) // 3 + 1}"        # 周度→每季度标一次
            else:
                key = datetime.fromtimestamp(ts).strftime("%Y")      # 月度→每年标一次

            if key not in year_labels_seen:
                year_labels_seen.add(key)
                label_x = x + bar_w / 2
                year_label_items.append(
                    f'<text x="{label_x:.1f}" y="{bar_area_h + 14}" '
                    f'fill="#8f98a0" font-size="10" text-anchor="middle">{key}</text>'
                )

    bad_periods = []
    for r in rollups:
        up = r.get("recommendations_up", 0) or 0
        down = r.get("recommendations_down", 0) or 0
        if down > up and down >= bad_threshold:
            ts = r.get("date", 0)
            m = datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else "?"
            bad_periods.append(f"{m} ({up}↑/{down}↓)")

    bad_html = ""
    if bad_periods:
        bad_html = (
            f'<div class="rh-bad-list">⚠️ 差评波峰{unit_label}：'
            f'{", ".join(html_lib.escape(m) for m in bad_periods)}</div>'
        )

    return f'''
    <h2>📊 舆论生命周期 <span style="font-size:0.6em;color:#8f98a0;">
        ({html_lib.escape(first_str)} → {html_lib.escape(last_str)} · 
        {len(rollups)} 个{unit_label}度数据 · 累计 {total:,} 条评论 · 
        <span style="color:{pct_color};">{pct:.1f}% 好评</span>)</span></h2>
    <div class="rh-legend">
        <span class="rh-chip" style="background:#5c9a4d;">好评 {total_up:,}</span>
        <span class="rh-chip" style="background:#c94f4f;">差评 {total_down:,}</span>
        <span class="rh-chip" style="background:#ffc82c;color:#000;">🟡 差评波峰高亮</span>
    </div>
    <div class="rh-chart">
        <svg viewBox="0 0 {svg_w} {svg_h}" preserveAspectRatio="none"
             xmlns="http://www.w3.org/2000/svg"
             style="width:100%;height:{svg_h}px;background:#1b2838;border-radius:4px;">
            {"".join(bars)}
            {"".join(year_label_items)}
        </svg>
    </div>
    {bad_html}
    '''


def render_achievements_section(appdetails_data, achievements_global, ctx):
    """
    渲染成就系统区块。
    - appdetails.achievements.highlighted 提供中文名/描述/图标（前 10 个）
    - achievements_global 提供全部成就的百分比
    通过顺序对齐做桥梁匹配（highlighted 按解锁率降序，正好和 global 排序后对齐）
    """
    if not achievements_global:
        return ""

    try:
        raw_list = achievements_global.get("raw", {}).get(
            "achievementpercentages", {}
        ).get("achievements", []) or []
    except Exception:
        return ""

    if not raw_list:
        return ""

    # 解析并按百分比升序（最稀有在前）
    parsed = []
    for a in raw_list:
        try:
            pct = float(a.get("percent", 0))
        except (ValueError, TypeError):
            pct = 0.0
        parsed.append({"name": a.get("name", ""), "percent": pct})
    parsed.sort(key=lambda x: x["percent"])

    # 从 appdetails.achievements.highlighted 提取中文数据（按 name 建映射，也备份按顺序）
    highlighted = []
    try:
        highlighted = appdetails_data.get("achievements", {}).get("highlighted", []) or []
    except Exception:
        pass

    # highlighted 按解锁率降序，转成 dict 用名字匹配（内部名 vs 显示名可能不同，只能按顺序兜底）
    hl_by_name = {}
    for h in highlighted:
        n = h.get("name", "")
        if n:
            hl_by_name[n] = h

    # 分级统计
    tier_count = {"💎": 0, "🔴": 0, "🟠": 0, "🟡": 0, "🟢": 0}
    for a in parsed:
        emoji, _, _ = _achievement_rarity(a["percent"])
        tier_count[emoji] = tier_count.get(emoji, 0) + 1

    # 生成卡片
    cards = []
    for a in parsed:
        name = a["name"]
        pct = a["percent"]
        emoji, tier_label, color = _achievement_rarity(pct)

        # 试图从 highlighted 匹配中文数据（大多数游戏 API 名和显示名不同，匹配率有限）
        hl = hl_by_name.get(name, {})
        display_name = hl.get("name", name) if hl else name
        icon_url = hl.get("path", "") if hl else ""

        # 图标离线化
        if icon_url:
            try:
                local_icon = to_rel(download(icon_url, ctx["assets_dir"], ctx))
                icon_html = f'<img class="ach-icon" src="{local_icon}" alt="">'
            except Exception:
                icon_html = '<div class="ach-icon-fallback">🏆</div>'
        else:
            icon_html = '<div class="ach-icon-fallback">🏆</div>'

        # 百分比进度条宽度（0.5% 保底可见）
        bar_width = max(pct, 0.5)

        cards.append(f'''
        <div class="ach-card">
            {icon_html}
            <div class="ach-body">
                <div class="ach-head">
                    <span class="ach-name">{html_lib.escape(display_name)}</span>
                    <span class="ach-badge" style="background:{color};">{emoji} {tier_label}</span>
                </div>
                <div class="ach-bar-wrap">
                    <div class="ach-bar" style="width:{bar_width:.2f}%; background:{color};"></div>
                    <span class="ach-pct">{pct:.2f}% 玩家解锁</span>
                </div>
            </div>
        </div>
        ''')

    # 分级统计条
    tier_summary_parts = []
    for emoji, label, color in [
        ("💎", "极稀", "#b565f5"), ("🔴", "稀有", "#c94f4f"),
        ("🟠", "中等", "#ff8c42"), ("🟡", "常见", "#e8c547"),
        ("🟢", "大众", "#5c9a4d")
    ]:
        cnt = tier_count.get(emoji, 0)
        if cnt > 0:
            tier_summary_parts.append(
                f'<span class="ach-tier-chip" style="border-color:{color};color:{color};">'
                f'{emoji} {label} × {cnt}</span>'
            )

    return f'''
    <h2>🏆 全球成就解锁率 <span style="font-size:0.6em;color:#8f98a0;">(共 {len(parsed)} 个 · 按稀有度排序)</span></h2>
    <div class="ach-tier-summary">
        {"".join(tier_summary_parts)}
    </div>
    <div class="ach-list">
        {"".join(cards)}
    </div>
    '''


# ────────────────────────────────────────────────
# 游戏新闻渲染
# ────────────────────────────────────────────────
def bbcode_to_html(bbcode):
    """将 Steam BBCode 富文本转成基础 HTML（保留归档价值，安全离线可用）
    🏛️ 架构：这份逻辑与 fetch_news.py 里的 bbcode_to_html 保持一致，
    修 BBCode bug 时优先改这里（零流量重渲染），fetch 侧那份仅为向后兼容旧 json。
    """
    if not bbcode:
        return ""
    s = bbcode
    # Bug 修复 #1：Steam 图片占位符替换
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
    # 列表（含有序列表 [olist]）
    s = re.sub(r'\[list\](.*?)\[/list\]', r'<ul>\1</ul>', s, flags=re.DOTALL)
    s = re.sub(r'\[olist\](.*?)\[/olist\]', r'<ol>\1</ol>', s, flags=re.DOTALL)
    # Bug 修复 #2：[*]...[/*] 完整包裹
    s = re.sub(r'\[\*\]\s*(.*?)\[/\*\]', r'<li>\1</li>', s, flags=re.DOTALL)
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
    # 清理剩余的未识别 BBCode 标签
    s = re.sub(r'\[/?\*\]', '', s)
    s = re.sub(r'\[/?[a-zA-Z][^\]]*\]', '', s)
    return s


def render_news_section(news_path, ctx):
    """
    读取 news.json，渲染游戏新闻区块 HTML。
    description_html 里的图片会通过 rewrite_html_urls 离线化。
    找不到文件返回空串。

    🏛️ 架构（中文优先层）：如果同目录下存在 news_details_zh.json（fetch_news_details.py 产物），
    按 gid 建索引，展示时优先用中文 event_name/announcement_body 覆盖英文 title/contents_bbcode。
    这样归档层（news.json 英文索引 + news_details_zh.json 中文详情）保持分离忠实搬运，
    展示层灵活演绎优先呈现用户友好语言。
    """
    if not news_path or not os.path.exists(news_path):
        return ""
    try:
        with open(news_path, "r", encoding="utf-8") as f:
            ndata = json.load(f)
    except Exception as e:
        print(f"⚠️  读取新闻文件失败: {e}")
        return ""

    items = ndata.get("items", []) or []
    count = ndata.get("count", len(items))
    language = ndata.get("language", "") or ""

    if not items:
        return (
            '<h2>📰 游戏新闻</h2>'
            '<p style="color:#8f98a0;">暂无新闻</p>'
        )

    # 🌏 中文详情覆盖层：探测同目录 news_details_zh.json，用 posttime 时间戳做主键
    # 铁证：两个数据源的 gid 编号体系完全不同（news.gid 是 RSS 站点 gid，
    # ajaxgeteventdetails 返回的是 clan event gid），但同一条公告的
    # news.date_ts == news_details.announcement_body.posttime，可 100% 匹配。
    zh_by_ts = {}
    news_dir = os.path.dirname(os.path.abspath(news_path))
    zh_path = os.path.join(news_dir, "news_details_zh.json")
    if os.path.exists(zh_path):
        try:
            with open(zh_path, "r", encoding="utf-8") as f:
                zh_data = json.load(f)
            for ev in zh_data.get("events", []) or []:
                ann = ev.get("announcement_body", {}) or {}
                ts = int(ann.get("posttime", 0) or 0)
                if ts:
                    zh_by_ts[ts] = ev
            if zh_by_ts:
                print(f"🌏 检测到中文详情 {len(zh_by_ts)} 条，将优先展示中文内容")
        except Exception as e:
            print(f"⚠️  读取中文详情失败: {e}")

    zh_used = 0
    cards = []
    for it in items:
        ts = int(it.get("date_ts", 0) or 0)
        title = it.get("title", "") or "(无标题)"
        link = it.get("link", "") or ""
        pub_iso = it.get("pub_date_iso", "") or "?"

        # 🌏 中文覆盖：用 posttime 时间戳匹配同一条公告
        zh_ev = zh_by_ts.get(ts)
        zh_body = None
        is_zh = False
        if zh_ev:
            zh_ann = zh_ev.get("announcement_body", {}) or {}
            # language=6 是简中，其他数字是回退语言
            # 🛡️ 二次校验：Steam 上游有脏数据（开发者把英文稿标成 language=6），
            # 只要正文里含至少一个 CJK 汉字才认定为真中文，避免伪中文误标 🌏 徽章。
            # 铁证：1049890（小魔女诺贝塔）46 条 language=6 里有 3 条零中文英文稿。
            zh_body_bbcode = zh_ann.get("body") or ""
            zh_title = zh_ev.get("event_name") or zh_ann.get("headline") or ""
            has_any_cjk = bool(re.search(r'[\u4e00-\u9fff]', zh_body_bbcode + zh_title))
            if zh_ann.get("language") == 6 and has_any_cjk:
                if zh_title:
                    title = zh_title
                    is_zh = True
                if zh_body_bbcode:
                    zh_body = bbcode_to_html(zh_body_bbcode)
                    is_zh = True
                if is_zh:
                    zh_used += 1

        # 🏛️ 架构：优先用原始 BBCode 现场转换（零流量修 bug），
        # 找不到时兜底用 fetch 阶段转好的 description_html（向后兼容旧 json）
        if zh_body:
            desc_html = zh_body
        else:
            contents_bbcode = it.get("contents_bbcode", "")
            if contents_bbcode:
                desc_html = bbcode_to_html(contents_bbcode)
            else:
                desc_html = it.get("description_html", "") or ""

        # 描述里的图片/视频离线化
        desc_offline = rewrite_html_urls(desc_html, ctx) if desc_html else ""

        link_html = (
            f'<a href="{html_lib.escape(link)}" target="_blank">🔗 查看原文</a>'
            if link else ""
        )

        zh_badge = ' <span style="font-size:0.7em;color:#5cba47;">🌏 中文</span>' if is_zh else ""

        cards.append(f'''
        <div class="news-card">
          <div class="news-head">
            <span class="news-date">{html_lib.escape(pub_iso)}</span>
            <b class="news-title">{html_lib.escape(title)}{zh_badge}</b>
          </div>
          <div class="news-body">{desc_offline}</div>
          <div class="news-foot">{link_html}</div>
        </div>''')

    lang_note = f' · 语言: {html_lib.escape(language)}' if language else ""
    zh_note = f' · 🌏 中文覆盖 {zh_used}/{len(items)}' if zh_used else ""
    disclaimer = ''
    if zh_used:
        disclaimer = (
            '<div style="margin-top:16px; padding:10px 14px; color:#8f98a0; '
            'font-size:0.8em; text-align:center; border-top:1px dashed #2a475e;">'
            'ℹ️ 中文覆盖依赖 Steam 上游 language 字段，个别公告可能因开发者上传时误标语言而识别不准确'
            '</div>'
        )
    section = f'''
  <h2>📰 游戏新闻 ({count} 条{lang_note}{zh_note})</h2>
  <div class="news-list">
    {"".join(cards)}
  </div>
  {disclaimer}
'''
    return section


# ────────────────────────────────────────────────
# 主转换逻辑
# ────────────────────────────────────────────────
def convert(json_path, output_dir, reviews_path=None, remote_assets=False):
    with open(json_path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    # 自动探测 appreviews.json：优先输入 JSON 同目录，其次输出目录
    if not reviews_path:
        candidates = [
            os.path.join(os.path.dirname(os.path.abspath(json_path)), "appreviews.json"),
            os.path.join(os.path.abspath(output_dir), "appreviews.json"),
        ]
        for c in candidates:
            if os.path.exists(c):
                reviews_path = c
                print(f"🔍 自动检测到玩家评论: {c}")
                break

    # 自动探测 playercount.json（在线人数时间胶囊）
    playercount_info = None
    pc_candidates = [
        os.path.join(os.path.dirname(os.path.abspath(json_path)), "playercount.json"),
        os.path.join(os.path.abspath(output_dir), "playercount.json"),
    ]
    for c in pc_candidates:
        if os.path.exists(c):
            try:
                with open(c, "r", encoding="utf-8") as f:
                    playercount_info = json.load(f)
                print(f"🔍 自动检测到在线人数快照: {c}")
                break
            except Exception:
                pass

    # 自动探测 pcgamingwiki.json（技术资料：DRM/引擎/云同步）
    pcgw_info = None
    pcgw_candidates = [
        os.path.join(os.path.dirname(os.path.abspath(json_path)), "pcgamingwiki.json"),
        os.path.join(os.path.abspath(output_dir), "pcgamingwiki.json"),
    ]
    for c in pcgw_candidates:
        if os.path.exists(c):
            try:
                with open(c, "r", encoding="utf-8") as f:
                    pcgw_info = json.load(f)
                print(f"🔍 自动检测到 PCGamingWiki: {c}")
                break
            except Exception:
                pass

    # 自动探测 market_items.json（Steam 社区市场物品：卡牌/表情/背景）
    market_info = None
    mk_candidates = [
        os.path.join(os.path.dirname(os.path.abspath(json_path)), "market_items.json"),
        os.path.join(os.path.abspath(output_dir), "market_items.json"),
    ]
    for c in mk_candidates:
        if os.path.exists(c):
            try:
                with open(c, "r", encoding="utf-8") as f:
                    market_info = json.load(f)
                print(f"🔍 自动检测到市场物品: {c}")
                break
            except Exception:
                pass

    # 自动探测 reviewhistogram.json（评论时间分布 · 舆论生命周期）
    reviewhistogram_info = None
    rh_candidates = [
        os.path.join(os.path.dirname(os.path.abspath(json_path)), "reviewhistogram.json"),
        os.path.join(os.path.abspath(output_dir), "reviewhistogram.json"),
    ]
    for c in rh_candidates:
        if os.path.exists(c):
            try:
                with open(c, "r", encoding="utf-8") as f:
                    reviewhistogram_info = json.load(f)
                print(f"🔍 自动检测到评论时间分布: {c}")
                break
            except Exception:
                pass

    # 自动探测 achievements_global.json（全球成就解锁率）
    achievements_global = None
    ag_candidates = [
        os.path.join(os.path.dirname(os.path.abspath(json_path)), "achievements_global.json"),
        os.path.join(os.path.abspath(output_dir), "achievements_global.json"),
    ]
    for c in ag_candidates:
        if os.path.exists(c):
            try:
                with open(c, "r", encoding="utf-8") as f:
                    achievements_global = json.load(f)
                print(f"🔍 自动检测到全球成就解锁率: {c}")
                break
            except Exception:
                pass

    # 加载标签映射表（tag_map.json）
    tag_map = {}
    tag_map_candidates = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "tag_map.json"),
        os.path.join(os.path.dirname(os.path.abspath(json_path)), "tag_map.json"),
    ]
    for c in tag_map_candidates:
        if os.path.exists(c):
            try:
                with open(c, "r", encoding="utf-8") as f:
                    tm_data = json.load(f)
                    tag_map = tm_data.get("tags", {})
                    print(f"🏷️ 加载标签映射表: {c}（{len(tag_map)} 个标签）")
                    break
            except Exception:
                pass

    # 自动探测 storebrowse.json（Steam 私有 API，含真实用户标签 + Deck 官方评级）
    storebrowse_info = None
    sb_candidates = [
        os.path.join(os.path.dirname(os.path.abspath(json_path)), "storebrowse.json"),
        os.path.join(os.path.abspath(output_dir), "storebrowse.json"),
    ]
    for c in sb_candidates:
        if os.path.exists(c):
            try:
                with open(c, "r", encoding="utf-8") as f:
                    storebrowse_info = json.load(f)
                print(f"🔍 自动检测到 IStoreBrowseService 数据: {c}")
                break
            except Exception:
                pass

    # 自动探测 protondb.json（Steam Deck / Linux 兼容性）
    protondb_info = None
    pdb_candidates = [
        os.path.join(os.path.dirname(os.path.abspath(json_path)), "protondb.json"),
        os.path.join(os.path.abspath(output_dir), "protondb.json"),
    ]
    for c in pdb_candidates:
        if os.path.exists(c):
            try:
                with open(c, "r", encoding="utf-8") as f:
                    protondb_info = json.load(f)
                print(f"🔍 自动检测到 ProtonDB 数据: {c}")
                break
            except Exception:
                pass

    # 自动探测 news.json（游戏新闻）
    news_path = None
    news_candidates = [
        os.path.join(os.path.dirname(os.path.abspath(json_path)), "news.json"),
        os.path.join(os.path.abspath(output_dir), "news.json"),
    ]
    for c in news_candidates:
        if os.path.exists(c):
            news_path = c
            print(f"🔍 自动检测到游戏新闻: {c}")
            break

    # 自动探测 steamspy.json（拥有量/CCU/玩家标签）
    steamspy_info = None
    ss_candidates = [
        os.path.join(os.path.dirname(os.path.abspath(json_path)), "steamspy.json"),
        os.path.join(os.path.abspath(output_dir), "steamspy.json"),
    ]
    for c in ss_candidates:
        if os.path.exists(c):
            try:
                with open(c, "r", encoding="utf-8") as f:
                    steamspy_info = json.load(f)
                print(f"🔍 自动检测到 SteamSpy 数据: {c}")
                break
            except Exception:
                pass
    
    appid = list(raw.keys())[0]
    data = raw[appid]["data"]
    
    os.makedirs(output_dir, exist_ok=True)
    assets_dir = os.path.join(output_dir, "assets")
    os.makedirs(assets_dir, exist_ok=True)
    
    # 加载/初始化 main.json 清单
    manifest = load_manifest(output_dir)
    manifest["appid"] = str(appid)
    
    # 启动时扫描本地已有文件，把之前下过但没登记的补进清单
    print("📇 扫描已有资源并登记 SHA256...")
    scan_and_register_existing(output_dir, manifest)
    save_manifest(output_dir, manifest)
    
    # 打包成 ctx，穿透传给所有下载函数
    ctx = {
        "session_cache": {},
        "output_dir": output_dir,
        "manifest": manifest,
        "remote_assets": bool(remote_assets),
    }

    if remote_assets:
        print("🌐 浏览器直链模式：不下载资源，HTML 将直接使用远程 URL")
    else:
        print("📥 开始下载资源...")
    
    def to_rel(local_path):
        """把下载函数返回的路径统一转成相对于 output_dir 的正斜杠路径"""
        if not local_path:
            return ""
        if local_path.startswith("http"):
            return local_path  # 下载失败的 fallback，保留原 URL
        return os.path.relpath(local_path, output_dir).replace(os.path.sep, "/")
    
    # 顶图
    header_rel = to_rel(download(data.get("header_image", ""), assets_dir, ctx))

    # 页面动态背景（Steam 商店页那张大背景图）
    background_rel = to_rel(download(data.get("background_raw", "") or data.get("background", ""), assets_dir, ctx))

    # Capsule 卡片图（做游戏库索引页时会用到）
    capsule_rel    = to_rel(download(data.get("capsule_image", ""),   assets_dir, ctx))
    capsule_v5_rel = to_rel(download(data.get("capsule_imagev5", ""), assets_dir, ctx))
    
    # 商店页大背景图（可用作页面 body 背景）
    bg_rel = to_rel(download(data.get("background_raw", "") or data.get("background", ""), assets_dir, ctx))
    
    # 截图（同时下载 thumbnail 和 full，缩略图画廊 + 点击查看大图）
    screenshots_dir = os.path.join(assets_dir, "screenshots")
    screenshot_tags = []
    for s in data.get("screenshots", []):
        thumb_rel = to_rel(download(s.get("path_thumbnail", ""), screenshots_dir, ctx))
        full_rel  = to_rel(download(s.get("path_full", ""),      screenshots_dir, ctx))
        screenshot_tags.append(
            f'<a href="{full_rel}" target="_blank">'
            f'<img src="{thumb_rel}" style="width:300px;margin:4px;border-radius:4px;vertical-align:top;">'
            f'</a>'
        )
    
    # 成就图标 + 名字（隐藏成就会标记 🔒）
    achievements_dir = os.path.join(assets_dir, "achievements")
    achievements_data = data.get("achievements", {}) or {}
    achievement_tags = []
    for ach in achievements_data.get("highlighted", []):
        icon_rel = to_rel(download(ach.get("path", ""), achievements_dir, ctx))
        ach_name = ach.get("localized_name") or ach.get("name", "")
        hidden_mark = "🔒 隐藏成就" if ach.get("hidden") else ""
        achievement_tags.append(
            f'<div style="display:inline-block;text-align:center;margin:8px;width:120px;vertical-align:top;">'
            f'<img src="{icon_rel}" style="width:64px;height:64px;border-radius:4px;">'
            f'<div style="font-size:0.85em;margin-top:4px;">{html_lib.escape(ach_name)}</div>'
            f'<div style="font-size:0.75em;color:#8f98a0;">{hidden_mark}</div>'
            f'</div>'
        )
    
    # 视频（预告片 movies —— Steam 只提供 HLS/DASH 清单，用 yt-dlp 下载）
    movies_dir = os.path.join(assets_dir, "movies")
    movie_tags = []
    
    if not YTDLP_BIN and data.get("movies"):
        print("⚠️  未检测到 yt-dlp，预告片将只保留缩略图 + 在线清单链接。")
        print("    安装建议: pip install -U yt-dlp")
    
    for idx, m in enumerate(data.get("movies", []), start=1):
        # 兼容两种可能的字段布局（新版 API 用 dash/hls，旧版曾用 mp4/webm 直链）
        direct_mp4  = m.get("mp4",  {}).get("max", "") if isinstance(m.get("mp4"),  dict) else ""
        direct_webm = m.get("webm", {}).get("max", "") if isinstance(m.get("webm"), dict) else ""
        # 注意：这里改名避免和外层的 main.json manifest 变量混淆
        stream_manifest = (
            m.get("hls_h264")
            or m.get("dash_h264")
            or m.get("dash_av1")
            or ""
        )
        poster_url = m.get("thumbnail", "")
        name = m.get("name", f"movie_{idx}")
        
        # 缩略图始终下载
        poster_local = download(poster_url, movies_dir, ctx) if poster_url else ""
        poster_rel = (
            os.path.relpath(poster_local, output_dir).replace(os.path.sep, "/")
            if poster_local and os.path.exists(poster_local) else poster_url
        )
        
        # 优先直链，其次 yt-dlp 下 manifest；浏览器直链模式保留远程 URL
        remote_assets = bool(ctx.get("remote_assets"))
        video_local = ""
        if direct_mp4:
            video_local = download(direct_mp4, movies_dir, ctx)
        elif direct_webm:
            video_local = download(direct_webm, movies_dir, ctx)
        elif stream_manifest and (YTDLP_BIN or remote_assets):
            stem = f"movie_{idx}_{hashlib.md5(stream_manifest.encode()).hexdigest()[:8]}"
            video_local = download_via_ytdlp(stream_manifest, movies_dir, stem, ctx=ctx) or ""
        
        is_remote_video = bool(video_local) and str(video_local).startswith("http")
        if is_remote_video or (video_local and os.path.exists(video_local)):
            if is_remote_video:
                # 浏览器直链模式：不下载，直接交给浏览器联网加载（HLS/DASH 需浏览器支持）
                video_rel = video_local
                low = video_rel.lower()
                if ".m3u8" in low:
                    mime = "application/vnd.apple.mpegurl"
                elif ".mpd" in low:
                    mime = "application/dash+xml"
                else:
                    ext = os.path.splitext(video_rel.split("?", 1)[0])[1].lstrip(".").lower() or "mp4"
                    mime = {"mp4": "video/mp4", "webm": "video/webm", "mkv": "video/x-matroska"}.get(ext, "video/mp4")
            else:
                video_rel = os.path.relpath(video_local, output_dir).replace(os.path.sep, "/")
                ext = os.path.splitext(video_rel)[1].lstrip(".").lower() or "mp4"
                mime = {"mp4": "video/mp4", "webm": "video/webm", "mkv": "video/x-matroska"}.get(ext, "video/mp4")
            remote_note = (
                f'<p style="color:#8f98a0;font-size:0.85em;">🌐 直链模式：'
                f'<a href="{video_rel}" target="_blank">直接打开远程视频</a>（由浏览器联网加载）</p>'
                if is_remote_video else ""
            )
            movie_tags.append(f'''
        <div style="margin:16px 0;">
          <h4>{html_lib.escape(name)}</h4>
          <video controls poster="{poster_rel}" style="max-width:100%;border-radius:4px;">
            <source src="{video_rel}" type="{mime}">
          </video>
          {remote_note}
        </div>
        ''')
        else:
            # 没下到视频：显示缩略图 + 原始清单链接作为 fallback
            fallback_link = stream_manifest or direct_mp4 or direct_webm or ""
            fallback_html = (
                f'<p style="color:#8f98a0;font-size:0.9em;">'
                f'⚠️ 未能离线保存视频（缺少 yt-dlp 或下载失败）。'
                f'{"在线清单: <a href=" + chr(34) + fallback_link + chr(34) + ">" + fallback_link[:60] + "...</a>" if fallback_link else ""}'
                f'</p>'
            )
            movie_tags.append(f'''
        <div style="margin:16px 0;">
          <h4>{html_lib.escape(name)}</h4>
          <img src="{poster_rel}" style="max-width:100%;border-radius:4px;">
          {fallback_html}
        </div>
        ''')
    
    # 处理 HTML 片段里的内嵌资源
    print("🔧 处理内嵌 HTML 里的图片/视频...")
    raw_detailed = data.get("detailed_description", "") or ""
    raw_about    = data.get("about_the_game", "") or ""

    def _normalize_html_text(s):
        s = html_lib.unescape(s or "")
        s = re.sub(r"<[^>]+>", " ", s)
        s = re.sub(r"\s+", " ", s)
        return s.strip()

    detailed = rewrite_html_urls(raw_detailed, ctx)
    about    = rewrite_html_urls(raw_about, ctx)

    # 如果 Steam 后台把 detailed_description 和 about_the_game 填成一样，
    # 只保留 detailed_description，避免页面出现两段一模一样的详情。
    if raw_about and _normalize_html_text(raw_detailed) == _normalize_html_text(raw_about):
        print("ℹ️  detailed_description 与 about_the_game 内容一致，去重仅保留详细描述")
        about = ""
    reviews_html = data.get("reviews", "")
    pc_req = data.get("pc_requirements", {})
    pc_req_html = pc_req.get("minimum", "") if isinstance(pc_req, dict) else ""

    # 在线人数快照（playercount.json）
    if playercount_info:
        pc_count = playercount_info.get("player_count", 0) or 0
        pc_iso = playercount_info.get("fetched_at_iso", "") or ""
        playercount_html = f'<div><b>归档时在线人数：</b>🎮 {pc_count:,} 人 · 快照于 {html_lib.escape(pc_iso)}</div>'
    else:
        playercount_html = ""

    # IStoreBrowseService 数据（真实标签 + Deck 官方评级 + 抢先体验 + 系列）
    storebrowse_meta_html = ""
    storebrowse_tags_section = ""
    if storebrowse_info:
        sb_raw = storebrowse_info.get("raw", {})
        sb_items = sb_raw.get("response", {}).get("store_items", [])
        if sb_items:
            sb_item = sb_items[0]
            meta_lines = []

            # 抢先体验徽章
            if sb_item.get("is_early_access"):
                meta_lines.append('<div><b>状态：</b><span style="color:#ffc82c;">🚧 抢先体验中</span></div>')

            # Steam Deck 官方评级
            deck_labels = {
                0: ("❓", "未评级", "#8f98a0"),
                1: ("🚫", "不支持", "#c94f4f"),
                2: ("⚠️", "可玩 (Playable)", "#ffc82c"),
                3: ("✅", "已验证 (Verified)", "#5c7e10"),
            }
            deck_cat = sb_item.get("platforms", {}).get("steam_deck_compat_category", 0)
            if deck_cat > 0:
                emoji, label, color = deck_labels.get(deck_cat, ("❓", "?", "#8f98a0"))
                meta_lines.append(
                    f'<div><b>Steam Deck 官方评级：</b>'
                    f'<span style="color:{color};">{emoji} {label}</span></div>'
                )

            # 所属系列
            franchises = sb_item.get("basic_info", {}).get("franchises", []) or []
            if franchises:
                fr_names = ", ".join(html_lib.escape(f.get("name", "")) for f in franchises if f.get("name"))
                if fr_names:
                    meta_lines.append(f'<div><b>所属系列：</b>{fr_names}</div>')

            # 评论汇总（IStoreBrowseService 内置的，作为兜底/交叉验证）
            sb_reviews = sb_item.get("reviews", {}).get("summary_filtered", {}) or {}
            if sb_reviews and sb_reviews.get("review_count", 0) > 0:
                rc = sb_reviews.get("review_count", 0)
                pp = sb_reviews.get("percent_positive", 0)
                rl = sb_reviews.get("review_score_label", "")
                meta_lines.append(
                    f'<div><b>Steam 评价：</b>{html_lib.escape(rl)} '
                    f'<span style="color:#8f98a0;font-size:0.9em;">({rc:,} 条 · {pp}% 好评)</span></div>'
                )

            storebrowse_meta_html = "\n".join(meta_lines)

            # 官方用户标签区块（Valve 排序，带权重着色）
            sb_tags = sb_item.get("tags", []) or []
            if sb_tags and tag_map:
                tag_chips = []
                max_weight = max((t.get("weight", 0) for t in sb_tags), default=1) or 1
                for i, t in enumerate(sb_tags):
                    tid = str(t.get("tagid", ""))
                    weight = t.get("weight", 0)
                    name = tag_map.get(tid, f"#{tid}")
                    # 前 3 个高亮，其余按权重比例调整不透明度
                    if i < 3:
                        style = 'background:#66c0f4; color:#000; font-weight:bold;'
                    else:
                        opacity = 0.5 + 0.5 * (weight / max_weight)
                        style = f'background:#3a4a5c; color:#c7d5e0; opacity:{opacity:.2f};'
                    tag_chips.append(
                        f'<span class="official-tag" style="{style}" title="权重 {weight}">'
                        f'{html_lib.escape(name)} <sub style="opacity:0.7;">{weight}</sub></span>'
                    )
                storebrowse_tags_section = f"""
    <h2>🏷️ 官方用户标签 <span style="font-size:0.6em; color:#8f98a0;">(Valve 排序 · 共 {len(sb_tags)} 个)</span></h2>
    <div class="official-tags">
        {"".join(tag_chips)}
    </div>
    """

    # ProtonDB 数据（Steam Deck / Linux 兼容性）
    protondb_html = ""
    if protondb_info:
        raw_pdb = protondb_info.get("raw", {})
        tier_map = {
            "platinum": ("🏅", "Platinum", "#b4b4b4"),
            "gold": ("🥇", "Gold", "#cd7f32"),
            "silver": ("🥈", "Silver", "#c0c0c0"),
            "bronze": ("🥉", "Bronze", "#a0522d"),
            "borked": ("💀", "Borked", "#c94f4f"),
            "pending": ("⏳", "Pending", "#8f98a0"),
        }
        pdb_tier = raw_pdb.get("tier", "pending")
        pdb_prov = raw_pdb.get("provisionalTier", "pending")
        pdb_total = raw_pdb.get("total", 0)

        # 优先显示官方评级，如果 pending 则显示暂定评级
        display_tier = pdb_tier if pdb_tier != "pending" else pdb_prov
        emoji, label, color = tier_map.get(display_tier, ("❓", display_tier, "#8f98a0"))

        if pdb_tier == "pending":
            note = f"（暂定，{pdb_total} 份报告）"
        elif pdb_total < 5:
            note = f"（{pdb_total} 份报告，样本不足）"
        else:
            note = f"（{pdb_total} 份报告）"

        protondb_html = (
            f'<div><b>Steam Deck 兼容性：</b>'
            f'<span style="color:{color}">{emoji} {label}</span> '
            f'<span style="color:#8f98a0; font-size:0.9em">{note}</span>'
            f'</div>'
        )

    # SteamSpy 数据（拥有量/CCU/玩家标签）
    steamspy_meta_html = ""
    steamspy_tags_section = ""
    if steamspy_info:
        ss_owners = steamspy_info.get("owners", "") or ""
        ss_ccu = steamspy_info.get("ccu", 0) or 0
        ss_tags = steamspy_info.get("tags", {}) or {}
        meta_lines = []
        if ss_owners:
            meta_lines.append(f'<div><b>拥有量估算：</b>👥 {html_lib.escape(ss_owners)} <span style="color:#8f98a0;font-size:0.85em;">(SteamSpy 估算)</span></div>')
        if ss_ccu:
            meta_lines.append(f'<div><b>24h 峰值同时在线：</b>📡 {ss_ccu:,} 人 <span style="color:#8f98a0;font-size:0.85em;">(SteamSpy)</span></div>')
        steamspy_meta_html = "\n    ".join(meta_lines)

        if ss_tags:
            sorted_tags = sorted(ss_tags.items(), key=lambda x: -x[1])
            tag_chips = "".join(
                f'<span class="tag-chip">{html_lib.escape(str(k))} <span style="opacity:0.7;font-size:0.85em;">{v}</span></span>'
                for k, v in sorted_tags
            )
            steamspy_tags_section = f'''
  <h2>🏷️ 玩家标签 <span style="font-size:0.6em;color:#8f98a0;">(SteamSpy · 玩家投票)</span></h2>
  <div>{tag_chips}</div>
'''

    # PCGamingWiki 技术资料（DRM/引擎/云同步/系列）
    print("💾 渲染 PCGamingWiki 技术资料...")
    pcgw_section = render_pcgamingwiki_section(pcgw_info)

    # Steam 社区市场物品（卡牌/表情/背景，market_items.json）
    print("🎴 渲染社区市场物品...")
    market_section = render_market_section(market_info, json_path, output_dir)

    # 评论时间分布（舆论生命周期，reviewhistogram.json）
    print("📊 渲染评论时间分布...")
    reviewhistogram_section = render_reviewhistogram_section(reviewhistogram_info)

    # 全球成就解锁率区块（achievements_global.json）
    print("🏆 渲染全球成就解锁率...")
    achievements_full_section = render_achievements_section(data, achievements_global, ctx)

    # 游戏新闻区块（news.json)
    print("📰 渲染游戏新闻...")
    news_section = render_news_section(news_path, ctx)

    # 玩家评论区块（appreviews.json）
    print("💬 渲染玩家评论...")
    player_reviews_section = render_reviews_section(reviews_path, output_dir)
    if not player_reviews_section:
        player_reviews_section = '<h2>💬 玩家评论</h2><p style="color:#8f98a0;">未提供 appreviews.json，跳过玩家评论区块。运行 <code>fetch_appreviews.py</code> 可拉取评论。</p>'

    # 媒体评价区块（appdetails.reviews 字段，独立于玩家评论共存）
    # 铁证：之前用 if/else 互斥导致有玩家评论时媒体评价被丢弃，改为两块共存相邻布局
    if reviews_html:
        media_reviews_section = f'<h2>📰 媒体评价</h2><div style="background:#2a475e;padding:12px 16px;border-radius:4px;border-left:3px solid #66c0f4;">{reviews_html}</div>'
    else:
        media_reviews_section = ''
    # 把媒体评价拼到玩家评论之前，视觉上"媒体在前 · 玩家在后"
    player_reviews_section = media_reviews_section + player_reviews_section

    # 元数据
    name = data.get("name", "Unknown Game")
    short_desc = data.get("short_description", "")
    developers = ", ".join(data.get("developers", []))
    publishers = ", ".join(data.get("publishers", []))
    release_date = data.get("release_date", {}).get("date", "")
    languages = data.get("supported_languages", "")
    website = data.get("website", "")
    
    price = data.get("price_overview", {}) or {}
    price_str = ""
    if price:
        initial_fmt  = price.get("initial_formatted", "") or ""
        final_fmt    = price.get("final_formatted", "") or ""
        discount_pct = price.get("discount_percent", 0) or 0
        # 折扣为 0 或 initial_formatted 为空 → 只显示最终价
        if discount_pct > 0 and initial_fmt and initial_fmt != final_fmt:
            price_str = f'{initial_fmt} → <b>{final_fmt}</b> (-{discount_pct}%)'
        else:
            price_str = f'<b>{final_fmt}</b>'
    
    mc = data.get("metacritic", {})
    mc_str = f'<a href="{mc.get("url","")}">{mc.get("score", "")} 分</a>' if mc else "—"
    
    # ── 新增元数据字段 ─────────────────────────
    steam_appid  = data.get("steam_appid", appid)
    is_free      = data.get("is_free", False)
    required_age = data.get("required_age", 0)
    
    controller     = data.get("controller_support", "无")
    controller_map = {"full": "完全支持", "partial": "部分支持"}
    controller_str = controller_map.get(controller, controller)
    
    platforms = data.get("platforms", {}) or {}
    plat_list = [k.capitalize() for k, v in platforms.items() if v]
    plat_str  = " / ".join(plat_list) if plat_list else "—"
    
    categories = data.get("categories", []) or []
    cat_str = "、".join(_translate_category(c.get("description", "")) for c in categories) if categories else "—"
    
    genres = data.get("genres", []) or []
    genre_str = "、".join(g.get("description", "") for g in genres) if genres else "—"
    
    dlc_list = data.get("dlc", []) or []
    dlc_str = "、".join(
        f'<a href="https://store.steampowered.com/app/{d}/">{d}</a>' for d in dlc_list
    ) if dlc_list else "—"
    
    demos = data.get("demos", []) or []
    demo_str = "、".join(
        f'<a href="https://store.steampowered.com/app/{d.get("appid")}/">试玩版 {d.get("appid")}</a>'
        for d in demos
    ) if demos else "—"
    
    recommendations_total = (data.get("recommendations", {}) or {}).get("total", 0)
    release_coming_soon   = (data.get("release_date", {}) or {}).get("coming_soon", False)
    release_status = "🕒 即将发售" if release_coming_soon else "✅ 已发售"
    
    legal_notice  = data.get("legal_notice", "")
    support       = data.get("support_info", {}) or {}
    support_url   = support.get("url", "")
    support_email = support.get("email", "")
    
    # ── 补齐挑刺字段 ─────────────────────────
    # type: game / dlc / demo / mod / music / video / …
    app_type = data.get("type", "")
    type_map = {"game": "🎮 游戏", "dlc": "🧩 DLC", "demo": "🎯 试玩版",
                "mod": "🛠️ Mod", "music": "🎵 原声", "video": "🎬 视频"}
    app_type_str = type_map.get(app_type, app_type) or "—"
    
    # packages / package_groups：购买选项
    packages_ids = data.get("packages", []) or []
    package_groups = data.get("package_groups", []) or []
    # 根据 price_overview.currency 动态选择货币符号，避免美元游戏显示 ¥
    currency_code = (price or {}).get("currency", "")
    currency_symbol_map = {
        "CNY": "¥", "USD": "$", "EUR": "€", "GBP": "£",
        "JPY": "¥", "KRW": "₩", "TWD": "NT$", "HKD": "HK$",
        "RUB": "₽", "BRL": "R$", "AUD": "A$", "CAD": "C$",
    }
    currency_symbol = currency_symbol_map.get(currency_code, currency_code or "¥")
    package_rows = []
    for pg in package_groups:
        for sub in pg.get("subs", []) or []:
            pkg_id   = sub.get("packageid", "")
            opt_text = sub.get("option_text", "")
            # option_text 可能内嵌 HTML，直接原样输出（Steam 官方就带 span）
            price_cents = sub.get("price_in_cents_with_discount", 0)
            price_str_pkg = f"{currency_symbol} {price_cents/100:.2f}" if price_cents else "—"
            package_rows.append(
                f'<tr><td style="padding:4px 12px;">#{pkg_id}</td>'
                f'<td style="padding:4px 12px;">{opt_text}</td>'
                f'<td style="padding:4px 12px;color:#8f98a0;">{price_str_pkg}</td></tr>'
            )
    packages_html = (
        '<table style="border-collapse:collapse;background:#2a475e;border-radius:4px;">'
        '<thead><tr>'
        '<th style="padding:6px 12px;text-align:left;">Package ID</th>'
        '<th style="padding:6px 12px;text-align:left;">购买选项</th>'
        '<th style="padding:6px 12px;text-align:left;">折后价</th>'
        '</tr></thead><tbody>' + "".join(package_rows) + '</tbody></table>'
    ) if package_rows else "<p style='color:#8f98a0;'>无购买选项信息</p>"
    
    # content_descriptors：内容警告标签
    content_desc = data.get("content_descriptors", {}) or {}
    cd_ids   = content_desc.get("ids", []) or []
    cd_notes = content_desc.get("notes") or ""
    # 转义后把 \r\n / \n 变成 <br>，保留原文换行显示
    cd_notes_html = (
        html_lib.escape(cd_notes).replace("\r\n", "<br>").replace("\n", "<br>")
        if cd_notes else "无"
    )
    cd_html  = (
        f'<div>ID 列表: {", ".join(str(x) for x in cd_ids) if cd_ids else "无"}</div>'
        f'<div style="margin-top:4px;">备注: {cd_notes_html}</div>'
    )
    
    # 地区评级
    ratings = data.get("ratings", {}) or {}
    region_map = {
        "dejus": "巴西 (DEJUS)", "steam_germany": "德国 (USK)", "igrs": "印尼 (IGRS)",
        "esrb": "北美 (ESRB)", "pegi": "欧洲 (PEGI)", "cero": "日本 (CERO)",
        "usk": "德国 (USK)", "csrr": "台湾 (CSRR)",
    }
    ratings_rows = []
    for region_key, region_data in ratings.items():
        region_name = region_map.get(region_key, region_key)
        rating_val  = region_data.get("rating", "")
        descriptors = region_data.get("descriptors", "")
        # descriptors 里可能含 \n（如 igrs 的多条描述），转 <br> 保留多行
        descriptors_html = html_lib.escape(descriptors).replace("\r\n", "<br>").replace("\n", "<br>")
        ratings_rows.append(
            f'<tr><td style="padding:4px 12px;">{html_lib.escape(region_name)}</td>'
            f'<td style="padding:4px 12px;"><b>{html_lib.escape(rating_val)}+</b></td>'
            f'<td style="padding:4px 12px;color:#8f98a0;">{descriptors_html}</td></tr>'
        )
    ratings_html = (
        '<table style="border-collapse:collapse;background:#2a475e;border-radius:4px;">'
        '<thead><tr>'
        '<th style="padding:6px 12px;text-align:left;">地区</th>'
        '<th style="padding:6px 12px;text-align:left;">评级</th>'
        '<th style="padding:6px 12px;text-align:left;">描述</th>'
        '</tr></thead><tbody>' + "".join(ratings_rows) + '</tbody></table>'
    ) if ratings_rows else "<p style='color:#8f98a0;'>无地区评级信息</p>"
    
    # 拼 HTML
    html_output = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>{html_lib.escape(name)}</title>
<style>
  html {{
    background: #1b2838 url("{bg_rel}") no-repeat center top;
    background-size: cover; background-attachment: fixed;
  }}
  body {{
    font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
    max-width: 960px; margin: 0 auto; padding: 20px;
    background: rgba(27, 40, 56, 0.92); color: #c7d5e0; line-height: 1.6;
    min-height: 100vh;
  }}
  h1 {{ color: #66c0f4; border-bottom: 2px solid #66c0f4; padding-bottom: 8px; }}
  h2 {{ color: #66c0f4; margin-top: 32px; }}
  .bb_paragraph {{ margin: 8px 0; }}
  .bb_img {{ max-width: 100%; height: auto; border-radius: 4px; }}
  .bb_tag {{ color: #66c0f4; font-size: 1.4em; margin-top: 20px; }}
  .bb_ul {{ background: #2a475e; padding: 12px 24px; border-radius: 4px; list-style: none; }}
  .bb_ul li {{ margin: 4px 0; }}
  /* 🆕 BBCode 扩展元素样式（spoiler 折叠 / 标题 / 引用 / 代码 / 分隔线） */
  .bb-spoiler {{
    background: #22303f; border: 1px dashed #4a6a8a; border-radius: 4px;
    margin: 8px 0; padding: 6px 10px;
  }}
  .bb-spoiler summary {{
    cursor: pointer; color: #a3d4ff; font-size: 0.92em;
    user-select: none; list-style: none;
  }}
  .bb-spoiler summary::-webkit-details-marker {{ display: none; }}
  .bb-spoiler summary::before {{ content: "▶ "; font-size: 0.8em; }}
  .bb-spoiler[open] summary::before {{ content: "▼ "; }}
  .bb-spoiler[open] summary {{ margin-bottom: 6px; color: #66c0f4; }}
  .bb-spoiler-body {{ color: #dbe6f0; }}
  .bb-h1 {{ font-size: 1.5em; font-weight: bold; color: #ffffff; margin: 12px 0 6px; }}
  .bb-h2 {{ font-size: 1.28em; font-weight: bold; color: #e8f4ff; margin: 10px 0 5px; }}
  .bb-h3 {{ font-size: 1.1em; font-weight: bold; color: #d4e8fa; margin: 8px 0 4px; }}
  .bb-quote {{
    border-left: 3px solid #66c0f4; background: rgba(42,71,94,0.5);
    margin: 8px 0; padding: 6px 12px; color: #b8c9d9;
  }}
  .bb-quote small {{ color: #8f98a0; }}
  .bb-code {{
    background: #16202b; border: 1px solid #2a475e; border-radius: 4px;
    padding: 8px 12px; font-family: Consolas, Monaco, monospace;
    font-size: 0.88em; white-space: pre-wrap; word-break: break-all;
  }}
  .bb-hr {{ border: none; border-top: 1px solid #3a5570; margin: 12px 0; }}
  .bb-table {{
    border-collapse: collapse; margin: 8px 0; max-width: 100%; display: block;
    overflow-x: auto; background: rgba(42,71,94,0.4); border-radius: 4px;
  }}
  .bb-table th, .bb-table td {{
    border: 1px solid #3a5570; padding: 5px 10px; text-align: left;
  }}
  .bb-table th {{ background: #2a475e; color: #cfe6ff; }}
  video {{ max-width: 100%; border-radius: 4px; }}
  .meta {{ background: #2a475e; padding: 16px; border-radius: 4px; margin: 12px 0; }}
  .meta div {{ margin: 4px 0; }}
  a {{ color: #66c0f4; }}
  .offline-badge {{
    display: inline-block; background: #5c7e10; color: white;
    padding: 2px 8px; border-radius: 3px; font-size: 0.8em; margin-left: 8px;
  }}
  .tag-chip {{
    display: inline-block; background: #316282; color: #cfe6ff;
    padding: 2px 10px; margin: 3px; border-radius: 3px; font-size: 0.85em;
  }}
  .gallery a img {{ transition: transform 0.15s ease, box-shadow 0.15s ease; }}
  .gallery a:hover img {{ transform: scale(1.03); box-shadow: 0 0 12px rgba(102,192,244,0.4); }}
  body {{
    background-image: linear-gradient(rgba(27,40,56,0.88), rgba(27,40,56,0.96)), url("{background_rel}");
    background-size: cover;
    background-attachment: fixed;
    background-position: center top;
  }}
  .achievement-grid {{ display: flex; flex-wrap: wrap; }}
  .review-summary {{ background:#2a475e; padding:12px 16px; border-radius:4px; margin:12px 0; }}
  .review-bar {{ background:#1b2838; height:10px; border-radius:5px; overflow:hidden; margin-bottom:8px; }}
  .review-bar span {{ display:block; height:100%; background:#66c0f4; }}
  .review-stats {{ color:#cfe6ff; font-size:0.9em; }}
  /* 归档完整度横幅 */
  .review-coverage-warn {{
    background: linear-gradient(90deg, #5c3a1a 0%, #4a2f18 100%);
    border-left: 4px solid #ffc82c;
    color: #ffe3a3; padding: 12px 16px; border-radius: 4px;
    margin: 12px 0; font-size: 0.95em; line-height: 1.6;
  }}
  .review-coverage-warn small {{ color: #d9c088; font-size: 0.85em; }}
  .review-coverage-ok {{
    background: linear-gradient(90deg, #2a4a2a 0%, #1e3a1e 100%);
    border-left: 4px solid #5c9a4d;
    color: #b8e0a8; padding: 10px 16px; border-radius: 4px;
    margin: 12px 0; font-size: 0.95em;
  }}
  /* 评论分批折叠段 */
  .review-chunk {{
    background: #1b2838; border: 1px solid #2a475e; border-radius: 4px;
    margin: 12px 0; padding: 0;
  }}
  .review-chunk summary {{
    cursor: pointer; padding: 12px 16px; color: #66c0f4;
    font-size: 1.02em; font-weight: 600; user-select: none;
    background: #2a3f5f; border-radius: 4px 4px 0 0;
  }}
  .review-chunk summary:hover {{ background: #375278; color: #a3d4ff; }}
  .review-chunk[open] summary {{ border-radius: 4px 4px 0 0; margin-bottom: 8px; }}
  .review-chunk > .review-list {{ padding: 0 12px 12px 12px; }}
  .review-list {{ display:flex; flex-direction:column; gap:12px; margin-top:12px; }}
  .review-card {{ display:flex; gap:12px; background:#2a475e; padding:12px; border-radius:4px; border-left:3px solid #66c0f4; }}
  .review-card-negative {{ border-left-color:#c94a4a; }}
  .review-avatar {{ width:48px; height:48px; border-radius:4px; object-fit:cover; flex-shrink:0; }}
  .review-avatar-fallback {{ display:flex; align-items:center; justify-content:center; background:#1b2838; font-size:24px; }}
  .review-body {{ flex:1; min-width:0; }}
  .review-head {{ display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin-bottom:6px; }}
  .review-playtime {{ color:#8f98a0; font-size:0.85em; }}
  .thumb-up {{ color:#a4d007; font-weight:bold; }}
  .thumb-down {{ color:#c94a4a; font-weight:bold; }}
  .review-badges {{ margin:4px 0; }}
  .review-badges .badge {{ display:inline-block; background:#316282; color:#cfe6ff; padding:1px 8px; margin:2px 4px 2px 0; border-radius:3px; font-size:0.75em; }}
  .review-badges .badge.lang {{ background:#4a5f7a; }}
  .review-text {{ white-space:normal; line-height:1.55; margin:6px 0; color:#e6e6e6; }}
        .review-foot {{ color:#8f98a0; font-size:0.8em; margin-top:6px; }}
  /* 🎨 玩家评论交互工具栏（Steam 风格） */
  .review-toolbar {{
    display: flex; flex-wrap: wrap; gap: 10px 16px; align-items: center;
    background: linear-gradient(180deg, rgba(42,71,94,0.7) 0%, rgba(27,40,56,0.7) 100%);
    backdrop-filter: blur(8px); -webkit-backdrop-filter: blur(8px);
    border: 1px solid rgba(102,192,244,0.15);
    padding: 12px 16px; border-radius: 6px; margin: 14px 0 10px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.25);
  }}
  .review-toolbar label {{
    display: inline-flex; align-items: center; gap: 6px;
    color: #a3d4ff; font-size: 0.88em; font-weight: 500;
  }}
  .review-toolbar select,
  .review-toolbar input[type="search"],
  .review-toolbar input[type="text"] {{
    appearance: none; -webkit-appearance: none; -moz-appearance: none;
    background: #1b2838; color: #e6e6e6;
    border: 1px solid #3a5570; border-radius: 4px;
    padding: 6px 28px 6px 10px; font-size: 0.9em;
    font-family: inherit; line-height: 1.4;
    transition: border-color 0.15s, box-shadow 0.15s;
    cursor: pointer;
  }}
  .review-toolbar input[type="search"],
  .review-toolbar input[type="text"] {{
    padding: 6px 10px; min-width: 200px; cursor: text;
  }}
  .review-toolbar select {{
    background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='10' height='6' viewBox='0 0 10 6'><path fill='%2366c0f4' d='M0 0l5 6 5-6z'/></svg>");
    background-repeat: no-repeat;
    background-position: right 10px center;
  }}
  .review-toolbar select:hover,
  .review-toolbar input:hover {{ border-color: #66c0f4; }}
  .review-toolbar select:focus,
  .review-toolbar input:focus {{
    outline: none; border-color: #66c0f4;
    box-shadow: 0 0 0 2px rgba(102,192,244,0.25);
  }}
  .review-toolbar #review-copy-all {{
    appearance: none; -webkit-appearance: none;
    background: linear-gradient(180deg, #5c9a4d 0%, #3d7a2e 100%);
    color: #fff; border: none; border-radius: 4px;
    padding: 6px 14px; font-size: 0.88em; font-weight: 500;
    cursor: pointer; transition: filter 0.15s, transform 0.05s;
  }}
  .review-toolbar #review-copy-all:hover {{ filter: brightness(1.15); }}
  .review-toolbar #review-copy-all:active {{ transform: translateY(1px); }}
  .review-count {{
    margin-left: auto; color: #8f98a0; font-size: 0.85em;
    font-style: italic;
  }}
  /* 🎨 评论卡片 hover 微高亮 */
  .review-card {{ transition: background 0.15s, transform 0.05s; }}
  .review-card:hover {{ background: #2f5273; }}
  /* 🎨 分页器 */
  .review-pager {{
    display: flex; justify-content: center; align-items: center;
    gap: 12px; margin: 18px 0 8px; padding: 12px 0;
    border-top: 1px solid rgba(102,192,244,0.12);
  }}
  .pg-btn {{
    appearance: none; -webkit-appearance: none;
    background: linear-gradient(180deg, #67c1f5 0%, #417a9b 100%);
    color: #fff; border: none; border-radius: 4px;
    padding: 8px 18px; font-size: 0.9em; font-weight: 500;
    font-family: inherit; cursor: pointer;
    transition: filter 0.15s, transform 0.05s;
    box-shadow: 0 1px 3px rgba(0,0,0,0.3);
  }}
  .pg-btn:hover:not(:disabled) {{ filter: brightness(1.2); }}
  .pg-btn:active:not(:disabled) {{ transform: translateY(1px); }}
  .pg-btn:disabled {{
    background: #3a4a5a; color: #6a7a8a;
    cursor: not-allowed; opacity: 0.6;
  }}
  .pg-info {{
    color: #a3d4ff; font-size: 0.9em;
    padding: 0 8px;
  }}
  /* 📱 移动端适配 */
  @media (max-width: 640px) {{
    .review-toolbar {{ padding: 10px 12px; gap: 8px 10px; }}
    .review-toolbar input[type="search"],
    .review-toolbar input[type="text"] {{ min-width: 140px; flex: 1; }}
    .review-count {{ margin-left: 0; width: 100%; text-align: center; }}
  }}
        .official-tags {{ display:flex; flex-wrap:wrap; gap:6px; margin:10px 0 20px; }}
        .official-tag {{ padding:4px 10px; border-radius:3px; font-size:0.9em; }}
        .ach-tier-summary {{ display:flex; flex-wrap:wrap; gap:8px; margin:10px 0 16px; }}
        .ach-tier-chip {{ padding:3px 10px; border:1px solid; border-radius:12px; font-size:0.85em; background:rgba(0,0,0,0.2); }}
        .ach-list {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(340px,1fr)); gap:10px; margin-bottom:20px; }}
        .ach-card {{ display:flex; gap:10px; background:#2a475e; padding:10px; border-radius:4px; align-items:center; }}
        .ach-icon {{ width:48px; height:48px; border-radius:3px; flex-shrink:0; }}
        .ach-icon-fallback {{ width:48px; height:48px; display:flex; align-items:center; justify-content:center; background:#1b2838; border-radius:3px; font-size:1.6em; flex-shrink:0; }}
        .ach-body {{ flex:1; min-width:0; }}
        .ach-head {{ display:flex; justify-content:space-between; align-items:center; gap:8px; margin-bottom:5px; }}
        .ach-name {{ color:#c7d5e0; font-weight:bold; font-size:0.95em; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
        .ach-badge {{ color:#000; padding:1px 8px; border-radius:3px; font-size:0.75em; white-space:nowrap; }}
        .ach-bar-wrap {{ position:relative; background:#1b2838; height:18px; border-radius:3px; overflow:hidden; }}
        .ach-bar {{ height:100%; transition:width 0.3s; }}
        .ach-pct {{ position:absolute; top:0; left:0; right:0; text-align:center; line-height:18px; font-size:0.75em; color:#fff; text-shadow:0 0 3px #000; }}
        .rh-legend {{ display:flex; flex-wrap:wrap; gap:8px; margin:8px 0 12px; }}
        .rh-chip {{ padding:3px 10px; border-radius:3px; font-size:0.85em; color:#fff; }}
        .rh-chart {{ margin-bottom:10px; }}
        .rh-bad-list {{ color:#ffc82c; font-size:0.9em; margin-bottom:20px; }}
        .mk-type-summary {{ display:flex; flex-wrap:wrap; gap:6px; margin:10px 0 14px; }}
        .mk-type-chip {{ padding:3px 10px; background:#3a4a5c; color:#c7d5e0; border-radius:12px; font-size:0.85em; }}
        .mk-grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(180px,1fr)); gap:12px; margin-bottom:24px; }}
        .mk-card {{ background:#2a475e; padding:10px; border-radius:4px; display:flex; flex-direction:column; align-items:center; text-align:center; }}
        .mk-card-foil {{ border:1px solid #ffc82c; box-shadow:0 0 8px rgba(255,200,44,0.3); }}
        .mk-icon {{ width:96px; height:96px; object-fit:contain; border-radius:3px; background:#1b2838; }}
        .mk-icon-fallback {{ width:96px; height:96px; display:flex; align-items:center; justify-content:center; background:#1b2838; border-radius:3px; font-size:2em; }}
        .mk-body {{ width:100%; margin-top:8px; }}
        .mk-name {{ color:#c7d5e0; font-weight:bold; font-size:0.9em; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
        .mk-type {{ color:#8f98a0; font-size:0.75em; margin-top:2px; }}
        .mk-price {{ color:#66c0f4; font-weight:bold; margin-top:4px; }}
        .mk-meta {{ color:#8f98a0; font-size:0.75em; margin-top:2px; }}
        .pcgw-info {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(280px,1fr)); gap:6px; margin:10px 0; }}
        .pcgw-drm-list {{ display:flex; flex-wrap:wrap; gap:6px; margin:8px 0 12px; }}
        .pcgw-drm-chip {{ padding:3px 10px; border-radius:3px; font-size:0.85em; color:#fff; }}
        .pcgw-cloud-list {{ display:flex; flex-wrap:wrap; gap:6px; margin:8px 0 12px; }}
        .pcgw-cloud-chip {{ padding:3px 10px; border:1px solid; border-radius:12px; font-size:0.85em; background:rgba(0,0,0,0.2); }}
  .news-list {{ display:flex; flex-direction:column; gap:12px; margin-top:12px; }}
  .news-card {{ background:#2a475e; padding:14px 16px; border-radius:4px; border-left:3px solid #a4d007; }}
  .news-head {{ display:flex; flex-direction:column; gap:4px; margin-bottom:10px; }}
  .news-date {{ color:#8f98a0; font-size:0.8em; font-family:monospace; }}
  .news-title {{ color:#ffffff; font-size:1.35em; font-weight:bold; line-height:1.3; }}
  .news-body {{ line-height:1.6; color:#e6e6e6; margin:6px 0; white-space:pre-wrap; word-wrap:break-word; }}
  .news-body img, .news-body video {{ max-width:100% !important; height:auto !important; border-radius:6px; display:block !important; margin:18px auto !important; box-shadow:0 2px 8px rgba(0,0,0,0.3); float:none !important; clear:both !important; }}
  .news-body p {{ margin:8px 0; }}
  .news-body a {{ color:#66c0f4; }}
  .news-foot {{ margin-top:8px; font-size:0.85em; }}
  footer {{ margin-top: 40px; padding-top: 20px; border-top: 1px solid #2a475e;
            text-align: center; color: #8f98a0; font-size: 0.85em; }}
</style>
</head>
<body>
  <img src="{header_rel}" style="width:100%;border-radius:4px;">
  <h1>{html_lib.escape(name)}<span class="offline-badge">离线版</span></h1>
  <p style="font-size:1.1em;font-style:italic;">{html_lib.escape(short_desc)}</p>
  
  <div class="meta">
    <div><b>类型：</b>{app_type_str}</div>
    <div><b>开发商：</b>{html_lib.escape(developers)}</div>
    <div><b>发行商：</b>{html_lib.escape(publishers)}</div>
    <div><b>发售日期：</b>{html_lib.escape(release_date)} · {release_status}</div>
    <div><b>平台：</b>{html_lib.escape(plat_str)}</div>
    <div><b>官方类型：</b>{html_lib.escape(genre_str)}</div>
    <div><b>语言：</b>{languages}</div>
    <div><b>价格：</b>{price_str} {'（免费）' if is_free else ''}</div>
    <div><b>Metacritic：</b>{mc_str}</div>
    <div><b>Steam 推荐数：</b>{recommendations_total}</div>
    <div><b>控制器支持：</b>{html_lib.escape(controller_str)}</div>
    <div><b>年龄门槛：</b>{required_age}+</div>
    <div><b>DLC：</b>{dlc_str}</div>
    <div><b>试玩版：</b>{demo_str}</div>
    <div><b>官网：</b><a href="{website}">{website}</a></div>
                {storebrowse_meta_html}
                {steamspy_meta_html}
                {protondb_html}
                {playercount_html}
  </div>
  
  <h2>Steam 特性</h2>
  <div>
    {"".join(f'<span class="tag-chip">{html_lib.escape(_translate_category(c.get("description","")))}</span>' for c in categories) or "—"}
  </div>
  
    {storebrowse_tags_section}
    {steamspy_tags_section}

    {achievements_full_section}
  
  <h2>关于游戏</h2>
  {about}
  
  <h2>详细描述</h2>
  {detailed}
  
  <h2>系统要求</h2>
  {pc_req_html}
  
  <h2>成就 (共 {achievements_data.get("total", 0)} 个)</h2>
  <div class="achievement-grid">
    {''.join(achievement_tags) or "<p style='color:#8f98a0;'>无成就信息</p>"}
  </div>
  
  <h2>截图</h2>
  <div class="gallery">
    {''.join(screenshot_tags)}
  </div>
  
  <h2>预告片</h2>
  {''.join(movie_tags)}
  
  <h2>购买选项</h2>
  {packages_html}
  
  <h2>内容描述符</h2>
  {cd_html}
  
  <h2>地区评级</h2>
  {ratings_html}
  
    {news_section}
    {pcgw_section}
    {reviewhistogram_section}
    {market_section}
    {player_reviews_section}
  
  <footer>
    <div>{legal_notice}</div>
    <div style="margin-top:8px;">
      客服：
      {f'<a href="{support_url}">{support_url}</a>' if support_url else ''}
      {f' · <a href="mailto:{support_email}">{support_email}</a>' if support_email else ''}
    </div>
    <div style="margin-top:8px;">
      离线归档 · 数据来源：Steam appdetails API · AppID {steam_appid}
    </div>
  </footer>
  <script src="assets/toc.js" defer></script>
</body>
</html>
"""
    
    # 目录导航脚本（所有归档页通用，写入 assets/toc.js）
    _write_toc_js(output_dir)

    # 写入 index.html
    index_path = os.path.join(output_dir, "index.html")
    with open(index_path, "w", encoding="utf-8") as f:
        f.write(html_output)
    
    print(f"\n✅ 转换完成！")
    print(f"   📄 主页面: {index_path}")
    print(f"   📁 资源目录: {assets_dir}")
    print(f"   📊 本次会话涉及 {len([v for v in ctx['session_cache'].values() if not v.startswith('http')])} 个资源")
    print(f"   🧾 清单已保存: {os.path.join(output_dir, MANIFEST_NAME)} ({len(ctx['manifest']['files'])} 个文件登记在册)")
    if remote_assets:
        print(f"\n💡 浏览器直链模式：打开 index.html 时需要联网加载远程资源。")
    else:
        print(f"\n💡 双击 index.html 即可离线查看，完全不联网。")

# ────────────────────────────────────────────────
# 入口
# ────────────────────────────────────────────────
if __name__ == "__main__":
    remote_assets_arg = "--remote-assets" in sys.argv
    cli_args = [arg for arg in sys.argv[1:] if arg != "--remote-assets"]

    if len(cli_args) not in (2, 3):
        print("用法:")
        print("  python steam_archiver.py <input.json> <output_dir> [appreviews.json] [--remote-assets]")
        print()
        print("示例:")
        print("  python steam_archiver.py appdetails_schinese.json ./out")
        print("  python steam_archiver.py appdetails_schinese.json ./out --remote-assets")
        print("  python steam_archiver.py appdetails_schinese.json ./out appreviews.json --remote-assets")
        print()
        print("提示: 若 <input.json> 或 <output_dir> 同目录存在 appreviews.json，会自动加载。")
        sys.exit(1)

    reviews_arg = cli_args[2] if len(cli_args) == 3 else None
    try:
        convert(cli_args[0], cli_args[1], reviews_arg, remote_assets_arg)
    except KeyboardInterrupt:
        print("\n⚠️  用户中断（Ctrl+C）。")
        print("   已下载并登记的文件仍然保留在磁盘上，main.json 已增量保存。")
        print("   再次运行本脚本会跳过已完成的资源，从中断处继续。")
        sys.exit(130)
    except Exception as e:
        print(f"\n❌ 发生未处理异常: {type(e).__name__}: {e}")
        print("   已下载的资源和 main.json 里已登记的记录仍然有效。")
        raise
