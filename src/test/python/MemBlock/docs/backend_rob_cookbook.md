# Backend / ROB Cookbook

## 1. 文档目的

本文档不是解释对象设计背景，而是给 testcase / sequence 作者一份可直接照着改的 cookbook。重点回答三个问题：

1. 什么时候继续用 `env.backend.send(...)`。
2. 什么时候切到 `env.backend.execute(BackendSendPlan(...))`。
3. 当前 ROB 半模型里的 `non-mem` / `store readiness` 到底该怎么表达。

如果你想看完整设计背景，请先读：

- `src/test/python/MemBlock/docs/backend_request_model_design.md`
- `src/test/python/MemBlock/docs/rob_model.md`

## 2. 先记住三个规则

- 普通单笔 load/store：优先 `send()`。
- 需要拍级排列、运行时 `StoreRef`、同拍多 lane：切到 `BackendSendPlan`。
- 需要表达 ROB 程序序阻塞：继续用 `BackendSendPlan`，不要直接改 `env.rob_agent`。

这里的 `non-mem` 在当前实现里是 **ROB placeholder / blocker**，不是新的 `IssueOp`，不会去占用某条 issue lane。

## 3. 最常见模板

### 3.1 单笔 load

```python
from transactions import LoadTxn

env.backend.send(
    LoadTxn(
        req_id=0x21,
        addr=0x9000_0000,
        lq_ptr=state.next_lq_ptr,
        sq_ptr=state.sq_ptr,
    )
)
```

适用场景：

- 普通 directed load
- sequence 内部的标准主路径
- 不关心具体哪一拍 issue 到哪个 lane

### 3.2 单笔 store

```python
from transactions import StoreTxn

allocated_sq_ptr = env.backend.send(
    StoreTxn(
        req_id=0x31,
        sq_ptr=state.sq_ptr,
        addr=0x9000_1000,
        data=0x1122334455667788,
        mask=0xFF,
    )
)
```

适用场景：

- 普通 scalar store 主路径
- 只想让环境按标准 `enqueue -> STD -> STA` 发送
- 不需要自己显式安排行为脚本

### 3.3 两条 load 同拍 issue

```python
from transactions import BackendSendPlan, EnqueueLoadCyclePlan, IssueCyclePlan, IssueOp

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

适用场景：

- 同拍多 lane 组合
- replay 探路
- backend/issue 时序 debug

## 4. ROB 相关模板

### 4.1 插入 non-mem blocker，阻塞 younger mem commit

```python
from transactions import (
    BackendSendPlan,
    EnqueueStoreStep,
    IssueCyclePlan,
    IssueOp,
    NonMemBlockerStep,
    StoreCommitStep,
    StoreRef,
)

store_ref = StoreRef("younger_store")

env.backend.execute(
    BackendSendPlan.from_steps(
        NonMemBlockerStep.insert(rob_idx_flag=0, rob_idx_value=0x40),
        EnqueueStoreStep.from_txn(store_txn, ref=store_ref),
        IssueCyclePlan.from_ops(
            IssueOp.std(req_id=store_txn.req_id, sq_ptr=store_ref, data=store_txn.data, mask=store_txn.mask)
        ),
        IssueCyclePlan.from_ops(
            IssueOp.sta(req_id=store_txn.req_id, sq_ptr=store_ref, addr=store_txn.addr, mask=store_txn.mask)
        ),
        StoreCommitStep(count=1),
        NonMemBlockerStep.release(rob_idx_flag=0, rob_idx_value=0x40),
        StoreCommitStep(count=1),
    )
)
```

这个模板表达的是：

- younger store 的 addr/data 都已经 ready；
- 但 older non-mem 还卡在 ROB head；
- 第一拍 `StoreCommitStep` 不应放行该 store；
- 只有 release blocker 后，下一拍 commit 才应该推进。

### 4.2 显式控制 store readiness

正常情况下，`STD + STA` 会自动把 data/address ready 同步到 ROB 半模型，所以你 **通常不需要** 额外写 readiness 步骤。

只有在你要验证“token 已到，但 store 还不该 commit”这类边界时，才建议显式使用 `StoreCommitReadyStep`：

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

适用场景：

- older unready store 阻塞 younger mem
- token-only 不应提交 store
- readiness 切换后 frontier 才继续前推

### 4.3 `StoreRef` 和真实 SQ pointer 的关系

如果后续步骤还要继续引用 enqueue 后才知道的 SQ pointer，就用 `StoreRef`：

```python
result = env.backend.execute(
    BackendSendPlan.from_steps(
        EnqueueStoreStep.from_txn(store_txn, ref=store_ref),
        IssueCyclePlan.from_ops(
            IssueOp.std(req_id=store_txn.req_id, sq_ptr=store_ref, data=store_txn.data, mask=store_txn.mask)
        ),
    )
)

real_sq_ptr = result.resolve_sq_ptr(store_ref)
```

推荐做法：

- 在 plan 内部继续优先用 `StoreRef`
- 只有 plan 执行完、外层真的需要拿实际 `QueuePtr` 时，再调用 `resolve_sq_ptr()`

## 5. 什么时候别用这些接口

- 不要把 `non-mem` 写成 `IssueOp(kind=...)`
- 不要在 testcase 里直接操作 `env.rob_agent._entries` 之类内部状态
- 不要为了普通 scalar store 主路径滥用 `StoreCommitReadyStep`
- 不要把 sequence 已经能稳定表达的业务场景全部降级成手写 plan

## 6. 推荐选择表

- `send(load_txn)`
  - 单笔 load 主路径
- `send(store_txn)`
  - 单笔 store 主路径
- `execute(BackendSendPlan(...))`
  - 同拍多 lane / 多步脚本 / 运行时 `StoreRef`
- `NonMemBlockerStep`
  - older non-mem 挡住 younger mem commit
- `StoreCommitReadyStep`
  - 显式验证 token 与 readiness 分离
- sequence
  - 当这类脚本已经成为稳定业务模板时
