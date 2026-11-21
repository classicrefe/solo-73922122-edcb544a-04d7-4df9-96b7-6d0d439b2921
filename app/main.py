"""FastAPI 入口：篮球五人阵容达标核对 API。"""
from __future__ import annotations

import time

from fastapi import Body, FastAPI
from fastapi.responses import JSONResponse

from .schemas import (
    CheckResponse,
    HealthResponse,
    RosterCheckRequest,
    SegmentAssignment,
    WitnessRoster,
    NetBounds,
)
from .solver import RosterCpSolver, SolveOutcome
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
        "- `guaranteed`（必定达标）：所有可行名单的同场秒数都达到阈值；\n"
        "- `possible`（可能达标）：存在达标名单，也存在不达标名单；\n"
        "- `impossible`（无法达标）：记录本身可行，但没有任何名单达标；\n"
        "- `infeasible_record`（原始记录冲突）：约束无法同时成立，只给冲突，不给达标结论。"
    ),
    version="1.0.0",
)

STATUS_ZH = {
    "guaranteed": "必定达标",
    "possible": "可能达标",
    "impossible": "无法达标",
    "infeasible_record": "原始记录冲突",
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
    witnesses: list[WitnessRoster],
    expect_qualifying: bool,
) -> tuple[str, list[str]]:
    """独立复核全部见证名单，返回 (复核结论, 错误列表)。"""
    errors: list[str] = []
    for w in witnesses:
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
        if expect_qualifying and not w.meets_minimum:
            errors.append(f"[{w.label}] 应属达标名单但同场秒数未达阈值")

    if errors:
        return "复核发现不一致（详见 verification_errors）", errors
    return f"已通过独立复核：{len(witnesses)} 套见证名单全部满足全部原始约束", []


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
    solver = RosterCpSolver(req)

    # ---------- 第 1 步：静态冲突预检 ----------
    pre_diag = solver.precheck()

    # ---------- 第 2 步：基础可行性（完整约束） ----------
    base = solver.feasible()
    if base.status_name not in FEASIBLE_STATUSES:
        # 原始记录无法同时成立：先报静态冲突，再做深度诊断
        conflicts = pre_diag.conflicts
        if not conflicts:
            conflicts = solver.diagnose_infeasibility()
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

    if pre_diag.has_conflict:
        # 理论上不应发生：预检报了冲突但完整模型可行。如实暴露。
        conflicts = pre_diag.conflicts + [
            {
                "code": "PRECHECK_SUPERSEDED",
                "message": "预检发现潜在冲突，但完整约束模型存在可行解，以求解器结果为准",
            }
        ]
    else:
        conflicts = []

    # ---------- 第 3 步：同场秒数上下界 ----------
    max_out = solver.optimize_shared(maximize=True)
    min_out = solver.optimize_shared(maximize=False)
    if max_out.status_name not in FEASIBLE_STATUSES or min_out.status_name not in FEASIBLE_STATUSES:
        return JSONResponse(
            status_code=500,
            content={"detail": "求解器在基础可行的情况下未能给出同场秒数边界，请重试"},
        )
    s_max = max_out.objective
    s_min = min_out.objective
    proven_optimal = (
        max_out.status_name == "OPTIMAL" and min_out.status_name == "OPTIMAL"
    )

    if s_max < req.minimum_seconds:
        status = "impossible"
    elif s_min >= req.minimum_seconds:
        status = "guaranteed"
    else:
        status = "possible"

    # ---------- 第 4 步：净胜分边界 ----------
    # 4a. 全部可行名单上的净胜分上下界
    net_max_all = solver.optimize_net(maximize=True)
    net_min_all = solver.optimize_net(maximize=False)
    if net_max_all.status_name not in FEASIBLE_STATUSES or net_min_all.status_name not in FEASIBLE_STATUSES:
        return JSONResponse(
            status_code=500,
            content={"detail": "求解器未能给出净胜分边界，请重试"},
        )

    # 4b. 达标名单上的净胜分上下界（只在存在达标名单时计算）
    qualifying_exists = s_max >= req.minimum_seconds
    if qualifying_exists:
        net_max_q = solver.optimize_net(
            maximize=True, shared_seconds_lower_bound=req.minimum_seconds
        )
        net_min_q = solver.optimize_net(
            maximize=False, shared_seconds_lower_bound=req.minimum_seconds
        )
        if net_max_q.status_name not in FEASIBLE_STATUSES or net_min_q.status_name not in FEASIBLE_STATUSES:
            return JSONResponse(
                status_code=500,
                content={"detail": "求解器未能给出达标名单净胜分边界，请重试"},
            )
        proven_optimal = proven_optimal and all(
            o.status_name == "OPTIMAL"
            for o in (net_max_all, net_min_all, net_max_q, net_min_q)
        )
    else:
        net_max_q = net_min_q = None
        proven_optimal = proven_optimal and all(
            o.status_name == "OPTIMAL" for o in (net_max_all, net_min_all)
        )

    # ---------- 第 5 步：组装见证名单并独立复核 ----------
    witnesses_all = [
        _build_witness(req, "全部可行名单-净胜分上界", net_max_all),
        _build_witness(req, "全部可行名单-净胜分下界", net_min_all),
    ]
    verification_msg, verification_errors = _check_witness(
        req, witnesses_all, expect_qualifying=False
    )
    bounds_all = NetBounds(
        min_net_points=net_min_all.objective,
        max_net_points=net_max_all.objective,
        min_witness=witnesses_all[1],
        max_witness=witnesses_all[0],
    )

    bounds_qualifying = None
    if qualifying_exists:
        witnesses_q = [
            _build_witness(req, "达标名单-净胜分上界", net_max_q),
            _build_witness(req, "达标名单-净胜分下界", net_min_q),
        ]
        qmsg, qerrs = _check_witness(req, witnesses_q, expect_qualifying=True)
        if qerrs:
            verification_msg = f"{verification_msg}；{qmsg}"
            verification_errors.extend(qerrs)
        bounds_qualifying = NetBounds(
            min_net_points=net_min_q.objective,
            max_net_points=net_max_q.objective,
            min_witness=witnesses_q[1],
            max_witness=witnesses_q[0],
        )

    return CheckResponse(
        status=status,
        status_label_zh=STATUS_ZH[status],
        target_lineup=req.target_lineup,
        minimum_seconds=req.minimum_seconds,
        feasible=True,
        shared_seconds_min=s_min,
        shared_seconds_max=s_max,
        bounds_proven_optimal=proven_optimal,
        net_bounds_all_feasible=bounds_all,
        net_bounds_among_qualifying=bounds_qualifying,
        verification=verification_msg,
        verification_errors=verification_errors,
        conflicts=conflicts,
        solver_runs=solver.runs,
        elapsed_ms=(time.perf_counter() - started) * 1000,
    )


@app.get("/health", response_model=HealthResponse, tags=["system"])
def health() -> HealthResponse:
    return HealthResponse(service="roster-check-api")
