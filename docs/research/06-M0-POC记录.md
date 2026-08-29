# 06 · M0 POC 实测记录

> 2026-08-29 ｜ 环境：Win11 26200 + Python 3.10.8 + MaaFw 5.12.3（pip）｜ 游戏：ドットアビスX（Unity IL2CPP / D3D12 / uGUI / 旧 Input），窗口 1280×720
> POC 脚本：`poc/`（运行产物在 `.local/poc_out/`，已 gitignore）

## 1. 测试矩阵总览

| # | 测试项 | 结果 | 说明 |
| --- | --- | --- | --- |
| 1 | 窗口发现与绑定 | ✅ | `UnityWndClass` / 标题 `ドットアビスX`，`Toolkit.find_desktop_windows()` 按标题过滤即可 |
| 2 | 前台截图（ScreenDC \| DesktopDup_Window） | ✅ | 1280×720 完整画面，质量佳 |
| 3 | 后台截图（FramePool \| PrintWindow），失焦/被遮挡 | ✅ | WGC 稳定出清晰画面；**ScreenDC 被遮挡时会截到遮挡物**，后台场景必须用 FramePool |
| 4 | 最小化时截图（伪最小化） | ❌ | **全黑帧**：Unity 最小化后引擎停止渲染，MAA 伪最小化机制救不了；且首次伪最小化截图后窗口**不会回到最小化态**（保持恢复但未激活） |
| 5 | 前台 Seize 点击 | ✅ | 主页↔队伍页多次往返成功（帧差 91%+），需先把游戏切到前台（ALT+SetForegroundWindow 可靠） |
| 6 | **前台** PostMessage 点击（判别实验） | ❌ | `GetForegroundWindow()==游戏窗口` 已验证的前提下点击仍无效 → **根因：Unity 旧 Input 不消费窗口消息队列（轮询硬件状态/Raw Input），与焦点无关** |
| 7 | 失焦 PostMessage / SendMessage 点击 | ❌ | 同上，消息路径本身无效 |
| 8 | 伪造 WM_ACTIVATE + PostMessage | ❌ | 同上 |
| 8b | 失焦操作顽固性 | ℹ️ | 游戏持有焦点时，AttachThreadInput+SetForegroundWindow(桌面) 与"最小化→SW_SHOWNOACTIVATE 恢复"都抢不走焦点（游戏会重新拿回前台）；对 M3 子会话方案无影响 |
| 9 | AnchoredTouch（WM_POINTER 合成触摸） | ⏸ 未测 | pip 5.12.3 尚未包含该功能（主分支才有），待新版本发布后验证 |
| 10 | MiMo-V2.5 视觉识别 | ✅ | 正确识别主城界面、全部可交互元素及位置、界面语言；细节见 §3 |
| 11 | 同进程创建两个 Win32 控制器 | ❌ | **native segfault** → 约定：**一个进程只建一个控制器**（换输入/截图方式就重启进程或重新 create） |
| 12 | 失焦时游戏渲染状态 | ℹ️ | 帧差 ~14%/1.5s，**主循环在后台持续运行**（只是输入被焦点门控）→ 等待类场景可用帧差检测 |

## 2. 关键结论

1. **设备层 MAA 完全可用**：绑窗、截图（前台/后台）、前台点击全部通过。M1~M2 按"前台模式"推进没有障碍。
2. **消息注入路线对此游戏彻底无效**（含前台）：Unity 旧 Input 体系轮询硬件状态（GetAsyncKeyState/Raw Input），不读窗口消息队列——PostMessage/SendMessage/伪造激活在前台也点不动。真后台的可行路径（按推荐排序）：
   - **子会话桌面分身**（M3 方案）：游戏在分身内是真前台，Seize+WGC 全套照常工作（BGI 已验证此模式）；
   - **BepInEx 桥**：游戏内插件直接调用 uGUI 按钮 / 注入输入事件，绕过硬件输入路径（注入环境已验证可用），确定性强、零打扰，但需要写 IL2CPP mod；
   - 前台闪切（聚焦→点击→还焦点）：每次点击短暂抢焦点，日常几百次点击对使用干扰大，仅作临时手段；
   - AnchoredTouch（WM_POINTER）：pip 发新版后可一试，但 Unity 若未启用触控路径大概率同样无效，期望值放低。
3. **无人值守约束：窗口可被遮挡，不可最小化**。Runner 启动时应检查 `IsIconic` 并恢复；任务期间用 FramePool 截图抗遮挡。
4. **等待检测便宜**：后台渲染不停 → 自动战斗/加载等待用帧差检测（连续 N 帧差异低于阈值=稳定）完全可行，不必让 LLM 轮询。
5. **游戏偶发"加载中卡住"**（用户观察：手动点击也无反应，帧差骤降是卡 loading 的信号）。处置约定：
   - Agent 检测到"连续点击无页面响应"时，先做一次前台 Seize 基线区分"卡住"还是"点错位置"；
   - **点右上角菜单按钮可加速加载/恢复**（用户经验，菜单按钮位于右上角 ≡ 图标处）；
   - 卡住不属于 403，不要重启游戏，等待或点菜单即可。

## 3. MiMo-V2.5 接入笔记

- `base_url=https://api.xiaomimimo.com/v1`（key `sk-` 前缀为按量通道）；模型名 `mimo-v2.5` 可用。
- **是推理模型**：`max_completion_tokens=1024` 时 reasoning 就耗尽预算导致正文为空 → 必须 ≥4096。
- 单帧 720p 截图 `image_tokens=880`；prompt caching 生效（第二次调用 `cached_tokens` 显著）。
- 实测单次调用（截图+定位提问）总 tokens ≈ 2k~5k，响应数秒级——放置游戏完全够用。
- 视觉质量：能读出界面文字（中文/日文混合，汉化覆盖不全）、图标语义（"礼盒带数字5"）与相对位置，满足 Agent 决策需求。

## 4. 对既有文档的修正

- 02 文档"推荐尝试顺序 PostMessage → AnchoredTouch → Seize"修正为：**前台 Seize（M1~M2）→ 后台走子会话/BepInEx（M3）**；PostMessage/AnchoredTouch 仅留作验证性备选。
- 05 风险表新增"最小化黑屏"约束。

## 5. 遗留待办

- [ ] AnchoredTouch 验证（等 MaaFw pip 发版）
- [ ] BepInEx 桥接插件可行性 spike（IL2CPP interop 调 uGUI）——作为 M3 备选与"确定性操作"增强
- [ ] 自动战斗/加载画面帧差阈值标定（M1 顺带）
- [ ] 403 弹窗特征样本收集（出现时机/画面特征），用于停止条件识别
- [ ] 游戏内"日常"实际操作序列梳理（哪些任务是纯点击，哪些有子页面）——需要用户配合列清单或首轮 explore 模式自动积累
