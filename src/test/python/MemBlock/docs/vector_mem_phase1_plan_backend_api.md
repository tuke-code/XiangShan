# MemBlock 向量访存 Phase 1：Backend API 设计

## 1. 文档目的

本文档专门回答一个问题：

> 在 `src/test/python/MemBlock/` 当前已经落地的 `BackendSendPlan` 体系下，Phase 1 的向量访存应该如何接入 backend 控制面，而不是再平行发明一套只服务 vector 的临时 API？

这里讨论的重点不是立即实现所有 RVV 细节，而是先把 **vector backend 交互界面** 设计成与当前 scalar backend 模型同构、可扩展、且不会把 testcase 再拖回 pin-level 脚本的形态。当前 Phase 1 front-door 明确为：**`enqLsq + vecIssue`**。

本文档希望收敛四件事：

1. 为什么向量访存不应绕开 `BackendSendPlan` 单独建一套 `send_vector_*_xxx()` 名字森林。
2. 现有 `transactions.py` / `agents/backend_facade.py` 中哪些抽象可以直接复用。
3. Phase 1 应新增哪些 vector step / op / result 抽象，才能把向量操作纳入同一脚本体系。
4. testcase / sequence / vector facade 在这个体系中各自停在哪一层。

本文档默认与以下文档配套阅读：

- `src/test/python/MemBlock/docs/vector_mem_phase1_plan.md`
- `src/test/python/MemBlock/docs/backend_request_model_design.md`
- `src/test/python/MemBlock/docs/vector_mem_plan.md`

## 2. 当前 backend 体系的基线

在继续设计 vector API 之前，先明确当前 scalar backend 已经是什么形状。

### 2.1 当前已存在的统一脚本骨架

`transactions.py` 中当前已经有三层抽象：

1. **事务层**
   - `LoadTxn`
   - `StoreTxn`
2. **单拍动作层**
   - `EnqueueLoadStep`
   - `EnqueueLoadCyclePlan`
   - `EnqueueStoreStep`
   - `IssueOp`
   - `IssueCyclePlan`
3. **多拍顺序脚本层**
   - `BackendSendPlan`
   - `BackendSendResult`

`agents/backend_facade.py` 中当前 `BackendFacade.send()` / `execute()` 已经承担“脚本解释器”职责，而不只是零散 helper 集合：

- `send(LoadTxn)` 会翻译成标准 load enqueue + issue 脚本
- `send(StoreTxn)` 会翻译成标准 store enqueue + `STD` + `STA` 脚本
- `execute(BackendSendPlan)` 会逐步解释：
  - enqueue step
  - issue cycle
  - ROB blocker step
  - store readiness step
  - commit pulse

### 2.2 当前体系已经证明的方向

这说明 MemBlock backend API 的扩展方向已经不是：

- 每多一种场景，就新增一个 `send_xxx_with_yyy_same_cycle()` helper

而是：

- 保持少量高层 `send(...)`
- 把复杂场景写成显式 `BackendSendPlan`
- 由 `BackendFacade.execute()` 解释脚本

向量访存如果不延续这条路，后面很快就会重新回到 helper 爆炸：

- `send_vector_unit_stride_load()`
- `send_vector_unit_stride_store()`
- `send_vector_stride_load()`
- `send_vector_masked_store()`
- `send_vector_load_with_vstart()`
- `send_vector_load_with_mask_and_stride()`
- ...

这正是当前 scalar backend 体系已经在主动避免的事情。

## 3. 向量 backend 接口的核心设计原则

## 3.1 不另起一套平行脚本系统

Phase 1 不建议出现：

- `VectorBackendPlan`
- `VectorIssuePlan`
- `VectorBackendResult`

这样一整套与 `BackendSendPlan` 平行的新容器。

原因不是“名字上看起来重复”，而是它会立刻带来两套控制面：

- scalar testcase/sequence 走 `BackendSendPlan`
- vector testcase/sequence 走 `VectorBackendPlan`

后果会是：

- `BackendFacade` 和未来 `VectorBackendFacade` 会各自维护一套 plan 解释器
- ROB blocker / commit pulse / future common steps 无法自然共享
- mixed scalar/vector case 很难写成一段统一时序脚本

因此，Phase 1 的设计原则应是：

> vector 进入 **同一个** `BackendSendPlan`，通过新增 step/op 类型扩展这套脚本语言，而不是另造一门语言。

## 3.2 保持“高层 send + 显式 plan”双入口

向量 backend 也应延续 scalar 当前的双入口结构：

1. **默认路径**：给简单场景一个稳定入口
   - `env.vector_backend.send(txn)`
2. **复杂路径**：把特殊编排收敛到统一脚本
   - `env.vector_backend.execute(BackendSendPlan(...))`
   - 或直接 `env.backend.execute(BackendSendPlan(...))`，如果后续确认 vector step 解释权仍留在统一 backend facade

也就是说，vector facade 的价值应是：

- 提供 vector 语义下的默认发送脚本
- 提供 context 配置 / completion 等 vector 专属等待接口
- 但底层脚本容器仍是 `BackendSendPlan`

## 3.3 把 vector 特有差异收敛到“step/op 类型”而不是函数名

向量访存与当前 scalar backend 的主要差异，不在于“也需要 send”，而在于它多了三类新的时序语义：

1. **向量上下文配置**
   - `vl`
   - `vtype`
   - `vstart`
   - mask register
2. **向量 issue 动作**
   - 发一条 vector memory op
3. **向量完成/异常等待**
   - completion / trap 的等待与采样

因此应该扩展的是：

- 新的 step dataclass
- 新的 issue op kind 或 vector issue step
- 新的 result 载荷

而不是继续堆新的 helper 名称。

## 3.4 对 testcase 暴露语义，不暴露 pin-level

即使 vector 最终要在 backend 内部驱动更多 CSR / issue bundle，testcase 也不应直接承担：

- 手动写 `vl` / `vtype` / `vstart` 的逐拍时序
- 手动装配 mask 寄存器写入握手
- 手动观察数十个 vector 请求 / 完成信号

因此 backend API 的目标不是“让 testcase 能摸到更多线”，而是：

- 把这些细节压到 plan step 和 facade 内部
- testcase 只表达向量语义脚本

## 4. Phase 1 推荐的扩展形状

## 4.1 保留 `BackendSendPlan`，新增 vector step 家族

Phase 1 推荐继续使用：

- `BackendSendPlan.steps: tuple[object, ...]`

但扩展其合法 step 类型集合，加入 vector 相关步骤。

推荐最小新增集合：

### 4.1.1 `VectorContextConfigStep`

职责：描述一次向量上下文配置，而不是描述某几个 pin 要如何 toggle。

建议字段：

- `vl`
- `vstart`
- `sew_bits`
- `lmul`（Phase 1 若暂不真正使用，可保留缺省字段）
- `mask_enable`
- `mask_bits`
- `tail_policy`（可选，若当前 DUT 路径不需要则保持缺省）
- `mask_policy`（可选）

它表达的语义是：

- “在后续 vector issue 前，把 DUT 置到这组向量上下文”

而不是：

- “依次写哪个 CSR、每拍拉什么 valid”

### 4.1.2 `VectorIssueStep`

职责：描述一次向量访存 issue。

建议字段：

- `txn: VectorMemTxn`
- `lane` 或 `issue_port`（若当前 DUT 只有单一 vector issue 入口，可先固定缺省）
- `max_cycles`

它的语义不是 element 级 issue，而是：

- “把这条 vector mem instruction 送入 DUT”

这里建议 **不要** 在 Phase 1 把 vector instruction 拆成一堆 element 级 `IssueOp`。原因是：

- element-level 展开属于 `VectorMemoryModel.expand()` 的参考语义
- DUT 侧当前要驱动的是 instruction 级 vector memory op
- 若把 element 级动作塞进 backend plan，会把 model 和 driver 边界搞乱

### 4.1.3 `VectorWaitStep`

职责：把 vector completion / trap 等待也纳入同一脚本顺序，而不是让 testcase 到处手动 call wait helper。

建议字段：

- `req_id`
- `event`
  - `complete`
  - `trap`
  - `complete_or_trap`
- `max_cycles`

这一步的价值是把：

- configure
- issue
- wait

表达成一段完整脚本，而不是只把“发送”放进 plan，其余阶段散落到 testcase。

### 4.1.4 `VectorContextRestoreStep`

职责：恢复先前保存的 vector context，或恢复到默认安全上下文。

Phase 1 若当前场景都由 testcase 独占 env，也可以先不强制实现；但文档层面建议保留这个位置，因为后续 mixed-path 或 sequence 复用时大概率需要。

## 4.2 `IssueOp` 不建议直接膨胀成所有 vector 语义入口

当前 `IssueOp.kind` 只支持：

- `load`
- `sta`
- `std`

理论上可以继续扩成：

- `vector_mem`
- `vector_cfg`
- `vector_mask_write`

但 Phase 1 不建议把所有 vector 行为都挤进 `IssueOp`，原因有三点：

1. `IssueOp` 当前明确是 **lane 级单拍 issue 动作**
2. vector context 配置并不一定等价于 issue lane 动作
3. 把 configure / wait 也塞进 `IssueOp.kind` 会使其语义过宽

因此更推荐的结构是：

- **保留 `IssueOp` 只负责 scalar issue lane 这一类既有原子动作**
- **新增 vector 专属 step** 来承载 vector configure / issue / wait

换句话说，Phase 1 的扩展重点应放在：

- `BackendSendPlan` 的 step 集合扩展

而不是：

- 把所有新能力都压成 `IssueOp.kind` 分支

## 4.3 `BackendSendResult` 需要增加 vector 观测载荷

当前 `BackendSendResult` 主要承载：

- `store_ptrs: dict[StoreRef, QueuePtr]`

向量接入后，推荐最小扩展：

- `vector_results: dict[int, VectorMemResult]`

key 推荐直接用 `req_id`。

原因：

- scalar store 的运行时产物是“实际分配到的 SQ ptr”
- vector op 的运行时产物更像是“完成后采到的结果摘要”

这样 `BackendSendPlan` 执行结束后，sequence 就可以直接从 result 中读取：

- `result.vector_results[txn.req_id]`

而不需要每个 sequence 再单独去 monitor 拉一遍。

如果担心 `BackendSendResult` 过胖，Phase 1 也可以只在 `VectorWaitStep` 执行时把结果缓存到 `env.vector_monitor`，然后由 facade helper 读取；但从接口一致性看，直接把 vector result 留在 send result 里会更完整。

## 4.4 `VectorMemTxn` 应成为 vector send 的单笔默认入口

建议让：

- `env.vector_backend.send(txn: VectorMemTxn)`

像 scalar `send(LoadTxn/StoreTxn)` 一样，负责把单笔 vector txn 翻译成默认脚本。

推荐默认脚本形状：

1. `VectorEnqueueStep.from_txn(txn)`
2. `VectorIssueStep.from_txn(txn)`
3. `VectorWaitStep(req_id=txn.req_id, event="complete_or_trap")`

这意味着单笔 vector 场景也保持“用户写 txn，facade 生成 plan”的模式，与 scalar 对齐。

## 5. 推荐的数据结构草案

下面给出 Phase 1 更适合的抽象草案。这里强调的是接口形状，不是要求一次性把全部字段都实现完。

## 5.1 `VectorContextConfigStep`

```python
@dataclass(frozen=True)
class VectorContextConfigStep:
    vl: int
    vstart: int = 0
    sew_bits: int = 32
    lmul: int = 1
    mask_enable: bool = False
    mask_bits: tuple[int, ...] | None = None
    restore_token: str | None = None

    @classmethod
    def from_txn(cls, txn: "VectorMemTxn") -> "VectorContextConfigStep":
        ...
```

说明：

- `from_txn()` 便于单笔默认 `send()` 路径直接复用
- `restore_token` 用于未来保存/恢复上下文；Phase 1 可先不落地复杂实现

## 5.2 `VectorIssueStep`

```python
@dataclass(frozen=True)
class VectorIssueStep:
    txn: "VectorMemTxn"
    issue_port: int = 0
    max_cycles: int = 50

    @classmethod
    def from_txn(cls, txn: "VectorMemTxn", issue_port: int = 0) -> "VectorIssueStep":
        ...
```

说明：

- 这里直接持有 `VectorMemTxn`
- 不要再重复把 `opcode_class/base_addr/stride/vl/...` 拆平复制一遍

## 5.3 `VectorWaitStep`

```python
@dataclass(frozen=True)
class VectorWaitStep:
    req_id: int
    event: str = "complete_or_trap"
    max_cycles: int = 200

    def __post_init__(self) -> None:
        if self.event not in {"complete", "trap", "complete_or_trap"}:
            raise ValueError(...)
```

说明：

- 这一步负责把 monitor/facade 等待语义纳入脚本
- `complete_or_trap` 最适合作为默认模式

## 5.4 `VectorContextRestoreStep`

```python
@dataclass(frozen=True)
class VectorContextRestoreStep:
    restore_token: str | None = None
```

说明：

- Phase 1 可先只支持“恢复默认上下文”
- 后续若引入嵌套配置，再扩 token 语义

## 5.5 `BackendSendResult` 扩展

```python
@dataclass(frozen=True)
class BackendSendResult:
    store_ptrs: dict[StoreRef, QueuePtr]
    vector_results: dict[int, "VectorMemResult"]

    def resolve_sq_ptr(self, sq_ptr: QueuePtr | StoreRef) -> QueuePtr:
        ...

    def get_vector_result(self, req_id: int) -> "VectorMemResult":
        return self.vector_results[req_id]
```

## 6. `BackendFacade.execute()` 的推荐扩展方式

## 6.1 保持“按 step 类型解释”的当前结构

当前 `BackendFacade.execute()` 的结构已经很适合作为 vector 扩展底座：

```python
for step in plan.steps:
    if isinstance(step, EnqueueLoadStep):
        ...
    elif isinstance(step, EnqueueLoadCyclePlan):
        ...
    elif isinstance(step, EnqueueStoreStep):
        ...
    elif isinstance(step, IssueCyclePlan):
        ...
    elif isinstance(step, StoreCommitStep):
        ...
    elif isinstance(step, NonMemBlockerStep):
        ...
    elif isinstance(step, StoreCommitReadyStep):
        ...
```

Phase 1 推荐直接在这个结构上增加分支，而不是额外发明第二个执行器。

建议扩展为：

```python
    elif isinstance(step, VectorContextConfigStep):
        self.configure_vector_context(step)
    elif isinstance(step, VectorIssueStep):
        self.issue_vector_mem(step)
    elif isinstance(step, VectorWaitStep):
        vector_result = self.wait_vector_event(step)
        vector_results[step.req_id] = vector_result
    elif isinstance(step, VectorContextRestoreStep):
        self.restore_vector_context(step)
```

## 6.2 不建议把 vector 解释器完全塞到 testcase/sequence

不建议让 sequence 自己做：

- `env.vector_backend.configure_vector_context(...)`
- `env.vector_backend.issue_vector_mem(...)`
- `env.vector_backend.wait_complete(...)`

然后一条用例里手写三四次。

因为这样虽然表面上“没有新增 step 类型”，实质上还是把脚本解释工作回退给了调用方。

正确的层次应是：

- sequence/testcase 负责决定脚本内容
- facade 负责解释脚本内容

## 6.3 允许 `VectorBackendFacade` 成为薄封装，而不是第二套 plan 体系

Phase 1 推荐存在：

- `agents/vector_backend_facade.py`

但它不应维护自己的 plan 类型系统，而应更像：

- 对 `BackendFacade` 中 vector 相关能力的薄封装
- 或者是专门承载 vector 语义默认入口的 facade

推荐分工：

### `BackendFacade`

负责：

- 统一解释 `BackendSendPlan`
- 支持 scalar + vector + ROB 共存的 step 集合

### `VectorBackendFacade`

负责：

- `send(txn: VectorMemTxn)`
- `execute(txns_or_plan)` 的 vector 语义包装
- `wait_complete()` / `wait_trap()` 的语义友好包装
- `configure_vector_context(...)` / `restore_vector_context(...)` 的便捷 facade

这样可以同时保持：

- backend plan 体系单一
- vector 调用入口语义清晰

## 7. testcase / sequence 的推荐用法

## 7.1 testcase 不直接拼 pin-level 操作

推荐：

1. preload memory
2. 构造 `VectorMemTxn`
3. 用 sequence 或 `env.vector_backend.send(txn)` 发请求
4. 通过 `env.memory.vector.expect_load(txn)` / `predict_store(txn)` 建立参考
5. 读取 `VectorMemResult` 做断言
6. `env.assert_no_outstanding()` 收口

不推荐：

- testcase 直接自己逐拍配置 `vl/vtype/vstart`
- testcase 手动轮询 vector completion 端口
- testcase 手动维护一套“本条向量请求到底完成没”的局部状态

## 7.2 simple case 用 `send(txn)`，复杂 case 用 `BackendSendPlan`

### 简单 case

比如 unit-stride load smoke：

```python
txn = VectorMemTxn(...)
expected = env.memory.vector.expect_load(txn)
result = env.vector_backend.send(txn)
assert result.req_id == txn.req_id
```

### 复杂 case

比如明确想表达：

- 配置向量上下文
- 发一条 vector load
- 插入一条 ROB blocker
- 等待 vector complete
- release blocker

则推荐：

```python
plan = BackendSendPlan.from_steps(
    VectorContextConfigStep.from_txn(txn),
    VectorIssueStep.from_txn(txn),
    NonMemBlockerStep.insert(
        rob_idx=RobIndex(flag=txn.rob_idx.flag, value=txn.rob_idx.value - 1),
    ),
    VectorWaitStep(req_id=txn.req_id, event="complete_or_trap"),
    NonMemBlockerStep.release(
        rob_idx=RobIndex(flag=txn.rob_idx.flag, value=txn.rob_idx.value - 1),
    ),
)
result = env.backend.execute(plan)
vector_result = result.get_vector_result(txn.req_id)
```

这类写法的好处是：

- vector step 和既有 ROB step 天然能混排
- mixed-path 场景不需要重新设计另一层 orchestration API

## 7.3 sequence 应成为 plan 模板的承载者

Phase 1 新增的 `Vector*Sequence` 更适合做的事是：

- 隐藏重复的 plan 模板
- 统一结果收集与断言前处理

例如：

- `VectorUnitStrideLoadSequence`
  - 内部生成：`config -> issue -> wait`
- `VectorStrideLoadSequence`
  - 内部生成：`config -> issue -> wait`
- `VectorMaskedStoreSequence`
  - 内部生成：`config -> issue -> wait -> optional drain`

这样 testcase 看到的仍然是语义级 sequence，而不是大段脚本重复。

## 8. 为什么这是比“新 helper 名字”更好的 Phase 1 方案

## 8.1 能自然承接 mixed scalar/vector 场景

若 vector 接口走同一 `BackendSendPlan`，后续 mixed-path 可以自然写成：

- scalar `IssueCyclePlan`
- vector `VectorIssueStep`
- ROB `NonMemBlockerStep`
- flush / commit / wait steps

都放在同一 plan 中。

若 vector 走独立 plan 体系，mixed 场景迟早会遇到：

- 谁是总 orchestration 容器？
- scalar plan 和 vector plan 如何嵌套？
- ROB step 放在哪边？

这些问题本质上都是“不该有两套 plan”导致的。

## 8.2 能继续复用当前 backend 文档与认知模型

当前团队已经开始围绕：

- `BackendSendPlan`
- `IssueCyclePlan`
- `NonMemBlockerStep`
- `StoreCommitReadyStep`

建立共享认知。

如果 vector 还沿这条线扩展，那么后续文档与协作成本都更低，因为大家只是在学习：

- 这门脚本语言新增了哪些 vector 语义步骤

而不是重新学习：

- 另一套 vector orchestration 模型

## 8.3 能控制 Phase 1 的实现面

Phase 1 只要实现以下最小链路，就能形成闭环：

1. `transactions.py`
   - 新增 vector step dataclass
   - 扩展 `BackendSendPlan` 合法 step 集
   - 扩展 `BackendSendResult`
2. `agents/backend_facade.py`
   - 扩展 `execute()` 分支
3. `agents/vector_backend_facade.py`
   - 提供 `send(txn)` 默认脚本包装
4. `sequences/vector_mem_sequences.py`
   - 提供常用 plan 模板

而不需要在 Phase 1 同时落一整套第二 plan runtime。

## 9. Phase 1 明确不建议做的设计

### 9.1 不建议为每类 vector 场景起一个 public helper

不建议新增：

- `send_vector_unit_stride_load()`
- `send_vector_stride_load()`
- `send_vector_masked_store()`
- `send_vector_nonzero_vstart_load()`

这些名字短期看似方便，长期只会重复 scalar 早期 helper 膨胀的问题。

### 9.2 不建议把 element-level 语义塞进 backend plan

不建议让 backend plan 直接表达：

- 每个 element 的地址
- 每个 element 的 active/inactive/tail
- 每个 element 是否访问 memory

这些都应留在：

- `VectorMemoryModel.expand()`
- `VectorMemoryModel.expect_load()`
- `VectorMemoryModel.predict_store()`

backend plan 只表达：

- 指令级配置、发射、等待

### 9.3 不建议把 vector configure/wait 都压成 `IssueOp.kind`

Phase 1 不建议把 `IssueOp` 变成无所不包的 mega-op 枚举。

更好的分层是：

- lane issue 动作继续留在 `IssueOp`
- vector configure / wait 用独立 step

## 10. 推荐实施顺序

建议按下面顺序推进，而不是一次性全铺：

1. 在 `transactions.py` 中定义：
   - `VectorContextConfigStep`
   - `VectorIssueStep`
   - `VectorWaitStep`
   - `VectorContextRestoreStep`
   - 扩展 `BackendSendResult`
2. 扩展 `BackendSendPlan.__post_init__()` 接受上述 step
3. 在 `agents/backend_facade.py` 中增加对应 `execute()` 分支
4. 新增 `agents/vector_backend_facade.py`
   - 先只提供 `send(txn)` 与 `wait_*` 薄封装
5. 在 `sequences/vector_mem_sequences.py` 中把常见 `config -> issue -> wait` 模板收敛成 sequence
6. 最后再决定哪些旧 `request_apis.py` 兼容入口值得保留

## 11. 结论

Phase 1 的向量 backend API 最重要的不是“把向量发出去”，而是：

- 不要重新走一遍 scalar 早期 helper 膨胀的老路；
- 不要把 vector 做成 `BackendSendPlan` 之外的平行控制面；
- 要把 vector 的真正差异收敛成少量新的 **step 类型**；
- 继续让 `BackendFacade` 扮演统一脚本解释器；
- 让 `VectorBackendFacade` 只承担 vector 语义默认入口与便捷包装。

如果按这个方向推进，那么后续：

- unit-stride / stride / mask / `vstart`
- mixed scalar/vector
- ROB blocker / completion / trap

都能继续在同一个 backend orchestration 体系里扩展，而不会把 MemBlock 真实 DUT 验证环境拆成两套互不兼容的主动控制面。
