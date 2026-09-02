"""用量页：LLM token 用量统计（总览 / 按日 / 分时 / 场景 / 模型 / 任务 / 明细）。

数据源是 usage 模块落盘的 .local/usage/*.jsonl（brain._chat 埋点）；
图表用 QPainter 自绘堆叠柱（引 QtCharts 会撑大打包体积，两根柱没必要）。
"""
import math

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (QGridLayout, QHBoxLayout, QTableWidgetItem,
                               QToolTip, QVBoxLayout, QWidget)
from qfluentwidgets import (BodyLabel, CaptionLabel, CardWidget, ComboBox,
                            FluentIcon as FIF, PushButton, SmoothScrollArea,
                            StrongBodyLabel, TableWidget, TitleLabel)
from qfluentwidgets.common.style_sheet import isDarkTheme

from . import usage

# 图例三色：输入(未命中) / 缓存命中 / 输出
C_INPUT = QColor("#4C8DFF")
C_CACHED = QColor("#9B59F6")
C_OUTPUT = QColor("#22C55E")


def _fmt_tokens(v) -> str:
    if not isinstance(v, (int, float)) or v <= 0:
        return "-" if v is None else "0"
    v = int(v)
    if v >= 1_000_000:
        return f"{v / 1_000_000:.2f}M"
    if v >= 100_000:
        return f"{v / 1000:.1f}k"
    return f"{v:,}"


def _fmt_latency(v) -> str:
    return f"{v:.2f}s" if isinstance(v, (int, float)) else "-"


def _fmt_when(ts: str | None, with_seconds: bool = False) -> str:
    """ISO ts → 'MM-DD HH:MM[:SS]'；无 T 分隔符，列宽友好。"""
    if not ts:
        return "-"
    body = ts[5:16].replace("T", " ")
    return f"{body}:{ts[17:19]}" if with_seconds and len(ts) >= 19 else body


def _pct(part, whole) -> str:
    if not isinstance(part, (int, float)) or not whole:
        return "0%"
    return f"{part * 100.0 / whole:.1f}%"


class _Metric(QWidget):
    """总览卡里的单个指标：数值 + 标题 + 副注。"""

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.value = StrongBodyLabel("-")
        f = self.value.font()
        f.setPointSize(15)
        self.value.setFont(f)
        self.title = CaptionLabel(title)
        self.sub = CaptionLabel("")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 4, 8, 4)
        lay.setSpacing(1)
        lay.addWidget(self.value)
        lay.addWidget(self.title)
        lay.addWidget(self.sub)


class _UsageBarChart(QWidget):
    """QPainter 自绘三段堆叠柱状图（输入未命中/缓存/输出），带 hover 提示。"""

    def __init__(self, height: int, parent=None):
        super().__init__(parent)
        self.setFixedHeight(height)
        self.setMouseTracking(True)
        self._items: list[dict] = []       # {label, tip, extra, cached, completion}
        self._hover = -1

    def set_data(self, items: list[dict]):
        self._items = items
        self._hover = -1
        self.update()

    def _colors(self):
        dark = isDarkTheme()
        text = QColor(255, 255, 255, 170) if dark else QColor(0, 0, 0, 150)
        grid = QColor(255, 255, 255, 26) if dark else QColor(0, 0, 0, 22)
        return text, grid

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        text, grid = self._colors()
        w, h = self.width(), self.height()
        ml, mr, mt, mb = 52, 10, 10, 24
        cw, ch = w - ml - mr, h - mt - mb
        p.setFont(self.font())

        items = self._items
        max_v = max((i["extra"] + i["cached"] + i["completion"] for i in items), default=0)
        nice = _nice_ceil(max_v)
        p.setPen(text)
        for frac in (1.0, 0.75, 0.5, 0.25):
            y = mt + ch * (1 - frac)
            p.setPen(grid)
            p.drawLine(ml, int(y), w - mr, int(y))
            p.setPen(text)
            p.drawText(QRectF(0, y - 8, ml - 6, 16), Qt.AlignRight | Qt.AlignVCenter,
                       _fmt_tokens(int(nice * frac)))

        n = len(items)
        if n:
            slot = cw / n
            bar = min(30.0, slot * 0.62)
            label_every = max(1, -(-n // 12))    # 标签抽稀，最多 ~12 个
            for idx, it in enumerate(items):
                x = ml + idx * slot + (slot - bar) / 2
                y = mt + ch
                total = it["extra"] + it["cached"] + it["completion"]
                if total > 0:
                    scale = ch / nice
                    for val, color in ((it["extra"], C_INPUT),
                                       (it["cached"], C_CACHED),
                                       (it["completion"], C_OUTPUT)):
                        if val <= 0:
                            continue
                        seg = max(2.0, val * scale)   # 极小值也画 2px，看得见
                        y -= seg
                        p.setPen(Qt.NoPen)
                        p.setBrush(color)
                        p.drawRoundedRect(QRectF(x, y, bar, seg), 2, 2)
                if idx % label_every == 0:
                    p.setPen(text)
                    p.drawText(QRectF(x - 8, h - mb + 4, bar + 16, 16),
                               Qt.AlignCenter, it["label"])
                if idx == self._hover:
                    p.setPen(QColor(255, 255, 255, 200) if isDarkTheme()
                             else QColor(0, 0, 0, 160))
                    p.setBrush(Qt.NoBrush)
                    p.drawRect(QRectF(x - 2, mt, bar + 4, ch))

    def mouseMoveEvent(self, e):
        ml, mr = 52, 10
        n = len(self._items)
        idx = -1
        if n:
            slot = (self.width() - ml - mr) / n
            k = int((e.position().x() - ml) / slot)
            if 0 <= k < n:
                idx = k
        if idx != self._hover:
            self._hover = idx
            self.update()
        if 0 <= idx < n:
            QToolTip.showText(e.globalPosition().toPoint(), self._items[idx]["tip"], self)

    def leaveEvent(self, e):
        self._hover = -1
        self.update()


def _nice_ceil(v: float) -> float:
    """向上取整到 1/2/5×10^k，坐标轴刻度好看。"""
    if v <= 0:
        return 1.0
    base = 10 ** int(math.log10(v))
    for m in (1, 2, 5, 10):
        if v <= m * base:
            return m * base
    return 10 * base


class UsagePage(QWidget):
    """侧栏「用量」页。"""

    RANGES = [("今天", 1), ("近 7 天", 7), ("近 30 天", 30), ("全部", None)]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("usagePage")

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 12)
        root.setSpacing(10)

        head = QHBoxLayout()
        head.addWidget(TitleLabel("用量"))
        head.addSpacing(8)
        head.addWidget(CaptionLabel("每次模型调用的 token 记录在 .local/usage/（一天一文件）"))
        head.addStretch(1)
        head.addWidget(CaptionLabel("时间范围"))
        self.cb_range = ComboBox()
        self.cb_range.setMinimumWidth(110)
        for name, _ in self.RANGES:
            self.cb_range.addItem(text=name)
        self.btn_refresh = PushButton(FIF.SYNC, "刷新")
        head.addWidget(self.cb_range)
        head.addWidget(self.btn_refresh)
        root.addLayout(head)

        self.scroll = SmoothScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setStyleSheet("QScrollArea{border:none; background:transparent;}")
        body = QVBoxLayout()
        body.setSpacing(10)
        inner = QWidget()
        inner.setLayout(body)
        inner.setStyleSheet("background:transparent;")
        self.scroll.setWidget(inner)
        root.addWidget(self.scroll, 1)

        # ---- 总览卡 ----
        overview = CardWidget()
        ov = QVBoxLayout(overview)
        ov.setContentsMargins(16, 10, 16, 10)
        head_row = QHBoxLayout()
        head_row.addWidget(StrongBodyLabel("总览"))
        self.range_hint = CaptionLabel("")
        head_row.addStretch(1)
        head_row.addWidget(self.range_hint)
        ov.addLayout(head_row)
        grid = QGridLayout()
        grid.setSpacing(6)
        self.m_req = _Metric("请求数")
        self.m_total = _Metric("总 tokens")
        self.m_in = _Metric("输入 tokens")
        self.m_cached = _Metric("缓存命中 tokens")
        self.m_out = _Metric("输出 tokens")
        self.m_lat = _Metric("平均耗时")
        for col, m in enumerate((self.m_req, self.m_total, self.m_in)):
            grid.addWidget(m, 0, col)
        for col, m in enumerate((self.m_cached, self.m_out, self.m_lat)):
            grid.addWidget(m, 1, col)
        ov.addLayout(grid)
        body.addWidget(overview)

        # ---- 按日图 ----
        day_card = CardWidget()
        dv = QVBoxLayout(day_card)
        dv.setContentsMargins(16, 10, 16, 10)
        row = QHBoxLayout()
        row.addWidget(StrongBodyLabel("按日用量"))
        row.addSpacing(10)
        for color, name in ((C_INPUT, "输入(未命中)"), (C_CACHED, "缓存命中"), (C_OUTPUT, "输出")):
            chip = _Chip(color, name)
            row.addWidget(chip)
        row.addStretch(1)
        self.day_hint = CaptionLabel("")
        row.addWidget(self.day_hint)
        dv.addLayout(row)
        self.day_chart = _UsageBarChart(220)
        dv.addWidget(self.day_chart)
        body.addWidget(day_card)

        # ---- 分时图 ----
        hour_card = CardWidget()
        hv = QVBoxLayout(hour_card)
        hv.setContentsMargins(16, 10, 16, 10)
        hrow = QHBoxLayout()
        self.hour_title = StrongBodyLabel("分时用量")
        hrow.addWidget(self.hour_title)
        hrow.addStretch(1)
        self.hour_hint = CaptionLabel("")
        hrow.addWidget(self.hour_hint)
        hv.addLayout(hrow)
        self.hour_chart = _UsageBarChart(170)
        hv.addWidget(self.hour_chart)
        body.addWidget(hour_card)

        # ---- 场景 / 模型 / 任务 表 ----
        self.tb_scene = self._make_table(["场景", "请求", "失败", "输入", "缓存命中",
                                          "输出", "平均耗时", "最近使用"])
        body.addWidget(self._card("按场景", self.tb_scene))
        self.tb_model = self._make_table(["Provider", "模型", "请求", "失败", "输入",
                                          "缓存命中", "输出", "平均耗时"])
        body.addWidget(self._card("按模型", self.tb_model))
        self.tb_task = self._make_table(["任务", "请求", "失败", "输入", "缓存命中",
                                         "输出", "总 tokens", "平均耗时"])
        body.addWidget(self._card("按任务", self.tb_task))

        # ---- 明细 ----
        self.tb_recent = self._make_table(["时间", "场景", "任务", "模型", "输入",
                                           "缓存", "输出", "耗时", "状态"])
        self.tb_recent.setFixedHeight(340)
        body.addWidget(self._card("最近调用", self.tb_recent))
        body.addStretch(1)

        self.cb_range.currentIndexChanged.connect(self._reload)
        self.btn_refresh.clicked.connect(self._reload)

    # ---- 构建辅助 -------------------------------------------------------

    def _card(self, title: str, table: TableWidget) -> CardWidget:
        card = CardWidget()
        v = QVBoxLayout(card)
        v.setContentsMargins(16, 10, 16, 10)
        v.setSpacing(6)
        v.addWidget(StrongBodyLabel(title))
        v.addWidget(table)
        return card

    @staticmethod
    def _make_table(headers: list[str]) -> TableWidget:
        t = TableWidget()
        t.setColumnCount(len(headers))
        t.setHorizontalHeaderLabels(headers)
        t.verticalHeader().hide()
        t.setBorderVisible(True)
        t.setBorderRadius(8)
        t.setWordWrap(False)
        t.setEditTriggers(t.EditTrigger.NoEditTriggers)
        t.setSelectionMode(t.SelectionMode.SingleSelection)
        return t

    @staticmethod
    def _fill(table: TableWidget, rows: list[list[str]], widths: list[int],
              tooltips: list[list[str]] | None = None, empty_text: str = "暂无数据"):
        n = len(rows)
        ncols = table.columnCount()
        if n == 0:
            table.setRowCount(1)
            table.setSpan(0, 0, 1, ncols)
            table.setItem(0, 0, QTableWidgetItem(empty_text))
            table.setFixedHeight(84)
            return
        table.setRowCount(n)
        table.setSpan(0, 0, 1, 1)   # 清掉可能残留的合并
        for r, row in enumerate(rows):
            for c, text in enumerate(row):
                it = QTableWidgetItem(text)
                if tooltips and c < len(tooltips[r]) and tooltips[r][c]:
                    it.setToolTip(tooltips[r][c])
                table.setItem(r, c, it)
        for c, w in enumerate(widths):
            table.setColumnWidth(c, w)
        table.setFixedHeight(min(36 * n + 46, 320))

    # ---- 数据装载 -------------------------------------------------------

    def _reload(self):
        days = self.RANGES[self.cb_range.currentIndex()][1]
        d = usage.aggregate(days)
        t = d["total"]

        self.range_hint.setText(_range_text(d))

        self.m_req.value.setText(f"{t['requests']:,}")
        self.m_req.sub.setText(f"失败 {t['fail']} · 成功率 {_pct(t['requests'] - t['fail'], t['requests'])}")
        self.m_total.value.setText(_fmt_tokens(t["total_tokens"]))
        self.m_total.sub.setText(f"平均 {_fmt_tokens(t['total_tokens'] // t['requests'])}/次"
                                 if t["requests"] else "")
        self.m_in.value.setText(_fmt_tokens(t["prompt_tokens"]))
        self.m_in.sub.setText(f"含缓存命中 {t['cached_tokens']:,}")
        self.m_cached.value.setText(_fmt_tokens(t["cached_tokens"]))
        self.m_cached.sub.setText(f"占输入 {_pct(t['cached_tokens'], t['prompt_tokens'])}")
        self.m_out.value.setText(_fmt_tokens(t["completion_tokens"]))
        self.m_out.sub.setText(f"其中推理 {t['reasoning_tokens']:,} ({_pct(t['reasoning_tokens'], t['completion_tokens'])})")
        self.m_lat.value.setText(_fmt_latency(t["latency_avg"]))
        self.m_lat.sub.setText(f"峰值 {_fmt_latency(t['latency_max'])}")

        self.day_chart.set_data(self._chart_items(d["by_day"], "date"))
        self.day_hint.setText(f"{'近 ' + str(days) + ' 天' if days else '全部'}·"
                              f"{d['from'] or '无数据'} ~ {d['to'] or ''}")

        if d["hour_date"]:
            self.hour_title.setText(f"分时用量（{d['hour_date']}）")
            self.hour_chart.set_data(self._chart_items(d["by_hour"], "hour"))
            peak = max(d["by_hour"], key=lambda h: h["total_tokens"])
            self.hour_hint.setText(f"峰值 {int(peak['hour']):02d} 时 · {_fmt_tokens(peak['total_tokens'])}"
                                   if peak["total_tokens"] else "")
        else:
            self.hour_title.setText("分时用量")
            self.hour_chart.set_data([])
            self.hour_hint.setText("")

        self._fill(self.tb_scene,
                   [[b["scene_zh"], f"{b['requests']:,}", str(b["fail"]) or "-",
                     f"{b['prompt_tokens']:,}", f"{b['cached_tokens']:,}",
                     f"{b['completion_tokens']:,}", _fmt_latency(b["latency_avg"]),
                     _fmt_when(b.get("last_ts"))]
                    for b in d["by_scene"]],
                   [150, 70, 56, 104, 104, 104, 84, 120],
                   empty_text="该范围内还没有调用记录")
        self._fill(self.tb_model,
                   [[b["provider"], b["model"], f"{b['requests']:,}", str(b["fail"]) or "-",
                     f"{b['prompt_tokens']:,}", f"{b['cached_tokens']:,}",
                     f"{b['completion_tokens']:,}", _fmt_latency(b["latency_avg"])]
                    for b in d["by_model"]],
                   [90, 170, 70, 56, 104, 104, 104, 84])
        self._fill(self.tb_task,
                   [[b["task"], f"{b['requests']:,}", str(b["fail"]) or "-",
                     f"{b['prompt_tokens']:,}", f"{b['cached_tokens']:,}",
                     f"{b['completion_tokens']:,}", _fmt_tokens(b["total_tokens"]),
                     _fmt_latency(b["latency_avg"])]
                    for b in d["by_task"]],
                   [180, 70, 56, 104, 104, 104, 110, 84],
                   empty_text="暂无任务维度的调用")

        tips = [[("" if e.get("ok") else f"错误: {e.get('err') or '?'}")] * 9
                for e in d["recent"]]
        self._fill(self.tb_recent,
                   [[_fmt_when(e.get("ts"), with_seconds=True), e["scene_zh"],
                     e.get("task") or "-",
                     f"{e.get('provider', '?')}/{e.get('model', '?')}",
                     _fmt_tokens(e.get("prompt_tokens")),
                     _fmt_tokens(e.get("cached_tokens")),
                     _fmt_tokens(e.get("completion_tokens")),
                     _fmt_latency(e.get("latency_s")),
                     "成功" if e.get("ok") else "失败"]
                    for e in d["recent"]],
                   [128, 130, 110, 170, 76, 76, 76, 72, 76],
                   tooltips=tips)

    @staticmethod
    def _chart_items(rows: list[dict], key: str) -> list[dict]:
        items = []
        for b in rows:
            prompt = b["prompt_tokens"]
            cached = min(b["cached_tokens"], prompt)   # 防御：缓存不可能超输入
            extra = prompt - cached
            comp = b["completion_tokens"]
            if key == "hour":
                label = f"{b['hour']:02d}"
                head = f"{b['hour']:02d}:00"
            else:
                label = b["date"][5:]                  # MM-DD
                head = b["date"]
            tip = (f"{head}\n请求 {b['requests']:,}（失败 {b['fail']}）\n"
                   f"输入 {_fmt_tokens(prompt)}（缓存 {_fmt_tokens(cached)}）\n"
                   f"输出 {_fmt_tokens(comp)} · 合计 {_fmt_tokens(b['total_tokens'])}")
            items.append({"label": label, "tip": tip,
                          "extra": extra, "cached": cached, "completion": comp})
        return items

    # ---- 事件 -----------------------------------------------------------

    def showEvent(self, e):
        super().showEvent(e)
        self._reload()


class _Chip(QWidget):
    """图例色块。"""

    def __init__(self, color: QColor, name: str, parent=None):
        super().__init__(parent)
        h = QHBoxLayout(self)
        h.setContentsMargins(6, 0, 6, 0)
        h.setSpacing(4)
        block = QWidget()
        block.setFixedSize(10, 10)
        block.setStyleSheet(f"background:{color.name()}; border-radius:2px;")
        h.addWidget(block)
        h.addWidget(CaptionLabel(name))


def _range_text(d: dict) -> str:
    return f"{d['from'] or '-'} ~ {d['to'] or '-'}（{len(d['by_day'])} 天）"
