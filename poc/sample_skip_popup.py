"""一次性采样：当前弹窗的 UI 树（场景/文本/按钮）+ 截屏，供新跳过弹窗接入用。"""
import sys

sys.path.insert(0, r"E:\Codes\MaaDotAbyss")

import cv2

from dotabyss_agent.device_bridge import BridgeDevice


def walk(n):
    yield n
    for c in n.get("children", []):
        yield from walk(c)


def walk_all(tree):
    for c0 in tree.get("canvases", []):
        yield from walk(c0)


dev = BridgeDevice()
print("bridge:", dev)

frame = dev.screenshot()
cv2.imwrite(r"E:\Codes\MaaDotAbyss\_refs\skip_popup_sample.png", frame)
print("screenshot saved")

tree = dev.ui_tree(max_nodes=30000)
print("scene:", tree.get("scene"))
print("--- canvases ---")
for c0 in tree.get("canvases", []):
    n_nodes = sum(1 for _ in walk(c0))
    print(f"  {c0.get('name')}  ({n_nodes} nodes)")
print("--- texts ---")
for n in walk_all(tree):
    t = (n.get("text") or "").strip()
    if t:
        print(f"  {n.get('name')}  screen={n.get('screen')}  text={t[:80]!r}")
print("--- buttons ---")
for n in walk_all(tree):
    b = n.get("button")
    if b:
        print(f"  {n.get('name')}  path={b.get('path')}  interactable={b.get('interactable')}")
