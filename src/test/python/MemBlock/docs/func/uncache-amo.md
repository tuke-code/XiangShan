# Uncacheable and AMO Functional Points

## 1. Document Purpose

This document describes functional points for uncacheable (NC/MMIO) memory operations and atomic memory operations (AMO) in the MemBlock verification environment. Each functional point is described using the Input-Process-Output (IPO) pattern to clearly define the verification scope, DUT behavior, and observability criteria.

Related documents:
- `src/test/python/MemBlock/docs/mmu_env_design_and_usage.md`
- `src/test/python/MemBlock/docs/store_todo.md`
- `src/test/python/MemBlock/tests/` (NC/MMIO/AMO-related testcases)

---

## 2. Basic Uncacheable Functional Points

### BAS-UC-001: Svpbmt cacheable / NCIO / MMIO classification

该功能点验证 LoadUnit 根据 Svpbmt 扩展中的 PBMT（Page-Based Memory Type）属性选择不同访存路径的正确性。PTE 叶节点中的 PBMT 字段编码四种内存类型：0（cacheable，正常缓存）、1（NCIO，非缓存但无 MMIO 语义）、2（NC，非缓存）、3（MMIO，内存映射 IO）。当 load 经过 TLB 翻译后，返回的 translation 结果携带 PBMT 属性，LoadUnit 根据该属性决定是否经过 DCache：cacheable 类型走 DCache 的 A（地址）/D（数据）/E（响应）标准流水级，NCIO 和 MMIO 类型旁路 DCache 直接走 outer 路径。MMIO 与 NCIO 的关键区别在于 outer 路径上的标记语义——MMIO 请求带有 mmio 标记，影响下游的总线协议行为和 fence 语义。验证的核心观测点在于正确区分不同 PBMT 值对应的 DUT 内部路径选择，确保 cacheable load 经过 DCache 而 NC/MMIO load 不经过，且 outer monitor 能够捕获正确的路径属性。

| Aspect | Description |
|--------|-------------|
| **ID** | `BAS-UC-001` |
| **Name** | Svpbmt PBMT-based load path classification |
| **Category** | basic uncacheable |
| **DUT scope** | TLB, LoadUnit, DCache, outer memory |

| IPO Stage | Details |
|-----------|---------|
| **Input** | Install Sv39 page tables with leaf PTE PBMT field set to `0` (cacheable), `1` (NCIO), `2` (NC), or `3` (MMIO). Send translated loads. |
| **Process** | TLB returns translation result with PBMT attribute. `LoadUnit` selects path based on PBMT: cacheable -> DCache, NC -> outer NCIO, MMIO -> outer MMIO. |
| **Output** | Cacheable load traverses DCache A/D/E. NCIO load traverses outer path without MMIO semantics. MMIO load traverses outer path with MMIO marking. |

---

## 3. Basic AMO Functional Points

### BAS-AMO-001: single-uop AMO (amoadd / amoswap / amoxor)

原子操作与普通 load/store 的关键区别在于其读-改-写（RMW）语义：AMO 直接在内存地址上执行原子操作，返回操作前的原值，并将操作结果写回同一地址。在 DUT 实现中，AMO 旁路 LSQ 的 store 路径，直接通过 MainPipe 执行 RMW 周期，操作结果经由 intWriteback 写回整数寄存器文件。验证通过 AtomicTxn 事务对象携带原子操作码（amoadd.w、amoswap.d、amoxor.w 等），经 backend.send() 发送到 DUT 的 atomic 端口。MemoryModel 在 reference 侧模拟相同的原子语义，确保 DUT 返回的原值和最终内存状态与 reference 一致。验证的关键观测点包括：intWriteback 返回的值是否正确（应为操作前内存值）、MemoryModel 是否准确记录了原子操作对内存的修改、以及 RMW 周期完成后内存的最终状态是否与 atomic 语义一致。

| Aspect | Description |
|--------|-------------|
| **ID** | `BAS-AMO-001` |
| **Name** | single-uop atomic memory operations |
| **Category** | basic AMO |
| **DUT scope** | MainPipe, intWriteback, load/store pipeline |

| IPO Stage | Details |
|-----------|---------|
| **Input** | `AtomicTxn` carrying atomic opcode (`amoadd.w` / `amoswap.d` / `amoxor.w`), sent via `backend.send()`. |
| **Process** | Atomic operation bypasses LSQ store path. Executes atomic RMW cycle directly through `MainPipe`. Result written back via `intWriteback`. |
| **Output** | `intWriteback` returns the pre-operation memory value (atomic semantics). `MemoryModel` records the atomic memory effect. |

---

### BAS-AMO-002: AMO mainPipe diagnostics

该功能点关注 MainPipe 内部诊断信号的可观测性，特别是 tag array 初始化窗口对 AMO 发射 readiness 的影响。DUT 启动后的一段时间内，MainPipe 的 req_ready 信号保持低电平，在此期间 tag array 正在进行初始化写入（wen=1 且 rst_cnt 单调递增）。验证通过 GetInternalSignal 机制探测 DUT 内部信号，监视 tag array 初始化状态和 mainPipe req_ready 的变化时序。关键观测点在于 req_ready 从低到高的转换时刻应恰好对应初始化完成的边界，且在此之后 AMO 能够正常发射。验证需排除 store、probe、refill 等其他请求对 req_ready 的干扰，确保 req_ready 的行为仅由 tag array 初始化状态决定。rst_cnt 的单调递增序列和 wen 标志的组合构成了初始化进度的时间参考。

| Aspect | Description |
|--------|-------------|
| **ID** | `BAS-AMO-002` |
| **Name** | AMO mainPipe internal signal diagnostics |
| **Category** | basic AMO |
| **DUT scope** | MainPipe, tag array initialization logic |

| IPO Stage | Details |
|-----------|---------|
| **Input** | Probe DUT internal signals via `GetInternalSignal`: tag array initialization state, `mainPipe req_ready`. |
| **Process** | During DUT startup, `mainPipe req_ready` remains low while tag arrays initialize. Store/probe/refill contention is excluded. After initialization completes, `req_ready` is restored. |
| **Output** | Internal signal trace shows `rst_cnt` monotonically increasing with `wen=1`. AMO can be normally issued after the tag array initialization window closes. |

---

## 4. Combined Uncacheable Functional Points

### CMB-UC-001: NC store flush (single + burst)

NC（Non-Cacheable）store 不走 sbuffer 路径，而是在 NC store shadow 中形成独立的写记录，最终通过 outer drain 路径刷出到内存。与 cacheable store 的关键区别在于，NC store 不需要经过写缓冲合并和 cache 一致性协议，而是直接旁路到 outer 总线。单个 NC store 的 flush 验证确保 NC shadow 记录正确、outer drain 事件可观测；burst（连续多个 NC store）场景则验证 shadow 队列的深度限制和 drain 顺序。当前该功能点的 function coverage 点 nc_store_flush_drain_observed 尚未被击中，说明验证环境在观测 outer drain 完成事件方面还存在 gap。验证的关键边界条件包括 NC store 与 cacheable store 交织时的 drain 顺序、NC shadow 满时的反压行为、以及 NC store 后紧跟 sfence 时的 flush 语义正确性。

| Aspect | Description |
|--------|-------------|
| **ID** | `CMB-UC-001` |
| **Name** | NC store flush with single and burst transactions |
| **Category** | combined uncacheable store |
| **DUT scope** | NC store shadow, outer drain path |

| IPO Stage | Details |
|-----------|---------|
| **Input** | `StoreTxn` with translated address having PBMT=NC, traversing NCIO path. |
| **Process** | NC store bypasses sbuffer, forming NC store shadow. Flush drains NC stores via outer path. |
| **Output** | (Partial coverage) NC store shadow is correctly recorded. Outer drain events are observable after flush. The `nc_store_flush_drain_observed` function coverage point is not yet hit. |

---

### CMB-UC-002: MMIO mixed-path flush exclusion

MMIO store 的可见语义与 cacheable store 本质不同——MMIO 写入针对的是外设寄存器而非普通内存，因此最终的 flush/compare 阶段不应将 MMIO 目标地址的数据纳入内存一致性比较。验证场景混合 PBMT=MMIO 的 store 和普通 cacheable store，确保 MemoryModel 在 final compare 中正确排除 MMIO 地址区间。具体来说，MMIO store 走 outer 路径，其写回行为由外设协议而非内存一致性模型决定，因此 DUT 的最终内存状态中 MMIO 地址区间的数据是不可预测的。MemoryModel 必须感知哪些地址属于 MMIO 范围，并在 compare 阶段跳过这些字节的检查。验证需确认 cacheable store 的数据仍然被正确比较、MMIO 地址范围被精确排除而非过度排除、以及混合同一 cacheline 中既有 MMIO 又有非 MMIO 字节时的比较边界处理。

| Aspect | Description |
|--------|-------------|
| **ID** | `CMB-UC-002` |
| **Name** | MMIO mixed-path flush exclusion from final compare |
| **Category** | combined uncacheable flush |
| **DUT scope** | MMIO outer path, MemoryModel, final flush/compare |

| IPO Stage | Details |
|-----------|---------|
| **Input** | Mixed PBMT=MMIO store and cacheable store. |
| **Process** | MMIO store traverses outer path. Cacheable store traverses sbuffer path. During flush, MMIO outer drain is excluded from final compare by `MemoryModel`. |
| **Output** | Final compare only validates cacheable bytes. MMIO bytes are not incorrectly compared. |

---

### CMB-UC-003: NC + cacheable mixed store flush

该功能点验证 NC store 和 cacheable store 混合场景下 flush 路径的正确并行性。NC store 进入 NC store shadow 后走 outer drain 路径，cacheable store 进入 sbuffer 后走 sbuffer drain 路径，flush 操作需同时触发两条 drain 路径并等待两者都完成。验证的关键观测在于两条 drain 路径是否并发执行、outer drain 和 sbuffer drain 的事件是否都能被 monitor 捕获、以及 MemoryModel 是否分别记录两条路径的写回数据。混合 flush 的验证难度在于：两条路径的完成时间可能不同步，但最终内存状态必须包含所有 store 的写入。验证边界条件包括 NC store 队列满时 cacheable store 是否仍然正常处理、两条 drain 路径中一条先完成时 flush 是否正确等待另一条、以及 sfence.vma 在混合 flush 中是否能够正确序列化所有 store 的全局可见性。

| Aspect | Description |
|--------|-------------|
| **ID** | `CMB-UC-003` |
| **Name** | NC and cacheable mixed store flush |
| **Category** | combined uncacheable flush |
| **DUT scope** | NC store shadow, sbuffer, outer drain, sbuffer drain |

| IPO Stage | Details |
|-----------|---------|
| **Input** | One PBMT=NC store and one cacheable store. |
| **Process** | NC store forms NC shadow -> outer drain. Cacheable store enters sbuffer -> sbuffer drain. Flush triggers both drain paths simultaneously. |
| **Output** | Both outer drain and sbuffer drain are observed concurrently. `MemoryModel` records each path separately. |

---

### CMB-UC-004: mmioBusy blocking younger load retire

该功能点验证 MMIO store 的 mmioBusy 信号对 younger load 提交比较的阻塞语义。当一条 older MMIO store 仍在 busy 状态（mmioBusy=1）时，其后发射的 younger cacheable load 虽然可以正常完成写回，但 commit-boundary 的 load-store 比较被 mmioBusy 阻塞，无法立即释放。验证场景中，在 MMIO store 的 mmioBusy 窗口期内发射多条 younger load，确认这些 load 的写回在比较阶段的延迟行为。关键观测点包括：load writeback 在 mmioBusy 置位期间是否正常到达、compare 完成是否在 mmioBusy 清除后才释放、以及多条 younger load 在 mmioBusy 窗口期的排队深度。该行为是 x86 风格 store-forwarding 限制在 RISC-V 实现中的体现，其验证意义在于确保 DUT 不会在 MMIO store 未完成时错误地提交 younger load 的数据。

| Aspect | Description |
|--------|-------------|
| **ID** | `CMB-UC-004` |
| **Name** | mmioBusy stalls younger load commit compare |
| **Category** | combined uncacheable ordering |
| **DUT scope** | MMIO store path, load writeback, commit-boundary compare |

| IPO Stage | Details |
|-----------|---------|
| **Input** | Older MMIO store is busy (`mmioBusy=1`). Younger cacheable load issues and writes back normally. |
| **Process** | Younger load writeback arrives normally, but commit-boundary compare is blocked by `mmioBusy`. After older MMIO store busy clears, load compare is released. |
| **Output** | Load writeback is observed before compare completion. Multiple younger loads can remain in-flight during the `mmioBusy` window. |

---

## 5. Revision History

| Date | Change |
|------|--------|
| 2026-05-07 | Initial creation. |
