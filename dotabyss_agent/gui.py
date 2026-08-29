"""正经 GUI：PySide6 + QFluentWidgets（Fluent 风，MFW-CFA 同款路线）。

用法: python -m dotabyss_agent.gui

页面：
- 任务：任务勾选、运行参数、启停控制、运行日志
- 监控：游戏画面实时预览 + LLM 决策流时间线（每步截图/动作/思考）

选型依据见 docs/research/10-UI框架选型调研.md。
"""
import json
import sys
import threading

import numpy as np
from PySide6.QtCore import QObject, Qt, Signal, QTimer
from PySide6.QtGui import QFont, QImage, QPixmap
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QListWidgetItem, QVBoxLayout, QWidget
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


class MainWindow(FluentWindow):
    def __init__(self):
        super().__init__()
        self.state = RunState()

        self.monitor = MonitorPage(self.state)
        self.tasks = TaskPage(self.state, self.monitor)

        self.addSubInterface(self.tasks, FIF.PLAY, "任务")
        self.addSubInterface(self.monitor, FIF.CAMERA, "监控")

        self.resize(1100, 720)
        self.setMinimumSize(960, 640)
        self.setWindowTitle("DotAbyss Agent")

    def closeEvent(self, event):
        # 运行中关窗：请求停止并断开跨线程信号，避免事件投递到已销毁的控件
        self.state.stop_event.set()
        for sig in (self.state.sig.log, self.state.sig.frame, self.state.sig.step,
                    self.state.sig.result, self.state.sig.running):
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
