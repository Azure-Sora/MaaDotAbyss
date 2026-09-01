"""程序化连打宏（docs/research/14）：ui_tree 场景状态机 + 战斗/结算/弹窗通用段。

只被 routines.py 的三个 sweep 使用。铁律：
- 一律 click_by_path（精确路径直触 onClick，零穿透；Footer 的 Gacha/R18 按钮
  永远不会出现在这些路径上）
- skip_page 仅用于无按钮结算页（点 (0,0) 全屏接管层，与深渊 _result_step 同款）
- 消费红线：消費/回復/購入句式的确认弹窗一律点キャンセル
"""
import math
import re
import time

BATTLE_SCENES = {"ExplorationBattle", "DisasterBattle"}
RESULT_SCENES = {"ExploreResult", "DisasterResult"}

# 结算路上绝不主动点的按钮名（铁律：绝不碰撤退/返回主页之外的可疑项）
FORBIDDEN_BTN = ("PullOut", "Retreat")


class TransitionTimeout(RuntimeError):
    """点击后的 Unity Transition 层未在时限内恢复空闲。"""


def observe_buttons(device, canvas=None, suffix="", contains="", text="",
                    max_rows: int = 40) -> tuple[str, list[str]]:
    """observe 动作后端：读树 → 紧凑按钮表（LLM 拿路径的按需通道）。

    返回 (场景名, 行列表, 总匹配数)，行形如 "✓ 完整路径｜可见文本"。
    过滤参数全可省，但按钮多时必须过滤（全场景 ~110 条，截断到 max_rows）。
    collect_buttons 已排除 FORBIDDEN_BTN（撤退类），禁区路径还有 agent 层黑名单兜底。
    """
    tree = device.ui_tree(max_nodes=30000)
    scene_name = str(tree.get("scene", ""))
    rows = []
    for b in collect_buttons(tree, canvas=canvas):
        p = str(b["path"])
        if suffix and not p.endswith(suffix):
            continue
        if contains and contains not in p:
            continue
        t = str(b["text"])
        if text and text not in t:
            continue
        rows.append(f"{'✓' if b['interactable'] else '✗'} {p}｜{t}")
    return scene_name, rows[:max_rows], len(rows)


def scene(device) -> str:
    try:
        return str(device.ui_tree(max_nodes=10).get("scene", ""))
    except Exception:
        return ""


def walk(node, anc_active=True):
    """深度遍历，yield (node, eff_active)——eff_active = 祖先链 activeSelf 全真。

    占位/隐藏节点的 button.interactable 不可信（势力任务未开放卡的
    ButtonChallenge 仍报 True），"是否真的在界面上"只能看 eff_active。
    """
    a = anc_active and bool(node.get("active", True))
    yield node, a
    for c in node.get("children", []):
        yield from walk(c, a)


def collect_buttons(tree, suffix=None, canvas=None):
    """收集 eff-active 的按钮 → [{"path","text","interactable"}]。

    text 取按钮子树里最短的可见 TMP 文本（按钮名语义在子节点，如 Button_Confirm
    下的 <出撃>）。suffix: 只保留 path 以之结尾的；canvas: 只遍历同名画布。
    """
    out = []
    for cv in tree.get("canvases", []):
        if canvas and cv.get("name") != canvas:
            continue
        for n, a in walk(cv):
            b = n.get("button")
            if not b or not a:
                continue
            p = str(b.get("path", ""))
            if suffix and not p.endswith(suffix):
                continue
            if any(k in p for k in FORBIDDEN_BTN):
                continue
            texts = []
            for m, ma in walk(n):
                if not ma:
                    continue
                t = m.get("text")
                if t and len(str(t)) < 30:
                    texts.append(str(t))
            out.append({
                "path": p,
                "interactable": bool(b.get("interactable")),
                "text": min(texts, key=len) if texts else "",
            })
    return out


def collect_texts(tree, canvas=None):
    """收集 eff-active 的 (node, text)。"""
    out = []
    for cv in tree.get("canvases", []):
        if canvas and cv.get("name") != canvas:
            continue
        for n, a in walk(cv):
            t = n.get("text")
            if a and t:
                out.append((n, str(t)))
    return out


def find_btn(buttons, name_suffix=None, text=None):
    """按路径后缀（优先）或按钮可见文本找按钮。"""
    if name_suffix:
        for b in buttons:
            if b["path"].endswith(name_suffix):
                return b
    if text:
        for b in buttons:
            if b["text"] == text:
                return b
    return None


def click_path(device, path) -> bool:
    """安全点击 UI 路径，并把点击后的 Transition 完整周期当作 barrier。

    旧实现只负责发 click，调用方经常在转场层延迟激活前就开始找/点下一颗按钮，
    会打断 CommonLoad 并留下永久 NOW LOADING。这里统一收口，任何 routine 的
    click_by_path 都不能越过仍活跃或刚要启动的转场。
    """
    try:
        # 点击前也取一个短连续空闲窗口，堵住“检查瞬间 idle、下一帧刚好激活”
        # 的最后一道竞态；程序连打宁可多等半秒，不拿整次游戏重启冒险。
        if not wait_transition_done(device, initial=0.5):
            raise TransitionTimeout(f"点击前转场未结束: {path}")
        device.click_by_path(path)
    except TransitionTimeout:
        raise
    except Exception:
        return False
    if not wait_transition_done(device):
        raise TransitionTimeout(f"点击后转场未结束: {path}")
    return True


def wait_scene(device, names: set[str], timeout: float, poll: float = 2.0) -> str | None:
    """轮询场景名直到进入 names 之一；超时返回 None。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        sc = scene(device)
        if sc in names:
            return sc
        time.sleep(poll)
    return None


def transition_busy(device) -> bool:
    """转场 loading 是否在播：Transition canvas 平时仅 2 个 eff-active 节点
    （Transition+TransitionService），转场时整组激活（实测 ~20，持续 1-2s）。
    注意场景名在转场中段就会切换，画面 diff 也可能很小——不能靠它们判断转场。"""
    if not hasattr(device, "ui_tree"):
        return False
    t = device.ui_tree(canvas="Transition", max_nodes=300)
    n = 0
    for cv in t.get("canvases", []):
        for _, a in walk(cv):
            if a:
                n += 1
                if n > 5:
                    return True
    return False


def wait_transition_done(device, timeout: float = 15.0, poll: float = 0.25,
                         initial: float = 1.5, quiet: float = 0.5) -> bool:
    """观察一次可能延迟启动的转场，直到连续空闲后才放行下一次点击。

    旧版只在固定睡 0.5s 后查一次：Transition 若在 0.5s 之后才激活，就会被当成
    “没有转场”立即放行。这里即使暂时未看到 busy，也会覆盖完整启动宽限期；一旦
    看到 busy，则必须再取得连续 ``quiet`` 秒空闲。按轮询次数实现，假设备把 sleep
    替换为空操作时测试也不会忙等真实时钟。
    """
    if not hasattr(device, "ui_tree"):
        return True
    poll = max(0.05, float(poll))
    max_polls = max(1, math.ceil(max(0.0, float(timeout)) / poll))
    grace_polls = max(0, math.ceil(max(0.0, float(initial)) / poll))
    quiet_polls = max(1, math.ceil(max(0.0, float(quiet)) / poll))
    busy_seen = False
    idle_streak = 0
    for sample in range(1, max_polls + 1):
        if transition_busy(device):
            busy_seen = True
            idle_streak = 0
        else:
            idle_streak += 1
            grace_done = busy_seen or sample >= grace_polls
            if grace_done and idle_streak >= quiet_polls:
                return True
        if sample < max_polls:
            time.sleep(poll)
    return False


def popup_cancel_consume(tree) -> str | None:
    """消费红线：发现 消費/回復/購入 确认弹窗 → 返回其キャンセル按钮路径。

    弹窗一律挂 Front canvas（PopupService），只查它避免大树截断漏检；
    按钮取文本为 キャンセル/いいえ 的那颗。
    """
    joined = [t for _, t in collect_texts(tree, canvas="Front")]
    blob = "\n".join(joined)
    if not re.search(r"消費して|回復させますか|購入しますか|復させます", blob):
        return None
    for b in collect_buttons(tree, canvas="Front"):
        if b["text"] in ("キャンセル", "いいえ"):
            return b["path"]
    return None


def settle_step(tree) -> str | None:
    """结算流单步决策，返回要点的按钮路径，None=无按钮页（调用方 skip）。

    弹窗类在 Front canvas，结算翻页在场景自身 canvas（UICanvas）。
    """
    front = collect_buttons(tree, canvas="Front")
    # 1) 消费红线最优先（在调用方，这里不管）
    # 2) 分解确认（战斗后装备满）：点 分解する
    b = find_btn(front, text="分解する")
    if b:
        return b["path"]
    # 3) 首通/报酬全屏弹窗：关闭
    b = find_btn(front, name_suffix="FullScreenCloseButton")
    if b:
        return b["path"]
    # 4) 结算翻页主按钮（次へ/OK 等）
    ui = collect_buttons(tree, canvas="UICanvas")
    for suffix in ("ButtonSet/Layout/Button_Next", "Button_ToExploration",
                   "Button_ToNextQuest"):
        b = find_btn(ui, name_suffix=suffix)
        if b and b["interactable"]:
            return b["path"]
    # 5) 通用弹窗关闭 X
    b = find_btn(front, name_suffix="Popup_Close")
    if b:
        return b["path"]
    return None


def battle_and_return(device, home_scene: str, log=print, timeout: float = 300.0,
                      frame_cb=None, leave_timeout: float = 25.0) -> bool:
    """战斗宏：从点完最终出撃确认后调用——等场景离开 home → 战斗 → 结算
    → 清弹窗 → 回 home_scene（home 上有残留弹窗时先清完再算回家）。"""
    t0 = time.time()
    left_home = False
    while time.time() - t0 < timeout:
        if frame_cb is not None:
            try:
                frame_cb(device.screenshot())
            except Exception:
                pass
        sc = scene(device)
        if not left_home:
            if sc != home_scene:
                left_home = True
            elif time.time() - t0 > leave_timeout:
                log(f"  [battle] 出撃后场景未离开 {home_scene}——出撃未生效")
                return False
            else:
                time.sleep(1.0)
                continue
        if sc == home_scene:
            # 转场动画必须播完才能点下一场：点击打断 CommonLoad 会 NOW LOAD 卡屏
            if not wait_transition_done(device):
                raise TransitionTimeout(f"回到 {home_scene} 后 Transition 未结束")
            device.wait_settled(device.screenshot(), max_wait=4.0)
            front = device.ui_tree(canvas="Front", max_nodes=2000)
            act = settle_step(front)
            if act:   # ClearRewards/分解报酬 等残留弹窗盖在列表上
                log("  [battle] 清理残留弹窗")
                click_path(device, act)
                time.sleep(1.2)
                continue
            log(f"  [battle] 回到 {home_scene}（{time.time() - t0:.0f}s）")
            return True
        if sc in BATTLE_SCENES:
            time.sleep(3.0)
            continue
        if sc in RESULT_SCENES:
            # 大 max_nodes：canvas 遍历顺序不可控（FindObjectsOfType 无序），
            # 截断会丢掉排在后面的 UICanvas（次へ按钮）/Front（弹窗）画布
            tree = device.ui_tree(max_nodes=30000)
            cancel = popup_cancel_consume(tree)
            if cancel:
                log("  [battle] 消费确认弹窗 → キャンセル（红线）")
                click_path(device, cancel)
                time.sleep(1.2)
                continue
            act = settle_step(tree)
            if act:
                click_path(device, act)
            else:
                try:
                    device.skip_page()
                except Exception:
                    pass
                if not wait_transition_done(device):
                    raise TransitionTimeout("结算跳页后 Transition 未结束")
            time.sleep(0.5)
            continue
        # 其它场景（转场/未知弹窗）：先查消费红线，再试清理，否则等
        front = device.ui_tree(canvas="Front", max_nodes=2000)
        cancel = popup_cancel_consume(front)
        if cancel:
            click_path(device, cancel)
            time.sleep(1.2)
            continue
        act = settle_step(front)
        if act:
            click_path(device, act)
        time.sleep(1.0)
    log(f"  [battle] 超时未回到 {home_scene}")
    return False
