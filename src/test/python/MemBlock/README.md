# MemBlock Python Verification Environment

`src/test/python/MemBlock/` 是面向 MemBlock 真实 DUT 的 Python 验证环境目录。这里不仅包含 testcase、运行环境和 WebUI，也收纳了当前验证状态、覆盖率结果、ROB 建模分析和后续验证规划文档。

当前目录已经从“基础 load/store 冒烟环境”演进到“真实 DUT 下的标量 ld/st 白盒验证平台”。环境的当前特点是：

- 基于真实 DUT 驱动，不依赖 mock DUT 替代真实流水线行为。
- 已完成 `env / agents / monitors / model / sequences` 分层。
- load 在线 compare 采用 commit-boundary 语义。
- store 采用 deferred visibility，测试结束时统一 flush/drain 收口。
- backend 请求模型已支持通过 `StoreTxn.mask` 表达连续字节宽度的 scalar partial-store。
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
  - 基于 `env.backend` 的 primitive helper / 兼容层；新 testcase/sequence 不应继续把类型定义或新场景模板堆在这里。
- `transactions.py`
  - `QueuePtr`、`LoadTxn`、`StoreTxn`、`BackendSendPlan` 等公共事务与拍级计划对象。
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
  - testcase 首选的可复用场景模板层，例如 reset、scalar load/store、same-cycle load batch、flush store buffer、replay/order 场景。
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
  - backend 主动控制请求模型专项说明，重点覆盖 `env.backend.send(...)`、`env.backend.execute(...)`、`IssueCyclePlan`、`BackendSendPlan`、`StoreTxn.mask -> SB/SH/SW/SD` 映射与兼容层收敛策略。
- `backend_rob_cookbook.md`
  - 面向 testcase / sequence 作者的 backend/ROB cookbook，集中给出 `BackendSendPlan`、`NonMemBlockerStep`、`StoreCommitReadyStep` 的常见脚本模板。
- `mmu_env_design_and_usage.md`
  - MMU/PTW/DTLB 环境设计与使用说明，集中说明 `env.mmu` 的控制面职责、PTW responder、PMP helper 和推荐调试顺序。
- `vmem_design_and_usage.md`
  - 向量访存环境设计与使用说明，集中说明 `VectorMemTxn`、`env.vector_backend`、`VectorLoadSequence`、当前 real-DUT smoke 口径与 known gap。
- `mmu_fault_directed_cases.md`
  - 标量 load 的 `MMU/TLB/PMP fault matrix` 专题说明，集中解释 `MmuFaultingScalarLoadSequence`、TLB-hit fault 背景和当前异常位断言口径。
- `misalign.md`
  - 面向 scalar load/store misalign 的专题分析，集中说明 DUT 中 misalign 的当前设计形态、验证功能点、推荐验证方案，以及当前环境与 testcase 的满足情况。
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
- `scalar_load_pipeline_probe_cases.md`
  - `scalar load pipeline probe` 专题说明，集中解释 bank-conflict、matchInvalid proxy、hi-prio replay 抢占和 late-STA violation 这组 probe case 的设计意图。
- `DUT_CHANGELOG-20260331.md`
  - 一次针对 RTL `mem/` 目录版本变化的详细对比与验证影响分析。

## 推荐阅读顺序

如果是首次接触当前环境，建议按以下顺序阅读：

1. `README.md`
2. `CHANGELOG.md`
3. `docs/verification_env_design.md`
4. `docs/backend_request_model_design.md`
5. `docs/backend_rob_cookbook.md`
6. `docs/mmu_env_design_and_usage.md`
7. `docs/mmu_fault_directed_cases.md`
8. `docs/vmem_design_and_usage.md`
9. `docs/clock_control_and_migration_guide.md`
10. `docs/memory_model_design.md`
11. `docs/rob_model.md`
12. `docs/coverage_summary.md`
13. `docs/coverage_todo.md`
14. `docs/vp_pipeline_plan.md`
15. `docs/scalar_load_pipeline_probe_cases.md`
16. `tests/test_MemBlock_scalar_load_pipeline.py`
17. `tests/test_MemBlock_scalar_store_pipeline.py`
18. `tests/test_MemBlock_scalar_ordering.py`
19. `tests/test_MemBlock_replay.py`

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
env.expect_scalar_load(rob_idx=txn.rob_idx, pdest=txn.resolved_pdest, addr=txn.addr)
env.drain_writebacks()
```

如果场景已经能被高层 sequence 表达，优先直接复用 `sequences/`；只有在编写新的 primitive 场景或 debug 拍级结构时，才直接调用 `env.backend`。`transactions.py` 负责承载公共数据模型，`request_apis.py` 只负责薄兼容/primitive helper。

如果 testcase 只是要表达稳定的“多条 `load` 同拍”业务场景，优先直接复用 `ScalarLoadBatchSameCycleSequence` / `ScalarLoadBatchWithStaSequence`；只有在需要精确控制拍级结构时，再直接构造脚本化 plan：

```python
from sequences import ScalarLoadBatchSameCycleSequence

result = ScalarLoadBatchSameCycleSequence((load0, load1)).run(env)
assert result.final_state.next_lq_ptr.value == 2
```

更底层的 plan 写法仍保留给 debug 与 backend/ROB 拍级脚本：

```python
from transactions import BackendSendPlan, EnqueueLoadCyclePlan, IssueCyclePlan, IssueOp

env.backend.execute(
    BackendSendPlan.from_steps(
        EnqueueLoadCyclePlan.from_txns(load0, load1),
        IssueCyclePlan.from_ops(
            IssueOp.load_from_txn(load0),
            IssueOp.load_from_txn(load1),
        ),
    )
)
```

从 `2026-04-15` 开始，`robIdx` 默认由 env 在 `prepare()/execute()/send()` 流程中统一分配，不再建议让 testcase 直接把 `req_id` 当成 `robIdx` 编码使用。现在 `req_id` 只保留为 testcase 标签；`txn.rob_idx`、`txn.resolved_pdest`、`txn.resolved_ftq_idx_*`、`txn.resolved_pc` 这类 runtime 字段若在 prepare/send 前访问，会直接报错。如果场景需要在 issue 前拿到已分配的 `robIdx`，先显式调用一次 `prepare(...)`：

```python
prepared = env.backend.prepare(load0)
assert prepared.rob_idx_of(load0) == load0.rob_idx
```

当前 `ResetEnvSequence` 默认会在 reset 后，把 allocator 与 commit frontier 一起 seed 到 wrap 边界前一项。这样大多数真实 DUT 回归都会自然跨过一次 ROB wrap；如果某个 env/unit 场景需要明确保留 `(0,0)` 起点，应显式关闭该 profile。

当前默认 transport 延迟已经收缩为较适合回归的保守值：

- `outer_delay = 4`
- `grant_delay_min = 2`
- `grant_delay_max = 8`

如果需要专门构造 ROB wrap 场景，不要再通过“大 `req_id`”旁路 legacy 编码；应显式 seed allocator / commit frontier：

```python
env.backend.set_next_rob_idx(RobIndex(flag=0, value=511))
env.backend.set_commit_frontier(RobIndex(flag=0, value=511))
```

如果 testcase / sequence 需要显式传 ROB 过滤条件，优先直接传一个 `RobIndex`；只有在最靠近 DUT bundle 的 driver / monitor 边界，再拆成 `rob_idx_flag/rob_idx_value`：

```python
prepared = env.backend.prepare(load0)
env.expect_scalar_load(rob_idx=load0.rob_idx, pdest=load0.resolved_pdest, addr=load0.addr)
env.wait_load_writeback_observed(rob_idx=load0.rob_idx)
env.backend.insert_non_mem_blocker(rob_idx=prepared.rob_idx_of(blocker_ref))
```

如果场景还需要显式操控 ROB 侧阻塞/放行语义，也继续在同一个 `BackendSendPlan` 中补充 ROB 语义步骤，例如 `NonMemBlockerStep`、`StoreCommitReadyStep`，而不是直接操作 `env.rob_agent` 内部队列。

例如，下面这类场景适合直接写在同一个 plan 里：

```python
from transactions import (
    BackendSendPlan,
    EnqueueStoreStep,
    IssueCyclePlan,
    IssueOp,
    NonMemBlockerStep,
    RobRef,
    StoreCommitReadyStep,
    StoreCommitStep,
    StoreRef,
)

store_ref = StoreRef("younger_store")
blocker_ref = RobRef("older_non_mem")

env.backend.execute(
    BackendSendPlan.from_steps(
        NonMemBlockerStep.insert(rob_ref=blocker_ref),
        EnqueueStoreStep.from_txn(store_txn, ref=store_ref),
        IssueCyclePlan.from_ops(
            IssueOp.std(req_id=store_txn.req_id, sq_ptr=store_ref, data=store_txn.data, mask=store_txn.mask)
        ),
        IssueCyclePlan.from_ops(
            IssueOp.sta(req_id=store_txn.req_id, sq_ptr=store_ref, addr=store_txn.addr, mask=store_txn.mask)
        ),
        StoreCommitReadyStep(sq_ptr=store_ref, ready=True),
        StoreCommitStep(count=1),  # 这里仍会被 older non-mem blocker 卡住
        NonMemBlockerStep.release(rob_ref=blocker_ref),
        StoreCommitStep(count=1),
    )
)
```

这里的 `non-mem` 不是一条真的 issue 到 lane 的指令，而是 ROB 程序序里的一个 non-mem placeholder，用来表达“older non-mem op 还没允许提交，younger mem 不能越过它”的语义。

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
- 需要验证 backend/issue 的拍级发送组合，或显式编排 ROB blocker / store readiness 时优先写 `BackendSendPlan`；需要沉淀可复用测试场景时优先上提成 sequence。
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
