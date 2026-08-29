"""从探索录制生成识图剧本。

分工：模型只做语义挑选（哪些点击构成最短正确路径）与命名；
坐标回放、锚点裁剪、yaml 生成、验证执行全部由框架完成——零人工打标。

自校准：锚点默认以点击点为中心裁剪，但周围可能混入走动角色/特效等动态元素。
生成时若提供 device，则对多种尺寸/位置候选在"当前实时画面"上实测匹配分，
选最优者并按实测分动态设定阈值——避免动态背景拖垮匹配。
"""
import json
import time
from pathlib import Path

import cv2
import numpy as np
import yaml
from PIL import Image

from .brain import Brain
from .flow import FLOWS_DIR, anchor_visible, match_anchor, run_flow

COMMON_ANCHORS = FLOWS_DIR / "anchors" / "common"


def ensure_home(device, log=print, max_rounds: int = 8) -> bool:
    """把游戏导航回主页面（探索与引导回放的公共起点）。

    优先级：已在主页 → 左上 ◆ 返回 → 左上角兜底点击。
    """
    home = cv2.imread(str(COMMON_ANCHORS / "home_btn.png"), cv2.IMREAD_COLOR)
    back = cv2.imread(str(COMMON_ANCHORS / "back_btn.png"), cv2.IMREAD_COLOR)
    for i in range(max_rounds):
        f = device.screenshot()
        if anchor_visible(f, home, 0.85):
            if i:
                log(f"  已回到主页面（{i} 轮导航）")
            return True
        pos = None
        if back is not None and anchor_visible(f, back, 0.8):
            pos = match_anchor(f, back, 0.8)
            log(f"  [{i}] 点左上返回 ◆ …")
        if pos is None:
            log(f"  [{i}] 未识别返回控件，左上角兜底点击…")
            pos = (46, 35)
        device.click(*pos)
        device.wait_settled(f)
    return bool(anchor_visible(device.screenshot(), home, 0.85))

ANCHOR_W, ANCHOR_H = 150, 75  # 默认锚点裁剪尺寸（以点击点为中心）
CANDIDATE_SIZES = [(150, 75), (110, 55), (80, 40), (64, 32)]
OFFSET_TRIES = [(0, 0), (0, -14), (-14, 0), (14, 0), (0, 14)]


def _calibrate_anchor(pre_rgb: Image.Image, cx: int, cy: int, cur_bgr, adir: Path, name: str, log) -> dict:
    """多候选锚点在当前画面上实测选优，返回 {anchor, threshold}。"""
    best = None  # (score, threshold, w, h, ox, oy)
    for w, h in CANDIDATE_SIZES:
        for ox, oy in OFFSET_TRIES:
            x0 = max(0, min(pre_rgb.width - w, cx + ox - w // 2))
            y0 = max(0, min(pre_rgb.height - h, cy + oy - h // 2))
            crop = pre_rgb.crop((x0, y0, x0 + w, y0 + h))
            a = cv2.cvtColor(np.asarray(crop), cv2.COLOR_RGB2BGR)
            if cur_bgr is not None:
                res = cv2.matchTemplate(cur_bgr, a, cv2.TM_CCOEFF_NORMED)
                _, score, _, loc = cv2.minMaxLoc(res)
            else:
                score, loc = 1.0, (0, 0)
            cand = (score, x0, y0, w, h)
            if best is None or score > best[0]:
                best = cand
        if best and best[0] >= 0.97:  # 已近满分，没必要再试更小的
            break
    if best is None:
        return {"anchor": name, "threshold": 0.85}
    score, x0, y0, w, h = best
    crop = pre_rgb.crop((x0, y0, x0 + w, y0 + h))
    crop.save(adir / name)
    thr = round(max(0.72, min(0.95, score - 0.06)), 2)
    log(f"  锚点 {name}: 尺寸{w}x{h} 偏移({x0 - cx + w // 2},{y0 - cy + h // 2}) 实测分 {score:.2f} → 阈值 {thr}")
    return {"anchor": name, "threshold": thr}


def generate_flow(task: dict, brain: Brain, run_dir: Path, flow_id: str, log=print, device=None) -> dict | None:
    rec_path = run_dir / "record.json"
    if not rec_path.exists():
        log("[flowgen] 无录制记录")
        return None
    record = json.loads(rec_path.read_text(encoding="utf-8"))
    clicks = [r for r in record if r.get("action") == "click"]
    if not clicks:
        log("[flowgen] 录制中没有点击步骤")
        return None

    lines = [
        f"step{r['step']}: click({r['x']},{r['y']}) eff={r.get('eff', '?')}｜{r.get('thought', '')}"
        for r in clicks
    ]
    picked = brain.select_flow_steps(lines)
    if not picked:
        log("[flowgen] 模型未选出关键步骤")
        return None
    log(f"[flowgen] 模型选出 {len(picked)} 个关键步骤")

    cur_bgr = None
    if device is not None:
        try:
            cur_bgr = device.screenshot()  # 生成时刻的实时画面，用于锚点自校准
        except Exception as e:
            log(f"[flowgen] 取当前画面失败（跳过自校准）: {e}")

    adir = FLOWS_DIR / "anchors" / flow_id
    adir.mkdir(parents=True, exist_ok=True)
    steps = []
    for p in picked:
        try:
            n = int(p["ref_step"])
        except (KeyError, TypeError, ValueError):
            continue
        entry = next((r for r in clicks if r["step"] == n), None)
        if entry is None:
            log(f"[flowgen] ref_step {n} 不在录制中，跳过")
            continue
        img = Image.open(run_dir / entry["pre"])
        anchor_cfg = _calibrate_anchor(
            img, int(entry["x"]), int(entry["y"]), cur_bgr, adir, f"s{n}.png", log
        )
        steps.append({
            "name": str(p.get("name", f"step{n}"))[:20],
            "find": anchor_cfg,
            "retry": 2,
            "act": "click",
            "expect": {"change_above": 0.03},
        })

    if not steps:
        log("[flowgen] 没有可用的剧本步骤")
        return None

    flow_path = FLOWS_DIR / f"{flow_id}.yaml"
    flow_path.write_text(
        yaml.safe_dump({"name": task.get("name", flow_id), "steps": steps},
                       allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    log(f"[flowgen] 剧本已写入 {flow_path}（{len(steps)} 步）")
    return {"steps": len(steps)}


def verify_flow(device, flow_id: str, log=print) -> dict:
    """生成后立即自动验证（shadow 跑）。"""
    log(f"[flowgen] 自动验证剧本 {flow_id} …")
    return run_flow(device, flow_id, log=log)


def guided_generate(task: dict, brain: Brain, run_dir: Path, flow_id: str, device,
                    log=print, max_retry: int = 2) -> dict | None:
    """引导式剧本生成：按选出的关键步骤真实回放一遍。

    每步在"上一步执行后的画面"上（环境天然正确）做多候选锚点选优并现场定位点击，
    走完即完成生成与首轮验证；最后再跑一次标准 run_flow 二次验证。
    """
    rec_path = run_dir / "record.json"
    if not rec_path.exists():
        log("[flowgen] 无录制记录")
        return None
    record = json.loads(rec_path.read_text(encoding="utf-8"))
    clicks = [r for r in record if r.get("action") == "click"]
    if not clicks:
        log("[flowgen] 录制中没有点击步骤")
        return None

    lines = [
        f"step{r['step']}: click({r['x']},{r['y']}) eff={r.get('eff', '?')}｜{r.get('thought', '')}"
        for r in clicks
    ]
    verdict = brain.select_flow_steps(lines)
    picked = verdict.get("steps", [])
    existing = FLOWS_DIR / f"{flow_id}.yaml"
    if verdict.get("degenerate"):
        # 空走（如"进入→发现空→退出"）生成的剧本是无效的：
        # 保留已有剧本（若有），等实质路径可走时再重新探索
        if existing.exists():
            log(f"[flowgen] 探索退化（{verdict.get('reason', '')}），保留现有剧本")
        else:
            log(f"[flowgen] 探索退化（{verdict.get('reason', '')}），不生成剧本；等可执行真实路径时再 explore")
        return None
    if not picked:
        log("[flowgen] 模型未选出关键步骤")
        return None
    log(f"[flowgen] 模型选出 {len(picked)} 个关键步骤，开始引导回放")

    # 回放起点必须是主页面（与探索起点一致）
    if not ensure_home(device, log=log):
        log("[flowgen] ✗ 无法导航回主页面，放弃生成")
        return None

    adir = FLOWS_DIR / "anchors" / flow_id
    adir.mkdir(parents=True, exist_ok=True)
    steps = []
    for p in picked:
        try:
            n = int(p["ref_step"])
        except (KeyError, TypeError, ValueError):
            continue
        entry = next((r for r in clicks if r["step"] == n), None)
        if entry is None:
            log(f"[flowgen] ref_step {n} 不在录制中，跳过")
            continue
        img_pre = Image.open(run_dir / entry["pre"])
        cx, cy = int(entry["x"]), int(entry["y"])

        best = None
        for attempt in range(max_retry + 1):
            cur = device.screenshot()
            best = _best_candidate(img_pre, cx, cy, cur)
            if best and best[0] >= 0.8:
                break
            log(f"  步骤『{p.get('name')}』锚点定位不稳（{best[0] if best else 0:.2f}），重试 {attempt + 1}/{max_retry}")
            time.sleep(2.0)
        if not best or best[0] < 0.8:
            log(f"[flowgen] ✗ 步骤『{p.get('name')}』无法稳定定位，放弃生成")
            return None

        score, loc, w, h, crop_bgr = best
        name = f"s{n}.png"
        cv2.imwrite(str(adir / name), crop_bgr)
        thr = round(max(0.72, min(0.95, score - 0.06)), 2)
        log(f"  ✓ {p.get('name')}: 锚点 {w}x{h} 实测分 {score:.2f} 阈值 {thr}，回放点击{loc}")
        device.click(*loc)
        pre = device.screenshot()
        device.wait_settled(pre)
        steps.append({
            "name": str(p.get("name", f"step{n}"))[:20],
            "find": {"anchor": name, "threshold": thr},
            "retry": 2,
            "act": "click",
            "expect": {"change_above": 0.03},
        })

    if not steps:
        log("[flowgen] 没有可用的剧本步骤")
        return None

    flow_path = FLOWS_DIR / f"{flow_id}.yaml"
    flow_path.write_text(
        yaml.safe_dump({"name": task.get("name", flow_id), "steps": steps},
                       allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    log(f"[flowgen] 剧本已写入 {flow_path}（{len(steps)} 步），进行二次标准验证")
    return run_flow(device, flow_id, log=log)


def _best_candidate(img_pre_rgb: Image.Image, cx: int, cy: int, cur_bgr: np.ndarray):
    """多尺寸/偏移候选在当前画面上实测，返回 (score, loc, w, h, crop_bgr) 最优者。"""
    pre_bgr = cv2.cvtColor(np.asarray(img_pre_rgb), cv2.COLOR_RGB2BGR)
    H, W = pre_bgr.shape[:2]
    best = None
    for w, h in CANDIDATE_SIZES:
        for ox, oy in OFFSET_TRIES:
            x0 = max(0, min(W - w, cx + ox - w // 2))
            y0 = max(0, min(H - h, cy + oy - h // 2))
            crop = pre_bgr[y0:y0 + h, x0:x0 + w]
            res = cv2.matchTemplate(cur_bgr, crop, cv2.TM_CCOEFF_NORMED)
            _, score, _, loc = cv2.minMaxLoc(res)
            if best is None or score > best[0]:
                best = (score, (loc[0] + w // 2, loc[1] + h // 2), w, h, crop.copy())
        if best and best[0] >= 0.98:
            break
    return best
