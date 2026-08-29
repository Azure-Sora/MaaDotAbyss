"""M0-R 失焦点击交叉重测：排除"游戏 UI 偶发卡住"对结论的污染。

用法:
    python poc/m0_retest.py fg <tag>   # 前台基线：Seize 点"队伍"→截图→点"主页"→截图
    python poc/m0_retest.py bg <tag>   # 失焦测试：失焦→PostMessage 点"队伍"→截图

判读（人工看图 + 帧差辅助）：
    fg 基线没切页 → UI 卡住，先唤醒再测；
    fg 切页成功 而 bg 没切页 → 焦点门控坐实；
    bg 也切页 → 上一轮结论被推翻，普通后台可用。
"""
import argparse
import ctypes
import time

from maa.controller import Win32Controller
from maa.define import MaaWin32InputMethodEnum as In
from maa.define import MaaWin32ScreencapMethodEnum as SCap

from common import (
    find_game_window,
    init,
    save_image,
    unfocus_window,
    user32,
)

TEAM_BTN = (245, 648)
HOME_BTN = (115, 648)


def foreground_title() -> str:
    buf = ctypes.create_unicode_buffer(256)
    hwnd = user32.GetForegroundWindow()
    user32.GetWindowTextW(hwnd, buf, 256)
    return buf.value


def focus_game(hwnd: int) -> None:
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)
        time.sleep(0.5)
    for _ in range(3):
        user32.keybd_event(0x12, 0, 0, 0)
        user32.SetForegroundWindow(hwnd)
        user32.keybd_event(0x12, 0, 2, 0)
        time.sleep(0.5)
        if user32.GetForegroundWindow() == hwnd:
            return
    raise SystemExit(f"无法把游戏切到前台（当前前台: {foreground_title()!r}）")


def diff_ratio(p1, p2) -> float:
    import numpy as np
    from PIL import Image

    a = np.asarray(Image.open(p1), dtype=np.int16)
    b = np.asarray(Image.open(p2), dtype=np.int16)
    changed = (np.abs(a - b).max(axis=2) > 12).sum()
    return round(changed / (a.shape[0] * a.shape[1]) * 100, 2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=["fg", "bg", "postfg"])
    ap.add_argument("tag")
    args = ap.parse_args()

    init()
    w = find_game_window()
    if w is None:
        raise SystemExit("未找到游戏窗口")
    hwnd = int(w.hwnd)

    if args.phase == "fg":
        focus_game(hwnd)
        ctrl = Win32Controller(hwnd, SCap.ScreenDC | SCap.FramePool,
                               mouse_method=In.Seize, keyboard_method=In.Seize)
    elif args.phase == "postfg":
        # 判别实验：游戏在前台时 PostMessage 点击是否生效
        # （生效 → Unity 读消息队列，之前失败是别的原因；无效 → Unity 轮询硬件状态，消息注入彻底无效）
        focus_game(hwnd)
        print("聚焦后前台窗口:", foreground_title())
        if user32.GetForegroundWindow() != hwnd:
            raise SystemExit("游戏未在前台，实验无效")
        ctrl = Win32Controller(hwnd, SCap.FramePool,
                               mouse_method=In.PostMessage, keyboard_method=In.PostMessage)
    else:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)
            time.sleep(0.5)
        for i in range(3):
            unfocus_window(hwnd)
            if user32.GetForegroundWindow() != hwnd:
                break
            # 兜底：最小化让焦点自然转移，再用 SW_SHOWNOACTIVATE 恢复显示（不抢焦点）
            print(f"第{i + 1}次失焦失败，用最小化-无激活恢复兜底")
            user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE
            time.sleep(0.8)
            user32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE
            time.sleep(0.8)
            if user32.GetForegroundWindow() != hwnd:
                break
        else:
            raise SystemExit(f"无法让游戏失焦（前台: {foreground_title()!r}）")
        print("失焦后前台窗口:", foreground_title())
        ctrl = Win32Controller(hwnd, SCap.FramePool,
                               mouse_method=In.PostMessage, keyboard_method=In.PostMessage)

    if not ctrl.post_connection().wait().succeeded:
        raise SystemExit("控制器连接失败")

    p0 = (ctrl.post_screencap().wait(), save_image(ctrl.cached_image, f"re_{args.tag}_{args.phase}_0_before.png"))[1]
    ctrl.post_click(*TEAM_BTN).wait()
    time.sleep(1.8)
    p1 = (ctrl.post_screencap().wait(), save_image(ctrl.cached_image, f"re_{args.tag}_{args.phase}_1_team.png"))[1]
    print(f"点『队伍』后帧差: {diff_ratio(p0, p1)}%")

    if args.phase == "fg":
        ctrl.post_click(*HOME_BTN).wait()
        time.sleep(1.8)
        p2 = (ctrl.post_screencap().wait(), save_image(ctrl.cached_image, f"re_{args.tag}_{args.phase}_2_home.png"))[1]
        print(f"点『主页』后帧差: {diff_ratio(p1, p2)}%")


if __name__ == "__main__":
    main()
