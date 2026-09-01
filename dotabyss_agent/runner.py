"""CLI/GUI 共用任务编排：按阶段选择 fast path、flow 或 LLM slow path。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from .agent import run_task
from .brain import Brain
from .config import TASKS_DIR
from .device import DeviceError
from .device_select import get_device
from .evolution import EvolutionError, EvolutionLedger
from .execution import ExecutionResult, ExecutionStatus, safe_callback
from .flow import run_flow
from .flowgen import generate_flow
from .precondition import check_precondition
from .routines import ROUTINES, run_routine
from .taskfile import update_task


def load_tasks() -> list[dict]:
    data = yaml.safe_load((TASKS_DIR / "daily.yaml").read_text(encoding="utf-8"))
    return data["tasks"]


@dataclass(slots=True)
class StageOutcome:
    result: dict | None = None
    stop_batch: bool = False
    flow_failed: bool = False


@dataclass(slots=True)
class RunnerContext:
    device: object
    brain: Brain
    evolution: EvolutionLedger | None
    evolution_expected: bool
    max_steps: int
    time_budget: float
    update_knowledge: bool
    log: object
    stop_event: object
    frame_cb: object
    event_cb: object


class ResultSink:
    """任务结果的唯一出口：结果列表和 GUI 事件保持原子一致。"""

    def __init__(self, event_cb=None, log=print):
        self.results: list[dict] = []
        self.event_cb = event_cb
        self.log = log

    def finish(self, result: dict) -> dict:
        normalized = dict(result)
        normalized["status"] = ExecutionStatus.parse(
            normalized.get("status", "error")
        ).value
        normalized.setdefault("steps", 0)
        normalized.setdefault("detail", "")
        self.results.append(normalized)
        safe_callback(
            self.event_cb, {"type": "result", **normalized},
            log=self.log, label="result event",
        )
        return normalized


def run_selected(
    task_ids: list[str],
    max_steps: int = 30,
    time_budget: float = 420.0,
    update_knowledge: bool = True,
    provider: str | None = None,
    log=print,
    stop_event=None,
    frame_cb=None,
    event_cb=None,
    _device=None,
    _brain=None,
) -> list[dict]:
    """按顺序执行任务；公开返回仍为 ``list[dict]``。"""
    all_tasks = {task["id"]: task for task in load_tasks()}
    todo = [all_tasks[task_id] for task_id in task_ids if task_id in all_tasks]
    if not todo:
        log("没有匹配的任务")
        return []

    device = _device or get_device()[0]
    brain = _brain or Brain(provider=provider)
    try:
        evolution = EvolutionLedger() if update_knowledge else None
    except EvolutionError as exc:
        evolution = None
        log(f"[evolve] {exc}；本轮关闭自动演进，未确认 flow 仍走终态验收")

    context = RunnerContext(
        device=device, brain=brain, evolution=evolution,
        evolution_expected=update_knowledge, max_steps=max_steps,
        time_budget=time_budget, update_knowledge=update_knowledge,
        log=log, stop_event=stop_event, frame_cb=frame_cb, event_cb=event_cb,
    )
    sink = ResultSink(event_cb=event_cb, log=log)
    try:
        device.bring_to_front()
    except DeviceError as exc:
        log(f"[设备] {exc}")

    for task in todo:
        if stop_event is not None and stop_event.is_set():
            break
        log(f"===== {task['id']} ({task.get('name', '')}) =====")
        outcome = _run_one_task(task, context)
        if outcome.result is not None:
            sink.finish(outcome.result)
        if outcome.stop_batch:
            break
    return sink.results


def _run_one_task(task: dict, context: RunnerContext) -> StageOutcome:
    try:
        skip, reason = check_precondition(task, context.device, context.brain)
    except Exception as exc:
        skip, reason = False, f"前置检查异常({exc.__class__.__name__})，保守执行"
    if skip:
        context.log(f"[skipped] {reason}")
        return StageOutcome(ExecutionResult(
            ExecutionStatus.SKIPPED, detail=reason
        ).to_task_dict(task["id"]))
    if reason:
        context.log(f"[precondition] {reason}")

    fast = _run_fast_routine_stage(task, context)
    if fast.result is not None:
        return fast

    flow = _run_flow_stage(task, context)
    if flow.result is not None:
        return flow

    result = run_task(
        task, context.device, context.brain,
        max_steps=context.max_steps,
        time_budget=context.time_budget,
        update_knowledge=False,
        log=context.log,
        stop_event=context.stop_event,
        frame_cb=context.frame_cb,
        record=context.update_knowledge,
        event_cb=context.event_cb,
    )
    status = ExecutionStatus.parse(result.get("status"))
    if status is ExecutionStatus.DONE and context.evolution is not None:
        _evolve_success(
            task, result, context.brain, context.evolution,
            allow_compile=not flow.flow_failed, log=context.log,
        )
    context.log(
        f"[{status.value}] {result['task']} steps={result['steps']} {result['detail']}"
    )
    if status is ExecutionStatus.DONE and task.get("supplement"):
        _consume_supplement(task, context.brain, log=context.log)
    if status.stops_batch:
        context.log("!! 疑似 403/网络错误，已停止全部任务，请人工检查游戏。")
    return StageOutcome(result=result, stop_batch=status.stops_batch)


def _run_fast_routine_stage(task: dict, context: RunnerContext) -> StageOutcome:
    routine_id = task.get("fast_routine")
    if not routine_id:
        return StageOutcome()
    context.log(f"[fast {routine_id}] 程序快跑开始")
    result = run_routine(
        str(routine_id), context.device, {}, log=context.log,
        stop_event=context.stop_event, frame_cb=context.frame_cb,
    )
    if result.status is ExecutionStatus.DONE:
        return StageOutcome(result.to_task_dict(
            task["id"], steps=result.actions,
            detail=f"程序快跑：{result.detail}",
        ))
    if result.status is ExecutionStatus.BLOCKED:
        context.log(f"[fast {routine_id}] 转场/网络异常，停止后续任务")
        return StageOutcome(
            result.to_task_dict(task["id"], steps=result.actions),
            stop_batch=True,
        )
    if context.stop_event is not None and context.stop_event.is_set():
        stopped = ExecutionResult(ExecutionStatus.INCOMPLETE, detail="用户停止")
        return StageOutcome(stopped.to_task_dict(task["id"]), stop_batch=True)
    context.log(f"[fast {routine_id}] {result.status.value}: {result.detail} → LLM 接管")
    return StageOutcome()


def _run_flow_stage(task: dict, context: RunnerContext) -> StageOutcome:
    flow_id = task.get("flow")
    if not flow_id:
        return StageOutcome()
    flow_id = str(flow_id)
    flow_state = (
        context.evolution.flow_state(task["id"], flow_id)
        if context.evolution is not None else None
    )
    if flow_state == "degraded":
        context.log(f"[flow {flow_id}] 已降级，跳过旧候选 → LLM 修复路径")
        return StageOutcome(flow_failed=True)

    try:
        raw = run_flow(
            context.device, flow_id, log=context.log,
            frame_cb=context.frame_cb, stop_event=context.stop_event,
        )
        result = ExecutionResult.from_mapping(raw)
    except Exception as exc:
        result = ExecutionResult(
            ExecutionStatus.FAILED,
            detail=f"{exc.__class__.__name__}: {exc}",
        )

    # 显式关闭学习时维持旧的纯 flow 模式；账本本应启用却不可读时必须 fail closed。
    needs_verification = (
        result.status is ExecutionStatus.DONE
        and flow_state != "trusted"
        and (context.evolution is not None or context.evolution_expected)
    )
    if needs_verification:
        try:
            frame = context.device.screenshot()
            safe_callback(context.frame_cb, frame, log=context.log, label="frame")
            ok, reason = context.brain.verify(
                task["prompt"], task.get("exit_condition", ""), frame
            )
        except Exception as exc:
            result = ExecutionResult(
                ExecutionStatus.INCOMPLETE,
                detail=f"shadow 验收异常: {exc.__class__.__name__}: {exc}",
                steps=result.steps,
            )
        else:
            if ok:
                context.log(f"[evolve] shadow flow {flow_id} 终态验收通过：{reason}")
            else:
                result = ExecutionResult(
                    ExecutionStatus.FAILED,
                    detail=f"shadow 终态验收未通过: {reason}",
                    steps=result.steps,
                )

    if (context.evolution is not None
            and result.status in {ExecutionStatus.DONE, ExecutionStatus.FAILED}):
        try:
            meta = context.evolution.record_flow_result(
                task["id"], flow_id, result.status.value, result.detail
            )
        except EvolutionError as exc:
            context.log(f"[evolve] {exc}；快跑结果仍按原状态返回")
        else:
            flow_state = meta.get("state")
            context.log(
                f"[evolve] flow {flow_id} 状态={flow_state} "
                f"连续成功={meta.get('consecutive_successes', 0)}/"
                f"{context.evolution.trusted_successes}"
            )

    if result.status is ExecutionStatus.DONE:
        seconds = result.extra.get("seconds", "")
        detail = f"flow 快跑 {seconds}s" + (f" [{flow_state}]" if flow_state else "")
        return StageOutcome(result.to_task_dict(task["id"], detail=detail))
    if context.stop_event is not None and context.stop_event.is_set():
        stopped = ExecutionResult(ExecutionStatus.INCOMPLETE, detail="用户停止")
        return StageOutcome(stopped.to_task_dict(task["id"]), stop_batch=True)
    context.log(f"[flow {flow_id}] {result.status.value}: {result.detail} → LLM 接管")
    return StageOutcome(flow_failed=result.status is ExecutionStatus.FAILED)


def _evolve_success(task: dict, result: dict, brain: Brain,
                    evolution: EvolutionLedger, allow_compile: bool = True,
                    log=print) -> None:
    """聚合一次 slow-path 成功；证据足够时蒸馏为受限 shadow flow。"""
    record = result.get("record") or []
    try:
        decision = evolution.observe_success(
            task["id"], record, str(result.get("run_dir", ""))
        )
    except EvolutionError as exc:
        log(f"[evolve] 记录成功轨迹失败: {exc}")
        return
    if not decision["eligible"]:
        log(f"[evolve] 本次只记账，不编译：{decision['reason']}")
        return
    log(
        f"[evolve] 稳定轨迹 {decision['signature']} "
        f"证据={decision['observations']}/{evolution.observations}"
    )
    if not decision["should_compile"]:
        return
    if not allow_compile:
        log("[evolve] 本轮从失败 flow 的中途现场接管，只记账；"
            "下次从任务起点取得完整成功轨迹后再修复候选")
        return
    if task.get("fast_routine"):
        log(f"[evolve] {task['id']} 已有 fast_routine，保留专用程序")
        return

    flow_id = str(task.get("flow") or task["id"])
    run_dir = Path(str(result.get("run_dir", "")))
    try:
        generated = generate_flow(task, brain, run_dir, flow_id, log=log, device=None)
        if not generated:
            evolution.record_compile_failure(task["id"], "模型未生成可用 flow")
            return
        if not task.get("flow"):
            update_task(task["id"], flow=flow_id)
        meta = evolution.mark_compiled(
            task["id"], flow_id, str(run_dir), int(generated.get("steps", 0))
        )
        log(
            f"[evolve] 已生成 shadow flow {flow_id}（{meta.get('steps', 0)} 步）；"
            f"后续连续 {evolution.trusted_successes} 次快跑成功后转 trusted"
        )
    except Exception as exc:
        detail = f"{exc.__class__.__name__}: {exc}"
        try:
            evolution.record_compile_failure(task["id"], detail)
        except Exception as record_exc:
            log(f"[evolve] 编译失败且账本记录失败: {record_exc}")
        log(f"[evolve] 编译失败，保留 LLM 路径：{detail}")


def _consume_supplement(task: dict, brain: Brain, log=print) -> None:
    """成功后把 supplement 合入 prompt；失败时保留原字段。"""
    supplement = str(task.get("supplement") or "").strip()
    if not supplement:
        return
    log(f"[supplement] 「{task['id']}」执行成功，正在把补充情报合入任务指令…")
    try:
        new_prompt = brain.merge_supplement(
            task.get("name", task["id"]), task["prompt"], supplement
        )
        update_task(task["id"], prompt=new_prompt, supplement="")
        log(f"[supplement] 已合入并清空「{task['id']}」的补充情报（下次执行按新指令走）")
    except Exception as exc:
        log(
            f"[supplement] 合入失败（{exc.__class__.__name__}: {exc}），"
            "补充情报保留，下次执行成功后再试"
        )
