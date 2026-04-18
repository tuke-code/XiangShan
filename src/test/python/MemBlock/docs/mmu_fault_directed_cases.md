# `scalar load MMU fault` 定向用例说明

## 1. 文档目的

本文档说明 `test_MemBlock_mmu_fault.py` 以及 `test_MemBlock_scalar_load_pipeline_probe.py` 中复用的 load-fault matrix 场景在验证什么、为什么这样设计，以及它与一般 MMU smoke 的边界是什么。

对应实现位于：

- sequence: `src/test/python/MemBlock/sequences/mmu_sequences.py`
- testcase:
  - `src/test/python/MemBlock/tests/test_MemBlock_mmu_fault.py`
  - `src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py`

本文档不重复介绍 `env.mmu` 的控制面细节；这些内容见：

- `src/test/python/MemBlock/docs/mmu_env_design_and_usage.md`

## 2. 为什么单独写成 fault directed

普通 MMU smoke 主要回答“translation/PTW/PMP 这套控制面能否工作”。这批 fault 用例要回答的是更窄、更偏行为判定的问题：

1. 在已经形成 TLB hit 背景后，load fault 是否仍按预期落在异常写回，而不是重新走 PTW/miss。
2. translation fault、PMP deny 以及两者叠加时，当前 RTL 暴露出来的异常位集合是什么。
3. fault 路径是否会意外退化成其它 load pipeline 行为，例如 `memoryViolation`、outer request 或 dcache error。

因此这批用例并不追求把 translation 子系统“全打透”，而是把 fault 行为从“能否跑通 MMU”里拆出来，变成可重复、可解释、可收敛的 directed case。

## 3. 覆盖的用例簇

当前这批用例分成两层：

### 3.1 `test_MemBlock_mmu_fault.py`

它负责 fault 语义本身的 directed matrix：

1. `scalar_word_load_tlb_error_tlb_hit_smoke`
   - prime 与 main 都处在 `PTE_MODE_PAGE_FAULT`
   - 目标是证明第二次访问在 TLB hit 背景下仍给出 translation error 写回
2. `scalar_word_load_pmp_access_fault_tlb_hit_smoke`
   - page table 正常，但主访问切成 `PMP_MODE_DENY`
   - 目标是证明 fault 收口到 `LOAD_ACCESS_FAULT_BIT`
3. `scalar_word_load_tlb_error_plus_pmp_fault_overlap_smoke`
   - translation error 与 PMP deny 叠加
   - 目标是记录当前 RTL 的优先级窗口，而不是预设一个并未文档化的组合语义
4. `scalar_mixed_size_fault_matrix`
   - 对 `byte/half/word/doubleword` 逐个复用同一 fault 骨架
   - 目标是把 fault 行为扩展到不同 size/mask 组合，而不是只盯着 word load
5. `scalar_load_pmp_deny_region_hit_allow_outside_smoke`
   - 在同一 Sv39/TLB hit 背景下，对比 deny region 内 fault 与 region 外继续成功
   - 目标是证明 PMP 不只是“全开/全关”，而是按地址区域裁剪权限
6. `scalar_load_pmp_deny_region_then_restore_allow_smoke`
   - 先命中 deny-region fault，再撤销 deny 并重发同一 VA
   - 目标是证明撤销 deny 后可以在不重新 PTW 的前提下恢复正常 writeback

### 3.2 `test_MemBlock_scalar_load_pipeline_probe.py`

它复用同一个 sequence，但额外加入 pipeline 约束：

1. fault 发生时不应退化成 `memoryViolation`
2. 不应新增 outer request / dcache A/D 事务
3. 不应拉起 `dcacheError`

因此这一层更像“fault + pipeline 纯度检查”，而不是重复写一遍 MMU smoke。

## 4. 复用 sequence 的设计意图

当前 fault 场景统一复用 `MmuFaultingScalarLoadSequence`，它把场景拆成两步：

1. prime load
2. main faulting load

其中 prime load 是可选的。它的作用不是业务目标本身，而是建立以下背景：

1. 让 PTW/TLB 路径先走通一次。
2. 让 testcase 可以明确断言“main 访问没有再触发 PTW”，从而把 fault 定位到 TLB hit 背景。
3. 在 `PMP deny` 这类场景里，把“translation 能成功”与“权限最终拒绝”分开。

实现上，这个 sequence 还做了三件重要的事：

1. 对同一 `va/root_pt_addr` 动态重装 leaf PTE，并在每次访问前 `pulse_sfence()`。
2. fault 场景下临时关闭 `strict_writeback_check`，允许异常写回不必预先登记普通 compare expectation。
3. 在 main load 前后抓取 transport 统计，便于 testcase 判断路径是否意外退化。

## 5. 当前断言口径

### 5.1 TLB hit 背景

对于带 prime 的用例，当前 testcase 会同时要求：

1. `prime_ptw_trace` 中出现 PTW A-fire
2. `main_ptw_trace` 为空

这组断言比“最终抛异常了”更重要，因为它证明了 fault 不是第一次访问冷态下的偶然结果，而是已经进入 DTLB 命中后的重复行为。

### 5.2 异常位集合

当前统一通过写回口上的 `exception_bits` 收口，而不是直接信任 testcase 本地推导。

具体口径是：

1. translation fault
   - 只要求命中 `AF/PF/GPF` 集合中的一个或多个
   - 不强行区分更细的叶子 fault 类型
2. PMP deny
   - 要求命中 `LOAD_ACCESS_FAULT_BIT`
3. overlap
   - 只限制在 translation error 集合内，不额外假设 RTL 会给出哪一个唯一优先级结果

这样做的原因是：当前 env 对 translation fault 的稳定可见语义仍是“translation error 集合”，还没有把更细粒度 fault leaf 完整公开成 testcase 契约。

### 5.3 非目标路径排除

fault 用例还会额外排除三类误命中：

1. outer request / dcache A/D 计数增长
2. `dcacheError` 拉高
3. `memoryViolation` 出现

这保证 testcase 不是用其它复杂路径“碰巧也失败了”来冒充 MMU/PMP fault。

## 6. 这批用例没有证明什么

当前这批 directed case 仍然没有覆盖：

1. 确定性的 TLB miss -> refill -> replay 成功
2. 多层级 TLB 命中/缺失组合
3. store side PMP deny
4. 多 entry PMP 的更复杂优先级交互，以及超出“单个 deny-region + allow-all fallback”的更长窗口语义

因此它们应被看作：

- 已经补上了 load-side fault 行为基线；
- 但还不能替代 `TLB/PTW/PMP` 的完整 coverage 计划。

## 7. 推荐使用方式

如果新 testcase 只是要复用“prime 一次后，再发一条 faulting scalar load”这类骨架，优先直接复用 `MmuFaultingScalarLoadSequence`：

```python
state = ResetEnvSequence(
    require_issue_lanes=(0,),
    require_lq_ready=True,
    require_sq_ready=True,
).run(env)

result = MmuFaultingScalarLoadSequence(
    root_pt_addr=root_pt,
    va=main_va,
    pa_base=pa_base,
    initial_state=state,
    main_req_id=1,
    size=4,
    mask=0xF,
    main_pte_mode=PTE_MODE_NORMAL,
    main_pmp_mode=PMP_MODE_DENY,
    prime_req_id=0,
    prime_pte_mode=PTE_MODE_NORMAL,
    prime_pmp_mode=PMP_MODE_ALLOW,
    prime_expected_data=0x1122334455667788,
    required_main_exception_bits=(LOAD_ACCESS_FAULT_BIT,),
).run(env)

assert any(event["event"] == "a_fire" for event in result.prime_ptw_trace)
assert not result.main_ptw_trace
```

如果 testcase 需要验证的是更深的 translation pipeline 行为，例如 miss/refill/replay 链路，就不应再沿用这套“fault + TLB hit”骨架，而应另外组织 `TLB miss` 专题 sequence。
