"""GUI 启动器：`python run_gui.py`；同时是 PyInstaller 打包入口（见 .github/workflows/release.yml）。"""
from dotabyss_agent.gui import main

if __name__ == "__main__":
    main()
