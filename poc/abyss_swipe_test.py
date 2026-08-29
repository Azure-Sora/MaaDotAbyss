"""深渊地图横向拖拽可行性探针：Seize swipe 能否平移镜头。

前提：游戏窗口已打开且停在深渊地图页。只拖不点，不改动游戏状态；
结束后把镜头拖回原位（拖不回也无妨，地图页有 現在地へ 可手动归位）。
"""
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotabyss_agent.device import GameDevice

OUT = Path(__file__).parent / "abyss_swipe_out"


def save(frame: np.ndarray, name: str) -> None:
    p = OUT / name
    Image.fromarray(frame[:, :, ::-1]).save(p)
    print(f"saved {p.name}")


def main():
    OUT.mkdir(exist_ok=True)
    d = GameDevice()
    d.bring_to_front()
    time.sleep(0.5)

    # 中下部偏右的空旷区起手（避开顶部 HUD / 右侧栏 / 左下撤退），向右拖 300px
    f0 = d.screenshot()
    save(f0, "0_before.png")
    d.swipe(950, 600, 650, 600, duration_ms=500)
    time.sleep(0.8)
    f1 = d.screenshot()
    save(f1, "1_after_swipe.png")
    print(f"拖拽后帧差: {d.diff_ratio(f0, f1):.3f}   (≈0 = 镜头没动/失败)")

    time.sleep(1.5)  # 观察是否自动弹回
    f2 = d.screenshot()
    save(f2, "2_snapback_check.png")
    print(f"1.5s 后帧差: {d.diff_ratio(f1, f2):.3f}   (≈0 = 镜头停住，无弹回)")

    d.swipe(650, 600, 950, 600, duration_ms=500)  # 拖回原位
    time.sleep(0.8)
    f3 = d.screenshot()
    save(f3, "3_drag_back.png")
    print(f"拖回后与初始帧差: {d.diff_ratio(f0, f3):.3f}   (小 = 成功复位)")


if __name__ == "__main__":
    main()
