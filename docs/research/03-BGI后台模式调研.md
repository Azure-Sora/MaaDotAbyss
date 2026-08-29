# 03 · BetterGenshinImpact 调研：原生 Win 截图/输入与"桌面分身"后台模式

> 调研日期：2026-08-29 ｜ 源码：`_refs/better-genshin-impact` ｜ [GitHub](https://github.com/babalae/better-genshin-impact) ｜ 子会话相关讨论：[issue #3409](https://github.com/babalae/better-genshin-impact/issues/3409)

## 1. 结论先行

BGI 的新后台模式（"桌面分身"）= **Windows RDP 子会话（Child Session）**：
开启子会话 → 在这个"隐形分身桌面"里以管理员启动游戏 → 游戏在分身里**是真前台**，全速渲染 → 主桌面通过 RDP loopback 镜像它的画面（窗口可隐藏，连接保持）并转发真实键鼠输入。

它从根上绕过了"后台窗口收不到输入/截不到图"这一整类问题——**分身里没有后台这回事**。这是目前 Windows 原生游戏后台自动化最彻底的方案。

## 2. BGI 的后台技术演进（三代）

1. 前台 SendInput（抢占鼠标）。
2. 消息注入后台（PostMessage 系）+ WGC/BitBlt 截图——Unity/米家游戏兼容性一般。
3. **子会话桌面分身**（本次调研重点，`Service/ChildSession/` 全套源码）。

## 3. 子会话机制拆解（源码级）

涉及 API（`wtsapi32.dll`，[微软文档](https://learn.microsoft.com/en-us/windows/win32/api/wtsapi32/nf-wtsapi32-wtsenablechildsessions)，Win8+ 引入，[Child Sessions 概念](https://learn.microsoft.com/en-us/windows/desktop/TermServ/child-sessions)）：

| API | 用途（BGI 用法） |
| --- | --- |
| `WTSEnableChildSessions(TRUE)` | 启用子会话特性（需管理员；`ChildSessionNativeMethods.cs`） |
| `WTSIsChildSessionsEnabled` | 查询是否已启用 |
| `WTSGetChildSessionId` | 拿到子会话 ID（`UINT_MAX` = 不存在） |
| `WTSLogoffSession` | 用完注销子会话 |

完整流程（`ChildSessionService.cs` 879 行 + `ChildSessionProcessLauncher.cs`）：

1. **启用**：管理员下 `WTSEnableChildSessions(true)`。
2. **连接**：主程序窗口内嵌 **RDP ActiveX 控件**（AxMSTSCLib，`View/Controls/ChildSession/RdpActiveXHost.cs`）连 `localhost:3389`（子会话 RDP 回环，自动读取注册表实际端口）。连上即创建并登录子会话（默认桌面 1920×1080）。
3. **拉起进程**：通过 **计划任务 COM 接口**（ITaskService）创建临时任务，参数带 `TASK_RUN_USE_SESSION_ID(0x4)` + `RUNLEVEL_HIGHEST` + 交互式令牌，把第二个 BGI 实例（`--instance childSession`）**以管理员身份发射到子会话里**。这是"往另一个会话里放进程"的标准姿势，普通 CreateProcess 做不到。
4. **协作**：主/子两个 BGI 实例走 `Service/Instance/` 的 IPC（`InstanceRequestHandler`、`RelativeMouseMessageHandler` 等）。
5. **输入**：RDP 控件子窗口里找到标题为 `Input Capture Window` 的输入窗口（`EnumChildWindows`），`SetFocus` 后键盘鼠标经 RDP 协议进入子会话 → **对游戏而言是 100% 真实前台输入**，任何前台/焦点检测都骗不过。
6. **收尾**：断开 RDP + `WTSLogoffSession` 注销。

## 4. 系统要求与坑（来自源码与 issue #3409）

| 项 | 说明 |
| --- | --- |
| 系统版本 | API 自 Win8 起有；实际要求 **Win10+**（Win7/8 无 Child Session） |
| 家庭版 | 无 RDP 服务端 → 需装 [RDP Wrapper](https://github.com/stascorp/rdpwrap)（BGI 代码里会检测 `TermService\Parameters\ServiceDll` 是否为 rdpwrap.dll） |
| 权限 | 启用子会话、提权拉进程都需要管理员 |
| 刷新率 | 子会话 DWM 合成上限约 60fps（对放置类游戏无影响） |
| 显示 | RDP 连接需保持（窗口可隐藏，"已隐藏 BetterGI 桌面分身，RDP 连接保持不变"）；断开 RDP 后子会话桌面的合成行为需实测 |
| 资源 | 两会话共享 GPU/CPU；子会话音频可静音 |

## 5. 对本项目的可复用性评估

### 5.1 直接搬 BGI 代码的障碍

BGI 是 C#/WPF，子会话模块强耦合其 DI/窗口体系。若我们主体用 Python，全部移植不划算；**但核心机制只有三个原子操作**，移植成本低：

1. 三个 WTS API 调用（ctypes 十几行）；
2. RDP ActiveX 连接（Python 无现成 ActiveX 宿主——但见 5.2，可以完全绕开）；
3. 计划任务 COM 拉进程（Python 可用 `comtypes` 走 ITaskService，或干脆生成一个 XML 计划任务交给 `schtasks.exe` 注册）。

### 5.2 更优的落地方案："Agent 跑进分身里"（BGI 架构的简化）

BGI 需要 RDP 镜像是因为它的主程序/用户要看画面。我们的 Agent 不需要人看：

```
主会话（用户的桌面）                子会话（桌面分身）
┌─────────────────┐   计划任务    ┌──────────────────────────┐
│ Runner: 启用子会话│ ──────────→ │ Agent(Python) + 游戏       │
│ 发射/停止/收状态  │  HTTP/文件   │ 游戏在分身里是真前台：        │
│ （可选：RDP 预览窗）│ ←────────── │  截图 = WGC/PrintWindow     │
└─────────────────┘              │  输入 = SendInput(Seize)    │
                                 │  全部走 MAA Win32Controller │
                                 └──────────────────────────┘
```

- 分身内游戏是前台 → **不需要任何消息注入技巧**，Seize/SendInput 就是最稳路径；截图也随便挑。
- 状态回传用本地 HTTP / 落盘日志即可；主会话要不要"看"游戏画面，挂一个可选的 RDP 预览窗（复刻 BGI）或干脆靠 Agent 存的截图流。
- 兼容性顺序建议：**普通后台（FramePool+PostMessage，M0 验证）→ 子会话分身（M3）**。若 M0 实测 PostMessage 对该 Unity 游戏生效，普通后台可能已经够用，子会话作为"终极稳态"保留。

## 6. 顺带可借鉴的 BGI 资产

- 捕获/输入库（WGC 封装、SendInput 封装）：`Core/`、`Fischless.*` 命名空间，C# 参考。
- "伪最小化"同类技巧 MAA 已内置，无需重复造。
- 任务编排/通知体系的 UI 思路（若将来做托盘 GUI）。
