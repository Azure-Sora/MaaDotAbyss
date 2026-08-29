"""探针2：拖拽灵敏度定量 + 西向连续扫读验证"到边判停" + 弹回检查 + 复位。

- 先对已存的 2/3 帧做相位相关，校准 shift 符号与灵敏度；
- 再连续向左拖（镜头西移）直到地图内容不再移动 = 到边；
- 等 2s 复查帧间位移 ≈0（无弹回）；
- 最后按累计位移拖回原位。

用 phaseCorrelate 而非 diff_ratio：地图有雾气/光柱常驻动画，原始帧差永远不归零，
房间位移必须用整体相位（房间纹理主导）来测。
"""
import sys
import time
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotabyss_agent.device import GameDevice

OUT = Path(__file__).parent / "abyss_swipe_out"
DRAG_PX = 600          # 每次鼠标位移
DRAG_Y = 350           # 拖拽高度（避开 HUD/侧栏/撤退）
SWIPE_MS = 600


def shift_px(a: np.ndarray, b: np.ndarray) -> tuple[float, float]:
    """b 相对 a 的内容位移（像素，相位相关，取整）。"""
    ga = cv2.cvtColor(a, cv2.COLOR_BGR2GRAY).astype(np.float32)
    gb = cv2.cvtColor(b, cv2.COLOR_BGR2GRAY).astype(np.float32)
    win = cv2.createHanningWindow((ga.shape[1], ga.shape[0]), cv2.CV_32F)
    (dx, dy), _ = cv2.phaseCorrelate(ga, gb, win)
    return round(float(dx), 1), round(float(dy), 1)


def main():
    # ---- 1. 用已存帧校准：2→3 是一次成功的西移（内容右移） ----
    f2 = cv2.imread(str(OUT / "2_snapback_check.png"))
    f3 = cv2.imread(str(OUT / "3_drag_back.png"))
    dx, dy = shift_px(f2, f3)
    print(f"[校准] 2→3 位移: dx={dx}, dy={dy}  （300px 拖动 → {abs(dx):.0f}px 地图位移，"
          f"灵敏度≈{abs(dx) / 300:.2f}）")

    # ---- 2. 实况：连续向西扫 ----
    d = GameDevice()
    d.bring_to_front()
    time.sleep(0.5)

    frames = []
    f = d.screenshot()
    cv2.imwrite(str(OUT / "s0_start.png"), f)
    frames.append(f)
    total = 0.0
    for i in range(1, 7):
        d.swipe(350, DRAG_Y, 350 + DRAG_PX, DRAG_Y, duration_ms=SWIPE_MS)  # 向右拖=镜头西移
        time.sleep(0.9)
        f = d.screenshot()
        cv2.imwrite(str(OUT / f"s{i}_west.png"), f)
        dx, dy = shift_px(frames[-1], f)
        frames.append(f)
        total += abs(dx)
        print(f"[扫读 {i}] 位移 dx={dx}, dy={dy}" + ("   ← 到边（≈0）" if abs(dx) < 3 else ""))
        if abs(dx) < 3:
            break

    # ---- 3. 弹回检查 ----
    time.sleep(2.0)
    fs = d.screenshot()
    cv2.imwrite(str(OUT / "s_snapback.png"), fs)
    dx, dy = shift_px(frames[-1], fs)
    print(f"[弹回检查] 2s 后位移 dx={dx}, dy={dy}  (|dx|<3 = 镜头稳定)")

    # ---- 4. 复位：向东拖回累计位移 ----
    if total > 3:
        back = int(total / max(abs(dx if dx else 1), 1) * 300) if False else int(total / 0.72)
        d.swipe(350 + back, DRAG_Y, 350, DRAG_Y, duration_ms=800)
        time.sleep(0.9)
        fe = d.screenshot()
        cv2.imwrite(str(OUT / "s_restore.png"), fe)
        dx, dy = shift_px(frames[0], fe)
        print(f"[复位] 与起点位移 dx={dx}, dy={dy}（|dx| 大也无妨，現在地へ 可归位）")


if __name__ == "__main__":
    main()
