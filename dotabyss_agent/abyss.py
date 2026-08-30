"""深渊自动刷装备控制器（docs/research/12）。

三权分立：识读=桥 UI 直读（abyss_ui，首选）/锚点模板（MAA 兜底）+ LLM 兜底；
规划=abyss_plan 纯函数（已单测）；执行=device（桥 click/click_ui/click_by_path）。

实机已验证（2026-08-30，doc 13 §2.7）：
- 场景链 Nether(地图) → ExplorationBattle(战斗，自动跑) → ExplorarionNetherResult(结算)
- 结算流：深渊代码三选一弹窗（精英/Boss 掉落）→ 探索報酬页「確認して次へ」（非 Button，
  射线点击）→ Button_Next → 回 Nether
- HUD（层数/侵蚀/钥匙/金币）全在 UICanvas 文本节点，对账零 OCR
- 拿码后结算页明写颜色系统（"リスクコード系統の…"）——颜色注册表的自证来源
"""
import json
import re
import time
from pathlib import Path

import cv2
import numpy as np

from .abyss_plan import AbyssLedger, Candidate, event_score, pick_code, pick_room, ticket_decision
from .abyss_ui import _walk_path
from .config import TASKS_DIR
from .flow import match_anchor

ABYSS_ANCHORS = TASKS_DIR / "flows" / "anchors" / "abyss"

ROOM_LABELS = {   # 地图标签模板 → 规划器房间类型
    "label_battle.png": "battle",
    "label_elite.png": "elite",
    "label_boss.png": "boss",
    "label_heal.png": "heal",
    "label_event.png": "event",
    "label_shop.png": "shop",
    "label_treasure.png": "treasure",
}
LABEL_THRESHOLD = 0.80
CHEVRON_THRESHOLD = 0.75


def _anchor(name: str) -> np.ndarray:
    img = cv2.imread(str(ABYSS_ANCHORS / name), cv2.IMREAD_COLOR)
    if img is None:
        raise RuntimeError(f"缺少深渊锚点素材: {ABYSS_ANCHORS / name}（先跑 poc/crop_abyss_anchors.py）")
    return img


def read_candidates(device, current_floor: int | None = None, log=print) -> list[Candidate]:
    """识别当前可进入的候选房间。桥后端=UI 直读（首选，屏外候选也可见）；
    MAA 后端=箭头/标签模板匹配兜底（仅视口内）。"""
    if hasattr(device, "ui_tree"):
        from .abyss_ui import read_candidates as ui_read
        return ui_read(device, current_floor=current_floor, log=log)
    return read_candidates_anchors(device, log=log)


def read_candidates_anchors(device, log=print) -> list[Candidate]:
    """模板匹配兜底：箭头定位 + 就近标签分类（仅视口内候选）。"""
    frame = device.screenshot()
    chev = _anchor("chevron.png")
    labels = {name: _anchor(name) for name in ROOM_LABELS}

    # 箭头可能同时出现多个：遍历匹配热点（简易多命中：抑制邻域后取前 4）
    res = cv2.matchTemplate(frame, chev, cv2.TM_CCOEFF_NORMED)
    ys, xs = np.where(res >= CHEVRON_THRESHOLD)
    points: list[tuple[int, int]] = []
    for x, y in sorted(zip(xs.tolist(), ys.tolist()), key=lambda p: -res[p[1], p[0]]):
        if all(abs(x - px) > 60 or abs(y - py) > 60 for px, py in points):
            points.append((x, y))
        if len(points) >= 4:
            break

    cands: list[Candidate] = []
    for x, y in points:
        cx, cy = x + chev.shape[1] // 2, y + chev.shape[0] // 2
        rtype = "battle"  # 未知兜底
        best_d = 150
        for lname, limg in labels.items():
            lres = cv2.matchTemplate(frame, limg, cv2.TM_CCOEFF_NORMED)
            _ys, _xs = np.where(lres >= LABEL_THRESHOLD)
            for lx, ly in zip(_xs.tolist(), _ys.tolist()):
                d = abs((lx + limg.shape[1] // 2) - cx) + (ly + limg.shape[0] // 2 - cy)
                if 0 < d < best_d:
                    best_d = d
                    rtype = ROOM_LABELS[lname]
        cands.append(Candidate(rtype, cx, cy, floor=-1))
        log(f"  候选: {rtype} @ ({cx},{cy})")
    return cands


def reveal_clipped_candidates(device) -> None:
    """候选贴屏幕边缘时定向拖半屏补读（箭头检测数为 0 或贴边 x<80/x>1200 时调用）。"""
    device.swipe(350, 400, 800, 400, duration_ms=500)  # 拖左半屏 → 镜头西移


# ---- 房间解决（实机联调项，先立框架） ---------------------------------------


def enter_room(device, room: Candidate, log=print) -> None:
    """进房：屏内房间用射线式真实点击（部分房间入口只挂指针事件管线，
    onClick.Invoke 是空操作——实测 2026-08-30）；屏外房间只能按路径 Invoke。
    点完 3s 内场景/覆盖层必须有反应，否则换法重试一次。"""
    attempts = []
    if room.visible:
        attempts += [("ray", room.x, room.y)]
    if room.btn_path:
        attempts.append(("path", room.btn_path, None))
    if not room.visible and not room.btn_path:
        attempts.append(("click", room.x, room.y))
    last_err = "无可用点击方式"
    for kind, a, b in attempts:
        try:
            if kind == "ray":
                p = device.click_ui(a, b)
                if "PullOut" in p or "Retreat" in p:
                    raise RuntimeError(f"射线命中撤退按钮 {p}——立即停止")
            elif kind == "path":
                device.click_by_path(a)
            else:
                device.click(a, b)
        except Exception as e:
            last_err = f"{kind}: {e}"
            continue
        time.sleep(PACING)
        device.wait_settled(device.screenshot())
        time.sleep(1.5)
        scene = _scene(device)
        if scene != "Nether":
            return
        tree = device.ui_tree(max_nodes=6000)
        if any(n.get("text") in ("確認", "確定") for n in _walk_all(tree)):
            return
        last_err = f"{kind} 点击无反应（场景仍 Nether）"
        log(f"  [enter] {last_err}，换下一种方式")
    raise RuntimeError(f"进房失败：{room.type} @ ({room.x},{room.y})——{last_err}")


def resolve_room(device, room: Candidate, led: AbyssLedger, brain, log=print) -> None:
    """按类型解决房间并更新账本。战斗类=等自动战斗+结算流（含代码弹窗）。
    层数统一在这里 +1（进房即推进；事件内置的战斗不再多加）。"""
    if room.type in ("battle", "elite", "boss"):
        led.hp_lost_pct = 0                    # 战斗回满，HP 预算清零
        resolve_battle(device, led, brain, log=log)
    elif room.type == "heal":
        _resolve_heal(device, led, log=log)
    elif room.type == "event":
        _resolve_event(device, led, brain, log)
    elif room.type == "treasure":
        _open_treasure(device, log)
    elif room.type == "shop":
        _close_shop(device, log)
    time.sleep(PACING)
    led.floor += 1


# ---- 战斗/结算流（实测 2026-08-30） ------------------------------------------

PACING = 0.45   # 点击后节流：点太快会卡 LOADING（用户实测反馈）

# 深渊代码名称 → 颜色（impact黄/rush红/safe蓝/risk紫）。
# 来源：拿码后结算页明写「XXコード系統の恩恵ゲージ…」——自证后入册；
# 未知名走 LLM 视觉定色（大方块=颜色种类，小菱形=效果种类，用户纠正 2026-08-30）。
CODE_COLORS = {
    "疫壳": "risk",
    "危险循环": "risk",    # 名带「危险」= リスク系（侵蚀≥70% 充能效率）
    "安全释放": "safe",    # 名带「安全」= セーフ系（安全代码研究点+15%）
    # 31F 特殊战斗掉落（监督定色 2026-08-30）：
    "后排偏斜": "impact",
    "深渊视界": "risk",
    "狙击热忱": "rush",
}


def _scene(device) -> str:
    return device.ui_tree(max_nodes=10).get("scene", "")


def resolve_battle(device, led: AbyssLedger, brain, log=print, timeout: float = 300.0) -> None:
    """进房后的战斗全流程（房间已由 enter_room 点入）：战斗自动跑 → 等结果
    → 代码弹窗/报酬页循环 → 回 Nether 地图。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if _scene(device) == "ExplorarionNetherResult":
            break
        time.sleep(3.0)
    else:
        raise RuntimeError("战斗超时未出结果（场景一直不是 Result）")
    log(f"  战斗结束（{time.time() - t0:.0f}s），进入结算流")
    t0 = time.time()
    while time.time() - t0 < 120:
        tree = device.ui_tree(max_nodes=12000)
        if _gate_present(tree):
            log("  检测到续行界面（Boss 层），交由主循环处理")
            return
        if tree.get("scene") == "Nether":
            log("  回到地图")
            return
        _result_step(device, led, brain, log=log, tree=tree)
        time.sleep(1.5)
    raise RuntimeError("结算流超时未回地图")


def _result_step(device, led: AbyssLedger, brain, log=print, tree: dict | None = None) -> bool:
    """结算流的单步：代码弹窗 > 结算按钮 > 射线连点。返回是否有动作。"""
    tree = tree or device.ui_tree(max_nodes=12000)
    def walk(n):
        yield n
        for c in n.get("children", []):
            yield from walk(c)
    # 1) 深渊代码三选一弹窗
    for c0 in tree["canvases"]:
        for n in walk(c0):
            if n["name"].startswith("Popup_AbyssCodeSelect"):
                _handle_code_popup(device, led, brain, log=log)
                return True
    # 2) 结算按钮（真实 Button）
    for c0 in tree["canvases"]:
        for n in walk(c0):
            if "button" not in n:
                continue
            nm = n["name"]
            if nm in ("Button_Next", "Button_ToExploration", "Button_ToNextQuest") \
                    and n["button"].get("interactable"):
                device.click_by_path(n["button"]["path"])
                log(f"  点击 {nm}")
                return True
    # 3) 射线连点（確認して次へ 等非 Button 区域；避开左下撤退区）
    for x, y in ((640, 660), (1177, 661)):
        try:
            p = device.click_ui(x, y)
        except Exception:
            continue
        if "PullOut" in p or "Retreat" in p:   # 铁律：绝不碰撤退
            raise RuntimeError(f"射线命中撤退按钮 {p}——立即停止")
        log(f"  射线点击 ({x},{y}) → {p[-40:]}")
        return True
    return False


def _handle_code_popup(device, led: AbyssLedger, brain, log=print) -> None:
    """深渊代码三选一：读选项→（注册表/LLM）定色→规划器择取→选+确定。
    卡片有入场滚动动画（名字/数值延迟出现，'+9999999' 是占位），读前先等出全。
    名字在 AbyssCodeName/Text、描述在 TextArea/Text、战力在 PowerUP 下的 Value。"""
    def walk(n):
        yield n
        for c in n.get("children", []):
            yield from walk(c)

    options = []
    popup = None
    for _round in range(8):
        tree = device.ui_tree(max_nodes=12000)
        popup = next((n for n in _walk_all(tree)
                      if n["name"].startswith("Popup_AbyssCodeSelect")), None)
        if popup is None:
            return   # 弹窗已消失
        options = []
        for n in walk(popup):
            m = re.match(r"Code_(\d)$", n["name"])
            if not m:
                continue
            name = desc = pw = None
            for x, xp in _walk_path(n):
                t = (x.get("text") or "").strip()
                if not t:
                    continue
                if name is None and "/AbyssCodeName/Text" in xp:
                    name = t
                elif desc is None and "/TextArea/Text" in xp:
                    desc = t
                elif pw is None and "/PowerUP/" in xp and x["name"] == "Value":
                    pw = t
            options.append({"idx": int(m.group(1)), "name": name, "desc": desc, "power": pw,
                            "screen": n.get("screen"),
                            "color": CODE_COLORS.get(name) if name else None})
        options.sort(key=lambda o: o["idx"])
        if options and all(o["name"] and o["power"] and not o["power"].startswith("+")
                           for o in options):
            break
        time.sleep(1.0)
    if not options or popup is None:
        log("  代码弹窗读取失败（选项/弹窗缺失）")
        return
    if brain is not None:
        _classify_codes_vision(device, [o for o in options if o["color"] is None], brain, log=log)
    m = re.search(r"残り\s*(\d+)", json.dumps(
        [(x.get("text") or "") for x in walk(popup)], ensure_ascii=False))
    rerolls = int(m.group(1)) if m else 0
    plan_opts = [{"color": o["color"] or "", "power": int((o["power"] or "0").replace(",", ""))}
                 for o in options]
    verb, idx = pick_code(plan_opts, led, rerolls)
    log(f"  代码弹窗: {[(o['name'], o['color'], o['power']) for o in options]} → {verb}")
    if verb == "reroll":
        device.click_by_path(_popup_btn(popup, "Button_Reroll"))
        return
    if verb == "skip":
        device.click_by_path(_popup_btn(popup, "Button_Cancel"))   # 受け取らない
        return
    o = options[idx]
    device.click_by_path(_popup_btn(popup, f"Code_{o['idx']}/AbyssCode_{o['idx']}"))
    time.sleep(0.8)
    device.click_by_path(_popup_btn(popup, "Button_Confirm"))
    color = o["color"] or "unknown"
    if color in led.buffs:
        led.buffs[color] += 1
    log(f"  拿码 {o['name']}（{color}，战力 {o['power']}）")


def _popup_btn(popup, suffix: str) -> str:
    def walk(n):
        yield n
        for c in n.get("children", []):
            yield from walk(c)
    for n in walk(popup):
        if "button" in n and n["button"]["path"].endswith(suffix):
            return n["button"]["path"]
    raise RuntimeError(f"弹窗内未找到 {suffix}")


def _classify_codes_vision(device, options, brain, log=print) -> None:
    """未知名代码：裁大方块图标 → LLM 定色 → 入注册表。"""
    if not options:
        return
    frame = device.screenshot()
    for o in options:
        if not o.get("screen"):
            continue
        x0, y0, x1, y1 = o["screen"]
        crop = frame[y0 + 20:y0 + 170, x0 + 150:x1 - 150]   # 大方块图标区（卡片上部中央）
        if crop.ndim != 3 or crop.shape[0] < 20 or crop.shape[1] < 20:
            continue
        try:
            data = brain.read_json_from_image(
                crop,
                "这是游戏深渊代码（buff）卡片的图标特写。大方块图标的边框/主色代表颜色种类："
                "黄=impact、红=rush、蓝=safe、紫=risk。只输出 JSON："
                '{"color": "impact|rush|safe|risk 之一"}')
            color = str(data.get("color", "")).lower()
            if color in ("impact", "rush", "safe", "risk"):
                o["color"] = color
                if o["name"]:
                    CODE_COLORS[o["name"]] = color
                log(f"  视觉定色: {o['name']} → {color}（已入注册表）")
        except Exception as e:
            log(f"  视觉定色失败 {o['name']}: {e.__class__.__name__}")


def _walk_all(tree):
    for c0 in tree.get("canvases", []):
        def walk(n):
            yield n
            for c in n.get("children", []):
                yield from walk(c)
        yield from walk(c0)


def _find_text_nodes(tree, keyword: str):
    """[(node)]：TMP 文本包含关键词的节点（覆盖层选项/确认按钮定位用）。"""
    return [n for n in _walk_all(tree)
            if keyword in ((n.get("text") or ""))]


def _click_text_center(device, tree, keyword: str, log=print) -> bool:
    """射线点击包含关键词的文本节点中心（覆盖层按钮普遍非 Button 组件）。"""
    for n in _find_text_nodes(tree, keyword):
        s = n.get("screen")
        if not s:
            continue
        cx, cy = (s[0] + s[2]) // 2, (s[1] + s[3]) // 2
        try:
            p = device.click_ui(cx, cy)
        except Exception:
            continue
        if "PullOut" in p or "Retreat" in p:
            raise RuntimeError(f"射线命中撤退按钮 {p}——立即停止")
        log(f"  点击『{keyword}』({cx},{cy})")
        return True
    return False


def _gate_present(tree) -> bool:
    return any(n.get("text") and "続行する" in n["text"] for n in _walk_all(tree))


def _resolve_heal(device, led: AbyssLedger, log=print) -> None:
    """回复房三选一（浄化-30侵蚀 / 休憩+30%HP / 変換）——按 pick_heal 选卡+確定。"""
    from .abyss_plan import pick_heal
    tree = device.ui_tree(max_nodes=12000)
    verb = pick_heal(led)
    kw = {"purify": "浄化", "rest": "休憩", "convert": "変換"}[verb]
    if not _click_text_center(device, tree, kw, log=log):
        raise RuntimeError(f"回复房未找到选项『{kw}』")
    time.sleep(0.6)
    tree2 = device.ui_tree(max_nodes=12000)
    if not _click_text_center(device, tree2, "確定", log=log):
        device.click_ui(639, 651)
        log("  点击 確定(兜底坐标)")
    if verb == "purify":
        led.erosion = max(0, led.erosion - 30)


def _resolve_event(device, led: AbyssLedger, brain, log=print) -> None:
    """事件房：选项效果标签按正则解析（机械化文案），兜底 LLM；pick_event 拍板。"""
    from .abyss_plan import pick_event
    tree = device.ui_tree(max_nodes=12000)
    # 选项卡 = TitleText（标题）+ 同级 Text（效果标签），同 x 邻域归组
    titles = [n for n in _walk_all(tree) if n["name"] == "TitleText" and n.get("text")]
    texts = [n for n in _walk_all(tree) if n["name"] == "Text" and n.get("text")]
    options = []
    for t in titles:
        if t["name"] != "TitleText" or not t.get("screen"):
            continue
        s0, s1 = t["screen"][0], t["screen"][2]   # 标题框即卡宽（防跨卡串文）
        desc = ""
        for x in texts:
            if not x.get("screen"):
                continue
            xx = (x["screen"][0] + x["screen"][2]) / 2
            if s0 - 20 <= xx <= s1 + 20 and x["screen"][1] >= t["screen"][1]:
                desc += x["text"]
        if desc:
            options.append({"title": t["text"], "desc": desc, "screen": t["screen"]})
    if not options:
        raise RuntimeError("事件房未找到选项卡")

    def parse(o):
        d = o["desc"]
        locked = "選択できません" in d or "選択できません" in o["title"]
        m = re.search(r"浸食率\s*(\d+)\s*上昇", d)
        ec = int(m.group(1)) if m else 0
        m = re.search(r"浸食率\s*(\d+)\s*減少|浸食率\s*減少\s*(\d+)", d)
        eg = int(m.group(1) or m.group(2) or 0) if m else 0
        m = re.search(r"HP.*?(\d+)%\s*減少", d)
        hp = int(m.group(1)) if m else 0
        m = re.search(r"(\d+)\s*個消費", d)
        coin = int(m.group(1)) if m else 0
        return {"hp_cost": hp, "erosion_cost": ec, "erosion_gain": eg,
                "coin_cost": coin, "locked": locked,
                "code_gain": "コード" in d and "獲得" in d,
                "item_gain": "アイテム" in d and "選択" in d}

    parsed = [parse(o) for o in options]
    open_opts = [(i, p) for i, p in enumerate(parsed) if not p["locked"]]
    if not open_opts:      # 全部锁定：选代价最小的凑合过（事件强制选择）
        i = min(range(len(options)),
                key=lambda k: (parsed[k]["hp_cost"], parsed[k]["erosion_cost"]))
    else:
        i = max((k for k, p in enumerate(parsed) if not p["locked"]),
                key=lambda k: event_score(parsed[k], led))
    o = options[i]
    log(f"  事件『{o['title']}』效果={o['desc'][:40]!r}"
        + ("（锁定，备选）" if parsed[i]["locked"] else ""))
    if not _click_text_center(device, tree, o["title"], log=log):
        raise RuntimeError("事件选项点击失败")
    time.sleep(0.6)
    tree2 = device.ui_tree(max_nodes=12000)
    if not _click_text_center(device, tree2, "確定", log=log):
        device.click_ui(639, 651)
        log("  点击 確定(兜底坐标)")
    p = parse(o)
    led.hp_lost_pct += p["hp_cost"]
    led.erosion = max(0, led.erosion - p["erosion_gain"] + p["erosion_cost"])
    led.coins = max(0, led.coins - p["coin_cost"])
    if p["hp_cost"] == 0 and (p["erosion_cost"] or p["erosion_gain"]):
        led.hp_lost_pct += 0


def _close_shop(device, log=print) -> None:
    """商店按 X 走人（用户确认）；X 非 Button 时射线探右上角。"""
    tree = device.ui_tree(max_nodes=8000)
    for suffix in ("Button_Close", "Popup_Close"):
        try:
            device.click_by_path(suffix)
            log(f"  商店 {suffix}")
            return
        except Exception:
            continue
    for x, y in ((1214, 40), (1015, 100)):
        try:
            p = device.click_ui(x, y)
            log(f"  商店 X ({x},{y})")
            return
        except Exception:
            continue
    raise RuntimeError("商店退出方式未找到")


def _open_treasure(device, log=print) -> None:
    """宝箱房（收益垃圾）：先试 X 不开离开；被迫开则选 浸食+40（绝不 HP/钥匙）。"""
    tree = device.ui_tree(max_nodes=8000)
    for suffix in ("Button_Close", "Popup_Close"):
        try:
            device.click_by_path(suffix)
            log("  宝箱 X 离开")
            return
        except Exception:
            continue
    if _click_text_center(device, tree, "浸食", log=log):   # 浸食+40 选项
        time.sleep(0.6)
        tree2 = device.ui_tree(max_nodes=8000)
        if not _click_text_center(device, tree2, "確定", log=log):
            device.click_ui(639, 651)
        led_erosion_note = 40
        return
    for x, y in ((1015, 100), (1214, 40)):
        try:
            device.click_ui(x, y)
            log(f"  宝箱 X ({x},{y})")
            return
        except Exception:
            continue
    raise RuntimeError("宝箱房既不能 X 也没找到浸食选项——需人工看一眼")


def boss_floor_continue(device, led: AbyssLedger, settle: bool, log=print) -> None:
    """探索続行確認：settle=False → 続行する→倍率(核验1倍)→使用→安全箱キャンセル；
    settle=True → 帰還する（就此结算，run 结束）。"""
    tree = device.ui_tree(max_nodes=12000)
    kw = "帰還する" if settle else "続行する"
    if not _click_text_center(device, tree, kw, log=log):
        raise RuntimeError(f"续行界面未找到『{kw}』")
    if settle:
        return
    time.sleep(1.5)
    # ゲットキー消費弹窗：核验倍率显示 1 倍再点使用
    tree = device.ui_tree(max_nodes=12000)
    texts = [n.get("text") or "" for n in _walk_all(tree)]
    has_popup = any("ゲットキー" in t for t in texts)
    mults = [t for t in texts if re.fullmatch(r"[123]\s*倍", t.strip())]
    if has_popup and any(not t.strip().startswith("1") for t in mults):
        raise RuntimeError(f"倍率不是 1 倍（{mults}）——中止续行，需人工核验")
    if not _click_text_center(device, tree, "使用", log=log):
        raise RuntimeError("倍率弹窗未找到『使用』")
    led.getkeys = max(0, led.getkeys - 1)
    time.sleep(1.5)
    # 安全箱 → キャンセル
    for attempt in range(6):
        time.sleep(1.2)
        tree = device.ui_tree(max_nodes=8000)
        if _click_text_center(device, tree, "キャンセル", log=log):
            return
    raise RuntimeError("安全箱キャンセル未出现")


def reconcile(device, led: AbyssLedger, log=print) -> dict:
    """HUD 对账（HUD 为准回填账本）。返回差值。"""
    from .abyss_ui import read_hud
    hud = read_hud(device)
    diff = {}
    for k, v in hud.items():
        old = getattr(led, k, None)
        if old is not None and old != v:
            diff[k] = (old, v)
        setattr(led, k, v)
    if diff:
        log(f"  [对账] HUD 回填: {diff}")
    return diff


def _wait_map_stable(device, led: AbyssLedger | None = None, log=print) -> None:
    """过渡动画期读数会跳（甚至 999999999 占位），连续两次 HUD 层数一致才算稳；
    若长时间停在非地图场景，用结算步进拉回。"""
    from .abyss_ui import read_hud
    last = None
    stuck = 0
    for _ in range(16):
        scene = _scene(device)
        if scene != "Nether":
            stuck += 1
            if stuck >= 3:
                _result_step(device, led, None, log=log)
                stuck = 0
            time.sleep(1.5)
            last = None
            continue
        stuck = 0
        try:
            hud = read_hud(device)
        except Exception:
            time.sleep(1.5)
            last = None
            continue
        if last is not None and hud.get("floor") == last.get("floor"):
            return
        last = hud
        time.sleep(1.5)


def _overlay_present(tree) -> bool:
    """结算/事件结果类浮层（会挡住候选读取，须先排空）。"""
    for n in _walk_all(tree):
        t = n.get("text") or ""
        if "確認して次へ" in t or "報酬獲得" in t or "QUEST CLEAR" in t \
                or "アビスコードを獲得" in t:
            return True
    return False


def _press_recenter(device, log=print) -> None:
    """現在地へ：镜头归位到当前房间（屏外候选随镜头回中进入视口）。"""
    tree = device.ui_tree(max_nodes=12000)
    for n in _walk_all(tree):
        if n["name"] == "Button_Reset" and "button" in n:
            device.click_by_path(n["button"]["path"])
            time.sleep(1.5)
            return
    log("  [recenter] 未找到 現在地へ 按钮")


# ---- 入场流（NetherTop → 深渊地图，实测 2026-08-30） --------------------------------


def _tap_text(device, keyword: str, log=print, tree: dict | None = None,
              tries: int = 3) -> bool:
    for _ in range(tries):
        t = tree or device.ui_tree(max_nodes=30000)
        if _click_text_center(device, t, keyword, log=log):
            return True
        time.sleep(1.5)
        tree = None
    return False


def enter_run(device, start_floor: int, log=print) -> None:
    """从 NetherTop 入口一路点到深渊地图；已在地图（Nether）中则跳过。"""
    time.sleep(1.5)
    scene = _scene(device)
    if scene == "Nether":
        log("[入场] 已在深渊地图中，跳过入场")
        return
    if scene != "NetherTop":
        raise RuntimeError(f"请先把游戏停在深渊入口页（NetherTop），当前场景: {scene or '未知'}")
    device.click_by_path(
        "/NetherTop/UICanvas/RootUI/UIGroup/Scene_NetherTop/Button_GateStart/Button_Start")
    log("[入场] 探索開始")
    time.sleep(2.0)
    # 地点选择：点目标层检查点（如 20F）→ 確定
    if not _tap_text(device, f"{start_floor}F", log=log):
        raise RuntimeError(f"地点选择页未找到检查点 {start_floor}F")
    time.sleep(0.6)
    if not _tap_text(device, "確定", log=log):
        raise RuntimeError("地点选择页未找到 確定")
    log(f"[入场] 检查点 {start_floor}F 確定")
    time.sleep(2.0)
    # 编成页：出撃
    if not _tap_text(device, "出撃", log=log):
        raise RuntimeError("编成页未找到 出撃")
    log("[入场] 出撃")
    time.sleep(2.0)
    # ゲットキー消費弹窗：核验 1 倍 → 使用
    tree = device.ui_tree(max_nodes=12000)
    texts = [n.get("text") or "" for n in _walk_all(tree)]
    mults = [t.strip() for t in texts if re.fullmatch(r"[123]\s*倍", t.strip())]
    if mults and any(not m.startswith("1") for m in mults):
        raise RuntimeError(f"倍率非 1 倍: {mults}——需人工核验")
    if not _tap_text(device, "使用", log=log):
        raise RuntimeError("倍率弹窗未找到 使用")
    log("[入场] 倍率 1 倍 使用")
    time.sleep(2.0)
    # 安全箱 → キャンセル
    for _ in range(6):
        if _tap_text(device, "キャンセル", log=log):
            log("[入场] 安全箱 キャンセル")
            break
        time.sleep(1.5)
    time.sleep(2.0)


def run_to_floor(device, led: AbyssLedger, brain=None, log=print,
                 max_rooms: int = 6) -> dict:
    """监督式主循环：推进房间直到 max_rooms 或到达 target_floor 的续行点结算。"""
    from .abyss_ui import read_candidates
    rooms = 0
    while rooms < max_rooms:
        _wait_map_stable(device, led=led, log=log)
        # 2) 续行界面（boss 层打完后出现；文本在截断线外，须 30000 全量）
        tree = device.ui_tree(max_nodes=30000)
        if _gate_present(tree):
            settle = led.floor >= led.target_floor
            log(f"  续行界面：{'帰還结算' if settle else '买票续行'}")
            boss_floor_continue(device, led, settle=settle, log=log)
            if settle:
                return {"status": "settled", "floor": led.floor, "rooms": rooms}
            time.sleep(2.0)
            continue
        # 2.5) 遗留结算/事件结果浮层（挡候选，先排空）
        if _overlay_present(tree):
            _result_step(device, led, brain, log=log, tree=tree)
            time.sleep(1.5)
            continue
        # 3) 对账 + 候选（事件结算后房间解锁有延迟；无 MapCanvas=过渡期，重试）
        reconcile(device, led, log=log)
        cands = []
        for _ in range(4):
            try:
                cands = read_candidates(device, current_floor=led.floor, log=log)
            except RuntimeError as e:
                log(f"  [read] {e}")
                cands = []
            if cands:
                break
            time.sleep(3.0)
        if not cands:
            return {"status": "no_candidates", "floor": led.floor, "rooms": rooms}
        room = pick_room(cands, led)
        if not room.visible:
            # 屏外房 Invoke 可能无反应：歸位镜头后重选屏内最优
            log(f"  最优 {room.type} 在屏外，現在地へ 归位后重选")
            _press_recenter(device, log=log)
            cands = read_candidates(device, current_floor=led.floor, log=log) or cands
            vis = [c for c in cands if c.visible]
            if vis:
                room = pick_room(vis, led)
        log(f"[{rooms + 1}] {led.floor}F 侵蚀{led.erosion} → {room.type} @ ({room.x},{room.y})")
        enter_room(device, room, log=log)
        resolve_room(device, room, led, brain, log=log)
        rooms += 1
        time.sleep(1.2)
    return {"status": "rooms_exhausted", "floor": led.floor, "rooms": rooms}
