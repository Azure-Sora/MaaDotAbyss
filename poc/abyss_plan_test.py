"""深渊规划器单测：纯逻辑，不碰设备/模型。python poc/abyss_plan_test.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotabyss_agent.abyss_plan import (
    AbyssLedger, Candidate, erosion_step, pick_code, pick_event, pick_heal,
    pick_room, ticket_decision,
)


def led(**kw) -> AbyssLedger:
    base = dict(floor=21, erosion=10, getkeys=120, coins=50,
                quota={"impact": 3, "rush": 0, "safe": 6, "risk": 0},
                target_floor=30)
    base.update(kw)
    return AbyssLedger(**base)


# ---- 选房 ----
cands = [Candidate("battle", 300, 400, 21), Candidate("elite", 800, 420, 21),
         Candidate("treasure", 600, 380, 21)]
assert pick_room(cands, led()).type == "elite", "精英权重最高"

l = led(erosion=85)
heal_c = Candidate("heal", 500, 400, 21)
assert pick_room(cands + [heal_c], l).type == "heal", "侵蚀过安全线回复房飙升"

l = led(erosion=95)
assert pick_room(cands + [heal_c], l).type == "heal", "接近硬顶近乎必选"

# ---- buff 择取 ----
l = led(buffs={"safe": 2})
r = pick_code([{"color": "impact", "power": 400}, {"color": "safe", "power": 400},
               {"color": "risk", "power": 1200}], l, 2)
assert r == ("take", 1), f"缺口最大的 safe 应入选: {r}"

# 冲突对单边拿取：impact 有缺口入选；rush 无缺口跳过（rush 无持码，无污染问题）
r = pick_code([{"color": "impact", "power": 400}, {"color": "rush", "power": 900}], l, 2)
assert r == ("take", 0), f"impact 按缺口入选: {r}"

# 选项全不在配额内（quota 只有 impact/safe）→ 有余量先重摇
r = pick_code([{"color": "rush", "power": 900}, {"color": "risk", "power": 800}], led(buffs={}), 2)
assert r == ("reroll", None), f"无匹配色且有余量应重摇: {r}"

# 同上但リロール用尽 → 放弃
r = pick_code([{"color": "rush", "power": 900}], led(buffs={}), 0)
assert r == ("skip", None), f"重摇耗尽应放弃: {r}"

r = pick_code([{"color": "impact", "power": 400}], led(buffs={"impact": 3, "safe": 6}), 0)
assert r == ("skip", None), "配额全满应放弃"

# 冲突污染回避：safe 配额进行中（已持 2），此时出现 risk（紫与蓝互斥）
l = led(buffs={"safe": 2})
r = pick_code([{"color": "risk", "power": 9999}], l, 0)
assert r == ("skip", None), "拿 risk 会互减正在攒的 safe，应放弃"

# ---- 回复房 ----
assert pick_heal(led()) == "purify", "默认浄化"
assert pick_heal(led(hp_lost_pct=60)) == "rest", "事件连扣血后休憩"

# ---- 事件 ----
opts = [
    {"hp_cost": 10, "erosion_cost": 40, "code_gain": True},   # 偷货：扣血10 侵蚀+40 拿码
    {"hp_cost": 0, "erosion_cost": 0, "item_gain": False},    # （示例第三项）纯观察
]
l = led(erosion=20)
i = pick_event(opts, l)
assert i == 0, f"侵蚀余量充足时拿码选项最优: {i}"

l = led(erosion=95)  # 侵蚀+40 会顶穿安全线 → 重罚
opts2 = [{"hp_cost": 10, "erosion_cost": 40, "code_gain": True},
         {"hp_cost": 0, "erosion_cost": 0}]
assert pick_event(opts2, l) == 1, "高危侵蚀选项应被绕开"

# HP 预算过滤：已扣 25%，预算 30 → 10% 扣血选项不可选
l = led(hp_lost_pct=25)
opts3 = [{"hp_cost": 10, "erosion_cost": 0, "code_gain": True},
         {"hp_cost": 5, "erosion_cost": 30}]
assert pick_event(opts3, l) == 1, "超预算的选项被过滤，选 5% 扣血项"

# 全超预算 → least-bad（先保 HP）
l = led(hp_lost_pct=30)
opts4 = [{"hp_cost": 20, "erosion_cost": 0}, {"hp_cost": 40, "erosion_cost": -30}]
assert pick_event(opts4, l) == 0, "全超预算选 HP 代价最小项"

# ---- 票决策 ----
assert ticket_decision(led(floor=21, target_floor=30)) == "continue"
assert ticket_decision(led(floor=30, target_floor=30)) == "settle"

# ---- 侵蚀动力学 ----
assert erosion_step({}) == 5
assert erosion_step({"safe": 5}) == 0, "5 蓝 Combo 归零"
assert erosion_step({"safe": 5}, second_blue=True) == -5, "双蓝源进房倒扣"
assert erosion_step({"risk": 5}) == 10, "紫 Combo 反向"

print("✅ 深渊规划器全部单测通过")
