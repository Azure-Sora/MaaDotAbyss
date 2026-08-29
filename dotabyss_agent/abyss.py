"""深渊自动刷装备控制器（docs/research/12）。

三权分立：识读=锚点模板（tasks/flows/anchors/abyss/）+ LLM 兜底；
规划=abyss_plan 纯函数（已单测）；执行=device（click/swipe/wait_stable）。

候选制读图：箭头/光圈标记当前可进入房间（已实测确认），免拓扑免全图拼接；
候选被屏幕边缘裁切时定向拖半屏补读（swipe 灵敏度 0.72，松手不弹回）。

实机联调待办（锚点素材齐 + 游戏在深渊页时可调）：
- HUD 数字（侵蚀/层数/钥匙）识别 → 账本对账
- 战斗序列（AUTO/倍速/结算收取）与日常任务共用，接线即可
- buff 弹窗/事件弹窗的 LLM 结构化读取 prompt
"""
import time
from pathlib import Path

import cv2
import numpy as np

from .abyss_plan import AbyssLedger, Candidate, pick_room, ticket_decision
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


def read_candidates(device, log=print) -> list[Candidate]:
    """识别当前屏幕上的候选房间：箭头定位 + 就近标签分类。

    箭头标记在候选房间上方，命中箭头后在其下方 30~140px 带内找最近的房间标签。
    找不到标签的箭头按未知类型（battle 兜底权重）处理。
    """
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
    """按类型解决房间并更新账本。战斗类=开自动挂机；其余=锚点+LLM 兜底。"""
    if room.type in ("battle", "elite", "boss"):
        _run_auto_battle(device, log)          # 与日常任务共用 AUTO/倍速锚点（接线 TODO）
        led.hp_lost_pct = 0                    # 战斗回满，HP 预算清零
        led.floor += 1
        return
    if room.type == "heal":
        _choose_overlay_option(device, pick_heal_index(led), log)
        led.erosion = max(0, led.erosion - 30)
    elif room.type == "event":
        _resolve_event(device, led, brain, log)
    elif room.type == "treasure":
        _open_treasure(device, log)
    elif room.type == "shop":
        _close_shop(device, log)
    led.floor += 1


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
