"""控制面回环测试（无 Qt/无游戏）：ControlServer + ctl_request 全命令。

覆盖：start 写发现文件、status、run/busy/结果回读、screenshot 存 PNG、
logs tail、未知命令 404、token 篡改 403、stop、关停清理发现文件。
跑法: python poc/ctl_loop_test.py
"""
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotabyss_agent import control
from dotabyss_agent.control import CTL_FILE, CtlError, ControlServer

state = {"worker": None, "stop": threading.Event(), "results": []}


def cmd_status(params):
    return {"running": bool(state["worker"] and state["worker"].is_alive()),
            "results": list(state["results"])}


def cmd_run(params):
    if state["worker"] and state["worker"].is_alive():
        raise CtlError("busy")

    def work():
        time.sleep(0.3)
        state["results"].append({"task": params["task_ids"][0], "status": "done"})

    state["worker"] = threading.Thread(target=work, daemon=True)
    state["worker"].start()
    return {"started": params["task_ids"]}


def cmd_stop(params):
    state["stop"].set()
    return {"stopping": True}


def cmd_screenshot(params):
    img = np.zeros((6, 8, 3), dtype=np.uint8)
    img[:] = (80, 160, 240)
    out = params.get("out") or str(control.SHOTS_DIR / "poc_shot.png")
    path = control.save_frame(img, out)
    return {"path": path, "width": 8, "height": 6}


def cmd_logs(params):
    lines = [f"line-{i}" for i in range(10)]
    return {"lines": lines[-max(1, int(params.get("tail", 50))):]}


routes = {"status": cmd_status, "run": cmd_run, "stop": cmd_stop,
          "screenshot": cmd_screenshot, "logs": cmd_logs}


def expect(cond, msg):
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)
    print(f"  ok: {msg}")


def main():
    srv = ControlServer(routes)
    port = srv.start()
    expect(CTL_FILE.exists(), "发现文件已写")
    info = json.loads(CTL_FILE.read_text(encoding="utf-8"))
    expect(info["port"] == port, "发现文件端口一致")

    ok, data = control.ctl_request("status")
    expect(ok and data["running"] is False, "status 空闲")

    ok, data = control.ctl_request("run", {"task_ids": ["t1"]})
    expect(ok and data["started"] == ["t1"], "run 启动")
    ok, data = control.ctl_request("run", {"task_ids": ["t2"]})
    expect(not ok and "busy" in data.get("error", ""), "忙时拒绝")
    time.sleep(0.4)
    ok, data = control.ctl_request("status")
    expect(ok and not data["running"] and data["results"], "运行结束 + 结果回读")

    ok, data = control.ctl_request("screenshot")
    expect(ok and Path(data["path"]).exists() and data["width"] == 8, "screenshot 存图")

    ok, data = control.ctl_request("logs", {"tail": 3})
    expect(ok and data["lines"] == ["line-7", "line-8", "line-9"], "logs tail=3")

    ok, data = control.ctl_request("nope")
    expect(not ok and "未知命令" in data.get("error", ""), "未知命令 404")

    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/status", data=b"{}",
        headers={"X-Ctl-Token": "bad"}, method="POST")
    try:
        urllib.request.urlopen(req, timeout=5)
        expect(False, "token 篡改应 403")
    except urllib.error.HTTPError as e:
        expect(e.code == 403, "token 篡改 → 403")

    ok, data = control.ctl_request("stop")
    expect(ok and data["stopping"], "stop")

    srv.shutdown()
    expect(not CTL_FILE.exists(), "退出清理发现文件")
    ok, data = control.ctl_request("status")
    expect(not ok and data.get("error") == "no-engine", "关停后 no-engine")

    print("\nALL PASS")


if __name__ == "__main__":
    main()
