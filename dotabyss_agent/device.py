"""设备层：MAA Win32 控制器封装（前台 Seize 输入 + ScreenDC|FramePool 截图）。

约定（来自 M0 实测，见 docs/research/06）：
- 一个进程只允许创建一个控制器实例；
- 截图后台用 FramePool 抗遮挡，前台 ScreenDC 快；
- 游戏窗口可遮挡、不可最小化（最小化时截图全黑）；
- 输入只有前台 Seize 有效（Unity 不消费窗口消息）。
"""
import ctypes
import time

import numpy as np

from maa.controller import Win32Controller
from maa.define import MaaWin32InputMethodEnum as In
from maa.define import MaaWin32ScreencapMethodEnum as SCap
from maa.toolkit import Toolkit

from .config import GAME_TITLE, LOCAL_DIR

user32 = ctypes.windll.user32


class DeviceError(RuntimeError):
    pass


class GameDevice:
    def __init__(self, title: str = GAME_TITLE):
        Toolkit.init_option(str(LOCAL_DIR))
        w = self._find_window(title)
        if w is None:
            raise DeviceError(f"未找到游戏窗口（标题需含: {title}）——请先人工启动游戏")
        self.hwnd = int(w.hwnd)
        self.window_title = w.window_name
        self.ctrl = Win32Controller(
            self.hwnd,
            screencap_method=SCap.ScreenDC | SCap.FramePool,
            mouse_method=In.Seize,
            keyboard_method=In.Seize,
        )
        if not self.ctrl.post_connection().wait().succeeded:
            raise DeviceError("MAA 控制器连接失败")
        if user32.IsIconic(self.hwnd):
            user32.ShowWindow(self.hwnd, 9)  # SW_RESTORE（最小化下截图全黑，必须恢复）
            time.sleep(1.0)

    @staticmethod
    def _find_window(title):
        for w in Toolkit.find_desktop_windows():
            if title in (w.window_name or ""):
                return w
        return None

    # ---- 窗口前台管理 -------------------------------------------------

    def is_foreground(self) -> bool:
        return user32.GetForegroundWindow() == self.hwnd

    def bring_to_front(self) -> None:
        """把游戏切到前台（Seize 输入的前提）。"""
        if self.is_foreground():
            return
        if user32.IsIconic(self.hwnd):
            user32.ShowWindow(self.hwnd, 9)
            time.sleep(0.5)
        for _ in range(3):
            user32.keybd_event(0x12, 0, 0, 0)   # ALT down，绕过前台锁定
            user32.SetForegroundWindow(self.hwnd)
            user32.keybd_event(0x12, 0, 2, 0)   # ALT up
            time.sleep(0.5)
            if self.is_foreground():
                return
        raise DeviceError("无法把游戏切到前台（用户可能正在操作电脑）")

    # ---- 感知 ---------------------------------------------------------

    def screenshot(self) -> np.ndarray:
        """返回 BGR numpy 数组（HxWx3）。"""
        self.ctrl.post_screencap().wait()
        img = self.ctrl.cached_image
        if img is None or img.size == 0:
            raise DeviceError("截图失败（窗口被最小化？）")
        return img

    # ---- 动作 ---------------------------------------------------------

    def click(self, x: int, y: int) -> None:
        if not self.is_foreground():
            self.bring_to_front()
        self.ctrl.post_click(int(x), int(y)).wait()

    # ---- 等待 ---------------------------------------------------------

    def wait_settled(self, ref_frame: np.ndarray, big_change: float = 0.05, max_wait: float = 8.0) -> np.ndarray:
        """点击后等待页面转换平息：持续截帧，画面相对基准大变化时刷新基准；
        连续 1.2s 无大变化即认为转场结束，返回当前帧。无变化的场景约 2s 即返回。"""
        t0 = time.time()
        last = ref_frame
        last_change = time.time()
        cur = ref_frame
        while time.time() - t0 < max_wait:
            time.sleep(0.6)
            cur = self.screenshot()
            if self.diff_ratio(last, cur) > big_change:
                last = cur
                last_change = time.time()
            elif time.time() - last_change >= 1.2:
                break
        return cur

    def diff_ratio(self, a: np.ndarray, b: np.ndarray) -> float:
        d = np.abs(a.astype(np.int16) - b.astype(np.int16)).max(axis=2)
        return float((d > 12).mean())

    def wait_until_stable(
        self,
        quiet_seconds: float = 2.0,
        threshold: float = 0.01,
        timeout: float = 180.0,
        poll: float = 0.7,
    ) -> bool:
        """帧差稳定检测：连续 quiet_seconds 内画面变化率低于 threshold 视为稳定。

        用于等待加载、入场动画、自动战斗结束（游戏失焦/前台都在渲染）。
        """
        prev = self.screenshot()
        stable_since: float | None = None
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(poll)
            cur = self.screenshot()
            if self.diff_ratio(prev, cur) < threshold:
                stable_since = stable_since or time.time()
                if time.time() - stable_since >= quiet_seconds:
                    return True
            else:
                stable_since = None
            prev = cur
        return False
