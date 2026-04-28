# MemoryModel 设计

## 1. 角色定位

`memory_model.py` 中的 `MemoryModel` 是 MemBlock Python 验证环境的核心收敛器。它不是单纯的内存数组，而是同时承担四类职责：

1. 模拟 outer buffer / uncache TileLink 请求响应。
2. 模拟 dcache client TileLink-C 请求响应。
3. 观测并校验 load writeback。
4. 观测 store shadow 和 drain 写出，并在测试结束时做一致性检查。

文件头部已经明确其设计原则：

- load 做在线 compare，但 compare 点在 commit 边界上。
- store 不做逐拍在线 compare，而是观察元数据与最终 drain 结果。

## 2. 核心状态

`MemoryModel` 内部状态可以分成五组。

### 2.1 backing store

- `self.memory`
  - 字节粒度的黄金内存。
- `preload_bytes()` / `preload_u64()` / `fill_random()`
  - 测试侧预置内存内容。
- `read()` / `read_masked()` / `read_cacheline()`
  - 提供按字节、掩码、cacheline 的读取能力。
- `apply_masked_write()`
  - 将 store 结果写回黄金内存。

### 2.2 事务队列

- `_pending_outer_d` / `_active_outer_d`
  - outer D 通道待发送和当前激活响应。
- `_pending_b` / `_active_b`
  - dcache B 通道待发送和当前激活响应。
- `_pending_d` / `_active_d`
  - dcache D 通道待发送和当前激活响应。
- `_inflight_grants`
  - 跟踪 dcache Grant 与后续 E 通道 GrantAck。

这些结构使模型能表达“请求先被接受，若干拍后返回响应”的异步行为。

### 2.3 Load compare 队列

- `_expected_loads`
  - 测试侧登记的期望 load，按 `RobIndex` 分桶。
- `_observed_load_writebacks`
  - 已观测到但尚未完成 compare 的实际 writeback。
- `_issued_loads`
  - 已 issue 的 load ROB 顺序队列。
- `_committed_load_budget`
  - 由 `lqDeq` 推进的可提交预算。
- `_completed_rob_indices`
  - 当前周期观测到完成的 ROB，用于回传给 `PendingPtrDriver`。

### 2.4 Store 跟踪

- `pending_stores`
  - 键为 `sq_idx`，值为 `PendingStore`。
- `PendingStore`
  - 保存 `rob_idx/addr/data/mask/committed/mmio/has_exception/...` 等信息。

Store 的地址、数据、mask、属性来自多个独立观测口，因此必须在模型侧拼装成完整条目。

### 2.5 统计与日志

- `drain_log`
  - 记录所有最终写出事件，来源可能是 sbuffer，也可能是 outer Put。
- 各类 `*_count`
  - 用于测试断言路径是否符合预期，例如 `outer_request_count`、`dcache_a_request_count`、`sbuffer_drain_count`。

## 3. 周期接口

`MemoryModel` 通过两个入口与 `MemBlockEnv` 的统一拍推进内核协同工作。

### 3.1 历史接口 `on_memory_edge(cycle)`

该函数最初被设计成“时钟上升沿统一处理 transport”的入口，曾同时承担两类职责。当前兼容入口已经删除，保留本节仅用于解释为什么现有实现改成 `capture_on_rise()` + `drive_pre_step()` 双 phase：

1. 保存当前周期号。
2. 驱动 outer、dcache 请求侧 ready。
3. 捕获 outer 请求。
4. 捕获 dcache A/C/E 请求。
5. 服务 outer D、dcache B、dcache D 响应队列。

这在功能较轻的场景下可以工作，但在 dcache `AcquireBlock -> GrantData*2 -> GrantAck` 这种多拍返回链路里，会把“采样本拍输出”和“为下一拍准备输入”两种语义混在一起。

沿着 `f06e7a10c87ea47c0cfd399ce6e46556a87ef092` 的修复可以确认：当前 xcomm/picker 语义下，`dut.StepRis(...)` 更接近 **capture-on-rise**，而不是 **pre-step drive hook**。因此，真正往 `outer_d/dcache_b/dcache_d` bundle 上打值的动作，不应该再完全依赖 `StepRis(self.memory.on_memory_edge)`。

### 3.2 `after_cycle()`

该函数在单拍 DUT 推进完成后调用，负责状态观测与检查：

1. 保持 writeback ready。
2. 如果处于 reset，则清空运行态。
3. 观测 store 相关事件。
4. 观测并检查 load writeback。

这部分更接近“收集器 + 校验器”。

### 3.3 当前缺失的 xcomm 时序语义

本轮问题定位暴露出一个此前没有在文档中写清楚的前提：

1. `StepRis` 回调是随 `dut.Step(1)` 的 rise callback 触发的。
2. 它适合采样 DUT 当前拍对外可见的输出。
3. 但它不等价于“在下一拍生效前驱动输入”的保留相位。

因此如果把 transport responder 写成：

- 在 `StepRis` 中同时捕获 `A/C/E`
- 再在同一个 `StepRis` 中直接驱动 `D/B/outer D`

就会把两种 phase 混用：

- 采样 phase：读取 DUT 在本拍边沿暴露出的请求
- 驱动 phase：准备下一拍应由 mock 提供的返回值

在低压场景下这种混用可能仍然“看起来能跑”，但在 back-to-back cacheable load、尤其是多个 `D` 响应 release 时间接近时，容易暴露成返回拍序错位、看似丢 beat、或数据晚一个 phase 才生效。

## 4. Outer / Uncache 路径建模

### 4.1 请求捕获

`_capture_outer_request()` 在 `outer_a.valid && ready` 时工作。

支持的 opcode 只有两类：

- `TL_A_GET`
  - 从 `self.memory` 按请求宽度读取数据。
  - 自动排队生成 `TL_D_ACCESS_ACK_DATA`。
- `TL_A_PUT_FULL` / `TL_A_PUT_PARTIAL`
  - 将写请求记录为 `drain_log` 事件。
  - 自动排队生成 `TL_D_ACCESS_ACK`。

若出现其他 opcode，模型会直接断言失败。

### 4.2 响应发射

`enqueue_outer_response()` 允许测试或模型将响应压入 `_pending_outer_d`。  
`_service_outer_d()` 在达到 `release_cycle` 后把该响应逐拍送到 `outer_d`。

模型还会检查：

- D 通道 `source` 必须与原 A 通道请求 `source` 一致。

这可以避免测试误注入不匹配的 TL 响应。

## 5. DCache 路径建模

### 5.1 A 通道

`_handle_dcache_a()` 当前只支持 `TL_A_ACQUIRE_BLOCK`。

处理过程：

1. 按 cacheline 对齐请求地址。
2. 从 `self.memory` 读取一整行数据。
3. 切成两个 32B beat。
4. 若 `echo_isKeyword=1`，按 RTL 期望翻转 beat 顺序。
5. 为本次 grant 分配唯一 `sink`。
6. 延迟若干拍后在 D 通道返回 `TL_D_GRANT_DATA` beats。

### 5.2 C 通道

`_handle_dcache_c()` 支持：

- `TL_C_PROBE_ACK`
- `TL_C_PROBE_ACK_DATA`
- `TL_C_RELEASE`
- `TL_C_RELEASE_DATA`

对 `Release/ReleaseData`，模型会自动排队一个 `TL_D_RELEASE_ACK`。

### 5.3 E 通道

`_handle_dcache_e()` 负责消费 GrantAck：

- 如果 sink 不在 `_inflight_grants` 中，直接报错。
- 正常情况下删除对应 inflight grant。

这保证了 dcache A -> D -> E 闭环的完整性。

## 6. Load compare 机制

### 6.1 为什么按 commit 边界 compare

模型并不在“writeback 到来时”立即比对数据，而是在“该 load 被允许提交时”才 compare。  
原因是 younger load 的可见值可能依赖于 older committed store，若过早 compare，会把合法行为误判成错误。

### 6.2 关键数据流

load compare 分三步完成：

1. 测试侧调用 `expect_load(...)`
  - 按 ROB 号登记一笔期望 load。
2. `check_writebacks()`
  - 观测到 writeback 后，只缓存到 `_observed_load_writebacks`。
3. `note_load_commits(commit_count)`
  - 当环境从 `mem_status.lqDeq` 得到提交预算后，才触发 `_try_complete_loads(...)`。

### 6.3 `_try_complete_loads()`

该函数要求三个条件同时成立：

1. 存在提交预算。
2. 存在期望 load。
3. 已观测到对应 writeback。

满足后执行：

1. 先 `_retire_stores_before_boundary(expected.rob_idx)`。
2. 从黄金内存读取期望数据。
3. 比较 `pdest`、`intWen`、`data`、`exception_bits`。
4. 成功后增加 `completed_loads`。

其中第一步是该模型最关键的语义：  
所有 ROB 不晚于当前 load 的 ready-for-retire store 都会先写入黄金内存，再拿该黄金视图比对当前 load。

### 6.4 断言策略

以下情况会被视为错误：

- 观测到未登记的 load writeback。
- `pdest` 不匹配。
- `intWen` 不匹配。
- 数据不匹配。
- 出现未预期的异常位。

这样能保证 DUT 侧返回的不仅是“有数据”，而是“在正确提交视图下的数据”。

## 7. Store 跟踪与退休

### 7.1 观测来源

store 的完整信息来自多个入口拼装：

- `note_store_allocated()`
  - 在 enqueue 成功后登记 `sq_idx -> rob_idx`，并负责 slot reuse 时的状态重置。
- `_observe_sq_commit_ptr()` / `_observe_sq_deq_ptr()` / store writeback
  - 读取 committed / completed 的强事实源。
- `_observe_store_addr()`
  - 读取地址、miss、NC 属性。
- `_observe_store_addr_re()`
  - 读取 MMIO、异常等修正属性。
- `_observe_store_mask()`
  - 读取字节掩码。
- `_observe_store_data()`
  - 读取数据。
- `_observe_sq_shadow()`
  - 当前仅保留为 legacy fallback，不再驱动 `allocated/addrvalid/datavalid/committed/completed` 等核心状态。
- `_observe_sbuffer_writes()`
  - 记录最终 sbuffer drain。

### 7.2 `PendingStore.ready_for_retire`

一笔 store 只有同时满足以下条件，才允许写入黄金内存：

- 已 committed。
- 地址有效。
- 数据有效。
- 地址和数据都非空。
- mask 非 0。
- 不是 MMIO。
- 没有异常。

这意味着：

- MMIO store 不通过黄金内存退休，而通过 outer 写路径体现在 `drain_log` 中。
- 有异常的 store 不会污染黄金视图。

### 7.3 Store 退休

退休分两种时机：

- `_retire_stores_before_boundary(load_rob_idx)`
  - 在某笔 load compare 之前，先退休所有不晚于它的 older store。
- `_retire_all_ready_stores()`
  - 测试结束时，把所有剩余 ready-for-retire store 全部写入黄金内存。

`_retire_store()` 会处理地址低位偏移、mask 左移和对齐写回问题。

## 8. Drain 校验

### 8.1 记录来源

`drain_log` 目前包含两类事件：

- channel=`sbuffer`
  - 来自 `_observe_sbuffer_writes()`。
  - 普通 store 记录 16B `addr/data/mask`。
  - `wline=1` 时记录为 `cbo.zero` line write 事件，`width_bytes=64`、`data=0`、`wline=true`。
- channel=`outer`
  - 来自 `_capture_outer_request()` 中的 Put 请求。

### 8.2 结束校验

`finalize_and_check_drain()` 的流程是：

1. 统计 ready-for-retire store 数量。
2. 调用 `_retire_all_ready_stores()` 更新黄金内存。
3. 收集已观测 MMIO store 的 touched-byte 窗口。
4. 若按理应有 store 可见，但 `drain_log` 为空，则报错。
5. 根据 `drain_log` 重建一份 `drained_memory`。
6. 对比 `drained_memory` 与 `self.memory` 在被触碰字节上的内容。

注意：

- MMIO outer drain 仍会保留在 `drain_log` 中，便于 testcase 和 coverage 证明“outer 写路径确实发生过”；
- 但这些 MMIO touched bytes 不再参与最终 non-MMIO golden compare；
- 因而 mixed `MMIO + cacheable` flush 场景现在可以同时满足：
  - 路径上看得到 outer / sbuffer 两类 drain
  - 最终一致性只约束 cacheable / non-MMIO 写出结果

如果任一字节不一致，会直接抛出首个不匹配地址。

这个检查适合用于：

- cacheable store flush 结束后的最终一致性确认。
- mixed MMIO + cacheable flush 场景下的最终一致性确认。
- non-MMIO outer/uncache 写路径是否按预期写出。

## 9. 与测试环境的协作关系

`MemoryModel` 不直接知道测试意图，它依赖环境和 backend facade 提供时序信息：

- `env.backend.note_load_issued()`
  - 把 issue 成功的 load ROB 告诉模型。
- `MemBlockEnv` 统一拍推进
  - 在每拍推进后把 `lqDeq` 转换成 `note_load_commits()`。
- `request_apis.py`
  - 负责按正确协议顺序调用 `env.backend` 发起 enqueue/issue。
- 测试用例
  - 负责在 load 发起后调用 `expect_load()`，并在合适时机调用 `drain_writebacks()` 或 `flush_store_buffers_and_wait()`。

因此，`MemoryModel`、`MemBlockEnv`、`env.backend`、`request_apis.py` 四者必须配套使用，单独抽离其中一个都会丢失完整语义。

## 10. 当前限制与后续扩展点

当前实现已有一些明确限制：

1. dcache A 通道只支持 `AcquireBlock` 的 load-only 场景。
2. sbuffer `wline` drain 目前只支持 `cbo.zero`，其余 CBO 类型尚未纳入。
3. compare 聚焦整数 load writeback，依赖 `intWriteback` 中的若干关键字段存在。
4. store 采用“观测 + 结尾核对”的思路，不做每拍在线严格 compare。
5. `MemoryModel` 仍然在一个 facade 内同时承担 transport capture、transport drive、scoreboard 组装和拍后观测收口，时序 phase 边界不够显式。

后续若要扩展，可优先考虑以下方向：

1. 增加更多 TileLink opcode 支持。
2. 扩展向量/特殊宽度 load-store 的黄金视图。
3. 增加更细粒度的 store replay、异常和 NC/MMIO 行为建模。
4. 将当前统计计数暴露为更稳定的调试 API，减少测试直接依赖内部字段名。

## 11. 更好的 `MemoryModel` 重构方案

当前 quick fix 的本质，是在 `MemBlockEnv._step_async()` 中把 transport 的驱动逻辑提前到真正 `dut.Step(1)` 之前。这能修复当前 dcache D 返回错位问题，但它仍属于“在 env 中手工补 phase”的收口方式，不是最好的长期结构。

更稳的重构方向应当是：**把 `MemoryModel` 从“单体 callback”拆成显式的多 phase model facade**。

### 11.1 目标分层

建议把现有 `MemoryModel` 拆成以下四层职责：

1. `TransportObserver`
   - 只负责采样 outer A、dcache A/C/E 这些 DUT 发出的请求。
   - 运行在 rise/capture phase。
   - 不直接 drive 任何返回 bundle。
2. `TransportDriver`
   - 只负责在 pre-step phase 把 `outer_d/dcache_b/dcache_d` 的 ready/valid/data 打到 bundle。
   - 消费由 observer/queue 准备好的待返回事务。
3. `ScoreboardFacade`
   - 继续承担 load compare、store retire、drain finalize。
   - 完全不感知 DUT bundle drive phase。
4. `MemoryModel`
   - 只做 facade / composition root，对外暴露 `preload_*`、`expect_*`、stats 和统一 reset。

### 11.2 推荐的时钟 phase

env 层应把当前拍推进语义收敛成固定 phase：

1. `before_step()`
   - `commit_agent.drive()`
   - `memory.transport_driver.drive_pre_step()`
2. `dut.Step(1)`
3. `rise_capture()`
   - `memory.transport_observer.capture_on_rise()`
4. `after_cycle()`
   - `store_monitor.after_cycle()`
   - `writeback_monitor.after_cycle()`
   - `mem_status_monitor.after_cycle()`
   - `commit_agent.advance()`
5. `after_step_callback`

关键原则是：

- 所有“写 DUT 输入”的 mock 行为都必须进入 `before_step`
- 所有“读 DUT 输出”的 monitor/mock 采样都进入 `rise_capture` 或 `after_cycle`
- 任何组件都不再同时承担 capture + drive 两种 phase

### 11.3 具体接口建议

为了避免再出现“把 `StepRis` 当成 pre-step drive hook”的误用，建议把接口名字也改得更显式：

- `memory.capture_on_rise(cycle)`
- `memory.drive_pre_step(cycle)`
- `memory.after_cycle()`

其中：

- `capture_on_rise()` 可继续通过 `dut.StepRis(...)` 挂接
- `drive_pre_step()` 由 `MemBlockEnv._step_async()` 或后续统一 `ClockPhaseCoordinator` 在 `dut.Step(1)` 前显式调用

### 11.4 对现有代码的迁移顺序

推荐按以下顺序渐进迁移，而不是一次性重写：

1. 保留现有 `Scoreboard` / `StoreMonitor` / `WritebackMonitor`
2. 先把 `TransportResponder` 拆成 `capture_requests()` 与 `drive_responses()`
3. 再把 `MemoryModel` 上的历史 `on_memory_edge()` 兼容入口删除，只保留显式 `capture_on_rise()`
4. 最后在 env 内引入统一 `before_step` phase，取代任何隐式 edge callback 承担 drive 的做法

这样改的收益是：

- transport 时序更接近真实总线 driver/monitor 分层
- `MemoryModel` 更容易单测，不再依赖隐式 `StepRis` 调度
- 后续扩展 vector/uncache/probe/release 路径时，不会再把时钟 phase 语义揉回单个 callback
