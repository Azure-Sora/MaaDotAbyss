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

# 多模型供给：按用途切换（探索录制/日常决策可用不同档位）
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
