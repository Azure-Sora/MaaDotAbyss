"""大模型用量记录与聚合。

埋点在 brain.Brain._chat（全项目唯一 chat 出口）：每次调用（成功或失败）追加一行
JSON 到 .local/usage/YYYYMMDD.jsonl（一天一文件，gitignore，不入库）。
聚合结果供 GUI「用量」页与控制面 usage 命令共用，本模块不依赖 Qt/openai。

字段：ts / scene / provider / model / task / ok / prompt_tokens /
completion_tokens / cached_tokens / reasoning_tokens / total_tokens /
latency_s / err。cached_tokens ⊆ prompt_tokens（缓存命中部分），
reasoning_tokens ⊆ completion_tokens（推理消耗部分）。
"""
import json
import threading
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import LOCAL_DIR

USAGE_DIR = LOCAL_DIR / "usage"
RECENT_N = 60                        # 页面/接口返回的明细条数上限

# scene → 中文名；Brain 各业务方法固定传自己的场景，多调用点方法由调用方细分
SCENE_ZH = {
    "daily_decide": "日常任务决策",
    "daily_decide_retry": "日常决策(JSON重试)",
    "teach_decide": "教学探索决策",
    "teach_decide_retry": "教学决策(JSON重试)",
    "teach_distill": "教学会话蒸馏",
    "task_verify": "任务完成验收",
    "flow_verify": "剧本终态验收",
    "knowledge_card": "知识卡沉淀",
    "prompt_merge": "补充情报改稿",
    "precondition": "前置条件检查",
    "abyss_code": "深渊代码定色",
    "abyss_rescue": "深渊兜底自救",
    "flow_compile": "剧本编译选步",
    "vision_read": "画面识读(未标)",
    "misc": "未标场景",
}

_lock = threading.Lock()


def scene_zh(scene: str) -> str:
    return SCENE_ZH.get(scene, scene)


def record(*, scene: str, provider: str, model: str, ok: bool, task: str | None = None,
           prompt_tokens=None, completion_tokens=None, cached_tokens=None,
           reasoning_tokens=None, total_tokens=None, latency_s=None,
           err: str | None = None) -> None:
    """追加一条调用记录。统计永不影响业务：内部异常一律吞掉。"""
    def _num(v):
        return int(v) if isinstance(v, (int, float)) else None

    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "scene": scene, "provider": provider, "model": model,
        "task": task, "ok": bool(ok),
        "prompt_tokens": _num(prompt_tokens),
        "completion_tokens": _num(completion_tokens),
        "cached_tokens": _num(cached_tokens),
        "reasoning_tokens": _num(reasoning_tokens),
        "total_tokens": _num(total_tokens),
        "latency_s": round(float(latency_s), 3) if isinstance(latency_s, (int, float)) else None,
        "err": (str(err)[:300] if err else None),
    }
    try:
        with _lock:
            USAGE_DIR.mkdir(parents=True, exist_ok=True)
            p = USAGE_DIR / f"{datetime.now():%Y%m%d}.jsonl"
            with p.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def load_entries(days: int | None = 30) -> list[dict]:
    """读近 N 天（含今天，文件名即日期）的记录，按 ts 升序；None=全部。"""
    if not USAGE_DIR.exists():
        return []
    files = sorted(USAGE_DIR.glob("*.jsonl"))
    if days is not None and days > 0:
        cutoff = date.today() - timedelta(days=days - 1)
        kept = []
        for f in files:
            try:
                if datetime.strptime(f.stem, "%Y%m%d").date() >= cutoff:
                    kept.append(f)
            except ValueError:
                kept.append(f)      # 非日期命名的文件不误删
        files = kept
    entries = []
    for f in files:
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                e = json.loads(line)
                if isinstance(e, dict) and e.get("ts"):
                    entries.append(e)
            except json.JSONDecodeError:
                continue            # 半行/坏行跳过，不影响其余
    entries.sort(key=lambda e: e.get("ts", ""))
    return entries


def _num_or_0(v):
    return v if isinstance(v, (int, float)) else 0


def _blank(scene=None, provider=None, model=None) -> dict:
    d = {"requests": 0, "fail": 0, "prompt_tokens": 0, "cached_tokens": 0,
         "completion_tokens": 0, "reasoning_tokens": 0, "total_tokens": 0,
         "latency_sum": 0.0, "latency_n": 0, "latency_max": 0.0}
    if scene is not None:
        d["scene"] = scene
    if provider is not None:
        d["provider"] = provider
        d["model"] = model
    return d


def _acc(d: dict, e: dict) -> None:
    d["requests"] += 1
    if not e.get("ok"):
        d["fail"] += 1
    for k in ("prompt_tokens", "cached_tokens", "completion_tokens",
              "reasoning_tokens", "total_tokens"):
        d[k] += _num_or_0(e.get(k))
    lat = e.get("latency_s")
    if isinstance(lat, (int, float)):
        d["latency_sum"] += lat
        d["latency_n"] += 1
        d["latency_max"] = max(d["latency_max"], lat)


def _finish(d: dict) -> dict:
    d["latency_avg"] = round(d["latency_sum"] / d["latency_n"], 3) if d["latency_n"] else None
    d["latency_max"] = round(d["latency_max"], 3)
    del d["latency_sum"], d["latency_n"]
    return d


def aggregate(days: int | None = 30, recent_n: int = RECENT_N) -> dict:
    """多维聚合：总览 / 按场景 / 按模型 / 按日 / 最近一日分时 / 明细。"""
    entries = load_entries(days)

    total = _blank()
    by_scene: dict = defaultdict(lambda: _blank())
    by_model: dict = defaultdict(lambda: _blank())
    by_task: dict = defaultdict(lambda: _blank())
    by_day: dict = defaultdict(lambda: _blank())
    last_day: str | None = None
    for e in entries:
        _acc(total, e)
        _acc(by_scene[e.get("scene") or "misc"], e)
        mk = (e.get("provider") or "?", e.get("model") or "?")
        _acc(by_model[mk], e)
        _acc(by_task[e.get("task") or "-"], e)
        day = str(e.get("ts", ""))[:10]
        if day:
            _acc(by_day[day], e)
            if last_day is None or day > last_day:
                last_day = day

    recent = [
        {**e, "scene_zh": scene_zh(e.get("scene") or "misc")}
        for e in entries[-recent_n:]
    ][::-1]

    return {
        "days": days,
        "from": min(by_day) if by_day else None,
        "to": max(by_day) if by_day else None,
        "total": _finish(total),
        "by_scene": [_finish({**v, "scene": k, "scene_zh": scene_zh(k),
                              "last_ts": _last_ts(entries, "scene", k)})
                     for k, v in sorted(by_scene.items(),
                                        key=lambda kv: -kv[1]["total_tokens"])],
        "by_model": [_finish({**v, "provider": k[0], "model": k[1]})
                     for k, v in sorted(by_model.items(),
                                        key=lambda kv: -kv[1]["total_tokens"])],
        "by_task": [_finish({**v, "task": k})
                    for k, v in sorted(by_task.items(),
                                       key=lambda kv: -kv[1]["total_tokens"])[:15]],
        "by_day": _pad_days(by_day, days),
        "by_hour": _hours(entries, last_day),
        "hour_date": last_day,
        "recent": recent,
    }


def _last_ts(entries: list[dict], key: str, val) -> str | None:
    ts = [e.get("ts", "") for e in entries if e.get(key) == val and e.get("ts")]
    return max(ts) if ts else None


def _pad_days(by_day: dict, days: int | None) -> list[dict]:
    """按日补零到完整区间：给定范围则从今天往前数 N 天；全部则覆盖首末日。"""
    if not by_day:
        return []
    today = date.today()
    if days is not None and days > 0:
        start = today - timedelta(days=days - 1)
        end = today
    else:
        start = date.fromisoformat(min(by_day))
        end = date.fromisoformat(max(by_day))
    out = []
    d = start
    while d <= end:
        key = d.isoformat()
        out.append({"date": key, **_finish(by_day[key])} if key in by_day
                   else {"date": key, **_blank()})
        d += timedelta(days=1)
    return out


def _hours(entries: list[dict], last_day: str | None) -> list[dict]:
    """最近有数据一天的 0-23 小时分时（当天没跑则取最后一天，图不至于全空）。"""
    if not last_day:
        return []
    buckets = [ _blank() for _ in range(24) ]
    for e in entries:
        if str(e.get("ts", ""))[:10] != last_day:
            continue
        try:
            h = int(e["ts"][11:13])
        except (ValueError, IndexError):
            continue
        _acc(buckets[h], e)
    return [{"hour": h, **_finish(b)} for h, b in enumerate(buckets)]
