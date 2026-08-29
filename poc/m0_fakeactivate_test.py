"""M0-5 伪造激活 + 消息点击测试。

Unity 在失焦时忽略鼠标消息，但渲染不停。尝试先向窗口投递 WM_ACTIVATE(WA_ACTIVE)
伪造"我在前台"，再用 PostMessage 点击，看能否绕过焦点门控。
"""
import ctypes
import time

from maa.controller import Win32Controller
from maa.define import MaaWin32InputMethodEnum as In
from maa.define import MaaWin32ScreencapMethodEnum as SCap

from common import find_game_window, init, save_image, unfocus_window

user32 = ctypes.windll.user32
WM_ACTIVATE = 0x0006
WA_ACTIVE = 1
WA_INACTIVE = 0


def main():
    init()
    w = find_game_window()
    if w is None:
        raise SystemExit("未找到游戏窗口")
    hwnd = int(w.hwnd)

    # 确保失焦
    if user32.GetForegroundWindow() == hwnd:
        unfocus_window(hwnd)
    if user32.GetForegroundWindow() == hwnd:
        raise SystemExit("游戏仍在前台，无法测试")

    ctrl = Win32Controller(
        hwnd,
        screencap_method=SCap.FramePool,
        mouse_method=In.PostMessage,
        keyboard_method=In.PostMessage,
    )
    if not ctrl.post_connection().wait().succeeded:
        raise SystemExit("控制器连接失败")

    ctrl.post_screencap().wait()
    print("点击前:", save_image(ctrl.cached_image, "fa_before.png"))

    # 伪造激活状态，再注入点击
    user32.PostMessageW(hwnd, WM_ACTIVATE, WA_ACTIVE, 0)
    time.sleep(0.2)
    ctrl.post_click(823, 648).wait()  # 冒险按钮
    time.sleep(1.5)

    ctrl.post_screencap().wait()
    print("点击后:", save_image(ctrl.cached_image, "fa_after.png"))

    # 恢复非激活状态，避免游戏焦点状态错乱
    user32.PostMessageW(hwnd, WM_ACTIVATE, WA_INACTIVE, 0)


if __name__ == "__main__":
    main()
