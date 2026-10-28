# Glean 拾取 · 接口与存储契约（冻结版 v1）

> 本文件是前后端、各平台适配器共同遵守的唯一契约。改动必须先改本文件。
> 目标：粘一个链接 → TikHub 拉全内容 → 网站里好看地看 → 规整落到本地给 agent 读。

## 0. 定位

- **只做一件事**：把互联网上的任意内容链接拉下来，可视化看，规整存本地。
- 不做分析、不做改写、不做发布。原始内容不改写、不纠错。
- 纯本地运行，不依赖任何 pip 包（仅 Python 3.11+ 标准库 + ffmpeg/yt-dlp 命令行）。
- 端口 `8899`（`127.0.0.1`）。

## 1. 目录结构

```
<GLEAN_ROOT>/
├── SPEC.md                  # 本文件
├── README.md
├── 启动.sh / 停止.sh
├── server/
│   ├── main.py              # HTTP 服务 + 路由 + SSE
│   ├── tikhub.py            # TikHub 客户端（环境变量取 key / 代理自探测 / 重试 / UA）
│   ├── resolver.py          # URL → 资源识别（平台 + 类型 + ID）
│   ├── adapters/            # 每平台一个模块，统一出口 fetch()
│   ├── media.py             # 下载媒体 + 字幕/转写
│   ├── store.py             # 本地库读写（原子写 + 索引）
│   └── jobs.py              # 任务队列 + 进度事件
├── web/                     # index.html / style.css / app.js
└── library/                 # ★ 本地库（agent 直接读这里）
```

## 2. 本地库布局（agent 可读性优先）

```
library/
├── AGENTS.md                # 告诉任何 agent 这个库怎么读（自解释）
├── index.json               # 全库索引（轻量，只放列表需要的字段）
├── index.md                 # 人/agent 都能读的目录表
└── items/<item_id>/
    ├── item.json            # 归一化完整记录（见 §3）
    ├── content.md           # ★ 正文 / 字幕原文（agent 主要读这个）
    ├── transcript.srt       # 标准 SRT 字幕（有 segments 时才生成）
    ├── raw/                 # 原始 API 返回，保留出处，便于追溯与重放
    │   └── <endpoint>.json
    └── media/               # 下载下来的图片 / 视频 / 封面
```

- `item_id` 规则：`<platform>-<kind>-<稳定业务ID>`，例：`douyin-video-7587425737197145390`。
  博主主页用 `douyin-profile-<sec_user_id>`。同一链接重复抓取 = 覆盖同目录，不产生重复。
- **视频文件必须是浏览器能播的编码**（H.264 / VP8 / VP9 / AV1）。抖音高码率档位普遍是
  H.265，Chrome 放不出画面（只有声音）。选流优先 H.264，下完用
  `media.probe_video_codec` 复核，不是就写进 `warnings`。
- **`items/` 目录是唯一真源。** 主页清单里"这条在不在本地"是按磁盘实况算的
  （`store._sync_child_locals`），不信任存下来的标记——存下来的会失真：条目删了标记还在、
  从别处单独抓的又不会补标记。按磁盘算，界面就永远不会说谎。
- 库位置可用环境变量 `GLEAN_LIBRARY_DIR` 覆盖。**跑测试必须用它指向临时目录**，
  否则测试数据会混进用户的真实库。
- 所有写入**原子**（先写 `.tmp` 再 `os.replace`）。
- `raw/` 保留原样，用于日后重放，不允许被裁剪。

### 2.1 `content.md` 格式（冻结）

```markdown
---
id: douyin-video-7587425737197145390
platform: douyin
platform_label: 抖音
kind: video
title: AI零代码开发 10分钟搞定小程序
author: 某某
author_id: MS4wLjABAAAA...
url: https://www.douyin.com/video/7587425737197145390
created_at: 2026-05-01T12:00:00+08:00
fetched_at: 2026-09-20T17:20:00+08:00
duration: 93.534
stats: {like: 1, comment: 2, share: 3, collect: 4, play: 5}
transcript_source: volcengine
media_count: 4
---

# <title>

> 作者：<author> · <created_at> · <url>

## 正文

<desc 原文，不改写>

## 字幕原文

[00:00] ......
[00:03] ......

## 媒体

- media/cover.jpg
- media/001.jpg
```

- 没有字幕就省略「字幕原文」整节，**不写**「无字幕」这种结论以外的话；用 `transcript_source: none`。
- 博主主页类 `content.md` 是**索引表**：一行一个作品（序号 / 标题 / 时长 / 数据 / 相对路径）。

## 3. `item.json` 归一化结构（冻结）

```jsonc
{
  "id": "douyin-video-7587425737197145390",
  "platform": "douyin",              // douyin|xiaohongshu|twitter|bilibili|
                                     // weibo|zhihu|youtube|kuaishou|tiktok|instagram|wechat_mp|generic
  "platform_label": "抖音",          // 给人看的中文名
  "kind": "video",                   // video|images|text|article|profile|unknown
  "source": "library",               // library（我拿过的，默认）| favorites（收藏夹同步来的，见 4.6）
  "collections": ["健身"],           // 所属收藏夹（分栏）；没有就是空数组。只有 source=favorites 会有
  "url": "原始链接（用户粘的那条）",
  "canonical_url": "平台规范链接",
  "title": "标题",
  "text": "正文（话题标签已拆走，见 text_raw / tags）",
  "text_raw": "★ 一字未改的原始文案（只在拆过标签时出现）",
  "tags": ["写真", "汉服"],          // 话题标签（从正文里拆出来的）
  "author": {
    "name": "昵称", "handle": "@xxx 或空", "id": "平台内 ID",
    "avatar": "media/avatar.jpg 或远程 URL", "url": "主页链接",
    "followers": 0
  },
  "created_at": "ISO8601 带时区 或 空字符串",
  "duration": 93.534,                // 秒；非视频为 null
  "stats": {"like":0,"comment":0,"share":0,"collect":0,"play":0},
  "transcript": {
    "source": "platform|volcengine|local|none",
    "text": "纯文本（无时间戳，段落用 \n）",
    "segments": [{"start": 0.0, "end": 3.2, "text": "..."}]
  },
  "media": [
    {"role":"cover|content|video|audio|avatar","type":"image|video|audio",
     "path":"media/001.jpg","url":"远程原始地址","width":0,"height":0,"bytes":0}
  ],
  "children": [                      // kind=profile 时：该博主的作品（每条同时落成独立 item 目录）
    {"id":"douyin-video-...","title":"...","kind":"video","duration":93.5,
     "stats":{...},"created_at":"...","url":"...","local":true}
  ],
  "profile": {"aweme_count": 237, "signature": "简介", "verified": "认证信息"},
  "fetched_at": "ISO8601",
  "source_api": ["/api/v1/douyin/app/v3/fetch_one_video"],
  "warnings": ["人话写的降级说明，例如：无音轨，未转写"]
}
```

约定：
- **取不到就留空**（`""` / `null` / `[]`），绝不用猜测值填充。
  **标题尤其如此**：不许写「（这条没有文案）」「作品 7683…」这种占位或编造标题。
- **标题也要去标签**：标题和正文一样要用 `textfmt.split_tags` 拆干净
  （原串仍留在 `text_raw`）。否则列表页会糊成一片 `#AI#智能体#openclaw`，
  根本认不出是哪条（踩过）。**拆完标题为空就留空 `""`**，由界面显示平台名兜底，
  不要把标签串或「（这条没有文案）」塞回标题。
- **正文与标签分家**（`server/textfmt.py`）：社交文案里 `#标签` 糊在正文里没法看，
  所以拆开存。原文一个字都不丢——拆过的话原串原样进 `text_raw`。
  三种东西必须区分：话题标签（拆走）、章节标记 `#00:11[时刻]# 标题`（是内容，留在正文）、
  正文里的井号 `第#3章` / `C#`（不能误伤）。长文（`kind=article`）不拆。
- `warnings` 记录降级路径，不伪装成功。
- **`stats.play`（播放量）要单独补。** 作品详情 / 列表接口的 `statistics.play_count` **恒为 0**
  （实测 49 份原始返回全是 0，是接口不给，不是没取）；只有批量统计接口
  `/api/v1/douyin/app/v3/fetch_multi_video_statistics` 给真实数字。
  所以：单条抓取后补一次，**批量抓取由收尾一次批量补**（逐条补是 N 次调用，白花钱），
  抓主页时对整页 children 一次补齐。取不到就保持 0，不因此判失败。
  评论数 `stats.comment` 是**条数**；评论正文本身不在本库范围内（Glean 不抓评论区）。
- `media[].path` 一律用相对 `items/<id>/` 的路径；远程地址同时留在 `url`。
- **图片必须是浏览器能渲染的格式**（`jpg` / `png` / `webp` / `gif`）。
  小红书图文会返回 **HEIC**，Chrome 白块（踩过：5 张图全看不了）。
  下载后嗅探到 HEIC/HEIF/BMP 就用 macOS 自带 `sips` 转成 JPG 落盘，
  `media[].path` 写转换后的名字，原格式记进 `media[].source_format`（可追溯，
  **不写 warnings**——转成功是好事，不该每张图刷一条黄条）。
  **转不动才**写 `warnings`，因为那时图真的看不了。和视频的 H.264 要求是同一条道理。

## 4. HTTP API（冻结）

Base: `http://127.0.0.1:8899`

| 方法 | 路径 | 说明 |
|---|---|---|
| GET  | `/api/status` | 健康 + 凭证 + 网络状态 |
| GET  | `/api/accounts` | 我的账号看板（含每个号的作品数/已拿数） |
| GET  | `/api/accounts/<id>/works` | 只读返回本地主页清单与本地作者作品合并结果，不访问平台 |
| POST | `/api/accounts` | 登记账号 `{url, name?}`，平台自动识别 |
| PATCH | `/api/accounts/<id>` | 改名 / 换链接 |
| DELETE | `/api/accounts/<id>` | 移除账号（已拿的内容不动） |
| POST | `/api/accounts/<id>/sync` | 点「更新作品」= 拉一份最新清单（预览） |
| POST | `/api/resolve` | 只识别，不抓取 |
| POST | `/api/jobs` | 建抓取任务 |
| GET  | `/api/jobs` | 最近 50 个任务（列表页 / 刷新后找回在跑的） |
| GET  | `/api/jobs/<job_id>` | 任务状态（轮询） |
| GET  | `/api/jobs/<job_id>/events` | SSE 进度流 |
| GET  | `/api/items` | 库列表（筛选/搜索/分页） |
| GET  | `/api/avatar?url=<https-url>` | 只代理抖音/小红书头像域名（内存缓存） |
| GET  | `/api/items/<item_id>` | 完整 item.json |
| GET  | `/api/items/<item_id>/content` | content.md 原文 |
| DELETE | `/api/items/<item_id>` | 删条目 |
| GET  | `/api/media/<item_id>/<file>` | 媒体字节（支持 Range，视频拖动要用） |
| GET  | `/api/config` / `POST /api/config` | 读写设置 |
| POST | `/api/recheck` | 重探网络/代理 |

`GET /api/accounts/<id>/works` 返回 `{ok, account, profile_id, fetched_at, works}`。
作品优先取主页条目的 `children`，再按作者 ID 前缀关系补入本地独立作品；本地条目标记 `local: true`。
博主页打开时先展示此缓存，缓存不存在或超过 12 小时才自动后台刷新；用户也可手动刷新。
刷新通过预览任务运行，分页快照需同步更新 `children_count`，使前端显示已取条数。

### 4.1 `GET /api/status`

```json
{
  "ok": true,
  "version": "1.0",
  "library_root": "<GLEAN_ROOT>/library",
  "key_present": true,
  "key_source": "env",
  "proxy": {"url": "http://127.0.0.1:7897", "ok": true, "checked_at": "...", "latency_ms": 420},
  "network": {"tikhub_ok": true, "direct_ok": false, "message": "直连被阻断，已走本机代理 7897"},
  "counts": {"items": 12, "by_platform": {"douyin": 5}},
  "ffmpeg": true, "ytdlp": true, "volcengine_key": true
}
```

### 4.2 `POST /api/resolve`

请求 `{"url": "..."}` → 200

```json
{
  "ok": true,
  "input_url": "https://v.douyin.com/xxx/",
  "resolved_url": "https://www.douyin.com/video/7587425737197145390",
  "platform": "douyin",
  "platform_label": "抖音",
  "kind": "video",
  "ids": {"aweme_id": "7587425737197145390"},
  "needs_expand": false,
  "hint": "抖音单条视频",
  "already_in_library": {"id": "douyin-video-...", "fetched_at": "..."}
}
```

失败 → 400 `{"ok": false, "error": "人话错误原因"}`。

### 4.3 `POST /api/jobs`

**单条抓取**（视频 / 图文 / 文章）：

```json
{
  "url": "...",
  "options": {
    "download_media": true,     // 下载图片/视频到本地
    "transcribe": true,         // 视频转写
    "keep_video": true,          // 是否保留下载后的视频；缺省读设置页默认值
    "limit": 20,                // 主页类最多抓多少条，0=全部
    "expand_children": false,   // 默认 false：主页只出清单，不逐条抓（见 4.2.1）
    "source": "library",        // 默认 library（我拿过的）；收藏夹同步传 favorites（见 4.6）
    "collections": ["健身"]     // 所属收藏夹（分栏）；收藏夹同步按夹名传（见 4.6）
  }
}
```

→ 202 `{"ok": true, "job_id": "j_xxx", "mode": "single", "resolve": { ...同 /api/resolve... }}`

**批量抓取**（用户在博主预览里挑中的那几条）：

```json
{
  "items": [ { "url": "...", "id": "...", "title": "...", "kind": "video",
               "duration": 52.0, "cover": "...", "stats": {} } ],
  "profile_id": "xiaohongshu-profile-xxxx",
  "options": { "download_media": true, "transcribe": true, "keep_video": true }
}
```

→ 202 `{"ok": true, "job_id": "j_xxx", "mode": "batch", "count": 3}`

- `items` 直接用预览返回的 `children_preview` 元素即可，多余字段会被忽略。
- 单条失败不中断整批，最后汇总 `children_ok` / `children_failed`，并写一条 warning。
- 全部结束后回写主页条目里对应作品的 `local: true`（`store.mark_children_local`）。

### 4.2.1 `POST /api/preview` —— 博主链接的预览（主流程第一步）

博主主页链接**默认不走 4.3 的单条抓取**，而是先预览：只花一次列表接口的钱，
拿到作品清单（封面 / 标题 / 时长 / 数据），让用户挑，再决定抓哪些。

请求 `{"url": "<博主主页链接>"}` → 202 `{"ok":true,"job_id":"j_xxx","mode":"preview","resolve":{...}}`

预览任务**不下载媒体、不转写、不逐条抓**。结果从 `job.partial` 里长出来：

```jsonc
"partial": {
  "id": "xiaohongshu-profile-5926956382ec391304e64116",
  "platform": "xiaohongshu", "platform_label": "小红书", "kind": "profile",
  "title": "楚豆泥的小红书主页",
  "author": { "name": "楚豆泥", "handle": "265885032", "followers": 10000 },
  "profile": { "signature": "...", "ip_location": "浙江", "note_count": 107, "following_count": 1000 },
  "children_count": 20,
  "children_preview": [
    { "id": "...", "title": "...", "kind": "video|images|text", "duration": 52.0,
      "stats": {...}, "created_at": "...", "url": "...", "cover": "https://..." }
  ]
}
```

预览完成后主页条目**会入库**（`item["preview"] = true`），所以作品清单在本地也留了一份索引。

### 4.2.2 `GET /api/thumb` —— 预览封面代理

```
/api/thumb?url=<encodeURIComponent(封面地址)>&ref=<平台链接，作为 Referer>
```

平台 CDN 校验 Referer，浏览器直连会 403，所以由服务端转发。
只允许 http/https，并拒绝内网地址（本地服务不该成为 SSRF 跳板）。

### 4.2.3 批次任务的 `partial`

批次任务的 `partial` 除了常规字段，还**原样带上逐条状态**：

```jsonc
"partial": {
  "kind": "profile", "title": "已选 3 条作品", "children_count": 3,
  "children": [
    { "id": "...", "title": "...", "kind": "video", "cover": "...",
      "local": false, "batch_state": "running|done|failed", "error": "", "item_id": "" }
  ]
}
```

`batch_state` 一条条推进，前端据此做"逐条打勾"的观感。
（注意区分：`children_preview` 是主页预览的摘要，`children` 只在批次任务里出现。）

### 4.4 任务对象

```json
{
  "id": "j_xxx", "url": "...", "state": "queued|running|done|failed|partial",
  "platform": "douyin", "kind": "video", "title": "...",
  "created_at": "...", "started_at": "...", "finished_at": "...",
  "progress": {"stage": "transcribe", "label": "正在转写语音", "percent": 62,
               "detail": "第 2/3 步"},
  "stages": [{"key":"resolve","label":"识别链接","state":"done"},
             {"key":"fetch","label":"调用接口","state":"done"},
             {"key":"media","label":"下载媒体","state":"running"},
             {"key":"transcribe","label":"转写语音","state":"pending"},
             {"key":"store","label":"写入本地库","state":"pending"}],
  "item_id": "douyin-video-...",
  "warnings": [], "error": null
}
```

- `state=partial`：主体拿到了但有降级（如转写失败、部分图片下载失败），前端照常展示并显示警告。
- 视频请求 `transcribe: true` 却没有有效文字时，必须保留 `transcript.note` 并写入 `warnings`，不能无警告 `done`。包括引擎 `off`、缺 Key、下载/提取音频失败、返回空文字；没有说话仍为 `partial`。显式 `transcribe: false` 不属于转写失败。
- 批量和展开主页把子条警告（带作品 ID）汇总到任务 `warnings`，子条保留 `item_id`、`has_transcript`、`warnings`。`children_ok` 计入库数，不代表字幕完整数。字幕验收要求每条 `content.md` 有非空「## 字幕原文」，不能只验主页文件。
- SSE 事件：`event: progress` + 上面的任务 JSON；结束时 `event: done` / `event: error`。

### 4.2.4 我的账号（看板）

账号存在 `~/.glean/accounts.json`（`server/accounts.py`），**不写死在代码里**——
用户加号换号不用改程序。每条只记平台、主页链接、显示名、上次更新时间；
作品数和"已拿几条"都**实时从本地库算**，账号记录里不重复存，免得两边不一致。

`POST /api/accounts/<id>/sync` 就是「更新作品」：复用预览流程（`mode=preview`），
只是默认 limit 抬到 50。前端拿到清单后**默认只勾"还没拿过的"**——
"更新"要的是增量，不该无差别全抓。

认"哪些作品是我的"用 `item.author.id` 和账号的 sec_user_id 比，**按前缀关系比而不是全等**：
同一个 sec_uid 在不同来源长度会不一样（实测主页链接里 72 字符、作品接口返回 76 字符），
要求全等会一条都认不出来。

### 4.2.5 引用路径（单条 + 批量）

每条内容的页面底部只有**一个**「引用原文」（`content.md` 绝对路径）。
**不再单独给「引用字幕」按钮**——`content.md` 里元数据、正文、标签、字幕原文全都有，
给 agent 一份就够；多摆一个按钮只是多一个要选的（批量引用本来就是这么做的）。
`transcript.srt` 仍然照常生成，需要它的 agent 直接读 `content.md` 底部的相对路径即可。

「我拿过的」支持多选：勾若干条 → 底部操作条一次性复制多行路径，可直接粘给 agent。

博主主页的作品清单也支持多选（`状态.多选场景 = 'works'`）。这里除了批量引用，
还能把**没拿的**直接提交抓取。判断"有没有字幕"必须用后端算好的
`children[].has_transcript`（按磁盘上的 transcript.srt），**不能拿 `local` 近似**——
纯音乐视频拿了也没字幕，近似会复制出指向不存在文件的路径。
复制的始终是**绝对路径**，前端从 `/api/status` 的 `library_root` 拼出来，
不硬编码目录。

### 4.2.6 从内容跳到博主主页（反向导航）

抓完一条内容，常常想顺手看看**这个博主还有别的什么**。所以：

- 每条**非主页**内容的作者行上有「看 TA 的主页 ›」，用 `item.author.url`
  （没有就用 `author.id` 拼 `https://www.douyin.com/user/<id>`）。
- 点了直接进**挑选页**（`POST /api/preview` + 清单勾选），**不落中间页**——
  用户要的是"看清单 → 勾 → 拉"，不是先看一份博主资料（走 4.2.1 的既有流程）。
- 清单里**已经拿过的必须标出来**（`children[].local`，按磁盘算），
  这样"再拉几条别的"不会重复抓、也不会漏。这是这条链路能不能用的关键。
- 「返回」回到刚才那条内容（靠 history，`打开预览` 走 pushState）。

### 4.3.1 一次粘多条：全部丢给后端排队

**不要在客户端串行 await。** 三个原因（都踩过）：

1. 转写走本机模型时要几分钟一条，三条就十几分钟，中间用户什么都看不到；
2. 前端为了显示清单会改动单条渲染要用的 DOM，渲染函数一抛异常就把进度跟踪打断，
   界面看起来"卡住不动"；
3. 刷新之后客户端循环没了，剩下的链接**根本没提交**。

正确做法：`POST /api/jobs` 提交 N 次，`JobManager`（2 个 worker）自己排队，
前端只轮询 `GET /api/jobs` 画列表。这样**关页面、刷新、换设备都不影响**，任务在后端跑。

前端约定：
- 有 `running` / `queued` 的任务时，进入「正在拿」页面并轮询（1.5 秒）。
- **启动时**先调 `GET /api/jobs`：真有在跑的才接管页面；
  只是"刚刚跑完"的**不要抢首页**（用户可能正要粘下一条），给一句可点的提示即可。
- 全部结束后停止轮询，避免空转。

### 4.4.1 `partial`：抓取过程中的内容快照（渐进展示用）

任务对象里的 `partial` 字段在**抓取过程中持续更新**，每次进度推进都会通过 SSE 广播出去，
让前端能"随进度一段段长出来"，而不是等整条抓完才有东西看。

```jsonc
"partial": {
  "id": "...", "platform": "douyin", "platform_label": "抖音", "kind": "video",
  "title": "...", "text": "...",
  "author": { "name": "", "handle": "", "avatar": "", "url": "", "followers": 0 },
  "created_at": "...", "duration": 0,
  "stats": { "like": 0, "comment": 0, "share": 0, "collect": 0, "play": 0 },
  "profile": {},
  "media": [ { "role": "cover|content|video", "type": "image|video", "path": "media/001.jpg", "url": "" } ],
  "media_count": 0,
  "transcript": { "source": "none", "note": "", "segments": [], "text": "" },
  "children_count": 0,
  "children_preview": [ /* 按本次请求 limit 返回，最多 200 条 */ ],
  "warnings": []
}
```

裁剪上限（防止 SSE 负载失控）：`media` ≤ 60 条、`transcript.segments` ≤ 400 段。`children_preview` 按请求的 `limit` 返回，最多 200 条，不得在任务快照中再截成 60 条。
前端应按"字段有没有内容"决定区块何时出现，并且**只追加新增项**，不要每次快照重建整个列表。

### 4.5 `GET /api/items`

Query：`platform`、`kind`、`exclude_kind`、`source`（`library` | `favorites`，不传=全部）、`collection`（收藏夹名，见 4.6）、`q`（标题/作者/正文模糊）、`author`（作者名精确匹配）、`author_id`（作者 ID 精确匹配，优先于 `author`）、`missing_transcript=1`（仅无文字稿的视频）、`limit`(默认 50)、`offset`、`order`(默认 `fetched_at_desc`)

`author`、`author_id` 与 `missing_transcript` 在分页前过滤，因此 `total` 是过滤后的总数。

```json
{"ok": true, "total": 12, "items": [
  {"id":"...","platform":"douyin","platform_label":"抖音","kind":"video",
   "source":"library","collections":["健身"],
   "title":"...","author":"...","author_id":"...","duration":93.5,"created_at":"...","fetched_at":"...",
   "cover":"/api/media/<id>/cover.jpg","stats":{...},
   "has_transcript": true,
   "excerpt": "正文前 120 字"}
],
"by_platform": {"douyin": 12},
"author_counts": [{"id":"...","name":"...","count":4}], "author_total": 12,
"by_source": {"library": 10, "favorites": 2},
"by_collection": {"健身": 12, "未分类": 3}}
```

`authors` 是当前来源下所有非主页条目的作者名，供完整筛选列表使用。`by_source` 是按来源的计数（不受 `source` 过滤影响，两个栏目顶部数字都靠它）。
`author_counts` 按作者 ID 汇总当前来源下非主页内容数，按数量降序；`author_total` 是同范围内非主页内容总数。
`by_collection` 是当前 `source` 下的收藏夹分栏计数，键是夹名；没归到任何夹的条目统一算进
`未分类`（这个键名是前端约定，真实夹名不许叫「未分类」）。

`GET /api/items/<id>/raw` 返回条目 `raw/` 目录中保存的接口 JSON，供详情页查看原始记录。

### 4.6 两个栏目：我拿过的 / 收藏夹

- **我拿过的**（`source=library`）：用户自己粘链接、或在「我的账号」里勾选抓下来的内容。
- **收藏夹**（`source=favorites`）：抖音收藏夹同步脚本（`抖音收藏夹同步/同步.py`）自动拿下来的收藏，
  分栏（`item.collections`）来自用户自己的抖音收藏夹分组名。
  它只是**来源不同**，抓取、存储、转写、字段结构和「我拿过的」完全一样，只在 `item.source` / `item.collections` 上区分。
- 没有 `source` 字段的老条目一律视为 `library`（向后兼容，不需要迁移）。
- 没有 `collections` 字段的条目视为未分类（同样不需要迁移）。

### 4.7 收藏夹的「更新」按钮

`GET /api/favorites/status`

```json
{"ok": true, "running": false, "message": "发现 12 条新收藏，已交给拾取抓取",
 "new_count": 12, "started_at": "2026-10-08T16:12:03", "finished_at": "2026-10-08T16:15:20",
 "configured": true, "pid": 25768}
```

- 进度来自同步脚本自己写的 `上次同步.json`（脚本所在目录）。
- `running` 不能只看那个文件：脚本崩了会留下 `running: true` 的假象，
  必须再用 `kill(pid, 0)` 确认进程还活着。
- `configured=false` 表示找不到脚本（配置键 `favorites_sync_script`）。

`POST /api/favorites/sync`

→ 202 `{"ok": true, "started": true, "message": "已开始更新，新内容会自动出现"}`

- 后台 `Popen` 起一次同步脚本（`sys.executable` + `start_new_session=True`），
  输出追加到脚本目录的 `手动更新.log`；接口立刻返回，进度靠 status 轮询。
- 已经在跑时返回 200 `{"ok": false, "running": true, ...}`，**不重复起**——
  两个浏览器抢同一个登录 profile 会互相打架。
- 找不到脚本 → 404。

## 5. 资源识别规则

`resolver.py` 必须先展开短链（跟随 302，最多 3 跳），再判定：

| 平台 | 单条 | 主页/批量 |
|---|---|---|
| douyin | `/video/<id>`、`modal_id=` query、`v.douyin.com` 短链 | `/user/<sec_uid>` |
| xiaohongshu | `/explore/<note_id>`、`/discovery/item/<id>`、`xhslink.com` | `/user/profile/<user_id>` |
| twitter/x | `/status/<id>` | `x.com/<handle>`（无 status） |
| bilibili | `/video/BVxxxx`、`av<id>` | `space.bilibili.com/<uid>` |
| weibo | `/detail/<id>`、`/status/<id>` | `weibo.com/u/<uid>`、`/n/<name>` |
| zhihu | `answer/<id>`、`p/<pin>`、`zhuanlan.zhihu.com/p/<id>` | `/people/<token>` |
| youtube | `watch?v=`、`youtu.be/<id>`、`/shorts/<id>` | `/@handle`、`/channel/<id>` |
| kuaishou | `/short-video/<id>`、`v.kuaishou.com` | `/profile/<id>` |
| tiktok | `/@user/video/<id>` | `/@user` |
| instagram | `/p/<code>`、`/reel/<code>` | `/<username>` |
| wechat_mp | `mp.weixin.qq.com/s?...` | （不做） |

识别不出来 → `platform=generic`，走 yt-dlp 兜底（能拿标题/字幕就拿），并如实说明。

## 6. 凭证与网络（重要）

- **三个 Key（`tikhub_api_key` / `volcengine_api_key` / `aliyun_api_key`）都可以在设置面板里填**，
  通过 `POST /api/config` 写进 `~/.glean/config.json`（权限 600）。
  **任何接口都不回显 key 明文**（连 `GET /api/config` 的返回里也要 pop 掉）。
  留空 = 这个 key 不设置（自动回落下一档），**不是清空**。
- Key 优先级（三个 Key 一致）：环境变量 → `~/.glean/config.json`（设置面板填的）。
  - TikHub：`TIKHUB_API_KEY`
  - 阿里云百炼：`DASHSCOPE_API_KEY`
  - 火山转写：`VOLCENGINE_ASR_API_KEY`
- 火山转写 Key：环境变量 `VOLCENGINE_ASR_API_KEY`。

## 7. 转写策略（如实记录用了哪个）

引擎由 `transcribe_engine` 决定：`volcengine` | `aliyun` | `local` | `off`。
`transcript.source` 如实标注实际用的是哪条路。

**火山**（`volcengine`）：一次 HTTP，base64 音频内嵌在请求体里，同步返回。
状态码在 **HTTP 响应头** `X-Api-Status-Code`（不在 body），`20000000` 才是成功；
`20000003` = 没有有效语音，属于内容问题不是故障。

**阿里云百炼**（`aliyun`）：走官方**临时上传 + 异步转写**，**不分段**：

```
① GET  /api/v1/uploads?action=getPolicy&model=<模型>   拿上传凭证（免费临时存储）
② POST <upload_host>（multipart）                       → oss:// 临时 URL（48h 有效）
③ POST /api/v1/services/audio/asr/transcription         提交任务
      必须带 X-DashScope-OssResourceResolve: enable     少这个头解析不了 oss://
④ GET  /api/v1/tasks/<task_id>                          轮询
⑤ 下载 transcription_url → transcripts[].sentences[].begin_time/end_time（毫秒）
```

硬约束（文档明写）：**上传时指定的模型必须和调用时完全一致**；上传与调用必须同一主账号；
模型必须用异步 filetrans 那类（同步模型如 `qwen-audio-3.1-asr-flash` 提交到异步端点会报
`url error`）。默认 `qwen-audio-3.1-asr-flash-filetrans`，支持 12 小时 / 2GB。

**本机**（`local`）：faster-whisper，慢但不花钱。火山/阿里云**故障**且
`transcribe_fallback_local` 为真时自动兜底，**并在 note 里写明"这次用的是本机模型"**，
不是静默降级。都没有 → `"none"` + `warnings` 写明原因。

### 7.1 「没有有效语音」是内容问题，不是故障

火山 `20000003`、阿里云 `ASR_RESPONSE_HAVE_NO_WORDS`、本机 whisper 空结果、
无音轨 —— 这四种都属于**这条视频本来就没说话**（纯画面 / 纯音乐），不是接口坏了。

- 直接 `transcript.source = "none"` **收工，不许再降级去跑本机模型**——
  为一条没人说话的视频等几分钟本机转写毫无意义（踩过：4 条视频白等）。
- `warnings` 写「这条视频没有说话」，**禁止写「转写失败」**，
  免得用户以为出故障了去重抓一遍（重抓还是没语音，白花钱）。
- 只有**接口故障**（网络断、鉴权失败、余额不足、超时）才允许降级兜底，
  也才允许 `warnings` 里出现「失败」字样。
