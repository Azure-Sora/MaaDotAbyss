"""dnfile 元数据枚举 interop 类型清单（不加载执行，避免递归解析爆栈）。

输出: .local/interop_types_all.txt，每行 "程序集\\t命名空间.类型\\t基类"。
跑法: python poc/dump_interop_types.py
"""
import sys
import time
from pathlib import Path

import dnfile

INTEROP = Path(r"E:\Games\DMM\dotabyss_x_cl\BepInEx\interop")
OUT = Path(__file__).resolve().parent.parent / ".local" / "interop_types_all.txt"

lines = []
t0 = time.time()
for dll in sorted(INTEROP.glob("*.dll")):
    try:
        pe = dnfile.dnPE(str(dll))
    except Exception:
        continue
    if not pe.net or not pe.net.mdtables:
        continue
    md = pe.net.mdtables
    td, tr = md.TypeDef, md.TypeRef
    if td is None:
        continue
    # 一次性物化，避免每类型重复物化整表（O(n²) 坑）
    td_rows = list(td.rows) if td else []
    tr_rows = list(tr.rows) if tr else []
    n0 = len(lines)
    for t in td_rows:
        ns = t.TypeNamespace or ""
        name = t.TypeName
        base = ""
        try:
            ext = t.Extends
            if ext is not None and ext.table is not None:
                if ext.table.name == "TypeDef" and 0 < ext.row_index <= len(td_rows):
                    r = td_rows[ext.row_index - 1]
                    base = f"{r.TypeNamespace}.{r.TypeName}" if r.TypeNamespace else r.TypeName
                elif ext.table.name == "TypeRef" and 0 < ext.row_index <= len(tr_rows):
                    r = tr_rows[ext.row_index - 1]
                    base = f"{r.TypeNamespace}.{r.TypeName}" if r.TypeNamespace else r.TypeName
        except Exception:
            pass
        lines.append(f"{dll.stem}\t{ns}.{name}\t{base}")
    print(f"  {dll.stem}: {len(lines) - n0} types", flush=True)

OUT.parent.mkdir(exist_ok=True)
OUT.write_text("\n".join(lines), encoding="utf-8")
print(f"written {len(lines)} types -> {OUT} ({time.time() - t0:.1f}s)", file=sys.stderr)
