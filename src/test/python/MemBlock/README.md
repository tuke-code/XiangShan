# MemBlock Python Verification Environment

`src/test/python/MemBlock/` 是面向 MemBlock 真实 DUT 的 Python 验证环境目录。这里不仅包含 testcase、运行环境和 WebUI，也收纳了当前验证状态、覆盖率结果、ROB 建模分析和后续验证规划文档。

当前目录已经从“基础 load/store 冒烟环境”演进到“真实 DUT 下的标量 ld/st 白盒验证平台”。环境的当前特点是：

- 基于真实 DUT 驱动，不依赖 mock DUT 替代真实流水线行为。
- 已完成 `env / agents / monitors / model / sequences` 分层。
- load 在线 compare 采用 commit-boundary 语义。
- store 采用 deferred visibility，测试结束时统一 flush/drain 收口。
- 已具备 cacheable load/store、store->load ordering、RAW/RAR/FF/DM/NC 等基础 replay 场景。
- 已接入 toffee 官方 function coverage 与 DUT line coverage 报告链路。

## 当前验证状态

截至当前版本，环境已具备以下稳定能力：

- 标量 cacheable load/store 主路径真实 DUT 回归。
- `same-addr` / `overwrite` / `unrelated` store->load ordering 验证。
- `flushSb + drain` 收尾与最终一致性比较。
- RAW / RAR / cache miss / forward fail / NC replay 基础场景。
- ROB 相关 function coverage。
- 使用 toffee 报告链路生成 DUT line/branch coverage。

当前阶段的状态总结和覆盖率结果请直接阅读：

- `docs/coverage_summary.md`
- `docs/coverage_todo.md`

这两份文档是当前验证状态与后续补强工作的真源；README 只给入口和概览，不重复维护完整数据表。

## 目录说明

### 根目录

- `MemBlock_api.py`
  - DUT 创建、波形/覆盖率路径配置、`dut` fixture。
- `MemBlock_env.py`
  - 顶层 `MemBlockEnv`、bundle 定义、统一时钟推进内核、`env.backend` 公共控制入口、toffee function coverage 接入点。
- `env_config.py`
  - 环境统一配置入口，收敛 queue 深度、transport 延迟、默认 sequence 时序和 strict check 策略。
- `request_apis.py`
  - 基于 `env.backend` 的公共驱动 helper 和事务薄封装。
- `transactions.py`
  - `QueuePtr`、`LoadTxn`、`StoreTxn` 等事务对象。
- `memory_model.py`
  - compare / store shadow / drain 校验的顶层编排层。
- `README.md`
  - 当前文件，作为目录入口与状态总览。
- `CHANGELOG.md`
  - Python 验证环境自身的重构、coverage 与维护变更记录。
- `ROLES.md`
  - MemBlock 目录下多人协同与 agent 默认角色规范。

### 子目录

- `agents/`
  - backend-facing active agents，包括 CSR、commit、LSQ enqueue、issue，以及统一编排的 backend facade。
- `monitors/`
  - passive monitors，包括 writeback、store、mem_status 三类被动观测器。
- `model/`
  - 已从 `MemoryModel` 中拆出的公共组件，例如 `RefMemory`、`TransportResponder`、`Scoreboard`、ROB function coverage collector。
- `sequences/`
  - 可复用场景模板，例如 reset、scalar load/store、flush store buffer、replay/order 场景。
- `tests/`
  - 环境冒烟、模型单测和真实 DUT 场景用例。
- `docs/`
  - 详细设计文档、覆盖率总结、DUT changelog、实施计划。
- `webui/`
  - LSQ 可视化相关资源。

## `docs/` 文档列表

- `verification_env_design.md`
  - 验证环境总设计，涵盖 `MemBlockEnv`、统一时钟内核、`agents`、`monitors`、`Scoreboard`、`sequences`、`EnvConfig` 的分层关系、实现思路和演进方向。
- `backend_request_model_design.md`
  - backend 主动控制请求模型专项说明，重点覆盖 `env.backend.send(...)`、`env.backend.execute(...)`、`IssueCyclePlan`、`BackendSendPlan` 与兼容层收敛策略。
- `clock_control_and_migration_guide.md`
  - 面向开发者的时钟控制与迁移指南，说明各层应该如何复用 env 时钟原语，避免重新引入零散 `Step()`。
- `memory_model_design.md`
  - `MemoryModel` 当前职责、load commit-boundary compare、store drain 校验和与 scoreboard 的边界。
- `dut_port_behavior.md`
  - DUT 输入输出端口行为说明，覆盖 `enqLsq`、`intIssue`、`mem_to_ooo`、TileLink 和 store shadow 观测口。
- `test_sequence_and_extension_guide.md`
  - 如何基于事务与 sequence 组织 load/store 用例，以及如何扩展环境。
- `rob_model.md`
  - 当前 ROB 建模现状、缺口、设计需求和后续演进分析。
- `rob_coverage_plan.md`
  - ROB function coverage 模型设计与 toffee 汇总方式说明。
- `coverage_summary.md`
  - 当前真实 DUT 回归的 function coverage / line coverage 结果分析。
- `coverage_todo.md`
  - 基于当前覆盖率结果整理出的用例补强清单。
- `vp_pipeline_plan.md`
  - 面向标量 ld/st pipeline 的细粒度白盒验证总体方案。
- `DUT_CHANGELOG-20260331.md`
  - 一次针对 RTL `mem/` 目录版本变化的详细对比与验证影响分析。

## 推荐阅读顺序

如果是首次接触当前环境，建议按以下顺序阅读：

1. `README.md`
2. `CHANGELOG.md`
3. `docs/verification_env_design.md`
4. `docs/backend_request_model_design.md`
5. `docs/clock_control_and_migration_guide.md`
6. `docs/memory_model_design.md`
7. `docs/rob_model.md`
8. `docs/coverage_summary.md`
9. `docs/coverage_todo.md`
10. `docs/vp_pipeline_plan.md`
11. `tests/test_MemBlock_scalar_load_pipeline.py`
12. `tests/test_MemBlock_scalar_store_pipeline.py`
13. `tests/test_MemBlock_scalar_ordering.py`
14. `tests/test_MemBlock_replay.py`

## 当前测试与报告入口

推荐的真实 DUT 回归入口：

- `src/test/python/MemBlock/tests/`

最小主动控制示例：

```python
from transactions import LoadTxn, QueuePtr

state = ResetEnvSequence(require_issue_lanes=(0,), require_lq_ready=True).run(env)
env.preload_u64(0x9000_0000, 0x1122334455667788)

txn = LoadTxn(
    req_id=0x21,
    addr=0x9000_0000,
    lq_ptr=state.next_lq_ptr,
    sq_ptr=state.sq_ptr,
)

env.backend.send(txn)
env.expect_scalar_load(req_id=txn.req_id, addr=txn.addr)
env.drain_writebacks()
```

如果场景已经能被高层 sequence 表达，仍然优先复用 sequence；只有在编写新的 primitive 场景或 debug 时，才直接调用 `env.backend`。

如果需要在同一拍发多条 `load`，或组合 `load + sta/std`，推荐直接构造脚本化计划：

```python
from transactions import BackendSendPlan, EnqueueLoadStep, IssueCyclePlan, IssueOp

env.backend.execute(
    BackendSendPlan.from_steps(
        EnqueueLoadStep.from_txn(load0),
        EnqueueLoadStep.from_txn(load1),
        IssueCyclePlan.from_ops(
            IssueOp.load_from_txn(load0),
            IssueOp.load_from_txn(load1),
        ),
    )
)
```

这套请求脚本模型的设计动机、对象关系与扩展规则，详见 `src/test/python/MemBlock/docs/backend_request_model_design.md`。

当前已稳定存在的测试类型：

- env / fixture 冒烟
- memory model / scoreboard 单测
- 标量 load/store 主路径
- ordering / mixed traffic
- replay / RAR / RAW / NC / MMIO 场景
- ROB coverage 接入冒烟

推荐直接查看的报告产物：

- toffee JSON 报告：`src/test/python/MemBlock/data/toffee_report_run/toffee_report.json`
- DUT line coverage HTML：`src/test/python/MemBlock/data/toffee_report_run/line_dat/index.html`

## 推荐工作方式

为了降低 testcase 与环境内部实现的耦合，后续开发建议遵循以下规则：

- 新 testcase 优先通过 sequence + `MemBlockEnv` public facade 组织，不直接依赖 `env.memory` 内部容器。
- 主动控制入口默认使用 `env.backend` 或 `request_apis.py`，不要再新增 `env.note_*` / `env.pulse_*` 风格 helper。
- 需要验证 backend/issue 的拍级发送组合时优先写 `BackendSendPlan`；需要沉淀可复用测试场景时优先上提成 sequence。
- 需要新增 DUT 白盒观测时，优先扩 `monitors/` 或 env facade，不把私有 DUT 命名直接散落到测试文件。
- 需要增强检查逻辑时，优先修改 `model/` 与相应设计文档，不在 testcase 中堆临时判断。
- 覆盖率状态与下一步补强工作以 `coverage_summary.md` 和 `coverage_todo.md` 为准。

## 多人协同建议

当多人同时在当前验证项目上工作时，建议按职责拆分，而不是多人共同修改同一批 testcase 与模型文件：

- testcase / sequence
  - 负责 `tests/`、`sequences/` 中的新场景与覆盖率补强。
- env / monitor / facade
  - 负责 `MemBlock_env.py`、`agents/`、`monitors/` 的稳定接口与白盒观测。
- model / coverage
  - 负责 `memory_model.py`、`model/`、ROB coverage、drain 校验与单测。
- integrator / owner
  - 负责 `README.md`、`CHANGELOG.md`、`ROLES.md`、`docs/` 与跨角色任务收口。

协同原则：

- 公共接口先文档化，再改代码。
- testcase、model、docs 尽量分开提交。
- CHANGELOG 只追加，不覆盖旧条目。
- `docs/coverage_summary.md` 与 `docs/coverage_todo.md` 作为统一状态源，不维护多份私有进度表。
- 角色分工、默认工作方式和 agent 角色选择规则以 `ROLES.md` 为准。

## 文档定位

- 根目录 `CHANGELOG.md` 记录的是 Python 验证环境自身的演进。
- `docs/DUT_CHANGELOG-20260331.md` 记录的是 DUT / RTL 侧变化及其对验证环境的影响。
- `docs/coverage_summary.md` 记录的是当前回归结果。
- `docs/coverage_todo.md` 记录的是下一步补强工作。

四者定位不同，不应混用。
