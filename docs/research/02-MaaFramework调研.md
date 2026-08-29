# 02 · MaaFramework 调研

> 调研日期：2026-08-29 ｜ 源码：`_refs/MaaFramework`（main 分支快照）｜ [GitHub](https://github.com/MaaXYZ/MaaFramework)

## 1. 结论先行

**MaaFramework 完全支持原生 Windows 游戏，并非只面向安卓模拟器。** 它可以作为本项目"设备层"（截图 + 输入 + 窗口管理）直接使用，其图像识别流水线我们用不到的部分可以整体绕开，只把 LLM 决策接进自定义识别/动作接口。原计划（以 MAA 为框架）可行，且比自研设备层省大量精力。

## 2. 架构组成（与本项目相关的部分）

```
MaaFramework (C++ 核心)
├── ControllerAgent ── 设备层统一抽象（截图/点击/滑动/滚动/按键/通知回调）
│   ├── MaaAdbControlUnit        （安卓/模拟器 —— 本项目不用）
│   ├── MaaWin32ControlUnit      （★ 原生 Windows 窗口 —— 本项目核心依赖）
│   ├── MaaCustomControlUnit     （自定义设备接入）
│   └── MaaAgentClient/Server    （跨进程自定义识别/动作，Python/NodeJS 绑定）
├── Resource + Tasker            （图像识别流水线引擎 —— 本项目基本不用）
└── Toolkit                      （窗口枚举/查找等实用工具）
```

## 3. Win32 控制单元能力（`MaaWin32ControlUnit`）

### 3.1 截图方式（`docs/zh_cn/2.4-控制方式说明.md` + `MaaDef.h`）

| 方式 | 速度 | 后台可用 | 说明 |
| --- | --- | --- | --- |
| GDI | 快 | 否 | |
| **FramePool**（Windows.Graphics.Capture） | 极快 | **是** | 需 Win10 1903+；**内置伪最小化**（最小化时透明+点击穿透恢复，不打扰用户） |
| DXGI_DesktopDup(_Window) | 极快 | 否 | 全屏复制后裁剪 |
| **PrintWindow** | 中 | **是** | 兼容性一般；内置伪最小化 |
| ScreenDC | 快 | 否 | 兼容性最高 |

- 官方预置组合：`Foreground = DesktopDup_Window|ScreenDC`，`Background = FramePool|PrintWindow`。
- 传入按位或组合后 MAA **自动测速并选用最快可用者**。
- 伪最小化：FramePool/PrintWindow 在窗口最小化时会临时"透明+免激活还原"再截图，用户无感。

### 3.2 输入方式（选一，鼠标/键盘可分别指定）

| 方式 | 后台可用 | 抢占鼠标 | 说明 |
| --- | --- | --- | --- |
| Seize | 否 | 是 | 前台 SendInput，兼容性最高 |
| SendMessage / PostMessage（含 WithCursorPos / WithWindowPos 变体） | 是 | 否 | 向窗口投递消息；WithWindowPos 会短暂挪窗口对齐光标后还原 |
| **AnchoredTouch** | 是 | 否 | `InjectSyntheticPointerInput` 合成触点 → 目标窗口收到 WM_POINTER；全程不动光标、不改前台；被遮挡时短暂提窗（约 70ms）。**文档明确：Unity 窗口带 CS_OWNDC，实测可用**（Win10 1809+） |
| Interception | 否 | 否 | 驱动级注入，需管理员，常规注入失效时用 |
| LegacyEvent / PostThreadMessage | — | — | 低兼容/已废弃 |

另有 **鼠标锁定跟随模式**（TPS/FPS 后台锁鼠标场景，本项目用不到）和 **后台受管键守护**。

> 对 DotAbyss 的实测结论（详见 [06-M0-POC记录](06-M0-POC记录.md)）：前台 Seize 点击 ✅；FramePool 后台截图 ✅；**PostMessage/SendMessage 失焦点击 ❌（Unity 焦点门控）**；最小化时伪最小化截图 ❌（全黑，引擎停渲染）；**同进程只允许一个控制器实例（多建 segfault）**；另注意 `post_connection()` 必须调用否则截图单元为 null；pip 版 5.12.3 尚无 AnchoredTouch（主分支才有）。

### 3.3 Python 绑定（`source/binding/python`，PyPI 包 MaaFw）

```python
from maa.toolkit import Toolkit
from maa.controller import Win32Controller

Toolkit.init_option("./")
wins = Toolkit.find_desktop_windows()
hwnd = next(w.hwnd for w in wins if "ドットアビスX" in w.window_name)

ctrl = Win32Controller(
    hwnd,
    screencap_method=MaaWin32ScreencapMethodEnum.Background,   # FramePool|PrintWindow
    mouse_method=MaaWin32InputMethodEnum.PostMessage,          # M0 实测后定
)
ctrl.post_screencap().wait()          # → cached_image 拿到 numpy 图
ctrl.post_click(x, y).wait()          # contact: 0左键 1右键 2中键
ctrl.post_swipe(...); ctrl.post_scroll(...); ctrl.post_key(...)
```

- `AgentServer`（`maa/agent/agent_server.py`）支持注册 **自定义识别器 / 自定义动作**（Python 类），可把"LLM 决策"注册为一个识别节点，嵌进 MAA 流水线，与其他确定性节点混排。
- 支持回调/通知（`event_sink`、回调协议 `2.3-回调协议.md`），方便做 GUI/日志/推送。

## 4. 两种集成姿势（架构分叉点）

| 姿势 | 做法 | 适合 |
| --- | --- | --- |
| A. 纯控制器 | 只用 `Win32Controller` 的截图+输入 API，LLM 循环完全自己写 | 最轻量，逻辑全部自主可控 —— **推荐主力姿势** |
| B. 混合流水线 | 确定性步骤用 MAA pipeline（模板匹配节点），LLM 注册为 CustomRecognition 处理难定位的节点 | 锚点库积累起来之后，把稳定路径固化提速 |

两者不冲突，可 A 起步、逐步长出 B。

## 5. 风险与成本

- MAA 迭代快，注意锁定版本（binding 与核心 dll 版本需匹配）。
- 无重大风险：MAA 系（明日方舟助手等）长期在生产环境验证 Win32 路径。
- 我们不用的部分（流水线协议、ADB）不构成负担，按需加载。
