"""执行层重构契约回归（无需真实游戏）。"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotabyss_agent import runner
from dotabyss_agent.evolution import EvolutionError
from dotabyss_agent.execution import ExecutionResult, ExecutionStatus
from dotabyss_agent.routines import ROUTINES, SweepSession, run_routine


def check(condition, label):
    if not condition:
        raise AssertionError(label)
    print(f"OK   | {label}")


class Device:
    def bring_to_front(self):
        pass

    def screenshot(self):
        return np.zeros((720, 1280, 3), dtype=np.uint8)


class VerifyBrain:
    def __init__(self):
        self.verify_calls = 0

    def verify(self, *_args, **_kwargs):    # **kwargs：runner 现带 scene 细分参数
        self.verify_calls += 1
        return True, "verified"


class StopFlag:
    def __init__(self):
        self.value = False

    def is_set(self):
        return self.value


def main():
    normalized = ExecutionResult.from_mapping({
        "status": "wrong_scene", "detail": "navigate", "cleared": 2
    })
    check(normalized.status is ExecutionStatus.WRONG_SCENE and normalized.cleared == 2,
          "routine/flow 裸字典统一归一化为 typed ExecutionResult")

    old_test_routine = ROUTINES.get("contract_test")
    ROUTINES["contract_test"] = lambda *_a, **_k: {
        "status": "done", "cleared": 3, "detail": "ok"
    }
    try:
        called = run_routine("contract_test", Device())
        missing = run_routine("missing_contract_test", Device())
        check(called.status is ExecutionStatus.DONE and called.cleared == 3,
              "唯一 registry 通过统一 run_routine 调用并保留指标")
        check(missing.status is ExecutionStatus.PARTIAL,
              "未知 routine 使用统一可恢复状态而非散落异常")
    finally:
        if old_test_routine is None:
            ROUTINES.pop("contract_test", None)
        else:
            ROUTINES["contract_test"] = old_test_routine

    check(SweepSession(0).checkpoint()["detail"] == "总超时",
          "三类 sweep 共用同一超时/停止生命周期")

    old_load = runner.load_tasks
    old_precondition = runner.check_precondition
    old_flow = runner.run_flow
    old_ledger = runner.EvolutionLedger
    old_slow = runner.run_task
    task = {
        "id": "flow_task", "name": "Flow", "flow": "flow_task",
        "prompt": "do it", "exit_condition": "done",
    }
    runner.load_tasks = lambda: [task]
    runner.check_precondition = lambda *_: (False, "")
    runner.run_flow = lambda *_a, **_k: {
        "status": "done", "step": 1, "seconds": 0.1, "detail": ""
    }
    runner.EvolutionLedger = lambda: (_ for _ in ()).throw(
        EvolutionError("ledger unreadable")
    )
    runner.run_task = lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("验收通过的 flow 不应进入 slow path")
    )
    brain = VerifyBrain()
    try:
        result = runner.run_selected(
            ["flow_task"], _device=Device(), _brain=brain, log=lambda *_: None
        )
        check(brain.verify_calls == 1 and result[0]["status"] == "done",
              "演进账本不可读时 shadow 验收 fail closed")
    finally:
        runner.load_tasks = old_load
        runner.check_precondition = old_precondition
        runner.run_flow = old_flow
        runner.EvolutionLedger = old_ledger
        runner.run_task = old_slow

    stop = StopFlag()
    events = []
    runner.load_tasks = lambda: [task]
    runner.check_precondition = lambda *_: (False, "")

    def interrupted_flow(*_args, **_kwargs):
        stop.value = True
        return {"status": "incomplete", "step": 1, "detail": "interrupted"}

    runner.run_flow = interrupted_flow
    try:
        result = runner.run_selected(
            ["flow_task"], update_knowledge=False, _device=Device(),
            _brain=VerifyBrain(), stop_event=stop, event_cb=events.append,
            log=lambda *_: None,
        )
        result_events = [event for event in events if event.get("type") == "result"]
        check(result[0]["status"] == "incomplete" and len(result_events) == 1,
              "flow 用户停止也经过唯一 ResultSink 并发送 GUI 结果事件")
    finally:
        runner.load_tasks = old_load
        runner.check_precondition = old_precondition
        runner.run_flow = old_flow

    print("ALL PASS")


if __name__ == "__main__":
    main()
