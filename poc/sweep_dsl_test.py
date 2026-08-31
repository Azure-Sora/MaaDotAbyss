"""generic_sweep / observe_buttons 假设备端到端测试（无需游戏）。

按 2026-08-31 真机采样建模迎击战页：详情弹窗 ButtonSet3（キャンセル/スキップ/出撃）、
打完 1 个后スキップ可跳过剩余委托（Popup_Confirm_SkipSimple）、出击链中会插入
自动分解确认与通行证里程结算弹窗。运行：python poc/sweep_dsl_test.py
"""
import json
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

time.sleep = lambda *_: None   # 测试全程禁真睡

from dotabyss_agent import sweep_dsl
from dotabyss_agent.macros import observe_buttons
from dotabyss_agent.sweep_dsl import (BadProgram, generic_sweep, load_saved_routines,
                                      save_program, validate_params)

DETAIL_CONFIRM = "Popup_QuestDetail_Disaster(Clone)/Box/Contents/Popup_ButtonSet3/Button_Confirm"
SKIP_BTN = "Popup_QuestDetail_Disaster(Clone)/Box/Contents/Popup_ButtonSet3/Button_Skip"
SKIP_CONFIRM = "Popup_Confirm_SkipSimple(Clone)/Box/Contents/Popup_ButtonSet2/Button_Confirm"
NOTE_CONFIRM = "Popup_Confirm_NoteButton2(Clone)/Box/Contents/Popup_ButtonSet2/Button_Confirm"
DISASSEMBLY = "Popup_Comfirm_AutoDisassembly(Clone)/Box/Contents/Popup_ButtonSet2/Button_Confirm"
NEXT = "ButtonSet/Layout/Button_Next"

# 打 1 跳 2：一场战斗 + 对剩余委托スキップ领奖（真机采样的完整链）
PARAMS = {
    "home_scene": "DisasterTop",
    "targets": {"canvas": "UICanvas", "btn_suffix": "Disaster/RootUI",
                "exclude_path": ["/Sp"], "max_targets": 1},
    "click_chain": ["{target.path}", f"!{DETAIL_CONFIRM}", NOTE_CONFIRM],
    "after_each": "battle",
    "finish_chain": ["{pending.path}", f"!{SKIP_BTN}", f"!{SKIP_CONFIRM}"],
}


class FakeDevice:
    """DisasterTop 状态机：boss 列表 + 详情/スキップ弹窗链 + 战斗场景机 + 打断弹窗。"""

    def __init__(self, n_bosses: int = 3, with_sp: bool = True):
        self.bosses = [{"label": f"ボス{i + 1}", "cleared": False} for i in range(n_bosses)]
        self.with_sp = with_sp
        self.popup = None            # None|detail|note|skip_confirm|skip_result
        self.disassembly = False     # 自动分解确认插队弹窗
        self.in_battle = False
        self.polls = 0
        self.fighting = None
        self.clicks: list[str] = []
        self.img = np.zeros((720, 1280, 3), np.uint8)

    # ---- 状态派生 ----
    def _scene(self) -> str:
        if self.in_battle:
            return "DisasterBattle" if self.polls <= 1 else "DisasterResult"
        return "DisasterTop"

    @staticmethod
    def _btn(path: str, text: str = "", interactable: bool = True) -> dict:
        return {"name": path.split("/")[-1],
                "button": {"path": path, "interactable": interactable},
                "text": text if text else None, "children": []}

    def _front(self) -> dict:
        kids = []
        if self.disassembly:
            kids.append(self._btn(f"Front/{DISASSEMBLY}", "分解する"))
        if self.popup == "detail":
            kids.append(self._btn(f"Front/{DETAIL_CONFIRM}", "出撃"))
            if any(b["cleared"] for b in self.bosses):   # 打完 1 个后スキップ才出现（实测）
                kids.append(self._btn(f"Front/{SKIP_BTN}", "スキップ"))
        elif self.popup == "note":
            kids.append(self._btn(f"Front/{NOTE_CONFIRM}", "決定"))
        elif self.popup == "skip_confirm":
            kids.append(self._btn(f"Front/{SKIP_CONFIRM}", "スキップ"))
        elif self.popup == "skip_result":
            kids.append(self._btn("Front/Popup_SkipResult(Clone)/Box/Popup_Close"))
        return {"name": "Front", "children": kids}

    def _ui(self) -> dict:
        kids = []
        if self.in_battle and self.polls >= 2:
            kids.append(self._btn(f"UICanvas/DisasterResult/{NEXT}"))
        else:
            for i, b in enumerate(self.bosses):
                node = {"name": "Disaster", "children": []}
                if not b["cleared"]:
                    node["children"].append({"name": "Label", "text": b["label"], "children": []})
                    node["children"].append(
                        self._btn(f"UICanvas/DisasterTop/Area{i + 1}/Disaster/RootUI"))
                kids.append(node)
            if self.with_sp:
                kids.append({"name": "Disaster", "children": [
                    {"name": "Label", "text": "スペシャル", "children": []},
                    self._btn("UICanvas/DisasterTop/Sp/Disaster/RootUI")]})
        return {"name": "UICanvas", "children": kids}

    # ---- 设备接口 ----
    def ui_tree(self, canvas=None, max_nodes=4000):
        if canvas == "Transition":
            return {"scene": self._scene(), "canvases": []}
        if canvas == "Front":
            return {"scene": self._scene(), "canvases": [self._front()]}
        if self.in_battle:
            self.polls += 1
        return {"scene": self._scene(), "canvases": [self._ui(), self._front()]}

    def click_by_path(self, path: str):
        self.clicks.append(path)
        if path.endswith("RootUI"):
            self.fighting = next(i for i, b in enumerate(self.bosses) if not b["cleared"])
            self.popup = "detail"
        elif path.endswith(DETAIL_CONFIRM):
            self.popup = "note"
        elif path.endswith(NOTE_CONFIRM):
            self.popup, self.in_battle, self.polls = None, True, 0
        elif path.endswith(NEXT):
            self.in_battle = False
            self.bosses[self.fighting]["cleared"] = True
            self.fighting = None
        elif path.endswith(DISASSEMBLY):
            self.disassembly = False
        elif path.endswith(SKIP_BTN):
            self.popup = "skip_confirm"
        elif path.endswith(SKIP_CONFIRM):
            self.popup = "skip_result"
            for b in self.bosses:
                b["cleared"] = True
        elif path.endswith("Popup_Close"):
            self.popup = None

    def screenshot(self):
        return self.img

    def wait_settled(self, ref, **_):
        return ref

    def wait_until_stable(self, **_):
        return True

    def diff_ratio(self, *_):
        return 0.0


def check(cond, msg):
    print(("OK  " if cond else "FAIL") + " | " + msg)
    assert cond, msg


def main():
    tmp = Path(tempfile.mkdtemp())
    sweep_dsl.ROUTINES_DIR = tmp          # 存盘测试隔离

    # 1) 参数校验
    for bad, frag in (({}, "home_scene"),
                      ({**PARAMS, "click_chain": ["A/{target.path}"]}, "首项"),
                      ({**PARAMS, "after_each": "fly"}, "after_each")):
        try:
            validate_params(bad)
            check(False, f"应拒绝: {bad}")
        except BadProgram as e:
            check(frag in str(e), f"校验拒绝并说明: {e}")

    # 2) 端到端打 1 跳 2（含打断弹窗：自动分解确认）
    dev = FakeDevice(3)
    dev.disassembly = True
    res = generic_sweep(dev, PARAMS, log=lambda s: print("   ", s))
    check(res["status"] == "done" and res["cleared"] == 1, f"打1跳2 done: {res}")
    check(all(b["cleared"] for b in dev.bosses), "战斗 1 场 + スキップ清剩余")
    tail = [c.split("/")[-1] for c in dev.clicks]
    check("Button_Confirm" in tail and dev.clicks[0].endswith("RootUI")
          and DISASSEMBLY in " ".join(dev.clicks),
          f"打断弹窗（分解する）被处理: {tail}")
    check(dev.clicks[-1].endswith("Popup_Close"), "收尾排空结果弹窗")
    check(not any("/Sp/" in c for c in dev.clicks), "特殊 boss 未被触碰")

    # 3) 纯战斗扫荡（无收尾链）：打满 3 场
    plain = {k: v for k, v in PARAMS.items() if k != "finish_chain"}
    plain = {**plain, "targets": {**plain["targets"], "max_targets": 3}}
    dev2 = FakeDevice(3)
    res2 = generic_sweep(dev2, plain, log=lambda s: None)
    check(res2["status"] == "done" and res2["cleared"] == 3, f"打满 3 场: {res2}")

    # 4) 无可清目标：done=0 且不点任何按钮
    dev3 = FakeDevice(3)
    for b in dev3.bosses:
        b["cleared"] = True
    res3 = generic_sweep(dev3, PARAMS, log=lambda s: None)
    check(res3["cleared"] == 0 and not dev3.clicks, "无可清目标 done=0 且不点收尾链")

    # 5) 存盘 → 按名调用（深合并覆盖）
    f = save_program("disaster_skip_sweep", PARAMS)
    check(f.exists() and json.loads(f.read_text(encoding="utf-8"))["name"] == "disaster_skip_sweep",
          f"存盘: {f.name}")
    saved = load_saved_routines()
    check("disaster_skip_sweep" in saved, "加载注册")
    dev4 = FakeDevice(3)
    res4 = saved["disaster_skip_sweep"](dev4, None, log=lambda s: None)
    check(res4["status"] == "done" and res4["cleared"] == 1, f"按名复跑: {res4}")
    dev5 = FakeDevice(3)
    res5 = saved["disaster_skip_sweep"](dev5, {"targets": {"max_targets": 3}},
                                        log=lambda s: None)
    check(res5["cleared"] == 3, "调用时深合并覆盖 targets.max_targets")
    try:
        save_program("Bad-Name", PARAMS)
        check(False, "坏名称应拒绝")
    except ValueError:
        check(True, "坏名称拒绝")

    # 6) observe_buttons
    dev6 = FakeDevice(3)
    scene_name, rows, total = observe_buttons(dev6)
    check(scene_name == "DisasterTop" and any("RootUI" in r and r.startswith("✓") for r in rows),
          f"observe 场景+可点标记: {scene_name}, {len(rows)} 行")
    _, rows2, total2 = observe_buttons(dev6, suffix="Button_Skip")
    check(len(rows2) == 0, "未清任何 boss 时主页无跳过按钮")
    dev6.bosses[0]["cleared"] = True
    dev6.popup = "detail"
    _, rows3, _ = observe_buttons(dev6, contains="Button_Skip")
    check(len(rows3) == 1 and "Button_Skip" in rows3[0], "清 1 后详情弹窗出现スキップ")

    print("\nALL PASS")


if __name__ == "__main__":
    main()
