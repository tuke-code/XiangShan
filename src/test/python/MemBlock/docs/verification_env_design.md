# MemBlock 验证环境设计

## 1. 设计目标

`src/test/python/MemBlock/` 下的验证环境围绕 `MemBlockEnv` 展开，目标是为真实 DUT 提供一个可直接驱动、可在线校验、可扩展的 Python 闭环测试平台。

环境解决的问题主要有四类：

1. 统一绑定 MemBlock 常用输入输出端口，避免测试用例直接操作零散信号。
2. 补齐外部依赖，包括 CSR、outer buffer、dcache client 等最小 mock 行为。
3. 提供稳定的时钟推进与复位语义，保证测试侧软件状态与 DUT 内部状态同步。
4. 将 load/store 校验逻辑集中到 `MemoryModel`，避免每个测试重复实现黄金模型。

## 2. 顶层结构

顶层入口分为两层：

- `MemBlock_api.py`
  - 创建 DUT。
  - 配置波形和覆盖率输出。
  - 暴露 `dut` fixture 以及 `api_MemBlock_reset`、`api_MemBlock_step`。
- `MemBlock_env.py`
  - 定义 Bundle 封装。
  - 定义 `PendingPtrDriver`、`MockCSRInterface`。
  - 定义顶层 `MemBlockEnv` fixture。
  - 将 `MemoryModel` 注册到时钟上升沿回调。

整体关系可概括为：

```text
pytest test
  -> env fixture
    -> dut fixture
      -> create_dut()
    -> MemBlockEnv(dut)
      -> bind input/output bundles
      -> create MemoryModel
      -> dut.StepRis(memory.on_memory_edge)
```

## 3. Bundle 分层

### 3.1 前端驱动输入

这部分输入由测试侧主动驱动，负责向 DUT 注入请求和控制事件。

- `RedirectBundle`
  - 对应 `io_redirect_*`。
  - 当前主要提供空闲值驱动，便于测试保持无重定向默认态。
- `LsqEnqMetaBundle`
  - 对应 `io_ooo_to_mem_enqLsq_*` 元信息。
  - 关键字段是 `canAccept` 和 `needAlloc[#]`。
- `LsqEnqReqBundle` / `LsqEnqRespBundle`
  - 对应 `enqLsq` 的请求与分配返回。
  - 测试用例通过它显式分配 `lqIdx/sqIdx`。
- `IntIssueBundle`
  - 对应 7 路整数 issue 输入。
  - load/store 地址和数据 issue 都通过这里进入 DUT。

### 3.2 观测输出

这部分信号由 DUT 输出，环境负责读取和汇总。

- `IntWritebackBundle`
  - 封装 `io_mem_to_ooo_intWriteback_*`。
  - 使用可选信号绑定方式，兼容某些 DUT 导出不完整的情况。
  - `MemoryModel` 用它观测 load 写回。
- `MemStatusBundle`
  - 封装 `mem_to_ooo` 状态口。
  - 关键字段包括 `lqDeq`、`sqDeq`、`sbIsEmpty`、`memoryViolation_*`。
- `LsqStatusBundle`
  - 封装 `io_mem_to_ooo_lsqio_*`。
  - 关键字段包括 `lqCanAccept`、`sqCanAccept`、`mmio[#]`。

### 3.3 Store 内部观测口

这些接口不直接驱动 DUT 功能，而是为在线观测和离线一致性校验服务。

- `StoreDataInputBundle`
  - 观测 `STD` 送入 store data 的事件。
- `StoreAddrInputBundle`
  - 观测 `STA` 送入物理地址、miss、NC 属性。
- `StoreMaskInputBundle`
  - 观测 store mask。
- `StoreAddrReInputBundle`
  - 观测重送后的 `mmio`、`hasException`、`memBackTypeMM`。
- `SbufferWriteBundle`
  - 观测 sbuffer drain 事件。
- `StoreQueueShadowEntry`
  - 观测 SQ shadow 中每个槽位的 `allocated/addrvalid/datavalid/committed/completed` 等状态。

### 3.4 外围接口

- `OuterTLABundle` / `OuterTLDBundle`
  - 表示 MemBlock 对外发起的 uncache/outer TileLink 请求和响应。
- `DcacheClientABundle/B/C/D/E`
  - 表示 cacheable 路径上的 dcache client TileLink-C 通道。
- `TlbCsrBundle` / `CsrCtrlBundle`
  - 为 CSR 和地址翻译相关输入提供统一绑定入口。

## 4. `MemBlockEnv` 组成

`MemBlockEnv.__init__()` 完成以下初始化：

1. 保存 `dut`。
2. 创建 `PendingPtrDriver`，用于维护 `io_ooo_to_mem_lsqio_pendingPtr`。
3. 绑定 redirect、CSR、LSQ enqueue、issue、writeback、mem status、lsq status 等 Bundle。
4. 绑定 outer 和 dcache client 接口。
5. 创建 `MemoryModel`，并把 writeback/store 观测口传给它。
6. 将 `memory.on_memory_edge` 注册到 `dut.StepRis(...)`。
7. 初始化默认输入值，例如 `reset=0`、`hartId=0`，随后调用 `idle_inputs()`。

因此测试看到的 `env` 实际上是一个带状态机的测试平台，而不是单纯的端口集合。

## 5. 时钟推进语义

### 5.1 `idle_inputs()`

`idle_inputs()` 每次会恢复环境管理的输入默认值：

- CSR 恢复到默认 M-mode。
- redirect 清零。
- 所有 `enqLsq` 请求置 idle。
- 所有 issue lane 置 idle。
- `flushSb`、`sfence` 等控制口清零。
- `MemoryModel` 输出口恢复 idle。
- `PendingPtrDriver` 重新驱动当前 pending 指针值。

这个函数的意义是保证测试按拍驱动时不会把旧输入残留到下一拍。

### 5.2 `Step(cycles)`

`MemBlockEnv.Step()` 是环境最重要的同步点。单拍行为顺序为：

1. 先驱动当前 `pendingPtr`。
2. 调用 `dut.Step(1)` 推进一个时钟。
3. 调用 `memory.after_cycle()`，观测 writeback、store shadow 和 drain 事件。
4. 若 DUT 或 backend 处于 reset，则同步清空 `pending_ptr` 侧状态。
5. 读取 `mem_status.lqDeq`，通知 `MemoryModel.note_load_commits(...)`。
6. 读取当拍完成的 load ROB，更新 `PendingPtrDriver` 的完成队列。
7. 调用 `pending_ptr.advance()`，把可推进的提交边界前移。

这说明环境把 DUT 周期推进和软件侧“提交视图推进”绑定在了一起。

### 5.3 `reset(cycles, settle_cycles)`

`reset()` 的语义不是简单拉高 `dut.reset`，而是一次完整的软件/硬件同步复位：

1. 先 `idle_inputs()`。
2. 清空 `MemoryModel` 运行态。
3. 清空 `PendingPtrDriver` 状态。
4. `dut.reset=1` 并推进 `cycles`。
5. `dut.reset=0`。
6. 再次 `idle_inputs()`。
7. 推进 `settle_cycles`，等待环境恢复稳定。

`request_apis.reset_env_and_wait_backend()` 还会额外等待 `io_reset_backend` 先拉高再拉低，确保 backend 子系统也完成同步复位。

## 6. `PendingPtrDriver` 的角色

`PendingPtrDriver` 维护 `io_ooo_to_mem_lsqio_pendingPtr` 与 `io_ooo_to_mem_lsqio_scommit`，其作用是为 load 提交边界提供一个软件侧可控、与 ROB 顺序一致的近似模型。

关键机制：

- `note_issued()`
  - 记录每一笔已发出的 load ROB。
- `note_completed()`
  - 记录某个 ROB 对应的 load writeback 已完成。
- `advance()`
  - 仅当 issued 队列头部 ROB 已完成时，才把 pending 指针前移。
- `queue_store_commit()`
  - 为下一个周期发送 `scommit` 脉冲。

这个设计保证了：

- younger load 不能越过 older unfinished load 推进 pending 指针。
- store commit 信号由测试显式控制，便于构造 store/load 顺序场景。

## 7. 请求 API 设计

`request_apis.py` 封装了真实 DUT 常用流量注入模式，避免测试直接逐位操作端口。

### 7.1 指针与复位

- `QueuePtr`
  - 测试侧维护的环形队列指针。
- `ptr_inc()`
  - 负责按队列大小做带 flag 的环回。
- `reset_env_and_wait_backend()`
  - 标准用例起始流程，先 reset，再检查 ready 状态。

### 7.2 enqueue API

- `enqueue_scalar_load()`
  - 等待 `lqCanAccept` 与 `canAccept`。
  - 驱动 `enqLsq` 请求，并在下一拍清空输入。
- `enqueue_scalar_store()`
  - 等待 `sqCanAccept` 与 `canAccept`。
  - 发起 store enqueue，并从响应侧读取真实分配到的 `sqIdx`。

### 7.3 issue API

- `issue_scalar_load()`
  - 通过 issue lane 注入 load 地址及元数据。
  - 成功握手后调用 `env.note_load_issued(...)`。
- `issue_scalar_sta()`
  - 注入 store 地址。
- `issue_scalar_std()`
  - 注入 store 数据。

内部公共函数 `_issue_until_fire()` 会循环等待 `issue.ready`，从而避免用例手写 ready 轮询。

## 8. 典型测试流

### 8.1 Load 路径

`tests/test_MemBlock_random_load.py` 代表了 load 用例的推荐写法：

1. `reset_env_and_wait_backend()`。
2. 使用 `env.memory.preload_u64()` 预置黄金内存。
3. `enqueue_scalar_load()` 申请 LSQ 资源。
4. `issue_scalar_load()` 发起地址请求。
5. `env.memory.expect_load()` 登记期望结果。
6. `env.drain_writebacks()` 等待事务和在线 compare 收敛。
7. `env.check_no_outstanding_transactions()` 检查无残留事务。

### 8.2 Store 路径

`tests/test_MemBlock_random_store.py` 展示了 store 用例的两类主路径：

- MMIO store
  - `enqueue_scalar_store()` -> `issue_scalar_std()` -> `issue_scalar_sta()`
  - 观察 `pending_stores` 中 materialized 的 shadow 信息
  - `env.pulse_store_commit(1)` 触发提交
  - 验证 outer 写请求增长且不经过 sbuffer
- Cacheable store
  - 同样先完成 enqueue/STD/STA
  - 验证 store 进入 committed
  - 最终通过 `env.flush_store_buffers_and_wait()` 等待 sbuffer drain 收敛并做一致性校验

## 9. 测试组织

当前 `tests/` 下的测试大致分为三层：

- `test_MemBlock_env_fixture.py`
  - 环境自身冒烟测试。
  - 验证 Bundle 数量、ready 驱动、idle/reset 行为。
- `test_memory_model_store_logic.py`
  - 纯 Python 单元测试。
  - 直接验证 `MemoryModel` 的关键语义，不依赖真实 DUT。
- `test_MemBlock_random_load.py` / `test_MemBlock_random_store.py`
  - 真实 DUT 级用例。
  - 用环境 API 驱动 load/store 主路径，并通过 `MemoryModel` 完成在线或结尾校验。

这样的分层带来两个收益：

1. 环境 bug 能先在纯 Python 或 fixture 冒烟层被隔离出来。
2. DUT 级场景只关心业务流，而不需要重复实现环境逻辑。

## 10. 扩展建议

当需要扩展新的验证能力时，建议遵守以下边界：

- 新的外设/总线行为优先补到 `MemoryModel`，而不是散落到测试里。
- 新的驱动序列优先补到 `request_apis.py`，保持测试描述简洁。
- 新的可选调试信号优先采用 `OptionalSignalBundle` 风格，避免因 DUT 导出差异导致环境失效。
- 若新增提交或回滚相关时序，优先检查 `PendingPtrDriver` 与 `Step()` 顺序是否仍然成立。

## 11. 当前限制

当前环境已有明确边界，编写新用例时需要注意：

1. `MemoryModel` 当前主要围绕标量 load/store 路径建模。
2. `wline` 类型的 sbuffer drain 尚未支持校验。
3. `MockCSRInterface` 默认固定为非虚拟化 M-mode，只覆盖最小功能闭环。
4. 很多断言建立在“测试先登记期望，再等待收敛”的约定上；如果绕过标准 API，容易造成未登记 writeback 断言。
