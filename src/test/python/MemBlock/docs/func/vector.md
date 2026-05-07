# Vector Memory Functional Points

## 1. Document Purpose

This document describes functional points for vector memory operations (load/store) in the MemBlock verification environment. Each functional point is described using the Input-Process-Output (IPO) pattern to clearly define the verification scope, DUT behavior, and observability criteria.

Related documents:
- `src/test/python/MemBlock/docs/vector_mem_plan.md`
- `src/test/python/MemBlock/docs/vector_mem_phase1_plan.md`
- `src/test/python/MemBlock/tests/` (vector-related testcases)

---

## 2. Basic Vector Load Functional Points

### BAS-VEC-001: vector unit-stride load (strict data compare)

向量单元步长 load 是向量访存最基础的场景，其验证核心在于确认 VectorLoadUnit 能够正确地将一条向量 load 指令展开为 vl 个独立的 element 访存请求。每个 element 遵循标量 load 的数据通路，独立经过 DCache 或 outer 路径完成数据读取，最终由 VLMergeBuffer 将各 element 的写回结果按 element index 顺序合并，写入向量寄存器文件。验证的关键观测点在于 VectorMemMonitor 捕获的写回信息：data 字段提供每拍写回的 256bit 数据负载，pdest 标识目标向量寄存器编号，vec_wen 指示该拍是否为有效向量写回，observed_vl 和 observed_vstart 则反映 DUT 实际执行的向量长度和起始位置。由于向量 load 涉及多 element 展开和乱序写回，验证必须确保 data 中各 element 的位置与语义地址严格对应，不能出现 element 窜位或数据错乱。边界条件包括 vl=0 时不应有任何写回、vstart=vl 时所有 element 被跳过，以及跨页边界 element 触发 PTW refill 后仍能正确合并。

| Aspect | Description |
|--------|-------------|
| **ID** | `BAS-VEC-001` |
| **Name** | vector unit-stride load with strict data compare |
| **Category** | basic vector load |
| **DUT scope** | VectorLoadUnit, VLMergeBuffer, DCache, outer memory |

| IPO Stage | Details |
|-----------|---------|
| **Input** | Enqueue a `VectorMemTxn` via `enqLsq` with opcode=`unit_stride`, `vl=N`, `sew=8/16/32/64`, `vstart=0`, mask=all-ones. Issue via `VectorIssueAgent`. |
| **Process** | `VectorLoadUnit` expands the vector load into `vl` element loads. Each element independently traverses the DCache/outer path. `VLMergeBuffer` merges all elements and writes back to the vector RF. |
| **Output** | `VectorMemMonitor` observes writeback (`data`/`pdest`/`vec_wen`/`observed_vstart`/`observed_vl`/`debug_paddr`). Data is compared element-by-element against preloaded golden values. |

---

### BAS-VEC-002: vector strided load (positive/zero/negative stride)

向量 stride load 在单元步长基础上增加了地址跨步变化，验证重点是 VectorLoadUnit 对 stride 参数的解析能力和 VLMergeBuffer 对非连续地址 element 的合并正确性。正 stride 时各 element 地址按 base + i * stride 递增，验证需确认 element 数据与地址的对应关系；零 stride 时所有 element 指向同一地址，验证需确认各 element 读取到相同数据且 VLMergeBuffer 不因地址重复而丢失写回拍数；负 stride 时地址递减，验证需确认写回数据的 element 排列仍按 element index 而非地址升序。stride 场景对 VLMergeBuffer 的合并逻辑提出了更高要求，因为 element 写回的完成顺序可能与地址顺序不一致，VLMergeBuffer 必须按照 element index 重新排序后再写回。验证的边界条件包括 stride 值跨越页边界、stride 过大导致地址溢出，以及 stride 与 mask 组合时部分 element 被跳过。

| Aspect | Description |
|--------|-------------|
| **ID** | `BAS-VEC-002` |
| **Name** | vector strided load with various stride values |
| **Category** | basic vector load |
| **DUT scope** | VectorLoadUnit, VLMergeBuffer, DCache, outer memory |

| IPO Stage | Details |
|-----------|---------|
| **Input** | `VectorMemTxn` with opcode=`stride`, `vl=N`, stride=`+-offset`. Addresses computed as `base + i * stride`. |
| **Process** | Positive stride: addresses increment by `i*stride`. Zero stride: all elements share the same address. Negative stride: addresses decrement. `VLMergeBuffer` merges results from all elements. |
| **Output** | Writeback data elements are correctly ordered according to stride addresses. Zero-stride: all elements return identical data. Negative-stride: elements are correctly arranged in reverse address order. |

---

## 3. Combined Vector Functional Points

### CMB-VEC-001: vector unit-stride load (mask / port / misalign)

该功能点在基础单元步长 load 之上叠加了 mask、双发射端口和地址非对齐三种组合条件，用于验证 VectorLoadUnit 和 VLMergeBuffer 在复杂配置下的鲁棒性。Mask 机制使部分 element 被标记为不活跃，这些 element 不产生实际访存请求，但在 VLMergeBuffer 写回中仍占据对应 slot 并以零数据填充，验证需确认 mask pattern 与写回 slot 的零填充严格对应。地址非对齐场景（base 跨 16B 边界）触发 VLMergeBuffer 的 cross-16B split/merge 逻辑，验证需确认跨边界 element 的数据在合并后保持正确。Port1 发射路径对应 VLMergeBuffer 的第二写回端口，验证需确认双端口模式下 port0 和 port1 的写回数据在独立的观测通道上均保持一致。组合测试的关键在于各条件之间的交互——例如 mask + misalign 同时作用时，零填充 slot 与跨边界合并不应相互干扰。

| Aspect | Description |
|--------|-------------|
| **ID** | `CMB-VEC-001` |
| **Name** | vector unit-stride load with mask, dual-port, and misalign combinations |
| **Category** | combined vector load |
| **DUT scope** | VectorLoadUnit, VLMergeBuffer (dual port), DCache |

| IPO Stage | Details |
|-----------|---------|
| **Input** | `VectorMemTxn` with checkerboard mask (every-other element masked), or misaligned base address (cross-16B boundary), or using `port1` (second vector enqueue/issue port). |
| **Process** | Masked elements leave empty slots (zero/tail data) in writeback. Misaligned addresses trigger cross-16B split/merge. Port1 targets the second port of `VLMergeBuffer`. |
| **Output** | Masked-element writeback slots are zero. Cross-16B merged data is correct. Port1 writeback data matches port0. |

---

### CMB-VEC-002: vector unit-stride store

向量单元步长 store 验证 vector 写操作的完整通路：VectorStoreUnit 将一条 store 指令展开为 vl 个 element store，每个 element 经过 STA（地址生成）和 STD（数据生成）进入 Store Buffer，最终通过 sbuffer 或 outer 路径写回内存层次。当前该功能点的验证覆盖存在已知 DUT 限制——store 的完成可观测，但 flush/drain 后的完整闭环比较无法正常完成，具体表现为 data==0 和 dcache transport==0 的异常信号状态。该问题被标记为 xfail（预期失败），意味着验证环境能够正确检测到 DUT 行为与预期的偏差，但该偏差的根源在于 DUT 而非验证环境。当前验证策略聚焦于部分覆盖：确认 store 发射、STA/STD 握手和 SQ 入队等中间状态的正确性，而非强求最终 flush 数据一致性。向量 store 的 element 展开机制与 load 类似，区别在于数据流向是 VRF 到 sbuffer/outer，且不涉及 VLMergeBuffer 的写回合并。

| Aspect | Description |
|--------|-------------|
| **ID** | `CMB-VEC-002` |
| **Name** | vector unit-stride store |
| **Category** | combined vector store |
| **DUT scope** | VectorStoreUnit, STA/STD, SQ, sbuffer, outer memory |

| IPO Stage | Details |
|-----------|---------|
| **Input** | `VectorMemTxn` with opcode=`unit_stride`, `vl=N`, `store_data`, issued via `VectorIssueAgent`. |
| **Process** | `VectorStoreUnit` expands into `vl` element stores. Each element traverses STA/STD -> SQ -> sbuffer/outer path. |
| **Output** | (Partial coverage, known DUT bug) Store completion is observable but flush/drain may not complete. `data==0` and `dcache transport==0`. Currently retained as `xfail`. |

---

### CMB-VEC-003: vector store masked-inactive / nonzero vstart

该功能点验证向量 store 在 mask 和 vstart 两种跳过机制下的正确性。当 mask 中部分 element 为不活跃时，对应的 element 不应产生任何 store 操作请求，SQ 中的活跃 element 计数应等于 vl 减去非活跃 element 数。vstart 机制使得前 vstart 个 element 被跳过，store 从 element vstart 开始执行，验证需确认被跳过的 element 地址区间确实没有被写入。当前该功能点同样受 DUT 已知问题影响，最终 drain 数据的完整闭环验证受限，但 SQ 级别的活跃 element 计数观测可以作为部分验证手段。关键观测点包括：mask pattern 与 store 请求的对应关系是否精确、vstart 值与起始 element index 是否匹配、跳过 element 的地址区间在最终内存状态中是否确实无修改。边界条件包括 mask 全零（无任何 store）、vstart=vl（跳过所有 store）、以及 mask 与 vstart 组合时跳过与不活跃的叠加计算。

| Aspect | Description |
|--------|-------------|
| **ID** | `CMB-VEC-003` |
| **Name** | vector store with masked-inactive elements and nonzero vstart |
| **Category** | combined vector store |
| **DUT scope** | VectorStoreUnit, SQ, sbuffer, outer memory |

| IPO Stage | Details |
|-----------|---------|
| **Input** | `VectorMemTxn` with mask (partial elements inactive) or `vstart>0` (skip first `vstart` elements). |
| **Process** | Masked-inactive elements produce no store operation. Nonzero `vstart` skips the first `vstart` elements and begins storing from element `vstart`. |
| **Output** | (Partial coverage) SQ observes the correct number of active elements (`vl - vstart - inactive_count`). Final drain data matches golden. Known DUT bug affects full close-loop verification. |

---

## 4. Revision History

| Date | Change |
|------|--------|
| 2026-05-07 | Initial creation. |
