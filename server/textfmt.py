#!/usr/bin/env python3
"""把社交文案里的正文和话题标签拆开。

为什么要拆：小红书的 desc 长这样——

    #摄影造型都来自妈咪 妆我自己画的[得意]

    这几张都是手机拍哒！
    主页还有妈咪拍照的花絮[自拍R][自拍R][自拍R]#写真[话题]# #汉服[话题]##秋风辞[话题]# #有香如故[话题]# #杭州[话题]#
    #秋天穿什么[话题]##我的质感出片指南[话题]#

正文和标签糊在一起，站点里看着就是一团乱。但**原文不能丢**：
调用方负责把原始串原样存进 `text_raw`，这里只产出"拆干净的正文"和"标签列表"。

三种必须区分开的东西（都踩过）：

1. **话题标签** `#写真[话题]#` / `#抖音标签` —— 要拆出来。
2. **章节标记** `#00:11[时刻]# 章节标题` —— 这是内容（视频目录），**绝不能当标签拆掉**。
3. **正文里的井号** `第#3章`、`C#`、`F#` —— 也要原样留着。

保守原则：宁可漏拆，不可误伤正文。
"""

from __future__ import annotations

import re

# 章节标记：#00:11[时刻]# 章节标题 —— 整体是内容，先藏起来，拆完再还原
_CHAPTER = re.compile(r"#\d{1,2}:\d{2}\[时刻\]#")
_HIDE = "\x01"

# 真标签的 # 前面一定是行首、空白、另一个标签、或某个括号/表情代码之后，
# 绝不会紧跟在汉字/字母后面。不然 "第#3章" 会被误拆成标签、把正文拆坏。
_NOT_GLUED = r"(?<![0-9A-Za-z\u4e00-\u9fff])"
# 小红书显式话题：#话题[话题]#
_XHS_TAG = re.compile(_NOT_GLUED + r"#([^#\s\[\]]{1,60})\[话题\]#")
# 通用话题：整段"标签串"一次匹配（#a#b#c 这种连写很常见），再在串内按 # 切。
# 逐个小正则替换会漏掉相邻标签——前一个标签的尾字母会挡住后一个的 lookbehind。
_TAG_RUN = re.compile(_NOT_GLUED + r"((?:#[^#\s\[\]()（）【】，。！？、；：\"'“”‘’]{1,60})+)")
# 粘在正文后面的标签：形如 它#ai工作台。只认 # 后面以英文字母开头的（明显是标签，
# 中文标签也可能是 #ai工作台 这种混合），这样 "第#3章" 不会被误伤。
_GLUED_TAG = re.compile(r"(?<=[\u4e00-\u9fff\]】）)])#([A-Za-z][A-Za-z0-9_\-\u4e00-\u9fff]{0,40})")
# 没配对上的残留标记
_LEFTOVER = re.compile(r"\[话题\]#")

# 纯数字 / 纯标点 / 时间码 不当标签
_HAS_WORD = re.compile(r"[0-9A-Za-z\u4e00-\u9fff]")
_TIMECODE = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?$")


def _looks_like_tag(name: str) -> bool:
    name = name.strip()
    if not name or len(name) > 60:
        return False
    if not _HAS_WORD.search(name):
        return False
    # 全是数字的当正文（例如 "第#3章" 里的 3）；时间码同理
    if name.isdigit() or _TIMECODE.match(name):
        return False
    return True


def split_tags(text: str) -> tuple[str, list[str]]:
    """返回 (拆干净的正文字符串, 标签列表)。

    正文里的换行和空行结构尽量保留；被拆掉标签后变空的行会被删掉。
    章节标记（#00:11[时刻]# xxx）原样留在正文里。
    """
    raw = str(text or "")
    if not raw or "#" not in raw:
        return raw, []

    tags: list[str] = []

    def 收(name: str) -> None:
        name = name.strip()
        if name and name not in tags:
            tags.append(name)

    # 先把章节标记藏起来，免得它的 #00:11 被当成标签
    chapters: list[str] = []

    def 藏(m: re.Match) -> str:
        chapters.append(m.group(0))
        return f"{_HIDE}{len(chapters) - 1}{_HIDE}"

    body = _CHAPTER.sub(藏, raw)

    def 替_小红书(m: re.Match) -> str:
        if _looks_like_tag(m.group(1)):
            收(m.group(1))
            return " "
        return m.group(0)

    def 替_通用(m: re.Match) -> str:
        run = m.group(1)
        pieces = run.split("#")
        if all(_looks_like_tag(p) or not p.strip() for p in pieces):
            for piece in pieces:
                if _looks_like_tag(piece):
                    收(piece)
            return " "
        return m.group(0)

    def 替_粘连(m: re.Match) -> str:
        if _looks_like_tag(m.group(1)):
            收(m.group(1))
            return ""
        return m.group(0)

    # 顺序：小红书带 [话题]# 的 → 标签串 → 粘在正文后的标签
    body = _XHS_TAG.sub(替_小红书, body)
    body = _TAG_RUN.sub(替_通用, body)
    body = _GLUED_TAG.sub(替_粘连, body)
    body = _LEFTOVER.sub(" ", body)

    # 还原章节标记
    if chapters:
        body = re.sub(rf"{_HIDE}(\d+){_HIDE}", lambda m: chapters[int(m.group(1))], body)

    # 收拾空白：行内多余空格压成一个，去掉拆空的行，最多留一个连续空行
    lines: list[str] = []
    for line in body.split("\n"):
        cleaned = re.sub(r"[ \t\u00a0]+", " ", line).strip()
        if cleaned:
            lines.append(cleaned)
        elif lines and lines[-1] != "":
            lines.append("")
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines).strip(), tags


def strip_tags(text: str) -> str:
    return split_tags(text)[0]


def title_from(raw: str, limit: int = 80) -> str:
    """拿正文/描述当标题时用：先把话题标签拆干净，拆完空了就**返回空串**。

    不拆的话列表页会被 `#AI#智能体#openclaw` 这种标签汤糊住，认不出是哪条（踩过）。
    空串是**故意**的：调用方不许拿「（这条没有文案）」「作品 7683…」之类的占位填回来，
    SPEC §3「取不到就留空，绝不猜测」。界面自己兜底显示平台名/作者/时长。
    """
    body, _ = split_tags(str(raw or ""))
    body = re.sub(r"\s+", " ", body).strip()
    return body[:limit].strip()
