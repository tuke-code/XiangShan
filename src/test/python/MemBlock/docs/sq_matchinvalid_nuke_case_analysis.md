# `sq_datainvalid_matchinvalid_nuke` 用例设计原理与分析

## 1. 文档目的

本文档解释 `sq_datainvalid_matchinvalid_nuke` 用例为什么这样设计，以及它到底想证明什么。

对应实现位于：

- sequence: `src/test/python/MemBlock/sequences/memblock_sequences.py`
- testcase: `src/test/python/MemBlock/tests/test_MemBlock_replay.py`

本文档不重复介绍 MMU env 的公共接口；如需了解 `env.mmu` 的设计和标准用法，请先看：

- `src/test/python/MemBlock/docs/mmu_env_design_and_usage.md`

当前实现已经从“一个大而全的 `ScalarSqDataInvalidMatchInvalidSequence`”重构为：

1. testcase 先调用 `MmuSv39AddressSpaceInstallSequence` 配置 root-A/root-B
2. testcase 再调用 `ScalarSqDataInvalidMatchInvalidTriggerSequence` 触发专题行为

这样后续其他 testcase 可以直接复用 MMU 配置部分，而不必复制这个专题用例的 trigger 逻辑。

## 2. 目标行为

本用例的目标不是“证明 MMU 可以工作”，而是证明以下更具体的组合行为：

1. younger load 落在 dcache hit 路径上
2. `dcacheError` 不触发
3. StoreQueue forward response 同时给出：
   - `dataInvalid = 1`
   - `matchInvalid = 1`
4. younger load 在 `mem_to_ooo` 侧触发 `memoryViolation`
5. 这个 `memoryViolation` 的 `level = 1`，代表 flush 级 redirect / nuke

从验证角度看，这个场景想卡住的不是普通 cache miss 或普通 forward fail，而是“older store 与 younger translated load 在虚拟地址相关性上碰撞、但在物理地址判定上失配”的边界行为。

## 3. 设计直觉

这个场景的关键直觉是：

- younger load 在当前 satp 下会把 `main_va` 翻译到 root-B 对应的物理地址
- older store 则保留了同一个虚拟地址字面值
- 因此 SQ 在前递相关性检查中会认为“这条 younger load 可能与 older store 相关”
- 但当物理地址或安全前递条件继续细化时，又发现不能直接把该 store 的数据当作稳定前递结果

于是 DUT 会给出：

- `matchInvalid = 1`
- `dataInvalid_valid = 1`
- 并最终在 `memoryViolation` 上要求 flush / nuke

## 4. 为什么最终采用“bare older store + Sv39-B younger load”

### 4.1 曾尝试的直观方案

更直观的做法原本是：

1. root-A 下发 older store
2. 再切到 root-B 下发 younger load
3. 利用相同 VA 在 root-A/root-B 下翻译到不同 PA 来触发 mismatch

但在真实 DUT 上，这条路径对 store 侧并不稳定。具体表现是：

1. store `STA` 在 MMU 背景下可能出现 `addr=0`
2. 或出现 `nc=True`
3. 或无法稳定留下后续 `matchInvalid` 所需的 store 视图

这说明“store translation 也一起纳入 root-A/root-B 切换”虽然概念上更对称，但对当前 DUT/环境来说并不是最稳定的可观测组合。

### 4.2 最终落地方案

最终 sequence 选择了更稳定的一条路径：

1. 先在 bare 模式下对 root-B 目标物理地址做 cache warmup
2. 再在 bare 模式下分配 older store，并只发 `STA(main_va)`
3. 之后切到 Sv39 root-B
4. 发一条 TLB prime load，确保 translation 路径已建立
5. 再发 younger main load(`main_va`)

这样做的价值是：

1. older store 的地址视图能稳定保留为 `main_va` 这个字面值
2. younger load 会在 root-B 下稳定翻译到目标 cacheable 物理页
3. dcache 命中条件由前面的物理 warmup 保证
4. replay/nuke 行为更稳定地落在目标组合上

## 5. 场景步骤拆解

当前用例可以拆成六步：

### 5.1 安装 root-A/root-B 映射

当前 testcase 会先通过 `MmuSv39AddressSpaceInstallSequence` 安装：

1. `main_va -> pa_base_a`
2. `main_va -> pa_base_b`
3. `tlb_prime_va -> pa_base_b`

这里 root-A 仍然保留，是为了文档化地表达“同一 VA 在两套页表下可映射到不同 PA”的场景背景；但当前稳定实现主要用到了 root-B 的 translated load 路径。
把 install sequence 设计成“单地址空间配置、可多次调用”的形式，比把 A/B 强耦合进一个大 config 更利于复用。

### 5.2 对 root-B 物理地址做 warmup

sequence 会先把：

- `main_pa_b`
- `tlb_prime_pa_b`

预置到黄金内存，并先发一条 bare 物理 load 到 `main_pa_b`。

这个 warmup 的目的有两个：

1. 把目标 cache line 提前带入 dcache
2. 证明“物理地址本身是可正常读写回的”，避免把后续失败误归因到 cache/内存闭环本身

### 5.3 在 bare 模式下发 older store 的 `STA(main_va)`

这一步只做地址发射，不立即补 `STD` 数据。目的有两个：

1. 让 SQ 中留下 older store 的地址相关性信息
2. 暂时不让它成为一个可安全前递的稳定 store

这是 `dataInvalid` 能被激活的重要前提之一。

### 5.4 切到 Sv39 root-B，并发 TLB prime

接着 trigger sequence 会：

1. `env.mmu.enable_sv39(root_pt_addr=root_pt_b)`
2. 发送 `tlb_prime_va`

TLB prime 的作用不是业务检查本身，而是把 translation 路径预先打热，减少主 load 时把“首个 page walk 建链”与“目标 replay 行为”混在一起。

### 5.5 发 younger main load

主 load 使用：

- `addr = main_va`
- `sq_ptr = younger_sq_ptr`

此时 younger load 会：

1. 在 root-B 下翻译到 `main_pa_b`
2. 命中已经 warmup 的 dcache line
3. 与 older store 发生 SQ 相关性检查

sequence 随后等待：

- `sq_forward_event`
- `memory_violation`

同时采集 replay window 和 transport stats。

### 5.6 在 violation 之后补 `STD` 并收尾

由于当前 DUT 的后续行为并不是“主 load 永远不回写”，而是会在 nuke/replay 之后再次完成，因此 sequence 在观测到目标 invalid+nuke 组合后，还会：

1. 给 older store 补 `STD`
2. 推进 store commit
3. 为 main load 登记 replay completion expectation
4. 等待主 load 后续真正完成写回

这个动作不是在改变场景目标，而是在把真实 DUT 的后续收敛行为也纳入 testcase 管理，避免测试因为“前半段行为对了、后半段收尾没人接”而误失败。

这里特别把“激活 root-B + TLB prime”放在 trigger sequence 内部，而不是单独先跑 activate sequence，是因为这个 testcase 依赖一条关键顺序：

- older bare store 的 `STA(main_va)` 必须先发生
- 之后才能切到 Sv39 root-B

如果把 activation 独立并放到 testcase 前面，older store 就会落在 MMU 背景下，重新回到当前 DUT 上不稳定的旧路径。

## 6. 断言语义

当前 testcase 锁定的断言分成三组：

### 6.1 核心功能断言

1. `sq_forward_event["data_invalid_valid"] == 1`
2. `sq_forward_event["match_invalid"] == 1`
3. `sq_forward_event["forward_invalid"] == 0`
4. `memory_violation["valid"] == 1`
5. `memory_violation["level"] == 1`

这组断言说明：

- DUT 不是简单地给出一个“彻底没法 forward”的 `forwardInvalid`
- 而是在“匹配关系存在，但数据不可安全使用”的细粒度状态上触发了 flush 级处理

### 6.2 路径断言

1. `outer_request_count` 从主 load 发射到最终收敛都不增长
2. `dcache_a_request_count` 从主 load 发射到最终收敛都不增长
3. `dcache_d_response_count` 从主 load 发射到最终收敛都不增长
4. `dcache_miss_signal == 0`（若端口存在）
5. `dcache_error_valid == 0`
6. 不允许同一 ROB 的 replay cause 再从 `replay_queue` / `replay_lane` / `ldu` / `nc_out` 暴露出来

这组断言用来证明主 load 的目标路径确实是：

- dcache hit
- 非 outer
- 非 miss/refill
- 非 error
- 非“redirect 之后仍继续向 LSQ 建立 replay 去路”

也就是说，当前 testcase 不是在用 cache miss 或 cache error 冒充 nuke 条件。

### 6.3 收尾断言

1. older store 最终进入 committed
2. store drain 成功
3. `env.assert_no_outstanding()`

这组断言确保 testcase 不只是“打到一个波形点”，而是整个场景可完整收敛、可进入回归。

## 7. 当前 testcase 的收口口径

在这轮语义收紧后，主 load 的后续恢复路径需要满足两个事实：

### 7.1 flush 级 `memoryViolation` 与 LSQ replay 路径应保持排他

虽然我们关注的核心是：

- `matchInvalid`
- `dataInvalid`
- `memoryViolation`

当前 testcase 不再接受“同一条 younger load 一边触发 flush 级 redirect，一边又继续把 replay cause 送往 LSQ”这种双去路语义。

因此对于同一 ROB：

- 允许观测到 `memory_violation` 本身
- 但不允许再从 `replay_queue` / `replay_lane` / `ldu` / `nc_out` 看到该 load 的 replay cause

当前稳定回归里，这条排他断言仍会命中已知 DUT bug：`DUTBUG-matchinvalid-redirect-replay-dual-path`。该问题的 commit 记录与处理状态见 `src/test/python/MemBlock/docs/BUGS.md`；pytest 侧暂时把它标记成精确条件触发的 `xfail`，等待后续 DUT 版本修复后再恢复为硬失败。

### 7.2 主 load 最终会完成写回，且数据来自 store

在当前真实 DUT 下，主 load 的后续完成数据是 older store 补齐后的 store data，而不是最初 warmup 的 memory data。

这也是 sequence 在 violation 之后补 `STD`、并把 replay completion expectation 改成 `store_txn.data` 的原因。

如果不承认这条真实行为，testcase 会在 compare 或 writeback monitor 上假失败。

## 8. 为什么这个用例值得保留

这个 testcase 的价值在于它不是普通 replay smoke，而是同时覆盖了四类机制的交点：

1. MMU translation
2. dcache hit 路径
3. StoreQueue invalid-match 判定
4. memoryViolation / nuke 恢复

它能帮助回答一个更高价值的问题：

- “当 younger translated load 在命中 cache 的同时，又与 older store 形成不安全的前递相关性时，DUT 是否会走对的 invalid + nuke 流程？”

这比单独验证：

- “MMU 能不能翻译”
- “SQ 能不能 forward”
- “memoryViolation 能不能拉起”

都更接近真实复杂场景。

## 9. 后续可扩展方向

基于当前 testcase，后续可以继续扩展三类变体：

1. root-A/root-B 不同权限位，而不是不同物理页
2. 同一场景下引入多条 younger load，观察 replay/nuke 扩散范围
3. 补充更细的白盒断言，锁定 `matchInvalid` 与 `s2PaddrNoMatch` 之间的直接对应关系

在继续扩展之前，建议保持当前 testcase 的主目标不变：优先保住“真实 DUT 下稳定可回归的 invalid-match-nuke 证明点”。
