# MemBlock Clock Control 设计与使用指南

## 1. 文档目的

本文档面向维护 `src/test/python/MemBlock/` 的开发者，专门说明当前验证环境中与时钟推进相关的设计、接口边界、推荐使用方式，以及在 testcase / sequence / env / agent 中应该如何组织 clock control。

与旧版“迁移指南”相比，本文档不再只强调“不要乱写 `Step()`”，而是把当前环境的 clock control 当成一个完整子系统来描述。它主要回答以下问题：

1. 现在到底谁拥有 DUT 时钟推进权。
2. 一次 env 单拍推进背后，环境内部到底做了哪些事情。
3. 哪些接口是给 testcase 用的，哪些只应该在 env/agent 内部使用。
4. 如何基于当前环境的真实测试用例，写出既稳定又易维护的 clock-related 代码。
5. 后续扩展时，怎样避免重新把时钟控制打散回高层。

如果你需要先理解整个验证环境的分层，请先阅读：

- `src/test/python/MemBlock/docs/verification_env_design.md`

如果你需要理解 backend 主动请求模型与 clock control 的关系，请配套阅读：

- `src/test/python/MemBlock/docs/backend_request_model_design.md`

## 2. 设计目标

当前 MemBlock 环境对 clock control 的设计，不只是“把 `dut.Step(1)` 包一层”，而是围绕以下四个目标展开：

1. **时钟推进所有权唯一**
   - `dut.Step(1)` 只能由 env 内核发起。
   - 这样每拍的驱动、观测、模型更新顺序才是唯一且可推理的。

2. **高层只表达语义，不表达拍级细节**
   - testcase 更关心“reset 后发一条 load，再等待写回”；
   - sequence 更关心“commit 后再 settle 若干拍”；
   - 它们都不应该自己实现底层轮询器。

3. **agent 内部可以等待，但不能重新拥有时钟**
   - `LsqAgent`、`IssueAgent` 可以决定“等 ready”或“握手一拍”；
   - 但真正推进拍数，仍然必须经过 env 的统一原语。

4. **拍后观测挂在统一栅栏后**
   - replay 采样、coverage 采样、debug trace 采样，都应该在“每拍结束后的固定阶段”执行；
   - 不再允许通过 monkey-patch `env.Step()` 的方式私下插桩。

一句话概括当前原则：

```text
高层描述行为语义，env 独占时钟推进；
拍后观察统一挂在 env 的 after-step 栅栏。
```

## 3. 时钟控制总览

当前环境中与 clock 相关的职责分布如下：

```text
testcase / sequence / request_apis / env.backend
    -> MemBlockEnv 同步 facade
        -> MemBlockEnv async 原语
            -> EnvClockKernel.step()
                -> dut.Step(1)
```

这里最重要的设计点有三个：

1. testcase 对外使用 `env.advance_cycles()`，而不是再直接依赖旧 `Step()` facade。
2. env 内部真正工作的，是 `_step_async()`、`_await_cycles()`、`_await_until()`、`_step_and_idle_async()` 这些 async 原语。
3. 最底层真正触发 DUT 时钟边沿的地方，集中在 `EnvClockKernel.step()`。

这意味着，当前环境里“谁能推进 DUT 一拍”这个问题已经有了非常明确的答案：

- 逻辑上只有 `MemBlockEnv` 拥有推进权；
- 物理上只有 `EnvClockKernel.step()` 会调用 `dut.Step(1)`。

## 4. 核心实现：`EnvClockKernel` 与 `MemBlockEnv`

### 4.1 `EnvClockKernel` 的角色

`EnvClockKernel` 是最薄的一层时钟内核。当前实现很克制：

- `step(cycles)`
  - 只负责循环调用 `dut.Step(1)`；
- `run(coro)`
  - 负责把 env 内部 async 协程在同步调用点上跑起来；
- `close()`
  - 当前没有复杂资源，只保留统一收口点。

它不承担业务语义，不做 wait，不做回调调度，也不直接碰 model。它存在的意义只有一个：**把对 DUT 时钟的最终控制权收敛到唯一位置**。

### 4.2 `MemBlockEnv._step_async()` 每拍做什么

真正有语义的地方在 `MemBlockEnv._step_async()`。每推进一拍，当前环境固定执行以下顺序：

1. `commit_agent.drive()`
   - 先把本拍需要驱动到 DUT 的 pending pointer / scommit 等状态推上去；
2. `await self._clock.step(1)`
   - 真正调用 `dut.Step(1)`；
3. `memory.after_cycle()`
   - 让 transport/mock/memory model 更新拍后状态；
4. reset/backend reset 判断
   - 若处于 reset，则重置 commit 侧运行态；
5. `mem_status_monitor.after_cycle()`
   - 采样 mem status，更新对 env / model 有用的状态；
6. `commit_agent.advance()`
   - 根据本拍观测到的完成情况推进 pending pointer 等软件侧状态机；
7. 执行 `after_step_callback`
   - replay trace、coverage sample、debug 采样器等都在这里运行。

这个顺序非常关键，因为它定义了“当前环境里一拍结束后的真相”。

如果未来某段逻辑绕开了 `_step_async()`，哪怕功能上也调用了 `dut.Step(1)`，它仍然会破坏以下假设：

- commit 驱动是否在正确相位生效；
- monitor / model 是否在统一拍后顺序更新；
- callback 观察到的是不是同一种“拍后状态”。

因此，在当前环境中，clock control 的第一条铁律就是：

```text
dut.Step(1) 不能绕过 _step_async() 的统一语义栈。
```

### 4.3 Rising-edge callback 与 after-step callback 的区别

当前环境同时存在两类“随时钟执行”的逻辑：

1. `dut.StepRis(self.memory.on_memory_edge)`
   - 这是注册在 DUT 时钟上升沿附近的底层 callback；
   - 主要用于 memory model / transport mock 与 DUT 边沿交互；
   - 它属于 env 内部装配的一部分，不建议在 testcase 层自行扩展。

2. `env.add_after_step_callback(callback)`
   - 这是 Python 层的拍后回调；
   - 保证在 `_step_async()` 一拍完整结束后执行；
   - 适合 replay/debug/coverage 采样。

可以把这两类 callback 理解为：

- `StepRis` 更靠近 DUT 边沿；
- `after_step_callback` 更靠近“env 语义上的拍后稳定点”。

绝大多数验证侧扩展，都应该优先使用 `after_step_callback`，而不是再去碰 DUT 级回调。

## 5. 对外接口分层

### 5.1 testcase 可直接使用的同步接口

当前允许 testcase 直接使用的 clock-related 公共接口主要包括：

- `env.reset(cycles=10, settle_cycles=5)`
  - 同步复位入口；
- `env.advance_cycles(cycles=1)`
  - 唯一公开的显式拍推进入口；
- `env.wait_until(predicate, max_cycles, timeout_message)`
  - 统一条件轮询入口；
- `env.add_after_step_callback(callback)` / `env.remove_after_step_callback(callback)`
  - 拍后采样挂载点。

其中：

- `env.advance_cycles()` 是“我明确要让 env 再前进若干拍”；
- `env.wait_until()` 是“我想让 env 用统一拍语义轮询某个条件”；

这三个接口的定位不同，不建议混用成一个“大杂烩习惯”。

### 5.2 env / agent 内部应使用的 async 原语

以下接口更偏 env 内部与 agent 内部使用：

- `_step_async(cycles=1)`
  - 真正的一拍语义栈；
- `_await_cycles(cycles=1)`
  - 只是等待若干拍，语义上比 `_step_async()` 更轻；
- `_await_until(predicate, max_cycles, timeout_message)`
  - env 内部 async 轮询入口；
- `_step_and_idle_async(cycles=1)`
  - 推进若干拍后恢复输入 idle，适合“一拍脉冲后清空驱动”的路径。

这些接口的共同特点是：

- 需要被 `_run_async()` 桥接到同步世界；
- 适合 agent/facade 封装内部时序；
- 不建议暴露给 testcase 直接当原语使用。

### 5.3 兼容 API 与推荐 API

当前环境中，旧的兼容 clock API 已删除；当前应直接使用：

- `env.reset(...)`
- `env.advance_cycles(...)`
- `request_apis.reset_env_and_wait_backend(...)`

新代码的推荐顺序是：

1. 先选 sequence；
2. 再选 `env.backend` / `request_apis`；
3. 最后才在 testcase 中直接写 `env.advance_cycles(...)`。

## 6. 当前环境中的典型 clock usage 模式

### 6.1 最底层 smoke：显式 `advance_cycles()`

`src/test/python/MemBlock/tests/test_MemBlock_env_fixture.py` 里保留了若干最底层用法，这是有意为之，因为这些测试的目标就是验证 env 本身的时钟推进和 bundle 绑定是否可用。

典型模式如下：

```python
env.reset(cycles=2, settle_cycles=5)
env.advance_cycles(3)
env.reset(cycles=2, settle_cycles=1)
env.advance_cycles(2)
```

这类写法适合：

- env 自测；
- fixture smoke；
- 调试 bundle 是否被成功驱动。

但它不应该成为大多数功能用例的默认风格。

### 6.2 语义化 settle：`advance_cycles()`

在 sequence 中，当前更推荐用 `env.advance_cycles()` 表达“再过几拍”的语义。例如 `ScalarStoreCommitSequence` 中：

```python
env.backend.pulse_store_commit(1)
if settle_cycles > 0:
    env.advance_cycles(settle_cycles)
```

这里比直接把“推进若干拍”散落写在本地循环里更清楚，因为调用者表达的是：

- 先给一个 store commit 脉冲；
- 再让环境按统一语义 settle 若干拍。

类似模式还出现在：

- `src/test/python/MemBlock/sequences/memblock_sequences.py`
- `src/test/python/MemBlock/sequences/violation_sequences.py`

当你看到“commit 后等几拍”“观测某事件后再等几拍”的语义时，优先想到 `advance_cycles()`。

### 6.3 条件等待：`wait_until()` 与专用 wait helper

当前环境已经把大量拍级轮询收敛成统一 wait helper。例如：

- `env.wait_backend_reset_deassert(...)`
- `env.wait_store_materialized(...)`
- `env.wait_memory_quiesce(...)`
- `env.wait_load_writeback_observed(...)`
- `env.wait_until(...)`

如果你只是要做条件轮询，推荐优先级是：

1. 先找是否已有语义化 helper；
2. 没有的话用 `env.wait_until(...)`；
3. 最后才考虑在 env 内部新增 `_await_until(...)` 风格封装。

不要在 testcase / sequence / request API 里重新写：

```python
for _ in range(max_cycles):
    if cond():
        return value
    env.advance_cycles(1)
```

因为这种写法一旦散落开来，就会让 clock semantics 重新变成“每个人各写各的 while 循环”。

### 6.4 一拍握手后恢复 idle：`_step_and_idle_async()`

在 agent 内部，load enqueue 是一个非常典型的“一拍握手后清空输入”模式。当前 `LsqAgent` 已经把它写成：

```python
await self.env._step_and_idle_async(1)
```

这比手工展开成：

```python
await self.env._step_async(1)
self.env.idle_inputs()
```

更不容易出错，也让“这个动作本质上是 pulse once and go idle”更清晰。

如果你在 env/agent 中遇到类似模式，应优先复用 `_step_and_idle_async()`。

### 6.5 after-step 采样：debug / replay / coverage 的统一入口

当前环境里已经有多种真实用法证明 `after_step_callback` 是当前最干净的采样接口：

1. `src/test/python/MemBlock/tests/test_MemBlock_env_fixture.py`
   - 用于验证同步/异步 callback 都能在拍后执行；
2. `src/test/python/MemBlock/sequences/memblock_sequences.py`
   - 用于捕获 load debug trace；
3. `src/test/python/MemBlock/sequences/violation_sequences.py`
   - 用于捕获 replay event、RAW/RAR query trace；
4. `src/test/python/MemBlock/MemBlock_env.py`
   - fixture 默认把 ROB coverage sample 挂在 after-step callback 上。

这一模式的优势是：

- 采样时机统一；
- 不需要侵入业务驱动路径；
- 可同步可异步；
- 不需要 monkey-patch env 的公开拍推进接口。

因此，如果你需要新增一个“每拍都看一眼”的观测器，首选应该是：

```python
env.add_after_step_callback(sample)
try:
    ...
finally:
    env.remove_after_step_callback(sample)
```

## 7. 最佳实践：结合当前测试/sequence 的写法

下面给出几种推荐模式，都是基于当前仓库里已经存在的测试风格提炼出来的。

### 7.1 最佳实践一：reset 不要手写循环，优先走 `ResetEnvSequence`

如果你的 testcase 不是在测 env 自己，而是在测 load/store/replay 语义，那么最推荐的入口通常是：

```python
state = ResetEnvSequence(
    require_issue_lanes=(0,),
    require_lq_ready=True,
).run(env)
```

它内部已经完成：

- `env.reset(...)`
- `wait_backend_reset_deassert(...)`
- issue lane ready / LQ ready / SQ ready 的必要检查

这个模式来自当前大量 sequence/testcase 的统一起点。相比手写：

```python
env.reset(...)
for _ in range(...):
    env.advance_cycles(1)
    ...
```

它更稳定，也更符合环境当前的 clock discipline。

### 7.2 最佳实践二：业务驱动优先走 sequence，其次才是少量 `advance_cycles()`

例如一条标准 load，当前最佳实践不是：

```python
env.backend.send(txn)
env.expect_scalar_load(...)
    env.advance_cycles(...)
```

而是：

```python
ScalarLoadSequence(
    txn,
    expected_completed_loads=1,
    assert_no_outstanding=True,
).run(env)
```

原因是 `ScalarLoadSequence` 已经把以下 clock-related 流程收敛好了：

- 标准发送时序；
- 期望登记时机；
- drain writeback；
- 收尾检查。

对 store 也类似，优先复用 `ScalarStoreCommitSequence`，而不是在 testcase 里重复写 commit pulse + settle。

### 7.3 最佳实践三：拍级观察用 callback，不要把观察逻辑塞进驱动循环

以 replay / nuke / query trace 的捕获为例，当前推荐风格是 context manager + `after_step_callback`：

```python
env.add_after_step_callback(_sample)
try:
    ... run scenario ...
finally:
    env.remove_after_step_callback(_sample)
```

这类写法已经在 `violation_sequences.py` 中大量使用。它的价值在于：

- 驱动逻辑保持干净；
- 采样逻辑可复用；
- trace 窗口边界清晰。

如果你把观察逻辑直接塞进“推进一拍再检查一次”的本地循环，通常意味着这段逻辑应该被下沉或重构。

### 7.4 最佳实践四：agent 内部使用 async 握手，同步接口只做桥接

以 `LsqAgent` 和 `IssueAgent` 为例，当前推荐模式是：

- async 私有方法：真正描述握手与等待；
- 同步 public 方法：只负责 `self.env._run_async(...)` 桥接。

这个模式有两个好处：

1. agent 可以自然表达“等 ready -> drive -> step -> idle”这类流程；
2. 对 testcase/sequence 来说仍然保留同步 API，不需要把整个环境改成 async 风格。

如果未来新增 agent 接口，也建议沿用这一模式。

### 7.5 最佳实践五：显式拍推进统一写成 `advance_cycles()`

当前环境不再保留 `env.Step()`；如果你确实需要在 testcase 中显式推进若干拍，应统一写成 `env.advance_cycles()`，它仍然适合以下场景：

- env smoke；
- 最小调试；
- 快速人工验证某个端口是否活着。

但“允许存在”不等于“适合作为默认抽象”。

一个简单判断标准是：

- 如果你写的是环境自测，直接 `advance_cycles()` 很正常；
- 如果你写的是业务场景验证，优先 sequence / wait helper / backend facade；
- 如果你发现自己在 testcase 里开始写第三个“推进一拍再检查”的本地循环，通常说明抽象层选错了。

## 8. 常见反模式

以下写法应尽量避免继续新增：

1. 在 `agents/` 中直接写新的公开拍推进 wrapper
2. 在 `request_apis.py` 中新增拍级 while/for 轮询
3. 在 `sequences/` 中为了等条件，手工复制“推进一拍 + 检查条件”的本地循环
4. 用 monkey-patch env 的拍推进入口做采样或 coverage
5. 在 testcase 本地拼出“pulse + settle + 条件轮询”的临时 helper，而不下沉到 env 或 sequence

这些反模式的共同问题是：

- 时序语义无法统一；
- callback/monitor/model 的观察相位容易错乱；
- 后续别人很难判断这段轮询是否与 env 其它路径一致。

## 9. 新功能应该落在哪一层

如果你后续还要新增与时钟相关的能力，建议按下面顺序判断落点：

1. 这是纯条件等待吗？
   - 先考虑 `env.wait_until()` 或现有专用 wait helper。
2. 这是一个稳定的 env 语义吗？
   - 下沉成 `MemBlockEnv` 的公共 helper。
3. 这是单通道握手吗？
   - 放到对应 agent，内部使用 async 原语。
4. 这是多个 agent 的组合动作吗？
   - 放到 `env.backend`。
5. 这是完整测试故事吗？
   - 放到 `sequences/`。
6. 这是 env 自身最小 smoke 吗？
   - 可以在测试中直接 `advance_cycles()`。

只要沿着这条判断链扩展，clock control 的所有权就不会再次散落。

## 10. 小结

当前 MemBlock 环境的 clock control，本质上已经从“允许任何层直接 `Step()` 的松散方式”，收敛成了“env 独占推进、agent 通过 async 原语等待、高层通过语义化 facade 使用”的统一模型。

真正需要记住的只有三条：

1. `dut.Step(1)` 只应该出现在 `EnvClockKernel.step()`。
2. 高层优先写 sequence / backend facade / wait helper，不要重复写拍级轮询。
3. 拍后观测统一挂 `after_step_callback`，不要 monkey-patch `env.Step()`。

如果未来继续按这三条原则维护，当前环境的 clock semantics 就能保持稳定，新的测试与功能也更容易复用已有抽象。
