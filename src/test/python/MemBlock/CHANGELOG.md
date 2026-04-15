# MemBlock Python Verification Environment CHANGELOG

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
