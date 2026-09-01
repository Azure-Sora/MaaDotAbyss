"""daily.yaml 任务条目的增删改排（文本级手术，保注释保排版）。

daily.yaml 的注释与排版本身就是内容（任务头上的 # 说明、行尾备注、多行 prompt），
PyYAML 一读一写会全部抹掉，故所有改动都按"任务块切割 + 原样重组"做文本手术：
- 块边界 = 顶格两空格的 `  - id: ` 列表项行，块内字节原样保留；
- 只重写被改的字段行（name/prompt/exit_condition/supplement），其余不动；
- 写入前先用 PyYAML 复检临时文件可解析、任务数不缺，再原子替换，
  写一半崩溃或改出坏 YAML 都不会损坏清单。

supplement（补充情报）是任务的可选字段：本次执行注入模型（冲突时以它为准），
任务成功后由模型合入 prompt 并清空——见 runner._consume_supplement。
"""
import json
import os
import re
from pathlib import Path

import yaml

from .config import TASKS_DIR

DAILY_YAML = TASKS_DIR / "daily.yaml"
_ID_RE = re.compile(r"^  - id: (\S+)\s*$")
_INDENT = "      "    # 块标量正文缩进（字段 4 空格 + 2），与现有文件一致


class TaskFileError(Exception):
    pass


# ---- 块切割与重组 --------------------------------------------------------

def _load_blocks(path: Path) -> tuple[list[str], list[list[str]], list[str]]:
    """按 `  - id: ` 切块，返回 (文件头, 块列表, id 列表)，块间空行随前一块。"""
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    starts = [i for i, ln in enumerate(lines) if _ID_RE.match(ln)]
    if not starts:
        raise TaskFileError(f"{path.name} 里没有任务条目（应存在两空格缩进的 '- id:' 行）")
    head = lines[:starts[0]]
    blocks = [lines[s:(starts[n + 1] if n + 1 < len(starts) else len(lines))]
              for n, s in enumerate(starts)]
    ids = [_ID_RE.match(b[0]).group(1) for b in blocks]
    if len(set(ids)) != len(ids):
        raise TaskFileError("存在重复的任务 id，请先手工修复 daily.yaml")
    return head, blocks, ids


def _write(head: list[str], blocks: list[list[str]], path: Path,
           expect_ids: list[str] | None = None) -> None:
    """重组全文 → 临时文件复检 YAML 与任务集合 → 原子替换。"""
    text = "".join(head) + "".join("".join(b) for b in blocks)
    if not text.endswith("\n"):
        text += "\n"
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        data = yaml.safe_load(tmp.read_text(encoding="utf-8")) or {}
        got = [t.get("id") for t in (data.get("tasks") or [])]
        if expect_ids is not None and got != expect_ids:
            raise TaskFileError(f"改写后任务集合不符：{got} 应为 {expect_ids}")
    except yaml.YAMLError as e:
        tmp.unlink(missing_ok=True)
        raise TaskFileError(f"改写后的 YAML 无法解析，已放弃写入: {e}") from e
    os.replace(tmp, path)


def list_task_ids(path: Path = DAILY_YAML) -> list[str]:
    return _load_blocks(path)[2]


# ---- 增删改排 ------------------------------------------------------------

def reorder_tasks(order: list[str], path: Path = DAILY_YAML) -> None:
    """按给定 id 顺序重排任务块（order 必须是现有全集的排列）。"""
    head, blocks, ids = _load_blocks(path)
    if sorted(order) != sorted(ids):
        raise TaskFileError("重排清单与现有任务不一致（增删后未刷新？）")
    by_id = {_ID_RE.match(b[0]).group(1): b for b in blocks}
    _write(head, [by_id[i] for i in order], path, expect_ids=order)


def delete_task(task_id: str, path: Path = DAILY_YAML) -> None:
    """删除任务条目（flow 剧本与知识卡文件不动，只摘清单）。"""
    head, blocks, ids = _load_blocks(path)
    if task_id not in ids:
        raise TaskFileError(f"任务不存在: {task_id}")
    rest = [i for i in ids if i != task_id]
    _write(head, [b for b in blocks if _ID_RE.match(b[0]).group(1) != task_id],
           path, expect_ids=rest)


def update_task(task_id: str, *, name=None, prompt=None, exit_condition=None,
                supplement=None, flow=None, path: Path = DAILY_YAML) -> None:
    """改单个任务的字段。None=不动；supplement/flow 传 "" 表示移除字段。"""
    head, blocks, ids = _load_blocks(path)
    if task_id not in ids:
        raise TaskFileError(f"任务不存在: {task_id}")
    block = blocks[ids.index(task_id)]
    for key, val in (("name", name), ("flow", flow), ("prompt", prompt),
                     ("exit_condition", exit_condition), ("supplement", supplement)):
        if val is None:
            continue
        if key in {"supplement", "flow"} and not str(val).strip():
            _remove_field(block, key)
            continue
        _set_field(block, key, str(val))
    _write(head, blocks, path, expect_ids=ids)


# ---- 字段级手术 ----------------------------------------------------------

def _field_idx(block: list[str], key: str) -> int | None:
    pat = re.compile(r"^ {4}" + key + r":(?:[ \t]+.*)?$")
    for i, ln in enumerate(block):
        if pat.match(ln):
            return i
    return None


def _scalar_span(block: list[str], i: int) -> int:
    """字段行 i 的标量占据到第几行（含块标量正文，不含其后的空行分隔）。"""
    j = i + 1
    while j < len(block) and (block[j].strip() == "" or block[j].startswith("      ")):
        j += 1
    while j > i + 1 and block[j - 1].strip() == "":   # 尾部空行是块间分隔，不属标量
        j -= 1
    return j


def _scalar(v: str) -> str:
    """单行标量：能安全裸写就裸写（贴近原文件风格），否则 JSON 双引号。"""
    v = v.strip()
    if not v or "\n" in v:
        return json.dumps(v, ensure_ascii=False)
    plain_unsafe = (
        v[0] in "-?:,[]{}#&*!|>'\"%@`"     # 首字符是指示符 → 需引号
        or v.endswith(":") or ": " in v    # 键值分隔歧义
        or " #" in v                       # 行尾歧义 → 注释
        or any(c in v for c in "\t\r\x0b\x0c")
    )
    return v if not plain_unsafe else json.dumps(v, ensure_ascii=False)


def _emit_field(key: str, value: str) -> list[str]:
    value = value.strip().rstrip()
    if "\n" in value:
        out = [f"    {key}: |\n"]
        for ln in value.splitlines():
            out.append(f"{_INDENT}{ln.rstrip()}\n" if ln.strip() else "\n")
        while out[-1] == "\n":      # 块标量末尾不留空行
            out.pop()
        return out
    return [f"    {key}: {_scalar(value)}\n"]


def _set_field(block: list[str], key: str, value: str) -> None:
    new = _emit_field(key, value)
    i = _field_idx(block, key)
    if i is None:
        end = len(block)                    # 无此字段：插到块尾空行分隔之前
        while end > 0 and block[end - 1].strip() == "":
            end -= 1
        block[end:end] = new
        return
    block[i:_scalar_span(block, i)] = new


def _remove_field(block: list[str], key: str) -> None:
    i = _field_idx(block, key)
    if i is not None:
        del block[i:_scalar_span(block, i)]
