"""深渊一键开局+监督推进：入口 → 选检查点 → 编成出击 → 倍率/安全箱 → run_to_floor → 结算。

用法（游戏停在深渊入口页 NetherTop）:
  python poc/abyss_run.py --quota safe:6,rush:3 --target 40 --start-floor 20 [--provider glm]

全流程已实测（2026-08-30，20F→40F 一把完整跑通）。
"""
import argparse
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotabyss_agent.abyss import (  # noqa: E402
    _click_text_center, _overlay_present, _walk_all, run_to_floor,
)
from dotabyss_agent.abyss_plan import AbyssLedger  # noqa: E402
from dotabyss_agent.abyss_ui import read_hud  # noqa: E402
from dotabyss_agent.brain import Brain  # noqa: E402
from dotabyss_agent.device_bridge import BridgeDevice  # noqa: E402


def _tap_text(d, keyword, tree=None, tries=3):
    for _ in range(tries):
        t = tree or d.ui_tree(max_nodes=30000)
        if _click_text_center(d, t, keyword, log=print):
            return True
        time.sleep(1.5)
        tree = None
    return False


def enter_run(d, start_floor: int) -> None:
    """从 NetherTop 入口一路打到深渊地图（实测路径 2026-08-30）。"""
    time.sleep(1.5)
    scene = d.ui_tree(max_nodes=10).get("scene", "")
    if scene == "Nether":
        print("已在深渊地图中，跳过入场")
        return
    assert scene == "NetherTop", f"请在深渊入口页启动（当前 {scene}）"
    d.click_by_path("/NetherTop/UICanvas/RootUI/UIGroup/Scene_NetherTop/Button_GateStart/Button_Start")
    print("[入场] 探索開始")
    time.sleep(2.0)
    # 地点选择：点目标层检查点（如 20F）→ 確定
    _tap_text(d, f"{start_floor}F")
    time.sleep(0.6)
    _tap_text(d, "確定")
    print(f"[入场] 检查点 {start_floor}F 確定")
    time.sleep(2.0)
    # 编成页：出撃
    assert _tap_text(d, "出撃"), "编成页未找到出撃"
    print("[入场] 出撃")
    time.sleep(2.0)
    # ゲットキー消費弹窗：核验 1 倍 → 使用
    tree = d.ui_tree(max_nodes=12000)
    texts = [n.get("text") or "" for n in _walk_all(tree)]
    mults = [t.strip() for t in texts if re.fullmatch(r"[123]\s*倍", t.strip())]
    if mults and any(not m.startswith("1") for m in mults):
        raise RuntimeError(f"倍率非 1 倍: {mults}")
    if not _tap_text(d, "使用"):
        raise RuntimeError("倍率弹窗未找到 使用")
    print("[入场] 倍率 1 倍 使用")
    time.sleep(2.0)
    # 安全箱 → キャンセル
    for _ in range(6):
        if _tap_text(d, "キャンセル"):
            print("[入场] 安全箱 キャンセル")
            break
        time.sleep(1.5)
    time.sleep(2.0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quota", default="safe:6,rush:3", help="如 safe:6,rush:3")
    ap.add_argument("--target", type=int, default=40)
    ap.add_argument("--start-floor", type=int, default=20)
    ap.add_argument("--provider", default="glm")
    ap.add_argument("--max-rooms", type=int, default=30)
    args = ap.parse_args()

    quota = {}
    for part in args.quota.split(","):
        k, v = part.split(":")
        quota[k.strip()] = int(v)

    d = BridgeDevice()
    enter_run(d, args.start_floor)
    hud = read_hud(d)
    print("[开局] HUD:", hud)
    led = AbyssLedger(
        floor=hud.get("floor", args.start_floor), erosion=hud.get("erosion", 0),
        getkeys=hud.get("keys", 0), coins=hud.get("coins", 0),
        quota=quota, target_floor=args.target)
    brain = Brain(provider=args.provider)   # 未知名代码 → 视觉定色入册
    r = run_to_floor(d, led, brain=brain, max_rooms=args.max_rooms, log=print)
    print("run result:", r)
    if r["status"] == "settled":
        print(f"✅ {args.start_floor}F→{args.target}F 完整跑完并结算")


if __name__ == "__main__":
    main()
