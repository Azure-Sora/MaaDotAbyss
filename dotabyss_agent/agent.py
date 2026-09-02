"""Episode 循环：单任务执行 + 完成验证 + 知识卡沉淀。

模型循环只负责感知、决策和进度看门狗；设备动作由 ActionExecutor 解释执行。
"""
import json
import time
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image

from .action_executor import ActionExecutor
from .brain import Brain, BrainError
from .config import HISTORY_KEEP, KNOWLEDGE_DIR, RUNS_DIR
from .device import GameDevice
from .execution import ExecutionStatus, safe_callback
from .routines import ROUTINES  # 兼容现有测试/扩展对 agent.ROUTINES 的运行时注入


def forbidden_scene(device) -> str | None:
    """点击后误入抽卡页时返回场景名，否则 None。"""
    if not hasattr(device, "ui_tree"):
        return None
    try:
        scene = str(device.ui_tree(max_nodes=10).get("scene", ""))
    except Exception as exc:
        raise RuntimeError(
            f"禁区场景检查失败: {exc.__class__.__name__}: {exc}"
        ) from exc
    return scene if "gacha" in scene.lower() else None


def knowledge_path(task_id: str) -> Path:
    return KNOWLEDGE_DIR / f"{task_id}.md"


def load_knowledge(task_id: str) -> str:
    path = knowledge_path(task_id)
    return path.read_text(encoding="utf-8").strip() if path.exists() else ""


def save_knowledge(task_id: str, text: str) -> None:
    KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
    knowledge_path(task_id).write_text(text.strip() + "\n", encoding="utf-8")


def _save_frame(frame: np.ndarray, run_dir: Path, name: str) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / name
    Image.fromarray(frame[:, :, ::-1]).save(path)
    return path


def _emit_thinking(event_cb, phase: str, brain=None, log=print) -> None:
    event = {"type": "thinking", "phase": phase}
    if phase == "done" and brain is not None:
        event["tokens"] = getattr(brain, "last_completion_tokens", None)
    safe_callback(event_cb, event, log=log, label="thinking")


def run_task(
    task: dict,
    device: GameDevice,
    brain: Brain,
    max_steps: int = 30,
    time_budget: float = 420.0,
    update_knowledge: bool = True,
    log=print,
    stop_event=None,
    frame_cb=None,
    record: bool = False,
    event_cb=None,
) -> dict:
    """执行单个任务；外部返回 dict 契约保持兼容。"""
    task_id = task["id"]
    brain.task_ctx = task_id          # 用量统计归属（executor 里的验收/知识卡同享）
    run_dir = RUNS_DIR / datetime.now().strftime("%Y%m%d_%H%M%S") / task_id
    knowledge = load_knowledge(task_id)
    supplement = str(task.get("supplement") or "").strip()
    history: list[str] = []
    result = {"task": task_id, "status": "error", "steps": 0, "detail": ""}
    started = time.time()
    parse_errors = 0

    pending_click: tuple[int, int, np.ndarray] | None = None
    previous_click: tuple[int, int] | None = None
    no_progress_streak = 0
    repeat_streak = 0
    record_list: list[dict] = []
    executor = ActionExecutor(
        task=task, device=device, brain=brain, run_dir=run_dir,
        knowledge=knowledge, history=history, record=record,
        record_list=record_list, update_knowledge=update_knowledge,
        save_frame=_save_frame, save_knowledge=save_knowledge,
        forbidden_scene=forbidden_scene,
        emit_thinking=lambda callback, phase, owner=None: _emit_thinking(
            callback, phase, owner, log=log
        ),
        log=log, stop_event=stop_event, frame_cb=frame_cb, event_cb=event_cb,
    )

    for step in range(1, max_steps + 1):
        if stop_event is not None and stop_event.is_set():
            result.update(status=ExecutionStatus.INCOMPLETE.value, detail="用户停止")
            break
        if time.time() - started > time_budget:
            result.update(status=ExecutionStatus.INCOMPLETE.value,
                          detail="时间预算耗尽，可重跑继续")
            break

        frame = device.screenshot()
        _save_frame(frame, run_dir, f"step{step:02d}.png")
        safe_callback(frame_cb, frame, log=log, label="frame")

        if pending_click is not None:
            x, y, pre_frame = pending_click
            pending_click = None
            diff = device.diff_ratio(pre_frame, frame)
            if record_list and record_list[-1]["step"] == step - 1:
                record_list[-1]["eff"] = round(float(diff), 3)
            if diff >= 0.02:
                no_progress_streak = 0
                repeat_streak = 0
            else:
                no_progress_streak += 1
                if (previous_click and abs(x - previous_click[0]) < 20
                        and abs(y - previous_click[1]) < 20):
                    repeat_streak += 1
                else:
                    repeat_streak = 0
                if no_progress_streak >= 3 or repeat_streak >= 3:
                    result.update(
                        status=ExecutionStatus.INCOMPLETE.value,
                        detail="连续 3 次点击无进展（无反应/重复点同一位置），判定卡住中止",
                    )
                    break

        _emit_thinking(event_cb, "start", log=log)
        try:
            action = brain.decide(task["prompt"], knowledge, history, frame,
                                  supplement=supplement)
            parse_errors = 0
        except BrainError as exc:
            parse_errors += 1
            history.append(f"step{step}: [解析失败 {exc}]")
            log(f"step{step}: [解析失败] {exc}")
            if parse_errors >= 3:
                result.update(detail=f"连续 {parse_errors} 次模型输出无法解析")
                break
            continue
        except Exception as exc:
            history.append(f"step{step}: [API 异常 {exc.__class__.__name__}]")
            log(f"step{step}: [API 异常] {exc}")
            time.sleep(5)
            continue
        finally:
            _emit_thinking(event_cb, "done", brain, log=log)

        action_name = str(action.get("action") or "")
        thought = str(action.get("thought", ""))
        log(f"step{step}: [{action_name}] {thought}")
        safe_callback(event_cb, {
            "type": "step", "task": task_id, "step": step,
            "action": action_name,
            "detail": {k: v for k, v in action.items() if k != "thought"},
            "thought": thought, "frame": frame,
        }, log=log, label="step event")

        outcome = executor.execute(action, frame, step)
        started += outcome.budget_credit
        if outcome.pending_click is not None:
            pending_click = outcome.pending_click
        if outcome.previous_click is not None:
            previous_click = outcome.previous_click
        history[:] = history[-HISTORY_KEEP:]
        result["steps"] = step
        if outcome.terminal is not None:
            result.update(
                status=outcome.terminal.status.value,
                detail=outcome.terminal.detail,
                steps=outcome.terminal.steps or step,
            )
            break

    if result["status"] == ExecutionStatus.ERROR.value and result["steps"] >= max_steps:
        result.update(status=ExecutionStatus.INCOMPLETE.value, detail="步数上限")
    result["run_dir"] = str(run_dir)
    if record:
        (run_dir / "record.json").write_text(
            json.dumps(record_list, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        result["record"] = record_list
    return result
