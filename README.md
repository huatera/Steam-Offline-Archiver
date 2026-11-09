# Steam-Offline-Archiver

> 纯 Python 标准库 · 零第三方依赖 · 真实工程编码的 Steam 游戏离线归档工具链
>
> 用最朴素的 Python 标准库 + HTTP，把 Steam 商店/社区/第三方数据源里能拿的信息**忠实搬运**下来，
> 长期离线保存。归档层不加工、不裁剪，展示层灵活演绎。

---

## 🎯 设计哲学

- **一个脚本一个数据源** —— 每个 `fetch_xxx.py` 只负责一个来源，互不耦合
- **归档层忠实搬运** —— 拿到什么存什么，不做数据清洗
- **展示层灵活演绎** —— GUI / 报表在 `archiver_gui.py` / `steam_archiver.py` 里自由发挥
- **中间文件平级共存** —— `news.json`、`news_details_zh.json` 独立存在，互不覆盖
- **无损归档** —— 网络抖动、拉取失败绝不覆盖已有产物

---

## 🗂️ 工具链一览

| # | 脚本 | 数据源 |
|:---:|---|---|
| 1 | `fetch_appdetails.py` | Steam 商店 appdetails API |
| 2 | `fetch_storebrowse.py` | IStoreBrowseService（灰色 API 救援） |
| 3 | `fetch_steamspy.py` | SteamSpy 玩家/标签数据 |
| 4 | `fetch_playercount.py` | 实时在线人数 |
| 5 | `fetch_appreviews.py` | 评测（含分页翻页） |
| 6 | `fetch_news.py` | **GetNewsForApp**（多 feed 新闻索引） |
| 7 | `fetch_news_details.py` | **ajaxgeteventdetails**（多语言新闻详情，27 语言） |
| 8 | `fetch_reviewhistogram.py` | 评测柱状图 |
| 9 | `fetch_achievements_global.py` | 全球成就完成率 |
| 10 | `fetch_market_items.py` | 市场物品列表 |
| 11 | `fetch_pcgamingwiki.py` | PCGamingWiki 元数据 |
| 12 | `fetch_protondb.py` | ProtonDB Linux 兼容度 |
| 13 | `tag_map.json` | Steam 标签 ID → 名称映射表 |
| 14 | `steam_archiver.py` | **主编排器**：调度多源脚本 + 生成 `main.json` 清单 + 渲染 `index.html` 离线主页 |
| 15 | `archiver_gui.py` | GUI 图形化启动器（含"打开 HTML"一键跳转） |
| 16 | `add_toc.py` | **目录导航注入器**：给已生成的归档页一键注入悬浮 TOC（可拖拽 / 收起 / 滚动高亮） |
| 17 | `__main__.py` | **命令行一键归档入口**：`python . <AppID>` 跑完整 13 步流水线（与 GUI 同构） |

---

## 📚 参考文档

- **[docs/api-cheatsheet.md](docs/api-cheatsheet.md)** —— Steam 新闻类 API 参数手册（实测盖章版）
  - GetNewsForApp 完整参数表（含 3 个社区文档陷阱）
  - ajaxgeteventdetails 4 参数 + Steam 内部 27 语言编号表
  - 源码逆向铁证（BATCH_SIZE = 30）

---

## 🚀 快速开始

```bash
# 单源示例：拉 CS2 新闻索引
python fetch_news.py 730

# 拉 CS2 中文新闻详情（403 条 · 84% 中文覆盖 · 约 43s）
python fetch_news_details.py 730

# 拉繁体中文（产物：news_details_tw.json）
python fetch_news_details.py 730 --lang tchinese

# 拉日文（产物：news_details_ja.json）
python fetch_news_details.py 730 --lang japanese

# GUI 启动器（推荐）
python archiver_gui.py

# 命令行一键归档（与 GUI 同构的 13 步流水线）
python . 1035760                        # 在项目目录内执行
python . 1035760 --list                 # 列出所有步骤
python . 1035760 --only appdetails,html # 只跑指定步骤
python . 1035760 --skip market          # 跳过指定步骤
python . 1035760 --dry-run              # 只打印将执行的命令

# 给已有的归档页注入「目录导航」（无需重新生成页面）
python add_toc.py out                 # 批量全部
python add_toc.py out/4513880        # 单个目录
```

---

## ⚙️ 环境要求

- **Python 3.8+** — 全部脚本实测兼容（纯标准库，零第三方依赖）
- **Windows / macOS / Linux** — GUI 使用 Tkinter（随 Python 自带）
- 需要能访问 Steam 网络；评论等模块支持自动探测本地代理（也可 `--proxy` 显式指定）

---

## 📦 输出结构

```
{appid}/
├── appdetails.json
├── storebrowse.json
├── news.json                     # 英文/多 feed 索引
├── news_details_zh.json          # 简中详情（--lang schinese）
├── news_details_tw.json          # 繁中详情（--lang tchinese）
├── news_details_ja.json          # 日文详情（--lang japanese）
├── reviews.json
├── playercount.json
├── ... (其余 8 个源产物)
├── main.json                     # 归档清单（steam_archiver.py 生成，防 Ctrl+C 丢数据 + 断点续传）
├── index.html                    # 🎯 离线主页（双击即可完全离线浏览，不联网）
└── assets/                       # 图片/视频/清单等资源
```

每个中间文件都是独立 JSON，含 `_meta` 元信息（fetched_at、api 版本、脚本来源）。

**终极产物**：`index.html` + `assets/` + `main.json` —— 由 `steam_archiver.py` 编排生成，
双击 HTML 即可完全离线浏览游戏商店信息，不需要任何网络连接。

---

## 🔬 项目特色

1. **灰色 API 救援** —— 官方 API 拉不到的字段，用 IStoreBrowseService 补齐
2. **归档完整度自监测** —— `steam_archiver.py` 主编排器会检查每个源的产物完整度
3. **社区文档避坑清单** —— 见 `docs/api-cheatsheet.md` 第三章，记录了 4 个网上流传但实际错误的参数信息
4. **源码级参数逆向** —— 通过 DevTools 抓包 + JS bundle grep 拿到 Steam 前端源码铁证

---

## 🛡️ 工程规范

- 所有 `fetch_xxx.py` 遵循统一模板：`argparse` + `MAX_RETRIES` + `TIMEOUT` + `USER_AGENT`
- 网络错误自动重试 3 次
- `http_get` 封装统一处理超时 + 重试 + 编码
- 输出层用 JSON 集合容器，附带 `_meta` 元信息
- 无第三方依赖（纯标准库）

---

## 📄 License

[MIT](LICENSE)

---

*最后更新：2026-11-09*
