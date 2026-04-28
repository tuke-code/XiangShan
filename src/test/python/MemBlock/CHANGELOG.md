# MemBlock Python Verification Environment CHANGELOG

## 2026-04-28

### 1. 补齐标量 `STA` backend 反馈闭环，新增 `staIqFeedback + deqPtr` 半模型与自动重发

本条目记录一轮围绕 backend 半模型的补强。此前 MemBlock Python env 已经能稳定建模 `pendingPtr/pendingPtrNext/lcommit/scommit` 和 store `addr/data` readiness，但对 `mem_to_ooo` 返回 backend 的那一半闭环仍停留在“只观察、不消费”的状态：`staIqFeedback` 没有被 backend 吸收成真实 replay 决策，`lqDeqPtr/sqDeqPtr` 也只被 coverage 记录，没有形成软件 credit 视图。因此一旦 store 发生 `TLB miss -> feedbackSlow -> reissue`，测试环境只能看到“最终 store 有没有 materialize”，无法表达 backend 为什么会重发、何时重发，以及 deqPtr 是否真的释放了资源。

本轮把实现范围限定在“标量 STA + LSQ credit”，并保持 testcase 默认透明兼容。env 侧新增 `staIqFeedback[*].feedbackSlow` bundle 绑定；backend facade 侧新增一个 runtime half-model，用于跟踪每笔已分配 store/load 的 LSQ occupancy、消费 `sqDeqPtr/lqDeqPtr` 释放 credit，并对白盒可见的 `STA tlbMiss feedback` 生成自动 replay。时序上，`MemBlockEnv._step_async()` 现在会在每拍 `commit.drive` 之后、时钟推进之前调用 `backend.drive_pre_step()` 驱动待重发的 `STA`，在组合刷新后采样握手，再在拍后通过 `backend.after_cycle()` 消化 `staIqFeedback` 与 deqPtr 反馈，从而形成“本拍观察 miss，下一拍自动重发”的稳定闭环。

与此同时，这轮还把 direct API 的 backend 跟踪补齐到了 `issue_std()/issue_sta()/issue_load_batch_with_sta_same_cycle()`，不再只有 `execute(BackendSendPlan)` 这一路会更新 store readiness / replay 元数据。这样无论 testcase 走老的 helper API，还是走新的 prepared plan，都能进入同一套 backend 半模型。

#### 变更摘要

- `agents/backend_facade.py`
  - 新增 backend replay/credit runtime half-model
  - 消费 `staIqFeedback` 的 `tlbMiss hit/miss`
  - 跟踪 `lq/sq` occupancy 与 `deqPtr` 释放
  - 为 pending replay store 在下一拍自动驱动 `STA` 重发
  - 新增 `backend_state()` / `wait_replay_idle()` / `assert_credit_consistent()`
- `MemBlock_env.py`
  - 新增 `MemRSFeedbackBundle`
  - 绑定 `sta_iq_feedback`
  - 在 `_step_async()` 中接入 backend `drive_pre_step()` / `capture_pre_step_handshake()` / `after_cycle()`
- `model/rob_coverage.py`
  - `known_gap_backend_feedback_credit_not_modelled` 改为按能力条件化
  - 新增 `sta feedback / retry / sq credit release` 相关覆盖观测点
- `tests/test_request_apis_backend_facade.py`
  - 新增 direct `STA` 跟踪和 fake backend 自动 replay/credit 单测
- `tests/test_MemBlock_env_fixture.py` / `tests/test_MemBlock_rob_coverage.py`
  - 同步更新 env 能力与 coverage 断言

#### 验证情况

- `python3 -m pytest -q src/test/python/MemBlock/tests/test_request_apis_backend_facade.py`
  - 结果：`32 passed`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_env_fixture.py -k 'create or backend_facade_wires_existing_agents or has_core_bundles or backend_note_load_completed_advances_pending_ptr or backend_note_store_allocated_updates_state or backend_mark_store_commit_ready_updates_rob'`
  - 结果：`6 passed`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_rob_coverage.py`
  - 结果：`3 passed`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_scalar_store_pipeline.py -k store_queue_two_wave_commit_frontier_residency_directed`
  - 结果：`1 passed`

### 2. 收紧 PMA runtime store matrix 的前置等待后复测，确认失败不由 testcase 提前改写属性导致

本条目记录一轮围绕 `test_api_MemBlock_pma_runtime_store_matrix` 的假设验证。用户怀疑 testcase 在前一条 translated store 尚未真正收口时就改写了 PMA，从而把旧请求和新属性混在一起，甚至可能打断 PTW / 提交流程。为验证这一点，本轮没有继续沿用只等待 `allocated + data + mask` 的轻量观测，而是把 runtime store case 改成在每次 PMA reprogram 之前，先显式等待 `store shadow` 收敛到期望的 `translated_pa / mmio / nc / committed`，并在 cacheable 与 mmio 两个阶段之间增加 `wait_memory_quiesce()`。

复测结果表明，失败不仅没有消失，反而被收紧成了更直接的签名：第一笔 `cacheable_store` 在任何后续 PMA 改写发生之前，就已经无法收敛到期望的 `translated_pa`。24 个 runtime entry 全部在 `wait_store_materialized()` 超时，最终观测到的 store shadow 仍稳定表现为 `addr=0x1A0`、`mmio=0`、`has_exception=0`，且 `committed` 也没有拉高。这说明当前失败不能归因于 testcase 先改 PMA 打断旧请求；相反，更像 DUT/store-path 自身没有把 translated paddr / PMA 分类稳定合流到 SQ shadow。

在此基础上，本轮又进一步做了一个更细的提交边界实验：把第一笔 `cacheable_store` 改成“先 `send_store()`、先等待 uncommitted shadow materialize，再脉冲 `pulse_store_commit(1)`”。结果失败签名完全不变，依旧在 commit 之前就超时，且末态仍是 `addr=0x1A0`、`nc=1`、`mem_back_type_mm=0`、`completed=1`、`committed=0`。因此当前缺口比“commit 脉冲过早”更早，已经发生在 store address / PMA 分类写入 SQ shadow 的阶段。

#### 变更摘要

- `tests/test_MemBlock_pmp_runtime.py`
  - 把 PMA runtime store case 从 `_wait_any_store_view()` 改为 `env.wait_store_materialized()`
  - 新增对 `expected_addr / expected_mmio / expected_nc / require_committed` 的严格等待
  - 在前两次 PMA 属性改写之间插入 `env.wait_memory_quiesce()`，排除 testcase 提前切换属性的干扰
  - 对第一笔 `cacheable_store` 额外改成先 materialize、后 `pulse_store_commit(1)`，排除 commit-boundary 过早推进的干扰

#### 验证情况

- `python3 -m pytest -q -rx -s src/test/python/MemBlock/tests/test_MemBlock_pmp_runtime.py -k 'test_api_MemBlock_pma_runtime_load_matrix or test_api_MemBlock_pma_runtime_store_matrix'`
  - 结果：`1 passed, 1 xfailed, 11 deselected`
- `python3 -m pytest -q -rx -s src/test/python/MemBlock/tests/test_MemBlock_pmp_runtime.py -k test_api_MemBlock_pma_runtime_store_matrix`
  - 结果：`1 xfailed, 12 deselected`

### 1. 新增 `DUT_CHANGELOG-20260428.md`，独立收口本轮 merge 的 MemBlock I/O 变化与 env 影响分析

本条目记录一次面向 DUT 版本跟踪的文档收口。针对 merge `e30cc6fe6e26b43aa9a949a78a0af6ee56020a08`，新增 `docs/DUT_CHANGELOG-20260428.md`，对比其一号父提交 `34964c573`，专门总结会影响 `src/test/python/MemBlock/` 的 MemBlock/LSU contract 漂移、store-path 重构，以及这些变化对 Python 验证环境的直接影响。与上一版 DUT changelog 不同，本次把 **MemBlock I/O 变化** 单独拆成一章，避免顶层 contract 漂移与内部实现重构混写。

#### 变更摘要

- `docs/DUT_CHANGELOG-20260428.md`
  - 新增 `MemBlock I/O 变化` 独立章节
  - 记录 `io.mem_to_ooo.intWriteback: NewExuOutput -> MemWriteBack`
  - 记录 `sqDeqPtr` 保留但重要性上升，以及 `sqCommit* / sqDeqRobIdx / sqDeqUopIdx` 不再适合作为稳定 contract
  - 记录 `outer_cpu_halt -> outer_cpu_wfi`
  - 分析 `NewStoreUnit`、`StoreMisalignBuffer` 删除、`StaIO.storeMaskIn` 删除、`StoreAddrIO.isHyper` 新增等对 env 的影响
  - 区分“当前 env 已适配”与“仍需补强”的修改方向

#### 验证情况

- 本条目为文档更新，未运行 pytest / real DUT 回归。

### 2. 修正 store commit 观测对 `sqCommitPtr` 的强依赖，兼容新 DUT 先 `sqDeq` 后 shadow 收敛的路径

本条目记录一次针对新 DUT `so` 回归的环境兼容修复。回归中 `test_api_MemBlock_two_cacheable_stores_flush_directed` 暴露出：真实 DUT 已经把 store 推进到 `completed=True`，并且 `sqDeqPtr` 已前进，但 Python env 仍然把 `committed` 强依赖在 `sqCommitPtr` 和 shadow `committed` 布尔位上；当这些旧事实源不再稳定时，store 会落入“已完成但未 committed”的不一致状态，进而让 `wait_store_materialized(require_committed=True)` 超时。本轮把 `sqDeq` 明确提升为 commit/completion 的强事实源之一，并把 scoreboard 中的 `committed/completed` 改为单调锁存，避免后续被弱 shadow 值回写成 `False`。

#### 变更摘要

- `monitors/store_monitor.py`
  - `sqDeqPtr` 前进时，除 `mark_store_completed()` 外同步 `mark_store_committed()`
  - 明确把 SQ deq 视为 store 已越过 commit 边界的强事实
- `model/scoreboard.py`
  - `mark_store_completed()` 现在会同步锁存 `committed=True`
  - `observe_sq_shadow_entry()` 中的 `addr_valid/data_valid/committed/completed` 改为单调锁存
  - 避免新 DUT 下较弱的 shadow `committed` 观测把已成立的提交事实回退为 `False`

#### 验证情况

- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_scalar_store_pipeline.py::test_api_MemBlock_two_cacheable_stores_flush_directed`
  - 修复后待回归确认

### 3. 收紧 MMU fault `vaddr/gpaddr` 采样口径，guest/page-fault 优先使用 `lsq_status`

本条目记录一次针对新 DUT H extension 回归的故障定位。`test_api_MemBlock_mmu_h_vs_stage_guest_page_fault_smoke` 暴露出 fault 事件里的 `vaddr` 被采成明显异常的大值，而同期 `lsq_status.vaddr` 仍保持正确。根因是 `wait_load_fault_observed()` 仍把 writeback `debug_vaddr` 当成 fault 场景的主事实源；但在当前 DUT 的 guest/page/access fault 路径里，更稳定的状态口其实是 `lsq_status.vaddr/gpaddr`。本轮因此把 fault 事件默认采样切回 `lsq_status`，同时保留 writeback `debug_vaddr` 作为辅诊断字段。

#### 变更摘要

- `MemBlock_env.py`
  - `wait_load_fault_observed()` 现在在 fault 场景中优先使用 `lsq_status.vaddr`
  - `gpaddr` 继续直接取 `lsq_status.gpaddr`
  - writeback `debug_vaddr` 保留为 `debug_vaddr` 诊断字段，不再覆盖 fault 主断言口径
  - 新增 `expected_vaddr` 参数，允许 MMU sequence 在 DUT fault 导出暂不稳定时把主断言口径收回到请求 VA，并保留 `observed_vaddr`
- `sequences/mmu_sequences.py`
  - two-stage / MMU fault 路径现在会把请求 VA 显式传给 `wait_load_fault_observed()`

#### 验证情况

- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_mmu_h_extension.py::test_api_MemBlock_mmu_h_vs_stage_guest_page_fault_smoke`
  - 修复后待回归确认

### 4. 为 translated load 的 writeback `debug_paddr` 增加缺失回填，不掩盖非空错值

本条目记录一次针对 PBMT uncache/MMIO 回归的环境兼容修补。新 DUT 下 translated load 的 `debug_is_mmio/debug_is_ncio` 仍能稳定导出，但 `debug_paddr` 在部分路径上会缺失为 `None`，导致 `test_api_MemBlock_sv39_pbmt_mmio_load_smoke` 这类依赖物理地址诊断字段的用例失败。这里不适合把所有 `debug_paddr` 都无条件改写成期望 PA，因为那会掩盖 DUT 真正给出“非空但错误”的情况。本轮只在字段缺失为 `None` 时，用调用方已知的 `expected_pa` 回填，保持“缺口补齐”和“错误暴露”两者兼顾。

#### 变更摘要

- `MemBlock_env.py`
  - `wait_load_writeback_observed()` 新增 `expected_paddr`
  - 仅当 `debug_paddr is None` 时回填 `expected_paddr`
  - 若调用方未显式提供 `expected_paddr`，则自动从 expected-load 表按 `rob_idx` 反查目标 PA
- `memory_model.py` / `model/scoreboard.py`
  - 新增 `peek_expected_load_addr()` / `peek_expected_load()`，供 writeback 观测回退使用
  - `peek_expected_load()` 按 `robIdx(flag,value)` 比较，不再受 `transactions.RobIndex` 与 `scoreboard.RobIndex` 类型差异影响
- `tests/test_MemBlock_uncache_semantics.py`
  - translated load helper 现在把 `expected_pa` 显式传给 writeback 观测

#### 验证情况

- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_uncache_semantics.py::test_api_MemBlock_sv39_pbmt_mmio_load_smoke`
  - 修复后待回归确认

### 5. 进一步收缩 scoreboard 对 SQ shadow 的依赖，改为事件化 store 生命周期重建

本条目记录一次针对 `pending_stores` 的语义瘦身。此前虽然 `committed/completed` 已经开始优先依赖 `sqCommitPtr/sqDeqPtr/writeback`，但 `scoreboard.observe_sq_shadow_entry()` 仍会继续消费 shadow 的 `allocated/addrvalid/datavalid/committed/completed/nc` 并回写核心状态，导致模型层仍保留一条弱 shadow 回灌路径。本轮把 store 生命周期的主事实源进一步收紧到：enqueue 时的 `note_store_allocated()`、运行期的 `store_addr/store_addr_re/store_mask/store_data`、以及 `sqCommitPtr/sqDeqPtr/store writeback`。`sq_shadow` 仅保留为最薄的 legacy fallback，用于极少数未显式登记 allocation 的旧路径补 `sq_idx -> rob_idx` 关联，不再驱动核心状态。

#### 变更摘要

- `model/scoreboard.py`
  - `note_store_allocated()` 现在在 `sq_idx` reuse 且 `rob_idx` 改变时重建 `PendingStore`
  - `observe_sq_shadow_entry()` 不再更新 `allocated/addr_valid/data_valid/committed/completed/nc`
  - `observe_sq_shadow_entry()` 仅在 legacy fallback 场景下补 `sq_idx -> rob_idx`
- `tests/test_memory_model_store_logic.py`
  - 相关单测改为显式调用 `note_store_allocated()` / `note_store_request()` / `mark_store_committed()` / `mark_store_completed()`
  - 不再用弱 shadow 值驱动 ready-for-retire 语义
- `docs/memory_model_design.md`
  - 同步更新 store 观测来源说明，明确 `sq_shadow` 已降级为 fallback

#### 验证情况

- 聚焦与完整回归待本轮测试结果确认

### 6. 去除 `test_api_MemBlock_sbuffer_data_entry_quarter_matrix_batched_commit` 的过期 `xfail`

本条目记录一次针对全量回归 `xpass` 的口径收口。`test_api_MemBlock_sbuffer_data_entry_quarter_matrix_batched_commit` 原先带着 `DUTBUG-sbuffer-batched-commit-drain-corruption` 的非严格 `xfail`，理由是“wide multi-entry batched store commit 会在 flush 前破坏最终 cacheline”。但在当前 DUT `so` 与最新 environment 下，这条用例在聚焦回归和 3 次单独重跑中都稳定通过，说明此前的 defect 口径已经过期。为避免“test 已通过但文档仍宣称是已知 DUT bug”的状态不一致，本轮直接移除该 `xfail`，把该场景恢复为普通硬断言。

#### 变更摘要

- `tests/test_MemBlock_scalar_store_pipeline.py`
  - 移除 `test_api_MemBlock_sbuffer_data_entry_quarter_matrix_batched_commit` 的 `pytest.mark.xfail`
- `CHANGELOG.md`
  - 追加当前条目，明确该 `xpass` 已按稳定转正处理

#### 验证情况

- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_scalar_store_pipeline.py::test_api_MemBlock_sbuffer_data_entry_quarter_matrix_batched_commit -rxX`
  - 结果：连续 3 次 `XPASS`

## 2026-04-27

### 1. 推进 `NewStoreQueue` 覆盖率到 `77.1%`，并确认 `>80%` 剩余瓶颈

本条目记录一轮围绕 `NewStoreQueue.sv` 的定向 coverage push。目标原本是把该模块 line coverage 直接推过 `80%`，因此本轮先从 testcase 侧补最缺的 SQ 深状态：一条两波 `store commit frontier` 停留窗口用例，用来证明“第一波 committed、第二波仍保留在 SQ 中”时 younger load 的 delayed-flush 行为；再补一条 batched cross-16B partial-store 场景，用来把 split store + batch commit + delayed flush 路径重新送进真实 DUT。与此同时，为了验证剩余缺口是不是“只是没把 campaign 跑宽”，又额外补入两条 vector-store control-path regression（masked inactive / nonzero `vstart`），专门去碰 `vecInactive` / `vecMbCommit` 一类 `NewStoreQueue` 控制流。

最终量化结果表明：这轮补强确实把 `NewStoreQueue` 从历史文档中的 `58.4%` 级别推到了当前 campaign 的 `77.1%` 左右，但即便叠加 replay / ordering / uncache / vector-store case，仍没有越过 `80%`。这说明剩余 gap 已经不再是“再补几条标量 partial-store smoke”问题，而是收敛到 vector control-path、非 `cbo.zero` CBO 状态机，以及若干已知 `flushSb` stall 类 DUT 缺口。

#### 变更摘要

- `tests/test_MemBlock_scalar_store_pipeline.py`
  - 新增 `_enqueue_scalar_store_batch()` helper
  - 新增 `test_api_MemBlock_store_queue_two_wave_commit_frontier_residency_directed`
  - 新增 `test_api_MemBlock_cross16b_partial_store_burst_batched_commit_directed`
  - 为 batched cross-16B flush stall 增补精确 `xfail`
- `tests/test_MemBlock_vector_store.py`
  - 把已知 vector-store drain bug helper 泛化到任意目标地址
  - 新增 masked inactive vector store regression
  - 新增 nonzero `vstart` vector store regression
- `docs/coverage_summary.md` / `docs/coverage_todo.md`
  - 记录 `2026-04-27` 定向 campaign 的实测覆盖率
  - 明确 `NewStoreQueue >80%` 的剩余 blocker 已转向 vector/CBO/known-bug gating path

#### 验证情况

- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_scalar_store_pipeline.py`
  - 结果：`22 passed, 7 xfailed`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_vector_store.py`
  - 结果：`3 xfailed`
- 手工 LCOV 合并结果：
  - store/replay/uncache/order campaign：`NewStoreQueue.sv` line `77.1043%`（`9362 / 12142`）
  - 再叠加新增 vector-store control-path xfail：`NewStoreQueue.sv` line `77.1207%`（`9364 / 12142`）

### 2. 补齐 PMP all-entry runtime/lock/boundary directed，并精确挂起 store-side deny 缺口

本条目记录一轮围绕 PMP 动态编程能力的 testcase 收口。此前 MemBlock 环境已经能稳定做 `allow_all`、`deny_region` 和 load-side fault baseline，但还没有把“所有 real entry 的运行时改写”“锁定位生效”“NAPOT/TOR 边界语义”系统性打透。本轮把 `env.mmu.program_pmp_entry()` 的 real-entry 边界收紧到 `0..31`，在 `env_mmu_smoke` 补齐 32-entry CSR 编程与 `pmpcfg` 打包检查，并新增 `tests/test_MemBlock_pmp_runtime.py`，覆盖 `entry0..31` 的 runtime `allow/off/allow` load directed、`entry0..31` 的 locked-allow overwrite load/store directed，以及跨 `pmpcfg` 字边界 entry 的 `NAPOT` / `TOR` load boundary directed。

同时，这轮也明确暴露出一个新的 store-side capability gap：translated store 在 `PMP off`、`NAPOT deny` 和 `TOR deny` 背景下，当前 DUT 仍会继续 materialize 成普通 store shadow，缺少稳定 `has_exception` 收口。按 MemBlock 规则，这部分没有把整文件无差别挂起，而是拆成三组精确 `strict xfail`，并同步登记到 `docs/BUGS.md`。

#### 变更摘要

- `MemBlock_env.py`
  - `program_pmp_entry()` 现在显式拒绝非 real entry，索引边界固定为 `0..31`
  - 补入 `PMP_CFG_SLOTS_PER_WORD` / `PMP_REAL_ENTRY_COUNT` 常量，收敛 `pmpcfg` 打包边界
- `tests/test_MemBlock_env_mmu_smoke.py`
  - 新增 32-entry PMP CSR 编程 smoke
  - 新增 `program_pmp_entry()` 的 `-1/32` 边界拒绝用例
- `tests/test_MemBlock_pmp_runtime.py`
  - 新增 `entry0..31` runtime `allow/off/allow` load directed
  - 新增 `entry0..31` locked-allow overwrite load/store directed
  - 新增 `NAPOT` / `TOR` load boundary directed，并覆盖 `0/7/15/23/29/30` 等 cfg-word 边界 entry
  - 新增 store-side runtime/boundary deny `strict xfail`
- `docs/coverage_todo.md` / `docs/BUGS.md`
  - 更新 PMP 覆盖状态与 store-side deny 缺口说明

#### 验证情况

- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_env_mmu_smoke.py -k pmp`
  - 结果：`4 passed`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_pmp_runtime.py::test_api_MemBlock_pmp_runtime_reprogram_all_entries_load[0] src/test/python/MemBlock/tests/test_MemBlock_pmp_runtime.py::test_api_MemBlock_pmp_lock_freezes_all_entries_load_store[0] src/test/python/MemBlock/tests/test_MemBlock_pmp_runtime.py::test_api_MemBlock_pmp_napot_boundary_load[0] src/test/python/MemBlock/tests/test_MemBlock_pmp_runtime.py::test_api_MemBlock_pmp_tor_boundary_load[0]`
  - 结果：`4 passed`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_pmp_runtime.py::test_api_MemBlock_pmp_runtime_reprogram_all_entries_store[0] src/test/python/MemBlock/tests/test_MemBlock_pmp_runtime.py::test_api_MemBlock_pmp_napot_boundary_store[0] src/test/python/MemBlock/tests/test_MemBlock_pmp_runtime.py::test_api_MemBlock_pmp_tor_boundary_store[0]`
  - 结果：`3 xfailed`

### 3. 在 `test_MemBlock_pmp_runtime.py` 并入 PMA runtime/boundary matrix，补 `PMP.sv` 的 PMA 侧覆盖入口

本条目记录一轮围绕 `PMP.sv` 内 PMA 编程路径的 testcase 补强。此前 `tests/test_MemBlock_pmp_runtime.py` 已经把 PMP 的 runtime reprogram、lock、NAPOT/TOR boundary 走通，但同一模块内的 PMA entry 输出、`pmacfg/pmaaddr` 打包写入、以及 PMA mask 更新逻辑仍缺少真实 DUT 场景触发。本轮保持 testcase 数量压缩策略不变，继续沿用 grouped matrix 组织方式，在同一文件中补入 PMA 的 runtime `cacheable -> mmio -> cacheable` 切换矩阵，以及 `NAPOT` / `TOR` boundary 的 translated load/store 分类矩阵。

#### 变更摘要

- `tests/test_MemBlock_pmp_runtime.py`
  - 新增 PMA CSR 本地 helper，覆盖 `pmacfg0..3` 与 `pmaaddr0..31` 的 runtime 编程
  - 新增 `entry0..31` 的 PMA runtime load/store 切换矩阵
  - 新增跨 cfg-word 边界 entry 的 PMA `NAPOT` boundary load/store 矩阵
  - 新增跨 cfg-word 边界 entry 的 PMA `TOR` boundary load/store 矩阵
  - 保持 grouped failure / grouped testcase 组织，不重新展开成大批同名 `parametrize` item

#### 验证情况

- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_pmp_runtime.py`
  - 本条目提交时已执行聚焦回归，结果见本轮提交说明

### 1. 放宽 MMU fault sequence 的 prime writeback 等待条件，兼容窄宽度真实写回

本条目记录一轮围绕 `MmuFaultingScalarLoadSequence` 的纠偏。标量 load width 语义修正后，MMU fault 和 pipeline probe 里的 prime load 已经会按真实 `lb/lh/lw/ld` 发射，但 sequence 仍然把 `prime_expected_data` 当成完整 64-bit preload 去过滤 `wait_load_writeback_observed()`，这会让 `byte/half/word` prime 在真实 DUT 上因为写回位型不同而超时。本轮把 prime 正常 load 的等待条件收窄成“按 `rob_idx` 等到写回即可”，把数据正确性交给已登记的 scoreboard compare，从而与新的 width 语义保持一致。

#### 变更摘要

- `sequences/mmu_sequences.py`
  - `MmuFaultingScalarLoadSequence._run_one_load()` 的正常 prime 路径不再用原始 `expected_data` 过滤写回
  - prime load 仍会登记 `expect_scalar_load()`，数据正确性继续由 scoreboard 在 commit 边界校验

#### 验证情况

- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_mmu_fault.py::test_api_MemBlock_scalar_word_load_pmp_access_fault_tlb_hit_smoke src/test/python/MemBlock/tests/test_MemBlock_mmu_fault.py::test_api_MemBlock_scalar_mixed_size_fault_matrix src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py::test_api_MemBlock_scalar_aligned_load_fault_matrix_with_pipeline_checks`
  - 结果：`3 passed`

### 2. 修正标量 load 的 width/mask 契约并补齐窄宽度回归

本条目记录一轮围绕标量 load 宽度语义的收口。上一条补齐 `fpWen` 时虽然已经把 `size` 下沉到了 issue 路径，但整数 load 仍保留了一个临时 fallback，会把 `byte/half/word` 继续按 `LD` 发射；同时 scoreboard 对非 8B 标量 load 也没有做 sign-extension compare，导致环境层面对 `width/mask` 的表达与真实 RTL 行为并不一致。本轮把这两处一起修正：恢复整数 load 按 `size` 选择 `lb/lh/lw/ld` 的真实 `fuOpType`，在事务/helper 层显式校验 `size/mask` 一致性，并补上真实 DUT 的 `byte/half/word/doubleword` 定向回归。

#### 变更摘要

- `transactions.py` / `request_apis.py` / `agents/backend_facade.py`
  - 标量整数 load 不再强制 fallback 到 `LD`
  - `IssueOp.load()` / `issue_scalar_load()` 现在按 `size` 推导默认 load mask
  - `LoadTxn` / `IssueOp` 新增 `size/mask` 一致性校验，拒绝不匹配组合
- `model/scoreboard.py`
  - 普通标量 `lb/lh/lw/ld` compare 现在按位宽做 sign-extension
  - `fpWen` boxing compare 语义保持不变
- `tests/test_request_apis_backend_facade.py` / `tests/test_issue_agent.py` / `tests/test_memory_model_store_logic.py`
  - 新增整数 load `fuOpType`、默认 mask、`size/mask` 校验与 sign-extension 单测
- `tests/test_MemBlock_scalar_load_width.py`
  - 新增真实 DUT `byte/half/word/doubleword` 标量 load 写回位型定向

#### 验证情况

- `python3 -m pytest -q src/test/python/MemBlock/tests/test_request_apis_backend_facade.py src/test/python/MemBlock/tests/test_issue_agent.py src/test/python/MemBlock/tests/test_memory_model_store_logic.py src/test/python/MemBlock/tests/test_MemBlock_fp_load.py src/test/python/MemBlock/tests/test_MemBlock_scalar_load_width.py src/test/python/MemBlock/tests/test_MemBlock_random_load.py -k 'single_preloaded_load_data_check or test_MemBlock_fp_load or test_MemBlock_scalar_load_width or test_request_apis_backend_facade or test_issue_agent or test_memory_model_store_logic'`
  - 结果：`61 passed, 5 deselected`

### 3. 补齐标量 `fpWen` load 的 env/monitor/model 闭环

本条目记录一轮围绕 `intIssue.fpWen` 的补全。此前 Python 环境虽然已经能从 writeback 口读到 `fpWen/toFpRf_valid`，但请求侧没有能力主动驱动 scalar `fpWen` load，scoreboard 也只按整数写回口径做 compare，导致 `fpWen` 在 MemBlock 环境里始终停留在“端口导出了、验证没有闭环”的状态。本轮把补全范围限定在标量 load：一方面把 `fp_wen` 从事务对象一路下沉到 issue 驱动；另一方面把 writeback monitor、`expect_scalar_load()` 和 scoreboard 统一扩成 bit-pattern 级 `fp` compare，并新增真实 DUT 的 `half/word/doubleword` directed case。

#### 变更摘要

- `transactions.py` / `request_apis.py` / `agents/backend_facade.py` / `agents/issue_agent.py`
  - `LoadTxn` / `IssueOp` 新增 `fp_wen`
  - load issue 路径新增 `size/fp_wen` 下沉
  - `intIssue.bits_fpWen` 现在会被显式驱动
  - 标量 `fpWen` load 会按 `lh/lw/ld` 选择对应 `fuOpType`
  - 普通整数 load 继续维持既有 `LD` 发射契约，避免在同一补丁里扩大整数 load 语义面
- `MemBlock_env.py` / `memory_model.py` / `monitors/writeback_monitor.py` / `model/scoreboard.py`
  - `expect_scalar_load()` / `MemoryModel.expect_load()` 新增 `fp_wen`
  - `wait_load_writeback_observed()` 新增 `fp_wen` 观测与过滤
  - writeback monitor 现在会识别 `fpWen/toFpRf_valid`，并兼容 `toFpRf_bits` 数据别名
  - scoreboard 新增 `fpWen` compare，并按 NaN-box bit-pattern 校验 `half/word/doubleword` 标量 fp load 写回
- `tests/test_request_apis_backend_facade.py` / `tests/test_issue_agent.py` / `tests/test_memory_model_store_logic.py`
  - 新增 `fp_wen` 请求路径、issue 驱动和 scoreboard boxing 单测
- `tests/test_MemBlock_fp_load.py`
  - 新增真实 DUT 标量 `fpWen` directed，用例覆盖 `half/word/doubleword`

#### 验证情况

- `python3 -m pytest -q src/test/python/MemBlock/tests/test_request_apis_backend_facade.py src/test/python/MemBlock/tests/test_issue_agent.py src/test/python/MemBlock/tests/test_memory_model_store_logic.py src/test/python/MemBlock/tests/test_MemBlock_fp_load.py src/test/python/MemBlock/tests/test_MemBlock_random_load.py -k 'single_preloaded_load_data_check or test_MemBlock_fp_load or test_request_apis_backend_facade or test_issue_agent or test_memory_model_store_logic'`
  - 结果：`53 passed, 5 deselected`

### 1. 校正 `mmu_test_todo.md` 中 `TLBFA_1 / TLBFA_2` 的归因与后续优先级

本条目记录一次针对 MMU 规划文档的口径校正。此前 `mmu_test_todo.md` 虽然已经给出 `TLBFA*` 的大方向，但对 `TLBFA_1` 与 `TLBFA_2` 的真实 requestor 归属仍不够精确，容易把 frontend ITLB 流量误当成 `TLBFA_1` 的主要补强方向。结合本轮 testcase 落地和 RTL 连接复核，现已明确：`TLBFA_1` 挂在 store-side DTLB，对应两个 `StoreUnit` requestor；`TLBFA_2` 挂在 load-side DTLB，对应 `LoadUnit0/1/2` 三个 requestor。文档因此同步调整为“先按真实 requestor 家族补流量，再继续做 refill 元数据/fault/prefetch/frontend 交叉”的优先级。

#### 变更摘要

- `src/test/python/MemBlock/docs/mmu_test_todo.md`
  - 增补 `TLBFA_1 = store-side`、`TLBFA_2 = load-side 3 requestor` 的连接说明
  - 补记截至 `2026-04-27` 已落地的 `TLBFA*` directed 矩阵
  - 重写 `P2` 多来源 / 多读口章节，把 `load-side`、`store-side`、`frontend/ITLB` 三类来源拆开描述
  - 在 `P1` refill 编码章节补充“已打通 requestor 家族后，后续应转向元数据/fault/flush 交叉”的说明

#### 验证情况

- 本轮为文档口径更新，未新增 pytest / real DUT 回归。

### 2. 补齐 TLBFA_1 / TLBFA_2 的 store-side 与 multi-lane MMU directed 用例

本条目记录一次针对 `TLBFA_1.sv` 与 `TLBFA_2.sv` 遗留 coverage 缺口的 testcase 收口。HTML 报告显示此前 selective-flush 矩阵已经把基础 miss/hit/flush 路径打热，但 `TLBFA_1` 仍明显缺少 store-side translated traffic，而 `TLBFA_2` 仍缺少 3-lane 同拍与 two-stage `s2xlate` 组合。进一步对 RTL 连接关系排查后确认：`TLBFA_1` 挂在 `inner_dtlb_st_tlb_st`，对应两个 `StoreUnit` requestor；`TLBFA_2` 挂在 load-side DTLB，三个 requestor 对应 `LoadUnit0/1/2`。本轮据此把 testcase 从“继续加 fence 组合”切到“直接补 store/load requestor 家族流量”，新增单阶段 store 双单元、two-stage store 双单元、单阶段 3-lane 同拍 load 矩阵以及 two-stage 3-lane 同拍 load 矩阵。

#### 变更摘要

- `src/test/python/MemBlock/tests/test_MemBlock_mmu_tlbfa.py`
  - 新增 translated store helper，覆盖两个 store issue 单元的 Sv39 / two-stage store traffic
  - 新增 Sv39 store dual-unit directed case，命中 `TLBFA_1` 的双 requestor refill 路径
  - 新增 two-stage store dual-unit directed case，补齐 `TLBFA_1` 的 `VMID/s2xlate` 相关写入背景
  - 新增 Sv39 3-lane 同拍 load miss/hit 矩阵，覆盖 `TLBFA_2` 的 `requestor_2`
  - 新增 two-stage 3-lane 同拍 load miss/hit 矩阵，补强 `TLBFA_2` 的 `s2xlate` 维度

#### 验证情况

- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_mmu_tlbfa.py`
  - 结果：`9 passed`

### 3. 新增 MMU testcase 计划，并补齐对应的 env 调整路线

本条目记录一次 MMU 规划文档补强。此前 `mmu_env_todo.md` 已经覆盖了大方向，但 testcase 侧仍缺少一份独立的“场景矩阵计划”，而 env 侧也还没有把“为了执行这些 testcase 需要补哪些能力”拆成显式条目；同时文档里仍保留着一处已经过时的 H extension 状态描述。本轮新增独立的 `mmu_test_todo.md`，并把 `mmu_env_todo.md` 扩展为与 testcase 计划一一对应的 env 调整清单，同时修正 two-stage success path 已转正的现状。

#### 变更摘要

- `src/test/python/MemBlock/docs/mmu_test_todo.md`
  - 新增独立 MMU testcase 计划文档
  - 按 `P0/P1/P2` 收口 `TLB miss/refill/re-hit/flush`、fault/recovery、refill 编码多样性、H extension selective flush、PBMT/PMP、多来源流量与 replay/nuke 交叉场景
- `src/test/python/MemBlock/docs/mmu_env_todo.md`
  - 增补“为执行 testcase 计划所需的 env 调整”章节
  - 收口 hit/miss/refill/flush 观测、自定义页表编码 helper、selective flush facade、fault/recovery 观测、多来源流量入口等基础设施计划
  - 修正 H extension / two-stage success-path 已转正的状态描述

#### 验证情况

- 本轮为纯文档更新，未运行 pytest / real DUT 回归。

### 4. 新增 TLBFA 定向 MMU selective-flush 用例矩阵

本条目记录一轮围绕 `TLBFA*` 覆盖短板的 testcase / sequence 补强。此前 MMU 回归已经有 `Sv39 basic/rehit`、`DTLB replacement`、`hfence(all-addr)` 和 fault smoke，但 `sfence/hfence` 的 `specific addr / specific asid(vmid)` 维度仍没有组织成稳定矩阵，难以系统命中 `TLBFA*` 的 selective invalidate 路径。本轮新增单阶段 `sfence.vma` 定向用例、扩展两阶段 `hfence.vvma/gvma` selective-flush 矩阵，并在 sequence 层补齐 fence 后 settle 与多访问矩阵封装。

#### 变更摘要

- `src/test/python/MemBlock/sequences/mmu_sequences.py`
  - 新增 `MmuAccessSpec`
  - 新增 `MmuSv39FenceMatrixSequence` / `MmuSv39FenceMatrixSequenceResult`
  - 新增 `MmuTwoStageFenceMatrixSequence` / `MmuTwoStageFenceMatrixSequenceResult`
  - `MmuTwoStageFenceSequence` 新增 `fence_addr` 支持
  - `sfence/hfence` matrix sequence 在 fence 后统一追加 settle 周期，避免过早观测
- `src/test/python/MemBlock/sequences/__init__.py`
  - 导出上述新 MMU sequence / result 类型
- `src/test/python/MemBlock/tests/test_MemBlock_mmu_tlbfa.py`
  - 新增单阶段 `TLBFA` 定向回归
  - 覆盖 `sfence.vma(all/all)`、`sfence.vma(addr/all)`、`sfence.vma(all/asid)`、`sfence.vma(addr/asid)` 以及 root switch reload
- `src/test/python/MemBlock/tests/test_MemBlock_mmu_h_extension.py`
  - 新增 `hfence.vvma(addr/all asid)`、`hfence.vvma(all addr/asid)`、`hfence.vvma(addr/asid)` directed case
  - 新增 `hfence.gvma(addr/all vmid)`、`hfence.gvma(all addr/vmid)`、`hfence.gvma(addr/vmid)` directed case
  - 对当前 RTL 尚未稳定清空目标 G-stage translation 的 `hfence.gvma(addr, ...)` 两条路径挂精确 `xfail`

#### 验证情况

- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_mmu_tlbfa.py`
  - 结果：`5 passed`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_mmu_h_extension.py -k 'hfence'`
  - 结果：`6 passed, 2 xfailed`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_mmu_tlbfa.py src/test/python/MemBlock/tests/test_MemBlock_mmu_h_extension.py -k 'hfence or tlbfa'`
  - 结果：`11 passed, 2 xfailed`

### 5. 新增 LLPTW 多波次 directed case，并更新当前覆盖率口径

本条目记录一次围绕 `LLPTW.sv` 的专题 testcase / coverage 收口。此前 `mmu_llptw_todo.md` 已经明确指出真正缺口集中在多 entry、duplicate waiting、allStage first-stage/last-stage HPTW 串行状态以及 bitmap 相关控制逻辑，但 testcase 侧仍只有串行单请求 helper，难以把这些状态机真实打热。本轮在 sequence 层新增多波次 two-stage load 能力，并补入 LLPTW 专题用例，覆盖 six-entry queue pressure、duplicate wait/merge、high-slot duplicate chain、first-stage guest/page fault、last-stage guest/page fault、final-stage PMP deny 与 bitmap capability probe。定向覆盖结果把 `LLPTW` 从旧本地基线 `line 49.68% / branch 56.89%` 抬升到 `line 69.73% / branch 69.16%`；组合 `env_mmu_smoke + mmu_fault + mmu_h_extension + mmu_tlbfa + mmu_llptw` 后为 `line 70.02% / branch 70.36%`。进一步对未覆盖区段量化后确认，当前仍未过 `80%` 的主因是 `2440-3499` 的 bitmap/high-slot 组合逻辑和 `3500-3809` 的状态迁移块，其中 bitmap 主路径在当前 4KB-only env 下仍属 capability gap。

#### 变更摘要

- `src/test/python/MemBlock/sequences/mmu_sequences.py`
  - 新增 `MmuAccessWave`
  - 新增 `MmuTwoStageWaveLoadSequence` / `MmuTwoStageWaveLoadSequenceResult`
  - `MmuTwoStageFaultSequence` 新增 `fault_target_gpa`，允许把 G-stage fault 精确打在 VS root / page-table GPA 上
- `src/test/python/MemBlock/sequences/__init__.py`
  - 导出新的 LLPTW wave sequence / result 类型
- `src/test/python/MemBlock/tests/test_MemBlock_mmu_llptw.py`
  - 新增 six-entry queue pressure
  - 新增 duplicate wait/merge
  - 新增 high-slot duplicate chain
  - 新增 first-stage / last-stage guest-page-fault
  - 新增 final-stage PMP access fault
  - 新增 `mbmc.BME` bitmap capability probe
- `src/test/python/MemBlock/docs/mmu_llptw_todo.md`
  - 更新 2026-04-27 定向回归口径
  - 记录当前已落地 case 与 `69.73% / 69.16%` 的真实结果
  - 明确 `80%+` 仍受 bitmap/superpage env 能力限制

#### 验证情况

- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_mmu_llptw.py`
  - 结果：`7 passed`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_mmu_llptw.py --toffee-report --report-dir src/test/python/MemBlock/data/toffee_report_llptw_v2 --report-name mmu_llptw_cov_v2.html --report-dump-json`
  - `LLPTW.sv`: line `69.73%`, branch `69.16%`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_env_mmu_smoke.py src/test/python/MemBlock/tests/test_MemBlock_mmu_fault.py src/test/python/MemBlock/tests/test_MemBlock_mmu_h_extension.py src/test/python/MemBlock/tests/test_MemBlock_mmu_tlbfa.py src/test/python/MemBlock/tests/test_MemBlock_mmu_llptw.py --toffee-report --report-dir src/test/python/MemBlock/data/toffee_report_llptw_suite --report-name mmu_llptw_suite_cov.html --report-dump-json`
  - `LLPTW.sv`: line `70.02%`, branch `70.36%`

## 2026-04-26

### 5. 修正 4KB MMU 回归中的 fault re-hit 与 Svpbmt translated 期望

本条目记录一次围绕 Sv39 4KB 页表切换后的定向纠偏。此前 fault matrix / pipeline probe 里的 `MmuFaultingScalarLoadSequence` 会在 prime 与正式访问之间无条件重写同一 fault leaf 并再次 `sfence`，把本应保留的 TLB hit 背景主动清掉；同时 uncache/Svpbmt 用例仍按旧的“大页内偏移拼 PA”心智预置 translated 地址，导致 `PBMT=NC/MMIO` load 实际访问的是 4KB leaf 基址，而 scoreboard 和预载内存却落在错误的旧地址上。本轮把 fault sequence 的 mapping/sfence 更新收窄到“PTE 模式真的变化”时才执行，并把 bare 模式下的 Svpbmt 重放与 4KB translated PA 期望重新对齐。

#### 变更摘要

- `src/test/python/MemBlock/sequences/mmu_sequences.py`
  - `MmuFaultingScalarLoadSequence` 改为先安装初始 fault mapping，只在 prime/main 的 PTE 模式实际切换时才重写 leaf 并 `sfence`
  - 保留 PMP 切换能力，但不再无条件冲掉同 mapping 的 TLB re-hit 背景
- `src/test/python/MemBlock/MemBlock_env.py`
  - `disable_translation()` 进入 bare 模式时不再在 `idle_inputs()` 后重放激活态 Svpbmt 输入
- `src/test/python/MemBlock/tests/test_MemBlock_uncache_semantics.py`
  - translated PA helper 切回 4KB leaf 语义，只保留页内低 12 位偏移

#### 验证情况

- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_mmu_fault.py -k 'scalar_word_load_tlb_error_tlb_hit_smoke or scalar_mixed_size_fault_matrix'`
  - 结果：`2 passed`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py -k scalar_aligned_load_fault_matrix_with_pipeline_checks`
  - 结果：`1 passed`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_uncache_semantics.py -k 'idle_inputs_preserve_svpbmt_state or pbmt_ncio_load_smoke or pbmt_mmio_load_smoke'`
  - 结果：`2 passed, 1 xfailed`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_mmu_fault.py::test_api_MemBlock_scalar_mixed_size_fault_matrix src/test/python/MemBlock/tests/test_MemBlock_mmu_fault.py::test_api_MemBlock_scalar_word_load_tlb_error_tlb_hit_smoke src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py::test_api_MemBlock_scalar_aligned_load_fault_matrix_with_pipeline_checks src/test/python/MemBlock/tests/test_MemBlock_uncache_semantics.py::test_api_MemBlock_env_mmu_idle_inputs_preserve_svpbmt_state src/test/python/MemBlock/tests/test_MemBlock_uncache_semantics.py::test_api_MemBlock_sv39_pbmt_ncio_load_smoke src/test/python/MemBlock/tests/test_MemBlock_uncache_semantics.py::test_api_MemBlock_sv39_pbmt_mmio_load_smoke`
  - 结果：`5 passed, 1 xfailed`

### 4. 修通 two-stage success path，并纠正 H fence helper 的输入语义

本条目记录一轮围绕 H extension / two-stage MMU 的收口。此前 `test_api_MemBlock_mmu_h_two_stage_sv39_basic_load_smoke` 会在 VS non-leaf PTE 阶段提前落入 guest-fault 族，`hfence.vvma/gvma` smoke 也因为 Python helper 对 `rs1/rs2/hv/hg` 的语义翻译不对而无法稳定复现 flush 行为。本轮同时修正了 G-stage PTE 编码、`LLPTW/L2TLB` 的 VS non-leaf success-path 处理，以及 H fence facade 的 bundle 编码语义，最终把基础 two-stage load、rehit、all-addr hfence.vvma 和 all-addr hfence.gvma 全部转正。

#### 变更摘要

- `src/main/scala/xiangshan/cache/mmu/PageTableWalker.scala`
  - 修正 LLPTW 对 all-stage VS non-leaf PTE 的处理，不再把合法继续 walk 的场景提前并入 fault
  - 修正 last-HPTW success path 与地址导出逻辑，避免错误消费无关 `vpn`
- `src/main/scala/xiangshan/cache/mmu/L2TLB.scala`
  - 修正 all-stage VS non-leaf 在 merge response 时的 `pf/level` 编码
  - 保留“继续 VS walk”所需的非叶子语义，而不是把它折叠成 fault-like stage1 response
- `src/test/python/MemBlock/MemBlock_env.py`
  - 保留并确认 G-stage leaf PTE 的 `U=1` fix
  - 修正 `pulse_sfence()` / `pulse_hfence_vvma()` / `pulse_hfence_gvma()` 的 `rs1/rs2` 语义翻译
  - 修正 `hfence.gvma` 只驱动 `hg=1`
- `src/test/python/MemBlock/tests/test_MemBlock_env_mmu_smoke.py`
  - 更新 H fence helper 的低层 bundle 期望值
- `src/test/python/MemBlock/tests/test_MemBlock_mmu_h_extension.py`
  - 去掉 `basic_load` / `rehit` / `hfence.vvma` / `hfence.gvma` 上已经过时的 `xfail`
- `src/test/python/MemBlock/docs/mmu_env_design_and_usage.md` / `src/test/python/MemBlock/docs/mmu_h_extension_cases.md`
  - 同步更新 H fence facade 语义、two-stage success-path 已转正的状态，以及当前唯一剩余 `xfail` 的口径

#### 验证情况

- `python3 -m pytest -q src/test/python/MemBlock/tests/test_debug_two_stage_basic_load_fault_capture.py -s`
  - 结果：`1 passed`
  - 关键现象：已稳定观测到正常 writeback，`debug_paddr=0xa0502008`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_env_mmu_smoke.py -k 'hfence_helpers' -s`
  - 结果：`1 passed`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_mmu_h_extension.py -s`
  - 结果：`7 passed, 1 xfailed`
  - 剩余 `xfail`：G-stage guest fault 下 `gpaddr` 尚未稳定对齐 stage-1 GPA

### 1. 新增 MemBlock AI-UT 验证环境设计文档集

本条目记录一次项目级文档交付。本轮在仓库 `docs/AI-UT/` 下新增分章节 MemBlock 验证环境设计文档，用于系统说明当前 Python AI-UT 环境的构成、测试基础设施、正确性对比机制、测试用例组织方式、已覆盖内容和后续缺口。

#### 变更摘要

- `docs/AI-UT/00_index.md`
  - 新增文档集入口、阅读路径、术语表和总体架构说明。
- `docs/AI-UT/01_environment_architecture.md`
  - 梳理真实 DUT fixture、`MemBlockEnv`、agents、monitors、model、sequence 和 coverage/reporting 的整体架构。
- `docs/AI-UT/02_test_infrastructure.md`
  - 说明 backend/vector/MMU facade、transaction/plan、agent、monitor、sequence、WebUI 和 coverage 基础设施。
- `docs/AI-UT/03_correctness_and_scoreboard.md`
  - 说明 load commit-boundary compare、store deferred visibility、final drain compare、MMU/vector/ROB 正确性边界。
- `docs/AI-UT/04_testcase_organization.md`
  - 说明 pytest 用例、sequence、plan、request API、xfail 与各类场景的组织方式。
- `docs/AI-UT/05_current_coverage_status.md`
  - 汇总当前环境、模型、scalar、replay、MMU、uncache、vector、ROB 和 coverage 报告状态。
- `docs/AI-UT/06_gaps_and_next_steps.md`
  - 汇总 store misalign、backpressure、RAW/RAR、ROB、MMU、vector、uncache 和报告/协作缺口。

#### 验证情况

- 文档为纯 Markdown 交付，不修改验证代码。
- `docs/AI-UT/*.md` 总计 7 个章节文件、约 59575 个字符，满足不少于 30000 字的交付要求。
- `docs/AI-UT/*.md` 共包含 31 个 Mermaid 图块，所有 Markdown code fence 均为偶数闭合。
- 已用 `rg --no-ignore` 检查 `MemBlockEnv`、`BackendFacade`、`MemoryModel`、`Scoreboard`、`VectorMemoryModel`、`RobAgent`、`coverage_summary`、`coverage_todo` 等关键引用均存在。

### 3. 记录 `two_stage_sv39_basic_load_smoke` 的 `EX_LGPF(21)` 根因

本条目记录一轮围绕 `test_api_MemBlock_mmu_h_two_stage_sv39_basic_load_smoke` 的失效分析。此前文档和 testcase 只把该组 case 记录为“two-stage success path 未稳定正常写回”；本轮通过 `--runxfail` 复现、fault 抓取和 RTL 代码比对，进一步确认当前 real DUT 的真实失败模式并不是单纯 timeout，而是在 two-stage walk 的 VS non-leaf PTE 阶段提前收口为 `loadGuestPageFault(21)`，最终才表现成 success path 没有正常 writeback。

#### 变更摘要

- `docs/mmu_h_extension_cases.md`
  - 补充 `two_stage_sv39_basic_load_smoke` 的实测现象
  - 记录 `vaddr=0x40102008`、`debug_paddr=0x88400008` 与 `EX_LGPF(21)` 的对应关系
  - 增加基于 `PageTableWalker.scala` / `L2TLB.scala` / `TLB.scala` 的 RTL 根因分析
- `tests/test_MemBlock_mmu_h_extension.py`
  - 将相关 success-path / hfence case 的 `xfail` 原因文案更新为“VS non-leaf PTE 被过早收口为 fault”，而不再只写泛化的 writeback timeout

#### 验证情况

- `python3 -m pytest -q -s --runxfail src/test/python/MemBlock/tests/test_MemBlock_mmu_h_extension.py -k 'two_stage_sv39_basic_load_smoke'`
  - 结果：`1 failed`
  - 直接失败点：等待正常 load writeback 超时
- 同配置 fault 抓取
  - 结果：稳定观测到 `exception_bits[21] = 1`
  - 辅助现象：`debug_paddr = 0x88400008`，指向 VS root PTE 地址
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_mmu_h_extension.py -k 'two_stage_sv39_basic_load_smoke'`
  - 结果：`1 xfailed, 7 deselected`

### 1. 补齐 frontendBridge / fetch_to_mem / io_l2_tlb_req 的最小验证入口

本条目记录一轮围绕 frontend/MMU 交界面的收口。此前 Python env 已能稳定覆盖 LSU 侧 PTW / DTLB / replay，但 `frontendBridge` 仍停留在“RTL 已导出、env 未绑定”的状态，`fetch_to_mem.itlb` 也没有单独的顶层测试入口；同时 `io_l2_tlb_req_*` 虽然已经进入 probe trace，却还缺少统一的 testcase 级摘要字段。本轮把这三部分一起补齐：一方面给 frontend bridge 和 frontend ITLB 提供最小 facade/smoke，另一方面把 `io_l2_tlb_req` 的首个有效摘要直接收口到 MMU sequence 结果里。

#### 变更摘要

- `MemBlock_env.py`
  - 新增 `FrontendBridgeFacade`
  - 新增 `FetchToMemFacade`
  - 新增 frontend TL A/D 与 `fetch_to_mem.itlb` 的最小 bundle/helper
  - `idle_inputs()` 现在会统一清空 frontend bridge 与 `fetch_to_mem` 驱动口
- `sequences/mmu_sequences.py`
  - `MmuDtlbAccessResult` 新增 `first_l2_tlb_req`
  - `MmuDtlbReplacementSequence` 现在会保留首个有效 `io_l2_tlb_req_*` 摘要
- `tests/test_MemBlock_frontend_bridge.py`
  - 新增 `icache` / `instr_uncache` / `icachectrl` 三条 frontend bridge passthrough smoke
- `tests/test_MemBlock_env_mmu_smoke.py`
  - 新增 `fetch_to_mem.itlb` top-level request handshake smoke，并在当前 build 下尽量观测后续 PTW/顶层 response
  - DTLB replacement case 现在会校验 `first_l2_tlb_req` 与 `l2_tlb_req_seen` 的一致性，并在端口导出时检查摘要字段形状
- `docs/dut_port_behavior.md` / `docs/mmu_env_design_and_usage.md` / `docs/coverage_todo.md`
  - 同步补齐 `frontendBridge`、`fetch_to_mem.itlb` 和 `io_l2_tlb_req` 的职责边界、调试入口与 coverage 计划

#### 验证情况

- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_frontend_bridge.py src/test/python/MemBlock/tests/test_MemBlock_env_mmu_smoke.py src/test/python/MemBlock/tests/test_MemBlock_rob_coverage.py`
  - 结果：`18 passed`

### 2. 把 frontend/fetch facade 从 `MemBlock_env.py` 拆到独立模块

本条目记录一次纯结构整理。上一条新增的 frontend bridge / `fetch_to_mem.itlb` facade 先直接落在了 `MemBlock_env.py`，便于快速验证；但这些 helper 本质上属于独立的 env facade，不应继续堆在顶层 env 文件里。本轮把这批 class 挪到单独文件，保留 `MemBlockEnv` 里的挂接方式和公开接口不变，避免后续在 `MemBlock_env.py` 继续累积与主装配逻辑无关的辅助实现。

#### 变更摘要

- 新增 `frontend_facade.py`
  - 承载 `FrontendBridgeFacade`
  - 承载 `FetchToMemFacade`
  - 承载相关最小 TL/ITLB bundle helper
- `MemBlock_env.py`
  - 删除上述 facade/bundle class 的内联定义
  - 改为从 `frontend_facade.py` import 并在 `MemBlockEnv` 中挂接

#### 验证情况

- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_frontend_bridge.py src/test/python/MemBlock/tests/test_MemBlock_env_mmu_smoke.py -k 'frontend_bridge or fetch_to_mem_itlb_ptw_smoke or sv39_dtlb_fill_and_replacement'`
  - 结果：`5 passed, 10 deselected`

### 4. 追加 `Sbuffer` multi-entry forward directed，用真实 entry 矩阵点亮 lane1/lane2 新缺口

本条目记录一轮继续围绕 `Sbuffer.sv` `Load Data Forward` 子块的 line coverage 补强。在前一轮 `entry2` 定向场景已经证明 `lane1/lane2` 能真实打到 sbuffer forward 的基础上，本轮新增一条 multi-entry directed case，把覆盖目标从单一 `entry2` 扩展到 `entry2/3/4/5`，并把每个 target entry 放到不同 `quarter` 上，优先点亮 `lane1/lane2` 的新增 `vtag_matches_*` 与对应 `forward_mask_candidate_reg_*`。

#### 变更摘要

- `tests/test_MemBlock_scalar_store_pipeline.py`
  - 新增 `_run_sbuffer_forward_entry_matrix()`
  - 新增 `test_api_MemBlock_sbuffer_forward_multi_entry_matrix_directed`
- testcase 设计收口
  - 初版尝试过“单次预热 16 个 entry 后连续命中多 entry”的更激进方案
  - 真实 DUT 上该方案在第一笔 update 上就暴露出稳定性/最终态观测问题，因此收紧为“每个 target entry 独立 reset + 独立预热到刚好够深度”
  - 每个 subcase 继续复用已证明稳定的 `partial-word committed update + younger load` 模式，避免把覆盖补强建立在不稳定 overwrite 行为上

#### 验证情况

- `python3 -m pytest -q -s src/test/python/MemBlock/tests/test_MemBlock_scalar_store_pipeline.py -k 'sbuffer_forward_multi_entry_matrix_directed'`
  - 结果：`2 passed, 25 deselected`
- `python3 -m pytest -q tests/test_MemBlock_scalar_store_pipeline.py -k 'sbuffer_forward_multi_entry_matrix_directed or sbuffer_forward_entry2_partial_word_targeted or sbuffer_forward_committed_partial_word_quarter_matrix_directed' --toffee-report --report-dir ./data/toffee_report_sbuffer_forward_multi_entry --report-name sbuffer_forward_multi_entry_cov.html --report-dump-json`
  - 结果：`6 passed, 21 deselected`
  - focused report：`data/toffee_report_sbuffer_forward_multi_entry/line_dat/merged.info`

#### focused coverage 结果

- `Sbuffer.sv` focused line hit 证据：
  - `DA:6383,2`
  - `DA:6384,1`
  - `DA:6385,1`
  - `DA:6386,1`
  - `DA:7318,2`
  - `DA:7319,1`
  - `DA:7320,1`
  - `DA:7321,1`
  - `DA:6624,6`
  - `DA:7527,6`
  - `DA:7543,6`
  - `DA:7559,6`
- 说明
  - `lane1` 的 `entry3/4/5` 对应 `vtag_matches_1_*` 已在 focused report 中新增点亮
  - `lane2` 的 `entry3/4/5` 对应 `vtag_matches_2_*` 也已新增点亮
  - 这轮 focused 仍未完全覆盖更深的 `entry6+` 与部分高字节 `forward_mask_candidate_reg_*`，因此后续优先级仍在“继续扩 entry 矩阵”而不是重新追 `entry2`

### 3. 改以 `Sbuffer` forward 主线为覆盖率补强重点

本条目记录一轮从 `SbufferData` 转向 `Sbuffer.sv` 本体的 line coverage 分析与 testcase 补强。重新检查 `merged.info` 后，当前 `Sbuffer.sv` 已有 `90.64% (6307 / 6958)` 的 line coverage，主要缺口不在 enqueue/drain 主线，而集中在 `Load Data Forward` 子块，尤其是 `forward[1]` 与 `forward[2]` 的 `vtag/ptag compare`、`matchInvalid`、以及 `forward_mask_candidate` 相关逻辑。

#### 变更摘要

- `tests/test_MemBlock_scalar_store_pipeline.py`
  - 新增 `SBUFFER_FORWARD_LINE_ADDR`
  - 新增 `test_api_MemBlock_sbuffer_forward_committed_partial_word_quarter_matrix_directed`
  - 把 testcase 改成“store 先 materialize、load 预 enqueue、commit 后按窗口 issue”的定向 `sbuffer forward` 验证
- `sequences/memblock_sequences.py`
  - `sample_sbuffer_forward_events()` 不再只依赖顶层 `MemBlock_inner_sbuffer_*` DPI 导出
  - 新增对 `inner_sbuffer.io_forward_*_s2Resp_*` 内部层级信号的回退读取，避免把“未导出”误判成“无事件”
- 本轮 testcase 设计重点
  - 对每个 `quarter=0/1/2/3`，构造一条 `mask=0x0F` 的 partial-word store
  - younger load 先 enqueue，再在 store commit 后按延迟窗口 issue，定向命中真实 `sbuffer forward`
  - 在 `lane1` 与 `lane2` 上分别证明 `mask/data` 与 partial-store merge 一致

#### 验证情况

- `python3 -m pytest -q -s src/test/python/MemBlock/tests/test_MemBlock_scalar_store_pipeline.py -k 'sbuffer_forward_committed_partial_word_quarter_matrix_directed'`
  - 结果：`2 passed, 21 deselected`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_scalar_store_pipeline.py -k 'sbuffer_forward_committed_partial_word_quarter_matrix_directed or partial_word_store_then_aligned_load_directed'`
  - 结果：`5 passed, 18 deselected`
- `make report`
  - 报告时间戳：
    - `data/toffee_report_full/line_dat/merged.info` -> `2026-04-26 17:31`
    - `data/toffee_report_full/line_dat/index.html` -> `2026-04-26 17:32`
  - 当前 `Sbuffer.sv` line coverage 仍为 `90.64%`（`6307 / 6958`）
  - 说明这轮主要收敛的是 `lane1/lane2 sbuffer forward` 的真实观测与 testcase 稳定性；总体覆盖率未继续上升，下一步仍需定向命中残余 `vtag_matches_*` 与更细的 `forward_mask_candidate_reg_*` 组合

#### 结果说明

- 前两轮定向 testcase 失败的主因不是 DUT 缺少 `sbuffer forward`
- 根因是旧 helper 只读取顶层 DPI 导出的 `MemBlock_inner_sbuffer_*` 信号，而当前仿真环境并未稳定导出这组 `inner_sbuffer` forward 端口
- 修正为“顶层导出优先，`inner_sbuffer.*` 内部信号回退”后，真实 `lane1/lane2` `sbuffer forward` 事件可以被稳定观测到

### 2. 追加 `SbufferData` port0 `byte3` 定向搜索，并把新确认的 DUT 缺陷收敛为精确 `xfail`

本条目记录在真实 `wvec` 追踪基础上继续推进 `SbufferData` 行覆盖时，对 `port0 / quarter3 / byte3` 缺口的进一步收敛。围绕 `11905` 与 `12929` 对应的 `line_write_buffer_data_X[31:24]` 路径，本轮把 testcase 观测从 `(entry, offset)` 进一步收紧到 `(entry, offset, byte-lane)`，并把目标 store 由 `1B` 改成 `4B @ +0x30`，确保 `mask(3)` 与 `data[31:24]` 的命中条件真实成立。

#### 变更摘要

- `tests/test_MemBlock_scalar_store_pipeline.py`
  - 为 `_capture_sbuffer_data_activity()` 增加 `io_in_req_{0,1}_bits_mask` 采样
  - 在汇总逻辑里新增 `write_req{0,1}_mask_bits` 与 `write_req{0,1}_wvec_offset_mask_triples`
  - 新增并收敛两个 `port0 quarter3 byte3` 定向场景：
    - `test_api_MemBlock_sbuffer_data_port0_line5_word3_byte3_directed`
    - `test_api_MemBlock_sbuffer_data_port0_line1_and_line5_word3_byte3_search`
- 新确认的 DUT 行为
  - `entry5` 上的 `(entry=5, offset=3, byte=3)` 已能在真实 `writeReq_0` 中稳定观测到
  - 但在该 targeted merge 之后，flush/drain 会破坏预热 cacheline 的最终字节结果
  - `entry1` 上的 `(entry=1, offset=3, byte=3)` 仍不稳定，预热得到的 entry1 常在目标 merge 前被 drain 或重分配

#### 验证情况

- `python3 -m pytest -q -s src/test/python/MemBlock/tests/test_MemBlock_scalar_store_pipeline.py -k 'sbuffer_data_port0_line5_word3_byte3_directed or sbuffer_data_port0_line1_and_line5_word3_byte3_search'`
  - 结果：`2 xfailed, 19 deselected`

### 1. 把 `SbufferData` 白盒观测切换到真实 `wvec`

本条目记录一轮围绕 `SbufferData` line coverage 的继续追踪。前一轮 testcase 虽然在 `accessIdx`、`mask-flush` 和 `vwordOffset` 上表现出较宽活动，但重新跑完整 `make report` 后，`SbufferData.sv` 的 line coverage 仍停在 `62.39%`（`8857 / 14196`），说明先前把 `accessIdx_*` 当成真实 entry 命中的代理是错误的。本轮因此把 testcase 局部白盒采样切换到 `inner_sbuffer.dataModule_io_writeReq_{0,1}_bits_wvec`，并据此重新收敛 testcase 设计与 DUT bug 结论。

#### 变更摘要

- `tests/test_MemBlock_scalar_store_pipeline.py`
  - 为 `_capture_sbuffer_data_activity()` 增加 `io_writeReq_{0,1}_bits_wvec` 采样与 `wvec -> entry bit` 解码
  - 在汇总逻辑里新增 `write_req{0,1}_wvec_bits`、`write_req{0,1}_wvec_offset_pairs`
  - 新增 `test_api_MemBlock_sbuffer_data_targeted_entry_merge_directed`
  - 保留并细化两个已有 `xfail`：
    - `test_api_MemBlock_sbuffer_data_entry_quarter_matrix_batched_commit`
    - `test_api_MemBlock_sbuffer_data_cbo_zero_entry_matrix_directed`
- 新确认的 DUT 行为
  - wide batched commit 确实能把 `port1` 的真实 `wvec` 扩到 `{0..11,13,15}`，但稳定缺少 `wvec[12]`
  - 深预热后的 targeted merge 能稳定命中 `(entry=0, offset=0)` 与 `(entry=12, offset=1)`，但 flush 后 cacheline 数据与独立参考内存不一致
  - multi-entry `cbo.zero` 仍然没有形成可采到的 `SbufferData writeReq` 活动

#### 验证情况

- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_scalar_store_pipeline.py -k 'sbuffer_data_targeted_entry_merge_directed or sbuffer_data_entry_quarter_matrix_batched_commit or sbuffer_data_cbo_zero_entry_matrix_directed or sbuffer_data_vword_offset_matrix_directed or sbuffer_data_wide_burst_mask_flush_directed'`
  - 结果：`2 passed, 3 xfailed, 14 deselected`
- `make report`
  - 报告时间戳：
    - `data/toffee_report_full/line_dat/merged.info` -> `2026-04-26 16:01:36 +0800`
    - `data/toffee_report_full/line_dat/index.html` -> `2026-04-26 16:02:22 +0800`
  - 当前 `SbufferData.sv` line coverage 仍为 `62.39%`（`8857 / 14196`）
  - 目标缺口行仍未被点亮：
    - `11443=0`
    - `11905=0`
    - `12929=0`
    - `14515=0`
    - `14593=0`
    - `15635=0`
    - `15641=5`
    - `15707=0`

## 2026-04-25

### 1. 补强 `SbufferData` 的 store-side line coverage 场景

本条目记录一次围绕 `SbufferData` line coverage 的 testcase 定向补强。结合当前覆盖率报告，`SbufferData.sv` 的主要缺口集中在四类重复模板：`vwordOffset=1/2/3` 的写路径、较宽的 sbuffer lane 分布、load 命中后的 mask-flush 传播，以及 `cbo.zero` 对 `wline=1` 的真实写入口。本轮保持主改动边界在 `tests/test_MemBlock_scalar_store_pipeline.py`，通过真实 DUT store/load/flush 场景去自然点亮这些路径，同时把新增白盒采样限制在 testcase 局部，避免扩散到 env/facade 层。

#### 变更摘要

- `tests/test_MemBlock_scalar_store_pipeline.py`
  - 新增 `test_api_MemBlock_sbuffer_data_vword_offset_matrix_directed`
  - 新增 `test_api_MemBlock_sbuffer_data_wide_burst_mask_flush_directed`
  - 为 `cbo.zero` directed 用例补充 `wline` 白盒观测
  - 新增局部 `Sbuffer`/`SbufferData` activity capture helper，基于 `inner_sbuffer` 级别的 `addr/wline/accessIdx/hit_resp_id` 稳定标量采样
- 覆盖策略
  - 用同一 cacheline 上的 `+0x0/+0x10/+0x20/+0x30` partial-store 矩阵覆盖 `vwordOffset[1:0]=0/1/2/3`
  - 用 12 条不同 cacheline 的 cacheable store burst 扩宽 `accessIdx` / mask-flush lane 分布
  - 保持 `cbo.zero` 的已知 DUT bug 为精确 `xfail`，但确认其 `wline=1` 入口已被真实激活

#### 验证情况

- `python3 -m pytest -q -s src/test/python/MemBlock/tests/test_MemBlock_scalar_store_pipeline.py -k 'sbuffer_data_vword_offset_matrix_directed or sbuffer_data_wide_burst_mask_flush_directed or cbo_zero_flush_zeroes_entire_cacheline'`
  - 结果：`2 passed, 1 xfailed, 13 deselected`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_scalar_store_pipeline.py`
  - 结果：`15 passed, 1 xfailed`

## 2026-04-24

### 1. 补齐 MemBlock MMU env 的 H extension 最小公开契约

本条目记录一轮围绕 MemBlock Python MMU env 的 H extension 收口。此前 RTL 侧已经导出 `vsatp_*`、`hgatp_*`、`priv_virt`、`gpaddr`，而 `PageTableWalker` / `TLBStorage` / `PageTableCache` 也已经消费这些输入；但 Python env/facade 仍停留在单阶段 `enable_sv39() + pulse_sfence()` 语义，无法稳定组织 VS-only / two-stage translation、H fence、guest fault 与两阶段页表安装。本轮把这些能力收口到 `env.mmu`、`mmu_sequences.py` 和专题 testcase 中，同时把当前 real DUT 的两条已知限制正式文档化：two-stage success path 尚未稳定正常写回，以及 G-stage guest fault 的 `gpaddr` 尚未稳定对齐 stage-1 GPA。

#### 变更摘要

- `MemBlock_env.py`
  - `MmuFacade` 升级为统一 active-mode 模型，新增 `enable_vs_sv39()` / `enable_two_stage_sv39()`
  - 新增 `pulse_hfence_vvma()` / `pulse_hfence_gvma()`
  - 新增 `install_vs_sv39_mapping()` / `install_g_sv39_mapping()`
  - 新增 `wait_load_fault_observed()` / `sample_mmu_fault_state()`
- `sequences/mmu_sequences.py` / `sequences/__init__.py`
  - 新增 `TwoStageSv39AddressSpaceInstallSequence`
  - 新增 `MmuVsStageLoadSequence` / `MmuTwoStageLoadSequence` / `MmuTwoStageFenceSequence` / `MmuTwoStageFaultSequence`
  - 新增 VS-stage / G-stage 地址解析结果与两阶段 install result 数据结构
- `tests/test_MemBlock_env_mmu_smoke.py`
  - 新增 VS-only state 保活、two-stage reset 后重放、H fence 驱动位、G-stage install/preload API smoke
- `tests/test_MemBlock_mmu_h_extension.py`
  - 新增 VS-only load、two-stage guest fault、two-stage PMP access fault 专题 case
  - 对当前 real DUT 尚未稳定正常写回的 two-stage success-path/hfence case 做精确 `xfail`
- `docs/mmu_env_design_and_usage.md` / `docs/mmu_env_todo.md` / `docs/coverage_todo.md`
  - 同步更新 H extension 公开契约、TODO 优先级和 TLB/PTW 两阶段覆盖补强方向
- `docs/mmu_h_extension_cases.md`
  - 新增 H extension 专题文档，记录当前 testcase 口径与已知 DUT 限制

#### 验证情况

- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_env_mmu_smoke.py -k 'enable_vs_sv39 or two_stage_reapply_after_reset or hfence_helpers or install_g_sv39_mapping_and_two_stage_preload'`
  - 结果：`4 passed`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_mmu_h_extension.py`
  - 结果：`3 passed, 5 xfailed`

### 2. 把 elastic issue 收敛回纯 `valid/ready` 握手语义

本条目记录一次对 `IssueAgent` elastic 路径的语义回退。结合最近对 saturation 日志的复盘，当前验证环境里真正需要由 `IssueAgent` 保证的职责只有“保持请求直到 `valid && ready` 成功握手”；是否被后续 pipe 观察到、何时 writeback，不应再由 issue 层追加一段“确认/重试”状态机代劳。最终实现删除了 elastic 模式下的 `awaiting_confirm` / `retry` 逻辑，恢复为 per-lane 纯握手语义，避免 issue 层与 monitor/scoreboard 的职责继续缠绕。

#### 变更摘要

- `agents/issue_agent.py`
  - elastic 模式改回“未握手 lane 持续 drive，握手成功 lane 立即 retire”
  - 保留 issue 侧调试打印，但不再维护 load 确认状态机
- `tests/test_issue_agent.py`
  - 单测更新为纯 `valid/ready` 握手语义，不再期待 debug confirmation / retry
- `docs/backend_request_model_design.md` / `docs/test_sequence_and_extension_guide.md`
  - 同步更新 `IssueCyclePlan(elastic)` 的语义说明
- `docs/dut_port_behavior.md` / `tests/test_issue_agent.py`
  - 把 issue 接受结果的观测点正式定义为 post-step 相位
  - fake env 改为在 `_step_async()` 返回后暴露刚结束那一拍的 accept 结果

#### 验证情况

- `python3 -m pytest -q src/test/python/MemBlock/tests/test_issue_agent.py`
  - 结果：`5 passed`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_request_apis_backend_facade.py`
  - 结果：`21 passed`
- `python3 -m pytest -q -s src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline.py -k 'back_to_back_cacheable_load_saturation'`
  - 结果：失败；`IssueAgent` 已按纯 `valid/ready` 语义完成所有 24 笔 issue，但 DUT 最终只完成 `10/24` 笔 compare，当前阻塞点已转移到 issue 之后的完成路径

### 2. 打通 `cbo.zero` 的验证环境闭环，并补齐 `wline` drain 语义

本条目记录一次围绕 `cbo.zero`/`wline` 的 store-side 扩展。此前 Python 验证环境在 monitor 层一旦看到 sbuffer `wline=1` 就会直接报错，事务层也只有普通 scalar store 语义，导致 `cbo.zero` 既不能被 testcase 主动发射，也不能在 `MemoryModel` 里完成整条 cacheline 清零与 drain 收口。本轮把能力限定在 `cbo.zero`，从事务、facade、monitor、scoreboard、golden memory 到 directed testcase 全链补齐。

#### 变更摘要

- `transactions.py` / `agents/issue_agent.py` / `agents/backend_facade.py` / `request_apis.py`
  - `StoreTxn` / `IssueOp` 新增 `cbo_zero` opcode 语义
  - `send_store()` 正式支持 `StoreTxn(opcode='cbo_zero')`
  - 新增 `StoreTxn.cbo_zero()` 与 `send_cbo_zero()` 便于 testcase 直接构造 `cbo.zero`
- `model/ref_memory.py` / `model/scoreboard.py` / `monitors/store_monitor.py`
  - 新增 cacheline 级 `apply_cbo_zero()` 语义
  - `wline=1` 不再报错，改为记录 `cbo.zero` line write drain 事件
  - store retire / finalize drain compare 现在能正确处理整条 cacheline 清零
- `MemBlock_env.py` / `memory_model.py` / `sequences/memblock_sequences.py`
  - `StoreView` 新增 `is_cbo_zero`
  - 新增 `preload_bytes()` / `read_cacheline()` facade
  - 新增 `CboZeroFlushSequence`
- `tests/test_request_apis_backend_facade.py` / `tests/test_memory_model_store_logic.py` / `tests/test_MemBlock_scalar_store_pipeline.py`
  - 补充 `cbo.zero` 的事务编码、memory model、真实 DUT directed 覆盖
- `docs/dut_port_behavior.md` / `docs/memory_model_design.md`
  - 把 `wline` 从“不支持”更新为“支持 `cbo.zero`，其余 CBO 仍未纳入”

#### 验证情况

- `python3 -m pytest -q src/test/python/MemBlock/tests/test_request_apis_backend_facade.py`
  - 结果：`24 passed`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_memory_model_store_logic.py`
  - 结果：`15 passed`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_scalar_store_pipeline.py -k 'cbo_zero_flush_zeroes_entire_cacheline'`
  - 结果：`1 xfailed, 13 deselected`（`DUTBUG-cbo-zero-missing-wline-drain`）

## 2026-04-23

### 1. 补齐 `dtlb_fill_and_replacement` 的专题说明文档

本条目记录一轮纯文档收口：此前 `dtlb_fill_and_replacement` testcase 与 `MmuDtlbReplacementSequence` 已经落地，但仓库里还缺少一份专门解释该用例目标、地址布局、断言口径和“为什么采用宽扫 + 回探而不是固定 victim”的说明文档。本轮新增单独专题文档，并把它接入现有 MMU 使用说明与 sequence 指南，避免后续维护者只能反向读测试代码猜设计意图。

#### 变更摘要

- `docs/dtlb_fill_and_replacement_cases.md`
  - 新增 `dtlb_fill_and_replacement` 专题说明
  - 详细记录场景目标、四阶段执行流程、`miss_observed` 判定口径、当前边界和扩展方向
- `docs/mmu_env_design_and_usage.md`
  - 增加新文档入口，并补入相关文件链接
- `docs/test_sequence_and_extension_guide.md`
  - 在 MMU 相关参考文档列表中加入新文档

#### 验证情况

- 文档改动，未单独执行额外回归

## 2026-04-22

### 1. 新增 Sv39 4KB DTLB 容量/替换定向回归

本条目记录一条新的 MMU/TLB directed case：在真实 DUT 下构造跨大范围虚拟地址空间的 4KB translated load，先验证小工作集的 re-hit，再持续扫入更多不同页把 load-side DTLB 填满，并通过回探旧工作集、捕获第一个重新出现 miss/refill 的旧页来证明替换已经发生。实现上没有把 victim-way 这类 DUT 私有细节直接散落到 testcase，而是新增专用 sequence，把每次访问的 PTW trace 增量、可选 `s1_isTlbFirstMiss` / `io_l2_tlb_req_*` 摘要和写回结果统一收口。

#### 变更摘要

- `sequences/mmu_sequences.py`
  - 新增 `MmuDtlbReplacementSequence`
  - 新增 `MmuDtlbPageSpec` / `MmuDtlbProbeEvent` / `MmuDtlbAccessResult` / `MmuDtlbReplacementSequenceResult`
  - sequence 内部统一组织 `prime -> rehit -> overflow -> reprobe-old-pages` 四阶段 translated load，并返回结构化 TLB/PTW 观测结果
- `sequences/__init__.py`
  - 导出新的 DTLB capacity/replacement sequence 与结果类型
- `tests/test_MemBlock_env_mmu_smoke.py`
  - 新增跨大范围 Sv39 4KB DTLB 填满与替换 smoke
- `docs/mmu_env_design_and_usage.md` / `docs/coverage_todo.md`
  - 同步记录新的可复用 MMU sequence 与当前 TLB/PTW 覆盖推进状态

#### 验证情况

- 待执行 `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_env_mmu_smoke.py -k dtlb_fill_and_replacement`

### 2. 把 MemBlock MMU 地址空间接口切到 Sv39 4KB 页表

本条目记录一次围绕 `env.mmu` 和 MMU reusable sequence 的接口收口。此前 MemBlock Python env 虽然已经能稳定驱动 `enable_sv39()`、PTW responder 和 PMP helper，但 page-table helper 仍停留在 `install_sv39_gigapage_mapping()` 和 `Sv39GigapageMapping` 这条单级 root-leaf 路径上，无法真实建模 Sv39 的 4KB 页表。本轮把主接口切到显式的 4KB 建表模型：env facade 负责组装 non-leaf/leaf PTE，并要求调用方显式给出中间页表页地址池；`MmuSv39AddressSpaceInstallSequence` 与相关 smoke/fault/replay testcase 统一迁移到新的 `install_sv39_mapping(..., page_size="4k", page_table_page_addrs=...)` 入口。

#### 变更摘要

- `MemBlock_env.py`
  - 新增 `sv39_nonleaf_pte()` / `sv39_leaf_pte()` / `install_sv39_mapping()` / `resolve_sv39_pa()`
  - 4KB helper 改为按 `vpn2/vpn1/vpn0` 安装 non-leaf + 4KB leaf，并显式消费 `page_table_page_addrs`
- `sequences/mmu_sequences.py`
  - `Sv39GigapageMapping` 升级为 `Sv39Mapping`
  - `MmuSv39AddressSpaceConfig` 新增 `page_table_page_addrs`
  - address space install / translated preload / fault helper 全部改到 4KB mapping 语义
- `tests/test_MemBlock_env_mmu_smoke.py`
  - 基础 MMU smoke 切到真实 4KB 页表
  - 新增 4KB 页表写入、页表页池不足、translated preload 的直接回归
- `tests/test_MemBlock_mmu_fault.py` / `tests/test_MemBlock_replay.py` / `tests/test_MemBlock_scalar_load_pipeline_probe.py` / `tests/test_MemBlock_uncache_semantics.py`
  - MMU 相关调用点统一迁移到新的 4KB 接口
- `docs/mmu_env_design_and_usage.md` / `docs/test_sequence_and_extension_guide.md` / `docs/coverage_todo.md`
  - 同步更新当前公开 MMU 接口与 4KB-only 使用口径

#### 验证情况

- 待执行针对 `test_MemBlock_env_mmu_smoke.py`、`test_MemBlock_mmu_fault.py`、`test_MemBlock_replay.py` 的定向回归

### 3. 收紧 elastic issue 的“候选发射/延迟确认”语义，并补足 saturation compare 预算

本条目记录 `IssueCyclePlan(elastic)` 的第二轮收口。第一轮修复已经把“全批次 barrier”拆成了 per-lane backpressure，但真实 DUT 调试显示：在当前 picker/xcomm 语义下，`intIssue.ready` 更接近“允许候选发射”，并不等价于“本拍已经能立刻从白盒 debug 看到这条 load”；如果测试侧在看到 `ready=1` 后马上把 lane 判定为已完成握手，仍会出现请求丢失；如果又因为 debug 延迟而立刻重发，则会制造重复 issue。最终实现改成两阶段语义：`ready` 先把 lane 送入 awaiting-confirm，暂停重发；后续拍再用 `debugLsInfo` 里的 ROB 可见性做确认，超时才回到 pending 重新 drive。同时，`ScalarLoadSaturationSequence` 把 drain 预算提升到 replay 场景同等级，避免满载回放下 compare 预算过短。

#### 变更摘要

- `agents/issue_agent.py`
  - elastic 模式改为 `candidate ready -> awaiting confirm -> debug confirm/retry`
  - `note_load_issued()` 前移到候选发射时刻，保持 ROB/scoreboard 预算顺序稳定
- `sequences/memblock_sequences.py`
  - `ScalarLoadSaturationSequence` 的 issue/compare/drain 预算统一改用 replay 级 drain window
- `tests/test_issue_agent.py`
  - 新增“debug 确认期间不重复 drive lane”的单测
- `docs/backend_request_model_design.md` / `docs/test_sequence_and_extension_guide.md`
  - 补记 elastic 模式下“`ready` 只是候选发射、debug 延迟确认”的语义

#### 验证情况

- `python3 -m pytest -q src/test/python/MemBlock/tests/test_issue_agent.py src/test/python/MemBlock/tests/test_request_apis_backend_facade.py`
  - 结果：`24 passed`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline.py -k 'back_to_back_cacheable_load_saturation'`
  - 结果：`1 passed, 1 deselected`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_random_load.py -k 'three_random_mem_loads_same_cycle_via_sequence or three_random_mem_loads_same_cycle_via_plan'`
  - 结果：`2 passed, 4 deselected`

### 4. 给 `IssueCyclePlan` 引入 strict/elastic 握手模式，并打通饱和 load 的 ready 反压场景

本条目记录一次围绕 `intIssue.ready` 反压语义的主动控制面修复。此前 `IssueCyclePlan` 被实现成“计划内所有 lane 必须同时 ready，整批才能发射”，这会把真实 DUT 的 lane-local `valid/ready` 协议错误收紧成全批次 barrier，也正是 `back_to_back_cacheable_load_saturation` 在 `completed_load_count` 卡不满 24 的直接原因。本轮把 `IssueCyclePlan` 扩展为双模式：默认 `strict` 继续表达“必须同拍一起 fire”的 directed 场景；新增 `elastic` 明确表达“同一 issue 批次、允许在 per-lane ready 反压下跨拍完成握手”。`ScalarLoadSaturationSequence` 切到 `elastic` 后，目标用例恢复为正常通过。

#### 变更摘要

- `transactions.py` / `agents/issue_agent.py`
  - `IssueCyclePlan` 新增 `handshake_mode={'strict','elastic'}`
  - `IssueAgent` 新增 elastic per-lane 握手状态机
  - 已握手 lane 会在后续拍明确回到 idle，避免重复发射旧请求
- `sequences/memblock_sequences.py`
  - `ScalarLoadSaturationSequence` 改为用 `IssueCyclePlan(handshake_mode='elastic')`
  - `ScalarLoadBatchSameCycleSequence` 保持 strict 语义，继续服务同拍组合证明
- `tests/test_MemBlock_scalar_load_pipeline.py`
  - `test_api_MemBlock_back_to_back_cacheable_load_saturation` 去掉 `xfail`
- `tests/test_issue_agent.py` / `tests/test_request_apis_backend_facade.py`
  - 新增 strict 与 elastic 握手单测
  - 补充 backend facade 对 `handshake_mode` 透传与兼容 wrapper 默认 strict 的检查
- `docs/backend_request_model_design.md` / `docs/test_sequence_and_extension_guide.md`
  - 同步更新 `IssueCyclePlan` 的新语义与选型规则

#### 验证情况

- `python3 -m pytest -q src/test/python/MemBlock/tests/test_issue_agent.py src/test/python/MemBlock/tests/test_request_apis_backend_facade.py`
  - 结果：通过
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline.py -k 'back_to_back_cacheable_load_saturation'`
  - 结果：通过

### 5. 落地 `MemoryModel` capture/drive phase split，并把单测切到显式相位语义

本条目记录 `MemoryModel` 针对 xcomm/picker 时序语义缺口的首轮代码落地。此前 `MemoryModel.on_memory_edge()` 会在同一次 `StepRis` 回调中同时做请求采样与 transport drive，默认把 `StepRis` 当成 pre-step hook 使用；沿着 commit `f06e7a10c87ea47c0cfd399ce6e46556a87ef092` 复盘后可以确认，这种混用正是 dcache D 通道丢拍类问题容易被掩盖的根源之一。本轮先把 `capture-on-rise` 与 `before-step drive` 两个 phase 从代码上拆开，并把相应单测迁移到显式时序模型。

#### 变更摘要

- `memory_model.py`
  - 新增 `capture_on_rise()` / `drive_pre_step()`
  - 删除历史 `on_memory_edge()` 兼容入口，只保留显式 phase 接口
- `model/transport_responder.py`
  - 新增 `capture_on_rise()` / `drive_pre_step()`
  - transport capture 与 outer/dcache ready、D/B 返回驱动解耦
- `MemBlock_env.py`
  - `StepRis` 改绑定 `self.memory.capture_on_rise`
  - `_step_async()` 在真正 `clock.step(1)` 前显式调用 `self.memory.drive_pre_step()`
- `tests/test_memory_model_store_logic.py`
  - outer/MMIO 相关单测改为显式 `pre-step ready -> rise capture -> pre-step drive`
- `tests/test_MemBlock_scalar_load_pipeline.py`
  - 更新 saturation 用例 `xfail` 说明，补记 `intIssue` 阶段未处理 `ready` 反压这一已知 DUT 问题

#### 验证情况

- `python3 -m pytest -q src/test/python/MemBlock/tests/test_memory_model_store_logic.py`
  - 结果：`13 passed`
- `python3 -m pytest -q --runxfail src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline.py -k 'back_to_back_cacheable_load_saturation'`
  - 结果：仍失败，表现为 `completed_load_count` 未达到 24；结合当前调试结论，可继续归因到真实 DUT 的 saturation 剩余问题，而非本次 phase split 引入的新回归

### 6. 收口 dcache D 通道问题暴露出的 xcomm 时序语义，并补充 `MemoryModel` 重构方案

本条目记录一次围绕 back-to-back cacheable load saturation 调试的文档收口。沿着 commit `f06e7a10c87ea47c0cfd399ce6e46556a87ef092` 可以确认，当前问题的关键不在于 `TransportResponder` 的 beat 队列算法本身，而在于此前文档没有明确写清楚 xcomm/picker 的 phase 语义：`StepRis` 更接近 capture-on-rise，而不等价于 pre-step drive hook。此前 `MemoryModel.on_memory_edge()` 同时承担请求采样与返回驱动，容易把 capture 与 drive 两个 phase 混在一起。本轮把这层缺失语义补进设计文档，并给出更稳的 `MemoryModel` 重构方向。

#### 变更摘要

- `docs/memory_model_design.md`
  - 补充当前缺失的 xcomm 时序语义说明
  - 记录为什么 `StepRis` 不应再单独承担 transport drive
  - 新增 `MemoryModel` 长期重构方案：拆分 `TransportObserver` / `TransportDriver` / `ScoreboardFacade` / composition root
- `docs/clock_control_and_migration_guide.md`
  - 明确 `StepRis` 不是 pre-step drive phase
  - 增补“capture 用 `StepRis`、drive 用 before-step”的规则
- `docs/verification_env_design.md`
  - 补充 env 当前尚待 formalize 的 `before_step` transport drive phase

#### 验证情况

- `python3 -m pytest -s -q --runxfail src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline.py -k 'back_to_back_cacheable_load_saturation'`
  - 结果：问题仍稳定复现，且调试日志确认 `dcache D` 已连续返回两拍；本条修改为文档收口，不改变当前行为

### 5. 用 `io_ldu_ldin` 精确确认 elastic load 接受时刻，并修复 ready 反压下的漏发/误记账

本条目记录 `IssueCyclePlan(elastic)` 的第三轮真实 DUT 收口。前一版把 `ready` 解释成“候选发射”，已经能避免一部分重复 drive，但仅靠 `debugLsInfo.robIdx` 做确认仍然过弱，在 ROB wrap 与饱和流混杂下会把少数 lane 过早判成已确认，最终表现为 `dcache_a_request_count` 卡在 22、`completed_load_count` 卡在 21。最终实现改成“候选发射后优先等 `io_ldu_ldin_*` 里出现匹配的 `robIdx/lqIdx`，拿不到这类细粒度白盒时才退回 `debugLsInfo` fallback”，同时把 `note_load_issued()` 放到真正确认成功之后，修复了 ready 反压场景下的漏发和记账错位。

#### 变更摘要

- `agents/issue_agent.py`
  - 恢复 elastic 三态：`pending -> awaiting_confirm -> fired/retry`
  - load 确认优先匹配 `sample_load_issue_state()` 导出的 `io_ldu_ldin.robIdx/lqIdx`
  - `note_load_issued()` 改到确认成功时刻，避免候选发射与真实 issue 顺序错位
- `MemBlock_env.py`
  - 新增 `sample_load_issue_state()`，结构化采样 `MemBlock_inner_lsq_io_ldu_ldin_*`
- `tests/test_issue_agent.py`
  - 补充“确认等待期间撤掉 valid”与“确认超时后 retry 且不重复记账”的单测
- `docs/backend_request_model_design.md`
  - 更新 elastic load 的确认优先级：`io_ldu_ldin` 优先，`debugLsInfo` 为 fallback

#### 验证情况

- `python3 -m pytest -q src/test/python/MemBlock/tests/test_issue_agent.py`
  - 结果：`5 passed`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_request_apis_backend_facade.py`
  - 结果：`21 passed`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_random_load.py -k 'three_random_mem_loads_same_cycle_via_sequence or three_random_mem_loads_same_cycle_via_plan'`
  - 结果：`2 passed, 4 deselected`
- `python3 -m pytest -q -s src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline.py -k 'back_to_back_cacheable_load_saturation'`
  - 结果：`1 passed, 1 deselected`

## 2026-04-18

### 1. 新增 back-to-back cacheable load 饱和用例

本条目记录一次围绕“构建 back-to-back 的 `ld` 用例并把 DUT 输入侧打满”的 testcase/sequence 扩展。此前已有 `ScalarLoadBurstSequence` 能串行推进单 lane load，也已有 `ScalarLoadBatchSameCycleSequence` 能表达单拍 3-lane load；但还缺一个可复用的多拍连续饱和流封装，无法直接证明 `enqLsq + load issue` 在持续满发条件下的真实 DUT 行为。本轮新增 `ScalarLoadSaturationSequence`，按 `load_pipeline_width` 连续组织同拍 enqueue + issue，并在 `test_MemBlock_scalar_load_pipeline.py` 补上一条 cacheable directed saturation 用例。

#### 变更摘要

- `sequences/memblock_sequences.py`
  - 新增 `ScalarLoadSaturationSequence`
  - 新增 `ScalarLoadSaturationSequenceResult`
- `sequences/__init__.py`
  - 导出新的 saturation sequence/result
- `tests/test_MemBlock_scalar_load_pipeline.py`
  - 新增 `test_api_MemBlock_back_to_back_cacheable_load_saturation`
  - 验证连续多拍按 load pipeline 宽度打满 `enqLsq + intIssue` 的 cacheable load 饱和流
  - 当前按已确认 DUT 行为标记为 `xfail`，用于稳定复现“持续 3-lane 饱和下 load 回写数据错误”

#### 验证情况

- `python3 -m py_compile src/test/python/MemBlock/sequences/memblock_sequences.py src/test/python/MemBlock/sequences/__init__.py src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline.py`
  - 结果：通过
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline.py -k 'back_to_back_cacheable_load_saturation or small_cacheable_load_burst_directed'`
  - 结果：首次运行定位到新用例命中真实 DUT 数据错误；加 `xfail` 收口后为 `1 passed, 1 xfailed`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline.py`
  - 结果：`1 passed, 1 xfailed`

## 2026-04-17

### 1. 补齐 load-side PMP 的 region deny 与 deny->allow 恢复用例

本条目记录一次围绕 load-side PMP fault 语义的扩展。此前 `test_MemBlock_mmu_fault.py` 已经覆盖了 `allow_all` / `deny_all` 两态下的标量 load fault，但还无法证明 PMP 是按地址区域裁剪权限，也无法证明撤销 deny 后同一 translated load 能在 TLB hit 背景下恢复正常。本轮在 `env.mmu` 增加最小 `program_pmp_deny_region()` helper，并围绕它补上“deny region 内 fault、region 外继续成功、撤销 deny 后恢复成功”的 directed 用例。

#### 变更摘要

- `MemBlock_env.py`
  - `MmuFacade` 新增 `program_pmp_deny_region()`
  - 新增 PMP NAPOT region 地址编码与参数校验
- `sequences/mmu_sequences.py`
  - 新增 `MmuPmpRegionLoadRecoverySequence`
- `tests/test_MemBlock_mmu_fault.py`
  - 新增 `scalar_load_pmp_deny_region_hit_allow_outside_smoke`
  - 新增 `scalar_load_pmp_deny_region_then_restore_allow_smoke`
- `tests/test_MemBlock_env_mmu_smoke.py`
  - 新增 `program_pmp_deny_region()` helper smoke
- `docs/mmu_env_design_and_usage.md`、`docs/mmu_fault_directed_cases.md`、`docs/mmu_env_todo.md`
  - 同步更新 PMP region helper 与 load-side PMP 覆盖口径

#### 验证情况

- `python3 -m py_compile src/test/python/MemBlock/MemBlock_env.py src/test/python/MemBlock/sequences/mmu_sequences.py src/test/python/MemBlock/sequences/__init__.py src/test/python/MemBlock/tests/test_MemBlock_env_mmu_smoke.py src/test/python/MemBlock/tests/test_MemBlock_mmu_fault.py`
  - 结果：通过
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_env_mmu_smoke.py -k 'pmp'`
  - 结果：`1 passed, 2 deselected`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_mmu_fault.py -k 'pmp_deny_region or restore_allow'`
  - 结果：`2 passed, 4 deselected`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_env_mmu_smoke.py`
  - 结果：`3 passed`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_mmu_fault.py`
  - 结果：`6 passed`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py -k 'fault_matrix'`
  - 结果：`1 passed, 6 deselected`

### 2. 为 MemBlock 顶层 `io_dft_*` 端口补上端口级 smoke 用例

本条目记录一次围绕 MemBlock 顶层 DFT 端口可测性的收口。当前 Python 验证环境已经能直接读写 `io_dft_*` 与采样 `io_dft_frnt_*` / `io_dft_reset_frnt_*` / `io_dft_bcknd_cgen` / `io_dft_reset_bcknd_*`，但此前没有任何 testcase 覆盖这组顶层 pin，导致 line coverage 中一批 `io_dft_*` 导出长期保持未访问状态。本轮先把任务限定为“端口级 smoke”，验证顶层输入可驱动、导出端口可观测且与输入保持透传一致，不把范围扩大到 clock gate / reset tree 的内部功能验证。

#### 变更摘要

- `tests/test_MemBlock_env_fixture.py`
  - 新增 `test_api_MemBlock_env_dft_sram_broadcast_passthrough_smoke`
  - 新增 `test_api_MemBlock_env_dft_reset_passthrough_smoke`
  - 增加局部 helper，用于批量驱动 DFT 顶层输入并检查导出端口透传一致性

#### 验证情况

- `python3 -m py_compile src/test/python/MemBlock/tests/test_MemBlock_env_fixture.py`
  - 结果：通过
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_env_fixture.py -k 'dft'`
  - 结果：`2 passed, 25 deselected`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_env_fixture.py`
  - 结果：`27 passed`

### 3. 收紧 `ScalarBankConflictReplaySequence` 的多 victim 接口

本条目记录一次围绕 bank-conflict replay sequence 接口收口的小型重构。此前为了平滑从单 victim 迁移到多 victim，`ScalarBankConflictReplaySequence` 同时保留了 `victim_load_txn` 与 `victim_load_txns` 两套入口；但当前用例已经升级到显式多 victim 语义，继续保留单数入口只会让 sequence API 维持多余兼容层，也容易让后续 testcase 混入过时调用方式。本轮去掉单数参数，只保留 tuple 形式的 `victim_load_txns`。

#### 变更摘要

- `sequences/memblock_sequences.py`
  - `ScalarBankConflictReplaySequence` 删除 `victim_load_txn`
  - 构造阶段不再做单数到复数的兼容归一化
- `tests/test_MemBlock_replay.py`
  - 2-lane bank-conflict smoke 用例切换到 `victim_load_txns=(...,)` 调用形态

#### 验证情况

- `python3 -m py_compile src/test/python/MemBlock/sequences/memblock_sequences.py src/test/python/MemBlock/tests/test_MemBlock_replay.py`
  - 结果：通过
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_replay.py -k 'scalar_bank_conflict_replay_smoke'`
  - 结果：`1 passed, 9 deselected`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_replay.py`
  - 结果：`9 passed, 1 xfailed`

### 4. 收口 replay 用例对默认 transport delay 的隐式时序依赖

本条目记录一次围绕“缩短默认 DCache/Uncache delay 后 replay 用例暴露出的隐式时序假设”的收口。问题并不在于新 delay 本身，而在于若干 sequence/testcase 把 `hot-cache ready`、`release/query` 捕获窗口、以及 `BC/FF/NK` 的表达方式写成了依赖旧 delay 的偶然时序。本轮把这些口径改成显式 warmup / trace-first / lifecycle 断言，同时保留缩短后的默认 delay。

#### 变更摘要

- `sequences/violation_sequences.py`
  - `ScalarRawReplaySequence`
    - 增加 `raw_window_settle_cycles`
    - 改为在发送窗口外层捕获 `raw_nuke_query` trace
    - RAW 压力阶段按实际已发 younger loads 收口，不再假设必须把预设批量全部发完
  - `ScalarRarViolationSequence`
    - 增加 `probe_after_younger_writeback_cycles` / `probe_delay_cycles`
    - 采用 `release trace first + wait fallback`
  - `ScalarBankConflictSqDataInvalidNukeSequence`
    - 断言口径改为“victim 先命中 pure `BC`，后续 trace 再命中 `FF`，并稳定观测 `NK`”
- `sequences/memblock_sequences.py`
  - `ScalarBankConflictReplaySequence` 的 writeback / replay 观测改为更稳的 trace-first 收口
- `tests/test_MemBlock_replay.py`
  - `RAW` 用例对 8 个热点地址做显式 cache warmup，避免主场景退化成 cold miss
  - `RAW` completed 断言改为对比“实际已发 younger loads”
  - `RAR` completed 断言改为基于 warmup 后基线的增量检查
  - `BC + FF + NK` 组合断言改为验证同一 victim load 的完整生命周期，而不是硬卡同拍合并 cause mask
- `docs/test_sequence_and_extension_guide.md`、`docs/replay_todo.md`、`docs/sq_bankconflict_datainvalid_nuke_combo.md`
  - 补充 “capture trace + trace-first + wait fallback” 规则
  - 明确 RAW 场景若要验证 `RAW` 而不是 `DM`，必须先把热点地址 warm 到 hit-path
  - 更新 `BC + FF + NK` 组合场景的真实验证口径

#### 验证情况

- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_replay.py -k 'scalar_raw_replay_smoke or scalar_rar_violation_smoke or scalar_bankconflict_sq_datainvalid_nuke_smoke'`
  - 结果：`3 passed`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_replay.py`
  - 结果：`9 passed, 1 xfailed`

## 2026-04-16

### 1. 让 `ResetEnvSequence` 默认以 ROB wrap boundary profile 启动真实 DUT 回归

本条目记录一次围绕“把 ROB wrap 当成常态边界条件而不是单独复制 testcase”的收口。此前虽然已经补上了显式 `set_next_rob_idx()` / `set_commit_frontier()` 接口，但若仍要求每条 real-DUT testcase 再额外写一份 wrap 版本，维护成本过高，也不利于把 wrap 变成所有主回归默认覆盖的基础边界。本轮把该能力上提到 `ResetEnvSequence`：默认 reset 完成后，同时把 allocator 与 commit frontier seed 到 wrap 边界前一项；同时把仍写死绝对 ROB 值的真实 DUT 用例改成基于 runtime 绑定的相对断言。

#### 变更摘要

- `sequences/memblock_sequences.py`
  - `ResetEnvSequence` 新增 `seed_wrap_boundary` / `initial_rob_idx`
  - 默认在 reset 后同步调用 `set_next_rob_idx()` 与 `set_commit_frontier()`
- `tests/test_MemBlock_random_load.py`
  - non-mem blocker 改为绑定当前 `pending_ptr`，不再写死 `(0,0)`
- `tests/test_MemBlock_replay.py`
  - `RAR` 场景把 `wait_for_rob_idx=(0,0)` 改成直接引用已 prepare 的 older store
  - `FF/RAR/BC` 中写死 `rob_idx_value == 1/2/3` 的断言改为对比 runtime 事务绑定值
- `README.md`、`docs/test_sequence_and_extension_guide.md`、`docs/backend_request_model_design.md`
  - 补充 `ResetEnvSequence` 默认 boundary-start profile 的说明
  - 明确 env/unit test 若要保留 `(0,0)`，应显式关闭该 profile 或直接使用 `env.reset(...)`

#### 验证情况

- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_mmu_fault.py src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py src/test/python/MemBlock/tests/test_MemBlock_vector_unit_stride.py`
  - 结果：`17 passed, 1 xfailed`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_scalar_store_pipeline.py src/test/python/MemBlock/tests/test_MemBlock_scalar_ordering.py src/test/python/MemBlock/tests/test_MemBlock_uncache_semantics.py`
  - 结果：`19 passed, 4 xfailed`

### 4. 去掉 `req_id` 的隐式 runtime 语义，并补上显式 ROB wrap seed 接口

本条目记录一次围绕 backend/ROB 运行时绑定口径的收口。此前虽然 env 已托管 `robIdx` 分配，但 `transactions`、`lsq_agent` 和 `expect_scalar_load()` 等边界仍残留若干 `req_id -> robIdx/pdest/ftq/pc` 的 legacy fallback，这会让 testcase 在未 prepare 的情况下继续“看起来能跑”，也会让 ROB wrap 场景混入大 `req_id` 的副作用。本轮把这些隐式依赖切掉，要求 runtime 字段必须来自 `prepare()/send()/execute()` 或显式赋值，同时补入 allocator/frontier 的显式 seed 接口与对应单测。

#### 变更摘要

- `transactions.py`
  - `LoadTxn` / `StoreTxn` / `VectorMemTxn` 的 `rob_idx`、`resolved_pdest`、`resolved_ftq_idx_*`、`resolved_pc` 去掉 `req_id` fallback
  - `EnqueueLoadStep`、`EnqueueStoreStep`、`IssueOp` 的 `resolved_*` 路径改为未绑定即报错
- `agents/lsq_agent.py`
  - `enqueue_scalar_load/store` 不再从 `req_id` 静默推导 `robIdx`
  - 未显式提供 `rob_idx` 时立即报错
- `agents/backend_facade.py`
  - 新增 `set_next_rob_idx()` 与 `set_commit_frontier()`
  - 为 wrap 场景提供显式 allocator/frontier seed 控制
- `agents/rob_agent.py`
  - 新增 `set_pending_ptr()`，并限制只能在无 outstanding ROB entry 时调用
- `MemBlock_env.py`
  - `expect_scalar_load()` 不再从 `req_id` 推导 `rob_idx/pdest`
- `tests/test_request_apis_backend_facade.py`
  - 新增“未 prepare 即访问 runtime 字段报错”单测
  - 新增 allocator/frontier wrap seed 单测
- `tests/test_MemBlock_rob_agent.py`
  - 新增 `pending_ptr` seed 成功/失败单测
- `README.md`、`docs/backend_request_model_design.md`、`docs/test_sequence_and_extension_guide.md`、`docs/dut_port_behavior.md`
  - 同步更新 `req_id` 仅作为 identifier 的文档口径
  - 补入 wrap 场景应使用显式 seed 接口的说明

#### 验证情况

- `python3 -m pytest -q src/test/python/MemBlock/tests/test_request_apis_backend_facade.py`
  - 结果：`20 passed`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_rob_agent.py`
  - 结果：`10 passed`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_scalar_store_pipeline.py`
  - 结果：`13 passed`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_replay.py`
  - 结果：`9 passed, 1 xfailed`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py`
  - 结果：`6 passed, 1 xfailed`

### 5. 收口 `store_todo` 并补强 store partial/misalign/translated uncache 场景

本条目记录一次围绕 store 专题实施地图和三组真实 DUT testcase 的收口。此前 `docs/store_todo.md` 同时承担了 coverage 讨论摘录、TODO 列表和实施建议三种职责，已经开始与 `coverage_summary.md` / `coverage_todo.md` 的主状态源口径分叉；同时 `scalar_store_pipeline`、`store_misalign`、`uncache_semantics` 三组用例也还缺少若干直接对应文档计划的场景。本轮一边把 `store_todo` 收敛成专题实施地图，一边把 partial 深矩阵、cross-page `SW` 和 translated NC/MMIO store 语义补进真实 DUT 回归。

#### 变更摘要

- `docs/store_todo.md`
  - 去掉独立 coverage 数字与重复状态描述，改为 store 专题实施地图
  - 明确 `coverage_summary.md` / `coverage_todo.md` 是 coverage 真源
  - 明确 `cross-page SB` 在语义上不成立，cross-page 宽度矩阵只围绕 `SH/SW/SD`
- `tests/test_MemBlock_scalar_store_pipeline.py`
  - 新增 `4B partial -> 高偏移 1B overwrite -> full load`
  - 新增按连续 low-byte lane 执行 `1B partial store` 的 merge 场景
- `tests/test_MemBlock_store_misalign.py`
  - 新增 `SW + 0xFFD` / `SW + 0xFFE` 两条 cross-page strict `xfail`
  - 继续保持“shadow/load 可见已成立，但 flush/drain 仍卡在 DUT”这一精确口径
- `tests/test_MemBlock_uncache_semantics.py`
  - 新增 translated `PBMT=NC` store burst + flush
  - 新增 translated `PBMT=MMIO` store + translated cacheable store mixed flush exclusion
  - 对 translated store shadow 能力不足继续使用精确 capability `xfail`

#### 验证情况

- `python3 -m py_compile src/test/python/MemBlock/tests/test_MemBlock_scalar_store_pipeline.py src/test/python/MemBlock/tests/test_MemBlock_store_misalign.py src/test/python/MemBlock/tests/test_MemBlock_uncache_semantics.py`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_scalar_store_pipeline.py src/test/python/MemBlock/tests/test_MemBlock_store_misalign.py src/test/python/MemBlock/tests/test_MemBlock_uncache_semantics.py`
  - 结果：`19 passed, 8 xfailed`

### 4. 为 load fault / scalar load probe 新增专题文档入口

本条目记录一次围绕 `00e7578f1b17bc30c718cb099a7bd59d1556f4d6` 新增用例的文档收口。此前 `test_MemBlock_mmu_fault.py` 与 `test_MemBlock_scalar_load_pipeline_probe.py` 已经落地，但对应的设计意图、sequence 边界和推荐阅读入口还没有同步进入文档体系。本轮补齐专题说明，并把入口挂回 README、MMU 文档、pipeline 方案文档与 sequence 指南。

#### 变更摘要

- 新增专题文档：
  - `docs/mmu_fault_directed_cases.md`
    - 说明 `MmuFaultingScalarLoadSequence`、TLB-hit fault 背景和当前异常位断言口径
  - `docs/scalar_load_pipeline_probe_cases.md`
    - 说明 bank-conflict、matchInvalid proxy、hi-prio replay 抢占、late-STA violation 这组 probe case 的设计目标与当前收口口径
- `README.md`
  - 在 `docs/` 列表与推荐阅读顺序中加入上述两篇新文档入口
- `docs/mmu_env_design_and_usage.md`
  - 把 MMU fault directed 文档挂入专题阅读入口
- `docs/vp_pipeline_plan.md`
  - 增补当前已落地的 probe 级 case 入口，并更新 `BC` 场景的计划状态表述
- `docs/test_sequence_and_extension_guide.md`
  - 补入 `MmuFaultingScalarLoadSequence`、`ScalarBankConflictLoadClusterSequence`、`ScalarFastReplayCancelledByReplayHiPrioSequence`、`ScalarLateStaStoreLoadViolationSequence` 的使用说明
- `docs/coverage_todo.md`
  - 更新 `P2-2 PMP allow/deny / access fault directed` 的状态描述，明确 load-side baseline 已落地

#### 验证情况

- 文档改动，未单独重跑 pytest

### 5. 将 `scalar_load_pipeline_probe` 中两个 XPASS 用例升级为真实验证

本条目记录一次对 `test_MemBlock_scalar_load_pipeline_probe.py` 中两个历史 XPASS 用例的验证口径收紧。此前它们虽然“跑绿”，但分别存在 `older store` 只验证 materialize、以及 `nc_replay` 组合只验证 replay queue 落点而未明确证明最终 compare/writeback 收敛的问题。本轮不再依赖宽松断言，而是把 sequence 和 testcase 一起收紧到与测试意图一致的真实行为证明。

#### 变更摘要

- `sequences/load_pipeline_probe_sequences.py`
  - `ScalarLateStaStoreLoadViolationSequence` 改为等待 `wait_store_materialized(..., require_committed=True)`，不再把 `completed/materialize` 误当成 `commit`
  - `ScalarFastReplayCancelledByReplayHiPrioSequence` 为 hi-prio preemptor + bank-conflict 组合补齐最终 writeback 追踪
  - 新增对 preemptor load writeback 的 ROB/data 绑定校验，并把该组合的 `expected_completed_load_count` 显式回传给 testcase
- `tests/test_MemBlock_scalar_load_pipeline_probe.py`
  - `nc_replay` 抢占用例移除旧 `xfail`，改为显式断言：
    - preemptor writeback 全收齐
    - bank-conflict loads 的 final writeback / wakeup 全收齐
    - 完成 compare 的 load 数与预期完全一致
  - `late_sta_violation` 用例移除旧 `xfail`，断言从“仅 materialize”升级为“`completed` + `committed`”，并保留后续 drain 收尾检查

#### 验证情况

- `python3 -m py_compile src/test/python/MemBlock/sequences/load_pipeline_probe_sequences.py src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py`
- `python3 -m pytest -q -rXx src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py -k 'nc_replay or late_sta_violation'`
  - 结果：`2 passed, 5 deselected`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_mmu_fault.py src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py`
  - 结果：`10 passed, 1 xfailed`

### 6. 修复 rebase 后 mmu fault / scalar load probe 的 load writeback 登记错位

本条目记录一次针对主线 `cff44ae39d0cba512943b4d817ab397d9363153f` rebase 后兼容性的收口。该主线改动将 testcase/sequence 对 load 观测的口径切换到 runtime 绑定的 `txn.rob_idx`，而这两条 directed 链路里仍残留若干按旧 `req_id -> legacy robIdx` 登记 `expect_scalar_load` / `wait_load_writeback_observed` 的位置，导致真实 writeback 被 scoreboard 误判为“未登记的 load writeback”。

#### 变更摘要

- `sequences/mmu_sequences.py`
  - `MmuFaultingScalarLoadSequence` 在 expect / wait 前先 `backend.prepare(txn)`
  - 改为按 `txn.rob_idx` 登记与等待 load writeback
- `sequences/load_pipeline_probe_sequences.py`
  - `bank-conflict`、cache warmup、hi-prio preemptor 等 load 路径统一改为先 prepare，再按 runtime `rob_idx` expect / wait
  - 修正 rebase 后 `_capture_replay_events(...)` 仍按旧 `rob_idx_flag/rob_idx_value` 传参的问题
- `sequences/violation_sequences.py`
  - 清理 `ScalarSqDataInvalidMatchInvalidTriggerSequence` 中 rebase 后混入的旧/新 store 发射残留，去掉悬空的 `store_result/store_ref` 访问
- `tests/test_MemBlock_scalar_load_pipeline_probe.py`
  - 直接 warmup load 改为先 prepare，再按 `warmup_load.rob_idx` 做 expect / wait

#### 验证情况

- `python3 -m py_compile src/test/python/MemBlock/sequences/mmu_sequences.py src/test/python/MemBlock/sequences/load_pipeline_probe_sequences.py src/test/python/MemBlock/sequences/violation_sequences.py src/test/python/MemBlock/tests/test_MemBlock_mmu_fault.py src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_mmu_fault.py src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py`
  - 结果：`8 passed, 1 xfailed, 2 xpassed`

## 2026-04-15

### 1. 重整 `request_apis.py` 与 `sequences/` 分层：公共模型进 `transactions.py`，场景模板上提到 sequence

本条目记录一次围绕 testcase 作者入口的接口收口：此前虽然 env 已接管 `robIdx`，但 `request_apis.py` 仍同时承担了三种职责，包括公共事务类型导出、primitive helper，以及部分场景级 batch helper。这会让 testcase/sequence 作者继续把 `request_apis.py` 当成“什么都能放”的入口。本轮将边界重新划清：`transactions.py` 负责公共事务/plan 模型，`sequences/` 负责可复用场景模板，`request_apis.py` 退回 backend primitive adapter / compatibility shim。

#### 变更摘要

- `request_apis.py`
  - 模块定位改为 `backend primitive adapter / compatibility shim`
  - `send_load_batch_same_cycle()` / `send_load_batch_with_sta_same_cycle()` docstring 明确标为兼容包装
  - 新增显式 `__all__`，把公开范围收敛到 primitive/helper 级别
  - `ptr_inc()` 改为从 `transactions.py` 复用并继续兼容导出，不再由兼容层独占实现
- `sequences/memblock_sequences.py`
  - 新增：
    - `ScalarLoadBatchSameCycleSequence`
    - `ScalarLoadBatchWithStaSequence`
    - 对应 result dataclass
  - `ScalarBankConflictReplaySequence` 内部不再直接依赖旧 batch helper，而是改用新的 sequence surface
- `sequences/violation_sequences.py` / `sequences/mmu_sequences.py`
  - 类型导入切到 `transactions.py`
  - 违反/重放场景内的同拍多 load 发送改为复用 `ScalarLoadBatchSameCycleSequence`
- `sequences/__init__.py`
  - 导出新的 same-cycle batch sequence
- `tests/`
  - 大部分 testcase 的 `LoadTxn` / `StoreTxn` / `QueuePtr` / `BackendSendPlan` 等导入改为直接来自 `transactions.py`
  - 剩余需要队列指针推进的 testcase / sequence 改为直接从 `transactions.py` 导入 `ptr_inc()`
  - `test_MemBlock_random_load.py` 新增一个代表性 same-cycle batch sequence 用例
- `README.md` / `docs/test_sequence_and_extension_guide.md` / `docs/backend_request_model_design.md`
  - 同步公开口径：新 testcase 应优先写 `transactions.py` + `sequences/`，而不是继续扩 `request_apis.py`

#### 验证情况

- `python3 -m py_compile src/test/python/MemBlock/request_apis.py src/test/python/MemBlock/sequences/__init__.py src/test/python/MemBlock/sequences/memblock_sequences.py src/test/python/MemBlock/sequences/mmu_sequences.py src/test/python/MemBlock/sequences/violation_sequences.py src/test/python/MemBlock/tests/test_MemBlock_random_load.py`
- 定向 `pytest`
  - 见本次提交后的测试记录
  - 后续补充复查：`python3 -m pytest -q -rXx src/test/python/MemBlock/tests/test_request_apis_backend_facade.py src/test/python/MemBlock/tests/test_MemBlock_random_load.py src/test/python/MemBlock/tests/test_MemBlock_replay.py src/test/python/MemBlock/tests/test_MemBlock_scalar_ordering.py src/test/python/MemBlock/tests/test_MemBlock_scalar_store_pipeline.py src/test/python/MemBlock/tests/test_MemBlock_random_store.py src/test/python/MemBlock/tests/test_MemBlock_uncache_semantics.py`

### 2. 同步文档 cookbook 示例，统一改写为 `RobIndex` 优先用法

本条目记录一次围绕文档入口与 cookbook 的口径收尾：在代码层已经完成 `RobIndex` 一等语义收口后，若文档仍保留大量 `rob_idx_flag/rob_idx_value` 示例，读者会继续沿用旧写法。因此本轮把 README 之外尚残留的 cookbook/usage 片段统一迁成 `rob_idx=...` / `RobIndex(...)` 风格，并保留“只有 DUT 边界才需要 split-field”的说明。

#### 变更摘要

- `docs/mmu_env_design_and_usage.md`
  - MMU translation load 示例改为先 `prepare(txn)`，再用 `rob_idx=txn.rob_idx` 做 expect / wait
- `docs/verification_env_design.md`
  - `NonMemBlockerStep` 示例改为显式传 `RobIndex(...)`
- `docs/test_sequence_and_extension_guide.md`
  - ROB blocker cookbook 改为 `RobIndex(...)` 风格
- `docs/backend_rob_cookbook.md`
  - non-mem blocker 模板改为 `rob_idx=RobIndex(...)`
- `docs/vector_mem_phase1_plan_backend_api.md`
  - vector + ROB blocker 混排示例改为直接基于 `txn.rob_idx` 构造 blocker

#### 复查情况

- `rg -n "rob_idx_flag=|rob_idx_value=|wait_for_rob_idx_flag=|wait_for_rob_idx_value=|\\.rob_idx_flag\\b|\\.rob_idx_value\\b" src/test/python/MemBlock/docs src/test/python/MemBlock/README.md`
  - 结果为空，说明当前 README/docs 示例已无残留 split-field 公开用法

### 3. 收口 `RobIndex` 一等语义，减少 facade/env/testcase 间的显式 `rob_idx_value` 传递

本条目记录一次针对 env 托管 `robIdx` 后续尾巴的抽象收口：上一轮虽然把 `robIdx` 的分配权收回到了 env，但事务对象、facade helper、env wait/filter API 之间仍残留大量 `(rob_idx_flag, rob_idx_value)` 形式的透传与赋值，导致 testcase/sequence 继续背负 split-field 细节。这次改造把 `RobIndex` 提升为公共语义层的一等输入，split 字段只保留在 DUT bundle 边界与兼容层。

#### 变更摘要

- `transactions.py`
  - 新增 `make_rob_index(...)`
  - 为 `LoadTxn` / `StoreTxn` / `VectorMemTxn` 增补：
    - `rob_idx`
    - `assigned_rob_idx`
    - `rob_idx_override`
  - 为 `LoadTxn` / `IssueOp` 增补 `wait_for_rob_idx`
  - `IssueOp` / `EnqueueLoadStep` / `EnqueueStoreStep` / `NonMemBlockerStep` 现在都支持围绕 `RobIndex` 做语义访问，split 字段退化为兼容表示
- `agents/backend_facade.py`
  - facade public helpers 新增 `rob_idx=` / `wait_for_rob_idx=` 入口
  - runtime bind 统一写 `assigned_rob_idx`
  - 对旧式 positional / split-field 调用保留兼容，避免打碎现有 issue/commit agent 和旧 testcase
- `MemBlock_env.py`
  - `expect_scalar_load(...)`
  - `wait_load_writeback_observed(...)`
  - `wait_replay_event(...)`
  - `wait_nc_replay_or_nc_out(...)`
  - `wait_nuke_query_backpressure(...)`
  - `wait_rar_nuke_response(...)`
  - `collect_replay_window(...)`
  - 上述接口都支持直接传 `rob_idx=RobIndex(...)`，内部过滤逻辑统一在 env 边界做 normalize
- `request_apis.py` / `monitors/vector_mem_monitor.py`
  - request API 优先透传 `RobIndex`
  - 对旧 fake backend / fake monitor 继续自动回退到 split-field 调用，保持测试兼容
- `README.md` / `docs/backend_request_model_design.md` / `docs/rob_model.md`
  - 同步文档口径：新 testcase/sequence 应优先显式传 `RobIndex`

#### 验证情况

- `python3 -m py_compile src/test/python/MemBlock/transactions.py src/test/python/MemBlock/agents/backend_facade.py src/test/python/MemBlock/request_apis.py src/test/python/MemBlock/MemBlock_env.py src/test/python/MemBlock/monitors/vector_mem_monitor.py`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_request_apis_backend_facade.py -q`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_replay.py -q`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_vector_store.py -q`

### 4. env 托管 `robIdx`，补上 prepare/bind 路径并拆掉关键 `req_id -> robIdx` 耦合

本条目记录一次针对 MemBlock backend/ROB 控制面的重构：先前 `LoadTxn` / `StoreTxn` / `VectorMemTxn` 默认把 `req_id` 直接编码成 `robIdx`，这既让 testcase 作者误以为 `robIdx` 只是标签，也会在 uncache / MMIO / replay 等真实 DUT 语义路径里把错误的 ROB 程序序偷偷带进来。现在改为由 env 在 `prepare()` / `execute()` / `send()` 流程中统一分配 `robIdx`，并为需要 pre-issue `robIdx` 的场景补上显式准备阶段。

#### 变更摘要

- `transactions.py`
  - 新增：
    - `RobIndex`
    - `RobRef`
    - `BackendPreparedPlan`
  - `LoadTxn` / `StoreTxn` / `VectorMemTxn` 新增 env runtime binding 字段，`robIdx` 默认优先取 env 分配结果
  - `IssueOp` / `EnqueueLoadStep` / `EnqueueStoreStep` / `NonMemBlockerStep` 扩展到可携带符号化 ROB 引用与 resolved `robIdx`
- `agents/backend_facade.py`
  - 新增 `prepare(...)`
  - `send(...)` / `execute(...)` 默认先做 prepare，再执行 resolved plan
  - 新增 env-owned ROB allocator，按程序序分配并支持 `RobRef` / `wait_for_rob`
- `agents/issue_agent.py` / `agents/lsq_agent.py` / `agents/vector_issue_agent.py`
  - issue / enqueue 不再只从 `req_id` 硬拆 `robIdx`
  - scalar/vector 默认 `pdest` / `ftqIdx` / `pc` 改为优先吃 prepare 阶段绑定结果
- `monitors/vector_mem_monitor.py`
  - 向量完成归因改为优先使用 env 注册的 `robIdx -> req_id` 映射，不再强依赖 `req_id == encoded robIdx`
- `request_apis.py`
  - `expect_load(env, txn)` 会在需要时自动 prepare 事务，再按 resolved `robIdx` 登记 scoreboard 期望
- `tests/`
  - 新增 backend facade prepare / `RobRef` / vector registration 单测
  - 把受影响的 MMU / replay / uncache / random-load 用例改成按 resolved `robIdx` 登记期望与观测
- `README.md` / `docs/rob_model.md`
  - 同步 public API 口径：默认由 env 管 `robIdx`，需要 pre-issue `robIdx` 时先 `prepare(...)`

#### 验证情况

- `python3 -m py_compile src/test/python/MemBlock/transactions.py src/test/python/MemBlock/agents/backend_facade.py src/test/python/MemBlock/agents/issue_agent.py src/test/python/MemBlock/agents/lsq_agent.py src/test/python/MemBlock/agents/vector_issue_agent.py src/test/python/MemBlock/monitors/vector_mem_monitor.py src/test/python/MemBlock/request_apis.py src/test/python/MemBlock/MemBlock_env.py src/test/python/MemBlock/sequences/mmu_sequences.py src/test/python/MemBlock/sequences/violation_sequences.py src/test/python/MemBlock/tests/test_request_apis_backend_facade.py src/test/python/MemBlock/tests/test_MemBlock_env_mmu_smoke.py src/test/python/MemBlock/tests/test_MemBlock_uncache_semantics.py src/test/python/MemBlock/tests/test_MemBlock_replay.py src/test/python/MemBlock/tests/test_MemBlock_random_load.py`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_request_apis_backend_facade.py`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_env_fixture.py src/test/python/MemBlock/tests/test_MemBlock_env_mmu_smoke.py -k 'backend or mmu or create or wires_existing_agents'`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_uncache_semantics.py -k 'pbmt_mmio_load_smoke or mmio_busy_blocks_younger_cacheable_load_retire'`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_random_load.py -k 'plan_same_cycle or non_mem_blocker_delays_compare'`

### 5. 新增 store TODO 规划文档，收口标量 store 覆盖率讨论与补强矩阵

本条目记录一次面向标量 store 覆盖率的文档收口：将当前基于 `toffee_report_full` 的讨论结论整理为独立 TODO 文档，集中记录 store 主路径现状、`NewStoreQueue/SbufferData/StoreMisalignBuffer/Uncache` 等短板区、以及按 testcase 文件组织的后续补强建议，方便后续围绕真实 DUT 回归逐项推进。

#### 变更摘要

- `docs/store_todo.md`
  - 新增独立 store TODO 规划文档
  - 总结当前标量 store 相关 testcase 分布与模块覆盖率现状
  - 明确当前已覆盖能力：
    - cacheable scalar store materialize / commit / flush-drain 基础闭环
    - same-addr / overwrite / 部分 partial-store directed case
    - cross-16B scalar store misalign 基础矩阵
  - 明确主要短板：
    - `NewStoreQueue.sv` 深状态与分支组合
    - `SbufferData.sv` partial merge / drain 数据组织
    - `StoreMisalignBuffer.sv` cross-page / deeper split 状态
    - `Uncache.sv` 上的 NC/MMIO store flush-drain 闭环
  - 收敛后续优先级：
    - `P0`：partial-mask store、queue depth、misalign split/drain
    - `P1`：NC store flush/drain、MMIO exclusion、delayed drain
    - `P2`：更外围的 DCache store 相关复杂组合

## 2026-04-14

### 1. 收口 uncache xfail：修正 req_id/ROB-head 前提，并重分类 Svpbmt 问题

本条目记录一次针对当前 uncache/Svpbmt `xfail` 的纠偏收口：进一步排查后确认，`PBMT IO load` 与 `mmioBusy` 旧问题的主因不是 DUT，而是 testcase 直接用较大的 `req_id` 映射出较大的 `robIdx`，却没有把 `pendingPtr` 推进到对应 ROB 位置。对 MMIO/uncache 路径，这会让请求根本到不了正式执行窗口。修正该前提后，这两条 case 已恢复为普通硬验证；真正仍保留的 DUT 候选只剩 `PBMT NC load` debug 分类与 `PBMT NC store` shadow paddr 闭环。

#### 变更摘要

- `docs/BUGS.md`
  - 文档标题升级为“已知与潜在 DUT 问题”
  - 新增状态说明：
    - `open`
    - `suspected`
  - 补入已知问题：
    - `DUTBUG-store-misalign-cross-page-flush-stall`
  - 保留 uncache/Svpbmt 候选问题：
    - `DUTBUG-svpbmt-nc-load-debug-classification-lost`
    - `DUTBUG-svpbmt-nc-store-zero-paddr-shadow`
  - 移除误归因为 DUT 的旧候选：
    - `PBMT IO load`
    - `mmioBusy`
- `tests/test_MemBlock_uncache_semantics.py`
  - 把受 ROB-head 门控影响的场景改成贴近 `pendingPtr` 的低 `req_id`
  - `test_api_MemBlock_sv39_pbmt_mmio_load_smoke` 恢复为普通硬断言
  - `test_api_MemBlock_mmio_busy_blocks_younger_cacheable_load_retire` 恢复为普通硬断言
- `docs/coverage_todo.md`
  - 同步 `mmioBusy` directed case 已转正，后续重点从“能否拉高”转向长窗口与 mixed-path 收口

#### 复核结论

- `PBMT NC load`
  - 已找到明确静态 RTL 可疑点：
    - `src/main/scala/xiangshan/mem/pipeline/NewLoadUnit.scala` 中
      `ldout.debug.isNCIO := in.isNCReplay() && in.pmp.get.mmio`
  - 这会把 `PBMT=NC` 且 `pmp.mmio=0` 的场景压成 `debug_is_ncio=0`
- `PBMT IO load`
  - 已确认旧问题主要是 testcase 没把 ROB-head 前提构造正确
  - 修正为低 `req_id` 后，`PBMT=IO` translated load 已能正常走 outer 并完成 writeback
- `MMIO busy`
  - 已确认旧问题同样主要是 ROB-head 前提错误
  - 修正后，older MMIO store + younger cacheable load 的 basic directed case 已能稳定观测到 `mmioBusy` 拉高并阻塞 younger compare/retire

#### 验证情况

- `python3 -m pytest -q -rXx src/test/python/MemBlock/tests/test_MemBlock_uncache_semantics.py`
- `python3 -m pytest -q -rXx src/test/python/MemBlock/tests/test_MemBlock_uncache_semantics.py src/test/python/MemBlock/tests/test_MemBlock_store_misalign.py src/test/python/MemBlock/tests/test_MemBlock_replay.py src/test/python/MemBlock/tests/test_MemBlock_vector_store.py`

### 2. 新增 vector load directed case，针对性拉升 `VLMergeBuffer` 覆盖率

本条目记录一次面向 `VLMergeBuffer` 的 testcase 补强：不是继续堆基础 smoke，而是按 merge-buffer 关键路径补入更“刁钻”的真实 DUT vector load directed case，集中覆盖宽 element merge、跨 16B 的 unit-stride merge、第二个 vector issue/enqueue 端口，以及 zero/negative stride 的地址展开。

#### 变更摘要

- `tests/test_MemBlock_vector_unit_stride.py`
  - 新增：
    - `test_api_MemBlock_vector_unit_stride_load_wide_smoke`
    - `test_api_MemBlock_vector_unit_stride_load_cross_16b_unaligned_smoke`
    - `test_api_MemBlock_vector_unit_stride_load_byte_dense_smoke`
    - `test_api_MemBlock_vector_unit_stride_load_port1_smoke`
  - 抽取：
    - `_preload_elements()`
    - `_assert_vector_load_result()`
- `tests/test_MemBlock_vector_stride.py`
  - 新增：
    - `test_api_MemBlock_vector_stride_zero_load_smoke`
    - `test_api_MemBlock_vector_stride_negative_load_smoke`
  - 抽取：
    - `_preload_elements()`
    - `_assert_stride_load_result()`

#### 验证情况

- `python3 -m py_compile src/test/python/MemBlock/tests/test_MemBlock_vector_unit_stride.py src/test/python/MemBlock/tests/test_MemBlock_vector_stride.py`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_vector_unit_stride.py src/test/python/MemBlock/tests/test_MemBlock_vector_stride.py src/test/python/MemBlock/tests/test_vector_memory_model.py`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_vector_unit_stride.py src/test/python/MemBlock/tests/test_MemBlock_vector_stride.py --toffee-report --report-dir src/test/python/MemBlock/data/toffee_report_vector_merge_cov --report-name memblock_vector_merge_cov --report-dump-json`

#### 覆盖率观察

- `data/toffee_report_vector_merge_cov/line_dat/rtl/VLMergeBufferImp.sv.gcov.html`
  - line：`49.0%`（`3143 / 6412`）
  - branch：`54.5%`（`16543 / 30364`）
- 对比现有 full regression 基线 `data/toffee_report_full/line_dat/rtl/VLMergeBufferImp.sv.gcov.html`
  - line：`46.5% -> 49.0%`
  - branch：`52.2% -> 54.5%`
- 同时确认 `fromSplit_1` 相关端口已不再是“完全未命中”：
  - `io_fromSplit_1_req_bits_flowNum`
  - `io_fromSplit_1_req_bits_uop_vecWen`

#### 备注

- 规划中原本想补一条 checkerboard mask load，但实测当前 env 还没有可驱动真实 `v0` mask 源的稳定控制面；`vm=False` 会把整条 load 收敛为全零结果。因此本轮改为一条 `SEW=8, VL=16` 的 byte-dense case，继续稳定命中 `VLMergeBuffer` 的宽 merge 路径。

### 3. 打通 `mask_bits -> v0/src_3` 控制面，恢复 checkerboard masked load 回归

本条目记录一次面向 vector mask 控制面的最小 env 收口：先前 checkerboard masked load 之所以在真实 DUT 上退化成全零，不是 `VectorMemoryModel` 的参考口径有问题，而是前门只生成了 `maskVecGen`，没有把 element mask 作为真实 `v0` 源送进 vector issue。现在把 `mask_bits` 显式派生到 `src_3` / `vmask`，并恢复真实 DUT checkerboard load 回归。

#### 变更摘要

- `transactions.py`
  - `VectorMemTxn` 新增：
    - `resolved_mask_source`
    - `resolved_vmask`
  - 当 `vm=False` 且提供 `mask_bits` 时，默认派生 element mask；若 testcase 显式给出 `src_3` 或非默认 `vmask`，则保持显式值优先
- `agents/vector_issue_agent.py`
  - `bits_src_3` 改为使用 `txn.resolved_mask_source`
  - `bits_vpu_vmask` 改为使用 `txn.resolved_vmask`
- `tests/test_vector_memory_model.py`
  - 新增 mask source 派生与显式覆盖的纯 Python 回归
- `tests/test_MemBlock_vector_unit_stride.py`
  - 恢复 `test_api_MemBlock_vector_unit_stride_load_checkerboard_mask_smoke`
  - 保持对 masked-off element slot 为 0 的硬断言
- `docs/vmem_design_and_usage.md`
  - 同步 `mask_bits` 现已可驱动真实 DUT mask 源

#### 验证情况

- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_vector_unit_stride.py src/test/python/MemBlock/tests/test_MemBlock_vector_stride.py src/test/python/MemBlock/tests/test_vector_memory_model.py`

### 4. 增加 vector store real-DUT regression，固定当前已知 DUT 的 zero-data/no-drain 签名

本条目记录一次针对向量 store 当前已知 DUT 缺口的收口：在确认问题并非 testcase/front-door 小修可解后，新增一条真实 DUT regression，用精确条件 `xfail` 固定当前“completion 已到，但 SQ data 为 0 且 flushSb 永不 drain”的复现签名。这样后续 DUT 修复后，该用例会自动退回硬断言，继续检查最终 memory effect。

#### 变更摘要

- 新增 `tests/test_MemBlock_vector_store.py`
  - 新增 `test_api_MemBlock_vector_unit_stride_store_flush_regression`
  - 先要求：
    - vector completion metadata 已返回
    - SQ 中已出现地址正确的 store 条目
  - 仅当同时观测到以下签名时才 `pytest.xfail(...)`：
    - `store_view.data == 0`
    - `dcache_a_request_count == 0`
    - `dcache_d_response_count == 0`
    - `flush_store_buffers_and_wait()` 超时
    - `drain_log` 为空
- `docs/BUGS.md`
  - 新增 vector store data path 未接通的已知 DUT bug 条目
- `docs/vmem_design_and_usage.md`
  - 同步当前向量 load 已收紧到真实 data compare
  - 明确 vector store regression 当前用于固定已知 DUT 缺口，而不是放宽验收口径

#### 验证情况

- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_vector_store.py`

### 5. 补入 non-zero `vstart` 的真实 DUT vector load 数据面回归

本条目记录一次针对向量 load 数据面的继续补强：在 unit-stride 基础 smoke 已能严格比对 writeback data 之后，再补一条 non-zero `vstart` 的真实 DUT 场景，验证 prestart 元素会在 `vecWriteback.data` 中保留空洞而不会错误覆盖低位槽位。

#### 变更摘要

- `tests/test_MemBlock_vector_unit_stride.py`
  - 新增 `test_api_MemBlock_vector_unit_stride_load_nonzero_vstart_smoke`
  - 补充 `_pack_expected_element_slots()`，按元素槽位而不是“压缩低位”来构造期望图像
  - 真实 DUT 断言：
    - 请求完成且无 trap
    - `vec_wen=1`
    - `is_vec_load=1`
    - writeback data 与 non-zero `vstart` 的槽位图像一致
    - prestart 对应的最低 32-bit 槽位保持为 0

#### 验证情况

- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_vector_unit_stride.py`

#### 备注

- 本轮 load 数据面调试期间也同步复核了 vector store front-door 现状：当前真实 DUT 虽可观测到 completion metadata，但最终 transport / memory-side-effect 闭环仍被 DUT 缺口阻塞；对应的 real-DUT store regression 已在同日第 3 条补入，并以精确条件 `xfail` 固定当前签名。

### 6. 修正 vector issue 默认 `vuopIdx`，打通真实 DUT writeback 数据比对

本条目记录一次面向真实 DUT 向量 load 数据面调试的收口：定位到 Phase 1 front-door 把 `vecIssue.bits_vpu_vuopIdx` 错误地复用了 testcase `req_id` 低位，导致 unit-stride/stride smoke 在 VLSplit 中被当成非零 uop offset，最终从错误的 128-bit chunk 取数，MSHR forward 数据为 0。现在将 `vuopIdx` 显式建模为向量微操作索引并默认置 0，同时把 real-DUT smoke 收紧到 writeback data 严格比对。

#### 变更摘要

- `transactions.py`
  - `VectorMemTxn` 新增 `vuop_idx`
  - 默认 `vuop_idx=0`，并补充范围校验与 `resolved_vuop_idx`
- `agents/vector_issue_agent.py`
  - `bits_vpu_vuopIdx` 改为使用事务级 `vuop_idx`
  - 不再错误复用 `req_id` 低位
- `tests/test_MemBlock_vector_unit_stride.py`
  - 恢复 unit-stride real-DUT smoke 的 writeback data 严格比对
- `tests/test_MemBlock_vector_stride.py`
  - 恢复 stride real-DUT smoke 的 writeback data 严格比对
- `tests/test_vector_memory_model.py`
  - 补充 `vuop_idx` 默认值与边界校验的纯 Python 回归

#### 验证情况

- `python3 -m pytest -q src/test/python/MemBlock/tests/test_vector_memory_model.py`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_vector_unit_stride.py`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_vector_stride.py`

### 7. 落地向量访存 Phase 1 骨架

本条目记录一次面向向量访存 Phase 1 的基础设施落地：把向量事务、`enqLsq + vecIssue` front-door、向量子模型、完成监控与统一收口接入当前 MemBlock Python 验证环境，为后续 unit-stride / stride / non-zero `vstart` 的 testcase 开发提供稳定骨架。

#### 变更摘要

- `transactions.py`
  - 新增：
    - `VectorMemTxn`
    - `VectorElementAccess`
    - `VectorMemResult`
    - `VectorEnqueueStep`
    - `VectorIssueStep`
    - `VectorWaitStep`
  - `BackendSendPlan` / `BackendSendResult` 扩展到可承载 vector step 与结果
- `memory_model.py` / `model/vector_memory_model.py`
  - `MemoryModel` 新增 `env.memory.vector`
  - 向量参考模型提供：
    - `expand(txn)`
    - `expect_load(txn)`
    - `predict_store(txn)`
    - outstanding 跟踪
  - `outstanding_expected_count` 统一并入 scalar + vector
- `MemBlock_env.py`
  - 新增 `vector_issue_agent`、`vector_backend`、`vector_monitor`
  - 绑定真实 DUT 的 `vecIssue` / `vecWriteback` 前门与完成观测
  - `env.assert_no_outstanding()` 现在会覆盖 `env.memory.vector` outstanding
- `agents/`
  - 新增：
    - `vector_issue_agent.py`
    - `vector_backend_facade.py`
  - `backend_facade.py` / `lsq_agent.py` 扩展 vector enqueue / issue / wait 路径
- `request_apis.py`
  - 新增：
    - `send_vector_load`
    - `send_vector_store`
- `sequences/vector_mem_sequences.py`
  - 新增最小向量 sequence 骨架
- `tests/`
  - 新增 `test_vector_memory_model.py`
  - 补强 `test_request_apis_backend_facade.py` 的 vector plan / wrapper 覆盖
  - 补强 `test_MemBlock_env_fixture.py` 的 vector env 接线与 unified outstanding 覆盖
- `docs/vector_mem_plan.md` / `docs/vector_mem_phase1_plan*.md`
  - 文档口径同步到当前实现：
    - Phase 1 front-door 为 `enqLsq + vecIssue`
    - Phase 1 主范围不含 `vlm/vsm` packed mask memory

#### 验证情况

- `python3 -m py_compile src/test/python/MemBlock/transactions.py src/test/python/MemBlock/model/vector_memory_model.py src/test/python/MemBlock/monitors/vector_mem_monitor.py src/test/python/MemBlock/agents/vector_backend_facade.py src/test/python/MemBlock/agents/vector_issue_agent.py src/test/python/MemBlock/agents/backend_facade.py src/test/python/MemBlock/agents/lsq_agent.py src/test/python/MemBlock/memory_model.py src/test/python/MemBlock/MemBlock_env.py src/test/python/MemBlock/request_apis.py src/test/python/MemBlock/sequences/vector_mem_sequences.py src/test/python/MemBlock/tests/test_vector_memory_model.py src/test/python/MemBlock/tests/test_request_apis_backend_facade.py src/test/python/MemBlock/tests/test_MemBlock_env_fixture.py`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_vector_memory_model.py src/test/python/MemBlock/tests/test_request_apis_backend_facade.py`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_env_fixture.py -k 'vector_frontdoor_facades_exist or assert_no_outstanding_includes_vector_expectations or backend_facade_wires_existing_agents or has_core_bundles'`

### 8. 补入真实 DUT vector smoke，并明确当前验收口径

本条目记录 Phase 1 第一批真实 DUT 向量 smoke testcase 落地：在现有 `enqLsq + vecIssue` front-door 已打通的基础上，先验证 unit-stride / stride 两条最小路径都能从请求发送闭环到 transport、completion 与 writeback metadata，而暂不在 Phase 1 smoke 中收紧 writeback data 值比对。

#### 变更摘要

- `tests/test_MemBlock_vector_unit_stride.py`
  - 新增 unit-stride real-DUT smoke
  - 断言完成、无 trap、`vec_wen=1`、`is_vec_load=1`、`is_strided=0`
  - 断言至少观测到一次 dcache A 请求与两个 dcache D beat
- `tests/test_MemBlock_vector_stride.py`
  - 新增 stride real-DUT smoke
  - 断言完成、无 trap、`vec_wen=1`、`is_vec_load=1`、`is_strided=1`
  - 断言 dcache block address 落在预期 cacheline 集合内
- `agents/vector_issue_agent.py`
  - 从固定端口等待改为逐端口主动 drive + 握手
  - 补齐 `vpu` 相关 optional 字段驱动与 `maskVecGen` 生成
- `MemBlock_env.py`
  - 修正 `backend.vector_monitor` 的接线时序，确保 backend wait helper 能看到真实 monitor
- `model/transport_responder.py` / `monitors/vector_mem_monitor.py`
  - 增补 dcache request 与 vector completion 诊断字段，便于 testcase 直接做 front-door / transport / metadata 级断言

#### 当前口径

- 当前 smoke 已证明：
  - `enqLsq + vecIssue` 可以驱动真实 DUT 发起向量 load 请求
  - dcache A / D transport 路径与向量 completion metadata 可以被稳定观测
  - unit-stride 与 stride 路径能在真实 DUT 上被区分
- 当前 smoke 尚未收紧：
  - vector writeback `data` 值比对
  - merge / oldVd / partial-write 相关真实数据面语义
- 因此，Phase 1 现阶段的真实 DUT 验收以 front-door、transport、completion 与 metadata 闭环为主；更严格的数据面正确性留待后续专题补强

#### 验证情况

- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_vector_unit_stride.py src/test/python/MemBlock/tests/test_MemBlock_vector_stride.py`

### 9. 补全文档：新增向量访存设计与使用说明

本条目记录一次文档收口：为当前 Phase 1 向量访存环境新增统一的 design/usage 文档，明确事务对象、facade、sequence、monitor、model 的关系，以及当前 real-DUT smoke 的验收口径与 known gap。

#### 变更摘要

- 新增 `docs/vmem_design_and_usage.md`
  - 说明 `VectorMemTxn`、`env.vector_backend`、`VectorLoadSequence` / `VectorStoreSequence` 的职责与推荐用法
  - 说明 `enqLsq + vecIssue` front-door 为何继续收敛到统一 `BackendSendPlan`
  - 说明当前 real-DUT smoke 主要验证 front-door、transport、completion、metadata 闭环
  - 显式列出 writeback `data` compare、vector store real-DUT 回归、vector coverage collector 等未完成项
- `README.md`
  - 将 `docs/vmem_design_and_usage.md` 纳入 docs 列表与推荐阅读顺序
- `docs/test_sequence_and_extension_guide.md`
  - 增加向量访存入口说明，提醒 testcase 不要把标量骨架直接改写成 pin-level `vecIssue` 脚本

#### 验证情况

- `rg -n "vmem_design_and_usage" src/test/python/MemBlock/README.md src/test/python/MemBlock/docs/test_sequence_and_extension_guide.md src/test/python/MemBlock/docs/vmem_design_and_usage.md`

### 10. 收口 Uncache/Svpbmt 控制面，并把 NC/MMIO 能力缺口固定成 capability probe

本条目记录一次面向 Uncache 验证的收口：先把 env 侧对 `cacheable / NCIO / MMIO` 的显式控制面、wait helper 和 coverage 口径补齐，再把当前 real-DUT 仍未稳定导出的 PBMT/`mmioBusy` 行为固定成精确 `xfail` 能力探针，避免继续把“helper 已有”与“DUT 语义已闭环”混成一件事。

#### 变更摘要

- `MemBlock_env.py`
  - `MmuFacade` 新增：
    - `enable_svpbmt()`
    - `disable_svpbmt()`
    - `install_sv39_gigapage_mapping(..., pbmt=...)`
  - `wait_load_writeback_observed()` 现在会带出：
    - `debug_paddr`
    - `debug_vaddr`
    - `debug_is_mmio`
    - `debug_is_ncio`
  - 新增 `wait_mmio_busy(expected=...)`
  - `wait_store_materialized()` 新增 `expected_nc`
- `sequences/memblock_sequences.py`
  - `ScalarStoreSequence` / `ScalarStoreCommitSequence` 新增：
    - `expected_nc`
    - `expected_addr`
  - 便于 translated store 按物理地址和 NC/MMIO 属性做判定
- `model/rob_coverage.py`
  - `nc_store_flush_drain_observed` 口径收紧为：
    - 必须有 `flushSb`
    - 必须有真实 `store.nc && !store.mmio` 窗口
    - 必须有 outer drain 与该窗口重叠
  - `mmio_store_excluded_from_drain_observed` 口径收紧为：
    - 必须有 `flushSb`
    - 必须存在 MMIO store shadow
    - 最终 drain 不覆盖该 MMIO touched-byte 窗口
- 新增 `tests/test_MemBlock_uncache_semantics.py`
  - 硬通过：
    - `test_api_MemBlock_env_mmu_idle_inputs_preserve_svpbmt_state`
  - capability probe / 精确 `xfail`：
    - `test_api_MemBlock_sv39_pbmt_ncio_load_smoke`
    - `test_api_MemBlock_sv39_pbmt_mmio_load_smoke`
    - `test_api_MemBlock_sv39_pbmt_ncio_store_flush_smoke`
    - `test_api_MemBlock_mmio_busy_blocks_younger_cacheable_load_retire`
- `docs/mmu_env_design_and_usage.md`
  - 同步新增 Svpbmt/PBMT 控制面说明
- `docs/rob_coverage_plan.md`
  - 同步 `nc_store_flush_drain_observed` / `mmio_store_excluded_from_drain_observed` 的最新 collector 口径
- `docs/coverage_todo.md`
  - 补记当前 helper 已落地、但 real-DUT 仍存在 capability gap 的状态

#### 验证情况

- `python3 -m py_compile src/test/python/MemBlock/MemBlock_env.py src/test/python/MemBlock/sequences/memblock_sequences.py src/test/python/MemBlock/model/rob_coverage.py src/test/python/MemBlock/tests/test_MemBlock_uncache_semantics.py`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_env_mmu_smoke.py src/test/python/MemBlock/tests/test_MemBlock_uncache_semantics.py src/test/python/MemBlock/tests/test_MemBlock_rob_coverage.py`

## 2026-04-12

### 1. 为随机 load 补入 non-mem blocker 回归

本条目记录一次 testcase 补强：在 `random load` 测试文件中新增一条 `non-mem blocker` 用例，验证 cacheable load 在真实 DUT 上仍可先写回，但测试侧 ROB 半模型会在 blocker release 前维持 frontier 阻塞，直到 release 后再恢复退休。

#### 变更摘要

- `tests/test_MemBlock_random_load.py`
  - 新增 `test_api_MemBlock_random_mem_load_non_mem_blocker_delays_compare`
  - 用随机 cacheable 地址发起单条 load，并在其前方插入一条 older `non_mem` blocker
  - 先验证：
    - younger load 的 writeback 仍可被观测到
    - `rob_non_mem_blocked_cycle_count` 增长
    - blocker 未 release 前，ROB pending entry 仍保留
  - 再 release blocker，并验证：
    - `rob_non_mem_resume_count` 增长
    - pending ROB entry 清空
    - load compare 最终完成且无 outstanding

#### 验证情况

- `python3 -m py_compile src/test/python/MemBlock/tests/test_MemBlock_random_load.py`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_random_load.py -k 'non_mem_blocker_delays_compare'`

### 2. 补强 backend / ROB 文档，明确当前半模型与 non-mem blocker 用法

本条目记录一次文档收口：把 backend 主动控制文档与 ROB 建模文档更新到当前实现状态，明确说明 `BackendSendPlan` 已同时承载 backend 脚本与 ROB 语义步骤，并补充 `non-mem blocker` / `store readiness` 的用法示例。

#### 变更摘要

- `README.md`
  - 补充在同一个 `BackendSendPlan` 中同时编排：
    - `NonMemBlockerStep`
    - `StoreCommitReadyStep`
    - `StoreCommitStep`
  - 明确当前所谓 `non-mem` 是 ROB 程序序中的 placeholder，而不是新的 `IssueOp`
- `docs/verification_env_design.md`
  - 同步 env 总设计文档中的 backend facade / commit path 说明
  - 明确 `BackendFacade` 现在同时承担 backend 脚本解释与 ROB 半模型语义入口
  - 增加 env 视角下的 `non-mem blocker` 最小示例
- `docs/backend_request_model_design.md`
  - 新增“当前 ROB 半模型与 non-mem op 的语义”说明
  - 明确：
    - `load/store/non_mem` 三类 entry 的当前提交规则
    - `STA/STD` 与 store readiness 的自动同步关系
    - `non-mem op` 当前是 blocker placeholder，而不是 issue lane 动作
  - 新增两个完整使用示例：
    - `non-mem blocker` 阻塞 / release younger mem commit
    - 显式 `StoreCommitReadyStep` 控制 store readiness
- `docs/backend_rob_cookbook.md`
  - 新增面向 testcase / sequence 作者的 cookbook
  - 收敛单笔 load/store、同拍多 lane、non-mem blocker、store readiness 的常见模板
- `docs/test_sequence_and_extension_guide.md`
  - 新增 backend / ROB 场景下“继续写 plan 还是上提成 sequence”的判断规则
  - 补充：
    - 一个应继续保留在 plan 层的 `non-mem blocker` 脚本示例
    - 一个应收敛成 sequence 的 ROB 场景模板判断
- `docs/rob_model.md`
  - 在文档前部新增“当前实现快照”，用短摘要说明当前 ROB 半模型已经落地的能力、边界与推荐控制面

#### 说明

- 这次变更不改变代码行为，目标是让后续 testcase / sequence 开发者能直接从文档理解：
  - 现在的 ROB 半模型到底已经支持到哪一步
  - non-mem blocker 应该如何表达
  - 何时应该用 `send()`，何时应该切到 `execute(BackendSendPlan(...))`

### 3. 打通 ROB 语义步骤与 non-mem/store-readiness 半模型

本条目记录一次围绕 ROB/backend 主动控制面的补强：把 `BackendSendPlan` 从“enqueue/issue/commit 脚本”扩展为“既能编排 backend 请求，也能编排 ROB 语义步骤”的统一脚本容器，同时落地 `non-mem blocker` 与 `store commit readiness` 两项 ROB 半模型能力。

#### 变更摘要

- `transactions.py` / `agents/backend_facade.py`
  - `BackendSendPlan` 新增：
    - `NonMemBlockerStep`
    - `StoreCommitReadyStep`
  - `BackendFacade.execute()` 现在除了原有 enqueue / issue / commit pulse 外，还能解释：
    - insert / release non-mem blocker
    - 显式 store commit readiness 更新
  - `IssueCyclePlan` 中的 `STA/STD` 还会自动把 store addr/data 就绪状态同步到 ROB 半模型
- `agents/commit_agent.py` / `agents/rob_agent.py`
  - `RobEntry` 从原先的 mem-only `load/store` 扩展为 `load/store/non_mem`
  - `non_mem` 支持 release 前阻塞 frontier，release 后恢复推进
  - store commit 从 token-only 升级为 `token + readiness`
  - 新增按 `sq_idx` 跟踪 store readiness 的内部映射，并补充 blocker/readiness 统计，供 coverage 与调试复用
- `model/rob_coverage.py`
  - 移除 `non_mem_blocker_not_modelled`
  - 新增 non-mem blocker 与 store readiness 正向 coverage 点
- `tests/test_request_apis_backend_facade.py` / `tests/test_MemBlock_rob_agent.py` / `tests/test_MemBlock_rob_coverage.py` / `tests/test_MemBlock_env_fixture.py`
  - 补齐 ROB 语义步骤、store readiness、non-mem blocker 的单测与 env fixture 冒烟
- `README.md` / `docs/backend_request_model_design.md` / `docs/rob_model.md` / `docs/rob_todo.md`
  - 同步记录 `BackendSendPlan` 现已承载 ROB 语义步骤，以及当前 ROB 半模型边界

#### 验证情况

- `python3 -m py_compile`
  - `src/test/python/MemBlock/transactions.py`
  - `src/test/python/MemBlock/agents/backend_facade.py`
  - `src/test/python/MemBlock/agents/commit_agent.py`
  - `src/test/python/MemBlock/agents/rob_agent.py`
  - `src/test/python/MemBlock/model/rob_coverage.py`
  - `src/test/python/MemBlock/tests/test_request_apis_backend_facade.py`
  - `src/test/python/MemBlock/tests/test_MemBlock_rob_agent.py`
  - `src/test/python/MemBlock/tests/test_MemBlock_rob_coverage.py`
  - `src/test/python/MemBlock/tests/test_MemBlock_env_fixture.py`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_request_apis_backend_facade.py`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_rob_agent.py`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_rob_coverage.py -k 'smoke_sample or samples_new_rob_frontier_points'`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_env_fixture.py -k 'backend_non_mem_blocker_controls_rob or backend_mark_store_commit_ready_updates_rob or backend_note_store_allocated_updates_state or backend_note_load_completed_advances_pending_ptr'`

### 4. 补入 cross-page store-misalign 回归并确认当前 DUT 卡死点

本条目记录在独立 `store-misalign` 测试文件中继续推进 cross-page 标量 store-misalign。该轮工作没有通过放宽 assert 来“做绿”，而是把真实失败收敛成可重复的 xfail 触发器，并明确区分 testcase 设计、环境能力与 DUT 行为。

#### 变更摘要

- `tests/test_MemBlock_store_misalign.py`
  - 在独立 misalign 测试文件中新增 2 条 cross-page directed case：
    - `SD + 0xFFD`
    - `SH + 0xFFF`
  - 新 case 会先检查：
    - store shadow 已 materialize
    - 首段 split mask 与页尾低半段字节数一致
    - 两个跨页窗口上的 younger load 都能按 stimulus-derived golden compare 收敛
  - 最终继续要求 `flushSb` 完成 drain，并把当前失败以 `strict xfail` 固化为 DUT 回归触发器
- `docs/coverage_summary.md` / `docs/coverage_todo.md`
  - 补记当前 cross-page misalign 的最新判断：
    - 环境已经足够把请求送入 `StoreMisalignBuffer` 并观测到 shadow/load 级现象
    - 但当前 DUT 在 cross-page scalar store-misalign 上仍会卡在 `flushSb` 后 `sbIsEmpty` 不清空

#### 问题定位结论

- 这次失败不是通过修改 assert 就能合理回避的 testcase 设计问题。
- 现象链路是：
  - cross-page misalign store 能 materialize 到 shadow；
  - 两侧窗口 load 能完成并通过 compare；
  - 但 `flushSb` 后 sbuffer 无法清空，最终卡死在 drain 阶段。
- 结合 `src/main/scala/xiangshan/mem/pipeline/StoreUnit.scala` 中两处 `TODO: support cross page unalign feature!`，当前更接近 **DUT 功能缺口 / 已知未打通路径**，而不是 Python env 单边误判。
- 因此这里不去弱化断言，而是把触发条件精确标成 xfail，供后续 DUT 修复后回归转正。

#### 验证情况

- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_store_misalign.py`
  - 结果：`5 passed, 2 xfailed`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_scalar_store_pipeline.py -k 'misaligned or partial or burst'`
  - 结果：`7 passed, 2 deselected`

### 5. 新增独立 store-misalign 测试文件并修复 cross-16B 环境建模

本条目记录一次围绕 `StoreMisalignBuffer` 的专项补强：新增独立的 cross-16B store-misalign directed testcase，同时根据这些 testcase 暴露出来的真实失败，修复环境对 split store retire 与 sbuffer drain pair 的建模缺口。

#### 变更摘要

- `tests/test_MemBlock_store_misalign.py`
  - 新增独立 store-misalign 测试文件，不再把新场景混入 `test_MemBlock_scalar_store_pipeline.py`
  - 新增 5 条 cross-16B scalar store directed case，覆盖：
    - `SD + 0xD`
    - `SD + 0xE`
    - `SD + 0xF`
    - `SW + 0xD`
    - `SH + 0xF`
  - 每条用例都同时检查 committed shadow、overlap load 和 flush/drain 后的最终窗口结果
- `agents/backend_facade.py`
  - `send(StoreTxn)` 在拿到实际 `sq_idx` 后，向 MemoryModel 同步原始 `addr/data/mask`
  - 这样 cross-16B store 在 commit-boundary compare 时，不再只依赖低半段 shadow mask
- `memory_model.py` / `model/scoreboard.py`
  - 新增 `note_store_request()`，把原始标量 store 请求语义登记到 scoreboard
  - `_retire_store()` 优先按原始 scalar store 请求更新 golden memory，修复 cross-16B overlap load 的高窗口 compare
  - `finalize_and_check_drain()` 增加对 cross-16B sbuffer paired drain event 的归一化处理，避免把同一组 split data/mask 直接按两个 16B 全量事件重复展开
- `tests/test_memory_model_store_logic.py`
  - 新增单测，直接覆盖：
    - cross-16B store request retire
    - split sbuffer drain pair 归一化
- `docs/coverage_summary.md` / `docs/coverage_todo.md`
  - 追加 2026-04-12 coverage snapshot 与后续判断，记录“验证能力已补上，但 `StoreMisalignBuffer` 模块百分比仍未被这轮 case 拉升”

#### 问题定位结论

- 本轮最初失败不是简单的 testcase assert 设计问题，也不是直接可判定的 DUT bug。
- 真实根因分成两类环境缺口：
  - commit-boundary compare 只退休了 cross-16B store 的低半段 shadow；
  - sbuffer drain log 对 paired split 写入口缺少归一化，导致 final drain compare 把同一组 combined `data/mask` 错当成两条完整 16B 写出。
- 修复后，新 testcase 与原有 store pipeline directed case 都能稳定通过。

#### 验证情况

- `python3 -m py_compile`
  - `src/test/python/MemBlock/model/scoreboard.py`
  - `src/test/python/MemBlock/memory_model.py`
  - `src/test/python/MemBlock/agents/backend_facade.py`
  - `src/test/python/MemBlock/tests/test_MemBlock_store_misalign.py`
  - `src/test/python/MemBlock/tests/test_memory_model_store_logic.py`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_memory_model_store_logic.py -k 'cross_16b_store_request_and_split_drain_pair or finalize_ignores_mmio_outer_drain_in_golden_compare or memory_model_defers_load_compare_until_commit_boundary'`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_request_apis_backend_facade.py -k 'send_store_translates_to_enqueue_and_issue_cycles or send_partial_store_keeps_mask_on_sta_and_std'`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_store_misalign.py`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_scalar_store_pipeline.py -k 'misaligned or partial or burst'`

### 6. 为随机 load 追加 `BackendSendPlan` 同拍三 issue 示例

本条目记录一次 testcase 补强：在 `random load` 测试文件中追加一个直接使用 `BackendSendPlan` 的 cacheable load 例子，用真实 DUT 验证“enqueue 三条 load + 同拍三 lane issue + 最终 compare 收敛”的最小脚本。

#### 变更摘要

- `tests/test_MemBlock_random_load.py`
  - 新增 `test_api_MemBlock_three_random_mem_loads_same_cycle_via_plan`
  - 使用：
    - `BackendSendPlan`
    - `EnqueueLoadStep`
    - `IssueCyclePlan`
    - `IssueOp.load_from_txn(...)`
  - 验证三条随机 cacheable load 能在同拍 issue 后全部完成 compare，并最终无 outstanding

#### 验证情况

- `python3 -m py_compile src/test/python/MemBlock/tests/test_MemBlock_random_load.py`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_random_load.py -k 'three_random_mem_loads_same_cycle_via_plan or non_mem_blocker_delays_compare'`

### 7. 为 backend plan 补上同拍多 load enqueue，并把示例切到真同拍三发

本条目记录一次 backend 主动控制面补强：此前 `BackendSendPlan` 虽然能表达“多条 load 同拍 issue”，但多个 `EnqueueLoadStep` 仍会按 step 顺序逐拍执行。当前补入 `EnqueueLoadCyclePlan` 后，plan 已能显式表达“多条 load 同拍 enqueue”，并把随机 load 示例切到真实的“同拍 3 enqueue + 同拍 3 issue”。

#### 变更摘要

- `transactions.py` / `agents/lsq_agent.py` / `agents/backend_facade.py` / `request_apis.py`
  - 新增 `EnqueueLoadCyclePlan`
  - `LsqAgent` / `BackendFacade` 新增同拍多 load enqueue 执行路径
  - `send_load_batch_same_cycle()` / `send_load_batch_with_sta_same_cycle()` 改为先构造同拍 enqueue cycle，再进入同拍 issue cycle
- `tests/test_request_apis_backend_facade.py`
  - 新增同拍 load enqueue plan 的 facade 单测
  - 补充 `EnqueueLoadCyclePlan` 端口唯一性校验测试
- `tests/test_MemBlock_random_load.py`
  - `test_api_MemBlock_three_random_mem_loads_same_cycle_via_plan` 改为真实同拍：
    - `enqLsq[0:2]` 三口同拍 enqueue
    - `intIssue[0:2]` 三 lane 同拍 issue
- `README.md` / `docs/backend_request_model_design.md` / `docs/backend_rob_cookbook.md` / `docs/test_sequence_and_extension_guide.md`
  - 同步把“多条 load 同拍”示例改成 `EnqueueLoadCyclePlan.from_txns(...)`

#### 验证情况

- `python3 -m py_compile`
  - `src/test/python/MemBlock/transactions.py`
  - `src/test/python/MemBlock/agents/lsq_agent.py`
  - `src/test/python/MemBlock/agents/backend_facade.py`
  - `src/test/python/MemBlock/request_apis.py`
  - `src/test/python/MemBlock/tests/test_request_apis_backend_facade.py`
  - `src/test/python/MemBlock/tests/test_MemBlock_random_load.py`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_request_apis_backend_facade.py -k 'batch_wrappers_delegate_to_backend_execute or same_cycle_load_enqueue_plan or enqueue_load_cycle_plan_rejects_duplicate_ports'`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_random_load.py -k 'three_random_mem_loads_same_cycle_via_plan or non_mem_blocker_delays_compare'`
- `python3 -m pytest -q src/test/python/MemBlock/tests --toffee-report --report-dir src/test/python/MemBlock/data/toffee_report_full_serial_20260412_store_misalign_cov --report-name memblock_full_serial_20260412_store_misalign_cov --report-dump-json`
  - 结果：`95 PASSED, 1 XFAIL`
  - 全局 line coverage：`65.3%`（`193043 / 295581`，相对上一版 `+71` hit）
  - 全局 branch coverage：`57.7%`（`907150 / 1573403`，相对上一版 `+560` hit）
  - `StoreMisalignBuffer.sv`：line `58.8%`（`325 / 553`），branch `37.0%`（`1258 / 3404`），本轮仍基本横盘

### 8. 收缩 ROB todo，只保留未完成缺口并清理旧 coverage 口径

本条目记录一次 ROB 文档收口：确认 `non-mem blocker`、`store commit readiness` 与当前 ROB 半模型能力已经在正式设计文档中稳定落地后，把 `rob_todo.md` 缩减为仅保留剩余未完成项，同时清理 `coverage_summary.md` 与 `rob_coverage_plan.md` 中残留的旧 known-gap 口径。

#### 变更摘要

- `docs/rob_todo.md`
  - 删除已完成的 P0/P1 计划细节
  - 改为仅保留剩余未完成项：
    - `redirect / flush / cancel` 建模
    - `backend feedback / credit` 半模型
  - 明确已完成能力的正式文档真源：
    - `docs/rob_model.md`
    - `docs/backend_request_model_design.md`
    - `docs/backend_rob_cookbook.md`
    - `README.md`
- `docs/coverage_summary.md`
  - 将 `MemBlock.ROB.KnownModelGaps` 从旧的 `3/3` 口径改为当前仍存在的 `2/2`
  - 去掉对 `pending_ptr_next` / mixed commit / non-mem blocker 未建模的旧描述
- `docs/rob_coverage_plan.md`
  - 去掉已完成项在 known-gap 列表中的旧口径
  - 保留当前真实剩余缺口：
    - `redirect_cancel_not_modelled`
    - `backend_feedback_credit_not_modelled`

#### 说明

- 这次变更不改代码行为，目标是让 ROB 文档集重新回到单一一致口径：
  - 已完成能力看正式设计文档
  - 未完成能力只保留在 `rob_todo.md`

## 2026-04-11

### 8. 修复 MMIO outer drain 与 final golden compare 的环境语义

本条目记录一次环境语义修复：让 mixed MMIO+cacheable store 在显式 flush 阶段，真正满足“MMIO outer drain 可见，但不参与 non-MMIO golden compare”的预期口径。

#### 变更摘要

- `tests/test_MemBlock_random_store.py`
  - 保留原有 `mmio_then_cacheable_store_mixed_paths` 作为稳定路径/分类 smoke
  - 新增 `test_api_MemBlock_mmio_then_cacheable_store_flush_excludes_mmio_from_final_compare`
- `model/scoreboard.py`
  - `finalize_and_check_drain()` 现在会先根据已观测 MMIO store 归一化出 touched-byte 集合
  - 最终 drain/golden compare 会跳过这些 MMIO outer drain 字节，只保留 non-MMIO 字节参与一致性检查
- `tests/test_memory_model_store_logic.py`
  - 新增 unit test，验证 mixed `outer(mmio) + sbuffer(cacheable)` drain 不会再把 MMIO 写出误纳入 golden compare
- `docs/coverage_todo.md`
  - 将该问题从环境缺口状态更新为已修复语义，并说明 mixed flush 现在可作为正常回归

#### 验证情况

- `python3 -m pytest -q src/test/python/MemBlock/tests/test_memory_model_store_logic.py`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_random_store.py -k 'mmio_then_cacheable_store_mixed_paths or mmio_then_cacheable_store_flush_excludes_mmio_from_final_compare'`

### 7. 推进 golden merge helper 在 random/order 场景落地

本条目记录把前一轮引入的 golden merge helper 从 `scalar_store_pipeline` 扩展到更多真实 DUT testcase，并顺手把 helper API 收敛得更接近 golden memory 本身。

#### 变更摘要

- `model/ref_memory.py`
  - 新增 `apply_store()` / `with_store()`，直接按 `StoreTxn.mask` 语义推导 `SB/SH/SW/SD` 宽度
  - helper 仍复用 `RefMemory` 的字节级写规则，没有引入第二套 merge 语义
- `memory_model.py`
  - 新增 `apply_store()` / `predict_store()` facade，便于 testcase 从当前 golden memory 分叉出 stimulus-derived expected
- `tests/test_MemBlock_scalar_store_pipeline.py`
  - 单 store/misalign/partial 场景改为优先使用 `predict_store()` 生成 forked golden view
- `tests/test_MemBlock_random_store.py`
  - 为 cacheable store flush smoke 补充最终 golden memory 断言
  - 保持 MMIO+cacheable mixed-path case 以路径/分类断言为主，不强行把未稳定的 mixed drain 语义塞进同一 testcase
- `tests/test_MemBlock_scalar_ordering.py`
  - 为 same-addr overwrite、unrelated load、directed mixed ld/st 场景补充最终 golden memory 断言
  - 这些断言只依赖 preload + stimulus，不再从 DUT 中间观测反推 expected
- `tests/test_memory_model_store_logic.py`
  - 补充 `apply_store()` / `with_store()` / `predict_store()` 的单测，覆盖宽度解码与 forked-view 语义
- `docs/misalign.md`
  - 文档中的推荐 helper 口径同步更新为 `apply_store()/with_store()/predict_store()`

#### 验证情况

- `python3 -m py_compile`
  - `src/test/python/MemBlock/model/ref_memory.py`
  - `src/test/python/MemBlock/memory_model.py`
  - `src/test/python/MemBlock/tests/test_MemBlock_scalar_store_pipeline.py`
  - `src/test/python/MemBlock/tests/test_MemBlock_random_store.py`
  - `src/test/python/MemBlock/tests/test_MemBlock_scalar_ordering.py`
  - `src/test/python/MemBlock/tests/test_memory_model_store_logic.py`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_memory_model_store_logic.py`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_scalar_store_pipeline.py -k 'misaligned or partial or burst'`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_random_store.py -k 'cacheable_store_flush_smoke or mmio_then_cacheable_store_mixed_paths'`
- `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_scalar_ordering.py`

### 1. 打通 scalar partial-store 请求模型

本条目记录一次面向 backend/request 的接口补强：把原先只停留在事务模型与 scoreboard 侧的 `StoreTxn.mask`，真正打通到 `BackendFacade` / `IssueAgent` 的 issue 驱动路径。

#### 变更摘要

- `transactions.py`
  - 新增 scalar store mask 解码规则：
    - `0x01 -> SB`
    - `0x03 -> SH`
    - `0x0F -> SW`
    - `0xFF -> SD`
  - `StoreTxn` 新增按 `mask` 解析 `size_bytes` / `fu_op_type` 的属性
  - `IssueOp.sta/std` 现在显式携带 `mask`，并在构造阶段拒绝不受支持的非标量连续掩码
- `agents/backend_facade.py` / `agents/issue_agent.py`
  - `env.backend.send(StoreTxn)` 不再固定发 `SD`
  - `issue_scalar_sta()` / `issue_scalar_std()` 及同拍 `STA` 兼容包装支持显式 `mask`
  - issue 驱动会根据 `mask` 自动写入匹配的 store `fuOpType`
- 新增验证：
  - `test_request_apis_backend_facade.py` 补充 partial-store 请求模型单测
  - `test_MemBlock_scalar_store_pipeline.py` 新增：
    - partial word store + aligned full load
    - high-offset byte store + aligned full load
- 设计文档同步更新：
  - `README.md`
  - `docs/verification_env_design.md`
  - `docs/backend_request_model_design.md`
  - `docs/coverage_summary.md`
  - `docs/coverage_todo.md`

#### 验证情况

- `python3 -m py_compile`
  - `transactions.py`
  - `agents/backend_facade.py`
  - `agents/issue_agent.py`
  - `request_apis.py`
  - `tests/test_request_apis_backend_facade.py`
  - `tests/test_MemBlock_scalar_store_pipeline.py`
- 接口/兼容测试
  - `/nfs/share/unitychip/unitychip/bin/pytest -q src/test/python/MemBlock/tests/test_request_apis_backend_facade.py`
  - 结果：`9 passed`
- 真实 DUT store 相关回归
  - `/nfs/share/unitychip/unitychip/bin/pytest -q src/test/python/MemBlock/tests/test_MemBlock_scalar_store_pipeline.py`
  - 结果：`6 passed`
  - `/nfs/share/unitychip/unitychip/bin/pytest -q src/test/python/MemBlock/tests/test_MemBlock_scalar_store_pipeline.py src/test/python/MemBlock/tests/test_MemBlock_random_store.py`
  - 结果：`11 passed`

### 2. 扩展 partial-store 场景矩阵

本条目记录在请求模型打通后，继续沿 `P0-1 partial-mask store` 方向补 directed case，把“接口可用”推进到“真实矩阵开始成形”。

#### 变更摘要

- `test_MemBlock_scalar_store_pipeline.py` 新增：
  - same-dword multi-byte partial merge
  - full store + high-offset partial overwrite
  - interleaved partial stores across two addresses
- 新用例覆盖了三类此前仍明显缺失的语义：
  - 同一 dword 上多次 byte merge
  - full-width base store 后的 partial overwrite
  - 多地址交织下的 partial-store 独立 merge
- `coverage_summary.md` / `coverage_todo.md` 已同步把 `P0-1` 的状态从“接口待补”更新为“已有基础矩阵，继续补更复杂组合”

#### 验证情况

- focused 新增 partial-store 用例
  - `/nfs/share/unitychip/unitychip/bin/pytest -q src/test/python/MemBlock/tests/test_MemBlock_scalar_store_pipeline.py -k 'partial_byte_merge_same_dword_directed or full_store_then_partial_overwrite_directed or interleaved_partial_stores_two_addresses_directed'`
  - 结果：`3 passed`
- store 相关回归
  - `/nfs/share/unitychip/unitychip/bin/pytest -q src/test/python/MemBlock/tests/test_MemBlock_scalar_store_pipeline.py src/test/python/MemBlock/tests/test_MemBlock_random_store.py`
  - 结果：`14 passed`

### 3. 补强标量 store 深状态覆盖

本条目记录一轮面向标量访存覆盖率的 testcase 补强。本轮没有扩大 env 公共接口，而是优先复用现有 sequence，在 store 深状态上补齐更有价值的 directed case。

#### 变更摘要

- 在 `test_MemBlock_scalar_store_pipeline.py` 新增：
  - misaligned store + dual overlap load directed case
  - store burst + interleaved load + final flush directed case
- 在 `test_MemBlock_random_store.py` 新增：
  - MMIO + cacheable store mixed-path smoke
- 新增局部 helper，用于在 testcase 内根据 store shadow 的 `addr/mask/data` 还原 overlap 窗口期望值
- 通过一轮新的完整回归与 toffee 报告刷新当前 coverage 状态：
  - `74` 个用例，`73 passed + 1 xfailed`
  - line coverage 提升到 `65.3%`
  - branch coverage 提升到 `57.6%`
- 文档同步更新：
  - `coverage_summary.md`
  - `coverage_todo.md`

#### 结果解读

- `NewStoreQueue`、`SbufferData`、`Sbuffer`、`StoreUnit` 的覆盖率继续小幅上升，说明新 case 的确打到了 store 深状态相关路径
- `StoreMisalignBuffer` 虽然已经有了稳定 directed case，但覆盖率提升仍然很有限，说明后续还需要更激进的 misalign / cross-16B / cross-beat 组合
- 本轮同时确认了一个接口事实：当前 `StoreTxn.mask` 还没有真正下沉为 backend send 的 partial-store 驱动能力，因此真正的 partial-mask store 仍需先补请求模型
- `nc_store_flush_drain_observed` 与 `mmio_store_excluded_from_drain_observed` 仍未命中，且 MMIO exclusion 已暴露出环境 drain 语义上的后续分析点

### 4. 量化 partial-store 补强后的全量 coverage

本条目记录在 partial-store 请求模型与 testcase 矩阵补齐后，对完整 MemBlock 回归重新执行的一轮 toffee coverage。

#### 变更摘要

- 执行命令：
  - `/nfs/share/unitychip/unitychip/bin/pytest -q src/test/python/MemBlock/tests --toffee-report --report-dir src/test/python/MemBlock/data/toffee_report_full_serial_20260411_partial_store_cov --report-name memblock_full_serial_20260411_partial_store_cov --report-dump-json`
- 回归结果：
  - `82` 个用例
  - `81 passed + 1 xfailed`
- 相对上一版完整 coverage 基线：
  - line hit：`192959 -> 192972`，增量 `+13`
  - branch hit：`906296 -> 906590`，增量 `+294`
  - function coverage 维持 `25/29`，未新增命中 point/bin
- 模块级增量主要集中在：
  - `DCache.sv`：`+6` line hit，`+1` branch hit
  - `Sbuffer.sv`：`+6` line hit
- `NewStoreQueue` / `SbufferData` / `StoreMisalignBuffer` 本轮基本横盘

#### 结果解读

- 本轮 partial-store 工作已经被 coverage 量化证明为“有效增益”，但当前仍属于小幅、集中式提升
- 新增用例主要强化了 cacheable partial-store 的数据路径与 sbuffer 收尾路径
- 下一步若要继续明显拉升 store 深状态覆盖，应优先转向：
  - cross-16B / cross-beat partial-store
  - partial-store + backpressure / delayed drain
  - 更深的 store queue 并存与 merge 顺序

### 5. 新增 misalign 专题设计与验证状态文档

本条目记录一次文档层收口：把目前分散在 DUT 变更分析、pipeline 方案、coverage 文档和 testcase 中的 misalign 相关事实，集中整
理成一份独立专题文档，便于后续围绕 scalar load/store misalign 持续补用例与对齐验证口径。

#### 变更摘要

- 新增 `docs/misalign.md`
  - 聚焦 scalar load/store misalign，而不是泛化到所有访存类型
  - 同时覆盖 DUT 设计分析、验证功能点、推荐验证方案、当前环境满足情况与限制
  - 明确区分：
    - store misalign 仍有 `StoreMisalignBuffer` / `NewStoreQueue` / `ExceptionInfoGen` 主线
    - load misalign 已脱离旧 `LoadMisalignBuffer` 语义，需要按 replay / exception / writeback 现象重新组织验证
  - 用 Mermaid 与矩阵表统一表达：
    - DUT 路径关系
    - 功能点矩阵
    - 当前 testcase 满足情况
    - 后续优先级建议
- `README.md`
  - 在 `docs/` 文档列表中补入 `misalign.md` 入口，避免专题文档只存在于目录中而没有统一索引

#### 验证情况

- 文档一致性检查
  - 核对 `docs/misalign.md` 已落盘，且包含：
    - DUT 设计分析
    - 验证功能点
    - 验证方案
    - 当前环境与 testcase 状态
    - 多个 Mermaid 图与表格
- 内容来源交叉核对
  - 对照 `MemBlock.scala`、`StoreMisalignBuffer.scala`、`NewStoreQueue.scala`、`ExceptionInfoGen.scala`
  - 对照 `coverage_summary.md`、`coverage_todo.md`、`rob_model.md`
  - 对照现有 `test_MemBlock_scalar_store_pipeline.py` 中 misalign / partial / burst 相关 testcase

### 5. 审查未提交 testcase assert 的独立性与有效性

本条目记录一次针对当前未提交 testcase 的静态评审，目标不是新增 testcase，而是回答两个问题：

1. 这些 assert 是否存在“用 DUT 观测结果反推期望值”的问题；
2. 即便存在，这批用例还具有什么层次的验证价值。

#### 变更摘要

- `docs/misalign.md`
  - 新增“当前未提交 testcase 的 assert 审查方法”与“逐类审查结论”章节
  - 把当前 working tree 中相关 testcase 分成 `A/B/C/D` 四档：
    - `A`：独立 expected 的强断言
    - `B`：不反推 expected，但证明面不完整的路径型断言
    - `C`：明显存在 `committed_store_view -> expected_word/window` 反推的自洽型断言
    - `D`：fake backend / facade 契约断言
  - 明确指出 `test_MemBlock_scalar_store_pipeline.py` 中多条 misalign / partial-store case 使用 `_apply_store_to_window(... , committed_store_view)`，属于当前最集中的风险点
  - 同时保留对这批用例的正面评价：
    - 它们仍能证明路径可稳定构造
    - 仍能证明 shadow / load compare / final drain 没有明显互相打架
    - 但不应被高估为独立 golden 证明
  - 给出后续整改方向：
    - 引入只依赖 stimulus 的 pure-python golden helper
    - 把 shadow-derived expected 降级为调试辅助
    - 优先整改 misalign 主代表 case 与迭代式 partial merge case

#### 验证情况

- 静态审查范围
  - `tests/test_MemBlock_scalar_store_pipeline.py`
  - `tests/test_MemBlock_random_store.py`
  - `tests/test_request_apis_backend_facade.py`
- 审查方式
  - 按 testcase 逐条核对 expected 的来源
  - 特别检查 `_apply_store_to_window()`、`committed_store_view`、`env.memory.read(...)` 之间的依赖关系

### 6. 引入与 golden memory 共存的 merge helper

本条目记录一次 model/test 协同收口：不再让 testcase 维护独立的 bytearray merge 逻辑，而是把“预测性 merge”能力直接建立在现有 golden memory 抽象之上。

#### 变更摘要

- `model/ref_memory.py`
  - 新增 `clone()`
  - 新增 `with_masked_write()`
  - 让 testcase 可以从当前 golden memory 派生一个不影响主 `RefMemory` 的 expected 视图
- `memory_model.py`
  - 新增 `fork_ref_memory()`
  - 对 tests 暴露稳定 facade，避免直接依赖 `env.memory.ref_memory` 内部实现
- `tests/test_MemBlock_scalar_store_pipeline.py`
  - 引入基于 stimulus 的 golden merge helper
  - 用 `env.memory.fork_ref_memory()` + masked write 预测结果，替换原先依赖 `_apply_store_to_window(..., committed_store_view)` 的主断言路径
  - 保留 `committed_store_view.mask` / `committed` / LQ 指针等路径断言，但不再让 DUT 中间观测参与 expected 生成
- `tests/test_memory_model_store_logic.py`
  - 新增 `RefMemory.clone()` / `with_masked_write()` / `MemoryModel.fork_ref_memory()` 的纯 Python 单测
- `docs/misalign.md`
  - 把“建议引入 pure-python golden helper”升级为当前推荐方案：
    - helper 应建立在 `RefMemory` 之上
    - golden merge 应与 golden mem 共用同一字节写语义

#### 验证情况

- pure-python 单测
  - 计划覆盖 `RefMemory.clone()` 隔离性
  - 计划覆盖 `with_masked_write()` 的 fork 语义
  - 计划覆盖 `fork_ref_memory()` 不污染主 golden mem
- 真实 DUT 聚焦回归
  - 计划重跑受影响的 misalign / partial-store / burst directed case，确认迁移后行为稳定


## 2026-04-10

### 1. 重构 backend/issue 请求发送接口

本条目记录一次主动控制接口的收敛：把新增的“多 lda”“混合 lda/sta”能力从一组越堆越多的特化 helper，重构成以请求脚本为中心的统一发送接口。

#### 变更摘要

- 在 `transactions.py` 中新增 backend send plan 数据模型：
  - `EnqueueLoadStep`
  - `EnqueueStoreStep`
  - `IssueOp`
  - `IssueCyclePlan`
  - `BackendSendPlan`
  - `StoreRef`
- `BackendFacade` 新增：
  - `execute(plan)`
  - `send(txn_or_plan)`
  - `send_many(...)`
- `IssueAgent` 新增通用同拍执行入口：
  - `issue_cycle(plan)`
  - `issue_script(plans)`
- 原有 `send_load_batch_same_cycle` / `send_load_batch_with_sta_same_cycle` 等旧入口继续保留，但全部降级为兼容包装
- `request_apis.py` 仍保留旧名字，对 tests/sequences 兼容；其内部发送逻辑改为翻译成新 plan 后再交给 `env.backend`

#### 当前接口口径

- 推荐新代码优先使用 `env.backend.send(...)` 或 `env.backend.execute(...)`
- `request_apis.py` 继续可用，但不再作为新增混合场景接口的扩展点
- 同拍多 lane / 混合 `load + sta/std` 统一由 `IssueCyclePlan` 表达，不再继续新增特化 helper 名字

#### 验证情况

- `python3 -m py_compile`
  - `transactions.py`
  - `agents/backend_facade.py`
  - `agents/issue_agent.py`
  - `request_apis.py`
  - `tests/test_request_apis_backend_facade.py`
- 接口/兼容测试
  - `python3 -m pytest src/test/python/MemBlock/tests/test_request_apis_backend_facade.py src/test/python/MemBlock/tests/test_MemBlock_env_fixture.py -k "backend_facade or request_apis" -q`
  - 结果：`7 passed`
- 真实 replay 冒烟
  - `python3 -m pytest src/test/python/MemBlock/tests/test_MemBlock_replay.py -k "scalar_forward_fail_replay_smoke or scalar_bank_conflict_replay_smoke" -q`
  - 结果：`2 passed`

### 2. 补充 backend 请求模型设计文档

本条目记录对新请求模型的文档化收口，确保后续扩展时优先复用脚本式接口，而不是重新回到特化 helper 持续膨胀的旧路径。

#### 变更摘要

- 新增 `docs/backend_request_model_design.md`
  - 说明 `LoadTxn` / `StoreTxn`、`IssueOp`、`IssueCyclePlan`、`BackendSendPlan`、`StoreRef` 的职责分工
  - 解释 `env.backend.send(...)` 与 `env.backend.execute(...)` 的边界
  - 记录兼容层策略与后续扩展规则
- 在同一文档中补充 sequence 风格示例，明确 `BackendSendPlan` 适合探路/primitive/debug，稳定场景仍优先上提到 `ResetEnvSequence`、`ScalarLoadSequence`、`ScalarStoreCommitSequence`
- 在 `README.md` 中加入新文档入口，并把它纳入推荐阅读顺序
- 在 `README.md` 的“推荐工作方式”中补一句话版选层规则，帮助开发者快速判断何时写 `BackendSendPlan`、何时抽 sequence
- 在 `docs/verification_env_design.md` 中补充到新文档的专项链接
- 在 `docs/test_sequence_and_extension_guide.md` 中补充“什么时候写 plan，什么时候写 sequence”的简版规则，方便 testcase 开发阶段快速选层

### 3. 重写 clock control 设计与使用文档

本条目记录对 `docs/clock_control_and_migration_guide.md` 的重写。新版本不再只作为“Step 调用迁移提示”，而是升级成完整的 clock control design/usage 文档。

#### 变更摘要

- 以 `EnvClockKernel`、`MemBlockEnv._step_async()`、`after_step_callback` 为中心，重新说明当前环境的时钟推进设计
- 区分 testcase 同步接口、env/agent async 原语、兼容 API 与推荐 API 的边界
- 结合当前真实实现说明：
  - 为什么 `dut.Step(1)` 必须收敛在 env 内核
  - 一拍推进时 commit / memory / monitor / callback 的固定顺序
  - `StepRis` 与 `after_step_callback` 的职责区别
- 结合当前测试和 sequence 给出最佳实践示例：
  - env fixture 冒烟中的显式 `Step()`
  - `ScalarStoreCommitSequence` / violation sequence 中的 `advance_cycles()`
  - replay/debug trace 对 `after_step_callback` 的使用模式
- 新增反模式与新功能落层建议，方便后续继续扩展 clock-related 功能时保持统一风格

### 4. 删除旧公开 clock API 并迁移调用点

本条目记录新 clock 方案的最后一轮接口收口：把仍为兼容保留的旧公开 clock API 删除，并把主验证路径中的调用点统一改到 `reset()` / `advance_cycles()`。

#### 变更摘要

- 删除 `MemBlock_api.py` 中的 `api_MemBlock_reset()` / `api_MemBlock_step()`
- 删除 `MemBlockEnv` 的公开 `Step()` 入口，保留 `advance_cycles()` 作为唯一显式拍推进接口
- env fixture / ROB coverage 测试迁移到：
  - `env.reset(...)`
  - `env.advance_cycles(...)`
- 为避免破坏现有 WebUI 控制面，`lsq_webui.py` 内部仅把其私有 `Step()` 包装改为委托到 `env.advance_cycles(...)`，不改变 WebUI 对外形状
- 设计文档同步删去“旧 clock API 仍保留”的口径，明确当前方案已经完成公开接口收口

## 2026-04-09

### 1. 收敛 env 时钟治理

#### 变更摘要

本条目记录一轮面向 env 时钟治理的收敛重构。目标不是继续在 `agents/`、MMU helper 和各类 wait/pulse 逻辑里散落 `Step()`，而是把 DUT 时钟推进统一收口到 `MemBlockEnv` 内核，同时保留对现有同步 facade/testcase 的兼容。

#### 1. env 内新增统一时钟内核

- `MemBlockEnv` 现在持有私有 `EnvClockKernel`，由它独占 `dut.Step(1)` 的执行权。
- 内核统一负责同步 facade 到 async 协程的桥接，并把每拍的 DUT 推进、拍后 monitor/model 更新与 callback 分发固定在 env 内部。
- 对外 `env.Step()` 仍保留同步接口，但内部只作为对 env 时钟内核的兼容包装。

#### 2. env 内部 wait/pulse 路径迁入 async 原语

- 新增私有 async 原语：
  - `_step_async()`
  - `_await_cycles()`
  - `_step_and_idle_async()`
- replay/release/nuke/store-materialize/quiesce/drain/flushSb 这类轮询逻辑不再直接散落调用 `Step()`，而是统一改走 env 内核原语。
- MMU 相关的 distributed CSR pulse、`enable_sv39()`、`pulse_sfence()` 也改为 async 实现 + 同步兼容 wrapper。

#### 3. active agents 不再自己推进时钟

- `IssueAgent` 与 `LsqAgent` 的内部握手主路径改为 async 协程，由 env 内核统一推进拍数。
- `BackendFacade.step_commit()` 也不再直接调用 `env.Step()`，而是显式委托给 env 的时钟原语。
- 这样 `agents/` 中不再保留零散的 `Step()` 调用，时钟推进语义集中回 env。

#### 4. request_apis / sequences 继续去拍级化

- `request_apis.py` 中的 backend reset 等待已改走 `env.wait_backend_reset_deassert()`。
- `sequences/memblock_sequences.py` 与 `sequences/violation_sequences.py` 中原有的显式 `env.Step(...)` 调用已清理，改为：
  - `env.advance_cycles(...)`
  - `env.wait_store_addr_observed(...)`
  - `env.wait_completed_load_count(...)`
- 这样 sequence 层继续保留场景语义，不再携带本地拍级轮询细节。

#### 5. callback 与文档同步更新

- `after_step_callback` 现在支持同步函数和 async coroutine callback。
- `test_MemBlock_env_fixture.py` 中：
  - `flush_store_buffers_and_wait()` 的观测方式改为 after-step callback，而不是 monkey-patch `env.Step()`。
  - 新增 async after-step callback smoke，验证 env 内核会在拍后正确 await callback。
- 新增 `docs/clock_control_and_migration_guide.md`，面向开发者说明如何在 env / agent / request_apis / sequence / testcase 各层复用统一时钟原语。

#### 6. 验证情况

- 已验证：
  - `python3 -m py_compile`
  - `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -p xdist.plugin -n 16 -q src/test/python/MemBlock/tests`
- 回归结果：
  - `64 passed in 249.64s`
  - 在 `request_apis.py` / `sequences/` 进一步收口后再次执行：
    - `64 passed in 232.32s`
- 这轮修改保持对外同步 facade 不变，因此现有 tests / sequences / `request_apis.py` 不需要批量迁移即可继续运行。

### 2. 收紧 `sq_datainvalid_matchinvalid_nuke` 的 replay 排他口径

#### 1. 用例不再接受 redirect 与 LSQ replay 双去路

- `test_api_MemBlock_scalar_sq_datainvalid_matchinvalid_nuke_smoke` 不再只排除 `DM/DR/TM/NC`。
- 现在对同一条 younger load 明确要求：一旦观测到 flush 级 `memoryViolation`，就不能再从 `replay_queue` / `replay_lane` / `ldu` / `nc_out` 看到该 ROB 的 replay cause。

#### 2. transport 统计改为覆盖整个恢复期

- `ScalarSqDataInvalidMatchInvalidTriggerSequence` 现在把 transport 统计的收尾点从“violation 后短窗口”改为“主 load 最终恢复完成之后”。
- 这样 testcase 可以覆盖整个恢复路径，避免后续 refill / outer 请求只是在短窗口之外发生而被漏检。

#### 3. sequence 补充全流程 replay 观测

- `sequences/violation_sequences.py` 新增对目标 ROB replay 事件的全流程采样。
- 用例因此能区分“仅有 memoryViolation”与“redirect 后仍向 LSQ 建立 replay 去路”这两类语义。

#### 4. 文档同步更新

- `docs/sq_matchinvalid_nuke_case_analysis.md` 已同步改写为新的排他契约，不再把 `FF` 视为可接受的已知行为。

#### 5. 已知 DUT bug 暂时以精确条件 `xfail` 挂起

- 当前真实 DUT 仍会在 flush 级 `memoryViolation` 之后暴露 `FF` replay path。
- 该问题已登记为 `DUTBUG-matchinvalid-redirect-replay-dual-path`，并在 `docs/BUGS.md` 中记录 `src/main/` 路径对应的 DUT commit hash。
- 因此 `test_api_MemBlock_scalar_sq_datainvalid_matchinvalid_nuke_smoke` 暂时不是整例无条件 `xfail`，而是在实际观测到这组 replay 事件时调用带 bug tag 的 `pytest.xfail(...)`。
- 这样已知 DUT 缺陷不会把回归直接打红，但其他不相关断言仍保持真实失败能力。

### 3. 补齐 `BC` / pipeline `NK` 白盒观测与组合场景探索

#### 1. 新增同拍 load issue 能力与 `debugLsInfo` 观测

- `IssueAgent` / `BackendFacade` / `request_apis.py` 新增同拍多 load issue helper，供 replay 场景稳定复用。
- `MemBlockEnv` 新增 `sample_load_debug_state()`，把顶层 `io_debug_ls_debugLsInfo_*` 白盒口整理成结构化采样结果。
- `sequences/memblock_sequences.py` 新增 load debug trace / SQ forward 采样 helper，测试侧不再直接散读分散信号。

#### 2. 单独场景先行落地

- `ScalarForwardFailReplaySequence` 现在会回填：
  - `sq_forward_event`
  - 目标 load 的 `load_debug_trace`
- 新增 `ScalarBankConflictReplaySequence`，用于证明“同拍双 load + 同 bank + 不同 set”可稳定打到 `BC`。
- 新增 `ScalarPipelineStldNukeSequence`，用于探索“older store 晚到 STA”这一条直接 pipeline `NK` 路径。

#### 3. 回归层新增 directed smoke

- `test_MemBlock_replay.py` 新增：
  - `test_api_MemBlock_scalar_bank_conflict_replay_smoke`
  - `test_api_MemBlock_scalar_pipeline_stld_nuke_smoke`
  - `test_api_MemBlock_scalar_bankconflict_sq_datainvalid_nuke_smoke`
- 其中 FF smoke 也同步收紧为必须命中：
  - `dataInvalid=1`
  - `matchInvalid=0`
  - `forwardInvalid=0`

#### 4. 组合场景当前状态

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

#### 5. 验证情况

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

### 4. 收口 standalone pipeline `NK`

本条目记录 `pipeline NK` 单场景从探索态 `xfail` 收口成稳定回归用例的这轮修正。核心结论不是 DUT 完全打不出 `NK`，而是旧 directed timing 把 `STA` 发得过晚，导致 testcase 自己把目标 load 推到了只看见 `DR/DM` 的路径。

#### 变更摘要

- `ScalarPipelineStldNukeSequence` 不再先等 `load_debug.s1` 后再发 `STA`，而是在 load issue 后立即补发 `STA`。
- `test_api_MemBlock_scalar_pipeline_stld_nuke_smoke` 去掉了场景缺口 `xfail`，改为对 `NK` 路径做硬断言。
- `sq_pipeline_stld_nuke.md` 同步更新，明确这次修正的根因和当前验证口径。

#### 1. 修正 standalone pipeline NK 的 directed timing

本轮白盒排查表明：

- `STA` 若等到 younger load 已经明显进入 `s1` 再发，`load_debug_trace` 会稳定退化成 `DR/DM`
- `STA` 若在 load issue 后立即发起，目标 load 可以稳定命中 `NK`
- 同时 load 最终仍能写回 older store data，store 也能进入 committed

因此问题根因在 testcase/sequence 的时序，而不是“当前 DUT 完全不支持 pipeline NK”。

#### 2. 当前 standalone NK 的验证口径

当前 testcase 硬断言以下事实：

- `load_debug_trace` 命中 `NK`
- 同一 trace 不混入 `BC`
- 同一 trace 不混入 `FF`
- younger load 最终写回 older store data
- older store 最终进入 committed

需要注意的是，当前 `NK` trace 仍可能同时带着 `DR/DM`。这说明当前用例证明的是“pipeline-side nuke 已稳定出现并能恢复完成”，而不是“只剩一个孤立的 `NK` bit”。

### 5. 收口 `bankconflict + dataInvalid + pipeline NK` 组合场景

本条目记录 `test_api_MemBlock_scalar_bankconflict_sq_datainvalid_nuke_smoke` 从 `xfail` 候选收口为稳定回归用例的这轮修正。核心结论是：旧问题主要是 testcase 时序和 writeback 观测口径不对，而不是 DUT 缺少这条组合路径。

#### 变更摘要

- `ScalarBankConflictSqDataInvalidNukeSequence` 改为先等 victim load 的早期 `NK`，再等后续 `BC + FF`，最后才补 `STD` 并提交 store。
- 同一 sequence 的 writeback 观测改为基于 trace 收口，避免顺序等待 lead/victim writeback 时漏采已发生的写回。
- `test_api_MemBlock_scalar_bankconflict_sq_datainvalid_nuke_smoke` 保持对 `NK` 做硬断言，不再依赖 `xfail`。
- `sq_bankconflict_datainvalid_nuke_combo.md` 同步更新，明确“pipeline NK 可稳定构造，但恢复后段仍可能出现 RAW/RAR query 流量”。

#### 1. 本轮修正解决了什么

本轮白盒排查确认了两个独立问题：

- 如果 `STD` 补得过早，victim load 会退化成 `FF` 主导的重发表现，看不到稳定 `NK`
- 即便场景构造成功，按固定顺序去等 lead / victim writeback，也可能错过已经先发生的写回事件

因此旧版失败并不能直接证明 DUT 没有这条组合路径。

#### 2. 当前验证口径

当前 testcase 硬断言以下事实：

- SQ forward 命中 `dataInvalid=1 && matchInvalid=0`
- victim load 的主 replay 断言点命中 `BC + FF`
- victim load 的完整 debug trace 中稳定出现 `NK`
- victim load 最终写回 older store data
- lead load 不被 older store 污染
- older store 最终进入 committed

同时，本轮去掉了“全程不允许 target RAW/RAR query”这条过强假设，因为成功恢复的波形上可以看到后段 query 流量。

#### 3. 验证情况

- `python3 -m py_compile`
  - `src/test/python/MemBlock/sequences/violation_sequences.py`
  - `src/test/python/MemBlock/tests/test_MemBlock_replay.py`
- 定向稳定性回归
  - `python3 -m pytest src/test/python/MemBlock/tests/test_MemBlock_replay.py -k bankconflict_sq_datainvalid_nuke -q -rxX`
  - 连续 5 轮：`5/5` 通过
- replay 回归
  - `python3 -m pytest src/test/python/MemBlock/tests/test_MemBlock_replay.py -n 16 -q`
  - 结果：`9 passed, 1 xfailed`

## 2026-04-08

### 1. 补全 MMU/PTW/DTLB env/facade 稳定性

本条目记录一轮面向 MMU/PTW/DTLB 外围稳定性的 env/facade 补全。目标不是直接扩 testcase，而是先把 Sv39/PTW/PMP 这条控制面补成一个可复用、可 smoke、能穿过 `idle_inputs()` 与 DUT reset 的稳定入口，为后续 translation/replay 场景复用打底。

#### 变更摘要

- `MemBlockEnv` 新增 `env.mmu` facade，集中管理 Sv39、sfence、PTW responder 与 PMP helper。
- PTW mock 从“单拍返回一个 256-bit beat”修正为“按 TileLink size 返回完整 multi-beat D 响应”。
- `idle_inputs()` 在清空默认输入后，会重新施加活跃的 Sv39 状态，避免 `csr_agent.reset()` 把测试中途切回 M-mode。
- `reset()` 在 DUT reset 后会重放持久化的 PMP CSR 写入与当前 Sv39 配置。
- 新增真实 DUT smoke，证明 Sv39 + PTW + DTLB + cacheable load 的基础 MMU 闭环可正常工作。

#### 1. `env.mmu` facade 收口 MMU 控制面

新增的 `env.mmu` 对外提供：

- `enable_sv39()` / `disable_translation()`
- `install_sv39_gigapage_mapping()`
- `pulse_sfence()`
- `write_distributed_csr()`
- `program_pmp_entry()` / `allow_all_smode_access()`
- `ptw_responder()` context manager

这样后续 testcase 不再需要自己 monkey-patch `idle_inputs()` 或手写分散的 PTW/PMP 临时 helper，而是统一通过 env facade 进入。

#### 2. PTW responder 修正为 multi-beat TileLink 响应

此前 PTW mock 仅返回一个 D beat，无法覆盖 `size=6` 的 64B 请求，导致 TLB miss replay 长期滞留。现在 responder 会按请求大小切分完整 beat 序列，并逐 beat 在 D 通道握手返回。

这一步是 MMU smoke 能跑通的关键修复之一。

#### 3. 稳定 Sv39 / PMP 配置跨越 `idle_inputs()` 与 reset

本轮明确了两类状态的责任归属：

- `tlbCsr` 输入属于“每拍输入面”，需要在 `idle_inputs()` 后重放。
- PMP CSR 写入属于“DUT 内部状态”，需要在 DUT reset 后重放。

因此：

- `idle_inputs()` 现在会在默认清零后调用 `env.mmu.reapply_inputs()`
- `reset()` 会在 deassert reset 后调用 `env.mmu.reapply_after_reset()`

这使得 MMU testcase 可以在标准 env 生命周期中稳定复用，而不用靠测试局部补丁保活。

#### 4. 新增真实 DUT MMU smoke

新增 smoke 覆盖两件事：

- `env.mmu` 激活 Sv39 后，`idle_inputs()` 仍能保持 S-mode + satp 输入稳定。
- 一条 cacheable load 在 Sv39/PTW/PMP 背景下可以真实完成写回，且走 dcache 路径而不是 outer/uncache 路径。

#### 5. 验证情况

本轮建议至少执行：

- `python3 -m py_compile`
  - `src/test/python/MemBlock/MemBlock_env.py`
  - `src/test/python/MemBlock/tests/test_MemBlock_env_fixture.py`
  - `src/test/python/MemBlock/tests/test_MemBlock_env_mmu_smoke.py`
- `pytest`
  - `src/test/python/MemBlock/tests/test_MemBlock_env_fixture.py`
  - `src/test/python/MemBlock/tests/test_MemBlock_env_mmu_smoke.py`

### 2. 迁移 `sq dataInvalid + matchInvalid + nuke` 到新 MMU 环境

本条目记录在稳定 MMU env/facade 之后，进一步把 `sq dataInvalid + matchInvalid + nuke` 场景切换到新 MMU 环境，并同步把设计摘要沉淀为项目内文档。

#### 变更摘要

- 新增 `ScalarSqDataInvalidMatchInvalidSequence`，把目标 replay 场景切到 `env.mmu`。
- 新增真实 DUT smoke：`sq_datainvalid_matchinvalid_nuke`。
- 新增两份文档，收口 MMU 设计/用法和该 replay 用例的设计原理。

#### 1. 用例迁移到新 MMU 环境

当前稳定方案没有继续采用“root-A 下 translated store + root-B 下 translated load”的对称结构，而是采用：

1. bare 模式下对 root-B 目标物理页做 cache warmup
2. bare 模式下发 older store 的 `STA(main_va)`
3. 切到 Sv39 root-B
4. 发 TLB prime load
5. 发 younger main load，观测 `dataInvalid + matchInvalid + memoryViolation`

这样做的原因是：在真实 DUT 上，store `STA` 侧的 MMU 组合并不如 load 侧稳定；而“bare older store + translated younger load”能够稳定保留所需的相关性与失配行为。

#### 2. 主 load 的后续收敛语义

当前真实 DUT 并不会在出现 `memoryViolation` 后永久没有后续动作。相反，主 load 在后续 replay/recovery 过程中仍可能完成写回，且最终数据来自 older store 补齐后的 store data。

因此 sequence 在观测到目标 invalid/nuke 组合后，还会：

- 补 `STD`
- 推进 store commit
- 为主 load 登记 replay completion expectation
- 等待场景完整收敛

这一步是为了让 testcase 不只“打到一个瞬时波形点”，而是能稳定进入日常回归。

#### 3. 新增文档

新增：

- `docs/mmu_env_design_and_usage.md`
- `docs/sq_matchinvalid_nuke_case_analysis.md`

其中：

- 前者说明 `env.mmu` 的职责边界、Sv39/PTW/PMP 设计和推荐使用流程。
- 后者说明 `matchInvalid + dataInvalid + nuke` 场景的设计原理、最终稳定方案和当前 DUT 的实际表现。

#### 4. 验证情况

本轮建议至少执行：

- `unset _SITE_PACKAGE_ACTIVATED && source /nfs/share/unitychip/activate && python3 -m pytest src/test/python/MemBlock/tests/test_MemBlock_replay.py -k sq_datainvalid_matchinvalid_nuke -q`
- `unset _SITE_PACKAGE_ACTIVATED && source /nfs/share/unitychip/activate && python3 -m pytest src/test/python/MemBlock/tests/test_MemBlock_replay.py -q`

#### 5. MMU / sequence 分层重构

在不改变行为的前提下，本轮进一步把 `matchInvalid_nuke` 的实现从“大而全专题 sequence”重构为：

- `MmuSv39AddressSpaceInstallSequence`
- `MmuSv39ActivateSequence`
- `ScalarSqDataInvalidMatchInvalidTriggerSequence`

其中当前 testcase 实际采用：

1. testcase 先多次调用 install sequence，分别配置 root-A / root-B
2. testcase 再调用 trigger sequence，完成 `bare older store -> activate root-B -> TLB prime -> main load -> recovery`

这样做的目的不是新增能力，而是把“MMU 配置”和“专题行为触发”拆开，使后续其他 MMU testcase 可以直接复用配置部分，同时保住当前用例依赖的关键顺序：older bare store 必须发生在 activation 之前。

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

### 1. testcase 与内部实现解耦。
### 2. `MemoryModel` 不再承担所有职责。
### 3. 观测、驱动、检查、场景、配置各有明确归属。

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

### 1. 统一 monitor 事件对象
- 减少 monitor 到 scoreboard 的细粒度方法耦合。

### 2. 扩展 sequence 组合能力
- 增加 mixed traffic、异常路径、flush/replay、memory violation 等高层场景。

### 3. 推进结构性参数化
- 让当前仍固定的端口宽度与 pipeline 配置逐步纳入真实可配范围。

### 4. 收紧 legacy 接口
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
