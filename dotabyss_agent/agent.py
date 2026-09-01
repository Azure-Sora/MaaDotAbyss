"""Episode 循环：单任务执行 + 完成验证 + 知识卡沉淀。

上下文管理遵循"任务即回合"：每步只携带 任务定义 + 知识卡 + 最近 N 步文本历史 +
当前帧截图，探索过程的图像不进历史（自动裁剪）。
"""
import json
import time
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image

from .brain import Brain, BrainError
from .config import HISTORY_KEEP, KNOWLEDGE_DIR, RUNS_DIR
from .device import GameDevice
from .macros import TransitionTimeout, observe_buttons, wait_transition_done
from .routines import ROUTINES, save_and_register

COORD_LIMIT = (1280, 720)


def forbidden_scene(device) -> str | None:
    """禁区看门狗（桥后端专属）：点击后误入抽卡页时返回场景名，否则 None。

    抽卡/亲密度页面是用户划定的绝对禁区；桥的按钮搜索型点击在无按钮浮层上
    会穿透误触下层入口（2026-08-30 领挂机奖励实测差点进抽卡页），
    故每次 click 后做场景级熔断，宁停勿进。
    """
    if not hasattr(device, "ui_tree"):
        return None
    try:
        scene = str(device.ui_tree(max_nodes=10).get("scene", ""))
    except Exception:
        return None
    return scene if "gacha" in scene.lower() else None


# click_path 路径级黑名单：模型给的路径绝不允许指向禁区入口（防线在场景熔断之前）
PATH_BLACKLIST = ("Gacha", "ガチャ", "親密", "好感", "PullOut", "Retreat")


def knowledge_path(task_id: str) -> Path:
    return KNOWLEDGE_DIR / f"{task_id}.md"


def load_knowledge(task_id: str) -> str:
    p = knowledge_path(task_id)
    return p.read_text(encoding="utf-8").strip() if p.exists() else ""


def save_knowledge(task_id: str, text: str) -> None:
    KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
    knowledge_path(task_id).write_text(text.strip() + "\n", encoding="utf-8")


def _save_frame(frame: np.ndarray, run_dir: Path, name: str) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    p = run_dir / name
    Image.fromarray(frame[:, :, ::-1]).save(p)
    return p


def _emit_thinking(event_cb, phase: str, brain=None) -> None:
    """向 GUI 发思考阶段事件（占位提示用）；失败不影响主流程。"""
    if event_cb is None:
        return
    try:
        ev = {"type": "thinking", "phase": phase}
        if phase == "done" and brain is not None:
            ev["tokens"] = getattr(brain, "last_completion_tokens", None)
        event_cb(ev)
    except Exception:
        pass


def run_task(
    task: dict,
    device: GameDevice,
    brain: Brain,
    max_steps: int = 30,
    time_budget: float = 420.0,
    update_knowledge: bool = True,
    log=print,
    stop_event=None,
    frame_cb=None,
    record: bool = False,
    event_cb=None,
) -> dict:
    """执行单个任务，返回 {"task", "status", "steps", "detail", "run_dir", "record"}。

    status: done / failed / blocked / incomplete / error
    blocked = 疑似 403/网络错误，需要人工接手（上层应停止后续所有任务）。
    stop_event: threading.Event，置位后在下一步边界安全停止。
    frame_cb: callable(frame)，每步截图回调（GUI 预览用）。
    event_cb: callable(dict)，结构化事件回调（GUI 决策流用）：
              每步 {"type":"step", task, step, action, detail, thought, frame}。
    record: 学习录制——保存动作轨迹，以及每次有效点击的前帧与坐标（供剧本生成）。
    """
    tid = task["id"]
    run_dir = RUNS_DIR / datetime.now().strftime("%Y%m%d_%H%M%S") / tid
    knowledge = load_knowledge(tid)
    # 用户补充情报（daily.yaml 的 supplement 字段）：本次执行的最高优先级情报，
    # 成功后由 runner._consume_supplement 合入 prompt 并清空
    supplement = str(task.get("supplement") or "").strip()
    history: list[str] = []
    result = {"task": tid, "status": "error", "steps": 0, "detail": ""}
    t0 = time.time()
    parse_errors = 0

    # 无进展看门狗：点击后画面变化 ≥2% 视为"有进展"（翻页/弹窗/切屏都会重置计数），
    # 只有"无进展的重复点击"才累计报警——结算页连续翻"確認して次へ"不会被误杀
    pending_click: tuple[int, int, np.ndarray] | None = None  # (x, y, 点击前帧)
    prev_click: tuple[int, int] | None = None
    no_prog_streak = 0
    repeat_streak = 0
    record_list: list[dict] = []
    frames_dir = run_dir / "frames"

    for step in range(1, max_steps + 1):
        if stop_event is not None and stop_event.is_set():
            result.update(status="incomplete", detail="用户停止")
            break
        if time.time() - t0 > time_budget:
            result.update(status="incomplete", detail="时间预算耗尽，可重跑继续")
            break

        frame = device.screenshot()
        _save_frame(frame, run_dir, f"step{step:02d}.png")
        if frame_cb is not None:
            try:
                frame_cb(frame)
            except Exception:
                pass

        if pending_click is not None:
            cx, cy, pre_frame = pending_click
            pending_click = None
            diff = device.diff_ratio(pre_frame, frame)
            if record_list and record_list[-1]["step"] == step - 1:
                record_list[-1]["eff"] = round(float(diff), 3)  # 回填上一步点击的效果
            if diff >= 0.02:
                no_prog_streak = 0
                repeat_streak = 0
            else:
                no_prog_streak += 1
                if prev_click and abs(cx - prev_click[0]) < 20 and abs(cy - prev_click[1]) < 20:
                    repeat_streak += 1
                else:
                    repeat_streak = 0
                if no_prog_streak >= 3 or repeat_streak >= 3:
                    result.update(
                        status="incomplete",
                        detail="连续 3 次点击无进展（无反应/重复点同一位置），判定卡住中止",
                    )
                    break

        _emit_thinking(event_cb, "start")
        try:
            action = brain.decide(task["prompt"], knowledge, history, frame,
                                  supplement=supplement)
            parse_errors = 0
        except BrainError as e:
            parse_errors += 1
            history.append(f"step{step}: [解析失败 {e}]")
            log(f"step{step}: [解析失败] {e}")
            if parse_errors >= 3:
                result.update(detail=f"连续 {parse_errors} 次模型输出无法解析")
                break
            continue
        except Exception as e:  # API 网络等错误，稍后重试
            history.append(f"step{step}: [API 异常 {e.__class__.__name__}]")
            log(f"step{step}: [API 异常] {e}")
            time.sleep(5)
            continue
        finally:
            _emit_thinking(event_cb, "done", brain)

        act = action.get("action")
        thought = str(action.get("thought", ""))
        log(f"step{step}: [{act}] {thought}")
        if event_cb is not None:
            try:
                event_cb({
                    "type": "step", "task": tid, "step": step, "action": act,
                    "detail": {k: v for k, v in action.items() if k not in ("thought",)},
                    "thought": thought, "frame": frame,
                })
            except Exception:
                pass

        # click 在确认真实命中后再记录；其余动作先写进完整轨迹。flowgen 仍只消费
        # action=click，而 evolution 会据此拒绝尚不能完整表达 auto/skip 等动作的轨迹。
        if record and act != "click":
            trace_row = {"step": step, "action": str(act or ""), "thought": thought}
            for key in ("routine", "path", "seconds", "timeout", "status", "evidence"):
                if key in action:
                    trace_row[key] = action[key]
            record_list.append(trace_row)

        if act == "click":
            x, y = int(action.get("x", -1)), int(action.get("y", -1))
            if not (0 <= x < COORD_LIMIT[0] and 0 <= y < COORD_LIMIT[1]):
                history.append(f"step{step}: [坐标越界 ({x},{y})] {thought}")
                continue
            if not device.tap(x, y):
                # 真实点击语义：未命中=目标被遮挡或非可点目标，绝不穿透
                # （礼物页一括受け取り→冒险、探索報酬→抽卡入口 两次实测事故）
                history.append(f"step{step}: [点击未命中 ({x},{y})——目标被遮挡或不可点] {thought}")
                log(f"step{step}: [点击未命中 ({x},{y})] 目标被遮挡或非可点目标（真实点击，不穿透）")
                continue
            if record:
                frames_dir.mkdir(parents=True, exist_ok=True)
                pre_path = frames_dir / f"s{step:02d}_pre.png"
                Image.fromarray(frame[:, :, ::-1]).save(pre_path)
                record_list.append({
                    "step": step, "action": "click", "x": x, "y": y,
                    "thought": thought, "pre": str(pre_path.relative_to(run_dir)),
                })
            device.wait_settled(frame)  # 等页面转场平息（慢加载的页面不再重复误点）
            if not wait_transition_done(device):
                # 点击打断 CommonLoad 会 NOW LOAD 卡屏：转场不结束绝不继续点
                log("[红线] 转场动画未结束（疑似卡屏），停止后续点击，请人工检查游戏")
                result.update(status="blocked", steps=step,
                              detail="转场 loading 疑似卡死，已熔断（请人工恢复）")
                break
            bad = forbidden_scene(device)
            if bad:
                log(f"[红线] 点击后误入禁区场景 {bad}——任务熔断，请人工退出该页面")
                result.update(status="blocked", steps=step,
                              detail=f"点击误入禁区场景 {bad}，已熔断（请人工退出）")
                break
            history.append(f"step{step}: 点击({x},{y})｜{thought}")
            pending_click = (x, y, frame)
            prev_click = (x, y)

        elif act == "observe":
            # 按需读 UI 树拿按钮路径（桥后端专属）；过滤参数省略会被截断提醒
            if not hasattr(device, "ui_tree"):
                history.append(f"step{step}: [observe] 当前设备后端不支持 UI 树"
                               f"（仅桥后端可用），请改用截图判断")
                continue
            try:
                scene_name, rows, total = observe_buttons(
                    device,
                    canvas=str(action.get("canvas") or "") or None,
                    suffix=str(action.get("suffix") or ""),
                    contains=str(action.get("contains") or ""),
                    text=str(action.get("text") or ""),
                )
            except Exception as e:
                history.append(f"step{step}: [observe] 读树失败 {e.__class__.__name__}: {e}")
                continue
            lines = [f"step{step}: [observe] scene={scene_name} "
                     f"按钮{total}条" + ("" if total <= len(rows) else f"（已截断至{len(rows)}条）")]
            lines += [f"  {r}" for r in rows]
            if total > len(rows):
                lines.append("  …其余未列出，请加 canvas/suffix/contains/text 过滤再 observe")
            history.append("\n".join(lines))
            log(f"step{step}: [observe] scene={scene_name} 按钮{total}条")

        elif act == "click_path":
            # 按 observe 拿到的完整路径精确点击（onClick.Invoke，不穿透）；
            # 红线与 click 同款：转场等待 + 禁区场景熔断 + 路径级黑名单
            if not hasattr(device, "click_by_path"):
                history.append(f"step{step}: [click_path] 当前设备后端不支持路径点击"
                               f"（仅桥后端可用）")
                continue
            path = str(action.get("path", "")).strip()
            bad = next((k for k in PATH_BLACKLIST if k in path), None)
            if not path or bad:
                history.append(f"step{step}: [click_path] 路径为空或含禁区关键词"
                               f"（{bad or '空'}），拒绝")
                continue
            try:
                device.click_by_path(path)
                hit = True
            except Exception as e:
                hit = False
                err = str(e)
            if not hit:
                history.append(f"step{step}: [click_path 未命中] {path}（{err}）"
                               f"——路径可能已过期，请重新 observe")
                continue
            device.wait_settled(frame)
            if not wait_transition_done(device):
                log("[红线] 转场动画未结束（疑似卡屏），停止后续点击，请人工检查游戏")
                result.update(status="blocked", steps=step,
                              detail="转场 loading 疑似卡死，已熔断（请人工恢复）")
                break
            bad = forbidden_scene(device)
            if bad:
                log(f"[红线] 点击后误入禁区场景 {bad}——任务熔断，请人工退出该页面")
                result.update(status="blocked", steps=step,
                              detail=f"点击误入禁区场景 {bad}，已熔断（请人工退出）")
                break
            history.append(f"step{step}: 点路径 …/{path.split('/')[-1]}｜{thought}")
            pending_click = (0, 0, frame)
            prev_click = (0, 0)

        elif act == "skip":
            # 无按钮翻页/结算提示（確認して次へ 等）：点文字会穿透，统一点左上角
            device.skip_page()
            device.wait_settled(frame)
            if not wait_transition_done(device):
                log("[红线] 转场动画未结束（疑似卡屏），停止后续点击")
                result.update(status="blocked", steps=step,
                              detail="转场 loading 疑似卡死，已熔断（请人工恢复）")
                break
            history.append(f"step{step}: 左上角跳页｜{thought}")
            pending_click = (0, 0, frame)
            prev_click = (0, 0)

        elif act == "wait":
            s = min(float(action.get("seconds", 3)), 10.0)
            time.sleep(s)
            history.append(f"step{step}: 等待{s:.0f}s｜{thought}")

        elif act == "wait_stable":
            timeout = min(float(action.get("timeout", 60)), 150.0)
            ok = device.wait_until_stable(timeout=timeout)
            history.append(f"step{step}: 等待稳定({'达成' if ok else '超时'})｜{thought}")

        elif act == "auto":
            # 程序接管连打（docs/research/14）：LLM 导航到入口页后把重复段交程序；
            # generic_sweep 及已存编排由 LLM 编排 params（sweep_dsl 解释执行）
            rid = str(action.get("routine", ""))
            fn = ROUTINES.get(rid)
            if fn is None:
                history.append(f"step{step}: [auto] 未知 routine {rid}"
                               f"（可用: {', '.join(ROUTINES)}）")
                continue
            log(f"step{step}: [auto {rid}] 程序接管开始")
            auto_t0 = time.time()
            try:
                res = fn(device, action.get("params") or {}, log=log,
                         stop_event=stop_event, frame_cb=frame_cb)
            except TransitionTimeout as e:
                # 这种异常不是“换模型再点几下”能恢复的；继续发输入只会加重
                # CommonLoad 卡死，直接熔断整条日常。
                res = {"status": "blocked", "cleared": 0, "detail": str(e)}
            except Exception as e:  # 其它 routine 异常交 LLM 决断
                res = {"status": "partial", "cleared": 0,
                       "detail": f"{e.__class__.__name__}: {e}"}
            # 编排跑通且要求存盘 → 注册为命名 routine 供下次直呼
            if (res.get("status") == "done" and str(action.get("save_as") or "")):
                try:
                    f = save_and_register(str(action["save_as"]),
                                          action.get("params") or {})
                    log(f"[auto] 编排已存盘注册: {f}")
                    history.append(f"step{step}: [auto] 编排已存盘为 routine "
                                   f"「{action['save_as']}」，下次可直接按名调用")
                except Exception as e:
                    log(f"[auto] 编排存盘失败: {e}")
            # 程序段耗时（战斗连打可达数分钟）不占 LLM 决策预算：整体后移计费起点
            t0 += time.time() - auto_t0
            status = res.get("status")
            hint = {
                "done": "程序已清剿完毕，请继续任务剩余步骤",
                "wrong_scene": "还不在入口页——请先按任务路径导航到入口页后再调 auto",
                "partial": f"程序中途交还控制权，按 detail 判断：接手处理或换目标",
                "blocked": "转场已熔断，禁止继续点击",
            }.get(status, "按 detail 处理")
            history.append(f"step{step}: [auto {rid}] status={status} "
                           f"cleared={res.get('cleared', 0)}｜{res.get('detail', '')}｜{hint}")
            log(f"step{step}: [auto {rid}] {status} cleared={res.get('cleared', 0)} "
                f"{res.get('detail', '')}")
            if status == "blocked":
                result.update(status="blocked", steps=step, detail=res.get("detail", "转场异常"))
                break
            frame2 = device.screenshot()
            _save_frame(frame2, run_dir, f"step{step:02d}_after_auto.png")
            if frame_cb is not None:
                try:
                    frame_cb(frame2)
                except Exception:
                    pass

        elif act == "report":
            status = str(action.get("status", ""))
            if status == "done":
                frame2 = device.screenshot()
                _save_frame(frame2, run_dir, "verify.png")
                evidence = str(action.get("evidence", "")).strip()
                validation = str(task.get("validation", "inline")).strip().lower()
                # decide 本来就是在新鲜画面上做出的结论。默认复用它随 report 提交的
                # 可见证据，避免同一模型紧接着再看同一张图做一次昂贵重复判断。
                # 资源消费/高风险任务可在 daily.yaml 显式设 validation: strict。
                if validation != "strict" and evidence:
                    ok, reason = True, f"本轮画面证据：{evidence}"
                    log(f"step{step}: [verify:inline] {evidence}")
                else:
                    _emit_thinking(event_cb, "start")
                    try:
                        ok, reason = brain.verify(
                            task["prompt"], task.get("exit_condition", ""), frame2
                        )
                    finally:
                        _emit_thinking(event_cb, "done", brain)
                if ok:
                    if update_knowledge:
                        try:
                            newk = brain.summarize_knowledge(
                                task.get("name", tid), knowledge, history
                            )
                            if newk:
                                save_knowledge(tid, newk)
                        except Exception as e:  # 知识卡失败不影响任务成功
                            log(f"[warn] 知识卡更新失败: {e}")
                    result.update(status="done", detail="验证通过", steps=step)
                else:
                    history.append(f"step{step}: 自报 done 但验证未通过：{reason}")
                    continue
            elif status == "blocked":
                result.update(status="blocked", detail=str(action.get("detail", "")), steps=step)
            else:  # failed
                result.update(status="failed", detail=str(action.get("detail", "")), steps=step)
            break

        else:
            history.append(f"step{step}: [未知动作 {act}]")

        history[:] = history[-HISTORY_KEEP:]
        result["steps"] = step

    if result["status"] == "error" and result["steps"] >= max_steps:
        result.update(status="incomplete", detail="步数上限")
    result["run_dir"] = str(run_dir)
    if record:
        (run_dir / "record.json").write_text(
            json.dumps(record_list, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        result["record"] = record_list
    return result
