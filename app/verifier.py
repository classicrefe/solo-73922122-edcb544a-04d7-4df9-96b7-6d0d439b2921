"""独立复核器：用纯 Python 重新核对求解器产出的逐段名单。

复核维度：
  1. 段数、每段恰好 5 名名单内且不重复的球员；
  2. 每段包含全部已确认球员；
  3. 逐人累计秒数与官方秒数完全一致；
  4. 相邻段实际变动人次 == 2 * 记录换下人数；
  5. 重新计算目标五人同场秒数与净胜分。
"""
from __future__ import annotations

from typing import Union

from .schemas import LINEUP_SIZE, RosterCheckRequest


def _as_request(req: Union[RosterCheckRequest, dict]) -> RosterCheckRequest:
    if isinstance(req, RosterCheckRequest):
        return req
    return RosterCheckRequest.model_validate(req)


def verify_assignments(
    req: Union[RosterCheckRequest, dict], assignments: list[list[int]]
) -> list[str]:
    req = _as_request(req)
    errors: list[str] = []
    roster = set(req.players)

    if len(assignments) != len(req.segments):
        errors.append(f"段数不符：期望 {len(req.segments)}，实际 {len(assignments)}")
        return errors

    for t, lineup in enumerate(assignments):
        tag = f"第 {t + 1} 段"
        if len(lineup) != LINEUP_SIZE:
            errors.append(f"{tag}在场人数为 {len(lineup)}，应为 5")
        if len(set(lineup)) != len(lineup):
            errors.append(f"{tag}存在重复球员: {lineup}")
        unknown = set(lineup) - roster
        if unknown:
            errors.append(f"{tag}出现名单外球员: {sorted(unknown)}")
        missing = set(req.segments[t].confirmed_players) - set(lineup)
        if missing:
            errors.append(f"{tag}缺少已确认球员: {sorted(missing)}")

    # 换人记录
    for t in range(1, len(assignments)):
        prev, cur = set(assignments[t - 1]), set(assignments[t])
        moved = len(prev.symmetric_difference(cur))
        expected = 2 * req.segments[t].players_substituted
        if moved != expected:
            errors.append(
                f"第 {t + 1} 段实际变动 {moved} 人次，"
                f"换人记录要求 {expected} 人次"
            )

    # 官方累计秒数
    totals = compute_player_totals(req, assignments)
    for p in req.players:
        if totals.get(p, 0) != req.official_seconds[p]:
            errors.append(
                f"球员 {p} 累计秒数 {totals.get(p, 0)} "
                f"与官方秒数 {req.official_seconds[p]} 不一致"
            )
    return errors


def compute_player_totals(
    req: Union[RosterCheckRequest, dict], assignments: list[list[int]]
) -> dict[int, int]:
    req = _as_request(req)
    totals = {p: 0 for p in req.players}
    for t, lineup in enumerate(assignments):
        for p in lineup:
            totals[p] += req.segments[t].duration_seconds
    return totals


def compute_shared_seconds(
    req: Union[RosterCheckRequest, dict], assignments: list[list[int]]
) -> int:
    req = _as_request(req)
    target = set(req.target_lineup)
    return sum(
        req.segments[t].duration_seconds
        for t, lineup in enumerate(assignments)
        if target <= set(lineup)
    )


def compute_net_points(
    req: Union[RosterCheckRequest, dict], assignments: list[list[int]]
) -> int:
    req = _as_request(req)
    target = set(req.target_lineup)
    return sum(
        req.segments[t].team_net_points
        for t, lineup in enumerate(assignments)
        if target <= set(lineup)
    )


def actual_substitution_counts(assignments: list[list[int]]) -> list[int]:
    """逐段实际换下人数（首段为 0）。"""
    counts = [0]
    for t in range(1, len(assignments)):
        prev, cur = set(assignments[t - 1]), set(assignments[t])
        counts.append(len(prev - cur))
    return counts
