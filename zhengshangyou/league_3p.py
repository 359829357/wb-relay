# -*- coding: utf-8 -*-
"""
三人争上游：自博弈联赛脚本（无界面）
====================================
固定随机种子，让不同 AI 版本（旧版 legacy / 新版 new）同桌对战，
统计各版本的名次分布（第 1/2/3 名比例）、平均名次、炸弹参与数。
用于验证新版 AI 的真实强度，并扫描写在 game_core_3p.py 里的规则参数。

用法：
    python league_3p.py            # 跑完整联赛（四种同桌组合）
    python league_3p.py 2000       # 指定每种组合的局数
    python league_3p.py sweep      # 参数扫描（新版 1 家 vs 旧版 2 家）
"""
import random
import sys
import time

import game_core_3p as gc


def make_info(seat, hands, played, last_player, passes, need, finished):
    """组装 AI 可见的公开信息（自己手牌以外的一切都在调用处显式传入）。"""
    return {
        "seat": seat,
        "played": list(played),
        "hand_counts": {i: len(hands[i]) for i in range(3) if i not in finished},
        "last_player": last_player,
        "passes": passes,
        "need_passes": need,
        "active": 3 - len(finished),
        "finished": list(finished),
    }


def play_game(seed, policies, cfg=None):
    """
    完整打一局。policies 为长度 3 的列表，元素为 'legacy' 或 'new'。
    返回 (finished 名次列表, 每家打出的炸弹/王炸数列表)。
    """
    rng = random.Random(seed)
    hands, _ = gc.deal(rng)
    hands = [list(h) for h in hands]

    cur = gc.first_player(hands)
    last_play = None
    last_player = -1
    passes = 0
    need = 0
    finished = []
    played = []
    bombs = [0, 0, 0]
    steps = 0

    while len(finished) < 3:
        steps += 1
        if steps > 20000:
            raise RuntimeError(f"seed={seed} 对局死循环，policies={policies}")
        if cur in finished:
            cur = (cur + 1) % 3
            continue

        hand = hands[cur]
        pol = policies[cur]
        if last_play is None:
            if pol == "legacy":
                play = gc.ai_lead_legacy(hand)
            else:
                info = make_info(cur, hands, played, -1, 0, need, finished)
                play = gc.ai_lead(hand, info, cfg)
        else:
            others = [len(hands[i]) for i in range(3)
                      if i != cur and i not in finished]
            if pol == "legacy":
                play = gc.ai_follow_legacy(
                    hand, last_play, min(others) if others else 99)
            else:
                info = make_info(cur, hands, played, last_player,
                                 passes, need, finished)
                play = gc.ai_follow(hand, last_play, info, cfg)

        if play is None:
            if last_play is None:
                raise RuntimeError(f"seed={seed} 自由出牌却返回不要")
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

        # 合法性校验
        if last_play is not None and not gc.can_beat(play, last_play):
            raise RuntimeError(f"seed={seed} {pol} AI 出牌压不过上家: {play} vs {last_play}")
        for c in play.cards:
            if c not in hand:
                raise RuntimeError(f"seed={seed} AI 出了手里没有的牌: {c}")
        for c in play.cards:
            hand.remove(c)
        played.extend(play.cards)
        if play.type in (gc.BOMB, gc.ROCKET):
            bombs[cur] += 1

        last_play = play
        last_player = cur
        passes = 0
        active = 3 - len(finished)
        if not hand:
            finished.append(cur)
            if len(finished) == 2:
                last = [i for i in range(3) if i not in finished][0]
                finished.append(last)
                break
            need = active - 1
        else:
            need = active - 1
        cur = (cur + 1) % 3

    return finished, bombs


def run_matchup(n, pattern, cfg=None, label=""):
    """
    pattern: 长度 3 的列表，元素 'legacy' / 'new' / 'rotate_new'（随种子轮换）。
    统计每种 AI 版本的名次分布。
    """
    stats = {
        "legacy": {"games": 0, "rank_sum": 0, "places": [0, 0, 0], "bombs": 0},
        "new": {"games": 0, "rank_sum": 0, "places": [0, 0, 0], "bombs": 0},
    }
    t0 = time.time()
    for seed in range(n):
        if pattern == "one_new_rotate":
            # 新版 1 家，座位随种子在 0/1/2 轮换，避免先手/座位偏差
            policies = ["legacy", "legacy", "legacy"]
            policies[seed % 3] = "new"
        elif pattern == "two_new_rotate":
            # 新版 2 家，旧版 1 家的座位轮换
            policies = ["new", "new", "new"]
            policies[seed % 3] = "legacy"
        else:
            policies = list(pattern)
        finished, bombs = play_game(seed, policies, cfg)
        for rank, seat in enumerate(finished):
            pol = policies[seat]
            if pol in stats:
                s = stats[pol]
                s["games"] += 1
                s["rank_sum"] += rank + 1
                s["places"][rank] += 1
                s["bombs"] += bombs[seat]
    dt = time.time() - t0
    print(f"\n=== {label} ===")
    for name in ("legacy", "new"):
        s = stats[name]
        if not s["games"]:
            continue
        g = s["games"]
        p = [100.0 * x / g for x in s["places"]]
        avg = s["rank_sum"] / g
        print(f"  {name:<6} 样本 {g:>5}  第1 {p[0]:5.1f}%  第2 {p[1]:5.1f}%  "
              f"第3 {p[2]:5.1f}%  平均名次 {avg:.3f}  每局炸弹 {s['bombs'] / g:.2f}")
    print(f"  耗时 {dt:.1f}s")
    return stats


def main():
    n = 3000
    mode = "full"
    for arg in sys.argv[1:]:
        if arg.isdigit():
            n = int(arg)
        else:
            mode = arg

    if mode == "sweep":
        print("参数扫描：新版 1 家（座位轮换）vs 旧版 2 家，局数", n)
        base = dict(gc.DEFAULT_CFG)
        variants = [
            # 新默认（严格必胜路线 + 第一道关必压 + 二人残局绝对压牌）
            ("新默认", dict(base)),
            # 绝对压牌范围
            ("绝对压牌 all", {**base, "unbeat_beat": "all"}),
            ("绝对压牌 off", {**base, "unbeat_beat": "off"}),
            # 危急阈值
            ("新默认+danger1", {**base, "danger_at": 1}),
            ("新默认+danger3", {**base, "danger_at": 3}),
            ("绝对压牌all+danger1", {**base, "unbeat_beat": "all", "danger_at": 1}),
            # 拆组
            ("新默认+一道关拆组6", {**base, "first_gate_break_self": 6}),
            ("新默认+末关拆组8", {**base, "last_gate_break_self": 8}),
            # 消融
            ("关掉必胜路线", {**base, "use_run_out": False}),
            ("关掉危急领牌", {**base, "near_out_lead": False}),
            ("关掉下家危急", {**base, "next_danger": False}),
        ]
        results = []
        for name, cfg in variants:
            s = run_matchup(n, "one_new_rotate", cfg, name)
            new = s["new"]
            avg = new["rank_sum"] / new["games"]
            win = 100.0 * new["places"][0] / new["games"]
            results.append((name, avg, win))
        print("\n--- 扫描汇总（新版平均名次越低越好，胜率越高越好）---")
        for name, avg, win in sorted(results, key=lambda x: x[1]):
            print(f"  {name:<26} 平均名次 {avg:.3f}  胜率 {win:.1f}%")
        return

    run_matchup(n, ["legacy", "legacy", "legacy"], label="三家旧版（基准，理论各 1/3）")
    run_matchup(n, "one_new_rotate", label="新版 1 家（座位轮换）vs 旧版 2 家")
    run_matchup(n, "two_new_rotate", label="新版 2 家 vs 旧版 1 家（旧版轮换）")
    run_matchup(n, ["new", "new", "new"], label="三家新版（自洽性检验）")


if __name__ == "__main__":
    main()
