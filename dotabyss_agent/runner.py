"""任务执行入口：CLI 与 GUI 共用的编排层。

任务若带 flow: <id> 字段 → 先走识图剧本（fast path，秒级）；
剧本失败/中断 → 自动落回 LLM 接管（slow path）。
"""
import time

import yaml

from .agent import run_task
from .brain import Brain
from .config import TASKS_DIR
from .device import DeviceError
from .device_select import get_device
from .flow import run_flow
from .precondition import check_precondition


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

        flow_id = t.get("flow")
        if flow_id:
            try:
                fr = run_flow(device, flow_id, log=log, frame_cb=frame_cb, stop_event=stop_event)
            except Exception as e:
                fr = {"status": "failed", "detail": f"{e.__class__.__name__}: {e}"}
            if fr.get("status") == "done":
                _finish({"task": t["id"], "status": "done", "steps": fr.get("step", 0),
                         "detail": f"flow 快跑 {fr.get('seconds', '')}s"})
                continue
            if stop_event is not None and stop_event.is_set():
                results.append({"task": t["id"], "status": "incomplete", "steps": 0, "detail": "用户停止"})
                break
            log(f"[flow {flow_id}] {fr.get('status')}: {fr.get('detail')} → LLM 接管")

        r = run_task(
            t,
            device,
            brain,
            max_steps=max_steps,
            time_budget=time_budget,
            update_knowledge=update_knowledge,
            log=log,
            stop_event=stop_event,
            frame_cb=frame_cb,
            event_cb=event_cb,
        )
        _finish(r)
        log(f"[{r['status']}] {r['task']} steps={r['steps']} {r['detail']}")
        if r["status"] == "blocked":
            blocked = True
            log("!! 疑似 403/网络错误，已停止全部任务，请人工检查游戏。")
            break

    return results
