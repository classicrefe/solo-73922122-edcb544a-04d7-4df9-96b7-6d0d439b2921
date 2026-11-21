"""FastAPI 入口：篮球五人阵容达标核对 API。"""
from __future__ import annotations

import time

from fastapi import Body, FastAPI

from .schemas import (
    CheckResponse,
    HealthResponse,
    RosterCheckRequest,
    SegmentAssignment,
    WitnessRoster,
    NetBounds,
)
from .solver import RosterCpSolver, SolveOutcome, TOTAL_TIME_BUDGET_SECONDS
from .verifier import (
    actual_substitution_counts,
    compute_net_points,
    compute_player_totals,
    compute_shared_seconds,
    verify_assignments,
)

app = FastAPI(
    title="篮球五人阵容达标核对 API",
    description=(
        "根据官方逐人在场秒数、换人切分片段与片段确认球员，"
        "用约束规划判定目标五人阵容能否达到最低同场上场时长，"
        "并在达标名单中计算净胜分上下界，每个边界附完整逐段名单供复核。\n\n"
        "判定状态：\n"
        "- `guaranteed`（必定达标）：已证实不存在不达标名单，且记录可行；\n"
        "- `possible`（可能达标）：已找到达标名单与不达标名单各至少一套；\n"
        "- `impossible`（无法达标）：记录可行，且已证实不存在达标名单；\n"
        "- `infeasible_record`（原始记录冲突）：已证实约束无法同时成立，只给冲突，不给达标结论；\n"
        "- `undetermined`（未判定）：求解超时或预算耗尽，证据不足；已证实的事实见 `evidence`，"
        "已证实的边界与见证名单照常返回。\n\n"
        "超时口径：只有求解器证实最优（OPTIMAL）才出具同场秒数极值与净胜分范围；"
        "只有证实无解（INFEASIBLE）才报原始记录冲突。"
    ),
    version="1.1.0",
)

STATUS_ZH = {
    "guaranteed": "必定达标",
    "possible": "可能达标",
    "impossible": "无法达标",
    "infeasible_record": "原始记录冲突",
    "undetermined": "未判定",
}

FEASIBLE_STATUSES = {"OPTIMAL", "FEASIBLE"}


def _build_witness(
    req: RosterCheckRequest,
    label: str,
    outcome: SolveOutcome,
) -> WitnessRoster:
    assert outcome.assignments is not None
    assignments = outcome.assignments
    shared = compute_shared_seconds(req, assignments)
    net = compute_net_points(req, assignments)
    totals = compute_player_totals(req, assignments)

    seg_views = []
    target = set(req.target_lineup)
    for t, lineup in enumerate(assignments):
        confirmed = list(req.segments[t].confirmed_players)
        seg_views.append(
            SegmentAssignment(
                segment_index=t + 1,
                duration_seconds=req.segments[t].duration_seconds,
                players_on_court=lineup,
                confirmed_players=confirmed,
                confirmed_included=all(p in lineup for p in confirmed),
                target_all_on_court=target <= set(lineup),
                team_net_points=req.segments[t].team_net_points,
            )
        )

    return WitnessRoster(
        label=label,
        solver_status=outcome.status_name,
        shared_seconds=shared,
        net_points=net,
        meets_minimum=shared >= req.minimum_seconds,
        assignments=seg_views,
        player_total_seconds={str(p): totals[p] for p in req.players},
        substitution_counts=actual_substitution_counts(assignments),
    )


def _check_witness(
    req: RosterCheckRequest,
    items: list[tuple[WitnessRoster, bool | None]],
) -> tuple[str, list[str]]:
    """独立复核全部见证名单，返回 (复核结论, 错误列表)。

    expect：True=该名单必须达标；False=必须不达标；None=不作达标要求。
    """
    errors: list[str] = []
    for w, expect in items:
        rosters = [a.players_on_court for a in w.assignments]
        errs = verify_assignments(req, rosters)
        if errs:
            errors.extend([f"[{w.label}] {e}" for e in errs])
        # 目标值与求解器报告一致性
        if w.shared_seconds != sum(
            req.segments[a.segment_index - 1].duration_seconds
            for a in w.assignments
            if a.target_all_on_court
        ):
            errors.append(f"[{w.label}] 同场秒数与逐段标记不一致")
        if w.net_points != sum(
            req.segments[a.segment_index - 1].team_net_points
            for a in w.assignments
            if a.target_all_on_court
        ):
            errors.append(f"[{w.label}] 净胜分与逐段标记不一致")
        if expect is True and not w.meets_minimum:
            errors.append(f"[{w.label}] 应属达标名单但同场秒数未达阈值")
        if expect is False and w.meets_minimum:
            errors.append(f"[{w.label}] 应属不达标名单但同场秒数已达阈值")

    if errors:
        return "复核发现不一致（详见 verification_errors）", errors
    return f"已通过独立复核：{len(items)} 套见证名单全部满足全部原始约束", []


def _infeasible_response(
    req: RosterCheckRequest,
    solver: RosterCpSolver,
    pre_conflicts: list[dict],
    started: float,
) -> CheckResponse:
    """原始记录已被证实无解（INFEASIBLE）时的冲突响应。"""
    conflicts = pre_conflicts or solver.diagnose_infeasibility()
    return CheckResponse(
        status="infeasible_record",
        status_label_zh=STATUS_ZH["infeasible_record"],
        target_lineup=req.target_lineup,
        minimum_seconds=req.minimum_seconds,
        feasible=False,
        conflicts=conflicts,
        solver_runs=solver.runs,
        elapsed_ms=(time.perf_counter() - started) * 1000,
    )


@app.post(
    "/api/v1/roster/check",
    response_model=CheckResponse,
    summary="核对目标五人阵容能否达到最低同场上场时长",
)
def check_roster(
    req: RosterCheckRequest = Body(
        ...,
        openapi_examples={
            "feasible_demo": {
                "summary": "可行记录：目标阵容可能达标（2 段、7 人，经暴力枚举验证共 3 套名单）",
                "description": (
                    "2 个换人片段各 300 秒，官方逐人秒数自洽，第 2 段换下 2 人。"
                    "目标阵容 [2,3,5,6,7] 要求至少同场 100 秒；"
                    "可达同场秒数区间为 [0,300]，因此判定为可能达标。"
                ),
                "value": {
                    "players": [1, 2, 3, 4, 5, 6, 7],
                    "target_lineup": [2, 3, 5, 6, 7],
                    "minimum_seconds": 100,
                    "official_seconds": {
                        "1": 300, "2": 600, "3": 600,
                        "4": 300, "5": 300, "6": 300, "7": 600,
                    },
                    "segments": [
                        {
                            "duration_seconds": 300,
                            "confirmed_players": [2, 7],
                            "players_substituted": 0,
                            "team_net_points": 4,
                        },
                        {
                            "duration_seconds": 300,
                            "confirmed_players": [3, 5, 7],
                            "players_substituted": 2,
                            "team_net_points": -3,
                        },
                    ],
                },
            },
            "conflict_demo": {
                "summary": "冲突记录：换人记录与确认球员矛盾",
                "description": (
                    "其余数据与可行示例相同，仅把第 2 段换下人数从 2 改成 1"
                    "（只允许 2 人次变动），但确认球员从 [2,7] 变为 [3,5,7]，"
                    "强制 3 人次变动（2 人换上 + 1 人换下），原始记录无法同时成立。"
                ),
                "value": {
                    "players": [1, 2, 3, 4, 5, 6, 7],
                    "target_lineup": [2, 3, 5, 6, 7],
                    "minimum_seconds": 100,
                    "official_seconds": {
                        "1": 300, "2": 600, "3": 600,
                        "4": 300, "5": 300, "6": 300, "7": 600,
                    },
                    "segments": [
                        {
                            "duration_seconds": 300,
                            "confirmed_players": [2, 7],
                            "players_substituted": 0,
                            "team_net_points": 4,
                        },
                        {
                            "duration_seconds": 300,
                            "confirmed_players": [3, 5, 7],
                            "players_substituted": 1,
                            "team_net_points": -3,
                        },
                    ],
                },
            },
        },
    )
) -> CheckResponse:
    started = time.perf_counter()
    solver = RosterCpSolver(
        req, deadline=time.monotonic() + TOTAL_TIME_BUDGET_SECONDS
    )
    evidence: list[str] = []

    # ---------- 第 1 步：静态冲突预检 ----------
    pre_diag = solver.precheck()

    # ---------- 第 2 步：基础可行性（完整约束） ----------
    # 只有求解器证实 INFEASIBLE 才报原始记录冲突；UNKNOWN/SKIPPED 一律进入
    # 证据收集流程，结论不足时标为未判定。
    base = solver.feasible()
    if base.status_name == "INFEASIBLE":
        return _infeasible_response(req, solver, pre_diag.conflicts, started)

    # ---------- 第 3 步：收集达标 / 不达标名单证据 ----------
    qualifying_outcome: SolveOutcome | None = None  # 已找到的达标名单
    non_qualifying_outcome: SolveOutcome | None = None  # 已找到的不达标名单
    no_qualifying_proven = False  # 已证实不存在达标名单
    # 阈值为 0 时任何名单都达标，不达标名单不可能存在
    all_qualify_proven = req.minimum_seconds == 0

    def classify(outcome: SolveOutcome) -> None:
        """把求解 run 产出的名单归入达标 / 不达标证据。"""
        nonlocal qualifying_outcome, non_qualifying_outcome
        if outcome.status_name not in FEASIBLE_STATUSES or outcome.assignments is None:
            return
        shared = compute_shared_seconds(req, outcome.assignments)
        if shared >= req.minimum_seconds:
            if qualifying_outcome is None:
                qualifying_outcome = outcome
        elif non_qualifying_outcome is None:
            non_qualifying_outcome = outcome

    classify(base)

    # 达标名单存在性探测：找到一套即证“存在”，INFEASIBLE 即证“不存在”
    if qualifying_outcome is None:
        probe = solver.feasible(shared_seconds_lower_bound=req.minimum_seconds)
        if probe.status_name == "INFEASIBLE":
            no_qualifying_proven = True
        else:
            classify(probe)

    # 不达标名单存在性探测：同场秒数 <= 阈值 - 1
    if non_qualifying_outcome is None and not all_qualify_proven:
        probe = solver.feasible(shared_seconds_upper_bound=req.minimum_seconds - 1)
        if probe.status_name == "INFEASIBLE":
            all_qualify_proven = True
        else:
            classify(probe)

    # ---------- 第 4 步：同场秒数上下界（仅 OPTIMAL 才出具） ----------
    max_out = solver.optimize_shared(maximize=True)
    min_out = solver.optimize_shared(maximize=False)
    classify(max_out)
    classify(min_out)

    # 完整约束模型的 INFEASIBLE 同样证实原始记录无解
    # （基础可行性探测超时、但优化 run 证实无解的情形）
    if "INFEASIBLE" in (max_out.status_name, min_out.status_name):
        return _infeasible_response(req, solver, pre_diag.conflicts, started)

    s_max = s_min = None
    if max_out.status_name == "OPTIMAL":
        s_max = max_out.objective
        if s_max < req.minimum_seconds:
            no_qualifying_proven = True
    if min_out.status_name == "OPTIMAL":
        s_min = min_out.objective
        if s_min >= req.minimum_seconds:
            all_qualify_proven = True

    # ---------- 第 5 步：净胜分边界（两端都证实最优才出具范围） ----------
    net_max_all = solver.optimize_net(maximize=True)
    net_min_all = solver.optimize_net(maximize=False)
    classify(net_max_all)
    classify(net_min_all)
    if "INFEASIBLE" in (net_max_all.status_name, net_min_all.status_name):
        return _infeasible_response(req, solver, pre_diag.conflicts, started)

    # 有达标名单即可核算达标组净胜分
    qualifying_exists = qualifying_outcome is not None
    if qualifying_exists:
        net_max_q = solver.optimize_net(
            maximize=True, shared_seconds_lower_bound=req.minimum_seconds
        )
        net_min_q = solver.optimize_net(
            maximize=False, shared_seconds_lower_bound=req.minimum_seconds
        )
    else:
        net_max_q = net_min_q = None

    # ---------- 第 6 步：判定（只使用已证实的证据） ----------
    feasible_proven = qualifying_outcome is not None or non_qualifying_outcome is not None

    # 达标名单与不达标名单同时被证实不存在 => 任何名单都不存在 => 原始记录无解
    # （基础可行性探测超时、但两个存在性探测均证实 INFEASIBLE 的情形）
    if no_qualifying_proven and all_qualify_proven and not feasible_proven:
        return _infeasible_response(req, solver, pre_diag.conflicts, started)

    if qualifying_outcome is not None and non_qualifying_outcome is not None:
        status = "possible"
    elif all_qualify_proven and feasible_proven:
        status = "guaranteed"
    elif no_qualifying_proven and feasible_proven:
        status = "impossible"
    else:
        status = "undetermined"

    # ---------- 第 7 步：组装见证名单、净胜分范围并独立复核 ----------
    verify_items: list[tuple[WitnessRoster, bool | None]] = []

    bounds_all = None
    if net_max_all.status_name == "OPTIMAL" and net_min_all.status_name == "OPTIMAL":
        w_all_max = _build_witness(req, "全部可行名单-净胜分上界", net_max_all)
        w_all_min = _build_witness(req, "全部可行名单-净胜分下界", net_min_all)
        verify_items += [(w_all_max, None), (w_all_min, None)]
        bounds_all = NetBounds(
            min_net_points=net_min_all.objective,
            max_net_points=net_max_all.objective,
            min_witness=w_all_min,
            max_witness=w_all_max,
        )

    bounds_qualifying = None
    if (
        qualifying_exists
        and net_max_q.status_name == "OPTIMAL"
        and net_min_q.status_name == "OPTIMAL"
    ):
        w_q_max = _build_witness(req, "达标名单-净胜分上界", net_max_q)
        w_q_min = _build_witness(req, "达标名单-净胜分下界", net_min_q)
        verify_items += [(w_q_max, True), (w_q_min, True)]
        bounds_qualifying = NetBounds(
            min_net_points=net_min_q.objective,
            max_net_points=net_max_q.objective,
            min_witness=w_q_min,
            max_witness=w_q_max,
        )

    qualifying_witness = None
    if qualifying_outcome is not None:
        qualifying_witness = _build_witness(req, "达标名单-存在性见证", qualifying_outcome)
        verify_items.append((qualifying_witness, True))
    non_qualifying_witness = None
    if non_qualifying_outcome is not None:
        non_qualifying_witness = _build_witness(
            req, "不达标名单-存在性见证", non_qualifying_outcome
        )
        verify_items.append((non_qualifying_witness, False))

    verification_msg = None
    verification_errors: list[str] = []
    if verify_items:
        verification_msg, verification_errors = _check_witness(req, verify_items)

    # ---------- 第 8 步：证据小结与完整性标记 ----------
    bounds_proven_optimal = (
        max_out.status_name == "OPTIMAL"
        and min_out.status_name == "OPTIMAL"
        and net_max_all.status_name == "OPTIMAL"
        and net_min_all.status_name == "OPTIMAL"
        and (
            not qualifying_exists
            or (
                net_max_q.status_name == "OPTIMAL"
                and net_min_q.status_name == "OPTIMAL"
            )
        )
    )

    if feasible_proven:
        evidence.append("已证实原始记录存在可行名单")
    else:
        evidence.append("原始记录可行性未在时限内判定（求解超时或预算耗尽）")
    if pre_diag.has_conflict and status != "infeasible_record":
        evidence.append(
            f"静态预检发现 {len(pre_diag.conflicts)} 处疑似冲突，"
            "但求解器未在时限内证实原始记录无解"
        )
    if req.minimum_seconds == 0:
        evidence.append("阈值为 0 秒，任何可行名单都达标")
    if qualifying_outcome is not None:
        q_shared = compute_shared_seconds(req, qualifying_outcome.assignments)
        evidence.append(
            f"已找到达标名单（同场 {q_shared} 秒 ≥ 阈值 {req.minimum_seconds} 秒）"
        )
    elif no_qualifying_proven:
        evidence.append("已证实不存在达标名单")
    else:
        evidence.append("未找到达标名单，也未证实其不存在")
    if non_qualifying_outcome is not None:
        nq_shared = compute_shared_seconds(req, non_qualifying_outcome.assignments)
        evidence.append(
            f"已找到不达标名单（同场 {nq_shared} 秒 < 阈值 {req.minimum_seconds} 秒）"
        )
    elif all_qualify_proven:
        evidence.append("已证实不存在不达标名单")
    else:
        evidence.append("未找到不达标名单，也未证实其不存在")
    evidence.append(
        f"已证实同场秒数上界为 {s_max} 秒"
        if s_max is not None
        else "同场秒数上界未在时限内证实"
    )
    evidence.append(
        f"已证实同场秒数下界为 {s_min} 秒"
        if s_min is not None
        else "同场秒数下界未在时限内证实"
    )
    evidence.append(
        "已证实全部可行名单的净胜分范围"
        if bounds_all is not None
        else "全部可行名单的净胜分范围未在时限内完整证实"
    )
    if qualifying_exists:
        evidence.append(
            "已证实达标名单的净胜分范围"
            if bounds_qualifying is not None
            else "达标名单的净胜分范围未在时限内完整证实"
        )
    if solver.skipped_runs:
        evidence.append(f"求解总预算已耗尽，{solver.skipped_runs} 个求解步骤被跳过")

    conflicts: list[dict] = []
    if pre_diag.has_conflict and base.status_name in FEASIBLE_STATUSES:
        # 理论上不应发生：预检报了冲突但完整模型可行。如实暴露。
        conflicts = pre_diag.conflicts + [
            {
                "code": "PRECHECK_SUPERSEDED",
                "message": "预检发现潜在冲突，但完整约束模型存在可行解，以求解器结果为准",
            }
        ]

    return CheckResponse(
        status=status,
        status_label_zh=STATUS_ZH[status],
        target_lineup=req.target_lineup,
        minimum_seconds=req.minimum_seconds,
        feasible=True if feasible_proven else None,
        shared_seconds_min=s_min,
        shared_seconds_max=s_max,
        bounds_proven_optimal=bounds_proven_optimal,
        net_bounds_all_feasible=bounds_all,
        net_bounds_among_qualifying=bounds_qualifying,
        qualifying_witness=qualifying_witness,
        non_qualifying_witness=non_qualifying_witness,
        evidence=evidence,
        verification=verification_msg,
        verification_errors=verification_errors,
        conflicts=conflicts,
        solver_runs=solver.runs,
        elapsed_ms=(time.perf_counter() - started) * 1000,
    )


@app.get("/health", response_model=HealthResponse, tags=["system"])
def health() -> HealthResponse:
    return HealthResponse(service="roster-check-api")
