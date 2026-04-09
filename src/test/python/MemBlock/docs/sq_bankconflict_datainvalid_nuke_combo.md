# `sq_bankconflict_datainvalid_nuke_combo` 用例设计说明

## 1. 文档目的

本文档说明 `test_api_MemBlock_scalar_bankconflict_sq_datainvalid_nuke_smoke` 当前验证的组合行为、最终收口方式，以及为什么这个场景不再保留 `xfail`。

对应实现位于：

- sequence: `src/test/python/MemBlock/sequences/violation_sequences.py`
- testcase: `src/test/python/MemBlock/tests/test_MemBlock_replay.py`

## 2. 目标组合

这个 directed case 要覆盖的不是单一 replay cause，而是以下叠加条件：

1. dcache hit
2. `BC`
3. SQ forward 返回 `dataInvalid=1`
4. SQ forward 返回 `matchInvalid=0`
5. 同一 victim load 在 pipeline 侧命中 `NK`

它要回答的核心问题是：

- `BC + FF + dataInvalid` 与 pipeline `NK` 能否在同一条 victim load 上稳定露出来
- 这条路径最终是否能恢复并读到 older store data
- 构造完成后的恢复阶段，是否仍可能伴随 queue-side `RAW/RAR` 查询流量

## 3. 收口后的时序要点

当前稳定版本的 sequence 采用如下时序：

1. 预热 victim 地址和 lead 地址，保证后续两条 load 都走 dcache hit
2. 先 enqueue older store，但暂不补 `STD`
3. lead load 与 victim load 同拍发射，稳定制造 `BC`
4. 立即补发 older store 的 `STA`
5. 先等待：
   - SQ forward 命中 `dataInvalid=1 && matchInvalid=0`
   - victim load 的早期 `NK`
   - victim load 的后续 `BC + FF`
6. 只有在上述窗口稳定出现后，才补 `STD` 并提交 store

关键点不在“把 `STD` 尽早发掉”，而在“先等 transient `NK` 露出来，再让 store data 完整化”。如果过早补 `STD`，场景会退化成只剩 `FF` 的重发表现，victim load 可能长期不收敛。

## 4. 当前稳定观测到的事实

### 4.1 SQ forward 事实

- `dataInvalid_valid == 1`
- `matchInvalid == 0`
- `forwardInvalid == 0`
- `dataInvalid_value` 指向目标 older store

### 4.2 victim load debug 事实

- 早期 trace 可稳定看到 `NK`
- 后续 trace 可稳定看到 `BC + FF`
- testcase 的主断言点 `load_debug_event` 仍要求不退化成 `RAW/RAR`

### 4.3 端到端恢复事实

- victim load 最终写回 older store data
- lead load 不会被 older store 污染
- older store 最终进入 committed

## 5. 为什么不再对 “没有 RAW/RAR query” 做硬断言

白盒实验表明，这个组合场景在成功恢复时，victim load 的早期确实走到了 pipeline `NK`；但在更晚的恢复阶段，仍可能看到与该 ROB 对应的 `raw_nuke_query` / `rar_nuke_query` 流量。

这说明之前那条更强的假设：

- “只要是 pipeline NK，这条指令整个生命周期都不该再碰 RAW/RAR queue”

并不符合当前 DUT 的真实实现。更准确的口径应当是：

- 目标 `NK` 可以稳定在 pipeline debug trace 上被构造出来
- testcase 不应在主断言点退化成 `RAW/RAR` cause
- 但恢复后段允许出现 queue-side 查询流量

因此，旧的 “全程不出现 target RAW/RAR query” 断言属于过度约束，已经移除。

## 6. 从通用 OoO LSU 设计角度看，这个现象是否合理

从通用乱序访存流水线设计角度看，`pipeline NK` 之后还能看到 `RAR/RAW query`，并不天然等价于设计错误。更关键的问题是：

- 这些 query 是不是只是可撤销的保守探测
- 它们是否会演化成第二条不可撤销的恢复路径
- 最终对后端可见的 redirect / rollback 是否仍然只有一个仲裁出口

在工程实现上，很多 LSU 会采用“前段宽松发 query，后段精确 revoke”的方式来换时序裕量。对于这类实现：

1. 同一条 load 可以先在 pipeline 内部命中 `NK`
2. 后续 replay / re-execute 尝试又把它送进 `RAR/RAW` 的保守检查
3. 但只要最终真正生效的 recovery 仍由统一 redirect 仲裁决定，这种“多观察点、单出口”的结构就是可以接受的

因此，通用判断不应是“一旦出现 `NK`，后续就绝不能再看到 queue query”，而应是：

- query 可以重复出现
- 但不能无约束地累积成第二套独立 recovery 副作用

## 7. 当前 DUT 为什么会在 `NK` 之后出现 `RAR/RAW query`

结合 Scala 代码，当前 DUT 出现这个现象的原因比较明确，而且更像是“设计上保守、实现上不够干净”，而不是直接的功能错误。

### 7.1 store 在 s1 就会广播 st-ld nuke query

`StoreUnit` 在 store s1 直接发出 `stld_nuke_query`：

- `src/main/scala/xiangshan/mem/pipeline/StoreUnit.scala:326`

这些 query 会广播到所有 `NewLoadUnit`：

- `src/main/scala/xiangshan/mem/MemBlock.scala:922`

因此，victim load 只要在 load s2 看到同地址 older store，就可能先命中 pipeline `NK`：

- `src/main/scala/xiangshan/mem/pipeline/NewLoadUnit.scala:985`
- `src/main/scala/xiangshan/mem/pipeline/NewLoadUnit.scala:1077`

### 7.2 S2 的 RAR/RAW query 不是分类单播，而是同时广播

当前 load s2 里，`RAR` 和 `RAW` 查询并不是先分类再只发一路，而是用同一个 `nukeQueryReqValid` 同时发到两边：

- `src/main/scala/xiangshan/mem/pipeline/NewLoadUnit.scala:1147`
- `src/main/scala/xiangshan/mem/pipeline/NewLoadUnit.scala:1149`

这意味着一旦 `nukeQueryReqValid` 为真，波形上同时看到 `raw_nuke_query` 和 `rar_nuke_query` 是设计允许的，不需要它们在源头互斥。

### 7.3 为什么第一次命中 NK 时通常看不到 query

当前 query 的发起条件是：

- `nukeQueryReqValid = troubleMaker && !(prevStageNuke || cause(C_BC))`
- `src/main/scala/xiangshan/mem/pipeline/NewLoadUnit.scala:1041`

也就是说，当前拍如果已经带着：

- 前一拍继承下来的 `NK`，或
- 当前拍判成 `BC`

那么这次 S2 就不会再往 `RAR/RAW` 发 query。

这正好解释了组合场景的早期窗口：

1. victim load 先在 pipeline 上看到 `NK`
2. 同时又叠加 `BC`
3. 因此早期那次执行通常不会对 queue 再发 query

### 7.4 为什么后续 replay attempt 又会重新发 query

关键点在于：进入 replay queue 时，cause 会被压成单一最高优先级 cause，而不是保留完整 cause 向量。

相关代码：

- `lqWriteCauseOH = PriorityEncoderOH(lqWriteCause)`
- `lqWrite.rep_info.cause := lqWriteCauseOH`
- `src/main/scala/xiangshan/mem/pipeline/NewLoadUnit.scala:1442`
- `src/main/scala/xiangshan/mem/pipeline/NewLoadUnit.scala:1516`

而 replay cause 的优先级里：

- `C_FF = 4`
- `C_BC = 8`
- `C_NK = 11`
- `src/main/scala/xiangshan/mem/lsqueue/LoadQueueReplay.scala:52`

在这个编码下，像本用例这种 `BC + FF + NK` 叠加场景，后续 replay 更容易保留下来的往往是更高优先级的 `FF`，而不是 `NK/BC` 的完整上下文。

于是到了 replay 后的下一次执行：

1. `prevStageNuke` 可能已经不再为真
2. 当前拍也不一定再次命中 `BC`
3. 但这条 load 仍然是 `troubleMaker`

于是 `nukeQueryReqValid` 又重新成立，`RAR/RAW` query 就会重新发出。

换句话说，当前看到的 `RAR/RAW query`，更像是：

- 同一个动态 load 的后续 replay attempt
- 重新走到了 queue-side 的保守检查

而不是“第一次已经 pipeline NK 的那次执行，又在同一拍分叉出第二条 recovery 目的地”。

### 7.5 当前 DUT 依赖 revoke 来清理前段保守 query

代码里明确写了：S2 发出的 violation query 可以不精确，但 S3 必须给精确 revoke。

对应逻辑在：

- `src/main/scala/xiangshan/mem/pipeline/NewLoadUnit.scala:1038`
- `src/main/scala/xiangshan/mem/pipeline/NewLoadUnit.scala:1380`
- `src/main/scala/xiangshan/mem/pipeline/NewLoadUnit.scala:1604`

RAR/RAW queue 也都支持把最近一两拍刚分配的 entry 撤掉：

- `src/main/scala/xiangshan/mem/lsqueue/LoadQueueRAR.scala:221`
- `src/main/scala/xiangshan/mem/lsqueue/LoadQueueRAW.scala:215`

这说明当前实现思路并不是“源头严禁重复 query”，而是“允许前段宽松发 query，靠后段 revoke 清掉不该留下的状态”。

## 8. 是否会产生额外副作用

从当前 Scala 结构看，这种行为更像“有额外微结构成本，但未必构成功能错误”。

### 8.1 为什么它未必会变成功能错误

最终所有 load unit / LSQ 产生的 rollback 都会统一进入 `allRedirect`，再选最老的一条送回后端：

- `src/main/scala/xiangshan/mem/MemBlock.scala:1073`

因此从架构可见出口看，当前 DUT 仍然试图保持：

- 多个 checker 可以并存
- 但最终真正生效的 redirect 只有一个仲裁出口

这也是为什么当前组合场景虽然能看到后段 `RAR/RAW query`，最终仍然可以稳定恢复并正确写回。

### 8.2 它确实会引入的副作用风险

虽然不一定错，但这种实现会带来额外的微结构副作用风险：

1. 增加 `RAR/RAW` queue 的瞬时占用
2. 增加 CAM / 比较逻辑活动量
3. 如果 query 发生在队列紧张时，还可能反过来把自己变成 `C_RAR` / `C_RAW`
   - `src/main/scala/xiangshan/mem/pipeline/NewLoadUnit.scala:1056`
   - `src/main/scala/xiangshan/mem/pipeline/NewLoadUnit.scala:1075`
4. 让“为什么这条 load 最后走到了哪条恢复路径”的推理变得更依赖后段 revoke 和统一仲裁，降低实现洁净度

因此，对当前 DUT 更准确的评价是：

- 这不是已经证实的功能 bug
- 但它确实比“早期就把路径严格分干净”的实现更脆弱，也更难分析

## 9. 对后续 testcase 断言口径的建议

基于当前实现特征，后续如果继续增强这个组合场景，推荐把断言分成“必须成立”和“不要过度约束”两类。

### 9.1 建议保留的硬断言

这些断言直接对应当前 testcase 真正想证明的行为边界：

1. victim load 的早期 debug trace 中必须稳定出现 `NK`
2. 主 replay 断言点必须稳定命中 `BC + FF`
3. SQ forward 必须稳定命中 `dataInvalid=1 && matchInvalid=0`
4. victim load 最终必须写回 older store data
5. lead load 不得被 older store 污染
6. older store 最终必须进入 committed
7. 最终不应出现额外错误 redirect 抢走主恢复路径

这类断言验证的是：

- 目标场景确实被构造出来
- 最终 architecturally visible 结果是正确的
- 没有出现明显的错误恢复

### 9.2 不建议继续使用的强排他断言

以下口径在当前 DUT 上都容易变成“为了贴合某种理想实现而过度约束 testcase”：

1. “只要出现 pipeline `NK`，整个生命周期都不允许再看到 `raw_nuke_query` / `rar_nuke_query`”
2. “一旦第一次执行出现 `NK`，后续 replay attempt 不允许再走 queue-side 保守检查”
3. “组合场景最终只能保留单一 replay cause，不能先后看到不同 checker 的观察结果”

这些断言的问题在于：

- 它们把“实现风格偏好”误写成了“功能正确性要求”
- 与当前 DUT 的“宽松 query、后段 revoke、统一 redirect 仲裁”结构不一致

### 9.3 如果后续要继续加白盒检查，更合适的方向

如果后面还要继续加强白盒验证，建议优先检查下面这些点，而不是直接禁止 query 出现：

1. `RAR/RAW query` 出现后，是否真的留下了持久 queue entry
2. 这些 query 是否在后续被 revoke 正确清理
3. 是否产生了额外的 `rollback`，并且该 `rollback` 是否真的抢占了主恢复路径
4. replay 后再次执行时，是否因为队列压力额外引入了 `C_RAR` / `C_RAW`
5. 最终统一送到后端的 `memoryViolation` 是否仍然只有一个合理来源

也就是说，更推荐的检查口径是：

- 允许 query 存在
- 重点防止 query 演化成额外且错误的恢复副作用

## 10. 当前结论

这个场景最终被证明是 testcase timing gap，而不是当前 DUT 缺少 `BC + dataInvalid + NK` 能力：

1. 旧版本 `xfail` 的根因是 directed timing 没有把 `NK` 暴露窗口和 `STD` 补齐窗口对齐
2. 修正时序后，`NK` 可稳定复现，且端到端恢复完成
3. 因此该用例已从 `xfail` 收口为普通回归测试

需要保留的设计认知是：

- pipeline `NK` 与后续 queue-side query 流量并非互斥
- 如果后续要把这个问题进一步上升到架构语义层面讨论，应当基于 Scala 实现和更精细的 white-box 观测，而不是沿用旧 testcase 的排他假设
