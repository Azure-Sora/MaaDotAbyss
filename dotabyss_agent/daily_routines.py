"""领取类日常的无模型快路径。

固定且可验证的点击不进入 agent 循环；只有入口不对、桥不可用或出现未知页面时，
runner 才降级给 LLM。
"""
import re
import time

import cv2

from .config import TASKS_DIR
from .flow import anchor_visible
from .macros import (
    TransitionTimeout, click_path, collect_texts, popup_cancel_consume, scene,
    settle_step, transition_busy, wait_transition_done,
)

HOME_ANCHOR = TASKS_DIR / "flows" / "anchors" / "common" / "home_btn.png"
HOME_ANCHOR_IMAGE = cv2.imread(str(HOME_ANCHOR), cv2.IMREAD_COLOR)
IDLE_CHEST = (1140, 645)
NETWORK_ERROR_RE = re.compile(r"403|通信エラー|ネットワークエラー", re.I)


def _home_visible(frame) -> bool:
    return HOME_ANCHOR_IMAGE is not None and anchor_visible(frame, HOME_ANCHOR_IMAGE, 0.85)


def _emit(frame_cb, frame) -> None:
    if frame_cb is not None:
        try:
            frame_cb(frame)
        except Exception:
            pass


def claim_idle_reward(device, log=print, stop_event=None, frame_cb=None,
                      timeout: float = 30.0) -> dict:
    """主页点宝箱 → 跳探索报酬 → 可选自动分解两弹窗 → 回主页。"""
    t0 = time.time()
    actions = 0
    try:
        frame = device.screenshot()
        _emit(frame_cb, frame)
        current_scene = scene(device) if hasattr(device, "ui_tree") else "Home"
        if current_scene != "Home" or not _home_visible(frame):
            return {"status": "wrong_scene", "actions": actions,
                    "detail": f"不在 Home 主页面（scene={current_scene or '?'}），请先返回主页"}

        pre = frame
        if not device.tap(*IDLE_CHEST):
            return {"status": "partial", "actions": actions,
                    "detail": "挂机宝箱固定坐标未命中"}
        actions += 1
        if not wait_transition_done(device):
            return {"status": "blocked", "actions": actions,
                    "detail": "点击挂机宝箱后 Transition 未结束"}
        frame = device.wait_settled(pre, max_wait=5.0)
        _emit(frame_cb, frame)

        # 没有可领内容时点击可能不换页；固定动作已经完成，不再重复点宝箱。
        if _home_visible(frame) and device.diff_ratio(pre, frame) < 0.02:
            return {"status": "done", "actions": actions,
                    "detail": "宝箱无新结算画面（无需领取或刚领过）"}

        skips = 0
        while time.time() - t0 < timeout:
            if stop_event is not None and stop_event.is_set():
                return {"status": "partial", "actions": actions, "detail": "用户停止"}
            if transition_busy(device) and not wait_transition_done(device, initial=0.0):
                return {"status": "blocked", "actions": actions,
                        "detail": "领取过程中 Transition 持续未结束"}

            if hasattr(device, "ui_tree"):
                front = device.ui_tree(canvas="Front", max_nodes=2000)
                blob = "\n".join(text for _, text in collect_texts(front, canvas="Front"))
                if NETWORK_ERROR_RE.search(blob):
                    return {"status": "blocked", "actions": actions,
                            "detail": "领取过程中出现网络/403错误"}
                cancel = popup_cancel_consume(front)
                if cancel:
                    click_path(device, cancel)
                    actions += 1
                    return {"status": "partial", "actions": actions,
                            "detail": "出现消费确认，已拒绝并停止快跑"}
                act = settle_step(front)
                if act:  # 装备满：分解する；随后分解报酬 Popup_Close
                    click_path(device, act)
                    actions += 1
                    log(f"[idle-fast] 清理结算弹窗: {act.split('/')[-1]}")
                    continue

            frame = device.screenshot()
            _emit(frame_cb, frame)
            current_scene = scene(device) if hasattr(device, "ui_tree") else "Home"
            if current_scene == "Home" and _home_visible(frame):
                return {"status": "done", "actions": actions,
                        "detail": f"已领取并回到主页（跳页 {skips} 次）"}

            if skips >= 3:
                return {"status": "partial", "actions": actions,
                        "detail": "连续 3 次跳页仍未回主页"}
            pre = frame
            try:
                device.skip_page()
            except Exception as exc:
                return {"status": "partial", "actions": actions,
                        "detail": f"探索报酬跳页失败: {exc.__class__.__name__}: {exc}"}
            skips += 1
            actions += 1
            if not wait_transition_done(device):
                return {"status": "blocked", "actions": actions,
                        "detail": "探索报酬跳页后 Transition 未结束"}
            device.wait_settled(pre, max_wait=4.0)

        return {"status": "partial", "actions": actions, "detail": "领取快跑总超时"}
    except TransitionTimeout as exc:
        return {"status": "blocked", "actions": actions, "detail": str(exc)}
    except Exception as exc:
        return {"status": "partial", "actions": actions,
                "detail": f"{exc.__class__.__name__}: {exc}"}
