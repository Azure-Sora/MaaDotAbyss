"""深渊规划器（docs/research/12）：纯函数决策层——无模型、无设备依赖，可单测。

三权分立中的"大脑"：持整局账本做全部战略决策（选房/选 buff/回复房三选一/
事件拍板/续行票决策）。模型只当眼睛，剧本只当手脚。

机制速查（实测口径，见 doc 12 §2）：
- 侵蚀：进房 +5；5 蓝码(セーフ) Combo −5；另有单个同效果蓝码再 −5（齐备进房净负）；
  紫(リスク)码对称反向。账面 erosion 以 HUD 实测为准，这里只做余量预估。
- buff 四色：impact=黄 / rush=红 / safe=蓝 / risk=紫；冲突对 黄红、蓝紫（同持互减计数）。
- 结算：帰還する=打到底=全额；暴毙（HP 归零/侵蚀 100）血本无归 → 一切从保守。
"""
import re
from dataclasses import dataclass, field

# 四色（游戏内组名：インパクト/ラッシュ/セーフ/リスク）
COLORS = ("impact", "rush", "safe", "risk")
CONFLICT_PAIRS = {"impact": "rush", "rush": "impact", "safe": "risk", "risk": "safe"}

# 房间基准价值（精英/装备为主 → 精英最高；宝箱=垃圾垫底；商店 V1 跳过）
ROOM_WEIGHTS = {"elite": 10.0, "event": 6.0, "battle": 4.0, "treasure": 1.0,
                "shop": 0.0, "boss": 8.0, "heal": 0.0}  # heal 动态计算


@dataclass
class Candidate:
    """地图上一个可进入的房间（UI 直读 enterable / 箭头模板兜底）。"""
    type: str      # elite/battle/boss/heal/event/shop/treasure
    x: int
    y: int
    floor: int
    visible: bool = True   # 屏内可见；False=视口外（桥可按路径直点，模板兜底不可）
    btn_path: str | None = None   # 桥直读：房间 Button 路径（enter_room 首选）


@dataclass
class AbyssLedger:
    """整局账本。er keys coins floor 以 HUD 实测回填；buffs 以拾取事件累加。"""
    floor: int
    erosion: int
    getkeys: int
    coins: int
    buffs: dict = field(default_factory=dict)   # color -> count
    quota: dict = field(default_factory=dict)   # color -> 目标数量
    carry_cap: int = 31
    target_floor: int = 30
    hp_lost_pct: int = 0        # 自上次战斗累计事件扣血（百分比点数）
    hp_budget_pct: int = 30     # 两场战斗之间允许的累计扣血
    erosion_safe: int = 80      # 侵蚀安全线（到线回复房价值飙升）
    grant_ack_pending: bool = False  # 弹窗拿码后待获得页消费（防重复计数，非规划态）

    def total_codes(self) -> int:
        return sum(self.buffs.values())

    def deficit(self, color: str) -> int:
        return self.quota.get(color, 0) - self.buffs.get(color, 0)

    def hp_budget_left(self) -> int:
        return max(0, self.hp_budget_pct - self.hp_lost_pct)


# ---- 侵蚀动力学（预估用） ---------------------------------------------------

def erosion_step(buff_counts: dict, second_blue: bool = False) -> int:
    """预估进一层的侵蚀增量（可为负）。账面值永远以 HUD 为准。"""
    r = 5
    if buff_counts.get("safe", 0) >= 5:
        r -= 5                      # 5 蓝 Combo
    if second_blue:
        r -= 5                      # 另一同效果蓝码（勘探确认具体码后启用）
    if buff_counts.get("risk", 0) >= 5:
        r += 5                      # 紫 Combo
    return r


# ---- 选房 -------------------------------------------------------------------

def room_value(c: Candidate, led: AbyssLedger) -> float:
    v = ROOM_WEIGHTS.get(c.type, 1.0)
    if c.type == "heal":
        # 动态价值：安全线以上飙升；projected 超硬顶则近乎必选
        projected = led.erosion + erosion_step(led.buffs)
        if led.erosion >= led.erosion_safe:
            v = 20.0
        elif projected >= 100 - 10:
            v = 50.0
        else:
            v = 2.0
    elif c.type == "event" and led.erosion >= led.erosion_safe - 20:
        # 高侵蚀时事件房降权：事件强制选一项，常含侵蚀/扣血代价（41F 事故：
        # 侵蚀 60 仍进事件房连吃 +20）。此区间战斗/回复优先，事件让位。
        v = min(v, 2.0)
    return v


def pick_room(candidates: list[Candidate], led: AbyssLedger) -> Candidate:
    """候选贪心：价值最大者平手取先出现。候选来自箭头/光圈识别。"""
    if not candidates:
        raise ValueError("没有候选房间")
    return max(candidates, key=lambda c: (room_value(c, led), -c.y))


# ---- buff 弹窗（N 选 1） -----------------------------------------------------

def pick_code(options: list[dict], led: AbyssLedger, rerolls_left: int) -> tuple[str, int | None]:
    """返回 ("take", 选项下标) | ("reroll", None) | ("skip", None)。

    options: [{"color": impact/rush/safe/risk, "power": 战力增量数字}]
    规则：配额缺口最大的色优先 → 同色内战力增量最高；冲突对污染回避
    （对色有持码且对色配额未满时拿此色会互减，两败俱伤）；全不合适→有余量先
    リロール；配额全满不拿（受け取らない），不追求溢出。
    """
    if led.total_codes() >= led.carry_cap:
        return ("skip", None)
    scored = []
    for i, opt in enumerate(options):
        c = opt["color"]
        if led.deficit(c) <= 0:
            continue
        pair = CONFLICT_PAIRS[c]
        if led.buffs.get(pair, 0) > 0 and led.deficit(pair) > 0:
            continue  # 拿它会互减正在攒的对色
        scored.append((led.deficit(c), int(opt.get("power", 0)), i))
    if scored:
        scored.sort(reverse=True)
        return ("take", scored[0][2])
    if rerolls_left > 0 and any(led.deficit(c) > 0 for c in COLORS):
        return ("reroll", None)  # 配额未满足而选项都不合适——重摇有机会摇出需要的色
    return ("skip", None)


# ---- 回复房三选一 -------------------------------------------------------------

def pick_heal(led: AbyssLedger) -> str:
    """浄化(侵蚀-30) / 休憩(HP+30%) / 変換(换码)。默认浄化——侵蚀只涨不跌。"""
    if led.hp_lost_pct >= 50:
        return "rest"      # 战斗基本回满血，走到这一步说明事件连续扣血且没打战斗
    return "purify"


# ---- 事件拍板 ----------------------------------------------------------------

# 效果文案解析不出侵蚀数字时的保守估计（宝箱浸食+40 同档；41F 事故教训：
# 「浸食率20上昇」行被读取层漏掉 → 侵蚀代价被当 0，高侵蚀时照选直接爆线）。
UNKNOWN_EROSION_COST = 40

_RICH_TAG = re.compile(r"<[^>]+>")
_HALF_FULL = {"％": "%"}   # 实测 'HP20％回復'（全角）与 'HP40%減少'（半角）并存


def parse_event_desc(desc: str) -> dict:
    """事件效果标签 → 结构化代价/收益（纯函数，可单测）。

    树文本自带 rich text 颜色标签（数字常被标签包着），先剥离再匹配。
    含「浸食」关键词却解析不出任何侵蚀数字 → erosion_unknown=True（文案变体
    如「浸食率がMAXまで上昇」），调用方必须按 UNKNOWN_EROSION_COST 保守处理，
    绝不可当 0。
    """
    d = _RICH_TAG.sub("", desc)
    for k, v in _HALF_FULL.items():
        d = d.replace(k, v)
    m = re.search(r"浸食率\s*(\d+)\s*上昇", d)
    ec = int(m.group(1)) if m else 0
    m = re.search(r"浸食率\s*(\d+)\s*減少", d)
    eg = int(m.group(1)) if m else 0
    m = re.search(r"HP\s*(\d+)\s*%?\s*減少", d)
    hp = int(m.group(1)) if m else 0
    m = re.search(r"HP\s*(\d+)\s*%?\s*回復", d)
    hp_gain = int(m.group(1)) if m else 0
    m = re.search(r"(\d+)\s*個消費", d)
    coin = int(m.group(1)) if m else 0
    return {
        "hp_cost": hp, "hp_gain": hp_gain,
        "erosion_cost": ec, "erosion_gain": eg, "coin_cost": coin,
        # 'アビスコイン30獲得' 含「コ」但不含「コード」——不可用 'コ' in d 判拿码
        "code_gain": "コード" in d and "獲得" in d,
        "item_gain": "アイテム獲得" in d,
        "erosion_unknown": ("浸食" in d) and not ec and not eg,
    }


def effective_erosion_cost(o: dict) -> int:
    """侵蚀代价的保守视图：unknown 按 UNKNOWN_EROSION_COST 计，负值当 0。"""
    c = int(o.get("erosion_cost", 0) or 0)
    if o.get("erosion_unknown") and c <= 0:
        return UNKNOWN_EROSION_COST
    return max(0, c)


def event_score(o: dict, led: AbyssLedger) -> float:
    """事件选项打分（pick_event 内部用；锁定了 HP/侵蚀硬约束后也可单独比较选项）。"""
    s = 0.0
    if o.get("code_gain") and any(led.deficit(c) > 0 for c in COLORS):
        s += 100.0
    if o.get("item_gain"):
        s += 40.0
    if o.get("hp_gain"):
        s += 0.5 * o["hp_gain"]
    if o.get("erosion_gain"):
        s += 30.0 + o["erosion_gain"]
    s -= 2.0 * effective_erosion_cost(o)
    s -= 0.5 * o.get("coin_cost", 0)
    return s


def pick_event(options: list[dict], led: AbyssLedger) -> int:
    """事件必须选一项（X 不可跳过）。返回选项下标。

    options: [{"hp_cost": 扣血%, "erosion_cost": 侵蚀+, "erosion_gain": 侵蚀-,
               "hp_gain": 回血%, "code_gain": bool, "item_gain": bool,
               "coin_cost": int, "erosion_unknown": bool}]
    三级过滤（2026-09-03 收紧——41F 侵蚀 60 时连选两个 +侵蚀事件直接爆线）：
    - HP 代价 ≤ 战斗间剩余预算（战斗后清零——战斗基本回满，唯一死法是事件连扣归零）；
    - 侵蚀投影 < 100 硬顶（暴毙线，无可协商）；
    - 侵蚀安全线：有侵蚀代价的选项，投影 ≥ erosion_safe（默认 80）即不可行——
      安全线以上只留给「到线回复房价值飙升」的被动兜底，绝不主动吃侵蚀贴线。
    另：erosion_unknown（效果文案读不懂，如「浸食率がMAXまで上昇」）直接排除——
    41F 教训：读不懂的卡连收益描述都可能是错的，不做低侵蚀赌博；least-bad 兜底。
    通过过滤后按 event_score 打分；
    全不可行时 least-bad：先保 HP 再看侵蚀（事件强制选择，必须有产出）。
    """
    def projected_erosion(o: dict) -> int:
        return led.erosion - o.get("erosion_gain", 0) + effective_erosion_cost(o)

    def cost(o: dict) -> int:
        return effective_erosion_cost(o)

    feasible = [i for i, o in enumerate(options)
                if o.get("hp_cost", 0) <= led.hp_budget_left()
                and projected_erosion(o) < 100
                and not (cost(o) > 0 and projected_erosion(o) >= led.erosion_safe)
                and not o.get("erosion_unknown")]
    if feasible:
        return max(feasible, key=lambda i: event_score(options[i], led))
    # 全不可行（逼近死局）：先保 HP 再看侵蚀
    return min(range(len(options)),
               key=lambda i: (options[i].get("hp_cost", 0),
                              cost(options[i])))


# ---- 宝箱三选一 ---------------------------------------------------------------

def pick_treasure(options: list[dict], led: AbyssLedger) -> int:
    """宝箱开箱方式三选一（消耗钥匙-1 / HP-40% / 浸食+40）——必答题。
    2026-09-03 实测改约：X 离开=弹窗关而房不完成，Front 层候选全锁且弹窗不重弹
    （候选 0 卡死），唯一出路是选卡+確定。

    options: [{"kind": "key"|"hp"|"erosion", "key_cost"/"hp_cost"/"erosion_cost":
               int, "interactable": bool}]，返回选项下标。
    奖励是垃圾（选房权重垫底），本决策纯止损——挑伤最轻的出门方式：
    1. HP 且战后缓冲够（hp_lost_pct+cost ≤ 60，进下一战 ≥40% 血）——战斗回满，
       HP 是可再生成本，永久性代价（侵蚀/钥匙）能省则省；
    2. 侵蚀投影 < 安全线（与事件同规则：绝不主动吃侵蚀贴线）；
    3. 侵蚀投影 < 100（越线但未暴毙——到线回复房价值飙升，有兜底）；
    4. 钥匙：只当侵蚀再吃即死时才烧——续行票烧一张=下个 boss 门提前结算，
       保住已有收益，好过战斗暴毙血本无归；
    5. HP 兜底（血线赌战斗）；6. 硬着头皮选第一个可选卡。
    """
    feas = [i for i, o in enumerate(options) if o.get("interactable")]
    if not feas:
        raise ValueError("宝箱没有可选卡")

    def cost(o: dict, kind: str) -> int:
        return int(o.get(f"{kind}_cost", 0) or 0)

    def by_kind(kind: str) -> list[int]:
        return [i for i in feas if options[i].get("kind") == kind]

    for i in by_kind("hp"):
        if led.hp_lost_pct + cost(options[i], "hp") <= 60:
            return i
    for i in by_kind("erosion"):
        if led.erosion + cost(options[i], "erosion") < led.erosion_safe:
            return i
    for i in by_kind("erosion"):
        if led.erosion + cost(options[i], "erosion") < 100:
            return i
    if by_kind("key"):
        return by_kind("key")[0]
    if by_kind("hp"):
        return by_kind("hp")[0]
    return feas[0]


# ---- Boss 后票决策 ------------------------------------------------------------

def ticket_decision(led: AbyssLedger) -> str:
    """"continue"（续行，倍率默认 1 倍）| "settle"（帰還する 结算）。"""
    return "continue" if led.floor < led.target_floor else "settle"
