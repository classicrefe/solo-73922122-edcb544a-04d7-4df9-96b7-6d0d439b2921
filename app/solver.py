"""CP-SAT 约束求解核心。

变量 x[t, p]：片段 t 球员 p 是否在场（布尔）。
约束：
  1. 每段恰有 5 人；
  2. 每段包含全部已确认球员；
  3. 逐人累计在场秒数等于官方秒数；
  4. 相邻片段实际变动人数 == 2 * 记录换下人数（换下=换上，双边计数）。
派生：
  z[t]：目标五人是否在片段 t 同场；
  S   ：目标五人同场总秒数；
  N   ：目标五人同场片段的得失分之和（净胜分）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ortools.sat.python import cp_model

from .schemas import LINEUP_SIZE, RosterCheckRequest

SOLVER_TIME_LIMIT_SECONDS = 30.0


@dataclass
class SolveOutcome:
    status_name: str  # OPTIMAL / FEASIBLE / INFEASIBLE / UNKNOWN
    assignments: list[list[int]] | None = None
    objective: int | None = None


@dataclass
class Diagnosis:
    """求解前 / 求解失败后的冲突诊断。"""

    conflicts: list[dict] = field(default_factory=list)

    def add(self, code: str, message: str, **detail: object) -> None:
        entry: dict = {"code": code, "message": message}
        entry.update(detail)
        self.conflicts.append(entry)

    @property
    def has_conflict(self) -> bool:
        return bool(self.conflicts)


class RosterCpSolver:
    def __init__(self, req: RosterCheckRequest) -> None:
        self.req = req
        self.players = list(req.players)
        self.p_index = {p: i for i, p in enumerate(self.players)}
        self.segments: list = list(req.segments)
        self.T = len(self.segments)
        self.durations = [s.duration_seconds for s in self.segments]
        self.nets = [s.team_net_points for s in self.segments]
        self.confirmed_idx: list[list[int]] = [
            [self.p_index[p] for p in s.confirmed_players] for s in self.segments
        ]
        self.target_idx = [self.p_index[p] for p in req.target_lineup]
        self.official = {p: req.official_seconds[p] for p in self.players}
        self.total_seconds = sum(self.durations)
        self.runs = 0

    # ------------------------------------------------------------------ #
    # 求解前的静态冲突诊断（不依赖求解器，给出精确定位）
    # ------------------------------------------------------------------ #
    def precheck(self) -> Diagnosis:
        diag = Diagnosis()

        # 1) 总人秒守恒：所有球员官方秒数之和 == 5 * 片段总秒数
        official_sum = sum(self.official.values())
        court_sum = LINEUP_SIZE * self.total_seconds
        if official_sum != court_sum:
            diag.add(
                "TOTAL_SECONDS_MISMATCH",
                "官方逐人秒数之和与 5 人在场总人秒不一致",
                official_seconds_sum=official_sum,
                required_court_seconds_sum=court_sum,
                total_segment_seconds=self.total_seconds,
            )

        # 2) 单人官方秒数不能超过整节总秒数
        for p in self.players:
            if self.official[p] > self.total_seconds:
                diag.add(
                    "PLAYER_SECONDS_EXCEED_TOTAL",
                    f"球员 {p} 的官方秒数 {self.official[p]} 超过整节总秒数 {self.total_seconds}",
                    player=p,
                    official_seconds=self.official[p],
                    total_seconds=self.total_seconds,
                )

        # 3) 已确认球员必须打满该片段：官方秒数至少为该片段时长
        for t, seg in enumerate(self.segments):
            for p in seg.confirmed_players:
                if self.official[p] < seg.duration_seconds:
                    diag.add(
                        "CONFIRMED_PLAYER_NOT_ENOUGH_SECONDS",
                        f"球员 {p} 在第 {t + 1} 段被确认在场，"
                        f"但其官方秒数 {self.official[p]} 小于该段时长 {seg.duration_seconds}",
                        segment_index=t + 1,
                        player=p,
                        official_seconds=self.official[p],
                        segment_duration=seg.duration_seconds,
                    )

        # 4) 相邻片段：确认球员的强制变动不能超过换人记录允许的变动
        for t in range(1, self.T):
            prev_c = set(self.segments[t - 1].confirmed_players)
            cur_c = set(self.segments[t].confirmed_players)
            forced_out = sorted(prev_c - cur_c)
            forced_in = sorted(cur_c - prev_c)
            forced_moves = len(forced_out) + len(forced_in)
            allowed_moves = 2 * self.segments[t].players_substituted
            if forced_moves > allowed_moves:
                diag.add(
                    "SUBSTITUTION_CONFIRMED_CONFLICT",
                    f"第 {t + 1} 段换人记录允许 {allowed_moves} 人次变动，"
                    f"但仅确认球员就强制 {forced_moves} 人次变动",
                    segment_index=t + 1,
                    recorded_substitutions=self.segments[t].players_substituted,
                    allowed_moves=allowed_moves,
                    forced_moves=forced_moves,
                    confirmed_forced_out=forced_out,
                    confirmed_forced_in=forced_in,
                )

        # 5) 换上需要足够的板凳球员：单段换下人数 <= 名单人数 - 5
        bench = len(self.players) - LINEUP_SIZE
        for t, seg in enumerate(self.segments):
            if seg.players_substituted > bench:
                diag.add(
                    "NOT_ENOUGH_BENCH_PLAYERS",
                    f"第 {t + 1} 段记录换下 {seg.players_substituted} 人，"
                    f"但名单 {len(self.players)} 人仅有 {bench} 名板凳球员可换上",
                    segment_index=t + 1,
                    recorded_substitutions=seg.players_substituted,
                    bench_size=bench,
                )
        return diag

    # ------------------------------------------------------------------ #
    # 建模
    # ------------------------------------------------------------------ #
    def _build_base_model(
        self,
        *,
        use_official_totals: bool = True,
        shared_seconds_lower_bound: int | None = None,
    ) -> tuple[cp_model.CpModel, dict, dict, dict]:
        """构建基础模型，返回 model 与变量字典。"""
        model = cp_model.CpModel()
        P = len(self.players)

        x: dict[tuple[int, int], cp_model.IntVar] = {}
        for t in range(self.T):
            for pi in range(P):
                x[t, pi] = model.new_bool_var(f"x_{t}_{pi}")

        # 每段恰有 5 人
        for t in range(self.T):
            model.add(sum(x[t, pi] for pi in range(P)) == LINEUP_SIZE)

        # 已确认球员在场
        for t in range(self.T):
            for pi in self.confirmed_idx[t]:
                model.add(x[t, pi] == 1)

        # 官方累计秒数
        if use_official_totals:
            for pi, p in enumerate(self.players):
                model.add(
                    sum(self.durations[t] * x[t, pi] for t in range(self.T))
                    == self.official[p]
                )

        # 换人：相邻片段变动人次 == 2 * 换下人数
        # b[t,p] = x[t,p] XOR x[t-1,p]
        # 四条不等式完整刻画异或真值表
        b: dict[tuple[int, int], cp_model.IntVar] = {}
        for t in range(1, self.T):
            for pi in range(P):
                bt = model.new_bool_var(f"chg_{t}_{pi}")
                model.add(bt <= x[t, pi] + x[t - 1, pi])
                model.add(bt >= x[t, pi] - x[t - 1, pi])
                model.add(bt >= x[t - 1, pi] - x[t, pi])
                model.add(bt <= 2 - x[t, pi] - x[t - 1, pi])
                b[t, pi] = bt
            model.add(
                sum(b[t, pi] for pi in range(P))
                == 2 * self.segments[t].players_substituted
            )

        # 目标五人同场指示变量 z[t]
        z: dict[int, cp_model.IntVar] = {}
        for t in range(self.T):
            zt = model.new_bool_var(f"z_{t}")
            on_target = [x[t, pi] for pi in self.target_idx]
            for var in on_target:
                model.add(zt <= var)
            model.add(zt >= sum(on_target) - (LINEUP_SIZE - 1))
            z[t] = zt

        if shared_seconds_lower_bound is not None:
            model.add(
                sum(self.durations[t] * z[t] for t in range(self.T))
                >= shared_seconds_lower_bound
            )

        return model, x, b, z

    def _solve(
        self,
        model: cp_model.CpModel,
        objective_var: cp_model.IntVar | None,
        maximize: bool = True,
        x: dict[tuple[int, int], cp_model.IntVar] | None = None,
    ) -> SolveOutcome:
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = SOLVER_TIME_LIMIT_SECONDS
        solver.parameters.num_workers = 8
        if objective_var is not None:
            if maximize:
                model.maximize(objective_var)
            else:
                model.minimize(objective_var)
        self.runs += 1
        status = solver.solve(model)
        name = solver.status_name(status)
        if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            assert x is not None
            return SolveOutcome(
                status_name=name,
                assignments=self._extract(solver, x),
                objective=int(solver.objective_value) if objective_var is not None else None,
            )
        return SolveOutcome(status_name=name)

    def _extract(
        self, solver: cp_model.CpSolver, x: dict[tuple[int, int], cp_model.IntVar]
    ) -> list[list[int]]:
        P = len(self.players)
        assignments: list[list[int]] = []
        for t in range(self.T):
            on_court = [
                self.players[pi]
                for pi in range(P)
                if solver.value(x[t, pi]) == 1
            ]
            assignments.append(sorted(on_court))
        return assignments

    # ------------------------------------------------------------------ #
    # 对外的分阶段求解
    # ------------------------------------------------------------------ #
    def feasible(
        self, *, shared_seconds_lower_bound: int | None = None
    ) -> SolveOutcome:
        model, x, _, _ = self._build_base_model(
            shared_seconds_lower_bound=shared_seconds_lower_bound
        )
        return self._solve(model, None, x=x)

    def optimize_shared(self, maximize: bool) -> SolveOutcome:
        model, x, _, z = self._build_base_model()
        s_var = model.new_int_var(0, self.total_seconds, "shared_seconds")
        model.add(s_var == sum(self.durations[t] * z[t] for t in range(self.T)))
        return self._solve(model, s_var, maximize=maximize, x=x)

    def optimize_net(
        self, maximize: bool, *, shared_seconds_lower_bound: int | None = None
    ) -> SolveOutcome:
        model, x, _, z = self._build_base_model(
            shared_seconds_lower_bound=shared_seconds_lower_bound
        )
        bound = sum(abs(v) for v in self.nets)
        n_var = model.new_int_var(-bound, bound, "net_points")
        model.add(n_var == sum(self.nets[t] * z[t] for t in range(self.T)))
        return self._solve(model, n_var, maximize=maximize, x=x)

    # ------------------------------------------------------------------ #
    # 基础模型不可行时的深入诊断
    # ------------------------------------------------------------------ #
    def diagnose_infeasibility(self) -> list[dict]:
        """基础约束不可行时，定位冲突来源。"""
        diag = Diagnosis()

        # A. 去掉官方秒数约束，检查结构（5 人 / 确认 / 换人）本身是否可行
        structural_model, x_struct, _, _ = self._build_base_model(use_official_totals=False)
        outcome = self._solve(structural_model, None, x=x_struct)
        if outcome.status_name not in ("OPTIMAL", "FEASIBLE"):
            diag.add(
                "STRUCTURAL_INFEASIBLE",
                "即便忽略官方累计秒数，每段五人、确认球员与换人记录仍无法同时满足",
            )
            return diag.conflicts

        # B. 结构可行：逐人求在结构约束下的可达秒数区间，定位超界球员
        out_of_range: list[dict] = []
        for pi, p in enumerate(self.players):
            lo_model, x_lo, _, _ = self._build_base_model(use_official_totals=False)
            t_var = lo_model.new_int_var(0, self.total_seconds, f"sec_{p}")
            lo_model.add(
                t_var == sum(self.durations[t] * x_lo[t, pi] for t in range(self.T))
            )
            lo = self._solve(lo_model, t_var, maximize=False, x=x_lo)

            hi_model, x_hi, _, _ = self._build_base_model(use_official_totals=False)
            t_var3 = hi_model.new_int_var(0, self.total_seconds, f"sec_{p}")
            hi_model.add(
                t_var3 == sum(self.durations[t] * x_hi[t, pi] for t in range(self.T))
            )
            hi = self._solve(hi_model, t_var3, maximize=True, x=x_hi)
            if lo.objective is None or hi.objective is None:
                continue
            if not (lo.objective <= self.official[p] <= hi.objective):
                out_of_range.append(
                    {
                        "code": "PLAYER_SECONDS_OUT_OF_REACH",
                        "message": (
                            f"球员 {p} 的官方秒数 {self.official[p]} 不在结构允许的 "
                            f"可达区间 [{lo.objective}, {hi.objective}] 内"
                        ),
                        "player": p,
                        "official_seconds": self.official[p],
                        "reachable_min": lo.objective,
                        "reachable_max": hi.objective,
                    }
                )

        if out_of_range:
            diag.conflicts.extend(out_of_range)
            return diag.conflicts

        diag.add(
            "JOINT_SECONDS_INFEASIBLE",
            "每名球员的官方秒数单独看都可达，但受换人结构限制无法同时满足，"
            "官方累计秒数与换人记录之间存在联合冲突",
        )
        return diag.conflicts
