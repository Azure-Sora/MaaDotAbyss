"""BepInEx 桥设备后端（docs/research/13 §2）：经游戏内插件 HTTP 直控游戏。

与 GameDevice（MAA 前台 Seize）接口语义对齐，但：
- 输入零焦点依赖：游戏内直接 onClick.Invoke（click=按坐标找按钮 / click_by_path=按路径）；
- 截图进程内直读（窗口可遮挡；最小化后 Unity 停渲染依旧黑帧，与引擎行为一致）。
桥未在线（游戏没开/插件未装载）时 bridge_info() 返回 None，由调用方决定回退 MAA 路径。
"""
import json
import time
import urllib.error
import urllib.request

import cv2
import numpy as np

from .config import GAME_DIR

BRIDGE_JSON = GAME_DIR / "BepInEx" / "bridge.json"
DEFAULT_PORT = 27124


class BridgeError(RuntimeError):
    pass


def bridge_info(timeout: float = 1.5) -> dict | None:
    """发现桥：优先 bridge.json 记录的端口，回退默认端口。在线返回 info（含 port）。"""
    ports = []
    try:
        ports.append(int(json.loads(BRIDGE_JSON.read_text(encoding="utf-8"))["port"]))
    except Exception:
        pass
    if DEFAULT_PORT not in ports:
        ports.append(DEFAULT_PORT)
    for port in ports:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
            if data.get("pong"):
                return {"port": port, **data}
        except Exception:
            continue
    return None


def bridge_post(path: str, params: dict | None = None, port: int = DEFAULT_PORT,
                timeout: float = 20.0) -> dict:
    """POST /命令 + JSON → JSON。桥侧业务错误（HTTPError 带 error 体）转 BridgeError。"""
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/{path}",
        data=json.dumps(params or {}).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            data = json.loads(e.read().decode("utf-8"))
        except Exception:
            data = {}
        raise BridgeError(data.get("error", f"HTTP {e.code}")) from None
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise BridgeError(f"桥连接失败: {e.__class__.__name__}") from None


def bridge_post_bytes(path: str, params: dict | None = None, port: int = DEFAULT_PORT,
                      timeout: float = 30.0) -> bytes:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/{path}",
        data=json.dumps(params or {}).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        try:
            data = json.loads(e.read().decode("utf-8"))
        except Exception:
            data = {}
        raise BridgeError(data.get("error", f"HTTP {e.code}")) from None
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise BridgeError(f"桥连接失败: {e.__class__.__name__}") from None


class BridgeDevice:
    """GameDevice 的桥后端等价物：感知/动作/等待全走游戏内桥，永不抢焦点。"""

    def __init__(self, info: dict | None = None):
        self.info = info or bridge_info()
        if self.info is None:
            raise BridgeError("BepInEx 桥未在线（游戏未启动或插件未装载）")
        self.port = int(self.info["port"])
        self.pid = self.info.get("pid")

    def __repr__(self):
        return f"<BridgeDevice port={self.port} pid={self.pid}>"

    # ---- 感知 ---------------------------------------------------------

    def screenshot(self) -> np.ndarray:
        """整屏 PNG（桥进程内回读）→ BGR ndarray。场景切换瞬间渲染纹理可能
        暂空（"截图纹理为空"），短重试渡过。"""
        last: Exception | None = None
        for _ in range(4):
            try:
                png = bridge_post_bytes("screenshot", port=self.port)
                img = cv2.imdecode(np.frombuffer(png, dtype=np.uint8), cv2.IMREAD_COLOR)
                if img is None:
                    raise BridgeError("截图 PNG 解码失败")
                return img
            except BridgeError as e:
                if "纹理为空" not in str(e):
                    raise
                last = e
                time.sleep(1.0)
        raise last or BridgeError("截图失败")

    def ui_tree(self, canvas: str | None = None, max_nodes: int = 4000) -> dict:
        """当前场景 Canvas 层级 JSON（节点名/坐标/尺寸/TMP文本/Button 路径）。

        canvas: 只导出同名 Canvas（如 "MapCanvas"——深渊地图画布节点上万，
        全量导出会撞默认 4000 上限）；max_nodes: 节点上限（v0.2.0+ 支持）。
        旧插件忽略参数，行为等同全量 4000。
        """
        params = {}
        if canvas:
            params["canvas"] = canvas
        if max_nodes != 4000:
            params["max_nodes"] = max_nodes
        return bridge_post("ui", params, port=self.port)

    def click_ui(self, x: int, y: int) -> str:
        """射线式真实点击（v0.2.0+）：RaycastAll 最上层对象 → IPointerClickHandler。

        弹窗按钮普遍不是 Button 组件（ゲットキー消費实测），click_at 只认 Button
        会穿透弹窗点到下层，弹窗一律用本方法。
        """
        r = bridge_post("click_ui", {"x": int(x), "y": int(y)}, port=self.port)
        if not r.get("clicked"):
            raise BridgeError(r.get("error", "点击失败"))
        return r.get("path", "")

    # ---- 动作（零焦点） -------------------------------------------------

    def is_foreground(self) -> bool:
        return True  # 桥输入不依赖焦点，恒真

    def bring_to_front(self) -> None:
        pass

    def click(self, x: int, y: int) -> str:
        """点 (x,y) 下面积最小的可交互按钮（onClick.Invoke），返回命中路径。"""
        r = bridge_post("click_at", {"x": int(x), "y": int(y)}, port=self.port)
        if not r.get("clicked"):
            raise BridgeError(r.get("error", "点击失败"))
        return r.get("path", "")

    def click_by_path(self, path: str) -> str:
        """按 UI 路径（或按钮名包含匹配）触发 onClick，返回实际路径。"""
        r = bridge_post("click", {"path": path}, port=self.port)
        return r.get("path", "")

    def swipe(self, *args, **kwargs) -> None:
        raise BridgeError("桥后端暂不支持 swipe（需游戏侧拖拽映射，doc 13 §2.4）")

    # ---- 等待（与 GameDevice 同语义，帧源换成桥截图） -------------------

    def diff_ratio(self, a: np.ndarray, b: np.ndarray) -> float:
        d = np.abs(a.astype(np.int16) - b.astype(np.int16)).max(axis=2)
        return float((d > 12).mean())

    def wait_settled(self, ref_frame: np.ndarray, big_change: float = 0.05,
                     max_wait: float = 8.0) -> np.ndarray:
        t0 = time.time()
        last = ref_frame
        last_change = time.time()
        cur = ref_frame
        while time.time() - t0 < max_wait:
            time.sleep(0.6)
            cur = self.screenshot()
            if self.diff_ratio(last, cur) > big_change:
                last = cur
                last_change = time.time()
            elif time.time() - last_change >= 1.2:
                break
        return cur

    def wait_until_stable(self, quiet_seconds: float = 2.0, threshold: float = 0.01,
                          timeout: float = 180.0, poll: float = 0.7) -> bool:
        prev = self.screenshot()
        stable_since: float | None = None
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(poll)
            cur = self.screenshot()
            if self.diff_ratio(prev, cur) < threshold:
                stable_since = stable_since or time.time()
                if time.time() - stable_since >= quiet_seconds:
                    return True
            else:
                stable_since = None
            prev = cur
        return False
