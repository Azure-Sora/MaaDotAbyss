"""M0 POC 公共工具：初始化 MAA、查找游戏窗口、保存截图、窗口焦点操作。"""
import ctypes
import time
from pathlib import Path

import numpy as np
from PIL import Image

from maa.toolkit import Toolkit

GAME_TITLE = "ドットアビスX"
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / ".local" / "poc_out"

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32


def init() -> None:
    Toolkit.init_option(str(ROOT / ".local"))


def find_game_window():
    for w in Toolkit.find_desktop_windows():
        if GAME_TITLE in (w.window_name or ""):
            return w
    return None


def save_image(img: np.ndarray, name: str) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    Image.fromarray(img[:, :, ::-1]).save(path)  # MAA 图像为 BGR，转 RGB 后保存
    return path


def unfocus_window(hwnd: int) -> None:
    """把前台焦点让给桌面，确保目标窗口失焦（AttachThreadInput 绕过前台锁定）。"""
    fg = user32.GetForegroundWindow()
    cur_thread = kernel32.GetCurrentThreadId()
    fg_thread = user32.GetWindowThreadProcessId(fg, None)
    attached = False
    if fg == hwnd and fg_thread:
        attached = bool(user32.AttachThreadInput(cur_thread, fg_thread, True))
    user32.SetForegroundWindow(user32.GetShellWindow())
    if attached:
        user32.AttachThreadInput(cur_thread, fg_thread, False)
    time.sleep(0.5)
