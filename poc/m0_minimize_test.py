"""M0-4 伪最小化截图测试。

最小化窗口后用 FramePool 截图——MAA 内置伪最小化（透明+免激活恢复），
理论上仍能截到画面且不打扰用户；测完恢复窗口。
"""
import ctypes
import time

from maa.controller import Win32Controller
from maa.define import MaaWin32InputMethodEnum as In
from maa.define import MaaWin32ScreencapMethodEnum as SCap

from common import find_game_window, init, save_image

user32 = ctypes.windll.user32


def shot(ctrl, name):
    ctrl.post_screencap().wait()
    print(name, save_image(ctrl.cached_image, name))


def main():
    init()
    w = find_game_window()
    if w is None:
        raise SystemExit("未找到游戏窗口")
    hwnd = int(w.hwnd)

    ctrl = Win32Controller(
        hwnd,
        screencap_method=SCap.FramePool,
        mouse_method=In.PostMessage,
        keyboard_method=In.PostMessage,
    )
    if not ctrl.post_connection().wait().succeeded:
        raise SystemExit("控制器连接失败")

    shot(ctrl, "min_normal.png")

    user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE
    time.sleep(1.0)
    print("已最小化:", bool(user32.IsIconic(hwnd)))
    shot(ctrl, "min_minimized.png")
    print("截图后仍最小化:", bool(user32.IsIconic(hwnd)))

    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    time.sleep(1.0)
    shot(ctrl, "min_restored.png")


if __name__ == "__main__":
    main()
