"""深渊规划器单测：纯逻辑，不碰设备/模型。python poc/abyss_plan_test.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotabyss_agent.abyss_plan import (
    AbyssLedger, Candidate, effective_erosion_cost, erosion_step, event_score,
    parse_event_desc, pick_code, pick_event, pick_heal, pick_room, pick_treasure,
    room_value, ticket_decision,
)


def led(**kw) -> AbyssLedger:
    base = dict(floor=21, erosion=10, keys=120, coins=50,
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

# ---- 侵蚀安全线（2026-09-03 收紧：41F 侵蚀60 连吃两个 +侵蚀事件爆线事故） ----
# 41F 事故场景复现：侵蚀 60 时 +20 拿码选项，投影 80 触安全线 → 拦截
l = led(erosion=60)
opts_safe = [{"hp_cost": 0, "erosion_cost": 20, "code_gain": True},
             {"hp_cost": 0, "erosion_cost": 0}]
assert pick_event(opts_safe, l) == 1, "投影触安全线(60+20=80)的侵蚀选项应被拦截"
# 余量内（45+20=65 < 80）仍允许拿收益
l = led(erosion=45)
assert pick_event(opts_safe, l) == 0, "安全线余量内拿码选项照常最优"

# unknown 侵蚀代价（文案解析不出数字，如「浸食率がMAXまで上昇」）→ 直接不可行：
# 41F 教训——读不懂的卡连收益描述都可能是错的，不做低侵蚀赌博
p = parse_event_desc("浸食率がMAXまで上昇")
assert p["erosion_unknown"] and effective_erosion_cost(p) == 40, "unknown 按 40 保守"
l = led(erosion=20)
opts_unk = [{"hp_cost": 0, "erosion_unknown": True, "item_gain": True},
            {"hp_cost": 0, "erosion_cost": 0}]
assert pick_event(opts_unk, l) == 1, "unknown 侵蚀选项任何侵蚀位都不可行"

# 全选项都带侵蚀且触线 → least-bad 选侵蚀代价最小项
l = led(erosion=85)
opts5 = [{"hp_cost": 0, "erosion_cost": 30}, {"hp_cost": 0, "erosion_cost": 10}]
assert pick_event(opts5, l) == 1, "触线死局选侵蚀代价最小项"

# ---- 选房：高侵蚀时事件房降权（事件强制选一项，常含侵蚀/扣血代价） ----
l = led(erosion=65)
ev = Candidate("event", 500, 400, 21)
ba = Candidate("battle", 300, 400, 21)
assert room_value(ev, l) <= 2.0 < room_value(ba, l), "侵蚀≥safe-20 事件房让位战斗"
assert pick_room([ev, ba], l).type == "battle", "高侵蚀时优先战斗房"
l = led(erosion=30)
assert pick_room([ev, ba], l).type == "event", "低侵蚀时事件房照常优先"

# ---- 事件效果解析（26F/28F 勘探 dump 真实样本，rich 标签剥离） ----
p = parse_event_desc("<color=#4cf37b>浸食率10減少</color>")
assert (p["erosion_gain"], p["erosion_unknown"]) == (10, False)
p = parse_event_desc("<color=#4cf37b>アビスコード獲得</color>\r\n<color=#ff8232>HP40%減少</color>")
assert p["code_gain"] and p["hp_cost"] == 40
p = parse_event_desc("<color=#4cf37b>アイテム獲得</color>\r\n<color=#ff8232>浸食率20上昇</color>")
assert p["item_gain"] and p["erosion_cost"] == 20
p = parse_event_desc("<color=#ff8232>アビスコイン40個消費</color>\r\n<color=#4cf37b>HP20％回復</color>")
assert p["coin_cost"] == 40 and p["hp_gain"] == 20, "全角％回復也要命中"
assert not p["code_gain"], "アビスコイン≠アビスコード，不可误判拿码"
p = parse_event_desc("<color=#4cf37b>アビスコイン30獲得</color>")
assert not p["code_gain"] and not p["item_gain"]
p = parse_event_desc("<color=#4cf37b>アイテム獲得</color>")
assert p["item_gain"], "物品判定不再依赖串入的 locked『選択』字样"

# ---- 宝箱三选一（2026-09-03 改约：必答题，X 离开会锁死地图） ----
# 卡片模型：key=消耗宝箱钥匙/hp=HP-40%/erosion=浸食+40
# 用户指认（2026-09-03）：宝箱钥匙是局内通货（结算即废），与续关継続券是两种东西
# → 有钥匙必第一顺位用钥匙，HP/侵蚀零代价
tk_key = {"kind": "key", "key_cost": 1, "interactable": True}
tk_hp = {"kind": "hp", "hp_cost": 40, "interactable": True}
tk_er = {"kind": "erosion", "erosion_cost": 40, "interactable": True}
assert pick_treasure([tk_key, tk_hp, tk_er], led(erosion=5)) == 0, "有钥匙无脑用钥匙"
assert pick_treasure([tk_key, tk_hp, tk_er], led(erosion=70)) == 0, "高侵蚀有钥匙也是钥匙"
assert pick_treasure([tk_key, tk_hp, tk_er], led(erosion=70, hp_lost_pct=50)) == 0
# 钥匙为 0（不可选）时按 HP/侵蚀止损链
tk_key0 = {"kind": "key", "key_cost": 1, "interactable": False}
assert pick_treasure([tk_key0, tk_hp, tk_er], led(erosion=5)) == 1, "无钥匙+新鲜HP → HP"
assert pick_treasure([tk_key0, tk_hp, tk_er], led(erosion=5, hp_lost_pct=30)) == 2, \
    "无钥匙+HP不新鲜 → 侵蚀(安全线内)"
assert pick_treasure([tk_key0, tk_hp, tk_er], led(erosion=45)) == 1, \
    "无钥匙+侵蚀45+新鲜HP → 仍 HP（侵蚀能省则省）"
assert pick_treasure([tk_key0, tk_hp, tk_er], led(erosion=45, hp_lost_pct=30)) == 2, \
    "无钥匙+侵蚀45+HP不新鲜 → 侵蚀越线但<100，回复房兜底"
assert pick_treasure([tk_key0, tk_hp, tk_er], led(erosion=70, hp_lost_pct=30)) == 1, \
    "无钥匙+侵蚀70+HP不新鲜 → 侵蚀=暴毙，只能血线赌战斗"
assert pick_treasure([tk_key0, tk_hp, tk_er], led(erosion=70)) == 1, \
    "无钥匙+侵蚀70+新鲜HP → HP（绝不主动越安防线）"
# 只剩钥匙可选（HP/侵蚀全灰的假想异常态）→ 用钥匙
opts_only_key = [{"kind": "key", "key_cost": 1, "interactable": True},
                 {"kind": "hp", "hp_cost": 40, "interactable": False},
                 {"kind": "erosion", "erosion_cost": 40, "interactable": False}]
assert pick_treasure(opts_only_key, led(erosion=5)) == 0
# 全不可选 → 报错
try:
    pick_treasure([{"kind": "hp", "hp_cost": 40, "interactable": False}], led())
    assert False, "全不可选应报错"
except ValueError:
    pass

# ---- 票决策 ----
assert ticket_decision(led(floor=21, target_floor=30)) == "continue"
assert ticket_decision(led(floor=30, target_floor=30)) == "settle"

# ---- 侵蚀动力学 ----
assert erosion_step({}) == 5
assert erosion_step({"safe": 5}) == 0, "5 蓝 Combo 归零"
assert erosion_step({"safe": 5}, second_blue=True) == -5, "双蓝源进房倒扣"
assert erosion_step({"risk": 5}) == 10, "紫 Combo 反向"

print("✅ 深渊规划器全部单测通过")
