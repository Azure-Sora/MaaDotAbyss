"""设备后端选择（docs/research/13 §2.6 第 7 项）：桥在线优先，MAA 前台兜底。

桥后端 = 零焦点输入 + 进程内截图（窗口可遮挡）；MAA 后端 = Seize 前台输入 +
FramePool 截图。选择只做"在线探测"，不做能力协商——两后端接口语义对齐。
"""
from .config import GAME_TITLE


def get_device(prefer_bridge: bool = True) -> tuple[object, str]:
    """返回 (device, backend)；backend ∈ {"bridge", "maa"}。

    优先尝试 BepInEx 桥（游戏内插件，零焦点）；桥未在线（游戏没开/插件未装载）
    或需要 MAA 时，回退绑定游戏窗口的 MAA 控制器（游戏未启动会抛 DeviceError）。
    """
    if prefer_bridge:
        try:
            from .device_bridge import BridgeDevice

            return BridgeDevice(), "bridge"
        except Exception:
            pass  # 桥未在线 → MAA 路径（其报错信息更贴"请先启动游戏"）
    from .device import GameDevice

    return GameDevice(), "maa"
