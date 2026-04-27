# MemBlock MMU Test TODO Plan

## 1. 文档目的

本文档用于把 `src/test/python/MemBlock/` 下一阶段 MMU testcase 的补强方向收敛成一份可执行计划。

它与 `mmu_env_todo.md` 的分工如下：

1. `mmu_test_todo.md`
   - 站在 testcase / sequence 视角，回答“接下来应该补哪些真实 DUT 场景”。
2. `mmu_env_todo.md`
   - 站在 env / monitor / facade 视角，回答“为了把这些场景稳定落地，需要补哪些基础设施”。

相关文档：

- `src/test/python/MemBlock/docs/coverage_summary.md`
- `src/test/python/MemBlock/docs/coverage_todo.md`
- `src/test/python/MemBlock/docs/mmu_env_todo.md`
- `src/test/python/MemBlock/docs/mmu_fault_directed_cases.md`
- `src/test/python/MemBlock/docs/mmu_h_extension_cases.md`

## 2. 当前背景

当前 MMU 回归已经证明：

1. Sv39 基础 PTW/load 闭环可跑通。
2. DTLB fill/replacement 已有最小 directed case。
3. VS-only / two-stage load、re-hit、`hfence.vvma`、`hfence.gvma` 的基础 smoke 已转正。
4. page/access fault、PBMT、frontend ITLB 也已有最小入口。

但当前 coverage 仍说明“能跑通”不等于“已经打透”：

1. `TLBFA.sv`
2. `TLBFA_1.sv`
3. `TLBFA_2.sv`
4. `L2TLB.sv`
5. `LLPTW.sv`
6. `PtwCache.sv`
7. `PMP.sv`

其中最核心的缺口，是 testcase 还没有把 `miss/refill/re-hit/flush/context-switch/refill-encoding` 做成稳定矩阵，因此 `TLBFA*` 的多读口、失效选择、entry 元数据和 permission 组合仍大量未命中。

补充说明：

1. `TLBFA_1.sv` 的当前主缺口已经确认主要来自 store-side translated traffic，而不是 frontend ITLB。
   - RTL 连接上它挂在 `inner_dtlb_st_tlb_st`，上游是两个 `StoreUnit` requestor。
2. `TLBFA_2.sv` 的当前主缺口已经确认主要来自 load-side multi-lane + two-stage 组合。
   - RTL 连接上它挂在 load-side DTLB，三个 requestor 对应 `LoadUnit0/1/2`。
3. frontend `fetch_to_mem.itlb` 仍然有价值，但它更直接服务于 frontend ITLB / `PTWRepeaterNB` / `PtwCache` 相关覆盖，不应再被误当成 `TLBFA_1` 的主补强方向。

## 3. 设计原则

### 3.1 总体原则

1. 优先真实 DUT hard-assert，不用 mock-only 证明点替代。
2. 优先复用现有 `env.mmu`、sequence、monitor 和 facade，不在 testcase 中散落内部信号探针。
3. capability probe 与 strict semantic regression 分层维护。
4. 一条 testcase 优先证明一个行为簇，不把无关断言塞进同一条用例。

### 3.2 口径原则

1. 先验证行为族别，再追求细粒度字段全覆盖。
2. 对已确认 DUT gap 使用精确 `xfail`，不要把整组 MMU case 降级。
3. 所有新 testcase 都应能回溯到 coverage gap、DUT 风险或既有文档中的缺口描述。

## 4. 优先级总览

- `P0`：deterministic TLB miss / refill / re-hit / flush 回归
- `P0`：MMU fault / recovery / exclusion 回归
- `P1`：TLB refill 编码多样性回归
- `P1`：H extension selective flush / context-switch 回归
- `P1`：PBMT + PMP translated semantic regression
- `P2`：多来源 / 多读口 TLB 流量回归
- `P2`：MMU 与 replay / nuke / redirect 交叉回归

建议顺序：

1. 先把最基础的 `TLBFA` 命中面做成矩阵。
2. 再补 fault / recovery，避免后续场景定位困难。
3. 然后扩展到 refill 编码和 H extension selective flush。
4. 最后再补更复杂的多机制交叉与多来源压力。

截至 `2026-04-27`，与 `TLBFA*` 直接相关、已经落地的 testcase 矩阵包括：

1. `sfence.vma` 四种粒度 + root switch reload。
2. `hfence.vvma/gvma` selective flush 基础矩阵。
3. `TLBFA_1` 对应的 store-side Sv39 / two-stage dual-unit translated store。
4. `TLBFA_2` 对应的 load-side Sv39 / two-stage 3-lane same-cycle miss/hit 矩阵。

因此后续优先级应从“先证明 requestor 家族能动起来”，转向“在已打通 requestor 家族后继续补 refill 编码多样性、fault/fence 交叉和 prefetch/frontend 来源”。

## 5. P0：TLB miss / refill / re-hit / flush 回归

### 5.1 目标

把当前“只有少量 smoke 能证明 page walk 成功”的状态，升级成“能稳定区分首次 miss、refill 后 re-hit、flush 后 re-miss”。

### 5.2 建议 testcase 组

#### A. Sv39 单阶段基础组

1. 首访 miss -> PTW refill -> writeback 成功。
2. 同 VA 二访 re-hit，不再重新 page walk。
3. 同 VA 三访，在 `sfence.vma(all addr, all asid)` 后重新 miss。
4. root-A / root-B 切换访问同 VA，旧 translation 不残留。

#### B. DTLB 容量与局部重访组

1. 先填满容量，再重访 warm 项，要求 hit。
2. 再继续 overflow，最后重访被顶出的旧项，要求 re-miss。
3. 同时保留 `first_l2_tlb_req` / PTW 活动量差异断言。

#### C. flush 粒度基础组

1. `sfence.vma(all addr, all asid)`。
2. `sfence.vma(specific addr, all asid)`。
3. `sfence.vma(all addr, specific asid)`。
4. `sfence.vma(specific addr, specific asid)`。

### 5.3 核心断言

1. 是否观测到首次 miss。
2. re-hit 时 PTW 活动量是否下降到预期。
3. flush 后是否重新出现 miss / refill。
4. 访问成功时不应串成 outer fault、cache error 或 silent timeout。

### 5.4 预期收益

1. 优先提升 `TLBFA*`、`L2TLB.sv`、`LLPTW.sv`、`PtwCache.sv`。
2. 为后续 permission/fault/H-extension 回归提供稳定翻译背景。

## 6. P0：MMU fault / recovery / exclusion 回归

### 6.1 目标

把 fault 从“顺带碰到”升级成独立专题，明确 fault 类型、恢复路径和与 replay/redirect 的排他边界。

### 6.2 建议 testcase 组

#### A. Page fault 基础组

1. root 缺失映射的 load/store fault。
2. leaf `V=0` 的 load/store fault。
3. leaf 权限不满足的 load/store fault。
4. 修复 PTE 后同 VA recovery 成功。

#### B. Access fault 基础组

1. page table 正确 + PMP deny load。
2. page table 正确 + PMP deny store。
3. allow -> deny -> allow 的切换恢复。

#### C. Fault aftermath 组

1. fault 请求不应产生正常 writeback。
2. fault 请求不应错误继续从 replay 路径冒出。
3. fault 后 ROB/LSQ/store/outer 状态应能收敛。
4. `sfence` / root switch / PTE 修复后 recovery 应成立。

### 6.3 核心断言

1. `page fault`、`guest page fault`、`access fault` 族别不串类。
2. `vaddr/gpaddr/debug_paddr/rob_idx` 等元信息尽量稳定。
3. fault case 不退化成“只是等超时”。

## 7. P1：TLB refill 编码多样性回归

### 7.1 目标

把当前“只覆盖少数合法 4KB leaf 编码”的 refill 形状，扩展到更接近 `TLBFA` entry 实际状态空间的矩阵。

### 7.2 建议 testcase 组

#### A. 页级别与 PTE 形态组

1. 4KB leaf。
2. 更高层级 leaf / superpage。
3. non-leaf walk 继续下钻。
4. root 切换后再次建立不同层级 translation。

#### B. S1 permission 组

1. `R/W/X/U/G/A/D` 组合变化。
2. `pf/af` 的 fault 对照组。
3. `perm_g` 与非 global 项对 flush 作用域的差异。

补充说明：

1. store-side `TLBFA_1` 已经有 dual-unit translated store 基础流量，后续这里应优先补“不同 PTE 编码如何进入 refill”，而不是继续只加 requestor 数量。
2. 仅靠 frontend ITLB 不能替代 store-side refill 编码覆盖。

#### C. S2 / two-stage 元数据组

1. `s2xlate` 的 VS-only / two-stage 对照。
2. `gpf/gaf` fault 对照。
3. `g_pbmt`、guest-side permission 组合。
4. `VMID` 变化后的 refill / hit / flush。

补充说明：

1. load-side `TLBFA_2` 已经有 two-stage 3-lane same-cycle 基础矩阵。
2. 后续重点应转向 `gpf/gaf`、`guest permission`、`VMID` 切换与 selective flush 对这些 entry 元数据的影响，而不是重复构造等价的 3-lane hit case。

#### D. `valididx/pteidx/ppn_low` 变化组

1. 不同层级 walk 触发不同 index 组合。
2. 不同 page size / page-table 形态触发不同 `ppn_low` 组合。

### 7.3 核心断言

1. testcase 侧不直接检查 `TLBFA` 私有寄存器值。
2. 通过 PTW trace、命中行为、fault 族别和 flush 结果间接证明 entry 元数据生效。
3. 如需要新增白盒观测，先进入 env/monitor 层，再复用到 testcase。

## 8. P1：H extension selective flush / context-switch 回归

### 8.1 目标

在已经转正的 two-stage `basic/re-hit/hfence(all-addr)` 基础上，继续补齐更细粒度的 VS/G-stage 语义。

### 8.2 建议 testcase 组

1. VS-only 与 two-stage 的 miss/hit 对照。
2. `hfence.vvma(all addr, specific asid)`。
3. `hfence.vvma(specific addr, all asid)`。
4. `hfence.vvma(specific addr, specific asid)`。
5. `hfence.gvma(all addr, specific vmid)`。
6. `hfence.gvma(specific addr, all vmid)`。
7. `hfence.gvma(specific addr, specific vmid)`。
8. VS ASID 切换后同 VA 的命中隔离。
9. VMID 切换后同 GPA 的命中隔离。
10. G-stage guest fault 下 `gpaddr` 与 stage-1 GPA 对齐收口。

### 8.3 核心断言

1. selective flush 只能清掉目标上下文，不能把无关 translation 一起清空。
2. VS-stage 与 G-stage 的 flush 作用域要可区分。
3. 当前仍未收口的 DUT gap，应保留精确 `xfail`。

## 9. P1：PBMT + PMP translated semantic regression

### 9.1 目标

把当前 translated path 的 cacheable / NCIO / MMIO / PMP 行为从“最小 capability”推进到“更稳定的语义回归”。

### 9.2 建议 testcase 组

1. translated cacheable load/store 对照组。
2. translated NCIO load/store。
3. translated MMIO load/store。
4. PBMT 与 PMP allow/deny 交叉。
5. cacheable / NCIO / MMIO mixed-path。
6. translated younger load 遇到 older MMIO store 的阻塞语义。

### 9.3 核心断言

1. 分类正确，不串成 fault。
2. store shadow / drain / writeback 闭环正确。
3. 已知 DUT gap 保持精确条件 `xfail`。

## 10. P2：多来源 / 多读口 TLB 流量回归

### 10.1 目标

补当前 `TLBFA*` 与周边 TLB/PTW 模块仍未系统命中的多读口和多来源流量。

### 10.2 建议 testcase 组

#### A. load-side 3 requestor / prefetcher 组

1. `LoadUnit0/1/2` 同窗口 mixed-page 访问，而不是只做等价的 3-lane hit。
2. load requestor 与 prefetcher requestor 交错访问同一 translation，检查 refill 共享/隔离。
3. 不同 load lane 分别处于 Sv39 / two-stage / fault 恢复窗口，验证 miss/hit/fault 不串扰。

#### B. store-side 2 requestor 组

1. `StoreUnit0/1` translated store 交错访问不同 VA，检查 refill/fault/flush 背景不串扰。
2. store-side requestor 与 load-side requestor 命中同一 translation，检查下层 refill 结果是否复用一致。
3. 在 `sfence/hfence` 前后混入 translated store，验证 selective flush 不会错误保留旧 translation。

#### C. frontend / ITLB 组

1. backend translated access 与 frontend `fetch_to_mem.itlb` 交错访问。
2. frontend ITLB 与 backend TLB 同页/异页访问对 `PTW/LLPTW/PtwCache` 的共享效果。
3. frontend 请求落在 flush / root-switch 前后，验证无错误残留。

### 10.3 说明

1. 这组 case 是否能真正做到“同周期并发”，取决于当前 DUT 公开端口与 env 发包能力；但即使先做到“同窗口交错”，也比当前单源 smoke 更接近真实读口负载。
2. `frontend ITLB` 应继续保留，但在 coverage 归因上要与 `TLBFA_1` 的 store-side requestor 缺口分开描述。

## 11. P2：MMU 与 replay / nuke / redirect 交叉回归

### 11.1 目标

在基础翻译和 fault 行为稳定后，再补复杂交互，避免早期把定位难度抬得过高。

### 11.2 建议 testcase 组

1. root switch 下 related-address replay。
2. permission 变化与 replay 的排他性。
3. translated younger load 遇到 older store / MMIO / fault 的边界。
4. fault、redirect、nuke 的族别不串扰。

## 12. 验收与同步要求

### 12.1 testcase 级验收

每新增一组 testcase，至少回答：

1. 命中了哪个既有 coverage 缺口。
2. 证明了哪个明确的 DUT 行为。
3. 需要依赖哪些 env 观测与 helper。
4. 是否存在保留中的 DUT gap / `xfail`。

### 12.2 文档同步

每完成一组专题后，应同步更新：

1. `src/test/python/MemBlock/docs/coverage_summary.md`
2. `src/test/python/MemBlock/docs/coverage_todo.md`
3. `src/test/python/MemBlock/docs/mmu_env_todo.md`
4. 必要时更新 `src/test/python/MemBlock/docs/mmu_env_design_and_usage.md`

## 13. 建议执行顺序

1. 先补 `miss/refill/re-hit/flush` 基础矩阵。
2. 再补 fault / recovery / exclusion。
3. 然后补 refill 编码多样性与 H extension selective flush。
4. 再推进 PBMT/PMP translated semantic regression。
5. 最后补多来源和 replay/nuke/redirect 交叉场景。
