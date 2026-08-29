"""命令行入口。

用法:
    python -m dotabyss_agent.cli list
    python -m dotabyss_agent.cli run <task_id> [--max-steps N] [--time-budget 秒]
    python -m dotabyss_agent.cli run-all [--only id1,id2]
"""
import argparse
import json
import sys

from .config import TASKS_DIR
from .runner import load_tasks, run_selected


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

    if args.cmd == "list":
        for t in load_tasks():
            print(f"{t['id']:24s} {t.get('name', '')}")
        return

    if args.cmd == "run":
        ids = [args.task_id]
    else:
        ids = [t["id"] for t in load_tasks()]
        if args.only:
            ids = [s.strip() for s in args.only.split(",")]

    results = run_selected(
        ids,
        max_steps=args.max_steps,
        time_budget=args.time_budget,
        update_knowledge=not (args.cmd == "run" and args.no_knowledge_update),
    )

    print("\n===== 汇总 =====")
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
