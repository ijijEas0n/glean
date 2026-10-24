# AGENTS.md · 给 Agent 的 glean 部署剧本

> 读者是 AI Agent（Grok Bot / Claude Code / Codex / Cursor 等）。用户说「按 AGENTS.md 帮我部署 glean」时，按下面步骤**逐步**执行。
> 每步先做检查，再动手；每步结束告诉用户结果和下一步要做什么。

## 铁律

1. **永远不要让用户把任何 Key 贴进聊天。** 让用户自己打开 `.env` 粘贴保存；你只检查「有没有填」，不读、不打印 Key 的值（检查用 `grep -c '^TIKHUB_API_KEY=.\+' .env` 这类只回数量的命令）。
2. 只在仓库目录内写文件；装系统依赖前先告诉用户要装什么、征得同意。
3. 失败时把原始报错给用户看，对照每步的「常见失败」排查，不要静默跳过。
4. 服务只监听 `127.0.0.1:8899`，不要改成 `0.0.0.0` 暴露到公网。

---

## 部署前检查

先对照这张表。缺了不要跳过，也不要把「进程起来了」说成部署完成。

| 检查 | 通过标准 |
|---|---|
| 代码 | 克隆在用户指定的目录。之后的命令都在仓库根目录执行，绝对路径记为 `<GLEAN_ROOT>` |
| 命令 | `python3`（3.11+）、`ffmpeg`、`yt-dlp`、`curl`、`lsof` 都能找到 |
| TikHub | 用户已自行把 `TIKHUB_API_KEY` 写入 `.env` 或网页设置。Agent 不接收、不打印 Key |
| 转写 | 要「抓取 + 转写」时，火山极速版、DashScope 北京、本机 whisper 三者至少一种可用。只抓文案可以把 `transcribe_engine` 设为 `off` |
| 服务启动 | 第 6 步 `GET /api/status` 的 `ok` 为 `true`。只说明进程在听 `127.0.0.1:8899` |
| 端到端通过 | 第 7 步用有人声样例抓完后，`content.md` 里有非空的「## 字幕原文」。纯 BGM 没有字幕、任务为 `partial`，对那种视频是正常结果，但不能用来声称端到端通过 |

两条已验证**有人声**的样例（平台删除或权限变化后可能失效；真抓会消耗 TikHub 和转写额度，先问用户再提交）：

- 抖音：`https://www.douyin.com/video/7401819680706448675`
- 小红书：`https://www.xiaohongshu.com/explore/6aad453b00000000110339cb`

---

## 第 0 步：拿到代码

```bash
git clone https://github.com/ijijEas0n/glean.git && cd glean
```
**问用户**：想装在哪个目录？（默认当前目录下的 `glean/`）。之后所有命令都在仓库根目录执行，记下它的绝对路径，记为 `<GLEAN_ROOT>`。

## 第 1 步：检查环境

```bash
python3 --version   # 需要 3.11+
ffmpeg -version | head -1
yt-dlp --version
command -v curl
command -v lsof
```

缺什么装什么（先征得同意）：

| 系统 | 命令 |
|---|---|
| macOS（Homebrew） | `brew install python ffmpeg yt-dlp`。`curl`、`lsof` 一般随系统自带，缺失时再装 |
| Debian/Ubuntu | `sudo apt-get update && sudo apt-get install -y python3 ffmpeg curl lsof pipx`，然后 `pipx install yt-dlp`，并把 `$HOME/.local/bin` 放进 `PATH`。不要往系统 Python 里 `pip install --user` |
| Windows | `winget install Python.Python.3.12 Gyan.FFmpeg yt-dlp.yt-dlp`；**启动脚本是 bash**，推荐在 WSL 里部署；不用 WSL 则见第 5 步的 Windows 写法 |

项目**没有 pip 依赖**（只用标准库），不需要 `pip install -r`。`./启动.sh` 自己要用 `curl` 和 `lsof`，缺了会直接退出并说明原因。

**常见失败**：macOS 自带的 `python3` 太旧 → 用 Homebrew 的；之后可在 `.env` 里写 `GLEAN_PYTHON=<解释器路径>`。

## 第 2 步：TikHub API Key（必需）

glean 不自己爬平台，抖音/小红书的数据都来自 [TikHub](https://tikhub.io) 的接口。

告诉用户：
1. 打开 https://user.tikhub.io/ 注册并登录（需要验证邮箱，否则接口会返回 403）。
2. 在用户中心左侧菜单找「API Token」，创建一个 Token 并复制。（来源：官方文档 https://docs.tikhub.io 「Get API Token」一节。）
3. **计费**：按调用量付费，余额不足接口返回 402，需要在用户中心充值。具体单价和是否有新人免费额度**以官网为准**。

**问用户**：Token 拿到了吗？拿到后进入第 4 步自己写进 `.env`，不要发给我。

**常见失败**：
- 国内网络直连 `api.tikhub.io` 经常被重置 / 超时（官方文档也建议请求 >30 秒时配代理）。glean 会**自动探测本机常见代理端口**（7897 / 7890 / 1087 / 7891 / 6152 / 10808）。用户的代理不在这些端口时，在网页「更多设置」里填代理地址，或写进 `~/.glean/config.json` 的 `"proxy"`；填 `"direct"` 强制直连。
- 401：Token 无效或过期；403：邮箱未验证或 Token 权限不够；429：调用太快。

## 第 3 步：选转写引擎（把视频里说的话转成文字）

**问用户**：要不要转写？要「正常抓取 + 转写」，下面三种至少配一种。可以多配，主引擎失败时会按设置回退到本机模型：

| 引擎 | 适合谁 | `.env` 变量 | `transcribe_engine` 值 |
|---|---|---|---|
| 火山引擎·大模型录音文件极速版（默认） | 要快：一次请求直接出结果 | `VOLCENGINE_ASR_API_KEY` | `volcengine` |
| 阿里云百炼 DashScope（北京地域 Key） | 要句子切得整、音频很长 | `DASHSCOPE_API_KEY` | `aliyun` |
| 本机 faster-whisper | 不想花钱、能接受慢 | `GLEAN_WHISPER_PYTHON`（可选） | `local` |
| 不转写 | 只要文案和数据。不能当作字幕验收通过 | — | `off` |

### 3a 火山引擎（豆包语音）
- 登录火山引擎控制台 → 豆包语音 → 开通「录音文件识别」的**极速版**（资源 ID `volc.bigasr.auc_turbo`）→ 在「API Key 管理」里复制 API Key（新版控制台只需要这一个 Key）。
- 官方文档：https://docs.volcengine.com/docs/6561/1631584 （限制：单文件 ≤2 小时、≤100MB）。
- 免费额度：社区教程称开通后有免费试用时长，**具体以官网控制台为准**。

### 3b 阿里云百炼 DashScope
- 打开百炼控制台的 API Key 页面创建 Key（官方说明：https://help.aliyun.com/zh/model-studio/get-api-key ）。
- **地域注意**：glean 调用的是**华北 2（北京）**的 `dashscope.aliyuncs.com`，所以要创建**北京地域**的 Key；不同地域的 Key 不通用。
- 默认模型 `qwen-audio-3.1-asr-flash-filetrans`（异步文件转写，≤12 小时 / 2GB）。ASR 模型可能需要先在业务空间里开通权限。
- 免费额度：官方说明新人免费额度**仅限新加坡地域**，北京地域无免费额度（https://help.aliyun.com/zh/model-studio/new-free-quota ），**以官网为准**。

### 3c 本机 faster-whisper
```bash
python3 -m venv ~/.glean/faster-whisper
~/.glean/faster-whisper/bin/pip install -U faster-whisper
```
放在这个位置会被自动发现；放别处就在 `.env` 写 `GLEAN_WHISPER_PYTHON=<那个 python 的路径>`。
- 模型用 `GLEAN_WHISPER_MODEL` 选，默认 `small`。纯 CPU 建议 `small`（准确度与速度折中）；机器慢用 `base`；要更准且能等用 `medium`。首次运行会自动下载模型，需要能访问 Hugging Face（国内可能要代理）。
- Windows 下路径是 `%USERPROFILE%\.glean\faster-whisper\Scripts\python.exe`，需显式写进 `GLEAN_WHISPER_PYTHON`。

## 第 4 步：写 .env（用户自己填 Key）

```bash
cp .env.example .env
```
然后**请用户自己**用编辑器打开 `<GLEAN_ROOT>/.env`，在 `TIKHUB_API_KEY=` 后面粘贴 Token，以及所选转写引擎的 Key，保存。（macOS 可以帮他执行 `open -e .env`，Linux 用 `nano .env`。）云转写 Key 按引擎填一个即可。也可以等服务起来后在网页「设置」里填，不必两处都写。

用户说「填好了」后，只做存在性检查：
```bash
for k in TIKHUB_API_KEY VOLCENGINE_ASR_API_KEY DASHSCOPE_API_KEY; do
  printf '%s: ' "$k"; grep -q "^$k=.\+" .env && echo 已填 || echo 空
done
```
`.env` 已在 `.gitignore` 里，不会被提交。`TIKHUB_API_KEY` 为空就不能抓。转写那两行按所选引擎看：选火山则火山那行应已填，选阿里云则阿里云那行应已填，选本机 whisper 或 `off` 时两行都可以是空。

## 第 5 步：启动服务

```bash
./启动.sh                 # 后台常驻，日志在 .runtime/glean.log
# 调试用：./启动.sh --foreground
```
`启动.sh` 会自动加载 `.env`。macOS 和 Linux 都是这条手动启动命令。日志超过 5MB 时脚本会轮转，Linux 上同样生效。

**Windows（不用 WSL）**：脚本不可用，在 PowerShell 里先把 `.env` 里的变量设进环境（如 `$env:TIKHUB_API_KEY` 由用户自己设置），再运行
`python server\main.py --host 127.0.0.1 --port 8899`。

**常见失败**：端口 8899 被占（脚本会提示）；`找不到 python3`（见第 1 步）；缺少 `curl` 或 `lsof`（脚本会点名缺哪个，以及 apt / brew 怎么装）。

这一步结束时，服务**可能**已经起来。用第 6 步确认。第 6 步通过仍然不是端到端通过。

## 第 6 步：自检 /api/status（服务启动，不是端到端）

```bash
curl -s http://127.0.0.1:8899/api/status
```
看这几个字段（只看布尔值，不要回显任何 Key）：
- `ok: true` —— 服务在跑。**只代表进程在听端口。**
- `key_present: true` —— TikHub Token 已读到（`key_source` 为 `env` 或 `config`）。为 `false` 说明 `.env` 没填好或服务启动后才填（改完要 `./停止.sh && ./启动.sh`）。读到 Key 不等于鉴权或余额已通过。
- `volcengine_key` / `aliyun_key` —— 对应转写 Key 是否已读到。要做转写验收时，所选引擎这一项应为 `true`（本机 whisper 不看这两项，看本机环境是否装好）。
- `tikhub_ok` / `tikhub_note` —— 首次是 `null`；在网页设置里点「检测」或抓一条后会更新，失败原因写在 note 里（多为代理问题，见第 2 步）。

`ok: true` 之后进入第 7 步。不要在这里告诉用户「抓取和转写已经可用」。

## 第 7 步：用有人声样例做端到端验收

先只识别、不花钱。可以用分享文案，确认抽链：

```bash
curl -s -X POST http://127.0.0.1:8899/api/resolve \
  -H 'Content-Type: application/json' \
  -d '{"url":"复制这段内容后打开抖音 https://www.douyin.com/video/7401819680706448675 看看"}'
```
期望：`platform` 为 `douyin`、`kind` 为 `video`。

再识别小红书样例（仍然只是识别）：

```bash
curl -s -X POST http://127.0.0.1:8899/api/resolve \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://www.xiaohongshu.com/explore/6aad453b00000000110339cb"}'
```
期望：`platform` 为 `xiaohongshu`。

**问用户**是否同意用下面两条已验证有人声的链接做真抓（会消耗 TikHub 和转写额度）。用户要换自己的链接也可以，但用没有人声的视频不能证明转写。

- 抖音：`https://www.douyin.com/video/7401819680706448675`
- 小红书：`https://www.xiaohongshu.com/explore/6aad453b00000000110339cb`

```bash
curl -s -X POST http://127.0.0.1:8899/api/jobs -H 'Content-Type: application/json' \
  -d '{"url":"<样例或用户的链接>","options":{"download_media":true,"transcribe":true}}'
curl -s http://127.0.0.1:8899/api/jobs/<job_id>
```

每 5 秒轮询一次。响应用 Python 解析 JSON，读 **`job.state`**，不要找 `status`。`stages` 里另有每步的 `state`，正则搜 `state` 会看错。

- `queued` / `running`：继续等。转写可能超过几分钟。
- `failed`：把 `job.error` 原样给用户。网络错误多为代理（第 2 步）；402 是 TikHub 余额不足；warnings 里写「未配置…Key」回到第 3/4 步。
- `done` / `partial`：只说明任务结束。`partial` 表示有 warnings。**不要把 `done` 或 `partial` 直接写成端到端通过。**

然后打开 `<GLEAN_ROOT>/library/items/<item_id>/content.md`（若设置了 `GLEAN_LIBRARY_DIR`，库在那个目录下）：

- **端到端通过**：文件里有「## 字幕原文」，并且这一节有正文。`item.json` 里 `transcript.text` 非空、`segments` 非空、`transcript.source` 不是 `none`。
- **有人声样例没有字幕正文**：验收不通过。即使 `state` 是 `partial` 也一样。
- **用户另给的纯 BGM**：没有字幕正文、`partial`，是正常结果。告诉用户这条没有说话，不要当成转写故障重抓，也不要把它算作字幕验收通过。

也可以让用户在浏览器打开 http://127.0.0.1:8899 直接用网页。网页能打开同样只说明服务已启动。

## 第 8 步（可选）：开机自启

仓库自带的自启是 **macOS launchd**。Linux 用第 5 步的 `./启动.sh` 手动启动即可，本步不纳入 Linux 验收。

仅 macOS：**问用户**要不要开机自启、崩溃自动拉起？
```bash
./安装自启.sh      # 标识 com.example.glean，可在脚本里改 LABEL
./卸载自启.sh      # 撤销
```

Linux 若用户明确要求开机自启，可以自己写 systemd user service 执行 `<GLEAN_ROOT>/启动.sh --foreground`。仓库没有这份单元文件，写之前先说明。

## 第 9 步：生成 Skill 并装进 Agent

`examples/SKILL.md` 是模板，含 `<GLEAN_HOST>`、`<GLEAN_ROOT>`、`<GLEAN_LIBRARY>`、`<MACHINE_LABEL>`。**不要原样复制，也不要用 sed 替换**（路径里有空格或 `#` 时会坏，也去不掉远程机器那一行）。

在 `<GLEAN_ROOT>` 生成可用副本：

```bash
python3 tools/render_skill.py \
  --host http://127.0.0.1:8899 \
  --root "$PWD" \
  --output ./generated/glean/SKILL.md
```

- `127.0.0.1` 指执行命令的那台机器。Agent 就在这台机器上跑时，不要加 `--machine-label`。
- Skill 会交给另一台机器上的 Agent、由它来连这台 glean 时，加上 `--machine-label <机器名>`。
- 库目录不是 `<GLEAN_ROOT>/library` 时，加上 `--library-dir <目录>`。
- 输出已存在时命令会失败且不覆盖。确认要替换再加 `--force`。
- 工具退出码为 0 时，生成文件里不应再有上述占位符。非 0 就把 stderr 给用户看。

把生成的文件放到对应位置（以各 Agent 当前文档为准）：
- Claude Code：`~/.claude/skills/glean/SKILL.md`
- Codex：`~/.codex/skills/glean/SKILL.md`（或写进项目 `AGENTS.md`）
- Grok Bot 等云端 Agent：把生成后的内容作为自定义 Skill / 工作流导入，并确保命令是在**跑着 glean 的那台机器**上执行。

```bash
mkdir -p ~/.claude/skills/glean
cp ./generated/glean/SKILL.md ~/.claude/skills/glean/SKILL.md
```

## 完成

分开告诉用户这两件事：

1. **服务已启动**：地址 http://127.0.0.1:8899 ，停止 `./停止.sh`，再开 `./启动.sh`。装了 macOS 自启的用 `launchctl kickstart -k gui/$(id -u)/com.example.glean`。
2. **端到端是否通过**：第 7 步的 `content.md` 有没有字幕正文。通过了才可以说「把链接丢给我，我帮你抓，并带回字幕」。库的位置一并告诉用户。

只完成第 6 步时，明确说服务在跑，字幕验收还没做。
