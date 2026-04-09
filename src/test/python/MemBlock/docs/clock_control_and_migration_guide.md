# MemBlock 时钟控制与迁移指南

## 1. 文档目的

本文档面向维护 `src/test/python/MemBlock/` 的开发者，说明当前环境的时钟控制原则，以及当你扩展 env、request API、sequence 或 testcase 时，应该如何避免重新把 `Step()` 调用打散回各层。

它不重复讲整个环境分层设计；整体架构请看：

- `verification_env_design.md`

本文只回答三个更直接的问题：

1. 现在到底谁拥有 DUT 时钟推进权？
2. 各层应该用什么 API，而不是自己 `Step()`？
3. 旧代码如果还带着轮询 `Step()`，应如何迁移？

## 2. 核心原则

当前 MemBlock 环境遵循以下原则：

1. `dut.Step(1)` 只允许出现在 `MemBlockEnv` 的时钟内核里。
2. env 内部的 wait / pulse / settle 应统一走 env 原语，而不是各自本地 `Step()`。
3. `request_apis.py`、`sequences/`、tests 应优先调用 env facade 或 backend facade，不要在高层复制拍级轮询。
4. 保留 `env.Step()` 作为兼容同步入口，但它不再是推荐的高层建模方式。

一句话概括就是：

```text
高层描述行为语义，env 独占时钟推进。
```

## 3. 各层推荐用法

### 3.1 env 内部

如果你在 `MemBlock_env.py` 中新增功能，优先复用这些原语：

- `_step_async()`
  - 需要真正推进时钟时使用；
  - 它会统一串起 `commit_agent.drive()`、DUT 拍推进、`memory.after_cycle()`、`MemStatusMonitor.after_cycle()`、`CommitAgent.advance()` 和拍后 callback。
- `_await_cycles()`
  - 只是“再过几拍”，没有别的语义。
- `_await_until(predicate, max_cycles, timeout_message)`
  - 条件轮询统一入口。
- `_step_and_idle_async()`
  - 一拍握手后立即恢复默认输入，例如 enqueue 这类一次性驱动。

env 内新增的同步 public helper，应该尽量只是这些 async 原语的薄包装。

### 3.2 `agents/`

`agents/` 的职责是“驱动什么”和“等待什么”，不是“自己如何推进时钟”。

因此 agent 代码里推荐：

- 握手主路径写成 async 协程；
- 通过 `await self.env._step_async(...)` 或更高层的 env 原语等待；
- 同步 public 方法只做 `self.env._run_async(...)` 桥接。

不推荐：

- 在 agent 内部自己写 `env.Step(1)` 轮询；
- 把一段业务语义拆成多个本地拍级小循环。

### 3.3 `request_apis.py`

`request_apis.py` 是 testcase/sequence 之下的兼容薄层，推荐只做“语义转发”，例如：

- backend reset 等待 -> `env.wait_backend_reset_deassert(...)`
- enqueue / issue -> `env.backend.*`
- reset -> `env.reset(...)`

不推荐在这里保留新的 `for ... env.Step(1)` 轮询。

### 3.4 `sequences/`

sequence 层应该描述场景语义，而不是 pin-level 时钟细节。当前推荐优先使用：

- `env.backend.pulse_store_commit(...)`
- `env.advance_cycles(...)`
- `env.wait_until(...)`
- `env.wait_store_addr_observed(...)`
- `env.wait_store_materialized(...)`
- `env.wait_completed_load_count(...)`
- `env.wait_memory_quiesce(...)`
- `env.flush_store_buffers_and_wait(...)`

这意味着 sequence 可以表达：

- “commit 后再 settle 4 拍”
- “等待 store 地址 materialize”
- “等待 completed load 数增长到 N”

但不需要自己实现拍级轮询器。

### 3.5 tests

testcase 仍然允许直接使用：

- `env.Step()`
- `env.reset()`

因为它们在环境冒烟和最小调试里仍然很有价值。

但在真实场景用例里，更推荐优先复用 sequence 或语义化 wait helper，而不是在 testcase 本地堆 `env.Step(1)`。

## 4. 迁移模式

### 4.1 条件轮询

旧写法：

```python
for _ in range(max_cycles):
    if cond():
        return value
    env.Step(1)
raise TimeoutError(...)
```

推荐迁移为：

```python
return env.wait_until(
    lambda: value if cond() else None,
    max_cycles=max_cycles,
    timeout_message="...",
)
```

如果语义已经稳定，优先直接下沉成 env 专用 helper，例如：

- `wait_store_addr_observed()`
- `wait_completed_load_count()`

### 4.2 单纯 settle 若干拍

旧写法：

```python
if settle_cycles > 0:
    env.Step(settle_cycles)
```

推荐迁移为：

```python
if settle_cycles > 0:
    env.advance_cycles(settle_cycles)
```

这里的重点不是功能差异，而是让调用方语义更清楚：这是“让 env 再前进几拍”，不是直接接管时钟细节。

### 4.3 backend reset 等待

旧写法：

```python
for _ in range(max_cycles):
    ...
    env.Step(1)
```

推荐迁移为：

```python
env.wait_backend_reset_deassert(
    must_observe_assert=True,
    max_cycles=max_cycles,
)
```

### 4.4 一拍握手 + 恢复 idle

这类模式不要在高层重复写：

```python
drive_valid()
env.Step(1)
env.idle_inputs()
```

如果这是 env/agent 内部行为，应改成 env 原语或 agent async 握手协程，例如 `_step_and_idle_async()`。

## 5. 反模式清单

以下写法应尽量避免继续新增：

1. 在 `agents/` 中直接 `env.Step(...)`
2. 在 `request_apis.py` 中写新的拍级 while/for 轮询
3. 在 `sequences/` 中为了等条件而自己复制轮询 `Step()`
4. 用 monkey-patch `env.Step` 的方式做观测
5. 在 testcase 本地直接拼一段临时 pulse + settle helper，而不是下沉到 env

其中第 4 点尤其要避免。现在如果你需要拍后观测，应优先使用：

- `env.add_after_step_callback(...)`
- `env.remove_after_step_callback(...)`

而不是劫持 `env.Step`。

## 6. 当前迁移完成情况

当前已经完成的收口包括：

- `MemBlockEnv` 内部 wait/pulse 路径
- `IssueAgent`
- `LsqAgent`
- `BackendFacade.step_commit()`
- `request_apis.py`
- `sequences/memblock_sequences.py`
- `sequences/violation_sequences.py`

因此，当前 `request_apis.py` 与 `sequences/` 已经不再保留显式 `env.Step(...)` 调用。

## 7. 后续扩展建议

如果后续还要新增新的 env 能力，建议按下面顺序判断落点：

1. 这是纯条件等待吗？
   - 先考虑 `env.wait_until()`
2. 这是一个稳定场景语义吗？
   - 下沉成专用 env helper
3. 这是单条驱动通道的握手吗？
   - 放到对应 agent
4. 这是多个 agent 的组合动作吗？
   - 放到 `env.backend`
5. 这是完整测试故事吗？
   - 放到 `sequences/`

只要保持这条判断链，时钟推进权就不会再次散落回高层。
