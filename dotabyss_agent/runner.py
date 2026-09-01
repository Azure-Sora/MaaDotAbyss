"""任务执行入口：CLI 与 GUI 共用的编排层。

任务若带 flow: <id> 字段 → 先走识图剧本（fast path，秒级）；
剧本失败/中断 → 自动落回 LLM 接管（slow path）。
"""
import time
from pathlib import Path

import yaml

from .agent import run_task
from .brain import Brain
from .config import TASKS_DIR
from .device import DeviceError
from .device_select import get_device
from .evolution import EvolutionError, EvolutionLedger
from .flow import run_flow
from .flowgen import generate_flow
from .macros import TransitionTimeout
from .precondition import check_precondition
from .routines import ROUTINES
from .taskfile import update_task


def load_tasks() -> list[dict]:
    data = yaml.safe_load((TASKS_DIR / "daily.yaml").read_text(encoding="utf-8"))
    return data["tasks"]


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
    """按顺序执行指定任务；返回逐任务结果。

    blocked（疑似 403）时立即停止后续任务。
    event_cb: callable(dict)，逐任务结果事件 {"type":"result", ...}，GUI 状态列用。
    """
    all_tasks = {t["id"]: t for t in load_tasks()}
    todo = [all_tasks[i] for i in task_ids if i in all_tasks]
    if not todo:
        log("没有匹配的任务")
        return []

    def _finish(res: dict):
        results.append(res)
        if event_cb is not None:
            try:
                event_cb({"type": "result", **res})
            except Exception:
                pass

    device = _device or get_device()[0]
    brain = _brain or Brain(provider=provider)
    try:
        evolution = EvolutionLedger() if update_knowledge else None
    except EvolutionError as e:
        evolution = None
        log(f"[evolve] {e}；本轮关闭自动演进，不影响任务执行")

    results = []
    blocked = False
    try:
        device.bring_to_front()
    except DeviceError as e:
        log(f"[设备] {e}")

    for t in todo:
        if stop_event is not None and stop_event.is_set():
            break
        log(f"===== {t['id']} ({t.get('name', '')}) =====")
        try:
            skip, reason = check_precondition(t, device, brain)
        except Exception as e:
            skip, reason = False, f"前置检查异常({e.__class__.__name__})，保守执行"
        if skip:
            log(f"[skipped] {reason}")
            _finish({"task": t["id"], "status": "skipped", "steps": 0, "detail": reason})
            continue
        if reason:
            log(f"[precondition] {reason}")
        flow_failed_this_run = False

        fast_routine = t.get("fast_routine")
        if fast_routine:
            fn = ROUTINES.get(str(fast_routine))
            if fn is None:
                rr = {"status": "partial", "detail": f"未知 fast_routine: {fast_routine}"}
            else:
                log(f"[fast {fast_routine}] 程序快跑开始")
                try:
                    rr = fn(device, {}, log=log, stop_event=stop_event, frame_cb=frame_cb)
                except TransitionTimeout as e:
                    rr = {"status": "blocked", "detail": str(e)}
                except Exception as e:
                    rr = {"status": "partial", "detail": f"{e.__class__.__name__}: {e}"}
            if rr.get("status") == "done":
                _finish({"task": t["id"], "status": "done",
                         "steps": rr.get("actions", 0),
                         "detail": f"程序快跑：{rr.get('detail', '')}"})
                continue
            if rr.get("status") == "blocked":
                _finish({"task": t["id"], "status": "blocked",
                         "steps": rr.get("actions", 0), "detail": rr.get("detail", "")})
                log(f"[fast {fast_routine}] 转场/网络异常，停止后续任务")
                break
            if stop_event is not None and stop_event.is_set():
                _finish({"task": t["id"], "status": "incomplete", "steps": 0,
                         "detail": "用户停止"})
                break
            log(f"[fast {fast_routine}] {rr.get('status')}: {rr.get('detail')} → LLM 接管")

        flow_id = t.get("flow")
        if flow_id:
            flow_state = evolution.flow_state(t["id"], str(flow_id)) if evolution else None
            fr = None
            if flow_state == "degraded":
                log(f"[flow {flow_id}] 已降级，跳过旧候选 → LLM 修复路径")
            else:
                try:
                    fr = run_flow(device, flow_id, log=log, frame_cb=frame_cb,
                                  stop_event=stop_event)
                except Exception as e:
                    fr = {"status": "failed", "detail": f"{e.__class__.__name__}: {e}"}
            # shadow 是保险期，不是“flow 步骤走完就盲信”。候选转 trusted 前仍用
            # 一张终态截图做独立验收；连续通过门槛后才完全取消模型调用。
            if (fr is not None and fr.get("status") == "done"
                    and evolution is not None and flow_state != "trusted"):
                frame = device.screenshot()
                if frame_cb is not None:
                    try:
                        frame_cb(frame)
                    except Exception:
                        pass
                try:
                    ok, reason = brain.verify(
                        t["prompt"], t.get("exit_condition", ""), frame
                    )
                except Exception as e:
                    fr = {"status": "incomplete",
                          "detail": f"shadow 验收异常: {e.__class__.__name__}: {e}"}
                else:
                    if ok:
                        log(f"[evolve] shadow flow {flow_id} 终态验收通过：{reason}")
                    else:
                        fr = {"status": "failed", "step": fr.get("step", 0),
                              "detail": f"shadow 终态验收未通过: {reason}"}
            if fr is not None and evolution is not None and fr.get("status") in {"done", "failed"}:
                try:
                    meta = evolution.record_flow_result(
                        t["id"], str(flow_id), str(fr.get("status")),
                        str(fr.get("detail", ""))
                    )
                except EvolutionError as e:
                    log(f"[evolve] {e}；快跑结果仍按原状态返回")
                else:
                    flow_state = meta.get("state")
                    log(f"[evolve] flow {flow_id} 状态={flow_state} "
                        f"连续成功={meta.get('consecutive_successes', 0)}/"
                        f"{evolution.trusted_successes}")
            if fr is not None and fr.get("status") == "done":
                _finish({"task": t["id"], "status": "done", "steps": fr.get("step", 0),
                         "detail": f"flow 快跑 {fr.get('seconds', '')}s"
                                   + (f" [{flow_state}]" if flow_state else "")})
                continue
            if fr is not None and fr.get("status") == "failed":
                flow_failed_this_run = True
            if stop_event is not None and stop_event.is_set():
                results.append({"task": t["id"], "status": "incomplete", "steps": 0, "detail": "用户停止"})
                break
            if fr is not None:
                log(f"[flow {flow_id}] {fr.get('status')}: {fr.get('detail')} → LLM 接管")

        r = run_task(
            t,
            device,
            brain,
            max_steps=max_steps,
            time_budget=time_budget,
            # 学习统一由下方 evolution 做：agent 不再在 report done 后额外调用一次
            # summarize_knowledge，避免用户等待无收益的尾部模型请求。
            update_knowledge=False,
            log=log,
            stop_event=stop_event,
            frame_cb=frame_cb,
            record=update_knowledge,
            event_cb=event_cb,
        )
        if r["status"] == "done" and evolution is not None:
            _evolve_success(t, r, brain, evolution,
                            allow_compile=not flow_failed_this_run, log=log)
        _finish(r)
        log(f"[{r['status']}] {r['task']} steps={r['steps']} {r['detail']}")
        if r["status"] == "done" and t.get("supplement"):
            _consume_supplement(t, brain, log=log)
        if r["status"] == "blocked":
            blocked = True
            log("!! 疑似 403/网络错误，已停止全部任务，请人工检查游戏。")
            break

    return results


def _evolve_success(t: dict, result: dict, brain: Brain,
                    evolution: EvolutionLedger, allow_compile: bool = True,
                    log=print) -> None:
    """聚合一次 slow-path 成功；证据足够时蒸馏为受限 shadow flow。"""
    record = result.get("record") or []
    try:
        decision = evolution.observe_success(
            t["id"], record, str(result.get("run_dir", ""))
        )
    except EvolutionError as e:
        log(f"[evolve] 记录成功轨迹失败: {e}")
        return
    if not decision["eligible"]:
        log(f"[evolve] 本次只记账，不编译：{decision['reason']}")
        return
    log(f"[evolve] 稳定轨迹 {decision['signature']} "
        f"证据={decision['observations']}/{evolution.observations}")
    if not decision["should_compile"]:
        return
    if not allow_compile:
        log("[evolve] 本轮从失败 flow 的中途现场接管，只记账；"
            "下次从任务起点取得完整成功轨迹后再修复候选")
        return

    # 专用 Python routine 已是更强的确定性实现；不拿通用 click flow 覆盖它。
    if t.get("fast_routine"):
        log(f"[evolve] {t['id']} 已有 fast_routine，保留专用程序")
        return
    flow_id = str(t.get("flow") or t["id"])
    run_dir = Path(str(result.get("run_dir", "")))
    try:
        generated = generate_flow(t, brain, run_dir, flow_id, log=log, device=None)
        if not generated:
            evolution.record_compile_failure(t["id"], "模型未生成可用 flow")
            return
        if not t.get("flow"):
            update_task(t["id"], flow=flow_id)
        meta = evolution.mark_compiled(
            t["id"], flow_id, str(run_dir), int(generated.get("steps", 0))
        )
        log(f"[evolve] 已生成 shadow flow {flow_id}（{meta.get('steps', 0)} 步）；"
            f"后续连续 {evolution.trusted_successes} 次快跑成功后转 trusted")
    except Exception as e:
        detail = f"{e.__class__.__name__}: {e}"
        try:
            evolution.record_compile_failure(t["id"], detail)
        except Exception:
            pass
        log(f"[evolve] 编译失败，保留 LLM 路径：{detail}")


def _consume_supplement(t: dict, brain: Brain, log=print) -> None:
    """任务带补充情报且执行成功 → 模型改稿合入 prompt 并清空 supplement 字段。

    合稿失败（解析异常/结果过短）不动文件，supplement 留着下次成功后再试。
    """
    sup = str(t.get("supplement") or "").strip()
    if not sup:
        return
    log(f"[supplement] 「{t['id']}」执行成功，正在把补充情报合入任务指令…")
    try:
        new_prompt = brain.merge_supplement(t.get("name", t["id"]), t["prompt"], sup)
        update_task(t["id"], prompt=new_prompt, supplement="")
        log(f"[supplement] 已合入并清空「{t['id']}」的补充情报（下次执行按新指令走）")
    except Exception as e:
        log(f"[supplement] 合入失败（{e.__class__.__name__}: {e}），"
            f"补充情报保留，下次执行成功后再试")
