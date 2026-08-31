DotAbyss Agent 使用说明
=======================

包内内容：
  DotAbyssAgent.exe        主程序（双击启动 GUI；.local/ 数据目录会生成在 exe 旁边）
  tasks/                   任务清单与知识卡（须与 exe 同目录，GUI 里编辑会直接写回）
  DotAbyssBridge.dll       BepInEx 桥插件（可选：后台直控/截图更快，见下）
  README.txt               本文件

首次配置：
  1. 模型：启动后进「模型」页添加 provider（OpenAI 兼容 Base URL + API Key，
     可点「发现模型」自动拉取模型列表）。密钥保存在 exe 旁的 .local/providers.json。
  2. 游戏目录：把游戏安装路径写入 exe 旁 .local/game_dir.txt（一行），
     或设环境变量 DOTABYSS_GAME_DIR。
  3. 启动游戏需人工经 DMM Game Player（日本 IP），工具不负责启动游戏。

桥插件（可选，推荐）：
  把 DotAbyssBridge.dll 拷到 <游戏目录>\BepInEx\plugins\DotAbyssBridge\ 下，
  游戏启动后自动生效（后台点击/截图，窗口可遮挡）。需要游戏已安装 BepInEx 6。
  没有桥也能用：自动回退 MAA 前台模式（窗口不可最小化、会抢焦点）。

命令行（可选）：
  GUI 开着时，可用另一份本仓库（或 pip 环境）执行
  python -m dotabyss_agent.cli ctl status|run|stop|screenshot 附着同一引擎。
