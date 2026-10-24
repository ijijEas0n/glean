#!/usr/bin/env python3
"""把 examples/SKILL.md 里的部署占位符换成这台机器的值。

只使用标准库。不读取 .env，不打印任何密钥。
默认拒绝覆盖已有输出文件。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

PLACEHOLDERS = (
    "<GLEAN_HOST>",
    "<GLEAN_ROOT>",
    "<GLEAN_LIBRARY>",
    "<MACHINE_LABEL>",
)

TEMPLATE_ONLY_RE = re.compile(
    r"<!--\s*glean:template-only\s*-->\n.*?<!--\s*/glean:template-only\s*-->\n?",
    re.DOTALL,
)
MACHINE_NOTE_RE = re.compile(
    r"<!--\s*glean:machine-note\s*-->\n(?P<body>.*?)<!--\s*/glean:machine-note\s*-->\n?",
    re.DOTALL,
)

LOCAL_MACHINE_NOTE = (
    "- 执行这些命令的 Agent 和 glean 在同一台机器上。"
    "不要把服务地址理解成另一台电脑。\n"
)


def default_template() -> Path:
    return Path(__file__).resolve().parent.parent / "examples" / "SKILL.md"


def fail(message: str) -> int:
    print(f"✗ {message}", file=sys.stderr)
    return 1


def normalize_dir(value: str) -> str:
    """保留用户写出的绝对路径；相对路径按当前目录补全。不跟随符号链接。"""
    text = value.strip()
    if not text or "\n" in text or "\r" in text:
        raise ValueError("路径不能为空，也不能包含换行")
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return os.path.abspath(path)


def normalize_host(value: str) -> str:
    host = value.strip().rstrip("/")
    if not host or any(ch.isspace() for ch in host):
        raise ValueError("服务地址不能为空，也不能包含空白")
    if not (host.startswith("http://") or host.startswith("https://")):
        raise ValueError("服务地址需要以 http:// 或 https:// 开头")
    return host


def render(template: str, *, host: str, root: str, library: str, machine_label: str) -> str:
    values = {
        "<GLEAN_HOST>": host,
        "<GLEAN_ROOT>": root,
        "<GLEAN_LIBRARY>": library,
        "<MACHINE_LABEL>": machine_label,
    }
    for placeholder, value in values.items():
        if value and any(item in value for item in PLACEHOLDERS):
            raise ValueError(f"{placeholder} 的替换值里不能再包含占位符")

    text = TEMPLATE_ONLY_RE.sub("", template)
    missing = [item for item in PLACEHOLDERS if item not in text]
    if missing:
        raise ValueError("模板去掉说明段之后仍缺少占位符：" + "、".join(missing))

    def replace_machine(match: re.Match[str]) -> str:
        if machine_label:
            return match.group("body").replace("<MACHINE_LABEL>", machine_label)
        return LOCAL_MACHINE_NOTE

    if MACHINE_NOTE_RE.search(text):
        text = MACHINE_NOTE_RE.sub(replace_machine, text)
    elif machine_label:
        text = text.replace("<MACHINE_LABEL>", machine_label)
    else:
        text = "".join(line for line in text.splitlines(keepends=True) if "<MACHINE_LABEL>" not in line)

    text = text.replace("<GLEAN_HOST>", host)
    text = text.replace("<GLEAN_ROOT>", root)
    text = text.replace("<GLEAN_LIBRARY>", library)
    leftover = [item for item in PLACEHOLDERS if item in text]
    if leftover:
        raise ValueError("生成结果仍含占位符：" + "、".join(leftover))
    text = re.sub(r"\n{3,}", "\n\n", text)
    if not text.endswith("\n"):
        text += "\n"
    return text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="生成可直接交给 Agent 的 glean Skill（替换部署占位符）。",
        epilog=(
            "示例：\n"
            "  python3 tools/render_skill.py \\\n"
            "    --host http://127.0.0.1:8899 \\\n"
            "    --root \"$PWD\" \\\n"
            "    --output ./generated/glean/SKILL.md\n"
            "\n"
            "127.0.0.1 指执行 Agent 命令的那台机器。\n"
            "Agent 与 glean 不在同一台机器时加上 --machine-label。\n"
            "库目录不是 <仓库>/library 时加上 --library-dir。\n"
            "已有输出文件默认不覆盖，确认替换再加 --force。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--host",
        default="http://127.0.0.1:8899",
        help="glean 服务地址（默认 http://127.0.0.1:8899）",
    )
    parser.add_argument("--root", required=True, help="glean 仓库的绝对路径")
    parser.add_argument(
        "--library-dir",
        default="",
        help="本地库目录。默认是 <root>/library；设置了 GLEAN_LIBRARY_DIR 时传那个路径",
    )
    parser.add_argument(
        "--machine-label",
        default="",
        help="glean 跑在另一台机器上时填写机器名。不填则写成同一台机器",
    )
    parser.add_argument("--output", required=True, help="写出的 SKILL.md 路径")
    parser.add_argument(
        "--template",
        default="",
        help="模板路径，默认是仓库里的 examples/SKILL.md",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="覆盖已有输出文件（默认拒绝）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        host = normalize_host(args.host)
        root = normalize_dir(args.root)
        library = normalize_dir(args.library_dir) if args.library_dir.strip() else str(Path(root) / "library")
    except ValueError as exc:
        return fail(str(exc))

    machine_label = args.machine_label.strip()
    if "\n" in machine_label or "\r" in machine_label:
        return fail("机器名不能包含换行")

    template_path = Path(args.template).expanduser() if args.template else default_template()
    if not template_path.is_file():
        return fail(f"找不到模板：{template_path}")

    output = Path(args.output).expanduser()
    if output.exists() and output.is_dir():
        return fail(f"输出路径是目录：{output}")
    try:
        if output.resolve() == template_path.resolve():
            return fail("不能把输出写回模板本身")
    except OSError:
        pass
    if output.exists() and not args.force:
        print(f"✗ 已经存在，没有覆盖：{output}", file=sys.stderr)
        print("  确认要替换时再加上 --force。", file=sys.stderr)
        return 1

    try:
        rendered = render(
            template_path.read_text(encoding="utf-8"),
            host=host,
            root=root,
            library=library,
            machine_label=machine_label,
        )
    except ValueError as exc:
        return fail(str(exc))
    except OSError as exc:
        return fail(f"读不到模板：{exc}")

    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name(output.name + ".tmp")
        temporary.write_text(rendered, encoding="utf-8")
        os.replace(temporary, output)
    except OSError as exc:
        return fail(f"写文件失败：{exc}")

    print(f"✓ 已写入 {output}")
    print("  部署占位符已替换，可以把这个文件交给 Agent。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
