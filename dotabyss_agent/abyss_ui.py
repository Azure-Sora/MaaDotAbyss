"""深渊地图 UI 直读（BepInEx 桥 v0.2.0+）：MapCanvas → 结构化房间/道路。

doc 12 识读层的桥后端实现，实测（2026-08-30）取代模板匹配+横向拖拽扫读：
- 房间：MapFloor_{Single|Double|Triple}_{Type}(Clone)，类型写在节点名与 StageTitle 文本；
- 推奨戦力：NetherStageInfo/TextPower（当前不用于决策，留档）；
- 候选（可进入）= 房间 Button.interactable——箭头/光圈的游戏态本体，且**屏外房间也可见**
  （实测下层 4 候选有 2 个在视口外）；
- 道路：MapRoad_{Light|Fire|Water|Artifact}(Clone)。
整个 10 层（当前→下一个 Boss）一次全量可得，规划器具备全程已知的前提。
"""
import re

from .abyss_plan import Candidate

ROOM_TYPES = {
    "battle": "battle", "battleminiboss": "elite", "battleboss": "boss",
    "recovery": "heal", "event": "event", "shop": "shop", "treasure": "treasure",
}
STAGE_TITLE = {"強敵": "elite", "ボス": "boss", "戦闘": "battle", "回復": "heal",
               "イベント": "event", "商店": "shop", "宝箱": "treasure"}
_FLOOR_RE = re.compile(r"MapFloor_(?:Single|Double|Triple)_(\w+?)\(Clone\)$")
_ROAD_RE = re.compile(r"MapRoad_(\w+?)\(Clone\)$")


def _walk(n):
    yield n
    for c in n.get("children", []):
        yield from _walk(c)


def _find_text(node: dict, name_contains: str) -> str | None:
    for x in _walk(node):
        if name_contains.lower() in x["name"].lower():
            return x.get("text")
    return None


def _front_of(tree: dict) -> dict | None:
    """Stage/Front（地图楼层容器）。UIGroup 下名为 Front 的节点。"""
    for c0 in tree.get("canvases", []):
        for x in _walk(c0):
            if x["name"] == "Front" and x.get("children"):
                return x
    return None


def read_map(tree: dict) -> dict:
    """/ui 的 MapCanvas JSON → {"rooms":[...], "roads":[...]}。

    room: {type, name_type, title, power, screen, enterable, btn_path}
    """
    rooms, roads = [], []
    front = _front_of(tree)
    if front is None:
        raise RuntimeError("UI 树中没有地图 Front（游戏在深渊地图页吗？MapCanvas 导出对吗？）")
    for c in front.get("children", []):
        m = _FLOOR_RE.match(c["name"])
        if m:
            btn = None
            for x in _walk(c):
                if "button" in x:
                    btn = x["button"]
                    break
            title = _find_text(c, "StageTitle")
            rooms.append({
                "type": ROOM_TYPES.get(m.group(1).lower())
                        or STAGE_TITLE.get((title or "").strip(), "unknown"),
                "name_type": m.group(1),
                "title": title,
                "power": _find_text(c, "TextPower"),
                "screen": c.get("screen"),
                "enterable": bool(btn and btn.get("interactable")),
                "btn_path": btn.get("path") if btn else None,
            })
            continue
        mr = _ROAD_RE.match(c["name"])
        if mr:
            roads.append({"elem": mr.group(1), "screen": c.get("screen")})
    return {"rooms": rooms, "roads": roads}


def read_candidates(device, current_floor: int | None = None, log=print) -> list[Candidate]:
    """桥直读当前可进入的房间（Candidate 列表，按视口内优先排序）。

    非 BridgeDevice（无 ui_tree）由调用方走模板匹配兜底（abyss.read_candidates_anchors）。
    """
    tree = device.ui_tree(canvas="MapCanvas", max_nodes=30000)
    data = read_map(tree)
    cands = []
    for r in data["rooms"]:
        if not r["enterable"] or not r["screen"]:
            continue
        x0, y0, x1, y1 = r["screen"]
        cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
        visible = -40 <= x0 and x1 <= 1320
        cands.append(Candidate(r["type"], cx, cy, current_floor or -1, visible))
    order = sorted(range(len(cands)),
                   key=lambda i: (not cands[i].visible, i))
    cands[:] = [cands[i] for i in order]
    n_off = sum(1 for c in cands if not c.visible)
    log(f"  [abyss_ui] 房间 {len(data['rooms'])}，候选 {len(cands)}"
        + (f"（含屏外 {n_off}）" if n_off else ""))
    return cands
