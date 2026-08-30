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

from .abyss_plan import AbyssLedger, Candidate, pick_code, pick_room, ticket_decision
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


def enter_room(device, room: Candidate) -> None:
    device.click(room.x, room.y)
    device.wait_settled(device.screenshot())


def resolve_room(device, room: Candidate, led: AbyssLedger, brain, log=print) -> None:
    """按类型解决房间并更新账本。战斗类=等自动战斗+结算流（含代码弹窗）。
    层数统一在这里 +1（进房即推进；事件内置的战斗不再多加）。"""
    if room.type in ("battle", "elite", "boss"):
        led.hp_lost_pct = 0                    # 战斗回满，HP 预算清零
        resolve_battle(device, led, brain, log=log)
    elif room.type == "heal":
        _choose_overlay_option(device, pick_heal_index(led), log)
        led.erosion = max(0, led.erosion - 30)
    elif room.type == "event":
        _resolve_event(device, led, brain, log)
    elif room.type == "treasure":
        _open_treasure(device, log)
    elif room.type == "shop":
        _close_shop(device, log)
    led.floor += 1


# ---- 战斗/结算流（实测 2026-08-30） ------------------------------------------

# 深渊代码名称 → 颜色（impact黄/rush红/safe蓝/risk紫）。
# 来源：拿码后结算页明写「XXコード系統の恩恵ゲージ…」——自证后入册；
# 未知名走 LLM 视觉定色（大方块=颜色种类，小菱形=效果种类，用户纠正 2026-08-30）。
CODE_COLORS = {
    "疫壳": "risk",
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
        if _scene(device) == "Nether":
            log("  回到地图")
            return
        _result_step(device, led, brain, log=log)
        time.sleep(1.5)
    raise RuntimeError("结算流超时未回地图")


def _result_step(device, led: AbyssLedger, brain, log=print) -> bool:
    """结算流的单步：代码弹窗 > 结算按钮 > 射线连点。返回是否有动作。"""
    tree = device.ui_tree(max_nodes=12000)
    def walk(n):
        yield n
        for c in n.get("children", []):
            yield from walk(c)
    # 1) 深渊代码三选一弹窗
    for c0 in tree["canvases"]:
        for n in walk(c0):
            if n["name"].startswith("Popup_AbyssCodeSelect"):
                _handle_code_popup(device, n, led, brain, log=log)
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


def _handle_code_popup(device, popup, led: AbyssLedger, brain, log=print) -> None:
    """深渊代码三选一：读选项→（注册表/LLM）定色→规划器择取→选+确定。"""
    def walk(n):
        yield n
        for c in n.get("children", []):
            yield from walk(c)
    options = []
    for n in walk(popup):
        m = re.match(r"Code_(\d)$", n["name"])
        if not m:
            continue
        texts = [(x["name"], (x.get("text") or "").strip()) for x in walk(n)]
        name = next((t for nm, t in texts if nm == "AbyssCodeName" and t), None)
        desc = next((t for nm, t in texts if nm == "Text" and len(t) > 12), None)
        pw = None
        in_power = False
        for nm, t in texts:
            if nm == "TextTitle" and "戦力" in t:
                in_power = True
            elif in_power and nm == "Value" and t:
                pw = t
                break
        options.append({"idx": int(m.group(1)), "name": name, "desc": desc, "power": pw,
                        "screen": n.get("screen"),
                        "color": CODE_COLORS.get(name) if name else None})
    options.sort(key=lambda o: o["idx"])
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


def pick_heal_index(led: AbyssLedger) -> int:
    """浄化/休憩/変換 → 选项下标（覆盖层三张卡片从左到右 0/1/2）。"""
    from .abyss_plan import pick_heal
    return {"purify": 0, "rest": 1, "convert": 2}[pick_heal(led)]


def _run_auto_battle(device, log=print) -> None:
    raise NotImplementedError("战斗序列待接线：AUTO/倍速锚点 + wait_stable + 结算收取")


def _choose_overlay_option(device, index: int, log=print) -> None:
    """覆盖层（回復/イベント/宝箱）选第 index 张卡 + 確定。选项卡位置待实测标定。"""
    raise NotImplementedError("覆盖层选项卡坐标待实测标定")


def _resolve_event(device, led, brain, log=print) -> None:
    """LLM 读事件选项（效果标签）→ abyss_plan.pick_event → 选择。"""
    raise NotImplementedError("事件弹窗 LLM 读取 prompt 待实机标定")


def _open_treasure(device, log=print) -> None:
    """被迫进宝箱：先试 X（不开离开，待实测），必须选则浸食+40（第三张卡）。"""
    raise NotImplementedError("开箱 X 语义待实测")


def _close_shop(device, log=print) -> None:
    """商店按 X 走人（用户确认）。"""
    raise NotImplementedError("商店 X 坐标待实测标定")


def boss_floor_continue(device, log=print) -> None:
    """探索続行確認 → 続行する → 倍率弹窗（核验 1 倍）→ 使用 → 安全箱 → キャンセル。"""
    for name in ("btn_continue.png", "btn_use.png", "btn_safecase_cancel.png"):
        _anchor(name)  # 缺素材即报错
    raise NotImplementedError("Boss 续行四连锚点坐标待实测标定")


def reconcile(device, led: AbyssLedger, brain, log=print) -> None:
    """HUD 对账：OCR/LLM 读 浸食率/层数/钥匙 → 与账本核对，失配挂起（教学模式通道）。"""
    raise NotImplementedError("HUD 数字识别待实机标定")
