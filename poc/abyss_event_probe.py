"""深渊事件房勘探：推进到事件房即停，dump UI 树+截图+现逻辑解析对照，供交叉验证。

不自动做事件决策——弹窗留在原地，人工分析 dump 后用 continue 子命令确认选项。

用法（游戏停在深渊入口页）:
  python poc/abyss_event_probe.py probe --start-floor 20
  python poc/abyss_event_probe.py continue --title "选项标题"   # 确认当前事件→继续找下一个
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotabyss_agent import abyss as A  # noqa: E402
from dotabyss_agent.abyss import (  # noqa: E402
    _code_popup_present, _event_overlay_present, _gate_present, _overlay_present,
    _press_recenter, _result_step, _scene, _wait_map_stable, _walk_all,
    boss_floor_continue, enter_run, reconcile, resolve_room,
)
from dotabyss_agent.abyss_plan import AbyssLedger, pick_room  # noqa: E402
from dotabyss_agent.abyss_ui import read_candidates, read_hud  # noqa: E402
from dotabyss_agent.brain import Brain  # noqa: E402
from dotabyss_agent.device_bridge import BridgeDevice  # noqa: E402

DUMP = Path(".local/debug/event_probe")


def parse_current(d: str) -> dict:
    """复刻 abyss.py _resolve_event 内 parse() 现逻辑（含缺陷），用于对照。"""
    m = re.search(r"浸食率\s*(\d+)\s*上昇", d)
    ec = int(m.group(1)) if m else 0
    m = re.search(r"浸食率\s*(\d+)\s*減少|浸食率\s*減少\s*(\d+)", d)
    eg = int(m.group(1) or m.group(2) or 0) if m else 0
    m = re.search(r"HP.*?(\d+)%\s*減少", d)
    hp = int(m.group(1)) if m else 0
    m = re.search(r"(\d+)\s*個消費", d)
    coin = int(m.group(1)) if m else 0
    return {"hp_cost": hp, "erosion_cost": ec, "erosion_gain": eg, "coin_cost": coin,
            "code_gain": "コード" in d and "獲得" in d,
            "item_gain": "アイテム" in d and "選択" in d}


def dump_event(device, led: AbyssLedger, tag: str, log=print) -> Path:
    tree = device.ui_tree(max_nodes=30000)
    DUMP.mkdir(parents=True, exist_ok=True)
    base = DUMP / f"{led.floor}F_{time.strftime('%H%M%S')}_{tag}"
    base.with_suffix(".tree.json").write_text(
        json.dumps(tree, ensure_ascii=False, indent=1), encoding="utf-8")
    try:
        import cv2
        cv2.imwrite(str(base.with_suffix(".png")), device.screenshot())
    except Exception as e:
        log(f"  [dump] 截图失败: {e}")

    titles = [n for n in _walk_all(tree) if n["name"] == "TitleText" and n.get("text")]
    texts = [n for n in _walk_all(tree) if n["name"] == "Text" and n.get("text")]
    log(f"== dump {base.name}  TitleText×{len(titles)} Text×{len(texts)}")
    for t in titles:
        log(f"  [title] {t['text']!r} screen={t.get('screen')}")
    for x in texts:
        log(f"  [text] {x['text'][:70]!r} screen={x.get('screen')}")

    # 复刻现逻辑 desc 归组（x 邻域 ±20、y≥title、无排序、全树 texts）
    for t in titles:
        if not t.get("screen"):
            continue
        s0, s1 = t["screen"][0], t["screen"][2]
        desc = ""
        for x in texts:
            if not x.get("screen"):
                continue
            xx = (x["screen"][0] + x["screen"][2]) / 2
            if s0 - 20 <= xx <= s1 + 20 and x["screen"][1] >= t["screen"][1]:
                desc += x["text"]
        if desc:
            p = parse_current(desc)
            log(f"  [现逻辑] 『{t['text']}』desc={desc[:80]!r}")
            log(f"           parse={p}")
    log(f"== dump 完成: {base.name}.tree.json / .png")
    return base


def advance_to_event(device, led: AbyssLedger, brain, log=print, max_rooms=12) -> bool:
    """推进房间直到事件覆盖层出现（返回 True）或房间数耗尽（False）。"""
    for _ in range(max_rooms):
        _wait_map_stable(device, led=led, brain=brain, log=log)
        tree = device.ui_tree(max_nodes=30000)
        if _event_overlay_present(tree):
            return True
        if _gate_present(tree):
            log(f"  续行界面：{led.floor}F 续行")
            boss_floor_continue(device, led, settle=False, log=log)
            time.sleep(2.0)
            continue
        if _overlay_present(tree) or _code_popup_present(tree):
            _result_step(device, led, brain, log=log, tree=tree)
            time.sleep(1.5)
            continue
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
            raise RuntimeError("地图无候选——人工看一眼")
        room = pick_room(cands, led)
        if not room.visible:
            log(f"  最优 {room.type} 在屏外，現在地へ 归位后重选")
            _press_recenter(device, log=log)
            cands = read_candidates(device, current_floor=led.floor, log=log) or cands
            vis = [c for c in cands if c.visible]
            if vis:
                room = pick_room(vis, led)
        log(f"[{led.floor}F 侵蚀{led.erosion}] → {room.type} @ ({room.x},{room.y})")
        A.enter_room(device, room, log=log)
        if room.type == "event":
            for _ in range(10):
                if _event_overlay_present(device.ui_tree(max_nodes=30000)):
                    return True
                time.sleep(1.0)
            raise RuntimeError("进事件房但覆盖层未出现")
        resolve_room(device, room, led, brain, log=log)
        time.sleep(1.2)
    return False


def choose_and_settle(device, led: AbyssLedger, brain, title: str, log=print) -> str:
    """确认当前事件选项并走完后续（下一轮事件/内置战斗/回图）。"""
    tree = device.ui_tree(max_nodes=30000)
    if not _event_overlay_present(tree):
        raise RuntimeError("事件覆盖层不在场")
    if not A._click_text_center(device, tree, title, log=log):
        raise RuntimeError(f"未找到选项文本『{title}』")
    time.sleep(0.8)
    cf = None
    for n in _walk_all(device.ui_tree(max_nodes=30000)):
        b = n.get("button")
        if b and n["name"] == "Button_Confirm" and "NetherEvent" in b.get("path", ""):
            cf = b
            break
    if not (cf and cf.get("interactable")):
        raise RuntimeError("確定未亮——选项未生效")
    device.click_by_path(cf["path"])
    log("  点击 確定")
    led.floor += 1
    saw_other = False
    for i in range(100):
        time.sleep(3.0)
        sc = _scene(device)
        if sc == "ExplorarionNetherResult":
            A.resolve_battle(device, led, brain, log=log)
            return "settled"
        if sc != "Nether":
            saw_other = True
            continue
        tree = device.ui_tree(max_nodes=30000)
        if _event_overlay_present(tree):
            return "next_round"
        if _overlay_present(tree):
            _result_step(device, led, brain, log=log, tree=tree)
            continue
        if saw_other or i > 3:
            return "back_map"
    raise RuntimeError("事件确认后 5 分钟未回图")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["probe", "continue"])
    ap.add_argument("--title", help="continue: 要确认的选项标题")
    ap.add_argument("--start-floor", type=int, default=20)
    ap.add_argument("--quota", default="impact:16,risk:16")
    ap.add_argument("--target", type=int, default=70)
    ap.add_argument("--provider", default="glm")
    ap.add_argument("--max-rooms", type=int, default=12)
    args = ap.parse_args()

    quota = {}
    for part in args.quota.split(","):
        k, v = part.split(":")
        quota[k.strip()] = int(v)

    d = BridgeDevice()
    brain = Brain(provider=args.provider)
    if args.mode == "probe":
        enter_run(d, args.start_floor)
    hud = read_hud(d)
    print("[HUD]", hud)
    led = AbyssLedger(floor=hud.get("floor", args.start_floor), erosion=hud.get("erosion", 0),
                      getkeys=hud.get("keys", 0), coins=hud.get("coins", 0),
                      quota=quota, target_floor=args.target)

    if args.mode == "continue":
        r = choose_and_settle(d, led, brain, args.title)
        print(f"[choose] → {r}")
        if r == "next_round":
            dump_event(d, led, "round2")
            return
    if not advance_to_event(d, led, brain, max_rooms=args.max_rooms):
        print("[probe] 房间数耗尽未遇事件房")
        return
    dump_event(d, led, "probe")


if __name__ == "__main__":
    main()
