# -*- coding: utf-8 -*-
"""
单机争上游（三人不合作斗地主）核心逻辑
======================================
- 完整一副牌 54 张（含小王、大王），3 人各 18 张，恰好发完
- 不合作：三人各自为战，先出完者名次靠前
- 抓到黑桃 3 的玩家先出
- 出牌牌型采用斗地主规则：
  单张 / 对子 / 三张 / 三带一 / 三带二 / 顺子 / 连对 /
  飞机（纯、带单、带对）/ 四带二 / 四带两对 / 炸弹 / 王炸
- 大小王：单张顺序为 2 < 小王 < 大王；双王同出为王炸，全场最大；
  王不进顺子/连对/飞机，也不能作为任何带牌
"""

import random
import time
import gc
from contextlib import contextmanager
from functools import lru_cache
from itertools import combinations, combinations_with_replacement

# ---------------- 牌的定义 ----------------

# 点数权重：3~10 为 3~10；J=11 Q=12 K=13 A=14 2=15；小王=16 大王=17
SUITS = ["spade", "heart", "club", "diamond"]
SUIT_SYMBOL = {"spade": "\u2660", "heart": "\u2665", "club": "\u2663", "diamond": "\u2666"}
SMALL_JOKER_VALUE = 16
BIG_JOKER_VALUE = 17
SMALL_JOKER_SUIT = "small_joker"
BIG_JOKER_SUIT = "big_joker"
JOKER_SUITS = (SMALL_JOKER_SUIT, BIG_JOKER_SUIT)
SMALL_JOKER = (SMALL_JOKER_VALUE, SMALL_JOKER_SUIT)
BIG_JOKER = (BIG_JOKER_VALUE, BIG_JOKER_SUIT)
VALUE_NAMES = {
    3: "3", 4: "4", 5: "5", 6: "6", 7: "7", 8: "8", 9: "9", 10: "10",
    11: "J", 12: "Q", 13: "K", 14: "A", 15: "2",
    SMALL_JOKER_VALUE: "小王", BIG_JOKER_VALUE: "大王",
}

# 牌型
SINGLE = "single"
PAIR = "pair"
TRIPLE = "triple"
TRIPLE_ONE = "triple_one"      # 三带一
TRIPLE_TWO = "triple_two"      # 三带二
STRAIGHT = "straight"          # 顺子
STRAIGHT_PAIR = "straight_pair"  # 连对
PLANE = "plane"                # 纯飞机
PLANE_SINGLE = "plane_single"  # 飞机带单
PLANE_PAIR = "plane_pair"      # 飞机带对
FOUR_TWO = "four_two"          # 四带二（6张）
FOUR_TWO_PAIR = "four_two_pair"  # 四带两对（8张）
BOMB = "bomb"                  # 炸弹
ROCKET = "rocket"              # 王炸

PLANE_MAX_VALUE = 14  # 飞机/顺子/连对最大到 A，不含 2 和王
PLAYER_COUNT = 3
CARDS_PER_PLAYER = 18


def build_deck():
    """生成一副 54 张牌（含大小王）。普通牌为 (value, suit)，王为 (16/17, joker 花色)。"""
    deck = []
    for v in range(3, 16):
        for s in SUITS:
            deck.append((v, s))
    deck.append(SMALL_JOKER)
    deck.append(BIG_JOKER)
    return deck


def deal(rng=None):
    """洗牌发牌：54 张，3 家各 18 张，恰好发完。返回 (hands, [])。

    rng 可传入 random.Random 实例用于自博弈复现；默认用全局 random。
    """
    rng = rng or random
    deck = build_deck()
    rng.shuffle(deck)
    hands = [sorted(deck[i * CARDS_PER_PLAYER:(i + 1) * CARDS_PER_PLAYER],
                    key=lambda c: (c[0], c[1]))
             for i in range(PLAYER_COUNT)]
    return hands, []


def first_player(hands):
    """抓到黑桃 3 的玩家先出（黑桃 3 必然在某家手中）。"""
    target = (3, "spade")
    for i, h in enumerate(hands):
        if target in h:
            return i
    return random.randrange(PLAYER_COUNT)


# ---------------- 牌型识别 ----------------

class Play:
    """一次出牌：cards 为牌元组列表，info 为 (type, key, length)。"""
    __slots__ = ("cards", "type", "key", "length")

    def __init__(self, cards, ptype, key, length=1):
        self.cards = tuple(cards)
        self.type = ptype
        self.key = key
        self.length = length

    def __repr__(self):
        return f"Play({self.type}, key={self.key}, len={self.length}, {self.cards})"

    @property
    def sig(self):
        """牌型签名：(类型, 关键点数, 长度)，残局求解器记忆化用。"""
        return (self.type, self.key, self.length)

    @property
    def is_bomb(self):
        return self.type in (BOMB, ROCKET)


def _group(cards):
    """按点数分组，返回 {value: [cards]}（保留原牌）。"""
    g = {}
    for c in cards:
        g.setdefault(c[0], []).append(c)
    return g


def find_all_plays(cards):
    """
    枚举一手牌中所有合法出牌组合（用于 AI 与“提示”功能）。
    返回 Play 列表。
    """
    groups = _group(cards)
    values = sorted(groups.keys())
    result = []

    def add(play_cards, ptype, key, length=1):
        result.append(Play(play_cards, ptype, key, length))

    # 单张 / 对子 / 三张 / 炸弹（王各只有 1 张，只会生成单张）
    for v in values:
        cs = groups[v]
        add(cs[0:1], SINGLE, v)
        if len(cs) >= 2:
            add(cs[0:2], PAIR, v)
        if len(cs) >= 3:
            add(cs[0:3], TRIPLE, v)
        if len(cs) == 4:
            add(cs[0:4], BOMB, v)

    # 三带一：三张 + 1 张其他点数的单牌（王不能作为带牌）
    # 三带二：三张 + 1 个其他点数的对子
    for tv in values:
        if len(groups[tv]) < 3:
            continue
        tri = groups[tv][0:3]
        for uv in values:
            if uv == tv or uv >= SMALL_JOKER_VALUE:
                continue
            ucs = groups[uv]
            # 三带一（同点剩余张数允许，取 1 张）
            add(tri + ucs[0:1], TRIPLE_ONE, tv)
            # 三带二（需该点至少 2 张）
            if len(ucs) >= 2:
                add(tri + ucs[0:2], TRIPLE_TWO, tv)

    # 顺子（3~A，5 张以上，每点 1 张）
    run_values = [v for v in range(3, PLANE_MAX_VALUE + 1) if v in groups]
    runs = []  # 连续段
    seg = []
    for v in run_values:
        if seg and v == seg[-1] + 1:
            seg.append(v)
        else:
            seg = [v]
            runs.append(seg)
    for seg in runs:
        if len(seg) >= 5:
            for s in range(len(seg)):
                for e in range(s + 5, len(seg) + 1):
                    seg_cards = [groups[v][0] for v in seg[s:e]]
                    add(seg_cards, STRAIGHT, seg[e - 1], e - s)

    # 连对（3~A，3 对以上连续）
    pair_values = [v for v in range(3, PLANE_MAX_VALUE + 1)
                   if v in groups and len(groups[v]) >= 2]
    pruns = []
    seg = []
    for v in pair_values:
        if seg and v == seg[-1] + 1:
            seg.append(v)
        else:
            seg = [v]
            pruns.append(seg)
    for seg in pruns:
        if len(seg) >= 3:
            for s in range(len(seg)):
                for e in range(s + 3, len(seg) + 1):
                    seg_cards = []
                    for v in seg[s:e]:
                        seg_cards.extend(groups[v][0:2])
                    add(seg_cards, STRAIGHT_PAIR, seg[e - 1], e - s)

    # 飞机（连续三张，2 组以上，3~A）
    triple_values = [v for v in range(3, PLANE_MAX_VALUE + 1)
                     if v in groups and len(groups[v]) >= 3]
    truns = []
    seg = []
    for v in triple_values:
        if seg and v == seg[-1] + 1:
            seg.append(v)
        else:
            seg = [v]
            truns.append(seg)
    for seg in truns:
        if len(seg) < 2:
            continue
        for s in range(len(seg)):
            for e in range(s + 2, len(seg) + 1):
                plane_pts = seg[s:e]
                n = len(plane_pts)
                plane_cards = []
                for v in plane_pts:
                    plane_cards.extend(groups[v][0:3])
                # 纯飞机
                add(list(plane_cards), PLANE, plane_pts[-1], n)

                plane_set = set(plane_pts)
                # 带 n 张单翼：候选为非飞机点、非王。
                # 同一非飞机点可以出 2 张当两张单翼（444555+66 是合法飞机带单），
                # 甚至 3 张（333444555+777 合法），故用可重复组合并受手牌张数限制，
                # 与参考引擎 parse 规则一致。
                single_cand = [v for v in values
                               if v not in plane_set and v < SMALL_JOKER_VALUE]
                for wing in combinations_with_replacement(single_cand, n):
                    used = {}
                    wing_cards = []
                    ok = True
                    for v in wing:
                        k = used.get(v, 0)
                        if k >= len(groups[v]):
                            ok = False
                            break
                        wing_cards.append(groups[v][k])
                        used[v] = k + 1
                    if ok:
                        add(plane_cards + wing_cards, PLANE_SINGLE,
                            plane_pts[-1], n)
                # 带 n 个对翼：候选为非飞机点且该点至少 2 张
                pair_cand = [v for v in values
                             if v not in plane_set and v < SMALL_JOKER_VALUE
                             and len(groups[v]) >= 2]
                for wing in combinations(pair_cand, n):
                    wing_cards = []
                    for v in wing:
                        wing_cards.extend(groups[v][0:2])
                    add(plane_cards + wing_cards, PLANE_PAIR, plane_pts[-1], n)

    # 四带二（6 张）：炸弹 + 任意 2 张（可两张不同单牌，也可一对；王不能作带牌）
    # 四带两对（8 张）：炸弹 + 2 个对子（对子不能来自另一个炸弹点）
    for bv in values:
        if len(groups[bv]) != 4:
            continue
        bomb4 = groups[bv][0:4]
        other = [v for v in values if v != bv and v < SMALL_JOKER_VALUE]
        # 6 张：两张翼牌
        wing_single = []
        for v in other:
            wing_single.extend(groups[v][0:min(2, len(groups[v]))])
        # 枚举两张（允许同点取 2 张，即带一对）
        for i in range(len(wing_single)):
            for j in range(i + 1, len(wing_single)):
                c1, c2 = wing_single[i], wing_single[j]
                if c1[0] == c2[0]:
                    # 同点两张：必须该点真的有 2 张（上面切片已保证）
                    add(bomb4 + [c1, c2], FOUR_TWO, bv)
                else:
                    add(bomb4 + [c1, c2], FOUR_TWO, bv)
        # 8 张：两个对子，且该点在出牌中恰好 2 张（排除另一个炸弹点）
        pair_pts = [v for v in other if len(groups[v]) in (2, 3)]
        for w1, w2 in combinations(pair_pts, 2):
            add(bomb4 + groups[w1][0:2] + groups[w2][0:2], FOUR_TWO_PAIR, bv)

    # 王炸：双王同出，全场最大
    if SMALL_JOKER_VALUE in groups and BIG_JOKER_VALUE in groups:
        add([groups[SMALL_JOKER_VALUE][0], groups[BIG_JOKER_VALUE][0]],
            ROCKET, BIG_JOKER_VALUE)

    return result


def evaluate(cards):
    """
    识别给定牌（必须是一次完整出牌）的牌型。
    通过在“全枚举”中按牌集合匹配，保证与 AI 认知完全一致。
    """
    if not cards:
        return None
    target = frozenset(cards)
    for play in find_all_plays(list(cards)):
        if frozenset(play.cards) == target:
            return play
    return None


def can_beat(new_play, last_play):
    """new_play 是否能压过 last_play。last_play 为 None 表示自由出牌。"""
    if last_play is None:
        return True
    if new_play.type == ROCKET:
        return True
    if last_play.type == ROCKET:
        return False
    if new_play.type == BOMB:
        if last_play.type == BOMB:
            return new_play.key > last_play.key
        return True
    if last_play.type == BOMB:
        return False
    # 普通牌型：必须同类型、同长度、主点数更大
    return (new_play.type == last_play.type
            and new_play.length == last_play.length
            and new_play.key > last_play.key)


# ---------------- 电脑 AI（旧版：线性加权贪心，保留供联赛对比） ----------------

def _lead_score(play, hand_counts):
    """自由出牌时的贪心评分：多出牌、先出小牌、保留炸弹、不拆炸弹。"""
    if len(play.cards) == sum(hand_counts.values()):
        return 100000  # 一手出完
    s = len(play.cards) * 10
    if play.type in (STRAIGHT,):
        s += 8
    elif play.type == STRAIGHT_PAIR:
        s += 12
    elif play.type in (PLANE, PLANE_SINGLE, PLANE_PAIR):
        s += 16
    elif play.type in (TRIPLE_ONE, TRIPLE_TWO):
        s += 6
    elif play.type in (FOUR_TWO, FOUR_TWO_PAIR):
        s -= 4
    # 小牌优先（王 key 最大，天然留到最后）
    s -= play.key
    # 炸弹/王炸轻易不出
    if play.type == BOMB:
        s -= 60
    elif play.type == ROCKET:
        s -= 80
    # 拆炸弹惩罚
    play_counts = {}
    for c in play.cards:
        play_counts[c[0]] = play_counts.get(c[0], 0) + 1
    for v, n in play_counts.items():
        if hand_counts.get(v, 0) == 4 and n < 4:
            s -= 25
    return s


def ai_lead_legacy(hand):
    """旧版 AI 自由出牌：选择评分最高的组合。"""
    plays = find_all_plays(hand)
    hand_counts = {}
    for c in hand:
        hand_counts[c[0]] = hand_counts.get(c[0], 0) + 1
    best = None
    best_score = -10 ** 9
    for p in plays:
        sc = _lead_score(p, hand_counts)
        if sc > best_score:
            best_score = sc
            best = p
    return best


def ai_follow_legacy(hand, last_play, min_other_count):
    """
    旧版 AI 跟牌：
    - 普通牌型用最小的同型组合压
    - 炸弹/王炸只在危急关头（有人快出完、自己快出完、或对方先炸）使用
    返回 Play 或 None（不要）。
    """
    plays = find_all_plays(hand)
    candidates = [p for p in plays if can_beat(p, last_play)]
    if not candidates:
        return None

    # 能一手出完直接出
    finish = [p for p in candidates if len(p.cards) == len(hand)]
    if finish:
        finish.sort(key=lambda p: (0 if p.type not in (BOMB, ROCKET) else 1, p.key))
        return finish[0]

    ordinary = [p for p in candidates if p.type not in (BOMB, ROCKET)]
    bombs = [p for p in candidates if p.type in (BOMB, ROCKET)]
    bombs.sort(key=lambda p: (p.type == ROCKET, p.key))

    if ordinary:
        # 同型同长，主点最小；同 key 张数少的优先
        ordinary.sort(key=lambda p: (p.key, len(p.cards)))
        return ordinary[0]

    # 只能靠炸弹
    crisis = min_other_count <= 3 or len(hand) <= 5 or last_play.type == BOMB
    if crisis and bombs:
        return bombs[0]
    return None


# ---------------- 新版电脑 AI（记牌 + 最少回合规划 + 座位博弈） ----------------
#
# 决策只依赖公开信息：自己手牌、已出牌历史、各家手牌张数、当前轮过牌数。
# 算法移植自 dsh-doudizhu 的 bot.js（规则阶梯 + 最少回合拆牌 + 记牌），并按
# 「三人不合作、各自为战、按名次记分」改造：
#   1. 出牌规划：贪心把一手牌拆成最少回合（结构牌优先、单张最后、炸弹压尾）
#   2. 领牌：优先打「能一路走完、外面压不住」的必胜路线；否则选残牌回合数最少的
#   3. 跟牌：优先不拆组的最小压牌；炸弹只在危急时使用
#   4. 记牌：全副牌减去自己手牌和已出牌 = 外面还剩什么，判断某手是否还压得住
#   5. 座位博弈：没有队友；最后一道关（别人都过了）必压，第一道关经联赛实测
#      「不拆组也必压」胜率最高（传统「搭便车放行」实测更差）
#   6. 危急：有人快出完时，领牌出其吃不下的牌型，跟牌不惜拆牌/用炸拦截
# 所有阈值由 league_3p.py 自博弈联赛数据确定，不凭直觉取值。

# 新 AI 的可调参数（联赛扫参用）
DEFAULT_CFG = {
    "danger_at": 1,             # 对手剩余张数 ≤ 此值视为危急（联赛 1 略优于 2）
    "last_gate_break_self": 5,  # 最后一道关愿拆组压牌时，自己手牌上限
    "first_gate_leader": 18,    # 第一道关不拆组也压：领牌者剩余张数上限（18=必压）
    "first_gate_self": 6,       # 第一道关不拆组也压：自己剩余张数上限
    "first_gate_break_self": 0,  # 第一道关拆组压牌的自己手牌上限（0=永不拆）
    "unbeat_beat": "2p",         # 跟牌优先外面压不住的：2p 仅二人残局 / all / off
    # 规则开关（消融实验用）
    "use_run_out": True,        # 领牌必胜路线（严格版：首手也必须外面压不住）
    "near_out_lead": True,      # 危急时领牌出对手吃不下的牌型
    "next_danger": True,        # 第一道关下家快跑完时抢领牌权
    # v3 增强一：二人残局精确求解器（只决定中游/下游，启用时双方最优）
    "solver_max_cards": 20,     # 双方总张数 ≤ 此值才启用
    "solver_nodes": 300000,     # 记忆化搜索节点预算
    "solver_deadline": 0.6,     # 墙钟上限（秒），超时回退底座
    # v3 增强二：抢上游前瞻（三人阶段配对采样自模拟，决定上游）
    "race_min_cards": 4,        # 任一对手剩 ≤ 此值时触发
    "race_max_hand": 12,        # 自己手牌 ≤ 此值才启用（牌多模拟失真）
    "race_samples": 12,         # 平行世界数（所有候选用同一批世界）
    "race_candidates": 3,       # 候选出牌数（底座选择恒在其中）
    "race_margin": 0.25,        # 候选平均分超过底座的阈值才覆盖
    "race_deadline": 0.6,       # 墙钟上限（秒），超时按已完成世界评分
}

# 拆牌规划的牌型加分（与 dsh rules.js 的 PLAN_BONUS 一致）：结构牌先出、单张最后
_PLAN_BONUS = {
    PLANE_PAIR: 14, PLANE_SINGLE: 12, PLANE: 10,
    STRAIGHT: 8, STRAIGHT_PAIR: 8,
    FOUR_TWO_PAIR: 6, FOUR_TWO: 5,
    TRIPLE_TWO: 5, TRIPLE_ONE: 4, TRIPLE: 3,
    PAIR: 2, SINGLE: 0,
    BOMB: 20, ROCKET: 22,
}

# 全副牌按点数的张数分布
_BASE_COUNTS = {v: 4 for v in range(3, 16)}
_BASE_COUNTS[SMALL_JOKER_VALUE] = 1
_BASE_COUNTS[BIG_JOKER_VALUE] = 1


def _remove(hand, cards):
    """从手牌中移除一次出牌（每张牌全局唯一，可直接按集合去）。"""
    used = set(cards)
    return [c for c in hand if c not in used]


def _all_plays_impl(hand_tuple):
    """全枚举的缓存入口（按排序后的手牌元组缓存）。"""
    return find_all_plays(list(hand_tuple))


def _plan_count_impl(hand_tuple):
    """
    贪心拆牌：每轮选「张数 + 牌型加分」最高的合法牌组，返回出完所需回合数。
    它不追求最优拆解，只要能正确给两个候选出牌排序即可（dsh 的 movesToEmpty）。
    """
    rest = list(hand_tuple)
    count = 0
    while rest:
        cands = _all_plays(tuple(sorted(rest)))
        if not cands:
            break
        best = max(cands, key=lambda p: _PLAN_BONUS.get(p.type, 0) + len(p.cards))
        count += 1
        rest = _remove(rest, best.cards)
    return count


# 真实对局使用的常驻小缓存（只缓存真实打过的牌局，内存可控）
_all_plays = lru_cache(maxsize=30000)(_all_plays_impl)
_plan_count = lru_cache(maxsize=40000)(_plan_count_impl)


@contextmanager
def _ephemeral_caches(play_size=60000, plan_size=30000):
    """二人残局求解 / 抢上游前瞻会探索大量「平行世界」假想牌局，
    这些状态一次性使用、绝不能污染常驻缓存（否则内存膨胀且触发
    长 GC 卡顿）。此处换入一次性大缓存，结束即丢弃；同时关闭 GC，
    避免深度模拟中途被数百毫秒的垃圾回收打断。"""
    global _all_plays, _plan_count
    saved_play, saved_plan = _all_plays, _plan_count
    _all_plays = lru_cache(maxsize=play_size)(_all_plays_impl)
    _plan_count = lru_cache(maxsize=plan_size)(_plan_count_impl)
    gc_was_enabled = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        _all_plays, _plan_count = saved_play, saved_plan
        if gc_was_enabled:
            gc.enable()


def enumerate_moves(hand):
    """枚举一手牌的全部合法出牌（带缓存）。"""
    return _all_plays(tuple(sorted(hand)))


def enumerate_beats(hand, last_play):
    """枚举手牌中所有能压过 last_play 的出牌。"""
    return [p for p in enumerate_moves(hand) if can_beat(p, last_play)]


def moves_to_empty(hand):
    """这手牌按贪心规划还需要几个回合出完。"""
    return _plan_count(tuple(sorted(hand)))


def outstanding_for(hand, played):
    """
    外面（两家对手手牌的并集）还剩哪些牌：全副牌 − 自己手牌 − 已出牌。
    三人版 54 张牌全发完、无底牌，所以这个推断是精确的。
    """
    counts = dict(_BASE_COUNTS)
    for c in list(hand) + list(played):
        counts[c[0]] = counts.get(c[0], 0) - 1
    return {v: n for v, n in counts.items() if n > 0}


def outstanding_cards(hand, played):
    """
    外面（两家对手手牌的并集）还剩哪些牌（精确牌对象列表）：
    全副牌 − 自己手牌 − 已出牌。三人版 54 张牌全发完，推断精确。
    二人残局时，外牌恰好等于唯一对手的全部手牌。
    """
    used = set(hand) | set(played)
    return [c for c in build_deck() if c not in used]


def beatable_from_outstanding(play, out):
    """
    外面（outstanding）是否还存在压得住 play 的组合。
    回答的是「有没有可能」，不指认具体座位；宁可保守也不猜牌。
    """
    if play.type == ROCKET:
        return False
    # 双王都还在外面 → 王炸
    if out.get(SMALL_JOKER_VALUE, 0) >= 1 and out.get(BIG_JOKER_VALUE, 0) >= 1:
        return True
    # 非炸弹牌型：外面任意一点还凑着 4 张 → 有炸弹
    if play.type != BOMB and any(n == 4 for n in out.values()):
        return True
    higher = [v for v in range(3, BIG_JOKER_VALUE + 1)
              if out.get(v, 0) > 0 and v > play.key]
    t = play.type
    if t == SINGLE:
        return any(out[v] >= 1 for v in higher)
    if t == PAIR:
        return any(out[v] >= 2 for v in higher)
    if t in (TRIPLE, TRIPLE_ONE, TRIPLE_TWO):
        return any(out[v] >= 3 for v in higher)
    if t == BOMB:
        # 只有更大的炸弹（王炸已在上面处理）
        return any(out[v] == 4 for v in higher)
    if t == FOUR_TWO:
        # 更大的四张 + 另外至少 2 张翼牌
        for v in higher:
            if out[v] == 4 and sum(n for w, n in out.items() if w != v) >= 2:
                return True
        return False
    if t == FOUR_TWO_PAIR:
        # 更大的四张 + 另外至少 2 个对子
        for v in higher:
            if out[v] == 4 and sum(1 for w, n in out.items()
                                   if w != v and n >= 2) >= 2:
                return True
        return False
    if t in (STRAIGHT, STRAIGHT_PAIR, PLANE, PLANE_SINGLE, PLANE_PAIR):
        # 同长度、整体更大的连续牌型滑动窗口（翼牌不建模，找不到就说不能）
        per = 1 if t == STRAIGHT else 2 if t == STRAIGHT_PAIR else 3
        length = play.length
        top = play.key - length + 1
        for start in range(top + 1, PLANE_MAX_VALUE - length + 2):
            if all(out.get(start + step, 0) >= per for step in range(length)):
                return True
        return False
    return True


def run_out_line(hand, out):
    """
    假设自己一直保有领牌权，反复挑出「外面压不住」的最大牌组，模拟一路出完。
    返回 {line, out（能否排空）, cards（沿途能出掉的张数）}。
    """
    rest = sorted(hand)
    line = []
    while rest:
        safe = [p for p in enumerate_moves(rest)
                if not beatable_from_outstanding(p, out)]
        if not safe:
            break
        # 张数多的先出（一次多出），同张数点数低的先出
        safe.sort(key=lambda p: (-len(p.cards), p.key))
        picked = safe[0]
        line.append(picked)
        rest = _remove(rest, picked.cards)
    return {"line": line, "out": not rest, "cards": len(hand) - len(rest)}


def _is_bomb(play):
    return play.type in (BOMB, ROCKET)


def _cheapest_nonbreaking(hand, moves, out=None, active=None, unbeat_mode="2p"):
    """
    最小的压牌，但「不拆组」优先：先按打出后残牌的回合数排序，再按点数小、
    张数多。避免为了压一张 6 拆掉一对 6，凭空多出一个回合。
    unbeat_mode：同回合数下是否优先出外面压不住的牌（保证夺回领牌权）
      - "2p"：仅二人残局（外牌精确属于最后一个对手，判断无噪声）
      - "all"：任何阶段都优先（三人时外牌是两家并集，判断偏保守）
      - "off"：不使用
    """
    prefer_unbeat = (out is not None and
                     ((unbeat_mode == "2p" and active == 2) or unbeat_mode == "all"))

    def key(p):
        after = _remove(hand, p.cards)
        unbeat = 0 if prefer_unbeat and not beatable_from_outstanding(p, out) else 1
        return (_plan_count(tuple(sorted(after))), unbeat, p.key, -len(p.cards))
    return min(moves, key=key)


def _cheapest_bomb(moves):
    """最小的炸弹/王炸（王炸留到最后用）。"""
    return min(moves, key=lambda p: (0 if p.type == BOMB else 1, p.key))


def _outstanding_from_info(hand, info):
    if not info:
        return None
    return outstanding_for(hand, info.get("played", []))


def ai_lead(hand, info=None, cfg=None):
    """
    新版 AI 自由出牌。info 为公开信息字典；不传则退化为无记牌的基础规划。
    """
    cfg = cfg or DEFAULT_CFG
    moves = enumerate_moves(hand)
    if not moves:
        return None

    # 1. 能一手出完，立刻出完（普通牌优先，炸弹/王炸收尾留后）
    winning = [p for p in moves if len(p.cards) == len(hand)]
    if winning:
        return min(winning, key=lambda p: (1 if _is_bomb(p) else 0, p.key))

    out = _outstanding_from_info(hand, info)
    nonbombs = [p for p in moves if not _is_bomb(p)]

    # 2. 必胜路线：从当前手牌出发，每一手都「外面压不住」地一路出完。
    #    严格版：首手本身也必须安全（否则领牌被压，路线根本走不起来）。
    if out is not None and cfg["use_run_out"]:
        line = run_out_line(hand, out)
        if line["out"]:
            return line["line"][0]

    # 3. 有人快出完：优先出其手牌数吃不下的牌型（牌张数 > 对手最少张数）
    if info and cfg["near_out_lead"]:
        seat = info.get("seat", 0)
        opp_counts = [n for s, n in info.get("hand_counts", {}).items()
                      if s != seat]
        if opp_counts:
            danger = min(opp_counts)
            if danger <= cfg["danger_at"]:
                fit = [p for p in nonbombs if len(p.cards) > danger]
                if fit:
                    fit.sort(key=lambda p: (
                        _plan_count(tuple(sorted(_remove(hand, p.cards)))),
                        -len(p.cards), p.key))
                    return fit[0]

    # 4. 常规：残牌回合数最少；非炸弹优先；同回合多带牌、点数低
    def key(p):
        after = _remove(hand, p.cards)
        return (1 if _is_bomb(p) else 0,
                _plan_count(tuple(sorted(after))),
                -len(p.cards), p.key)
    return min(moves, key=key)


def _danger_block(hand, moves, plain, out, active=3, unbeat_mode="2p"):
    """有人快出完时的拦截：优先外面压不住的牌（含炸弹），否则最小硬拦。"""
    if out is not None:
        unanswerable = [p for p in moves if not beatable_from_outstanding(p, out)]
        if unanswerable:
            up = [p for p in unanswerable if not _is_bomb(p)] or unanswerable
            return _cheapest_nonbreaking(hand, up, out, active, unbeat_mode)
    if plain:
        return _cheapest_nonbreaking(hand, plain, out, active, unbeat_mode)
    return _cheapest_bomb(moves)


def ai_follow(hand, last_play, info=None, cfg=None):
    """
    新版 AI 跟牌。info 为公开信息字典；
    为兼容旧调用，第三参数传 int（对手最少张数）时走旧版 AI。
    """
    if isinstance(info, int):
        return ai_follow_legacy(hand, last_play, info)
    cfg = cfg or DEFAULT_CFG
    moves = enumerate_beats(hand, last_play)
    if not moves:
        return None

    # 1. 能一手出完立刻出完
    winning = [p for p in moves if len(p.cards) == len(hand)]
    if winning:
        return min(winning, key=lambda p: (1 if _is_bomb(p) else 0, p.key))

    plain = [p for p in moves if not _is_bomb(p)]
    bombs = [p for p in moves if _is_bomb(p)]

    seat = info.get("seat", 0) if info else 0
    active = info.get("active", 3) if info else 3
    last_player = info.get("last_player", -1) if info else -1
    passes = info.get("passes", 0) if info else 0
    counts = dict(info.get("hand_counts", {})) if info else {}
    finished = list(info.get("finished", [])) if info else []
    out = _outstanding_from_info(hand, info)

    next_seat = (seat + 1) % 3  # 下家：我之后出牌
    last_gate = passes >= active - 2  # 其他活跃玩家都已过，我是最后一道关
    leader_cnt = counts.get(last_player, 99)
    next_danger = (
        next_seat != last_player
        and next_seat not in finished
        and counts.get(next_seat, 99) <= cfg["danger_at"]
    )

    # 2. 领牌者快出完：全力拦截（压不住的牌才算数，必要时炸）
    if leader_cnt <= cfg["danger_at"]:
        return _danger_block(hand, moves, plain, out, active, cfg["unbeat_beat"])

    # 3. 我是第一道关、下家却快出完：不能把这手放给下家接，抢过领牌权
    if cfg["next_danger"] and (not last_gate) and next_danger:
        if plain:
            return _cheapest_nonbreaking(hand, plain, out, active, cfg["unbeat_beat"])
        # 下家只剩 1 张时，炸掉后领多牌，他接不住
        if bombs and counts.get(next_seat, 99) <= 1:
            return _cheapest_bomb(bombs)
        return None

    # 4. 常规压牌
    if plain:
        cb = _cheapest_nonbreaking(hand, plain, out, active, cfg["unbeat_beat"])
        base_turns = _plan_count(tuple(sorted(hand)))
        cost = _plan_count(tuple(sorted(_remove(hand, cb.cards)))) - base_turns
        own = len(hand)
        if cost > 0:
            # 这手要拆组：最后一道关自己快跑完才拆；第一道关按阈值放行
            if last_gate and own <= cfg["last_gate_break_self"]:
                return cb
            if (not last_gate) and cfg["first_gate_break_self"] > 0 \
                    and own <= cfg["first_gate_break_self"]:
                return cb
            return None
        # 不拆组：最后一道关直接压（不压领牌权就没了）
        if last_gate:
            return cb
        # 第一道关：联赛实测「必压」最佳；阈值为 18 时等价于必压
        if leader_cnt <= cfg["first_gate_leader"] or own <= cfg["first_gate_self"]:
            return cb
        return None

    # 5. 只有炸弹能压：非危急不炸
    return None


# ================= v3 增强：二人残局精确求解器 =================
#
# 当场上只剩两人（第一名已产生）时，外牌精确等于唯一对手的手牌，
# 局面是完全信息有限博弈。用记忆化极小极大判定「在双方最优对打下，
# 当前玩家是否先出完」，从而保证必胜残局不打输、必输残局争取少输。
# 只影响中游/下游名次（上游在三人阶段已决出）。
# 总张数、节点数、墙钟任一超限即回退底座启发式，绝不拖慢界面。

class _Budget(Exception):
    """求解器节点预算或墙钟超限。"""


_SKIP = object()  # 求解器不适用/无结论，回退底座启发式


def _sig_play(sig):
    """由牌型签名构造一个只用于比较的占位 Play。"""
    return Play((), sig[0], sig[1], sig[2])


def _remove_tuple(hand, cards):
    used = set(cards)
    return tuple(c for c in hand if c not in used)


def _win(a, b, last, memo, counter, cap, deadline):
    """完全信息二人残局：当前玩家手牌 a、对手手牌 b、桌面 last（sig 或 None）。
    返回 True = 当前玩家在双方最优下先出完（赢）。
    last 为 None 时必须出牌；last 非 None 时可压可不要。"""
    counter[0] += 1
    if counter[0] > cap:
        raise _Budget()
    if (counter[0] & 0x1FF) == 0 and time.monotonic() > deadline[0]:
        raise _Budget()
    key = (a, b, last)
    r = memo.get(key)
    if r is not None:
        return r
    moves = enumerate_moves(list(a))
    result = False
    if last is None:
        for m in moves:
            na = _remove_tuple(a, m.cards)
            if not na or not _win(b, na, m.sig, memo, counter, cap, deadline):
                result = True
                break
    else:
        lp = _sig_play(last)
        for m in moves:
            if not can_beat(m, lp):
                continue
            na = _remove_tuple(a, m.cards)
            if not na or not _win(b, na, m.sig, memo, counter, cap, deadline):
                result = True
                break
        else:
            # 压不过或压了也赢不了：考虑不要，让对手自由领出
            result = not _win(b, a, None, memo, counter, cap, deadline)
    memo[key] = result
    return result


def _solver_pick(hand, opp, last_play, cfg):
    """二人残局精确决策。返回 Play / None（不要）/ _SKIP。"""
    total = len(hand) + len(opp)
    if total > cfg["solver_max_cards"] or not hand or not opp:
        return _SKIP
    a = tuple(sorted(hand))
    b = tuple(sorted(opp))
    memo = {}
    counter = [0]
    cap = cfg["solver_nodes"]
    deadline = [time.monotonic() + cfg["solver_deadline"]]
    with _ephemeral_caches():
        try:
            if last_play is None:
                # 自由领出：找必胜的一手；优先残牌回合少、点数小
                best = None
                bestkey = None
                for m in enumerate_moves(list(a)):
                    if time.monotonic() > deadline[0]:
                        raise _Budget()
                    na = _remove_tuple(a, m.cards)
                    if not na:
                        return m  # 一手出完
                    if _win(b, na, m.sig, memo, counter, cap, deadline):
                        continue  # 打出去后对手必赢，弃
                    k = (moves_to_empty(list(na)), m.key, -len(m.cards))
                    if bestkey is None or k < bestkey:
                        best, bestkey = m, k
                return best if best is not None else _SKIP

            # 跟牌：压得赢就压；压不赢但「不要」能赢就等
            winning = []
            for m in enumerate_beats(list(a), last_play):
                if time.monotonic() > deadline[0]:
                    raise _Budget()
                na = _remove_tuple(a, m.cards)
                if not na:
                    return m  # 一手出完
                if not _win(b, na, m.sig, memo, counter, cap, deadline):
                    winning.append(m)
            if winning:
                return min(winning, key=lambda m: (
                    moves_to_empty(list(_remove_tuple(a, m.cards))),
                    1 if m.is_bomb else 0, m.key))
            if not _win(b, a, None, memo, counter, cap, deadline):
                return None  # 不要就能赢，放对手走
            return _SKIP
        except _Budget:
            return _SKIP


# ================= v3 增强：抢上游前瞻（三人阶段） =================
#
# 上游在三人阶段决出，二人残局求解器帮不上这里。方法：把外牌按对手
# 张数随机采样成若干「平行世界」，每个候选出牌在同一批世界里用全员
# 底座策略自模拟到第一名产生，比平均名次分。三个护栏专门绕开研究报告
# 中普通蒙特卡洛惨败的两个根因：
#   1. 配对采样：所有候选用同一批世界 + 同一随机流，比的是差分不是绝对值
#   2. 评估=执行：模拟中的自己就是底座策略，不会选出现实中接不上的路线
#   3. 保守阈值：候选必须明显更好（race_margin）才覆盖底座选择

_SUIT_CODE = {"spade": 0, "heart": 1, "club": 2, "diamond": 3,
              SMALL_JOKER_SUIT: 4, BIG_JOKER_SUIT: 4}


def _race_seed(hand, played, seat):
    """状态 -> 采样种子（进程内确定性，联赛可复现）。"""
    h = 2166136261
    for c in sorted(hand):
        h = ((h ^ ((c[0] << 3) + _SUIT_CODE[c[1]])) * 16777619) & 0xFFFFFFFF
    for c in played[-8:]:
        h = ((h ^ ((c[0] << 3) + _SUIT_CODE[c[1]])) * 16777619) & 0xFFFFFFFF
    return h ^ (seat * 2654435761) ^ (len(played) * 40503)


def _race_worlds(hand, info, m):
    """把外牌按两家对手的精确张数采样成 m 个平行世界。
    返回 (worlds, opps)，opps 为按座位序的对手座位，worlds 为
    [(对手1手牌, 对手2手牌), ...]。"""
    played = info.get("played", [])
    seat = info.get("seat", 0)
    out_cards = outstanding_cards(hand, played)
    opps = sorted(s for s in range(3)
                  if s != seat and s not in info.get("finished", []))
    counts = info.get("hand_counts", {})
    c1 = counts.get(opps[0], 0)
    rng = random.Random(_race_seed(hand, played, seat))
    worlds = []
    for _ in range(m):
        pool = list(out_cards)
        rng.shuffle(pool)
        h1 = sorted(pool[:c1])
        h2 = sorted(pool[c1:])
        worlds.append((h1, h2))
    return worlds, opps


def _sim_race(hands, start_seat, start_play, last_play, last_player,
              passes, need, finished, played, cfg, deadline=None):
    """从当前局面自模拟到「第一名产生」为止。
    start_seat 的第一步固定为 start_play（候选），其后全员用底座策略。
    返回我的名次分：+2 我是第一个出完的，-1 别人先出完，0 模拟超限；
    超过 deadline 返回 None（调用方废弃整个世界）。"""
    hands = [list(h) for h in hands]
    finished = list(finished)
    played = list(played)
    cur = start_seat
    first = True
    steps = 0

    while len(finished) < 3:
        steps += 1
        if steps > 400:
            return 0
        if deadline is not None and time.monotonic() > deadline:
            return None  # 时间到：短模拟也会在每一步被拦住，废弃该世界
        if cur in finished:
            cur = (cur + 1) % 3
            continue
        hand = hands[cur]

        if first:
            play = start_play
            first = False
        else:
            sim_info = {
                "seat": cur,
                "played": played,
                "hand_counts": {i: len(hands[i]) for i in range(3)
                                if i not in finished},
                "last_player": last_player if last_play is not None else -1,
                "passes": passes if last_play is not None else 0,
                "need_passes": need,
                "active": 3 - len(finished),
                "finished": finished,
            }
            if last_play is None:
                play = ai_lead(hand, sim_info, cfg)
            else:
                play = ai_follow(hand, last_play, sim_info, cfg)

        if play is None:
            if last_play is None:
                return 0  # 底座领出不可能返回空，保守按未知算
            passes += 1
            if passes >= need:
                leader = last_player
                guard = 0
                while leader in finished and guard < 5:
                    leader = (leader + 1) % 3
                    guard += 1
                last_play = None
                last_player = -1
                passes = 0
                cur = leader
            else:
                cur = (cur + 1) % 3
            continue

        for c in play.cards:
            hand.remove(c)
        played.extend(play.cards)
        last_play = play
        last_player = cur
        passes = 0
        active = 3 - len(finished)
        if not hand:
            finished.append(cur)
            if len(finished) == 1:
                return 2 if cur == start_seat else -1
            break
        need = active - 1
        cur = (cur + 1) % 3
    return 0


def _race_candidates(hand, last_play, info, cfg, base_choice):
    """构造候选集（去重，≤ race_candidates 个，底座选择恒在首位）。"""
    cands = [base_choice]
    seen = {id(base_choice)}
    if last_play is None:
        moves = [p for p in enumerate_moves(hand) if not p.is_bomb]

        def key(p):
            after = _remove(hand, p.cards)
            return (_plan_count(tuple(sorted(after))), -len(p.cards), p.key)
    else:
        moves = [p for p in enumerate_beats(hand, last_play)
                 if not p.is_bomb]
        if moves:
            moves.sort(key=lambda p: (
                _plan_count(tuple(sorted(_remove(hand, p.cards)))),
                p.key, -len(p.cards)))
    for p in moves:
        if id(p) in seen:
            continue
        cands.append(p)
        seen.add(id(p))
        if len(cands) >= cfg["race_candidates"]:
            break
    # 跟牌且底座选择是「压」时，把「不要」也纳入候选
    if last_play is not None and base_choice is not None and None not in cands:
        cands.append(None)
        if len(cands) > cfg["race_candidates"]:
            cands = cands[:cfg["race_candidates"] - 1] + [None]
    return cands


def _race_decide(hand, last_play, info, cfg, base_choice):
    """抢上游前瞻。返回 (是否覆盖, 候选)。候选为 None 表示「不要」。"""
    with _ephemeral_caches():
        worlds, opps = _race_worlds(hand, info, cfg["race_samples"])
        cands = _race_candidates(hand, last_play, info, cfg, base_choice)
        if len(cands) <= 1:
            return (False, None)

        seat = info.get("seat", 0)
        scores = [0.0] * len(cands)
        deadline = time.monotonic() + cfg["race_deadline"]
        done = 0
        for w in worlds:
            hands_w = [None, None, None]
            hands_w[seat] = list(hand)
            hands_w[opps[0]] = list(w[0])
            hands_w[opps[1]] = list(w[1])
            world_scores = [0.0] * len(cands)
            world_ok = True
            for ci, cand in enumerate(cands):
                r = _sim_race(
                    hands_w, seat, cand, last_play,
                    info.get("last_player", -1),
                    info.get("passes", 0) if last_play is not None else 0,
                    info.get("need_passes", 0), [], info.get("played", []),
                    cfg, deadline)
                if r is None:
                    world_ok = False
                    break
                world_scores[ci] = r
            if not world_ok:
                break  # 超时：废弃该世界，只用已完整完成的世界
            for ci in range(len(cands)):
                scores[ci] += world_scores[ci]
            done += 1
        if done < 3:
            return (False, None)
        scores = [s / done for s in scores]

        base_score = scores[0]
        best_ci, best_score = 0, base_score
        for ci in range(1, len(cands)):
            if scores[ci] > best_score:
                best_ci, best_score = ci, scores[ci]
        if best_score > base_score + cfg["race_margin"]:
            return (True, cands[best_ci])
        return (False, None)


# ================= v3 决策入口 =================

def ai_decide(hand, last_play=None, info=None, cfg=None):
    """统一决策入口（默认 v4）。
    三人阶段：底座规则 + 抢上游搜索；二人残局：精确求解器，超预算回退底座。
    last_play 为 None 表示自由领出；返回 Play 或 None（不要）。
    传 cfg={"use_v4": False} 可回退 v3 路径。
    """
    cfg = cfg or DEFAULT_CFG
    if cfg is not DEFAULT_CFG:
        use_v4 = cfg.get("use_v4", True)
    else:
        use_v4 = True
    if use_v4:
        return v4_decide(hand, last_play, info, None)

    # v3 路径需要完整底座配置（调用方只可能传 use_v4 开关）
    cfg = DEFAULT_CFG
    info = info or {}
    hand = list(hand)

    # 二人残局：外牌精确等于对手全部手牌，精确求解
    if info.get("active") == 2 and info.get("played") is not None:
        opp = outstanding_cards(hand, info["played"])
        counts = info.get("hand_counts", {})
        other = [n for s, n in counts.items() if s != info.get("seat")]
        if not other or len(opp) == other[0]:
            r = _solver_pick(hand, opp, last_play, cfg)
            if r is not _SKIP:
                return r

    # 底座选择
    if last_play is None:
        base_choice = ai_lead(hand, info, cfg)
    else:
        base_choice = ai_follow(hand, last_play, info, cfg)

    # 抢上游前瞻：三人阶段、有人接近出完、且底座不是一步出完
    if info.get("active") == 3 and len(hand) <= cfg["race_max_hand"]:
        counts = info.get("hand_counts", {})
        if counts and min(counts.values()) <= cfg["race_min_cards"]:
            base_finish = (base_choice is not None
                           and len(base_choice.cards) == len(hand))
            if not base_finish:
                override, choice = _race_decide(
                    hand, last_play, info, cfg, base_choice)
                if override:
                    return choice

    return base_choice


# ================= v4：点数计数引擎 + 精确手数 + 精确收尾搜索 =================
#
# 来源：D:\Qoder\争上游（fastcore.py + brain.py），已用无偏联赛独立验证有效。
# 相对 v3 的四项增强：
# 1. 手牌压成 15 维点数计数向量，记忆化 DFS 求精确最少手数（替代贪心 _plan_count），
#    底座只在「保持最少手数」的候选里挑牌，评估口径与执行口径一致；
# 2. 二人残局求解器在计数向量上记忆化，状态数少几十倍，可算到双方合计 34 张；
# 3. 三人阶段配对采样搜索，模拟到只剩两家时切换精确求解器收尾，终局值是算出来的；
# 4. 按座位记录过牌（pass）上限，用来约束采样，让平行世界更贴近真实。

# 计数向量：index 0..12 -> 3..A,2；13=小王；14=大王
_FC_NR = 15
_FC_JS, _FC_JB = 13, 14
_FC_TWO, _FC_ACE, _FC_TOP = 12, 11, 11

_FC_SINGLE = 0
_FC_PAIR = 1
_FC_TRIPLE = 2
_FC_TRIPLE_ONE = 3
_FC_TRIPLE_TWO = 4
_FC_STRAIGHT = 5
_FC_STRAIGHT_PAIR = 6
_FC_PLANE = 7
_FC_PLANE_SINGLE = 8
_FC_PLANE_PAIR = 9
_FC_FOUR_TWO = 10
_FC_FOUR_TWO_PAIR = 11
_FC_BOMB = 12
_FC_ROCKET = 13
_FC_BOMB_TYPES = (_FC_BOMB, _FC_ROCKET)

_FC_TO_STR = {
    _FC_SINGLE: SINGLE, _FC_PAIR: PAIR, _FC_TRIPLE: TRIPLE,
    _FC_TRIPLE_ONE: TRIPLE_ONE, _FC_TRIPLE_TWO: TRIPLE_TWO,
    _FC_STRAIGHT: STRAIGHT, _FC_STRAIGHT_PAIR: STRAIGHT_PAIR,
    _FC_PLANE: PLANE, _FC_PLANE_SINGLE: PLANE_SINGLE,
    _FC_PLANE_PAIR: PLANE_PAIR, _FC_FOUR_TWO: FOUR_TWO,
    _FC_FOUR_TWO_PAIR: FOUR_TWO_PAIR, _FC_BOMB: BOMB, _FC_ROCKET: ROCKET,
}
_FC_FROM_STR = {v: k for k, v in _FC_TO_STR.items()}

_FC_PLAN_BONUS = {
    _FC_PLANE_PAIR: 14, _FC_PLANE_SINGLE: 12, _FC_PLANE: 10,
    _FC_STRAIGHT: 8, _FC_STRAIGHT_PAIR: 8,
    _FC_FOUR_TWO_PAIR: 6, _FC_FOUR_TWO: 5,
    _FC_TRIPLE_TWO: 5, _FC_TRIPLE_ONE: 4, _FC_TRIPLE: 3,
    _FC_PAIR: 2, _FC_SINGLE: 0,
    _FC_BOMB: 20, _FC_ROCKET: 22,
}
_FC_EMPTY = (0,) * _FC_NR
_FC_FULL = tuple([4] * 13 + [1, 1])


def _fc_val(i):
    return i + 3


def _fc_idx(v):
    return v - 3


def _fc_to_counts(cards):
    """牌列表（任意花色表示）-> 15 维点数计数向量。"""
    c = [0] * _FC_NR
    for v, _s in cards:
        c[v - 3] += 1
    return tuple(c)


def _fc_total(cnt):
    return sum(cnt)


def _fc_lowest(cnt):
    for i in range(_FC_NR):
        if cnt[i]:
            return i
    return -1


def _fc_pick_cards(hand, cnt, nxt):
    """把「计数差」映射回手上真实的牌对象。"""
    need = [cnt[i] - nxt[i] for i in range(_FC_NR)]
    out = []
    for card in hand:
        i = card[0] - 3
        if need[i]:
            need[i] -= 1
            out.append(card)
    return out


def _fc_beats(a, b):
    """出牌签名 a 能否压过 b（b 为 None 表示自由出牌）。"""
    if b is None:
        return True
    at, ak, _al = a
    bt, bk, bl = b
    if at == _FC_ROCKET:
        return True
    if bt == _FC_ROCKET:
        return False
    if at == _FC_BOMB:
        return ak > bk if bt == _FC_BOMB else True
    if bt == _FC_BOMB:
        return False
    return at == bt and _al == bl and ak > bk


def _fc_is_bomb(sig):
    return sig[0] in _FC_BOMB_TYPES


def _fc_sub(cnt, used):
    lst = list(cnt)
    for i, n in used:
        lst[i] -= n
    return tuple(lst)


def _fc_segments(vals):
    segs, cur = [], [vals[0]]
    for v in vals[1:]:
        if v == cur[-1] + 1:
            cur.append(v)
        else:
            segs.append(cur)
            cur = [v]
    segs.append(cur)
    return segs


def _fc_multisets(ranks, k, avail, start=0):
    """从 ranks 中选 k 个元素（可重复），每个 rank 最多取 avail[r] 个。
    按 rank 非递减生成，每项为 (rank, 取数) 的序列。"""
    if k == 0:
        yield ()
        return
    for i in range(start, len(ranks)):
        r = ranks[i]
        for take in range(1, min(avail[r], k) + 1):
            for rest in _fc_multisets(ranks, k - take, avail, i + 1):
                yield ((r, take),) + rest


def _fc_gen_all_impl(cnt):
    """计数向量上的全部合法出牌，元素为 (sig, nxt)。
    sig=(牌型码, 主点索引, 长度)。"""
    nz = [i for i in range(_FC_NR) if cnt[i]]
    if not nz:
        return ()
    out = []
    add = out.append

    for i in nz:
        c = cnt[i]
        add(((_FC_SINGLE, i, 1), _fc_sub(cnt, ((i, 1),))))
        if c >= 2:
            add(((_FC_PAIR, i, 1), _fc_sub(cnt, ((i, 2),))))
        if c >= 3:
            add(((_FC_TRIPLE, i, 1), _fc_sub(cnt, ((i, 3),))))
        if c == 4:
            add(((_FC_BOMB, i, 1), _fc_sub(cnt, ((i, 4),))))

    plain = [i for i in nz if i < _FC_JS]
    for tv in nz:
        if cnt[tv] < 3:
            continue
        for uv in plain:
            if uv == tv:
                continue
            add(((_FC_TRIPLE_ONE, tv, 1),
                 _fc_sub(cnt, ((tv, 3), (uv, 1)))))
            if cnt[uv] >= 2:
                add(((_FC_TRIPLE_TWO, tv, 1),
                     _fc_sub(cnt, ((tv, 3), (uv, 2)))))

    run = [i for i in nz if i <= _FC_TOP]
    if len(run) >= 5:
        for seg in _fc_segments(run):
            if len(seg) < 5:
                continue
            n = len(seg)
            for s in range(n):
                for e in range(s + 5, n + 1):
                    pts = seg[s:e]
                    add(((_FC_STRAIGHT, pts[-1], e - s),
                         _fc_sub(cnt, tuple((p, 1) for p in pts))))

    prun = [i for i in nz if i <= _FC_TOP and cnt[i] >= 2]
    if len(prun) >= 3:
        for seg in _fc_segments(prun):
            if len(seg) < 3:
                continue
            n = len(seg)
            for s in range(n):
                for e in range(s + 3, n + 1):
                    pts = seg[s:e]
                    add(((_FC_STRAIGHT_PAIR, pts[-1], e - s),
                         _fc_sub(cnt, tuple((p, 2) for p in pts))))

    trun = [i for i in nz if i <= _FC_TOP and cnt[i] >= 3]
    if len(trun) >= 2:
        for seg in _fc_segments(trun):
            n = len(seg)
            if n < 2:
                continue
            for s in range(n):
                for e in range(s + 2, n + 1):
                    pts = seg[s:e]
                    k = e - s
                    body = tuple((p, 3) for p in pts)
                    add(((_FC_PLANE, pts[-1], k), _fc_sub(cnt, body)))
                    pset = set(pts)
                    # 单翼：非机身点、非王，允许同点多张（444555+66 合法），
                    # 每个点数最多取手牌张数
                    sc = [v for v in plain if v not in pset]
                    avail = {v: cnt[v] for v in sc}
                    for wing in _fc_multisets(sc, k, avail):
                        add(((_FC_PLANE_SINGLE, pts[-1], k),
                             _fc_sub(cnt, body + tuple(wing))))
                    pc = [v for v in sc if cnt[v] >= 2]
                    if len(pc) >= k:
                        for wing in combinations(pc, k):
                            add(((_FC_PLANE_PAIR, pts[-1], k),
                                 _fc_sub(cnt, body
                                         + tuple((w, 2) for w in wing))))

    for bv in nz:
        if cnt[bv] != 4:
            continue
        bomb4 = ((bv, 4),)
        others = [v for v in plain if v != bv]
        wings = []
        for v in others:
            wings.extend([v] * min(2, cnt[v]))
        nw = len(wings)
        for a in range(nw):
            for b in range(a + 1, nw):
                u = {bv: 4}
                u[wings[a]] = u.get(wings[a], 0) + 1
                u[wings[b]] = u.get(wings[b], 0) + 1
                add(((_FC_FOUR_TWO, bv, 1), _fc_sub(cnt, tuple(u.items()))))
        pair_pts = [v for v in others if cnt[v] in (2, 3)]
        for w1, w2 in combinations(pair_pts, 2):
            add(((_FC_FOUR_TWO_PAIR, bv, 1),
                 _fc_sub(cnt, bomb4 + ((w1, 2), (w2, 2)))))

    if cnt[_FC_JS] and cnt[_FC_JB]:
        add(((_FC_ROCKET, _FC_JB, 1),
             _fc_sub(cnt, ((_FC_JS, 1), (_FC_JB, 1)))))

    seen = set()
    ded = []
    for m in out:
        if m not in seen:
            seen.add(m)
            ded.append(m)
    return tuple(ded)


_fc_gen_all = lru_cache(maxsize=300000)(_fc_gen_all_impl)


def _fc_gen_beats_impl(cnt, last):
    if last is None:
        return _fc_gen_all(cnt)
    return tuple(m for m in _fc_gen_all(cnt) if _fc_beats(m[0], last))


_fc_gen_beats = lru_cache(maxsize=300000)(_fc_gen_beats_impl)

_FC_INF = 1 << 30


def _fc_min_plays_impl(cnt):
    """精确最少手数：最低点数必属于某一手，枚举所有含它的出牌覆盖全部拆法。"""
    i0 = _fc_lowest(cnt)
    if i0 < 0:
        return 0
    c0 = cnt[i0]
    best = _FC_INF
    for _sig, nxt in _fc_gen_all(cnt):
        if c0 - nxt[i0] >= 1:
            v = _fc_min_plays(nxt)
            if v < best:
                best = v
                if best == 0:
                    break
    return best + 1


_fc_min_plays = lru_cache(maxsize=400000)(_fc_min_plays_impl)


def _fc_beatable_impl(out, sig):
    """把 out 当「外面还剩的牌」，问是否可能存在压过 sig 的组合（不枚举）。"""
    t, k, L = sig
    if t == _FC_ROCKET:
        return False
    if out[_FC_JS] and out[_FC_JB]:
        return True
    if t != _FC_BOMB and any(n == 4 for n in out):
        return True
    if t == _FC_SINGLE:
        return any(out[i] >= 1 for i in range(k + 1, _FC_NR))
    if t == _FC_PAIR:
        return any(out[i] >= 2 for i in range(k + 1, _FC_NR))
    if t in (_FC_TRIPLE, _FC_TRIPLE_ONE, _FC_TRIPLE_TWO):
        return any(out[i] >= 3 for i in range(k + 1, _FC_NR))
    if t == _FC_BOMB:
        return any(out[i] == 4 for i in range(k + 1, _FC_NR))
    if t == _FC_FOUR_TWO:
        rest = sum(out)
        for i in range(k + 1, _FC_NR):
            if out[i] == 4 and rest - 4 >= 2:
                return True
        return False
    if t == _FC_FOUR_TWO_PAIR:
        for i in range(k + 1, _FC_NR):
            if out[i] == 4 and sum(1 for j in range(_FC_NR)
                                  if j != i and out[j] >= 2) >= 2:
                return True
        return False
    if t in (_FC_STRAIGHT, _FC_STRAIGHT_PAIR, _FC_PLANE,
             _FC_PLANE_SINGLE, _FC_PLANE_PAIR):
        per = 1 if t == _FC_STRAIGHT else (
            2 if t == _FC_STRAIGHT_PAIR else 3)
        top_start = k - L + 1
        for s in range(top_start + 1, _FC_TOP - L + 2):
            if all(out[s + i] >= per for i in range(L)):
                return True
        return False
    return True


_fc_beatable = lru_cache(maxsize=400000)(_fc_beatable_impl)


def _fc_outstanding_counts(hand, played):
    """外面（对手手牌并集）的点数计数。"""
    c = list(_FC_FULL)
    for v, _s in list(hand) + list(played):
        c[v - 3] -= 1
    return tuple(c)


_FC_CACHED = (_fc_gen_all, _fc_gen_beats, _fc_min_plays, _fc_beatable)


# ---------------- v4 决策层 ----------------

V4_CFG = {
    "tempo_filter": True,
    "danger_at": 1,
    "last_gate_break_self": 5,
    "first_gate_leader": 18,
    "first_gate_self": 6,
    "first_gate_break_self": 0,
    "use_run_out": True,
    "near_out_lead": True,
    "next_danger": True,
    "lead_order": "tempo",
    "keep_control": True,
    # 二人残局精确求解
    "solver": True,
    "solver_max_cards": 26,
    "solver_nodes": 3000000,
    "solver_ms": 300,
    # 按座位过牌推断
    "infer": True,
    "infer_use": True,
    "infer_tries": 8,
    # 三人阶段配对采样搜索
    "search": True,
    "search_min_cards": 5,
    "search_self_max": 14,
    "search_samples": 20,
    "search_candidates": 3,
    "search_margin": 0.15,
    "search_budget_ms": 220,
    "search_rollout_solver": True,
}
_V4_ROLL = {}


def _v4_roll_cfg(cfg):
    """模拟用配置：与实盘同源，只是不再嵌套搜索。"""
    c = _V4_ROLL.get(id(cfg))
    if c is None:
        c = dict(cfg, search=False)
        _V4_ROLL[id(cfg)] = c
    return c


class _V4Sit:
    """一次决策的局面，手牌与外牌均用点数计数。"""
    __slots__ = ("cnt", "out", "T", "seat", "counts", "finished",
                 "last_player", "passes", "need", "caps", "opps", "active_n")

    def __init__(self, cnt, out, seat, counts, finished, last_player,
                 passes, need, caps=None):
        self.cnt = cnt
        self.out = out
        self.T = _fc_min_plays(cnt)
        self.seat = seat
        self.counts = counts
        self.finished = finished
        self.last_player = last_player
        self.passes = passes
        self.need = need
        self.caps = caps
        self.opps = [s for s in range(3)
                     if s != seat and s not in finished]
        self.active_n = 3 - len(finished)


def _v4_ncards(cnt, nxt):
    return _fc_total(cnt) - _fc_total(nxt)


def _v4_run_out_line(cnt, out):
    rest = cnt
    line = []
    while _fc_total(rest):
        safe = [m for m in _fc_gen_all(rest) if not _fc_beatable(out, m[0])]
        if not safe:
            break
        safe.sort(key=lambda m: (-_v4_ncards(rest, m[1]), m[0][1]))
        line.append(safe[0])
        rest = safe[0][1]
    return line, _fc_total(rest) == 0


def _v4_lead_move(sit, cfg):
    cnt = sit.cnt
    moves = _fc_gen_all(cnt)
    if not moves:
        return None

    win = [m for m in moves if _fc_total(m[1]) == 0]
    if win:
        return min(win, key=lambda m: (1 if _fc_is_bomb(m[0]) else 0, m[0][1]))

    plain = [m for m in moves if not _fc_is_bomb(m[0])]
    T = sit.T

    if cfg["use_run_out"]:
        line, done = _v4_run_out_line(cnt, sit.out)
        if done:
            return line[0]

    if cfg["near_out_lead"] and sit.opps:
        danger = min(sit.counts.get(s, 99) for s in sit.opps)
        if danger <= cfg["danger_at"]:
            fit = [m for m in plain if _v4_ncards(cnt, m[1]) > danger]
            if fit:
                fit.sort(key=lambda m: (_fc_min_plays(m[1]),
                                       -_v4_ncards(cnt, m[1]), m[0][1]))
                return fit[0]

    pool = plain or moves
    if cfg["tempo_filter"]:
        keep = [m for m in pool if _fc_min_plays(m[1]) == T - 1]
        if keep:
            pool = keep

    order = cfg["lead_order"]
    bomb = lambda m: 1 if _fc_is_bomb(m[0]) else 0
    if order == "cards":
        key = lambda m: (bomb(m), _fc_min_plays(m[1]),
                         -_v4_ncards(cnt, m[1]), m[0][1])
    elif order == "key":
        key = lambda m: (bomb(m), _fc_min_plays(m[1]), m[0][1],
                         -_v4_ncards(cnt, m[1]))
    elif order == "plain":
        key = lambda m: (bomb(m), _fc_min_plays(m[1]), m[0][1])
    else:
        if cfg["keep_control"]:
            ctl = lambda m: 0 if _fc_beatable(sit.out, m[0]) else 1
        else:
            ctl = lambda m: 1 if _fc_beatable(sit.out, m[0]) else 0
        key = lambda m: (bomb(m), _fc_min_plays(m[1]), ctl(m), m[0][1],
                         -_v4_ncards(cnt, m[1]))
    return min(pool, key=key)


def _v4_cheapest(sit, moves):
    cnt = sit.cnt
    return min(moves, key=lambda m: (_fc_min_plays(m[1]), m[0][1],
                                    -_v4_ncards(cnt, m[1])))


def _v4_cheapest_bomb(moves):
    return min(moves, key=lambda m: (0 if m[0][0] == _FC_BOMB else 1, m[0][1]))


def _v4_danger_block(sit, moves, plain):
    un = [m for m in moves if not _fc_beatable(sit.out, m[0])]
    if un:
        up = [m for m in un if not _fc_is_bomb(m[0])] or un
        return _v4_cheapest(sit, up)
    if plain:
        return _v4_cheapest(sit, plain)
    return _v4_cheapest_bomb(moves)


def _v4_follow_move(sit, last_sig, cfg):
    cnt = sit.cnt
    moves = _fc_gen_beats(cnt, last_sig)
    if not moves:
        return None

    win = [m for m in moves if _fc_total(m[1]) == 0]
    if win:
        return min(win, key=lambda m: (1 if _fc_is_bomb(m[0]) else 0, m[0][1]))

    plain = [m for m in moves if not _fc_is_bomb(m[0])]
    bombs = [m for m in moves if _fc_is_bomb(m[0])]

    seat = sit.seat
    counts, finished = sit.counts, sit.finished
    next_seat = (seat + 1) % 3
    last_gate = sit.passes >= sit.active_n - 2
    leader_cnt = counts.get(sit.last_player, 99)
    next_danger = (next_seat != sit.last_player
                   and next_seat not in finished
                   and counts.get(next_seat, 99) <= cfg["danger_at"])

    if leader_cnt <= cfg["danger_at"]:
        return _v4_danger_block(sit, moves, plain)

    if cfg["next_danger"] and (not last_gate) and next_danger:
        if plain:
            return _v4_cheapest(sit, plain)
        if bombs and counts.get(next_seat, 99) <= 1:
            return _v4_cheapest_bomb(bombs)
        return None

    if plain:
        sig, nxt = _v4_cheapest(sit, plain)
        cost = _fc_min_plays(nxt) - sit.T
        own = _fc_total(cnt)
        if cost > 0:
            if last_gate and own <= cfg["last_gate_break_self"]:
                return (sig, nxt)
            if (not last_gate) and cfg["first_gate_break_self"] > 0 \
                    and own <= cfg["first_gate_break_self"]:
                return (sig, nxt)
            return None
        if last_gate:
            return (sig, nxt)
        if leader_cnt <= cfg["first_gate_leader"] \
                or own <= cfg["first_gate_self"]:
            return (sig, nxt)
        return None

    return None


def _v4_policy(sit, last_sig, cfg):
    if last_sig is None:
        return _v4_lead_move(sit, cfg)
    return _v4_follow_move(sit, last_sig, cfg)


class _V4Budget(Exception):
    pass


_V4_SKIP = object()
_V4_MEMO = {}
_V4_MEMO_CAP = 1500000


def v4_reset():
    """新一局开始：清空计数查表缓存与残局记忆化表（不同发牌的残型不重合）。"""
    _V4_MEMO.clear()
    _V4_ROLL.clear()
    for f in _FC_CACHED:
        f.cache_clear()


def v4_warm(hand):
    """发牌时预热精确手数缓存，把第一手的冷缓存开销挪到发牌瞬间。"""
    cnt = _fc_to_counts(hand)
    for _sig, nxt in _fc_gen_all(cnt):
        _fc_min_plays(nxt)
    _fc_min_plays(cnt)


def _v4_order(moves):
    return sorted(moves, key=lambda m: (_fc_min_plays(m[1]),
                                       1 if _fc_is_bomb(m[0]) else 0,
                                       m[0][1], -_fc_total(m[1])))


def _v4_win(a, b, last, counter, cap, deadline):
    """完全信息二人残局：当前玩家 a、对手 b、桌面 last（sig 或 None）。
    返回 True = 当前玩家在双方最优下先出完。"""
    counter[0] += 1
    if counter[0] > cap:
        raise _V4Budget()
    if time.perf_counter() > deadline:
        raise _V4Budget()
    memo = _V4_MEMO
    key = (a, b, last)
    r = memo.get(key)
    if r is not None:
        return r
    result = False
    if last is None:
        for _sig, nxt in _v4_order(_fc_gen_all(a)):
            if _fc_total(nxt) == 0 or not _v4_win(
                    b, nxt, _sig, counter, cap, deadline):
                result = True
                break
    else:
        found = False
        for sig, nxt in _v4_order(_fc_gen_beats(a, last)):
            if _fc_total(nxt) == 0 or not _v4_win(
                    b, nxt, sig, counter, cap, deadline):
                result = found = True
                break
        if not found:
            result = not _v4_win(b, a, None, counter, cap, deadline)
    if len(memo) < _V4_MEMO_CAP:
        memo[key] = result
    return result


def _v4_solver_exact(a, b, last, counter, cap, deadline):
    """精确决策。返回 (sig, nxt) / None（不要）/ _V4_SKIP（算不完）。"""
    if not _fc_total(a) or not _fc_total(b):
        return _V4_SKIP
    if time.perf_counter() > deadline:
        return _V4_SKIP
    try:
        if last is None:
            best, bestkey = None, None
            for sig, nxt in _v4_order(_fc_gen_all(a)):
                if _fc_total(nxt) == 0:
                    return (sig, nxt)
                if _v4_win(b, nxt, sig, counter, cap, deadline):
                    continue
                k = (_fc_min_plays(nxt), sig[1], -_v4_ncards(a, nxt))
                if bestkey is None or k < bestkey:
                    best, bestkey = (sig, nxt), k
            return best if best is not None else _V4_SKIP

        winning = []
        for sig, nxt in _v4_order(_fc_gen_beats(a, last)):
            if _fc_total(nxt) == 0:
                return (sig, nxt)
            if not _v4_win(b, nxt, sig, counter, cap, deadline):
                winning.append((sig, nxt))
        if winning:
            return min(winning, key=lambda m: (
                _fc_min_plays(m[1]), 1 if _fc_is_bomb(m[0]) else 0, m[0][1]))
        if not _v4_win(b, a, None, counter, cap, deadline):
            return None
        return _V4_SKIP
    except (_V4Budget, RecursionError):
        return _V4_SKIP


def _v4_solver_move(a, b, last_sig, cfg):
    if _fc_total(a) + _fc_total(b) > cfg["solver_max_cards"]:
        return _V4_SKIP
    deadline = time.perf_counter() + cfg["solver_ms"] / 1000.0
    return _v4_solver_exact(a, b, last_sig, [0], cfg["solver_nodes"],
                            deadline)


def _v4_seed(cnt, out, seat, extra):
    h = 2166136261
    for i, n in enumerate(cnt):
        h = ((h ^ ((i << 3) + n)) * 16777619) & 0xFFFFFFFF
    for i, n in enumerate(out):
        h = ((h ^ ((i << 5) + n)) * 16777619) & 0xFFFFFFFF
    return h ^ (seat * 2654435761) ^ (extra * 40503)


def _v4_split(out, n1, rng):
    """把外牌计数随机分成 n1 / 剩下 两家。"""
    slots = []
    for i, n in enumerate(out):
        slots.extend([i] * n)
    rng.shuffle(slots)
    a = [0] * _FC_NR
    for i in slots[:n1]:
        a[i] += 1
    b = [0] * _FC_NR
    for i in slots[n1:]:
        b[i] += 1
    return tuple(a), tuple(b)


_V4_CHAIN_PER = {_FC_STRAIGHT: 1, _FC_STRAIGHT_PAIR: 2,
                 _FC_PLANE: 3, _FC_PLANE_SINGLE: 3, _FC_PLANE_PAIR: 3}


def _v4_violates(cnt, caps):
    """cnt 这一手是否违反它主人积累下来的过牌上限。"""
    if not caps:
        return False
    for (t, L), k in caps.items():
        if t == _FC_SINGLE:
            if any(cnt[i] >= 1 for i in range(k + 1, _FC_NR)):
                return True
        elif t == _FC_PAIR:
            if any(cnt[i] >= 2 for i in range(k + 1, _FC_NR)):
                return True
        elif t in (_FC_TRIPLE, _FC_TRIPLE_ONE, _FC_TRIPLE_TWO):
            if any(cnt[i] >= 3 for i in range(k + 1, _FC_NR)):
                return True
        elif t in _V4_CHAIN_PER:
            per = _V4_CHAIN_PER[t]
            for s in range(k - L + 2, _FC_TOP - L + 2):
                if s <= k - L + 1:
                    continue
                if all(cnt[s + i] >= per for i in range(L)):
                    return True
    return False


def _v4_finish2p(hands, cur, last_sig, last_player, finished, cfg, deadline):
    """二人残局精确收尾，返回这两家的名次顺序；算不完返回 None。"""
    seats = [i for i in range(3) if i not in finished]
    if len(seats) != 2:
        return []
    counter = [0]
    cap = cfg["solver_nodes"]
    p = cur if cur in seats else seats[0]
    ls = last_sig if (last_sig is not None and last_player != p) else None
    order = []
    guard = 0
    while True:
        guard += 1
        if guard > 200:
            return None
        q = seats[1] if p == seats[0] else seats[0]
        mine, opp = hands[p], hands[q]
        if not _fc_total(mine):
            order.append(p)
            break
        if _fc_total(mine) + _fc_total(opp) > cfg["solver_max_cards"]:
            return None
        mv = _v4_solver_exact(mine, opp, ls, counter, cap, deadline)
        if mv is _V4_SKIP:
            return None
        if mv is None:
            ls = None
            p = q
            continue
        sig, nxt = mv
        hands[p] = nxt
        if not _fc_total(nxt):
            order.append(p)
            break
        ls = sig
        p = q
    other = seats[1] if order[0] == seats[0] else seats[0]
    order.append(other)
    return order


def _v4_sim(hands, cur, last_sig, last_player, passes, need, finished,
            cfg, me, deadline):
    """自模拟到终局，返回我的名次（0 上游/1 中游/2 下游）；超时返回 None。"""
    hands = [hands[0], hands[1], hands[2]]
    finished = list(finished)
    rcfg = _v4_roll_cfg(cfg)
    use_solver = cfg["search_rollout_solver"]
    order = None
    steps = 0

    while len(finished) < 3:
        steps += 1
        if steps > 300:
            return None
        if time.perf_counter() > deadline:
            return None
        if cur in finished:
            cur = (cur + 1) % 3
            continue
        cnt = hands[cur]
        if not _fc_total(cnt):
            finished.append(cur)
            if len(finished) == 2:
                break
            cur = (cur + 1) % 3
            continue

        if 3 - len(finished) == 2 and use_solver:
            rest = _v4_finish2p(hands, cur, last_sig, last_player,
                                finished, cfg, deadline)
            if rest is not None:
                order = finished + rest
                break
            use_solver = False

        active = 3 - len(finished)
        out = [0] * _FC_NR
        for i in range(3):
            if i != cur and i not in finished:
                hi = hands[i]
                for k in range(_FC_NR):
                    out[k] += hi[k]
        counts = {i: _fc_total(hands[i])
                  for i in range(3) if i not in finished}
        sit = _V4Sit(cnt, tuple(out), cur, counts, finished,
                     last_player if last_sig is not None else -1,
                     passes if last_sig is not None else 0, need)
        mv = _v4_policy(sit, last_sig, rcfg)

        if mv is None:
            passes += 1
            if passes >= need:
                leader = last_player
                g = 0
                while leader in finished and g < 5:
                    leader = (leader + 1) % 3
                    g += 1
                last_sig = None
                last_player = -1
                passes = 0
                cur = leader
            else:
                cur = (cur + 1) % 3
            continue

        sig, nxt = mv
        hands[cur] = nxt
        last_sig = sig
        last_player = cur
        passes = 0
        if not _fc_total(nxt):
            finished.append(cur)
            if len(finished) == 2:
                break
        need = active - 1
        cur = (cur + 1) % 3

    if order is None:
        order = finished
    if len(order) == 2:
        order = order + [i for i in range(3) if i not in order]
    try:
        return order.index(me)
    except ValueError:
        return None


_V4_RANK_SCORE = (2.0, 0.0, -2.0)


def _v4_run_cand(sit, hands0, cand, last_sig, cfg, deadline):
    hands = [hands0[0], hands0[1], hands0[2]]
    finished = list(sit.finished)
    active = 3 - len(finished)
    if cand is None:
        passes = sit.passes + 1
        need = sit.need
        cur = (sit.seat + 1) % 3
        ls, lp = last_sig, sit.last_player
        if passes >= need:
            leader = lp
            g = 0
            while leader in finished and g < 5:
                leader = (leader + 1) % 3
                g += 1
            ls, lp, passes, cur = None, -1, 0, leader
        r = _v4_sim(hands, cur, ls, lp, passes, need, finished, cfg,
                    sit.seat, deadline)
    else:
        sig, nxt = cand
        hands[sit.seat] = nxt
        if not _fc_total(nxt):
            finished.append(sit.seat)
            if len(finished) == 2:
                return _V4_RANK_SCORE[0] if finished[0] == sit.seat \
                    else _V4_RANK_SCORE[1]
            r = _v4_sim(hands, (sit.seat + 1) % 3, None, -1, 0,
                        3 - len(finished) - 1, finished, cfg, sit.seat,
                        deadline)
        else:
            r = _v4_sim(hands, (sit.seat + 1) % 3, sig, sit.seat, 0,
                        active - 1, finished, cfg, sit.seat, deadline)
    if r is None:
        return None
    return _V4_RANK_SCORE[r]


def _v4_candidates(sit, last_sig, cfg, base):
    cands = []
    seen = set()

    def push(m):
        k = None if m is None else (m[0], m[1])
        if k in seen:
            return
        seen.add(k)
        cands.append(m)

    push(base)
    cnt = sit.cnt
    moves = _fc_gen_all(cnt) if last_sig is None else _fc_gen_beats(
        cnt, last_sig)
    moves = [m for m in moves if not _fc_is_bomb(m[0])]
    moves = sorted(moves, key=lambda m: (_fc_min_plays(m[1]), m[0][1],
                                        -_v4_ncards(cnt, m[1])))
    for m in moves:
        if len(cands) >= cfg["search_candidates"]:
            break
        push(m)
    if last_sig is not None and base is not None:
        push(None)
    return cands


def _v4_search(sit, last_sig, cfg, base):
    opps = sit.opps
    if len(opps) != 2:
        return (False, None)
    if _fc_total(sit.cnt) > cfg["search_self_max"]:
        return (False, None)
    c1 = sit.counts.get(opps[0], 0)
    c2 = sit.counts.get(opps[1], 0)
    if c1 + c2 != _fc_total(sit.out):
        return (False, None)
    cands = _v4_candidates(sit, last_sig, cfg, base)
    if len(cands) <= 1:
        return (False, None)

    deadline = time.perf_counter() + cfg["search_budget_ms"] / 1000.0
    rng = random.Random(_v4_seed(sit.cnt, sit.out, sit.seat,
                                 _fc_total(sit.cnt) * 31
                                 + (last_sig[1] if last_sig else 0)))
    scores = [0.0] * len(cands)
    done = 0
    hands0 = [None, None, None]
    hands0[sit.seat] = sit.cnt
    caps = sit.caps if cfg["infer_use"] else None
    tries = cfg["infer_tries"] if caps else 1

    for _ in range(cfg["search_samples"]):
        if time.perf_counter() > deadline:
            break
        h1 = h2 = None
        for _t in range(tries):
            a, b = _v4_split(sit.out, c1, rng)
            if not caps or (not _v4_violates(a, caps[opps[0]])
                            and not _v4_violates(b, caps[opps[1]])):
                h1, h2 = a, b
                break
        if h1 is None:
            h1, h2 = _v4_split(sit.out, c1, rng)
        hands0[opps[0]], hands0[opps[1]] = h1, h2
        vals = []
        ok = True
        for cand in cands:
            if time.perf_counter() > deadline:
                ok = False
                break
            v = _v4_run_cand(sit, hands0, cand, last_sig, cfg, deadline)
            if v is None:
                ok = False
                break
            vals.append(v)
        if not ok:
            continue
        for ci, v in enumerate(vals):
            scores[ci] += v
        done += 1

    if done < max(3, cfg["search_samples"] // 4):
        return (False, None)
    scores = [s / done for s in scores]
    best_ci, best = 0, scores[0]
    for ci in range(1, len(cands)):
        if scores[ci] > best:
            best_ci, best = ci, scores[ci]
    if best > scores[0] + cfg["search_margin"]:
        return (True, cands[best_ci])
    return (False, None)


def _v4_infer_caps(hand, info):
    """从公开历史还原每家的过牌上限：caps[seat][(牌型码,长度)] = 已知压不过的主点。
    过牌是软信息（可能压不住也可能留着不压），只当倾向用。"""
    hist = info.get("history")
    if not hist:
        return None
    caps = [dict() for _ in range(3)]
    seat = info.get("seat", 0)
    table = None
    for s, cards in hist:
        if cards is None:
            if table is not None and s != seat:
                k = (table[0], table[2])
                if caps[s].get(k, -1) < table[1]:
                    caps[s][k] = table[1]
            continue
        ev = evaluate(list(cards))
        if ev is not None:
            table = (_FC_FROM_STR[ev.type], _fc_idx(ev.key), ev.length)
        else:
            table = None
    return caps


def _v4_build_sit(hand, info, cfg):
    played = info.get("played") or []
    cnt = _fc_to_counts(hand)
    out = _fc_outstanding_counts(hand, played)
    caps = _v4_infer_caps(hand, info) if cfg["infer"] else None
    return _V4Sit(
        cnt, out, info.get("seat", 0), dict(info.get("hand_counts", {})),
        list(info.get("finished", [])), info.get("last_player", -1),
        info.get("passes", 0), info.get("need_passes", 0), caps)


def _v4_make_play(hand, cnt, sig, nxt):
    cards = _fc_pick_cards(hand, cnt, nxt)
    return Play(cards, _FC_TO_STR[sig[0]], _fc_val(sig[1]), sig[2])


def v4_decide(hand, last_play=None, info=None, cfg=None):
    """v4 决策入口。返回 Play 或 None（不要）。"""
    cfg = cfg or V4_CFG
    info = info or {}
    hand = list(hand)
    cnt = _fc_to_counts(hand)
    sit = _v4_build_sit(hand, info, cfg)
    last_sig = None
    if last_play is not None:
        last_sig = (_FC_FROM_STR[last_play.type],
                    _fc_idx(last_play.key), last_play.length)

    # 求解/搜索期间产生大量一次性模拟对象，关 GC 避免中途被长停顿打断；
    # 计数缓存是纯 int 元组、每局 reset，常驻内存有界，不会重蹈 v3 膨胀。
    gc_was_enabled = gc.isenabled()
    gc.disable()
    try:
        if cfg["solver"] and sit.active_n == 2 and len(sit.opps) == 1:
            n = sit.counts.get(sit.opps[0])
            if _fc_total(sit.out) and (n is None or _fc_total(sit.out) == n):
                r = _v4_solver_move(cnt, sit.out, last_sig, cfg)
                if r is None:
                    return None
                if r is not _V4_SKIP:
                    return _v4_make_play(hand, cnt, r[0], r[1])

        base = _v4_policy(sit, last_sig, cfg)

        if cfg["search"] and sit.active_n == 3 and sit.counts:
            if min(sit.counts.values()) <= cfg["search_min_cards"]:
                one_shot = base is not None and _fc_total(base[1]) == 0
                if not one_shot:
                    over, choice = _v4_search(sit, last_sig, cfg, base)
                    if over:
                        base = choice

        if base is None:
            return None
        return _v4_make_play(hand, cnt, base[0], base[1])
    finally:
        if gc_was_enabled:
            gc.enable()
