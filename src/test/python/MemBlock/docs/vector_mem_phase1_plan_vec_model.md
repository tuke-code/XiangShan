# Context

用户当前关心的不是立即实现，而是先厘清 `VectorMemoryModel` 在现有 MemBlock 环境中的归属边界：它应如何嵌入已有 `MemoryModel`，以及 testcase 应通过哪一层与它交互。当前环境里，`MemoryModel` 已经是顶层内存/scoreboard/transport 组合 facade，因此向量模型不应绕开它另起一套平行入口，而应作为其下的一个子模型/子 facade 暴露给 sequence 与 testcase 使用。

# Recommended approach

## 1. 把 `VectorMemoryModel` 放在 `MemoryModel` 之下，而不是直接挂到 testcase

推荐修改/扩展的关键文件：
- `src/test/python/MemBlock/memory_model.py`
- `src/test/python/MemBlock/MemBlock_env.py`
- `src/test/python/MemBlock/model/vector_memory_model.py`
- `src/test/python/MemBlock/agents/vector_backend_facade.py`
- `src/test/python/MemBlock/sequences/vector_mem_sequences.py`
- `src/test/python/MemBlock/tests/test_MemBlock_vector_unit_stride.py`
- `src/test/python/MemBlock/tests/test_MemBlock_vector_stride.py`

推荐结构：
- `MemBlockEnv` 继续持有统一入口 `env.memory`，对应现有 `MemoryModel`。
- `MemoryModel` 继续负责顶层组装职责：`RefMemory`、`TransportResponder`、`Scoreboard`、monitor 协同。
- `VectorMemoryModel` 作为 `MemoryModel` 内部的向量子模型，例如 `env.memory.vector`。

原因：
- 现有 `MemoryModel` 已经是统一 facade，负责 preload/read/predict/scoreboard/drain 汇总，见 `src/test/python/MemBlock/memory_model.py:57`。
- `MemBlockEnv` 已把 `self.memory = MemoryModel(...)` 作为环境统一内存入口，见 `src/test/python/MemBlock/MemBlock_env.py:1456`。
- 若 testcase 直接持有独立 `VectorMemoryModel`，会绕开已有 `RefMemory` 与 transport/scoreboard 生命周期，造成两套 reference state。

## 2. `VectorMemoryModel` 与 `MemoryModel` 的推荐交互契约

推荐 `MemoryModel` 提供：
- `self.vector = VectorMemoryModel(self.ref_memory, ...)`

其中 `VectorMemoryModel` 应复用现有对象，而不是复制：
- 复用 `self.ref_memory`
- 必要时只读访问 `self.scoreboard` / monitor 结果
- store 最终比较仍复用 `RefMemory.clone()/with_store()` 这一类现有思路

应复用的现有能力：
- `MemoryModel.preload_u64/preload_bytes/read/fork_ref_memory/predict_store`：`src/test/python/MemBlock/memory_model.py:197-231`
- `MemoryModel.finalize_and_check_drain`：`src/test/python/MemBlock/memory_model.py:430-431`
- `MemBlockEnv.expect_scalar_load` 作为“env 对 memory facade 的薄封装”模式参考：`src/test/python/MemBlock/MemBlock_env.py:1667-1694`
- `MemBlockEnv.flush_store_buffers_and_wait` 与 `assert_no_outstanding` 作为 testcase 收口入口：`src/test/python/MemBlock/MemBlock_env.py:2614-2631`

推荐边界：
- `MemoryModel`：持有共享 reference memory、共享 transport/drain 事实、统一对外 facade。
- `VectorMemoryModel`：只负责向量语义展开与 reference prediction：
  - `expand(txn)`
  - `expect_load(txn)`
  - `predict_store(txn)`
- `VectorBackendFacade`：负责发 DUT、配置向量上下文、等待 completion/trap。
- `VectorMemMonitor`：负责采集 completion/request/trap 事实。

## 3. testcase 不直接操作 `VectorMemoryModel` 的内部细节

现有标量模式表明，testcase 通常不直接手写 scoreboard/transport 时序，而是通过 sequence + env facade 交互：
- `ScalarLoadSequence.run()` 内部调用 `send_load(env, txn)` 与 `expect_load(env, txn)`，然后 `env.drain_writebacks(...)`：`src/test/python/MemBlock/sequences/memblock_sequences.py:515-532`
- store case 在 testcase 中只做 preload、构造 txn、运行 sequence、最后比对 `env.memory.read(...)` 与 predicted refmem：如 `src/test/python/MemBlock/tests/test_MemBlock_scalar_store_pipeline.py:178-229`

因此 vector testcase 推荐契约为：
1. `env.preload_*` 预置初始内存
2. 构造 `VectorMemTxn`
3. 通过 `env.vector_backend.send(txn)` 或 `Vector*Sequence(...).run(env)` 发请求
4. 通过 `env.memory.vector.expect_load(txn)` / `env.memory.vector.predict_store(txn)` 建立 reference
5. 用 `env.vector_backend.wait_complete()` / `wait_trap()` 等待结果
6. load 比对 vector result 与 expected plan；store 比对 `env.memory.read(...)` 与 predicted ref memory
7. 结尾复用 `env.assert_no_outstanding()` / drain 收口；其实现应把 `env.memory.vector` 的 outstanding 一并计入

## 4. 推荐的最小 API 形状

### `MemoryModel`
- `env.memory.vector.expand(txn)`
- `env.memory.vector.expect_load(txn)`
- `env.memory.vector.predict_store(txn)`

### `MemBlockEnv`
- 继续保留 `env.memory` 作为统一入口
- 增加 `env.vector_backend`
- 增加 `env.vector_monitor` 的只读等待/查询 facade

### testcase`
- 优先使用 `VectorUnitStrideLoadSequence` / `VectorStrideLoadSequence` 之类 sequence
- 若暂时不封 sequence，也应只调用 facade 级入口，不直接碰 pin-level 或 element 展开逻辑

## 5. 关键复用模式

应复用的现有模式：
- 统一环境入口：`env.memory` 与 `env.backend`，见 `src/test/python/MemBlock/MemBlock_env.py:1398,1456`
- load expectation 由 sequence 代登记，而不是 testcase 手搓 compare 流程，见 `src/test/python/MemBlock/sequences/memblock_sequences.py:518-520`
- store testcase 用 predicted ref memory 做最终 memory effect compare，见 `src/test/python/MemBlock/tests/test_MemBlock_scalar_store_pipeline.py:181-182,219-224`
- testcase 末尾统一做 `env.assert_no_outstanding()` 收口，见 `src/test/python/MemBlock/MemBlock_env.py:2628-2631`

# Verification

若后续进入实现，建议这样验证端到端交互是否正确：
- 先跑纯 Python 单测：
  - `VectorMemoryModel.expand()` 对 active/inactive/tail/prestart、正/零/负 stride 的展开
  - `expect_load/predict_store` 对 shared `RefMemory` 的读写预测是否正确
- 再跑真实 DUT smoke：
  - `tests/test_MemBlock_vector_unit_stride.py`
  - `tests/test_MemBlock_vector_stride.py`
- 每条用例都检查：
  - completion/trap 事实
  - load data compare 或 store final memory effect compare
  - `env.assert_no_outstanding()` 收口
