"""正经 GUI：PySide6 + QFluentWidgets（Fluent 风，MFW-CFA 同款路线）。

用法: python -m dotabyss_agent.gui

页面：
- 任务：任务勾选、运行参数、启停控制、运行日志
- 监控：游戏画面实时预览 + LLM 决策流时间线（每步截图/动作/思考）

选型依据见 docs/research/10-UI框架选型调研.md。
"""
import json
import queue
import sys
import threading

import numpy as np
from PySide6.QtCore import QObject, Qt, Signal, QTimer
from PySide6.QtGui import QFont, QImage, QPixmap
from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidgetItem, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
    ComboBox,
    DoubleSpinBox,
    FluentIcon as FIF,
    FluentWindow,
    InfoBar,
    InfoBarPosition,
    ListWidget,
    PrimaryPushButton,
    PushButton,
    SmoothScrollArea,
    SpinBox,
    StrongBodyLabel,
    TextEdit,
    Theme,
    TitleLabel,
    setTheme,
)

from .config import ACTIVE_PROVIDER, PROVIDERS
from .runner import load_tasks, run_selected

PREVIEW_W, PREVIEW_H = 512, 288   # 1280x720 缩放
THUMB_W, THUMB_H = 160, 90        # 时间线缩略图
MAX_CARDS = 120                   # 决策流保留步数

ACTION_ZH = {"click": "点击 ", "wait": "等待 ", "wait_stable": "等待画面稳定", "report": "上报 → "}


class RunSignals(QObject):
    """worker 线程 → UI 线程 的全部通路。"""
    log = Signal(str)
    frame = Signal(object)          # np.ndarray BGR HxWx3
    step = Signal(dict)             # {"type":"step", task, step, action, detail, thought, frame}
    result = Signal(dict)           # {"type":"result", task, status, ...}
    running = Signal(bool)
    chat = Signal(dict)             # 教学模式 {"type":"chat","role","text"}
    tstate = Signal(str)            # 教学模式状态机 auto/awaiting/distilling/done


class RunState:
    """跨页面共享：信号、停止事件、worker 线程。"""

    def __init__(self):
        self.sig = RunSignals()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None


def frame_to_pixmap(frame) -> QPixmap | None:
    """BGR ndarray → QPixmap（无引用悬挂，Qt 会拷贝像素）。"""
    if not isinstance(frame, np.ndarray):
        return None
    arr = np.ascontiguousarray(frame)
    img = QImage(arr.data, arr.shape[1], arr.shape[0], arr.shape[1] * 3, QImage.Format_BGR888)
    return QPixmap.fromImage(img)


# ---- 决策流时间线 -------------------------------------------------------


class StepCard(CardWidget):
    def __init__(self, ev: dict, parent=None):
        super().__init__(parent)
        self.setFixedHeight(104)

        thumb = QLabel()
        thumb.setFixedSize(THUMB_W + 8, THUMB_H + 8)
        thumb.setAlignment(Qt.AlignCenter)
        thumb.setStyleSheet("background:rgba(128,128,128,0.15); border-radius:4px;")
        pix = frame_to_pixmap(ev.get("frame"))
        if pix is not None:
            thumb.setPixmap(pix.scaled(THUMB_W, THUMB_H, Qt.KeepAspectRatio, Qt.SmoothTransformation))

        act = ev.get("action", "")
        d = ev.get("detail") or {}
        if act == "click":
            desc = f"({d.get('x', '?')}, {d.get('y', '?')})"
        elif act == "wait":
            desc = f"{d.get('seconds', 3):g}s"
        elif act == "report":
            desc = str(d.get("status", "?"))
        else:
            desc = ""
        title = StrongBodyLabel(f"#{ev.get('step', '?')}  {ACTION_ZH.get(act, act or '?')}{desc}")
        thought = BodyLabel(str(ev.get("thought", "")))
        thought.setWordWrap(True)

        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(4)
        right.addWidget(title)
        right.addWidget(thought)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(12)
        lay.addWidget(thumb, 0, Qt.AlignVCenter)
        lay.addLayout(right, 1)


class MonitorPage(QWidget):
    """游戏画面预览 + 决策流时间线。"""

    def __init__(self, state: RunState, parent=None):
        super().__init__(parent)
        self.setObjectName("monitorPage")
        self.state = state
        self._cards: list[StepCard] = []
        self._placeholder: CaptionLabel | None = None

        self.preview = QLabel("等待画面…")
        self.preview.setFixedSize(PREVIEW_W, PREVIEW_H)
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setStyleSheet(
            "background:rgba(128,128,128,0.12); border-radius:6px; font-size:14px;"
        )

        self.title = StrongBodyLabel("决策流")
        self.count = CaptionLabel("")

        self.scroll = SmoothScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setStyleSheet("QScrollArea{border:none; background:transparent;}")
        self.timeline_box = QVBoxLayout()
        self.timeline_box.setSpacing(6)
        self.timeline_box.setAlignment(Qt.AlignTop)
        inner = QWidget()
        inner.setLayout(self.timeline_box)
        inner.setStyleSheet("background:transparent;")
        self.scroll.setWidget(inner)
        self._add_placeholder()

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 12)
        lay.setSpacing(10)
        top = QHBoxLayout()
        top.addWidget(self.preview)
        top.addStretch(1)
        lay.addLayout(top)

        head = QHBoxLayout()
        head.addWidget(self.title)
        head.addWidget(self.count)
        head.addStretch(1)
        lay.addLayout(head)
        lay.addWidget(self.scroll, 1)

        state.sig.frame.connect(self.set_frame)
        state.sig.step.connect(self.add_step)

    # ---- 槽 ----

    def set_frame(self, frame):
        pix = frame_to_pixmap(frame)
        if pix is None:
            return
        self.preview.setPixmap(
            pix.scaled(PREVIEW_W, PREVIEW_H, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        )

    def add_step(self, ev: dict):
        self._clear_placeholder()
        card = StepCard(ev)
        self._cards.append(card)
        self.timeline_box.addWidget(card)
        if len(self._cards) > MAX_CARDS:
            old = self._cards.pop(0)
            self.timeline_box.removeWidget(old)
            old.deleteLater()
        self.count.setText(f"{len(self._cards)} 步")
        QTimer.singleShot(0, self._scroll_bottom)

    def reset(self):
        for c in self._cards:
            self.timeline_box.removeWidget(c)
            c.deleteLater()
        self._cards.clear()
        self.count.setText("")
        self._add_placeholder()

    # ---- 内部 ----

    def _add_placeholder(self):
        self._placeholder = CaptionLabel("运行任务后，每一步的截图、动作与模型思考会按时间排列在这里")
        self._placeholder.setAlignment(Qt.AlignCenter)
        self.timeline_box.addWidget(self._placeholder)

    def _clear_placeholder(self):
        if self._placeholder is not None:
            self.timeline_box.removeWidget(self._placeholder)
            self._placeholder.deleteLater()
            self._placeholder = None

    def _scroll_bottom(self):
        bar = self.scroll.verticalScrollBar()
        bar.setValue(bar.maximum())


# ---- 任务页 --------------------------------------------------------------


class TaskPage(QWidget):
    def __init__(self, state: RunState, monitor: MonitorPage, parent=None):
        super().__init__(parent)
        self.setObjectName("taskPage")
        self.state = state
        self.monitor = monitor
        self._items: dict[str, QListWidgetItem] = {}

        self.status_label = CaptionLabel("待机（游戏需已由人工启动）")

        ctrl = CardWidget()
        c = QHBoxLayout(ctrl)
        c.setContentsMargins(16, 10, 16, 10)
        c.setSpacing(8)

        self.btn_all = PrimaryPushButton(FIF.PLAY, "运行全部")
        self.btn_sel = PushButton(FIF.PLAY, "运行选中")
        self.btn_stop = PushButton(FIF.PAUSE, "停止")
        self.btn_stop.setEnabled(False)

        self.provider = ComboBox()
        self.provider.addItems(list(PROVIDERS))
        self.provider.setCurrentText(ACTIVE_PROVIDER)
        self.max_steps = SpinBox()
        self.max_steps.setRange(1, 200)
        self.max_steps.setValue(30)
        self.budget = DoubleSpinBox()
        self.budget.setRange(30, 7200)
        self.budget.setValue(420)
        self.budget.setDecimals(0)
        self.budget.setSuffix(" s")

        for w in (self.btn_all, self.btn_sel, self.btn_stop):
            c.addWidget(w)
        c.addStretch(1)
        for label, w in (("模型", self.provider), ("步数上限", self.max_steps), ("时间预算", self.budget)):
            c.addWidget(BodyLabel(label))
            c.addWidget(w)

        list_card = CardWidget()
        lv = QVBoxLayout(list_card)
        lv.setContentsMargins(12, 8, 12, 8)
        self.task_list = ListWidget()
        self._fill_tasks()
        lv.addWidget(self.task_list)

        log_card = CardWidget()
        gv = QVBoxLayout(log_card)
        gv.setContentsMargins(12, 8, 12, 8)
        self.log_box = TextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setFont(QFont("Consolas", 9))
        self.log_box.document().setMaximumBlockCount(2000)
        gv.addWidget(self.log_box)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 12)
        lay.setSpacing(10)
        head = QHBoxLayout()
        head.addWidget(TitleLabel("任务"))
        head.addStretch(1)
        head.addWidget(self.status_label)
        lay.addLayout(head)
        lay.addWidget(ctrl)
        lay.addWidget(list_card, 5)
        lay.addWidget(log_card, 4)

        self.btn_all.clicked.connect(self._run_all)
        self.btn_sel.clicked.connect(self._run_checked)
        self.btn_stop.clicked.connect(self._stop)
        state.sig.log.connect(self.log_box.append)
        state.sig.result.connect(self._on_result)
        state.sig.running.connect(self._on_running)

    def _fill_tasks(self):
        for t in load_tasks():
            item = QListWidgetItem(f"{t['id']}   {t.get('name', '')}")
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            item.setData(Qt.UserRole, t["id"])
            self.task_list.addItem(item)
            self._items[t["id"]] = item

    def refresh_tasks(self):
        """教学入库后重载任务列表（保留原勾选状态）。"""
        checked = set(self._checked_ids())
        self.task_list.clear()
        self._items.clear()
        self._fill_tasks()
        for tid in checked:
            if tid in self._items:
                self._items[tid].setCheckState(Qt.Checked)

    # ---- 启停 ----

    def _checked_ids(self) -> list[str]:
        return [it.data(Qt.UserRole) for it in self._items.values() if it.checkState() == Qt.Checked]

    def _run_all(self):
        self._start(list(self._items))

    def _run_checked(self):
        self._start(self._checked_ids())

    def _start(self, ids: list[str]):
        if not ids:
            InfoBar.warning("提示", "没有选择任务（勾选列表项或直接运行全部）",
                            parent=self, position=InfoBarPosition.TOP, duration=2500)
            return
        if self.state.worker and self.state.worker.is_alive():
            return
        self.state.stop_event.clear()
        for tid in ids:
            self._set_status(tid, "排队")
        self.monitor.reset()
        self.status_label.setText(f"运行中：{', '.join(ids)}")
        args = (ids, self.max_steps.value(), self.budget.value(), self.provider.currentText())
        self.state.worker = threading.Thread(target=self._work, args=args, daemon=True)
        self.state.worker.start()
        self.state.sig.running.emit(True)

    def _stop(self):
        self.state.stop_event.set()
        self.status_label.setText("停止中（等待当前步结束）…")

    def _work(self, ids, max_steps, budget, provider):
        s = self.state.sig

        def on_event(ev: dict):
            if ev.get("type") == "result":
                s.result.emit(ev)
            else:
                s.step.emit(ev)

        try:
            results = run_selected(
                ids,
                max_steps=max_steps,
                time_budget=budget,
                provider=provider,
                log=s.log.emit,
                stop_event=self.state.stop_event,
                frame_cb=s.frame.emit,
                event_cb=on_event,
            )
            s.log.emit("===== 汇总 =====")
            s.log.emit(json.dumps(results, ensure_ascii=False, indent=1))
        except Exception as e:
            s.log.emit(f"[异常] {e.__class__.__name__}: {e}")
        finally:
            s.running.emit(False)

    # ---- 状态 ----

    def _on_result(self, r: dict):
        self._set_status(r.get("task", ""), r.get("status", "?"))
        if r.get("status") == "blocked":
            InfoBar.error("已阻塞", "疑似 403/网络错误，请人工检查游戏后再继续",
                          parent=self, position=InfoBarPosition.TOP, duration=-1)

    def _on_running(self, running: bool):
        self.btn_all.setEnabled(not running)
        self.btn_sel.setEnabled(not running)
        self.btn_stop.setEnabled(running)
        if not running:
            self.status_label.setText("待机")

    def _set_status(self, tid: str, status: str):
        item = self._items.get(tid)
        if item is None:
            return
        base = item.text().split("【")[0]
        item.setText(f"{base}【{status}】")


# ---- 教学页（新建任务，docs/research/11） --------------------------------

STATE_ZH = {"auto": "探索中…", "awaiting": "⬇ 等待你的指示", "distilling": "蒸馏中…", "done": "已结束"}


class Bubble(QLabel):
    """聊天气泡：agent=蓝(左)、user=绿(右)、system=黄(中)、step=灰字。"""

    def __init__(self, role: str, text: str, parent=None):
        super().__init__(text, parent)
        self.role = role
        self.setWordWrap(True)
        self.setTextInteractionFlags(Qt.TextSelectableByMouse)
        if role == "user":
            self.setMaximumWidth(560)
            self.setAlignment(Qt.AlignRight)
            self.setStyleSheet(
                "background:rgba(0,180,120,0.22); border-radius:8px; padding:8px 12px;")
        elif role == "agent":
            self.setMaximumWidth(560)
            self.setStyleSheet(
                "background:rgba(48,110,235,0.28); border-radius:8px; padding:8px 12px;")
        elif role == "step":
            self.setStyleSheet("color:rgba(210,210,210,0.55); font-size:12px;")
        else:
            self.setAlignment(Qt.AlignCenter)
            self.setStyleSheet("color:rgba(255,200,90,0.9); font-size:13px;")


class TeachPage(QWidget):
    """会话式新建任务：上=游戏画面镜像，下=与模型的对话流。

    会话在 worker 线程跑 run_teach_session；用户输入经 reply 队列喂回。
    """

    MAX_BUBBLES = 200

    def __init__(self, state: RunState, tasks_page: TaskPage, parent=None):
        super().__init__(parent)
        self.setObjectName("teachPage")
        self.state = state
        self.tasks_page = tasks_page
        self.reply_q: queue.Queue = queue.Queue()
        self._bubbles: list[Bubble] = []

        self.status_label = CaptionLabel("待机")

        # ---- 开局配置 ----
        setup = CardWidget()
        s = QVBoxLayout(setup)
        s.setContentsMargins(16, 10, 16, 10)
        s.setSpacing(6)
        row1 = QHBoxLayout()
        self.in_id = QLineEdit()
        self.in_id.setPlaceholderText("任务ID（小写字母/数字/下划线，如 abyss_sweep）")
        self.in_name = QLineEdit()
        self.in_name.setPlaceholderText("任务名（中文）")
        self.provider = ComboBox()
        self.provider.addItems(list(PROVIDERS))
        self.provider.setCurrentText(ACTIVE_PROVIDER)
        self.btn_start = PrimaryPushButton(FIF.PLAY, "开始教学")
        row1.addWidget(self.in_id, 3)
        row1.addWidget(self.in_name, 2)
        row1.addWidget(BodyLabel("模型"))
        row1.addWidget(self.provider)
        row1.addWidget(self.btn_start)
        row2 = QHBoxLayout()
        self.in_goal = QLineEdit()
        self.in_goal.setPlaceholderText(
            "目标：一句话告诉模型要做什么；入口路径不确定也没关系，它会问")
        row2.addWidget(BodyLabel("目标"))
        row2.addWidget(self.in_goal, 1)
        s.addLayout(row1)
        s.addLayout(row2)

        # ---- 画面 + 会话 ----
        self.preview = QLabel("教学开始后显示画面…")
        self.preview.setFixedSize(PREVIEW_W, PREVIEW_H)
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setStyleSheet(
            "background:rgba(128,128,128,0.12); border-radius:6px; font-size:14px;")

        self.chat_scroll = SmoothScrollArea()
        self.chat_scroll.setWidgetResizable(True)
        self.chat_scroll.setStyleSheet("QScrollArea{border:none; background:transparent;}")
        self.chat_box = QVBoxLayout()
        self.chat_box.setSpacing(6)
        self.chat_box.setAlignment(Qt.AlignTop)
        inner = QWidget()
        inner.setLayout(self.chat_box)
        inner.setStyleSheet("background:transparent;")
        self.chat_scroll.setWidget(inner)

        # ---- 输入行 ----
        bottom = QHBoxLayout()
        self.in_msg = QLineEdit()
        self.in_msg.setPlaceholderText("输入指示…（模型提问时回答它；也可随时主动插入指示）")
        self.btn_send = PushButton(FIF.SEND, "发送")
        self.btn_finish = PrimaryPushButton(FIF.ACCEPT, "完成教学")
        self.btn_abort = PushButton(FIF.CLOSE, "中止")
        bottom.addWidget(self.in_msg, 1)
        bottom.addWidget(self.btn_send)
        bottom.addWidget(self.btn_finish)
        bottom.addWidget(self.btn_abort)

        # ---- 蒸馏结果 ----
        self.result_box = TextEdit()
        self.result_box.setReadOnly(True)
        self.result_box.setVisible(False)
        self.result_box.setFixedHeight(150)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 12)
        lay.setSpacing(10)
        head = QHBoxLayout()
        head.addWidget(TitleLabel("新建任务"))
        head.addStretch(1)
        head.addWidget(self.status_label)
        lay.addLayout(head)
        lay.addWidget(setup)
        lay.addWidget(self.preview)
        lay.addWidget(self.chat_scroll, 1)
        lay.addLayout(bottom)
        lay.addWidget(self.result_box)

        self.btn_start.clicked.connect(self._start)
        self.btn_send.clicked.connect(self._send)
        self.in_msg.returnPressed.connect(self._send)
        self.btn_finish.clicked.connect(self._finish)
        self.btn_abort.clicked.connect(self._abort)
        state.sig.frame.connect(self.set_frame)
        state.sig.chat.connect(self._on_chat)
        state.sig.tstate.connect(self._on_state)
        state.sig.step.connect(self._on_step)
        state.sig.result.connect(self._on_result)
        self._set_running(False)

    # ---- 槽 ----

    def set_frame(self, frame):
        pix = frame_to_pixmap(frame)
        if pix is None:
            return
        self.preview.setPixmap(
            pix.scaled(PREVIEW_W, PREVIEW_H, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        )

    def _on_chat(self, ev: dict):
        self._add_bubble(ev.get("role", "system"), str(ev.get("text", "")))

    def _on_step(self, ev: dict):
        act = ev.get("action", "")
        d = ev.get("detail") or {}
        if act == "click":
            text = f"「点击 ({d.get('x', '?')}, {d.get('y', '?')})」{ev.get('thought', '')}"
        elif act == "wait":
            text = f"「等待 {d.get('seconds', 3):g}s」{ev.get('thought', '')}"
        else:
            text = f"「{act}」{ev.get('thought', '')}"
        self._add_bubble("step", text)

    def _on_state(self, state: str):
        self.status_label.setText(STATE_ZH.get(state, state))
        if state == "awaiting":
            self.in_msg.setFocus()

    def _on_result(self, r: dict):
        # 教学会话的 result 带 task_card（日常任务的没有），据此区分
        if "task_card" not in r:
            return
        if r.get("status") == "distilled":
            card = r["task_card"]
            lines = [f"任务「{card.get('name')}」已入库（shadow，下次执行自动验证）", "",
                     f"ID：{card.get('id')}",
                     f"完成判据：{card.get('exit_condition', '')}", "",
                     "任务指令：", str(card.get("prompt", ""))]
            notes = card.get("notes") or []
            if notes:
                lines += ["", "注意事项："] + [f"- {n}" for n in notes]
            self.result_box.setPlainText("\n".join(lines))
            self.result_box.setVisible(True)
            self.tasks_page.refresh_tasks()
            InfoBar.success("新建任务完成", f"{card.get('name')} 已写入任务清单",
                            parent=self, position=InfoBarPosition.TOP, duration=4000)
        else:
            InfoBar.warning("教学会话结束", str(r.get("detail", "未完成")),
                            parent=self, position=InfoBarPosition.TOP, duration=4000)

    # ---- 会话控制 ----

    def _start(self):
        tid = self.in_id.text().strip()
        name = self.in_name.text().strip() or tid
        goal = self.in_goal.text().strip()
        if not tid or tid != tid.lower() or not tid.replace("_", "").isalnum():
            InfoBar.warning("提示", "任务ID 只能是小写字母/数字/下划线",
                            parent=self, position=InfoBarPosition.TOP, duration=2500)
            return
        if not goal:
            InfoBar.warning("提示", "请先写一句话目标——模型据此开始探索",
                            parent=self, position=InfoBarPosition.TOP, duration=2500)
            return
        if self.state.worker and self.state.worker.is_alive():
            InfoBar.warning("提示", "有任务或教学正在进行", parent=self,
                            position=InfoBarPosition.TOP, duration=2500)
            return
        self.state.stop_event.clear()
        self.reply_q = queue.Queue()
        for w in (self.btn_start, self.in_id, self.in_name, self.in_goal, self.provider):
            w.setEnabled(False)
        self.result_box.setVisible(False)
        self._clear_chat()
        self._add_bubble("system", "会话建立——模型从当前画面开始探索；拿不准会停下来问你。")
        self.state.worker = threading.Thread(
            target=self._work, args=(tid, name, goal, self.provider.currentText()), daemon=True)
        self.state.worker.start()
        self.state.sig.running.emit(True)
        self._set_running(True)
        self.status_label.setText(STATE_ZH["auto"])

    def _work(self, tid, name, goal, provider):
        s = self.state.sig
        from .brain import Brain
        from .device import GameDevice
        from .teach import run_teach_session
        try:
            device = GameDevice()
            brain = Brain(provider=provider)
            device.bring_to_front()
            run_teach_session(
                tid, name, goal, device, brain,
                log=s.log.emit,
                stop_event=self.state.stop_event,
                frame_cb=s.frame.emit,
                event_cb=lambda ev: (
                    s.chat.emit(ev) if ev.get("type") == "chat"
                    else s.tstate.emit(ev.get("state", "")) if ev.get("type") == "state"
                    else s.result.emit(ev) if ev.get("type") == "result"
                    else s.step.emit(ev)
                ),
                reply_get=self.reply_q.get,
            )
        except Exception as e:
            s.log.emit(f"[教学异常] {e.__class__.__name__}: {e}")
            s.chat.emit({"type": "chat", "role": "system", "text": f"会话异常结束：{e}"})
        finally:
            s.running.emit(False)
            s.tstate.emit("done")

    def _send(self):
        text = self.in_msg.text().strip()
        if not text:
            return
        self.reply_q.put({"kind": "msg", "text": text})
        self.in_msg.clear()

    def _finish(self):
        self.reply_q.put({"kind": "finish"})

    def _abort(self):
        self.reply_q.put({"kind": "abort"})

    def _set_running(self, running: bool):
        for w in (self.btn_send, self.btn_finish, self.btn_abort, self.in_msg):
            w.setEnabled(running)
        for w in (self.btn_start, self.in_id, self.in_name, self.in_goal, self.provider):
            w.setEnabled(not running)

    # ---- 气泡 ----

    def _add_bubble(self, role: str, text: str):
        if not text:
            return
        b = Bubble(role, text)
        self._bubbles.append(b)
        if role == "user":
            row = QHBoxLayout()
            row.addStretch(1)
            row.addWidget(b)
            holder = QWidget()
            holder.setLayout(row)
            holder.setStyleSheet("background:transparent;")
            self.chat_box.addWidget(holder)
            b._holder = holder
        else:
            self.chat_box.addWidget(b)
        if len(self._bubbles) > self.MAX_BUBBLES:
            old = self._bubbles.pop(0)
            (old._holder if hasattr(old, "_holder") else old).deleteLater()
        QTimer.singleShot(0, self._scroll_bottom)

    def _clear_chat(self):
        for b in self._bubbles:
            (b._holder if hasattr(b, "_holder") else b).deleteLater()
        self._bubbles.clear()

    def _scroll_bottom(self):
        bar = self.chat_scroll.verticalScrollBar()
        bar.setValue(bar.maximum())


class MainWindow(FluentWindow):
    def __init__(self):
        super().__init__()
        self.state = RunState()

        self.monitor = MonitorPage(self.state)
        self.tasks = TaskPage(self.state, self.monitor)
        self.teach = TeachPage(self.state, self.tasks)

        self.addSubInterface(self.tasks, FIF.PLAY, "任务")
        self.addSubInterface(self.monitor, FIF.CAMERA, "监控")
        self.addSubInterface(self.teach, FIF.ADD, "新建任务")

        self.resize(1100, 720)
        self.setMinimumSize(960, 640)
        self.setWindowTitle("DotAbyss Agent")

    def closeEvent(self, event):
        # 运行中关窗：请求停止并断开跨线程信号，避免事件投递到已销毁的控件
        self.state.stop_event.set()
        for sig in (self.state.sig.log, self.state.sig.frame, self.state.sig.step,
                    self.state.sig.result, self.state.sig.running,
                    self.state.sig.chat, self.state.sig.tstate):
            try:
                sig.disconnect()
            except RuntimeError:
                pass
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    setTheme(Theme.DARK)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
