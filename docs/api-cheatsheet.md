# Steam 新闻类 API 参数手册（实测盖章版）

> 本手册记录 Steam-Offline-Archiver 项目在实战中通过抓包 + 源码逆向 + 参数枚举实测**验证过**的两个新闻端点参数行为。
> 每一行结论都有铁证来源；社区文档里的错误信息也在这里明确标记，避免后人踩坑。
>
> 最后更新：2026-08-17
> 相关脚本：`fetch_news.py`（源 6）、`fetch_news_details.py`（源 15）

---

## 一、GetNewsForApp（ISteamNews · 官方 Web API）

```
GET https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/
```

- **认证**：无需 API Key
- **格式**：JSON / XML / VDF
- **用途**：拉取某 AppID 的新闻/公告索引（多 feed 混合）

### 参数完整表

| 参数 | 类型 | 必传 | 状态 | 说明 |
|---|:---:|:---:|:---:|---|
| `appid` | uint32 | ✅ | ✅ | 游戏 AppID |
| `count` | uint32 | ❌ | ✅ | 返回条数上限，默认 20，实测最多约 100 |
| `maxlength` | uint32 | ❌ | ✅ 实测 | 截断 `contents` 字段，超长会加 `...` 后缀 |
| `enddate` | uint32 | ❌ | ✅ 实测 | Unix 时间戳，返回该时间**之前**的公告（翻页神器） |
| `feeds` | string | ❌ | ✅ | 逗号分隔的 feed 名，例：`steam_community_announcements` |
| `tags` | string | ❌ | ✅ 实测 | 逗号分隔标签，**默认 AND 逻辑**（与常规 REST 惯例相反） |
| `format` | string | ❌ | ✅ | `json` / `xml` / `vdf` |
| ~~`excludedtags`~~ | string | ❌ | ❌ **实测无效** | 社区文档 jes.re 误传，服务端不认 |
| ~~`match_all_tags`~~ | bool | ❌ | ❌ **实测无效** | 同上误传，默认就是 AND，此参数无效 |
| ⚠️ `l` / `lang` | string | ❌ | ⚠️ 部分无效 | 对 `steam_community_announcements` feed 完全无效，只对少数第三方 feed 有效 |

### 已发现的 tags 词表

```
patchnotes                         更新日志（占公告大头）
workshop                           创意工坊相关
hide_store                         商店页不显示
hide_library_overview              库总览不显示
hide_library_detail                库详情不显示
mod_reviewed                       Mod 已审核
mod_require_rereview               Mod 需重审
ModAct_<id1>_<id2>_<n>             Mod 活动运行时 ID（非固定词）
```

### 关键行为铁证

**1. `tags=A,B` 是 AND 不是 OR**
- 想拿"A 或 B"：需分两次请求再合并
- 想拿"A 且 B"：直接 `tags=A,B` 即可

**2. `enddate` 翻页机制**
- 传 Unix 时间戳 → 返回该时间之前的公告
- 结合末条 `date` 实现无限向前翻页
- `fetch_news.py` 当前翻页逻辑就是靠它

**3. `l=schinese` 为什么拉不到中文**
- `steam_community_announcements` feed 只存 clan 原始语言，不做自动本地化
- 想要中文正文必须走 `ajaxgeteventdetails`（见第二节）

### 铁证来源

- Better Steam Web API Docs（`steamwebapi.azurewebsites.net`）
- TF2 Wiki（`wiki.teamfortress.com/wiki/WebAPI/GetNewsForApp`）
- python-valve 库文档
- 项目自实测（2026-08-17，AppID=730 CS2）

---

## 二、ajaxgeteventdetails（Store SPA · 私有内部 API）

```
GET https://store.steampowered.com/events/ajaxgeteventdetails/
```

- **认证**：无需登录
- **格式**：JSON
- **用途**：批量拉取指定 clan 公告的**多语言完整详情**（含中文/日文/俄文等 27 门语言）

### 参数完整表（源码盖章）

| 参数 | 类型 | 必传 | 说明 |
|---|:---:|:---:|---|
| `clanid_list` | uint64 | ✅ | Clan ID（不是 AppID），CS2 是 `3381077` |
| `uniqueid_list` | uint64 | ✅ | 公告的 gid（可以多条，逗号分隔或多次传参） |
| `lang_list` | string | ✅ | 格式 `{目标 code}_{回退 code}`，例：`6_0` = 简中优先，英文回退 |
| `origin` | string | ❌ | 通常填 `https://store.steampowered.com` |

**只支持这 4 个参数**，源码里 `InternalLoadPartnerEventList` 函数体铁证 —— 没有 `count` / `feeds` / `cc` / `since` / `event_type_filter` 等。

### Steam 内部语言编号完整表（实测验证）

| code | 语言 | code | 语言 | code | 语言 |
|:---:|---|:---:|---|:---:|---|
| 0 | english | 10 | **japanese** | 20 | romanian |
| 2 | french | 11 | portuguese | 21 | turkish |
| 3 | italian | 12 | polish | 22 | brazilian |
| 4 | koreana | 13 | danish | 23 | bulgarian |
| 5 | spanish | 14 | dutch | 24 | greek |
| 6 | **schinese** | 15 | finnish | 26 | ukrainian |
| 7 | **tchinese** | 16 | norwegian | 27 | latam |
| 8 | **russian** | 17 | swedish | 28 | vietnamese |
| 9 | thai | 18 | hungarian | | |
| | | 19 | czech | | |

⚠️ **注意坑**：网上流传的映射表常把 `japanese=8`，实测这是错的！`8=russian`、`10=japanese`。

### 关键行为铁证

**1. 批次上限 30 条（源码铁证）**
```javascript
// chunk~beb94d585.js: InternalLoadPartnerEventList
if (this.m_rgQueuedEventsClanIDs.length >= 30) { ... }
```
- Steam 前端就是 30 条/批
- `fetch_news_details.py` 已按此设置 `BATCH_SIZE = 30`

**2. 顺路发现的三个金矿端点**
| 端点 | 用途 | 权限 |
|---|---|---|
| `ajaxgeteventdetailsforedit` | 编辑模式拉详情 | 需管理员权限 |
| `ajaxgetadjacentpartnerevents` | 拉相邻公告 | 无 |
| `ajaxgetpartnerevents` | 拉 clan 全部公告 | 无（可能替代 HTML 索引） |

**3. clanid 探测方法**
- 访问 `store.steampowered.com/news/app/{appid}?updates=true`
- HTML 首屏含 `documents` JSON 数组
- 正则匹配即可拿到完整索引（gid + clanid + event_type + start_time）
- CS2 一次拿到 403 条完整索引

### 铁证来源

- 抓包（DevTools Network 面板）
- Steam 前端源码：`chunk~beb94d585.js`（`InternalLoadPartnerEventList` 函数）
- 项目自实测（2026-08-17，AppID=730 CS2，403 条全量归档 43s 跑通）

---

## 三、社区文档陷阱清单（避坑）

| 参数 | 出处 | 实际状态 |
|---|---|---|
| `excludedtags` | jes.re Steam API List | ❌ 服务端不认，无过滤效果 |
| `match_all_tags` | jes.re Steam API List | ❌ 服务端不认，默认就是 AND |
| `l=schinese` 能拉中文公告 | Stack Overflow / 多篇教程 | ⚠️ 对 `steam_community_announcements` feed 无效 |
| `japanese=8` | 多个 GitHub 项目 | ❌ 错误，实际 `10=japanese`、`8=russian` |
| 用 `gid` 桥接 news.json ↔ news_details.json | 直觉猜测 | ❌ 两个数据源 gid 编号体系完全不同，全 0 匹配 |

---

## 四、跨数据源公告桥接铁证（重要！）

### 问题
`news.json`（GetNewsForApp）与 `news_details_{lang}.json`（ajaxgeteventdetails）**返回同一条公告时用完全不同的 gid**：

| 数据源 | 字段 | 样本值 | 编号体系 |
|---|---|---|---|
| news.json | `it.gid` | `1784506359212881` | RSS 站点 gid（15-19位）|
| news_details | `ev.gid` | `706654285854670869` | Clan event gid（18位）|
| news_details | `ann.gid` | `706654285854670870` | Announcement gid（ev.gid + 1）|
| news_details | `ev.news_post_gid` | `"0"` | ❌ 全是 0，无效 |

### 唯一可靠的桥接主键

```
news.json          news_details_{lang}.json
  it.date_ts  ═══  ev.announcement_body.posttime
```

**实测（2190220 东方弹幕神乐 50 条 · 100% 匹配）**：
- `date_ts` vs `rtime32_start_time`: 0/50 ❌（start_time 有偏移）
- `date_ts` vs `rtime_created`: 0/50 ❌（created 时间不同）
- **`date_ts` vs `ann.posttime`: 50/50 ✅ 完美**

### 参考实现

见 `steam_archiver.py` 的 `render_news_section()`：

```python
# 建索引时用 posttime
zh_by_ts[int(ann["posttime"])] = ev

# 查询时用 date_ts
zh_ev = zh_by_ts.get(int(it["date_ts"]))
```

---

## 五、参考实现

- **fetch_news.py**（源 6）：GetNewsForApp 索引拉取，支持 `enddate` 翻页 + `feeds` 过滤 + 断点续传（读旧 → gid 命中停 → merge 保存）
- **fetch_news_details.py**（源 7）：HTML 首屏索引 + ajaxgeteventdetails 分批（30/批）拉多语言详情
- **steam_archiver.py**：展示层用 `posttime` 时间戳桥接两个数据源，实现中文覆盖层
- 中间文件：`{appid}/news.json`（英文/多 feed 索引）+ `{appid}/news_details_{lang_tag}.json`（多语言详情，平级独立；`lang_tag` 由 `LANG_TAG` 映射，如 schinese→zh、tchinese→tw、japanese→ja）
