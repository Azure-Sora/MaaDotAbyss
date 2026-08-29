"""命令行入口。

用法:
    python -m dotabyss_agent.cli list
    python -m dotabyss_agent.cli run <task_id> [--max-steps N] [--time-budget 秒]
    python -m dotabyss_agent.cli run-all [--only id1,id2]
"""
import argparse
import json
import sys
from pathlib import Path

from .config import TASKS_DIR
from .runner import load_tasks, run_selected


def _explore(args) -> int:
    """探索任务 → 模型挑选关键步骤 → 自动生成识图剧本 → 立即自动验证。"""
    from .agent import run_task
    from .brain import Brain
    from .device import GameDevice, DeviceError
    from .flowgen import ensure_home, guided_generate
    from .precondition import check_precondition

    todo = {t["id"]: t for t in load_tasks()}
    task = todo.get(args.task_id)
    if task is None:
        print(f"未知任务: {args.task_id}")
        return 1
    try:
        device = GameDevice()
    except DeviceError as e:
        print(f"[设备错误] {e}")
        return 1
    brain = Brain(provider=args.provider)
    print(f"[explore] 使用模型: {brain.provider}/{brain.model}")
    device.bring_to_front()

    # 探索与回放的公共起点是主页面
    if not ensure_home(device, log=print):
        print("[explore] 无法导航回主页面，中止")
        return 1

    # 前置条件不满足（如挂机奖励还没攒够）时，探索只会录到"空走"路径，拒绝生成
    try:
        skip, reason = check_precondition(task, device, brain)
    except Exception as e:
        skip, reason = False, f"前置检查异常({e.__class__.__name__})，继续探索"
    if skip:
        print(f"[explore] {reason}——现在探索录不到实质路径，等可执行时再跑。")
        return 1

    r = run_task(
        task, device, brain,
        max_steps=args.max_steps,
        time_budget=args.time_budget,
        record=True,
    )
    print(f"[explore] 探索结果: [{r['status']}] {r['detail']}（{r['steps']} 步）")
    if r["status"] != "done":
        print("[explore] 探索未完成，不生成剧本")
        return 1

    fr = guided_generate(task, brain, Path(r["run_dir"]), task["id"], device)
    if fr is None:
        print("[explore] 剧本生成未完成（详见上方日志）")
        return 1
    print(f"[explore] 剧本验证: [{fr.get('status')}] {fr.get('detail')}（{fr.get('seconds', '?')}s）")
    if fr.get("status") == "done":
        print(f"[explore] ✅ 剧本转正：任务 {task['id']} 已可识图快跑")
        return 0
    print("[explore] ⚠️ 剧本验证未通过——已保留 yaml，可重跑 explore 或人工修锚点")
    return 1


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
    p_exp = sub.add_parser("explore", help="探索任务并自动生成识图剧本，生成后立即验证")
    p_exp.add_argument("task_id")
    p_exp.add_argument("--max-steps", type=int, default=25)
    p_exp.add_argument("--time-budget", type=float, default=360.0)
    p_exp.add_argument("--provider", type=str, default=None, help="覆盖默认模型供给 (mimo/glm)")
    args = ap.parse_args()

    if args.cmd == "list":
        for t in load_tasks():
            print(f"{t['id']:24s} {t.get('name', '')}")
        return

    if args.cmd == "explore":
        sys.exit(_explore(args))

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
