"""命令行入口。

用法:
    python -m dotabyss_agent.cli list
    python -m dotabyss_agent.cli run <task_id> [--max-steps N] [--time-budget 秒]
    python -m dotabyss_agent.cli run-all [--only id1,id2]
"""
import argparse
import json
import sys

import yaml

from .agent import run_task
from .brain import Brain
from .config import TASKS_DIR
from .device import GameDevice, DeviceError
from .precondition import check_precondition


def load_tasks() -> list[dict]:
    data = yaml.safe_load((TASKS_DIR / "daily.yaml").read_text(encoding="utf-8"))
    return data["tasks"]


def main():
    ap = argparse.ArgumentParser(prog="dotabyss-agent")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    p_run = sub.add_parser("run")
    p_run.add_argument("task_id")
    p_run.add_argument("--max-steps", type=int, default=30)
    p_run.add_argument("--time-budget", type=float, default=420.0)
    p_run.add_argument("--no-knowledge-update", action="store_true")
    p_all = sub.add_parser("run-all")
    p_all.add_argument("--only", type=str, default="")
    p_all.add_argument("--max-steps", type=int, default=30)
    p_all.add_argument("--time-budget", type=float, default=420.0)
    args = ap.parse_args()

    tasks = load_tasks()
    if args.cmd == "list":
        for t in tasks:
            print(f"{t['id']:24s} {t.get('name', '')}")
        return

    todo = tasks
    if args.cmd == "run":
        todo = [t for t in tasks if t["id"] == args.task_id]
        if not todo:
            sys.exit(f"未知任务: {args.task_id}")
    elif args.cmd == "run-all" and args.only:
        ids = {s.strip() for s in args.only.split(",")}
        todo = [t for t in tasks if t["id"] in ids]

    try:
        device = GameDevice()
    except DeviceError as e:
        sys.exit(f"[设备错误] {e}")
    brain = Brain()
    device.bring_to_front()

    results = []
    blocked = False
    for t in todo:
        print(f"\n===== {t['id']} ({t.get('name', '')}) =====")
        try:
            skip, reason = check_precondition(t, device, brain)
        except Exception as e:  # 前置检查自身失败不阻断任务
            skip, reason = False, f"前置检查异常({e.__class__.__name__})，保守执行"
        if skip:
            print(f"[skipped] {reason}")
            results.append({"task": t["id"], "status": "skipped", "steps": 0, "detail": reason})
            continue
        if reason:
            print(f"[precondition] {reason}")
        r = run_task(
            t,
            device,
            brain,
            max_steps=args.max_steps,
            time_budget=args.time_budget,
            update_knowledge=not (args.cmd == "run" and args.no_knowledge_update),
        )
        results.append(r)
        print(f"[{r['status']}] {r['task']} steps={r['steps']} {r['detail']}")
        if r["status"] == "blocked":
            blocked = True
            break  # 403/异常 → 停止一切，等人工

    print("\n===== 汇总 =====")
    print(json.dumps(results, ensure_ascii=False, indent=2))
    if blocked:
        print("\n!! 检测到 blocked（疑似 403/网络错误），已停止全部任务，请人工检查游戏。")


if __name__ == "__main__":
    main()
