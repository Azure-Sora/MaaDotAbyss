# 13 · 控制面（GUI/CLI 联动）与真后台主路线：BepInEx 桥

> 2026-08-30 ｜ 用户发起讨论定稿：真后台主路线改选 BepInEx 桥（子会话分身降为兜底）；控制面（GUI/CLI 联动）先行实施 ｜ 状态：控制面已落地✅；桥插件已编译部署✅，**待游戏内验证**（用户暂无法开游戏）

## 0. 决策记录（2026-08-30 与用户确认）

1. **真后台主路线 = BepInEx 桥**；BGI 子会话桌面分身（doc 03）降为兜底。
   - 关键变量：**启动链 = DMM Game Player + 日本 IP**，用户手操启动都很麻烦 → 把整条启动链搬进子会话自动化，不确定性最大；而 BepInEx 桥对启动链零成本——手操照旧，游戏进程一起来桥就加载，工具 attach。
   - 技术上限也更高：输入 = 进程内直调 uGUI（不依赖坐标/焦点，find 可查对象树）；窗口可最小化（进程内截图可解除 doc 06 的"不可最小化"约束）。
   - 利好：游戏已装 BepInEx 6 + 汉化 Mod（宿主环境现成，汉化 Mod 即活例）；**用户实测 DMMGP 更新游戏不会清掉 BepInEx，更新后仍自动注入**（原顾虑的运维成本不存在）。
   - 环境确认：本机 Windows Pro + 原生 RDP 服务端在线（termsrv.dll，3389 监听中），子会话兜底无系统门槛（家庭版才需要 RDP Wrapper）。
2. **GUI/CLI 统一 = 控制面先行**（本期实施）：GUI 进程承载引擎 + 内嵌 localhost 控制接口；CLI 变 `ctl` 附着客户端操作同一引擎；GUI 未开时 CLI 保持独立直跑（无人值守场景不受影响）。
   - 顺带消除隐患：GUI/CLI 独立跑 = 两套引擎各建 MAA 控制器抢同一游戏窗（M0 铁律：一进程一控制器）。

## 1. 控制面设计

### 1.1 架构

```
GUI 进程（引擎宿主）                          CLI（附着客户端，一条命令一进程）
┌──────────────────────────────────┐        ┌────────────────────────────┐
│ RunState（信号/停止事件/共享设备）    │        │ ctl status / run / stop /   │
│ TaskPage / MonitorPage / TeachPage │        │    screenshot / logs / quit │
│ GuiCtlAdapter（命令表，HTTP 线程跑） │◄─HTTP──┤ 读 .local/ctl.json 发现端口  │
│ ControlServer（127.0.0.1 随机端口）  │  JSON  │ urllib POST + token 校验    │
└──────────────────────────────────┘        └────────────────────────────┘
```

- 服务端：stdlib `ThreadingHTTPServer`，零新依赖；端口自动挑选，发现信息 `{port, pid, token}` 写 `.local/ctl.json`，宿主退出时删除。
- 客户端：`ctl_request(cmd, params)` 读发现文件 → POST JSON；引擎未运行返回 `{"error": "no-engine"}`，由 CLI 决定回退策略（`ctl run` 回退独立直跑）。
- 线程约定：handler 在 HTTP 线程执行，**不碰 Qt**；涉及 GUI 控件的操作经 `RunSignals.ctl_call` marshal 到 GUI 线程并取回返回值（`call_in_gui`，超时 5s）。
- token：服务端每次启动随机生成，防发现文件过期后打到占端口的无关程序。

### 1.2 命令集 v1

| 命令 | 参数 | 行为 |
| --- | --- | --- |
| `run` | `task_ids[]` 或 `all:true`，`max_steps`、`time_budget`、`provider`、`update_knowledge` | 与 GUI"运行"按钮同路径启动 worker；忙时业务错误 |
| `stop` | — | 置 stop_event（对任务运行与教学会话均生效） |
| `status` | — | `running` / `mode`(task/teach) / `current_ids` / `game_bound` / 最近 results |
| `screenshot` | `out?` | 经共享设备截图存 `.local/ctl_shots/*.png`，返回路径；运行中亦可调（MAA job 队列内部串行，截图只是排队） |
| `logs` | `tail` | GUI 侧同步收集的日志环形缓冲（最近 800 行） |
| `tasks` | — | 任务清单（id/name/flow） |
| `quit` | — | 关闭 GUI（走 closeEvent 正常收尾） |

教学模式（ask_user 交互）暂不接入 ctl——CLI 已有独立 `teach`；后续需要时加 reply 通道。

### 1.3 共享设备与 M0 约束

- `RunState` 持共享 `GameDevice`（懒创建），TaskPage / TeachPage / ctl screenshot 复用同一实例 → **GUI 进程内始终一个控制器**，优于现状（每次 run 新建）。
- screenshot 失败（窗口没了）且引擎空闲时 drop 缓存、下次重建；**运行中绝不重建**（避免并发双控制器 segfault，M0 实测约束）。

### 1.4 测试

- `poc/ctl_loop_test.py`：无 Qt 假引擎回环（服务端/客户端全命令、busy、403、清理发现文件）。
- 真机：GUI 开 → ctl 全命令；游戏未开时 screenshot 应优雅报错而非崩溃。

## 2. BepInEx 桥方案（待 spike）

### 2.1 attach 语义（回答"游戏启动后能把进程转移给后台吗"）

Windows 进程创建即绑死会话，**没有跨会话/跨宿主迁移机制**；但也不需要迁移——BepInEx 装在游戏目录，**无论谁启动游戏（用户手操 / DMMGP / 任何会话）都会随进程加载**。桥插件在游戏进程内开 IPC 服务端，工具 attach 为客户端。"转移进程"的正确姿势是"进程内开后门"。

- 注意：裸进程无法事后注入 IL2CPP（无成熟工具链）；但"装一次 BepInEx，之后每次启动自带"已完全满足需求（汉化 Mod 就是这么工作的）。
- 403 约束不变：游戏报 403 仍只能人工重开（doc 06），桥不解决网络层问题。

### 2.2 桥能带来什么

| 能力 | 现状（MAA 前台 Seize） | 桥之后 |
| --- | --- | --- |
| 输入 | 模板匹配算坐标 → Seize 点击 | 找到按钮对象直接 `onClick.Invoke()`；find 可查对象树，坐标匹配降为兜底 |
| 截图 | FramePool（可遮挡、不可最小化） | 现状保留；可升级进程内 RenderTexture 读回（最小化也解除） |
| 焦点 | 点击需前台 | 零焦点依赖 |
| 确定性 | 依赖识图 | 游戏内状态/对象直读 |

与双速架构（doc 09）的关系：剧本四要素不变，`act` 层多一种"invoke"实现；锚点模板匹配降级为兜底手段。

### 2.3 spike 步骤（全程不碰 DMMGP 启动链）

1. **侦察**：确认 Unity/IL2CPP 版本；Il2CppDumper/Cpp2IL dump 类结构，定位主页按钮类名；参考现有汉化 Mod 的工程结构（绑定写法现成）。
2. **最小插件**：只做两个 RPC——截图回传（RenderTexture 读回）+ 列出当前 UI 层级；IPC 选用游戏内 `HttpListener`（localhost HTTP，与控制面同构，最省事）。
3. 用户手操启动游戏一次，验证桥加载与 RPC 通。
4. **输入通路**：uGUI `Button.onClick.Invoke()` 验证；确认有无必须走 Input 状态的场景。
5. **合流**：桥作为 `GameDevice` 的第二种后端实现（screenshot/click 换实现），上层 agent/flow/剧本零改动。

### 2.4 风险

- IL2CPP interop 门槛（BepInEx 6 IL2CPP + Il2CppInterop），spike 才见真章；游戏大更新改 UI 结构 → 现有"剧本失配降级 LLM"机制兜底。
- DMMGP 更新清 BepInEx 的顾虑已由用户实测排除（§0）。

### 2.5 侦察与实现记录（2026-08-30，未开游戏完成的部分）

**环境**：Unity **6000.3.8f1**（Unity 6.3）；BepInEx 6 IL2CPP（经典 interop 命名：`Il2Cppmscorlib`/`Il2CppSystem` + `UnityEngine.*`）；interop 已生成 199 个程序集；机器有 dotnet SDK 9/10 + net6 目标包 6.0.36（离线可编译）。

**游戏代码地图**（来自 interop 元数据 + Project.dll 字符串堆，全量在 `.local/project_ui_strings.txt`）：
- 游戏主程序集 = `interop/Project.dll`（50MB，Assembly-CSharp 重命名）；`Absl.*` = Noa 客户端框架（DMM SDK 血统：`dmm.games.sdk.*`、`PHPCompat`、`NoaMaster`、`CryptedPrefsIO`）。
- UI 生态：uGUI + TextMeshPro + **Arbor**（节点 FSM）+ UniRx/DOTween/EnhancedScroller/spine/Live2D。
- 架构命名：场景 = `AppSceneBase`/`TopScene`/`SubScene` + `SubSceneManager`；每功能 `View`/`ViewController`/`ViewModel` 三件套；弹窗 = `PopupBase`（OpenAsync/CloseAsync）。
- 功能前缀：主城 = `Project.Home.*`（`TopViewController`/`ButtonViewController`/`BuildingController`/`HomeCamera`）；深渊 = `Project.Nether.*`/`NetherTop.*`；酒馆 = `Tavern`；**抽卡 = `Gacha`（禁区，见 NSFW 约束）**。

**桥插件** `bridge/DotAbyssBridge/`（已编译并部署到 `BepInEx/plugins/DotAbyssBridge/DotAbyssBridge.dll`）：
- HTTP 用 **TcpListener 手写**（绕开 HttpListener 的 URLACL 权限坑）；所有 Unity API 经主线程任务队列执行（HTTP 线程只做 IO）。
- 截图 = `ScreenCapture.CaptureScreenshotAsTexture()` + 等 2 帧回读 + 翻 Y + `EncodeToPNG`。
- 注册类注入 API = `ClassInjector.RegisterTypeInIl2Cpp<T>()`（这版 Il2CppInterop 没有 `DerivedFromTypeIl2Cpp`）；**注入类的 Awake/Update/OnDestroy 按方法名挂钩，写普通私有方法，不写 override**。
- csproj 引用闭包：除用到的模块外还需 `Il2Cppmscorlib.dll`/`Il2CppSystem.dll`/`SharedInternalsModule`（Roslyn 解析签名用），全部 `Private=false`。

**接口**：`GET /ping`；`POST /screenshot`（→ image/png）、`/ui`（Canvas 树：节点名/坐标/尺寸/TMP 文本/Button.interactable/路径）、`/click {path}`（路径精确或按钮名包含）、`/click_at {x,y}`（点下面积最小的可交互 Button → `onClick.Invoke()`）。发现文件 `BepInEx/bridge.json {port,pid,unity,plugin}`；端口配置 `BepInEx/config/DotAbyssBridge.cfg`（默认 **27124**）。

**Python 侧** `dotabyss_agent/device_bridge.py`：`bridge_info()`（发现文件→默认端口 ping）、`BridgeDevice`（screenshot/click/click_by_path/ui_tree/wait_settled/wait_until_stable/diff_ratio；`is_foreground()` 恒真、`bring_to_front()` 空操作；**swipe 未支持**——深渊拖拽等游戏侧映射后加）。`poc/bridge_mock_test.py` 假服务回环 10 项 ALL PASS。

### 2.6 待游戏验证清单（下次能开游戏时）

1. 启动游戏 → `BepInEx/LogOutput.log` 应出现 `DotAbyssBridge 0.1.0 loaded` + `bridge listening on 127.0.0.1:27124`；游戏目录生成 `BepInEx/bridge.json`。
2. `python -c "from dotabyss_agent.device_bridge import bridge_info; print(bridge_info())"` → pong。
3. `/screenshot`：与 ctl screenshot（MAA）对照——内容一致、分辨率 1280×720、UI 完整、颜色/翻转正确。
4. `/ui`：主城页面树完整性、Button 路径样本（为剧本 invoke 化积累）。
5. `/click_at`：在主城点几个按钮验证"面积最小可交互按钮"命中律（Screen Space Overlay 假设）；Camera 空间 canvas 的点击偏移待观察。
6. BridgeDevice 与 GameDevice 并行对照 wait_settled/wait_until_stable 语义。
7. 验证通过后合流：设备后端选择器（bridge 在线优先、否则回退 MAA GameDevice）接入 runner/cli ctl。

## 3. 子会话兜底（挂起）

触发条件：桥 interop 失败，或未来要"连 DMMGP 启动都无人值守"。届时需攻克：DMMGP 在分身内的登录态、日本 IP 网络环境（系统代理/TUN）在 RDP 子会话的表现、DMMGP 经计划任务发射（`TASK_RUN_USE_SESSION_ID`，doc 03 §3）。机制本身已源码级拆解完毕，环境门槛已确认无。

## 4. 实施状态

- [x] 决策定稿（本文档）
- [x] `control.py`：ControlServer（服务端）+ ctl_request（客户端）
- [x] GUI 集成：GuiCtlAdapter、共享设备、ctl_call marshal、quit
- [x] CLI `ctl` 子命令（无 GUI 时 run 回退独立直跑）
- [x] poc 回环测试 + GUI 真机验证
- [x] 桥 spike·侦察（§2.5：Unity/BepInEx/interop/游戏代码地图）
- [x] 桥 spike·插件编写+编译+部署（`bridge/DotAbyssBridge` → `BepInEx/plugins/DotAbyssBridge/`）
- [x] 桥 spike·Python 侧 `device_bridge.py` + 假服务回环（mock ALL PASS）
- [ ] **桥真机验证**（§2.6 清单，待用户可开游戏）
- [ ] 设备后端选择器合流（bridge 优先 / MAA 回退）接入 runner 与 ctl
