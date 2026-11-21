"""搜索并冻结测试夹具：用暴力枚举找齐 guaranteed/possible/impossible/infeasible 实例。"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tests.brute_force import ground_truth  # noqa: E402
from tests.generator import (  # noqa: E402
    add_conflicting_confirmation,
    generate_feasible_instance,
    inflate_substitution,
    perturb_seconds,
)


def evaluate(inst: dict) -> dict:
    gt = ground_truth(
        players=inst["players"],
        target=inst["target_lineup"],
        minimum=inst["minimum_seconds"],
        durations=[s["duration_seconds"] for s in inst["segments"]],
        nets=[s["team_net_points"] for s in inst["segments"]],
        confirmed=[s["confirmed_players"] for s in inst["segments"]],
        substitutions=[s["players_substituted"] for s in inst["segments"]],
        official={int(k): v for k, v in inst["official_seconds"].items()},
    )
    return gt


def classify(gt: dict, minimum: int) -> str:
    if not gt["feasible"]:
        return "infeasible_record"
    if gt["shared_max"] < minimum:
        return "impossible"
    if gt["shared_min"] >= minimum:
        return "guaranteed"
    return "possible"


def main() -> None:
    rng = random.Random(20261121)
    wanted = {"guaranteed": [], "possible": [], "impossible": [], "infeasible_record": []}
    attempts = 0
    while attempts < 4000 and any(len(v) < 1 for v in wanted.values()):
        attempts += 1
        inst = generate_feasible_instance(rng)
        gt = evaluate(inst)
        status = classify(gt, inst["minimum_seconds"])
        # guaranteed 夹具要求有正的同场秒数，避免 minimum=0 的平凡情形
        if (
            status == "guaranteed"
            and gt["shared_min"] == 0
            and len(wanted["guaranteed"]) == 0
        ):
            continue
        if status in wanted and len(wanted[status]) < 1:
            wanted[status].append((inst, gt))

        # 派生不可行变体
        if len(wanted["infeasible_record"]) < 1:
            for variant in (
                perturb_seconds(inst, rng),
                inflate_substitution(inst, rng),
                add_conflicting_confirmation(inst, rng),
            ):
                vgt = evaluate(variant)
                if not vgt["feasible"]:
                    wanted["infeasible_record"].append((variant, vgt))
                    break

    fixtures: dict = {}
    for status, items in wanted.items():
        if not items:
            print(f"WARNING: 未找到 {status} 夹具", file=sys.stderr)
            continue
        inst, gt = items[0]
        public = {k: v for k, v in inst.items() if k != "_lineups"}
        fixtures[status] = {"request": public, "ground_truth": gt}
        print(
            f"{status:18s} P={len(inst['players'])} T={len(inst['segments'])} "
            f"rosters={gt.get('count')} shared=[{gt.get('shared_min')},{gt.get('shared_max')}] "
            f"net=[{gt.get('net_min')},{gt.get('net_max')}] "
            f"q_net=[{gt.get('qualifying_net_min')},{gt.get('qualifying_net_max')}]"
        )

    out = Path(__file__).resolve().parent / "fixtures.json"
    out.write_text(json.dumps(fixtures, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n夹具已写入 {out}（尝试 {attempts} 次）")


if __name__ == "__main__":
    main()
