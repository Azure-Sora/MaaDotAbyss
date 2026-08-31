"""generic_sweep：LLM 编排的通用扫荡解释器（docs/research/14 三个手写 sweep 的泛化）。

分工原则不变：LLM 出脑子（observe 读树 → 编排一份 JSON 参数），程序出手脚
（固定控制流：场景校验 → 目标循环 → 点击链 → 战斗宏 → 进展确认 → 收尾链）。
战斗/转场/消费红线/禁区熔断全部固化在本解释器与 macros 里，不依赖模型自觉。

编排成功且执行 done 的程序可经 save_as 存盘为命名 routine
（tasks/routines/<name>.json），注册表启动时加载，下次直接按名调用。

params schema（详见 validate_params）：
  home_scene    入口场景名（observe 输出顶部的 scene，必填）
  targets       目标筛选：canvas / btn_suffix / btn_contains / exclude_path /
                text_must / text_not / require_text / interactable_only / max_targets
  click_chain   每个目标的点击链：首项必须是 "{target.path}"（点目标本体），
                其余为弹窗按钮路径后缀；"!"前缀=必须出现否则中止，默认等待后跳过
  after_each    "battle"（每目标跑战斗宏回家）| "none"（纯领取，不战斗）
  finish_chain  清完目标后（至少清掉 1 个）的收尾点击链，语义同链尾；
                "{pending.path}" 占位=打开一个仍未清的目标（如剩余 boss 详情，
                供后续スキップ类按钮使用）；链尾自动排空残留结果弹窗
  popup_timeout 单个弹窗等待秒数（默认 8）
"""
import json
import re
import time

from .config import TASKS_DIR
from .macros import (
    battle_and_return, click_path, collect_buttons, collect_texts,
    find_btn, popup_cancel_consume, scene, walk,
)

ROUTINES_DIR = TASKS_DIR / "routines"
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,39}$")
SUSPECT_LIMIT = 2   # 连续 N 次"目标签名未变"视为异常，交还 LLM


class BadProgram(ValueError):
    """LLM 编排的参数不合法（detail 会带回给模型修正重发）。"""


# ---- 参数校验 ------------------------------------------------------------

def _str_list(v, key: str, cap: int = 10) -> list[str]:
    if v is None:
        return []
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise BadProgram(f"{key} 必须是字符串列表")
    if len(v) > cap:
        raise BadProgram(f"{key} 最多 {cap} 项")
    return [s.strip() for s in v if str(s).strip()]


def _opt_str(v) -> str | None:
    """可选字符串字段：None/空串 → None。必须显式处理 None——
    str(None) 会变成字符串 "None"，validate_params 二次规范化时即被污染。"""
    s = v.strip() if isinstance(v, str) else ""
    return s or None


def validate_params(p: dict) -> dict:
    """校验并规范化编排参数；不合法抛 BadProgram（消息面向模型可读）。"""
    if not isinstance(p, dict):
        raise BadProgram("params 必须是 JSON 对象")
    home = _opt_str(p.get("home_scene"))
    if not home:
        raise BadProgram("home_scene 必填——先 observe 看输出顶部的 scene 名")
    t = p.get("targets") or {}
    if not isinstance(t, dict):
        raise BadProgram("targets 必须是对象")
    suffix = _opt_str(t.get("btn_suffix"))
    contains = _opt_str(t.get("btn_contains"))
    if not suffix and not contains:
        raise BadProgram("targets 至少给 btn_suffix 或 btn_contains 之一（目标按钮的路径特征）")
    chain = _str_list(p.get("click_chain"), "click_chain", cap=8)
    if not chain:
        raise BadProgram("click_chain 必填")
    if chain[0] != "{target.path}":
        raise BadProgram('click_chain 首项必须是 "{target.path}"（先点目标本体）')
    after = str(p.get("after_each", "battle")).strip()
    if after not in ("battle", "none"):
        raise BadProgram("after_each 只能是 battle 或 none")
    try:
        max_t = int(t.get("max_targets", 99))
        popup_t = float(p.get("popup_timeout", 8.0))
    except (TypeError, ValueError):
        raise BadProgram("max_targets / popup_timeout 必须是数字") from None
    if not 1 <= max_t <= 50:
        raise BadProgram("max_targets 范围 1~50")
    return {
        "home_scene": home,
        "targets": {
            "canvas": _opt_str(t.get("canvas")),
            "btn_suffix": suffix,
            "btn_contains": contains,
            "exclude_path": _str_list(t.get("exclude_path"), "exclude_path"),
            "text_must": _opt_str(t.get("text_must")),
            "text_not": _str_list(t.get("text_not"), "text_not"),
            "require_text": bool(t.get("require_text", True)),
            "interactable_only": bool(t.get("interactable_only", True)),
            "max_targets": max_t,
        },
        "click_chain": chain,
        "after_each": after,
        "finish_chain": _str_list(p.get("finish_chain"), "finish_chain", cap=8),
        "popup_timeout": min(max(popup_t, 3.0), 30.0),
    }


# ---- 目标读取 ------------------------------------------------------------

def _nodes_with_parent(node, anc_active=True, parent=None):
    """walk 的带父版：yield (node, eff_active, parent_node)。"""
    a = anc_active and bool(node.get("active", True))
    yield node, a, parent
    for c in node.get("children", []):
        yield from _nodes_with_parent(c, a, node)


def _blob(node) -> str:
    """节点子树的可见文本聚合（短文本去重拼接），作为目标的上下文签名。"""
    parts = []
    for m, ma in walk(node):
        if not ma:
            continue
        t = m.get("text")
        if t:
            s = str(t).strip()
            if 0 < len(s) < 40 and s not in parts:
                parts.append(s)
    return " ".join(parts)[:80]


def _read_targets(device, spec: dict) -> dict[str, str]:
    """当前待清目标：{完整路径: 上下文文本}。

    文本上下文取"按钮子树 + 父节点子树"——同时覆盖两种已清形态：
    计数递减（势力任务 4/3→3/3，count 在按钮子树里）与标签消失
    （迎击战 boss Label 消失，label 是按钮的兄弟节点）。重读后签名
    不变才算"无可疑进展"。
    """
    tree = device.ui_tree(max_nodes=30000)
    out: dict[str, str] = {}
    for cv in tree.get("canvases", []):
        if spec["canvas"] and cv.get("name") != spec["canvas"]:
            continue
        for n, a, parent in _nodes_with_parent(cv):
            b = n.get("button")
            if not b or not a:
                continue
            p = str(b.get("path", ""))
            if spec["btn_suffix"] and not p.endswith(spec["btn_suffix"]):
                continue
            if spec["btn_contains"] and spec["btn_contains"] not in p:
                continue
            if any(x in p for x in spec["exclude_path"]):
                continue
            if spec["interactable_only"] and not b.get("interactable"):
                continue
            texts = [_blob(n)]
            if parent is not None and parent is not n:
                texts.append(_blob(parent))
            blob = " ".join(dict.fromkeys(" ".join(texts).split()))[:80]
            if spec["require_text"] and not blob:
                continue
            if spec["text_must"] and spec["text_must"] not in blob:
                continue
            if any(x in blob for x in spec["text_not"]):
                continue
            out[p] = blob
    return out


# ---- 链执行 --------------------------------------------------------------

def _wait_click_popup(device, item: str, popup_timeout: float, log=print,
                      stop_event=None) -> tuple[str | None, str]:
    """等待链中一项（弹窗按钮路径后缀）出现并可点，点击后返回 (路径, 说明)。

    "!"前缀 = 必须出现（等 1.5 倍时长，缺失即失败）；默认宽松，超时跳过。
    轮询中始终先查消费红线弹窗（消費/回復/購入 → キャンセル）。
    """
    required = item.startswith("!")
    suffix = item.lstrip("!")
    t0 = time.time()
    timeout = popup_timeout * (1.5 if required else 1.0)
    while time.time() - t0 < timeout:
        if stop_event is not None and stop_event.is_set():
            return None, "用户停止"
        tree = device.ui_tree(max_nodes=30000)
        front = collect_buttons(tree, canvas="Front")
        cancel = popup_cancel_consume(tree)
        if cancel:
            click_path(device, cancel)
            log("  [sweep] 消费确认弹窗 → キャンセル（红线）")
            time.sleep(1.2)
            continue
        b = find_btn(front, text="分解する")
        if b:   # 自动分解确认（装备满）：用户已启用自动分解，分解=授权行为
            click_path(device, b["path"])
            log("  [sweep] 自动分解确认 → 分解する（已授权）")
            time.sleep(1.2)
            continue
        b = find_btn(front, name_suffix="Popup_MileageResult(Clone)/Box/Popup_Close")
        if b:   # 通行证里程结算：纯通知弹窗，关掉继续
            click_path(device, b["path"])
            log("  [sweep] 里程结算弹窗 → 关闭")
            time.sleep(1.2)
            continue
        b = find_btn(collect_buttons(tree), name_suffix=suffix)
        if b and b["interactable"]:
            click_path(device, b["path"])
            return b["path"], "ok"
        time.sleep(0.8)
    return None, (f"必要弹窗未出现: {suffix}" if required else f"弹窗未出现(跳过): {suffix}")


def _run_chain(device, spec: dict, target_path: str, log=print,
               stop_event=None) -> tuple[bool, str]:
    """执行一个目标的点击链。返回 (是否成功, 失败说明)。"""
    for i, item in enumerate(spec["click_chain"]):
        if i == 0:
            if not click_path(device, target_path):
                return False, f"点目标失败: {target_path.split('/')[-1]}"
            continue
        p, why = _wait_click_popup(device, item, spec["popup_timeout"], log, stop_event)
        if p is None:
            if why == "用户停止":
                return False, why
            if item.startswith("!"):
                return False, why
            log(f"  [sweep] {why}")
            continue
        time.sleep(0.5)
    return True, ""


# ---- 主流程 --------------------------------------------------------------

def _at_home(device, home: str, tries: int = 3, gap: float = 1.5) -> bool:
    """入口场景确认：桥瞬时超时会把 scene() 读成空串，多读几次防误判。"""
    for i in range(tries):
        if scene(device) == home:
            return True
        if i < tries - 1:
            time.sleep(gap)
    return False


def generic_sweep(device, params: dict | None = None, *, log=print,
                  stop_event=None, frame_cb=None, timeout: float = 1200.0) -> dict:
    """执行 LLM 编排的扫荡程序。返回契约与手写 sweep 一致：
    {"status": done|partial|wrong_scene, "cleared": 场数, "detail": 说明}。"""
    try:
        spec = validate_params(params or {})
    except BadProgram as e:
        return {"status": "partial", "cleared": 0,
                "detail": f"编排参数不合法: {e}（修正后重新编排）"}
    home = spec["home_scene"]
    if not _at_home(device, home):
        return {"status": "wrong_scene", "cleared": 0,
                "detail": f"不在 {home}，请先导航（scene 名以 observe 输出顶部为准）"}
    t0 = time.time()
    cleared, suspect = 0, 0
    ts = spec["targets"]
    while cleared < ts["max_targets"]:
        if stop_event is not None and stop_event.is_set():
            return {"status": "partial", "cleared": cleared, "detail": "用户停止"}
        if time.time() - t0 > timeout:
            return {"status": "partial", "cleared": cleared, "detail": "总超时"}
        targets = _read_targets(device, ts)
        if not targets:
            break
        path, blob = next(iter(targets.items()))
        log(f"[sweep] 目标 {path.split('/')[-1]}｜{blob[:40]}")
        ok, why = _run_chain(device, spec, path, log, stop_event)
        if not ok:
            return {"status": "partial", "cleared": cleared, "detail": why}
        if spec["after_each"] == "battle":
            if not battle_and_return(device, home, log=log, timeout=240, frame_cb=frame_cb):
                return {"status": "partial", "cleared": cleared,
                        "detail": "战斗/结算超时未回入口页"}
        cleared += 1
        time.sleep(2.0)   # 结算数据刷新延迟：等一下再重读定责
        again = _read_targets(device, ts).get(path)
        if again is None or again != blob:
            suspect = 0
            log(f"[sweep] ✓ 清掉一个（累计 {cleared}）")
        else:
            suspect += 1
            log(f"[sweep] ? 目标状态未变（suspect {suspect}）——可能败北或界面延迟")
            if suspect >= SUSPECT_LIMIT:
                return {"status": "partial", "cleared": cleared,
                        "detail": "目标连续未判定为已清（可能打不过/筛选不符），交还 LLM"}
    if cleared == 0:
        return {"status": "done", "cleared": 0,
                "detail": "没有匹配的待清目标（可能已全部清完；若不符请 observe 核对筛选条件）"}
    for item in spec["finish_chain"]:
        if item == "{pending.path}":
            # 占位：打开一个仍未清的目标（如剩余 boss 的详情弹窗，供后续スキップ）
            pend = _read_targets(device, ts)
            if not pend:
                log("  [sweep] 无剩余目标可打开（可能已全清）")
                continue
            p2 = next(iter(pend))
            if not click_path(device, p2):
                return {"status": "partial", "cleared": cleared,
                        "detail": f"点剩余目标失败: {p2.split('/')[-1]}"}
            time.sleep(1.5)
            continue
        p, why = _wait_click_popup(device, item, spec["popup_timeout"], log, stop_event)
        if p is None:
            if why == "用户停止":
                return {"status": "partial", "cleared": cleared, "detail": why}
            if item.startswith("!"):
                return {"status": "partial", "cleared": cleared, "detail": why}
            log(f"  [sweep] {why}")
            continue
        time.sleep(1.0)
    _drain_popups(device, log=log)
    return {"status": "done", "cleared": cleared,
            "detail": f"编排扫荡完成（{cleared} 个目标）"}


def _drain_popups(device, tries: int = 6, log=print) -> None:
    """收尾后排空残留弹窗（结果页/通知类），让 done 回报时画面干净。"""
    from .macros import settle_step, wait_transition_done
    for _ in range(tries):
        tree = device.ui_tree(max_nodes=30000)
        cancel = popup_cancel_consume(tree)
        act = cancel or settle_step(tree)
        if not act:
            return
        click_path(device, act)
        time.sleep(1.2)
        wait_transition_done(device)


# ---- 编排存盘与注册 --------------------------------------------------------

def save_program(name: str, params: dict) -> "object":
    """把已跑通（done）的编排存盘为命名 routine。名称/参数不合法抛 ValueError。"""
    if not NAME_RE.fullmatch(name or ""):
        raise ValueError("save_as 名称须为小写字母开头、仅含 [a-z0-9_]，3~40 字符")
    spec = validate_params(params or {})
    ROUTINES_DIR.mkdir(parents=True, exist_ok=True)
    f = ROUTINES_DIR / f"{name}.json"
    f.write_text(json.dumps({"routine": "generic_sweep", "name": name, "params": spec},
                            ensure_ascii=False, indent=1), encoding="utf-8")
    return f


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并（override 优先）：按名调用时可只覆盖个别字段
    （如 {"targets": {"max_targets": 3}}），不必重抄整份 targets。"""
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _saved_wrapper(saved: dict):
    def fn(device, params=None, *, log=print, stop_event=None, frame_cb=None, **_):
        merged = _deep_merge(saved["params"], params or {})
        return generic_sweep(device, merged, log=log, stop_event=stop_event,
                             frame_cb=frame_cb)
    fn.__name__ = f"saved:{saved.get('name', '?')}"
    return fn


def load_saved_routines() -> dict:
    """加载 tasks/routines/*.json 为可调用 routine；坏文件跳过不炸注册表。"""
    out = {}
    if not ROUTINES_DIR.exists():
        return out
    for f in sorted(ROUTINES_DIR.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if data.get("routine") == "generic_sweep":
                validate_params(data.get("params") or {})
                out[f.stem] = _saved_wrapper(data)
        except Exception:
            continue
    return out
