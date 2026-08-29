"""M0-1 窗口绑定与截图测试。

验证：能否按标题找到游戏窗口；前台（ScreenDC|DesktopDup_Window）与
后台（FramePool|PrintWindow）两种截图方式是否出图。
前台方式不支持最小化窗口，测试前会先恢复窗口。
"""
import ctypes
import sys
import time

from maa.controller import Win32Controller
from maa.define import MaaWin32InputMethodEnum as In
from maa.define import MaaWin32ScreencapMethodEnum as SCap

from common import find_game_window, init, save_image

user32 = ctypes.windll.user32


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "all"  # fg | bg | all
    init()
    w = find_game_window()
    if w is None:
        raise SystemExit("未找到游戏窗口（标题需含: ドットアビスX）")
    print(f"窗口: hwnd={w.hwnd} class={w.class_name!r} title={w.window_name!r}")
    print("最小化:", bool(user32.IsIconic(int(w.hwnd))))

    if which in ("fg", "all"):
        print("[step] 创建前台控制器", flush=True)
        fg = Win32Controller(
            w.hwnd,
            screencap_method=SCap.ScreenDC | SCap.DXGI_DesktopDup_Window,
            mouse_method=In.Seize,
            keyboard_method=In.Seize,
        )
        if user32.IsIconic(int(w.hwnd)):
            user32.ShowWindow(int(w.hwnd), 9)  # SW_RESTORE
            time.sleep(1.0)
        print("[step] 前台连接", flush=True)
        if not fg.post_connection().wait().succeeded:
            raise SystemExit("前台控制器连接失败（截图/输入单元初始化失败）")
        print("[step] 前台截图", flush=True)
        fg.post_screencap().wait()
        print("前台截图:", save_image(fg.cached_image, "fg_screencap.png"), flush=True)

    if which in ("bg", "all"):
        print("[step] 创建后台控制器", flush=True)
        bg = Win32Controller(
            w.hwnd,
            screencap_method=SCap.FramePool | SCap.PrintWindow,
            mouse_method=In.PostMessage,
            keyboard_method=In.PostMessage,
        )
        print("[step] 后台连接", flush=True)
        if not bg.post_connection().wait().succeeded:
            raise SystemExit("后台控制器连接失败（截图/输入单元初始化失败）")
        print("[step] 后台截图", flush=True)
        bg.post_screencap().wait()
        print("后台截图:", save_image(bg.cached_image, "bg_screencap.png"), flush=True)


if __name__ == "__main__":
    main()

