"""识图执行器（双速架构 fast path）：按剧本做模板匹配定位 + 点击 + 判据验证。

剧本 yaml（tasks/flows/<id>.yaml）步骤字段：
  name: 步骤名
  find: {anchor: xx.png, threshold: 0.85}      # 定位（模板匹配）
  no_match: fail | continue | done_ok          # 找不到锚点时的处理（默认 fail）
                                               # done_ok = 不存在即任务完成（幂等任务）
  act: click                                   # click | wait_settled
  expect: {anchor: xx.png, threshold: 0.85}    # 动作后判据：某锚点出现
        | {change_above: 0.05}                 #           或画面相对动作前变化下限
  max_wait: 8                                  # expect 等待上限（秒）
  loop_click:                                  # 循环点击直到 until 锚点出现/目标消失
    target: {anchor: ..., threshold: 0.8}
    until: {anchor: ..., threshold: 0.85}
    max_times: 8
  on_fail: continue                            # 本步失败不中断（默认中断）

锚点图放 tasks/flows/anchors/<id>/。失败 → FlowError → 上层决定 LLM 接管。
"""
import time

import cv2
import numpy as np
import yaml

from .config import TASKS_DIR
from .execution import safe_callback

FLOWS_DIR = TASKS_DIR / "flows"


class FlowError(RuntimeError):
    pass


class DoneOk(Exception):
    """no_match=done_ok 信号：定位锚点不存在即任务已完成（幂等任务）。"""


def match_anchor(frame_bgr: np.ndarray, anchor_bgr: np.ndarray, threshold: float):
    """模板匹配，命中返回锚点中心坐标，否则 None。"""
    if anchor_bgr.shape[0] > frame_bgr.shape[0] or anchor_bgr.shape[1] > frame_bgr.shape[1]:
        return None
    res = cv2.matchTemplate(frame_bgr, anchor_bgr, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(res)
    if max_val >= threshold:
        h, w = anchor_bgr.shape[:2]
        return max_loc[0] + w // 2, max_loc[1] + h // 2
    return None


def anchor_visible(frame_bgr, anchor_bgr, threshold: float) -> bool:
    return match_anchor(frame_bgr, anchor_bgr, threshold) is not None


class FlowRunner:
    def __init__(self, device, flow_id: str):
        self.device = device
        self.flow_id = flow_id
        path = FLOWS_DIR / f"{flow_id}.yaml"
        self.flow = yaml.safe_load(path.read_text(encoding="utf-8"))
        self.anchor_dir = FLOWS_DIR / "anchors" / flow_id
        self._cache: dict[str, np.ndarray] = {}

    def _anchor(self, name: str) -> np.ndarray:
        if name not in self._cache:
            # common/ 前缀供 flow 复用主页/返回等稳定上下文锚点，避免每个教学任务
            # 各复制一份相同素材。
            path = FLOWS_DIR / "anchors" / name if name.startswith("common/") else self.anchor_dir / name
            img = cv2.imread(str(path), cv2.IMREAD_COLOR)
            if img is None:
                raise FlowError(f"锚点图不存在: {name}")
            self._cache[name] = img
        return self._cache[name]

    def run(self, log=print, frame_cb=None, stop_event=None) -> dict:
        t0 = time.time()
        steps = self.flow.get("steps", [])
        for i, step in enumerate(steps, 1):
            if stop_event is not None and stop_event.is_set():
                return {"status": "incomplete", "detail": "用户停止", "step": i}
            name = step.get("name", f"step{i}")
            try:
                self._run_step(step, log, frame_cb)
                log(f"[flow {self.flow_id}] ✓ {name}")
            except DoneOk:
                log(f"[flow {self.flow_id}] ✓ {name}（定位锚点不存在，幂等完成）")
                return {"status": "done", "detail": "幂等跳过", "step": i}
            except FlowError as e:
                log(f"[flow {self.flow_id}] ✗ {name}: {e}")
                if step.get("on_fail") == "continue":
                    continue
                return {"status": "failed", "detail": f"{name}: {e}", "step": i}
        return {"status": "done", "detail": "", "step": len(steps), "seconds": round(time.time() - t0, 1)}

    # ---- 单步 -----------------------------------------------------------

    def _emit(self, frame_cb, frame, log=print):
        safe_callback(frame_cb, frame, log=log, label=f"flow {self.flow_id} frame")

    def _run_step(self, step: dict, log, frame_cb):
        if "loop_click" in step:
            self._run_loop_click(step["loop_click"], log, frame_cb)
            return

        # context 锚点：本步骤的前提页面。缺失说明"不在预期页面"，
        # 此时 no_match=done_ok 不能想当然（防止在错误页面上误判幂等完成）
        if "context" in step:
            ctx = step["context"]
            frame = self.device.screenshot()
            self._emit(frame_cb, frame, log)
            if not anchor_visible(frame, self._anchor(ctx["anchor"]), float(ctx.get("threshold", 0.85))):
                raise FlowError(f"上下文锚点缺失: {ctx['anchor']}（不在预期页面上）")

        attempts = max(1, int(step.get("retry", 1)))
        for attempt in range(1, attempts + 1):
            pre_frame = None
            if "coord" in step:  # 固定坐标直点（用于无锚点的已知位置，如右上角 X）
                x, y = step["coord"]
                pre_frame = self.device.screenshot()
                if not self.device.tap(x, y):
                    # 真实点击语义：未命中=被遮挡/不可点，不穿透；算失败重试
                    log(f"  click({x},{y}) 未命中可点目标（被遮挡/不可点）"
                        + (f" 第{attempt}次" if attempt > 1 else ""))
                    if attempt >= attempts:
                        raise FlowError(f"点击未命中: ({x},{y})（被遮挡/不可点，已重试 {attempts} 次）")
                    time.sleep(1.0)
                    continue
                log(f"  click{tuple(step['coord'])} (coord)" + (f" 第{attempt}次" if attempt > 1 else ""))
            elif "find" in step:
                find = step["find"]
                frame = self.device.screenshot()
                self._emit(frame_cb, frame, log)
                pos = match_anchor(frame, self._anchor(find["anchor"]), float(find.get("threshold", 0.85)))
                if pos is None:
                    mode = step.get("no_match", "fail")
                    if mode == "done_ok":
                        raise DoneOk()
                    if mode == "continue":
                        return
                    raise FlowError(f"锚点未命中: {find['anchor']}")
                pre_frame = frame
                if not self.device.tap(*pos):
                    log(f"  click{pos} 未命中可点目标（被遮挡/不可点）"
                        + (f" 第{attempt}次" if attempt > 1 else ""))
                    if attempt >= attempts:
                        raise FlowError(f"点击未命中: {pos}（锚点在但不可点，已重试 {attempts} 次）")
                    time.sleep(1.0)
                    continue
                log(f"  click{pos}" + (f" 第{attempt}次" if attempt > 1 else ""))
            elif step.get("act") == "wait_settled":
                pre_frame = self.device.screenshot()

            if step.get("act") == "wait_settled":
                self.device.wait_settled(pre_frame if pre_frame is not None else self.device.screenshot())
                return

            time.sleep(0.4)
            frame = self.device.wait_settled(
                pre_frame if pre_frame is not None else self.device.screenshot(),
                max_wait=float(step.get("max_wait", 8)),
            )
            self._emit(frame_cb, frame, log)

            expect = step.get("expect")
            if not expect:
                return
            ok = self._expect_met(expect, frame, pre_frame)
            if ok:
                return
            if attempt < attempts:
                log(f"  判据未满足，重试点击（{attempt}/{attempts}）")
                time.sleep(1.0)
            else:
                raise FlowError(f"判据未满足: {expect}（已重试 {attempts} 次）")

    def _expect_met(self, expect: dict, frame: np.ndarray, pre_frame: np.ndarray | None) -> bool:
        threshold = float(expect.get("threshold", 0.85))
        if "anchor" in expect:
            return anchor_visible(frame, self._anchor(expect["anchor"]), threshold)
        if "change_above" in expect and pre_frame is not None:
            return self.device.diff_ratio(pre_frame, frame) > float(expect["change_above"])
        return True

    def _run_loop_click(self, cfg: dict, log, frame_cb):
        target, until = cfg["target"], cfg.get("until")
        max_times = int(cfg.get("max_times", 8))
        for i in range(1, max_times + 1):
            frame = self.device.screenshot()
            self._emit(frame_cb, frame, log)
            if until and anchor_visible(frame, self._anchor(until["anchor"]), float(until.get("threshold", 0.85))):
                log(f"  循环结束（退出锚点出现，共点击 {i - 1} 次）")
                return
            pos = match_anchor(frame, self._anchor(target["anchor"]), float(target.get("threshold", 0.8)))
            if pos is None:
                log(f"  循环结束（目标锚点消失，共点击 {i - 1} 次）")
                return
            pre = self.device.screenshot()
            if not self.device.tap(*pos):
                log(f"  loop click{pos} 未命中可点目标（被遮挡/不可点），跳过本次")
                continue
            self.device.wait_settled(pre)
            log(f"  loop click{pos} 第 {i} 次")
        raise FlowError(f"循环点击 {max_times} 次仍未达成退出条件")


def run_flow(device, flow_id: str, log=print, frame_cb=None, stop_event=None) -> dict:
    return FlowRunner(device, flow_id).run(log=log, frame_cb=frame_cb, stop_event=stop_event)
