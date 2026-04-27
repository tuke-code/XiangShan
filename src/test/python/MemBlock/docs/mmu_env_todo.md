# MemBlock MMU Env TODO Plan

## 1. 文档目的

本文档用于整理 `src/test/python/MemBlock/` 当前关于 MMU/TLB/PTW/PMP/PBMT 的讨论结论，并把下一阶段工作收敛成一份可执行的 TODO plan。

它不重复 `mmu_env_design_and_usage.md` 中已经落地的设计细节，重点回答三个问题：

1. 当前 MMU 环境已经具备哪些能力。
2. 当前验证的薄弱点主要在哪里。
3. 下一阶段应按什么顺序补强，才最容易形成专题化、可回归、可收口的真实 DUT 验证。

相关文档：

- `src/test/python/MemBlock/docs/mmu_env_design_and_usage.md`
- `src/test/python/MemBlock/docs/mmu_test_todo.md`
- `src/test/python/MemBlock/docs/coverage_summary.md`
- `src/test/python/MemBlock/docs/coverage_todo.md`
- `src/test/python/MemBlock/docs/sq_matchinvalid_nuke_case_analysis.md`

## 2. 当前基础能力

当前 `env.mmu` 与配套 sequence 已经具备以下基础设施：

1. Sv39 控制面保活，不会被 `idle_inputs()` 意外清空。
2. PTW TileLink responder 可返回 multi-beat D 响应，能覆盖 64B page-table walk。
3. PMP distributed CSR helper 已打通，可显式放开 S-mode 访问权限。
4. Svpbmt/PBMT 控制面已具备最小可复用接口。
5. `MmuSv39AddressSpaceInstallSequence` / `MmuSv39ActivateSequence` 已可复用。
6. 真实 DUT 下已经有基础 smoke 与部分 replay/uncache 相关 MMU 场景。
7. H extension 控制面已具备最小公开契约：
   - `enable_vs_sv39()` / `enable_two_stage_sv39()`
   - `pulse_hfence_vvma()` / `pulse_hfence_gvma()`
   - `install_g_sv39_mapping()`
   - `wait_load_fault_observed()` / `sample_mmu_fault_state()`

这说明当前问题已不是“MMU 环境搭不起来”，而是“专题化验证矩阵还没有铺开”。

## 3. 当前主要薄弱点

### 3.1 专题 testcase 仍偏少

当前独立 MMU testcase 主要还是：

- Sv39 状态保活 smoke
- Sv39 + PTW + DTLB + cacheable load 基础闭环
- 少量 PBMT / replay 场景中的配套 MMU 使用

不足以系统证明以下主题：

- TLB miss / refill / re-hit
- page fault / access fault 分类
- PMP allow/deny 切换
- translated NCIO/MMIO store 语义
- fault/replay/redirect 之间的排他关系

### 3.2 TLB/PTW 覆盖率仍明显偏低

根据当前 coverage 文档，以下模块仍属于明显薄弱区：

- `TLBFA.sv`
- `TLBFA_1.sv`
- `TLBFA_2.sv`
- `L2TLB.sv`
- `LLPTW.sv`
- `PMP.sv`
- `PtwCache.sv`

这说明当前环境虽然能触发基础翻译，但还没有把 refill、cache、fault、permission 这些深状态真正打透。

### 3.3 MMU 异常验证尚未形成独立闭环

当前更多是在“放开权限后成功访问”的正向路径上做验证，而没有独立建立以下 fault 矩阵：

- `load page fault`
- `store page fault`
- `load access fault`
- `store access fault`
- fault 后 recovery
- fault 与 replay/redirect/nuke 的边界

### 3.4 PBMT/uncache/MMIO 仍有 capability gap

当前部分 translated NCIO/MMIO case 仍依赖精确条件 `xfail` 记录 gap，说明：

- 环境已经能把请求送到目标路径；
- 但 DUT 的 debug 分类、store shadow、drain 闭环还没有全部稳定；
- 因而还不能把所有 PBMT 场景都当成“已完全收口”。

### 3.5 H extension success path 已转正，但 selective flush 与 guest-side 细节仍未铺开

当前 real DUT 已经转正并稳定通过：

- VS-only state 保活
- two-stage success path
- two-stage re-hit
- `hfence.vvma(all addr, all asid)`
- `hfence.gvma(all addr, all vmid)`
- two-stage guest page fault
- two-stage PMP access fault
- H fence helper 驱动位

但当前仍有两类缺口：

1. testcase 还没有把 `specific addr / specific asid / specific vmid` 的 selective flush 矩阵铺开。
2. G-stage guest page fault 下的 `gpaddr` 导出值与 stage-1 GPA 还未稳定对齐。

### 3.6 TLBFA 低覆盖率暴露出 testcase 与 env 的共同缺口

从当前 coverage 结果和既有 smoke 形状看，`TLBFA*` 低覆盖率主要不是“没有 MMU 测试”，而是“缺少系统化 hit/miss/refill/flush/context-switch/refill-encoding 矩阵”。

这类缺口会同时反映为两层问题：

1. testcase 层缺少足够多的 directed scenario：
   - 多读口 / 多来源流量不足
   - selective flush 不足
   - refill 编码多样性不足
2. env 层缺少足够稳定的公共能力：
   - 自定义 leaf/non-leaf/permission helper 不够完整
   - miss/refill/re-hit/flush 观测未统一抽象
   - 不同上下文切换后的命中隔离断言还不够复用化

## 4. TODO 优先级总览

- `P0`：deterministic TLB miss/refill/re-hit/flush 回归
- `P0`：MMU 异常专题回归
- `P1`：TLB refill 编码多样性回归
- `P1`：H extension selective flush / context-switch 回归
- `P1`：PBMT translated NCIO/MMIO semantic regression
- `P2`：多来源 / 多读口 TLB 流量回归
- `P2`：MMU 与 replay/nuke/fault 交叉场景补强
- `P2`：覆盖率与状态文档同步更新

建议顺序：先把 `TLBFA` 相关的基础命中面立住，再补“分类和正确性”，最后扩展到更深的编码组合和复杂交互。

## 5. P0：MMU 异常专题回归

### 5.1 目标

把 MMU 异常从“偶尔在其他 testcase 里顺带碰到”升级成独立专题，明确区分：

- translation/page fault
- PMP access fault
- 非 fault 的 MMIO/NCIO 提交阻塞或 capability gap

### 5.2 计划补强的 fault 矩阵

#### A. Translation / Page Fault

1. root page table 未安装映射，translated load fault。
2. root page table 未安装映射，translated store fault。
3. leaf PTE `V=0`，translated load fault。
4. leaf PTE `V=0`，translated store fault。
5. leaf 权限不满足，load/store 分别 fault。
6. 修复 page table 后，同 VA 重发应恢复正常完成。

#### B. PMP Access Fault

1. page table 正确 + PMP deny load，触发 load access fault。
2. page table 正确 + PMP deny store，触发 store access fault。
3. 同一地址 allow -> deny 切换后，再次访问必须 fault。
4. deny -> allow 恢复后，再次访问必须恢复成功。

#### C. Fault Aftermath

1. fault 请求不应产生正常 data writeback。
2. fault 请求不应同时继续从 replay 路径冒出。
3. fault 后 ROB/LSQ/store/outer 状态应能收敛到 quiesce。
4. `sfence`、切 root、修复 PTE/PMP 后，应可验证 recovery。

### 5.3 所需环境补强

为了避免 testcase 依赖“超时未写回”去猜 fault，需要补足统一观测：

1. load/store 是否发生 fault。
2. fault 属于 page/access 哪一类。
3. fault 是否带出稳定的地址/请求元信息。
4. fault 后是否出现 replay、redirect、nuke。
5. fault 请求是否仍错误地产生正常 writeback。

若当前 DUT 导出不够稳定，优先补 `env/monitor/facade`，不要直接在 testcase 中散落内部信号探针。

### 5.4 建议新增 sequence/helper

建议在 `src/test/python/MemBlock/sequences/mmu_sequences.py` 增加：

- `MmuPageTableFaultInstallSequence`
- `MmuPmpFaultConfigSequence`
- `MmuFaultTriggerSequence`
- `MmuFaultRecoverySequence`

建议在 env facade 增加：

- `install_invalid_sv39_mapping(...)`
- `install_sv39_leaf_with_perm(...)` [done]
- `configure_smode_access(...)` [done]
- `wait_load_fault_observed(...)`
- `wait_store_fault_observed(...)`
- `wait_no_normal_writeback_for_rob(...)`
- `sample_fault_state(...)`

### 5.5 验收标准

每类 fault 至少满足：

1. fault 类型可稳定观测。
2. fault 不串类。
3. fault 不退化成 silent timeout。
4. fault 后系统能收尾。
5. 修复条件后可恢复正常访问。

## 6. P0：TLB miss / refill / re-hit 专题回归

### 6.1 目标

把当前“能 page walk 成功”升级成“能证明 TLB 层级 miss/refill/hit 行为正确”。

### 6.2 建议场景矩阵

1. 首次 translated load：deterministic miss -> PTW refill -> writeback 成功。
2. 同一 VA 二次访问：应命中，不再重新 page walk。
3. `sfence` 后再访：应重新 miss / refill。
4. root-A / root-B 切换后访问同 VA：应重新建立 translation。
5. 不同 VA 交错访问，覆盖不同层级 TLB hit/miss 组合。

### 6.3 重点断言

1. `ptw_responder.trace` 中 A/D beat 数符合预期。
2. 首次访问与二次访问的 PTW 活动量不同。
3. 访问成功时不误判为 outer fault 或 cache error。
4. `sfence` 或 root 切换后，旧 translation 不应错误残留。

### 6.4 预期收益

- 提升 `TLBFA*`、`L2TLB.sv`、`LLPTW.sv`、`PtwCache.sv` 覆盖率。
- 为更复杂的 replay/fault 场景提供稳定的翻译背景。

## 7. P1：PBMT translated semantic regression

### 7.1 目标

把当前 PBMT 能力从“helper 已打通、部分路径可激活”进一步推进到“cacheable / NCIO / MMIO 三分类语义更稳定”。

### 7.2 建议场景

1. translated cacheable load/store：作为对照组。
2. translated NCIO load：验证 outer 路径、非 MMIO 分类。
3. translated MMIO load：验证 MMIO 分类。
4. translated NCIO store：验证 outer write + drain 闭环。
5. translated MMIO store busy：验证 younger cacheable load retire 被阻塞，但不应误报 fault。
6. cacheable / NCIO / MMIO mixed-path：验证分类与提交语义不串扰。

### 7.3 收口原则

1. capability probe 与 strict semantic regression 分层维护。
2. 对已知 DUT gap，保留精确条件 `xfail`，不要放宽成整类 smoke。
3. 只有在 debug 分类、store shadow、drain 收尾都稳定后，才把场景升级为硬断言。

## 8. P2：MMU 与 replay/nuke/fault 交叉场景

### 8.1 目标

在基础 translation/fault/TLB 场景稳定后，继续补强多机制交叉行为，尤其是：

- MMU + replay
- MMU + matchInvalid/dataInvalid
- MMU + redirect/nuke
- MMU + exception/fault recovery

### 8.2 建议方向

1. 继续补 root-A/root-B 切换下的 related-address replay 场景。
2. 把 fault 与 replay 的排他关系做成通用断言，而不是只在单个 testcase 中验证。
3. 补强 translated younger load 遇到 older store / MMIO / permission change 的边界行为。
4. 区分“真正的 fault”与“合法的重放/阻塞/redirect”。

## 9. 为执行 testcase 计划所需的 env 调整

本节只讨论“为了把 `mmu_test_todo.md` 中的 testcase 稳定落地，env 还需要补什么”，不重复 testcase 本身的场景定义。

### 9.1 P0：统一 hit/miss/refill/flush 观测

#### 目标

让 testcase 可以用统一语义断言：

1. 这次访问是否 miss。
2. 这次 miss 是否触发 refill。
3. 这次访问是否命中既有 translation。
4. flush 后是否真的重新 miss。

#### 建议补强

1. 在现有 `first_l2_tlb_req`、PTW trace 基础上，整理成可复用的 `MmuTlbAccessResult` 风格摘要。
2. 把“首次 miss / re-hit / flush 后 re-miss”的判定逻辑统一下沉到 sequence/helper，而不是散落在 testcase。
3. 为 `sfence/hfence` 前后访问提供统一的 warmup / reprobe 摘要结构。

### 9.2 P0：补齐自定义页表编码 helper

#### 目标

避免 testcase 直接手拼 PTE 位，从而能稳定覆盖 `TLBFA` entry 的更多编码维度。

#### 建议补强

1. `install_sv39_leaf_with_perm(...)`
   - 支持 `R/W/X/U/G/A/D/PBMT` 参数化。
2. `install_invalid_sv39_mapping(...)`
   - 支持缺失 leaf、`V=0`、权限不满足等 fault 形态。
3. `install_sv39_mapping_at_level(...)`
   - 支持更高层级 leaf / superpage。
4. `install_g_sv39_leaf_with_perm(...)`
   - 支持 guest-side permission / `g_pbmt` / `U` 等参数化。
5. `build_two_stage_config(...)` 对应的公共 sequence/helper 化
   - 避免 testcase 自己拼 VS/G-stage 页表页 identity mapping。

### 9.3 P1：补齐 selective flush 与上下文切换 facade

#### 目标

把 selective flush、ASID/VMID 切换和 root switch 收敛成语义化公共接口。

#### 建议补强

1. `pulse_sfence(...)`
   - 显式支持 `all/specific addr`、`all/specific asid` 语义。
2. `pulse_hfence_vvma(...)`
   - 显式支持 `all/specific addr`、`all/specific asid`。
3. `pulse_hfence_gvma(...)`
   - 显式支持 `all/specific addr`、`all/specific vmid`。
4. `switch_sv39_root(...)` / `switch_two_stage_context(...)`
   - 统一根页表、ASID、VMID、`priv_virt` 切换与必要 settle。
5. 提供“切换上下文后访问并断言旧 translation 不残留”的 sequence 壳层。

### 9.4 P1：补齐 fault / recovery 观测面

#### 目标

让 testcase 不再通过“等不到 writeback”去猜 fault。

#### 建议补强

1. `wait_load_fault_observed(...)`
2. `wait_store_fault_observed(...)`
3. `wait_no_normal_writeback_for_rob(...)`
4. `sample_fault_state(...)`
5. fault 后 replay / redirect / nuke / outstanding 收敛摘要

原则：

1. 若当前 DUT 导出不稳定，优先补 monitor/facade。
2. 不在 testcase 中散落内部层级探针。

### 9.5 P1：把 refill 编码矩阵抽成可复用 sequence

#### 目标

让“不同 level / permission / context / fault_stage”的组合能通过参数化 sequence 生成，而不是在 tests 中复制时序。

#### 建议新增 sequence/helper

1. `MmuTlbMissRefillRehitSequence`
2. `MmuTlbSelectiveFlushSequence`
3. `MmuTlbContextSwitchSequence`
4. `MmuRefillEncodingMatrixSequence`
5. `MmuFaultRecoverySequence`

### 9.6 P2：补多来源 / 多读口流量入口

#### 目标

为 `TLBFA*` 多读口相关覆盖准备更接近真实负载的驱动与观测入口。

#### 建议补强

1. frontend `itlb` 与 backend scalar/MMU 访问的统一 timeline helper。
2. 多来源请求的 trace 汇总接口，避免 testcase 手工拼接多个 monitor。
3. 如果当前 fixture 能稳定做到同窗口交错，先把“交错请求”收口成公共 sequence；若未来能做到更强并发，再扩展为更严格模式。

### 9.7 文档与状态同步要求

每次 env 调整落地后，应同步更新：

1. `src/test/python/MemBlock/docs/mmu_env_design_and_usage.md`
2. `src/test/python/MemBlock/docs/mmu_test_todo.md`
3. `src/test/python/MemBlock/docs/coverage_summary.md`
4. `src/test/python/MemBlock/docs/coverage_todo.md`

## 10. 文档与状态同步 TODO

### 10.1 覆盖率文档

后续每完成一组专题，应同步更新：

- `src/test/python/MemBlock/docs/coverage_summary.md`
- `src/test/python/MemBlock/docs/coverage_todo.md`

至少记录：

1. 命中的新模块与覆盖提升方向。
2. 已转正的 capability gap。
3. 仍需保留的 DUT gap 与原因。

### 10.2 MMU 文档

后续如新增 fault helper、TLB refill sequence、PMP deny helper，应同步更新：

- `src/test/python/MemBlock/docs/mmu_env_design_and_usage.md`

避免设计文档仍停留在“只有基础 smoke 用法”的旧状态。

## 11. 下一步建议执行顺序

建议按下面顺序推进：

1. 先补 `miss/refill/re-hit/flush` 的统一观测与最小 testcase。
2. 再补 fault / recovery 观测面与 page/access fault testcase。
3. 然后补 selective flush、context switch 与 refill 编码矩阵。
4. 再收紧 PBMT translated NCIO/MMIO store 语义。
5. 最后扩展到多来源和 replay/nuke/fault 多机制交叉场景。

这样可以先把 `TLBFA` 的基础命中面立住，再去追异常分类、编码多样性和更复杂的交互状态。
