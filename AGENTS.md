# AGENTS.md · 给 Agent 的 glean 部署剧本

> 读者是 AI Agent（Grok Bot / Claude Code / Codex / Cursor 等）。用户说「按 AGENTS.md 帮我部署 glean」时，按下面步骤**逐步**执行。
> 每步先做检查，再动手；每步结束告诉用户结果和下一步要做什么。

## 铁律

1. **永远不要让用户把任何 Key 贴进聊天。** 让用户自己打开 `.env` 粘贴保存；你只检查「有没有填」，不读、不打印 Key 的值（检查用 `grep -c '^TIKHUB_API_KEY=.\+' .env` 这类只回数量的命令）。
2. 只在仓库目录内写文件；装系统依赖前先告诉用户要装什么、征得同意。
3. 失败时把原始报错给用户看，对照每步的「常见失败」排查，不要静默跳过。
4. 服务只监听 `127.0.0.1:8899`，不要改成 `0.0.0.0` 暴露到公网。

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
```

缺什么装什么（先征得同意）：

| 系统 | 命令 |
|---|---|
| macOS（Homebrew） | `brew install python ffmpeg yt-dlp` |
| Debian/Ubuntu | `sudo apt install -y python3 ffmpeg` + `python3 -m pip install --user -U yt-dlp`（apt 里的 yt-dlp 往往过旧） |
| Windows | `winget install Python.Python.3.12 Gyan.FFmpeg yt-dlp.yt-dlp`；**启动脚本是 bash**，推荐在 WSL 里部署；不用 WSL 则见第 5 步的 Windows 写法 |

项目**没有 pip 依赖**（只用标准库），不需要 `pip install -r`。

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

**问用户**：要不要转写？要的话选哪个（可以配多个，主引擎失败时会按设置回退到本机模型）：

| 引擎 | 适合谁 | `.env` 变量 | `transcribe_engine` 值 |
|---|---|---|---|
| 火山引擎·大模型录音文件极速版（默认） | 要快：一次请求直接出结果 | `VOLCENGINE_ASR_API_KEY` | `volcengine` |
| 阿里云百炼 DashScope | 要句子切得整、音频很长 | `DASHSCOPE_API_KEY` | `aliyun` |
| 本机 faster-whisper | 不想花钱、能接受慢 | `GLEAN_WHISPER_PYTHON`（可选） | `local` |
| 不转写 | 只要文案和数据 | — | `off` |

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
然后**请用户自己**用编辑器打开 `<GLEAN_ROOT>/.env`，在 `TIKHUB_API_KEY=` 后面粘贴 Token，以及所选转写引擎的 Key，保存。（macOS 可以帮他执行 `open -e .env`，Linux 用 `nano .env`。）

用户说「填好了」后，只做存在性检查：
```bash
for k in TIKHUB_API_KEY VOLCENGINE_ASR_API_KEY DASHSCOPE_API_KEY; do
  printf '%s: ' "$k"; grep -q "^$k=.\+" .env && echo 已填 || echo 空
done
```
`.env` 已在 `.gitignore` 里，不会被提交。

## 第 5 步：启动服务

```bash
./启动.sh                 # 后台常驻，日志在 .runtime/glean.log
# 调试用：./启动.sh --foreground
```
`启动.sh` 会自动加载 `.env`。

**Windows（不用 WSL）**：脚本不可用，在 PowerShell 里先把 `.env` 里的变量设进环境（如 `$env:TIKHUB_API_KEY` 由用户自己设置），再运行
`python server\main.py --host 127.0.0.1 --port 8899`。

**常见失败**：端口 8899 被占（脚本会提示）；`找不到 python3`（见第 1 步）。

## 第 6 步：自检 /api/status

```bash
curl -s http://127.0.0.1:8899/api/status
```
看这几个字段（只看布尔值，不要回显任何 Key）：
- `ok: true` —— 服务在跑。
- `key_present: true` —— TikHub Token 已读到（`key_source` 为 `env` 或 `config`）。为 `false` 说明 `.env` 没填好或服务启动后才填（改完要 `./停止.sh && ./启动.sh`）。
- `volcengine_key` / `aliyun_key` —— 对应转写 Key 是否已读到。
- `tikhub_ok` / `tikhub_note` —— 首次是 `null`；在网页设置里点「检测」或抓一条后会更新，失败原因写在 note 里（多为代理问题，见第 2 步）。

## 第 7 步：跑一条示例链接

先只识别、不花钱：
```bash
curl -s -X POST http://127.0.0.1:8899/api/resolve \
  -H 'Content-Type: application/json' \
  -d '{"url":"复制这段内容后打开抖音 https://www.douyin.com/video/7300000000000000000 看看"}'
```
期望：`platform` 为 `douyin`、`kind` 为 `video`，说明分享文案里的链接被正确抽出。

**问用户**：给一条你真实想抓的抖音/小红书链接，然后提交真抓（会消耗 TikHub 额度，先确认）：
```bash
curl -s -X POST http://127.0.0.1:8899/api/jobs -H 'Content-Type: application/json' \
  -d '{"url":"<用户的链接>","options":{"download_media":true,"transcribe":true}}'
curl -s http://127.0.0.1:8899/api/jobs/<job_id>    # 每 5 秒轮询，done/partial 即成功
```
成功后结果在 `<GLEAN_ROOT>/library/items/<item_id>/content.md`。也可以让用户在浏览器打开 http://127.0.0.1:8899 直接用网页。

**常见失败**：`failed` + 网络错误 → 代理；402 → TikHub 余额不足；转写 warnings 里写「未配置…Key」→ 第 3/4 步。

## 第 8 步（可选）：开机自启

仅 macOS（launchd）：**问用户**要不要开机自启、崩溃自动拉起？
```bash
./安装自启.sh      # 标识 com.example.glean，可在脚本里改 LABEL
./卸载自启.sh      # 撤销
```
Linux 可以自己写一个 systemd user service 执行 `<GLEAN_ROOT>/启动.sh --foreground`。

## 第 9 步：把 Skill 装进 Agent

`examples/SKILL.md` 是让 Agent 以后「收到链接就自动入库」的技能说明。复制并替换占位符：

| 占位符 | 替换为 |
|---|---|
| `<GLEAN_HOST>` | `http://127.0.0.1:8899` |
| `<GLEAN_ROOT>` | 第 0 步记下的仓库绝对路径 |
| `<MACHINE_LABEL>` | 跑 glean 的那台电脑的名字（Agent 在本机运行时可删掉这一行） |

放到对应位置（以各 Agent 当前文档为准）：
- Claude Code：`~/.claude/skills/glean/SKILL.md`
- Codex：`~/.codex/skills/glean/SKILL.md`（或写进项目 `AGENTS.md`）
- Grok Bot 等云端 Agent：把替换后的内容作为自定义 Skill / 工作流导入，并确保它能在这台电脑上执行命令。

```bash
mkdir -p ~/.claude/skills/glean
sed -e "s#<GLEAN_HOST>#http://127.0.0.1:8899#g" -e "s#<GLEAN_ROOT>#$(pwd)#g" \
    examples/SKILL.md > ~/.claude/skills/glean/SKILL.md
```

## 完成

告诉用户：服务地址、库的位置、以后怎么用（「把链接丢给我，我帮你抓」），以及停止 / 重启命令：
`./停止.sh`、`./启动.sh`；装了自启的用 `launchctl kickstart -k gui/$(id -u)/com.example.glean`。
