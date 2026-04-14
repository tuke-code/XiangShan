# MemBlock 向量访存验证 Phase 1 实施细化

## 1. 文档定位

本文档细化 `src/test/python/MemBlock/docs/vector_mem_plan.md` 中的第一阶段实现，目标不是一次性覆盖全部 RVV 向量访存，而是用**最小但可闭环**的一组能力，把当前 MemBlock 真实 DUT 环境从 scalar-only 推进到“可以开始稳定验证 vector unit-stride / basic stride / non-zero `vstart`”的状态。

本文档重点回答：

1. 第一阶段到底先做什么，不做什么。
2. 哪些文件应新增，哪些现有文件应最小扩展。
3. 每个新增层次的接口边界是什么。
4. 第一阶段的 testcase、coverage、验收口径是什么。

## 2. Phase 1 范围冻结

第一阶段**只**承诺以下能力：

### 2.1 指令范围

- unit-stride vector load/store
- constant-stride 的最小子集：
  - 正 stride
  - 零 stride
  - 负 stride
- 非零 `vstart`

### 2.2 功能语义范围

- active / inactive / tail / prestart 元素分类
- element-level address/data/reference 展开
- 最终 load correctness / store memory effect correctness
- 最小 vector completion / replay / trap 事实采样

### 2.3 暂不进入 Phase 1 的内容

以下内容不放入第一阶段主交付：

- indexed ordered/unordered
- fault-only-first
- segment
- 完整 vector/scalar mixed-path
- MMIO / nc vector memory
- 复杂 translation / permission 专题
- ordered memory request 顺序证明
- `vlm/vsm` packed mask memory 语义

这些内容在 `vector_mem_plan.md` 中保留为 Phase 2/3，而不是在 Phase 1 里半做半留坑。

## 3. 第一阶段的落地原则

## 3.1 先打通骨架，不先追大矩阵

第一阶段的关键不是“先写很多 testcase”，而是确保下面四层同时成形：

1. **transaction schema**：能表达一条 vector memory op 的最小架构语义。
2. **facade**：能稳定驱动 DUT，不把 pin-level 时序泄漏到 testcase。
3. **model**：能把一条 vector op 展开成 element-level reference plan。
4. **monitor**：能采集最小 completion / exception / memory request 事实。

若这四层不完整，直接先堆 testcase 只会得到一批脆弱脚本。

## 3.2 先保留最小观测闭环，不急于追求“所有白盒点都看见”

第一阶段允许 monitor 只覆盖最小必要事实：

- 请求已发出
- 请求已完成或 trap
- 观测到的 memory request 基本信息
- `vl` / `vstart` 的必要 side effect

不要求第一阶段就完整复刻标量 replay/ROB 级白盒粒度。

## 3.3 继续沿用现有 MemBlock 分层

第一阶段建议新增文件，但不建议破坏现有目录逻辑：

- transaction / request 语义留在 `transactions.py` 与 facade 层
- front-door 统一走 `enqLsq + vecIssue`
- 实现骨架留在 `agents/`、`monitors/`、`model/`、`sequences/`
- testcase 只负责场景与断言
- coverage collector 独立放在 `model/`

## 4. 建议新增/修改文件

## 4.1 建议新增文件

### 4.1.1 `src/test/python/MemBlock/model/vector_memory_model.py`

职责：

- 根据 `VectorMemTxn` 展开 reference access plan
- 处理 unit-stride / stride / mask / `vstart`
- 生成 expected load data / expected store memory effect

### 4.1.2 `src/test/python/MemBlock/monitors/vector_mem_monitor.py`

职责：

- 采集 vector memory request / completion / trap 事实
- 如条件允许，采集 element memory request 粒度的白盒事实

### 4.1.3 `src/test/python/MemBlock/agents/vector_backend_facade.py`

职责：

- 持有 vector memory issue 的主动控制面
- 封装默认 `VectorEnqueueStep -> VectorIssueStep -> VectorWaitStep` 脚本
- 对 tests / sequences 暴露稳定入口

### 4.1.4 `src/test/python/MemBlock/agents/vector_issue_agent.py`

职责：

- 驱动真实 DUT 的 `vecIssue` 前门
- 与 `enqLsq` 一起组成向量访存 Phase 1 的 front-door

### 4.1.5 `src/test/python/MemBlock/sequences/vector_mem_sequences.py`

职责：

- 封装 unit-stride / stride smoke 场景
- 封装 mask / `vstart` 场景
- 减少 testcase 直接编排底层驱动

### 4.1.6 `src/test/python/MemBlock/model/vector_coverage.py`

职责：

- 定义第一阶段 vector coverage group
- 记录 observed behavior / current model / known gaps

### 4.1.7 testcase 文件

建议新增：

- `tests/test_MemBlock_vector_unit_stride.py`
- `tests/test_MemBlock_vector_stride.py`

第一阶段先不拆太多 testcase 文件，避免骨架未稳时目录扩张过快。

## 4.2 建议最小修改的现有文件

### 4.2.1 `transactions.py`

建议新增最小对象：

- `VectorMemTxn`
- `VectorElementAccess`
- `VectorMemResult`

### 4.2.2 `MemBlock_env.py`

建议最小接入：

- 组装 `vector_issue_agent`
- 组装 `vector_backend`
- 组装 `vector_monitor`
- 提供只读 facade：
  - 获取 vector 观测结果
  - 等待 vector completion / trap

不建议在 Phase 1 中把全部 vector helper 直接堆回 `MemBlockEnv`，避免重回单体 env。

### 4.2.3 `memory_model.py`

建议只新增组合入口，不改坏现有 scalar 语义：

- 持有 `VectorMemoryModel`
- 为 tests / sequences 暴露最小 facade

### 4.2.4 `request_apis.py`

第一阶段不建议在这里塞太多 vector 特化 helper。若确有必要，只保留最薄兼容层，例如：

- `send_vector_load(env, txn)`
- `send_vector_store(env, txn)`

其内部直接委托给 `env.vector_backend`。

## 5. 第一阶段核心数据结构建议

## 5.1 `VectorMemTxn`

建议第一阶段只包含最小字段：

- `req_id`
- `is_load`
- `opcode_class`
  - `unit_stride`
  - `stride`
- `base_addr`
- `stride`
- `sew_bits`
- `vl`
- `vstart`
- `mask_bits`
- `element_count`
- `lq_ptr` / `sq_ptr`
- `enq_port` / `issue_port`
- `num_ls_elem`
- `expected_exception`

第一阶段不要把 `mask packed memory` / `segment` / ordering 保证等复杂字段全部一次性塞满；不需要的字段保持缺省或不引入。

## 5.2 `VectorElementAccess`

建议第一阶段字段：

- `element_idx`
- `active`
- `is_tail`
- `is_prestart`
- `addr`
- `size_bytes`
- `expected_load_data`
- `store_data`
- `should_access_memory`

第一阶段先不引入 `field_idx` 之外的 segment 复杂性；如需兼容后续扩展，可把 `field_idx` 固定为 0。

## 5.3 `VectorMemResult`

建议第一阶段字段：

- `req_id`
- `completed`
- `trapped`
- `observed_vl`
- `observed_vstart`
- `observed_requests`
- `observed_writebacks`
- `observed_exception`

## 6. 第一阶段 facade 设计建议

## 6.1 `VectorBackendFacade` 的公共入口

建议最小公共入口：

- `send(txn: VectorMemTxn)`
- `execute(txns)`
- `wait_complete(req_id, max_cycles=...)`
- `wait_trap(req_id, max_cycles=...)`

如果当前 DUT 必须经 CSR 配置才能发向量访存，建议 facade 还提供：

- `configure_vector_context(...)`
- `restore_vector_context(...)`

但这些 helper 仍应保持“配置向量上下文”的语义，不要暴露过细 pin-level 驱动。

## 6.2 facade 与 testcase 的契约

testcase 应只表达：

1. preload memory
2. 构造 `VectorMemTxn`
3. `env.vector_backend.send(txn)`
4. `env.memory.vector.expect_*` 或 sequence 内部登记期望
5. 等待 completion / trap
6. 做结果断言

testcase 不应承担：

- 手动写 `vl` / `vtype` / `vstart` 时序
- 手动轮询数十个散端口
- 手动展开每个 element 的 expected 逻辑

## 7. 第一阶段 model 设计建议

## 7.1 `VectorMemoryModel` 的最小职责

建议包含三个公开方法：

### `expand(txn)`

输入 `VectorMemTxn`，输出 `tuple[VectorElementAccess, ...]`。

### `expect_load(txn)`

基于 `RefMemory` 与展开后的 element plan，生成这条 vector load 的预期结果。

### `predict_store(txn)`

基于当前 `RefMemory` 派生一个 forked expected view，用于比较 vector store 的最终 memory effect。

## 7.2 第一阶段的建模规则

### unit-stride

- `addr = base_addr + idx * size_bytes`

### stride

- `addr = base_addr + idx * stride`

### mask / active / tail / prestart

对每个 element index：

- `idx < vstart` -> prestart
- `vstart <= idx < vl` 且 mask=1 -> active
- `vstart <= idx < vl` 且 mask=0 -> inactive
- `idx >= vl` -> tail

只有 active 元素：

- `should_access_memory = True`
- 参与 expected load / store compare

### store compare

第一阶段建议采用**最终 memory effect compare**，而不是一开始就尝试为 vector store 建立复杂 online compare。

### load compare

第一阶段建议按 element 级比对 observed load result 与 expected plan。

## 7.3 第一阶段明确不建模的内容

- FOF `vl` trim
- ordered/unordered 顺序证明
- segment `nf>1`
- vector MMIO / NC 特殊可见性
- vector store drain 细粒度归并语义

这些都留到后续 phase，避免第一阶段 model 越做越厚。

## 8. 第一阶段 monitor 设计建议

## 8.1 最低必须观测的事实

`VectorMemMonitor` 在第一阶段至少要能导出：

1. 某条 vector memory op 已发起
2. 某条 op 已完成或 trap
3. 对应的 memory request 基本信息
4. 观测到的 `vl` / `vstart` 必要 side effect

## 8.2 第一阶段允许的现实折中

如果当前 DUT 暂时无法稳定导出 element-level memory request，则第一阶段允许：

- 以 instruction-level completion + 最终 load/store architectural effect 为主闭环
- 把更细粒度 request order / split order 放到 Phase 2/3

但必须在 `vector_coverage.py` 中显式挂入 known gap，不可默认“已覆盖”。

### 当前实现快照（2026-04-14）

当前已落地的 real-DUT smoke 口径比最初计划更保守，具体如下：

- 已验证闭环：
  - `enqLsq + vecIssue` front-door
  - dcache A request / dcache D response transport
  - vector completion / writeback metadata
  - unit-stride 与 basic stride 路径区分
- 当前 testcase 主要检查：
  - `completed` / `trapped`
  - `vec_wen`
  - `is_vec_load`
  - `is_strided`
  - `observed_vl`
  - dcache request / response 计数与 block address
- 当前尚未在 smoke 中收紧：
  - writeback `data` 数值 compare
  - merge / oldVd / partial-write 数据面语义

这意味着 Phase 1 已经证明请求面与完成面可稳定闭环，但更细的数据面正确性仍应作为后续专题，而不是在当前 smoke 中伪装为“已经稳定支持”。

## 9. 第一阶段 sequence 与 testcase 设计

## 9.1 sequence 级 helper

建议至少提供：

### `VectorUnitStrideLoadSequence`

职责：

- 预配置 vector context
- 发送一条 unit-stride load
- 等待 completion
- 返回结构化结果

### `VectorUnitStrideStoreSequence`

职责：

- 发送一条 unit-stride store
- 等待 completion
- 返回结构化结果

### `VectorStrideLoadSequence`

职责：

- 覆盖正/零/负 stride
- 生成结构化结果

### `VectorMaskedStoreSequence`

职责：

- 覆盖 active / inactive / tail / prestart 的最小组合

## 9.2 第一阶段 testcase 清单

建议最小 testcase 集合：

### `test_MemBlock_vector_unit_stride.py`

1. unit-stride load full active
2. unit-stride store full active
3. masked unit-stride store checkerboard mask
4. non-zero `vstart` unit-stride load
5. tail present but not accessed

### `test_MemBlock_vector_stride.py`

1. positive stride load
2. zero stride load
3. negative stride load
4. masked stride store

第一阶段不追求大矩阵；关键是每类语义至少有一条 stable directed case。

## 10. 第一阶段 coverage 设计

建议 `model/vector_coverage.py` 先做最小 coverage 组：

## 10.1 `MemBlock.Vector.ObservedBehavior`

建议点位：

- `unit_stride_load_observed`
- `unit_stride_store_observed`
- `stride_positive_observed`
- `stride_zero_observed`
- `stride_negative_observed`
- `masked_inactive_skip_observed`
- `nonzero_vstart_observed`
- `tail_skip_observed`
- `vector_completion_observed`
- `vector_trap_observed`

## 10.2 `MemBlock.Vector.CurrentModel`

建议点位：

- `unit_stride_load_closed`
- `unit_stride_store_closed`
- `basic_stride_closed`
- `mask_skip_closed`
- `nonzero_vstart_closed`

## 10.3 `MemBlock.Vector.KnownModelGaps`

建议先显式记录：

- `ordered_request_order_not_modelled`
- `fault_only_first_not_modelled`
- `segment_not_modelled`
- `vector_mmio_nc_not_modelled`
- `vector_scalar_mixed_path_not_modelled`

## 11. 第一阶段验收标准

第一阶段不以“覆盖所有向量访存”为目标，而以“建立可持续扩展的真实 DUT 骨架”为验收标准。

建议验收条件：

1. 新增骨架文件全部存在，并能通过 `py_compile`。
2. 至少 2 个 testcase 文件可运行：
   - `test_MemBlock_vector_unit_stride.py`
   - `test_MemBlock_vector_stride.py`
3. 当前最小 real-DUT smoke 至少覆盖：
   - 1 条 unit-stride load
   - 1 条 stride load
   - 且二者都能证明 front-door、transport、completion、metadata 闭环
4. `VectorMemoryModel.expand()` 能正确处理：
   - active / inactive / tail / prestart
   - positive / zero / negative stride
5. 若 writeback `data` compare 尚未稳定，则文档与 testcase 必须明确它仍属 known gap，而不是静默跳过。
6. vector coverage collector 若未落地，也必须在文档中保留为未完成项，而不是默认已接入。
7. 所有 known gap 均已显式挂出，没有把未实现能力伪装成“已支持”。

## 12. 推荐实施顺序

建议按下面顺序推进，而不是并行摊大饼：

1. 先补 `VectorMemTxn` / `VectorElementAccess` / `VectorMemResult`
2. 再补 `VectorMemoryModel.expand()`
3. 再接 `VectorBackendFacade`
4. 再接 `VectorMemMonitor`
5. 再写 unit-stride smoke testcase
6. 再补 stride / mask / non-zero `vstart`
7. 最后接最小 coverage collector

这样做的原因是：

- 若没有稳定 schema，facade 和 model 都会反复改形状
- 若没有 model，testcase 会开始复制 expected 逻辑
- 若没有 facade，testcase 会退化成 pin-level 脚本
- 若没有 monitor，coverage 和结果解释会严重不足

## 13. 与角色边界的建议配合

第一阶段天然是一个跨角色任务，但建议按主次拆分：

- `Bridgekeeper` / `守桥人（env/monitor/facade）`
  - 负责 vector facade / monitor / env 组装
- `Oracle` / `神谕者（model/coverage）`
  - 负责 vector memory model / vector coverage
- `Pathfinder` / `探路者（testcase/sequence）`
  - 负责第一批 unit-stride / stride directed case
- `Captain` / `船长（integrator/owner）`
  - 负责文档、任务拆分、验收口径与合并顺序

如果必须由单人或单 agent 先打底，优先顺序也应是：

- 先搭骨架与文档
- 再落最小 directed case
- 最后扩矩阵

## 14. 结论

第一阶段的成功标准不是“已经做完向量访存验证”，而是：

- 已经把当前 MemBlock 验证环境从 scalar-only 推进到 vector-capable 的最小骨架；
- 已经证明 unit-stride / basic stride / mask / non-zero `vstart` 可以在真实 DUT 上形成稳定闭环；
- 已经把后续的 indexed / FOF / segment / mixed-path 留在清晰的下一阶段，而不是在第一阶段留下大面积半成品。

只要按本文档的边界推进，后续向量访存能力就能在现有 MemBlock 真实 DUT 环境上自然扩展，而不会演变成一套脱离现有架构的平行验证系统。
