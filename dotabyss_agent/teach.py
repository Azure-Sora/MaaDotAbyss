"""交互式新建任务（教学模式）：人在回路的探索录制 + 蒸馏产出任务三件套。

设计文档：docs/research/11-交互式新建任务设计.md
状态机：AUTO（模型自主）↔ AWAITING（等用户答复；含看门狗强制提问）→ 用户宣布完成
        → DISTILLING（离线蒸馏）→ DONE。中止则只存档不蒸馏。

通道约定：
- reply_get(timeout) -> {"kind": "msg"|"finish"|"abort", "text": str}，无消息抛 queue.Empty；
  等待期间会话自己持续截图，保持画面预览活性（游戏须前台，等待时用户可随意打字）。
- event_cb(dict)：GUI/CLI 事件流——
  {"type":"state","state":...}  {"type":"chat","role":"agent"|"user"|"system","text":...}
  {"type":"step",...}（与 agent.run_task 同构）  {"type":"result",...}
- 用户消息全量入轨迹与决策上下文（聊天记录即任务规格，永不裁剪）。
"""
import json
import queue
import time
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image

from .brain import Brain, BrainError
from .config import HISTORY_KEEP, RUNS_DIR, TASKS_DIR
from .flowgen import generate_flow
from .agent import save_knowledge

COORD_LIMIT = (1280, 720)
DAILY_YAML = TASKS_DIR / "daily.yaml"


def run_teach_session(
    task_id: str,
    name: str,
    goal: str,
    device,
    brain: Brain,
    log=print,
    stop_event=None,
    frame_cb=None,
    event_cb=None,
    reply_get=None,
    max_steps: int = 120,
) -> dict:
    """教学会话主循环。返回 {"task","status","steps","run_dir","task_card"?}。

    status: distilled（完成并入库）/ aborted（中止，仅存档）/ blocked
    """
    brain.task_ctx = task_id          # 用量统计归属
    run_dir = RUNS_DIR / datetime.now().strftime("%Y%m%d_%H%M%S") / f"teach_{task_id}"
    run_dir.mkdir(parents=True, exist_ok=True)
    dialogue: list[dict] = []      # {"step","role","text"}
    instructions: list[str] = []   # 用户消息全量（=任务规格）
    history: list[str] = []
    record: list[dict] = []        # 与 agent.run_task 同构（flowgen 直接消费）
    result = {"task": task_id, "status": "aborted", "steps": 0, "detail": "", "run_dir": str(run_dir)}

    def chat(role: str, text: str, step: int = 0):
        dialogue.append({"step": step, "role": role, "text": text})
        _emit(event_cb, {"type": "chat", "role": role, "text": text})

    def set_state(state: str):
        _emit(event_cb, {"type": "state", "state": state})

    chat("system", f"教学会话开始：{name}（{task_id}）——目标：{goal}")
    set_state("auto")

    parse_errors = 0
    pending_click = None      # (x, y, 点击前帧)
    prev_click = None
    no_prog_streak = 0
    repeat_streak = 0
    step = 0

    while step < max_steps:
        step += 1
        result["steps"] = step
        if stop_event is not None and stop_event.is_set():
            result["detail"] = "用户停止"
            break

        frame = device.screenshot()
        _save_frame(frame, run_dir, f"step{step:02d}.png")
        if frame_cb is not None:
            try:
                frame_cb(frame)
            except Exception:
                pass

        # 看门狗：点击无进展 → 不中止，挂起向用户提问（教学版的根本差异）
        if pending_click is not None:
            cx, cy, pre_frame = pending_click
            pending_click = None
            diff = device.diff_ratio(pre_frame, frame)
            if record and record[-1]["step"] == step - 1:
                record[-1]["eff"] = round(float(diff), 3)
            if diff >= 0.02:
                no_prog_streak = 0
                repeat_streak = 0
            else:
                no_prog_streak += 1
                if prev_click and abs(cx - prev_click[0]) < 20 and abs(cy - prev_click[1]) < 20:
                    repeat_streak += 1
                else:
                    repeat_streak = 0
                if no_prog_streak >= 3 or repeat_streak >= 3:
                    chat("system", "我连续 3 次点击画面都没有变化，已暂停。请告诉我现状或下一步该怎么做。", step)
                    set_state("awaiting")
                    rep = _wait_reply(device, reply_get, stop_event, frame_cb)
                    set_state("auto")
                    if not _apply_reply(rep, instructions, history, chat, step, result):
                        break
                    no_prog_streak = 0
                    repeat_streak = 0
                    continue

        _emit(event_cb, {"type": "thinking", "phase": "start"})
        try:
            action = brain.decide_teach(goal, instructions, history, frame)
        except BrainError as e:
            parse_errors += 1
            history.append(f"step{step}: [解析失败 {e}]")
            if parse_errors >= 3:
                result["detail"] = f"连续 {parse_errors} 次模型输出无法解析"
                break
            continue
        except Exception as e:  # API 网络等错误，稍后重试
            history.append(f"step{step}: [API 异常 {e.__class__.__name__}]")
            log(f"step{step}: [API 异常] {e}")
            time.sleep(5)
            continue
        finally:
            _emit(event_cb, {"type": "thinking", "phase": "done",
                             "tokens": getattr(brain, "last_completion_tokens", None)})
        parse_errors = 0

        act = action.get("action")
        thought = str(action.get("thought", ""))
        log(f"step{step}: [{act}] {thought}")
        _emit(event_cb, {
            "type": "step", "task": task_id, "step": step, "action": act,
            "detail": {k: v for k, v in action.items() if k != "thought"},
            "thought": thought, "frame": frame,
        })

        if act == "click":
            x, y = int(action.get("x", -1)), int(action.get("y", -1))
            if not (0 <= x < COORD_LIMIT[0] and 0 <= y < COORD_LIMIT[1]):
                history.append(f"step{step}: [坐标越界 ({x},{y})] {thought}")
                continue
            if not device.tap(x, y):
                # 真实点击语义：未命中=被遮挡/不可点，不穿透；不录无效点击
                history.append(f"step{step}: [点击未命中 ({x},{y})——目标被遮挡或不可点] {thought}")
                continue
            frames_dir = run_dir / "frames"
            frames_dir.mkdir(exist_ok=True)
            pre_path = frames_dir / f"s{step:02d}_pre.png"
            Image.fromarray(frame[:, :, ::-1]).save(pre_path)
            record.append({
                "step": step, "action": "click", "x": x, "y": y,
                "thought": thought, "pre": str(pre_path.relative_to(run_dir)),
            })
            device.wait_settled(frame)
            history.append(f"step{step}: 点击({x},{y})｜{thought}")
            pending_click = (x, y, frame)
            prev_click = (x, y)

        elif act == "skip":
            # 无按钮翻页/结算提示（確認して次へ 等）：点文字会穿透，统一点左上角
            device.skip_page()
            device.wait_settled(frame)
            history.append(f"step{step}: 左上角跳页｜{thought}")
            pending_click = (0, 0, frame)
            prev_click = (0, 0)

        elif act == "wait":
            s = min(float(action.get("seconds", 3)), 10.0)
            time.sleep(s)
            history.append(f"step{step}: 等待{s:.0f}s｜{thought}")

        elif act == "wait_stable":
            timeout = min(float(action.get("timeout", 60)), 150.0)
            ok = device.wait_until_stable(timeout=timeout)
            history.append(f"step{step}: 等待稳定({'达成' if ok else '超时'})｜{thought}")

        elif act == "ask_user":
            q = str(action.get("question", "需要指示"))
            guess = str(action.get("guess", ""))
            chat("agent", q + (f"（{guess}）" if guess else ""), step)
            set_state("awaiting")
            rep = _wait_reply(device, reply_get, stop_event, frame_cb)
            set_state("auto")
            if not _apply_reply(rep, instructions, history, chat, step, result):
                break

        elif act == "report":
            status = str(action.get("status", ""))
            if status == "blocked":
                result["status"] = "blocked"
                result["detail"] = str(action.get("detail", ""))
                chat("system", f"检测到网络异常（{result['detail']}），会话中止，请人工检查游戏。", step)
                break
            # 教学模式里模型不宣布完成：把它的判断抛给用户确认
            chat("agent", f"我认为任务已经完成：{action.get('detail', '')}。请确认——完成请点「完成教学」，否则直接输入下一步指示。", step)
            set_state("awaiting")
            rep = _wait_reply(device, reply_get, stop_event, frame_cb)
            set_state("auto")
            if not _apply_reply(rep, instructions, history, chat, step, result):
                break

        else:
            history.append(f"step{step}: [未知动作 {act}]")

        history[:] = history[-HISTORY_KEEP:]

    if result["status"] != "distilled" and not result["detail"]:
        result["detail"] = "步数上限"

    # ---- 存档 ----
    (run_dir / "session.json").write_text(
        json.dumps({"task_id": task_id, "name": name, "goal": goal,
                    "status": result["status"], "dialogue": dialogue,
                    "instructions": instructions},
                   ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    if record:
        (run_dir / "record.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    # ---- 蒸馏（仅正常完成；事后验证——入库即 shadow，下次执行算保险期） ----
    if result["status"] == "distilled":
        set_state("distilling")
        chat("system", "正在蒸馏：生成识图剧本与任务规格…", step)
        try:
            card = brain.summarize_session(
                name, goal,
                [f"{d['role']}: {d['text']}" for d in dialogue if d["role"] == "user"],
                [f"step{r['step']}: click({r['x']},{r['y']}) eff={r.get('eff', '?')}｜{r.get('thought', '')}"
                 for r in record if r.get("action") == "click"],
            )
            task_card = {"id": task_id, "name": name, **card}
            (run_dir / "task_card.json").write_text(
                json.dumps(task_card, ensure_ascii=False, indent=1), encoding="utf-8")
            save_knowledge(task_id, "\n".join(f"- {n}" for n in card["notes"]))
            flow_ok = False
            if record:
                fr = generate_flow({"name": name}, brain, run_dir, task_id, log=log, device=None)
                flow_ok = fr is not None
            _append_task_entry(DAILY_YAML, task_card, flow_ok)
            result["task_card"] = task_card
            result["flow_generated"] = flow_ok
            result["detail"] = "已入库（shadow，下次执行即保险期验证）"
            chat("system", "✅ 蒸馏完成：任务已写入任务清单（shadow 状态，下次执行自动验证转正）。", step)
        except Exception as e:
            result["status"] = "aborted"
            result["detail"] = f"蒸馏失败: {e.__class__.__name__}: {e}"
            chat("system", f"⚠️ 蒸馏失败（{result['detail']}）；会话轨迹已存档，可重试。", step)
        set_state("done")

    _emit(event_cb, {"type": "result", **result})
    return result


# ---- 辅助 ---------------------------------------------------------------

def _emit(cb, ev: dict):
    if cb is None:
        return
    try:
        cb(ev)
    except Exception:
        pass


def _save_frame(frame: np.ndarray, run_dir: Path, name: str) -> None:
    Image.fromarray(frame[:, :, ::-1]).save(run_dir / name)


def _wait_reply(device, reply_get, stop_event, frame_cb) -> dict:
    """等待用户答复；等待期持续截图保持预览活性（device 单线程访问，无竞态）。"""
    while True:
        if stop_event is not None and stop_event.is_set():
            return {"kind": "abort", "text": "用户停止"}
        try:
            return reply_get(timeout=0.5)
        except queue.Empty:
            pass
        except Exception as e:
            log_err = f"[reply 通道异常] {e}"
            print(log_err)
            time.sleep(0.5)
            continue
        try:
            if frame_cb is not None:
                frame_cb(device.screenshot())
        except Exception:
            pass


def _apply_reply(rep: dict, instructions: list[str], history: list[str],
                 chat, step: int, result: dict) -> bool:
    """处理用户答复。返回 True 继续会话；False 结束（finish/abort 已落 status）。"""
    kind = (rep or {}).get("kind", "msg")
    text = str((rep or {}).get("text", "")).strip()
    if kind == "finish":
        result["status"] = "distilled"
        result["detail"] = "用户宣布完成"
        chat("user", "（任务完成，开始蒸馏）", step)
        return False
    if kind == "abort":
        result["status"] = "aborted"
        result["detail"] = "用户中止教学"
        chat("user", "（中止教学）", step)
        return False
    if not text:
        return True
    chat("user", text, step)
    instructions.append(text)
    history.append(f"step{step}: 用户指示：{text}")
    return True


def _append_task_entry(daily_path: Path, card: dict, flow_ok: bool) -> None:
    """向 daily.yaml 的 tasks 列表末尾追加新任务（文本追加，保留原文件注释）。"""
    entry = [f"  - id: {card['id']}", f"    name: {card['name']}"]
    if flow_ok:
        entry.append(f"    flow: {card['id']}          # 教学：先剧本后 LLM 兜底")
    entry.append("    prompt: |")
    for line in str(card.get("prompt", "")).rstrip().splitlines():
        entry.append(f"      {line}" if line.strip() else "")
    entry.append(f"    exit_condition: {card.get('exit_condition', '')}")
    with open(daily_path, "a", encoding="utf-8") as f:
        f.write("\n".join(entry) + "\n")
