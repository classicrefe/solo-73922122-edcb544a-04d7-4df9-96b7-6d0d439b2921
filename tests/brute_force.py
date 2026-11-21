"""暴力枚举真值：枚举满足结构约束的全部逐段名单，再核对官方秒数。

仅用于小规模随机实例（P<=8, T<=4），与 CP-SAT 结果做独立对照。
"""
from __future__ import annotations

import itertools
from typing import Iterator

LINEUP = 5


def enumerate_rosters(
    players: list[int],
    durations: list[int],
    confirmed: list[list[int]],
    substitutions: list[int],
    official: dict[int, int],
) -> Iterator[list[frozenset[int]]]:
    """生成所有满足全部约束的完整逐段名单。"""
    P = set(players)
    T = len(durations)
    all_fives = [frozenset(c) for c in itertools.combinations(sorted(P), LINEUP)]
    options = [
        [L for L in all_fives if set(confirmed[t]) <= L] for t in range(T)
    ]
    totals = {p: 0 for p in players}

    def dfs(t: int, prev: frozenset[int] | None) -> Iterator[list[frozenset[int]]]:
        if t == T:
            if all(totals[p] == official[p] for p in players):
                yield []
            return
        for L in options[t]:
            if prev is not None:
                moved = len(L.symmetric_difference(prev))
                if moved != 2 * substitutions[t]:
                    continue
            for p in L:
                totals[p] += durations[t]
            for tail in dfs(t + 1, L):
                yield [L] + tail
            for p in L:
                totals[p] -= durations[t]

    yield from dfs(0, None)


def ground_truth(
    players: list[int],
    target: list[int],
    minimum: int,
    durations: list[int],
    nets: list[int],
    confirmed: list[list[int]],
    substitutions: list[int],
    official: dict[int, int],
) -> dict:
    target_set = set(target)
    shared_values: list[int] = []
    net_values: list[int] = []
    qualifying_nets: list[int] = []
    count = 0
    for rosters in enumerate_rosters(
        players, durations, confirmed, substitutions, official
    ):
        count += 1
        shared = sum(
            durations[t] for t in range(len(durations)) if target_set <= rosters[t]
        )
        net = sum(
            nets[t] for t in range(len(durations)) if target_set <= rosters[t]
        )
        shared_values.append(shared)
        net_values.append(net)
        if shared >= minimum:
            qualifying_nets.append(net)

    if count == 0:
        return {"feasible": False, "count": 0}
    return {
        "feasible": True,
        "count": count,
        "shared_min": min(shared_values),
        "shared_max": max(shared_values),
        "net_min": min(net_values),
        "net_max": max(net_values),
        "qualifying_exists": bool(qualifying_nets),
        "qualifying_net_min": min(qualifying_nets) if qualifying_nets else None,
        "qualifying_net_max": max(qualifying_nets) if qualifying_nets else None,
    }
