# MemBlock 验证环境中的 ROB 建模分析、需求与设计

## 1. 文档目的

本文档专门分析 `src/test/python/MemBlock/` 当前真实 DUT 验证环境中的 ROB 建模问题。这里所说的“ROB 建模”，不是要在 Python 侧重写一份完整后端，而是指测试环境如何向 MemBlock 提供足够真实的 `ROB -> MemBlock` 提交边界信息，使得 load compare、store committed、flush/drain、replay/violation、uncache/MMIO 等场景都能建立正确的程序序与提交语义。

当前环境已经从“只靠 load 完成推进 `pendingPtr`”演进到了“按 mem op 程序序推进 `pendingPtr`，同时用 `scommit` 推进 store 提交”的阶段。这个版本已经足以支持当前一批真实 DUT 标量 ld/st 冒烟与定向场景，例如单条 cacheable store、store->load same addr、双 store 覆盖、small mixed ld/st 等。但这并不意味着当前建模已经等价于真实后端 ROB，也不意味着它已经覆盖了后端与 MemBlock 的全部交互语义。

从真实 RTL 来看，后端提供给 MemBlock 的 ROB 相关输入远不止一个 `pendingPtr`：

- `lcommit`
- `scommit`
- `commit`
- `pendingPtr`
- `pendingPtrNext`

而当前环境真正主动驱动的，主要只有：

- `pendingPtr`
- `scommit`

load compare 则仍然主要靠 MemBlock 对外导出的 `lqDeq` 做提交预算收口，而不是由测试侧主动驱动一套完整的 `lcommit` 输入模型。因此，当前建模更准确的描述应该是：它是一个“面向 MemBlock 基础验证的 ROB/commit 代理模型”，而不是“真实后端 ROB 行为模型”。

本文档的目标有四个：

1. 记录当前环境中 ROB 建模的真实现状，而不是凭印象描述。
2. 对照真实后端接口，分析当前模型的一致点、偏差点，以及这些偏差是否会影响 MemBlock 验证。
3. 总结一份面向未来演进的 ROB 建模需求清单，说明每一项需求背后的场景、用例与验证价值。
4. 给出一套可落地的概要设计、详细设计和验证计划，使后续实现者可以基于此文档直接推进下一代 ROB 建模。

本文档聚焦的是 `ROB <-> MemBlock` 的验证代理建模问题，不讨论完整后端流水线功能验证，也不尝试在 Python 侧重建整个 XiangShan backend。

> 2026-04-12 更新：当前代码已经落地本文后续规划中的两项关键补强——`non-mem blocker` 与 `store commit readiness`。因此这里对“当前模型”的描述，应理解为“一个已支持 `load/store/non_mem`、可通过 `BackendSendPlan` 编排 ROB 语义步骤的半模型”，而不再是最早期的 mem-only token 模型。

## 1.1 当前实现快照

为了避免把后文的大量历史分析与“当前代码到底已经做到哪里”混在一起，这里先给出一个简明快照。以当前实现为准，ROB 半模型具备以下行为：

- entry 类型
  - `load`
  - `store`
  - `non_mem`
- commit frontier 规则
  - `load` 只有在 `exec_completed=True` 时才能进入 commit packet
  - `store` 只有在 `store token` 可用且 entry 自身 `commit_ready=True` 时才能进入 commit packet
  - `non_mem` 在 release 前会卡住 ROB 连续前缀，younger mem 不得越过
- store readiness 来源
  - 常规路径下，`STD` / `STA` 对应的数据和地址就绪会在 `BackendFacade.execute()` 执行 `IssueCyclePlan` 时自动同步到 ROB 半模型
  - 如 testcase 需要更细粒度地控制 ready 时机，可显式使用 `StoreCommitReadyStep`
- non-mem op 的语义
  - 当前实现里的 “non-mem op” 本质上是 **ROB non-mem placeholder / blocker**
  - 它表达的是“ROB 程序序上存在一条 older non-mem 指令，当前是否允许提交”
  - 它不是 `IssueOp`，不会去驱动某条 issue lane
- 推荐控制面
  - 普通单笔 load/store 继续优先用 `env.backend.send(...)`
  - 需要控制拍级排列、non-mem blocker、store readiness 时，优先用 `env.backend.execute(BackendSendPlan(...))`
  - testcase 不应直接改 `env.rob_agent` 内部状态

一个最小示意例如下：

```python
env.backend.execute(
    BackendSendPlan.from_steps(
        NonMemBlockerStep.insert(rob_idx_flag=0, rob_idx_value=0x21),
        EnqueueStoreStep.from_txn(store_txn, ref=store_ref),
        IssueCyclePlan.from_ops(
            IssueOp.std(req_id=store_txn.req_id, sq_ptr=store_ref, data=store_txn.data, mask=store_txn.mask)
        ),
        IssueCyclePlan.from_ops(
            IssueOp.sta(req_id=store_txn.req_id, sq_ptr=store_ref, addr=store_txn.addr, mask=store_txn.mask)
        ),
        StoreCommitStep(count=1),  # younger store 仍会被 non-mem blocker 卡住
        NonMemBlockerStep.release(rob_idx_flag=0, rob_idx_value=0x21),
        StoreCommitStep(count=1),
    )
)
```

如果你关心的是“当前代码怎么用”，优先参考：

- `src/test/python/MemBlock/docs/backend_request_model_design.md`
- `src/test/python/MemBlock/tests/test_request_apis_backend_facade.py`
- `src/test/python/MemBlock/tests/test_MemBlock_rob_agent.py`

后文则更多保留设计分析、历史缺口与长期演进背景。

## 2. 真实后端与 MemBlock 的 ROB 交互接口概览

### 2.1 真实接口定义

在真实 RTL 中，后端 ROB 暴露给 LSQ/MemBlock 的接口定义在 `src/main/scala/xiangshan/backend/rob/RobBundles.scala` 的 `RobLsqIO` 中，核心字段包括：

- `lcommit`
- `scommit`
- `commit`
- `pendingPtr`
- `pendingPtrNext`
- `mmioBusy` 为反向输入

对应地，MemBlock 顶层在 `src/main/scala/xiangshan/mem/MemBlock.scala` 中接收：

- `io.ooo_to_mem.lsqio.lcommit`
- `io.ooo_to_mem.lsqio.scommit`
- `io.ooo_to_mem.lsqio.commit`
- `io.ooo_to_mem.lsqio.pendingPtr`
- `io.ooo_to_mem.lsqio.pendingPtrNext`

这说明，真实系统中 ROB 与 MemBlock 的关系不是“给一个提交计数就完了”，而是同时存在：

1. 提交数量信息：`lcommit`、`scommit`
2. 提交事件有效性：`commit`
3. 提交边界位置：`pendingPtr`
4. 下一边界预测：`pendingPtrNext`

这四类信息共同决定了 LoadQueue、StoreQueue、LoadQueueUncache、StoreMisalignBuffer 等模块对“哪条访存已经到达可提交边界”的理解。

### 2.2 `ROB -> MemBlock` 与 `MemBlock -> Backend` 是双向契约

当前讨论经常只盯着 `pendingPtr`，但真实系统其实是双向交互：

- 后端给 MemBlock：
  - `lcommit/scommit/commit/pendingPtr/pendingPtrNext`
- MemBlock 给后端：
  - `lqDeq/sqDeq/lqDeqPtr/sqDeqPtr`

后者会回到 backend/dispatch 资源视图中。例如 `src/main/scala/xiangshan/backend/Backend.scala` 中，`sqDeq` 和 `lqDeqPtr/sqDeqPtr` 都会送回 CtrlBlock/Dispatch 一侧。这意味着：

- 前向接口决定 MemBlock 是否认为某些 load/store 已达提交边界
- 反向接口决定 backend 是否认为某些 LSQ 资源已经释放

如果测试环境只模拟前向的一部分、不模拟后向反馈的效果，那么它能覆盖 MemBlock 内部部分语义，但不能说已经覆盖了全部后端与 MemBlock 的交互。

### 2.3 真实语义中 `scommit` 不等于完整 ROB frontier

一个非常重要但很容易被忽略的事实是：`scommit` 只表示本拍有多少条 store 从 ROB 提交到 LSQ 相关逻辑，并不等于“当前 ROB frontier 的全部语义”。真实后端里，ROB 提交是统一的程序序事件，只是提交的指令类型不同，才会拆分成：

- `lcommit`
- `scommit`
- `commit`

所以如果测试环境只把 `scommit` 当成“store 提交全部真相”，就会天然丢失：

- 非 mem 指令阻塞 mem 指令 commit 的语义
- load/store 混合提交的同拍关系
- `pendingPtrNext` 对边界推导的影响
- `commit` 布尔在某些状态机里的节拍意义

这也是后续为什么需要把当前模型继续升级的根本原因。

## 3. 当前测试环境中的 ROB 建模现状

### 3.1 当前实现的核心位置

当前 ROB 建模主要分布在以下文件中：

- `src/test/python/MemBlock/MemBlock_env.py`
- `src/test/python/MemBlock/agents/commit_agent.py`
- `src/test/python/MemBlock/monitors/mem_status_monitor.py`

其中最核心的是 `PendingPtrDriver`。它当前做的事情可以概括为：

1. 驱动 `io_ooo_to_mem_lsqio_pendingPtr_flag/value`
2. 驱动 `io_ooo_to_mem_lsqio_scommit`
3. 维护一个测试侧的 `_issued` 队列，按程序序记录当前环境关心的 mem op
4. 对 load，只有在观测到“完成”后才允许前移 `pendingPtr`
5. 对 store，只有在收到 `scommit` 预算后才允许前移 `pendingPtr`

这一版相较早期版本的最大进步在于：store 不再完全被排除在 `pendingPtr` 推进之外，因而双 store same-addr、store burst、mixed ld/st 等用例终于可以用递增 `robIdx` 正常建模，而不是复用同一个 `robIdx` 走捷径。

### 3.2 当前建模的真实含义

当前模型的真实含义并不是“测试侧有了一个完整 ROB”，而是：

- 测试侧有了一个只关心 mem op 的程序序队列
- 这个队列知道哪些 entry 是 load、哪些是 store
- load 依赖“已完成”才能释放 head
- store 依赖“收到 scommit 预算”才能释放 head
- 一旦释放 head，就同步推进 `pendingPtr`

因此，它是一个“mem-only commit frontier proxy”。这个设计对 MemBlock 来说很有用，因为 MemBlock 最直接关心的确实是：

- 这条 store 是否已到达可提交边界
- 这条 younger load 在 compare 时能否看到 older committed store

但它仍然不是“全局 ROB 模型”，因为它根本没有表达所有非 mem 指令。

### 3.3 load compare 与当前 ROB 模型的关系

当前环境中，load compare 的提交预算主要不是由测试侧主动驱动 `lcommit`，而是依赖 MemBlock 对外导出的 `lqDeq`。`MemStatusMonitor.after_cycle()` 会调用：

- `memory.note_load_commits(int(self.mem_status.lqDeq.value))`

这意味着当前 load compare 的链路是：

1. 测试侧 issue load
2. DUT 执行并写回
3. DUT 内部 LoadQueue/VirtualLoadQueue 在合适时机对外导出 `lqDeq`
4. Scoreboard 依据 `lqDeq` 给出的 budget 完成 commit-boundary compare

这条链路对当前 memory model 很重要，而且已经被大量测试复用。但从“ROB 输入建模是否真实”角度看，它是：

- 用 `MemBlock -> Backend` 的观察口，反推 load 提交预算
- 而不是显式构造 `ROB -> MemBlock` 的 `lcommit`

所以它是一个非常实用的 compare 收口手段，但不是完整的前向 ROB 驱动。

### 3.4 当前模型已经解决的核心问题

尽管存在缺口，当前模型仍然解决了几个非常关键的问题：

1. 单条 cacheable store 不再需要怪异的 `robIdx` 绕法才能进入 committed。
2. 双 store same-addr overwrite 可以用递增 `robIdx` 正常建模。
3. small mixed ld/st random 中，store 不再必须固定 `robIdx=0` 才能收敛。
4. load compare 与 older committed store 的程序序关系得到了基本维持。
5. 环境至少建立起了“store committed 既依赖程序序，又依赖 scommit”的概念。

这些收益说明当前模型不是错误方向，而是正确但尚未完成的第一阶段实现。

## 4. 当前模型与真实后端的一致点

为了避免文档只强调不足，这里先明确当前模型与真实后端已有的几个一致点。

### 4.1 store commit 确实依赖 ROB 边界

在真实 `NewStoreQueue` 中，store 被标记为 committed 的条件之一就是其 `robIdx` 不能晚于 `pendingPtr`。当前环境现在也通过 `pendingPtr + scommit` 的组合去推动 store committed。这说明方向是对的：

- store committed 不是看 STA/STD 完成就行
- 也不是仅靠 `scommit` 一个脉冲就行
- 必须同时考虑程序序边界

### 4.2 `robIdx` 仍是 MemBlock 验证中的程序序主键

不论是真实 RTL 还是当前测试环境，`robIdx` 都是连接：

- issue
- store shadow
- load writeback
- pendingPtr
- commit-boundary compare

的主键。这一点当前环境没有偏离真实设计。

### 4.3 younger load 的 architectural view 必须依赖 older committed store

当前 Scoreboard 的核心语义是：在 load compare 时，要先把所有 younger load 边界之前的 older committed store 合并进 architectural memory，再去比较 load 返回值。这个思想与真实系统高度一致，因为真实程序序下：

- older committed store 对 younger load 应该可见
- 但未提交、未满足可见条件的 store 不应提前进入年轻 load 的架构视图

### 4.4 `scommit` 的使用方向是正确的

当前环境虽然没有完整建模 ROB，但已经把 `scommit` 用在了正确方向上：

- 它不是“外部 drain 成功数”
- 不是“store issue 数”
- 不是“SQ 已写地址/数据条数”
- 而是“允许 store 沿程序序推进提交边界的预算”

这使得环境能够把 STA/STD 完成和 committed 语义分开处理，避免再次退回“store data 到了就算 committed”的错误模型。

## 5. 当前模型与真实后端的不一致点

### 5.1 只建模了 `RobLsqIO` 的一部分

真实接口有 `lcommit/scommit/commit/pendingPtr/pendingPtrNext` 五元组，而当前模型实质上只主动驱动了：

- `pendingPtr`
- `scommit`

这直接导致模型只覆盖了提交边界的一部分语义。

### 5.2 当前 `pendingPtr` 是 mem-op 队列，不是真实全局 ROB

当前 `_issued` 队列里只有 load/store。真实后端 ROB 则包含：

- load/store
- ALU/FPU/vector 非访存指令
- fence/sfence/special 指令
- 异常、flushPipe、replay、single-step 等可能阻塞提交的 entry

因此，当前模型天然无法表达“非 mem 指令阻塞 mem commit”的真实现象。

### 5.3 没有真实建模 commit width 与混合提交

当前模型虽然支持 `queue_store_commit(count)`，但整体仍然更像“测试侧给一拍 store 提交预算”。它没有形成真正的 commit packet 概念，因此同拍：

- 2 条 load + 1 条 store
- 1 条 load + 2 条 store
- load/store/head 阻塞混合

这类场景都只能被粗略近似。

### 5.4 没有建模 `pendingPtrNext`

真实接口里 `pendingPtrNext` 是正式输入，StoreMisalignBuffer 和 LSQWrapper 都接了它。当前环境没有驱动它，意味着凡是依赖“下一拍或下一提交边界推导”的逻辑都没有覆盖。

### 5.5 没有建模 `commit` 布尔语义

当前环境没有驱动 `commit`。如果 DUT 内部某些逻辑不只依赖“数量”，还依赖“本拍是否是合法 commit 拍”，那么当前模型无法验证这类状态机。

### 5.6 load side 仍以 `lqDeq` 为主，而非完整 `lcommit`

这使当前 compare 收口很实用，但也意味着“ROB 到 MemBlock 的 load commit 输入语义”并没有真正建立。

### 5.7 没有建模 redirect/flush/exception/cancel 对 ROB frontier 的影响

当前 `PendingPtrDriver` 是线性推进。真实 LSQ/ROB 交互里却存在：

- redirect
- cancel count
- exception at head
- flushPipe
- vector inactive / special walk

这些都可能改变 frontier 推进和可提交条数。

### 5.8 没有建模 backend feedback 资源闭环

当前环境会观测：

- `lqDeq`
- `sqDeq`
- `lqDeqPtr`
- `sqDeqPtr`

但基本没有把它们当作测试侧 backend credit/dispatch 模型的输入。因此，它更偏“MemBlock 单边验证”，而非“完整 backend <-> MemBlock 交互验证”。

## 6. “不能覆盖或覆盖不完整的”逐项影响分析

下面对之前识别出的每一项不足逐条分析：它是否会影响 MemBlock 验证、典型影响场景是什么、为什么会有影响、以及当前阶段能否接受。

### 6.1 ROB 中夹杂非 mem 指令的真实提交阻塞

**是否影响 MemBlock 验证：强影响，但主要影响高级顺序与异常类场景。**

当前模型只维护 mem op 队列，因此如果测试程序序里存在：

- `store(rob=10)`
- `alu(rob=11)`
- `load(rob=12)`

且 `alu(11)` 因异常、等待结果、flushPipe 或 replay 暂时不能提交，那么真实后端里：

- `pendingPtr` 会卡在 `rob=11` 之前
- `load(12)` 不应被视为已到达提交边界
- `store(10)` 之后的 younger mem op 也不应越过这条非 mem 阻塞点

而当前模型由于根本没有 `alu(11)` 这个 entry，可能会直接把 frontier 从 `rob=10` 推到 `rob=12`。这样会带来两类风险：

1. **假阳性**：某些本应因为非 mem head 阻塞而晚提交的 load/store，在测试中过早被视为 committed/visible。
2. **覆盖空洞**：与真实后端“阻塞提交但不阻塞执行”的复杂窗口相关的 bug，测试完全表达不出来。

典型影响场景包括：

- younger load 已完成写回，但中间有一条异常 ALU 指令尚未 commit，此时 current model 可能提前 compare；
- store committed 依赖 older non-mem 指令先过 ROB head，但 current model 只看 mem op，因此 store 可能过早 committed；
- mixed ld/st + fence/sfence/order instruction 场景中，真实顺序被非访存 head 约束，而当前模型完全忽略。

这一项对当前基础标量 smoke 的影响不大，因为那些用例大多是 mem-dominant 场景，没有显式插入 non-mem entry。但它对“是否能覆盖真实程序序下的 MemBlock 顺序问题”影响很大，尤其是一旦后续要验证：

- fence 前后的 store/load 可见性
- replay 后 older 指令是否真的已经跨过 ROB head
- 异常/flush 与 younger mem 指令的关系

当前阶段可以暂时接受它未实现，但前提必须写清楚：**当前回归验证的是 mem-only 或 mem-dominant 程序序，不是完整 ROB 程序序。**

### 6.2 同拍 `lcommit + scommit` 混合提交

**是否影响 MemBlock 验证：中到强影响，尤其影响高并发 commit 和 commit-width 相关场景。**

真实后端每拍可以同时提交多条指令，而这些指令里可能同时包含：

- load
- store
- non-mem

因此 `lcommit` 和 `scommit` 在同一拍同时非零是非常自然的事情。当前模型却把 load commit 主要交给 `lqDeq` 去反推，把 store commit 交给测试侧 `scommit` 去驱动。这样会造成两个问题：

1. 没有一个统一的“本拍 commit packet”概念。
2. load 和 store 的提交关系被拆成两套来源，失去了同拍一致性。

典型影响场景：

- 同拍 older load 获得提交预算、older store 也获得 `scommit`，而 younger load 的 compare 是否应看到该 store，需要一个严格的拍边界定义；
- 多发射/multi-commit 场景下，前两条是 load，第三条是 store，真实 `pendingPtr/pendingPtrNext` 的推进不是简单地把两套事件分别处理；
- 某条 store 与同拍若干 load 一起提交时，LSQ/StoreQueue/LoadQueue 的观察点可能存在拍间延迟差，若测试侧不统一建模，容易得到“能跑，但对拍边界定义含糊”的结果。

这一项为什么影响 MemBlock 验证？因为 MemBlock 很多细节问题并不是“最终对不对”，而是“同一提交边界下，哪些 older op 应该被看见，哪些还不该”。如果测试侧把 load commit 和 store commit 分裂成两种时钟源，就很难严谨回答这个问题。

当前阶段，这一项对单发射、少量 directed case 的影响中等，因为大多数 case 不刻意构造同拍混合提交。但一旦要向更真实的：

- mixed ld/st burst
- replay 后同拍收敛
- higher commit-width program order

推进，它就会成为显著缺口。

### 6.3 `pendingPtrNext` 参与的路径

**是否影响 MemBlock 验证：中等影响，当前基础场景影响有限，但对完整性与边界条件很重要。**

真实接口显式包含 `pendingPtrNext`，且它被 StoreMisalignBuffer、LSQWrapper 等路径接收，说明这不是一个“为了美观多导出”的冗余信号，而是某些逻辑确实需要知道下一提交边界。当前环境完全没有驱动它，意味着这些路径只能在默认或未定义值下运行。

典型影响场景：

- 某些 misalign / split store / special walk 路径需要同时参考当前边界和下一边界，决定是否可推进；
- 某些 store queue 判断可能依赖“本拍提交后下一拍 frontier”来处理边界重叠；
- 某些 load replay / uncache / mmio 相关状态机在 commit-stuck 或边界观察时，可能需要 next pointer 才能避免多拍抖动。

为什么它会影响 MemBlock 验证？因为有些 bug 不是出在“当前拍 head 是谁”，而是出在“下一拍 head 将是谁”的推导错误。尤其在：

- 一拍提交多条
- 当前拍 head 释放后下一条马上可见
- 边界和 replay/nuke 的判定跨拍生效

这些窗口里，`pendingPtrNext` 可以帮助 DUT 内部做更稳定的边界判断。测试环境若始终不驱动它，就只能验证“不使用该信号或对其不敏感”的路径。

当前阶段可以暂时接受，因为当前已覆盖的标量 ld/st directed case 大多不深入 misalign/next-boundary 细节。但如果后续要把 ROB 建模提升到“与真实后端接口一致”，`pendingPtrNext` 不能再长期留空。

### 6.4 `commit` 布尔与 `mmioBusy` 的交互

**是否影响 MemBlock 验证：中等影响，对 MMIO/uncache/commit-stuck 类场景影响明显。**

真实 `RobLsqIO` 里既有 `commit`，又有 `mmioBusy` 反向输入。这说明真实系统中存在一些不是靠 `lcommit/scommit` 计数就能表达的状态，例如：

- 本拍是否真的处于 commit 周期
- MMIO/uncache 路径是否使 commit 受阻或粘滞
- 某些队列是否要用 commit-stuck 检测避免死锁

`LoadQueueUncache` 中明确提到了 `mmio commit`、`commit-stuck detect` 和 `io.rob.mmioBusy`。当前测试环境虽然能观测 `mmioBusy`，但没有把 `commit` 布尔与 `mmioBusy` 建立成统一的 forward model。这带来的影响主要有：

1. MMIO/NC 场景下，测试能验证请求走没走对路径，但很难验证“该路径是否正确影响 ROB 提交节奏”。
2. 某些“MMIO busy 时 younger load/store 是否应延迟 committed/visible”的问题，当前模型表达不完整。
3. commit-stuck 相关 deadlock 类问题在当前环境中很难被定向构造。

典型影响场景包括：

- older MMIO load/store 长时间占用 uncache buffer，真实后端 commit 与 mmioBusy 联动阻塞 younger mem op；
- sfence/mmio/nc 混合窗口中，本拍是否有 commit 对某些状态机很重要，但当前环境只在 store path 上脉冲 `scommit`；
- MMIO 测试虽然能看到 outer 请求发出，但并不能证明 backend/ROB 视角的 busy/commit 行为正确。

这一项对普通 cacheable load/store 冒烟影响有限，但对 “MMIO/uncache 行为是否只是数据正确，还是提交语义也正确” 影响明显。当前若继续扩大 MMIO/NC 场景覆盖，这一项迟早需要补上。

### 6.5 redirect / flush / cancel count 对 LSQ free-count 和 pointer 的影响

**是否影响 MemBlock 验证：强影响，尤其影响 replay、flush、exception 和恢复类场景。**

真实 `LSQWrapper` 的 free-count 和 pointer 更新不是简单的线性“加提交、减分配”，还会在 redirect 后结合 cancel count 修正。当前测试侧 `PendingPtrDriver` 完全没有这一层，只是顺序推进 mem-op frontier。这意味着凡是涉及：

- redirect
- flush
- replay rollback
- exception/cancel
- younger load/store 被冲刷

的场景，当前 ROB 建模都只能部分近似。

典型影响场景：

- RAW/RAR replay 后，younger load 被回滚，真实 LSQ 资源和 frontier 会一起修正；当前模型更多只看 writeback 与 replay 事件，不表达 ROB/LSQ 恢复；
- exception at ROB head 时，年轻 store/load 虽然已经 issue 甚至已经部分执行，但不应继续按正常 commit 边界推进；
- flushSb/sfence 后，如果还有 younger mem op 被取消，真实 sq/lq counter 与 deqPtr 的变化会影响后续分配和提交，而当前模型没有覆盖。

这一项为什么重要？因为 MemBlock 的许多白盒价值恰恰体现在 replay/redirect/violation 恢复路径上。如果测试侧 commit frontier 始终“只会向前，不会被恢复语义约束”，那么就会出现：

- 表面上 replay cause 看起来命中
- 最终 data 也可能正确
- 但 middle-state 的提交恢复其实与真实后端不一致

对于当前基础标量 smoke，这一项不是首要瓶颈；但对于未来要构造更复杂的：

- replay mix
- redirect after replay
- exception + younger mem
- flush/recovery stress

场景，它是明显必须解决的差距。

### 6.6 backend <- mem 的 `sqDeq/lqDeqPtr` 反馈场景

**是否影响 MemBlock 验证：当前阶段中等影响，对“完整 backend <-> MemBlock 闭环验证”是强影响。**

当前环境已经绑定了：

- `sqDeq`
- `lqDeq`
- `sqDeqPtr`
- `lqDeqPtr`

但多数情况下把它们当作观测口，而不是测试侧 backend 资源模型的输入。这样做对“单边 MemBlock 功能验证”足够直接，但它无法回答更完整的问题：

- backend 是否因为 MemBlock 的 deq 反馈而正确恢复 dispatch/rename credits
- 某些 replay/blocking 是否与 `sqDeqPtr` 的推进同步
- `LoadQueueReplay` 等模块对 `sqDeqPtr` 的观察是否与 backend 侧消费节奏一致

典型影响场景包括：

- `LoadQueueReplay` 中某些 blocking 条件会参考 `sqDeqPtr`，真实系统里这个指针与 backend free-count/dispatch 是联动的；
- 如果 future test 需要验证“队列快满 -> deq 恢复 -> younger 指令重新可派发”，当前环境无法表达完整闭环；
- 某些复杂 replay 场景表面上只需要 MemBlock 内部事件，但它们实际上与队列资源何时释放密切相关。

这一项对当前基础 MemBlock directed tests 的影响相对较弱，因为这些用例多数不依赖 backend dispatch credit 精确建模；但如果目标升级成“验证后端与 MemBlock 的交互场景”，那它就是决定性缺口。换句话说：

- 对“MemBlock 内部功能验证”：当前可接受
- 对“ROB/Dispatch/LSQ 联动验证”：当前明显不足

## 7. ROB 建模需求清单

下面给出下一阶段 ROB 建模的正式需求清单。每项需求都包含背景、典型场景与用例分析、缺失风险与优先级判断。这里的重点不是列清单，而是说明“为什么需要它”，以及“若不做会漏掉什么类型的 MemBlock 验证”。

### 7.1 需求一：完整驱动 `RobLsqIO` 五元组

需求内容是：测试环境不能再只驱动 `pendingPtr` 与 `scommit`，而应形成统一的 `RobLsqIO` 驱动，至少覆盖：

- `lcommit`
- `scommit`
- `commit`
- `pendingPtr`
- `pendingPtrNext`

为什么这是第一优先级需求？因为当前所有后续改进几乎都依赖于“接口先完整”。如果接口本身只驱动一半，后面再讨论 mixed commit、non-mem 占位、redirect 恢复，都会变成建立在不完整 contract 上的补丁。

典型场景一是“同拍 load/store 混合提交”。例如测试中构造一段程序序：

- `load rob=20`
- `store rob=21`
- `load rob=22`

真实后端在某一拍可能同时提交前两条，导致：

- `lcommit=1`
- `scommit=1`
- `commit=1`
- `pendingPtr/pendingPtrNext` 同拍推进

若测试环境只驱动 `scommit` 而不驱动 `lcommit/commit/pendingPtrNext`，那么：

- store 的 committed 可能能前进
- 但 load compare 的 budget 仍依赖 `lqDeq`
- 同拍边界下“younger load 是否该看到 older store”的拍语义会变得模糊

典型场景二是“MMIO/uncache commit-stuck”。真实系统里，某拍可能 `commit=0`，即便前一拍已有 `pendingPtr`，也不表示本拍发生了新的提交事件。如果测试环境永远没有 `commit` 这个概念，那么：

- 某些需要区分“本拍 frontier 稳定”和“本拍 frontier 刚刚前移”的状态机无法验证

典型用例可以设计为：

1. directed mixed ld/st with same-cycle commit；
2. older MMIO busy + younger cacheable load/store；
3. load compare 与 store committed 同拍边界检查；
4. pendingPtr/pendingPtrNext 一致性自测。

若不实现该需求，测试环境将持续处于“部分接口代理”的状态。这样虽然能支撑当前部分 directed 用例，但所有后续设计都会建立在脆弱假设上：一旦真实 DUT 某条路径开始真正依赖 `pendingPtrNext` 或 `commit`，环境就会出现“看起来能跑、实际上没在验证真实语义”的问题。

因此，这项需求的优先级是**最高**。它不是锦上添花，而是后续所有更真实 ROB 建模的基础。

### 7.2 需求二：建立统一的测试侧 ROB 提交队列

当前环境里 load 和 store 的提交推进来源是分裂的：

- load 通过 issue/complete/lqDeq 间接收口
- store 通过 allocate/scommit/pendingPtr 推进

这在实现上很实用，但从建模角度看，它不是“统一 ROB 提交队列”，而是“两条不同语义链路的局部拼接”。下一阶段必须引入一个统一的测试侧 ROB 队列，至少能记录：

- entry 类型：load / store / non-mem placeholder
- `robIdx`
- 是否完成执行
- 是否允许提交
- 是否被 redirect/flush kill
- 提交后应对 `lcommit/scommit/commit/pendingPtr/pendingPtrNext` 产生什么影响

为什么这项需求重要？因为“程序序”不是 load 程序序和 store 程序序的简单并集，而是统一 ROB 序列。如果没有统一 ROB 队列，测试环境就很难定义下面这些场景：

- older load 未完成，younger store 已经 ready，但仍然不能越过；
- older store 已经 ready，younger load 也已经写回，但两者真正进入提交边界的先后取决于 ROB head；
- 两条 mem 指令中间插着非 mem entry 时，frontier 是否还能推进。

典型场景一是“load/store/alu 混合顺序”：

- `store rob=30`
- `alu rob=31`
- `load rob=32`

真实 ROB 会认为三条是一整个序列，frontier 是否前进取决于 31 是否可提交。若测试侧只有 mem-op 局部队列，就会把 30 与 32 直接视为相邻，从而错误推进。

典型场景二是“same-addr younger load 先执行但未 commit”：

- `store rob=40`
- `load rob=41`
- `store rob=42`

其中 41 可能先写回，但真正 compare 仍受统一 commit frontier 约束。如果测试侧没有一条统一 ROB 队列，就只能用多个局部 helper 间接近似，很难保持一致。

典型用例分析应包括：

1. non-mem 阻塞下的 store->load 可见性；
2. replay 后 younger load 已 writeback 但未 commit；
3. 同拍混合提交包生成；
4. 头部异常导致 younger mem 不得推进。

若不实现该需求，后续所有所谓“更真实的 ROB 驱动”都会退化成对若干独立 helper 的修补，而不是建立一个统一、可推理、可验证的 ROB 程序序模型。因此，这项需求优先级为**最高**，仅次于完整驱动五元组。

### 7.3 需求三：支持 non-mem 占位 entry

测试环境不需要真的驱动整个 ALU/FPU/backend，但必须能在测试侧 ROB 模型里表达“存在一条非访存指令，占据 ROB 程序序位置，并且当前尚未允许提交”。这类 non-mem 占位 entry 是让 ROB 建模摆脱“mem-only proxy”的关键。

为什么需要它？因为很多 MemBlock 相关问题从根本上讲不是“MemBlock 内部算错了”，而是“MemBlock 被放在一个不真实的 commit 环境里测试了”。如果测试环境里永远只有 mem entry，那么：

- 所有 younger mem 都只会被 older mem 约束
- 永远不会出现真实后端里大量存在的“非 mem 头阻塞”
- 某些依赖 commit frontier 精确位置的 bug 根本不会出现

典型场景一是“异常非 mem 指令阻塞 younger store commit”：

- `store rob=50`
- `branch/alu exception rob=51`
- `store rob=52`

真实系统里，52 即使已经 STA/STD 全部 ready，也不能因为测试侧打了一拍 `scommit=1` 就 committed；它必须等 51 先被处理。若没有 non-mem 占位，测试模型就会把 52 错误地放行。

典型场景二是“fence/sfence/order instruction 阻塞 younger load compare”：

- `load rob=60`
- `sfence rob=61`
- `load rob=62`

如果测试环境里没有 61 这个 entry，就无法表达“62 已经执行到了某阶段，但还不应穿过提交边界”的语义。

典型场景三是“MMIO + non-mem blocking”：

- `mmio store rob=70`
- `non-mem rob=71`
- `cacheable load rob=72`

真实行为中，72 的可提交性不仅取决于 mmio 结果，也取决于 71 是否跨过 ROB head。当前 mem-only 模型无法表达这一点。

典型用例分析可以围绕：

1. same-addr store/load 中间插一条 non-mem blocker；
2. older load 已 writeback，但中间 non-mem 未 commit，younger load 不应提前 compare；
3. mixed ld/st with fence placeholder；
4. exception placeholder + redirect 场景。

若不实现 non-mem 占位，测试环境可以继续覆盖很多“纯 mem 程序序”场景，但无法声称自己在验证“真实后端语义下的 MemBlock”。因为真实后端中，ROB head 被非访存指令占据是常态，而不是角落案例。这项需求的优先级是**高**，它是从“mem proxy”走向“近似 ROB 模型”的分水岭。

### 7.4 需求四：支持 commit width 下的混合提交包

真实 ROB 提交不是“每拍只能提交一条，而且只能是 load 或 store 之一”。它是一个多条目、可混合的提交事件。因此，测试侧需要一个明确的“commit packet”概念，能在一拍内描述：

- 本拍是否有 commit
- 本拍提交了几条 load
- 本拍提交了几条 store
- 这些提交对应的 head/frontier 推导结果是什么

这一需求的重要性在于：一旦没有 commit packet，测试环境就会被迫依赖多个彼此松散耦合的事件源：

- `queue_store_commit(count)`
- `lqDeq`
- `load completed`
- `pendingPtr` 自己前移

而这些事件的拍关系很容易在复杂场景中失真。

典型场景一是“同拍 older store 提交，younger load compare”：

- `store rob=80`
- `load rob=81`

若两者都在同一提交窗口被考虑，那么测试侧需要明确：

- 本拍 commit packet 是否已经包含 store；
- 本拍 load compare 是看当前 `pendingPtr` 还是 `pendingPtrNext`；
- younger load 是否应在这一拍视 store 为 committed；

没有 commit packet，就很难在测试环境中为这一拍给出唯一解释。

典型场景二是“两个 load + 一个 store 同拍 commit”：

- `load rob=90`
- `load rob=91`
- `store rob=92`

真实后端下，commitWidth 允许三者在一个拍或相邻拍被消费，相关 `lcommit/scommit` 会同时变化。若测试侧只用 अलग立 helper，便无法表达：

- 同拍混合提交的原子性；
- `pendingPtrNext` 相对于 `pendingPtr` 的一致推导；
- 当前拍和下一拍边界谁可见、谁不可见。

典型用例分析应覆盖：

1. same-addr store 与 younger load 同拍 commit；
2. 多条 younger load 已写回，但 commit packet 每拍只放一部分；
3. store burst + load burst 混合窗口；
4. replay 恢复后与正常提交的交错。

若不实现该需求，测试环境就会继续只适合单拍、低并行、低歧义的提交模型。一旦 commitWidth 场景增多，环境的解释力会明显下降。这项需求优先级为**高**，建议在完整五元组和统一 ROB 队列之后尽快跟上。

### 7.5 需求五：支持 redirect/flush/exception/cancel 对 commit frontier 的影响

真实后端和 LSQ 的交互中，commit frontier 不是一条只会前进的直线。redirect、flush、exception、cancel count 都可能使：

- 某些 entry 被取消
- 某些已分配资源被回收
- pointer/free count 出现修正
- 某些已执行但未提交的 mem op 失效

当前测试环境几乎没有建模这部分，因此只适合验证“正常向前推进”的提交语义，不适合验证“恢复路径下 frontier 的变化”。

为什么这项需求必须存在？因为 MemBlock 很多白盒验证价值正体现在 replay/violation/recovery 中。如果 ROB 模型不理解 redirect/flush/cancel，那么：

- replay 场景只能验证 replay event 是否出现，不能验证 replay 后提交边界是否恢复正确；
- exception/flush 场景只能依赖最终无 outstanding，而不能验证 intermediate frontier 是否合理；
- 某些 younger mem op 本应在 redirect 后失效，但测试模型可能仍把它们保留在提交队列中。

典型场景一是“RAW replay 后 younger load 被冲刷”：

- older store unresolved
- younger loads fill raw window
- 触发 RAW replay 或 nuke
- redirect 生效后，部分 younger load 不应再出现在提交 frontier 上

如果测试侧 ROB 队列没有 cancel 语义，那么这些 younger load 可能仍然被误认为将来要 commit。

典型场景二是“RAR violation 后 older load 重放、younger 路径作废”：

- younger load 已先写回
- release/probe 触发 RAR violation
- older load 成为真正需要保留的路径

此时测试侧不仅要观察 replay/nuke，还要知道 commit frontier 上哪些条目应被取消、哪些应保留。

典型场景三是“异常 store 或 MMIO 路径引发 flush”：

- 某条 store 已经 materialize，但 head 发生 exception
- younger mem op 已 issue 但不应继续 commit

这一需求的典型用例分析应覆盖 replay、exception、flushSb/sfence、redirect 等。若不实现，该环境虽然还能做基础功能验证，但无法把复杂 replay/violation 的验证上升到“提交语义也正确”的层级。这项需求优先级为**高**，通常放在非 mem 占位与 mixed commit 之后。

### 7.6 需求六：支持 `mmioBusy` / uncache / commit-stuck 相关辅助语义

真实 `LoadQueueUncache` 明确带有 `mmio commit`、`commit-stuck detect` 等行为，且 `RobLsqIO` 中还存在 `mmioBusy` 反向接口。说明在 MMIO/uncache 路径里，ROB 与 MemBlock 的交互不只是：

- “这拍提交几条”
- “当前 head 到哪”

还包括：

- 当前是否存在 MMIO busy
- 该 busy 是否影响 commit
- 某些 uncache buffer 是否因为 commit-stuck 进入特殊状态

当前环境虽然能观测 `mmioBusy`，但没有把它变成 ROB forward model 的组成部分。因此，当前 MMIO/NC 验证更多只是在验证：

- 请求是否发出
- 数据是否返回
- 最终是否没有 outstanding

却没有很好验证：

- MMIO busy 是否真的阻塞或改变提交节奏
- older MMIO 是否影响 younger cacheable op 的提交边界
- commit-stuck 相关保护是否生效

典型场景一是“older MMIO store 长时间 busy，younger cacheable load 已经 ready”：

- 当前模型可能只要 `scommit` 给了就继续推进
- 真实系统则可能受到 `mmioBusy` 与 commit 约束

典型场景二是“NC load/store 与普通 cacheable op 交织”：

- 某些 younger op 的执行可以先完成
- 但提交/可见性仍受 older uncache path 影响

典型场景三是“uncache response 慢、commit frontier 卡住”：

- 若没有 commit-stuck 相关建模，测试只能看 timeout，而无法验证 DUT 是否按设计处理阻塞。

典型用例分析应包括：

1. older MMIO busy + younger load compare 延迟；
2. NC store / NC load 与 normal cacheable mixed；
3. commit-stuck detect 的 directed case；
4. flush 与 mmioBusy 组合。

若不实现该需求，环境仍然可以做“MMIO/NC 功能 smoke”，但不能做“MMIO/NC 提交语义验证”。这项需求优先级为**中高**，建议在五元组和统一 commit packet 之后尽快加入。

### 7.7 需求七：支持 backend 反馈接口的半模型

如果测试目标始终只限定为“MemBlock 单边功能验证”，那么观测 `lqDeq/sqDeq/lqDeqPtr/sqDeqPtr` 已经有价值；但如果目标扩展为“后端与 MemBlock 的交互场景验证”，就必须为这些反馈接口建立一个至少半真实的 backend resource model。

所谓“半模型”，不是要求测试环境真的重建 dispatch/rename/ROB 全流程，而是至少要能在测试侧表达：

- LSQ 资源是否因为 `lqDeq/sqDeq` 被释放
- 某些分配/阻塞/回放条件是否与 `lqDeqPtr/sqDeqPtr` 一致
- backend 侧是否能观察到 MemBlock 的资源反馈并据此改变允许发射的流量

为什么需要这一项？因为真实系统里，MemBlock 不是孤立模块。某些 replay、blocking、queue full、younger op 可否继续入队的问题，本质上依赖：

- backend 认为 LSQ 还有多少资源
- deqPtr 是否已经回收对应条目

典型场景一是“LoadQueueReplay 与 sqDeqPtr 的联动”：

- younger load 的 replay/blocking 条件可能参考 `sqDeqPtr`
- 若测试侧永远只盯内部 replay event，而不检查 `sqDeqPtr` 反馈是否合理，就很难保证场景接近真实资源状态

典型场景二是“队列接近满 -> deq 恢复 -> 再次分配”：

- 真实 backend 会因为 MemBlock 对外导出的 deq 信息恢复 credits
- 若测试环境没有这一层，就只能用固定队列大小和被动 ready 近似

典型场景三是“复杂 replay mix + queue pressure”：

- 仅靠随机流量很难知道问题是 replay 条件错，还是 resource feedback 错

典型用例分析应包括：

1. lq/sq near-full directed case；
2. deqPtr 推进后 younger op 恢复 issue；
3. replay blocking 与 sqDeqPtr 一致性；
4. backend feedback 对 stress regression 的价值。

若不实现该需求，当前环境仍适合做 MemBlock 内部白盒功能验证；但它不能覆盖“真实后端资源闭环”。这项需求优先级为**中**，更适合作为二期或三期目标，而不是第一步就上。

### 7.8 需求八：与现有 MemoryModel/Scoreboard 保持兼容

这是一个经常被低估，但实际上非常关键的需求。下一代 ROB 建模不能因为追求“更真实”而打破当前已经建立的：

- load commit-boundary compare
- deferred store visibility
- flush/drain 最终一致性收口
- replay 事件观测与结构化 helper

为什么这一点如此重要？因为当前环境最大的价值之一就是：它已经拥有一套可工作的、与真实 DUT 对齐的 memory model/scoreboard 体系。如果新 ROB 建模把这套体系打碎，那么即便 forward model 更逼真，整体环境也可能退化为更难维护、更难解释的状态。

典型场景一是“load compare 预算来源变化”：

- 当前 compare 主要靠 `lqDeq`
- 如果未来引入测试侧 `lcommit`
- 就必须决定：是完全替代 `lqDeq`，还是双轨对齐，还是保持 `lqDeq` 收口、`lcommit` 只做一致性检查

这一决策直接决定了现有上百条 sequence/testcase 是否要大面积重写。

典型场景二是“store committed 语义升级”：

- 当前 store 通过 `pendingPtr + scommit + store shadow` 收敛
- 若 future 引入完整 commit packet
- 必须保证 flush/drain 与 architectural memory 的闭环仍成立

典型场景三是“RAR/RAW replay 等复杂场景”：

- 当前已经能在真实 DUT 上跑出 replay 事件和最终 compare
- 新模型不能把这些用例重新变成 fragile 状态

这一需求的典型用例分析，应围绕兼容性回归展开：

1. 现有 random load/store 不应因新 ROB 模型失效；
2. 现有 replay smoke 不应因新 commit packet 失效；
3. 新旧模型下的 compare 结果应保持一致；
4. 新模型引入后，应先增加一致性断言，再逐步切换主语义来源。

若忽略这一需求，后续 ROB 建模极易走向“局部更真实，整体不可用”。因此，它的优先级也是**最高**，是所有设计与实现工作的边界约束。

## 8. ROB 建模的概要设计

下一代 ROB 建模的设计目标，不是把 XiangShan 后端完整搬到 Python，而是构造一套“对 MemBlock 验证足够真实、对当前环境足够兼容、对未来 replay/ordering/uncache 场景足够可扩展”的测试侧 ROB 代理层。这个代理层的职责很明确：统一描述测试场景中的程序序、提交边界、提交包、取消/恢复语义，并把这些信息转换成 `RobLsqIO` 的前向驱动，以及必要的后向一致性检查。

概要上，建议把新模型拆成三层。

第一层是**ROB entry 层**。这一层维护测试侧的程序序对象，每个 entry 至少包含：

- `robIdx`
- `kind`：load/store/non-mem
- `is_mem`
- `is_store`
- `is_mmio` / `is_nc`
- `issued`
- `exec_completed`
- `commit_ready`
- `cancelled`
- `exception_like`

这层的目的不是做精确流水线，而是表达“ROB 序列里现在有哪些条目，它们各自的状态如何”。只有有了统一的 entry 层，后续的 `pendingPtr/pendingPtrNext` 推导才有统一输入。

第二层是**commit packet 层**。这层按拍生成一个结构化提交包，明确给出：

- 本拍是否存在 commit 事件：`commit`
- 本拍提交的 load 数量：`lcommit`
- 本拍提交的 store 数量：`scommit`
- 本拍提交前的 ROB 边界：`pendingPtr`
- 本拍提交后的下一边界：`pendingPtrNext`

这样，测试环境就不再需要分别从 `load completed`、`scommit`、`lqDeq` 等多处拼凑语义，而是可以先有一个统一的“提交真值”，再决定哪些字段直接驱动 DUT，哪些字段只用于一致性检查。概念上，这比当前 `PendingPtrDriver` 更接近真实后端每拍向 MemBlock 输出的 ROB-LSQ 交互包。

第三层是**兼容桥接层**。由于当前环境已经形成了：

- `MemoryModel.note_load_commits(lqDeq)`
- `Scoreboard` 的 commit-boundary compare
- store shadow + flush/drain 的语义闭环

所以新设计不能粗暴替换全部链路，而应采用兼容桥接：

1. 先引入新的 ROB model 与 commit packet；
2. 初期仍保留当前 `lqDeq -> note_load_commits()` 作为 compare 主收口；
3. 新模型输出的 `lcommit/pendingPtr/pendingPtrNext` 先用于：
   - 驱动 DUT
   - 做一致性检查
4. 在模型足够成熟后，再决定是否把部分 compare 预算来源从 `lqDeq` 升级为“`lcommit` 与 `lqDeq` 双轨校验”。

这样的设计有几个明显好处。

第一，它能把“接口真实性”和“scoreboard 稳定性”分离。也就是说，我们可以先让 DUT 看到更真实的 `RobLsqIO`，而不必第一步就动摇现有 compare 体系。

第二，它允许分阶段演进。第一阶段先补齐五元组与 commit packet，第二阶段再加 non-mem 占位与 mixed commit，第三阶段再引入 redirect/cancel 和 backend feedback 半模型。这样每个阶段都有明确边界，不会把环境复杂度一次性推到不可维护。

第三，它能更清楚地定义 sequence 层与 ROB model 的关系。今后 sequence 不应再直接理解“这拍怎么 pulse store commit”，而应主要做两件事：

- 组织事务与场景；
- 在必要时向 ROB model 声明某些测试意图，例如插入 non-mem blocker、安排某拍 mixed commit、触发 redirect/cancel。

真正的 `pendingPtr/pendingPtrNext/commit/lcommit/scommit` 推导则统一收敛到 ROB model。

第四，它能为未来的更复杂验证提供自然扩展点。例如：

- RAR/RAW 场景中 older load 先写回但未 commit；
- MMIO busy 与 commit-stuck；
- lq/sq near-full 与 deqPtr/dispatch feedback；

这些都需要一个统一的 ROB-LSQ 交互建模层，而不是继续把语义散落在 `PendingPtrDriver`、`env.backend.pulse_store_commit()`、`lqDeq` monitor 和 testcase 的局部等待逻辑中。

因此，概要设计的核心结论是：**下一代 ROB 建模应演化为“统一 ROB entry + commit packet + 兼容桥接”三层结构**。它既不是完整 backend，也不能只是当前 `PendingPtrDriver` 的增量打补丁。只有这样，测试环境才能在不破坏现有回归的前提下，逐步逼近真实后端与 MemBlock 的交互语义。

## 9. 详细设计与验证计划

本节给出下一代 ROB 建模的详细设计与验证计划。目标不是把所有工作一次性做完，而是把结构、数据流、接口、兼容策略、分阶段落地方式和验证方案写清楚，让实现者可以按图施工。

### 9.1 总体设计原则

详细设计遵循四条原则。

第一，**不重建完整 backend，只重建对 MemBlock 验证必须真实的 ROB-LSQ 交互**。这意味着：

- 不实现真实 rename/dispatch/scheduler
- 不实现非 mem 指令执行细节
- 但要实现能约束 mem commit frontier 的最小非 mem 占位语义

第二，**优先保留现有 MemoryModel/Scoreboard 的主判定逻辑**。当前环境最稳定的部分是：

- load commit-boundary compare
- store deferred visibility
- flush/drain consistency

ROB 模型升级应该服务于这些判定更真实，而不是替换它们。

第三，**统一真值来源**。今后的程序序与提交真值应尽量集中在 ROB model 中产生，而不是由多个 helper 各自维护一部分事实。

第四，**分阶段启用更真实语义**。先做驱动，再做一致性检查，再做复杂场景覆盖，避免大爆炸式重构。

### 9.2 测试侧 ROB entry 模型设计

建议引入一个测试侧 `RobEntry` 数据结构，用于统一表示当前测试中需要进入 ROB 提交模型的条目。该结构最少需要这些字段：

- `rob_idx_flag`
- `rob_idx_value`
- `kind`：`load` / `store` / `non_mem`
- `source_id`：可选，对接 req_id 或测试序列内局部编号
- `sq_idx` / `lq_idx`：如适用
- `allocated`
- `exec_completed`
- `commit_ready`
- `cancelled`
- `exception_like`
- `mmio`
- `nc`
- `note`

这里的关键不是字段多，而是区分两个概念：

1. **执行完成**
   - 对 load，可能表示写回已观测；
   - 对 store，可能表示 STA/STD/shadow 已齐；
   - 对 non-mem，占位场景下由测试显式指定。
2. **可提交**
   - 不仅依赖执行完成，还依赖是否被 cancel、是否被 redirect kill、是否存在异常阻塞等。

当前 `PendingPtrDriver` 把 load 的推进条件近似为“完成了”，store 的推进条件近似为“scommit 预算到了”。而未来模型应明确：真正决定是否能进入当前拍 commit packet 的，是 entry 状态，而不是某个 helper 的局部事件。

`RobEntry` 层还需要支持 non-mem placeholder。该占位不要求携带真实 backend 结果，但必须能表达：

- 它在程序序上确实存在；
- 当前是否允许提交；
- 它是否会阻塞 younger mem op。

这是未来覆盖 non-mem head blocking 的最小抽象。

### 9.3 ROB 队列与提交前沿

在 `RobEntry` 之上，测试侧需要维护一个统一 `RobModel`：

- `entries`: 按程序序排列的 ROB entry 队列
- `head_ptr`: 当前提交前沿
- `tail_ptr`: 仅用于调试或完整性检查，可选
- `issued_map`: 便于通过 `robIdx` 查 entry
- `completed_map`: 便于更新执行完成状态

这个模型的核心职责是：在每个测试拍或每次 sequence 驱动后，生成一个 **commit packet**。它不直接做 load compare，也不直接改动 MemoryModel，而是负责告诉环境：

- 这一拍 ROB 应向 MemBlock 输出什么

在实现上，可以允许 sequence 通过 facade 做几类动作：

- `env.backend.note_load_issued(rob_idx, lq_idx, ...)`
- `env.backend.note_store_allocated(rob_idx, sq_idx, ...)`
- `insert_non_mem_placeholder(rob_idx, can_commit=False, note=...)`
- `mark_entry_exec_completed(rob_idx)`
- `mark_entry_cancelled(rob_idx)`
- `queue_commit_window(max_entries=...)`

这里最重要的是：测试侧不再直接思考“要不要 pulse 一次 `scommit`”，而是通过 ROB model 声明程序序和 entry 状态，再由 ROB model 决定本拍 `scommit/lcommit/commit/pendingPtr/pendingPtrNext` 应该是什么。

### 9.4 commit packet 设计

建议引入一个显式 `RobCommitPacket` 结构，字段至少包括：

- `commit`: bool
- `lcommit`: int
- `scommit`: int
- `pending_ptr_before`: RobIndex
- `pending_ptr_after`: RobIndex
- `pending_ptr_next`: RobIndex
- `committed_entries`: list[RobEntry]

这里有两个容易混淆的点需要明确。

第一，`pending_ptr_before` 与 `pending_ptr_after` 是测试侧推导值，分别对应：

- 本拍驱动 DUT 前看到的 current frontier
- 本拍提交消费后的 frontier

第二，`pendingPtrNext` 在测试侧可以先采用一种“最小但自洽”的近似：

- 若本拍有 commit，则 `pendingPtrNext = pending_ptr_after`
- 若未来要更逼近真实后端，再扩展为“下一提交包预测边界”

在 v1 详细设计中，这种定义虽然未必完全等价于真实 backend，但至少比“完全不驱动”强，而且可验证。

commit packet 的推导原则应该是：

1. 从当前 head 开始顺序扫描 `RobEntry`
2. 找到在当前拍满足提交条件的连续前缀
3. 按 entry 类型统计 `lcommit/scommit`
4. 形成 `commit` 布尔
5. 生成 `pendingPtr/pendingPtrNext`

这里要特别强调“连续前缀”概念，因为真实 ROB 提交本质上是顺序提交。即便第 5 条 entry 已完成，只要第 3 条 non-mem placeholder 还不可提交，第 5 条就不能进入 commit packet。

### 9.5 `pendingPtr` 与 `pendingPtrNext` 推导逻辑

当前环境只有 `pendingPtr`。下一代模型应把两者都纳入统一推导。

建议 v1 定义如下：

- `pendingPtr`：本拍 commit 之前，ROB 当前 head
- `pendingPtrNext`：若本拍 commit packet 被接受后，下一拍起始的 head

若本拍没有 commit，则：

- `pendingPtr == pendingPtrNext`

若本拍提交了 N 条连续 entry，则：

- `pendingPtrNext = pendingPtr + N`

这样的定义虽然比真实 backend 内部细节更粗，但在测试侧具有三个优点：

1. 它可预测、可验证、容易调试
2. 它直接服务于 LSQ/StoreQueue/StoreMisalignBuffer 等“当前拍/下一拍边界”逻辑
3. 它能自然兼容 commit packet

未来若发现 DUT 某些路径对 `pendingPtrNext` 的语义要求更特殊，再逐步细化，而不是一开始就试图复刻所有 backend 内部时序。

### 9.6 `lcommit`、`scommit` 与当前 `lqDeq` 的关系

这是详细设计中最敏感的部分。因为当前 Scoreboard 的 load compare 预算主要来自 `lqDeq`，如果粗暴改成测试侧 `lcommit`，极易破坏已有稳定用例。

因此建议分两步。

第一步，**引入 `lcommit` 驱动，但不立即替代 `lqDeq` 作为 compare 主来源**。也就是说：

- DUT 前向输入看到更完整的 `lcommit`
- Scoreboard 仍主要依据 `lqDeq` 进行 compare budget 推进

第二步，新增一致性检查：

- 统计测试侧发给 DUT 的 `lcommit`
- 统计 DUT 最终导出的 `lqDeq`
- 在 directed 场景下检查二者是否满足基本一致性约束

例如：

- 对无 replay、无异常、无 redirect 的普通 load burst，测试侧 `lcommit` 与 DUT `lqDeq` 的总量应收敛一致；
- 若存在 replay 或 younger/older 复杂窗口，则可以先只检查总量和单调性，不要求拍级完全重合。

这样做的原因是：`lqDeq` 已经深度嵌入当前 compare 体系，而 `lcommit` 是 ROB 输入模型升级的一部分。让它们先并行存在，是最稳妥的演进策略。

### 9.7 Store committed 与 flush/drain 的对接

store path 当前的关键优点是已经有：

- store shadow
- committed/materialized 观察
- flush/drain 收尾一致性检查

新 ROB model 不应破坏这条链路，而应强化其前向真实性。

具体策略应为：

1. store 仍由 `StoreTxn -> enqueue -> STD/STA -> shadow` 建立内部状态
2. ROB model 负责决定该 store 何时真正进入当前拍 commit packet
3. `scommit` 从 commit packet 里导出，而不是 testcase 手工 pulse
4. store shadow 中的 `committed` 仍然作为 DUT 侧被观测事实
5. 最终 flush/drain 一致性校验保持不变

这会带来一个非常重要的收益：今后“store committed”不再是 sequence 中局部时序技巧，而是 ROB model 的正式输出结果。这样对于：

- 双 store same-addr overwrite
- store burst
- mixed ld/st
- NC/MMIO store

都能使用统一解释。

### 9.8 non-mem placeholder 的详细设计

non-mem placeholder 不需要模拟真实 ALU 指令，只需提供最少状态：

- `robIdx`
- `can_commit`
- `completed`
- `cancelled`
- `kind=non_mem`

sequence 层可通过 facade 插入它，例如：

- `insert_non_mem_placeholder(rob_idx=11, can_commit=False, note="alu blocker")`

随后，在某个阶段再显式释放：

- `mark_non_mem_committable(rob_idx=11)`

这套设计的价值在于：它能以极低复杂度引入“真实 ROB 中 mem 与非 mem 共处”的程序序约束，而不必真的建 ALU pipeline。

典型测试就可以变成：

1. 发 older store
2. 插入 non-mem blocker
3. 发 younger load
4. 验证 younger load 虽可能写回，但不能越过 placeholder 形成最终 commit compare

这种场景对 MemBlock 验证非常关键，因为它能区分：

- “执行完成”
- “达到提交边界”

而这正是很多 replay/ordering 问题的根源。

### 9.9 redirect / flush / cancel 的详细设计

redirect/flush/cancel 是当前模型最薄弱、也是未来复杂场景最关键的部分。详细设计建议采用“事件注入 + entry 状态变更”的方式，而不是一开始就精确复制 LSQWrapper 所有计数逻辑。

最小设计如下：

- ROB model 接受 `redirect_event` 或 `cancel_after(rob_idx)` 之类的输入
- 事件到来后：
  - 标记 younger entries 为 `cancelled`
  - 停止它们进入 future commit packet
  - 必要时记录 cancel count 供一致性检查

这样做的好处是：

1. 可以首先覆盖“younger 不应继续 commit”这一核心语义
2. 不必第一步就重建所有 free-count 算法
3. 能与现有 replay/redirect 测试逐步结合

随后再二期细化：

- 把 cancel count 与 `sqDeq/lqDeqPtr` 反馈、一致性断言结合起来
- 对应到 `LSQWrapper` 中 lq/sq counter 更新逻辑

### 9.10 backend feedback 半模型设计

这一部分建议放在后续阶段，但文档里必须先定下边界。

v1 不要求完整 dispatch credit model，但至少应增加两类检查：

1. **一致性检查**
   - `lqDeq/sqDeq` 是否与测试侧 commit packet 统计基本一致
   - `lqDeqPtr/sqDeqPtr` 是否单调并与 deq 数量匹配

2. **半模型驱动**
   - 对少量 near-full directed case，引入一个极简 credit tracker
   - 用于决定测试侧是否继续允许高层 sequence 发新请求

这样既不会把环境一步变成 backend 仿真器，又能让一些关键资源反馈路径进入验证视野。

### 9.11 兼容性与迁移计划

迁移不能一次性重写所有 sequence/test。建议如下：

#### Phase 1：接口补全与双轨兼容

- 新增 `RobModel` 与 `RobCommitPacket`
- 驱动完整五元组
- 现有 `ScalarLoadSequence` / `ScalarStoreSequence` 不改语义，只改其底层 commit driver 来源
- `lqDeq` 仍是 compare 主预算来源
- 增加 `lcommit` 与 `lqDeq` 的一致性日志/断言

验收目标：

- 现有 scalar load/store/random/replay 回归基本不变
- store committed 仍能收敛
- `pendingPtrNext` 有定义且不再悬空

#### Phase 2：统一 ROB 队列与 non-mem placeholder

- 用统一 `RobEntry` 队列替换当前 mem-only `_issued`
- 支持插入 non-mem blocker
- 支持 commit packet 下的 mixed commit
- 为 directed ordering tests 增加 non-mem 阻塞场景

验收目标：

- 能覆盖“non-mem 头阻塞 younger mem commit”
- 能覆盖“同拍 load/store mixed commit”基本窗口

#### Phase 3：redirect/flush/cancel 与 backend feedback 半模型

- 支持 entry cancel
- 引入 redirect/flush 驱动后的 commit frontier 修正
- 为 `sqDeq/lqDeqPtr` 建立一致性检查与简易 credit model
- 将复杂 replay/ordering 用例迁移到更真实 ROB model 下

验收目标：

- replay/redirect 类场景不再只验证 replay event，还能验证 commit frontier 恢复
- near-full / deq feedback 场景具备 directed coverage

### 9.12 新模型的验证计划

新 ROB model 的验证必须分成两层：模型自测与 MemBlock 集成回归。

#### 9.12.1 模型自测

先验证 ROB model 自己的正确性，避免一上来就把所有问题混到 DUT 行为里。建议包括：

1. **单条 load**
   - issue -> complete -> commit packet
   - 检查 `lcommit=1, scommit=0, pendingPtrNext=+1`

2. **单条 store**
   - allocate -> scommit -> commit packet
   - 检查 `lcommit=0, scommit=1`

3. **双 store 连续递增 rob**
   - 分拍提交两条 store
   - 检查 frontier 正确前移

4. **load/store mixed commit packet**
   - 同拍提交 1 load + 1 store
   - 检查统计与 pointer 推导

5. **non-mem blocker**
   - younger mem ready，但 head placeholder 未放行
   - 检查 commit packet 为空

6. **cancel/redirect**
   - younger entries 被 cancel 后不再进入 future commit packet

这些自测不必驱动真实 DUT，可作为 commit driver/rob model 单元测试。

#### 9.12.2 MemBlock 集成回归

在模型自测稳定后，再接到真实 DUT 上，重点覆盖：

1. **现有基础回归不退化**
   - random load
   - random store
   - scalar load/store pipeline
   - replay smoke

2. **双 store same-addr overwrite**
   - 递增 `robIdx`
   - younger load 在线 compare 应看到第二条值

3. **non-mem blocker 场景**
   - older store / non-mem placeholder / younger load
   - younger load 不应过早 compare

4. **mixed commit 场景**
   - load/store 混合同拍或近拍提交
   - 对 `lqDeq` 与测试侧 `lcommit` 做一致性检查

5. **MMIO busy / NC**
   - older MMIO/NC + younger cacheable
   - 检查 busy 与 commit frontier 的配合

6. **redirect/replay**
   - RAW/RAR replay 后 younger entries cancel
   - 检查 frontier 与 compare 不错误前移

#### 9.12.3 验收标准

新的 ROB 建模完成后，验收标准不应只看“测试都跑过”，而应明确包括：

1. `RobLsqIO` 五元组在测试环境中全部有定义且驱动稳定
2. 现有 MemoryModel/Scoreboard 主判定逻辑不被破坏
3. 基础 scalar ld/st regression 全通过
4. 至少新增一批 directed case，覆盖：
   - non-mem 阻塞
   - mixed commit
   - pendingPtrNext
   - redirect/cancel
5. `lcommit/scommit/commit/pendingPtr/pendingPtrNext` 与 DUT 导出状态之间具备至少基础一致性断言

## 10. 结论

当前测试环境中的 ROB 建模，相比早期版本已经迈出了关键一步：它不再只靠 load 完成推进 `pendingPtr`，而是已经能让 store 进入程序序 frontier，足以支撑现阶段一批真实 DUT 的标量 ld/st directed 用例。这一点是有价值的，也不应被低估。

但如果问题是“它是否与真实后端实现一致”，答案仍然是否定的。当前模型本质上仍是一个：

- `mem-only`
- `partial RobLsqIO`
- `load/store 语义分裂`
- `无 non-mem、无 pendingPtrNext、无 commit bool、无 redirect/cancel`

的代理模型。它已经足够支撑当前基础 MemBlock 功能验证，却不足以覆盖所有后端 ROB 与 MemBlock 的交互场景。

因此，对这套模型最合理的评价不是“已经够了”或“完全不行”，而是：

- 对基础标量 ld/st smoke、定向 same-addr/mixed、部分 replay case：**阶段性足够**
- 对更真实的 backend 程序序、mixed commit、non-mem 阻塞、MMIO busy、redirect/cancel：**明显不足**

下一阶段最值得优先补的三件事是：

1. 完整驱动 `RobLsqIO` 五元组
2. 建立统一的测试侧 ROB 队列与 commit packet
3. 引入 non-mem placeholder，让 MemBlock 验证首次具备“真实 ROB 阻塞”表达能力

在此基础上，再逐步补：

- pendingPtrNext
- commit 布尔
- redirect/cancel
- backend feedback 半模型

这样推进，既不会破坏现有已经稳定的 MemoryModel/Scoreboard，又能让 MemBlock 验证从“面向 mem-only 的局部代理”逐渐演进到“对真实后端交互足够可信的 ROB-LSQ 验证模型”。
