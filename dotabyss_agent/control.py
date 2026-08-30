"""控制面：GUI 承载引擎，CLI/脚本经 localhost HTTP 附着同一引擎实例。

设计见 docs/research/13-控制面与BepInEx桥.md §1。要点：
- 服务端 ControlServer 在宿主进程内起 ThreadingHTTPServer（127.0.0.1 随机端口），
  命令路由到宿主给的 handler 表（name -> fn(params) -> dict）；发现信息
  {port, pid, token} 写 .local/ctl.json，宿主退出时删除。
- 客户端 ctl_request() 读发现文件发请求；引擎未运行返回 (False, {"error":
  "no-engine"})，由调用方决定回退策略（CLI 的 run 回退独立直跑）。
- 本模块不感知 Qt：handler 在 HTTP 线程执行，涉及 GUI 控件的操作由宿主自行
  marshal（gui.call_in_gui 经 RunSignals.ctl_call 投递）。
"""
import json
import os
import secrets
import threading
from collections import deque  # noqa: F401  (宿主侧日志缓冲用，放这里仅为文档一致性)
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import urllib.error
import urllib.request

from .config import LOCAL_DIR

CTL_FILE = LOCAL_DIR / "ctl.json"
SHOTS_DIR = LOCAL_DIR / "ctl_shots"


class CtlError(RuntimeError):
    """业务错误：服务端转成 ok=False 的 JSON 响应（区别于 500 内部错误）。"""


def save_frame(img, path) -> str:
    """BGR ndarray 存 PNG（imencode 走显式路径写，避开中文路径 imwrite 的坑）。"""
    import cv2

    ok, buf = cv2.imencode(".png", img)
    if not ok:
        raise RuntimeError("PNG 编码失败")
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(buf.tobytes())
    return str(p)


class ControlServer:
    """宿主进程内嵌的命令服务。routes: {name: fn(params)->dict}。"""

    def __init__(self, routes: dict[str, callable]):
        self._routes = routes
        self._token = secrets.token_hex(16)
        self._http: ThreadingHTTPServer | None = None

    def start(self) -> int:
        """启动并写发现文件，返回实际端口。"""
        LOCAL_DIR.mkdir(exist_ok=True)
        self._http = ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        port = self._http.server_address[1]
        threading.Thread(
            target=self._http.serve_forever, name="ctl-server", daemon=True
        ).start()
        CTL_FILE.write_text(
            json.dumps({"port": port, "pid": os.getpid(), "token": self._token})
        )
        return port

    def shutdown(self) -> None:
        if self._http is not None:
            self._http.shutdown()
            self._http.server_close()
            self._http = None
        try:
            CTL_FILE.unlink()
        except FileNotFoundError:
            pass

    # ---- HTTP 层 -------------------------------------------------------

    def _make_handler(self):
        srv = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # 静默访问日志
                pass

            def do_GET(self):
                if self.path == "/ping":
                    self._json(200, {"pong": True, "pid": os.getpid()})
                else:
                    self._json(404, {"ok": False, "error": "not found"})

            def do_POST(self):
                name = self.path.strip("/").rsplit("/", 1)[-1]
                if self.headers.get("X-Ctl-Token") != srv._token:
                    self._json(403, {"ok": False, "error": "token 不匹配（发现文件已过期？）"})
                    return
                fn = srv._routes.get(name)
                if fn is None:
                    self._json(404, {"ok": False, "error": f"未知命令: {name}"})
                    return
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    params = json.loads(self.rfile.read(length) or b"{}")
                except json.JSONDecodeError:
                    self._json(400, {"ok": False, "error": "JSON 解析失败"})
                    return
                try:
                    self._json(200, {"ok": True, **(fn(params) or {})})
                except CtlError as e:
                    self._json(200, {"ok": False, "error": str(e)})
                except Exception as e:
                    self._json(500, {"ok": False, "error": f"{e.__class__.__name__}: {e}"})

            def _json(self, code: int, obj: dict):
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler


def ctl_request(cmd: str, params: dict | None = None, timeout: float = 15.0) -> tuple[bool, dict]:
    """客户端单次命令。返回 (ok, data)；引擎未运行 → (False, {"error": "no-engine"})。"""
    try:
        info = json.loads(CTL_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return False, {"error": "no-engine"}
    req = urllib.request.Request(
        f"http://127.0.0.1:{info['port']}/{cmd}",
        data=json.dumps(params or {}).encode("utf-8"),
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "X-Ctl-Token": info.get("token", ""),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # 4xx/5xx 也带 JSON 错误体（未知命令/token/内部错误），原样透传，
        # 不能落进下面的 URLError 分支被误判成 no-engine
        try:
            data = json.loads(e.read().decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            data = {"error": f"HTTP {e.code}"}
        return bool(data.get("ok")), data
    except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
        return False, {"error": f"no-engine ({e.__class__.__name__})"}
    except json.JSONDecodeError:
        return False, {"error": "no-engine (响应非 JSON)"}
    return bool(data.get("ok")), data
