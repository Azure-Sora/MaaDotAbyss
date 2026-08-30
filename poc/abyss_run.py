"""深渊一键开局+监督推进：入口 → 选检查点 → 编成出击 → 倍率/安全箱 → run_to_floor → 结算。

用法（游戏停在深渊入口页 NetherTop）:
  python poc/abyss_run.py --quota safe:6,rush:3 --target 40 --start-floor 20 [--provider glm]

全流程已实测（2026-08-30，20F→40F 一把完整跑通）。
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotabyss_agent.abyss import enter_run, run_to_floor  # noqa: E402
from dotabyss_agent.abyss_plan import AbyssLedger  # noqa: E402
from dotabyss_agent.abyss_ui import read_hud  # noqa: E402
from dotabyss_agent.brain import Brain  # noqa: E402
from dotabyss_agent.device_bridge import BridgeDevice  # noqa: E402


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
