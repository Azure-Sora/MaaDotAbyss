"""LLM 动作解释器：把模型循环与设备副作用分开。"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
from PIL import Image

from .execution import ExecutionResult, ExecutionStatus, safe_callback
from .macros import observe_buttons, wait_transition_done
from .routines import available_routines, run_routine, save_and_register


COORD_LIMIT = (1280, 720)
PATH_BLACKLIST = ("Gacha", "ガチャ", "親密", "好感", "PullOut", "Retreat")


@dataclass(slots=True)
class ActionOutcome:
    pending_click: tuple[int, int, np.ndarray] | None = None
    previous_click: tuple[int, int] | None = None
    terminal: ExecutionResult | None = None
    budget_credit: float = 0.0


class ActionExecutor:
    """执行一个已解析动作，并以结构化 outcome 返回对 episode 的影响。"""

    def __init__(self, *, task: dict, device, brain, run_dir: Path,
                 knowledge: str, history: list[str], record: bool,
                 record_list: list[dict], update_knowledge: bool,
                 save_frame: Callable, save_knowledge: Callable,
                 forbidden_scene: Callable, emit_thinking: Callable,
                 log=print, stop_event=None, frame_cb=None, event_cb=None):
        self.task = task
        self.device = device
        self.brain = brain
        self.run_dir = run_dir
        self.knowledge = knowledge
        self.history = history
        self.record = record
        self.record_list = record_list
        self.update_knowledge = update_knowledge
        self.save_frame = save_frame
        self.save_knowledge = save_knowledge
        self.forbidden_scene = forbidden_scene
        self.emit_thinking = emit_thinking
        self.log = log
        self.stop_event = stop_event
        self.frame_cb = frame_cb
        self.event_cb = event_cb
        self.frames_dir = run_dir / "frames"
        self.handlers = {
            "click": self._click,
            "observe": self._observe,
            "click_path": self._click_path,
            "skip": self._skip,
            "wait": self._wait,
            "wait_stable": self._wait_stable,
            "auto": self._auto,
            "report": self._report,
        }

    def execute(self, action: dict, frame: np.ndarray, step: int) -> ActionOutcome:
        act = str(action.get("action") or "")
        thought = str(action.get("thought", ""))
        if self.record and act != "click":
            row = {"step": step, "action": act, "thought": thought}
            for key in ("routine", "path", "seconds", "timeout", "status", "evidence"):
                if key in action:
                    row[key] = action[key]
            self.record_list.append(row)
        handler = self.handlers.get(act)
        if handler is None:
            self.history.append(f"step{step}: [未知动作 {act}]")
            return ActionOutcome()
        return handler(action, frame, step, thought)

    def _blocked_after_input(self, step: int, *, check_scene: bool = True) -> ExecutionResult | None:
        if not wait_transition_done(self.device):
            self.log("[红线] 转场动画未结束（疑似卡屏），停止后续点击，请人工检查游戏")
            return ExecutionResult(
                ExecutionStatus.BLOCKED, steps=step,
                detail="转场 loading 疑似卡死，已熔断（请人工恢复）",
            )
        if check_scene:
            try:
                bad = self.forbidden_scene(self.device)
            except Exception as exc:
                self.log(f"[红线] {exc}——无法确认安全场景，停止后续点击")
                return ExecutionResult(
                    ExecutionStatus.BLOCKED, steps=step,
                    detail=f"安全场景检查失败，已熔断（{exc}）",
                )
            if bad:
                self.log(f"[红线] 点击后误入禁区场景 {bad}——任务熔断，请人工退出该页面")
                return ExecutionResult(
                    ExecutionStatus.BLOCKED, steps=step,
                    detail=f"点击误入禁区场景 {bad}，已熔断（请人工退出）",
                )
        return None

    def _click(self, action: dict, frame: np.ndarray, step: int, thought: str) -> ActionOutcome:
        x, y = int(action.get("x", -1)), int(action.get("y", -1))
        if not (0 <= x < COORD_LIMIT[0] and 0 <= y < COORD_LIMIT[1]):
            self.history.append(f"step{step}: [坐标越界 ({x},{y})] {thought}")
            return ActionOutcome()
        if not self.device.tap(x, y):
            self.history.append(f"step{step}: [点击未命中 ({x},{y})——目标被遮挡或不可点] {thought}")
            self.log(f"step{step}: [点击未命中 ({x},{y})] 目标被遮挡或非可点目标（真实点击，不穿透）")
            return ActionOutcome()
        if self.record:
            self.frames_dir.mkdir(parents=True, exist_ok=True)
            pre_path = self.frames_dir / f"s{step:02d}_pre.png"
            Image.fromarray(frame[:, :, ::-1]).save(pre_path)
            self.record_list.append({
                "step": step, "action": "click", "x": x, "y": y,
                "thought": thought, "pre": str(pre_path.relative_to(self.run_dir)),
            })
        self.device.wait_settled(frame)
        terminal = self._blocked_after_input(step)
        if terminal:
            return ActionOutcome(terminal=terminal)
        self.history.append(f"step{step}: 点击({x},{y})｜{thought}")
        return ActionOutcome(pending_click=(x, y, frame), previous_click=(x, y))

    def _observe(self, action: dict, frame: np.ndarray, step: int, thought: str) -> ActionOutcome:
        if not hasattr(self.device, "ui_tree"):
            self.history.append(
                f"step{step}: [observe] 当前设备后端不支持 UI 树（仅桥后端可用），请改用截图判断"
            )
            return ActionOutcome()
        try:
            scene_name, rows, total = observe_buttons(
                self.device,
                canvas=str(action.get("canvas") or "") or None,
                suffix=str(action.get("suffix") or ""),
                contains=str(action.get("contains") or ""),
                text=str(action.get("text") or ""),
            )
        except Exception as exc:
            self.history.append(f"step{step}: [observe] 读树失败 {exc.__class__.__name__}: {exc}")
            return ActionOutcome()
        lines = [f"step{step}: [observe] scene={scene_name} 按钮{total}条"
                 + ("" if total <= len(rows) else f"（已截断至{len(rows)}条）")]
        lines += [f"  {row}" for row in rows]
        if total > len(rows):
            lines.append("  …其余未列出，请加 canvas/suffix/contains/text 过滤再 observe")
        self.history.append("\n".join(lines))
        self.log(f"step{step}: [observe] scene={scene_name} 按钮{total}条")
        return ActionOutcome()

    def _click_path(self, action: dict, frame: np.ndarray, step: int,
                    thought: str) -> ActionOutcome:
        if not hasattr(self.device, "click_by_path"):
            self.history.append(f"step{step}: [click_path] 当前设备后端不支持路径点击（仅桥后端可用）")
            return ActionOutcome()
        path = str(action.get("path", "")).strip()
        bad_word = next((word for word in PATH_BLACKLIST if word in path), None)
        if not path or bad_word:
            self.history.append(
                f"step{step}: [click_path] 路径为空或含禁区关键词（{bad_word or '空'}），拒绝"
            )
            return ActionOutcome()
        try:
            self.device.click_by_path(path)
        except Exception as exc:
            self.history.append(
                f"step{step}: [click_path 未命中] {path}（{exc}）——路径可能已过期，请重新 observe"
            )
            return ActionOutcome()
        self.device.wait_settled(frame)
        terminal = self._blocked_after_input(step)
        if terminal:
            return ActionOutcome(terminal=terminal)
        self.history.append(f"step{step}: 点路径 …/{path.split('/')[-1]}｜{thought}")
        return ActionOutcome(pending_click=(0, 0, frame), previous_click=(0, 0))

    def _skip(self, action: dict, frame: np.ndarray, step: int, thought: str) -> ActionOutcome:
        self.device.skip_page()
        self.device.wait_settled(frame)
        terminal = self._blocked_after_input(step)
        if terminal:
            return ActionOutcome(terminal=terminal)
        self.history.append(f"step{step}: 左上角跳页｜{thought}")
        return ActionOutcome(pending_click=(0, 0, frame), previous_click=(0, 0))

    def _wait(self, action: dict, frame: np.ndarray, step: int, thought: str) -> ActionOutcome:
        seconds = min(float(action.get("seconds", 3)), 10.0)
        time.sleep(seconds)
        self.history.append(f"step{step}: 等待{seconds:.0f}s｜{thought}")
        return ActionOutcome()

    def _wait_stable(self, action: dict, frame: np.ndarray, step: int,
                     thought: str) -> ActionOutcome:
        timeout = min(float(action.get("timeout", 60)), 150.0)
        ok = self.device.wait_until_stable(timeout=timeout)
        self.history.append(f"step{step}: 等待稳定({'达成' if ok else '超时'})｜{thought}")
        return ActionOutcome()

    def _auto(self, action: dict, frame: np.ndarray, step: int, thought: str) -> ActionOutcome:
        routine_id = str(action.get("routine", ""))
        if routine_id not in available_routines():
            self.history.append(
                f"step{step}: [auto] 未知 routine {routine_id}（可用: {', '.join(available_routines())}）"
            )
            return ActionOutcome()
        self.log(f"step{step}: [auto {routine_id}] 程序接管开始")
        started = time.time()
        result = run_routine(
            routine_id, self.device, action.get("params") or {}, log=self.log,
            stop_event=self.stop_event, frame_cb=self.frame_cb,
        )
        if result.status is ExecutionStatus.DONE and str(action.get("save_as") or ""):
            try:
                saved = save_and_register(str(action["save_as"]), action.get("params") or {})
                self.log(f"[auto] 编排已存盘注册: {saved}")
                self.history.append(
                    f"step{step}: [auto] 编排已存盘为 routine 「{action['save_as']}」，下次可直接按名调用"
                )
            except Exception as exc:
                self.log(f"[auto] 编排存盘失败: {exc}")
        hints = {
            ExecutionStatus.DONE: "程序已清剿完毕，请继续任务剩余步骤",
            ExecutionStatus.WRONG_SCENE: "还不在入口页——请先按任务路径导航到入口页后再调 auto",
            ExecutionStatus.PARTIAL: "程序中途交还控制权，按 detail 判断：接手处理或换目标",
            ExecutionStatus.BLOCKED: "转场已熔断，禁止继续点击",
        }
        hint = hints.get(result.status, "按 detail 处理")
        self.history.append(
            f"step{step}: [auto {routine_id}] status={result.status.value} "
            f"cleared={result.cleared}｜{result.detail}｜{hint}"
        )
        self.log(
            f"step{step}: [auto {routine_id}] {result.status.value} "
            f"cleared={result.cleared} {result.detail}"
        )
        credit = time.time() - started
        if result.status is ExecutionStatus.BLOCKED:
            result.steps = step
            return ActionOutcome(terminal=result, budget_credit=credit)
        frame2 = self.device.screenshot()
        self.save_frame(frame2, self.run_dir, f"step{step:02d}_after_auto.png")
        safe_callback(self.frame_cb, frame2, log=self.log, label="frame")
        return ActionOutcome(budget_credit=credit)

    def _report(self, action: dict, frame: np.ndarray, step: int, thought: str) -> ActionOutcome:
        status = ExecutionStatus.parse(action.get("status", "failed"))
        if status is ExecutionStatus.DONE:
            frame2 = self.device.screenshot()
            self.save_frame(frame2, self.run_dir, "verify.png")
            evidence = str(action.get("evidence", "")).strip()
            validation = str(self.task.get("validation", "inline")).strip().lower()
            if validation != "strict" and evidence:
                ok, reason = True, f"本轮画面证据：{evidence}"
                self.log(f"step{step}: [verify:inline] {evidence}")
            else:
                self.emit_thinking(self.event_cb, "start")
                try:
                    ok, reason = self.brain.verify(
                        self.task["prompt"], self.task.get("exit_condition", ""), frame2
                    )
                finally:
                    self.emit_thinking(self.event_cb, "done", self.brain)
            if not ok:
                self.history.append(f"step{step}: 自报 done 但验证未通过：{reason}")
                return ActionOutcome()
            if self.update_knowledge:
                try:
                    new_knowledge = self.brain.summarize_knowledge(
                        self.task.get("name", self.task["id"]), self.knowledge, self.history
                    )
                    if new_knowledge:
                        self.save_knowledge(self.task["id"], new_knowledge)
                except Exception as exc:
                    self.log(f"[warn] 知识卡更新失败: {exc}")
            return ActionOutcome(terminal=ExecutionResult(
                ExecutionStatus.DONE, detail="验证通过", steps=step
            ))
        if status is ExecutionStatus.BLOCKED:
            return ActionOutcome(terminal=ExecutionResult(
                ExecutionStatus.BLOCKED, detail=str(action.get("detail", "")), steps=step
            ))
        return ActionOutcome(terminal=ExecutionResult(
            ExecutionStatus.FAILED, detail=str(action.get("detail", "")), steps=step
        ))
