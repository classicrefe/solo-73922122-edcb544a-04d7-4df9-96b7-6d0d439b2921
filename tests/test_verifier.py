"""复核器与求解器诊断的单元测试。"""
from __future__ import annotations

import json
from pathlib import Path

from app.schemas import RosterCheckRequest
from app.verifier import (
    compute_net_points,
    compute_shared_seconds,
    verify_assignments,
)

FIXTURES = json.loads(
    (Path(__file__).resolve().parent / "fixtures.json").read_text(encoding="utf-8")
)


def _parse(data: dict) -> RosterCheckRequest:
    return RosterCheckRequest.model_validate(data)


def test_verifier_accepts_generated_roster() -> None:
    for key in ("guaranteed", "possible", "impossible"):
        req = _parse(FIXTURES[key]["request"])
        # 夹具本身由可行名单生成；用暴力枚举取一套可行名单复核
        from tests.brute_force import enumerate_rosters

        gen = next(
            enumerate_rosters(
                players=req.players,
                durations=[s.duration_seconds for s in req.segments],
                confirmed=[s.confirmed_players for s in req.segments],
                substitutions=[s.players_substituted for s in req.segments],
                official=req.official_seconds,
            )
        )
        rosters = [sorted(L) for L in gen]
        assert verify_assignments(req, rosters) == []
        assert compute_shared_seconds(req, rosters) >= 0
        assert isinstance(compute_net_points(req, rosters), int)


def test_verifier_detects_wrong_size() -> None:
    req = _parse(FIXTURES["guaranteed"]["request"])
    bad = [[1, 2, 3, 4]] + [[1, 2, 3, 4, 5]] * 3
    errors = verify_assignments(req, bad)
    assert any("在场人数" in e for e in errors)


def test_verifier_detects_missing_confirmed() -> None:
    req = _parse(FIXTURES["guaranteed"]["request"])
    # 第 1 段确认了 1,2,3,4；用 5 顶替 4 -> 缺少确认球员 4
    bad = [[1, 2, 3, 5, 5]] + [[1, 2, 3, 4, 5]] * 3
    errors = verify_assignments(req, bad)
    assert any("缺少已确认球员" in e for e in errors)
    assert any("重复球员" in e for e in errors)


def test_verifier_detects_substitution_mismatch() -> None:
    data = FIXTURES["possible"]["request"]
    req = _parse(data)
    from tests.brute_force import enumerate_rosters

    rosters = [
        sorted(L)
        for L in next(
            enumerate_rosters(
                players=req.players,
                durations=[s.duration_seconds for s in req.segments],
                confirmed=[s.confirmed_players for s in req.segments],
                substitutions=[s.players_substituted for s in req.segments],
                official=req.official_seconds,
            )
        )
    ]
    # 篡改第 2 段（换人发生段）
    tampered = [list(L) for L in rosters]
    tampered[1] = sorted(set(req.players) - set(rosters[0]))[:5]
    errors = verify_assignments(req, tampered)
    assert any("换人" in e or "变动" in e for e in errors)


def test_verifier_detects_seconds_mismatch() -> None:
    data = json.loads(json.dumps(FIXTURES["impossible"]["request"]))
    req = _parse(data)
    from tests.brute_force import enumerate_rosters

    rosters = [
        sorted(L)
        for L in next(
            enumerate_rosters(
                players=req.players,
                durations=[s.duration_seconds for s in req.segments],
                confirmed=[s.confirmed_players for s in req.segments],
                substitutions=[s.players_substituted for s in req.segments],
                official=req.official_seconds,
            )
        )
    ]
    # 篡改官方秒数后复核必须报错
    data["official_seconds"]["1"] += 100
    req2 = _parse(data)
    errors = verify_assignments(req2, rosters)
    assert any("累计秒数" in e for e in errors)


def test_precheck_diagnoses_substitution_conflict() -> None:
    from app.solver import RosterCpSolver

    data = json.loads(json.dumps(FIXTURES["possible"]["request"]))
    # 第 2 段确认 [1,2,7]，把换人从 2 改成 1 -> 强制变动 6 人次 > 允许 2
    data["segments"][1]["players_substituted"] = 1
    req = _parse(data)
    diag = RosterCpSolver(req).precheck()
    codes = {c["code"] for c in diag.conflicts}
    assert "SUBSTITUTION_CONFIRMED_CONFLICT" in codes
