# TLB/PTW Deep Functional Points

## 1. Document Purpose

This document describes functional points for deep TLB/PTW scenarios in the MemBlock verification environment, including multi-ASID sfence, batch fill-refill cycles, multi-port TLB miss, LLPTW queue pressure, H-extension fence, and pfTLB (L2TLB prefetcher) port scenarios. Each functional point is described using the Input-Process-Output (IPO) pattern.

Related documents:
- `src/test/python/MemBlock/docs/mmu_env_design_and_usage.md`
- `src/test/python/MemBlock/docs/mmu_llptw_todo.md`
- `src/test/python/MemBlock/docs/mm_u_h_extension_cases.md`
- `src/test/python/MemBlock/tests/test_MemBlock_mmu_tlbfa_deep.py`
- `src/test/python/MemBlock/tests/test_MemBlock_mmu_tlbfa_pftlb.py`

---

## 2. Multi-ASID / Sfence Functional Points

### SCN-TLB-001: sv39 multi-ASID / sfence flush matrix

该功能点验证 DTLB 在多 ASID 环境下 sfence.vma 指令的冲刷精确性。sfence.vma 是 RISC-V 中用于 TLB 一致性维护的关键指令，其 rs1（地址）和 rs2（ASID）两个操作数共同编码了冲刷粒度。当多个 ASID 映射到同一 VA 时，TLB 中同时存在属于不同地址空间的翻译条目，sfence.vma 必须按 ASID 准确区分目标条目。验证通过切换 ASID 或发送特定 ASID/地址组合的 sfence 指令，观察 TLB hit/miss 状态的变化来确认冲刷效果。关键边界条件包括：全局映射（G-bit 置位）不受 ASID 过滤影响、零 ASID（rs2=0）的语义与非零 ASID 的区别、以及冲刷后同一 VA 应重新触发 PTW refill 而非沿用旧的 TLB 条目。

| Aspect | Description |
|--------|-------------|
| **ID** | `SCN-TLB-001` |
| **Name** | sv39 multi-ASID sfence flush matrix |
| **Category** | TLB flush coherence |
| **DUT scope** | DTLB, PTW, sfence.vma logic |

| IPO Stage | Details |
|-----------|---------|
| **Input** | Switch ASID for the same VA, or execute `sfence.vma` with specific ASID/address combinations. |
| **Process** | `sfence.vma` precisely flushes TLB entries according to four `rs1(addr)`/`rs2(asid)` combinations. After flush, the same VA triggers TLB miss -> PTW -> refill. |
| **Output** | Global flush (all addr, all asid): all entries re-miss. Precise flush (addr, asid): only target page re-misses; other pages remain hit. |

---

### SCN-TLB-002: sfence.vma four rs1/rs2 combinations

该功能点聚焦 sfence.vma 的四种 rs1/rs2 编码组合，验证 TLB 冲刷粒度从全量到精确逐级递减的正确性。四种组合为：(rs1!=0, rs2=asid) 按指定 ASID 冲刷所有地址、(rs1=0, rs2=0) 仅冲刷 ASID=0 的非全局条目、(rs1=addr, rs2=0) 按精确地址冲刷所有 ASID、(rs1=addr, rs2=asid) 按精确地址和精确 ASID 的最细粒度冲刷。验证需预先建立多个不同 ASID 和不同 VA 的翻译条目，然后依次执行每种组合的 sfence，确认只有目标条目重新触发 miss，非目标条目仍保持 hit 状态。最细粒度冲刷的验证挑战在于确保击中且仅击中目标翻译条目——既不能遗漏也不能过度冲刷。该验证对于多进程操作系统场景下的 TLB 管理正确性至关重要。

| Aspect | Description |
|--------|-------------|
| **ID** | `SCN-TLB-002` |
| **Name** | sfence.vma with all four rs1/rs2 encoding combinations |
| **Category** | TLB flush granularity |
| **DUT scope** | DTLB, sfence decode logic |

| IPO Stage | Details |
|-----------|---------|
| **Input** | Establish multiple translations (different ASIDs, different VAs). Execute `sfence.vma` with all four `rs1`/`rs2` combinations. |
| **Process** | `rs1!=0,rs2=asid`: flush specific ASID, all addresses. `rs1=0,rs2=0`: flush ASID=0 non-global entries only. `rs1=addr,rs2=0`: precise address flush. `rs1=addr,rs2=asid`: finest-granularity flush. |
| **Output** | After each combination, only target entries re-miss. Non-target entries remain hit. |

---

### SCN-TLB-003: batch fill + sfence refill cycle

该功能点验证 DTLB 在满容量填充-全冲刷-重填充-再冲刷的完整生命周期中的行为正确性。第一轮通过 48 个以上不同页面的翻译 load 将 DTLB 填至接近满容量，触发替换策略；随后执行全量 sfence 冲刷使所有条目失效；第二轮通过相同的 PTW 路径重填 TLB；第三轮再次冲刷并验证循环可重复性。该场景的核心验证目标包括：DTLB 替换策略的公平性（不会因替换抖动导致某些页面始终 miss）、PTW 在连续 refill 场景下不出现队列溢出或死锁、以及多次 fill-flush-refill 周期后 DUT 不会累积内部错误状态。验证的边界条件包括 DTLB 完全填满后新 miss 触发的 eviction 行为、全量 sfence 后所有 entry 的 valid 位清零时序、以及大量连续 PTW refill 请求对 LLPTW 队列的压力。

| Aspect | Description |
|--------|-------------|
| **ID** | `SCN-TLB-003` |
| **Name** | batch fill + sfence refill cycle |
| **Category** | TLB fill/replacement |
| **DUT scope** | DTLB, PTW, sfence logic |

| IPO Stage | Details |
|-----------|---------|
| **Input** | 48+ translated loads from different pages to fill DTLB -> sfence full flush -> refill -> verify hit. |
| **Process** | First round fills many TLB entries. Full sfence invalidates all entries. Second round refills using the same PTW. Third round proves cycle repeatability. |
| **Output** | All three fill-flush-refill cycles execute correctly. Load writeback data is correct after each refill round. |

---

## 3. Multi-Port / Concurrent TLB Functional Points

### SCN-TLB-004: three-port same-cycle TLB miss + store refill

该功能点验证 DTLB 三 load 端口在同周期同时 miss 的并发处理能力，以及 store 侧 STA/STD 触发独立 refill 的行为。DTLB 提供三个 load 请求端口，当三条 load 在同一周期访问三个不同 VA 且全部 miss 时，TLB 需要在三个端口上并行处理 miss 请求并触发三次独立的 PTW refill。同时，STA/STD 两个 store 端口也各自具有 TLB 访问能力，store 触发的 miss 通过独立的 refill 路径完成，携带 S1/S2 两阶段翻译的完整 refill 元数据。验证的关键观测在于三端口 miss 的 refill 完成时序是否存在饥饿或活锁、store 侧 refill 的元数据是否完整、以及 load 与 store 的 refill 请求在 LLPTW 队列中的合并与调度。

| Aspect | Description |
|--------|-------------|
| **ID** | `SCN-TLB-004` |
| **Name** | three-port simultaneous TLB miss with store-side refill |
| **Category** | concurrent TLB access |
| **DUT scope** | DTLB three load ports, STA/STD store ports, PTW |

| IPO Stage | Details |
|-----------|---------|
| **Input** | Three load lanes access three different VAs in the same cycle, all TLB miss. Or multiple translated stores trigger TLB miss via STA/STD simultaneously. |
| **Process** | Three load requestors issue TLB miss requests in the same cycle. TLB processes each miss on its three ports independently -> PTW -> refill. Store-side STA/STD requestors independently trigger TLB refill. |
| **Output** | Three-port misses complete refill independently. Store-side refill carries correct S1/S2 refill metadata. |

---

### SCN-TLB-005: LLPTW (queue pressure / duplicate / fault)

该功能点覆盖 LLPTW（Last-Level Page Table Walker）的深层次行为，包括队列压力、重复合并和故障处理三类场景。队列压力场景通过两个周期内注入 6 个不同页面的翻译 load 来填满 LLPTW entry queue，测试超量请求的反压和排队机制。重复合并场景向 LLPTW 发送同一 VA 的多次请求，验证 LLPTW 的 duplicate detection 能否将相同 VA 的请求合并为单个 entry，避免重复的 PTW 访问。故障场景构造 VS-stage 翻译故障、G-stage 翻译故障和 PMP 故障，验证 LLPTW 在各阶段故障下的 exception bit 和 gpaddr 正确设置。验证的关键边界包括队列满时的 backpressure 信号时序、duplicate entry 的 refill 完成后的广播通知机制、以及多级故障叠加时 LLPTW 的优先级处理逻辑。

| Aspect | Description |
|--------|-------------|
| **ID** | `SCN-TLB-005` |
| **Name** | LLPTW queue pressure, duplicate merge, and fault scenarios |
| **Category** | LLPTW deep |
| **DUT scope** | LLPTW entry queue, duplicate detection, fault handling |

| IPO Stage | Details |
|-----------|---------|
| **Input** | Inject 6 translated loads from different pages in two cycles (fill LLPTW entry queue). Or repeat the same VA (duplicate merge). Or construct fault scenarios (VS-stage/G-stage/PMP). |
| **Process** | New requests wait when LLPTW queue is full. Same-VA repeated requests merge into a duplicate entry. Faults are handled at the LLPTW level (first-stage/last-stage/PMP). |
| **Output** | All loads complete under queue pressure. Duplicate merging is correct. Fault `gpaddr`/exception bits are correctly set. |

---

### SCN-TLB-006: H-extension fence (hfence.vvma / gvma)

该功能点验证 H-extension 提供的两级 fence 指令在不同粒度下的独立控制能力。在 Sv39 两阶段翻译模式下，hfence.vvma 操作 VS-stage TLB（客户机虚拟地址到客户机物理地址），hfence.gvma 操作 G-stage TLB（客户机物理地址到监督者物理地址）。每条 fence 指令都支持地址粒度和 ASID/VMID 粒度的独立控制，形成四维的冲刷组合空间。验证的核心在于确认 hfence.vvma 不影响 G-stage、hfence.gvma 不影响 VS-stage，即两级 fence 的控制域完全独立。验证还覆盖精确冲刷（指定 addr+ASID/VMID）不过度冲刷其他条目的行为，以及两阶段翻译场景下 fence 执行后后续翻译的重新建立过程。

| Aspect | Description |
|--------|-------------|
| **ID** | `SCN-TLB-006` |
| **Name** | H-extension fence: hfence.vvma and hfence.gvma |
| **Category** | H-extension TLB coherence |
| **DUT scope** | VS-stage TLB, G-stage TLB, hfence logic |

| IPO Stage | Details |
|-----------|---------|
| **Input** | Under two-stage Sv39, execute `hfence.vvma` (VS-stage fence) or `hfence.gvma` (G-stage fence), covering all/specific address and all/specific ASID/VMID combinations. |
| **Process** | `hfence.vvma` flushes VS-stage TLB. `hfence.gvma` flushes G-stage TLB. Each fence independently controls flush granularity (address x ASID/VMID). |
| **Output** | `hfence.vvma` only affects VS-stage translations. `hfence.gvma` only affects G-stage translations. Precise flush does not over-flush. |

---

### SCN-TLB-007: pfTLB (L2TLB prefetcher port)

该功能点验证 L2TLB（TLBFA_2）的 pfTLB 预取器端口的请求/响应通路。pfTLB 是 L2TLB 的一个独立端口，用于预取器向 L2TLB 发送 VA 查询请求，预取 miss 后触发 PTW refill 并将结果存入 L2TLB。验证通过 PftlbAgent 驱动 pfTLB 端口的请求和响应信号，批量查询多个页面（最多 32 个 VA）的翻译信息。当前该功能点的 PTW 路径标记为 xfail，意味着 pfTLB miss 后的 PTW refill 在 DUT 中存在已知问题，但端口的连通性（request/response 握手）已验证正常。验证还覆盖两阶段翻译模式下 pfTLB 查询的正确性、sfence/hfence 对 pfTLB 缓存条目的冲刷语义、以及 PftlbAgent 的 reset 清除正确性。

| Aspect | Description |
|--------|-------------|
| **ID** | `SCN-TLB-007` |
| **Name** | pfTLB L2TLB prefetcher request/response port |
| **Category** | L2TLB pfTLB port |
| **DUT scope** | TLBFA_2, pfTLB port, PTW |

| IPO Stage | Details |
|-----------|---------|
| **Input** | Drive L2TLB (TLBFA_2) pfTLB request/response port via `PftlbAgent`, sending VA queries. |
| **Process** | pfTLB miss -> PTW refill -> hit. Batch query multiple pages (32 VA). Two-stage translation pfTLB query. `sfence`/`hfence` impact on pfTLB. |
| **Output** | (Partial coverage, PTW path is xfail) pfTLB port sends/receives normally. Agent reset clears correctly. Connectivity is maintained before/after `sfence`. |

---

## 4. Revision History

| Date | Change |
|------|--------|
| 2026-05-07 | Initial creation. |
