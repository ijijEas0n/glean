---
name: glean
description: >-
  用户发来抖音、小红书等链接或 App 分享文案，要把视频、文案和字幕抓回来做引用或分析时使用。
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
