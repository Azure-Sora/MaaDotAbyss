"""深渊地图 UI 直读原型：MapCanvas → 结构化房间/道路/候选（doc 12 识读层升级验证）。

用法: python poc/abyss_ui_read.py   （游戏需停在深渊地图页）

验证目标：
- 房间类型、推奨戦力、screen 坐标从 UI 树直读（MapFloor_*_类型 + NetherStageInfo）；
- 候选房间 = MapFloor 内 Button.interactable == true（箭头/光圈的游戏态本体）；
- 与截图对照（強敵/戦闘双箭头应命中）。
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotabyss_agent.device_bridge import BridgeDevice  # noqa: E402

# 节点名/StageTitle → 规划器房间类型
ROOM_TYPES = {
    "battle": "battle", "battleminiboss": "elite", "battleboss": "boss",
    "recovery": "heal", "event": "event", "shop": "shop", "treasure": "treasure",
}
STAGE_TITLE = {"強敵": "elite", "ボス": "boss", "戦闘": "battle", "回復": "heal",
               "イベント": "event", "商店": "shop", "宝箱": "treasure"}


def walk(n):
    yield n
    for c in n.get("children", []):
        yield from walk(c)


def find_text(node: dict, name_contains: str) -> str | None:
    for x in walk(node):
        if name_contains.lower() in x["name"].lower():
            return x.get("text")
    return None


def read_map(tree: dict) -> dict:
    """MapCanvas → {"rooms": [...], "roads": [...]}。房间含 type/power/screen/enterable。"""
    stage = tree["canvases"][0]
    def find(name: str):
        for x in walk(stage):
            if x["name"] == name:
                return x
        return None
    front = None
    for x in walk(stage):
        if x["name"] == "Front" and x.get("children"):
            front = x  # Stage/Front（最后一个同名者优先，Stage 下的才是地图）
    rooms, roads = [], []
    for c in front.get("children", []):
        m = re.match(r"MapFloor_(?:Single|Double|Triple)_(\w+?)\(Clone\)$", c["name"])
        if m:
            t = ROOM_TYPES.get(m.group(1).lower())
            btn = None
            # Button 在 Anim/Button；interactable 即"当前可进入"
            for x in walk(c):
                if "button" in x:
                    btn = x["button"]
                    break
            power = find_text(c, "TextPower")
            title = find_text(c, "StageTitle")
            rooms.append({
                "type": t or STAGE_TITLE.get((title or "").strip(), "unknown"),
                "name_type": m.group(1),
                "title": title,
                "power": power,
                "screen": c.get("screen"),
                "enterable": bool(btn and btn.get("interactable")),
                "btn_path": btn.get("path") if btn else None,
            })
            continue
        mr = re.match(r"MapRoad_(\w+?)\(Clone\)$", c["name"])
        if mr:
            roads.append({"elem": mr.group(1), "screen": c.get("screen")})
    return {"rooms": rooms, "roads": roads}


def main():
    d = BridgeDevice()
    tree = d.ui_tree(canvas="MapCanvas", max_nodes=30000)
    out = Path(__file__).parent / "abyss_map_ui.json"
    out.write_text(json.dumps(tree, ensure_ascii=False, indent=1), encoding="utf-8")
    data = read_map(tree)
    print(f"scene={tree.get('scene')}  房间 {len(data['rooms'])}  道路 {len(data['roads'])}")
    print("\n=== 房间（y 排序，enterable★）===")
    for r in sorted(data["rooms"], key=lambda r: (r["screen"][1], r["screen"][0])):
        mark = "★" if r["enterable"] else " "
        print(f" {mark} {r['type']:9s} {str(r['title']):6s} power={r['power'] or '-':>8s}"
              f"  screen={r['screen']}")
    cand = [r for r in data["rooms"] if r["enterable"]]
    print(f"\n候选（enterable）: {len(cand)} 个")
    for r in cand:
        print(f"  ★ {r['type']} @ {r['screen']}")
    json.dump(data, open(Path(__file__).parent / "abyss_rooms.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
