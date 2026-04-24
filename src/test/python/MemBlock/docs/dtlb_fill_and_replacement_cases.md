# `dtlb_fill_and_replacement` 用例说明

## 1. 文档目的

本文档说明 `test_api_MemBlock_env_mmu_sv39_dtlb_fill_and_replacement` 这一条 Sv39 4KB DTLB 定向用例在验证什么、为什么按当前方式组织，以及它与普通 MMU smoke / fault case 的边界是什么。

对应实现位于：

- testcase:
  - `src/test/python/MemBlock/tests/test_MemBlock_env_mmu_smoke.py`
- sequence:
  - `src/test/python/MemBlock/sequences/mmu_sequences.py`
  - `MmuDtlbReplacementSequence`
  - `MmuDtlbPageSpec`
  - `MmuDtlbAccessResult`
  - `MmuDtlbReplacementSequenceResult`

本文档不重复介绍 `env.mmu` 的通用控制面能力；这些内容见：

- `src/test/python/MemBlock/docs/mmu_env_design_and_usage.md`

## 2. 为什么单独写成 DTLB replacement directed

普通 MMU smoke 主要回答“Sv39/PTW/PMP 这条基础翻译闭环能不能跑通”。这条用例要回答的是更窄、更偏 TLB 行为的问题：

1. 小工作集第一次访问时，load-side DTLB 是否表现为 miss/refill。
2. 同一工作集立即重访时，是否已经形成 re-hit，而不是重新走 miss。
3. 当继续向更大地址空间扫入更多不同页时，旧页是否最终会被挤出。
4. 被挤出的旧页再次访问时，是否重新出现 miss/refill 迹象。

因此它不是“更大的 MMU smoke”，而是一条专门围绕 DTLB capacity / replacement 的 directed case。

## 3. 场景总览

当前用例固定在 Sv39 4KB 页表背景下工作，核心参数位于 `test_MemBlock_env_mmu_smoke.py`：

- `MMU_DTLB_REPLACEMENT_ROOT_PT = 0x88100000`
- `MMU_DTLB_CAPACITY = 4`
- `MMU_DTLB_REPLACEMENT_SWEEP_PAGE_COUNT = 64`

### 3.1 地址布局

用例不是在一小段连续页内做局部命中，而是故意把虚拟页拉开：

```python
page_va = 0x0040001000 + (index << 30)
pa_base = 0x81001000 + (index << 12)
data = 0x1111222233334444 + index
```

这有两个目的：

1. 场景语义明确是“跨大范围地址空间”的 translation load，而不是“相邻页上的局部热访问”。
2. 每页都带独立数据，便于 testcase 在 overflow / reprobe 阶段仍精确确认是哪一页被访问、哪一页重新 miss。

### 3.2 为什么 sweep 64 页

当前 sequence 构造 64 个不同虚拟页，而不是只访问 `capacity + 1` 个页。原因是：

1. testcase 希望证明的是“填满并发生替换”，不是“猜一个固定 victim 恰好被替换”。
2. 当前 Python test env 能稳定观测到的，是 miss/refill 行为痕迹；没有稳定导出的 victim-way / eviction-way 契约。
3. 真实 DUT 的命中层级可能不只是一层 DTLB，某次 miss 也未必一定继续走到 PTW；如果只打 `4 -> 5` 这样的最小窗口，很容易把结论绑死在某一版具体实现细节上。

因此当前选择是：

- 先用 4 个页建立一个小 working set；
- 验证这 4 个页的 re-hit；
- 再持续扫入更多不同页，把旧工作集尽量往外推；
- 最后回探旧工作集，捕获第一个重新 miss 的旧页作为 replacement 证据。

## 4. sequence 的四个阶段

`MmuDtlbReplacementSequence.run()` 当前按四阶段组织：

### 4.1 phase A: install

先通过 `MmuSv39AddressSpaceInstallSequence` 安装整套 Sv39 4KB 地址空间，并对每个页的 `load_va` 做 translated preload。

这一步保证：

1. 后续所有 load 都是合法 translated load。
2. 每次 writeback 的数据都能唯一对应到某一个 page spec。
3. testcase 讨论的是“TLB 行为”，而不是 page table 没装好导致的伪 miss。

### 4.2 phase B: prime

对前 `dtlb_capacity` 个页各访问一次：

- 期望：这些访问都表现为 `miss_observed == True`
- 语义：建立 working set，同时证明首次访问不是 hit-path

这里的 `miss_observed` 不是只看某一个白盒信号，而是由 `MmuDtlbAccessResult` 统一收口：

```python
tlb_first_miss_seen
or l2_tlb_req_seen
or ptw_a_fire_count
or ptw_d_fire_count
```

也就是说，只要出现任何一种稳定的 miss/refill 证据，就算这次访问成功暴露了 miss-path。

### 4.3 phase C: rehit

立即重访前 4 个页：

- 期望：`miss_observed == False`
- 语义：证明刚刚建立的 working set 已经进入 hot TLB 状态

这一步很关键，因为后面的 replacement 证明依赖一个前提：旧页曾经确实命中过，而不是压根没有成功留在 TLB 中。

### 4.4 phase D: wide sweep + reprobe

先把剩余的 60 个页全部访问一遍，这些访问都要求 `miss_observed == True`。  
之后按原 working set 顺序回探前 4 个页，直到找到第一个重新出现 `miss_observed == True` 的旧页：

- 如果直到 4 个旧页都回探完，仍然没有 miss，则 sequence 直接报错；
- 一旦找到旧页重新 miss，就把它记为 `result.reprobe_access`，作为 replacement 已发生的证据。

这个口径刻意没有写成“第 0 个页一定是 victim”，因为当前 testcase 并不想把 replacement policy 的具体 victim 次序当成公开契约。

## 5. 当前断言口径

`test_api_MemBlock_env_mmu_sv39_dtlb_fill_and_replacement` 主要做四组断言。

### 5.1 prime 阶段必须 miss

对前 4 个页逐个确认：

1. `va` / `expected_pa` 与 page spec 对齐
2. writeback data 与 preload 对齐
3. `miss_observed == True`

这证明 working set 建立时确实走了 miss/refill。

### 5.2 rehit 阶段必须不再 miss

对同一 working set 逐个确认：

1. 访问到的还是同一组页
2. 数据正确
3. `miss_observed == False`

这证明当前工作集已经被缓存，而不是每次都重新走 miss。

### 5.3 wide sweep 阶段必须持续产生 miss

对剩余 60 个新页逐个确认：

1. 都命中不同的 page spec
2. 数据正确
3. `miss_observed == True`

这组断言的目标不是证明每次都 page walk 到 PTW，而是证明扫入这些新页时确实在持续消耗翻译冷态路径。

### 5.4 reprobe 阶段必须最终命中 replacement

回探旧 working set 时，当前 testcase 要求：

1. `1 <= len(result.reprobe_accesses) <= dtlb_capacity`
2. 除最后一个外，之前回探到的旧页都仍是 hit-path
3. 最后一个 `result.reprobe_access` 必须重新出现 `miss_observed == True`

这组断言表明：

1. 旧 working set 并没有在一开始就全部丢失；
2. 但在 wide sweep 之后，至少有一个旧页已经被替换掉；
3. 因而“DTLB 被填满并产生替换”这一结论成立。

## 6. 为什么不用“固定 victim”断言

当前用例没有写成“访问第 5 个页后，第 0 个页一定立即 miss”，主要因为这会把 testcase 绑死到过窄的实现假设上。

当前已知的约束是：

1. `ldtlbParameters` 在当前配置里可读到 `NWays=4`，但 testcase 想证明的是行为，而不是对某个局部实现细节做白盒锁定。
2. 当前 Python env 没有稳定公开的 victim-way / eviction-way 接口。
3. miss 访问也不一定每次都继续走到 PTW，因为更高层翻译缓存可能吸收一部分行为。

因此当前文档化的验证口径是：

- 证明小 working set 可重访命中；
- 证明大 sweep 持续引入冷页；
- 证明 sweep 之后，旧 working set 中至少一个页重新回到 miss/refill 路径。

这已经足以说明 replacement 发生，同时保持 testcase 对后续 RTL 调整有一定韧性。

## 7. sequence 返回结果如何使用

`MmuDtlbReplacementSequenceResult` 当前主要包含：

- `prime_accesses`
- `rehit_accesses`
- `overflow_accesses`
- `reprobe_accesses`
- `reprobe_access`
- `completed_load_count`

其中每个 `MmuDtlbAccessResult` 都带：

- `txn`
- `va`
- `expected_pa`
- `expected_data`
- `writeback`
- `ptw_trace`
- `probe_events`
- `ptw_a_fire_count`
- `ptw_d_fire_count`
- `tlb_first_miss_seen`
- `l2_tlb_req_seen`
- `miss_observed`

因此后续如果要扩 testcase，可以直接在这些结构化结果上做行为断言，而不必在测试文件里重新写 callback 或手动采样 `io_debug_ls_debugLsInfo_*`。

## 8. 这条用例没有证明什么

当前它仍然没有覆盖：

1. replacement policy 的精确 victim 次序
2. store-side TLB 的容量/替换
3. `sfence`、root 切换和 replacement 的交叉语义
4. 多层级 TLB 命中/缺失的精确分层归因
5. page fault / PMP deny 与 replacement 的交叉情况

因此应把它看作：

- 已经补上了 load-side DTLB capacity / replacement 的基础 directed case；
- 但还不是完整的 TLB/PTW 覆盖计划终点。

## 9. 推荐扩展方向

如果后续要继续补强，建议优先沿下面几条线扩展：

1. `sfence` 后重新回探 old working set，验证 replacement 与显式 flush 的边界。
2. root-A / root-B 切换后复用相同 VA，验证 replacement 与 root switch 的组合行为。
3. 把当前 load-only 版本扩展到 store / hybrid TLB，但不要在同一 testcase 里混入过多机制。
4. 若 env 后续稳定公开 victim-way 观测，再考虑增加更白盒的 replacement policy directed case。

## 10. 最小复用示例

如果新 testcase 只想复用当前“建立 working set -> 宽扫 -> 回探 replacement”骨架，推荐直接消费 sequence：

```python
state = ResetEnvSequence(
    require_issue_lanes=(0,),
    require_lq_ready=True,
    require_sq_ready=True,
).run(env)

result = MmuDtlbReplacementSequence(
    root_pt_addr=root_pt,
    page_table_page_addrs=page_table_page_addrs,
    page_specs=page_specs,
    initial_state=state,
    dtlb_capacity=4,
).run(env)

assert all(access.miss_observed for access in result.prime_accesses)
assert all(not access.miss_observed for access in result.rehit_accesses)
assert all(access.miss_observed for access in result.overflow_accesses)
assert result.reprobe_access.miss_observed
```

如果新场景真正关心的是：

- page fault / access fault
- store-side translation
- root switch / sfence / replay 的交叉

就不应继续把所有逻辑都塞进这条用例，而应另外抽专题 sequence。
