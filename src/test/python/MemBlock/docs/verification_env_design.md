# MemBlock 验证环境设计

## 1. 文档目的

本文档说明 `src/test/python/MemBlock/` 当前 Python 验证环境的完整设计形态，重点回答四个问题：

1. 现在的环境分成了哪些层，每层职责是什么。
2. 为什么这些边界要这样切，而不是继续维持单体式实现。
3. 这些实现如何对应 toffee/picker 风格建议，以及 UVM 中 env/agent/monitor/scoreboard/sequence/config 的常见分工。
4. 后续如果继续演进，哪些方向是低风险且有收益的。

与早期版本相比，当前环境已经不再只是“一个大 env + 一个大 memory model + 一堆 helper 函数”的组合，而是形成了明确的五层结构：`testcase -> sequence/request API -> env facade -> agents/monitors -> model components`。这使得环境能够在不改变主回归语义的前提下持续重构。

## 2. 设计目标与约束

本环境面向真实 MemBlock DUT，而不是纯 Python mock。设计时必须同时满足以下约束：

1. 不能破坏现有 load commit-boundary compare 语义。
2. 不能改变 store flush / drain 的最终可见性语义。
3. 要允许 testcase 在较长时间内保持兼容，不能因为内部重构而反复大改用例。
4. 要让行为驱动、被动观测、参考模型、场景组织、环境参数这五类职责逐步分离。

这与 UVM 的基本思想一致：driver 不应该承担 checker 职责，monitor 不应该偷偷写状态，scoreboard 不应该直接读 DUT 端口，sequence 不应该了解 env 内部容器，config 应该作为全局策略入口而不是散落常量。当前实现不是机械地复刻 UVM 命名，而是把这些边界转写成更适合 Python/toffee 生态的结构。

## 3. 当前目录结构

当前目录可概括为：

```text
MemBlock/
  README.md
  CHANGELOG.md
  MemBlock_api.py
  MemBlock_env.py
  env_config.py
  request_apis.py
  transactions.py
  memory_model.py
  agents/
  monitors/
  model/
  sequences/
  tests/
  docs/
```

其中：

- `MemBlock_env.py` 是顶层装配点和对外 facade。
- `env_config.py` 是统一参数入口。
- `agents/` 持有主动驱动 DUT 的逻辑。
- `monitors/` 持有从 DUT 输出采样并发布事件的逻辑。
- `model/` 持有参考模型、传输响应器、scoreboard。
- `sequences/` 持有 testcase 可复用的场景模板。
- `request_apis.py` 是 `env.backend` 之上的薄封装，不再自成一套控制面。

这个目录结构的价值不是“更像某种教科书图”，而是让一个开发者在接手问题时能快速判断该改哪一层。例如：端口驱动问题先看 agent，状态采样问题先看 monitor，load/store 一致性判断先看 scoreboard，场景复用则先看 sequence。

## 4. 顶层架构与数据流

当前推荐的阅读和执行路径是：

```text
pytest testcase
  -> sequences / request_apis
    -> env.backend
      -> MemBlockEnv facade
      -> active agents
      -> passive monitors
      -> MemoryModel
         -> RefMemory
         -> TransportResponder
         -> Scoreboard
      -> DUT
```

从运行时关系看，主要有三条流：

1. 驱动流
   testcase 通过 `ScalarLoadSequence`、`ScalarStoreSequence` 等场景对象发起请求；sequence 再调用 `request_apis.py` 或 `env.backend`，把 enqueue/issue/flush 等动作下沉到 `LsqAgent`、`IssueAgent`、`CommitAgent`、`CsrAgent`。

2. 观测流
   DUT 每拍推进后，`StoreMonitor`、`WritebackMonitor`、`MemStatusMonitor` 分别观测 store shadow、writeback、mem_status 等输出，把事件发布给 scoreboard 或 commit path，而不是由 env 或 scoreboard 直接散读端口。

3. 参考与检查流
   `RefMemory` 提供黄金内存视图；`TransportResponder` 提供 outer/dcache 最小响应器；`Scoreboard` 根据 monitor 发布的事件完成 load compare、ROB 边界下的 store retire，以及 drain 收尾校验。

这三条流被拆开后，问题定位明显更清晰。比如 load 写回错了，先看 sequence 是否登记了期望，再看 writeback monitor 是否采到了正确事件，再看 scoreboard compare 是否按 ROB 边界退休 store，最后才看 DUT。

## 5. `MemBlockEnv` 的职责

`MemBlockEnv` 当前只做三件事：

1. 组装
   创建 bundle、agent、monitor、`MemoryModel`，并把 `memory.on_memory_edge` 注册到时钟回调。

2. 协调
   负责 `Step()`、`reset()`、`idle_inputs()` 等顶层时序编排；同时在每拍末尾调用 `MemStatusMonitor` 与 `CommitAgent.advance()`，维持 pending pointer 和 commit 相关闭环。

3. 对外暴露稳定 facade
   例如 `preload_u64()`、`expect_scalar_load()`、`get_counter()`、`wait_store_materialized()`、`wait_memory_quiesce()`、`flush_store_buffers_and_wait()`、`assert_no_outstanding()`；主动控制入口则统一收口到 `env.backend`。

这相当于 UVM 中 env 的角色，但实现上更轻量。env 不再直接做 pin-level issue，也不再直接承担 compare 逻辑，而是尽量只负责装配、调度和对外接口稳定性。

## 6. Active Agents

当前 active agents 包括：

- `CsrAgent`
- `CommitAgent`
- `LsqAgent`
- `IssueAgent`
- `BackendFacade`

其中 `BackendFacade` 负责统一编排其余 backend-facing agents，并作为 testcase/sequence 的默认主动控制入口。它们共同特点是“主动写 DUT 输入”。这和 monitor 的只读性质形成了明确对照。

这种划分与 UVM driver 的思想一致，但比传统 driver/seq_item 体系更简单：Python 场景对象不直接碰 pin，agent 统一持有时序细节；testcase 只描述“我要发一个 load/store/flush”，不描述“第几拍拉哪个 valid”。这既降低了 testcase 冗余，也避免多个用例各自复制握手时序。

## 7. Passive Monitors

`monitors/` 是本轮重构的关键新增层，包括：

- `WritebackMonitor`
- `StoreMonitor`
- `MemStatusMonitor`

它们的核心原则是：

1. 只读 DUT 输出，不主动驱动业务含义上的输入。
2. 不持有参考模型语义，只把观测到的事实翻译成事件。
3. 不做最终判定，最终判定统一交给 scoreboard 或 env 协调逻辑。

例如：

- `WritebackMonitor` 不判断数据是否正确，只负责把 load/store writeback 事件发布给 scoreboard。
- `StoreMonitor` 不判断 store 是否应该退休，只负责把 SQ shadow、addr/data/mask、sbuffer drain 等事实同步给 scoreboard。
- `MemStatusMonitor` 不决定测试是否通过，只负责把 `lqDeq` 和完成 ROB 传播给 commit/scoreboard 流水。

这个层次的引入，把过去 `MemoryModel` 中“大量直接读 bundle 并更新状态”的做法拆开了。现在 scoreboard 已不直接依赖 DUT 端口命名，因此未来如果 DUT 输出前缀、shadow 结构、或某些 status 信号发生变化，修改成本更多集中在 monitor，而不是扩散到 compare 主逻辑。

## 8. Model 层设计

### 8.1 `RefMemory`

`RefMemory` 负责黄金内存本体，包括 preload、random fill、masked read/write、cacheline read 等。它是 scoreboard 和 transport 的共享基础，不再掺杂时序和端口语义。

### 8.2 `TransportResponder`

`TransportResponder` 负责 outer TL 和 dcache TL-C 的最小闭环、延迟模型、outstanding 统计、grant/release 响应组织。它只关心传输，不关心 load 是否 compare 成功，也不关心 store 是否该退休。

### 8.3 `Scoreboard`

`Scoreboard` 是当前检查逻辑的核心。它负责：

1. 维护待 compare 的 load 期望。
2. 维护已观测 writeback。
3. 按 commit boundary 推进 load compare。
4. 按 ROB 边界退休 older store 到黄金内存。
5. 记录和检查 sbuffer/outer drain 事件。
6. 在测试收尾时执行 `finalize_and_check_drain()`。

它现在已经是独立组件，而不是 `MemoryModel` 的私有实现细节。UVM 语义上，它已经承担 scoreboard + reference state machine 的角色，只是为降低层数仍由 `MemoryModel` 统一装配。

### 8.4 `MemoryModel`

当前 `MemoryModel` 更准确地说是 model facade。它不再自己完成所有逻辑，而是组合：

- `RefMemory`
- `TransportResponder`
- `Scoreboard`
- `WritebackMonitor`
- `StoreMonitor`

它对上保持兼容接口，对下协调 transport、scoreboard 和 monitor 的调用顺序。这种设计很实用：上层 testcase 仍可通过 `env.memory` 访问兼容 API，但内部职责已被切开。

## 9. Sequence 层设计

`sequences/` 目录引入后，testcase 不再需要直接拼接 `send_load()`、`expect_load()`、`wait_store_materialized()`、`flush_store_buffers_and_wait()` 这些细节，而是优先复用场景对象：

- `ResetEnvSequence`
- `ScalarLoadSequence`
- `ScalarStoreSequence`
- `FlushStoreBuffersSequence`
- `ScalarForwardFailReplaySequence`
- `ScalarCacheMissReplaySequence`
- `ScalarNcReplaySequence`
- `ScalarRawReplaySequence`
- `ScalarRarViolationSequence`

这与 UVM sequence 的思想非常接近，但保留了 Python 侧更直接的表达方式。它的主要收益有三点：

1. 时序约束收敛
   例如 reset 后要等待 backend deassert、load 后要 drain、store 后要 wait materialized，这些规则不再散落在各测试文件。

2. 指针推进收敛
   LQ/SQ 指针推进由 sequence 结果统一返回，避免 testcase 重复写 `ptr_inc()` 并手工维护多份常量。

3. 语义清晰
   testcase 更接近“执行一个场景并断言结果”，而不是“手工执行若干 helper 并推测时序是否已经满足”。

当前 sequence 层仍是轻量版本，没有再额外引入 sequencer arbitration、virtual sequence、随机约束类等复杂设施。这是刻意的：在 Python/toffee 环境里，先把高频场景模板化，比过早照搬完整 UVM 术语更有收益。

与之配套，`MemBlockEnv` 现在还对 replay 观测提供了稳定 facade：

- `sample_replay_state()`
- `wait_replay_event()`
- `wait_nc_replay_or_nc_out()`
- `collect_replay_window()`
- `wait_nuke_query_backpressure()`
- `wait_release_event()`
- `wait_rar_nuke_response()`
- `wait_load_writeback_observed()`

这意味着 replay testcase 可以继续保持 “sequence 描述事务 + env 提供观测入口” 的边界，而不必在测试文件里手工拼装 `MemBlock_inner_lsq_*` 的内部信号轮询。

其中：

- `ScalarRawReplaySequence` 负责“older store 长时间未补全 -> younger loads 触发 `RAW` replay/backpressure”的真实 DUT 模板。
- `ScalarRarViolationSequence` 负责“older load 因精确 load-wait 暂停 -> younger same-addr load 先写回旧值 -> probe/release -> `RAR nuke` -> older load 写回新值”的真实 DUT 模板。
- `wait_release_event()` 与 `wait_rar_nuke_response()` 把 `RAR` 场景所需的 release / query-response 观测也收口进 env facade。
- `wait_load_writeback_observed()` 允许 testcase 直接证明某条 load 已经先写回，而不要求它已经穿过 commit-boundary compare，这对 `RAR` 这类“younger 先写回、older 后退休”的场景很重要。

## 10. `EnvConfig` 统一参数入口

`env_config.py` 提供了当前环境的统一配置对象。它的目标不是立即把所有结构都做成完全可参数化，而是先把“运行策略参数”和“高频默认值”收口，包括：

- queue 深度与 ROB 深度
- transport 延迟模型
- strict writeback check 策略
- reset / drain / materialize / flush 等默认周期

当前实现对“结构性参数”和“运行时参数”采取区别对待：

1. 运行时参数可以直接通过 `EnvConfig` 改写并生效。
2. 某些结构性参数仍与 bundle 声明方式绑定，例如固定宽度的 `SignalList`。这类参数目前只允许使用默认值，env 会显式做校验，避免用户以为改了配置就真的改了端口结构。

这是一种工程上更稳妥的中间态：配置先统一，能力再逐步开放，而不是一开始就宣称“完全参数化”却暗含大量无效项。

## 11. testcase 分工建议

当前推荐的 testcase 分工是：

1. testcase
   只描述业务场景、初始化数据、统计断言、最终结果断言。

2. sequence
   封装高频交互流程与默认等待规则。

3. env facade
   提供稳定能力入口。

4. agent / monitor
   分别承担主动驱动与被动采样。

5. scoreboard / ref memory / transport
   承担参考行为、传输闭环和最终检查。

如果未来要增加更复杂场景，例如 memory violation、misalign、异常 load/store、多 lane 并发回归，优先扩 sequence 和 monitor；只有在行为语义真正变化时才修改 scoreboard 或 transport。

## 12. 与 toffee/picker 和 UVM 的对照

从 toffee/picker 的实践建议看，当前环境已经遵循了几条关键原则：

1. testcase 不直接写 pin-level 细节。
2. 环境能力通过 facade 和事务对象暴露。
3. 将时序模板收进 sequence，而不是散布到测试代码。
4. 通过 monitor/scoreboard 解耦“观测事实”和“结果判断”。

从 UVM 的角度看，当前环境可以映射为：

- `MemBlockEnv` 对应 env
- `CsrAgent/LsqAgent/IssueAgent/CommitAgent` 对应 active agent 的 driver 部分
- `WritebackMonitor/StoreMonitor/MemStatusMonitor` 对应 passive monitor
- `Scoreboard` 对应 scoreboard
- `RefMemory` 对应 reference model 的存储核心
- `ScalarLoadSequence/ScalarStoreSequence/...` 对应 sequence
- `EnvConfig` 对应 config object

这说明当前重构已经不是“只是把文件拆开”，而是完成了验证环境角色模型的重建。

## 13. 未来演进方向

后续继续演进时，最值得做的方向有：

1. 把结构性端口宽度也逐步纳入可验证的 config 能力，而不是仅做默认值校验。
2. 为 monitor 增加统一事件类型，减少 monitor 到 scoreboard 的细粒度方法耦合。
3. 在 sequence 层引入更高层的组合场景，例如 mixed traffic、flush/replay、异常路径模板。
4. 把 memory violation、异常位、MMIO 特殊路径的检查进一步对象化。
5. 保持 `env.backend` 作为唯一默认主动控制入口，不再新增 `env.note_*` / `env.pulse_*` 风格 public helper。

## 14. 总结

本轮重构后的 MemBlock Python 验证环境已经形成了清晰、可持续的结构：

- env 负责装配和协调。
- agent 负责主动驱动。
- monitor 负责被动观测。
- scoreboard 负责判定。
- sequence 负责场景复用。
- config 负责统一默认策略。

这套结构既吸收了 UVM 的分层思想，也保持了 Python/toffee 环境所需要的直接性和可维护性。它的最大价值不是“更抽象”，而是让环境能够在真实 DUT 长期演进过程中保持稳定、可扩展、可定位问题。
