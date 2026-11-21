"""小规模随机实例生成器：随机游走生成可行名单，再派生各类变体。"""
from __future__ import annotations

import random


def generate_feasible_instance(rng: random.Random) -> dict:
    """生成一个保证存在可行名单的实例（P<=8, T<=4，小秒数）。"""
    P = rng.randint(5, 8)
    T = rng.randint(1, 4)
    players = list(range(1, P + 1))
    bench_size = P - 5

    # 随机游走生成逐段名单
    lineups: list[set[int]] = [set(rng.sample(players, 5))]
    substitutions = [0]
    for _ in range(1, T):
        k = rng.randint(0, min(bench_size, 3))
        if k == 0:
            lineups.append(set(lineups[-1]))
            substitutions.append(0)
            continue
        cur = set(lineups[-1])
        outs = rng.sample(sorted(cur), k)
        ins = rng.sample(sorted(set(players) - cur), k)
        cur.difference_update(outs)
        cur.update(ins)
        lineups.append(cur)
        substitutions.append(k)

    durations = [rng.randint(1, 5) * 100 for _ in range(T)]
    nets = [rng.randint(-10, 10) for _ in range(T)]

    # 已确认球员：随机揭示子集（保证信息不完全）
    confirmed: list[list[int]] = []
    for L in lineups:
        L_sorted = sorted(L)
        reveal = rng.randint(0, min(4, len(L_sorted)))
        confirmed.append(sorted(rng.sample(L_sorted, reveal)))

    official = {p: 0 for p in players}
    for t, L in enumerate(lineups):
        for p in L:
            official[p] += durations[t]

    target = sorted(rng.sample(players, 5))
    minimum = rng.choice([0, 100, 200, 300, 500, 800, 1000])

    return {
        "players": players,
        "target_lineup": target,
        "minimum_seconds": minimum,
        "official_seconds": {str(p): v for p, v in official.items()},
        "segments": [
            {
                "duration_seconds": durations[t],
                "confirmed_players": confirmed[t],
                "players_substituted": substitutions[t],
                "team_net_points": nets[t],
            }
            for t in range(T)
        ],
        "_lineups": [sorted(L) for L in lineups],
    }


def perturb_seconds(inst: dict, rng: random.Random) -> dict:
    """调整官方秒数（保持总和不变），可能变为不可行。"""
    out = _clone(inst)
    players = out["players"]
    a, b = rng.sample(players, 2)
    delta = rng.choice([100, 200, 300])
    secs = out["official_seconds"]
    if secs[str(b)] >= delta and rng.random() < 0.7:
        secs[str(a)] += delta
        secs[str(b)] -= delta
    else:
        secs[str(a)] += delta
        # 从另一个够扣的人扣
        candidates = [p for p in players if p != a and secs[str(p)] >= delta]
        if not candidates:
            return out
        c = rng.choice(candidates)
        secs[str(c)] -= delta
    return out


def inflate_substitution(inst: dict, rng: random.Random) -> dict:
    """把某段换人记录调大（不超过板凳容量），可能变为不可行。"""
    out = _clone(inst)
    if len(out["segments"]) < 2:
        return out
    t = rng.randint(1, len(out["segments"]) - 1)
    bench = len(out["players"]) - 5
    cur = out["segments"][t]["players_substituted"]
    if cur >= bench:
        return out
    out["segments"][t]["players_substituted"] = rng.randint(cur + 1, bench)
    return out


def add_conflicting_confirmation(inst: dict, rng: random.Random) -> dict:
    """在某段强行加入一名生成时不在场的确认球员，通常导致不可行。"""
    out = _clone(inst)
    t = rng.randrange(len(out["segments"]))
    on_court = set(out["_lineups"][t])
    off_bench = [p for p in out["players"] if p not in on_court]
    if not off_bench:
        return out
    pick = rng.choice(off_bench)
    if pick not in out["segments"][t]["confirmed_players"]:
        out["segments"][t]["confirmed_players"].append(pick)
        out["segments"][t]["confirmed_players"].sort()
    return out


def _clone(inst: dict) -> dict:
    import json

    data = json.loads(json.dumps({k: v for k, v in inst.items() if k != "_lineups"}))
    data["_lineups"] = [list(L) for L in inst["_lineups"]]
    return data
