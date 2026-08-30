"""桥设备后端回环测试：用假桥服务（http.server）验证 device_bridge.py 客户端逻辑。

不依赖游戏/真实插件。覆盖：发现文件 → ping、screenshot 解码、click_at 成功/失败、
click_by_path、ui_tree、swipe 拒绝、no-engine 判定。
跑法: python poc/bridge_mock_test.py
"""
import json
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import dotabyss_agent.device_bridge as db
from dotabyss_agent.device_bridge import BridgeDevice, BridgeError, bridge_info

STATE = {"clicks": [], "buttons": True}


class MockBridge(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/ping":
            body = json.dumps({"pong": True, "pid": 424242, "unity": "6000.3.8f1",
                               "product": "ドットアビスX", "plugin": "0.1.0", "focused": True}).encode()
            self.send_response(200)
        else:
            body = b'{"error": "not found"}'
            self.send_response(404)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        params = json.loads(self.rfile.read(length) or b"{}")
        cmd = self.path.strip("/")
        if cmd == "screenshot":
            img = np.zeros((72, 128, 3), np.uint8)
            img[:] = (40, 90, 200)
            ok, png = cv2.imencode(".png", img)
            body, ctype = png.tobytes(), "image/png"
            self.send_response(200)
        elif cmd == "click_at":
            STATE["clicks"].append(params)
            if STATE["buttons"]:
                body = json.dumps({"clicked": True, "path": "/Canvas/Home/StartBtn"}).encode()
                self.send_response(200)
            else:
                body = json.dumps({"error": f"({params['x']},{params['y']}) 下没有可交互按钮"}).encode()
                self.send_response(404)
        elif cmd == "click":
            if params.get("path") == "/Canvas/Home/StartBtn":
                body = json.dumps({"clicked": True, "path": params["path"]}).encode()
                self.send_response(200)
            else:
                body = json.dumps({"error": f"未找到按钮: {params.get('path')}"}).encode()
                self.send_response(404)
        elif cmd == "ui":
            body = json.dumps({"scene": "Home", "canvases": [{"name": "Canvas", "children": []}]}).encode()
            self.send_response(200)
        else:
            body = json.dumps({"error": "未知命令: " + cmd}).encode()
            self.send_response(404)
        self.send_header("Content-Type", "application/json; charset=utf-8" if cmd != "screenshot" else "image/png")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def expect(cond, msg):
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)
    print(f"  ok: {msg}")


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", 27125), MockBridge)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    # 发现：monkeypatch 发现文件指向 mock 端口
    tmpdir = tempfile.mkdtemp()
    db.BRIDGE_JSON = Path(tmpdir) / "bridge.json"
    db.BRIDGE_JSON.write_text(json.dumps({"port": 27125, "pid": 424242}), encoding="utf-8")

    info = bridge_info()
    expect(info is not None and info["port"] == 27125 and info["pid"] == 424242, "bridge_info 经发现文件命中")

    dev = BridgeDevice()
    expect(dev.pid == 424242 and dev.is_foreground(), "BridgeDevice 构建 + is_foreground 恒真")

    img = dev.screenshot()
    expect(isinstance(img, np.ndarray) and img.shape == (72, 128, 3), "screenshot 解码 BGR")

    path = dev.click(100, 200)
    expect(path == "/Canvas/Home/StartBtn" and STATE["clicks"][-1] == {"x": 100, "y": 200}, "click → click_at")
    try:
        STATE["buttons"] = False
        dev.click(5, 5)
        expect(False, "无可点按钮应报错")
    except BridgeError as e:
        expect("没有可交互按钮" in str(e), "无可点按钮 → BridgeError")
    finally:
        STATE["buttons"] = True

    expect(dev.click_by_path("/Canvas/Home/StartBtn") == "/Canvas/Home/StartBtn", "click_by_path 命中")
    try:
        dev.click_by_path("/nope")
        expect(False, "未知路径应报错")
    except BridgeError as e:
        expect("未找到按钮" in str(e), "未知路径 → BridgeError")

    tree = dev.ui_tree()
    expect(tree["scene"] == "Home", "ui_tree")

    try:
        dev.swipe(1, 2, 3, 4)
        expect(False, "swipe 应拒绝")
    except BridgeError:
        expect("swipe 拒绝", "")

    # 无桥：发现文件指向死端口且默认端口无服务 → None
    db.BRIDGE_JSON.write_text(json.dumps({"port": 27999, "pid": 1}), encoding="utf-8")
    expect(bridge_info(timeout=0.5) is None, "无桥 → bridge_info None")

    srv.shutdown()
    print("\nALL PASS")


if __name__ == "__main__":
    main()
