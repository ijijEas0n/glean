---
name: glean
description: >-
  用户发来抖音、小红书等链接或 App 分享文案，要把视频、文案和字幕抓回来做引用或分析时使用。
  通过本机 glean 服务入库，返回 content.md 路径并基于原文分析。
---
# 前提
- glean 跑在 `<GLEAN_HOST>`（默认 `http://127.0.0.1:8899`），库在 `<GLEAN_ROOT>/library/items/<id>/`。
- 若 Agent 远程执行命令，在 `<MACHINE_LABEL>` 这台机器上跑；机器离线就请用户上线，不要绕路。
- 不要绕过服务直接调第三方接口；一个链接只抓一次。

# 步骤
1. 健康检查：`curl -s --max-time 5 <GLEAN_HOST>/api/status` 要 `ok:true`。连不上（macOS）：`launchctl kickstart -k gui/$(id -u)/com.example.glean`，等 10 秒再查。
2. 查重：`curl -s -X POST <GLEAN_HOST>/api/resolve -H 'Content-Type: application/json' -d '{"url":"<链接或整段分享文案>"}'`。`already_in_library` 有 id 就直接用。
3. 提交：`curl -s -X POST <GLEAN_HOST>/api/jobs -H 'Content-Type: application/json' -d '{"url":"<链接>","options":{"download_media":true,"transcribe":true,"keep_video":true}}'`，拿 `job_id`。
4. 轮询 `curl -s <GLEAN_HOST>/api/jobs/<job_id>`，约 5 秒一次，最多约 5 分钟。返回可能含未转义控制字符：用 `grep -o '"status": *"[a-z]*"'` 或 Python `json.loads(s, strict=False)`。`done`/`partial` 算成功；`failed` 原样转告 `error`，不擅自重试。
5. 结果：`<GLEAN_ROOT>/library/items/<item_id>/content.md`；时间轴字幕 `transcript.srt`；精确字段 `item.json`。
6. 先把路径和标题发给用户，再读 content.md 分析。引用必须来自原文并注明路径；`warnings`（缺字幕、转写降级等）照实说。

# 博主主页
先 `POST /api/preview`（`{"url":"<主页>","options":{"limit":50}}`）拿作品清单让用户挑，再 `POST /api/jobs` 带 `items` 批量抓。

# 多条
各自提交，一次报完，给 AI 的格式：
```
请读这 N 条参考资料：
<GLEAN_ROOT>/library/items/<id>/content.md  # 标题
```
