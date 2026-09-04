"""深渊地图 UI 直读（BepInEx 桥 v0.2.0+）：MapCanvas → 结构化房间/道路 + HUD 对账。

doc 12 识读层的桥后端实现，实测（2026-08-30）取代模板匹配+横向拖拽扫读：
- 房间：MapFloor_{Single|Double|Triple}_{Type}(Clone)，类型写在节点名与 StageTitle 文本；
- 推奨戦力：NetherStageInfo/TextPower（当前不用于决策，留档）；
- 候选（可进入）= 房间 Button.interactable——箭头/光圈的游戏态本体，拓扑感知
  （清完房间后只剩连通房），且**屏外房间也可见**（实测下层 4 候选 2 个在视口外）；
- 道路：MapRoad_{Light|Fire|Water|Artifact}(Clone)；
- HUD 全在 UICanvas 文本节点：层数 UI/L/StageName、侵蚀 UI/R/Gauge_Abyss/Value、
  钥匙 UI/R/..Key/Value、金币 ..Coin/Value——**对账零 OCR**。
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
VIEW_W, VIEW_H = 1280, 720   # 游戏渲染分辨率（doc 13 §2，与截图像素系一致）
MIN_CLICKABLE = 60           # 可见区最小边长：低于此算屏外（贴边窄条易被边缘 HUD 挡射线）


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


def _walk_path(n, base=""):
    """(node, 相对路径)——HUD 节点按路径匹配（Value 这种名字太通用）。"""
    p = f"{base}/{n['name']}"
    yield n, p
    for c in n.get("children", []):
        yield from _walk_path(c, p)


def read_hud(device) -> dict:
    """UICanvas HUD → {floor, erosion, keys, coins}（对账用，零 OCR）。"""
    tree = device.ui_tree(max_nodes=12000)
    uic = next((c for c in tree.get("canvases", []) if c["name"] == "UICanvas"), None)
    if uic is None:
        raise RuntimeError("UICanvas 不存在（当前不在深渊 run 内？）")
    out: dict = {}
    for n, p in _walk_path(uic):
        t = (n.get("text") or "").strip()
        if not t:
            continue
        if n["name"] == "Text" and p.endswith("/StageName/Base/Text"):
            m = re.search(r"(\d+)", t)
            out["floor"] = int(m.group(1)) if m else None
        elif n["name"] == "Value" and "/Gauge_Abyss/" in p:
            out["erosion"] = int(t)
        elif n["name"] == "Value" and "/Key/" in p:
            out["keys"] = int(t.replace(",", ""))
        elif n["name"] == "Value" and "/Coin/" in p:
            out["coins"] = int(t.replace(",", ""))
    return out


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
        # 射线点击只要求点中的部分在屏内：贴边房裁到视口、取可见区中心当点击点。
        # 旧判定要求整矩形入界，半露房被误判屏外 → enter_room 只剩路径 Invoke，
        # 而部分房间入口 onClick.Invoke 是空操作（只认指针事件管线）→ 进房必败
        # （2026-09-04 52F 实战：唯一候选贴右缘露大半，Invoke 无反应，手点可进）。
        vx0, vy0 = max(x0, 0), max(y0, 0)
        vx1, vy1 = min(x1, VIEW_W), min(y1, VIEW_H)
        if vx1 - vx0 >= MIN_CLICKABLE and vy1 - vy0 >= MIN_CLICKABLE:
            visible, cx, cy = True, (vx0 + vx1) // 2, (vy0 + vy1) // 2
        else:
            visible, cx, cy = False, (x0 + x1) // 2, (y0 + y1) // 2
        cands.append(Candidate(r["type"], cx, cy, current_floor or -1, visible,
                               r.get("btn_path")))
    order = sorted(range(len(cands)),
                   key=lambda i: (not cands[i].visible, i))
    cands[:] = [cands[i] for i in order]
    n_off = sum(1 for c in cands if not c.visible)
    log(f"  [abyss_ui] 房间 {len(data['rooms'])}，候选 {len(cands)}"
        + (f"（含屏外 {n_off}）" if n_off else ""))
    return cands
