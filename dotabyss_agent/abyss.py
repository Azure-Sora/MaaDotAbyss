"""深渊自动刷装备控制器（docs/research/12）。

三权分立：识读=桥 UI 直读（abyss_ui，首选）/锚点模板（MAA 兜底）+ LLM 兜底；
规划=abyss_plan 纯函数（已单测）；执行=device（桥 click/click_ui/click_by_path）。

实机已验证（2026-08-30，doc 13 §2.7）：
- 场景链 Nether(地图) → ExplorationBattle(战斗，自动跑) → ExplorarionNetherResult(结算)
- 结算流：深渊代码三选一弹窗（精英/Boss 掉落）→ 探索報酬页「確認して次へ」（非 Button，
  点文本会穿透到底下按钮，统一点屏幕左上角 (0,0) 整页跳过）→ Button_Next → 回 Nether
- HUD（层数/侵蚀/钥匙/金币）全在 UICanvas 文本节点，对账零 OCR
- 拿码后结算页明写颜色系统（"リスクコード系統の…"）——颜色注册表的自证来源

2026-09 版新增：編成出撃/ボス層続行后、ゲットキー倍率弹窗之前多一个 スキップ確認
（Popup_Confirm_NetherSkip，三选项 行わない/スキップ/レアスキップ）——固定 行わない
（跳过会把倍率锁 1 倍、少拿 10 层收益，_skip_confirm_decline）。
入场流チケット使用之后还有 編成確認（Popup_Confirm_NetherParty，深淵内では編成の
変更ができません）——固定 挑む（_party_confirm_challenge，仅入场流、续行流无此步）。

2026-09-02 新增：代码弹窗退场核验（_close_out_popup）——弹窗确认后偶发关闭动画
冻结成僵尸（不可见+確認/キャンセル/左上角全关不掉+全屏卡片锁输入，唯一解法重启
游戏），点完必须等它真消失，射线补点仍不退就报错停机；左上角跳过前留生成窗复查。

2026-09-03 新增：浮层先点自带关闭按钮再左上角（_overlay_close_btn）——拿码获得
弹窗 Popup_FullScreen_AbyssCodeReceived 的关按钮不在 (0,0) 射线上，左上角跳过两连
未命中静默返回，结算流空转 120s 超时；LLM 兜底点它自带关成功后，报酬页还要 次へ
才回图，单一动作兜底验证不过把进展白扔——兜底改多轮接力（有进展继续、没进展止损）。

2026-09-03 新增：代码主图标模板定色（_match_code_color）——同色系主图标完全相同
（图鉴四系实測），图鉴每系取一纯净竖条做模板，多尺度匹配即定色，LLM 只兜底匹配
不中的；「获得代码」页=一切入码路径的统一关口（_ack_received_page，弹窗拿码经
grant_ack_pending 去重，其余来源在此补计）；结算评价屏 CodeArea 各色代码数对账
（reconcile_code_counts）兜底回填一切漏计；run_to_floor 支持 settle_mode=takeover
（到层不帰還，停在续行界面等人工接管）。
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
    """进房：屏内房间（含贴边半露房，点击点已裁到可见区）用射线式真实点击
    （部分房间入口只挂指针事件管线，onClick.Invoke 是空操作——实测 2026-08-30）；
    全屏外房间只能按路径 Invoke。点完 3s 内场景/覆盖层必须有反应，否则换法重试一次。"""
    m = re.search(r"MapFloor_\w+?\(Clone\)", room.btn_path or "")
    room_token = m.group(0) if m else None
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
                if room_token and room_token not in p:
                    # 点中的是别的层（贴边房被边缘 HUD 按钮挡住射线等）：
                    # 本房多半没收到点击，先记一笔，后续无反应时日志可对上
                    log(f"  [enter] ray 命中 {p}（非本房 {room_token}）")
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
        tree = device.ui_tree(max_nodes=30000)
        if any(n.get("text") in ("確認", "確定") for n in _walk_all(tree)):
            return
        if _overlay_popup_open(tree):
            return   # 商店/宝箱等覆盖层已开（场景名不切，2026-09-01 实战误判"无反应"）
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
        _open_treasure(device, led, brain, log)
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


def _code_popup_present(tree) -> bool:
    """深渊代码三选一弹窗是否在场（elite/boss 常规掉落 + 战斗房稀有掉落同构）。"""
    return any(n["name"].startswith("Popup_AbyssCodeSelect") for n in _walk_all(tree))


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
    from .macros import wait_transition_done
    # 场景名在转场中段就切：此刻上一场转场可能还在播，立刻点击会打断 CommonLoad
    # 卡 LOADING（2026-09-01 实战复现），先等播完再开始点。
    if not wait_transition_done(device):
        raise RuntimeError("战斗结束转场疑似卡死（LOADING 不退）——停止点击等待人工")
    t0 = time.time()
    # 掉落弹窗挂出晚于场景切换（2026-09-01 实战：Button_Next 抢跑把弹窗拖到地图上
    # 才出现），进循环前先给一拍挂出时间
    time.sleep(1.5)
    while time.time() - t0 < 120:
        tree = device.ui_tree(max_nodes=30000)
        if _gate_present(tree):
            log("  检测到续行界面（Boss 层），交由主循环处理")
            return
        if _code_popup_present(tree):
            _handle_code_popup(device, led, brain, log=log)   # 须先于 Nether 判断：
            time.sleep(1.5)                                   # 场景名先切、弹窗后挂出
            continue
        if tree.get("scene") == "Nether":
            # 抢跑/时序错位会让弹窗与拿码浮层拖到回图后才出现（此时场景已是 Nether）：
            # 有遗留就主动推掉，全干净才返回——只查不推会在此死循环到超时（实战 2026-09-01）
            if _code_popup_present(tree) or _overlay_present(tree):
                time.sleep(1.5)
                tree = device.ui_tree(max_nodes=30000)
                if _code_popup_present(tree) or _overlay_present(tree):
                    _result_step(device, led, brain, log=log, tree=tree)
                    time.sleep(1.5)
                continue
            log("  回到地图")
            return
        _result_step(device, led, brain, log=log, tree=tree)
        time.sleep(1.5)
    raise RuntimeError("结算流超时未回地图")


# 僵尸弹窗（2026-09-02 实测定性）：代码弹窗偶发带着冻结的开场动画出生——
# 整体不可见、三卡同亮「選択中」、確認不亮；内部按钮逻辑还活着（选卡/リロール
# 有反应）但关闭链路全死（確認/キャンセル/左上角均不退场），全屏卡片还吃掉所有
# 射线点击 = 输入彻底锁死。唯一解法是重启游戏。诱因是上一个浮层跳过/弹窗关闭
# 动画进行中时下一个弹窗抢生（拿码后连掉两码时必现）。
ZOMBIE_POPUP_MSG = (
    "深渊代码弹窗变僵尸（开场动画冻结：不可见且確認/キャンセル均无法关闭，"
    "输入已被锁死）——请重启游戏后从检查点继续"
)


def _code_popup_on_screen(device) -> bool:
    return _code_popup_present(device.ui_tree(max_nodes=30000))


def _wait_popup_gone(device, timeout: float) -> bool:
    """轮询等代码弹窗从树上消失（只读不点，转场期间也安全）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.8)
        if not _code_popup_on_screen(device):
            return True
    return False


def _settle_after_popup(device) -> None:
    """弹窗退场后先等转场播完，再给一拍：拿码确认页/下一个弹窗要 1-2s 后才挂出，
    这段时间里的任何点击都可能打断动画链（卡 LOADING / 僵尸弹窗的诱因）。"""
    from .macros import wait_transition_done
    wait_transition_done(device)
    time.sleep(1.0)


def _ray_click_popup_btn(device, popup, suffix: str, log=print) -> bool:
    """射线真实点击弹窗内指定按钮（onClick.Invoke 不走交互事件层，个别状态只认射线）。"""
    def walk(n):
        yield n
        for c in n.get("children", []):
            yield from walk(c)
    for n in walk(popup):
        b = n.get("button")
        if b and b["path"].endswith(suffix) and n.get("screen"):
            cx = (n["screen"][0] + n["screen"][2]) // 2
            cy = (n["screen"][1] + n["screen"][3]) // 2
            try:
                device.click_ui(cx, cy)
                log(f"  射线点击 {suffix} @ ({cx},{cy})")
                return True
            except Exception:
                return False
    return False


def _wait_code_popup_window(device, timeout: float = 6.0, poll: float = 0.8,
                            log=print) -> bool:
    """结算按钮按下前的代码弹窗挂出窗口。弹窗挂出晚于 Button_Next 亮起（结算动画
    还在播），不等就点 = 100% 抢跑（2026-09-02 实战）：结算页被推走、弹窗拖到回图
    后才挂到地图上——地图上处理弹窗时序脆弱，正是僵尸弹窗的温床。只轻量读
    Front 画布（弹窗固定挂 Front/PopupService）。True=弹窗已挂出，交回调用方处理。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _code_popup_present(device.ui_tree(canvas="Front", max_nodes=5000)):
            return True
        time.sleep(poll)
    return False


def _close_out_popup(device, popup, log=print) -> None:
    """確認/キャンセル点完后的退场核验：弹窗必须真从树上消失。
    不退先射线补点 確定（Invoke 不走交互事件层，个别状态只认射线），再不退试
    キャンセル；两条路都关不掉 = 僵尸弹窗（2026-09-02 实测定性，见
    ZOMBIE_POPUP_MSG）——输入已被全屏卡片锁死，继续任何点击都无效甚至更乱，
    立刻抛错止损等人工重启。"""
    if _wait_popup_gone(device, 6.0):
        _settle_after_popup(device)
        return
    log("  確定后弹窗未退场，射线补点 確定")
    _ray_click_popup_btn(device, popup, "ButtonSet/Button_Confirm", log=log)
    if _wait_popup_gone(device, 5.0):
        _settle_after_popup(device)
        return
    _ray_click_popup_btn(device, popup, "ButtonSet/Button_Cancel", log=log)
    if _wait_popup_gone(device, 4.0):
        _settle_after_popup(device)
        return
    raise RuntimeError(ZOMBIE_POPUP_MSG)


def _overlay_popup_open(tree) -> bool:
    """PopupService 下是否有弹窗本体（商店/宝箱等覆盖层开着时场景名仍是 Nether）。"""
    for n in _walk_all(tree):
        b = n.get("button")
        if b and "PopupService" in b.get("path", "") and "Popup_" in b.get("path", ""):
            return True
    return False


def _overlay_close_btn(tree) -> str | None:
    """覆盖层自带关闭按钮的完整路径：shim 记忆优先，其次弹窗层级内名字含 Close 的
    interactable 按钮（Button_Close/Popup_Close/FullScreenCloseButton——拿码获得
    弹窗 Popup_FullScreen_AbyssCodeReceived 的关就是后者，2026-09-03 实战 LLM 兜底
    点它成功）。只认路径带 Popup 的：底下结算页的同名按钮 Invoke 会穿透（§14.4），
    全树裸找不安全。"""
    key = None
    for n in _walk_all(tree):
        b = n.get("button")
        if b and "/Popup_" in b.get("path", ""):
            key = b["path"].split("/Popup_")[1].split("/")[0]
            break
    if key:
        shim = _load_shims().get(key)
        if shim:
            return shim
    for n in _walk_all(tree):
        b = n.get("button")
        if b and b.get("interactable") and "close" in n["name"].lower() \
                and ("Popup" in b.get("path", "") or "FullScreen" in n["name"]):
            return b["path"]
    return None


# 覆盖层关闭解法记忆：{弹窗节点名: 完整按钮路径}。LLM 兜底成功后沉淀，下次直接重放。
SHIMS_PATH = TASKS_DIR.parent / ".local" / "abyss_shims.json"


def _load_shims() -> dict:
    try:
        return json.loads(SHIMS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_shim(key: str, path: str) -> None:
    shims = _load_shims()
    shims[key] = path
    try:
        SHIMS_PATH.parent.mkdir(parents=True, exist_ok=True)
        SHIMS_PATH.write_text(json.dumps(shims, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def _close_overlay(device, log=print) -> None:
    """关闭当前覆盖层弹窗（商店/宝箱等）：浮层自带关闭按钮（shim 记忆优先）→ 左上角。
    关闭按钮从树里拿完整路径再点（覆盖层开着时底下地图按钮全变灰，路径重试必失败）。"""
    path = _overlay_close_btn(device.ui_tree(max_nodes=30000))
    if path:
        device.click_by_path(path)
        log(f"  关闭覆盖层 → {path[-50:]}")
        return
    if _skip_overlay_by_corner(device, log=log):
        return
    raise RuntimeError("覆盖层既无关闭按钮也无法左上角跳过")


def _skip_overlay_by_corner(device, log=print) -> bool:
    """无按钮浮层的唯一安全关法：点屏幕最左上角整页跳过。这类页面（確認して次へ/
    拿码确认页）的文本不是射线目标，直接点文本或 Invoke 底层按钮都会穿透（实测
    2026-08-30/09-01）；左上角只会命中全屏接管层。click_ui 优先（全屏层未必是
    Button），click 兜底。
    跳过前留 1.2s 生成窗再复查：跳过瞬间正是下一个代码弹窗的挂出时刻，抢跳会让
    它带着冻结的开场动画出生（僵尸弹窗，ZOMBIE_POPUP_MSG）。发现弹窗即放弃跳过，
    交回调用方循环先处理弹窗。"""
    from .macros import wait_transition_done
    time.sleep(1.2)
    if _code_popup_on_screen(device):
        log("  左上角跳过前发现代码弹窗挂出，改为先处理弹窗")
        return False
    for call in (device.click_ui, device.click):
        try:
            p = call(0, 0)
        except Exception:
            continue
        if "PullOut" in p or "Retreat" in p:   # 铁律：绝不碰撤退
            raise RuntimeError(f"射线命中撤退按钮 {p}——立即停止")
        log(f"  左上角跳过 (0,0) → {p[-40:]}")
        if not wait_transition_done(device):
            raise RuntimeError("跳过转场疑似卡死（LOADING 不退）——停止点击等待人工")
        return True
    log("  左上角 (0,0) 两次尝试均未命中可点击层（浮层多半自带关闭按钮，下轮改点它）")
    return False


def _ack_received_page(device, led: AbyssLedger, brain, log=print, tree: dict | None = None) -> None:
    """「アビスコードを獲得しました」获得页=所有入码路径的统一关口（2026-09-03）：
    弹窗拿码已在 _handle_code_popup 计数（grant_ack_pending 标记本页只是其退场确认，
    消费标记不重复计数）；其余来源（直得/奖励/首通等，正是账本漏计的盲区）在此认领——
    主图标模板定色入账，不中再 LLM 视觉，仍认不出就记日志放弃（宁可漏计不瞎计，
    结算屏 CodeArea 对账会兜底回填）。非获得页零开销返回。"""
    tree = tree or device.ui_tree(max_nodes=30000)
    texts = [n.get("text") or "" for n in _walk_all(tree)]
    if not any("アビスコードを獲得" in t for t in texts):
        return
    if getattr(led, "grant_ack_pending", False):
        led.grant_ack_pending = False
        log("  拿码确认页（弹窗拿码退场确认，已计）")
        return
    color = None
    frame = device.screenshot()
    h, w = frame.shape[:2]
    m = _match_code_color(frame, (int(w * 0.2), int(h * 0.08), int(w * 0.8), int(h * 0.85)))
    if m:
        color, score = m
        log(f"  获得代码入账: {color}（模板 {score:.2f}）")
    elif brain is not None:
        crop = frame[int(h * 0.1):int(h * 0.8), int(w * 0.2):int(w * 0.8)]
        try:
            data = brain.read_json_from_image(
                crop,
                "这是游戏获得深渊代码的确认页特写。代码图标的主色代表颜色种类："
                "黄=impact、红=rush、蓝=safe、紫=risk。只输出 JSON："
                '{"color": "impact|rush|safe|risk 之一"}', scene="abyss_code")
            c = str(data.get("color", "")).lower()
            if c in ("impact", "rush", "safe", "risk"):
                color = c
                log(f"  获得代码入账: {color}（LLM 视觉）")
        except Exception as e:
            log(f"  获得页视觉定色失败: {e.__class__.__name__}")
    if color:
        led.buffs[color] = led.buffs.get(color, 0) + 1
    else:
        log("  获得代码页定色失败——暂不入账，待结算屏代码数对账回填")


def _result_step(device, led: AbyssLedger, brain, log=print, tree: dict | None = None) -> bool:
    """结算流的单步：代码弹窗 > 无按钮浮层 > 结算按钮 > 射线兜底。返回是否有动作。
    每个点击后等转场播完再返回——点太快打断 CommonLoad 会卡 LOADING（2026-09-01）。"""
    tree = tree or device.ui_tree(max_nodes=30000)
    def walk(n):
        yield n
        for c in n.get("children", []):
            yield from walk(c)
    # 1) 深渊代码三选一弹窗（elite/boss 常规 + 战斗房稀有掉落同构）
    if _code_popup_present(tree):
        _handle_code_popup(device, led, brain, log=log)
        return True
    # 2) 无按钮浮层（確認して次へ/拿码确认页 アビスコードを獲得しました 等）——必须
    #    先于结算按钮：浮层挂着时底下 Button_Next 仍 interactable，Invoke 会穿透到
    #    下层页面而浮层卡死（2026-09-01 实战；此前 12000 截断时代 Button_Next 恰好
    #    不可见，歪打正着走左上角）
    if _overlay_present(tree):
        _ack_received_page(device, led, brain, log=log, tree=tree)
        close = _overlay_close_btn(tree)
        if close:
            # 关闭瞬间可能是下一个代码弹窗的挂出时刻（僵尸弹窗诱因），留生成窗复查
            time.sleep(1.2)
            if _code_popup_on_screen(device):
                log("  关闭浮层前发现代码弹窗挂出，改为先处理弹窗")
                return False
            device.click_by_path(close)
            log(f"  关闭浮层 → {close[-50:]}")
            from .macros import wait_transition_done
            if not wait_transition_done(device):
                raise RuntimeError("浮层关闭转场疑似卡死（LOADING 不退）——停止点击等待人工")
            return True
        return _skip_overlay_by_corner(device, log=log)
    # 3) 结算按钮（真实 Button）
    for c0 in tree["canvases"]:
        for n in walk(c0):
            if "button" not in n:
                continue
            nm = n["name"]
            if nm in ("Button_Next", "Button_ToExploration", "Button_ToNextQuest") \
                    and n["button"].get("interactable"):
                # 点前留弹窗挂出窗口：掉落弹窗挂出晚于按钮亮起，抢跑=回图后才弹
                # （正确时序是 Result 内选码 → 继续 → 回图，2026-09-02 用户指认）
                if _wait_code_popup_window(device, log=log):
                    _handle_code_popup(device, led, brain, log=log)
                    return True
                device.click_by_path(n["button"]["path"])
                log(f"  点击 {nm}")
                from .macros import wait_transition_done
                if not wait_transition_done(device):
                    raise RuntimeError("结算转场疑似卡死（LOADING 不退）——停止点击等待人工")
                return True
    # 4) 射线兜底（无浮层无按钮的未知页面）
    return _skip_overlay_by_corner(device, log=log)


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
        tree = device.ui_tree(max_nodes=30000)
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
    # 点选前确认没有转场在播——CommonLoad 压着弹窗动画是僵尸弹窗的诱因之一
    from .macros import wait_transition_done
    wait_transition_done(device)
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
        _close_out_popup(device, popup, log=log)
        return
    o = options[idx]
    device.click_by_path(_popup_btn(popup, f"Code_{o['idx']}/AbyssCode_{o['idx']}"))
    time.sleep(0.8)
    device.click_by_path(_popup_btn(popup, "Button_Confirm"))
    color = o["color"] or "unknown"
    led.buffs[color] = led.buffs.get(color, 0) + 1   # buffs 懒初始化：in 判断会漏掉首码
    led.grant_ack_pending = True   # 拿码确认页即将挂出，届时只确认不重复计数
    log(f"  拿码 {o['name']}（{color}，战力 {o['power']}）")
    _close_out_popup(device, popup, log=log)   # 拿码不算数，弹窗退场才算数


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
    """未知名代码定色：主图标模板匹配（快、零 token）→ 不中的才 LLM 视觉兜底。
    同色系主图标完全相同（图鉴四系实測，docs/12 §14.11），只有右下菱形（效果）与
    边框（稀有度）因码而异——图鉴每系取一纯净竖条做模板即可代表全系。"""
    if not options:
        return
    frame = device.screenshot()
    rest = []
    for o in options:
        if not o.get("screen"):
            continue
        x0, y0, x1, y1 = o["screen"]
        w, h = x1 - x0, y1 - y0
        m = _match_code_color(frame, (x0 + int(w * 0.03), y0 + int(h * 0.03),
                                      x1 - int(w * 0.03), y0 + int(h * 0.7)))
        if m:
            color, score = m
            o["color"] = color
            if o["name"]:
                CODE_COLORS[o["name"]] = color
            log(f"  模板定色: {o['name']} → {color}（{score:.2f}）")
        else:
            rest.append(o)
    if not rest:
        return
    for o in rest:
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
                '{"color": "impact|rush|safe|risk 之一"}', scene="abyss_code")
            color = str(data.get("color", "")).lower()
            if color in ("impact", "rush", "safe", "risk"):
                o["color"] = color
                if o["name"]:
                    CODE_COLORS[o["name"]] = color
                log(f"  视觉定色: {o['name']} → {color}（已入注册表）")
        except Exception as e:
            log(f"  视觉定色失败 {o['name']}: {e.__class__.__name__}")


# 主图标模板定色：tasks/flows/anchors/abyss/code_icons/<色系>.png（图鉴收集脚本沉淀）。
CODE_ICON_DIR = ABYSS_ANCHORS / "code_icons"
MATCH_SCALE_MIN, MATCH_SCALE_MAX, MATCH_SCALE_STEP = 0.8, 2.1, 0.1
CODE_MATCH_THRESHOLD = 0.75   # 实测同系 0.90+、异系 ≤0.42（含跨渲染尺度）
_TPL_CACHE: dict[str, "np.ndarray"] = {}


def _code_templates() -> dict:
    if not _TPL_CACHE:
        for p in CODE_ICON_DIR.glob("*.png"):
            img = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
            if img is not None:
                _TPL_CACHE[p.stem] = img
    return _TPL_CACHE


def _match_code_color(frame, region) -> tuple[str, float] | None:
    """区域内多尺度模板匹配主图标 → (色系, 得分)；低于阈值返回 None。
    frame=BGR 截图，region=(x0,y0,x1,y1)。模板是图标左部的纯净竖条（无菱形无框），
    在卡片区域内搜它，几何/渲染尺度差异由多尺度吸收（弹窗/图鉴/获得页通用）。"""
    tps = _code_templates()
    if not tps:
        return None
    x0, y0, x1, y1 = region
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)[max(0, int(y0)):int(y1),
                                                   max(0, int(x0)):int(x1)]
    if gray.shape[0] < 30 or gray.shape[1] < 30:
        return None
    best_color, best_score = None, -1.0
    for color, tpl in tps.items():
        th, tw = tpl.shape
        s = MATCH_SCALE_MIN
        while s <= MATCH_SCALE_MAX + 1e-9:
            t = cv2.resize(tpl, (max(2, int(tw * s)), max(2, int(th * s))))
            if t.shape[0] < gray.shape[0] and t.shape[1] < gray.shape[1]:
                res = cv2.matchTemplate(gray, t, cv2.TM_CCOEFF_NORMED)
                score = float(res.max())
                if score > best_score:
                    best_color, best_score = color, score
            s += MATCH_SCALE_STEP
    if best_color is not None and best_score >= CODE_MATCH_THRESHOLD:
        return best_color, best_score
    return None


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
    tree = device.ui_tree(max_nodes=30000)
    verb = pick_heal(led)
    kw = {"purify": "浄化", "rest": "休憩", "convert": "変換"}[verb]
    if not _click_text_center(device, tree, kw, log=log):
        raise RuntimeError(f"回复房未找到选项『{kw}』")
    time.sleep(0.6)
    tree2 = device.ui_tree(max_nodes=30000)
    if not _click_text_center(device, tree2, "確定", log=log):
        device.click_ui(639, 651)
        log("  点击 確定(兜底坐标)")
    if verb == "purify":
        led.erosion = max(0, led.erosion - 30)


def _event_overlay_present(tree) -> bool:
    """事件覆盖层 Popup_NetherEvent 是否在场（不切场景名；可能连续多轮，31F 实测）。"""
    for n in _walk_all(tree):
        b = n.get("button")
        if b and "Popup_NetherEvent" in b.get("path", ""):
            return True
    return False


def _walk_node(n):
    yield n
    for c in n.get("children", []):
        yield from _walk_node(c)


def _is_locked_text(t: str) -> bool:
    """locked 常驻层文案（与效果行同 screen 叠加，26F/28F 勘探实测）——非效果行。"""
    return "条件を満たしていないため" in t or "選択できません" in t


def _collect_event_options(tree) -> tuple[list[dict], dict]:
    """从 UI 树收集事件选项卡，返回 (options, btn_map)。

    options: [{"title", "desc", "screen"}]（屏外装饰卡已排除）
    btn_map: title -> button info dict（selectable 判定/確定用）

    效果行收集规则（2026-09-03 重做，26F/28F 勘探 dump 实锤）：效果文本挂在选项卡
    Button 子树内；而弹窗外的 HUD/图例/活动倒计时（'MAXまで'）/角色名（'アリシア'）
    等 Text 节点遍布全树——旧版全树取 Text 按 x 邻域（±20）归组、y 无上界，把这些
    统统串进 desc（41F 事故 desc 出现 'MAXまでMAX'），真实「浸食率20上昇/HP減少」
    行反而可能因中心 x 偏出邻域被漏、代价当 0。现按卡子树收集：y 排序拼接；卡子树
    无效果行（直挂布局，42F 实测）回退 popup 子树内 x 邻域法（限定 popup + y 上界
    =卡底+60 + y 排序）。
    """
    def _subtree_desc(root) -> str:
        parts = [(c["screen"][1], c["text"])
                 for c in _walk_node(root)
                 if c.get("name") == "Text" and c.get("text")
                 and c.get("screen") and not _is_locked_text(c["text"])]
        return "".join(t for _, t in sorted(parts))

    popup = next((n for n in _walk_all(tree)
                  if n["name"].startswith("Popup_NetherEvent")), None)
    if popup is None:
        return [], {}
    popup_nodes = list(_walk_node(popup))
    cards = []      # (树节点, button dict, title, title screen)
    for n in popup_nodes:
        b = n.get("button")
        if not b:
            continue
        tnode = next((c for c in _walk_node(n)
                      if c.get("name") == "TitleText" and c.get("text")), None)
        if tnode:
            cards.append((n, b, tnode.get("text"), tnode.get("screen")))
    options, btn_map = [], {}
    for node, b, tt, tscr in cards:
        if not tscr or tscr[0] < 0:
            continue            # 屏外装饰卡（x<0）
        desc = _subtree_desc(node)
        if not desc:
            s0, s1 = tscr[0], tscr[2]
            ytop, ybot = tscr[1], tscr[3] + 60
            parts = sorted((x["screen"][1], x["text"])
                           for x in popup_nodes
                           if x.get("name") == "Text" and x.get("text")
                           and x.get("screen") and not _is_locked_text(x["text"])
                           and s0 - 20 <= (x["screen"][0] + x["screen"][2]) / 2 <= s1 + 20
                           and ytop <= x["screen"][1] <= ybot)
            desc = "".join(t for _, t in parts)
        if desc:
            options.append({"title": tt, "desc": desc, "screen": tscr})
            btn_map[tt] = b
    return options, btn_map


def _resolve_event(device, led: AbyssLedger, brain, log=print) -> None:
    """事件房：选项效果行按卡子树收集 + 正则解析（机械化文案），可疑时 LLM 复读
    交叉验证；pick_event 拍板（侵蚀安全线硬约束）。
    事件可能连续多轮（31F 实测：第一轮確定后又弹第二轮营地事件），逐轮处理直到
    回图或触发内置战斗。"""
    from .abyss_plan import effective_erosion_cost, parse_event_desc, pick_event

    def one_round(tree) -> None:
        def _event_btn(name):
            for n in _walk_all(device.ui_tree(max_nodes=30000)):
                b = n.get("button")
                if b and n["name"] == name and "NetherEvent" in b.get("path", ""):
                    return b
            return None

        # 覆盖层入场动画期选项卡/按钮未挂全（42F 实测空表被误判死局），等就绪
        for _ in range(8):
            probe_opts, _ = _collect_event_options(tree)
            if len([o for o in probe_opts if o["screen"][0] >= 0]) >= 2:
                break
            time.sleep(1.0)
            tree = device.ui_tree(max_nodes=30000)
        options, btn_map = _collect_event_options(tree)
        if not options:
            raise RuntimeError("事件房未找到选项卡")

        parsed = [parse_event_desc(o["desc"]) for o in options]
        # LLM 交叉验证（2026-09-03 加固）：效果文案变体多（rich 标签/省略写法），
        # 树读出现 unknown、或高侵蚀区存在带侵蚀代价的选项时，截图让模型逐卡复读。
        # 合并原则=代价取较大、收益取 LLM 值——一切从保守（暴毙血本无归）。
        risky = (any(p["erosion_unknown"] for p in parsed)
                 or (led.erosion >= led.erosion_safe - 20
                     and any(effective_erosion_cost(p) > 0 for p in parsed)))
        if brain is not None and risky:
            try:
                frame = device.screenshot()
                titles_line = "、".join(o["title"] for o in options)
                data = brain.read_json_from_image(
                    frame,
                    "这是深渊事件弹窗截图，选项卡标题从左到右：" + titles_line + "。"
                    "逐个读出每个选项卡内标注的效果行（绿=收益/橙=代价）。只输出 JSON："
                    '{"options": [{"title": "标题", "hp_cost": 0, "erosion_cost": 0, '
                    '"erosion_gain": 0, "hp_gain": 0, "coin_cost": 0, '
                    '"code_gain": false, "item_gain": false}]}；数值没有就填 0，'
                    "布尔没有就填 false，不要漏掉任何一张卡。",
                    scene="abyss_event")
                by_title = {str(x.get("title", "")).strip(): x
                            for x in data.get("options", []) if isinstance(x, dict)}
                n_hit = 0
                for o, p in zip(options, parsed):
                    j = by_title.get(o["title"])
                    if not j:
                        continue
                    n_hit += 1
                    p["erosion_cost"] = max(p["erosion_cost"], int(j.get("erosion_cost") or 0))
                    p["hp_cost"] = max(p["hp_cost"], int(j.get("hp_cost") or 0))
                    p["coin_cost"] = max(p["coin_cost"], int(j.get("coin_cost") or 0))
                    p["erosion_gain"] = int(j.get("erosion_gain") or 0) or p["erosion_gain"]
                    p["hp_gain"] = int(j.get("hp_gain") or 0) or p["hp_gain"]
                    p["code_gain"] = bool(p["code_gain"] or j.get("code_gain"))
                    p["item_gain"] = bool(p["item_gain"] or j.get("item_gain"))
                    if p["erosion_unknown"] and (p["erosion_cost"] or p["erosion_gain"]):
                        p["erosion_unknown"] = False
                log(f"  [事件] LLM 复读 {n_hit}/{len(options)} 卡，代价取并集较大值")
            except Exception as e:
                log(f"  [事件] LLM 复读失败（{e.__class__.__name__}）——按树读保守值决策")

        def _p_summary(p: dict) -> str:
            seg = []
            if p["hp_cost"]:
                seg.append(f"HP-{p['hp_cost']}%")
            if p["hp_gain"]:
                seg.append(f"HP+{p['hp_gain']}%")
            if p["erosion_cost"]:
                seg.append(f"侵+{p['erosion_cost']}")
            if p["erosion_gain"]:
                seg.append(f"侵-{p['erosion_gain']}")
            if p["erosion_unknown"]:
                seg.append("侵?未知")
            if p["coin_cost"]:
                seg.append(f"币-{p['coin_cost']}")
            if p["code_gain"]:
                seg.append("码")
            if p["item_gain"]:
                seg.append("物")
            return "+".join(seg) or "无效果"

        log("  [事件解析] " + "；".join(
            f"『{o['title']}』{_p_summary(p)}" for o, p in zip(options, parsed)))

        # 选项可选性 = 选项卡按钮 interactable（42F 实测）：文案「条件を満たしていない/
        # 選択できません」不代表不可选——按钮活即可选，確定会亮、效果照常发生。事件是
        # 必答题：进房后不能横走/后退，X 关闭会自动重弹、地图锁死到选完为止，唯一出路
        # 是完成一个可选选项。文案 locked 判定弃用。屏外装饰卡（x<0）排除。
        for o in options:
            b = btn_map.get(o["title"])
            o["selectable"] = bool(b and b.get("interactable")
                                   and o["screen"] and o["screen"][0] >= 0)
        order = sorted((k for k, o in enumerate(options) if o["selectable"]),
                       key=lambda k: event_score(parsed[k], led), reverse=True)
        if not order:
            raise RuntimeError("事件所有选项按钮均不可选（疑似死局）——需人工")
        confirmed = False
        for k in order:
            o = options[k]
            log(f"  事件『{o['title']}』效果={parsed[k]!r}")
            if not _click_text_center(device, tree, o["title"], log=log):
                continue
            time.sleep(0.6)
            cf = _event_btn("Button_Confirm")
            if cf and cf.get("interactable"):
                device.click_by_path(cf["path"])
                log("  点击 確定")
                confirmed = True
                break
            log("  確定未亮，换下一个可选选项")
        if not confirmed:
            raise RuntimeError("事件没有任何可確定的选项——需人工")
        p = parsed[k]   # k 即确认选项在 options/parsed 中的下标
        led.hp_lost_pct += p["hp_cost"]
        led.erosion = max(0, led.erosion - p["erosion_gain"] + effective_erosion_cost(p))
        led.coins = max(0, led.coins - p["coin_cost"])
        # 事件可能触发内置特殊战斗（野兽类，实测 2026-08-30）：等它打完并走完整结算流
        # （QUEST CLEAR→代码弹窗/報酬→回图）；也可能直接弹下一轮事件（31F 实测）或干净
        # 回图。确定后转场启动有延迟窗，场景短暂仍是 Nether——前 ~10s 不下"无战斗"结论。
        saw_other = False
        for i in range(100):
            time.sleep(3.0)
            sc = _scene(device)
            if sc == "ExplorarionNetherResult":
                resolve_battle(device, led, brain, log=log)   # 已在 Result → 直接进结算流
                return
            if sc != "Nether":
                saw_other = True    # 战斗/转场进行中
                continue
            tree = device.ui_tree(max_nodes=30000)
            if _event_overlay_present(tree):
                break               # 下一轮事件，回外层继续
            if _overlay_present(tree):
                _result_step(device, led, brain, log=log, tree=tree)   # 事件结果页(確認して次へ)
                continue
            if saw_other or i > 3:
                return              # 打完回图；或无战斗直接回图
        else:
            raise RuntimeError("事件房确定后 5 分钟未出结算也未回图——需人工")

    for _round in range(6):
        tree = device.ui_tree(max_nodes=30000)
        if not _event_overlay_present(tree):
            return
        one_round(tree)


def _close_shop(device, log=print) -> None:
    """商店按 X 走人（用户确认）——走通用覆盖层关闭（shim 记忆→关闭按钮→左上角）。"""
    _close_overlay(device, log=log)


def _collect_treasure_options(tree) -> tuple[list[dict], dict]:
    """宝箱弹窗三卡（Popup_NetherTreasure/Anim/Contnet/Select/{Key,Hp,Abyss}，Contnet
    是游戏内拼写）→ (options, btn_map)。kind=key/hp/erosion，代价数字从卡内文本提取
    （'HP-40%'/'浸食+40'/Value'-1'），读不到按 9999 保守（只会沦为最后兜底）。"""
    popup = next((n for n in _walk_all(tree)
                  if n["name"].startswith("Popup_NetherTreasure")), None)
    if popup is None:
        return [], {}
    kind_map = {"key": "key", "hp": "hp", "abyss": "erosion"}
    sel = next((n for n in _walk_node(popup)
                if n.get("name") == "Select"
                and any(c.get("name", "").lower() in kind_map
                        for c in n.get("children", []))), None)
    if sel is None:
        return [], {}
    options, btn_map = [], {}
    for node in sel.get("children", []):
        kind = kind_map.get(node.get("name", "").lower())
        btn = next((x.get("button") for x in _walk_node(node) if x.get("button")), None)
        if kind is None or btn is None:
            continue
        blob = "".join(x.get("text") or "" for x in _walk_node(node) if x.get("text"))
        m = re.search(r"(\d+)", blob)
        n = int(m.group(1)) if m else 9999
        options.append({"kind": kind,
                        "key_cost": n if kind == "key" else 0,
                        "hp_cost": n if kind == "hp" else 0,
                        "erosion_cost": n if kind == "erosion" else 0,
                        "interactable": bool(btn.get("interactable"))})
        btn_map[kind] = btn["path"]
    return options, btn_map


def _open_treasure(device, led, brain, log=print) -> None:
    """宝箱房（2026-09-03 实测改约）：开箱方式三选一是必答题——旧版先点 X 离开不可行，
    X 关掉弹窗但房不完成，Front 层候选全锁且弹窗不重弹（候选 0 卡死，现场复现）。
    流程：等弹窗选项卡挂全 → 规划器选卡 → 確定 → 排空开箱结算回图。"""
    from .abyss_plan import pick_treasure
    options, btn_map = [], {}
    for _ in range(10):                       # 入场动画：弹窗/卡片延迟挂出
        tree = device.ui_tree(max_nodes=30000)
        options, btn_map = _collect_treasure_options(tree)
        if options:
            break
        time.sleep(1.0)
    if not options:
        raise RuntimeError("宝箱弹窗未挂出选项卡——需人工")
    log("  [宝箱] " + "；".join(
        f"{o['kind']}-{o['key_cost'] + o['hp_cost'] + o['erosion_cost']}"
        + ("" if o["interactable"] else "×不可选") for o in options))
    k = pick_treasure(options, led)
    o = options[k]
    device.click_by_path(btn_map[o["kind"]])
    log(f"  宝箱选卡 → {o['kind']}")
    time.sleep(0.8)
    cf = None
    for _ in range(6):                        # 选中后確定才亮（动画延迟）
        tree = device.ui_tree(max_nodes=30000)
        cf = next((n["button"] for n in _walk_all(tree)
                   if n.get("button")
                   and n["button"]["path"].endswith(
                       "Popup_NetherTreasure(Clone)/Anim/ButtonSet/Button_Confirm")),
                  None)
        if cf and cf.get("interactable"):
            break
        time.sleep(1.0)
    if not (cf and cf.get("interactable")):
        raise RuntimeError("宝箱確定未亮——选卡未生效，需人工")
    device.click_by_path(cf["path"])
    log("  宝箱確定")
    if o["kind"] == "hp":
        led.hp_lost_pct += o["hp_cost"]
    elif o["kind"] == "erosion":
        led.erosion += o["erosion_cost"]      # HUD 对账会以实测回填
    else:
        led.keys = max(0, led.keys - o["key_cost"])   # 宝箱钥匙（HUD 对账回填）
    # 开箱结算：宝箱浮层（確認して次へ 全屏层）/获得页逐个排空，直到干净地图
    clean = 0
    for _ in range(30):
        time.sleep(2.0)
        if _scene(device) != "Nether":
            clean = 0
            continue
        tree = device.ui_tree(max_nodes=30000)
        if any(n["name"].startswith("Popup_NetherTreasure") for n in _walk_all(tree)):
            clean = 0
            full = next((n["button"] for n in _walk_all(tree)
                         if n.get("button") and n["name"] == "FullScreenButton"
                         and n["button"].get("interactable")
                         and "Popup_NetherTreasure" in n["button"].get("path", "")), None)
            if full:
                device.click_by_path(full["path"])
                log("  宝箱確認して次へ")
            continue
        if _code_popup_present(tree) or _overlay_present(tree) or _overlay_popup_open(tree):
            _result_step(device, led, brain, log=log, tree=tree)
            clean = 0
            continue
        clean += 1
        if clean >= 2:
            return
    raise RuntimeError("宝箱確定后 1 分钟未回干净地图——需人工")


def _reopen_locked_room(device, led, brain, tree, log=print) -> bool:
    """地图全锁且无任何弹窗=当前房未完成（2026-09-03 宝箱 X 卡死实测：Front 层候选
    全灰、弹窗不重弹，唯一活口是 Back 层当前房按钮仍 interactable）。点它重拉弹窗；
    宝箱弹窗出现则就地解决，其他弹窗交回调用方循环。返回是否采取了动作。"""
    btns = [n["button"] for n in _walk_all(tree)
            if n.get("button") and n["button"].get("interactable")
            and "MapFloor_" in n["button"].get("path", "")]
    if len(btns) != 1:
        return False
    device.click_by_path(btns[0]["path"])
    log(f"  [自救] 地图全锁——重进当前房 {btns[0]['path'][-42:]}")
    time.sleep(2.0)
    tree2 = device.ui_tree(max_nodes=30000)
    if any(n["name"].startswith("Popup_NetherTreasure") for n in _walk_all(tree2)):
        _open_treasure(device, led, brain, log=log)
    return True


def _multipliers(tree) -> list[str]:
    """倍率弹窗的倍率值列表（"1"/"2"/"3"…）。旧版 UI=『1倍』整文本；2026-09 版=
    数值 Value 节点与『倍』单位节点相邻成对（Value 在左，包围盒略重叠）。读不到 []。"""
    texts = [(n.get("text") or "").strip() for n in _walk_all(tree)]
    old = [t for t in texts if re.fullmatch(r"[123]\s*倍", t)]
    if old:
        return [m[:-1].strip() for m in old]
    vals = []
    for b in (n for n in _walk_all(tree)
              if (n.get("text") or "").strip() == "倍" and n.get("screen")):
        bs = b["screen"]
        near = [n for n in _walk_all(tree)
                if n.get("name") == "Value" and n.get("screen")
                and abs(n["screen"][3] - bs[3]) < 20       # 与『倍』同行
                and -25 <= bs[0] - n["screen"][2] < 45]    # 紧贴其左
        if near:
            vals.append((near[0].get("text") or "").strip())
    return vals


def _skip_confirm_decline(device, log=print, tries: int = 6) -> bool:
    """スキップ確認弹窗（2026-09 版新增，倍率弹窗之前出现）：固定 行わない（不跳过）。
    三选项 行わない/スキップ/レアスキップ = Button_Cancel/Skip/RareSkip；跳过会把
    ゲートキー倍率锁 1 倍少拿收益，故永远 行わない。弹窗不在场返回 False。"""
    for _ in range(tries):
        tree = device.ui_tree(max_nodes=30000)
        btn = next((n["button"]["path"] for n in _walk_all(tree)
                    if "button" in n
                    and "Popup_Confirm_NetherSkip" in n["button"]["path"]
                    and n["button"]["path"].endswith("Button_Cancel")), None)
        if btn is not None:
            device.click_by_path(btn)
            log("  スキップ確認 → 行わない（不跳过）")
            return True
        if any((n.get("text") or "") == "スキップ確認" for n in _walk_all(tree)):
            time.sleep(1.0)   # 标题已挂出、按钮树未就绪（入场动画）：等一拍重读
            continue
        return False
    raise RuntimeError("スキップ確認弹窗可见但始终没等到 行わない 按钮")


def boss_floor_continue(device, led: AbyssLedger, settle: bool, log=print) -> None:
    """探索続行確認：settle=False → 続行する→[スキップ確認→行わない]→倍率(核验1倍)
    →使用→安全箱キャンセル；settle=True → 帰還する（就此结算，run 结束）。"""
    tree = device.ui_tree(max_nodes=30000)
    kw = "帰還する" if settle else "続行する"
    if not _click_text_center(device, tree, kw, log=log):
        raise RuntimeError(f"续行界面未找到『{kw}』")
    if settle:
        return
    time.sleep(1.5)
    # スキップ確認弹窗（2026-09 新增）：固定 行わない（不跳过）
    if _skip_confirm_decline(device, log=log):
        time.sleep(1.5)
    # ゲットキー消費弹窗（2026-09 起文案为 ゲートキー）：核验倍率显示 1 倍再点使用
    tree = device.ui_tree(max_nodes=30000)
    texts = [n.get("text") or "" for n in _walk_all(tree)]
    has_popup = any(("ゲットキー" in t or "ゲートキー" in t) for t in texts)
    mults = _multipliers(tree)
    if has_popup and any(m != "1" for m in mults):
        raise RuntimeError(f"倍率不是 1 倍（{mults}）——中止续行，需人工核验")
    if not _click_text_center(device, tree, "使用", log=log):
        raise RuntimeError("倍率弹窗未找到『使用』")
    # 续关消耗的是継続券，与 HUD 宝箱钥匙（led.keys）无关——不在此扣账
    # （旧版误扣宝箱钥匙，2026-09-03 用户指认两者是两种东西）
    time.sleep(1.5)
    # 安全箱 → キャンセル
    for attempt in range(6):
        time.sleep(1.2)
        tree = device.ui_tree(max_nodes=30000)
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


# 结算评价页 Contents2/CodeList/CodeArea：各色代码数（H5/Value，游戏真值）。
_CODE_COUNT_NODES = {"ImpactCode": "impact", "RushCode": "rush",
                     "ReduceCode": "safe", "IncreaseCode": "risk"}


def read_code_counts(tree) -> dict:
    """结算评价页的各色持码数 {色系: 数量}。页面不在场/数值未挂出返回 {}。
    只认 H5 小节下的 Value——RushCode 子树里的 gauge Value（恩恵ゲージ点数）不混入。"""
    out: dict = {}
    def walk(n):
        yield n
        for c in n.get("children", []):
            yield from walk(c)
    for n in _walk_all(tree):
        color = _CODE_COUNT_NODES.get(n["name"])
        if not color or color in out:
            continue
        for x in walk(n):
            if x["name"] == "H5":
                for v in walk(x):
                    if v["name"] == "Value" and v.get("text"):
                        t = str(v["text"]).strip().replace(",", "")
                        if t.isdigit():
                            out[color] = int(t)
                        break
                break
    return out


def reconcile_code_counts(device, led: AbyssLedger, log=print,
                          timeout: float = 20.0) -> None:
    """结算屏各色代码数对账（游戏真值回填，只增不减）：中途一切漏计入账的路径
    （直得/奖励等）在此统一补齐。评价页未出现（超时）则静默跳过。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        counts = read_code_counts(device.ui_tree(max_nodes=30000))
        if counts:
            diff = {}
            for c, v in counts.items():
                old = led.buffs.get(c, 0)
                if v > old:
                    diff[c] = (old, v)
                    led.buffs[c] = v
            if diff:
                log(f"  [对账] 代码数回填: {diff}（有漏计入码，已按游戏真值补齐）")
            else:
                log(f"  [对账] 代码数与游戏一致: {counts}")
            return
        time.sleep(2.0)


def _wait_map_stable(device, led: AbyssLedger | None = None, brain=None, log=print) -> None:
    """过渡动画期读数会跳（甚至 999999999 占位），连续两次 HUD 层数一致才算稳；
    若长时间停在非地图场景，用结算步进拉回（结算期间弹出的代码弹窗也在此处理）。"""
    from .abyss_ui import read_hud
    last = None
    stuck = 0
    for _ in range(16):
        scene = _scene(device)
        if scene != "Nether":
            stuck += 1
            if stuck >= 3:
                _result_step(device, led, brain, log=log)
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
    tree = device.ui_tree(max_nodes=30000)
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
    # 编成页一到就可能先弹 スキップ確認（2026-09，挡住出撃按钮）：先 行わない 再 出撃
    _skip_confirm_decline(device, log=log)
    # 编成页：出撃
    if not _tap_text(device, "出撃", log=log):
        raise RuntimeError("编成页未找到 出撃")
    log("[入场] 出撃")
    time.sleep(2.0)
    # スキップ確認弹窗（2026-09 新增，倍率弹窗之前）：固定 行わない（不跳过）
    if _skip_confirm_decline(device, log=log):
        time.sleep(2.0)
    # ゲットキー消費弹窗：核验 1 倍 → 使用
    tree = device.ui_tree(max_nodes=30000)
    mults = _multipliers(tree)
    if mults and any(m != "1" for m in mults):
        raise RuntimeError(f"倍率非 1 倍: {mults}——需人工核验")
    if not _tap_text(device, "使用", log=log):
        raise RuntimeError("倍率弹窗未找到 使用")
    log("[入场] 倍率 1 倍 使用")
    time.sleep(2.0)
    # 編成確認弹窗（仅入场流有，续行流无）：深淵内では編成の変更ができません → 挑む；
    # 入场到此直进地图，没有安全箱环节（用户确认 2026-09-01，安全箱只在续行流）
    if _party_confirm_challenge(device, log=log):
        time.sleep(2.0)
    time.sleep(2.0)   # 入渊转场


def _party_confirm_challenge(device, log=print, tries: int = 6) -> bool:
    """編成確認弹窗（入场流专属，チケット使用之后；续行流无此步，此前代码漏处理）：
    深淵内では編成の変更ができません → 固定 挑む（Button_Confirm，キャンセル会取消入场）。
    弹窗内嵌 ゲートキー消費効果 预览，点前再核验一次 1 倍。不在场返回 False。"""
    for _ in range(tries):
        tree = device.ui_tree(max_nodes=30000)
        btn = next((n["button"]["path"] for n in _walk_all(tree)
                    if "button" in n
                    and "Popup_Confirm_NetherParty" in n["button"]["path"]
                    and n["button"]["path"].endswith("Button_Confirm")), None)
        if btn is not None:
            mults = _multipliers(tree)
            if any(m != "1" for m in mults):
                raise RuntimeError(f"編成確認弹窗倍率非 1 倍（{mults}）——需人工核验")
            device.click_by_path(btn)
            log("  編成確認 → 挑む（以当前编成入渊）")
            return True
        if any("編成の変更ができません" in (n.get("text") or "") for n in _walk_all(tree)):
            time.sleep(1.0)   # 正文已挂出、按钮树未就绪（入场动画）：等一拍重读
            continue
        return False
    raise RuntimeError("編成確認弹窗可见但始终没等到 挑む 按钮")


def _llm_rescue(device, brain, situation: str, log=print, max_rounds: int = 4) -> bool:
    """房间处理失败时的 LLM 兜底：按钮表交模型拍板一个动作，执行并验证回到干净地图。

    多轮接力（2026-09-03 实战）：一步往往到不了地图——拿码获得弹窗
    Popup_FullScreen_AbyssCodeReceived 关掉后还停在探索報酬页，得再点 次へ 才回图。
    上轮执行后未回图但画面确有进展（场景/按钮表变了）→ 继续下一轮；画面纹丝不动
    =点不动，止损。单一动作版会在这里把「已关掉浮窗」的进展整轮白扔。

    「失败叫模型、成功沉淀」闭环：解法写入 .local/abyss_shims.json（覆盖层名→关闭
    按钮路径），下次 _close_overlay/_result_step 直接重放——自动修复以参数级沉淀
    实现，不自动改 .py 代码（不可审不可回滚）。brain=None 时跳过。"""
    if brain is None:
        return False
    from .macros import observe_buttons, wait_transition_done
    popup_click = None        # 序列里第一个 Popup_ 关闭点击（成功后沉淀为 shim）
    prev_sig: tuple | None = None
    for round_no in range(1, max_rounds + 1):
        try:
            scene, rows, total = observe_buttons(device, max_rows=30)
        except Exception as e:
            log(f"  [兜底] 读按钮表失败: {e.__class__.__name__}")
            return False
        sig = (scene, tuple(rows))
        if sig == prev_sig:
            log(f"  [兜底] 第{round_no}轮画面与上轮相同（点不动）——止损")
            return False
        prev_sig = sig
        if total > len(rows):
            rows = rows + [f"(其余 {total - len(rows)} 条略，多为地图房间/道路)"]
        tail = f"（第{round_no}轮，上一步已执行但还没回图，继续朝目标推进）" if round_no > 1 else ""
        prompt = (
            f"深渊自动化在步骤「{situation}」后卡住，当前场景 {scene}{tail}。\n"
            "可点击按钮（✓可点/✗灰，格式 路径｜可见文本）：\n" + "\n".join(rows) +
            "\n目标：关掉当前覆盖层/界面，回到深渊地图。只输出 JSON："
            '{"analysis":"一句话判断","action":"click_path|corner","path":"要点的完整按钮路径,corner 时省略"}\n'
            "corner=点屏幕左上角跳过无按钮浮层。绝不选含 撤退/PullOut/Retreat/"
            "TutorialFocus/Mask/FrameButton 的按钮（遮罩类点了无效甚至跳去别的界面）。"
        )
        try:
            data = brain.ask_json("你是游戏自动化脚本的兜底决策器，只输出一个 JSON 对象。", prompt, scene="abyss_rescue")
        except Exception as e:
            log(f"  [兜底] 模型调用失败: {e.__class__.__name__}")
            return False
        action = str(data.get("action", ""))
        path = str(data.get("path", ""))
        log(f"  [兜底] 第{round_no}轮: {str(data.get('analysis', '?'))[:60]} → {action}")
        try:
            if action == "click_path":
                if any(k in path for k in ("PullOut", "Retreat", "TutorialFocus",
                                           "Mask", "FrameButton")):
                    raise RuntimeError(f"模型选了禁区按钮 {path}")
                device.click_by_path(path)
                if "Popup_" in path and popup_click is None:
                    popup_click = path
            elif action == "corner":
                if not _skip_overlay_by_corner(device, log=log):
                    return False
            else:
                return False
        except Exception as e:
            log(f"  [兜底] 执行失败: {e}")
            return False
        time.sleep(1.5)
        wait_transition_done(device)
        tree = device.ui_tree(max_nodes=30000)
        ok = (tree.get("scene") == "Nether" and not _overlay_popup_open(tree)
              and not _overlay_present(tree) and not _code_popup_present(tree))
        if ok:
            if popup_click:
                _save_shim(popup_click.split("/Popup_")[1].split("/")[0], popup_click)   # 沉淀解法供重放
            extra = "，解法已沉淀" if popup_click else ""
            log(f"  [兜底] 已回到干净地图（{round_no} 轮接力{extra}）")
            return True
        log(f"  [兜底] 第{round_no}轮执行后未回到干净地图")
    return False


def run_to_floor(device, led: AbyssLedger, brain=None, log=print,
                 max_rooms: int = 6, settle_mode: str = "settle") -> dict:
    """监督式主循环：推进房间直到 max_rooms 或到达 target_floor 的续行点结算。
    settle_mode="takeover" 时到层不帰還，停在续行界面等人工接管（status=takeover）。"""
    from .abyss_ui import read_candidates
    rooms = 0
    while rooms < max_rooms:
        _wait_map_stable(device, led=led, brain=brain, log=log)
        # 2) 续行界面（boss 层打完后出现；文本在截断线外，须 30000 全量）
        tree = device.ui_tree(max_nodes=30000)
        if _gate_present(tree):
            settle = led.floor >= led.target_floor
            if settle and settle_mode == "takeover":
                log(f"  续行界面：已到目标层 {led.floor}F——按设定停在续行界面，等人工接管")
                return {"status": "takeover", "floor": led.floor, "rooms": rooms}
            log(f"  续行界面：{'帰還结算' if settle else '买票续行'}")
            boss_floor_continue(device, led, settle=settle, log=log)
            if settle:
                reconcile_code_counts(device, led, log=log)   # 结算屏各色代码数对账
                return {"status": "settled", "floor": led.floor, "rooms": rooms}
            time.sleep(2.0)
            continue
        # 2.5) 遗留结算/事件结果浮层 + 代码弹窗（挡候选，先排空）
        if _overlay_present(tree) or _code_popup_present(tree):
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
            # 空读兜底：遗留弹窗（代码弹窗/结算浮层/事件覆盖层）会挡住候选读取
            tree = device.ui_tree(max_nodes=30000)
            if _code_popup_present(tree) or _overlay_present(tree):
                log("  [兜底] 地图被遗留弹窗遮挡，排空后重读")
                _result_step(device, led, brain, log=log, tree=tree)
            elif _event_overlay_present(tree):
                log("  [兜底] 事件覆盖层遗留，重新处理")
                _resolve_event(device, led, brain, log=log)
            elif _reopen_locked_room(device, led, brain, tree, log=log):
                log("  [兜底] 当前房未完成导致锁图，已重进解决")
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
        try:
            enter_room(device, room, log=log)
            resolve_room(device, room, led, brain, log=log)
        except Exception as e:
            if ZOMBIE_POPUP_MSG in str(e):
                raise   # 输入已被僵尸弹窗锁死，自救点击全无效，直接停等人工
            try:
                tree = device.ui_tree(max_nodes=30000)
                map_clean = (tree.get("scene") == "Nether"
                             and not _overlay_popup_open(tree)
                             and not _overlay_present(tree)
                             and not _code_popup_present(tree)
                             and not _event_overlay_present(tree))
            except Exception:
                map_clean = False   # 树读不到按不干净处理，交原自救流程止损
            if map_clean:
                # 地图本来就干净：进房失败不是「有浮层挡路」，自救的目标（关浮层回图）
                # 不成立，放它出去只会在按钮表里乱点（52F 实战：见 TutorialFocus 遮罩
                # 就 Invoke，把界面点回 NetherTop 主页）——直接上报等重试。
                log(f"  [异常] {e}——地图干净无浮层可关，自救无对象")
                raise
            log(f"  [异常] {e}——尝试 LLM 自救")
            if not _llm_rescue(device, brain, f"进入/解决 {room.type} 房间", log=log):
                raise
        rooms += 1
        time.sleep(1.2)
    return {"status": "rooms_exhausted", "floor": led.floor, "rooms": rooms}
