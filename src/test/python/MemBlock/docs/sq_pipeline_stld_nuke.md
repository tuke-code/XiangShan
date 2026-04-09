# `sq_pipeline_stld_nuke` 用例设计说明

## 1. 文档目的

本文档解释 `test_api_MemBlock_scalar_pipeline_stld_nuke_smoke` 的设计目标、构造方式和当前验证边界。

对应实现位于：

- sequence: `src/test/python/MemBlock/sequences/violation_sequences.py`
- testcase: `src/test/python/MemBlock/tests/test_MemBlock_replay.py`

## 2. 目标行为

本用例要验证的是“流水线上直接产生的 st-ld nuke”，而不是：

1. `RAW` queue 的 backpressure / nack
2. `RAR` queue 的 response
3. `FF` 或 `BC` 这种其他 replay cause

换句话说，它想证明的是：

- older store 的 `STA` 晚到
- younger load 已经进入 load pipeline
- 二者在 pipeline 内直接发生碰撞
- 目标 load 最终需要走一次 `NK` 路径

## 3. 构造方法

当前 sequence 采用以下时序：

1. 先对目标地址做 cache warmup，尽量建立 cache-hot 背景
2. 分配 older store，但先只发 `STD`
3. 再发 younger load
4. 在 load issue 之后立刻补 older store 的 `STA`

这里 deliberately 把 `STA` 放在 load issue 之后、但又不再等到 `s1` 采样之后，是因为：

- `STA` 发得太晚时，目标 load 会稳定退化成只看到 `DR/DM` 的路径
- `STA` 在 load issue 后立即补上时，`load_debug_trace` 可以稳定看到 `NK`
- 同时 load 仍能恢复并最终写回 older store data

## 4. 当前收口状态

当前用例已经不再是探索型 smoke，而是稳定可回归的 directed case。

本轮分析得到的结论是：

1. 旧版本 sequence 先等 younger load 进入 `s1`，再发 `STA`
2. 这种时序会让目标 load 更容易只留下 `DR/DM`，看不到 `NK`
3. 把 `STA` 提前到 load issue 后立即发起后，`NK` 可以稳定出现
4. 因此原先的 `xfail` 属于 scene construction gap，而不是 DUT 已知 bug

## 5. 当前断言口径

### 5.1 成功时必须满足

- 目标 load 最终写回的数据等于 older store data
- older store 最终进入 committed
- `load_debug_trace` 中能看到 `NK`
- 同一 trace 中不应混入 `BC`
- 同一 trace 中不应混入 `FF`

## 6. 这个用例的价值

这个场景的价值主要体现在两个方面：

1. 它把“pipeline 直接 nuke”从 `RAW/RAR` 这类 queue 路径里拆出来单独验证
2. 它为后续更复杂的组合场景提供 `NK` 的单独基线

如果连这个单独场景都不成立，那么后续 `BC + FF + NK` 组合场景就很难定位问题到底出在组合，还是出在 `NK` 本身。

## 7. 当前结论

截至当前版本，这个用例已经能稳定命中 `NK` 并完成最终写回。需要注意的是，当前 `NK` trace 仍可能同时带着 `DR/DM`，因此它验证的是“pipeline-side nuke 已经出现且不混入 `BC/FF`”，而不是“只剩单一 `NK`、无任何 dcache 侧 replay 位”。
