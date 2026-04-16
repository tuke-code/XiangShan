# MemBlock MMU Env TODO Plan

## 1. 文档目的

本文档用于整理 `src/test/python/MemBlock/` 当前关于 MMU/TLB/PTW/PMP/PBMT 的讨论结论，并把下一阶段工作收敛成一份可执行的 TODO plan。

它不重复 `mmu_env_design_and_usage.md` 中已经落地的设计细节，重点回答三个问题：

1. 当前 MMU 环境已经具备哪些能力。
2. 当前验证的薄弱点主要在哪里。
3. 下一阶段应按什么顺序补强，才最容易形成专题化、可回归、可收口的真实 DUT 验证。

相关文档：

- `src/test/python/MemBlock/docs/mmu_env_design_and_usage.md`
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

## 4. TODO 优先级总览

- `P0`：MMU 异常专题回归
- `P1`：TLB miss/refill/hit 专题回归
- `P1`：PBMT translated NCIO/MMIO semantic regression
- `P2`：MMU 与 replay/nuke/fault 交叉场景补强
- `P2`：覆盖率与状态文档同步更新

建议顺序：先补“分类和正确性”，再补“覆盖率和复杂交互”。

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
- `install_sv39_leaf_with_perm(...)`
- `program_pmp_deny_region(...)`
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

## 6. P1：TLB miss / refill / re-hit 专题回归

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

## 9. 文档与状态同步 TODO

### 9.1 覆盖率文档

后续每完成一组专题，应同步更新：

- `src/test/python/MemBlock/docs/coverage_summary.md`
- `src/test/python/MemBlock/docs/coverage_todo.md`

至少记录：

1. 命中的新模块与覆盖提升方向。
2. 已转正的 capability gap。
3. 仍需保留的 DUT gap 与原因。

### 9.2 MMU 文档

后续如新增 fault helper、TLB refill sequence、PMP deny helper，应同步更新：

- `src/test/python/MemBlock/docs/mmu_env_design_and_usage.md`

避免设计文档仍停留在“只有基础 smoke 用法”的旧状态。

## 10. 下一步建议执行顺序

建议按下面顺序推进：

1. 先补 fault 观测面与最小 page/access fault testcase。
2. 再补 deterministic TLB miss/refill/hit testcase。
3. 再收紧 PBMT translated NCIO/MMIO store 语义。
4. 最后扩展到 replay/nuke/fault 多机制交叉场景。

这样可以先把“异常分类是否正确”立住，再去追更复杂的状态组合与覆盖率提升。
