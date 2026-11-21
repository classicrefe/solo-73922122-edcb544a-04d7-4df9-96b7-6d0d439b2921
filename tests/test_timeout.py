"""求解超时 / 预算耗尽场景：接口只能给出已证实的结论，不得误报、不得 500。

覆盖的回归点：
  1. 全部求解超时 -> 未判定，不得误报原始记录冲突，不给任何未证实数值；
  2. 请求级总预算耗尽 -> 未判定，且不真正调用求解器；
  3. 纯可行性探测超时但优化 run 正常 -> 结论由优化证据给出，不得误报冲突；
  4. 优化 run 只有 incumbent（FEASIBLE，未证最优）-> 不出具上下界，
     但见证名单仍足以判定“可能达标”；
  5. 某一组净胜分范围未算完 -> 仍返回已证实的判定与另一组已证实的范围；
  6. 不可行记录的诊断求解超时 -> 不得把超时误报为结构性冲突；
  7. 真实求解器在极小时间预算下 -> 出具的任何数值都必须与完整求解一致。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.solver as solver_module
from app.main import app
from app.schemas import RosterCheckRequest
from app.solver import RosterCpSolver, SolveOutcome
from tests.test_large_scale import build_large_instance

client = TestClient(app)
FIXTURES = json.loads(
    (Path(__file__).resolve().parent / "fixtures.json").read_text(encoding="utf-8")
)


def _fake_unknown(self, model, objective_var, maximize=True, x=None):
    """模拟求解器超时：不调用 CP-SAT，直接返回 UNKNOWN。"""
    self.runs += 1
    return SolveOutcome(status_name="UNKNOWN")


def _demote_optimal(self, real_solve, model, objective_var, maximize=True, x=None):
    """模拟超时拿到 incumbent：求解结果可用但未证实最优。"""
    out = real_solve(self, model, objective_var, maximize=maximize, x=x)
    if out.status_name == "OPTIMAL":
        out.status_name = "FEASIBLE"
    return out


# --------------------------------------------------------------------------- #
# 1. 全部求解超时
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("fixture", sorted(FIXTURES.keys()))
def test_total_timeout_marks_undetermined(fixture: str, monkeypatch) -> None:
    """所有求解 run 超时：标未判定，绝不误报冲突，不出具任何未证实数值。"""
    monkeypatch.setattr(RosterCpSolver, "_solve", _fake_unknown)
    resp = client.post("/api/v1/roster/check", json=FIXTURES[fixture]["request"])
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "undetermined"
    assert body["status_label_zh"] == "未判定"
    assert body["feasible"] is None
    assert body["shared_seconds_min"] is None
    assert body["shared_seconds_max"] is None
    assert body["net_bounds_all_feasible"] is None
    assert body["net_bounds_among_qualifying"] is None
    assert body["qualifying_witness"] is None
    assert body["non_qualifying_witness"] is None
    # 未证实无解，不得报冲突（即使静态预检有疑似冲突，也只能写进 evidence）
    assert body["conflicts"] == []
    assert body["bounds_proven_optimal"] is False
    assert body["evidence"]
    assert any("未在时限内判定" in e for e in body["evidence"])


def test_conflict_fixture_timeout_not_reported_as_conflict(monkeypatch) -> None:
    """回归：原实现把基础可行性超时当成记录冲突。超时不得再升级为冲突。"""
    monkeypatch.setattr(RosterCpSolver, "_solve", _fake_unknown)
    body = client.post(
        "/api/v1/roster/check", json=FIXTURES["infeasible_record"]["request"]
    ).json()
    assert body["status"] == "undetermined"
    assert body["feasible"] is None
    assert body["conflicts"] == []
    # 该夹具静态预检能发现疑似冲突，应如实写进证据说明而不是直接判定
    assert any("疑似冲突" in e for e in body["evidence"])


# --------------------------------------------------------------------------- #
# 2. 请求级总预算耗尽
# --------------------------------------------------------------------------- #
def test_budget_exhausted_marks_undetermined(monkeypatch) -> None:
    """总预算为 0：所有 run 被跳过（SKIPPED），结论为未判定而非冲突。"""
    monkeypatch.setattr("app.main.TOTAL_TIME_BUDGET_SECONDS", 0.0)
    body = client.post(
        "/api/v1/roster/check", json=FIXTURES["possible"]["request"]
    ).json()
    assert body["status"] == "undetermined"
    assert body["feasible"] is None
    assert body["conflicts"] == []
    assert body["solver_runs"] == 0  # 没有真正调用求解器
    assert any("被跳过" in e for e in body["evidence"])


# --------------------------------------------------------------------------- #
# 3. 可行性探测超时，但优化 run 正常
# --------------------------------------------------------------------------- #
def test_feasibility_timeout_but_optimization_succeeds(monkeypatch) -> None:
    """纯可行性 run 全部超时：结论由优化 run 的证据给出，不得误报冲突。"""
    real_solve = RosterCpSolver._solve

    def selective(self, model, objective_var, maximize=True, x=None):
        if objective_var is None:  # 纯可行性 run 一律超时
            self.runs += 1
            return SolveOutcome(status_name="UNKNOWN")
        return real_solve(self, model, objective_var, maximize=maximize, x=x)

    monkeypatch.setattr(RosterCpSolver, "_solve", selective)

    expected = {
        "guaranteed": "guaranteed",
        "possible": "possible",
        "impossible": "impossible",
        # 不可行夹具：优化 run 在完整模型上证实 INFEASIBLE，仍可报冲突
        "infeasible_record": "infeasible_record",
    }
    for name, want in expected.items():
        fx = FIXTURES[name]
        body = client.post("/api/v1/roster/check", json=fx["request"]).json()
        assert body["status"] == want, name
        gt = fx["ground_truth"]
        if want == "infeasible_record":
            assert body["feasible"] is False
            assert body["conflicts"]
            continue
        assert body["feasible"] is True
        assert body["conflicts"] == []
        # 优化证据完整 => 边界与完整求解一致
        assert body["shared_seconds_min"] == gt["shared_min"]
        assert body["shared_seconds_max"] == gt["shared_max"]
        assert body["bounds_proven_optimal"] is True
        assert body["verification_errors"] == []


# --------------------------------------------------------------------------- #
# 4. 优化 run 只有未证最优的 incumbent
# --------------------------------------------------------------------------- #
def test_unproven_bounds_withheld_but_verdict_from_witnesses(monkeypatch) -> None:
    """FEASIBLE（非 OPTIMAL）的目标值不得作为上下界出具；
    但找到的达标/不达标名单仍足以判定“可能达标”。"""
    real_solve = RosterCpSolver._solve

    def demote(self, model, objective_var, maximize=True, x=None):
        return _demote_optimal(self, real_solve, model, objective_var, maximize, x)

    monkeypatch.setattr(RosterCpSolver, "_solve", demote)
    body = client.post(
        "/api/v1/roster/check", json=FIXTURES["possible"]["request"]
    ).json()
    # 判定：达标与不达标名单各找到一套 -> 可能达标
    assert body["status"] == "possible"
    assert body["feasible"] is True
    # 未证实最优 -> 不出具任何数值边界
    assert body["shared_seconds_min"] is None
    assert body["shared_seconds_max"] is None
    assert body["net_bounds_all_feasible"] is None
    assert body["net_bounds_among_qualifying"] is None
    assert body["bounds_proven_optimal"] is False
    # 见证名单照常出具并通过独立复核
    assert body["qualifying_witness"]["meets_minimum"] is True
    assert body["non_qualifying_witness"]["meets_minimum"] is False
    assert body["verification"].startswith("已通过独立复核")
    assert body["verification_errors"] == []


def test_unproven_bounds_withheld_guaranteed_and_impossible(monkeypatch) -> None:
    """guaranteed / impossible 的“不存在另一类名单”证明不依赖最优性，
    探测 INFEASIBLE 即可证实；边界同样不出具。"""
    real_solve = RosterCpSolver._solve

    def demote(self, model, objective_var, maximize=True, x=None):
        return _demote_optimal(self, real_solve, model, objective_var, maximize, x)

    monkeypatch.setattr(RosterCpSolver, "_solve", demote)

    body = client.post(
        "/api/v1/roster/check", json=FIXTURES["guaranteed"]["request"]
    ).json()
    assert body["status"] == "guaranteed"
    assert body["shared_seconds_min"] is None
    assert body["shared_seconds_max"] is None
    assert body["net_bounds_all_feasible"] is None
    assert body["qualifying_witness"]["meets_minimum"] is True
    assert body["verification_errors"] == []

    body = client.post(
        "/api/v1/roster/check", json=FIXTURES["impossible"]["request"]
    ).json()
    assert body["status"] == "impossible"
    assert body["net_bounds_among_qualifying"] is None
    assert body["qualifying_witness"] is None
    assert body["non_qualifying_witness"]["meets_minimum"] is False
    assert body["verification_errors"] == []


# --------------------------------------------------------------------------- #
# 5. 某一组净胜分范围未算完
# --------------------------------------------------------------------------- #
def test_partial_all_feasible_bounds_still_return_qualifying_range(monkeypatch) -> None:
    """全部可行名单净胜分下界超时：仍返回已证实判定与达标名单的已证实范围。"""
    real_opt_net = RosterCpSolver.optimize_net

    def flaky(self, maximize, *, shared_seconds_lower_bound=None):
        if shared_seconds_lower_bound is None and not maximize:
            self.runs += 1
            return SolveOutcome(status_name="UNKNOWN")  # 全部可行名单-下界超时
        return real_opt_net(
            self, maximize, shared_seconds_lower_bound=shared_seconds_lower_bound
        )

    monkeypatch.setattr(RosterCpSolver, "optimize_net", flaky)
    body = client.post(
        "/api/v1/roster/check", json=FIXTURES["possible"]["request"]
    ).json()
    gt = FIXTURES["possible"]["ground_truth"]
    assert body["status"] == "possible"
    # 另一组（达标名单）范围两端都证实 -> 照常出具
    qb = body["net_bounds_among_qualifying"]
    assert qb is not None
    assert qb["min_net_points"] == gt["qualifying_net_min"]
    assert qb["max_net_points"] == gt["qualifying_net_max"]
    # 本组缺一端 -> 整组不出具
    assert body["net_bounds_all_feasible"] is None
    assert body["bounds_proven_optimal"] is False
    # 同场秒数极值不受影响
    assert body["shared_seconds_min"] == gt["shared_min"]
    assert body["shared_seconds_max"] == gt["shared_max"]
    assert body["verification_errors"] == []


def test_partial_qualifying_bounds_still_return_all_feasible_range(monkeypatch) -> None:
    """达标名单净胜分求解超时：仍返回已证实判定与全部可行名单的已证实范围。"""
    real_opt_net = RosterCpSolver.optimize_net

    def flaky(self, maximize, *, shared_seconds_lower_bound=None):
        if shared_seconds_lower_bound is not None:
            self.runs += 1
            return SolveOutcome(status_name="UNKNOWN")  # 达标名单净胜分全部超时
        return real_opt_net(
            self, maximize, shared_seconds_lower_bound=shared_seconds_lower_bound
        )

    monkeypatch.setattr(RosterCpSolver, "optimize_net", flaky)
    body = client.post(
        "/api/v1/roster/check", json=FIXTURES["possible"]["request"]
    ).json()
    gt = FIXTURES["possible"]["ground_truth"]
    assert body["status"] == "possible"
    all_b = body["net_bounds_all_feasible"]
    assert all_b is not None
    assert all_b["min_net_points"] == gt["net_min"]
    assert all_b["max_net_points"] == gt["net_max"]
    assert body["net_bounds_among_qualifying"] is None
    assert body["bounds_proven_optimal"] is False
    assert body["verification_errors"] == []


# --------------------------------------------------------------------------- #
# 6. 不可行记录的诊断超时
# --------------------------------------------------------------------------- #
def test_diagnose_infeasibility_timeout_is_honest(monkeypatch) -> None:
    """诊断求解超时不得误报 STRUCTURAL_INFEASIBLE，应如实报无法定位。"""
    req = RosterCheckRequest.model_validate(FIXTURES["infeasible_record"]["request"])
    solver = RosterCpSolver(req)
    monkeypatch.setattr(RosterCpSolver, "_solve", _fake_unknown)
    conflicts = solver.diagnose_infeasibility()
    codes = {c["code"] for c in conflicts}
    assert "STRUCTURAL_INFEASIBLE" not in codes
    assert "DIAGNOSIS_INCONCLUSIVE" in codes


def test_infeasible_record_still_proven_when_base_infeasible() -> None:
    """完整模型被证实 INFEASIBLE：照常报冲突（不受超时逻辑影响）。"""
    body = client.post(
        "/api/v1/roster/check", json=FIXTURES["infeasible_record"]["request"]
    ).json()
    assert body["status"] == "infeasible_record"
    assert body["feasible"] is False
    assert body["conflicts"]


# --------------------------------------------------------------------------- #
# 7. 真实求解器 + 极小时间预算（端到端超时）
# --------------------------------------------------------------------------- #
def test_tiny_time_limit_never_reports_unproven(monkeypatch) -> None:
    """12 人 × 16 段实例：先把完整求解结果作为真值，再把单 run 时限压到
    0.1ms 强制超时；出具的任何数值都必须与真值一致，且不得误报冲突。"""
    inst = build_large_instance(11)
    req = {k: v for k, v in inst.items() if k != "_lineups"}

    truth = client.post("/api/v1/roster/check", json=req).json()
    assert truth["bounds_proven_optimal"] is True

    monkeypatch.setattr(solver_module, "SOLVER_TIME_LIMIT_SECONDS", 0.0001)
    resp = client.post("/api/v1/roster/check", json=req)
    assert resp.status_code == 200
    body = resp.json()

    # 实例本身可行：不得报冲突、不得判 feasible=False
    assert body["status"] != "infeasible_record"
    assert body["feasible"] is not False
    assert body["conflicts"] == []
    assert body["verification_errors"] == []

    # 出具的数值必须等于已证实的真值
    for key in ("shared_seconds_min", "shared_seconds_max"):
        if body[key] is not None:
            assert body[key] == truth[key], key
    for group in ("net_bounds_all_feasible", "net_bounds_among_qualifying"):
        got = body[group]
        if got is not None:
            assert got["min_net_points"] == truth[group]["min_net_points"], group
            assert got["max_net_points"] == truth[group]["max_net_points"], group

    # 非未判定的结论必须与完整求解一致（判定只依赖已证实证据）
    if body["status"] != "undetermined":
        assert body["status"] == truth["status"]
    else:
        assert body["bounds_proven_optimal"] is False
