---
name: glean
description: >-
  用户发来抖音、小红书等链接或 App 分享文案，要把视频、文案和字幕抓回来做引用或分析时使用。
  也支持抖音/小红书低粉爆款候选筛选，等用户选中后入库。
  通过本机 glean 服务入库，返回 content.md 路径并基于原文分析。
---

<!-- glean:template-only -->

> **这是模板，不能直接交给 Agent。** 先替换 `<GLEAN_HOST>`、`<GLEAN_ROOT>`、`<GLEAN_LIBRARY>`、`<MACHINE_LABEL>`，再使用生成结果。推荐：
>
> ```bash
> python3 tools/render_skill.py \
>   --host http://127.0.0.1:8899 \
>   --root "$PWD" \
>   --output ./generated/glean/SKILL.md
> ```
>
> 已有输出默认不会覆盖。Agent 和 glean 不在同一台机器时加 `--machine-label`；库不在仓库下的 `library/` 时加 `--library-dir`。

<!-- /glean:template-only -->

# 前提

- glean 服务地址是 `<GLEAN_HOST>`。仓库在 `<GLEAN_ROOT>`，本地库在 `<GLEAN_LIBRARY>/items/<id>/`。
- 地址里的 `127.0.0.1` 指**执行下面命令的那台机器**。云端 Agent 若在自己的环境里执行，连到的是自己的回环地址，不是用户的电脑。
<!-- glean:machine-note -->
- 这些命令要在 `<MACHINE_LABEL>` 上执行（glean 装在那台机器）。机器离线就请用户上线，不要改去调别的接口。
<!-- /glean:machine-note -->
- 不要绕过服务直接调第三方接口；一个链接只抓一次。
- 不要向用户索要 API Key，也不要打印 `.env` 或配置文件里的密钥。

# 服务没起来时

在 `<GLEAN_ROOT>` 手动启动。macOS、Linux、WSL 都用同一条命令：

```bash
cd "<GLEAN_ROOT>"
./启动.sh
```

等几秒后执行 `curl -s --max-time 5 <GLEAN_HOST>/api/status`，需要 `ok` 为 `true`。这只说明进程在听端口。

- 已经装过 macOS launchd 自启（标识 `com.example.glean`）时，才用 `launchctl kickstart -k gui/$(id -u)/com.example.glean`。仓库里的自启模板是 launchd，不能拿来在 Linux 上开机自启。
- Windows 不用 WSL 时没有 `./启动.sh`。在仓库目录用 PowerShell 运行 `python server\main.py --host 127.0.0.1 --port 8899`。Key 由用户自己放进环境变量，不要贴进聊天。

# 检查更新

部署验收后或会话开始抓取前，可执行（结果给自己看，不要把 JSON / 链接原样甩给用户）：

```bash
curl -s --max-time 5 <GLEAN_HOST>/api/update
```

- `update_available` 为 true：用中文极简提醒用户，带 `current` → `latest` 版本号和 1–2 个 emoji，例如「✨ 拾取有新版本啦：`1.0.0` → `1.1.0`，主要新增：…。要我帮你更新吗？」；`notes` 有内容就精简成一两句中文功能说明，没有就只说有新版 + 版本号（如「✨ 拾取有新版本啦：`1.0.0` → `1.1.0`。要我帮你更新吗？」）。**不要**贴 `release_url` / 发布页 / 网页链接。等用户明确同意后，再按 `<GLEAN_ROOT>/AGENTS.md` 升级：在仓库内 `git pull` 后重启服务。禁止偷偷升级。
- `error` 非空：一句「暂时查不到更新」带过，继续正常抓取，不当成服务故障。

# 入库

1. 查重：`curl -s -X POST <GLEAN_HOST>/api/resolve -H 'Content-Type: application/json' -d '{"url":"<链接或整段分享文案>"}'`。`already_in_library` 有 id 就直接用那条。
2. 提交：`curl -s -X POST <GLEAN_HOST>/api/jobs -H 'Content-Type: application/json' -d '{"url":"<链接>","options":{"download_media":true,"transcribe":true,"keep_video":true}}'`，从 JSON 里取 `job_id`。
3. 轮询 `curl -s <GLEAN_HOST>/api/jobs/<job_id>`，大约每 5 秒一次，直到结束。用 Python 解析 JSON，读取 **`job["state"]`**。字段名是 `state`，不是 `status`。`stages` 里也有各自的 `state`，不要用正则去搜。
   - `queued` / `running`：继续等。长视频转写会超过几分钟，不要到点就当成失败。
   - `done`：任务结束，且没有 warnings。
   - `partial`：任务结束，但带有 warnings。
   - `failed`：把 `job["error"]` 原样告诉用户，不要擅自重试。
4. `item_id` 取 `job["item_id"]`。结果在 `<GLEAN_LIBRARY>/items/<item_id>/`。

# 入库成功和字幕完整不是一回事

- **入库成功**：`<GLEAN_LIBRARY>/items/<item_id>/content.md` 已经写好（通常还有 `item.json`）。`done` 和带警告的 `partial` 都可能已经入库。
- **字幕完整**：这份 `content.md` 里有「## 字幕原文」，并且这一节有正文。`item.json` 里 `transcript.text` 非空、`segments` 非空、`transcript.source` 不是 `none`。
- 纯 BGM、没有人声时，没有字幕正文，任务停在 `partial`，是正常结果。不要当成转写故障去重抓。
- 用户要的是「话被转成文字」时，只有字幕完整才算做到。服务在跑、任务是 `partial`、或者只入库了文案，都不能说成字幕已经拿到。

# 交给用户

先把 `content.md` 的路径和标题发给用户，再读正文做分析。引用必须来自原文并注明路径。`job["warnings"]`（缺字幕、转写降级等）照实说。

# 博主主页

先 `POST <GLEAN_HOST>/api/preview`，正文 `{"url":"<主页>","options":{"limit":50}}`，拿作品清单让用户挑，再 `POST <GLEAN_HOST>/api/jobs`，带 `items` 批量抓。

批量入库时不要只验主页的 `content.md`：从 `job.partial.children[]`（展开主页可看 `job.partial.children_preview[]` 或主页 `item.json` 的 `children[]`）取子条 `item_id`，逐条读 `<GLEAN_LIBRARY>/items/<item_id>/content.md`。`local: true` 只表示已入库；`has_transcript` 是提示，字幕验收仍要求非空「## 字幕原文」。子条的 `warnings` 和 `transcript.note` 写明缺 Key、引擎关闭、下载失败或没有说话等原因，照实反馈。

# 多条

各自提交，一次报完，给 AI 的格式：

```
请读这 N 条参考资料：
<GLEAN_LIBRARY>/items/<id>/content.md  # 标题
```

# 低粉爆款候选（非预测）

用户要求找低粉爆款时，先说明候选查询使用 TikHub 额度（含另查作者粉丝），不保证爆款预测，得到用户查询授权后：

1. `POST <GLEAN_HOST>/api/viral/preview`，正文如 `{"platforms":["douyin","xiaohongshu"],"keywords":["AI"],"tier":3,"limit":20}`。可在 `thresholds.<平台>` 覆盖门槛。缺 TikHub Key 时请用户自己去网页设置，不索要/打印密钥，不绕过服务维护cookie或加浏览器自动化。
2. 返回202 job_id；每5秒 GET `<GLEAN_HOST>/api/jobs/<job_id>`，读 job.state。queued/running 继续等；failed 原样报告 job.error。done/partial 读 `job.viral.candidates` 与 warnings，这还没有入库，partial 的补粉失败/截断须说明。
3. 向用户逐条编号展示「平台、标题、作者、粉丝、互动、倍数、发布距今天数、是否已入库」。用 key=`platform:作品id` 对应选择。不能只给倍数不展示粉丝。默认宽松3档：抖音≤50000粉、≥1000点赞、≥0.3倍；小红书≤10000粉、≥300赞+藏、≥0.1倍；都限本地14天。倍数=互动/max(粉丝,1)。未知粉/日期/互动留在pool待核，不算过线；展示数是近似计数时不暗示精确。
   用户调松紧时仅用已返回pool本地重算，不能再次POST preview或补查作者；仅用户明确「重新查询」才拉源。展示严选/本档/本批参考/待核计数（有包含关系），未过线显示fail_reasons。空候选报告scanned/recognized，建议调松或看参考，不断言关键词没有好内容。
4. **等用户明确勾选/选择编号后**，才 POST `<GLEAN_HOST>/api/viral/save`，如 `{"items":[{"platform":"douyin","id":"7401819680706448675"}],"options":{"transcribe":false}}`。也可传候选原 url（保留小红书链接参数）。禁止自动全量入库/下载；转写默认关闭，需要用户要字幕时才显式true。
5. save 返回 batch job_id，沿用批次轮询与子条验收；existing 是跳过下载的本地条目，全部已存在时 job_id=null，直接用 existing[].item_id。不能把 batch 的空 item_id 当作品目录，从 job.partial.children[].item_id 取实际文件。读 `<GLEAN_LIBRARY>/items/<item_id>/content.md`，字幕仍须非空原文才算成功。

这是候选筛选，不是预测；不做账号相对中位数R、评论抓取、官方话题榜代替作品筛选、浏览器核心或自行维护登录态。每关键词只查首屏，默认最多24次逻辑调用（重试另计）和200条扫描；结果受覆盖范围限制，不能称“全平台爆款榜”。

主控是一条五档松紧滑条，默认「3 宽松」找灵感。四个数字放在折叠的高级门槛内；手改后显示「自定义」，再次拖条覆盖两平台四项数字。粉≤上限、互动≥下限、倍数≥下限、0≤日期距今≤本地天数同时满足才算本档通过，倍数=互动/max(粉丝,1)。

| 档位 | 抖音：粉上限 / 最低赞 / 最低倍数 / 本地天数 | 小红书：粉上限 / 最低赞+藏 / 最低倍数 / 本地天数 |
|---|---|---|
| 0 最严 | 30000 / 20000 / 5 / 7 | 5000 / 2000 / 2 / 7 |
| 1 严选（旧默认） | 50000 / 10000 / 3 / 14 | 10000 / 1000 / 1 / 14 |
| 2 适中 | 50000 / 3000 / 1 / 14 | 10000 / 500 / 0.5 / 14 |
| 3 宽松（新默认） | 50000 / 1000 / 0.3 / 14 | 10000 / 300 / 0.1 / 14 |
| 4 最松 | 100000 / 300 / 0.1 / 30 | 30000 / 100 / 0.05 / 30 |

拖条或改门槛只用本次 `pool` 免费重筛，不拉源、不补查作者；换关键词/平台后点「重新查询」才消耗 TikHub 额度。界面显示「严选 N · 本档 M · 本批参考 K · 待核 U」，这些计数有包含关系，不能相加。「看本批参考」展示未达标原因、待核记录，仍须明确勾选才入库。每次最多选择100条入库。

缺粉不当0，缺日期不伪装近期，缺互动也标待核；待核不算任何档通过。即使旧调用传 `allow_missing_date:true`，也不会放行日期未知项。无候选时显示扫描/可识别条数，提示向宽松拖或看参考，不能断言关键词没有好内容。本地天数调至30不扩大源覆盖：抖音默认低粉榜仍约168小时；仅各关键词首屏，最多200条扫描、24次逻辑调用（重试另计）。本批保存在页面/内存任务中，重启或刷新后须重新查询，未知粉不会随拖条自动补查。

配置默认档保存于viral.tier；preview响应pool、tier/preset、thresholds_applied、stats、coverage；预算触顶须提示覆盖不全。
