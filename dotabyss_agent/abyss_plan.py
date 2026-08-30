"""深渊规划器（docs/research/12）：纯函数决策层——无模型、无设备依赖，可单测。

三权分立中的"大脑"：持整局账本做全部战略决策（选房/选 buff/回复房三选一/
事件拍板/续行票决策）。模型只当眼睛，剧本只当手脚。

机制速查（实测口径，见 doc 12 §2）：
- 侵蚀：进房 +5；5 蓝码(セーフ) Combo −5；另有单个同效果蓝码再 −5（齐备进房净负）；
  紫(リスク)码对称反向。账面 erosion 以 HUD 实测为准，这里只做余量预估。
- buff 四色：impact=黄 / rush=红 / safe=蓝 / risk=紫；冲突对 黄红、蓝紫（同持互减计数）。
- 结算：帰還する=打到底=全额；暴毙（HP 归零/侵蚀 100）血本无归 → 一切从保守。
"""
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

def event_score(o: dict, led: AbyssLedger) -> float:
    """事件选项打分（pick_event 内部用；锁定了 HP/侵蚀硬约束后也可单独比较选项）。"""
    s = 0.0
    if o.get("code_gain") and any(led.deficit(c) > 0 for c in COLORS):
        s += 100.0
    if o.get("item_gain"):
        s += 40.0
    if o.get("erosion_gain"):
        s += 30.0 + o["erosion_gain"]
    s -= 2.0 * o.get("erosion_cost", 0)
    s -= 0.5 * o.get("coin_cost", 0)
    return s


def pick_event(options: list[dict], led: AbyssLedger) -> int:
    """事件必须选一项（X 不可跳过）。返回选项下标。

    options: [{"hp_cost": 扣血%, "erosion_cost": 侵蚀+, "erosion_gain": 侵蚀-,
               "code_gain": bool, "item_gain": bool, "coin_cost": int}]
    两级过滤：
    - HP 代价 ≤ 战斗间剩余预算（战斗后清零——战斗基本回满，唯一死法是事件连扣归零）；
    - 侵蚀投影（当前 − 收益 + 代价）< 100 硬顶（暴毙线，无可协商）。
    通过过滤后按 event_score 打分；
    全不可行时 least-bad：先保 HP 再看侵蚀（事件强制选择，必须有产出）。
    """
    def projected_erosion(o: dict) -> int:
        return led.erosion - o.get("erosion_gain", 0) + o.get("erosion_cost", 0)

    feasible = [i for i, o in enumerate(options)
                if o.get("hp_cost", 0) <= led.hp_budget_left()
                and projected_erosion(o) < 100]
    if feasible:
        return max(feasible, key=lambda i: event_score(options[i], led))
    # 全不可行（逼近死局）：先保 HP 再看侵蚀
    return min(range(len(options)),
               key=lambda i: (options[i].get("hp_cost", 0),
                              options[i].get("erosion_cost", 0)))


# ---- Boss 后票决策 ------------------------------------------------------------

def ticket_decision(led: AbyssLedger) -> str:
    """"continue"（续行，倍率默认 1 倍）| "settle"（帰還する 结算）。"""
    return "continue" if led.floor < led.target_floor else "settle"
