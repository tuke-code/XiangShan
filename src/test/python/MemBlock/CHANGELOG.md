# MemBlock Python Verification Environment CHANGELOG

## 2026-04-01

本条目记录 `src/test/python/MemBlock/` Python 验证环境在 2026-04-01 这一轮分层重构中的主要变化。它和 `docs/DUT_CHANGELOG-20260331.md` 的定位不同：后者主要记录 RTL 版本变化及其对验证的影响，这份 changelog 记录的是验证环境自身的架构演进。

### 变更背景

在本次重构前，验证环境已经可以稳定支撑 MemBlock 的真实 DUT load/store 主路径回归，但整体组织仍然偏向单体实现：

- `MemBlockEnv` 同时持有 bundle 绑定、默认输入值、请求驱动、环境控制和状态查询。
- `MemoryModel` 同时持有黄金内存、outer/dcache responder、load compare、store shadow 与 drain 收尾逻辑。
- testcase 大量直接依赖 `env.memory.pending_stores`、`env.memory.completed_loads`、`env.memory.*count` 等内部字段。
- `request_apis.py` 以函数式 helper 为主，缺少稳定的事务对象。

这套结构在功能较小时足够直接，但随着 DUT 接口变化、测试数量增加、以及后续要继续引入 monitor/scoreboard/sequence，维护成本会快速升高。因此本轮重构的核心目标是先建立清晰边界，而不是一次性完成所有理想分层。

### 已完成的阶段

#### 1. 添加 env facade 与事务 helper

commit: `ef0791a5d`

主要改动：

- 为 `MemBlockEnv` 增加公开 facade：
  - `preload_u64`
  - `expect_scalar_load`
  - `get_transport_stats`
  - `get_completed_load_count`
  - `get_counter`
  - `get_store_view`
  - `wait_store_materialized`
  - `wait_counter_growth`
  - `wait_memory_quiesce`
  - `assert_no_outstanding`
- 新增 `StoreView`，对外暴露 store 只读视图。
- 新增 `transactions.py`，定义：
  - `QueuePtr`
  - `LoadTxn`
  - `StoreTxn`
- 在 `request_apis.py` 中新增事务接口：
  - `send_load`
  - `expect_load`
  - `send_store`
- 将真实 DUT tests 从直接访问 `env.memory` 内部容器迁移到 facade / transaction 风格接口。

这一步的核心收益是：testcase 开始依赖行为接口，而不是内部实现细节，为后续继续拆 `MemoryModel` 打下了基础。

#### 2. 拆分 active driver agents

commit: `a316e3b46`

主要改动：

- 新增 `agents/` 目录。
- 新增 active agents：
  - `csr_agent.py`
  - `commit_agent.py`
  - `lsq_agent.py`
  - `issue_agent.py`
- `MemBlockEnv` 现在显式组装这些 agent。
- `request_apis.py` 中原先直接驱动 pin 的 enqueue / issue 逻辑下沉到 agent。
- `PendingPtrDriver` 仍保留在 `MemBlock_env.py`，但它的使用入口转为 `CommitAgent`。
- 原先的 `MockCSRInterface` 职责被 `CsrAgent` 承接，env 不再直接持有一坨 CSR 初始化细节。

这一步完成后，`MemBlockEnv` 的角色从“万能控制器”进一步收敛成“顶层组装 + 环境协调 + facade”。这也是后续向 monitor/scoreboard/sequence 继续演进的前提。

#### 3. 提取 `RefMemory`

commit: `bbeca071f`

主要改动：

- 新增 `model/` 目录。
- 新增 `model/ref_memory.py`。
- 将黄金内存相关逻辑从 `MemoryModel` 中拆出，包括：
  - `preload_bytes`
  - `preload_u64`
  - `fill_random`
  - `read`
  - `read_masked`
  - `read_cacheline`
  - `apply_masked_write`
- `MemoryModel` 改为组合 `RefMemory`，同时保留原有 facade，以降低上层改动范围。
- 增加 `RefMemory` 的独立单元测试。

这一步的意义在于：黄金内存不再和 responder、compare、shadow 观测逻辑混在一起。后续无论是 responder 还是 scoreboard，都可以共享 `RefMemory` 提供的统一视图。

#### 4. 提取 `TransportResponder`

commit: `58341892a`

主要改动：

- 新增 `model/transport_responder.py`。
- 将 outer/dcache 请求响应与传输计数器从 `MemoryModel` 中拆出，包括：
  - outer TL-A/TL-D 闭环
  - dcache A/B/C/D/E 闭环
  - 响应延迟建模
  - outstanding 事务统计
  - outer put 对 `drain_log` 的记录
- `MemoryModel` 改为组合 `TransportResponder`，并通过 facade 暴露兼容接口：
  - `stats`
  - `outstanding_transaction_count`
  - `enqueue_outer_response`
  - `enqueue_b_response`
  - `enqueue_d_response`
  - 若干 transport counter property

这一步的意义是：传输响应器和 compare / store retire 逻辑已经不再是一个不可分割的整体。后续如果 outer/dcache 路径回归异常，可以更明确地区分是 responder 问题还是 compare 问题。

#### 5. 提取 `Scoreboard`

commit: `d5c281507`

主要改动：

- 新增 `model/scoreboard.py`。
- 将 `MemoryModel` 中剩余的 load compare、ROB 边界下的 store retire、drain 收尾校验独立出来。
- `MemoryModel` 退化为 `RefMemory + TransportResponder + Scoreboard` 的组装层，并保留兼容 facade。
- 增加 `Scoreboard` 的独立单测入口，验证它可以脱离 transport 单独检查 load compare 语义。

这一步完成后，参考检查逻辑不再和传输响应器粘在一起。后续无论是扩 monitor 还是调整 compare 策略，边界都更稳定。

#### 6. 引入 passive monitors

commit: `6d5b22be5`

主要改动：

- 新增 `monitors/` 目录。
- 新增：
  - `writeback_monitor.py`
  - `store_monitor.py`
  - `mem_status_monitor.py`
- 将原先 `Scoreboard`/`MemBlockEnv` 中直接读取 DUT 输出并更新状态的逻辑下沉到 monitor。
- `Scoreboard` 改为只接收事件并维护检查状态，不再直接依赖 DUT 端口命名。

这一步的意义是明确“观测事实”和“判断结果”的边界。今后如果 DUT 导出口名、shadow 结构或 status 位发生变化，更大概率只需要改 monitor。

#### 7. 引入 sequence 层

commit: `fa9a9eafa`

主要改动：

- 新增 `sequences/` 目录。
- 新增可复用 sequence：
  - `ResetEnvSequence`
  - `ScalarLoadSequence`
  - `ScalarStoreSequence`
  - `FlushStoreBuffersSequence`
- 将典型真实 DUT 用例迁移为 sequence 驱动，而不是在 testcase 中手工拼接 helper。

这一步的核心收益是让测试场景开始成为一等对象。testcase 的关注点转移到“场景和断言”，而不是“每个 helper 该按什么顺序调用”。

#### 8. 收敛统一 `EnvConfig`

主要改动：

- 新增 `env_config.py`。
- 引入 `EnvConfig`、`TransportConfig`、`SequenceConfig`。
- 将 queue 深度、ROB 深度、transport 默认延迟、strict check 策略、reset/drain/materialize/flush 默认周期统一收口。
- `MemBlockEnv` 和 sequence 层开始从统一配置读取默认行为，而不是依赖分散常量。

这一步不是追求“所有结构立即完全参数化”，而是先建立统一配置入口，并对当前不能动态改动的结构性参数做显式校验，避免假参数化。

### 本轮重构后环境的总体形态

当前环境已经形成如下分层：

```text
testcase
  -> sequences / request_apis / transactions
    -> MemBlockEnv facade
      -> active agents
      -> passive monitors
      -> MemoryModel
         -> RefMemory
         -> TransportResponder
         -> Scoreboard
      -> DUT
```

这不是终点，但已经完成了核心角色拆分：

1. testcase 与内部实现解耦。
2. `MemoryModel` 不再承担所有职责。
3. 观测、驱动、检查、场景、配置各有明确归属。

### 兼容与约束

本次重构刻意遵循以下约束：

- 不修改现有 load commit-boundary compare 语义。
- 不改变 store flush / drain 的最终校验语义。
- 不一次性删除旧 helper，而是以 facade 和兼容层平滑过渡。
- 优先保证真实 DUT 主路径回归可持续运行。

因此，当前代码里仍然保留了一些“中间态”设计，例如：

- `MemoryModel` 仍作为 model facade 存在，而不是继续下沉成更薄的 assembler。
- `request_apis.py` 仍保留为 sequence 之下的兼容薄层。
- `EnvConfig` 目前对结构性端口宽度仍以校验为主，尚未完全支持动态重参数化。

这些都属于有意保留的过渡层，而不是遗留错误。

### 验证情况

本轮各阶段都使用非自动插件模式执行回归，即：

`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`

原因是默认 `pytest` 在当前环境下会被外部插件 `pytest_rerunfailures` 的 socket 初始化拦住，不是 MemBlock 代码本身的问题。

已验证的关键路径包括：

- `test_memory_model_store_logic.py`
- `test_MemBlock_random_load.py -k single_preloaded_load_data_check`
- `test_MemBlock_random_store.py -k "single_mmio_store_smoke or single_cacheable_store_then_load_same_addr or single_cacheable_store_flush_smoke"`
- `test_MemBlock_env_fixture.py` 中若干关键冒烟子集
- 对所有新增/修改 Python 文件执行 `python -m py_compile`

### 后续演进方向

当前最值得继续推进的方向主要有：

1. 统一 monitor 事件对象
   - 减少 monitor 到 scoreboard 的细粒度方法耦合。

2. 扩展 sequence 组合能力
   - 增加 mixed traffic、异常路径、flush/replay、memory violation 等高层场景。

3. 推进结构性参数化
   - 让当前仍固定的端口宽度与 pipeline 配置逐步纳入真实可配范围。

4. 收紧 legacy 接口
   - 让新 testcase 优先依赖 sequence + env public API，而不是继续堆叠兼容 helper。

### 总结

这次重构的价值，不在于目录看起来更“像 UVM”，而在于环境已经具备长期维护所需的结构化边界：

- 对外接口稳定。
- active driver 已显式分层。
- passive monitor 已独立。
- 黄金内存、传输响应器、scoreboard 已拆开。
- testcase 已开始依赖 sequence，而不是内部容器或散装 helper。
- 默认策略开始由统一 config 管理。

这为后续继续扩异常场景、随机回归和更复杂的检查器提供了可持续的基础。
