"""教学模式状态机端到端自检（假设备 + 假大脑，不碰游戏/真模型）。

场景：
  模型脚本 = 点两下 → ask_user（用户答"点右边的门"）→ 再点一下 → report done
  （用户不点完成，直接输入指示继续）→ 模型再点两下 → 用户 /finish → 蒸馏入库
验证：
  状态流转 auto→awaiting→auto→…→distilling→done；录制/会话存档齐全；
  蒸馏产出 task_card + flow yaml + daily.yaml 追加（全部写到临时目录）。
"""
import json
import queue
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TMP = Path(tempfile.mkdtemp(prefix="teach_test_"))

# ---- 假设备：点击会让画面变（eff 判定可用），常驻动画即轻微噪声 ------------


class FakeDevice:
    def __init__(self):
        self.frame = np.full((720, 1280, 3), 40, dtype=np.uint8)
        self.clicks = []

    def screenshot(self):
        return self.frame.copy()

    def click(self, x, y):
        self.clicks.append((x, y))
        self.frame[:, :, 0] = (self.frame[:, :, 0] + 30) % 255  # 画面变化 → eff>0.02

    def swipe(self, *a, **k):
        pass

    def wait_settled(self, ref, *a, **k):
        return self.screenshot()

    def wait_until_stable(self, timeout=60):
        return True

    def diff_ratio(self, a, b):
        return float(np.mean(np.abs(a.astype(int) - b.astype(int)) > 25))

    def bring_to_front(self):
        pass

    def is_foreground(self):
        return True


# ---- 假大脑：脚本化动作序列 ------------------------------------------------


class FakeBrain:
    def __init__(self):
        self.script = [
            {"thought": "点A", "action": "click", "x": 100, "y": 100},
            {"thought": "点B", "action": "click", "x": 200, "y": 100},
            {"thought": "不认识了", "action": "ask_user",
             "question": "这里有两个门", "guess": "我猜点右边的门"},
            {"thought": "按指示点", "action": "click", "x": 300, "y": 100},
            {"thought": "好像完成了", "action": "report", "status": "done", "detail": "奖励已领"},
            {"thought": "继续收尾", "action": "click", "x": 400, "y": 100},
            {"thought": "再点一下", "action": "click", "x": 500, "y": 100},
        ]
        self.i = 0

    def decide_teach(self, goal, instructions, history, frame):
        act = self.script[self.i]
        self.i += 1
        return act

    def summarize_session(self, name, goal, dialogue, record_lines):
        return {
            "prompt": f"去做 {goal}；用户说过：{'；'.join(dialogue)}",
            "exit_condition": "结算画面消失，回到主页面",
            "notes": ["注意弹窗", "日文按钮按语义理解"],
        }

    def select_flow_steps(self, record_lines):
        return {"degenerate": False, "reason": "ok",
                "steps": [{"ref_step": 1, "name": "第一步"}, {"ref_step": 4, "name": "关键步"}]}


# ---- monkeypatch 蒸馏落盘目标到临时目录，然后跑 ------------------------------

import dotabyss_agent.teach as teach
from dotabyss_agent.agent import save_knowledge  # noqa: E402  (确认可导入)

teach.DAILY_YAML = TMP / "daily.yaml"
teach.DAILY_YAML.write_text("# 每日任务清单\ntasks:\n  - id: dummy\n    name: 占位\n", encoding="utf-8")
teach.RUNS_DIR = TMP / "runs"
import dotabyss_agent.flowgen as flowgen  # noqa: E402

flowgen.FLOWS_DIR = TMP / "flows"
(flowgen.FLOWS_DIR / "anchors").mkdir(parents=True, exist_ok=True)
teach.save_knowledge = lambda tid, text: (TMP / f"kb_{tid}.md").write_text(text, encoding="utf-8")

from dotabyss_agent.config import KNOWLEDGE_DIR  # noqa: E402  (仅确认模块依赖未破坏)

events: list[dict] = []
states = []


def event_cb(ev):
    events.append(ev)
    if ev.get("type") == "state":
        states.append(ev["state"])


replies = queue.Queue()
for msg in ({"kind": "msg", "text": "点右边的门"}, {"kind": "finish", "text": ""}):
    replies.put(msg)

r = teach.run_teach_session(
    "test_task", "测试任务", "把测试流程走完", FakeDevice(), FakeBrain(),
    event_cb=event_cb, reply_get=replies.get,
)

print("status:", r["status"], "| detail:", r["detail"], "| steps:", r["steps"])
print("states:", " → ".join(states))
run_dir = Path(r["run_dir"])
print("session.json:", (run_dir / "session.json").exists(),
      "| record.json:", (run_dir / "record.json").exists())
print("task_card:", json.dumps(r.get("task_card", {}), ensure_ascii=False)[:120], "…")
daily = teach.DAILY_YAML.read_text(encoding="utf-8")
print("daily.yaml 追加:", "test_task" in daily, "| flow 标记:", "flow: test_task" in daily)
flow_yaml = flowgen.FLOWS_DIR / "test_task.yaml"
print("flow yaml:", flow_yaml.exists())

assert r["status"] == "distilled", "应蒸馏成功"
assert states[0] == "auto" and states[-1] == "done"
assert states.count("awaiting") == 2, "ask_user 与 report-done 确认各挂起一次"
assert (run_dir / "session.json").exists() and (run_dir / "record.json").exists()
sess = json.loads((run_dir / "session.json").read_text(encoding="utf-8"))
user_msgs = [d for d in sess["dialogue"] if d["role"] == "user"]
assert any("点右边的门" in d["text"] for d in user_msgs), "用户消息应入档"
assert r["task_card"]["exit_condition"], "任务卡应有完成判据"
assert flow_yaml.exists(), "剧本应生成（依赖 flowgen 修复）"
assert "flow: test_task" in daily, "daily.yaml 应带 flow 标记"
print("\n✅ 教学状态机端到端自检通过")
