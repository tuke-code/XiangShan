# MemBlock 向量访存验证方案

## 1. 文档目的

本文档基于 `docs/riscv-isa-manual/src/v-st-ext.adoc` 中的向量访存规范，结合当前 `src/test/python/MemBlock/` 真实 DUT 验证环境，设计一套面向 XiangShan MemBlock 的向量访存白盒验证方案。

本文档回答四个问题：

1. 向量访存应按哪些语义族拆分验证。
2. 当前 MemBlock 环境有哪些能力可以直接复用，哪些地方必须扩展。
3. 向量访存的 testcase / sequence / facade / monitor / model / coverage 应如何分层。
4. 这项工作应如何分阶段落地，而不是一次性做成一个无法收口的大补丁。

本文档定位是**总体方案**，不直接展开到第一阶段每个接口该如何命名、每类 helper 先做哪些最小实现。第一阶段的实施细化单列在：

- `src/test/python/MemBlock/docs/vector_mem_phase1_plan.md`

## 2. 设计目标

在现有 MemBlock 标量 load/store 真实 DUT 验证环境之上，新增一套面向 RVV vector memory operations 的验证能力，覆盖：

1. **功能正确性**
   - unit-stride / strided / indexed / segment / mask load-store
   - `vl` / `vtype` / `SEW` / `LMUL` / `EEW` / `vstart`
   - active / inactive / tail / prestart 元素语义
   - ordered vs unordered indexed 语义
   - fault-only-first 语义
2. **白盒实现语义**
   - 请求形成、队列占用、split、replay、异常恢复、drain
   - 与现有 LSQ / sbuffer / replay / TLB/PTW / PMP 路径的耦合
   - vector/scalar 混合场景下的共享资源行为
3. **覆盖率闭环**
   - 继续采用真实 DUT 驱动，不退回 mock-only proof
   - coverage 仍以 DUT 观测事实为主，不按 testcase 名称手工打点
   - 与 `coverage_summary.md` / `coverage_todo.md` 一样形成阶段性状态源

## 3. 规范语义到验证族的映射

基于 `v-st-ext.adoc`，建议把向量访存拆成 8 个验证族。

### 3.1 unit-stride load/store

对应：

- `vle*`, `vse*`
- `vlm.v`, `vsm.v`

重点语义：

- 连续地址访问
- mask 控制仅 active 元素真正访存
- tail / inactive / prestart 不应访问 memory
- 支持非零 `vstart`

这是第一批必须落地的基础族，也是最适合复用当前 MemBlock memory model 的入口。当前 Phase 1 以这一族和基础 stride / `vstart` 为主闭环，不把 mask packed 语义并入首批实现。

### 3.2 constant-stride load/store

对应：

- `vlse*`, `vsse*`

重点语义：

- stride 可正、可负、可零
- 零 stride 不等于“只访问一次”；每个 active element 仍是一次架构访问
- 负 stride 地址按倒序展开
- 与 mask / `vstart` 组合

### 3.3 indexed unordered / ordered

对应：

- `vluxei*`, `vsuxei*`
- `vloxei*`, `vsoxei*`

重点语义：

- offsets 以 byte 为单位
- offset 相对 XLEN 的 zero-extension / truncation
- ordered 指令要求元素访问次序保序
- unordered 指令不能假设访问顺序

### 3.4 fault-only-first load

对应：

- `vle*ff.v`

重点语义：

- element 0 fault：trap，`vl` 不缩
- element > 0 fault：`vl` 允许 trim
- 只保证 first-fault 语义
- 与 page fault / access fault / PMP 组合

### 3.5 segment load/store

对应：

- `vlseg*`, `vsseg*`
- `vlsseg*`, `vssseg*`
- `vluxseg*`, `vloxseg*`, `vsuxseg*`, `vsoxseg*`

重点语义：

- `nf` 个 field 组成一个 segment
- `vstart` 的语义单位是 segment index，而不是单个 field
- mask 作用在 segment index 上
- field 间地址关系必须正确

### 3.6 mask load/store

对应：

- `vlm.v`, `vsm.v`

重点语义：

- mask register bit packing / unpacking
- `evl = ceil(vl/8)` 的存储语义
- 不能简单当成普通 byte-vector load/store 处理

### 3.7 restart / `vstart`

所有 vector memory ops 都应横向覆盖：

- 非零 `vstart`
- prestart element 不应访存
- resumed execution 从 `vstart` 开始
- trap / replay / restart 后行为一致

### 3.8 cross-boundary / exception / system interaction

包括：

- cross-16B / cross-32B beat / cross-64B line
- cross-page
- misalign
- TLB miss / page fault / access fault / PMP deny
- uncache / MMIO / nc path
- 与标量请求并发

## 4. 当前环境的复用点与缺口

## 4.1 可直接复用的部分

### 4.1.1 真实 DUT 运行骨架

- `MemBlock_api.py`
- `MemBlock_env.py`

可继续复用：

- DUT 创建
- waveform / coverage 打开
- reset / clock / backend sync

### 4.1.2 memory reference 与 transport responder

- `memory_model.py`
- `model/ref_memory.py`
- `model/transport_responder.py`

可继续复用：

- preload memory
- dcache / outer response 注入
- 最终 memory image 比较

### 4.1.3 sequence 组织方式

- `sequences/memblock_sequences.py`

可复用其模式：

- sequence result dataclass
- reusable scenario helper
- testcase 只描述场景与断言，不直接写大量 pin-level 时序

### 4.1.4 coverage 设计原则

- `model/rob_coverage.py`
- `docs/coverage_summary.md`
- `docs/coverage_todo.md`

可延续的原则：

- coverage 由 DUT 事实驱动
- 已知模型缺口要显式挂出，避免 coverage 报告误导
- coverage 补强优先围绕真实低覆盖/高价值路径，而不是机械堆 case

## 4.2 当前明确缺口

### 4.2.1 request API 目前是 scalar-only

当前 `request_apis.py` 仅覆盖：

- `send_load`
- `send_store`
- `issue_scalar_load`
- `issue_scalar_sta`
- `issue_scalar_std`

因此第一缺口不是 testcase 数量，而是必须先引入：

- vector txn schema
- vector backend facade
- vector issue / execution sequence

### 4.2.2 scoreboard 当前是 scalar transaction 粒度

当前 `model/scoreboard.py` 的核心对象仍是：

- `ExpectedLoad`
- `PendingStore`

它们的核心字段是 scalar 语义：

- `addr`
- `size`
- `mask`
- `expected_data`

向量访存至少需要升级为：

- instruction-level vector op
- element-level memory access record
- segment-level field access record
- active/inactive/tail/prestart aware compare

### 4.2.3 缺少 vector-specific monitor / coverage

当前环境已有 writeback/store/status monitor，但没有清晰的 vector memory fact collector。

因此必须新增：

- vector memory request / completion / exception / replay 监控
- `vl` / `vstart` side effect 监控
- vector coverage collector

## 5. 推荐的分层扩展

建议在现有 MemBlock 环境之上新增 4 层，而不是把逻辑直接塞进 testcase。

## 5.1 transaction 层：向量事务与展开结果

建议新增：

### `VectorMemTxn`

描述一条向量访存指令的架构意图，例如：

- `opcode_class`
- `is_load`
- `base_addr`
- `stride`
- `index_eew`
- `sew`
- `lmul`
- `nf`
- `vl`
- `vstart`
- `mask_bits`
- `tail_policy`
- `mask_policy`
- `expected_exception`
- `expected_trimmed_vl`

### `VectorElementAccess`

描述 reference 展开后的每个元素/field 访问：

- `element_idx`
- `field_idx`
- `active`
- `is_tail`
- `is_prestart`
- `addr`
- `size_bytes`
- `byte_mask`
- `expected_data`
- `should_access_memory`
- `should_raise_exception`

### `VectorMemResult`

sequence / testcase 使用的结果对象，例如：

- 实际访存日志
- replay / trap / writeback 观测
- `vl` / `vstart` side effect
- split / drain / exception 汇总

核心思想是：

- 一条 vector instruction
- 先展开成 element / segment 级 reference access plan
- 再与 DUT 事实对账

## 5.2 facade 层：vector backend facade

建议新增：

- `send_vector_load(txn)`
- `send_vector_store(txn)`
- `execute_vector_plan(plan)`

职责：

1. 驱动 vector issue 接口或等价前端入口
2. 配置与恢复：`vl` / `vtype` / `vstart` / mask register
3. 协同 commit / replay / exception 相关控制

不建议在 testcase 中直接逐拍 drive vector 相关信号。

## 5.3 model 层：vector memory model facade

建议在 `memory_model.py` 之上新增：

- `VectorMemoryModel`

职责：

1. 根据 `VectorMemTxn` 展开 reference access plan
2. 逐 element/field 生成 expected load data 或更新 refmem
3. 处理：
   - mask
   - active / inactive / tail / prestart
   - `vstart`
   - segment `nf`
   - ordered / unordered
   - FOF trim `vl`
4. 尽量复用现有：
   - `RefMemory`
   - `TransportResponder`
   - 最终 drain / memory image compare

## 5.4 monitor / coverage 层：vector fact collector

建议新增：

### `VectorMemMonitor`

采集：

- vector memory request 发出
- 实际 memory request 序列
- response / replay / exception
- `vl` / `vstart` / trap side effect
- split / multi-beat / drain 事实

### `VectorCoverageCollector`

参考 `rob_coverage.py` 风格建立三组 coverage：

1. `MemBlock.Vector.ObservedBehavior`
2. `MemBlock.Vector.CurrentModel`
3. `MemBlock.Vector.KnownModelGaps`

建议点位示例：

- unit-stride load/store observed
- stride zero / negative observed
- indexed unordered / ordered observed
- masked inactive-skip observed
- non-zero `vstart` observed
- segment `nf>1` observed
- FOF trim observed
- vector replay observed
- vector cross-page observed

## 6. testcase 与 sequence 组织建议

建议不要按“指令名一个文件”机械铺开，而是按语义族 + 深度等级组织。

## 6.1 第一层：smoke / bring-up

建议新增：

- `tests/test_MemBlock_vector_unit_stride.py`
- `tests/test_MemBlock_vector_stride.py`
- `tests/test_MemBlock_vector_indexed.py`
- `tests/test_MemBlock_vector_fof.py`

目标：

- 打通基础链路
- 不追求大矩阵
- 尽早证明 env/facade/model 的方向正确

## 6.2 第二层：directed matrix

### unit-stride matrix

轴：

- SEW: 8/16/32/64
- LMUL: 1/2/4
- mask density: full / sparse / checkerboard
- `vl`: 0 / 1 / small / boundary
- `vstart`: 0 / middle / last
- address: aligned / misaligned / cross-line / cross-page

### stride matrix

轴：

- stride: `+SEW`, `+2*SEW`, `0`, `-SEW`
- mask
- tail
- replay / miss / page boundary

### indexed matrix

轴：

- ordered / unordered
- duplicate address
- overlapping offsets
- offset truncation / zero-extension boundary
- cross-line / cross-page

### segment matrix

轴：

- `nf=2/3/4/8`
- field size 8/16/32/64
- masked segment skip
- `vstart` by segment index
- partial segment exception

## 6.3 第三层：stress / mixed-path

建议专项：

1. vector store queue / sbuffer 深状态
2. vector replay / RAW / RAR
3. vector uncache / MMIO / nc
4. vector translation / permission
5. vector/scalar 混合 traffic

## 7. 参考模型的关键建模原则

## 7.1 统一元素分类

对每个 element/segment index 先分类：

- `prestart`: `idx < vstart`
- `body`: `vstart <= idx < vl`
- `tail`: `idx >= vl`
- `inactive`: body 中但 mask=0
- `active`: body 且 mask=1

统一规则：

- 只有 `active` 元素允许访存或触发异常
- `inactive` / `tail` / `prestart` 不应产生 memory access
- store 对这些非 active 元素不应改 memory
- load 对这些非 active 元素的寄存器结果按当前 policy 决定是否比对，但 memory side effect 不应存在

## 7.2 ordered vs unordered

### unordered indexed

只检查：

- 最终 architectural data / memory effect 正确
- 不强约束访问顺序

### ordered indexed

至少需要额外检查一项：

1. 观测到的 memory request 顺序与 element 顺序一致；或
2. 有足够强的等价白盒事实可证明保序

若当前 monitor 看不到细粒度顺序，应把该项列入 known gap，而不是硬说已闭环。

## 7.3 fault-only-first

FOF 不能只比较 memory data，必须同时比较：

- trap 是否发生
- `vl` 是否被 trim
- trim 位置是否符合 first-fault 语义
- element 0 fault 时 `vl` 不变

建议单独实现 FOF reference helper，不与普通 vector load 共用同一条简化路径。

## 7.4 segment

segment reference 展开必须以 segment index 为主循环、field index 为内循环，才能正确表达：

- `vstart` 作用在 segment 维度
- mask 作用在 segment 维度
- `nf` field 间地址关系

## 8. 与当前 coverage 短板的结合方式

向量访存方案不应脱离 `coverage_summary.md` 中当前 MemBlock 的现实短板，反而应主动对齐。

## 8.1 store 深状态

vector store 天然有机会继续拉高：

- `NewStoreQueue.sv`
- `SbufferData.sv`
- `Sbuffer.sv`
- `StoreMisalignBuffer.sv`

优先高价值场景：

- partial active-mask vector store
- cross-line / cross-page vector store
- segment store 跨 beat
- vector store 后 overlap vector/scalar load

## 8.2 replay / VLQ 深状态

vector load 的 overlap / restart / replay 非常适合继续拉升：

- `LoadQueueReplay.sv`
- `VirtualLoadQueue.sv`
- `LoadQueueRAW.sv`
- `LoadQueueRAR.sv`

## 8.3 translation / exception

FOF、cross-page、indexed scatter 场景也更适合继续命中：

- `TLBFA*`
- `PtwCache.sv`
- `PMP.sv`
- `ExceptionInfoGen.sv`

## 9. 分阶段落地建议

## 9.1 Phase 0：接口与可达性验证

目标：

- 明确 vector memory issue / retire / exception 的 DUT 接口与观测点
- 建立最小 `VectorMemTxn`
- 打通 unit-stride load/store smoke

产物：

- vector facade 雏形
- vector monitor 雏形
- 最小 smoke testcase

## 9.2 Phase 1：基础功能闭环

目标：

- unit-stride / stride / mask / non-zero `vstart`
- element-level reference plan
- vector observed coverage 雏形

Phase 1 的实现细化见：

- `src/test/python/MemBlock/docs/vector_mem_phase1_plan.md`

## 9.3 Phase 2：复杂寻址与异常

目标：

- indexed ordered/unordered
- fault-only-first
- cross-line / cross-page
- replay / translation / exception

## 9.4 Phase 3：segment 与深状态

目标：

- `nf>1` segment family
- vector/scalar mixed-path
- queue / sbuffer / replay 深状态

## 10. 推荐的首批 directed case

如果只选第一批最值得落地的 directed case，建议优先：

1. unit-stride vector load，full active
2. unit-stride vector store，full active
3. masked unit-stride store，checkerboard mask
4. non-zero `vstart` unit-stride load
5. stride = 0 load
6. stride = -SEW load/store
7. unordered indexed load with shuffled offsets
8. ordered indexed store with monotonic offsets
9. FOF load：element 0 page fault
10. FOF load：element > 0 page fault with `vl` trim
11. cross-line vector store + overlap load
12. vector store burst + delayed flush/drain

## 11. 目录建议

建议保持 MemBlock 当前分层：

- `tests/test_MemBlock_vector_unit_stride.py`
- `tests/test_MemBlock_vector_stride.py`
- `tests/test_MemBlock_vector_indexed.py`
- `tests/test_MemBlock_vector_fof.py`
- `tests/test_MemBlock_vector_segment.py`
- `tests/test_MemBlock_vector_mixed_scalar.py`
- `sequences/vector_mem_sequences.py`
- `model/vector_memory_model.py`
- `model/vector_coverage.py`
- `monitors/vector_mem_monitor.py`
- `agents/vector_backend_facade.py`
- `docs/vector_mem_phase1_plan.md`

## 12. 风险与边界

1. ordered indexed 的“外部可见顺序”可能暂时不好观测。
2. FOF 对 `vl` / `vstart` side effect 的观测要求高。
3. vector path 可能并不完全复用 scalar issue/commit 面，因此第一阶段工作量更可能集中在 facade / monitor / schema，而不是 testcase 数量。
4. MMIO / nc vector memory 若当前 DUT 未支持，应通过 capability probe 明确列入 known gap，而不是强塞进 P0。

## 13. 结论

对于当前 `src/test/python/MemBlock/` 环境，最合适的向量访存验证路线不是“先堆一批向量 testcase”，而是：

1. 先补最小 vector transaction / facade / monitor / model 骨架。
2. 以 unit-stride + mask + `vstart` 建立第一批真实 DUT 闭环。
3. 再逐步推进 stride / indexed / FOF / segment。
4. 最后把 mixed-path、translation、queue 深状态并入 coverage 驱动补强。

这样做既复用了当前 MemBlock 的真实 DUT 验证资产，也避免把向量访存做成一套与现有环境平行、无法共享维护成本的新系统。
