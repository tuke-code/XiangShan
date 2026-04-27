# LLPTW Coverage Analysis And TODO

## 1. 文档目的

本文档用于收口 `src/test/python/MemBlock/` 当前 `LLPTW` 模块的覆盖率现状、真实薄弱点和下一阶段补强顺序。

它重点回答四个问题：

1. 当前 `LLPTW` 到底覆盖到了什么程度。
2. 为什么已有 MMU/TLB testcase 仍没有把 `LLPTW` 打透。
3. 当前覆盖率空洞主要落在哪几类状态。
4. 下一批最值得补的 directed case 应该怎么排优先级。

相关文件：

- `src/test/python/MemBlock/data/toffee_report_full/line_dat/code_coverage.json`
- `src/test/python/MemBlock/docs/coverage_summary.md`
- `src/test/python/MemBlock/docs/coverage_todo.md`
- `src/test/python/MemBlock/tests/test_MemBlock_env_mmu_smoke.py`
- `src/test/python/MemBlock/tests/test_MemBlock_mmu_h_extension.py`
- `src/main/scala/xiangshan/cache/mmu/PageTableWalker.scala`

## 2. 当前覆盖率口径

### 2.1 旧文档快照

`coverage_summary.md` 当前记录的旧口径是：

- `LLPTW.sv`: line `34.9%`
- `LLPTW.sv`: branch `48.6%`

这份口径位于 2026-04-11 的覆盖率总结中，适合作为历史基线，但不应再直接当成当前最新结论。

### 2.2 历史本地产物

当前仓库内 `src/test/python/MemBlock/data/toffee_report_full/line_dat/code_coverage.json` 的本地统计为：

- line `1236 / 2488`，即 `49.68%`
- branch `190 / 334`，即 `56.89%`
- expr `132 / 302`，即 `43.71%`
- toggle `7260 / 13366`，即 `54.32%`

因此，当前更准确的结论应该是：

1. `LLPTW` 并不是“几乎完全没测到”。
2. 它已经明显高于 2026-04-11 那版旧快照。
3. 但相对 MMU 相关模块整体水平，它仍然属于明显偏低的一组。

### 2.3 2026-04-27 定向回归结果

本轮已落地 `LLPTW` 专题 testcase，并用 `toffee-report` 做了两轮定向统计：

1. `src/test/python/MemBlock/tests/test_MemBlock_mmu_llptw.py`
   - line `1735 / 2488`，即 `69.73%`
   - branch `231 / 334`，即 `69.16%`
   - expr `156 / 302`，即 `51.66%`
   - toggle `7548 / 13366`，即 `56.47%`
2. `env_mmu_smoke + mmu_fault + mmu_h_extension + mmu_tlbfa + mmu_llptw` 组合回归
   - line `1742 / 2488`，即 `70.02%`
   - branch `235 / 334`，即 `70.36%`
   - expr `157 / 302`，即 `51.99%`
   - toggle `7688 / 13366`，即 `57.52%`

因此，本轮结论应更新为：

1. 新增 directed case 已把 `LLPTW` 从旧本地基线 `49.68% / 56.89%` 抬升到 `69.73% / 69.16%` 左右。
2. 组合现有 MMU 回归后还能再涨一点，但增幅已经很小，说明主增益来自新的 `LLPTW` 专题 case，而不是继续堆已有 smoke。
3. 当前仍未达到 `80%`，剩余缺口主要卡在 `bitmap` 相关状态和更细的高位 entry 变体上。

### 2.4 与同组模块的相对位置

基于同一份本地 `code_coverage.json`：

- `HPTW`: line `93.9%`，branch `67.3%`
- `L2TLB`: line `64.9%`，branch `73.9%`
- `PtwCache`: line `80.9%`，branch `60.1%`
- `LLPTW`: line `49.68%`，branch `56.89%`

这说明当前 MMU 测试并不是普遍都只打到 very shallow smoke。更符合现状的判断是：

1. H-stage page walk 基础路径已经相对充分。
2. L2TLB / PtwCache 已经被 hit/miss/refill 基础场景带起来。
3. `LLPTW` 的独特薄弱点主要集中在自身的并发 entry、dup 合流、last-stage S2 translate 和 bitmap 相关控制逻辑。

## 3. 当前已经覆盖到的行为

现有 testcase 已经把 `LLPTW` 的以下行为稳定打通：

1. Sv39 4KB translated load 的基础 PTW 闭环。
2. DTLB fill / rehit / replacement 的基础 miss-refill-hit 行为。
3. two-stage `VS VA -> GPA -> HPA` 的 success-path 基础闭环。
4. two-stage rehit。
5. `hfence.vvma(all addr, all asid)` 后重访重新 miss/refill。
6. `hfence.gvma(all addr, all vmid)` 后重访重新 miss/refill。
7. VS-stage invalid leaf 导致的 guest/page fault。
8. G-stage invalid leaf 导致的 guest/page fault。
9. two-stage 最终 HPA 被 PMP deny 时的 access fault。
10. `MmuTwoStageWaveLoadSequence` 下的 6-entry queue pressure。
11. duplicate waiting / merge 的多波次 two-stage overlap。
12. 先占满低位 slot，再把主请求 / duplicate 压到 `entry3/4/5` 的 high-slot duplicate chain。
13. first-stage G-stage leaf invalid，即 VS root page table walk 本身的 guest/page fault。
14. `mbmc.BME` 打开后的 bitmap capability probe，不会把基础 two-stage load 打挂。

因此，当前 `LLPTW` 的问题不是“入口完全没通”，而是“只打到了单请求、基础闭环和少量 fault/control case”。

## 4. 为什么现有用例没有把 LLPTW 打透

### 4.1 当前 helper 以单请求串行为主

现有 `MmuTwoStageLoadSequence` / `MmuTwoStageFenceSequence` / `MmuTwoStageFaultSequence` 的实际执行方式，本质上都是：

1. 发一条 request。
2. 等该条 request 完成或 fault 收口。
3. 再发下一条 request。

这意味着 testcase 虽然覆盖了很多“类别”，但大多没有在时序上制造 `LLPTW` 最需要的压力：

- 多个 entry 同时在 flight
- 同 VPN duplicate request 重叠到来
- 前一条仍在 `mem_waiting` 时后一条进入
- first-stage HPTW 与 last-stage HPTW 串行排队

### 4.2 当前 Sv39 env 只支持 4KB leaf

当前 env 明确限制：

1. `install_sv39_mapping()` 仅支持 Sv39 `4KB` leaf。
2. `install_g_sv39_mapping()` 仅支持 G-stage Sv39x4 `4KB` leaf。

这直接导致 `LLPTW` 中一整块与下列主题相关的逻辑难以被触发：

- superpage
- bitmap check
- l0 shortcut + `jmp_bitmap_check`
- 不同 `way_info` / `cfs` 组合

所以 `state_bitmap_check` / `state_bitmap_resp` 和相关 `cf/cfs/from_l0` 大片分支，在现阶段基本仍属于 capability gap，而不是 testcase 组织问题。

### 4.3 当前 flush 只覆盖 all-addr/all-id

H extension 相关 case 已经覆盖 `hfence.vvma` / `hfence.gvma` 的全量 flush，但尚未覆盖：

- specific ASID
- specific VMID
- root switch
- targeted flush 与旧项残留隔离

因此，`LLPTW` 中与“已有 entry 如何在更细粒度失效后重新收敛”相关的分支还没有被足够触发。

## 5. 主要覆盖率空洞

结合 `LLPTW` RTL 结构，当前最重要的空洞可分为 4 组。

### 5.1 P0: 多 entry / duplicate / waiting / cache 合流

`LLPTW` 维护了一个小型多 entry 队列，并显式处理：

- `dup_vec`
- `dup_req_fire`
- `dup_vec_wait`
- `dup_vec_having`
- `dup_vec_bitmap`
- `dup_vec_last_hptw`
- `to_wait`
- `to_cache`

这部分逻辑的价值，在于验证“同一 last-level walk 被多条 request 复用”时，状态迁移是否正确，而不是简单重复发起多次 mem access。

本轮已补入：

1. 同一 VPN 的重叠 request。
2. `entry3/4/5` 的 high-slot duplicate chain。
3. delayed PTW responder 下的 queue pressure。

但仍残留两类关键空洞：

1. `state_last_hptw_resp -> state_mem_out` 的更多 high-slot 收尾变体仍不足。
2. `dup_vec_last_hptw / dup_vec_bitmap / to_cache` 相关窗口仍未被稳定打热。

### 5.2 P0: allStage 的 first-stage / last-stage HPTW 串行状态

`LLPTW` 对 `allStage` 有两段关键状态：

- `state_hptw_req/state_hptw_resp`
- `state_last_hptw_req/state_last_hptw_resp`

它们分别对应：

1. first stage-2 translation
2. VS leaf 成功后的 final stage-2 translation

本轮已补到：

1. first-stage HPTW 直接 `guest/page fault`。
2. last-stage HPTW `guest/page fault`。
3. final-stage PMP access fault。
4. `state_hptw_resp -> state_mem_waiting` 的 duplicate overlap。

但仍明显不足的是：

1. first-stage HPTW 的 `perm_fail/gaf` 细分组合。
2. `state_last_hptw_resp -> state_mem_out` 的更多 multi-entry 收尾。
3. 与 targeted flush/root switch 交叉时的 allStage 深状态。

### 5.3 P1: targeted flush / root switch / re-probe 组合

目前已覆盖的是：

- warmup
- all-addr `hfence.vvma`
- all-addr `hfence.gvma`
- reprobe miss/refill

但真正更能提升 `LLPTW` 价值的，是下面这些组合：

1. specific ASID flush 只冲掉目标 translation，不影响其他 ASID。
2. specific VMID flush 只冲掉目标 G-stage translation。
3. root-A/root-B 切换后，相同 VA 重新进入 LLPTW。
4. root switch 与旧 duplicate/waiting entry 的边界。

### 5.4 P2: bitmap / l0 shortcut / superpage

`LLPTW` 内部有显式 bitmap 流程：

- `state_bitmap_check`
- `state_bitmap_resp`
- `jmp_bitmap_check`
- `from_l0`
- `way_info`
- `cfs`
- `cf`

当前这些分支大面积未命中的根因仍然成立，而且本轮还新增了一个验证事实：

1. env 还没有大页安装能力。
2. 虽然 testcase 已显式拉起 `mbmc.BME` 并做 bitmap capability probe，但覆盖率基本没有变化，说明仅靠当前 4KB-only 安装能力和现有 facade，还不足以把 `bitmap` 主状态机真正跑热。

因此这一组仍应视为“环境能力补齐后的第二阶段专题”，不要再把“继续堆 load case”误当成能把 `LLPTW` 拉过 `80%` 的主手段。

## 6. 建议补强顺序

### 6.1 第一优先级：LLPTW queue pressure + duplicate directed

建议新增一组专题 case，目标是主动命中：

1. 两条相同 VPN 的 translated load back-to-back。
2. 第一条卡在 PTW responder 时，第二条进入 duplicate waiting。
3. 再插入不同 VPN 请求，逼出多 entry 占用。
4. 把 `response_delay_cycles` 拉长，确保状态真实停留在 `mem_waiting` / `hptw_resp`。

预期收益：

1. 明显提升 `to_wait` / `dup_wait_resp` / `to_cache` 相关分支。
2. 提升高位 `entry3/4/5` 的状态切换覆盖。
3. 直接补 `LLPTW` 最有特征的一组控制逻辑。

### 6.2 第二优先级：two-stage deep-state matrix

建议围绕同一 two-stage 地址空间，至少补下列组合：

1. success -> success
2. success -> rehit
3. first-stage guest fault
4. last-stage guest fault
5. first-stage permission fail
6. final-stage PMP deny

关键不是增加 case 数量，而是让不同 fault/success 组合落在：

- first-stage HPTW
- final-stage HPTW
- mem-out
- duplicate waiting

这些不同收口点上。

### 6.3 第三优先级：specific ASID/VMID flush 和 root switch

在现有 `MmuTwoStageFenceSequence` 基础上，优先补：

1. `hfence.vvma` specific ASID。
2. `hfence.gvma` specific VMID。
3. root-A/root-B 切换后复用相同 VA。

这组 case 的价值是把“translation 是否被重新建立”的结论，从全量 flush 扩展到更细粒度的失效语义。

### 6.4 第四优先级：bitmap/superpage 能力补齐后再补 case

这一组不建议直接把 testcase 作为第一步。更合理的顺序是：

1. 扩 `env.mmu.install_sv39_mapping()` 支持 `2MB/1GB`。
2. 明确 bitmap CSR 的最小 facade 契约。
3. 再补 `jmp_bitmap_check`、`from_l0`、`cfs/cf` 的 directed case。

否则现在写出的 testcase 只能停留在 capability-probe，很难形成稳定回归。

## 7. 建议验收目标

本轮已达成的阶段性目标：

1. line coverage 从 `49.68%` 提升到 `69.73%`。
2. branch coverage 从 `56.89%` 提升到 `69.16%`。
3. duplicate waiting case、first-stage fault、last-stage fault、PMP deny 和 high-slot queue pressure 都已有 real DUT 回归。

基于当前结果，后续目标应改成：

1. 若仅在当前 env/test API 能力内补 testcase，现实目标是把 `LLPTW` 稳在 `70%` 左右，而不是继续承诺 `80%+`。
2. 若必须推进到 `80%+`，需要先补环境能力：
   - superpage / bitmap install 能力
   - bitmap CSR / wakeup 的最小 facade
   - 更细的 first-stage permission / root-switch 可控注入点

## 8. 当前直接行动项

本轮已完成：

1. `sequences/mmu_sequences.py`
   - 新增 `MmuAccessWave`
   - 新增 `MmuTwoStageWaveLoadSequence`
   - `MmuTwoStageFaultSequence` 新增 `fault_target_gpa`
2. `tests/test_MemBlock_mmu_llptw.py`
   - 新增 six-entry queue pressure
   - 新增 duplicate wait/merge
   - 新增 high-slot duplicate chain
   - 新增 first-stage / last-stage guest fault
   - 新增 final-stage PMP deny
   - 新增 bitmap-enable capability probe

接下来更合理的动作是：

1. 先在 env 层补 `superpage/bitmap` 能力，而不是继续堆同类 load smoke。
2. 再补 `specific ASID/VMID flush + root switch` 与高位 entry overlap 的交叉矩阵。
3. 若后续重新跑覆盖，优先看 `2440-3499` 与 `3500-3809` 这两段是否真正下降；它们当前分别代表 bitmap/high-slot 和记忆化状态迁移的主体缺口。
