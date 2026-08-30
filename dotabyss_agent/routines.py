"""auto 动作的三个程序化清剿 routine（docs/research/14）。

调用契约：LLM 已把画面导航到对应入口页；routine 第一步校验场景名，
不对立即返回 wrong_scene（绝不自己摸路）。全程 click_by_path（不穿透），
结算/弹窗走 macros 通用段。返回：
{"status": done|partial|wrong_scene, "cleared": 场数, "detail": 说明}
partial = 中途卡住/可疑，剩余情况已写入 detail，由 LLM 兜底决断。
"""
import re
import time

from .macros import (
    battle_and_return, click_path, collect_buttons, collect_texts, find_btn,
    popup_cancel_consume, scene, walk,
)

COUNTRIES = ("Milesgard", "Peldion", "Eldorana", "Coalition", "Luxnova")

SUSPECT_LIMIT = 2   # 连续 N 场"计数未递减"视为异常，交还 LLM


# ---- 通用小件 -----------------------------------------------------------

def _popup_btn_wait(device, suffix: str, timeout: float = 8.0) -> str | None:
    """轮询等待某弹窗按钮出现且可点，返回路径。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        for b in collect_buttons(device.ui_tree(max_nodes=30000)):
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
        return {"status": "wrong_scene", "cleared": 0,
                "detail": f"不在 {home}（势力任务列表），请先导航"}
    t0 = time.time()
    cleared, suspect = 0, 0
    while time.time() - t0 < timeout:
        if _check_stop(stop_event):
            return {"status": "partial", "cleared": cleared, "detail": "用户停止"}
        cards = [c for c in _forces_cards(device)
                 if c["open"] and c["btn"] and c["remaining"] > 0]
        if not cards:
            left = [(c["country"], c["remaining"]) for c in _forces_cards(device) if c["open"]]
            return {"status": "done", "cleared": cleared,
                    "detail": "开放关卡全部打完 " + (str(left) if left else "")}
        card = cards[0]
        log(f"[forces] {card['country']} {card['title']} 剩余 {card['remaining']}")
        if not click_path(device, card["btn"]):
            return {"status": "partial", "cleared": cleared,
                    "detail": f"点 {card['country']} 挑戦失败"}
        p = _popup_btn_wait(
            device, "Popup_UnionRequestDetail(Clone)/Box/Contents/Popup_ButtonSet2/Button_Confirm")
        if not p:
            # 弹窗没开（回数可能在列表页显示延迟）→ 回读状态重判
            suspect += 1
            log(f"[forces] 详情弹窗未出现（suspect {suspect}）")
            if suspect >= SUSPECT_LIMIT:
                return {"status": "partial", "cleared": cleared,
                        "detail": "挑戦弹窗连续未出现"}
            time.sleep(2.0)
            continue
        # 周回(Button_SkipMode)禁用——拿不全奖励，只走出撃单刷
        click_path(device, p)
        sortie = _popup_btn_wait(
            device, "Popup_Confirm_Sortie(Clone)/Box/Contents/Popup_ButtonSet2/Button_Confirm", 8)
        if sortie:
            click_path(device, sortie)
        if not battle_and_return(device, home, log=log, timeout=240, frame_cb=frame_cb):
            return {"status": "partial", "cleared": cleared,
                    "detail": f"{card['country']} 战斗/结算超时未回列表"}
        cleared += 1
        after = next((c for c in _forces_cards(device) if c["country"] == card["country"]), None)
        if after and after["remaining"] < card["remaining"]:
            suspect = 0
            log(f"[forces] ✓ {card['country']} 剩余 {card['remaining']}→{after['remaining']}")
        else:
            # 结算后卡片数据可能有服务器刷新延迟，等一下重读再定责
            time.sleep(2.0)
            after = next((c for c in _forces_cards(device) if c["country"] == card["country"]), None)
            if after and after["remaining"] < card["remaining"]:
                suspect = 0
                log(f"[forces] ✓ {card['country']} 剩余 {card['remaining']}→{after['remaining']}（延迟刷新）")
                continue
            suspect += 1
            log(f"[forces] ? {card['country']} 计数未递减（suspect {suspect}）"
                "——可能败北或界面延迟")
            if suspect >= SUSPECT_LIMIT:
                return {"status": "partial", "cleared": cleared,
                        "detail": f"{card['country']} 连续计数未递减，请 LLM 检查"}
    return {"status": "partial", "cleared": cleared, "detail": "总超时"}


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
        return {"status": "wrong_scene", "cleared": 0,
                "detail": f"不在 {home}（迎击战页），请先导航"}
    t0 = time.time()
    cleared, suspect = 0, 0
    while time.time() - t0 < timeout:
        if _check_stop(stop_event):
            return {"status": "partial", "cleared": cleared, "detail": "用户停止"}
        bosses = [b for b in _disaster_bosses(device) if not b["done"] and "/Sp" not in b["btn"]]
        if not bosses:
            return {"status": "done", "cleared": cleared,
                    "detail": "三个小 boss 全部击退"}
        boss = bosses[0]
        log(f"[disaster] 出击 {boss['btn'].split('/Area')[-1].split('/')[0]}（{boss['label'][:30]}）")
        if not click_path(device, boss["btn"]):
            return {"status": "partial", "cleared": cleared, "detail": "点 boss 失败"}
        p = _popup_btn_wait(
            device, "Popup_QuestDetail_Disaster(Clone)/Box/Contents/Popup_ButtonSet2/Button_Confirm")
        if not p:
            suspect += 1
            if suspect >= SUSPECT_LIMIT:
                return {"status": "partial", "cleared": cleared,
                        "detail": "boss 详情弹窗连续未出现（可能已讨伐状态判断失效）"}
            time.sleep(2.0)
            continue
        click_path(device, p)   # 出撃
        p2 = _popup_btn_wait(
            device, "Popup_Confirm_NoteButton2(Clone)/Box/Contents/Popup_ButtonSet2/Button_Confirm", 8)
        if p2:
            click_path(device, p2)  # 決定（「…に出撃します」）
        if not battle_and_return(device, home, log=log, timeout=240, frame_cb=frame_cb):
            return {"status": "partial", "cleared": cleared,
                    "detail": "战斗/结算超时未回迎击战页"}
        cleared += 1
        after = _disaster_bosses(device)
        this = next((b for b in after if b["btn"] == boss["btn"]), None)
        if not (this and this["done"]):
            time.sleep(2.0)   # 结算数据刷新延迟，重读一次再定责
            after = _disaster_bosses(device)
            this = next((b for b in after if b["btn"] == boss["btn"]), None)
        if this and this["done"]:
            suspect = 0
            log(f"[disaster] ✓ boss 击退（{cleared}/3）")
        else:
            suspect += 1
            log(f"[disaster] ? boss 状态未变（suspect {suspect}）——可能败北")
            if suspect >= SUSPECT_LIMIT:
                return {"status": "partial", "cleared": cleared,
                        "detail": "boss 连续未判定为已讨伐（可能打不过），交还 LLM"}
    return {"status": "partial", "cleared": cleared, "detail": "总超时"}


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
        return {"status": "wrong_scene", "cleared": 0,
                "detail": f"不在 {home}（探索队页），请先导航"}
    t0 = time.time()
    cleared, suspect = 0, 0
    opened = False
    while time.time() - t0 < timeout:
        if _check_stop(stop_event):
            return {"status": "partial", "cleared": cleared, "detail": "用户停止"}
        if not opened:
            if _expedition_quests(device):
                opened = True   # 任务列表已开着（上轮遗留/LLM 先开了），直接读
                continue
            entry = next((b["path"] for b in collect_buttons(device.ui_tree(max_nodes=30000))
                          if b["path"].endswith("Top/Right/Button_EncountQuest")), None)
            if not entry:
                return {"status": "partial", "cleared": cleared,
                        "detail": "找不到探索クエスト入口"}
            click_path(device, entry)
            time.sleep(1.5)
            opened = True
        quests = _expedition_quests(device)
        if not quests:
            return {"status": "done", "cleared": cleared,
                    "detail": "没有进行中的探索任务"}
        # 所有任务共享同一免费次数池（2026-08-30 用户确认：打完的任务会消失，
        # 下一个顶上，各卡显示的挑戦回数一致）——打第一个可用条目即可
        quest = quests[0]
        if quest["remaining"] == 0:
            return {"status": "done", "cleared": cleared,
                    "detail": "免费次数已用完"}
        if quest["remaining"] < 0:
            log("[expedition] 回数读取失败，按可打处理")
        log(f"[expedition] {quest['title'][:36]}… 剩余 {quest['remaining']}")
        try:
            d = device.click_ui(quest["cx"], quest["cy"])   # 開始（坐标区分同名条目）
        except Exception:
            d = ""
        if not d:
            suspect += 1
            if suspect >= SUSPECT_LIMIT:
                return {"status": "partial", "cleared": cleared,
                        "detail": "点「開始」未命中"}
            opened = False
            time.sleep(2.0)
            continue
        time.sleep(1.5)
        # 回数耗尽时会弹恢复确认（消費アビスジェム）——红线キャンセル，视为完成
        cancel = popup_cancel_consume(device.ui_tree(canvas="Front", max_nodes=2000))
        if cancel:
            click_path(device, cancel)
            log("[expedition] 免费次数已尽（恢复确认已拒绝）→ 完成")
            return {"status": "done", "cleared": cleared,
                    "detail": "免费次数用完（消费弹窗已拒绝）"}
        p = _popup_btn_wait(
            device, "Popup_QuestDetail_Exploration(Clone)/Box/Contents/Popup_ButtonSet3/Button_Confirm")
        if not p:
            suspect += 1
            if suspect >= SUSPECT_LIMIT:
                return {"status": "partial", "cleared": cleared,
                        "detail": "任务详情弹窗连续未出现"}
            opened = False
            time.sleep(2.0)
            continue
        click_path(device, p)   # 出撃（无二段确认，直接进战斗）
        if not battle_and_return(device, home, log=log, timeout=240, frame_cb=frame_cb):
            return {"status": "partial", "cleared": cleared,
                    "detail": "战斗/结算超时未回探索队页"}
        cleared += 1
        opened = False   # 结算后列表弹层自动关闭，重开再读回数
        time.sleep(1.5)
    return {"status": "partial", "cleared": cleared, "detail": "总超时"}


ROUTINES = {
    "forces_sweep": forces_sweep,
    "disaster_sweep": disaster_sweep,
    "expedition_sweep": expedition_sweep,
}
