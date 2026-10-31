# 拾取 · Glean

> **一句话部署**：把这个仓库链接 https://github.com/ijijEas0n/glean 发给你的 Agent（Grok Bot / Claude Code / Codex 等），说「按 AGENTS.md 帮我部署 glean」。

**正常抓取并且转写**，需要两样东西：TikHub Key，再加上一种转写方式——火山引擎「大模型录音文件极速版」、阿里云百炼 DashScope（**北京**地域），或本机 faster-whisper。三者有一种即可。Key 自己写入 `.env` 或网页设置，**不要贴进聊天**。

把互联网上的内容「拿下来」：粘一个链接 → 抓到全部内容（含视频语音转写）→ 本地网页里查看 → 规整地存进本地库，给 AI Agent 直接读。

**只做一件事**：抓取与入库。不做分析、不做改写、不做发布——分析交给 Agent，回填（如飞书文档）交给 Agent 的其他工具。

当前重点支持 **抖音、小红书**（单条作品 + 博主主页）；推特/B 站/YouTube 等适配器代码保留，但未作完整验收。

服务进程起来，还不等于端到端通过。验收标准见下面的「怎样才算通过」。

## 架构

```mermaid
flowchart LR
  A[链接 / App 分享文案] --> B[resolver<br>抽 URL · 展开短链 · 判平台和类型]
  B --> C[jobs 串行队列<br>SSE 推进度]
  C --> D[adapters<br>TikHub 第三方接口取详情/主页/播放量]
  D --> E[media<br>多线路下载视频 · ffmpeg 抽音频]
  E --> F[转写<br>平台字幕 > 火山 > 阿里云 > 本机 faster-whisper]
  F --> G[store 本地库<br>content.md · transcript.srt · item.json · raw/ · media/]
  G --> H[Agent 读 content.md 分析<br>再用别的工具回填文档]
```

关键设计：
- **链接解析防脏输入**：从分享文案里抽 URL，跟随 302 展开短链（`v.douyin.com`、`xhslink.com`），`user/<id>?modal_id=<作品id>` 识别为单条作品。
- **不自己维护 cookie / 登录态**：数据走 [TikHub](https://tikhub.io) 聚合接口（付费）。本机直连被拦时会自动探测本地代理端口。
- **播放量**：抖音详情接口的 `play_count` 恒为 0，改用批量统计接口（一次 50 条）收尾补齐；取不到不影响整次抓取。
- **转写不静默降级**：实际用了哪个引擎写进 `transcript.source` 与 `warnings`。
- **为 Agent 设计输出**：每条一个目录，`content.md` 带 YAML 头 + 正文 + 标签 + 字幕；`raw/` 保留接口原始返回以便溯源；原文不改写。
- **零 pip 依赖**：仅 Python 3.11+ 标准库 + `ffmpeg` / `yt-dlp` 命令行。

## 配置

仓库里不存任何密钥。公开包也不会附带 Key。

写到哪里（两处都行，**环境变量优先**）：

1. 复制 `.env.example` 为 `.env`，用编辑器自己粘贴后保存。`./启动.sh` 会加载它。`.env` 已在 `.gitignore`。
2. 服务起来之后，在网页「设置」里填。写入 `~/.glean/config.json`（权限 600，目录可用 `GLEAN_CONFIG_DIR` 改）。

**不要把 Key 贴进聊天、Issue 或 PR。** Agent 只检查某一行是否非空，不读、不打印值。`/api/status` 里的 `key_present: true` 只表示读到了 TikHub 配置，不表示鉴权或余额已经通过。

| 名称 | 用途 | 何时必需 | 写到哪里 |
|---|---|---|---|
| `TIKHUB_API_KEY` | 抓取抖音、小红书作品 / 主页 / 播放量 | **抓取必需** | `.env`，或网页设置 |
| `VOLCENGINE_ASR_API_KEY` | 火山「大模型录音文件极速版」（默认引擎，快） | 选择火山转写时必需 | `.env`，或网页设置 |
| `DASHSCOPE_API_KEY` | 阿里云百炼异步文件转写。必须是**华北 2（北京）**的 Key | 选择阿里云转写时必需 | `.env`，或网页设置 |
| 本机 faster-whisper | 不花钱的转写，较慢。不需要云转写 Key | 选择本机转写时要装好 | 可选 `GLEAN_WHISPER_PYTHON`；默认路径会自动发现 |
| `GLEAN_WHISPER_MODEL` | 本机模型：`base` / `small` / `medium`，默认 `small` | 可选 | `.env` |
| `GLEAN_LIBRARY_DIR` | 本地库目录，默认 `./library` | 可选 | `.env` |
| `GLEAN_CONFIG_DIR` | 配置目录，默认 `~/.glean` | 可选 | `.env` |
| `GLEAN_PYTHON` | 启动脚本使用的 Python，默认 `python3`（需 3.11+） | 可选 | `.env` |

云转写的两个 Key **按所选引擎填一个即可**，不是「两个都要」，也不是「填了任意一个就等于转写可用」。引擎在网页上切换，或写在配置里的 `transcribe_engine`（`volcengine` | `aliyun` | `local` | `off`）。

只抓文案、不转写：把引擎设为 `off`，可以不配转写 Key。这样不能算「有人声字幕」通过。

本机 whisper：

```bash
python3 -m venv ~/.glean/faster-whisper && ~/.glean/faster-whisper/bin/pip install faster-whisper
```

放在这个默认位置可自动发现，否则设 `GLEAN_WHISPER_PYTHON`。

## 怎样才算通过

| 层级 | 意思 |
|---|---|
| 服务启动 | `GET /api/status` 的 `ok` 为 `true`。只说明进程在听 `127.0.0.1:8899` |
| 端到端通过 | 打开转写后再抓一条**有人声**的视频，`content.md` 里有「## 字幕原文」并且这一节有正文 |

`GET /api/jobs/<job_id>` 的任务字段是 **`job.state`**（不是 `status`）：

- `done`：结束，且没有 warnings。
- `partial`：结束，但带有 warnings。文件可能已经入库。
- `failed`：没有完成。

请求转写的视频若没有有效文字，任务会带原因并以 `partial` 结束（包括引擎 `off`、缺 Key、下载或转写失败）。主动选择 `transcribe: false` 不作字幕验收。网页提醒和详情会展示原因。

主页批量把子条警告汇总到任务 `warnings`，子条带 `item_id`、`has_transcript`；入库成功数不代表字幕成功数。验字幕需按子条 `item_id` 逐条检查 `content.md`，主页清单本身没有视频字幕。

**纯 BGM、没有人声**时，没有字幕正文、任务停在 `partial`，是正常结果，不要当成故障重抓。它也不能用来声称「转写验收通过」。

有人声样例若 `content.md` 没有字幕正文，验收不通过，即使任务不是 `failed`。更细的对照：`item.json` 里 `transcript.text` 非空、`segments` 非空、`transcript.source` 不是 `none`。

## 已验证有人声样例

下面两条在本轮验收里确认有人声。平台删除、权限或接口变化后可能失效。真抓会消耗 TikHub 和转写额度。

- 抖音：https://www.douyin.com/video/7401819680706448675
- 小红书：https://www.xiaohongshu.com/explore/6aad453b00000000110339cb

用它们做端到端验收时，通过条件是对应 `library/items/<item_id>/content.md` 里有非空的字幕正文，而不是仅仅服务能打开。

## 安装

依赖：Python 3.11+、`ffmpeg`、`yt-dlp`。启动脚本另外需要 `curl` 和 `lsof`（确认服务、检查端口）。缺 `curl` 或 `lsof` 时 `./启动.sh` 会直接说明并退出。

### macOS

```bash
brew install python ffmpeg yt-dlp      # curl、lsof 一般随系统自带
git clone https://github.com/ijijEas0n/glean.git && cd glean
cp .env.example .env                   # 自己填 Key，不要贴到聊天里
./启动.sh                               # 手动后台启动 → http://127.0.0.1:8899
./启动.sh --foreground                  # 前台调试
./停止.sh
```

开机自启、崩溃拉起：`./安装自启.sh`（**macOS launchd**，标识 `com.example.glean`，可在脚本里改），卸载 `./卸载自启.sh`。这套模板不能在 Linux 上当自启用。

### Linux

以 Ubuntu 24.04 / Debian 12、Python 3.11+ 为例。用 `pipx` 装 `yt-dlp`，避免往系统 Python 里 `pip install --user`。

```bash
sudo apt-get update
sudo apt-get install -y python3 ffmpeg curl lsof pipx
pipx install yt-dlp
export PATH="$HOME/.local/bin:$PATH"

python3 --version
ffmpeg -version
yt-dlp --version

git clone https://github.com/ijijEas0n/glean.git && cd glean
cp .env.example .env                   # 自己填 Key，不要贴到聊天里
./启动.sh                               # 手动后台启动 → http://127.0.0.1:8899
./停止.sh
```

Linux 上的验收范围是手动执行 `./启动.sh` / `./启动.sh --foreground`。仓库没有提供 systemd 单元；若要开机自启，需要自己写 user service 去跑 `./启动.sh --foreground`，这不在当前验收里。

## 本地库结构

```
library/
├── AGENTS.md / index.json / index.md
└── items/<platform-kind-id>/
    ├── content.md      # ★ Agent 优先读；字幕在「## 字幕原文」
    ├── transcript.srt  # 有字幕分段才有
    ├── item.json       # 结构化完整记录
    ├── raw/            # 接口原始返回
    └── media/          # 封面、图片、视频
```

`GLEAN_LIBRARY_DIR` 可以改掉这个目录。

## 接入 Agent

服务是本机 HTTP API。`127.0.0.1` 指**执行命令的那台机器**。云端 Agent 必须在跑 glean 的机器上执行，不能把自己的 localhost 当成用户的电脑。

| 接口 | 作用 |
|---|---|
| `GET /api/update` | 只读检查公开仓新版，提醒用户，不自动升级 |
| `GET /api/status` | 健康检查。`ok: true` 只表示服务已启动 |
| `POST /api/resolve` `{"url": ...}` | 识别链接 + 查重（返回 `already_in_library`） |
| `POST /api/jobs` `{"url": ..., "options": {"download_media": true, "transcribe": true}}` | 提交抓取，返回 `job_id` |
| `GET /api/jobs/<job_id>` | 轮询 `job.state`：`queued` / `running` / `done` / `partial` / `failed` |
| `POST /api/preview` `{"url": <主页>, "options": {"limit": 50}}` | 博主主页先预览作品清单 |
| `POST /api/viral/preview` `{"platforms":["douyin","xiaohongshu"],"keywords":["AI"],"publish_time":7,"douyin_sources":["search","lowfan"],"max_pages":2,"tier":3,"limit":20}` | 低粉爆款候选筛选；202 返回 job_id，结果读 job.viral.candidates |
| `POST /api/viral/save` `{"items":[{"platform":"douyin","id":"作品ID"}],"options":{"transcribe":false}}` | 只保存用户勾选的作品，复用批量 jobs；已有作品跳过下载 |

`examples/SKILL.md` 是模板，里面有 `<GLEAN_HOST>`、`<GLEAN_ROOT>` 等占位符，**不能原样复制给 Agent**。在仓库根目录生成可用副本：

```bash
python3 tools/render_skill.py \
  --host http://127.0.0.1:8899 \
  --root "$PWD" \
  --output ./generated/glean/SKILL.md
```

Agent 和 glean 不在同一台机器时加上 `--machine-label <机器名>`。库目录不是 `<仓库>/library` 时加上 `--library-dir`。已有输出默认不覆盖，确认要替换再加 `--force`。生成结果里不应再出现这些占位符。放到哪个 Skill 目录见 [`AGENTS.md`](AGENTS.md) 第 9 步。

抓下来后，让 Agent 读 `library/items/<id>/content.md` 做分析；需要回填到飞书等文档时，用对应平台的 CLI / Skill 单独完成（本项目不包含）。

## 检查更新与升级

`curl -s --max-time 5 http://127.0.0.1:8899/api/update` 返回当前版、最新版、`notes` 等（程序自用；`release_url` 不必展示给用户）。查不到更新不影响抓取。
版本号以根目录 `VERSION` 为准；修改后重启服务生效。

有新版时，Agent 用中文极简提醒，例如「✨ 拾取有新版本啦：`1.0.0` → `1.1.0`，主要新增：…。要我帮你更新吗？」（带版本号与少量 emoji；无 notes 则只说有新版 + 版本号），不要贴发布页链接。必须先征得用户同意，才可在 `<GLEAN_ROOT>` 执行 `git pull`，再按 [AGENTS.md](AGENTS.md) 重启服务（手动启动使用 `./停止.sh`、`./启动.sh`；已安装 launchd 自启的按其说明操作）。不会自动升级。

## 已移除

开源版不包含任何需要登录态 / cookie 或客户端解密的抓取能力。

## 免责声明

仅供个人学习与研究。请遵守各平台服务条款与著作权法，抓取内容勿用于再分发或商业用途。

## License

MIT © 2026 迅仔

## 低粉爆款候选

在「收集 → 低粉爆款」填关键词、选平台，拖松紧后查询。排除词、时间窗、来源、翻页、高级门槛、排序分组与 R 预算在默认折叠的「高级设置」。预览会消耗 TikHub 额度，不下载媒体、不转写、不写库；仍须勾选入库。这是候选筛选，非流量预测。

每条只显示一个结论与一句中文原因。计数也只有三档，互斥且相加等于本批条数：

| 结论 | 规则（从上到下优先） |
|---|---|
| 值得跟 | 用户手动标记「值得跟」，优先覆盖后面的规则；或四门槛全过且 R 为「明显爆」（原始 R≥10） |
| 跳过 | 用户标「不相关」；否则命中排除词/作者屏蔽；否则开启带货筛选且有疑似带货依据；否则有已知门槛失败 |
| 可参考 | 四门槛通过但未算 R、R 偏爆/平常/样本不足/基线为零；或仅缺数据待核，没有已知门槛失败 |

人工标记优先于排除/屏蔽，排除/屏蔽优先于疑似带货，最后看数据。未知粉丝同时互动不足时归跳过；仅缺粉则可参考。默认值得跟→可参考→跳过，跳过折叠，可展开复查和撤销反馈。组内有 R 的优先，R 降序，再按互动降序；高级设置可换排序或细分平台。M、R 数值/评级、门槛和带货/排除/收藏缺失依据都收进默认折叠的「详情」。

跳过项默认不能点选或全选入库，包括不相关、带货、未过门槛；可以在该卡详情点击「仍要入库（手动强制）」单独放行，可撤销，刷新或重新查询不保留强制状态。已入库项始终不可重复勾选。每次最多入库100条。

两平台互动 I 均为赞+收藏；M=I/max(粉丝,1)。抖音收藏缺失/非法按0，并注明「收藏缺失」，不触发额外查询；点赞缺失仍待核。小红书仍需赞和收藏都有数据。搜索离线样例已确认抖音 `statistics.collect_count`；兼容扁平 `collect_count` / `collect_cnt` 等别名。旧低粉榜3份离线样本共71条仅有 `like_cnt`，没有收藏计数，`favorite_id/is_favorite` 不作计数；作者近作实际收藏字段本轮仍待真实响应验证。详情会保留缺失依据。搜索与榜单不保证全量覆盖或语义相关。

五档默认宽松；第二档改称「较严」。四门槛为粉≤上限、I≥下限、M≥下限、0≤天数≤有效天数，全部满足才过。缺粉/点赞/日期不伪装0或近期。抖音最低互动较旧点赞口径统一上调20%，粗略抵消加入收藏后的自然抬升，保持原档位间距；这是保守产品默认值，未经真实样本校准，不代表统计最佳值。小红书不变。

| 档位 | 抖音：粉上限 / 最低赞+藏 / 最低M / 本地天数 | 小红书：粉上限 / 最低赞+藏 / 最低M / 本地天数 |
|---|---|---|
| 0 最严 | 30000 / 24000 / 5 / 7 | 5000 / 2000 / 2 / 7 |
| 1 较严 | 50000 / 12000 / 3 / 14 | 10000 / 1000 / 1 / 14 |
| 2 适中 | 50000 / 3600 / 1 / 14 | 10000 / 500 / 0.5 / 14 |
| 3 宽松（默认） | 50000 / 1200 / 0.3 / 14 | 10000 / 300 / 0.1 / 14 |
| 4 最松 | 100000 / 360 / 0.1 / 30 | 30000 / 100 / 0.05 / 30 |

拖条或改门槛只免费重筛本批 `pool`，不会拉源、补粉或自动算 R。手改门槛后显示「自定义」，再次拖条覆盖两平台数字；换关键词/平台要重新查询。无候选不能断言关键词没有好内容。

默认先按关键词发现，再核验低粉：抖音 POST `/api/v1/douyin/search/fetch_video_search_v2`，最多点赞 `sort_type="1"`；勾选的低粉榜 `/api/v1/douyin/billboard/fetch_hot_total_low_fan_list` 作为补充，固定 `date_window=168`，每词仅 1 页。页面可单独勾选「抖音搜索 / 抖音低粉榜」，默认都开，API 对应 `douyin_sources:["search","lowfan"]`。旧配置显式 `douyin.source` 仍兼容单来源，新请求的 `douyin_sources` 优先。旧 `source_window_hours` 仍接受 24–168 的整数，但不再改变榜单固定窗口。

「发布时间范围」对两平台生效：`publish_time` 默认 7，可选 180（近半年）或 0（不限）。抖音传同名字符串参数，小红书 `search_notes` 显式传 `time_filter` 为「一周内 / 半年内 / 不限」。接口可能返回旧作品，因此按真实发布日期再次复核；高级门槛 `max_age_days` 与所选发布时间范围取较严格值（选不限仍保留高级门槛）。下方拖条仅重筛本批，不扩大搜索范围或补查作者。

`max_pages` 默认 2，可设 1–3；搜索逐平台逐关键词翻页，抖音使用响应的 cursor/search_id/backtrace，小红书沿用 search_id/search_session_id。空页或 `has_more=false` 停止；抖音游标缺失/不前进时停止并标覆盖不全。关键词支持 1–20 个，去重，每词最多 80 字，页面可逗号或换行粘贴。先覆盖所有来源/关键词首屏，再搜索深翻，最后按作者缓存补粉；最多 300 条扫描、24 次逻辑调用（重试另计），触顶标 `coverage.incomplete`。

抖音搜索自带正粉丝数直接使用，0/缺失标待核，只为互动和日期可能过线的作者补查 `handler_user_profile`；小红书继续以作者接口核粉，补查受同一预算约束。余额不足会显示「TikHub 余额不足，请充值」并停止后续查询。

跨来源、跨关键词按平台+作品 ID 去重。每条卡片标来源，结果保留 `source`、`metrics_source`、`fetched_at`；指标冲突保留首个来源，后续来源快照存入 `raw.alternatives`，不相加。`coverage` 返回各词页数、分来源页数 `pages_by_source`、调用数和发布时间范围；`calls` 含补粉。搜索结果来自平台搜索接口，不保证全量覆盖或语义相关；搜索与榜单指标也可能口径不同。最近一批完成的预览及筛选设置保存于当前浏览器 localStorage，刷新可恢复；仅保存快照，不会自动更新互动、粉丝或查询时计算的天数。浏览器禁用存储或空间不足时会提示，清除浏览器数据会丢失预览。


排除词按标题、描述、话题做包含匹配，支持中英文逗号或空格分隔。疑似带货来自本次响应已有的营销词、商品/广告字段；可关闭筛选，依据仍留在详情，可能误判「下载教程」「广告分析」。不补详情确认。「不相关 / 值得跟」与作者屏蔽可撤销，按平台+ID保存在仓库 `data/viral-feedback.json`（Git忽略）；无作者ID时不提供屏蔽。`GET/POST /api/viral/feedback` 只读写本地反馈。排除、排序、分组、反馈和恢复预览新增 **0 次 TikHub 调用**；多浏览器共用后端反馈，各自保存预览，多标签页反馈刷新同步。

「算 R」或「给所有手动标记值得跟的算 R」先免费估算，确认后才查询：`R = 本条赞+收藏 / 作者最近最多20条其它作品赞+收藏中位数`。只计算当前批次内人工标记的作品，不会自动追算系统判为值得跟的卡。样本去重、排除本条，少于5条不评级；中位数0不替换分母。抖音缺收藏按0且注明；缺点赞跳过该历史样本，小红书缺赞或藏均跳过。R≥10明显爆、3≤R<10偏爆、R<3平常，评级用未舍入值，展示保留2位。R、M和样本/基线/获取时间位于详情。R不是增长速度，也非跨平台统一热度。

R 使用现有主页作品端点：抖音 `GET /api/v1/douyin/app/v3/fetch_user_post_videos`（sec_user_id/count/max_cursor），小红书 `GET /api/v1/xiaohongshu/app_v2/get_user_posted_notes`（user_id/cursor）。每位未缓存作者最多1–3页，默认2页；预计上限=未缓存作者数×页数，同作者多条只取一次。独立预算默认且最多20次/批，超预算整批不发起；达到21条有效作品或没有下一页会提前结束。R专用请求不自动重试、不向TikHub做代理探测，402/限流/未知结构停止后续作者并显示中文原因，实际调用数含失败尝试；网络未通需在设置检查代理后手动重试。

互动口径升级后，旧抖音点赞版 R 缓存不再用于新口径；重新点击估算并确认后才查询，不会自动花额度。小红书原缓存仍可复用。旧浏览器预览会恢复作品和设置，从赞/藏重算 I/M，清除旧抖音 R；非自定义档同步新默认数值。

历史缓存位于 `data/viral-r.json`，按平台:作者ID保存获取时间和近作，24小时内复用为0次调用；复用先前少页数的缓存也不会自动补页。结果随最近预览保存在localStorage。当前互动来自预览快照，历史来自查询快照，可能不同步；只比较本次主页返回的近作，置顶按日期重排，缺日期提示待核。反馈文件只存作品ID，批量计算会用当前预览补作者，未在本批或缺作者ID的逐项列出；数字抖音UID不能代替sec_uid，不额外付费换ID。

Agent 按需使用 `POST /api/viral/r/estimate`，传 `{items:当前预览pool, worth_only:true, pages:2, budget:20}`（单条算R传所选条目、worth_only:false）。此步仅访问本地文件，返回预计调用及缺失项。用户确认后才 `POST /api/viral/r` 传返回的 `plan_id`；计划10分钟有效且只能执行一次，返回results/errors/calls/uncomputed。API为同步执行，批量时客户端须放宽等待时间。仅有估算不算已计算，错误和未完成作品不能当成功。


Agent：先请求 `/api/viral/preview`，每5秒轮询 `/api/jobs/<job_id>` 的 `job.state`，终态读 `job.viral.candidates`；展示平台、标题、作者、粉丝、互动、倍数和日期给用户，等用户选择后才调 `/api/viral/save`。
保存默认关闭转写，可显式传 `options.transcribe=true`，并继续通过原有 job 查看入库进度。全已存在时 `job_id=null`，直接使用 `existing[].item_id`。

门槛可在 preview 的 `thresholds.<平台>` 临时覆盖，持久配置用 `/api/config` 的 `viral` 对象（部分对象按默认值深合并；再次保存 viral 对象会替换前次自定义对象）。`GLEAN_VIRAL_ENABLED=0` 可关闭查询入口的后端能力。
完整配置、返回字段、端点来源和待实测限制见 [SPEC.md](SPEC.md#427-低粉爆款候选筛选)。解析和分页已用 2026-10-29 真实响应的脱敏离线 fixture 回归；这不代表当前线上权限、余额或完整搜索链路已测活。

默认档可用 `POST /api/config` 保存 `{"viral":{"tier":3}}`（viral 对象整体替换，请保留已有自定义项）；仅保存配置，不发起查询。preview 支持 `tier:0–4` 和分平台 `thresholds` 覆盖。
