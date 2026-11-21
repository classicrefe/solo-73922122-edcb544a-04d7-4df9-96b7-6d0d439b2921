"""端到端 API 测试：冻结夹具 + 随机暴力枚举真值对照。"""
from __future__ import annotations

import json
import random
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests.brute_force import ground_truth
from tests.generator import (
    add_conflicting_confirmation,
    generate_feasible_instance,
    inflate_substitution,
    perturb_seconds,
)

client = TestClient(app)
FIXTURES = json.loads(
    (Path(__file__).resolve().parent / "fixtures.json").read_text(encoding="utf-8")
)


def expected_status(gt: dict, minimum: int) -> str:
    if not gt["feasible"]:
        return "infeasible_record"
    if gt["shared_max"] < minimum:
        return "impossible"
    if gt["shared_min"] >= minimum:
        return "guaranteed"
    return "possible"


def assert_witness_consistent(req: dict, payload: dict, witness: dict) -> None:
    """复核单套见证名单。"""
    roster = set(req["players"])
    target = set(req["target_lineup"])
    segs = req["segments"]
    assert len(witness["assignments"]) == len(segs)

    calc_shared = calc_net = 0
    totals = {p: 0 for p in req["players"]}
    prev = None
    actual_subs = [0]
    for i, a in enumerate(witness["assignments"]):
        on = a["players_on_court"]
        assert len(on) == 5
        assert len(set(on)) == 5
        assert set(on) <= roster
        assert set(a["confirmed_players"]) <= set(on)
        assert a["duration_seconds"] == segs[i]["duration_seconds"]
        assert a["team_net_points"] == segs[i]["team_net_points"]
        for p in on:
            totals[p] += segs[i]["duration_seconds"]
        if target <= set(on):
            assert a["target_all_on_court"] is True
            calc_shared += segs[i]["duration_seconds"]
            calc_net += segs[i]["team_net_points"]
        else:
            assert a["target_all_on_court"] is False
        if prev is not None:
            actual_subs.append(len(set(prev) - set(on)))
        prev = on

    assert calc_shared == witness["shared_seconds"]
    assert calc_net == witness["net_points"]
    assert witness["meets_minimum"] == (
        witness["shared_seconds"] >= req["minimum_seconds"]
    )
    # 官方秒数
    assert {int(k): v for k, v in witness["player_total_seconds"].items()} == totals
    for p in req["players"]:
        assert totals[p] == req["official_seconds"][str(p)]
    # 换人记录
    assert actual_subs == witness["substitution_counts"]
    for i in range(1, len(segs)):
        assert actual_subs[i] == segs[i]["players_substituted"]


@pytest.mark.parametrize("status", list(FIXTURES.keys()))
def test_frozen_fixtures(status: str) -> None:
    fx = FIXTURES[status]
    req, gt = fx["request"], fx["ground_truth"]
    resp = client.post("/api/v1/roster/check", json=req)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == status
    assert body["feasible"] == gt["feasible"]

    if status == "infeasible_record":
        assert body["conflicts"], "冲突记录必须返回冲突说明"
        assert body["net_bounds_among_qualifying"] is None
        assert body["shared_seconds_min"] is None
        return

    # 与暴力真值逐项对照
    assert body["shared_seconds_min"] == gt["shared_min"]
    assert body["shared_seconds_max"] == gt["shared_max"]
    assert body["bounds_proven_optimal"] is True
    assert body["verification_errors"] == []
    assert body["verification"].startswith("已通过独立复核")

    all_b = body["net_bounds_all_feasible"]
    assert all_b["min_net_points"] == gt["net_min"]
    assert all_b["max_net_points"] == gt["net_max"]
    assert_witness_consistent(req, body, all_b["min_witness"])
    assert_witness_consistent(req, body, all_b["max_witness"])
    assert all_b["max_witness"]["net_points"] == gt["net_max"]
    assert all_b["min_witness"]["net_points"] == gt["net_min"]

    if gt["qualifying_exists"]:
        qb = body["net_bounds_among_qualifying"]
        assert qb is not None
        assert qb["min_net_points"] == gt["qualifying_net_min"]
        assert qb["max_net_points"] == gt["qualifying_net_max"]
        assert_witness_consistent(req, body, qb["min_witness"])
        assert_witness_consistent(req, body, qb["max_witness"])
        assert qb["min_witness"]["meets_minimum"] is True
        assert qb["max_witness"]["meets_minimum"] is True
    else:
        assert body["net_bounds_among_qualifying"] is None
        assert body["status"] == "impossible"


def test_random_property_against_brute_force() -> None:
    """250 个随机实例：API 判定与边界必须与暴力枚举完全一致。"""
    rng = random.Random(777)
    counts = {"feasible": 0, "infeasible_record": 0}
    for n in range(250):
        inst = generate_feasible_instance(rng)
        # 约一半实例施加扰动
        if n % 2 == 1:
            mutator = rng.choice(
                [perturb_seconds, inflate_substitution, add_conflicting_confirmation]
            )
            inst = mutator(inst, rng)
        req = {k: v for k, v in inst.items() if k != "_lineups"}

        gt = ground_truth(
            players=req["players"],
            target=req["target_lineup"],
            minimum=req["minimum_seconds"],
            durations=[s["duration_seconds"] for s in req["segments"]],
            nets=[s["team_net_points"] for s in req["segments"]],
            confirmed=[s["confirmed_players"] for s in req["segments"]],
            substitutions=[s["players_substituted"] for s in req["segments"]],
            official={int(k): v for k, v in req["official_seconds"].items()},
        )
        resp = client.post("/api/v1/roster/check", json=req)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == expected_status(gt, req["minimum_seconds"])
        counts["infeasible_record" if not gt["feasible"] else "feasible"] += 1
        assert body["verification_errors"] == []

        if gt["feasible"]:
            assert body["shared_seconds_min"] == gt["shared_min"]
            assert body["shared_seconds_max"] == gt["shared_max"]
            assert (
                body["net_bounds_all_feasible"]["min_net_points"] == gt["net_min"]
            )
            assert (
                body["net_bounds_all_feasible"]["max_net_points"] == gt["net_max"]
            )
            for w in (
                body["net_bounds_all_feasible"]["min_witness"],
                body["net_bounds_all_feasible"]["max_witness"],
            ):
                assert_witness_consistent(req, body, w)
            if gt["qualifying_exists"]:
                qb = body["net_bounds_among_qualifying"]
                assert qb["min_net_points"] == gt["qualifying_net_min"]
                assert qb["max_net_points"] == gt["qualifying_net_max"]
                assert_witness_consistent(req, body, qb["min_witness"])
                assert_witness_consistent(req, body, qb["max_witness"])
            else:
                assert body["net_bounds_among_qualifying"] is None
        else:
            assert body["conflicts"]

    # 随机样本里两种情况都应出现
    assert counts["feasible"] > 50
    assert counts["infeasible_record"] > 20


def test_guaranteed_threshold_boundary() -> None:
    """阈值恰等于同场秒数下/上界时的判定边界。"""
    fx = FIXTURES["possible"]["request"]
    gt = FIXTURES["possible"]["ground_truth"]
    # minimum = shared_min(0) -> guaranteed
    req = dict(fx)
    req["minimum_seconds"] = gt["shared_min"]
    body = client.post("/api/v1/roster/check", json=req).json()
    assert body["status"] == "guaranteed"
    # minimum = shared_max+100 -> impossible
    req = dict(fx)
    req["minimum_seconds"] = gt["shared_max"] + 100
    body = client.post("/api/v1/roster/check", json=req).json()
    assert body["status"] == "impossible"
    assert body["net_bounds_among_qualifying"] is None
    # minimum 恰为 shared_max -> 仍可能达标（因为 shared_min < 阈值）
    req = dict(fx)
    req["minimum_seconds"] = gt["shared_max"]
    body = client.post("/api/v1/roster/check", json=req).json()
    assert body["status"] == "possible"


def test_health() -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "service": "roster-check-api"}


def test_openapi_examples_present() -> None:
    spec = client.get("/openapi.json").json()
    schema_ref = spec["paths"]["/api/v1/roster/check"]["post"]["requestBody"][
        "content"
    ]["application/json"]
    assert "examples" in schema_ref
    assert set(schema_ref["examples"]) == {"feasible_demo", "conflict_demo"}
    # 两个示例都必须可直接调用
    for name, ex in schema_ref["examples"].items():
        resp = client.post("/api/v1/roster/check", json=ex["value"])
        assert resp.status_code == 200, name


# --------------------------------------------------------------------------- #
# 422 结构校验
# --------------------------------------------------------------------------- #
def test_validation_errors() -> None:
    base = FIXTURES["guaranteed"]["request"]

    bad = json.loads(json.dumps(base))
    bad["target_lineup"] = [1, 2, 3, 4]  # 不足 5 人
    assert client.post("/api/v1/roster/check", json=bad).status_code == 422

    bad = json.loads(json.dumps(base))
    bad["target_lineup"] = [1, 2, 3, 4, 99]  # 名单外
    assert client.post("/api/v1/roster/check", json=bad).status_code == 422

    bad = json.loads(json.dumps(base))
    bad["players"] = [1, 2, 3, 4]  # 少于 5 人
    assert client.post("/api/v1/roster/check", json=bad).status_code == 422

    bad = json.loads(json.dumps(base))
    bad["official_seconds"].pop("1")  # 键不全
    assert client.post("/api/v1/roster/check", json=bad).status_code == 422

    bad = json.loads(json.dumps(base))
    bad["segments"][0]["players_substituted"] = 1  # 首段非 0
    assert client.post("/api/v1/roster/check", json=bad).status_code == 422

    bad = json.loads(json.dumps(base))
    bad["segments"][0]["duration_seconds"] = 0  # 时长非正
    assert client.post("/api/v1/roster/check", json=bad).status_code == 422

    bad = json.loads(json.dumps(base))
    bad["segments"][0]["confirmed_players"] = [1, 1, 2, 3, 4, 5]  # 重复且超 5
    assert client.post("/api/v1/roster/check", json=bad).status_code == 422

    bad = json.loads(json.dumps(base))
    bad["minimum_seconds"] = -1
    assert client.post("/api/v1/roster/check", json=bad).status_code == 422

    # 17 段超限
    bad = json.loads(json.dumps(base))
    seg = dict(bad["segments"][-1])
    bad["segments"] = bad["segments"] + [dict(seg) for _ in range(17 - len(bad["segments"]))]
    assert client.post("/api/v1/roster/check", json=bad).status_code == 422


def test_total_seconds_conflict_message() -> None:
    """人秒总和不一致时给出精确定位。"""
    req = json.loads(json.dumps(FIXTURES["impossible"]["request"]))
    req["official_seconds"]["1"] += 100  # 破坏总和
    body = client.post("/api/v1/roster/check", json=req).json()
    assert body["status"] == "infeasible_record"
    codes = {c["code"] for c in body["conflicts"]}
    assert "TOTAL_SECONDS_MISMATCH" in codes
