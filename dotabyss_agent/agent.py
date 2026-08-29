"""Episode 循环：单任务执行 + 完成验证 + 知识卡沉淀。

上下文管理遵循"任务即回合"：每步只携带 任务定义 + 知识卡 + 最近 N 步文本历史 +
当前帧截图，探索过程的图像不进历史（自动裁剪）。
"""
import time
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image

from .brain import Brain, BrainError
from .config import HISTORY_KEEP, KNOWLEDGE_DIR, RUNS_DIR
from .device import GameDevice

COORD_LIMIT = (1280, 720)


def knowledge_path(task_id: str) -> Path:
    return KNOWLEDGE_DIR / f"{task_id}.md"


def load_knowledge(task_id: str) -> str:
    p = knowledge_path(task_id)
    return p.read_text(encoding="utf-8").strip() if p.exists() else ""


def save_knowledge(task_id: str, text: str) -> None:
    KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
    knowledge_path(task_id).write_text(text.strip() + "\n", encoding="utf-8")


def _save_frame(frame: np.ndarray, run_dir: Path, name: str) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    p = run_dir / name
    Image.fromarray(frame[:, :, ::-1]).save(p)
    return p


def run_task(
    task: dict,
    device: GameDevice,
    brain: Brain,
    max_steps: int = 30,
    time_budget: float = 420.0,
    update_knowledge: bool = True,
    log=print,
) -> dict:
    """执行单个任务，返回 {"task", "status", "steps", "detail"}。

    status: done / failed / blocked / incomplete / error
    blocked = 疑似 403/网络错误，需要人工接手（上层应停止后续所有任务）。
    """
    tid = task["id"]
    run_dir = RUNS_DIR / datetime.now().strftime("%Y%m%d_%H%M%S") / tid
    knowledge = load_knowledge(tid)
    history: list[str] = []
    result = {"task": tid, "status": "error", "steps": 0, "detail": ""}
    t0 = time.time()
    parse_errors = 0

    # 无进展看门狗：点击后画面变化 ≥2% 视为"有进展"（翻页/弹窗/切屏都会重置计数），
    # 只有"无进展的重复点击"才累计报警——结算页连续翻"確認して次へ"不会被误杀
    pending_click: tuple[int, int, np.ndarray] | None = None  # (x, y, 点击前帧)
    prev_click: tuple[int, int] | None = None
    no_prog_streak = 0
    repeat_streak = 0

    for step in range(1, max_steps + 1):
        if time.time() - t0 > time_budget:
            result.update(status="incomplete", detail="时间预算耗尽，可重跑继续")
            break

        frame = device.screenshot()
        _save_frame(frame, run_dir, f"step{step:02d}.png")

        if pending_click is not None:
            cx, cy, pre_frame = pending_click
            pending_click = None
            if device.diff_ratio(pre_frame, frame) >= 0.02:
                no_prog_streak = 0
                repeat_streak = 0
            else:
                no_prog_streak += 1
                if prev_click and abs(cx - prev_click[0]) < 20 and abs(cy - prev_click[1]) < 20:
                    repeat_streak += 1
                else:
                    repeat_streak = 0
                if no_prog_streak >= 3 or repeat_streak >= 3:
                    result.update(
                        status="incomplete",
                        detail="连续 3 次点击无进展（无反应/重复点同一位置），判定卡住中止",
                    )
                    break

    for step in range(1, max_steps + 1):
        if time.time() - t0 > time_budget:
            result.update(status="incomplete", detail="时间预算耗尽，可重跑继续")
            break

        frame = device.screenshot()
        _save_frame(frame, run_dir, f"step{step:02d}.png")

        try:
            action = brain.decide(task["prompt"], knowledge, history, frame)
            parse_errors = 0
        except BrainError as e:
            parse_errors += 1
            history.append(f"step{step}: [解析失败 {e}]")
            log(f"step{step}: [解析失败] {e}")
            if parse_errors >= 3:
                result.update(detail=f"连续 {parse_errors} 次模型输出无法解析")
                break
            continue
        except Exception as e:  # API 网络等错误，稍后重试
            history.append(f"step{step}: [API 异常 {e.__class__.__name__}]")
            log(f"step{step}: [API 异常] {e}")
            time.sleep(5)
            continue

        act = action.get("action")
        thought = str(action.get("thought", ""))
        log(f"step{step}: [{act}] {thought}")

        if act == "click":
            x, y = int(action.get("x", -1)), int(action.get("y", -1))
            if not (0 <= x < COORD_LIMIT[0] and 0 <= y < COORD_LIMIT[1]):
                history.append(f"step{step}: [坐标越界 ({x},{y})] {thought}")
                continue
            device.click(x, y)
            device.wait_settled(frame)  # 等页面转场平息（慢加载的页面不再重复误点）
            history.append(f"step{step}: 点击({x},{y})｜{thought}")
            pending_click = (x, y, frame)
            prev_click = (x, y)

        elif act == "wait":
            s = min(float(action.get("seconds", 3)), 10.0)
            time.sleep(s)
            history.append(f"step{step}: 等待{s:.0f}s｜{thought}")

        elif act == "wait_stable":
            timeout = min(float(action.get("timeout", 60)), 150.0)
            ok = device.wait_until_stable(timeout=timeout)
            history.append(f"step{step}: 等待稳定({'达成' if ok else '超时'})｜{thought}")

        elif act == "report":
            status = str(action.get("status", ""))
            if status == "done":
                frame2 = device.screenshot()
                _save_frame(frame2, run_dir, "verify.png")
                ok, reason = brain.verify(task["prompt"], task.get("exit_condition", ""), frame2)
                if ok:
                    if update_knowledge:
                        try:
                            newk = brain.summarize_knowledge(
                                task.get("name", tid), knowledge, history
                            )
                            if newk:
                                save_knowledge(tid, newk)
                        except Exception as e:  # 知识卡失败不影响任务成功
                            log(f"[warn] 知识卡更新失败: {e}")
                    result.update(status="done", detail="验证通过", steps=step)
                else:
                    history.append(f"step{step}: 自报 done 但验证未通过：{reason}")
                    continue
            elif status == "blocked":
                result.update(status="blocked", detail=str(action.get("detail", "")), steps=step)
            else:  # failed
                result.update(status="failed", detail=str(action.get("detail", "")), steps=step)
            break

        else:
            history.append(f"step{step}: [未知动作 {act}]")

        history[:] = history[-HISTORY_KEEP:]
        result["steps"] = step

    if result["status"] == "error" and result["steps"] >= max_steps:
        result.update(status="incomplete", detail="步数上限")
    return result
