"""模型供给注册表：OpenAI 兼容端点的 base_url/model/key 管理。

仓库里的 config.PROVIDERS 只是首次运行的种子；真正的用户配置落在
.local/providers.json（gitignore），密钥要么内联存这里、要么继续引用
key_path 文件，均不入库。GUI 模型页增删改走本模块，brain 按名字取配置。
"""
import json
import threading
from pathlib import Path

from .config import ACTIVE_PROVIDER, LOCAL_DIR, PROVIDERS as SEED_PROVIDERS

STORE_PATH = LOCAL_DIR / "providers.json"


def discover_models(base_url: str, api_key: str = "", timeout: float = 15.0) -> list[str]:
    """调 OpenAI 兼容的 GET {base_url}/models，返回排序后的模型 id 列表。"""
    from openai import OpenAI

    # openai 客户端要求 api_key 非空；个别自建网关不校验鉴权时用占位符
    client = OpenAI(api_key=api_key or "EMPTY", base_url=base_url.strip(), timeout=timeout)
    return sorted(m.id for m in client.models.list())


class ModelStore:
    """provider 表：名字 → {base_url, model, api_key|key_path}。

    首次运行用种子播种并落盘，此后 providers.json 是唯一事实源——
    删除/改名内置 provider 也只在 json 里生效，不会被仓库默认值复活。
    """

    def __init__(self, path: Path = STORE_PATH):
        self.path = path
        self._lock = threading.Lock()
        self._items: dict[str, dict] = {}
        self._active = ACTIVE_PROVIDER
        self._load()

    # ---- 读取 ----------------------------------------------------------

    def names(self) -> list[str]:
        return sorted(self._items)

    def get(self, name: str) -> dict:
        try:
            return dict(self._items[name])
        except KeyError:
            raise KeyError(f"未知 provider: {name}（可用: {', '.join(self._items) or '无'}）")

    def active(self) -> str:
        if self._active in self._items:
            return self._active
        if not self._items:
            raise KeyError("没有任何可用 provider，先在模型页添加")
        return self.names()[0]

    def key(self, name: str) -> str:
        """解析密钥：api_key 内联优先，其次 key_path 文件。"""
        cfg = self.get(name)
        if cfg.get("api_key"):
            return str(cfg["api_key"]).strip()
        kp = cfg.get("key_path")
        if kp:
            return Path(kp).read_text(encoding="utf-8-sig").strip()
        raise KeyError(f"provider {name} 未配置 API Key")

    def key_desc(self, name: str) -> str:
        """列表展示用：密钥来源的脱敏描述。"""
        cfg = self.get(name)
        if cfg.get("api_key"):
            k = str(cfg["api_key"])
            return k[:4] + "****" + k[-4:] if len(k) > 10 else "已内联"
        kp = cfg.get("key_path")
        if kp:
            p = Path(kp)
            return f"文件 {p.name}" + ("" if p.exists() else "（缺失）")
        return "未设置"

    # ---- 变更 ----------------------------------------------------------

    def upsert(self, name: str, base_url: str, model: str, api_key: str = "", key_path=""):
        name, base_url, model = name.strip(), base_url.strip(), model.strip()
        if not name:
            raise ValueError("名称不能为空")
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("Base URL 需以 http(s):// 开头")
        if not model:
            raise ValueError("模型名不能为空（可手动填写或点「发现模型」选择）")
        with self._lock:
            entry = {"base_url": base_url, "model": model}
            if api_key.strip():
                entry["api_key"] = api_key.strip()
            elif key_path:
                entry["key_path"] = str(key_path)
            self._items[name] = entry
            if self._active not in self._items:
                self._active = name
            self._save()

    def remove(self, name: str):
        if name not in self._items:
            raise KeyError(f"未知 provider: {name}")
        if name == self._active:
            raise ValueError(f"{name} 是当前主力，先切换主力再删除")
        with self._lock:
            del self._items[name]
            self._save()

    def set_active(self, name: str):
        if name not in self._items:
            raise KeyError(f"未知 provider: {name}")
        with self._lock:
            self._active = name
            self._save()

    # ---- 持久化 --------------------------------------------------------

    def _load(self):
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
            self._items = {n: dict(c) for n, c in data.get("providers", {}).items()}
            self._active = data.get("active", ACTIVE_PROVIDER)
        else:
            for n, c in SEED_PROVIDERS.items():
                self._items[n] = {k: str(v) if isinstance(v, Path) else v
                                  for k, v in c.items()}
            self._save()

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"active": self._active, "providers": self._items}
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


store = ModelStore()
