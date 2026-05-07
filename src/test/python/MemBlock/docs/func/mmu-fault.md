# MMU Fault Functional Points

## 1. Document Purpose

This document describes functional points for MMU fault scenarios in the MemBlock verification environment, including translation faults, PMP access faults, fault superposition, PMP boundary/restore, and the TLB/PMP access fault combination matrix. Each functional point is described using the Input-Process-Output (IPO) pattern.

Related documents:
- `src/test/python/MemBlock/docs/mmu_fault_directed_cases.md`
- `src/test/python/MemBlock/docs/mmu_env_design_and_usage.md`
- `src/test/python/MemBlock/tests/test_MemBlock_mmu_fault.py`
- `src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py`

---

## 2. Translation Fault Functional Points

### SCN-FLT-001: load translation fault (TLB hit)

该功能点验证 DUT 在 TLB hit 情况下直接返回翻译故障（不触发 PTW）的正确行为。验证通过两步操作实现：首先发送 priming load 使故障 PTE 的翻译结果进入 TLB，随后对同一 VA 再次发起 load 请求。当第二个 load 在 TLB 中命中时，DUT 应直接根据 TLB 中存储的故障标记返回 LOAD_PAGE_FAULT 异常，而不需要再次发起 PTW 请求。关键观测点包括：异常类型编码为 LOAD_PAGE_FAULT 而非 LOAD_ACCESS_FAULT、PTW 侧未观察到任何请求（确认故障来自 TLB 快照而非 PTW 重查）、以及 fault 源 load 不应退化为 memoryViolation 或产生 outer 访存请求。该场景的验证价值在于确认 TLB 能够正确缓存 PTE 的故障状态并在后续 hit 时快速重放故障，这是 TLB 设计的关键性能特性。

| Aspect | Description |
|--------|-------------|
| **ID** | `SCN-FLT-001` |
| **Name** | load translation fault on TLB hit |
| **Category** | translation fault |
| **DUT scope** | DTLB, LoadUnit, exception writeback |

| IPO Stage | Details |
|-----------|---------|
| **Input** | Prime load and main load both access a leaf PTE configured with `PTE_MODE_PAGE_FAULT`. The second access hits the TLB. |
| **Process** | On TLB hit, the DUT directly returns a translation fault without triggering PTW. Exception writeback carries the `LOAD_PAGE_FAULT` exception type. |
| **Output** | Exception bit is `LOAD_PAGE_FAULT`. No PTW request is observed. No `memoryViolation` occurs. |

---

### SCN-FLT-002: load PMP access fault (deny region)

该功能点验证 PMP（Physical Memory Protection）检查器在 load 的目标物理地址落入 deny 区域时生成 LOAD_ACCESS_FAULT 的能力。PMP 配置为 deny 模式的地址区域禁止任何访问，即使 PTE 翻译本身正常（页表有效且权限充足），PMP 检查器仍会在 load 的最后阶段拦截该访问。验证的关键在于区分 LOAD_ACCESS_FAULT 与上文 SCN-FLT-001 的 LOAD_PAGE_FAULT：前者因物理地址安全策略拒绝而产生，后者因页表翻译故障而产生。验证需确认 TLB hit 正常（证明翻译路径无问题）、异常准确发生在 PMP 检查阶段、以及 DUT 在产生 fault 后不会向 outer 发出任何请求（fault 纯度检查）。PMP deny 配置支持动态切换，验证还需关注 deny 区间大小（NAPOT/NAT 等编码方式）和边界对齐的精确性。

| Aspect | Description |
|--------|-------------|
| **ID** | `SCN-FLT-002` |
| **Name** | load PMP access fault within deny region |
| **Category** | PMP access fault |
| **DUT scope** | PMP checker, DTLB, LoadUnit, exception writeback |

| IPO Stage | Details |
|-----------|---------|
| **Input** | Page table is normal but the main load's final HPA falls within a PMP deny region (`PMP_MODE_DENY`). |
| **Process** | TLB hit returns a valid translation. PMP checker detects the deny region and generates `LOAD_ACCESS_FAULT`. |
| **Output** | Exception bit is `LOAD_ACCESS_FAULT`. TLB hit is normal. PMP deny correctly gates the access. |

---

## 3. Combined Fault Functional Points

### SCN-FLT-003: translation fault + PMP fault superposition

该功能点验证 translation fault 和 PMP fault 同时生效时 DUT 的异常优先级裁决行为。当某 load 的 PTE 翻译无效（如 PTE_V=0 或 PPN 非法）且目标地址同时落在 PMP deny 区域内时，DUT 需要根据内部优先级规则选择其中一种异常类型上报。当前观测表明 translation fault 在优先级上高于 PMP fault，即 DUT 优先报告 LOAD_PAGE_FAULT 而非 LOAD_ACCESS_FAULT。该优先级的确定性至关重要——验证环境需要确认该行为在多次重复下一致，且与设计规格相符。验证的关键观测包括：异常类型的一致性（多次运行结果相同）、fault 源的 trace 信息完整（gpaddr、exception bits）、以及优先级行为在 TLB hit 和 TLB miss 两种背景下的稳定性。

| Aspect | Description |
|--------|-------------|
| **ID** | `SCN-FLT-003` |
| **Name** | translation fault and PMP fault simultaneous overlap |
| **Category** | fault superposition |
| **DUT scope** | DTLB, PMP checker, exception priority logic |

| IPO Stage | Details |
|-----------|---------|
| **Input** | A load with both translation error (PTE invalid) and PMP deny in effect simultaneously. |
| **Process** | RTL evaluates according to its priority window (current observation: translation error takes priority over PMP deny). |
| **Output** | Exception bit resolves to either translation fault or access fault depending on RTL priority. Verifies priority ordering determinism. |

---

### SCN-FLT-004: PMP deny region boundary + restore

该功能点验证 PMP deny region 动态配置场景下的边界行为和状态恢复能力。验证流程分为三个阶段：首先确认 deny region 内的 load 产生 LOAD_ACCESS_FAULT 而同一 deny region 边界外的 load 正常写回；随后动态修改 PMP 配置，移除 deny 区域；最后对同一 VA 重新发起 load，确认在 PMP 恢复后能正常写回而非继续报 fault。该场景验证的是 PMP 动态重新配置（使用 csr_write 更新 pmpcfg/pmpaddr CSR）后 DUT 是否正确识别配置变化并恢复访问能力。关键观测点包括：deny 边界的精确性（紧邻边界内外地址的区别行为）、PMP 配置修改后 TLB 中缓存的旧权限信息是否被正确冲刷或更新、以及 restore 后 load 的状态恢复是否彻底（不应残存活锁或 stall）。

| Aspect | Description |
|--------|-------------|
| **ID** | `SCN-FLT-004` |
| **Name** | PMP deny region boundary hit with outside pass, then restore |
| **Category** | PMP dynamic reconfiguration |
| **DUT scope** | PMP checker, DTLB, LoadUnit |

| IPO Stage | Details |
|-----------|---------|
| **Input** | One load hits inside a PMP deny region. Another load hits outside the same deny region. Then the deny is revoked and the same VA is re-issued. |
| **Process** | Deny-region load -> access fault. Outside-region load -> normal writeback. After PMP deny is removed, same-VA load -> normal writeback. |
| **Output** | Fault/normal writeback boundary is correct. State is correctly restored after dynamic PMP reconfiguration. |

---

### SCN-FLT-005: TLB AF + PMP AF combination matrix

该功能点构建 TLB access fault 和 PMP access fault 的 2x2 组合矩阵，覆盖两种故障源在 TLB hit 和 TLB miss 背景下的所有组合情况。TLB-AF 通过设置叶 PTE 的高位 PPN 非法来触发，PMP-AF 通过目标地址落在 deny region 来触发。四种组合为：(TLB-AF=0, PMP-AF=0) 无故障基线、(TLB-AF=1, PMP-AF=0) 仅有 TLB 故障、(TLB-AF=0, PMP-AF=1) 仅有 PMP 故障、(TLB-AF=1, PMP-AF=1) 双故障叠加。所有存在故障的组合最终都收敛为 LOAD_ACCESS_FAULT 异常类型——因为 TLB access fault 与 PMP access fault 对于软件层面而言都表现为 access fault，区别于 page fault。验证在 TLB miss 背景下确认 PTW refill 成功但 access fault 仍被正确触发，在 TLB hit 背景下确认故障来自缓存的 TLB 信息。

| Aspect | Description |
|--------|-------------|
| **ID** | `SCN-FLT-005` |
| **Name** | TLB access fault and PMP access fault combination matrix |
| **Category** | fault combination matrix |
| **DUT scope** | DTLB, PTW, PMP checker, LoadUnit |

| IPO Stage | Details |
|-----------|---------|
| **Input** | Iterate over all 4 combinations of TLB-side access fault (high PPN illegal) and PMP access fault: `(TLB-AF x PMP-AF)`. Test under both TLB hit and TLB miss backgrounds. |
| **Process** | TLB-AF: leaf PTE has illegal PPN, TLB returns AF. PMP-AF: PMP deny region. All 4 combinations converge to `LOAD_ACCESS_FAULT`. |
| **Output** | Exception bit is `LOAD_ACCESS_FAULT` for all 4 combinations. Under TLB miss background, PTW refill succeeds but AF still correctly triggers. |

---

## 4. Revision History

| Date | Change |
|------|--------|
| 2026-05-07 | Initial creation. |
