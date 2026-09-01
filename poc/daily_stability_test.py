"""三项日常修复的无游戏回归：fast routine 路由、锚点裁剪、Transition barrier。"""
import sys
import tempfile
import time
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotabyss_agent import agent, flowgen, runner
from dotabyss_agent.daily_routines import claim_idle_reward
from dotabyss_agent.macros import TransitionTimeout, click_path, wait_transition_done


class TransitionDevice:
    def __init__(self, states):
        self.states = list(states)
        self.polls = 0
        self.clicks = []

    def ui_tree(self, canvas=None, max_nodes=300):
        busy = self.states[min(self.polls, len(self.states) - 1)]
        self.polls += 1
        children = ([{"name": f"n{i}", "children": []} for i in range(6)]
                    if busy else [])
        return {"scene": "Home", "canvases": [{"name": "Transition", "children": children}]}

    def click_by_path(self, path):
        self.clicks.append(path)
        self.polls = 0
        return path


class FastDevice:
    def bring_to_front(self):
        pass


class StaticHomeDevice:
    def __init__(self):
        anchor = cv2.imread("tasks/flows/anchors/common/home_btn.png")
        self.frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        h, w = anchor.shape[:2]
        self.frame[80:80 + h, 80:80 + w] = anchor
        self.taps = 0

    def screenshot(self):
        return self.frame.copy()

    def tap(self, *_args):
        self.taps += 1
        return True


class BombBrain:
    def __getattr__(self, name):
        raise AssertionError(f"fast routine 不应调用 Brain.{name}")


class AutoBrain:
    def decide(self, *_args, **_kwargs):
        return {"thought": "交给程序", "action": "auto", "routine": "test_blocked"}


class IdleDevice:
    def __init__(self, equipment_full=False):
        anchor = cv2.imread("tasks/flows/anchors/common/home_btn.png")
        self.home = np.zeros((720, 1280, 3), dtype=np.uint8)
        h, w = anchor.shape[:2]
        self.home[80:80 + h, 80:80 + w] = anchor
        self.reward = np.full_like(self.home, 80)
        self.state = "home"
        self.equipment_full = equipment_full
        self.clicked = []

    def screenshot(self):
        return (self.home if self.state == "home" else self.reward).copy()

    def ui_tree(self, canvas=None, max_nodes=4000):
        if canvas == "Transition":
            return {"scene": "Home", "canvases": [{"name": "Transition", "children": []}]}
        kids = []
        if self.state == "disassemble":
            kids.append({"name": "Button_Confirm", "text": "分解する", "children": [],
                         "button": {"path": "Front/AutoDisassembly/Button_Confirm",
                                    "interactable": True}})
        elif self.state == "close":
            kids.append({"name": "Popup_Close", "children": [],
                         "button": {"path": "Front/DisassemblyReward/Popup_Close",
                                    "interactable": True}})
        return {"scene": "Home", "canvases": [{"name": "Front", "children": kids}]}

    def tap(self, x, y):
        self.clicked.append((x, y))
        if self.state != "home":
            return False
        self.state = "result"
        return True

    def skip_page(self):
        self.clicked.append("skip")
        self.state = "disassemble" if self.equipment_full else "home"
        return "FullScreen"

    def click_by_path(self, path):
        self.clicked.append(path)
        if self.state == "disassemble":
            self.state = "close"
        elif self.state == "close":
            self.state = "home"
        return path

    def wait_settled(self, _ref, **_kwargs):
        return self.screenshot()

    @staticmethod
    def diff_ratio(a, b):
        d = np.abs(a.astype(np.int16) - b.astype(np.int16)).max(axis=2)
        return float((d > 12).mean())


def check(cond, msg):
    print(("OK  " if cond else "FAIL") + " | " + msg)
    assert cond, msg


def main():
    original_sleep = time.sleep
    time.sleep = lambda *_: None
    try:
        late = TransitionDevice([False, False, False, True, True, False, False])
        check(wait_transition_done(late, timeout=3, poll=0.25, initial=1.5, quiet=0.5),
              "晚启动 Transition 被观察到并等到连续空闲")
        check(late.polls >= 7, f"没有在首次 idle 时提前放行（polls={late.polls}）")

        stuck = TransitionDevice([True])
        check(not wait_transition_done(stuck, timeout=1, poll=0.25, initial=0, quiet=0.5),
              "永久 busy 有界超时")

        guarded = TransitionDevice([False, False, True, True, False, False, False])
        check(click_path(guarded, "Front/Button") and guarded.polls >= 6,
              "click_path 点击后统一等待延迟启动的 Transition")
        blocked = TransitionDevice([True])
        try:
            click_path(blocked, "Front/Button")
            check(False, "点击前永久 busy 应熔断")
        except TransitionTimeout:
            check(not blocked.clicks, "点击前永久 busy 熔断且未发出输入")

        # 录制帧和回放帧相同会令所有候选近乎满分；应选最小稳定模板而非首个 150x75。
        rng = np.random.default_rng(7)
        arr = rng.integers(0, 256, (160, 240, 3), dtype=np.uint8)
        best = flowgen._best_candidate(Image.fromarray(arr[:, :, ::-1]), 120, 80, arr)
        check(best[2:4] == (64, 32), f"同分时选择最小候选，实际 {best[2:4]}")

        normal = claim_idle_reward(IdleDevice(), log=lambda *_: None)
        check(normal["status"] == "done" and normal["actions"] == 2,
              f"挂机常规路径固定两动作完成: {normal}")
        full = claim_idle_reward(IdleDevice(equipment_full=True), log=lambda *_: None)
        check(full["status"] == "done" and full["actions"] == 4,
              f"装备满路径只多处理分解和关闭: {full}")

        no_gift = flowgen.run_flow(StaticHomeDevice(), "claim_gifts_new", log=lambda *_: None)
        check(no_gift["status"] == "done" and no_gift.get("detail") == "幂等跳过",
              "主页无礼物角标时 flow 直接幂等完成，不回退 LLM")

        old_load_tasks = runner.load_tasks
        old_precondition = runner.check_precondition
        old_routine = runner.ROUTINES.get("test_fast")
        runner.load_tasks = lambda: [{"id": "fast", "name": "fast",
                                      "fast_routine": "test_fast", "prompt": "", "exit_condition": ""}]
        runner.check_precondition = lambda *_: (False, "")
        runner.ROUTINES["test_fast"] = lambda *_a, **_k: {
            "status": "done", "actions": 2, "detail": "deterministic"
        }
        result = runner.run_selected(["fast"], _device=FastDevice(), _brain=BombBrain(),
                                     log=lambda *_: None)
        check(result == [{"task": "fast", "status": "done", "steps": 2,
                          "detail": "程序快跑：deterministic"}],
              "任务级 fast_routine 完成后不进入 LLM")
        runner.load_tasks = old_load_tasks
        runner.check_precondition = old_precondition
        if old_routine is None:
            runner.ROUTINES.pop("test_fast", None)
        else:
            runner.ROUTINES["test_fast"] = old_routine

        old_agent_run_dir = agent.RUNS_DIR
        old_blocked_routine = agent.ROUTINES.get("test_blocked")
        agent.RUNS_DIR = Path(tempfile.mkdtemp(prefix="daily_stability_"))

        def raise_transition(*_args, **_kwargs):
            raise TransitionTimeout("post-click transition stuck")

        agent.ROUTINES["test_blocked"] = raise_transition
        blocked_result = agent.run_task(
            {"id": "blocked", "name": "blocked", "prompt": "", "exit_condition": ""},
            IdleDevice(), AutoBrain(), update_knowledge=False, log=lambda *_: None,
        )
        check(blocked_result["status"] == "blocked" and blocked_result["steps"] == 1,
              "auto 的 TransitionTimeout 直接 task blocked，不再请求 LLM 接管")
        agent.RUNS_DIR = old_agent_run_dir
        if old_blocked_routine is None:
            agent.ROUTINES.pop("test_blocked", None)
        else:
            agent.ROUTINES["test_blocked"] = old_blocked_routine
    finally:
        time.sleep = original_sleep

    # 当前礼物素材必须能跨数量匹配：历史 19 → 最新截图角标 8。
    anchor = cv2.imread("tasks/flows/anchors/claim_gifts_new/s1.png")
    frame = cv2.imread(".local/runs/20260901_203220/claim_idle_reward/step01.png")
    if anchor is not None and frame is not None:
        score = cv2.minMaxLoc(cv2.matchTemplate(frame, anchor, cv2.TM_CCOEFF_NORMED))[1]
        check(score >= 0.94, f"礼物首锚点跨数量仍命中（score={score:.3f}）")
    claimed = cv2.imread(".local/runs/20260901_203347/claim_gifts_new/verify.png")
    if anchor is not None and claimed is not None:
        score = cv2.minMaxLoc(cv2.matchTemplate(claimed, anchor, cv2.TM_CCOEFF_NORMED))[1]
        check(score < 0.94, f"礼物领完角标消失后不再命中（score={score:.3f}）")

    print("\nALL PASS")


if __name__ == "__main__":
    main()
