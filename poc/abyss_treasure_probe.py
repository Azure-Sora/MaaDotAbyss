"""深渊宝箱房勘探：dump 当前状态（截图+UI树+宝箱弹窗结构），不自动做决策。

用法（游戏停在宝箱房/地图任意状态）:
  python poc/abyss_treasure_probe.py            # dump 现场
  python poc/abyss_treasure_probe.py --pick N   # 点击第 N 张选项卡（0 起）后重新 dump
  python poc/abyss_treasure_probe.py --close    # 点 Button_Close 后重新 dump
"""
import argparse
import json
import sys
import time
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotabyss_agent.abyss import _walk_node, _walk_all  # noqa: E402
from dotabyss_agent.device_bridge import BridgeDevice  # noqa: E402

DUMP = Path(".local/debug/treasure_probe")


def dump(device, tag: str, log=print) -> Path:
    tree = device.ui_tree(max_nodes=30000)
    DUMP.mkdir(parents=True, exist_ok=True)
    base = DUMP / f"{time.strftime('%H%M%S')}_{tag}"
    base.with_suffix(".tree.json").write_text(
        json.dumps(tree, ensure_ascii=False, indent=1), encoding="utf-8")
    cv2.imwrite(str(base.with_suffix(".png")), device.screenshot())

    popups = [n for n in _walk_all(tree) if n["name"].startswith("Popup_NetherTreasure")]
    log(f"== dump {base.name}  Popup_NetherTreasure×{len(popups)}")
    for p in popups:
        for x in _walk_node(p):
            b = x.get("button")
            if b:
                log(f"  [btn] {b['path']}\n        interactable={b.get('interactable')}")
            t = (x.get("text") or "").strip()
            if t:
                log(f"  [text] {x['name']}: {t[:80]!r} screen={x.get('screen')}")
    if not popups:
        for x in _walk_all(tree):
            t = (x.get("text") or "").strip()
            b = x.get("button")
            if b and x.get("name", "").startswith("Button"):
                log(f"  [btn*] {b['path']} interactable={b.get('interactable')}")
    log(f"== dump 完成: {base.name}.tree.json / .png")
    return base


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pick", type=int, default=None, help="点击第 N 张宝箱选项卡（0 起）")
    ap.add_argument("--close", action="store_true", help="点击 Button_Close")
    ap.add_argument("--confirm", action="store_true", help="点击 Button_Confirm")
    args = ap.parse_args()

    d = BridgeDevice()
    if args.pick is not None or args.close or args.confirm:
        tree = d.ui_tree(max_nodes=30000)
        popups = [n for n in _walk_all(tree) if n["name"].startswith("Popup_NetherTreasure")]
        if not popups:
            sys.exit("Popup_NetherTreasure 不在场")
        if args.pick is not None:
            cards = []
            for n in _walk_node(popups[0]):
                b = n.get("button")
                if b and not b["path"].endswith(("Button_Close", "Button_Confirm")):
                    tnode = next((c for c in _walk_node(n)
                                  if c.get("name") == "TitleText" and c.get("text")), None)
                    cards.append((b, tnode.get("text") if tnode else "?"))
            for i, (b, tt) in enumerate(cards):
                print(f"  card {i}: {tt!r} interactable={b.get('interactable')}")
            d.click_by_path(cards[args.pick][0]["path"])
            print(f"  → 点击 card {args.pick}")
        elif args.close:
            for n in _walk_node(popups[0]):
                b = n.get("button")
                if b and b["path"].endswith("Button_Close"):
                    d.click_by_path(b["path"])
                    print("  → 点击 Button_Close")
                    break
        elif args.confirm:
            for n in _walk_node(popups[0]):
                b = n.get("button")
                if b and b["path"].endswith("Button_Confirm"):
                    d.click_by_path(b["path"])
                    print("  → 点击 Button_Confirm")
                    break
        time.sleep(2.0)
    dump(d, "after" if (args.pick is not None or args.close or args.confirm) else "probe")


if __name__ == "__main__":
    main()
