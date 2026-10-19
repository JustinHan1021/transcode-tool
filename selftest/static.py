"""Static sweep: unused imports, unreferenced module-level defs, duplicate names.

Not a substitute for running things, but it catches the dead weight that builds
up when a feature is replaced -- and this codebase has had several rounds of
that.
"""
from __future__ import annotations

import ast
import pathlib
import re
import sys

# The repo root is wherever this file ended up, so a clone runs anywhere.
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FILES = sorted(pathlib.Path(ROOT / "trans").rglob("*.py"))
ALL = {p: p.read_text(encoding="utf-8") for p in FILES}
WHOLE = "\n".join(ALL.values())

problems = 0


def report(kind: str, where, name: str, extra: str = "") -> None:
    global problems
    problems += 1
    print(f"  {kind:<16} {where}: {name} {extra}")


print("=== 1) 未使用的 import ===")
found_any = False
for p, src in ALL.items():
    tree = ast.parse(src)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                names.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name != "*":
                    names.add(a.asname or a.name)
    # Remove the import statements themselves before counting uses.
    stripped = "\n".join(line for line in src.splitlines()
                         if not re.match(r"\s*(from|import)\s", line))
    for n in sorted(names):
        if not re.search(r"(?<!\w)" + re.escape(n) + r"(?!\w)", stripped):
            report("UNUSED IMPORT", p.name, n)
            found_any = True
if not found_any:
    print("  无")

print()
print("=== 2) 定义后全项目没人引用的模块级函数/类 ===")
found_any = False
for p, src in ALL.items():
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            name = node.name
            if name.startswith("__"):
                continue
            uses = len(re.findall(r"(?<!\w)" + re.escape(name) + r"(?!\w)",
                                  WHOLE))
            # one occurrence is the definition itself
            if uses <= 1:
                report("UNREFERENCED", p.name, name)
                found_any = True
if not found_any:
    print("  无")

print()
print("=== 3) 重复定义的函数名（后一个覆盖前一个） ===")
found_any = False
for p, src in ALL.items():
    tree = ast.parse(src)
    seen: dict[str, int] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            if node.name in seen:
                report("DUPLICATE DEF", p.name, node.name,
                       f"(line {seen[node.name]} 与 {node.lineno})")
                found_any = True
            seen[node.name] = node.lineno
if not found_any:
    print("  无")

print()
print("=== 4) 导入的 UI 模块能否实例化（tkinter 可达性） ===")
try:
    import tkinter  # noqa: F401
    import trans.ui.app  # noqa: F401
    import trans.ui.tab_video  # noqa: F401
    import trans.ui.tab_image  # noqa: F401
    import trans.ui.tab_audio  # noqa: F401
    import trans.ui.trim_bar  # noqa: F401
    print("  全部 UI 模块导入 OK")
except Exception as exc:
    report("UI IMPORT FAIL", "trans.ui", type(exc).__name__, str(exc))
    problems += 1

print()
print("=== 5) 用户可见文案里是否有 TODO/FIXME/占位符 ===")
found_any = False
for p, src in ALL.items():
    for i, line in enumerate(src.splitlines(), 1):
        if re.search(r"\b(TODO|FIXME|XXX|HACK)\b", line):
            report("MARKER", p.name, f"line {i}", line.strip()[:70])
            found_any = True
if not found_any:
    print("  无")

print()
print("问题总数: %d" % problems)
