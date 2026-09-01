"""auto 动作的三个程序化清剿 routine（docs/research/14）。

调用契约：LLM 已把画面导航到对应入口页；routine 第一步校验场景名，
不对立即返回 wrong_scene（绝不自己摸路）。全程 click_by_path（不穿透），
结算/弹窗走 macros 通用段。返回：
{"status": done|partial|wrong_scene, "cleared": 场数, "detail": 说明}
partial = 中途卡住/可疑，剩余情况已写入 detail，由 LLM 兜底决断。
"""
import re
import time
from dataclasses import dataclass, field

from .daily_routines import claim_idle_reward
from .execution import ExecutionResult, ExecutionStatus, Routine
from .macros import (
    TransitionTimeout, battle_and_return, click_path, collect_buttons,
    collect_texts, find_btn, popup_cancel_consume, scene, walk,
)
from .sweep_dsl import generic_sweep, load_saved_routines, save_program

COUNTRIES = ("Milesgard", "Peldion", "Eldorana", "Coalition", "Luxnova")

SUSPECT_LIMIT = 2   # 连续 N 场"计数未递减"视为异常，交还 LLM


def _routine_result(status: ExecutionStatus, cleared: int, detail: str) -> dict:
    return {
        "status": status.value,
        "cleared": int(cleared),
        "detail": str(detail),
    }


@dataclass(slots=True)
class SweepSession:
    """三类连打共享的生命周期状态，不包含具体页面知识。"""

    timeout: float
    started: float = field(default_factory=time.time)
    cleared: int = 0
    suspect: int = 0

    def checkpoint(self, stop_event=None) -> dict | None:
        if _check_stop(stop_event):
            return self.partial("用户停止")
        if time.time() - self.started >= self.timeout:
            return self.partial("总超时")
        return None

    def done(self, detail: str) -> dict:
        return _routine_result(ExecutionStatus.DONE, self.cleared, detail)

    def partial(self, detail: str) -> dict:
        return _routine_result(ExecutionStatus.PARTIAL, self.cleared, detail)

    def mark_success(self) -> None:
        self.cleared += 1
        self.suspect = 0

    def mark_cleared(self) -> None:
        self.cleared += 1

    def mark_suspect(self, detail: str) -> dict | None:
        self.suspect += 1
        return self.partial(detail) if self.suspect >= SUSPECT_LIMIT else None


# ---- 通用小件 -----------------------------------------------------------

def _popup_btn_wait(device, suffix: str, timeout: float = 8.0) -> str | None:
    """轮询等待某弹窗按钮出现且可点，返回路径。

    等待期间处理打断弹窗：消费红线（キャンセル）、自动分解确认（分解する=
    授权行为）、通行证里程结算（纯通知，关闭）——2026-08-31 实测这些会插在
    出撃确认链中间，不处理会白白等超时。
    """
    t0 = time.time()
    while time.time() - t0 < timeout:
        tree = device.ui_tree(max_nodes=30000)
        front = collect_buttons(tree, canvas="Front")
        cancel = popup_cancel_consume(tree)
        if cancel:
            click_path(device, cancel)
            time.sleep(1.2)
            continue
        b = find_btn(front, text="分解する")
        if b:
            click_path(device, b["path"])
            time.sleep(1.2)
            continue
        b = find_btn(front, name_suffix="Popup_MileageResult(Clone)/Box/Popup_Close")
        if b:
            click_path(device, b["path"])
            time.sleep(1.2)
            continue
        for b in collect_buttons(tree):
            if b["path"].endswith(suffix) and b["interactable"]:
                return b["path"]
        time.sleep(0.8)
    return None


def _check_stop(stop_event) -> bool:
    return stop_event is not None and stop_event.is_set()


def _at_home(device, home: str, tries: int = 3, gap: float = 1.5) -> bool:
    """入口场景确认：桥瞬时超时会把 scene() 读成空串，多读几次防误判。"""
    for i in range(tries):
        if scene(device) == home:
            return True
        if i < tries - 1:
            time.sleep(gap)
    return False


# ---- 势力任务 ------------------------------------------------------------

def _forces_cards(device) -> list[dict]:
    """五张卡状态：country/open/remaining/btn/title。"""
    tree = device.ui_tree(max_nodes=30000)
    out = []
    for cv in tree.get("canvases", []):
        if cv.get("name") != "UICanvas":
            continue
        for n, a in walk(cv):
            name = str(n.get("name", ""))
            if not name.startswith("List_Country_") or not a:
                continue
            country = name.split("List_Country_", 1)[1]
            card = {"country": country, "open": False, "remaining": 0,
                    "btn": None, "title": ""}
            for m, ma in walk(n):
                mn = str(m.get("name", ""))
                if mn == "Open" and ma:
                    card["open"] = True
                t = m.get("text") if ma else None
                if t and mn == "TextTitle":
                    card["title"] = str(t)
                b = m.get("button")
                if b and ma and str(b.get("path", "")).endswith("Open/ButtonChallenge"):
                    card["btn"] = b["path"]
                    for x, xa in walk(m):
                        xt = x.get("text") if xa else None
                        if not xt:
                            continue
                        # 计数可能带富文本：<color=..>4</color>/3 → 剥标签再取
                        plain = re.sub(r"<[^>]+>", "", str(xt))
                        mt = re.search(r"(\d+)\s*/\s*3", plain)
                        if mt:
                            card["remaining"] = int(mt.group(1))
                            break
            out.append(card)
    return out


def forces_sweep(device, log=print, stop_event=None, frame_cb=None,
                 timeout: float = 1200.0) -> dict:
    home = "UnionRequest"
    if not _at_home(device, home):
        return _routine_result(
            ExecutionStatus.WRONG_SCENE, 0, f"不在 {home}（势力任务列表），请先导航"
        )
    session = SweepSession(timeout)
    while True:
        halted = session.checkpoint(stop_event)
        if halted:
            return halted
        cards = [c for c in _forces_cards(device)
                 if c["open"] and c["btn"] and c["remaining"] > 0]
        if not cards:
            left = [(c["country"], c["remaining"]) for c in _forces_cards(device) if c["open"]]
            return session.done("开放关卡全部打完 " + (str(left) if left else ""))
        card = cards[0]
        log(f"[forces] {card['country']} {card['title']} 剩余 {card['remaining']}")
        if not click_path(device, card["btn"]):
            return session.partial(f"点 {card['country']} 挑戦失败")
        p = _popup_btn_wait(
            device, "Popup_UnionRequestDetail(Clone)/Box/Contents/Popup_ButtonSet2/Button_Confirm")
        if not p:
            # 弹窗没开（回数可能在列表页显示延迟）→ 回读状态重判
            failed = session.mark_suspect("挑戦弹窗连续未出现")
            log(f"[forces] 详情弹窗未出现（suspect {session.suspect}）")
            if failed:
                return failed
            time.sleep(2.0)
            continue
        # 周回(Button_SkipMode)禁用——拿不全奖励，只走出撃单刷
        click_path(device, p)
        sortie = _popup_btn_wait(
            device, "Popup_Confirm_Sortie(Clone)/Box/Contents/Popup_ButtonSet2/Button_Confirm", 8)
        if sortie:
            click_path(device, sortie)
        if not battle_and_return(device, home, log=log, timeout=240, frame_cb=frame_cb):
            return session.partial(f"{card['country']} 战斗/结算超时未回列表")
        session.mark_cleared()
        after = next((c for c in _forces_cards(device) if c["country"] == card["country"]), None)
        if after and after["remaining"] < card["remaining"]:
            session.suspect = 0
            log(f"[forces] ✓ {card['country']} 剩余 {card['remaining']}→{after['remaining']}")
        else:
            # 结算后卡片数据可能有服务器刷新延迟，等一下重读再定责
            time.sleep(2.0)
            after = next((c for c in _forces_cards(device) if c["country"] == card["country"]), None)
            if after and after["remaining"] < card["remaining"]:
                session.suspect = 0
                log(f"[forces] ✓ {card['country']} 剩余 {card['remaining']}→{after['remaining']}（延迟刷新）")
                continue
            failed = session.mark_suspect(
                f"{card['country']} 连续计数未递减，请 LLM 检查"
            )
            log(f"[forces] ? {card['country']} 计数未递减（suspect {session.suspect}）"
                "——可能败北或界面延迟")
            if failed:
                return failed


# ---- 迎击战 ---------------------------------------------------------------

def _disaster_bosses(device) -> list[dict]:
    """三个小 boss：area/btn/done（Label 文本消失+Anim/None = 已讨伐）。"""
    tree = device.ui_tree(max_nodes=30000)
    out = []
    for cv in tree.get("canvases", []):
        if cv.get("name") != "UICanvas":
            continue
        for n, a in walk(cv):
            p = ""  # path 逐层拼太贵，用子树按钮反查
            name = str(n.get("name", ""))
            if not (name == "Disaster" and a):
                continue
            btns = [b for b in collect_buttons({"canvases": [n]})
                    if b["path"].endswith("Disaster/RootUI")]
            if not btns:
                continue
            texts = [t for _, t in collect_texts({"canvases": [n]})]
            out.append({"btn": btns[0]["path"], "done": not texts,
                        "label": " ".join(texts)[:60]})
    return out


def disaster_sweep(device, log=print, stop_event=None, frame_cb=None,
                   timeout: float = 900.0) -> dict:
    home = "DisasterTop"
    if not _at_home(device, home):
        return _routine_result(
            ExecutionStatus.WRONG_SCENE, 0, f"不在 {home}（迎击战页），请先导航"
        )
    session = SweepSession(timeout)
    while True:
        halted = session.checkpoint(stop_event)
        if halted:
            return halted
        bosses = [b for b in _disaster_bosses(device) if not b["done"] and "/Sp" not in b["btn"]]
        if not bosses:
            return session.done("三个小 boss 全部击退")
        boss = bosses[0]
        log(f"[disaster] 出击 {boss['btn'].split('/Area')[-1].split('/')[0]}（{boss['label'][:30]}）")
        if not click_path(device, boss["btn"]):
            return session.partial("点 boss 失败")
        p = _popup_btn_wait(
            # 2026-08-31 游戏更新：详情弹窗按钮组 ButtonSet2→ButtonSet3（キャンセル/スキップ/出撃）
            device, "Popup_QuestDetail_Disaster(Clone)/Box/Contents/Popup_ButtonSet3/Button_Confirm")
        if not p:
            failed = session.mark_suspect(
                "boss 详情弹窗连续未出现（可能已讨伐状态判断失效）"
            )
            if failed:
                return failed
            time.sleep(2.0)
            continue
        click_path(device, p)   # 出撃
        p2 = _popup_btn_wait(
            device, "Popup_Confirm_NoteButton2(Clone)/Box/Contents/Popup_ButtonSet2/Button_Confirm", 8)
        if p2:
            click_path(device, p2)  # 決定（「…に出撃します」）
        if not battle_and_return(device, home, log=log, timeout=240, frame_cb=frame_cb):
            return session.partial("战斗/结算超时未回迎击战页")
        session.mark_cleared()
        after = _disaster_bosses(device)
        this = next((b for b in after if b["btn"] == boss["btn"]), None)
        if not (this and this["done"]):
            time.sleep(2.0)   # 结算数据刷新延迟，重读一次再定责
            after = _disaster_bosses(device)
            this = next((b for b in after if b["btn"] == boss["btn"]), None)
        if this and this["done"]:
            session.suspect = 0
            log(f"[disaster] ✓ boss 击退（{session.cleared}/3）")
        else:
            failed = session.mark_suspect(
                "boss 连续未判定为已讨伐（可能打不过），交还 LLM"
            )
            log(f"[disaster] ? boss 状态未变（suspect {session.suspect}）——可能败北")
            if failed:
                return failed


# ---- 探索任务 --------------------------------------------------------------

def _expedition_quests(device) -> list[dict]:
    """任务列表全部条目：{cx,cy,remaining,title}。

    CellView(Clone) 同名导致 path 无法区分条目，点击一律用屏幕中心坐标
    （click_ui 射线真实点击，弹层在最上层，安全）。
    """
    tree = device.ui_tree(max_nodes=30000)
    out = []
    for cv in tree.get("canvases", []):
        if cv.get("name") != "UICanvas":
            continue
        for n, a in walk(cv):
            if not a or not str(n.get("name", "")).startswith("CellView"):
                continue
            box = None
            blob_parts = []
            for m, ma in walk(n):
                b = m.get("button")
                if b and ma and str(b.get("path", "")).endswith("EncounterQuestList/Button_Challenge"):
                    box = m.get("screen")
                t = m.get("text") if ma else None
                if t:
                    blob_parts.append(str(t))
            if box is None:
                continue
            blob = "".join(blob_parts)
            mt = re.search(r"挑戦回数【\s*(\d+)", blob)
            out.append({
                "cx": (box[0] + box[2]) // 2, "cy": (box[1] + box[3]) // 2,
                "remaining": int(mt.group(1)) if mt else -1,
                "title": blob[:50],
            })
    return out


def expedition_sweep(device, log=print, stop_event=None, frame_cb=None,
                     timeout: float = 1500.0) -> dict:
    home = "IdleExploration"
    if not _at_home(device, home):
        return _routine_result(
            ExecutionStatus.WRONG_SCENE, 0, f"不在 {home}（探索队页），请先导航"
        )
    session = SweepSession(timeout)
    opened = False
    while True:
        halted = session.checkpoint(stop_event)
        if halted:
            return halted
        if not opened:
            if _expedition_quests(device):
                opened = True   # 任务列表已开着（上轮遗留/LLM 先开了），直接读
                continue
            entry = next((b["path"] for b in collect_buttons(device.ui_tree(max_nodes=30000))
                          if b["path"].endswith("Top/Right/Button_EncountQuest")), None)
            if not entry:
                return session.partial("找不到探索クエスト入口")
            click_path(device, entry)
            time.sleep(1.5)
            opened = True
        quests = _expedition_quests(device)
        if not quests:
            return session.done("没有进行中的探索任务")
        # 所有任务共享同一免费次数池（2026-08-30 用户确认：打完的任务会消失，
        # 下一个顶上，各卡显示的挑戦回数一致）——打第一个可用条目即可
        quest = quests[0]
        if quest["remaining"] == 0:
            return session.done("免费次数已用完")
        if quest["remaining"] < 0:
            log("[expedition] 回数读取失败，按可打处理")
        log(f"[expedition] {quest['title'][:36]}… 剩余 {quest['remaining']}")
        try:
            d = device.click_ui(quest["cx"], quest["cy"])   # 開始（坐标区分同名条目）
        except Exception as exc:
            return session.partial(
                f"点「開始」异常: {exc.__class__.__name__}: {exc}"
            )
        if not d:
            failed = session.mark_suspect("点「開始」未命中")
            if failed:
                return failed
            opened = False
            time.sleep(2.0)
            continue
        time.sleep(1.5)
        # 回数耗尽时会弹恢复确认（消費アビスジェム）——红线キャンセル，视为完成
        cancel = popup_cancel_consume(device.ui_tree(canvas="Front", max_nodes=2000))
        if cancel:
            click_path(device, cancel)
            log("[expedition] 免费次数已尽（恢复确认已拒绝）→ 完成")
            return session.done("免费次数用完（消费弹窗已拒绝）")
        p = _popup_btn_wait(
            device, "Popup_QuestDetail_Exploration(Clone)/Box/Contents/Popup_ButtonSet3/Button_Confirm")
        if not p:
            failed = session.mark_suspect("任务详情弹窗连续未出现")
            if failed:
                return failed
            opened = False
            time.sleep(2.0)
            continue
        click_path(device, p)   # 出撃（无二段确认，直接进战斗）
        if not battle_and_return(device, home, log=log, timeout=240, frame_cb=frame_cb):
            return session.partial("战斗/结算超时未回探索队页")
        session.mark_success()
        opened = False   # 结算后列表弹层自动关闭，重开再读回数
        time.sleep(1.5)


# ---- 注册表（统一签名 + LLM 编排的已存 routine） ---------------------------
#
# 统一调用契约：fn(device, params, *, log, stop_event, frame_cb)。
# 三个手写 sweep 不吃 params（经适配器忽略）；generic_sweep 与
# tasks/routines/*.json 里的已存编排吃 params（已存的可在调用时覆盖个别字段）。


def _adapt(fn) -> Routine:
    def wrapper(device, params=None, *, log=print, stop_event=None, frame_cb=None, **_):
        return fn(device, log=log, stop_event=stop_event, frame_cb=frame_cb)
    wrapper.__name__ = fn.__name__
    return wrapper


_BUILTIN_ROUTINES: dict[str, Routine] = {
    "claim_idle_reward": _adapt(claim_idle_reward),
    "forces_sweep": _adapt(forces_sweep),
    "disaster_sweep": _adapt(disaster_sweep),
    "expedition_sweep": _adapt(expedition_sweep),
    "generic_sweep": generic_sweep,
}
ROUTINES: dict[str, Routine] = dict(_BUILTIN_ROUTINES)


def reload_saved() -> None:
    """把 tasks/routines/*.json 的已存编排并入注册表（新存盘后调用）。"""
    saved = load_saved_routines()
    ROUTINES.clear()
    ROUTINES.update(_BUILTIN_ROUTINES)
    ROUTINES.update(saved)


def available_routines() -> tuple[str, ...]:
    return tuple(sorted(ROUTINES))


def run_routine(name: str, device, params: dict | None = None, *, log=print,
                stop_event=None, frame_cb=None) -> ExecutionResult:
    """统一 routine 查找、调用与异常语义。"""
    fn = ROUTINES.get(str(name))
    if fn is None:
        return ExecutionResult(
            ExecutionStatus.PARTIAL,
            detail=f"未知 routine: {name}（可用: {', '.join(available_routines())}）",
        )
    try:
        raw = fn(device, params or {}, log=log, stop_event=stop_event,
                 frame_cb=frame_cb)
    except TransitionTimeout as exc:
        return ExecutionResult(ExecutionStatus.BLOCKED, detail=str(exc))
    except Exception as exc:
        return ExecutionResult(
            ExecutionStatus.PARTIAL,
            detail=f"{exc.__class__.__name__}: {exc}",
        )
    if isinstance(raw, ExecutionResult):
        return raw
    return ExecutionResult.from_mapping(raw)


def save_and_register(name: str, params: dict) -> str:
    """存盘编排并即刻注册；返回文件路径字符串。名称/参数不合法抛 ValueError。"""
    f = save_program(name, params)
    reload_saved()
    return str(f)


reload_saved()   # 进程启动即加载已存编排（LLM 可按名直调）
