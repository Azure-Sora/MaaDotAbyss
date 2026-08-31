<p align="center">
  <img src="packaging/icon.png" width="140" alt="DotAbyss Agent">
</p>
<h1 align="center">MaaDotAbyss</h1>

<p align="center">
  ✨ ドットアビスX 小助手 ✨<br>
  大模型看屏幕做决策 · 程序负责执行 · 每日任务全自动
</p>


<p align="center">
  <img src="https://img.shields.io/badge/platform-Windows-blue" alt="Windows">
  <img src="https://img.shields.io/badge/python-3.10%2B-blue" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/LLM-OpenAI%20%E5%85%BC%E5%AE%B9-orange" alt="OpenAI 兼容">
  <img src="https://img.shields.io/badge/license-AGPL--3.0-green" alt="AGPL-3.0">
</p>

> **⚡ 由 [MaaFramework](https://github.com/MaaXYZ/MaaFramework) 强力驱动！**

---

**玩家首次体验 DotAbyss Agent！被吓到眩晕瘫坐，那一刻就像看到原子弹爆炸**

<p align="center">
  <img src="packaging/dizzy.png" width="520" alt="眩晕瘫坐">
</p>

刚刚，玩家曝光了 DotAbyss Agent 的使用初体验。AI 把每日任务清完的效率让他瘫坐在椅子上，那一刻，“自己毫无用处”的想法，从未如此强烈。此外，他宣称要为每个玩家免费提供全自动的每日任务，另外还有个乌托邦设想——未来从日常里解放出来的 80 亿小时，会分给全部 80 亿地球人。

---

也许是首个服务于小黄油的MAA？

刷深渊、清日常、领奖励——把每天那套重复劳动交给多模态大模型盯着屏幕替你点。LLM 负责看画面做决策，纯 Python 程序负责精确执行，两条腿走路。个人自用项目，随缘维护，只在 Release 提供 Windows 版。

## ✨ 功能一览

- 🤖 **日常任务自动化** —— 任务清单驱动（`tasks/daily.yaml`），LLM 按截图逐步决策推进；支持勾选运行、排序、编辑，任务做完还会把本次实操经验自动合入任务说明
- 💬 **交互式新建任务** —— 在 GUI 里像聊天一样带着模型把新任务走一遍，自动沉淀成 **剧本 + 锚点 + 知识卡**，之后一键回放——不会写代码也能给助手加任务
- ⚔️ **程序连打** —— 势力任务 / 迎击战 / 探索任务这类重复战斗整体交给程序执行，LLM 只管导航与验证；没有现成流程的清剿、领取，可用通用扫荡编排现教
- 🗺️ **深渊全自动** —— 入场 → 检查点 → 逐层推进 → 结算：路线与四色代码配额由纯 Python 规划器计算，LLM 兜底识图（buff 定色、事件选项、异常弹窗），HUD 对账防跑偏
- 🌉 **双设备后端** —— 游戏装了 BepInEx 就用桥插件：进程内直调 UI、窗口可遮挡不抢焦点、截图更快；没装则自动回退 MAA 前台模式
- 🔌 **任意 OpenAI 兼容模型** —— GUI 里填 base_url + key 即用，模型名手动填或从 `/models` 自动发现；GLM、DeepSeek、本地 vLLM 随意切换（注意一定要支持多模态！！！）
- 🛰️ **控制面** —— GUI 内嵌本地 HTTP 接口，CLI 可附着同一引擎：`status` / `run` / `stop` / `screenshot`，方便脚本联动

## 📖 使用须知

> [!WARNING]
> 游戏必须**人工**经 DMM Game Player 启动（需日本 IP）。工具绝不自动启动或重启游戏；遇到 403 网络错误会立刻停下等你处理。游戏窗口分辨率需 1280×720。

## 🚀 快速开始

**Release 版**

1. 从 [Releases](../../releases) 下载 zip 解压
2. 双击 `DotAbyssAgent.exe`，进「模型」页配置 API：填 OpenAI 兼容的 base_url + key，点「发现模型」选一个
3. 把游戏安装路径写进 exe 旁的 `.local/game_dir.txt`（一行，如xxx\dotabyss_x_cl），或设环境变量 `DOTABYSS_GAME_DIR`
4. 任务页勾任务 → 运行全部 / 运行选中

**源码运行**

```bash
pip install -r requirements.txt
python run_gui.py          # 纯命令行用 python -m dotabyss_agent.cli
```

API key 等敏感文件一律放 `.local/`（已 gitignore），密钥永远不入仓库。

> [!TIP]
> **推荐搭配 [AbyssMod](https://github.com/anosu/AbyssMod) 使用**： Releases 里的
> `AbyssMod.7z` 自带完整 BepInEx 结构，解压到游戏根目录即完成安装——省去手动装 BepInEx 的功夫，
> 还附赠汉化。装好后把本项目的 `DotAbyssBridge.dll` 放进 `BepInEx/plugins/DotAbyssBridge/`，
> 桥后端即刻生效：翻译归翻译、自动化归自动化，互不冲突，配合使用效果更佳。

## 🛠️ 构建

```bash
pip install -r requirements.txt pyinstaller
pyinstaller --onefile --windowed --noconfirm --name DotAbyssAgent ^
  --icon build/icon.ico --add-data "packaging/icon.png;." ^
  --collect-all qfluentwidgets --collect-all maa --exclude-module matplotlib ^
  run_gui.py
```

桥插件（仅改了 `bridge/` 下 C# 时需要重新构建）：`dotnet build bridge/DotAbyssBridge -c Release`，
GameDir 通过环境变量 `DOTABYSS_GAME_DIR` 或 `bridge/Directory.Build.props` 指向游戏目录。
构建产物覆盖 `bridge/dist/DotAbyssBridge.dll`。

## 💡 注意事项

- 深渊功能会消耗游戏内入场券等资源，配额参数在深渊页配置；消费类确认弹窗一律自动拒绝，免费次数打完即收工
- `.local/` 是运行时数据目录（密钥、截图、运行记录、providers 配置），更新版本时保留它即可
- 桥 DLL 找不到游戏窗口时确认游戏确实由 DMM Player 启动；桥不在线时 GUI 会显示并自动走 MAA 路线

## ⚠️ 免责声明

> [!CAUTION]
>
> - 本项目是第三方模拟交互工具，不修改任何游戏数据，仅供个人学习与日常自动化使用
> - 使用自动化工具存在账号被封禁的风险，请自行评估、后果自负
> - 与游戏官方无关，请勿用于商业用途

## 🧾 开源许可

本项目以 [AGPL-3.0](LICENSE) 协议开源——与 MAA 生态保持一致：发布的 exe 打包了 AGPL-3.0 的
MaaFramework 与 GPL-3.0 的 PyQt-Fluent-Widgets，按协议要求整体以 AGPL-3.0 发布。

## ❤️ 鸣谢

- [MaaFramework](https://github.com/MaaXYZ/MaaFramework) — 设备层（Win32 控制器与截图）
- [BepInEx](https://github.com/BepInEx/BepInEx) — 桥后端宿主
- [AbyssMod](https://github.com/anosu/AbyssMod) — 优秀的社区整合 Mod，推荐与本项目搭配使用
- [PyQt-Fluent-Widgets](https://github.com/zhiyiYo/PyQt-Fluent-Widgets) — GUI 界面
- [openai-python](https://github.com/openai/openai-python) — LLM 客户端
