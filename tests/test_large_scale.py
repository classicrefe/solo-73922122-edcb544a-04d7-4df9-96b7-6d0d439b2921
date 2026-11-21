"""规模上限（12 人 / 16 段）端到端性能与正确性测试。"""
from __future__ import annotations

import random
import time

from fastapi.testclient import TestClient

from app.main import app
from app.verifier import verify_assignments

client = TestClient(app)


def build_large_instance(seed: int) -> dict:
    """随机游走构造 12 人、16 段的可行实例。"""
    rng = random.Random(seed)
    P, T = 12, 16
    players = list(range(1, P + 1))
    lineups = [set(rng.sample(players, 5))]
    subs = [0]
    for _ in range(1, T):
        k = rng.randint(1, 4)
        cur = set(lineups[-1])
        outs = rng.sample(sorted(cur), k)
        ins = rng.sample(sorted(set(players) - cur), k)
        cur.difference_update(outs)
        cur.update(ins)
        lineups.append(cur)
        subs.append(k)

    durations = [rng.randint(30, 180) for _ in range(T)]
    nets = [rng.randint(-12, 12) for _ in range(T)]
    official = {p: 0 for p in players}
    for t, L in enumerate(lineups):
        for p in L:
            official[p] += durations[t]

    confirmed = []
    for L in lineups:
        L_sorted = sorted(L)
        reveal = rng.randint(1, 3)
        confirmed.append(sorted(rng.sample(L_sorted, reveal)))

    return {
        "players": players,
        "target_lineup": sorted(rng.sample(players, 5)),
        "minimum_seconds": rng.choice([0, 120, 300, 600, 900]),
        "official_seconds": {str(p): v for p, v in official.items()},
        "segments": [
            {
                "duration_seconds": durations[t],
                "confirmed_players": confirmed[t],
                "players_substituted": subs[t],
                "team_net_points": nets[t],
            }
            for t in range(T)
        ],
        "_lineups": [sorted(L) for L in lineups],
    }


def test_large_scale_performance() -> None:
    for seed in (11, 22, 33):
        inst = build_large_instance(seed)
        req = {k: v for k, v in inst.items() if k != "_lineups"}
        started = time.perf_counter()
        resp = client.post("/api/v1/roster/check", json=req)
        elapsed = time.perf_counter() - started
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] in ("guaranteed", "possible", "impossible")
        assert body["feasible"] is True
        assert body["verification_errors"] == []
        assert body["bounds_proven_optimal"] is True
        assert elapsed < 20.0, f"seed={seed} 耗时 {elapsed:.1f}s 超出预算"

        # 见证名单逐段复核
        for witness in (
            body["net_bounds_all_feasible"]["min_witness"],
            body["net_bounds_all_feasible"]["max_witness"],
        ):
            rosters = [a["players_on_court"] for a in witness["assignments"]]
            assert verify_assignments(req, rosters) == []

        # 生成时的原始名单本身必须可行且通过复核器
        assert verify_assignments(req, inst["_lineups"]) == []

        # 目标同场秒数落在求解边界内
        target = set(req["target_lineup"])
        gen_shared = sum(
            req["segments"][t]["duration_seconds"]
            for t in range(len(req["segments"]))
            if target <= set(inst["_lineups"][t])
        )
        assert body["shared_seconds_min"] <= gen_shared <= body["shared_seconds_max"]


def test_large_scale_infeasible_conflict() -> None:
    inst = build_large_instance(44)
    req = {k: v for k, v in inst.items() if k != "_lineups"}
    # 破坏人秒总和
    req["official_seconds"]["1"] += 50
    body = client.post("/api/v1/roster/check", json=req).json()
    assert body["status"] == "infeasible_record"
    assert body["conflicts"]
    assert any(c["code"] == "TOTAL_SECONDS_MISMATCH" for c in body["conflicts"])
