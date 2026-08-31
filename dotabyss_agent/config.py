"""全局配置与路径常量。"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCAL_DIR = ROOT / ".local"          # 密钥、运行产物（gitignore）
TASKS_DIR = ROOT / "tasks"
KNOWLEDGE_DIR = TASKS_DIR / "knowledge"
RUNS_DIR = LOCAL_DIR / "runs"

"""全局配置与路径常量。"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCAL_DIR = ROOT / ".local"          # 密钥、运行产物（gitignore）
TASKS_DIR = ROOT / "tasks"
KNOWLEDGE_DIR = TASKS_DIR / "knowledge"
RUNS_DIR = LOCAL_DIR / "runs"

# 游戏安装目录（BepInEx 桥发现文件所在地）：env DOTABYSS_GAME_DIR 优先，
# 其次 .local/game_dir.txt；本机路径不入仓库
_game_dir = os.environ.get("DOTABYSS_GAME_DIR", "")
if not _game_dir:
    _gd_file = LOCAL_DIR / "game_dir.txt"
    if _gd_file.exists():
        _game_dir = _gd_file.read_text(encoding="utf-8-sig").strip()
GAME_DIR = Path(_game_dir) if _game_dir else None

GAME_TITLE = "ドットアビスX"
WINDOW_SIZE = (1280, 720)

MIMO_BASE_URL = "https://api.xiaomimimo.com/v1"
MIMO_MODEL = "mimo-v2.5"
MIMO_KEY_PATH = LOCAL_DIR / "mimokey.txt"
MAX_COMPLETION_TOKENS = 4096         # MiMo 是推理模型，太小会被 reasoning 耗尽

# 多模型供给：仅作 modelstore 的首次播种种子；真实配置在 .local/providers.json，
# 增删改走 GUI「模型」页（或直接改那个文件）
PROVIDERS = {
    "mimo": {
        "base_url": MIMO_BASE_URL,
        "model": MIMO_MODEL,
        "key_path": MIMO_KEY_PATH,
    },
    "glm": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-5.3-flash",
        "key_path": LOCAL_DIR / "glmkey.txt",
    },
}
ACTIVE_PROVIDER = "glm"              # 当前主力

MAX_STEPS_DEFAULT = 30
HISTORY_KEEP = 8                     # 步历史只保留最近 N 条（上下文裁剪）
