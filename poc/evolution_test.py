"""自进化账本、任务字段与 inline 完成校验的无游戏回归。"""
import json
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotabyss_agent import agent, runner
from dotabyss_agent.evolution import EvolutionLedger, analyze_record
from dotabyss_agent.taskfile import update_task


def check(cond, msg):
    print(("OK  " if cond else "FAIL") + " | " + msg)
    assert cond, msg


SAFE_RECORD = [
    {"step": 1, "action": "observe"},
    {"step": 2, "action": "click", "x": 100, "y": 200, "eff": 0.31},
    {"step": 3, "action": "click", "x": 500, "y": 300, "eff": 0.22},
    {"step": 4, "action": "report", "status": "done", "evidence": "已回主页"},
]


class ReportBrain:
    def __init__(self, evidence="已回主页"):
        self.evidence = evidence
        self.verify_calls = 0

    def decide(self, *_args, **_kwargs):
        return {"thought": "完成", "action": "report", "status": "done",
                "evidence": self.evidence}

    def verify(self, *_args, **_kwargs):
        self.verify_calls += 1
        return True, "strict ok"


class FrameDevice:
    def bring_to_front(self):
        pass

    def screenshot(self):
        return np.zeros((720, 1280, 3), dtype=np.uint8)


def main():
    tmp = Path(tempfile.mkdtemp(prefix="evolution_test_"))

    safe = analyze_record(SAFE_RECORD)
    check(safe["eligible"] and safe["clicks"] == 2, "纯点击成功轨迹可编译")
    unsafe = analyze_record(SAFE_RECORD[:-1] + [
        {"step": 4, "action": "auto", "routine": "forces_sweep"},
        {"step": 5, "action": "report", "status": "done"},
    ])
    check(not unsafe["eligible"] and unsafe["unsupported"] == ["auto"],
          "包含 auto 的混合轨迹不会被错误编译为短 flow")

    ledger = EvolutionLedger(tmp / "evolution.json", observations=2, trusted_successes=3)
    first = ledger.observe_success("daily", SAFE_RECORD, "run1")
    second = ledger.observe_success("daily", SAFE_RECORD, "run2")
    check(not first["should_compile"] and second["should_compile"],
          "相同成功轨迹累计两次才请求编译")
    ledger.mark_compiled("daily", "daily", "run2", 2)
    states = [ledger.record_flow_result("daily", "daily", "done")["state"]
              for _ in range(3)]
    check(states == ["shadow", "shadow", "trusted"], "三次快跑成功后转 trusted")
    degraded = ledger.record_flow_result("daily", "daily", "failed", "anchor missing")
    check(degraded["state"] == "degraded" and ledger.flow_state("daily", "daily") == "degraded",
          "任一 flow 失败立即 degraded")
    persisted = json.loads((tmp / "evolution.json").read_text(encoding="utf-8"))
    check(persisted["tasks"]["daily"]["flow"]["failures"] == 1, "账本原子持久化")

    old_generate = runner.generate_flow
    old_update = runner.update_task
    generated = []
    runner.generate_flow = lambda *_a, **_k: generated.append("called") or {"steps": 2}
    runner.update_task = lambda *_a, **_k: None
    try:
        recovery = {"status": "done", "record": SAFE_RECORD, "run_dir": str(tmp / "run3")}
        runner._evolve_success(
            {"id": "daily", "name": "Daily", "flow": "daily"}, recovery,
            object(), ledger, allow_compile=False, log=lambda *_: None,
        )
        check(not generated, "flow 失败后的半路恢复轨迹不会覆盖完整候选")
        runner._evolve_success(
            {"id": "daily", "name": "Daily", "flow": "daily"}, recovery,
            object(), ledger, allow_compile=True, log=lambda *_: None,
        )
        check(generated == ["called"] and ledger.flow_state("daily", "daily") == "shadow",
              "下一次从任务起点成功后可重编译 degraded 候选")
    finally:
        runner.generate_flow = old_generate
        runner.update_task = old_update

    daily = tmp / "daily.yaml"
    daily.write_text(
        "tasks:\n  - id: sample\n    name: Sample\n    prompt: do it\n"
        "    exit_condition: done\n",
        encoding="utf-8",
    )
    update_task("sample", flow="sample", path=daily)
    check("    flow: sample\n" in daily.read_text(encoding="utf-8"),
          "任务文件编辑器可增量添加 flow 字段")

    old_runs = agent.RUNS_DIR
    agent.RUNS_DIR = tmp / "runs"
    try:
        inline_brain = ReportBrain()
        inline = agent.run_task(
            {"id": "inline", "name": "inline", "prompt": "", "exit_condition": ""},
            FrameDevice(), inline_brain, max_steps=1, update_knowledge=False,
            log=lambda *_: None,
        )
        check(inline["status"] == "done" and inline_brain.verify_calls == 0,
              "report 携带画面证据时跳过重复模型复核")

        strict_brain = ReportBrain()
        strict = agent.run_task(
            {"id": "strict", "name": "strict", "prompt": "", "exit_condition": "",
             "validation": "strict"},
            FrameDevice(), strict_brain, max_steps=1, update_knowledge=False,
            log=lambda *_: None,
        )
        check(strict["status"] == "done" and strict_brain.verify_calls == 1,
              "strict 任务仍执行独立模型复核")
    finally:
        agent.RUNS_DIR = old_runs

    old_load = runner.load_tasks
    old_precondition = runner.check_precondition
    old_run_flow = runner.run_flow
    old_run_task = runner.run_task
    old_ledger_factory = runner.EvolutionLedger
    flow_brain = ReportBrain()
    flow_ledger_path = tmp / "flow_evolution.json"
    runner.load_tasks = lambda: [{
        "id": "flow_task", "name": "Flow", "flow": "flow_task",
        "prompt": "领取奖励", "exit_condition": "已回主页",
    }]
    runner.check_precondition = lambda *_: (False, "")
    runner.run_flow = lambda *_a, **_k: {
        "status": "done", "step": 2, "seconds": 0.2, "detail": ""
    }
    runner.run_task = lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("shadow flow 验收通过后不应进入 slow path")
    )
    runner.EvolutionLedger = lambda: EvolutionLedger(
        flow_ledger_path, observations=2, trusted_successes=3
    )
    try:
        states = []
        for _ in range(4):
            result = runner.run_selected(
                ["flow_task"], _device=FrameDevice(), _brain=flow_brain,
                log=lambda *_: None,
            )
            states.append(result[0]["detail"])
        check(flow_brain.verify_calls == 3, "shadow 三次独立验收，trusted 后取消模型复核")
        check("[trusted]" in states[2] and "[trusted]" in states[3],
              "第三次验收成功即晋升 trusted，后续保持快跑")
    finally:
        runner.load_tasks = old_load
        runner.check_precondition = old_precondition
        runner.run_flow = old_run_flow
        runner.run_task = old_run_task
        runner.EvolutionLedger = old_ledger_factory

    print("ALL PASS")


if __name__ == "__main__":
    main()
