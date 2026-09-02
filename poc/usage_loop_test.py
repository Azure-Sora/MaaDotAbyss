"""用量统计回环测试（无 Qt/无游戏/无网络）：usage 落盘聚合、Brain._chat 埋点、控制面命令。

覆盖：record→aggregate 全维度（总览/场景/模型/任务/按日/分时/明细）、
_chat 成功与异常埋点（含 decide 的 JSON 重试双记录）、ControlServer usage 命令。
跑法: PYTHONUTF8=1 python poc/usage_loop_test.py
"""
import json
import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotabyss_agent import usage                      # noqa: E402
from dotabyss_agent.brain import Brain, BrainError    # noqa: E402
from dotabyss_agent.control import ControlServer, ctl_request  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="usage_test_"))
usage.USAGE_DIR = TMP          # 隔离：不碰真实 .local/usage

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"[{'ok' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail and not ok else ""))


def last_entry() -> dict:
    files = sorted(TMP.glob("*.jsonl"))
    lines = files[-1].read_text(encoding="utf-8").splitlines()
    return json.loads(lines[-1])


# ---- 1) record + aggregate ------------------------------------------------

usage.record(scene="daily_decide", provider="glm", model="glm-5.3-flash", task="daily_pack",
             ok=True, prompt_tokens=100, completion_tokens=50, cached_tokens=30,
             reasoning_tokens=20, total_tokens=150, latency_s=1.234)
usage.record(scene="abyss_code", provider="glm", model="glm-5.3-flash", task="abyss",
             ok=True, prompt_tokens=200, completion_tokens=10, cached_tokens=0,
             reasoning_tokens=0, total_tokens=210, latency_s=0.5)
usage.record(scene="abyss_rescue", provider="mimo", model="mimo-v2.5", task="abyss",
             ok=False, latency_s=2.0, err="TimeoutError: boom")

d = usage.aggregate(None)
t = d["total"]
check("总请求数与失败数", t["requests"] == 3 and t["fail"] == 1, json.dumps(t, ensure_ascii=False))
check("token 汇总", t["prompt_tokens"] == 300 and t["completion_tokens"] == 60
      and t["cached_tokens"] == 30 and t["total_tokens"] == 360)
check("延迟均值/峰值", t["latency_avg"] == round((1.234 + 0.5 + 2.0) / 3, 3) and t["latency_max"] == 2.0)
check("场景数与排序(按tokens降序)", [b["scene"] for b in d["by_scene"]]
      == ["abyss_code", "daily_decide", "abyss_rescue"],
      json.dumps(d["by_scene"], ensure_ascii=False))
check("场景中文名", d["by_scene"][0]["scene_zh"] == "深渊代码定色")
check("模型维度", len(d["by_model"]) == 2 and d["by_model"][0]["provider"] == "glm")
check("任务维度", any(b["task"] == "daily_pack" for b in d["by_task"]))
check("按日聚合", d["by_day"] and d["by_day"][-1]["requests"] == 3)
check("分时 24 桶", len(d["by_hour"]) == 24 and d["hour_date"] is not None)
check("明细倒序+中文名", d["recent"][0]["scene"] == "abyss_rescue"
      and d["recent"][0]["scene_zh"] == "深渊兜底自救" and d["recent"][0]["ok"] is False)
d7 = usage.aggregate(7)
check("范围过滤不误删今天", d7["total"]["requests"] == 3)

# ---- 2) Brain._chat 埋点 ---------------------------------------------------


class _Det:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Resp:
    def __init__(self, content):
        self.usage = _Det(prompt_tokens=11, completion_tokens=7, total_tokens=18,
                          prompt_tokens_details=_Det(cached_tokens=4),
                          completion_tokens_details=_Det(reasoning_tokens=3))
        self.choices = [type("C", (), {"message": type("M", (), {"content": content})()})()]


class _Seq:
    """按序吐预设回复；空队列时抛网络错误。"""
    outs: list = []

    @staticmethod
    def create(**kw):
        _Seq.last_kwargs = kw
        out = _Seq.outs.pop(0)
        if isinstance(out, Exception):
            raise out
        return _Resp(out)


class _Chat:
    completions = _Seq


class _Client:
    chat = _Chat


brain = Brain.__new__(Brain)   # 绕过 __init__：不依赖真实 provider 配置
brain.client = _Client()
brain.model = "fake-model"
brain.provider = "fake"
brain.task_ctx = "t1"
brain.last_completion_tokens = None

# 成功路径
_Seq.outs = ['{"ok": 1}']
out = brain._chat([{"type": "text", "text": "hi"}], scene="daily_decide")
e = last_entry()
check("_chat 返回与透传", out == '{"ok": 1}' and "scene" not in _Seq.last_kwargs
      and "scene" not in json.dumps(_Seq.last_kwargs["messages"][1]["content"]))
check("埋点全字段", e["scene"] == "daily_decide" and e["task"] == "t1"
      and e["prompt_tokens"] == 11 and e["completion_tokens"] == 7
      and e["cached_tokens"] == 4 and e["reasoning_tokens"] == 3
      and e["total_tokens"] == 18 and e["ok"] is True and e["latency_s"] is not None,
      json.dumps(e, ensure_ascii=False))
check("last_completion_tokens 兼容", brain.last_completion_tokens == 7)

# 异常路径：失败也落记录，且异常原样抛出
_Seq.outs = [RuntimeError("boom")]
try:
    brain._chat([{"type": "text", "text": "hi"}], scene="abyss_rescue")
    raised = False
except RuntimeError:
    raised = True
e = last_entry()
check("异常埋点", raised and e["ok"] is False and e["scene"] == "abyss_rescue"
      and "RuntimeError" in (e["err"] or "") and e["prompt_tokens"] is None,
      json.dumps(e, ensure_ascii=False))

# decide 整链：坏 JSON → 重试，两条场景记录（daily_decide + daily_decide_retry）
_Seq.outs = ["这不是 JSON", '{"thought": "t", "action": "wait", "seconds": 1}']
action = brain.decide("任务", "", [], np.zeros((2, 2, 3), dtype=np.uint8))
e2 = last_entry()
check("decide 成功解析", action.get("action") == "wait")
check("decide 双场景埋点", e2["scene"] == "daily_decide_retry" and e2["ok"] is True)

# read_json_from_image / verify 的细分场景参数
_Seq.outs = ['{"color": "safe"}']
brain.read_json_from_image(np.zeros((2, 2, 3), dtype=np.uint8), "只输出JSON", scene="abyss_code")
check("定色场景透传", last_entry()["scene"] == "abyss_code")

# ---- 3) 控制面 usage 命令 ---------------------------------------------------

srv = ControlServer({"usage": lambda p: usage.aggregate(
    (lambda v: int(v) if int(v) > 0 else None)(p.get("days", 30)))})
port = srv.start()
ok, data = ctl_request("usage", {"days": 7})
check("ctl usage", ok and "total" in data and "by_scene" in data and data.get("days") == 7)
ok, data = ctl_request("usage", {"days": 0})
check("ctl usage days=0 → 全部", ok and data.get("days") is None)
srv.shutdown()

print()
print(f"===== 用量统计回环测试: {sum(results)}/{len(results)} 通过 =====")
sys.exit(0 if all(results) else 1)
