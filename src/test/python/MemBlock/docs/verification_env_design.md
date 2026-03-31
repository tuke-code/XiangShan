# MemBlock 验证环境设计

## 1. 文档目的

本文档总结 2026-04-01 这一轮重构后的 `src/test/python/MemBlock/` Python 验证环境设计。它不再只描述“现有脚本如何使用”，而是试图完整回答以下问题：

1. 当前验证环境已经完成了哪些分层重构。
2. 每一层的职责边界是什么，为什么要这样切。
3. 这次重构解决了旧环境中的哪些痛点。
4. 现阶段还保留了哪些兼容层，为什么没有一步到位继续拆。
5. 后续如果继续向更清晰的 UVM 风格环境演进，下一步应该从哪里下手。

本文档基于当前目录下的以下实现整理：

- `MemBlock_env.py`
- `request_apis.py`
- `transactions.py`
- `agents/`
- `memory_model.py`
- `model/ref_memory.py`
- `model/transport_responder.py`
- `tests/`

文档关注的是 Python 侧验证环境设计，而不是 MemBlock RTL 本体的微结构实现。

## 2. 重构背景与目标

重构前，环境虽然已经能稳定支撑真实 DUT 的 load/store 主路径回归，但整体结构更接近“单个超大 env + 单个超大 memory model + 一组 helper 函数”。这种组织方式在功能较少时很高效，但当需求扩展到更复杂的路径、更多测试人员协作、以及对 DUT 接口变化的长期适配时，会暴露出几个问题：

1. `MemBlockEnv` 既负责端口绑定，又负责时钟推进、请求发送、状态查询、store commit 和环境管理，职责过重。
2. `MemoryModel` 同时承担黄金内存、outer/dcache responder、load compare、store drain 收尾校验等多类逻辑，任何改动都容易影响多条路径。
3. testcase 大量直接依赖 `env.memory.pending_stores`、`env.memory.completed_loads`、`env.memory.*count` 这类内部字段，导致测试与模型内部结构强耦合。
4. `request_apis.py` 虽然屏蔽了部分 pin-level 驱动细节，但仍然主要是“函数式散装参数接口”，没有稳定的事务对象。

这次重构的总体目标不是把环境一次性改造成完整的 UVM 体系，而是分阶段做到四点：

1. testcase 面向公开 facade 和事务对象写，不再直接穿透到 `env.memory` 内部容器。
2. active driver 逻辑显式下沉到 agent，`MemBlockEnv` 更接近组装层和协调层。
3. 黄金内存和传输响应器从大一统 `MemoryModel` 中独立出来，降低单点复杂度。
4. 保持当前 load/store 回归语义不变，避免在结构重构时同时引入行为变化。

## 3. 当前目录结构

重构后，验证环境的目录结构已经从原来的“平铺式脚本集合”开始向分层结构演进：

```text
MemBlock/
  MemBlock_api.py
  MemBlock_env.py
  request_apis.py
  transactions.py
  memory_model.py
  agents/
    csr_agent.py
    commit_agent.py
    lsq_agent.py
    issue_agent.py
  model/
    ref_memory.py
    transport_responder.py
  tests/
  docs/
```

当前结构中最重要的变化是：

- `transactions.py` 提供事务对象。
- `agents/` 持有主动驱动 DUT 的逻辑。
- `model/` 持有从 `MemoryModel` 中剥离出来的通用组件。
- `MemBlock_env.py` 仍然是顶层环境入口，但不再承担全部驱动细节。

这意味着环境已经从“单层大对象”转向“顶层 env 组装多个职责明确的部件”。

## 4. 顶层架构

### 4.1 总体分层

当前环境可以概括为如下分层：

```text
pytest testcase
  -> request_apis / transactions
    -> MemBlockEnv facade
      -> active agents
      -> MemoryModel
         -> RefMemory
         -> TransportResponder
      -> DUT
```

从职责上看可以分为五层：

1. testcase 层
   - 描述场景、初始化状态、断言结果。
   - 不再直接操作 `env.memory.pending_stores` 这类内部容器。

2. 事务与公共 API 层
   - `transactions.py` 定义 `LoadTxn`、`StoreTxn`、`QueuePtr`。
   - `request_apis.py` 提供兼容型 helper，并逐步转向事务接口。

3. env facade 层
   - `MemBlockEnv` 对外提供稳定入口，如 preload、expect、wait、stats、flush。
   - testcase 应优先面向这一层写。

4. active driver 层
   - `csr_agent.py`
   - `commit_agent.py`
   - `lsq_agent.py`
   - `issue_agent.py`
   - 这层直接负责驱动 DUT 输入。

5. 模型与响应层
   - `RefMemory` 维护黄金内存。
   - `TransportResponder` 负责 outer/dcache 请求响应。
   - `MemoryModel` 暂时仍持有 compare、store shadow 和收尾校验逻辑，并组合前述组件。

### 4.2 设计思路

这里采用的是“先把最容易稳定下来的边界收出来，再逐步继续拆”的思路，而不是追求一次性完美抽象。原因很现实：

1. MemBlock 环境直接服务真实 DUT 回归，结构重构不能牺牲当前主路径可用性。
2. load commit-boundary compare、store drain 校验、sbuffer/outer 双路径这些逻辑彼此之间存在时序耦合，一次性全拆风险很高。
3. 先把 testcase 和内部实现解耦，可以显著降低后续继续拆模型时的连带改动。

因此本轮实现优先顺序是：

1. 收口 facade。
2. 引入事务对象。
3. 抽出 active agents。
4. 抽出 `RefMemory`。
5. 抽出 `TransportResponder`。

而 monitor、scoreboard、sequence、统一 config 则留到未来继续演进。

## 5. `MemBlockEnv` 的角色变化

### 5.1 重构前

旧版 `MemBlockEnv` 同时负责：

- 绑定 bundle
- 管理默认输入值
- 维护 `PendingPtrDriver`
- 处理 CSR 默认态
- 驱动 enqueue / issue
- 提供 response injection
- 提供 reset / step / flush / drain
- 作为 testcase 查询内部状态的入口

这使得 `MemBlockEnv` 成为了事实上的“超级对象”。

### 5.2 重构后

当前 `MemBlockEnv` 的职责已经明显收敛为三类：

1. 顶层组装
   - 创建 bundle
   - 创建 agent
   - 创建 `MemoryModel`
   - 把 `memory.on_memory_edge` 注册到 `StepRis`

2. 时钟与环境协调
   - `idle_inputs()`
   - `Step()`
   - `reset()`
   - `flush_store_buffers_and_wait()`

3. 对外 facade
   - `preload_u64()`
   - `expect_scalar_load()`
   - `get_transport_stats()`
   - `get_completed_load_count()`
   - `get_counter()`
   - `get_store_view()`
   - `wait_store_materialized()`
   - `wait_counter_growth()`
   - `wait_memory_quiesce()`
   - `assert_no_outstanding()`

换句话说，当前 `MemBlockEnv` 已经更像“顶层 env + facade”，而不是“所有逻辑都往里塞的工具箱”。

### 5.3 这样设计的价值

主要价值有三个：

1. testcase 不需要知道 `MemoryModel` 内部字段名字。
2. 后续 `MemoryModel` 再拆分时，tests 可以基本不动。
3. env 的公开行为已经可以稳定下来，便于写更系统的冒烟测试。

## 6. 事务层设计

### 6.1 `QueuePtr`

`QueuePtr` 被保留为独立 dataclass，而不是重新塞回 helper 函数。原因是当前环境里 LQ/SQ 指针既参与 enqueue，也参与 testcase 侧的虚拟流量状态维护。把它显式建模可以避免多处散装传 `(flag, value)`。

### 6.2 `LoadTxn`

`LoadTxn` 当前封装了：

- `req_id`
- `addr`
- `lq_ptr`
- `sq_ptr`
- `size`
- `mask`
- `pdest`
- `enq_port`
- `issue_lane`

同时通过 property 统一导出：

- `rob_idx_flag`
- `rob_idx_value`
- `resolved_pdest`

这使得 load 的 enqueue、issue、expect 三个步骤不需要分别重新计算同一套 ROB / pdest 信息。

### 6.3 `StoreTxn`

`StoreTxn` 当前封装了：

- `req_id`
- `sq_ptr`
- `addr`
- `data`
- `mask`
- `enq_port`
- `sta_lane`
- `std_lane`

它的重点不是让 store 立即变成完整的 UVM sequence item，而是先把 testcase 和 helper 中原本分散的参数组合收口到一个对象上。

### 6.4 兼容策略

当前并没有直接删除旧 `enqueue_scalar_*` / `issue_scalar_*` 接口，而是增加：

- `send_load(env, txn)`
- `expect_load(env, txn)`
- `send_store(env, txn)`

这样做的原因是：

1. 旧测试和工具脚本还能继续跑。
2. 新写法可以逐步迁移到事务对象。
3. 同一条事务的字段填充逻辑开始集中，减少重复实现。

## 7. Agent 设计

### 7.1 `CsrAgent`

`CsrAgent` 当前负责：

- 驱动 CSR 默认值
- 恢复到默认 M-mode
- 封装原来 `MockCSRInterface` 的职责

它的意义不在于“提供很多复杂 CSR 功能”，而在于把“环境默认态配置”从 `MemBlockEnv` 里拿出去。以后如果需要加 VS/VS-stage、虚拟化、PMA 或异常路径相关 CSR 场景，可以继续在这个 agent 上扩展，而不是把字段设置逻辑重新散落到 env 和 testcase。

### 7.2 `CommitAgent`

`CommitAgent` 是对 `PendingPtrDriver` 的一层封装，负责：

- `pendingPtr` 驱动
- load issued / completed 记录
- 提交边界推进
- `scommit` 脉冲排队

这层抽象的重要意义是明确：`pendingPtr` 和 `scommit` 是一种“提交边界驱动”能力，而不是 env 杂项功能的一部分。未来如果要把 commit 相关逻辑进一步改成更完整的 commit-side agent，这个边界已经有了。

### 7.3 `LsqAgent`

`LsqAgent` 负责：

- 等待 load/store enqueue ready
- 驱动 `enqLsq`
- 读取 store 分配后的真实 `sqIdx`

把这部分从 `request_apis.py` 和 env 中抽出来之后，enq 逻辑的职责边界变得很清晰：只管 LSQ 分配，不管 issue、不管 compare。

### 7.4 `IssueAgent`

`IssueAgent` 负责：

- issue ready 等待
- load / STD / STA 的 lane 驱动
- load 握手成功后的 `env.note_load_issued()`

它的重要价值在于：issue 侧的 pin-level 细节，包括 optional signal 补齐、lane 握手、后端 reset 中断，都不再散落在外部 helper 里。未来如果要支持新的 issue lane 或新字段，也只需要修改这一层。

## 8. `MemoryModel` 的新边界

### 8.1 当前保留的职责

这轮重构后，`MemoryModel` 仍然持有以下逻辑：

- expected load 管理
- observed writeback 管理
- commit-boundary compare
- store shadow 观测
- store retire 语义
- drain 结果校验

也就是说，它现在更接近“scoreboard + store observation coordinator”，但还不是完全独立的 scoreboard。

### 8.2 `RefMemory`

`RefMemory` 是第一块被抽出来的通用组件，负责：

- `preload_bytes`
- `preload_u64`
- `fill_random`
- `read`
- `read_masked`
- `read_cacheline`
- `apply_masked_write`

抽这一层的动机很直接：黄金内存本身不应该和 TL responder、writeback compare 或 store shadow 采样绑死在一起。把它抽出后有三个直接收益：

1. 黄金内存的读写接口变得单一、稳定。
2. `TransportResponder` 与 compare 逻辑都可以共享同一份权威内存视图。
3. `RefMemory` 可以单独单测，降低回归定位成本。

### 8.3 `TransportResponder`

`TransportResponder` 是第二块被抽出来的组件，负责：

- outer TL-A/TL-D 请求与响应闭环
- dcache A/B/C/D/E 请求与响应闭环
- 响应延迟建模
- transport 计数器维护
- 事务 outstanding 状态统计
- 将 outer put 记录到 `drain_log`

这一步很关键，因为在旧结构里 responder 和 compare 都在同一个 `MemoryModel` 类里。拆出 responder 后，现在可以更清楚地回答：

- 如果 outer/dcache 的 ready/response 队列有问题，是 responder 的责任。
- 如果 compare 边界、写回登记或 store retire 逻辑有问题，是 `MemoryModel` 剩余部分的责任。

### 8.4 当前仍保留的兼容层

为了不一次性改穿所有调用点，`MemoryModel` 仍然保留了以下兼容 facade：

- `stats`
- `outstanding_transaction_count`
- `enqueue_outer_response`
- `enqueue_b_response`
- `enqueue_d_response`
- 若干 transport counter property

这些 facade 实际上已经转调到 `TransportResponder`，但对 env 和 tests 来说接口暂时保持不变。这是刻意保留的兼容策略，避免本轮重构同时强制修改所有上层代码。

## 9. testcase 写法的变化

当前 testcase 的一个重要变化是：真实 DUT 测试已经不再直接通过 `env.memory` 内部字段驱动主流程。

### 9.1 load testcase

新写法更接近：

1. reset 环境。
2. `env.preload_u64()` 预置黄金内存。
3. 构造 `LoadTxn`。
4. `send_load(env, txn)`。
5. `expect_load(env, txn)`。
6. `env.drain_writebacks()`。
7. `env.get_completed_load_count()` 和 `env.assert_no_outstanding()` 断言。

### 9.2 store testcase

store 用例也从直接读取 `env.memory.pending_stores` 改成：

1. 构造 `StoreTxn`。
2. `send_store(env, txn)`。
3. `env.wait_store_materialized(...)`。
4. `env.get_counter(...)` 检查 outer / sbuffer 计数。
5. `env.flush_store_buffers_and_wait()`。

### 9.3 这种迁移的意义

最大意义在于 testcase 开始依赖“行为接口”，而不是“内部实现容器”。这会显著降低未来继续拆 model/monitor/scoreboard 时的改动范围。

## 10. 当前设计仍然刻意没有做的事

为了控制重构风险，本轮没有继续做以下事项：

1. 没有把 `MemoryModel` 完全拆成独立 scoreboard。
2. 没有引入 passive monitor 层。
3. 没有引入 sequence / virtual sequence 层。
4. 没有统一抽出 `EnvConfig`。
5. 没有新增功能覆盖收集器。

这些不是不重要，而是它们比 facade、agent、ref memory、responder 的拆分更容易引入行为变化。当前策略是先把“最明显的职责混叠”拆掉，再继续向更标准的验证分层演进。

## 11. 未来演进方向

### 11.1 独立 `Scoreboard`

下一步最值得做的是把 `MemoryModel` 中剩余的 compare 和 store retire 逻辑独立成 `Scoreboard`。理想形态下：

- `RefMemory` 只管黄金内存。
- `TransportResponder` 只管 TL 响应。
- `Scoreboard` 只管 expected / observed / commit-boundary compare。

这一步完成后，`memory_model.py` 很可能会退化成“兼容层或编排层”。

### 11.2 passive monitors

建议新增：

- `WritebackMonitor`
- `StoreMonitor`
- `MemStatusMonitor`

其职责是从 DUT 采样事件，再交由 scoreboard 处理。当前 `MemoryModel` 仍然直接扫 bundle，这在规模继续扩大后不够清晰。

### 11.3 sequence 库

当前 `request_apis.py` 已经有了事务层基础，但还没有真正把测试场景抽象成 sequence。未来建议按以下方向演进：

- `SingleLoadSequence`
- `SingleStoreSequence`
- `StoreThenLoadSameAddrSequence`
- `MixedLoadStoreRandomSequence`

届时 testcase 会更偏向“选择 sequence + 断言结果”，而不是手工拼接大量请求步骤。

### 11.4 统一 config

当前常量仍散落在多个文件中，例如 queue 深度、lane 选择、默认延迟、随机种子等。后续应收敛为 `EnvConfig`，至少统一以下信息：

- issue lane
- LQ/SQ/ROB depth
- outer/dcache delay
- strict check 开关
- PMA 地址边界
- 是否启用某些内部 probe

### 11.5 coverage collector

未来验证环境如果要进一步系统化，必须补上功能覆盖，而不仅仅是断言和计数器。建议 coverage 起点包括：

- MMIO / cacheable / NC
- load/store 类型
- 同址 store-load 顺序
- sbuffer drain 与 outer 写出
- ROB wrap / LQ wrap / SQ wrap
- 事务延迟桶

## 12. 结论

这轮重构的意义，不是把 MemBlock Python 环境直接改造成“完整 UVM”，而是完成了几个关键的基础性动作：

1. testcase 与 `MemoryModel` 内部容器的直接耦合被明显削弱。
2. env 的对外稳定接口已经形成。
3. active driver 已经具备 agent 形态。
4. 黄金内存和传输响应器已经从单一大类中分离出来。

从工程维护角度看，这比继续在旧结构上堆功能重要得多。因为只有先把边界清出来，后续 monitor、scoreboard、sequence、coverage 才有稳定落点。当前环境已经从“能跑的单体脚本环境”走到了“可持续演进的分层环境”的第一阶段，这正是本轮重构最核心的价值。
