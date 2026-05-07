# MemBlock Python 验证环境 — 功能覆盖总览

`src/test/python/MemBlock/` 是面向 XiangShan MemBlock 真实 DUT 的 Python 白盒验证环境。
本文档以 **功能点 (functional point)** 为主线，按基础单点功能、功能空间探索与组合、专项场景覆盖、环境 API 单测四个维度组织已验证、部分覆盖和未覆盖的验证项。
详细覆盖率数据与补强计划请见 [coverage_summary.md](docs/coverage_summary.md) 和 [coverage_todo.md](docs/coverage_todo.md)。

## 状态图例

| 图标 | 含义 |
|------|------|
| 🟢 | 已覆盖 — 有稳定的真实 DUT directed case |
| 🟡 | 部分覆盖 — 有 smoke 或基础能力证明，但矩阵未打全 |
| 🔴 | 未覆盖 — 尚无稳定的真实 DUT 用例 |

## 一、基础单点功能 (BAS)

各功能域最基础的端到端 smoke 用例，验证单条操作路径的连通性与正确性。

### Load 基础

| 编号 | 功能点 | 用例数 | 状态 | 文档 | 测试代码 |
|------|--------|--------|------|------|----------|
| BAS-LD-001 | 基础 cacheable load (burst / 饱和) | 2 | 🟢 | [Link](docs/func/load.md) | [Link](tests/test_MemBlock_scalar_load_pipeline.py) |
| BAS-LD-002 | 标量 load 宽度/掩码 (lb/lh/lw/ld) | 1 | 🟢 | [Link](docs/func/load.md) | [Link](tests/test_MemBlock_scalar_load_width.py) |
| BAS-LD-003 | 标量 fp load (fpWen boxed) | 1 | 🟢 | [Link](docs/func/load.md) | [Link](tests/test_MemBlock_fp_load.py) |

### Store 基础

| 编号 | 功能点 | 用例数 | 状态 | 文档 | 测试代码 |
|------|--------|--------|------|------|----------|
| BAS-ST-001 | 基础 cacheable store 提交 + flush + drain | 3 | 🟢 | [Link](docs/func/store.md) | [Link](tests/test_MemBlock_scalar_store_pipeline.py) |
| BAS-ST-002 | MMIO store smoke | 1 | 🟢 | [Link](docs/func/store.md) | [Link](tests/test_MemBlock_random_store.py) |
| BAS-ST-003 | CBO (cbo.zero / clean / flush / inval) | 5 | 🟢 | [Link](docs/func/store.md) | [Link](tests/test_MemBlock_scalar_store_pipeline.py) |
| BAS-ST-004 | SBufferData entry/quarter/byte 定向 | 5 | 🟢 | [Link](docs/func/store.md) | [Link](tests/test_MemBlock_scalar_store_pipeline.py) |

### MMU 基础

| 编号 | 功能点 | 用例数 | 状态 | 文档 | 测试代码 |
|------|--------|--------|------|------|----------|
| BAS-MM-001 | Sv39 地址空间安装与翻译命中 | 5 | 🟢 | [Link](docs/func/mmu.md) | [Link](tests/test_MemBlock_env_mmu_smoke.py) |
| BAS-MM-002 | DTLB fill + replacement | 1 | 🟢 | [Link](docs/func/mmu.md) | [Link](tests/test_MemBlock_env_mmu_smoke.py) |
| BAS-MM-003 | PMP CSR 编程 (all 32 entries) | 3 | 🟢 | [Link](docs/func/mmu.md) | [Link](tests/test_MemBlock_env_mmu_smoke.py) |
| BAS-MM-004 | H-extension two-stage translation load | 3 | 🟢 | [Link](docs/func/mmu.md) | [Link](tests/test_MemBlock_mmu_h_extension.py) |
| BAS-MM-005 | H-extension two-stage fault | 3 | 🟢 | [Link](docs/func/mmu.md) | [Link](tests/test_MemBlock_mmu_h_extension.py) |
| BAS-MM-006 | ITLB PTW smoke (frontend-side) | 1 | 🟢 | [Link](docs/func/mmu.md) | [Link](tests/test_MemBlock_env_mmu_smoke.py) |

### Vector 基础

| 编号 | 功能点 | 用例数 | 状态 | 文档 | 测试代码 |
|------|--------|--------|------|------|----------|
| BAS-VEC-001 | vector unit-stride load (strict data compare) | 4 | 🟢 | [Link](docs/func/vector.md) | [Link](tests/test_MemBlock_vector_unit_stride.py) |
| BAS-VEC-002 | vector strided load (正/零/负 stride) | 3 | 🟢 | [Link](docs/func/vector.md) | [Link](tests/test_MemBlock_vector_stride.py) |

### Uncache / MMIO 基础

| 编号 | 功能点 | 用例数 | 状态 | 文档 | 测试代码 |
|------|--------|--------|------|------|----------|
| BAS-UC-001 | Svpbmt cacheable / NCIO / MMIO 分类 | 3 | 🟢 | [Link](docs/func/uncache-amo.md) | [Link](tests/test_MemBlock_uncache_semantics.py) |

### AMO 基础

| 编号 | 功能点 | 用例数 | 状态 | 文档 | 测试代码 |
|------|--------|--------|------|------|----------|
| BAS-AMO-001 | single-uop AMO (amoadd / amoswap / amoxor) | 1 | 🟢 | [Link](docs/func/uncache-amo.md) | [Link](tests/test_MemBlock_amo.py) |
| BAS-AMO-002 | AMO mainPipe diagnostics | 4 | 🟢 | [Link](docs/func/uncache-amo.md) | [Link](tests/test_MemBlock_amo_diagnostics.py) |

---

## 二、功能空间探索与组合 (CMB)

在基础能力之上，对参数空间进行遍历、对多条路径进行组合，暴露边界条件与交互行为。

### Load 参数空间与 Probe

| 编号 | 功能点 | 用例数 | 状态 | 文档 | 测试代码 |
|------|--------|--------|------|------|----------|
| CMB-LD-001 | 随机 load (IO + cacheable 混合路径) | 6 | 🟢 | [Link](docs/func/load.md) | [Link](tests/test_MemBlock_random_load.py) |
| CMB-LD-002 | bank conflict hit (无 forward / 无 violation) | 1 | 🟢 | [Link](docs/func/load.md) | [Link](tests/test_MemBlock_scalar_load_pipeline_probe.py) |
| CMB-LD-003 | matchInvalid proxy nuke | 1 | 🟢 | [Link](docs/func/load.md) | [Link](tests/test_MemBlock_scalar_load_pipeline_probe.py) |
| CMB-LD-004 | hi-prio replay 抢占 fast replay | 1 | 🟢 | [Link](docs/func/load.md) | [Link](tests/test_MemBlock_scalar_load_pipeline_probe.py) |
| CMB-LD-005 | late-STA store-load violation | 1 | 🟢 | [Link](docs/func/load.md) | [Link](tests/test_MemBlock_scalar_load_pipeline_probe.py) |
| CMB-LD-006 | MMU fault pipeline probe (fault 纯度检查) | 3 | 🟢 | [Link](docs/func/load.md) | [Link](tests/test_MemBlock_scalar_load_pipeline_probe.py) |
| CMB-LD-007 | 标量 load misalign | 0 | 🔴 | [Link](docs/func/load.md) | — |

### Store 参数空间与组合

| 编号 | 功能点 | 用例数 | 状态 | 文档 | 测试代码 |
|------|--------|--------|------|------|----------|
| CMB-ST-001 | 随机 store (cacheable + MMIO 混合) | 6 | 🟢 | [Link](docs/func/store.md) | [Link](tests/test_MemBlock_random_store.py) |
| CMB-ST-002 | Batched store commit | 1 | 🟢 | [Link](docs/func/store.md) | [Link](tests/test_MemBlock_scalar_store_pipeline.py) |
| CMB-ST-003 | SBuffer forward (sbuffer 内 store→load) | 3 | 🟢 | [Link](docs/func/store.md) | [Link](tests/test_MemBlock_scalar_store_pipeline.py) |
| CMB-ST-004 | partial-mask store 矩阵 (B/H/W/D) | 6 | 🟡 | [Link](docs/func/store.md) | [Link](tests/test_MemBlock_scalar_store_pipeline.py) |
| CMB-ST-005 | cross-16B / cross-beat store | 5 | 🟡 | [Link](docs/func/store.md) | [Link](tests/test_MemBlock_store_misalign.py) |
| CMB-ST-006 | SQ backpressure + delayed drain | 3 | 🟡 | [Link](docs/func/store.md) | [Link](tests/test_MemBlock_scalar_store_pipeline.py) |
| CMB-ST-007 | cross-page scalar store-misalign | 4 | 🔴 | [Link](docs/func/store.md) | [Link](tests/test_MemBlock_store_misalign.py) |

### MMU 参数空间与组合

| 编号 | 功能点 | 用例数 | 状态 | 文档 | 测试代码 |
|------|--------|--------|------|------|----------|
| CMB-MM-001 | B/H/W/D load fault size 矩阵 | 1 | 🟢 | [Link](docs/func/mmu.md) | [Link](tests/test_MemBlock_mmu_fault.py) |
| CMB-MM-002 | mixed size permission fault (TLB miss) | 1 | 🟢 | [Link](docs/func/mmu.md) | [Link](tests/test_MemBlock_mmu_fault.py) |
| CMB-MM-003 | 2MB / 1GB 大页翻译 | 1 | 🟡 | [Link](docs/func/mmu.md) | [Link](tests/test_MemBlock_mmu_tlbfa_deep.py) |
| CMB-MM-004 | store-side PMP deny | 1 | 🟡 | [Link](docs/func/mmu.md) | [Link](tests/test_MemBlock_pmp_runtime.py) |

### Vector 参数空间与组合

| 编号 | 功能点 | 用例数 | 状态 | 文档 | 测试代码 |
|------|--------|--------|------|------|----------|
| CMB-VEC-001 | vector unit-stride load (mask / port / 非对齐) | 3 | 🟢 | [Link](docs/func/vector.md) | [Link](tests/test_MemBlock_vector_unit_stride.py) |
| CMB-VEC-002 | vector unit-stride store | 1 | 🟡 | [Link](docs/func/vector.md) | [Link](tests/test_MemBlock_vector_store.py) |
| CMB-VEC-003 | vector store masked-inactive / nonzero vstart | 2 | 🟡 | [Link](docs/func/vector.md) | [Link](tests/test_MemBlock_vector_store.py) |

### Uncache / MMIO 组合

| 编号 | 功能点 | 用例数 | 状态 | 文档 | 测试代码 |
|------|--------|--------|------|------|----------|
| CMB-UC-001 | NC store flush (单条 + burst) | 2 | 🟡 | [Link](docs/func/uncache-amo.md) | [Link](tests/test_MemBlock_uncache_semantics.py) |
| CMB-UC-002 | MMIO mixed-path flush exclusion | 2 | 🟢 | [Link](docs/func/uncache-amo.md) | [Link](tests/test_MemBlock_uncache_semantics.py) |
| CMB-UC-003 | NC + cacheable 混合 store flush | 1 | 🟡 | [Link](docs/func/uncache-amo.md) | [Link](tests/test_MemBlock_uncache_semantics.py) |
| CMB-UC-004 | mmioBusy 阻塞 younger load retire | 2 | 🟡 | [Link](docs/func/uncache-amo.md) | [Link](tests/test_MemBlock_uncache_semantics.py) |

### 访存排序组合

| 编号 | 功能点 | 用例数 | 状态 | 文档 | 测试代码 |
|------|--------|--------|------|------|----------|
| CMB-ORD-001 | two stores → load (same-addr) | 1 | 🟢 | [Link](docs/func/ordering.md) | [Link](tests/test_MemBlock_scalar_ordering.py) |
| CMB-ORD-002 | store → unrelated load (无污染) | 1 | 🟢 | [Link](docs/func/ordering.md) | [Link](tests/test_MemBlock_scalar_ordering.py) |
| CMB-ORD-003 | 定向混合 ld/st 序列 (覆盖/重读) | 1 | 🟢 | [Link](docs/func/ordering.md) | [Link](tests/test_MemBlock_scalar_ordering.py) |

---

## 三、专项场景覆盖 (SCN)

针对特定子系统的协议、状态机、异常路径进行深度定向验证。

### TLB / PTW 深度

| 编号 | 功能点 | 用例数 | 状态 | 文档 | 测试代码 |
|------|--------|--------|------|------|----------|
| SCN-TLB-001 | sv39 多 ASID / sfence 冲刷矩阵 | 4 | 🟢 | [Link](docs/func/tlb-ptw-deep.md) | [Link](tests/test_MemBlock_mmu_tlbfa.py) |
| SCN-TLB-002 | sfence.vma 四种 rs1/rs2 组合 | 4 | 🟢 | [Link](docs/func/tlb-ptw-deep.md) | [Link](tests/test_MemBlock_mmu_tlbfa_deep.py) |
| SCN-TLB-003 | 批量填充 + sfence refill 循环 | 2 | 🟢 | [Link](docs/func/tlb-ptw-deep.md) | [Link](tests/test_MemBlock_mmu_tlbfa_deep.py) |
| SCN-TLB-004 | 三端口同拍 TLB miss + store refill | 2 | 🟢 | [Link](docs/func/tlb-ptw-deep.md) | [Link](tests/test_MemBlock_mmu_tlbfa_deep.py) |
| SCN-TLB-005 | LLPTW (队列压力 / duplicate / fault) | 7 | 🟡 | [Link](docs/func/tlb-ptw-deep.md) | [Link](tests/test_MemBlock_mmu_llptw.py) |
| SCN-TLB-006 | H-extension fence (hfence.vvma / gvma) | 8 | 🟡 | [Link](docs/func/tlb-ptw-deep.md) | [Link](tests/test_MemBlock_mmu_h_extension.py) |
| SCN-TLB-007 | pfTLB (L2TLB prefetcher port) | 9 | 🟡 | [Link](docs/func/tlb-ptw-deep.md) | [Link](tests/test_MemBlock_mmu_tlbfa_pftlb.py) |

### MMU Fault 定向

| 编号 | 功能点 | 用例数 | 状态 | 文档 | 测试代码 |
|------|--------|--------|------|------|----------|
| SCN-FLT-001 | load translation fault (TLB hit) | 1 | 🟢 | [Link](docs/func/mmu-fault.md) | [Link](tests/test_MemBlock_mmu_fault.py) |
| SCN-FLT-002 | load PMP access fault (deny region) | 1 | 🟢 | [Link](docs/func/mmu-fault.md) | [Link](tests/test_MemBlock_mmu_fault.py) |
| SCN-FLT-003 | translation fault + PMP fault 叠加 | 1 | 🟢 | [Link](docs/func/mmu-fault.md) | [Link](tests/test_MemBlock_mmu_fault.py) |
| SCN-FLT-004 | PMP deny region 边界 + 恢复 | 2 | 🟢 | [Link](docs/func/mmu-fault.md) | [Link](tests/test_MemBlock_mmu_fault.py) |
| SCN-FLT-005 | TLB AF + PMP AF 组合矩阵 | 3 | 🟢 | [Link](docs/func/mmu-fault.md) | [Link](tests/test_MemBlock_mmu_fault.py) |

### PMP / PMA 深度

| 编号 | 功能点 | 用例数 | 状态 | 文档 | 测试代码 |
|------|--------|--------|------|------|----------|
| SCN-PMP-001 | PMP runtime: 32-entry 重编程 + lock/freeze | 4 | 🟢 | [Link](docs/func/pmp-pma.md) | [Link](tests/test_MemBlock_pmp_runtime.py) |
| SCN-PMP-002 | PMP NAPOT/TOR boundary 矩阵 (load + store) | 4 | 🟢 | [Link](docs/func/pmp-pma.md) | [Link](tests/test_MemBlock_pmp_runtime.py) |
| SCN-PMP-003 | PMA runtime 切换 (cacheable↔mmio) | 2 | 🟢 | [Link](docs/func/pmp-pma.md) | [Link](tests/test_MemBlock_pmp_runtime.py) |
| SCN-PMP-004 | PMA NAPOT/TOR boundary 矩阵 | 4 | 🟢 | [Link](docs/func/pmp-pma.md) | [Link](tests/test_MemBlock_pmp_runtime.py) |

### Replay 机制

| 编号 | 功能点 | 用例数 | 状态 | 文档 | 测试代码 |
|------|--------|--------|------|------|----------|
| SCN-RPL-001 | FF (forward fail replay) | 1 | 🟢 | [Link](docs/func/replay.md) | [Link](tests/test_MemBlock_replay.py) |
| SCN-RPL-002 | DM (dcache miss replay) | 1 | 🟢 | [Link](docs/func/replay.md) | [Link](tests/test_MemBlock_replay.py) |
| SCN-RPL-003 | NC (uncache/nc path replay) | 1 | 🟢 | [Link](docs/func/replay.md) | [Link](tests/test_MemBlock_replay.py) |
| SCN-RPL-004 | RAW (read-after-write violation) | 1 | 🟢 | [Link](docs/func/replay.md) | [Link](tests/test_MemBlock_replay.py) |
| SCN-RPL-005 | RAR (read-after-read violation) | 1 | 🟢 | [Link](docs/func/replay.md) | [Link](tests/test_MemBlock_replay.py) |
| SCN-RPL-006 | BC (bank conflict) | 1 | 🟢 | [Link](docs/func/replay.md) | [Link](tests/test_MemBlock_replay.py) |
| SCN-RPL-007 | NK (pipeline st-ld nuke) | 1 | 🟢 | [Link](docs/func/replay.md) | [Link](tests/test_MemBlock_replay.py) |
| SCN-RPL-008 | BC + dataInvalid + nuke 组合 | 1 | 🟢 | [Link](docs/func/replay.md) | [Link](tests/test_MemBlock_replay.py) |
| SCN-RPL-009 | SQ dataInvalid + matchInvalid + nuke | 1 | 🟡 | [Link](docs/func/replay.md) | [Link](tests/test_MemBlock_replay.py) |
| SCN-RPL-010 | FF + DM + NC 小规模混合流 | 1 | 🟢 | [Link](docs/func/replay.md) | [Link](tests/test_MemBlock_replay.py) |
| SCN-RPL-011 | RAW replay 细分 (full/partial overlap) | 0 | 🟡 | [Link](docs/func/replay.md) | — |
| SCN-RPL-012 | RAR violation 细分 (release / backpressure) | 0 | 🟡 | [Link](docs/func/replay.md) | — |
| SCN-RPL-013 | load-wait / waitForRobIdx 定向 | 0 | 🟡 | [Link](docs/func/replay.md) | — |

### 子系统深度

| 编号 | 功能点 | 用例数 | 状态 | 文档 | 测试代码 |
|------|--------|--------|------|------|----------|
| SCN-SYS-001 | DCache CtrlUnit (ECC error injection / recovery) | 4 | 🟢 | [Link](docs/func/subsystem.md) | [Link](tests/test_MemBlock_dcache_ctrlunit.py) |
| SCN-SYS-002 | VLQ coverage (8-port enqueue / redirect / wrap) | 4 | 🟢 | [Link](docs/func/subsystem.md) | [Link](tests/test_MemBlock_vlq_coverage.py) |
| SCN-SYS-003 | frontendBridge (icache / instr_uncache / ctrl) | 3 | 🟢 | [Link](docs/func/subsystem.md) | [Link](tests/test_MemBlock_frontend_bridge.py) |

---

## 四、环境 API 单测 (UT)

纯 Python 单元测试，验证 env fixture、agent、model、scoreboard 等基础设施的正确性，不依赖真实 DUT 功能。

| 编号 | 功能点 | 用例数 | 状态 | 文档 | 测试代码 |
|------|--------|--------|------|------|----------|
| UT-ENV-001 | env fixture (bootstrap / bundle / clock / reset) | 29 | 🟢 | [Link](docs/func/ut-env.md) | [Link](tests/test_MemBlock_env_fixture.py) |
| UT-ENV-002 | backend facade (send / execute / prepare / credit) | 40 | 🟢 | [Link](docs/func/ut-env.md) | [Link](tests/test_request_apis_backend_facade.py) |
| UT-ENV-003 | MemoryModel store logic (unit test) | 23 | 🟢 | [Link](docs/func/ut-env.md) | [Link](tests/test_memory_model_store_logic.py) |
| UT-ENV-004 | VectorMemoryModel element expansion | 7 | 🟢 | [Link](docs/func/ut-env.md) | [Link](tests/test_vector_memory_model.py) |
| UT-ENV-005 | ROB agent (blocker / readiness / commit) | 10 | 🟢 | [Link](docs/func/ut-env.md) | [Link](tests/test_MemBlock_rob_agent.py) |
| UT-ENV-006 | ROB function coverage collector | 4 | 🟢 | [Link](docs/func/ut-env.md) | [Link](tests/test_MemBlock_rob_coverage.py) |
| UT-ENV-007 | IssueAgent (lane shapes / fuType binding) | 9 | 🟢 | [Link](docs/func/ut-env.md) | [Link](tests/test_issue_agent.py) |

---

## 覆盖缺口汇总

以下缺口按 `coverage_todo.md` 中的优先级排列。详情和最新进度请直接阅读 [coverage_todo.md](docs/coverage_todo.md)。

| 优先级 | 缺口功能点 | 编号 | 现状 |
|--------|-----------|------|------|
| P0 | vector store control-path 深度覆盖 | CMB-VEC-002/003 | 已知 DUT flushSb stall，现有 case 以 xfail 保留 |
| P0 | 标量 partial-mask store 矩阵补全 | CMB-ST-004 | 已有基础矩阵，merge 组合未打全 |
| P0 | cross-line / cross-beat scalar store | CMB-ST-005 | StoreMisalignBuffer 覆盖横盘 |
| P0 | misaligned store 深状态 (cross-page / exception overwrite) | CMB-ST-007 | cross-page normal path 有已知 DUT xfail |
| P0 | SQ backpressure + delayed drain | CMB-ST-006 | 已有最小闭环，ready 抖动仍缺 |
| P1 | RAW replay 细分 (committed/uncommitted window) | SCN-RPL-011 | 已有 smoke，矩阵未打全 |
| P1 | RAR violation 细分 (release / backpressure) | SCN-RPL-012 | 已有 smoke，细分未打全 |
| P1 | load-wait / waitForRobIdx 成体系回归 | SCN-RPL-013 | 可触发，未有成体系用例 |
| P1 | NC store flush/drain 闭环 | CMB-UC-001 | `nc_store_flush_drain_observed` 未命中 |
| P1 | MMIO store exclusion from drain compare | CMB-UC-002 | `mmio_store_excluded_from_drain_observed` 未命中 |
| P1 | mmioBusy 长窗口 / uncache 多 outstanding | CMB-UC-004 | 基础 smoke 存在，深组合缺 |
| P2 | TLB miss → refill → replay 成功路径 | SCN-TLB-005 | hierarchy hit/miss 组合缺 |
| P2 | store-side PMP deny 收口 (xfail → 硬断言) | CMB-MM-004 | 仍缺稳定 fault 收口 |
| P2 | frontendBridge PTW 纵深 (repeated hit/miss) | SCN-SYS-003 | 仅最小往返 smoke |
| P2 | 大页翻译路径 | CMB-MM-003 | 待 PTW 能力开放 |
| P2 | LR/SC, AMOCAS | — | 尚无稳定入口 |

## 快速开始

```python
from transactions import LoadTxn
from sequences import ResetEnvSequence

state = ResetEnvSequence(require_issue_lanes=(0,), require_lq_ready=True).run(env)
env.preload_u64(0x9000_0000, 0x1122334455667788)

txn = LoadTxn(
    req_id=0x21, addr=0x9000_0000,
    lq_ptr=state.next_lq_ptr, sq_ptr=state.sq_ptr,
)
env.backend.send(txn)
env.expect_scalar_load(rob_idx=txn.rob_idx, pdest=txn.resolved_pdest, addr=txn.addr)
env.drain_writebacks()
```

推荐新 testcase 优先复用 `sequences/` 中已有的场景模板。编写指南和调试方法见：

- [test_sequence_and_extension_guide.md](docs/test_sequence_and_extension_guide.md) — 测试序列编写与扩展指南
- [backend_rob_cookbook.md](docs/backend_rob_cookbook.md) — backend/ROB 常见脚本模板
- [verification_env_design.md](docs/verification_env_design.md) — 验证环境架构总览

## 目录结构

```
src/test/python/MemBlock/
├── MemBlock_env.py          # 顶层环境、bundle、统一时钟内核
├── MemBlock_api.py          # DUT fixture 与覆盖率路径配置
├── transactions.py          # 公共事务模型 (LoadTxn, StoreTxn, BackendSendPlan, ...)
├── memory_model.py          # load compare / store drain 校验编排
├── env_config.py            # 统一配置入口 (queue 深度、transport 延迟)
├── request_apis.py          # 兼容层 primitive helper
├── README.md                # <- 当前文件 (功能覆盖入口)
├── CHANGELOG.md             # Python 验证环境演进记录
├── ROLES.md                 # 多人协同角色规范
├── agents/                  # 主动 agents (backend_facade, issue, commit, csr, lsq, pftlb, vector_*)
├── monitors/                # 被动 monitors (writeback, store, mem_status, vector_mem)
├── model/                   # 公共组件 (scoreboard, rob_coverage, transport_responder, ref_memory)
├── sequences/               # 可复用场景模板 (6 个 sequence 模块)
├── tests/                   # 真实 DUT 用例 (36 个测试文件)
├── docs/                    # 详细设计文档 + 功能点 IPO 描述 (docs/func/)
├── webui/                   # LSQ 可视化资源
└── data/                    # 覆盖率报告产物
```

## 参考文档

| 文档 | 定位 |
|------|------|
| [CHANGELOG.md](CHANGELOG.md) | Python 验证环境自身演进记录 |
| [ROLES.md](ROLES.md) | 多人协同角色与 agent 规范 |
| [docs/coverage_summary.md](docs/coverage_summary.md) | 当前回归覆盖率结果分析 (**真源**) |
| [docs/coverage_todo.md](docs/coverage_todo.md) | 覆盖率驱动补强待办清单 (**真源**) |
| [docs/vp_pipeline_plan.md](docs/vp_pipeline_plan.md) | 标量 ld/st pipeline 白盒验证总方案 |
| [docs/verification_env_design.md](docs/verification_env_design.md) | 验证环境分层架构总设计 |
| [docs/backend_request_model_design.md](docs/backend_request_model_design.md) | backend 主动控制请求模型 |
| [docs/backend_rob_cookbook.md](docs/backend_rob_cookbook.md) | backend/ROB 常见脚本模板 |
| [docs/rob_model.md](docs/rob_model.md) | ROB 建模现状、缺口与演进 |
| [docs/rob_coverage_plan.md](docs/rob_coverage_plan.md) | ROB function coverage 模型设计 |
| [docs/memory_model_design.md](docs/memory_model_design.md) | MemoryModel 职责与设计 |
| [docs/mmu_env_design_and_usage.md](docs/mmu_env_design_and_usage.md) | MMU/PTW/DTLB 环境设计与使用 |
| [docs/mmu_fault_directed_cases.md](docs/mmu_fault_directed_cases.md) | MMU fault 定向用例说明 |
| [docs/vmem_design_and_usage.md](docs/vmem_design_and_usage.md) | 向量访存环境设计与使用 |
| [docs/misalign.md](docs/misalign.md) | 标量 load/store misalign 专题分析 |
| [docs/scalar_load_pipeline_probe_cases.md](docs/scalar_load_pipeline_probe_cases.md) | load pipeline probe 用例说明 |
| [docs/test_sequence_and_extension_guide.md](docs/test_sequence_and_extension_guide.md) | 测试序列编写与扩展指南 |
| [docs/dut_port_behavior.md](docs/dut_port_behavior.md) | DUT 端口行为说明 |
| [docs/clock_control_and_migration_guide.md](docs/clock_control_and_migration_guide.md) | 时钟控制与迁移指南 |
| [docs/BUGS.md](docs/BUGS.md) | 已知 DUT bug 记录 |
| [docs/DUT_CHANGELOG-20260331.md](docs/DUT_CHANGELOG-20260331.md) | DUT RTL 变更日志 (3月) |
| [docs/DUT_CHANGELOG-20260428.md](docs/DUT_CHANGELOG-20260428.md) | DUT RTL 变更日志 (4月) |
