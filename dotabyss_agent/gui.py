"""正经 GUI：PySide6 + QFluentWidgets（Fluent 风，MFW-CFA 同款路线）。

用法: python -m dotabyss_agent.gui
      启动后内嵌控制接口（发现文件 .local/ctl.json），CLI 可用 `ctl` 子命令
      附着操作同一引擎，见 docs/research/13-控制面与BepInEx桥.md

页面：
- 任务：任务勾选、运行参数、启停控制、运行日志
- 深渊：自动刷深渊（入场→监督推进→结算），账本实时上屏
- 监控：游戏画面实时预览 + LLM 决策流时间线（每步截图/动作/思考）

选型依据见 docs/research/10-UI框架选型调研.md。
"""
import json
import os
import queue
import sys
import threading
import time
from pathlib import Path
from collections import deque

import numpy as np
from PySide6.QtCore import QFile, QObject, QRectF, QSize, Qt, Signal, QTimer
from PySide6.QtGui import QFont, QFontMetrics, QIcon, QImage, QPainter, QPixmap, QTextCursor
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel, QLineEdit,
                               QListWidgetItem, QToolButton, QVBoxLayout, QWidget)
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
    LineEdit,
    ListWidget,
    MessageBox,
    MessageBoxBase,
    PasswordLineEdit,
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
from qfluentwidgets.components.widgets.spin_box import SpinButton, SpinIcon
from qfluentwidgets.common.style_sheet import isDarkTheme

from .config import LOCAL_DIR, ROOT
from .modelstore import discover_models, store
from .control import CtlError, ControlServer, SHOTS_DIR, save_frame
from .device import DeviceError
from .runner import load_tasks, run_selected
from .taskfile import TaskFileError, delete_task, reorder_tasks, update_task
from .usage_page import UsagePage

PREVIEW_W, PREVIEW_H = 512, 288   # 1280x720 缩放
THUMB_W, THUMB_H = 160, 90        # 时间线缩略图
MAX_CARDS = 120                   # 决策流保留步数
ABYSS_PARAMS_PATH = LOCAL_DIR / "abyss_params.json"   # 深渊页参数记忆

ACTION_ZH = {"click": "点击 ", "wait": "等待 ", "wait_stable": "等待画面稳定",
             "report": "上报 → ", "skip": "左上角跳页", "auto": "程序接管 → "}


class RunSignals(QObject):
    """worker 线程 → UI 线程 的全部通路。"""
    log = Signal(str)
    frame = Signal(object)          # np.ndarray BGR HxWx3
    step = Signal(dict)             # {"type":"step", task, step, action, detail, thought, frame}
    result = Signal(dict)           # {"type":"result", task, status, ...}
    running = Signal(bool)
    chat = Signal(dict)             # 教学模式 {"type":"chat","role","text"}
    tstate = Signal(str)            # 教学模式状态机 auto/awaiting/distilling/done
    think = Signal(dict)            # 教学模式 {"phase":"start"/"done","tokens":int|None}
    ctl_call = Signal(object)       # 控制面：HTTP 线程投递到 GUI 线程执行的闭包
    ledger = Signal(dict)           # 深渊账本快照 {floor, erosion, keys, coins, 四色码}
    providers_changed = Signal()    # 模型页增删改后，三页模型下拉联动刷新


class RunState:
    """跨页面共享：信号、停止事件、worker 线程、共享设备。"""

    def __init__(self):
        self.sig = RunSignals()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.mode: str | None = None            # task / teach（ctl status 用）
        self.current_ids: list[str] = []
        self.backend: str | None = None         # bridge / maa（ctl status 用）
        self._device = None                     # 进程内唯一 MAA 控制器（懒创建）

    def get_device(self):
        if self._device is None:
            from .device_select import get_device

            self._device, self.backend = get_device()
        return self._device

    def has_device(self) -> bool:
        return self._device is not None

    def drop_device(self) -> None:
        # 仅在引擎空闲时调用：运行中重建会产生并发双控制器（M0 实测 segfault）
        self._device = None
        self.backend = None

    def clear_run(self) -> None:
        self.mode = None
        self.current_ids = []


def frame_to_pixmap(frame) -> QPixmap | None:
    """BGR ndarray → QPixmap（无引用悬挂，Qt 会拷贝像素）。"""
    if not isinstance(frame, np.ndarray):
        return None
    arr = np.ascontiguousarray(frame)
    img = QImage(arr.data, arr.shape[1], arr.shape[0], arr.shape[1] * 3, QImage.Format_BGR888)
    return QPixmap.fromImage(img)


# ---- 竖排步进按钮的 SpinBox ----------------------------------------------
# 库里的 InlineSpinBoxBase 把两个 31×23 的箭头按钮横排在框右侧（合计约 70px），
# 深渊页参数框只有 110px 宽，数字区被挤没。这里换成竖排小按钮（24×16×2）。


class _VSpinButton(SpinButton):
    """缩小的步进按钮；父类把图标坐标写死，缩小后需自行居中绘制。"""

    def __init__(self, icon: SpinIcon, parent=None):
        super().__init__(icon, parent)
        self.setFixedSize(24, 16)

    def paintEvent(self, e):
        QToolButton.paintEvent(self, e)
        painter = QPainter(self)
        painter.setRenderHints(QPainter.Antialiasing |
                               QPainter.SmoothPixmapTransform)
        if not self.isEnabled():
            painter.setOpacity(0.36)
        elif self.isPressed:
            painter.setOpacity(0.7)
        self._icon.render(painter, QRectF((self.width() - 10) / 2,
                                          (self.height() - 10) / 2, 10, 10))


def _apply_spin_qss(box):
    """库的 spin_box.qss 为默认横排按钮预留了 80px 右内边距（按钮列本身只有 28px），
    窄框下输入区被压得只剩 ~20px、数字被裁——换成贴合竖排按钮列的 padding。
    库 qss 同时承担背景/边框/hover 样式，故整体取回改写而非简单覆盖。"""
    theme = "dark" if isDarkTheme() else "light"
    f = QFile(f":/qfluentwidgets/qss/{theme}/spin_box.qss")
    text = f.open(QFile.ReadOnly) and bytes(f.readAll()).decode()
    if "padding: 0px 80px 0 10px" in text:
        text = text.replace("padding: 0px 80px 0 10px", "padding: 0px 34px 0 10px")
    else:    # 库改版兜底：同优先级覆盖规则，后来者胜
        text += "\nQSpinBox, QDoubleSpinBox { padding: 0px 34px 0 10px; }"
    box.setStyleSheet(text)


def _stack_spin_buttons(box):
    """把 InlineSpinBoxBase 横排的上下按钮替换为右侧竖排一列。"""
    for b in (box.upButton, box.downButton):
        box.hBoxLayout.removeWidget(b)
        b.deleteLater()
    box.upButton = _VSpinButton(SpinIcon.UP, box)
    box.downButton = _VSpinButton(SpinIcon.DOWN, box)
    box.upButton.clicked.connect(box.stepUp)
    box.downButton.clicked.connect(box.stepDown)
    box.hBoxLayout.setContentsMargins(0, 0, 4, 0)
    col = QVBoxLayout()
    col.setContentsMargins(0, 0, 0, 0)
    col.setSpacing(0)
    col.addWidget(box.upButton)
    col.addWidget(box.downButton)
    box.hBoxLayout.addLayout(col)
    box.hBoxLayout.setAlignment(col, Qt.AlignRight | Qt.AlignVCenter)
    _apply_spin_qss(box)


class _FitMinMixin:
    """最小宽度按「最大值文本完整可见」计算。

    库的 minimumSizeHint 跟着 qss 的大 padding 走（虚高 ~40px/框），七个框排在
    默认 1100px 窗宽下必然溢出；这里给出真实下限，布局压缩时数字不裁字。
    """

    def minimumSizeHint(self):
        fm = QFontMetrics(self.font())
        text = self.textFromValue(self.maximum()) + self.suffix()
        return QSize(fm.horizontalAdvance(text) + 46, 33)


class VSpinBox(_FitMinMixin, SpinBox):
    """上下箭头竖排成列的 SpinBox，窄框也能显示数字。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        _stack_spin_buttons(self)


class VDoubleSpinBox(_FitMinMixin, DoubleSpinBox):
    """同 VSpinBox，浮点版。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        _stack_spin_buttons(self)


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
        elif act == "auto":
            desc = str(d.get("routine", ""))
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


class TaskEditDialog(MessageBoxBase):
    """任务编辑卡：任务名 / 任务指令 / 完成判据 / 补充情报。

    补充情报是给模型的临时情报（如版本更新后的新机制）：本次执行按它为准，
    任务成功后由模型把合入任务指令并自动清空本字段。
    """

    def __init__(self, task: dict, parent=None):
        super().__init__(parent)
        self.task = task
        # 卡片定宽 720：宽度只能设在 self.widget（居中卡片）上——
        # 设在 self 会把全屏遮罩层一起钉死，不再随主窗口缩放（基类
        # eventFilter 靠监听主窗口 Resize 同步自身尺寸）
        self.widget.setFixedWidth(720)

        self.titleLabel = StrongBodyLabel(f"编辑任务：{task['id']}")
        self.in_name = LineEdit()
        self.in_name.setText(task.get("name", ""))
        self.in_name.setClearButtonEnabled(True)
        self.in_prompt = TextEdit()
        self.in_prompt.setPlainText(str(task.get("prompt", "")))
        self.in_prompt.setFixedHeight(190)
        self.in_exit = TextEdit()
        self.in_exit.setPlainText(str(task.get("exit_condition", "")))
        self.in_exit.setFixedHeight(64)
        self.in_sup = TextEdit()
        self.in_sup.setPlainText(str(task.get("supplement", "")))
        self.in_sup.setPlaceholderText(
            "补充情报（可留空）：给模型的最新情报，如「今天更新后打完一个 boss 可直接跳过其余两个并领奖励」。"
            "本次执行以它为准；成功一次后模型会把它合入任务指令并自动清空。")
        self.in_sup.setFixedHeight(64)

        self.viewLayout.addWidget(self.titleLabel)
        for label, w in (("任务名", self.in_name), ("任务指令", self.in_prompt),
                         ("完成判据", self.in_exit), ("补充情报", self.in_sup)):
            self.viewLayout.addWidget(CaptionLabel(label))
            self.viewLayout.addWidget(w)
        self.yesButton.setText("保存")
        self.cancelButton.setText("取消")

    def validate(self) -> bool:
        if not self.in_name.text().strip():
            InfoBar.warning("提示", "任务名不能为空", parent=self,
                            position=InfoBarPosition.TOP, duration=2500)
            return False
        if not self.in_prompt.toPlainText().strip():
            InfoBar.warning("提示", "任务指令不能为空", parent=self,
                            position=InfoBarPosition.TOP, duration=2500)
            return False
        return True

    def result_fields(self) -> dict:
        return {
            "name": self.in_name.text().strip(),
            "prompt": self.in_prompt.toPlainText().strip(),
            "exit_condition": self.in_exit.toPlainText().strip(),
            "supplement": self.in_sup.toPlainText().strip(),
        }


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
        self.refresh_providers()
        self.max_steps = VSpinBox()
        self.max_steps.setRange(1, 200)
        self.max_steps.setValue(30)
        self.budget = VDoubleSpinBox()
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
        self._edit_btns = []
        ops = QHBoxLayout()
        ops.setSpacing(6)
        for icon, label, slot in (
                (FIF.UP, "上移", self._move_up), (FIF.DOWN, "下移", self._move_down),
                (FIF.EDIT, "编辑", self._edit_task), (FIF.DELETE, "删除", self._delete_task)):
            b = PushButton(icon, label)
            b.clicked.connect(slot)
            self._edit_btns.append(b)
            ops.addWidget(b)
        ops.addStretch(1)
        ops.addWidget(CaptionLabel("改动直接写回 tasks/daily.yaml（保留注释与排版）"))
        lv.addLayout(ops)

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
        state.sig.think.connect(self._on_thinking)
        self._think_block = None    # 日志末尾的思考占位行（QTextBlock 句柄，原位刷新）
        self._think_t0 = 0.0
        self._think_timer = QTimer(self)
        self._think_timer.timeout.connect(self._tick_thinking)

    def refresh_providers(self):
        """模型页变更后重建下拉项；尽量保持当前选择。"""
        cur = self.provider.currentText()
        names = store.names()
        self.provider.clear()
        self.provider.addItems(names)
        self.provider.setCurrentText(cur if cur in names else store.active())

    def _fill_tasks(self):
        for t in load_tasks():
            mark = "  💡补充" if str(t.get("supplement") or "").strip() else ""
            item = QListWidgetItem(f"{t['id']}   {t.get('name', '')}{mark}")
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            item.setData(Qt.UserRole, t["id"])
            self.task_list.addItem(item)
            self._items[t["id"]] = item

    def refresh_tasks(self):
        """教学入库/增删改排后重载任务列表（保留勾选与当前选中行）。"""
        checked = set(self._checked_ids())
        cur_id = self.task_list.currentItem().data(Qt.UserRole) \
            if self.task_list.currentItem() else None
        self.task_list.clear()
        self._items.clear()
        self._fill_tasks()
        for tid in checked:
            if tid in self._items:
                self._items[tid].setCheckState(Qt.Checked)
        if cur_id in self._items:
            self.task_list.setCurrentItem(self._items[cur_id])

    # ---- 任务管理（排序/编辑/删除，直接写回 daily.yaml） ----

    def _current_id(self) -> str | None:
        it = self.task_list.currentItem()
        return it.data(Qt.UserRole) if it else None

    def _reload_yaml(self):
        """改动落盘后刷新列表；失败提示（文件未被写坏，taskfile 原子写保证）。"""
        try:
            self.refresh_tasks()
            return True
        except Exception as e:
            InfoBar.error("写入失败", f"{e.__class__.__name__}: {e}",
                          parent=self, position=InfoBarPosition.TOP, duration=-1)
            return False

    def _move_task(self, delta: int):
        tid = self._current_id()
        if tid is None:
            return
        order = [it.data(Qt.UserRole) for it in self._items.values()]
        i = order.index(tid)
        j = i + delta
        if not 0 <= j < len(order):
            return
        order[i], order[j] = order[j], order[i]
        try:
            reorder_tasks(order)
        except TaskFileError as e:
            InfoBar.error("写入失败", str(e), parent=self,
                          position=InfoBarPosition.TOP, duration=-1)
            return
        self._reload_yaml()

    def _move_up(self):
        self._move_task(-1)

    def _move_down(self):
        self._move_task(1)

    def _edit_task(self):
        tid = self._current_id()
        if tid is None:
            return
        task = next((t for t in load_tasks() if t["id"] == tid), None)
        if task is None:
            self._reload_yaml()
            return
        dlg = TaskEditDialog(task, self.window())
        if dlg.exec():
            try:
                update_task(tid, **dlg.result_fields())
            except TaskFileError as e:
                InfoBar.error("写入失败", str(e), parent=self,
                              position=InfoBarPosition.TOP, duration=-1)
                return
            self._reload_yaml()
            InfoBar.success("已保存", f"{tid} 已写回 daily.yaml",
                            parent=self, position=InfoBarPosition.TOP, duration=3000)

    def _delete_task(self):
        tid = self._current_id()
        if tid is None:
            return
        box = MessageBox("删除任务", f"确定从任务清单删除「{tid}」？\n"
                                    f"（剧本与知识卡文件保留，仅摘出清单）",
                         self.window())
        if not box.exec():
            return
        try:
            delete_task(tid)
        except TaskFileError as e:
            InfoBar.error("写入失败", str(e), parent=self,
                          position=InfoBarPosition.TOP, duration=-1)
            return
        self._reload_yaml()
        InfoBar.success("已删除", f"{tid} 已移出任务清单",
                        parent=self, position=InfoBarPosition.TOP, duration=3000)

    # ---- 启停 ----

    def _checked_ids(self) -> list[str]:
        return [it.data(Qt.UserRole) for it in self._items.values() if it.checkState() == Qt.Checked]

    def _run_all(self):
        self.start(list(self._items), self.max_steps.value(), self.budget.value(),
                   self.provider.currentText())

    def _run_checked(self):
        self.start(self._checked_ids(), self.max_steps.value(), self.budget.value(),
                   self.provider.currentText())

    def start(self, ids: list[str], max_steps: int, budget: float, provider: str,
              update_knowledge: bool = True) -> bool:
        """启动任务（GUI 按钮与 ctl run 共用路径）；忙/空选时提示并返回 False。"""
        if not ids:
            InfoBar.warning("提示", "没有选择任务（勾选列表项或直接运行全部）",
                            parent=self, position=InfoBarPosition.TOP, duration=2500)
            return False
        if self.state.worker and self.state.worker.is_alive():
            return False
        self.state.stop_event.clear()
        self.state.mode = "task"
        self.state.current_ids = list(ids)
        for tid in ids:
            self._set_status(tid, "排队")
        self.monitor.reset()
        self.status_label.setText(f"运行中：{', '.join(ids)}")
        args = (ids, max_steps, budget, provider, update_knowledge)
        self.state.worker = threading.Thread(target=self._work, args=args, daemon=True)
        self.state.worker.start()
        self.state.sig.running.emit(True)
        return True

    def _stop(self):
        self.state.stop_event.set()
        self.status_label.setText("停止中（等待当前步结束）…")

    def _work(self, ids, max_steps, budget, provider, update_knowledge):
        s = self.state.sig

        def on_event(ev: dict):
            t = ev.get("type")
            if t == "result":
                s.result.emit(ev)
            elif t == "thinking":
                s.think.emit(ev)
            else:
                s.step.emit(ev)

        try:
            device = self.state.get_device()
            results = run_selected(
                ids,
                max_steps=max_steps,
                time_budget=budget,
                provider=provider,
                update_knowledge=update_knowledge,
                log=s.log.emit,
                stop_event=self.state.stop_event,
                frame_cb=s.frame.emit,
                event_cb=on_event,
                _device=device,
            )
            s.log.emit("===== 汇总 =====")
            s.log.emit(json.dumps(results, ensure_ascii=False, indent=1))
        except Exception as e:
            s.log.emit(f"[异常] {e.__class__.__name__}: {e}")
        finally:
            self.state.clear_run()
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
        for b in self._edit_btns:           # 运行中锁定清单编辑（防执行中途改文件竞态）
            b.setEnabled(not running)
        if not running:
            if self._think_timer.isActive():    # 中途停止：占位行落定，不留悬空"思考中"
                self._think_timer.stop()
                self._write_think_line("🤔 思考已中断")
                self._think_block = None
            self.status_label.setText("待机")

    def _set_status(self, tid: str, status: str):
        item = self._items.get(tid)
        if item is None:
            return
        base = item.text().split("【")[0]
        item.setText(f"{base}【{status}】")

    # ---- 思考占位行（与教学页气泡同款交互：日志末行原位刷新） ----

    def _on_thinking(self, ev: dict):
        if self.state.mode != "task":
            return
        if ev.get("phase") == "start":
            self._think_block = None
            self.log_box.append("🤔 思考中… 0.0s")
            self._think_block = self.log_box.document().lastBlock()
            self._think_t0 = time.monotonic()
            self._think_timer.start(100)
        else:
            self._think_timer.stop()
            tokens = ev.get("tokens")
            quant = f"{tokens} tokens" if tokens else "（无用量数据）"
            self._write_think_line(
                f"🤔 思考完成 · {quant} · {time.monotonic() - self._think_t0:.1f}s")
            self._think_block = None

    def _tick_thinking(self):
        if self._think_block is None:
            self._think_timer.stop()
            return
        self._write_think_line(f"🤔 思考中… {time.monotonic() - self._think_t0:.1f}s")

    def _write_think_line(self, text: str):
        block = self._think_block
        if block is None or not block.isValid():    # 行被日志上限滚出 → 退化为追加
            self.log_box.append(text)
            self._think_block = self.log_box.document().lastBlock()
            return
        # 只替换块自身文本：BlockUnderCursor 会连前一块分隔符一起选中，
        # 删除后块合并、句柄失效（实测退化为一味追加）
        cursor = QTextCursor(block)
        cursor.setPosition(block.position(), QTextCursor.MoveAnchor)
        cursor.setPosition(block.position() + block.length() - 1, QTextCursor.KeepAnchor)
        cursor.removeSelectedText()
        cursor.insertText(text)


# ---- 深渊页（自动刷装备，docs/research/12） --------------------------------

QUOTA_ZH = (("safe", "蓝"), ("rush", "红"), ("impact", "黄"), ("risk", "紫"))
LEDGER_ZH = (("floor", "层数"), ("erosion", "侵蚀"), ("keys", "钥匙"), ("coins", "金币"),
             ("impact", "黄码"), ("rush", "红码"), ("safe", "蓝码"), ("risk", "紫码"))


class AbyssPage(QWidget):
    """自动深渊：入场 → 监督式推进（选房/拿码/事件）→ 到层结算。

    封装 abyss.enter_run + run_to_floor（poc/abyss_run 同款实测流程）；
    账本经 ledger 信号实时上屏；log 回调兼任停止检查点与画面抽帧。
    """

    def __init__(self, state: RunState, parent=None):
        super().__init__(parent)
        self.setObjectName("abyssPage")
        self.state = state
        self._led = None            # AbyssLedger（worker 线程在写，GUI 只读快照）
        self._last_shot = 0.0

        self.status_label = CaptionLabel("待机（游戏停在深渊入口页或深渊地图中即可启动）")

        # ---- 启停 ----
        ctrl = CardWidget()
        c = QHBoxLayout(ctrl)
        c.setContentsMargins(16, 10, 16, 10)
        c.setSpacing(8)
        self.btn_start = PrimaryPushButton(FIF.GLOBE, "开始探索")
        self.btn_stop = PushButton(FIF.PAUSE, "停止")
        self.btn_stop.setEnabled(False)
        c.addWidget(self.btn_start)
        c.addWidget(self.btn_stop)
        c.addStretch(1)
        c.addWidget(BodyLabel("模型"))
        self.provider = ComboBox()
        self.refresh_providers()
        c.addWidget(self.provider)

        # ---- 参数（两行：三个数值一行，四色配额一行——单行在 1100px 默认窗宽放不下）----
        cfg = CardWidget()
        gv = QVBoxLayout(cfg)
        gv.setContentsMargins(16, 10, 16, 10)
        gv.setSpacing(6)
        self.target = VSpinBox()
        self.target.setRange(1, 200)
        self.target.setValue(40)
        self.start_floor = VSpinBox()
        self.start_floor.setRange(1, 100)
        self.start_floor.setValue(20)
        self.max_rooms = VSpinBox()
        self.max_rooms.setRange(1, 200)
        self.max_rooms.setValue(30)
        row1 = QHBoxLayout()
        row1.setSpacing(6)
        for label, w in (("目标层", self.target), ("起始检查点", self.start_floor),
                         ("房间上限", self.max_rooms)):
            row1.addWidget(BodyLabel(label))
            row1.addWidget(w)
        row1.addStretch(1)
        gv.addLayout(row1)
        row2 = QHBoxLayout()
        row2.setSpacing(6)
        row2.addWidget(BodyLabel("代码配额"))
        self.quota_spins: dict[str, VSpinBox] = {}
        for color, zh in QUOTA_ZH:
            sp = VSpinBox()
            sp.setRange(0, 31)
            self.quota_spins[color] = sp
            row2.addWidget(BodyLabel(zh))
            row2.addWidget(sp)
        self.quota_spins["safe"].setValue(6)
        self.quota_spins["rush"].setValue(3)
        row2.addStretch(1)
        gv.addLayout(row2)

        # ---- 账本 ----
        ledger_card = CardWidget()
        lh = QHBoxLayout(ledger_card)
        lh.setContentsMargins(16, 8, 16, 8)
        lh.setSpacing(20)
        self.ledger_vals: dict[str, StrongBodyLabel] = {}
        for key, zh in LEDGER_ZH:
            box = QVBoxLayout()
            box.setSpacing(0)
            box.addWidget(CaptionLabel(zh))
            val = StrongBodyLabel("-")
            self.ledger_vals[key] = val
            box.addWidget(val)
            holder = QWidget()
            holder.setLayout(box)
            lh.addWidget(holder)
        lh.addStretch(1)

        # ---- 日志 ----
        log_card = CardWidget()
        lv = QVBoxLayout(log_card)
        lv.setContentsMargins(12, 8, 12, 8)
        self.log_box = TextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setFont(QFont("Consolas", 9))
        self.log_box.document().setMaximumBlockCount(2000)
        lv.addWidget(self.log_box)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 12)
        lay.setSpacing(10)
        head = QHBoxLayout()
        head.addWidget(TitleLabel("深渊"))
        head.addStretch(1)
        head.addWidget(self.status_label)
        lay.addLayout(head)
        lay.addWidget(ctrl)
        lay.addWidget(cfg)
        lay.addWidget(ledger_card)
        lay.addWidget(log_card, 1)

        self.btn_start.clicked.connect(self._start)
        self.btn_stop.clicked.connect(self._stop)
        state.sig.log.connect(self.log_box.append)
        state.sig.ledger.connect(self._on_ledger)
        state.sig.result.connect(self._on_result)
        state.sig.running.connect(self._on_running)
        self._load_params()

    # ---- 参数记忆（.local/abyss_params.json：下次打开恢复上次填的值） ----

    def _save_params(self):
        data = {
            "target": self.target.value(),
            "start_floor": self.start_floor.value(),
            "max_rooms": self.max_rooms.value(),
            "quota": {c: sp.value() for c, sp in self.quota_spins.items()},
        }
        try:
            ABYSS_PARAMS_PATH.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    def _load_params(self):
        try:
            data = json.loads(ABYSS_PARAMS_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for key, w in (("target", self.target), ("start_floor", self.start_floor),
                       ("max_rooms", self.max_rooms)):
            v = data.get(key)
            if isinstance(v, (int, float)):
                w.setValue(int(v))       # 超范围时 Qt 自动钳回合法区间
        quota = data.get("quota")
        if isinstance(quota, dict):
            for c, sp in self.quota_spins.items():
                v = quota.get(c)
                if isinstance(v, (int, float)):
                    sp.setValue(int(v))

    # ---- 启停 ----

    def refresh_providers(self):
        """模型页变更后重建下拉项；尽量保持当前选择。"""
        cur = self.provider.currentText()
        names = store.names()
        self.provider.clear()
        self.provider.addItems(names)
        self.provider.setCurrentText(cur if cur in names else store.active())

    def _start(self):
        if self.state.worker and self.state.worker.is_alive():
            InfoBar.warning("提示", "有任务/教学/深渊正在进行", parent=self,
                            position=InfoBarPosition.TOP, duration=2500)
            return
        target = self.target.value()
        start_floor = self.start_floor.value()
        if start_floor > target:
            InfoBar.warning("提示", "起始检查点不能高于目标层", parent=self,
                            position=InfoBarPosition.TOP, duration=2500)
            return
        quota = {c: sp.value() for c, sp in self.quota_spins.items() if sp.value() > 0}
        self.state.stop_event.clear()
        self.state.mode = "abyss"
        self.state.current_ids = ["abyss"]
        self._led = None
        self._last_shot = 0.0
        for val in self.ledger_vals.values():
            val.setText("-")
        args = (target, start_floor, quota, self.provider.currentText(), self.max_rooms.value())
        self._save_params()     # 启动即记住本次填法，下次打开自动恢复
        self.state.worker = threading.Thread(target=self._work, args=args, daemon=True)
        self.state.worker.start()
        self.state.sig.running.emit(True)

    def _stop(self):
        self.state.stop_event.set()
        self.status_label.setText("停止中（下一个日志点生效；战斗等待中需等本场结束）…")

    def _work(self, target, start_floor, quota, provider, max_rooms):
        s = self.state.sig
        stop = self.state.stop_event

        class StopRequested(Exception):
            pass

        def log(line: str):
            s.log.emit(line)
            if stop.is_set():
                raise StopRequested()
            self._maybe_frame()
            if self._led is not None:
                s.ledger.emit(self._snapshot())

        try:
            from .abyss import enter_run, run_to_floor
            from .abyss_plan import AbyssLedger
            from .abyss_ui import read_hud
            from .brain import Brain

            device = self.state.get_device()
            device.bring_to_front()
            enter_run(device, start_floor, log=log)
            hud = read_hud(device)
            log(f"[开局] HUD: {hud}")
            led = AbyssLedger(
                floor=hud.get("floor", start_floor), erosion=hud.get("erosion", 0),
                getkeys=hud.get("keys", 0), coins=hud.get("coins", 0),
                quota=quota, target_floor=target)
            self._led = led
            brain = Brain(provider=provider)   # 未知名代码 → 视觉定色入册
            brain.task_ctx = "abyss"
            r = run_to_floor(device, led, brain=brain, max_rooms=max_rooms, log=log)
            log(f"===== 深渊结束: {json.dumps(r, ensure_ascii=False)}")
            s.result.emit({"type": "result", "task": "abyss", "status": r.get("status", "?")})
        except StopRequested:
            s.log.emit("[深渊] 已停止——可重新从检查点继续")
            s.result.emit({"type": "result", "task": "abyss", "status": "stopped"})
        except Exception as e:
            s.log.emit(f"[深渊异常] {e.__class__.__name__}: {e}")
            s.result.emit({"type": "result", "task": "abyss", "status": "failed"})
        finally:
            self._led = None
            self.state.clear_run()
            s.running.emit(False)

    # ---- 槽 ----

    def _on_ledger(self, snap: dict):
        for key, val in self.ledger_vals.items():
            v = snap.get(key)
            if v is not None:
                val.setText(str(v))

    def _on_result(self, r: dict):
        if r.get("task") != "abyss":
            return
        status = r.get("status")
        tip = {
            "settled": ("深渊完成", "已到目标层并结算", InfoBar.success, 5000),
            "rooms_exhausted": ("房间上限", "已推进到房间上限但未到结算点", InfoBar.warning, 5000),
            "no_candidates": ("深渊受阻", "地图上没有候选房间，请人工看一眼", InfoBar.error, -1),
            "stopped": ("深渊已停止", "进度保留，可从检查点继续", InfoBar.warning, 5000),
            "failed": ("深渊异常", "详见下方日志", InfoBar.error, -1),
        }.get(status)
        if tip:
            title, text, maker, dur = tip
            maker(title, text, parent=self, position=InfoBarPosition.TOP, duration=dur)

    def _on_running(self, running: bool):
        self.btn_start.setEnabled(not running)
        self.btn_stop.setEnabled(running)
        if not running:
            self.status_label.setText("待机")

    # ---- 内部 ----

    def _snapshot(self) -> dict:
        led = self._led
        if led is None:
            return {}
        hud_keys = getattr(led, "keys", None)   # reconcile 以 HUD 键名回填，优先于账面 getkeys
        return {
            "floor": led.floor, "erosion": led.erosion,
            "keys": led.getkeys if hud_keys is None else hud_keys,
            "coins": led.coins,
            **{c: led.buffs.get(c, 0) for c, _ in QUOTA_ZH},
        }

    def _maybe_frame(self):
        """日志点顺带抽帧（≥2.5s 一张，失败不影响主流程）→ 监控页同步可见。"""
        now = time.monotonic()
        if now - self._last_shot < 2.5:
            return
        self._last_shot = now
        try:
            self.state.sig.frame.emit(self.state.get_device().screenshot())
        except Exception:
            pass


# ---- 教学页（新建任务，docs/research/11） --------------------------------

STATE_ZH = {"auto": "探索中…", "awaiting": "⬇ 等待你的指示", "distilling": "蒸馏中…", "done": "已结束"}


class Bubble(QLabel):
    """聊天气泡：agent=蓝(左)、user=绿(右)、system=黄(中)、step=灰字。"""

    def __init__(self, role: str, text: str, parent=None):
        super().__init__(text, parent)
        self.role = role
        self.setWordWrap(True)
        self.setTextInteractionFlags(Qt.TextSelectableByMouse)
        # 关键：换行 QLabel 必须开启 heightForWidth，否则布局按单行高度分配，
        # 文字会溢出气泡背景（实测教学页裁字的原因）
        sp = self.sizePolicy()
        sp.setHeightForWidth(True)
        self.setSizePolicy(sp)
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
        self.refresh_providers()
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
        state.sig.think.connect(self._on_thinking)
        self._think_bubble: Bubble | None = None
        self._think_timer = QTimer(self)
        self._think_timer.timeout.connect(self._tick_thinking)
        self._think_t0 = 0.0
        self._set_running(False)

    # ---- 槽 ----

    def set_frame(self, frame):
        pix = frame_to_pixmap(frame)
        if pix is None:
            return
        self.preview.setPixmap(
            pix.scaled(PREVIEW_W, PREVIEW_H, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        )

    def refresh_providers(self):
        """模型页变更后重建下拉项；尽量保持当前选择。"""
        cur = self.provider.currentText()
        names = store.names()
        self.provider.clear()
        self.provider.addItems(names)
        self.provider.setCurrentText(cur if cur in names else store.active())

    def _on_chat(self, ev: dict):
        self._add_bubble(ev.get("role", "system"), str(ev.get("text", "")))

    def _on_step(self, ev: dict):
        if self.state.mode != "teach":
            return  # 任务/深渊运行中的逐步事件不进教学聊天流
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

    # ---- 思考占位符（避免长决策时看似卡死） ----

    def _on_thinking(self, ev: dict):
        if self.state.mode != "teach":
            return  # 任务/深渊运行中的思考事件不进教学聊天流
        if ev.get("phase") == "start":
            self._think_bubble = self._add_bubble("step", "🤔 思考中… 0.0s")
            self._think_t0 = time.monotonic()
            self._think_timer.start(100)
        else:
            self._think_timer.stop()
            if self._think_bubble is not None:
                tokens = ev.get("tokens")
                quant = f"{tokens} tokens" if tokens else "（无用量数据）"
                try:
                    self._think_bubble.setText(
                        f"🤔 思考完成 · {quant} · {time.monotonic() - self._think_t0:.1f}s")
                except RuntimeError:
                    pass  # 气泡已被滚出上限清理
                self._think_bubble = None

    def _tick_thinking(self):
        if self._think_bubble is None:
            self._think_timer.stop()
            return
        try:
            self._think_bubble.setText(f"🤔 思考中… {time.monotonic() - self._think_t0:.1f}s")
        except RuntimeError:
            self._think_timer.stop()

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

    def _dispatch(self, ev: dict):
        """worker 线程事件 → 对应信号（跨线程投递）。"""
        s = self.state.sig
        t = ev.get("type")
        if t == "chat":
            s.chat.emit(ev)
        elif t == "state":
            s.tstate.emit(ev.get("state", ""))
        elif t == "thinking":
            s.think.emit(ev)
        elif t == "result":
            s.result.emit(ev)
        else:
            s.step.emit(ev)

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
        self.state.mode = "teach"
        self.state.current_ids = [tid]
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
        from .teach import run_teach_session
        try:
            device = self.state.get_device()
            brain = Brain(provider=provider)
            device.bring_to_front()
            run_teach_session(
                tid, name, goal, device, brain,
                log=s.log.emit,
                stop_event=self.state.stop_event,
                frame_cb=s.frame.emit,
                event_cb=self._dispatch,
                reply_get=self.reply_q.get,
            )
        except Exception as e:
            s.log.emit(f"[教学异常] {e.__class__.__name__}: {e}")
            s.chat.emit({"type": "chat", "role": "system", "text": f"会话异常结束：{e}"})
        finally:
            self.state.clear_run()
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

    def _add_bubble(self, role: str, text: str) -> Bubble:
        if not text:
            return None
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
        return b

    def _clear_chat(self):
        for b in self._bubbles:
            (b._holder if hasattr(b, "_holder") else b).deleteLater()
        self._bubbles.clear()

    def _scroll_bottom(self):
        bar = self.chat_scroll.verticalScrollBar()
        bar.setValue(bar.maximum())


# ---- 控制面附着（docs/research/13 §1） ------------------------------------


def call_in_gui(state: RunState, fn, timeout: float = 5.0):
    """HTTP 线程借用 GUI 线程执行 fn() 并取回返回值（经 ctl_call 信号投递）。"""
    q: queue.Queue = queue.Queue()

    def _job():
        try:
            q.put(("ok", fn()))
        except Exception as e:
            q.put(("err", f"{e.__class__.__name__}: {e}"))

    state.sig.ctl_call.emit(_job)
    kind, payload = q.get(timeout=timeout)
    if kind == "err":
        raise CtlError(payload)
    return payload


class ModelPage(QWidget):
    """模型管理：OpenAI 兼容 provider 增删改、密钥保存、/models 自动发现。

    配置落在 .local/providers.json（gitignore），仓库只留内置种子；
    增删改后发 providers_changed，任务/深渊/教学三页的模型下拉联动刷新。
    """

    _discovered = Signal(list, str)     # 发现完成：模型 id 列表 / 错误信息

    def __init__(self, state: RunState, parent=None):
        super().__init__(parent)
        self.setObjectName("modelPage")
        self.state = state
        self._editing: str | None = None    # 正在编辑的 provider 名（None=新增）

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 12)
        lay.setSpacing(10)
        head = QHBoxLayout()
        head.addWidget(TitleLabel("模型"))
        head.addStretch(1)
        head.addWidget(CaptionLabel("OpenAI 兼容接口；密钥只存 .local，不随仓库提交"))
        lay.addLayout(head)

        # ---- provider 列表 ----
        list_card = CardWidget()
        lv = QVBoxLayout(list_card)
        lv.setContentsMargins(12, 8, 12, 8)
        lv.setSpacing(4)
        self.list_box = QVBoxLayout()
        self.list_box.setSpacing(4)
        lv.addLayout(self.list_box)
        lay.addWidget(list_card)

        # ---- 编辑器 ----
        edit_card = CardWidget()
        ev = QVBoxLayout(edit_card)
        ev.setContentsMargins(16, 10, 16, 10)
        ev.setSpacing(6)
        self.edit_hint = CaptionLabel("新增")
        ev.addWidget(self.edit_hint)
        self.in_name = LineEdit()
        self.in_name.setPlaceholderText("名称标识（任务页模型下拉里显示的名字，如 deepseek）")
        self.in_base = LineEdit()
        self.in_base.setPlaceholderText("Base URL（OpenAI 兼容，如 https://api.example.com/v1）")
        r1 = QHBoxLayout()
        r1.addWidget(self.in_name, 2)
        r1.addWidget(self.in_base, 5)
        ev.addLayout(r1)
        self.in_key = PasswordLineEdit()
        self.in_key.setPlaceholderText("API Key（sk-…，明文保存到 .local/providers.json）")
        self.btn_discover = PushButton(FIF.SEARCH, "发现模型")
        r2 = QHBoxLayout()
        r2.addWidget(self.in_key, 3)
        r2.addWidget(self.btn_discover)
        ev.addLayout(r2)
        self.in_model = LineEdit()
        self.in_model.setPlaceholderText("模型名（手动填写，或点发现后从下拉选）")
        self.cb_remote = ComboBox()
        self.cb_remote.setMinimumWidth(240)
        self.cb_remote.currentTextChanged.connect(
            lambda t: self.in_model.setText(t) if t else None)
        r3 = QHBoxLayout()
        r3.addWidget(self.in_model, 3)
        r3.addWidget(self.cb_remote, 2)
        ev.addLayout(r3)
        btns = QHBoxLayout()
        self.btn_save = PrimaryPushButton(FIF.SAVE, "保存")
        self.btn_reset = PushButton("清空表单")
        btns.addWidget(self.btn_save)
        btns.addWidget(self.btn_reset)
        btns.addStretch(1)
        ev.addLayout(btns)
        lay.addWidget(edit_card)
        lay.addStretch(1)

        self.btn_discover.clicked.connect(self._discover)
        self.btn_save.clicked.connect(self._save)
        self.btn_reset.clicked.connect(self._reset_form)
        self._discovered.connect(self._on_discovered)
        self._reset_form()
        self._rebuild_list()

    # ---- 列表 ----------------------------------------------------------

    def _rebuild_list(self):
        while self.list_box.count():
            it = self.list_box.takeAt(0)
            if w := it.widget():
                w.deleteLater()
        for name in store.names():
            self.list_box.addWidget(self._row(name))
        if not store.names():
            self.list_box.addWidget(CaptionLabel("暂无 provider，用下方表单添加"))

    def _row(self, name: str) -> QWidget:
        cfg = store.get(name)
        row = QFrame()
        h = QHBoxLayout(row)
        h.setContentsMargins(8, 2, 8, 2)
        h.setSpacing(8)
        h.addWidget(StrongBodyLabel(name))
        h.addWidget(CaptionLabel(f"{cfg['model']}  ·  {cfg['base_url']}  ·  {store.key_desc(name)}"), 1)
        if name == store.active():
            tag = PrimaryPushButton("主力")
            tag.setEnabled(False)
            h.addWidget(tag)
        else:
            b_act = PushButton("设为主力")
            b_act.clicked.connect(lambda _, n=name: self._set_active(n))
            h.addWidget(b_act)
        b_edit = PushButton(FIF.EDIT, "编辑")
        b_edit.clicked.connect(lambda _, n=name: self._edit(n))
        h.addWidget(b_edit)
        b_del = PushButton(FIF.DELETE, "删除")
        b_del.clicked.connect(lambda _, n=name: self._remove(n))
        h.addWidget(b_del)
        return row

    # ---- 动作 ----------------------------------------------------------

    def _set_active(self, name: str):
        store.set_active(name)
        self._changed()
        InfoBar.success("已切换主力", f"新任务默认使用 {name}",
                        parent=self, position=InfoBarPosition.TOP, duration=3000)

    def _edit(self, name: str):
        cfg = store.get(name)
        self._editing = name
        self.edit_hint.setText(f"正在编辑 {name}（名称不可改，改名请新增后删除旧的）")
        self.in_name.setText(name)
        self.in_name.setReadOnly(True)
        self.in_base.setText(cfg["base_url"])
        self.in_model.setText(cfg["model"])
        self.in_key.clear()
        self.in_key.setPlaceholderText(f"API Key（留空保持原密钥：{store.key_desc(name)}）")

    def _remove(self, name: str):
        box = MessageBox("删除 provider", f"确定删除「{name}」？\n（.local 里的密钥文件不会被动）",
                         self.window())
        if not box.exec():
            return
        try:
            store.remove(name)
        except (ValueError, KeyError) as e:
            InfoBar.warning("删除失败", str(e),
                            parent=self, position=InfoBarPosition.TOP, duration=4000)
            return
        if self._editing == name:
            self._reset_form()
        self._changed()

    def _discover(self):
        base_url = self.in_base.text().strip()
        key = self.in_key.text().strip()
        if not base_url.startswith(("http://", "https://")):
            InfoBar.warning("提示", "先填 Base URL（http(s):// 开头）",
                            parent=self, position=InfoBarPosition.TOP, duration=3000)
            return
        # 编辑已有 provider 且没填新 key：沿用旧密钥去发现
        if not key and self._editing:
            try:
                key = store.key(self._editing)
            except (KeyError, OSError):
                key = ""
        self.btn_discover.setEnabled(False)
        self.btn_discover.setText("发现中…")
        threading.Thread(target=self._discover_work, args=(base_url, key), daemon=True).start()

    def _discover_work(self, base_url: str, key: str):
        try:
            self._discovered.emit(discover_models(base_url, key), "")
        except Exception as e:
            self._discovered.emit([], f"{e.__class__.__name__}: {e}")

    def _on_discovered(self, models: list, err: str):
        self.btn_discover.setEnabled(True)
        self.btn_discover.setText("发现模型")
        self.cb_remote.clear()
        if err:
            InfoBar.error("发现失败", err[:160],
                          parent=self, position=InfoBarPosition.TOP, duration=-1)
            return
        self.cb_remote.addItems(models)
        InfoBar.success("发现成功", f"共 {len(models)} 个模型，下拉选择自动填入模型名",
                        parent=self, position=InfoBarPosition.TOP, duration=3000)

    def _save(self):
        old = None
        if self._editing:
            try:
                old = store.get(self._editing)
            except KeyError:
                old = None
        try:
            store.upsert(
                self.in_name.text(), self.in_base.text(), self.in_model.text(),
                api_key=self.in_key.text(),
                key_path=(old or {}).get("key_path", ""))
        except (ValueError, KeyError) as e:
            InfoBar.warning("保存失败", str(e),
                            parent=self, position=InfoBarPosition.TOP, duration=4000)
            return
        name = self.in_name.text().strip()
        self._changed()
        InfoBar.success("已保存", f"{name} 已写入 .local/providers.json",
                        parent=self, position=InfoBarPosition.TOP, duration=3000)

    def _reset_form(self):
        self._editing = None
        self.edit_hint.setText("新增")
        self.in_name.setReadOnly(False)
        for w in (self.in_name, self.in_base, self.in_model, self.in_key):
            w.clear()
        self.in_key.setPlaceholderText("API Key（sk-…，明文保存到 .local/providers.json）")
        self.cb_remote.clear()

    def _changed(self):
        self._rebuild_list()
        self.state.sig.providers_changed.emit()


class GuiCtlAdapter:
    """GUI 引擎能力 → ControlServer 命令表。方法在 HTTP 线程执行，
    碰 Qt 控件的部分经 call_in_gui marshal 到 GUI 线程。"""

    def __init__(self, state: RunState, tasks: TaskPage, window: "MainWindow"):
        self.state = state
        self.tasks = tasks
        self.window = window
        self._logs: deque[str] = deque(maxlen=800)
        self._results: list[dict] = []
        self._lock = threading.Lock()
        state.sig.log.connect(self._on_log)
        state.sig.result.connect(self._on_result)

    def routes(self) -> dict:
        return {
            "status": self.status, "tasks": self.tasks_list, "run": self.run,
            "stop": self.stop, "screenshot": self.screenshot,
            "logs": self.logs, "usage": self.usage_stats, "quit": self.quit,
        }

    # ---- 命令（HTTP 线程） ----

    def status(self, params: dict) -> dict:
        running = bool(self.state.worker and self.state.worker.is_alive())
        with self._lock:
            results = list(self._results[-20:])
        return {
            "running": running,
            "mode": self.state.mode if running else None,
            "tasks": list(self.state.current_ids),
            "game_bound": self.state.has_device(),
            "backend": self.state.backend,
            "pid": os.getpid(),
            "results": results,
        }

    def tasks_list(self, params: dict) -> dict:
        return {"tasks": [
            {"id": t["id"], "name": t.get("name", ""), "flow": t.get("flow")}
            for t in load_tasks()
        ]}

    def run(self, params: dict) -> dict:
        if self.state.worker and self.state.worker.is_alive():
            raise CtlError("引擎忙碌（任务/教学进行中），先 ctl stop 或等其结束")
        known = {t["id"] for t in load_tasks()}
        ids = list(params.get("task_ids") or [])
        if params.get("all"):
            ids = list(known)
        if not ids:
            raise CtlError("task_ids 为空（或用 all:true）")
        unknown = [i for i in ids if i not in known]
        if unknown:
            raise CtlError(f"未知任务: {', '.join(unknown)}")
        provider = params.get("provider") or store.active()
        if provider not in store.names():
            raise CtlError(f"未知 provider: {provider}")
        max_steps = max(1, int(params.get("max_steps", 30)))
        budget = max(30.0, float(params.get("time_budget", 420.0)))
        update_knowledge = bool(params.get("update_knowledge", True))
        with self._lock:
            self._results.clear()
        self.state.sig.log.emit(f"[ctl] run {' '.join(ids)}")
        call_in_gui(self.state, lambda: self.tasks.start(
            ids, max_steps, budget, provider, update_knowledge))
        return {"started": ids, "max_steps": max_steps,
                "time_budget": budget, "provider": provider}

    def stop(self, params: dict) -> dict:
        self.state.stop_event.set()
        self.state.sig.log.emit("[ctl] stop 请求")
        return {"stopping": True}

    def screenshot(self, params: dict) -> dict:
        try:
            img = self.state.get_device().screenshot()
        except DeviceError as e:
            # 窗口没了：空闲时丢缓存下次重建；运行中不动（防并发双控制器）
            if not (self.state.worker and self.state.worker.is_alive()):
                self.state.drop_device()
            raise CtlError(str(e))
        out = params.get("out") or str(SHOTS_DIR / f"shot_{time.strftime('%Y%m%d_%H%M%S')}.png")
        save_frame(img, out)
        return {"path": out, "width": int(img.shape[1]), "height": int(img.shape[0])}

    def logs(self, params: dict) -> dict:
        with self._lock:
            lines = list(self._logs)
        return {"lines": lines[-max(1, int(params.get("tail", 50))):]}

    def usage_stats(self, params: dict) -> dict:
        """LLM 用量聚合（不碰 Qt，HTTP 线程直接算）。days<=0 表示全部。"""
        from . import usage
        days = params.get("days", 30)
        if days is not None:
            days = int(days) if int(days) > 0 else None
        return usage.aggregate(days)

    def quit(self, params: dict) -> dict:
        # 延迟关窗：让本请求的响应先发回客户端
        call_in_gui(self.state, lambda: QTimer.singleShot(100, self.window.close))
        return {"quitting": True}

    # ---- 信号收集（GUI 线程） ----

    def _on_log(self, line: str):
        with self._lock:
            self._logs.append(line)

    def _on_result(self, r: dict):
        with self._lock:
            self._results.append(r)


class MainWindow(FluentWindow):
    def __init__(self):
        super().__init__()
        self.state = RunState()

        self.monitor = MonitorPage(self.state)
        self.tasks = TaskPage(self.state, self.monitor)
        self.abyss = AbyssPage(self.state)
        self.teach = TeachPage(self.state, self.tasks)
        self.models = ModelPage(self.state)
        self.usage = UsagePage()

        self.addSubInterface(self.tasks, FIF.PLAY, "任务")
        self.addSubInterface(self.abyss, FIF.GLOBE, "深渊")
        self.addSubInterface(self.monitor, FIF.CAMERA, "监控")
        self.addSubInterface(self.teach, FIF.ADD, "新建任务")
        self.addSubInterface(self.usage, FIF.HISTORY, "用量")
        self.addSubInterface(self.models, FIF.ROBOT, "模型")

        # 模型页变更 → 任务/深渊/教学三页下拉联动
        self.state.sig.providers_changed.connect(self._refresh_provider_combos)
        self._refresh_provider_combos()

        # 1100px 默认窗宽下深渊参数行才放得下：侧栏保持图标模式，不自动展开
        self.navigationInterface.setMinimumExpandWidth(1200)

        self.resize(1100, 720)
        self.setMinimumSize(960, 640)
        self.setWindowTitle("DotAbyss Agent")

    def _refresh_provider_combos(self):
        for page in (self.tasks, self.abyss, self.teach):
            page.refresh_providers()

        # 控制面：本进程承载引擎，CLI 经 .local/ctl.json 附着（docs/research/13）
        self.ctl = GuiCtlAdapter(self.state, self.tasks, self)
        self.state.sig.ctl_call.connect(lambda fn: fn())
        self.ctl_server = ControlServer(self.ctl.routes())
        port = self.ctl_server.start()
        self.state.sig.log.emit(
            f"[ctl] 控制接口已启动 127.0.0.1:{port}（发现文件 .local/ctl.json）")

    def closeEvent(self, event):
        # 先停控制面与运行任务，再断开跨线程信号，避免事件投递到已销毁的控件
        self.ctl_server.shutdown()
        self.state.stop_event.set()
        for sig in (self.state.sig.log, self.state.sig.frame, self.state.sig.step,
                    self.state.sig.result, self.state.sig.running,
                    self.state.sig.chat, self.state.sig.tstate, self.state.sig.think,
                    self.state.sig.ctl_call, self.state.sig.ledger,
                    self.state.sig.providers_changed):
            try:
                sig.disconnect()
            except RuntimeError:
                pass
        super().closeEvent(event)


def _load_app_icon(app: QApplication):
    """窗口/任务栏图标：源码态取仓库 packaging/icon.png，exe 态取打包资源。"""
    icon_path = ROOT / "packaging" / "icon.png"
    if getattr(sys, "frozen", False) and not icon_path.exists():
        icon_path = Path(sys._MEIPASS) / "icon.png"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))


def main():
    app = QApplication(sys.argv)
    _load_app_icon(app)
    setTheme(Theme.DARK)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
