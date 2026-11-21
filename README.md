# 篮球五人阵容达标核对 API

根据**官方逐人在场秒数**、**按换人切分的有序片段**（时长 / 已确认球员 / 相对前段换下人数 / 本队得失分），
用约束规划（Google OR-Tools CP-SAT）判定一套目标五人阵容能否达到最低同场上场时长，
并在达标名单中计算净胜分上下界，每个边界都附一套完整的逐段名单供独立复核。

## 判定语义

| 状态 | 含义 |
| --- | --- |
| `guaranteed` 必定达标 | 已证实不存在不达标名单，且原始记录可行（找到可行名单） |
| `possible` 可能达标 | 已找到达标名单与不达标名单各至少一套（两套见证均随响应出具） |
| `impossible` 无法达标 | 原始记录可行，且已证实不存在达标名单 |
| `infeasible_record` 原始记录冲突 | 已证实约束无法同时成立；只返回冲突说明，不给达标结论 |
| `undetermined` 未判定 | 求解超时或预算耗尽导致证据不足；已证实的事实见 `evidence`，已证实的边界与见证名单照常返回 |

判定只使用已证实的证据：

- 找到一套合规名单即证实该类名单"存在"（达标 / 不达标分开记账）；
- "必定达标 / 无法达标"必须证实另一类名单**不存在**（存在性探测返回 INFEASIBLE，
  或已证实的同场秒数极值越过阈值）；
- 只有求解器证实 INFEASIBLE 才报原始记录冲突——求解超时（UNKNOWN）或预算耗尽
  （SKIPPED）一律导向 `undetermined`，绝不误报冲突。

## 超时与边界口径

- 每个求解 run 限时 30 秒，单次请求全部 run 共享 120 秒总预算，耗尽后剩余 run 记为跳过；
- `shared_seconds_min/max`：仅当对应优化 run 证实最优（OPTIMAL）才出具，否则为 `null`；
- 两个净胜分范围（全部可行名单 / 达标名单）分别出具：**两端都证实最优**才给出，
  各附一套逐段见证名单；某一组未算完时，另一组已证实的范围与已达成的判定照常返回；
- `bounds_proven_optimal=false` 表示响应不完整（存在未证实的边界）；
- `feasible`：`true`=已证实存在可行名单，`false`=已证实无解，`null`=未判定。

## 建模口径

- 变量 `x[t,p]`：片段 `t` 球员 `p` 是否在场（布尔）。
- 硬约束：
  1. 每段恰有 5 人，且包含该段全部已确认球员；
  2. 逐人累计在场秒数 `Σ duration[t]·x[t,p]` 等于官方秒数；
  3. 相邻片段变动人次 `Σ (x[t,p] XOR x[t-1,p])` 等于 `2 × 换下人数`
     （换下与换上人数相等，按双边人次计数；首段换下人数必须为 0）。
- 派生：
  - `z[t]`：目标五人是否在片段 `t` 同场；
  - 同场秒数 `S = Σ duration[t]·z[t]`；
  - 净胜分 `N = Σ team_net_points[t]·z[t]`（只累计目标五人同场的片段）。
- 对 `S`、`N` 分别最小化 / 最大化得到严格边界；达标名单的净胜分边界通过追加
  `S ≥ minimum_seconds` 约束后再次优化得到。
- 规模上限：12 名球员、16 个片段。

## 响应里的复核材料

每个净胜分边界都附带一套**完整逐段见证名单**，包含：

- 每段在场 5 人、已确认球员是否都在、目标五人是否同场；
- 逐人累计秒数（可直接与 `official_seconds` 对账）；
- 逐段实际换下人数（可与 `players_substituted` 对账）；
- 该名单的同场秒数与净胜分。

判定证据也以见证名单形式出具：`qualifying_witness`（达标名单）与
`non_qualifying_witness`（不达标名单）——`possible` 判定即由这两套名单直接证明；
`evidence` 数组逐条列出已证实的事实与未判定的事项。

服务端在返回前会用**不依赖求解器的独立复核器**（纯 Python 重算全部约束）校验每套见证名单，
结果写在 `verification` / `verification_errors` 字段。

## 快速开始（Docker Compose）

```bash
docker compose up --build
```

- 交互式文档（含可直接调用的请求示例）：<http://localhost:8000/docs>
- 健康检查：<http://localhost:8000/health>
- 接口：`POST /api/v1/roster/check`

## 本地开发（无 Docker 时）

```bash
python3 -m venv --without-pip .venv
# 若系统没有 pip，可用 https://bootstrap.pypa.io/get-pip.py 引导
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --reload --port 8000
```

## 请求示例

```json
{
  "players": [1, 2, 3, 4, 5, 6, 7],
  "target_lineup": [2, 3, 5, 6, 7],
  "minimum_seconds": 100,
  "official_seconds": {"1": 300, "2": 600, "3": 600, "4": 300, "5": 300, "6": 300, "7": 600},
  "segments": [
    {"duration_seconds": 300, "confirmed_players": [2, 7], "players_substituted": 0, "team_net_points": 4},
    {"duration_seconds": 300, "confirmed_players": [3, 5, 7], "players_substituted": 2, "team_net_points": -3}
  ]
}
```

## 冲突诊断

记录无法同时成立时，`conflicts` 数组给出带错误码的精确定位，例如：

- `TOTAL_SECONDS_MISMATCH`：官方秒数总和不等于 5 人在场总人秒；
- `CONFIRMED_PLAYER_NOT_ENOUGH_SECONDS`：确认球员的官方秒数不够覆盖该片段；
- `SUBSTITUTION_CONFIRMED_CONFLICT`：确认球员的强制变动超过换人记录允许值；
- `NOT_ENOUGH_BENCH_PLAYERS`：换下人数超过板凳容量；
- `PLAYER_SECONDS_OUT_OF_REACH`：某球员官方秒数不在结构允许的可达区间内（附区间）；
- `JOINT_SECONDS_INFEASIBLE`：单人秒数都可达，但联合换人结构使它们无法同时满足；
- `STRUCTURAL_INFEASIBLE`：忽略秒数后五人 / 确认 / 换人结构本身就不成立；
- `DIAGNOSIS_INCONCLUSIVE`：已证实记录无解，但诊断求解超时，无法在时限内进一步定位
  （诊断只采纳已证实最优的结果，超时不会升级为冲突结论）。

## 测试

```bash
.venv/bin/pip install pytest httpx
.venv/bin/python -m pytest tests/ -q
```

测试策略：

- `tests/brute_force.py`：小规模实例的暴力枚举真值（独立于 CP-SAT 的第二套实现）；
- 250 个随机实例（含可行 / 扰动冲突变体）逐项对照状态、同场秒数与净胜分边界；
- 四种判定状态的冻结夹具、阈值边界、422 结构校验；
- 12 人 × 16 段规模上限的性能与正确性（秒级完成，边界均证明最优）；
- 独立复核器对各类人为篡改的检出测试；
- `tests/test_timeout.py`：超时 / 预算耗尽场景——全部求解超时标未判定且不误报冲突、
  未证最优不出具边界、单组范围未完成时另一组照常出具、诊断超时不误报结构性冲突，
  以及真实求解器在极小时间预算下出具的任何数值都与完整求解一致。
