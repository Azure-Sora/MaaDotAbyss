"""执行层公共契约。

内部编排使用强类型状态与结果；CLI/GUI 边界仍输出普通 dict，保持兼容。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Mapping, Protocol


class ExecutionStatus(str, Enum):
    DONE = "done"
    FAILED = "failed"
    BLOCKED = "blocked"
    INCOMPLETE = "incomplete"
    PARTIAL = "partial"
    SKIPPED = "skipped"
    WRONG_SCENE = "wrong_scene"
    ERROR = "error"

    @classmethod
    def parse(cls, value: object) -> "ExecutionStatus":
        try:
            return cls(str(value))
        except ValueError:
            return cls.ERROR

    @property
    def stops_batch(self) -> bool:
        return self is ExecutionStatus.BLOCKED


@dataclass(slots=True)
class ExecutionResult:
    status: ExecutionStatus
    detail: str = ""
    steps: int = 0
    actions: int = 0
    cleared: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ExecutionResult":
        known = {"status", "detail", "steps", "step", "actions", "cleared"}
        return cls(
            status=ExecutionStatus.parse(value.get("status", "error")),
            detail=str(value.get("detail", "")),
            steps=int(value.get("steps", value.get("step", 0)) or 0),
            actions=int(value.get("actions", 0) or 0),
            cleared=int(value.get("cleared", 0) or 0),
            extra={k: v for k, v in value.items() if k not in known},
        )

    def to_dict(self) -> dict[str, Any]:
        data = {
            "status": self.status.value,
            "detail": self.detail,
            "steps": self.steps,
        }
        if self.actions:
            data["actions"] = self.actions
        if self.cleared:
            data["cleared"] = self.cleared
        data.update(self.extra)
        return data

    def to_task_dict(self, task_id: str, *, steps: int | None = None,
                     detail: str | None = None) -> dict[str, Any]:
        return {
            "task": task_id,
            "status": self.status.value,
            "steps": self.steps if steps is None else int(steps),
            "detail": self.detail if detail is None else str(detail),
        }


class Routine(Protocol):
    def __call__(self, device, params: dict | None = None, *, log=print,
                 stop_event=None, frame_cb=None, **kwargs) -> Mapping[str, Any]: ...


def safe_callback(callback: Callable[[Any], Any] | None, payload: Any, *,
                  log=print, label: str = "callback") -> bool:
    """调用非关键回调；失败可观察但不破坏执行主链。"""
    if callback is None:
        return True
    try:
        callback(payload)
        return True
    except Exception as exc:
        log(f"[warn] {label} 回调失败: {exc.__class__.__name__}: {exc}")
        return False
