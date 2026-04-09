# `sq_bank_conflict_replay` 用例设计说明

## 1. 文档目的

本文档解释 `test_api_MemBlock_scalar_bank_conflict_replay_smoke` 想证明什么，以及为什么当前实现选择这套构造方式。

对应实现位于：

- sequence: `src/test/python/MemBlock/sequences/memblock_sequences.py`
- testcase: `src/test/python/MemBlock/tests/test_MemBlock_replay.py`

## 2. 目标行为

本用例的目标很聚焦：稳定构造一次纯 `BC` replay。

这里的“纯”指的是：

1. replay 目标是 younger / lower-priority 那条 load
2. 白盒 `debugLsInfo` 中能看到 `BC`
3. 同一目标 load 不应同时混入 `FF`
4. 同一目标 load 不应同时混入 `NK`

因此这个用例主要是在给后续组合场景提供一个干净、可复用的 `BC` 基线。

## 3. 构造方法

当前实现采用“两条 cache hit load 同拍 issue”的方式：

1. 先预热两个地址对应的 cache line
2. 这两个地址满足：
   - 落在同一个 dcache bank/div 冲突域
   - 但不是完全同一个地址
3. 之后把两条 load 分配到不同 lq 项
4. 再让它们在同一拍分别从 lane0 和 lane1 发射

这样做的原因是：

- lane0 在 load side 仲裁上优先级更高
- lane1 更容易成为被 replay 的 victim
- 地址不同，可以避免误把“同地址前递相关性”混进来

## 4. 为什么不是直接复用同地址 load

如果两条 load 使用完全相同的地址，场景会更接近“同地址竞争”，但会带来两个问题：

1. 更容易和其他相关性判定混在一起
2. 不利于证明这个 smoke 真正在验证 bank conflict，而不是别的 replay cause

所以当前单独场景特意把 lead load 和 victim load 放在不同地址上，只保留 bank 级冲突这一项。

## 5. 当前断言口径

当前 testcase 锁定三类事实：

### 5.1 目标 load 身份

- replay 观测到的 `rob_idx_value` 必须对应 victim load

### 5.2 白盒 cause

- `load_debug_event["replay_causes"]` 中必须含有 `BC`
- 不能含有 `FF`
- 不能含有 `NK`

### 5.3 收尾

- 两条 load 最终都要完成
- testcase 最终需要 `assert_no_outstanding()`

## 6. 这个用例的价值

它的价值不在于“bank conflict 这个名字被点亮一次”，而在于给后续更复杂场景提供一个稳定支点：

1. 可以单独验证 `BC` 是否仍然可构造
2. 可以在组合场景失败时判断问题出在 `BC` 基线，还是出在组合叠加时序
3. 可以作为后续 `BC + FF`、`BC + NK` 等场景的最小前提

## 7. 当前结论

截至当前实现，这个单独场景已经可以稳定回归，适合作为后续组合场景的基础构件。
