"""全局配置与路径常量。"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCAL_DIR = ROOT / ".local"          # 密钥、运行产物（gitignore）
TASKS_DIR = ROOT / "tasks"
KNOWLEDGE_DIR = TASKS_DIR / "knowledge"
RUNS_DIR = LOCAL_DIR / "runs"

GAME_TITLE = "ドットアビスX"
WINDOW_SIZE = (1280, 720)

MIMO_BASE_URL = "https://api.xiaomimimo.com/v1"
MIMO_MODEL = "mimo-v2.5"
MIMO_KEY_PATH = LOCAL_DIR / "mimokey.txt"
MAX_COMPLETION_TOKENS = 4096         # MiMo 是推理模型，太小会被 reasoning 耗尽

MAX_STEPS_DEFAULT = 30
HISTORY_KEEP = 8                     # 步历史只保留最近 N 条（上下文裁剪）
