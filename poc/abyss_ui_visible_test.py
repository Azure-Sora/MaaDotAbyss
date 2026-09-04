"""abyss_ui 贴边候选可见性单测：不碰设备。python poc/abyss_ui_visible_test.py

背景（2026-09-04 52F 实战）：唯一候选 battle 房贴右缘露大半，旧判定要求整矩形
入界 → 误判屏外 → enter_room 只剩路径 Invoke，而部分房间入口 onClick.Invoke
是空操作 → 进房必败、LLM 自救乱点回主页。修复=射线可达（可见区≥60px）即算
屏内，点击点取裁剪到视口的可见区中心。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotabyss_agent.abyss_ui import read_candidates, read_map


def _cands_for(screens):
    """构造最小 MapCanvas 树（Front/MapFloor_*）→ read_candidates。"""
    children = []
    for name, screen in screens:
        children.append({
            "name": name,
            "screen": screen,
            "children": [{
                "name": "Button",
                "button": {"interactable": True, "path": f"/MapCanvas/Front/{name}/Button"},
            }],
        })
    tree = {"scene": "Nether", "canvases": [
        {"name": "MapCanvas", "children": [{"name": "Front", "children": children}]}]}

    class Dev:
        def ui_tree(self, canvas=None, max_nodes=4000):
            return tree
    return read_candidates(Dev(), current_floor=52, log=lambda *a: None)


# ---- 判定矩阵 ----
# 52F 实战形态：贴右缘露大半（旧判定 x1=1360>1320 → 屏外；现在应屏内+裁剪中心）
c = _cands_for([("MapFloor_Double_Battle(Clone)", [988, 390, 1360, 610])])
assert c[0].visible and (c[0].x, c[0].y) == (1134, 500), f"贴右缘应屏内+裁剪中心: {c[0]}"

# 贴左缘露 200px（doc 13 实测 x0=-72 形态）：旧判定屏外，现在射线可达
c = _cands_for([("MapFloor_Single_Battle(Clone)", [-72, 300, 200, 500])])
assert c[0].visible and c[0].x == 100, f"贴左缘应屏内: {c[0]}"

# 窄条露 40px（<60）：不可靠射线，仍算屏外走 Invoke
c = _cands_for([("MapFloor_Single_Shop(Clone)", [1240, 300, 1560, 500])])
assert not c[0].visible and (c[0].x, c[0].y) == (1400, 400), f"窄条仍应屏外: {c[0]}"

# 全屏外：坐标与旧逻辑一致（全矩形中心）
c = _cands_for([("MapFloor_Single_Heal(Clone)", [1400, 300, 1700, 500])])
assert not c[0].visible and (c[0].x, c[0].y) == (1550, 400), f"全屏外不变: {c[0]}"

# 贴下缘：y 也参与裁剪（旧判定完全不看 y）
c = _cands_for([("MapFloor_Single_Event(Clone)", [400, 650, 700, 900])])
assert c[0].visible and (c[0].x, c[0].y) == (550, 685), f"贴下缘应屏内: {c[0]}"

# 完全在视口内：中心点与旧逻辑逐像素一致（回归保护）
c = _cands_for([("MapFloor_Double_Treasure(Clone)", [400, 300, 700, 500])])
assert c[0].visible and (c[0].x, c[0].y) == (550, 400), f"屏内房不变: {c[0]}"

# ---- 真实 dump 回归：read_map 照常解析 ----
dump = json.loads((Path(__file__).parent / "abyss_map_ui.json").read_text(encoding="utf-8"))
data = read_map(dump)
assert data["rooms"], "dump 应解析出房间"
for r in data["rooms"]:
    assert "screen" not in r or len(r["screen"]) == 4

print("abyss_ui_visible_test: all ok")
