"""M0-2 点击测试。

用法:
    python poc/m0_click_test.py seize <x> <y>   # 前台 Seize：激活窗口、移动真实鼠标点击
    python poc/m0_click_test.py post  <x> <y>   # 后台 PostMessage：不抢焦点，向窗口投递消息

点击前后各存一张截图（.local/poc_out/before_*/after_*），人工比对验证点击是否生效。
"""
import argparse
import ctypes
import time

from maa.controller import Win32Controller
from maa.define import MaaWin32InputMethodEnum as In
from maa.define import MaaWin32ScreencapMethodEnum as SCap

from common import find_game_window, init, save_image

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32


def foreground_title() -> str:
    buf = ctypes.create_unicode_buffer(256)
    hwnd = user32.GetForegroundWindow()
    user32.GetWindowTextW(hwnd, buf, 256)
    return buf.value


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
    if user32.GetForegroundWindow() == hwnd:
        print("警告：游戏仍在前台，本次 post 点击不是严格失焦测试")
    else:
        print("游戏已失焦，进行严格后台点击测试")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["seize", "post", "send", "anchored"])
    ap.add_argument("x", type=int)
    ap.add_argument("y", type=int)
    args = ap.parse_args()

    init()
    w = find_game_window()
    if w is None:
        raise SystemExit("未找到游戏窗口")
    hwnd = int(w.hwnd)

    if args.mode == "seize":
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            time.sleep(0.5)
        # 安全检查：必须确认游戏在前台，否则 Seize 点击会落在别的窗口
        for _ in range(3):
            user32.keybd_event(0x12, 0, 0, 0)  # ALT 按下，绕过前台锁定
            user32.SetForegroundWindow(hwnd)
            user32.keybd_event(0x12, 0, 2, 0)  # ALT 抬起
            time.sleep(0.5)
            if user32.GetForegroundWindow() == hwnd:
                break
        else:
            raise SystemExit(f"无法把游戏切到前台（当前前台: {foreground_title()!r}），中止点击")
        mouse = In.Seize
    else:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE（恢复会激活窗口；无人值守时可接受）
            time.sleep(0.5)
        unfocus_window(hwnd)
        mouse = {
            "post": In.PostMessage,
            "send": In.SendMessage,
        }.get(args.mode)
        if mouse is None:
            raise SystemExit("AnchoredTouch 需要更新的 MaaFw（pip 5.12.3 尚未包含，主分支才有）")

    print(f"点击前前台窗口: {foreground_title()!r}")

    # 后台测试固定用 FramePool（WGC 抗遮挡）；ScreenDC 在窗口被遮挡时会截到遮挡物
    cap = SCap.FramePool if args.mode != "seize" else (SCap.ScreenDC | SCap.FramePool)

    ctrl = Win32Controller(
        hwnd,
        screencap_method=cap,
        mouse_method=mouse,
        keyboard_method=In.Seize,
    )
    if not ctrl.post_connection().wait().succeeded:
        raise SystemExit("控制器连接失败")
    ctrl.post_screencap().wait()
    print("点击前:", save_image(ctrl.cached_image, f"before_{args.mode}.png"))

    ctrl.post_click(args.x, args.y).wait()
    time.sleep(1.5)

    ctrl.post_screencap().wait()
    print("点击后:", save_image(ctrl.cached_image, f"after_{args.mode}.png"))


if __name__ == "__main__":
    main()
