"""基础 GUI：任务列表 / 实时日志 / 截图预览 / 启停控制。

用法: python -m dotabyss_agent.gui
零第三方 GUI 依赖（tkinter 标准库 + 已装的 pillow）。
"""
import json
import queue
import threading
import tkinter as tk
from tkinter import scrolledtext, ttk

from PIL import Image, ImageTk

from .runner import load_tasks, run_selected

PREVIEW_W, PREVIEW_H = 512, 288  # 1280x720 缩放


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("DotAbyss Agent")
        root.geometry("1000x680")

        self.log_q: queue.Queue = queue.Queue()
        self.frame_q: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self._photo = None  # 防止 PhotoImage 被回收

        self._build_ui()
        self._fill_tasks()
        self._poll()

    # ---- UI 构建 -------------------------------------------------------

    def _build_ui(self):
        top = ttk.Frame(self.root)
        top.pack(fill="x", padx=8, pady=6)
        self.btn_all = ttk.Button(top, text="▶ 运行全部", command=self._run_all)
        self.btn_sel = ttk.Button(top, text="▶ 运行选中", command=self._run_checked)
        self.btn_stop = ttk.Button(top, text="■ 停止", command=self._stop, state="disabled")
        self.btn_all.pack(side="left")
        self.btn_sel.pack(side="left", padx=6)
        self.btn_stop.pack(side="left")
        self.status_var = tk.StringVar(value="待机（游戏需已由人工启动）")
        ttk.Label(top, textvariable=self.status_var).pack(side="right")

        main = ttk.Frame(self.root)
        main.pack(fill="both", expand=True, padx=8)

        left = ttk.Frame(main)
        left.pack(side="left", fill="both", expand=True)

        cols = ("id", "name", "status")
        self.tree = ttk.Treeview(left, columns=cols, show="headings", height=7)
        for cid, text, w in [("id", "任务 ID", 180), ("name", "名称", 140), ("status", "状态", 90)]:
            self.tree.heading(cid, text=text)
            self.tree.column(cid, width=w, anchor="w")
        self.tree.pack(fill="x")

        log_box = ttk.LabelFrame(left, text="日志")
        log_box.pack(fill="both", expand=True, pady=(6, 0))
        self.log_text = scrolledtext.ScrolledText(
            log_box, height=16, state="disabled", font=("Consolas", 9), wrap="word"
        )
        self.log_text.pack(fill="both", expand=True)

        right = ttk.LabelFrame(main, text="当前画面")
        right.pack(side="right", fill="y", padx=(8, 0))
        self.preview = ttk.Label(right)
        self.preview.pack(padx=4, pady=4)

    def _fill_tasks(self):
        for t in load_tasks():
            self.tree.insert("", "end", iid=t["id"], values=(t["id"], t.get("name", ""), "待机"))

    # ---- 日志 / 预览队列 ------------------------------------------------

    def log(self, msg: str):
        self.log_q.put(str(msg))

    def _poll(self):
        try:
            while True:
                msg = self.log_q.get_nowait()
                if msg == "__DONE__":
                    self._on_worker_done()
                    continue
                self.log_text.config(state="normal")
                self.log_text.insert("end", msg + "\n")
                self.log_text.see("end")
                self.log_text.config(state="disabled")
        except queue.Empty:
            pass
        try:
            frame = self.frame_q.get_nowait()
            img = Image.fromarray(frame[:, :, ::-1]).resize((PREVIEW_W, PREVIEW_H))
            self._photo = ImageTk.PhotoImage(img)
            self.preview.config(image=self._photo)
        except queue.Empty:
            pass
        self.root.after(120, self._poll)

    # ---- 启停 -----------------------------------------------------------

    def _checked_ids(self) -> list[str]:
        sel = self.tree.selection()
        return list(sel) if sel else []

    def _start(self, ids: list[str]):
        if not ids:
            self.status_var.set("没有选择任务")
            return
        if self.worker and self.worker.is_alive():
            return
        self.stop_event.clear()
        self.btn_stop.config(state="normal")
        self.btn_all.config(state="disabled")
        self.btn_sel.config(state="disabled")
        for tid in ids:
            self.tree.set(tid, "status", "排队")
        self.status_var.set(f"运行中: {', '.join(ids)}")
        self.worker = threading.Thread(target=self._work, args=(ids,), daemon=True)
        self.worker.start()

    def _run_all(self):
        self._start([t["id"] for t in load_tasks()])

    def _run_checked(self):
        self._start(self._checked_ids())

    def _stop(self):
        self.stop_event.set()
        self.status_var.set("停止中（等待当前步结束）…")

    def _work(self, ids: list[str]):
        def on_result(r: dict):
            try:
                self.tree.set(r["task"], "status", r["status"])
            except Exception:
                pass

        def log_and_mark(msg: str):
            self.log(msg)
            # "[状态] task_id steps=..." 形式的结果行 → 更新列表状态列
            if msg.startswith("[") and "] " in msg:
                try:
                    status, rest = msg[1:].split("]", 1)
                    tid = rest.split(" ")[0].strip()
                    self.tree.set(tid, "status", status)
                except Exception:
                    pass

        try:
            results = run_selected(
                ids,
                log=log_and_mark,
                stop_event=self.stop_event,
                frame_cb=lambda f: self.frame_q.put(f),
            )
            self.log("===== 汇总 =====")
            self.log(json.dumps(results, ensure_ascii=False, indent=1))
        except Exception as e:  # 设备错误等
            self.log(f"[异常] {e.__class__.__name__}: {e}")
        finally:
            self.log_q.put("__DONE__")

    def _on_worker_done(self):
        self.btn_stop.config(state="disabled")
        self.btn_all.config(state="normal")
        self.btn_sel.config(state="normal")
        self.status_var.set("待机")


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
