"""日常任务的可审计自进化账本。

模型成功轨迹不会直接变成任意 Python。这里只负责：

1. 聚合重复成功的、可由受限 flow 表达的轨迹；
2. 到达证据门槛后请求上层编译一个 shadow flow；
3. 记录 shadow 连续成功并晋升 trusted；失败立即 degraded。

账本放在 ``.local/evolution.json``，不进入仓库；真正的候选程序仍是可读、可版本化的
``tasks/flows/<task>.yaml``。
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path

from .config import LOCAL_DIR

EVOLUTION_PATH = LOCAL_DIR / "evolution.json"
SCHEMA_VERSION = 1
DEFAULT_OBSERVATIONS = 2
DEFAULT_TRUSTED_SUCCESSES = 3

# observe/report 不改变游戏状态，可以从回放程序中省略。其它非 click 动作目前都可能
# 承担核心语义；在 DSL 正式支持前绝不能只抽取 click 后冒充完整任务。
SAFE_TRACE_ACTIONS = {"click", "observe", "report"}


class EvolutionError(RuntimeError):
    pass


def analyze_record(record: list[dict]) -> dict:
    """判断一次成功轨迹能否安全编译，并生成抗坐标微抖的粗粒度签名。"""
    actions = [str(row.get("action") or "") for row in record]
    unsupported = sorted({a for a in actions if a and a not in SAFE_TRACE_ACTIONS})
    clicks = [
        row for row in record
        if row.get("action") == "click"
        and (row.get("eff") is None or float(row.get("eff", 0.0)) >= 0.02)
    ]
    reason = ""
    if unsupported:
        reason = "包含尚不可编译的动作: " + ", ".join(unsupported)
    elif not clicks:
        reason = "没有产生有效画面变化的点击"

    # 以 64px 网格吸收模型每次落点的轻微偏差；动作数和顺序仍必须一致。
    signature_rows = [
        ["click", int(row.get("x", 0)) // 64, int(row.get("y", 0)) // 64]
        for row in clicks
    ]
    signature = hashlib.sha256(
        json.dumps(signature_rows, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    return {
        "eligible": not reason,
        "reason": reason,
        "signature": signature,
        "clicks": len(clicks),
        "unsupported": unsupported,
    }


class EvolutionLedger:
    def __init__(self, path: Path = EVOLUTION_PATH,
                 observations: int = DEFAULT_OBSERVATIONS,
                 trusted_successes: int = DEFAULT_TRUSTED_SUCCESSES):
        self.path = Path(path)
        self.observations = max(1, int(observations))
        self.trusted_successes = max(1, int(trusted_successes))
        self.data = self._load()

    @staticmethod
    def _now() -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")

    def _load(self) -> dict:
        if not self.path.exists():
            return {"version": SCHEMA_VERSION, "tasks": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise EvolutionError(f"演进账本无法读取: {exc}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("tasks", {}), dict):
            raise EvolutionError("演进账本格式无效")
        data.setdefault("version", SCHEMA_VERSION)
        data.setdefault("tasks", {})
        return data

    def _save(self) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise EvolutionError(f"演进账本无法写入: {exc}") from exc

    def _task(self, task_id: str) -> dict:
        return self.data["tasks"].setdefault(task_id, {
            "state": "observing",
            "slow_successes": 0,
            "signatures": {},
        })

    def flow_state(self, task_id: str, flow_id: str) -> str | None:
        flow = self._task(task_id).get("flow") or {}
        return str(flow.get("state")) if flow.get("id") == flow_id else None

    def observe_success(self, task_id: str, record: list[dict], run_dir: str) -> dict:
        analysis = analyze_record(record)
        task = self._task(task_id)
        task["slow_successes"] = int(task.get("slow_successes", 0)) + 1
        task["last_slow_success"] = self._now()
        task["last_run_dir"] = str(run_dir)
        task["last_analysis"] = analysis

        count = 0
        if analysis["eligible"]:
            sigs = task.setdefault("signatures", {})
            sig = sigs.setdefault(analysis["signature"], {"count": 0})
            sig["count"] = int(sig.get("count", 0)) + 1
            sig["last_run_dir"] = str(run_dir)
            sig["last_seen"] = self._now()
            count = sig["count"]
        flow = task.get("flow") or {}
        repair = flow.get("state") == "degraded"
        should_compile = bool(analysis["eligible"] and (repair or count >= self.observations))
        self._save()
        return {**analysis, "observations": count, "should_compile": should_compile,
                "repair": repair}

    def mark_compiled(self, task_id: str, flow_id: str, run_dir: str,
                      steps: int) -> dict:
        task = self._task(task_id)
        task["state"] = "shadow"
        task["flow"] = {
            "id": flow_id,
            "state": "shadow",
            "consecutive_successes": 0,
            "successes": 0,
            "failures": 0,
            "compiled_from": str(run_dir),
            "compiled_at": self._now(),
            "steps": int(steps),
        }
        self._save()
        return task["flow"].copy()

    def record_flow_result(self, task_id: str, flow_id: str, status: str,
                           detail: str = "") -> dict:
        task = self._task(task_id)
        flow = task.get("flow")
        if not isinstance(flow, dict) or flow.get("id") != flow_id:
            flow = {
                "id": flow_id, "state": "shadow", "consecutive_successes": 0,
                "successes": 0, "failures": 0, "adopted_at": self._now(),
            }
            task["flow"] = flow
        flow["last_status"] = str(status)
        flow["last_detail"] = str(detail)
        flow["last_run"] = self._now()
        if status == "done":
            flow["successes"] = int(flow.get("successes", 0)) + 1
            flow["consecutive_successes"] = int(flow.get("consecutive_successes", 0)) + 1
            if flow["consecutive_successes"] >= self.trusted_successes:
                flow["state"] = "trusted"
                task["state"] = "trusted"
            else:
                flow["state"] = "shadow"
                task["state"] = "shadow"
        else:
            flow["failures"] = int(flow.get("failures", 0)) + 1
            flow["consecutive_successes"] = 0
            flow["state"] = "degraded"
            task["state"] = "degraded"
        self._save()
        return flow.copy()

    def record_compile_failure(self, task_id: str, detail: str) -> None:
        task = self._task(task_id)
        task["last_compile_failure"] = {"at": self._now(), "detail": str(detail)}
        self._save()
