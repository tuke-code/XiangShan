# MemBlock Backend 请求模型设计

## 1. 文档目的

本文档专门说明 `src/test/python/MemBlock/` 当前采用的新 backend 请求模型，也就是围绕 `env.backend.send(...)` 与 `env.backend.execute(...)` 展开的那套主动控制接口。它是对总设计文档 `verification_env_design.md` 中 backend facade 部分的展开说明，也是对 `README.md` 中“最小主动控制示例”的进一步补充。

如果把 `verification_env_design.md` 看成“验证环境为什么要分层”的总图，那么本文档回答的是更具体的问题：

1. 为什么原先零散增长的 `send_load_batch_same_cycle()`、`send_load_batch_with_sta_same_cycle()` 一类接口已经不适合作为后续扩展方向。
2. 为什么新的控制面要从“新增 helper 名字”转为“描述一段 backend 请求脚本”。
3. `LoadTxn`、`StoreTxn`、`IssueOp`、`IssueCyclePlan`、`BackendSendPlan`、`StoreRef` 分别承担什么职责，它们之间如何配合。
4. testcase、sequence、facade、issue agent 各自应该停留在什么抽象层级，避免再次把实现拖回到命令式、零散式、难维护的状态。

为了降低阅读跳转成本，建议把本文档与以下两份文档配套阅读：

- 环境总设计：`src/test/python/MemBlock/docs/verification_env_design.md`
- 项目入口与用法概览：`src/test/python/MemBlock/README.md`
- 常见脚本模板：`src/test/python/MemBlock/docs/backend_rob_cookbook.md`

> 2026-04-15 更新：`robIdx` 默认已改为由 env 在 `prepare()` / `execute()` / `send()` 流程中统一分配。本文档中较早期那些“直接手填 `rob_idx_flag/value`”的片段，现应优先理解为兼容层示意；新的 testcase/sequence 建议改用 `RobIndex`、`RobRef`、`wait_for_rob` 与 `prepare(...)`，只在最靠近 DUT 端口的边界再拆成 flag/value。`req_id` 现在只作为请求标识符，不再隐式推导 `robIdx/pdest/ftq/pc`；若需要边界场景，应显式使用 `set_next_rob_idx()` / `set_commit_frontier()` 之类的 seed 接口，而不是拿大 `req_id` 旁路编码。

## 2. 背景：旧接口为什么会越来越臃肿

在早期版本里，主动控制路径的主问题并不是“功能不够”，而是“扩展方式不收敛”。一开始只有单条 load/store，于是自然会写出：

- `send_load()`
- `send_store()`
- `issue_scalar_load()`
- `issue_scalar_sta()`
- `issue_scalar_std()`

这在单笔请求时代没有问题，因为“一个 helper 对应一种原子动作”，接口数量还处于人脑容易掌握的范围内。但一旦开始支持更复杂的场景，问题就会出现：

1. 需要同拍发多条 load，于是增加 `send_load_batch_same_cycle()`。
2. 需要多条 load 与一条 STA 同拍，于是增加 `send_load_batch_with_sta_same_cycle()`。
3. 如果后续还要支持 `load + std`、`sta + std`、两条 store 混发、先 enqueue 一批再分两拍 issue，那么继续沿着“每种组合都起一个新 helper 名字”的思路走下去，接口数量会指数式增长。

更关键的问题是，这些 helper 的区别并不体现在语义层级上，而只是体现在“内部到底替用户执行了哪几步动作、这些动作排成什么顺序”。也就是说，旧模型把“脚本内容”编码进了“函数名”。这种方式在场景少的时候看起来直观，但在场景多的时候会迅速失控：

- testcase 开发者记不住名字；
- facade 维护者必须不断新增近似 helper；
- issue agent 会被迫承载一堆为历史接口服务的特化路径；
- 任何新的组合场景都会推动公共接口继续膨胀。

因此，这次重构的核心并不是把旧 helper 改个名字，而是把扩展方向从“增加新名字”改成“描述一个统一脚本”。

## 3. 设计目标

新的 backend 请求模型同时追求四个目标：

1. **统一语义入口**
   单笔事务与复杂组合场景都通过 `env.backend` 进入，而不是散落到 `request_apis.py` 与 `IssueAgent` 的各种特化入口。

2. **把组合复杂度从接口名中移出来**
   场景复杂不再意味着要发明更多 helper 名字，而是意味着要写出更明确的 plan。

3. **保持旧 testcase 的兼容性**
   `request_apis.py` 里的旧名字继续保留一轮，内部转成新模型执行；这样已有 tests/sequences 不需要同一天全部迁移。

4. **让 backend/issue 从“动作库”变成“解释器”**
   `BackendFacade` 不再只暴露零散动作，而是能解释一段 ordered plan；`IssueAgent` 不再只会执行若干特化 helper，而是能执行一个通用的 `IssueCyclePlan`。

这四点合在一起，才形成了真正可扩展的请求模型。

## 4. 新模型的核心对象

新模型不是一个大而全的类，而是一组职责单一的事务/计划对象。

在继续展开对象细节之前，先明确一个经常会让人困惑的点：当前 `BackendSendPlan` 已经不只是“backend enqueue/issue 脚本”，它同时也是 **ROB 半模型的语义编排入口**。也就是说：

- backend lane 级动作仍然通过 `Enqueue*Step` / `IssueCyclePlan` 描述；
- ROB 侧阻塞/放行语义则通过独立的 ROB 语义步骤描述；
- testcase 不应该绕过 `env.backend` 直接去改 `env.rob_agent`。

这让“请求怎么发”和“ROB frontier 如何被约束”仍然能留在同一段顺序脚本里描述，便于验证程序序相关行为。

### 4.1 `LoadTxn` / `StoreTxn`

这两个对象仍然是最基础的“单笔事务”表示：

- `LoadTxn` 描述一条 load 的 enqueue 信息、issue 地址、LQ/SQ 指针、等待位、lane 等。
- `StoreTxn` 描述一条 store 的 enqueue 起始 SQ 指针、地址、数据、字节掩码以及 STA/STD lane 等。

其中 `StoreTxn.mask` 现在不再只是 scoreboard 侧的辅助字段，而是请求模型本身的一部分。当前环境把它收敛成“标量连续低位字节掩码”语义，支持：

- `0x01` -> `SB`
- `0x03` -> `SH`
- `0x0F` -> `SW`
- `0xFF` -> `SD`

也就是说，backend/request 层现在已经能够把 `StoreTxn.mask` 一路下沉到 issue `fuOpType`。如果地址本身带偏移，例如 `addr + 5` 配合 `mask=0x01`，DUT 最终观测到的 store mask 可能已经是移位后的窗口位置；但请求模型对外仍然保持“从 store 地址起始的连续字节宽度”这个稳定语义。

它们的价值在于：**让单笔场景仍然保持简单**。如果只是发一笔 load，就不应该要求 testcase 手工写 `EnqueueLoadStep + IssueCyclePlan` 这么长的一段脚本。因此：

- 单笔 load 推荐 `env.backend.send(load_txn)`
- 单笔 store 推荐 `env.backend.send(store_txn)`

也就是说，txn 是用户最先接触的对象，而 plan 是在组合场景下才需要显式使用的对象。

### 4.2 `EnqueueLoadStep` / `EnqueueLoadCyclePlan` / `EnqueueStoreStep`

这几个对象表示 backend 脚本中的“enqueue 阶段动作”。

- `EnqueueLoadStep` 表示把一条 load 放进 LSQ enqueue 口。
- `EnqueueLoadCyclePlan` 表示把多条 load 在同一拍一起送进不同的 LSQ enqueue port。
- `EnqueueStoreStep` 表示把一条 store 放进 store queue，并可选择把运行时分配到的实际 SQ pointer 绑定到某个 `StoreRef`。

这层对象的意义，是把“请求进入队列”与“请求在 issue lane 上发射”分开。对乱序访存流水线来说，这是必要的，因为 enqueue 和 issue 本来就是两个不同阶段。旧模型下很多 helper 把这两件事打包到一起，短期方便，长期却掩盖了时序结构。

### 4.3 `IssueOp`

`IssueOp` 表示“某个 issue lane 在某一拍要做什么”。当前支持三种 `kind`：

- `load`
- `sta`
- `std`

它比旧 helper 更清楚的一点在于：它把 issue 层的最小原子动作抽象成了统一形状。无论是 load 还是 store address/data，本质上都是“在某条 lane 上驱动一组字段并握手一拍”。以前这些差异分散在不同 helper 里，现在则统一收敛到 `IssueOp`。

对于 `sta/std`，`IssueOp` 还显式携带 `mask`。这使得同一个 store 的 address issue 与 data issue 能共享同一份宽度语义，`IssueAgent` 则根据该 `mask` 自动解码对应的标量 store `fuOpType`。因此，后续如果 testcase 需要写 partial-store 的拍级脚本，已经不需要再手工猜 DUT 的 `fuOpType` 编码。

这样做的直接收益是：

- testcase/sequence 可以显式描述“这一拍有哪些 lane 要发”；
- `IssueAgent` 只需要维护一个通用 issue-cycle 执行器；
- 新增同拍组合时，不需要再为每种排列组合增加一个 public API。

### 4.4 `IssueCyclePlan`

`IssueCyclePlan` 表示“一组 lane 唯一的 issue 批次”，并显式携带握手模式。这个对象是整个新模型的关键，因为它把“这批 op 想如何与 `ready` 交互”变成了显式数据，而不是隐含在 helper 名字里。

例如：

- 两条 load 同拍 issue，可以写成一个 `IssueCyclePlan(load0, load1)`。
- 两条 load 加一条 STA 同拍 issue，也只是把这三条 op 放到同一个 cycle plan 里。

当前有两种握手模式：

- `strict`
  - 默认模式。
  - 语义是“这批 op 必须在同一拍一起完成握手”；任何 lane 不 ready，整批都继续等待。
  - 适合 replay / bank-conflict 这类真的要证明“同拍组合”的场景。
- `elastic`
  - 语义是“这批 op 属于同一个 issue 批次，但每条 lane 可按自己的 `ready` 独立完成握手”。
  - 对 load 来说，env 会先 drive 一个周期，再在 post-step 相位观察该 lane 的接受结果；若该 lane 在该轮 drive 中被接受，就停止继续驱动。
  - 未握手 lane 会保持同一份请求，在后续拍继续驱动直到被接受。
  - 适合 saturation / throughput / backpressure 场景。

`IssueCyclePlan` 还做了一件很重要的事情：它在构造时检查 lane 唯一性。也就是说，同一批次里重复占用同一条 issue lane，不再等运行时偶然出错，而是在 plan 构建阶段就被拒绝。这比旧模型里把冲突逻辑藏在各种特化 helper 中更稳健，也更容易测试。

### 4.5 `StoreRef`

`StoreRef` 是新模型里最容易被忽视、但非常关键的对象。它解决的问题是：

> store 的实际 SQ pointer 往往要等 enqueue 完成后才能拿到，而后面的 `STA` / `STD` issue 又需要这个实际 pointer；那么在构造脚本时，如何引用一个“运行时才知道”的值？

答案就是 `StoreRef`。它是一个符号句柄，而不是最终值：

1. 在 `EnqueueStoreStep` 里声明“这条 store 的实际 SQ ptr 以后绑定到这个 `StoreRef`”；
2. 在后续 `IssueOp.sta()` / `IssueOp.std()` 里先引用这个 `StoreRef`；
3. `BackendFacade.execute()` 执行脚本时，再把它解析成真实的 `QueuePtr`。

这使得脚本可以同时保持：

- 编写阶段的可读性；
- 运行阶段的正确依赖解析；
- 对 enqueue/issue 分阶段结构的忠实表达。

### 4.6 `BackendSendPlan` / `BackendSendResult`

`BackendSendPlan` 是顶层脚本容器，表示“按顺序执行的一组 backend 步骤”。当前步骤可以是：

- enqueue load
- enqueue store
- issue 某一拍的一组 op
- commit pulse
- 插入 / release 一条 ROB-side non-mem blocker
- 显式设置某条 store 的 ROB commit readiness

`BackendSendResult` 则负责承载脚本执行过程中生成的运行时结果，例如 `StoreRef -> QueuePtr` 的映射。它的作用不是暴露一大堆状态，而是只把脚本后续可能需要的运行时产物保存下来。

其中后两类步骤的设计目的，是把“ROB 语义编排”继续收敛在同一个 backend 脚本模型中：调用者仍然描述一段顺序脚本，`BackendFacade` 负责解释这段脚本并把它翻译到 `CommitAgent/RobAgent`，而不是让 testcase 直接去修改 `rob_agent` 内部状态。

从抽象层级看：

- `IssueOp` 是“lane 级动作”；
- `IssueCyclePlan` 是“单个 issue 批次”；
- `BackendSendPlan` 是“多拍顺序脚本”。

这三个层级构成了新的请求模型骨架。

### 4.7 当前 ROB 半模型与 non-mem op 的语义

当前环境中的 `RobAgent` 已经不再是最早期的 mem-only token proxy，而是一个 **ROB-aware 半模型**。对使用者而言，最重要的是理解它当前支持什么、又明确不支持什么。

当前 ROB entry 类型有三类：

- `load`
  - 由 `note_load_issued()` 或 load issue 流程登记
  - 只有在 `exec_completed=True` 后，才允许沿 ROB frontier 提交
- `store`
  - 由 `note_store_allocated()` 或 store enqueue 流程登记
  - 进入 commit packet 的条件是：`store token` 可用，且该 entry 自身 `commit_ready=True`
  - 默认情况下，`STA` / `STD` 对应的 addr/data ready 会在 `IssueCyclePlan` 执行时自动同步到 ROB 半模型
  - 如 testcase 需要显式控制 ready 节点，也可以使用 `StoreCommitReadyStep`
- `non_mem`
  - 这是一个 **ROB 程序序占位项**，不是 `IssueOp`
  - 它不模拟真实 ALU/FPU/backend 执行，只表达“这里存在一条 older non-mem op，目前是否允许提交”
  - 当它位于 ROB head 或连续前缀内且尚未 release 时，younger load/store 都不得越过它进入 commit frontier

这里要特别强调：当前文档里的 “non-mem op” 在实现上是一个 **non-mem blocker placeholder**，用于表达 ROB 顺序约束，而不是一条真的会去驱动 issue lane 的非访存指令。因此：

- 不要把它建成 `IssueOp(kind=...)`
- 不要在 testcase 里试图给它补一堆 backend 执行细节
- 正确做法是通过 `NonMemBlockerStep` 或 `env.backend.insert_non_mem_blocker(...)` / `release_non_mem_blocker(...)` 表达它

当前半模型仍然明确 **不负责**：

- redirect / cancel / flush 对 ROB frontier 的恢复与重建
- backend feedback / credit 闭环
- 完整复刻真实 backend 的所有 non-mem 执行来源

因此它的定位依然是：**足够真实地表达 MemBlock 所关心的 ROB 程序序阻塞与 store commit 边界，而不是重建整个 backend。**

## 5. `send()` 与 `execute()` 的分工

新模型最终收敛成两个推荐入口：

### 5.1 `env.backend.send(request)`

适用于两类场景：

1. 单笔 `LoadTxn`
2. 单笔 `StoreTxn`

它代表“按标准发送语义执行这一笔请求”。例如对一条 store，`send()` 会自动完成：

1. enqueue store
2. 发 `STD`
3. 发 `STA`

因此，对于大多数 primitive 场景来说，`send()` 是最简洁、也最推荐的入口。

### 5.2 `env.backend.execute(plan)`

适用于所有“你需要自己决定发送脚本”的场景。例如：

- 多条 load 同拍
- load 与 STA 混合 issue
- 先 enqueue 一批请求，再在后续多拍按某种顺序 issue
- 需要在脚本中间插入 commit pulse
- 需要显式插入 / release non-mem blocker
- 需要在脚本中显式改变某条 store 的 commit readiness

`execute()` 的设计理念是：**facade 不替你猜复杂意图，而是解释你明确给出的意图**。这能避免 helper 风格接口常见的一个问题：调用者以为 helper 做的是 A+B+C，维护者后来为了新场景把它扩成 A+B+D+E，最后 nobody can remember the contract。

## 6. 典型使用方式

在阅读下面这些示例之前，先强调一个原则：**新请求模型并不是要把所有 testcase 都降级成手写 plan**。对于已经能被高层 sequence 良好表达的场景，仍然应该优先写 sequence；`env.backend.send(...)` 与 `env.backend.execute(...)` 更适合以下三类情况：

1. 编写新的 primitive 场景；
2. 为 sequence 探路，先验证某种发送脚本是否可行；
3. debug 时需要最短路径复现某个 backend/issue 组合。

### 6.1 单笔 load

```python
txn = LoadTxn(req_id=req_id, addr=addr, lq_ptr=lq_ptr, sq_ptr=sq_ptr)
env.backend.send(txn)
```

这条路径最适合 primitive testcase，也最接近历史使用习惯。

### 6.2 单笔 store

```python
txn = StoreTxn(req_id=req_id, sq_ptr=sq_ptr, addr=addr, data=data)
allocated_sq_ptr = env.backend.send(txn)
```

这里 `send()` 返回实际分配到的 SQ pointer，供外部需要时继续使用。

如果需要表达 partial-store，可以直接在 `StoreTxn.mask` 上描述标量宽度：

```python
txn = StoreTxn(
    req_id=req_id,
    sq_ptr=sq_ptr,
    addr=addr,
    data=0xA1A2_A3A4,
    mask=0x0F,
)
allocated_sq_ptr = env.backend.send(txn)
```

上面这条请求会被 backend 自动翻译成 `SW` 宽度的 `STD + STA` 组合，而不是旧版本里那种固定 `SD` 的 issue 方式。

### 6.3 多条 load 同拍

```python
env.backend.execute(
    BackendSendPlan.from_steps(
        EnqueueLoadCyclePlan.from_txns(load0, load1),
        IssueCyclePlan.from_ops(
            IssueOp.load_from_txn(load0),
            IssueOp.load_from_txn(load1),
        ),
    )
)
```

这个例子说明：所谓“多 load 同拍”不再需要专门的 helper 名字，本质上只是“一个同拍 enqueue cycle + 一个同拍 issue cycle”。

### 6.4 store enqueue 后复用实际 SQ pointer

```python
store_ref = StoreRef("older_store")
result = env.backend.execute(
    BackendSendPlan.from_steps(
        EnqueueStoreStep.from_txn(store_txn, ref=store_ref),
        IssueCyclePlan.from_ops(
            IssueOp.std(
                req_id=store_txn.req_id,
                sq_ptr=store_ref,
                data=store_txn.data,
                mask=store_txn.mask,
            ),
        ),
        IssueCyclePlan.from_ops(
            IssueOp.sta(
                req_id=store_txn.req_id,
                sq_ptr=store_ref,
                addr=store_txn.addr,
                mask=store_txn.mask,
            ),
        ),
    )
)
real_sq_ptr = result.resolve_sq_ptr(store_ref)
```

这个例子体现了新模型处理“运行时分配值”的能力，也是旧 helper 很难优雅表达的部分。

### 6.5 插入 non-mem blocker，阻塞 younger mem commit

当你需要表达“older non-mem op 卡在 ROB head，younger mem 不能提交”时，应把它当作 ROB 语义步骤写进同一个 `BackendSendPlan`，而不是把它当作 issue 动作：

```python
from transactions import (
    BackendSendPlan,
    EnqueueLoadStep,
    EnqueueStoreStep,
    IssueCyclePlan,
    IssueOp,
    NonMemBlockerStep,
    RobIndex,
    StoreCommitStep,
    StoreRef,
)

younger_store = StoreRef("younger_store")

env.backend.execute(
    BackendSendPlan.from_steps(
        EnqueueLoadStep.from_txn(load0),
        NonMemBlockerStep.insert(rob_idx=RobIndex(flag=0, value=1)),
        EnqueueStoreStep.from_txn(store1, ref=younger_store),
        IssueCyclePlan.from_ops(IssueOp.load_from_txn(load0)),
        IssueCyclePlan.from_ops(
            IssueOp.std(req_id=store1.req_id, sq_ptr=younger_store, data=store1.data, mask=store1.mask)
        ),
        IssueCyclePlan.from_ops(
            IssueOp.sta(req_id=store1.req_id, sq_ptr=younger_store, addr=store1.addr, mask=store1.mask)
        ),
        StoreCommitStep(count=1),
        NonMemBlockerStep.release(rob_idx=RobIndex(flag=0, value=1)),
        StoreCommitStep(count=1),
    )
)
```

这个脚本表达的是：

1. older load 可以先完成并提交到 blocker 前；
2. 中间 non-mem blocker 未 release 时，younger store 即使 addr/data 都 ready，也不应提交；
3. release blocker 后，再给一拍 `StoreCommitStep`，younger store 才能进入 commit frontier。

### 6.6 显式控制 store commit readiness

对大多数 scalar store 来说，`STD + STA` 对应的 data/address ready 会自动同步到 ROB 半模型，因此 testcase 通常不需要手工再写 ready 步骤。

但如果你要做更细粒度的 ROB 边界实验，想把“store 已 enqueue / 已 issue”与“ROB 允许提交”拆开，推荐显式写 `StoreCommitReadyStep`：

```python
from transactions import (
    BackendSendPlan,
    EnqueueStoreStep,
    IssueCyclePlan,
    IssueOp,
    StoreCommitReadyStep,
    StoreCommitStep,
    StoreRef,
)

store_ref = StoreRef("blocked_store")

env.backend.execute(
    BackendSendPlan.from_steps(
        EnqueueStoreStep.from_txn(store_txn, ref=store_ref),
        IssueCyclePlan.from_ops(
            IssueOp.std(req_id=store_txn.req_id, sq_ptr=store_ref, data=store_txn.data, mask=store_txn.mask)
        ),
        IssueCyclePlan.from_ops(
            IssueOp.sta(req_id=store_txn.req_id, sq_ptr=store_ref, addr=store_txn.addr, mask=store_txn.mask)
        ),
        StoreCommitReadyStep(sq_ptr=store_ref, ready=False),
        StoreCommitStep(count=1),
        StoreCommitReadyStep(sq_ptr=store_ref, ready=True),
        StoreCommitStep(count=1),
    )
)
```

这个例子适合用来验证：

- token 有了但 store entry 还不该提交；
- ready 状态切换后，ROB frontier 才继续前推。

如果你的目标只是普通 scalar store 主路径，请不要滥用这个接口；默认的 `send(store_txn)` 或 sequence 会更简洁。

### 6.7 对应的 sequence 风格：单笔 load

如果当前目标只是“reset 环境 -> 发送一笔 load -> compare -> drain”，那么 sequence 风格依然更推荐。它把 reset、期望登记、写回收敛这些标准步骤一起封装掉，避免 testcase 反复复制样板代码。

```python
from sequences.memblock_sequences import ResetEnvSequence, ScalarLoadSequence
from transactions import LoadTxn

state = ResetEnvSequence(require_issue_lanes=(0,), require_lq_ready=True).run(env)
env.preload_u64(0x9000_0000, 0x1122334455667788)

load_result = ScalarLoadSequence(
    LoadTxn(
        req_id=0x21,
        addr=0x9000_0000,
        lq_ptr=state.next_lq_ptr,
        sq_ptr=state.sq_ptr,
    ),
    expected_completed_loads=1,
    assert_no_outstanding=True,
).run(env)
```

这里的 `ResetEnvSequence` 默认还会把 allocator 与 commit frontier 一起 seed 到 wrap 边界前一项，使普通 real-DUT 回归天然覆盖一次 ROB wrap。若某个 env/unit 场景必须从 `(0,0)` 起步，应显式传 `seed_wrap_boundary=False`。

这一层和 `env.backend.send(load_txn)` 的关系可以理解为：

- `send(load_txn)` 负责“怎么发”
- `ScalarLoadSequence` 负责“按验证环境约定，发完之后还要做什么”

也就是说，sequence 并没有绕开新请求模型；它只是把新模型再封成更适合 testcase 复用的业务模板。

### 6.8 对应的 sequence 风格：单笔 store

对 store 也一样。如果你的目标不是研究 enqueue/STA/STD 的分拍结构，而只是要完成“发一笔 store，并确认它 materialize/committed”的业务语义，那么优先用 sequence：

```python
from sequences.memblock_sequences import ResetEnvSequence, ScalarStoreCommitSequence
from transactions import StoreTxn

state = ResetEnvSequence(require_issue_lanes=(3, 5), require_sq_ready=True).run(env)

store_result = ScalarStoreCommitSequence(
    StoreTxn(
        req_id=0x31,
        sq_ptr=state.sq_ptr,
        addr=0x9000_1000,
        data=0x1122334455667788,
    ),
    require_committed=True,
).run(env)
```

这里 sequence 内部仍然会走 `send_store()`，而 `send_store()` 又已经下沉到 `env.backend.send(store_txn)`。因此，新的 backend 请求模型并不会削弱 sequence，反而让 sequence 的底座变得更统一。

### 6.9 如何理解 sequence 与 plan 的边界

很多时候，开发者会犹豫“这个场景到底该直接写 `BackendSendPlan`，还是应该抽成 sequence”。经验上可以按下面这个标准判断：

1. 如果你关心的是“backend 这一拍到底发了哪些 lane、顺序怎么排”，优先写 plan。
2. 如果你关心的是“一个测试场景从 reset 到收敛应该怎么组织”，优先写 sequence。
3. 如果一个脚本模式已经在多个 testcase 中重复出现两次以上，就应该考虑把它上提成 sequence。

例如，“两条 load 同拍 issue”本身属于 backend 脚本结构，plan 很合适；但“构造两条 load 同拍 issue，然后等待 replay event，再检查最终 writeback 和 outstanding 清空”则已经进入 testcase 场景模板的范畴，更适合 sequence。

### 6.10 一个推荐的工作流

结合当前环境，比较稳妥的演进方式通常是：

1. 先用 `env.backend.execute(...)` 写出最短可运行脚本，确认 DUT 路径可达；
2. 再把脚本外侧那些固定流程，例如 reset、expect、drain、materialize、commit、统计检查，收敛成 sequence；
3. 最后让 testcase 尽量只消费 sequence result，而不是反复自己拼 backend 细节。

这个工作流的好处是：

- 探路时足够快；
- 收口时层次清楚；
- 不会把 testcase 永久锁死在 primitive 脚本层。

## 7. 为什么说这是从 backend/issue 角度重构了接口

这次重构的一个重要视角是：接口不再围绕“测试作者想偷懒调用什么 helper”来命名，而是围绕 backend/issue 自身真实存在的结构来设计。

从 backend/issue 的角度，真正存在的实体是：

1. enqueue 某条请求；
2. 在某一拍的若干 lane 上发起 issue；
3. 在必要时插入 ROB 语义步骤，例如 non-mem blocker / readiness 变化；
4. 在必要时插入 commit 推进。

换句话说，硬件并不知道什么叫“send_load_batch_with_sta_same_cycle”这种 API 名字；硬件只知道某拍哪些 lane 发了什么、某笔 store 何时 enqueue、何时拿到 data/address。新的请求模型正是按这个真实结构来建模，因此更稳定，也更贴近 DUT。

这也是为什么本次重构不把复杂度藏起来，而是允许用户在需要时显式写 plan。显式 plan 的价值不只是“灵活”，更在于它让验证代码和硬件行为之间的映射关系变得直接、可读、可审查。

## 8. 对 `request_apis.py` 和已有 testcase 的影响

为了平滑迁移，旧接口并没有立刻删除。当前策略是：

1. `request_apis.py` 仍保留旧名字；
2. 但旧名字内部已经不再拥有独立实现，而是把请求翻译成新的 `send()` / `execute()` 模型；
3. 公共数据模型统一收敛到 `transactions.py`；
4. 新增复杂场景时，不再继续往 `request_apis.py` 里堆新的特化 helper，而是优先上提到 `sequences/`。

这意味着：

- 旧 testcase 仍然能跑；
- 新 testcase 可以直接消费 `transactions.py` + `sequences/`，不必背历史兼容层包袱；
- 后续如果要做接口收敛，可以在迁移完成后有计划地削减兼容层，而不是继续让兼容层扩张。

## 9. 扩展规则：以后应该怎么继续演进

为了避免几年后再次回到“helper 名字爆炸”的状态，后续扩展建议遵循以下规则：

1. 如果只是单笔标准 load/store，优先继续使用 `LoadTxn` / `StoreTxn` + `env.backend.send(...)`。
2. 如果问题本质是“多步脚本如何排列”，优先新增 plan/step/op 的表达能力，而不是新增 facade 名字。
3. 如果只是某类场景在 tests/sequences 层出现频率很高，应优先抽 sequence，而不是把 sequence 语义塞回 backend facade。
4. 如果某个扩展需要直接碰 DUT 新白盒字段，应优先补 `IssueOp` 所需字段或 agent 驱动逻辑，而不是在 testcase 里临时写私有 pin-level 驱动。

一个简单判断标准是：

> 你要增加的是“新的硬件动作种类”，还是“旧动作的新排列方式”？

- 如果是新的动作种类，才考虑扩展数据模型。
- 如果只是排列方式变化，就应该复用 `BackendSendPlan` / `IssueCyclePlan`。

## 10. 与环境总设计文档的关系

本文档不是独立于环境架构之外的局部技巧，而是 `verification_env_design.md` 中“统一时钟内核 + backend facade + issue agent + request_apis 薄封装”这一设计路线的自然结果。

更具体地说：

- `verification_env_design.md` 解释“为什么主动控制要统一收口到 `env.backend`”；
- 本文档解释“`env.backend` 收口之后，内部推荐用什么模型表达复杂发送脚本，以及 `StoreTxn.mask` 如何映射到真实标量 store 宽度”；
- `README.md` 则给出最短上手入口，告诉开发者该从哪种用法开始。

因此三份文档的定位分别是：

- `README.md`：入口和索引
- `verification_env_design.md`：全局分层设计
- `backend_request_model_design.md`：backend 主动控制模型的专项说明

## 11. 小结

新的 backend 请求模型，本质上是一次从“helper 名字驱动”到“脚本结构驱动”的转向。它并没有让简单场景变复杂：单笔 load/store 仍然可以直接 `send()`。真正变化的是复杂场景的扩展方式——以后不再通过堆更多名字来表达差异，而是通过显式计划对象，把 enqueue、issue、同拍约束、运行时 SQ pointer 解析等关系写清楚。

这套模型的长期价值主要体现在三点：

1. **接口收敛**
   公共入口数量不再随着场景组合数线性膨胀。

2. **实现稳定**
   `BackendFacade` 与 `IssueAgent` 从特化 helper 集合，转成更容易测试和维护的通用解释器。

3. **场景可读**
   testcase/sequence 中写出的 plan，更接近实际 backend 时序结构，更便于做白盒分析和长期维护。

后续如果再增加新的 mixed traffic 场景、需要更复杂的 replay 组合、或要把 commit/pulse 进一步纳入统一脚本，应该继续沿着当前模型扩展，而不是回退到“每出现一种新组合就新增一个 helper 名字”的旧路。
