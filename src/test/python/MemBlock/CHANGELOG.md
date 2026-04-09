# MemBlock Python Verification Environment CHANGELOG

## 2026-04-08

本条目记录一轮面向 MMU/PTW/DTLB 外围稳定性的 env/facade 补全。目标不是直接扩 testcase，而是先把 Sv39/PTW/PMP 这条控制面补成一个可复用、可 smoke、能穿过 `idle_inputs()` 与 DUT reset 的稳定入口，为后续 translation/replay 场景复用打底。

### 变更摘要

- `MemBlockEnv` 新增 `env.mmu` facade，集中管理 Sv39、sfence、PTW responder 与 PMP helper。
- PTW mock 从“单拍返回一个 256-bit beat”修正为“按 TileLink size 返回完整 multi-beat D 响应”。
- `idle_inputs()` 在清空默认输入后，会重新施加活跃的 Sv39 状态，避免 `csr_agent.reset()` 把测试中途切回 M-mode。
- `reset()` 在 DUT reset 后会重放持久化的 PMP CSR 写入与当前 Sv39 配置。
- 新增真实 DUT smoke，证明 Sv39 + PTW + DTLB + cacheable load 的基础 MMU 闭环可正常工作。

### 1. `env.mmu` facade 收口 MMU 控制面

新增的 `env.mmu` 对外提供：

- `enable_sv39()` / `disable_translation()`
- `install_sv39_gigapage_mapping()`
- `pulse_sfence()`
- `write_distributed_csr()`
- `program_pmp_entry()` / `allow_all_smode_access()`
- `ptw_responder()` context manager

这样后续 testcase 不再需要自己 monkey-patch `idle_inputs()` 或手写分散的 PTW/PMP 临时 helper，而是统一通过 env facade 进入。

### 2. PTW responder 修正为 multi-beat TileLink 响应

此前 PTW mock 仅返回一个 D beat，无法覆盖 `size=6` 的 64B 请求，导致 TLB miss replay 长期滞留。现在 responder 会按请求大小切分完整 beat 序列，并逐 beat 在 D 通道握手返回。

这一步是 MMU smoke 能跑通的关键修复之一。

### 3. 稳定 Sv39 / PMP 配置跨越 `idle_inputs()` 与 reset

本轮明确了两类状态的责任归属：

- `tlbCsr` 输入属于“每拍输入面”，需要在 `idle_inputs()` 后重放。
- PMP CSR 写入属于“DUT 内部状态”，需要在 DUT reset 后重放。

因此：

- `idle_inputs()` 现在会在默认清零后调用 `env.mmu.reapply_inputs()`
- `reset()` 会在 deassert reset 后调用 `env.mmu.reapply_after_reset()`

这使得 MMU testcase 可以在标准 env 生命周期中稳定复用，而不用靠测试局部补丁保活。

### 4. 新增真实 DUT MMU smoke

新增 smoke 覆盖两件事：

- `env.mmu` 激活 Sv39 后，`idle_inputs()` 仍能保持 S-mode + satp 输入稳定。
- 一条 cacheable load 在 Sv39/PTW/PMP 背景下可以真实完成写回，且走 dcache 路径而不是 outer/uncache 路径。

### 5. 验证情况

本轮建议至少执行：

- `python3 -m py_compile`
  - `src/test/python/MemBlock/MemBlock_env.py`
  - `src/test/python/MemBlock/tests/test_MemBlock_env_fixture.py`
  - `src/test/python/MemBlock/tests/test_MemBlock_env_mmu_smoke.py`
- `pytest`
  - `src/test/python/MemBlock/tests/test_MemBlock_env_fixture.py`
  - `src/test/python/MemBlock/tests/test_MemBlock_env_mmu_smoke.py`

## 2026-04-07

本条目记录围绕 `env.backend` 公共控制面完成收口的这一轮 API 清理。重点不再是“引入 facade”，而是把旧的兼容入口真正撤出业务路径，并同步更新环境文档，避免后续新 testcase 再沿着过时接口扩散。

### 变更摘要

- `env.backend` 明确成为 MemBlock Python 验证环境的默认主动控制入口。
- 业务路径中的 `env.note_*` / `env.pulse_*` 风格 helper 已清理，不再作为 public API 继续扩散。
- `request_apis.py`、sequence、webui、低层 agent 的业务流量已经统一走 `env.backend`。
- README 与设计文档已同步更新，明确区分 public facade 与内部 agent。

### 1. 清理旧的 env 兼容控制入口

主要改动：

- 从 `MemBlockEnv` 中移除了以下旧 public control helper：
  - `note_load_issued()`
  - `note_store_allocated()`
  - `note_load_completed()`
  - `pulse_store_commit()`
- 对外保留的主动控制面统一收敛到 `env.backend`。
- `flush_store_buffers_and_wait()` 继续保留为 env facade 的稳定能力，但内部仍通过 backend facade 收口。

这一步的意义在于：新 testcase 不再容易误以为 `MemBlockEnv` 既是被动装配层、又是主动 driver helper 的堆放点。env 与 backend control plane 的职责边界现在更清晰。

### 2. 清理业务路径中的 legacy 调用

主要改动：

- `IssueAgent` 在 load issue 成功后，直接调用 `env.backend.note_load_issued()`。
- `LsqAgent` 在 store enqueue 成功后，直接调用 `env.backend.note_store_allocated()`。
- `sequences/memblock_sequences.py` 中的 store commit 推进统一改为 `env.backend.pulse_store_commit()`。
- `lsq_webui.py` 中的手动 store commit 操作也统一改走 `env.backend`。

清理之后，业务路径里保留的 direct-agent/compatibility 引用，主要只剩 env 自测、ROB 自测和 coverage 自测这类“刻意验证内部结构”的测试代码，而不再是普通场景用例的默认写法。

### 3. 文档与测试同步

主要改动：

- 更新 `README.md`，明确：
  - `MemBlock_env.py` 的公共控制入口是 `env.backend`
  - `request_apis.py` 是 `env.backend` 之上的薄封装
  - 新 testcase 不再新增 `env.note_*` / `env.pulse_*` 风格 helper
- 更新：
  - `docs/verification_env_design.md`
  - `docs/memory_model_design.md`
  - `docs/test_sequence_and_extension_guide.md`
- 更新 env fixture 测试，增加“legacy public control helper 已移除”的断言。

### 4. 验证情况

本轮针对 API 清理执行了以下验证：

- `python3 -m py_compile`
  - `agents/issue_agent.py`
  - `agents/lsq_agent.py`
  - `lsq_webui.py`
  - `MemBlock_env.py`
  - 更新后的测试与文档相关 Python 文件
- `pytest`
  - `tests/test_request_apis_backend_facade.py`
  - `tests/test_MemBlock_scalar_ordering.py`
  - `tests/test_MemBlock_replay.py`
  - `tests/test_MemBlock_env_fixture.py` 相关 backend/env 子集

### 总结

到这一轮为止，MemBlock Python 验证环境在主动控制面上的默认工程约定已经非常明确：

- `env.backend` 是唯一默认 public control plane。
- `request_apis.py` 和 `sequences/` 负责把它包装成更适合 testcase 的调用方式。
- `lsq_agent` / `issue_agent` / `commit_agent` / `rob_agent` 仍然存在，但它们属于 env 内部 backend-facing agents，而不是后续 testcase 应优先直接依赖的 API。

## 2026-04-08（续）

本条目记录在稳定 MMU env/facade 之后，进一步把 `sq dataInvalid + matchInvalid + nuke` 场景切换到新 MMU 环境，并同步把设计摘要沉淀为项目内文档。

### 变更摘要

- 新增 `ScalarSqDataInvalidMatchInvalidSequence`，把目标 replay 场景切到 `env.mmu`。
- 新增真实 DUT smoke：`sq_datainvalid_matchinvalid_nuke`。
- 新增两份文档，收口 MMU 设计/用法和该 replay 用例的设计原理。

### 1. 用例迁移到新 MMU 环境

当前稳定方案没有继续采用“root-A 下 translated store + root-B 下 translated load”的对称结构，而是采用：

1. bare 模式下对 root-B 目标物理页做 cache warmup
2. bare 模式下发 older store 的 `STA(main_va)`
3. 切到 Sv39 root-B
4. 发 TLB prime load
5. 发 younger main load，观测 `dataInvalid + matchInvalid + memoryViolation`

这样做的原因是：在真实 DUT 上，store `STA` 侧的 MMU 组合并不如 load 侧稳定；而“bare older store + translated younger load”能够稳定保留所需的相关性与失配行为。

### 2. 主 load 的后续收敛语义

当前真实 DUT 并不会在出现 `memoryViolation` 后永久没有后续动作。相反，主 load 在后续 replay/recovery 过程中仍可能完成写回，且最终数据来自 older store 补齐后的 store data。

因此 sequence 在观测到目标 invalid/nuke 组合后，还会：

- 补 `STD`
- 推进 store commit
- 为主 load 登记 replay completion expectation
- 等待场景完整收敛

这一步是为了让 testcase 不只“打到一个瞬时波形点”，而是能稳定进入日常回归。

### 3. 新增文档

新增：

- `docs/mmu_env_design_and_usage.md`
- `docs/sq_matchinvalid_nuke_case_analysis.md`

其中：

- 前者说明 `env.mmu` 的职责边界、Sv39/PTW/PMP 设计和推荐使用流程。
- 后者说明 `matchInvalid + dataInvalid + nuke` 场景的设计原理、最终稳定方案和当前 DUT 的实际表现。

### 4. 验证情况

本轮建议至少执行：

- `unset _SITE_PACKAGE_ACTIVATED && source /nfs/share/unitychip/activate && python3 -m pytest src/test/python/MemBlock/tests/test_MemBlock_replay.py -k sq_datainvalid_matchinvalid_nuke -q`
- `unset _SITE_PACKAGE_ACTIVATED && source /nfs/share/unitychip/activate && python3 -m pytest src/test/python/MemBlock/tests/test_MemBlock_replay.py -q`

### 5. MMU / sequence 分层重构

在不改变行为的前提下，本轮进一步把 `matchInvalid_nuke` 的实现从“大而全专题 sequence”重构为：

- `MmuSv39AddressSpaceInstallSequence`
- `MmuSv39ActivateSequence`
- `ScalarSqDataInvalidMatchInvalidTriggerSequence`

其中当前 testcase 实际采用：

1. testcase 先多次调用 install sequence，分别配置 root-A / root-B
2. testcase 再调用 trigger sequence，完成 `bare older store -> activate root-B -> TLB prime -> main load -> recovery`

这样做的目的不是新增能力，而是把“MMU 配置”和“专题行为触发”拆开，使后续其他 MMU testcase 可以直接复用配置部分，同时保住当前用例依赖的关键顺序：older bare store 必须发生在 activation 之前。

## 2026-04-06

本条目记录 2026-04-05 到 2026-04-06 这一轮围绕 ROB coverage、toffee 覆盖率报告、环境状态总结和后续验证规划的新增变化。该条目是对 2026-04-01 分层重构之后“环境已经稳定下来并开始进入覆盖率驱动补强阶段”的补充记录。

### 变更摘要

这一轮工作的重点不再是拆分层次，而是把“当前环境已经能验证什么、覆盖到了哪里、下一步该补什么”真正收敛成可执行的工程资产，主要包括：

- 接入 ROB 相关 function coverage，并通过 toffee 官方 `set_func_coverage` 机制汇总。
- 跑通 toffee 测试报告与 DUT line coverage 报告链路。
- 形成当前验证状态与覆盖率分析文档。
- 形成基于覆盖率结果的下一批 testcase 补强清单。
- 开始把多人协同时的任务边界和文档入口显式化。
- 新增 MemBlock 目录级角色规范，并把 agent 启动时的角色选择流程显式化。

### 1. ROB function coverage 接入 toffee 官方机制

commit: `1c9e965b2`

主要改动：

- 在 `MemBlock_env.py` 的 `env` fixture 生命周期中挂载 ROB coverage collector。
- 使用 toffee 官方 `set_func_coverage(request, groups)` 汇总 function coverage，而不是额外自造报告机制。
- 新增 `model/rob_coverage.py`，把当前 ROB 半模型相关的 DUT 观测事实、当前模型已覆盖能力和已知 gap 组织成 4 组 coverage：
  - `MemBlock.ROB.ObservedBehavior`
  - `MemBlock.ROB.CurrentModel`
  - `MemBlock.ROB.GapObserved`
  - `MemBlock.ROB.KnownModelGaps`
- 新增 `tests/test_MemBlock_rob_coverage.py`，验证 coverage collector 已挂载且能正常导出 toffee 可汇总的结构。
- 新增 `docs/rob_coverage_plan.md`，说明 coverage 模型的来源、分组和与 `rob_model.md` 的映射关系。

这一步的意义在于：ROB 建模讨论不再停留在文档层，而是已经有真实 DUT 观测驱动的 function coverage 落地，可以在每轮回归里持续看到哪些行为已命中、哪些 gap 仍只是“已知缺口”。

### 2. toffee DUT 覆盖率报告链路打通

本轮使用 unitychip/toffee 环境直接运行了当前 `src/test/python/MemBlock/tests/` 回归，并通过 toffee 官方 reporter 生成：

- HTML 测试报告
- JSON 测试报告
- DUT line coverage 原始数据
- `merged.info`
- `genhtml` 生成的 DUT line coverage HTML

执行入口已经明确为：

```bash
source /nfs/share/unitychip/activate >/dev/null 2>&1 || true
pytest -q src/test/python/MemBlock/tests \
  --toffee-report \
  --report-dir src/test/python/MemBlock/data/toffee_report_run \
  --report-name memblock_rob_cov \
  --report-dump-json
```

在补齐 Perl 模块 `DateTime.pm` 后，又重新执行了：

```bash
genhtml --branch-coverage \
  src/test/python/MemBlock/data/toffee_report_run/line_dat/merged.info \
  -o src/test/python/MemBlock/data/toffee_report_run/line_dat
```

最终得到可直接查看的 DUT line coverage HTML：

- `src/test/python/MemBlock/data/toffee_report_run/line_dat/index.html`

这一步的重要性在于：环境现在不仅能跑 testcase，还能稳定给出 function coverage + line/branch coverage 的统一结果，为后续覆盖率驱动补强提供客观依据。

### 3. 当前回归与覆盖率结果收口成文档

commit: `c8ca7a7f7`

主要新增：

- `docs/coverage_summary.md`
- `docs/coverage_todo.md`

其中：

- `coverage_summary.md` 记录当前版本、分支、时间、pytest/toffee 命令、报告路径、功能覆盖率结果、DUT line/branch coverage 结果以及模块级解读。
- `coverage_todo.md` 按优先级整理下一批最值得补的用例，例如：
  - partial-mask store
  - cross-line / cross-beat scalar store
  - misaligned scalar store/load
  - replay 精细化
  - nc store drain / mmio exclusion
  - translation / permission 专题

这一步完成后，当前验证状态不再分散在聊天记录或临时命令输出里，而是有了稳定的项目内文档落点。

### 4. 当前验证状态结论

基于当前这轮完整通过的真实 DUT 回归，可以把环境状态概括为：

- 已完成：
  - 真实 DUT 下的 cacheable scalar load/store 主路径
  - store->load same-addr / overwrite / unrelated ordering
  - flushSb + drain 收尾
  - RAW / RAR / FF / DM / NC 基础 replay 场景
  - ROB function coverage
  - toffee DUT line coverage
- 当前强项：
  - `NewLoadUnit` / `LoadUnitS*` / `LoadPipe`
  - `StoreUnit` / `MainPipe`
  - `ForwardModule`
  - `LsqWrapper`
- 当前主要短板：
  - `NewStoreQueue`
  - `SbufferData`
  - `StoreMisalignBuffer`
  - `LoadQueueUncache` / `Uncache`
  - `VirtualLoadQueue`
  - TLB / PTW / PMP 相关外围路径

也就是说，当前环境已经越过“只会做 smoke”的阶段，但距离“完整 MemBlock/LSU 白盒验证平台”还有明显差距。后续重点不应继续机械堆普通 load case，而应转向 store 深状态、replay 组合、NC/MMIO 闭环和 translation/perms 这几类高价值缺口。

### 5. README 与入口文档开始承担项目总览职责

虽然本轮主要增量落在 docs 与 coverage 报告，但一个很重要的变化是：项目已经需要从“实现先行、文档补记”切换到“文档与状态入口先收口，再继续扩 testcase”。因此 README 和 CHANGELOG 后续将承担更明确的职责：

- `README.md`
  - 负责给出当前环境是什么、能做什么、该读哪些文档、当前在哪个阶段。
- `CHANGELOG.md`
  - 负责记录环境自身最近新增了什么能力与状态结果。
- `docs/coverage_summary.md`
  - 负责记录当前回归结果。
- `docs/coverage_todo.md`
  - 负责记录下一批补强方向。

这一点对多人协同尤其重要，因为如果没有统一入口，后续很容易出现“每个人都知道一点，但没有人知道当前全貌”的状态。

### 6. 多人协同进入可规划阶段

随着环境结构化和 coverage 报告机制落地，当前项目已经适合按职责并行推进。建议后续协同按以下几类工作流拆开：

- testcase / sequence
- env / monitor / facade
- model / scoreboard / coverage
- docs / report / planning

并遵循以下规则：

- 公共接口先在文档中冻结，再改代码。
- testcase、model、docs 尽量分开提交。
- 覆盖率结果统一以 toffee report + `coverage_summary.md` 为准。
- CHANGELOG 只做追加，不覆盖旧条目。

这并不是流程形式主义，而是因为当前项目已经跨过“单人快速迭代”阶段：一旦多人同时修改 testcase、scoreboard、README 和报告脚本，如果没有明确分工和统一状态源，回归结果很快就会失真。

### 7. 增加角色规范与 agent 入口约束

本轮进一步把多人协同从 README 中的“建议”固化成了可执行规则，新增：

- `src/test/python/MemBlock/ROLES.md`
- 仓库根目录 `AGENTS.md`

其中：

- `ROLES.md` 为 MemBlock 目录定义了 4 个默认角色：
  - `Pathfinder` -> `testcase/sequence`
  - `Bridgekeeper` -> `env/monitor/facade`
  - `Oracle` -> `model/coverage`
  - `Captain` -> `integrator/owner`
- 每个角色都使用固定模板描述：
  - 角色名与代号
  - 职责边界
  - 典型工作内容
  - 默认工作风格
  - 提交边界
  - 提交前自查
  - 从 remote 更新代码后的动作
- `AGENTS.md` 明确要求：只要任务落在 `src/test/python/MemBlock/`，agent 在开始规划或修改前必须先阅读 `ROLES.md`，并在首条任务响应中主动告知当前推测的主角色；如果角色推测有歧义，必须先向用户确认。

这一步的意义不只是“多一份文档”，而是把后续多人协同时最容易失控的部分显式收口：

- 谁负责 testcase、env、model、项目收口
- 什么情况下需要拆 commit
- remote 更新后应该做多深的自检
- agent 如何避免一上来就跨层乱改

配合 README 中新增的 `ROLES.md` 入口，当前项目的“入口文档 - 状态文档 - 角色规范 - agent 启动约束”已经形成一个闭环，后续无论是人还是 agent 加入项目，都可以先对齐工作方式，再开始改代码。

## 2026-04-01

本条目记录 `src/test/python/MemBlock/` Python 验证环境在 2026-04-01 这一轮分层重构中的主要变化。它和 `docs/DUT_CHANGELOG-20260331.md` 的定位不同：后者主要记录 RTL 版本变化及其对验证的影响，这份 changelog 记录的是验证环境自身的架构演进。

### 变更背景

在本次重构前，验证环境已经可以稳定支撑 MemBlock 的真实 DUT load/store 主路径回归，但整体组织仍然偏向单体实现：

- `MemBlockEnv` 同时持有 bundle 绑定、默认输入值、请求驱动、环境控制和状态查询。
- `MemoryModel` 同时持有黄金内存、outer/dcache responder、load compare、store shadow 与 drain 收尾逻辑。
- testcase 大量直接依赖 `env.memory.pending_stores`、`env.memory.completed_loads`、`env.memory.*count` 等内部字段。
- `request_apis.py` 以函数式 helper 为主，缺少稳定的事务对象。

这套结构在功能较小时足够直接，但随着 DUT 接口变化、测试数量增加、以及后续要继续引入 monitor/scoreboard/sequence，维护成本会快速升高。因此本轮重构的核心目标是先建立清晰边界，而不是一次性完成所有理想分层。

### 已完成的阶段

#### 1. 添加 env facade 与事务 helper

commit: `ef0791a5d`

主要改动：

- 为 `MemBlockEnv` 增加公开 facade：
  - `preload_u64`
  - `expect_scalar_load`
  - `get_transport_stats`
  - `get_completed_load_count`
  - `get_counter`
  - `get_store_view`
  - `wait_store_materialized`
  - `wait_counter_growth`
  - `wait_memory_quiesce`
  - `assert_no_outstanding`
- 新增 `StoreView`，对外暴露 store 只读视图。
- 新增 `transactions.py`，定义：
  - `QueuePtr`
  - `LoadTxn`
  - `StoreTxn`
- 在 `request_apis.py` 中新增事务接口：
  - `send_load`
  - `expect_load`
  - `send_store`
- 将真实 DUT tests 从直接访问 `env.memory` 内部容器迁移到 facade / transaction 风格接口。

这一步的核心收益是：testcase 开始依赖行为接口，而不是内部实现细节，为后续继续拆 `MemoryModel` 打下了基础。

#### 2. 拆分 active driver agents

commit: `a316e3b46`

主要改动：

- 新增 `agents/` 目录。
- 新增 active agents：
  - `csr_agent.py`
  - `commit_agent.py`
  - `lsq_agent.py`
  - `issue_agent.py`
- `MemBlockEnv` 现在显式组装这些 agent。
- `request_apis.py` 中原先直接驱动 pin 的 enqueue / issue 逻辑下沉到 agent。
- `PendingPtrDriver` 仍保留在 `MemBlock_env.py`，但它的使用入口转为 `CommitAgent`。
- 原先的 `MockCSRInterface` 职责被 `CsrAgent` 承接，env 不再直接持有一坨 CSR 初始化细节。

这一步完成后，`MemBlockEnv` 的角色从“万能控制器”进一步收敛成“顶层组装 + 环境协调 + facade”。这也是后续向 monitor/scoreboard/sequence 继续演进的前提。

#### 3. 提取 `RefMemory`

commit: `bbeca071f`

主要改动：

- 新增 `model/` 目录。
- 新增 `model/ref_memory.py`。
- 将黄金内存相关逻辑从 `MemoryModel` 中拆出，包括：
  - `preload_bytes`
  - `preload_u64`
  - `fill_random`
  - `read`
  - `read_masked`
  - `read_cacheline`
  - `apply_masked_write`
- `MemoryModel` 改为组合 `RefMemory`，同时保留原有 facade，以降低上层改动范围。
- 增加 `RefMemory` 的独立单元测试。

这一步的意义在于：黄金内存不再和 responder、compare、shadow 观测逻辑混在一起。后续无论是 responder 还是 scoreboard，都可以共享 `RefMemory` 提供的统一视图。

#### 4. 提取 `TransportResponder`

commit: `58341892a`

主要改动：

- 新增 `model/transport_responder.py`。
- 将 outer/dcache 请求响应与传输计数器从 `MemoryModel` 中拆出，包括：
  - outer TL-A/TL-D 闭环
  - dcache A/B/C/D/E 闭环
  - 响应延迟建模
  - outstanding 事务统计
  - outer put 对 `drain_log` 的记录
- `MemoryModel` 改为组合 `TransportResponder`，并通过 facade 暴露兼容接口：
  - `stats`
  - `outstanding_transaction_count`
  - `enqueue_outer_response`
  - `enqueue_b_response`
  - `enqueue_d_response`
  - 若干 transport counter property

这一步的意义是：传输响应器和 compare / store retire 逻辑已经不再是一个不可分割的整体。后续如果 outer/dcache 路径回归异常，可以更明确地区分是 responder 问题还是 compare 问题。

#### 5. 提取 `Scoreboard`

commit: `d5c281507`

主要改动：

- 新增 `model/scoreboard.py`。
- 将 `MemoryModel` 中剩余的 load compare、ROB 边界下的 store retire、drain 收尾校验独立出来。
- `MemoryModel` 退化为 `RefMemory + TransportResponder + Scoreboard` 的组装层，并保留兼容 facade。
- 增加 `Scoreboard` 的独立单测入口，验证它可以脱离 transport 单独检查 load compare 语义。

这一步完成后，参考检查逻辑不再和传输响应器粘在一起。后续无论是扩 monitor 还是调整 compare 策略，边界都更稳定。

#### 6. 引入 passive monitors

commit: `6d5b22be5`

主要改动：

- 新增 `monitors/` 目录。
- 新增：
  - `writeback_monitor.py`
  - `store_monitor.py`
  - `mem_status_monitor.py`
- 将原先 `Scoreboard`/`MemBlockEnv` 中直接读取 DUT 输出并更新状态的逻辑下沉到 monitor。
- `Scoreboard` 改为只接收事件并维护检查状态，不再直接依赖 DUT 端口命名。

这一步的意义是明确“观测事实”和“判断结果”的边界。今后如果 DUT 导出口名、shadow 结构或 status 位发生变化，更大概率只需要改 monitor。

#### 7. 引入 sequence 层

commit: `fa9a9eafa`

主要改动：

- 新增 `sequences/` 目录。
- 新增可复用 sequence：
  - `ResetEnvSequence`
  - `ScalarLoadSequence`
  - `ScalarStoreSequence`
  - `FlushStoreBuffersSequence`
- 将典型真实 DUT 用例迁移为 sequence 驱动，而不是在 testcase 中手工拼接 helper。

这一步的核心收益是让测试场景开始成为一等对象。testcase 的关注点转移到“场景和断言”，而不是“每个 helper 该按什么顺序调用”。

#### 8. 收敛统一 `EnvConfig`

主要改动：

- 新增 `env_config.py`。
- 引入 `EnvConfig`、`TransportConfig`、`SequenceConfig`。
- 将 queue 深度、ROB 深度、transport 默认延迟、strict check 策略、reset/drain/materialize/flush 默认周期统一收口。
- `MemBlockEnv` 和 sequence 层开始从统一配置读取默认行为，而不是依赖分散常量。

这一步不是追求“所有结构立即完全参数化”，而是先建立统一配置入口，并对当前不能动态改动的结构性参数做显式校验，避免假参数化。

### 本轮重构后环境的总体形态

当前环境已经形成如下分层：

```text
testcase
  -> sequences / request_apis / transactions
    -> MemBlockEnv facade
      -> active agents
      -> passive monitors
      -> MemoryModel
         -> RefMemory
         -> TransportResponder
         -> Scoreboard
      -> DUT
```

这不是终点，但已经完成了核心角色拆分：

1. testcase 与内部实现解耦。
2. `MemoryModel` 不再承担所有职责。
3. 观测、驱动、检查、场景、配置各有明确归属。

### 兼容与约束

本次重构刻意遵循以下约束：

- 不修改现有 load commit-boundary compare 语义。
- 不改变 store flush / drain 的最终校验语义。
- 不一次性删除旧 helper，而是以 facade 和兼容层平滑过渡。
- 优先保证真实 DUT 主路径回归可持续运行。

因此，当前代码里仍然保留了一些“中间态”设计，例如：

- `MemoryModel` 仍作为 model facade 存在，而不是继续下沉成更薄的 assembler。
- `request_apis.py` 仍保留为 sequence 之下的兼容薄层。
- `EnvConfig` 目前对结构性端口宽度仍以校验为主，尚未完全支持动态重参数化。

这些都属于有意保留的过渡层，而不是遗留错误。

### 验证情况

本轮各阶段都使用非自动插件模式执行回归，即：

`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`

原因是默认 `pytest` 在当前环境下会被外部插件 `pytest_rerunfailures` 的 socket 初始化拦住，不是 MemBlock 代码本身的问题。

已验证的关键路径包括：

- `test_memory_model_store_logic.py`
- `test_MemBlock_random_load.py -k single_preloaded_load_data_check`
- `test_MemBlock_random_store.py -k "single_mmio_store_smoke or single_cacheable_store_then_load_same_addr or single_cacheable_store_flush_smoke"`
- `test_MemBlock_env_fixture.py` 中若干关键冒烟子集
- 对所有新增/修改 Python 文件执行 `python -m py_compile`

### 后续演进方向

当前最值得继续推进的方向主要有：

1. 统一 monitor 事件对象
   - 减少 monitor 到 scoreboard 的细粒度方法耦合。

2. 扩展 sequence 组合能力
   - 增加 mixed traffic、异常路径、flush/replay、memory violation 等高层场景。

3. 推进结构性参数化
   - 让当前仍固定的端口宽度与 pipeline 配置逐步纳入真实可配范围。

4. 收紧 legacy 接口
   - 让新 testcase 优先依赖 sequence + env public API，而不是继续堆叠兼容 helper。

### 总结

这次重构的价值，不在于目录看起来更“像 UVM”，而在于环境已经具备长期维护所需的结构化边界：

- 对外接口稳定。
- active driver 已显式分层。
- passive monitor 已独立。
- 黄金内存、传输响应器、scoreboard 已拆开。
- testcase 已开始依赖 sequence，而不是内部容器或散装 helper。
- 默认策略开始由统一 config 管理。

这为后续继续扩异常场景、随机回归和更复杂的检查器提供了可持续的基础。

## 2026-04-09

### 变更摘要

本条目记录一轮面向 env 时钟治理的收敛重构。目标不是继续在 `agents/`、MMU helper 和各类 wait/pulse 逻辑里散落 `Step()`，而是把 DUT 时钟推进统一收口到 `MemBlockEnv` 内核，同时保留对现有同步 facade/testcase 的兼容。

### 1. env 内新增统一时钟内核

- `MemBlockEnv` 现在持有私有 `EnvClockKernel`，由它独占 `dut.Step(1)` 的执行权。
- 内核统一负责同步 facade 到 async 协程的桥接，并把每拍的 DUT 推进、拍后 monitor/model 更新与 callback 分发固定在 env 内部。
- 对外 `env.Step()` 仍保留同步接口，但内部只作为对 env 时钟内核的兼容包装。

### 2. env 内部 wait/pulse 路径迁入 async 原语

- 新增私有 async 原语：
  - `_step_async()`
  - `_await_cycles()`
  - `_step_and_idle_async()`
- replay/release/nuke/store-materialize/quiesce/drain/flushSb 这类轮询逻辑不再直接散落调用 `Step()`，而是统一改走 env 内核原语。
- MMU 相关的 distributed CSR pulse、`enable_sv39()`、`pulse_sfence()` 也改为 async 实现 + 同步兼容 wrapper。

### 3. active agents 不再自己推进时钟

- `IssueAgent` 与 `LsqAgent` 的内部握手主路径改为 async 协程，由 env 内核统一推进拍数。
- `BackendFacade.step_commit()` 也不再直接调用 `env.Step()`，而是显式委托给 env 的时钟原语。
- 这样 `agents/` 中不再保留零散的 `Step()` 调用，时钟推进语义集中回 env。

### 4. request_apis / sequences 继续去拍级化

- `request_apis.py` 中的 backend reset 等待已改走 `env.wait_backend_reset_deassert()`。
- `sequences/memblock_sequences.py` 与 `sequences/violation_sequences.py` 中原有的显式 `env.Step(...)` 调用已清理，改为：
  - `env.advance_cycles(...)`
  - `env.wait_store_addr_observed(...)`
  - `env.wait_completed_load_count(...)`
- 这样 sequence 层继续保留场景语义，不再携带本地拍级轮询细节。

### 5. callback 与文档同步更新

- `after_step_callback` 现在支持同步函数和 async coroutine callback。
- `test_MemBlock_env_fixture.py` 中：
  - `flush_store_buffers_and_wait()` 的观测方式改为 after-step callback，而不是 monkey-patch `env.Step()`。
  - 新增 async after-step callback smoke，验证 env 内核会在拍后正确 await callback。
- 新增 `docs/clock_control_and_migration_guide.md`，面向开发者说明如何在 env / agent / request_apis / sequence / testcase 各层复用统一时钟原语。

### 6. 验证情况

- 已验证：
  - `python3 -m py_compile`
  - `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -p xdist.plugin -n 16 -q src/test/python/MemBlock/tests`
- 回归结果：
  - `64 passed in 249.64s`
  - 在 `request_apis.py` / `sequences/` 进一步收口后再次执行：
    - `64 passed in 232.32s`
- 这轮修改保持对外同步 facade 不变，因此现有 tests / sequences / `request_apis.py` 不需要批量迁移即可继续运行。

## 2026-04-09：收紧 `sq_datainvalid_matchinvalid_nuke` 的 replay 排他口径

### 1. 用例不再接受 redirect 与 LSQ replay 双去路

- `test_api_MemBlock_scalar_sq_datainvalid_matchinvalid_nuke_smoke` 不再只排除 `DM/DR/TM/NC`。
- 现在对同一条 younger load 明确要求：一旦观测到 flush 级 `memoryViolation`，就不能再从 `replay_queue` / `replay_lane` / `ldu` / `nc_out` 看到该 ROB 的 replay cause。

### 2. transport 统计改为覆盖整个恢复期

- `ScalarSqDataInvalidMatchInvalidTriggerSequence` 现在把 transport 统计的收尾点从“violation 后短窗口”改为“主 load 最终恢复完成之后”。
- 这样 testcase 可以覆盖整个恢复路径，避免后续 refill / outer 请求只是在短窗口之外发生而被漏检。

### 3. sequence 补充全流程 replay 观测

- `sequences/violation_sequences.py` 新增对目标 ROB replay 事件的全流程采样。
- 用例因此能区分“仅有 memoryViolation”与“redirect 后仍向 LSQ 建立 replay 去路”这两类语义。

### 4. 文档同步更新

- `docs/sq_matchinvalid_nuke_case_analysis.md` 已同步改写为新的排他契约，不再把 `FF` 视为可接受的已知行为。

### 5. 已知 DUT bug 暂时以精确条件 `xfail` 挂起

- 当前真实 DUT 仍会在 flush 级 `memoryViolation` 之后暴露 `FF` replay path。
- 该问题已登记为 `DUTBUG-matchinvalid-redirect-replay-dual-path`，并在 `docs/BUGS.md` 中记录 `src/main/` 路径对应的 DUT commit hash。
- 因此 `test_api_MemBlock_scalar_sq_datainvalid_matchinvalid_nuke_smoke` 暂时不是整例无条件 `xfail`，而是在实际观测到这组 replay 事件时调用带 bug tag 的 `pytest.xfail(...)`。
- 这样已知 DUT 缺陷不会把回归直接打红，但其他不相关断言仍保持真实失败能力。

## 2026-04-09：补齐 `BC` / pipeline `NK` 白盒观测与组合场景探索

### 1. 新增同拍 load issue 能力与 `debugLsInfo` 观测

- `IssueAgent` / `BackendFacade` / `request_apis.py` 新增同拍多 load issue helper，供 replay 场景稳定复用。
- `MemBlockEnv` 新增 `sample_load_debug_state()`，把顶层 `io_debug_ls_debugLsInfo_*` 白盒口整理成结构化采样结果。
- `sequences/memblock_sequences.py` 新增 load debug trace / SQ forward 采样 helper，测试侧不再直接散读分散信号。

### 2. 单独场景先行落地

- `ScalarForwardFailReplaySequence` 现在会回填：
  - `sq_forward_event`
  - 目标 load 的 `load_debug_trace`
- 新增 `ScalarBankConflictReplaySequence`，用于证明“同拍双 load + 同 bank + 不同 set”可稳定打到 `BC`。
- 新增 `ScalarPipelineStldNukeSequence`，用于探索“older store 晚到 STA”这一条直接 pipeline `NK` 路径。

### 3. 回归层新增 directed smoke

- `test_MemBlock_replay.py` 新增：
  - `test_api_MemBlock_scalar_bank_conflict_replay_smoke`
  - `test_api_MemBlock_scalar_pipeline_stld_nuke_smoke`
  - `test_api_MemBlock_scalar_bankconflict_sq_datainvalid_nuke_smoke`
- 其中 FF smoke 也同步收紧为必须命中：
  - `dataInvalid=1`
  - `matchInvalid=0`
  - `forwardInvalid=0`

### 4. 组合场景当前状态

- 当前 directed timing 已能稳定构造：
  - `BC`
  - `SQ dataInvalid=1`
  - `matchInvalid=0`
  - 且不向 `RAW/RAR` queue 发起 nuke query
- 但在当前 DUT / issue 时序下，目标 victim load 还没有稳定观测到同一条 load debug `NK` cause。
- 因此：
  - standalone pipeline `NK`
  - `BC + FF + NK` 组合场景
  暂时都以精确条件 `xfail` 形式挂起，避免把其余已构造成功的回归能力一起打红。

### 5. 验证情况

- `python3 -m py_compile`
  - `agents/issue_agent.py`
  - `agents/backend_facade.py`
  - `request_apis.py`
  - `MemBlock_env.py`
  - `sequences/memblock_sequences.py`
  - `sequences/violation_sequences.py`
  - `tests/test_MemBlock_replay.py`
- `pytest`
  - `python3 -m pytest src/test/python/MemBlock/tests/test_MemBlock_replay.py -n 16 -q`
  - 结果：`7 passed, 3 xfailed`

## 2026-04-09：收口 standalone pipeline `NK`

本条目记录 `pipeline NK` 单场景从探索态 `xfail` 收口成稳定回归用例的这轮修正。核心结论不是 DUT 完全打不出 `NK`，而是旧 directed timing 把 `STA` 发得过晚，导致 testcase 自己把目标 load 推到了只看见 `DR/DM` 的路径。

### 变更摘要

- `ScalarPipelineStldNukeSequence` 不再先等 `load_debug.s1` 后再发 `STA`，而是在 load issue 后立即补发 `STA`。
- `test_api_MemBlock_scalar_pipeline_stld_nuke_smoke` 去掉了场景缺口 `xfail`，改为对 `NK` 路径做硬断言。
- `sq_pipeline_stld_nuke.md` 同步更新，明确这次修正的根因和当前验证口径。

### 1. 修正 standalone pipeline NK 的 directed timing

本轮白盒排查表明：

- `STA` 若等到 younger load 已经明显进入 `s1` 再发，`load_debug_trace` 会稳定退化成 `DR/DM`
- `STA` 若在 load issue 后立即发起，目标 load 可以稳定命中 `NK`
- 同时 load 最终仍能写回 older store data，store 也能进入 committed

因此问题根因在 testcase/sequence 的时序，而不是“当前 DUT 完全不支持 pipeline NK”。

### 2. 当前 standalone NK 的验证口径

当前 testcase 硬断言以下事实：

- `load_debug_trace` 命中 `NK`
- 同一 trace 不混入 `BC`
- 同一 trace 不混入 `FF`
- younger load 最终写回 older store data
- older store 最终进入 committed

需要注意的是，当前 `NK` trace 仍可能同时带着 `DR/DM`。这说明当前用例证明的是“pipeline-side nuke 已稳定出现并能恢复完成”，而不是“只剩一个孤立的 `NK` bit”。

## 2026-04-09：收口 `bankconflict + dataInvalid + pipeline NK` 组合场景

本条目记录 `test_api_MemBlock_scalar_bankconflict_sq_datainvalid_nuke_smoke` 从 `xfail` 候选收口为稳定回归用例的这轮修正。核心结论是：旧问题主要是 testcase 时序和 writeback 观测口径不对，而不是 DUT 缺少这条组合路径。

### 变更摘要

- `ScalarBankConflictSqDataInvalidNukeSequence` 改为先等 victim load 的早期 `NK`，再等后续 `BC + FF`，最后才补 `STD` 并提交 store。
- 同一 sequence 的 writeback 观测改为基于 trace 收口，避免顺序等待 lead/victim writeback 时漏采已发生的写回。
- `test_api_MemBlock_scalar_bankconflict_sq_datainvalid_nuke_smoke` 保持对 `NK` 做硬断言，不再依赖 `xfail`。
- `sq_bankconflict_datainvalid_nuke_combo.md` 同步更新，明确“pipeline NK 可稳定构造，但恢复后段仍可能出现 RAW/RAR query 流量”。

### 1. 本轮修正解决了什么

本轮白盒排查确认了两个独立问题：

- 如果 `STD` 补得过早，victim load 会退化成 `FF` 主导的重发表现，看不到稳定 `NK`
- 即便场景构造成功，按固定顺序去等 lead / victim writeback，也可能错过已经先发生的写回事件

因此旧版失败并不能直接证明 DUT 没有这条组合路径。

### 2. 当前验证口径

当前 testcase 硬断言以下事实：

- SQ forward 命中 `dataInvalid=1 && matchInvalid=0`
- victim load 的主 replay 断言点命中 `BC + FF`
- victim load 的完整 debug trace 中稳定出现 `NK`
- victim load 最终写回 older store data
- lead load 不被 older store 污染
- older store 最终进入 committed

同时，本轮去掉了“全程不允许 target RAW/RAR query”这条过强假设，因为成功恢复的波形上可以看到后段 query 流量。

### 3. 验证情况

- `python3 -m py_compile`
  - `src/test/python/MemBlock/sequences/violation_sequences.py`
  - `src/test/python/MemBlock/tests/test_MemBlock_replay.py`
- 定向稳定性回归
  - `python3 -m pytest src/test/python/MemBlock/tests/test_MemBlock_replay.py -k bankconflict_sq_datainvalid_nuke -q -rxX`
  - 连续 5 轮：`5/5` 通过
- replay 回归
  - `python3 -m pytest src/test/python/MemBlock/tests/test_MemBlock_replay.py -n 16 -q`
  - 结果：`9 passed, 1 xfailed`
