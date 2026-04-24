# MemBlock MMU H Extension Directed Cases

## 1. 文档目的

本文档用于收口 `src/test/python/MemBlock/` 当前 H extension / two-stage MMU directed case 的场景边界、断言口径和已知 DUT 限制。

它回答三个问题：

1. 当前 env/facade 已经把哪些 H 扩展控制面变成公开契约。
2. 当前 testcase 实际覆盖了哪些 VS-only / two-stage 行为。
3. 哪些条件已经被确认是 DUT 限制，应使用精确 `xfail` 而不是把整组测试降级。

## 2. 当前落地范围

当前已落地的公共接口包括：

- `env.mmu.enable_vs_sv39()`
- `env.mmu.enable_two_stage_sv39()`
- `env.mmu.install_vs_sv39_mapping()`
- `env.mmu.install_g_sv39_mapping()`
- `env.mmu.pulse_hfence_vvma()`
- `env.mmu.pulse_hfence_gvma()`
- `env.wait_load_fault_observed()`
- `env.sample_mmu_fault_state()`
- `TwoStageSv39AddressSpaceInstallSequence`
- `MmuVsStageLoadSequence`
- `MmuTwoStageLoadSequence`
- `MmuTwoStageFenceSequence`
- `MmuTwoStageFaultSequence`

首版范围明确限定为：

1. `Sv39 + 4KB-only`
2. load-side priority，高于 store-side
3. 先打通 control/fault，再推进 full success-path regression

## 3. 当前 testcase 口径

当前 H extension 专题 testcase 分三簇：

### 3.1 API / facade smoke

- `enable_vs_sv39()` 的 `vsatp/priv_virt` 保活
- `enable_two_stage_sv39()` 的 `vsatp/hgatp/priv_virt` reset 后重放
- `hfence.vvma/gvma` 的 `hv/hg/rs1/rs2/id/addr` 驱动位
- `install_g_sv39_mapping()` 与 two-stage translated preload helper

这些 case 的设计重点不是“证明 real DUT 已经支持完整两阶段翻译”，而是先把 Python env 新暴露的 H 扩展控制面钉成公开契约：

1. `enable_vs_sv39()` / `enable_two_stage_sv39()` 是否真的驱动了 `vsatp/hgatp/priv_virt`
2. `idle_inputs()` / `reset()` 后 facade 是否还能正确重放 active mode
3. `hfence.vvma/gvma` helper 是否把 `hv/hg/rs1/rs2/id/addr` 正确送到 DUT
4. `install_g_sv39_mapping()` 与 two-stage translated preload 是否真的按“最终 HPA”落内存

换句话说，这一簇先证明“桥搭好了”，而不是一开始就把所有 H 语义塞进同一条 must-pass case。

### 3.2 硬断言通过的 fault / control case

- VS-only translated load smoke
- VS-stage guest page fault smoke
- G-stage guest page fault smoke
- two-stage PMP access fault smoke

这一簇的设计思路是优先选择当前 DUT 已经稳定导出的行为：

1. `VS-only translated load`
   - 先证明 `vsatp + priv_virt` 这条最小虚拟化背景能独立闭环
   - 不混入 `hgatp`，避免把“VS-stage 本身没搭好”和“两阶段组合还未收口”混在一起
2. `VS-stage guest page fault`
   - 通过把 VS leaf PTE 改成 `0`，直接验证 stage-1 fault 是否能收口到 guest/page-fault 族
   - 这条 case 重点是“fault 族别正确”，不是精细 trap code
3. `G-stage guest page fault`
   - 先通过 VS-stage 把 VA 解析成 GPA，再把 G-stage leaf 置 invalid
   - 重点验证 stage-2 fault 确实发生，并尽量把 `gpaddr` 与 stage-1 GPA 对上
4. `two-stage PMP access fault`
   - 保持两阶段页表都正确，只在最终 HPA 上施加 PMP deny
   - 这条 case 的目的就是证明 access fault 不会被串类成 guest page fault

这也是为什么首批 hard-assert 先押 fault/control，而不是直接押 two-stage success path。

### 3.3 当前已转正的 success-path / flush case

- `two_stage_sv39_basic_load_smoke`
- `two_stage_rehit_smoke`
- `hfence_vvma_flushes_vs_translation_smoke`
- `hfence_gvma_flushes_g_stage_translation_smoke`

当前这 4 条 case 已经从“能力探针”转为正常硬断言，覆盖目标分别是：

1. `two_stage_sv39_basic_load_smoke`
   - 证明最基本的 `VS VA -> GPA -> HPA -> normal load writeback` 闭环已经成立
2. `two_stage_rehit_smoke`
   - 在同一 VS VA 上连续访问两次，区分“首访 miss/refill”和“二访 re-hit”
3. `hfence_vvma_flushes_vs_translation_smoke`
   - 先 warmup 建立两阶段 translation
   - 再发 `hfence.vvma(all addr, all asid)`
   - 最后同 VA 重访，要求重新 miss/refill
4. `hfence_gvma_flushes_g_stage_translation_smoke`
   - 同样先 warmup
   - 再发 `hfence.gvma(all addr, all vmid)`
   - 最后重访，要求重新 miss/refill

它们构成了后续 `specific ASID/VMID`、`root switch`、更深 `TLBFA* / PtwCache / LLPTW` 场景的基础能力面。

## 4. 这组用例的整体设计思路

为了让 H extension 验证从第一天起就可维护，这组 testcase 按下面的收口顺序设计，而不是把所有语义揉成一条“全能 smoke”：

### 4.1 先拆控制面，再拆行为面

第一层先验证：

- active mode 是否被 facade 正确驱动
- reset / idle 后是否还能重放
- H fence helper 是否把 CSR-like flush 输入送对

只有控制面先稳定，后面的 load/fault/flush 行为差异才有解释力。

### 4.2 两阶段地址空间显式拆成 VS-stage 与 G-stage

`_build_two_stage_config()` 不是只给一条数据页 mapping，而是同时显式安装：

1. VS VA -> GPA 的 stage-1 leaf
2. GPA -> HPA 的 stage-2 leaf
3. VS root page table 与中间页表页在 G-stage 下的 identity mapping

这样做的原因是：

- `vsatp` root 与 VS 页表页在 two-stage 背景下按 guest-physical 语义理解
- 如果不把这些 guest-physical 页表页再通过 G-stage 映射到 host-physical，VS walker 根本拿不到自己的页表页
- 因此 testcase 必须把“数据页 mapping”和“页表页自身在 stage-2 下可访问”一起装好

### 4.3 translated preload 一律按最终 HPA 预置

testcase 只声明“CPU 最终应该读到什么数据”，不手算最终 HPA。`TwoStageSv39AddressSpaceInstallSequence` 负责：

1. 先解析 `VA -> GPA`
2. 再解析 `GPA -> HPA`
3. 最后把 preload 写到最终 HPA

这样能避免 testcase 中散落两阶段地址合成逻辑，也让后续 fault case 更容易复用同一套地址空间配置。

### 4.4 首版先选 fault/control 作为 must-pass，再逐步把 success-path 转正

当前 DUT 已经稳定导出：

- `vsatp/hgatp/priv_virt`
- `gpaddr`
- guest/page/access-fault 相关异常位

因此首版设计先把这些“已经稳定可观测”的点做成 hard-assert；对于当时尚未稳定的 two-stage success path，则先保留 real DUT case 并用精确 `xfail` 记录 gap。当前这条路径已经完成转正，保留下来的只有 `gpaddr` 导出稳定性这一条精确 `xfail`。

这样做的好处是：

1. 不会因为 success path 还没转正，就把整组 H extension 测试全部降级
2. 现有 fault/control coverage 仍能持续守住
3. 后续 DUT 修复后，只需要逐条把 `xfail` 还原成硬断言

## 5. 已知 DUT 限制

### 5.1 `hfence.vvma` address-match limitation

当前不把 `hfence.vvma` 的按地址精确 flush 作为首批 must-pass 断言。

原因是 RTL 在 VS large-page + G small-page 组合下，L1TLB address-match 已知有限制；因此首批主线只验证：

1. `all addr / all asid`
2. `all addr / specific asid`

### 5.2 已修复的 success-path / flush 根因

这轮转正涉及两类问题，且二者缺一不可：

1. Python env 的 G-stage leaf PTE helper 之前沿用了普通 Sv39 leaf 编码，默认 `U=0`
   - 但当前 DUT 的 HPTW guest-fault 判定会把 `!perm.u` 也并入 G-stage fault 条件
   - 因此 testcase 想表达“合法 G-stage leaf”时，也会被 DUT 当成 guest-fault 候选
   - 当前 fix 是让 `install_g_sv39_mapping()` 生成的 G-stage leaf PTE 固定带 `U=1`
2. RTL 的 two-stage success path 之前把合法的 VS non-leaf 过早折叠成 fault-like response
   - `LLPTW` 在 all-stage walk 中把 `!isLeaf()` 直接并入 `vsStagePf`
   - `L2TLB.contiguous_pte_to_merge_ptwResp()` 也把 all-stage non-leaf 再次编码成 `pf`
   - 结果是本应继续对“下一层 VS 页表页 GPA”做 G-stage 翻译的路径，被提前收口到 guest-fault 族

修复后当前实测状态是：

1. `two_stage_sv39_basic_load_smoke`
   - 已能稳定完成正常 writeback
2. `two_stage_rehit_smoke`
   - 已能稳定区分首访 miss 与二访 hit
3. `hfence_vvma/gvma all-addr` smoke
   - 已能在 warmup 后重新 miss/refill

### 5.3 H fence helper 曾经存在的验证环境输入问题

这轮还确认了一个独立的 env 问题：`pulse_sfence()` / `pulse_hfence_vvma()` / `pulse_hfence_gvma()` 的 Python facade 之前把 `rs1/rs2` 当成“直接写 DUT bundle”的位。

但当前 RTL 真正消费的是：

1. `bits.rs1 = 1`
   - 表示 `rs1 == x0`
   - 语义是 `all addr`
2. `bits.rs1 = 0`
   - 表示提供了具体地址
   - 语义是 `specific addr`
3. `bits.rs2 = 1`
   - 语义是 `all asid/vmid`
4. `bits.rs2 = 0`
   - 语义是 `specific asid/vmid`

因此旧 helper 的默认值虽然文档写的是“all addr / all asid(vmid)”，实际发给 DUT 的却是“specific addr / specific id”的编码。`hfence.gvma` 还额外把 `hv` 误拉高了。

当前 env 已修正为：

1. facade 参数保持语义化：
   - `rs1=False` -> `all addr`
   - `rs1=True` -> `specific addr`
   - `rs2=False` -> `all asid/vmid`
   - `rs2=True` -> `specific asid/vmid`
2. helper 内部再把这些语义翻译成 DUT bundle 所需的反向编码
3. `hfence.gvma` 只驱动 `hg=1`

### 5.4 当前仍保留的精确 `xfail`：G-stage guest fault 下 `gpaddr` 尚未稳定对齐 stage-1 GPA

当前 G-stage guest page fault 已能稳定体现为 guest/page-fault 族，但 `gpaddr` 的导出值还没有稳定对齐 testcase 侧解析出的 stage-1 GPA。

因此当前口径是：

1. guest/page-fault 族保持硬断言。
2. 若 `gpaddr` 未对齐 stage-1 GPA，则使用精确 `xfail` 记录该导出 gap。

这条 `xfail` 的意义是：

- 先保留“G-stage guest fault 确实发生”的硬断言
- 把更细的 guest-side 地址一致性单独作为 capability gap 记录
- 避免因为 `gpaddr` 还没稳定就把整个 G-stage fault case 一起降级

## 6. 后续转正顺序

建议按下面顺序推进：

1. `gpaddr == stage-1 GPA` 的精确 guest-fault 收口
2. `specific ASID` / `specific VMID` flush 隔离矩阵
3. `root-switch`

## 7. 相关文件

- `src/test/python/MemBlock/tests/test_MemBlock_env_mmu_smoke.py`
- `src/test/python/MemBlock/tests/test_MemBlock_mmu_h_extension.py`
- `src/test/python/MemBlock/sequences/mmu_sequences.py`
- `src/test/python/MemBlock/MemBlock_env.py`
