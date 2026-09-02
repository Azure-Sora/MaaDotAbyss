"""深渊代码主图标模板采集：图鉴（アビスコード図鑑）四色系 Tab 各裁一条纯净竖条。

模板依据（docs/12 §14.11）：同色系所有代码的主图标完全相同，只有右下菱形（效果）
与边框（稀有度）因码而异——每系一张模板即可代表全系，运行时 _match_code_color
多尺度匹配定色，LLM 只兜底。裁片取图标方块左部竖条（避开内框与菱形）。

用法：游戏停在 アビスコード図鑑 页（NetherTop → 探索 → アビスコード図鑑），
`python poc/abyss_code_icons.py`。游戏版本更新图标后可随时重采。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2  # noqa: E402

from dotabyss_agent.device_bridge import BridgeDevice  # noqa: E402

OUT_DIR = Path(__file__).resolve().parents[1] / "tasks" / "flows" / "anchors" / "abyss" / "code_icons"
TAB_TITLES = {"Tab1": ("impact", "インパクトコード"), "Tab2": ("rush", "ラッシュコード"),
              "Tab3": ("safe", "セーフコード"), "Tab4": ("risk", "リスクコード")}
# 图标方块内的纯净竖条（cell 相对偏移，避开内框 ~10px 与右下菱形 47% 起）
STRIP = (48, 50, 96, 138)


def walk(n):
    yield n
    for c in n.get("children", []):
        yield from walk(c)


def walk_tree(tree):
    for c0 in tree.get("canvases", []):
        yield from walk(c0)


def main() -> None:
    d = BridgeDevice()
    tree = d.ui_tree(max_nodes=30000)
    tabs = {}
    for n in walk_tree(tree):
        b = n.get("button")
        if not b:
            continue
        for tab, (_, title) in TAB_TITLES.items():
            if f"/{tab}/Button_Tab" in b.get("path", ""):
                tabs[tab] = n
    if len(tabs) < 4:
        raise SystemExit(f"图鉴 Tab 未找全（{list(tabs)}）——游戏停在图鉴页了吗？")
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    def counter_labels(tr):
        return [(n.get("text") or "").strip() for n in walk_tree(tr)
                if "系統" in (n.get("text") or "")]

    for tab, (color, title) in TAB_TITLES.items():
        scr = tabs[tab].get("screen")
        if not scr:
            raise SystemExit(f"{tab} 无坐标")
        if not any(title in t for t in counter_labels(tree)):   # 已激活就别点
            cx, cy = (scr[0] + scr[2]) // 2, (scr[1] + scr[3]) // 2
            try:
                d.click_ui(cx, cy)
            except Exception:
                d.click_by_path(tabs[tab]["button"]["path"])   # 激活态 Tab 射线不命中
            time.sleep(1.2)
            tree = d.ui_tree(max_nodes=30000)
        # 核验：底部计数标签（インパクトコード系統 等）跟随激活 Tab——Selected_Tab
        # 的 TMP 文本树里导出为空不可靠（UI 多层，一切以截图/可见文本为准）
        labels = counter_labels(tree)
        assert any(title in t for t in labels), f"{tab} 未选中（计数标签 {labels}）——看截图核对"
        frame = d.screenshot()
        # 已持有卡=亮图，未持有 ??? 是暗剪影：完整可见的卡里选裁条最亮的一张。
        # 必须滤掉被列表底边裁一半的格子（rect 超出视口，裁条会混进背景/按钮）。
        # 视口=ScrollerV（名字唯一；别拿 'Box' 找——同名词满天飞会撞上背景装饰）
        sv = next((n for n in walk_tree(tree)
                   if n["name"] == "ScrollerV" and n.get("screen")), None)
        vy0, vy1 = (sv["screen"][1], sv["screen"][3]) if sv else (156, 612)
        cells = [n for n in walk_tree(tree)
                 if n["name"].startswith("AbyssCode") and not n["name"].startswith("AbyssCode_")
                 and n.get("screen") and n["screen"][0] > 400   # 右侧列表（x<400 是左侧详情面板）
                 and abs((n["screen"][3] - n["screen"][1]) - 182) < 40
                 and n["screen"][1] >= vy0 and n["screen"][3] <= vy1]
        if not cells:
            raise SystemExit(f"{color}: 图鉴里没读到完整可见的卡片格子——看截图核对")
        def strip_mean(n):
            x0, y0 = n["screen"][0], n["screen"][1]
            sx0, sy0, sx1, sy1 = STRIP
            return float(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)[y0 + sy0:y0 + sy1,
                                                               x0 + sx0:x0 + sx1].mean())
        cell = max(cells, key=strip_mean)
        if strip_mean(cell) < 60:
            raise SystemExit(f"{color}: 最亮卡裁条均值 {strip_mean(cell):.0f} 过暗（全是 ??? 未持有？）")
        x0, y0 = cell["screen"][0], cell["screen"][1]
        sx0, sy0, sx1, sy1 = STRIP
        tpl = frame[y0 + sy0:y0 + sy1, x0 + sx0:x0 + sx1]
        out = OUT_DIR / f"{color}.png"
        cv2.imwrite(str(out), tpl)
        print(f"{color} ← {title} cell@({x0},{y0}) strip {tpl.shape[1]}x{tpl.shape[0]} → {out}")
    print("完成。图鉴可能停在最后一个 Tab，无碍。")


if __name__ == "__main__":
    main()
