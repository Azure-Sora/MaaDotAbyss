"""深渊锚点素材裁剪：从实机截图裁出模板 → tasks/flows/anchors/abyss/。

MANUAL 表：name -> (源图路径, (x0, y0, x1, y1))，坐标为该图内像素。
裁完自动在 .local/poc_out/abyss_anchor_review/ 生成核对图（裁片放大 2x）。
锚点原则：只裁控件/标签本体小区域（doc 09——动态背景拖不走小模板）。

已核实素材来自 .local/poc_out/abyss_live.png（1280x720 客户区实拍）；
其余条目等游戏再停在对应页面时补充（坐标从现场实拍帧取，勿用窗口截图换算）。
"""
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotabyss_agent.config import TASKS_DIR  # noqa: E402

OUT = TASKS_DIR / "flows" / "anchors" / "abyss"
REVIEW = Path(__file__).resolve().parent.parent / ".local" / "poc_out" / "abyss_anchor_review"
LIVE = Path(__file__).resolve().parent.parent / ".local" / "poc_out" / "abyss_live.png"

MANUAL = {
    # ---- 已核实（abyss_live.png 实拍帧） ----
    "chevron.png":     (LIVE, (245, 240, 335, 305)),   # 紫色候选箭头（当前可进入房间标记）
    "label_elite.png": (LIVE, (588, 224, 698, 258)),   # 強敵 标签横幅
    "label_heal.png":  (LIVE, (225, 462, 332, 498)),   # 回復 标签横幅
    "ctx_retreat.png": (LIVE, (28, 602, 122, 696)),    # 撤退按钮（地图页上下文锚点，绝不点击）
    # ---- 待补（游戏停到对应页面后从实拍帧取坐标） ----
    # "label_battle.png":    (…, (…)),   # 戦闘
    # "label_event.png":     (…, (…)),   # イベント
    # "label_shop.png":      (…, (…)),   # 商店
    # "label_treasure.png":  (…, (…)),   # 宝箱
    # "label_boss.png":      (…, (…)),   # ボス
    # "btn_continue.png":    (…, (…)),   # 続行する（探索続行確認）
    # "btn_return.png":      (…, (…)),   # 帰還する
    # "btn_use.png":         (…, (…)),   # 使用（倍率弹窗）
    # "btn_safecase_cancel.png": (…),   # キャンセル（安全箱）
    # "btn_confirm.png":     (…, (…)),   # 確定（覆盖层通用）
    # "hud_erosion.png":     (…, (…)),   # 浸食率 HUD 区（数字识别用）
}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    REVIEW.mkdir(parents=True, exist_ok=True)
    cache: dict[str, np.ndarray] = {}
    for name, (src, box) in MANUAL.items():
        if src not in cache:
            cache[src] = cv2.imread(str(src), cv2.IMREAD_COLOR)
        img = cache[src]
        if img is None:
            print(f"✗ 源图不存在: {src}")
            continue
        x0, y0, x1, y1 = box
        crop = img[y0:y1, x0:x1]
        cv2.imwrite(str(OUT / name), crop)
        big = cv2.resize(crop, None, fx=2, fy=2, interpolation=cv2.INTER_NEAREST)
        cv2.imwrite(str(REVIEW / name), big)
        print(f"✓ {name}: {crop.shape[1]}x{crop.shape[0]} ← {Path(src).name}{box}")
    print(f"\n锚点 → {OUT}\n核对图 → {REVIEW}")


if __name__ == "__main__":
    main()
