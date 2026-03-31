# MemBlock Python Verification Environment

`src/test/python/MemBlock/` 是面向 MemBlock 真实 DUT 的 Python 验证环境目录。这里既包含 testcase 和运行环境，也包含环境自身的设计文档与变更记录。

当前目录的组织原则是：

- 根目录放环境入口和总览文档。
- `docs/` 放详细设计文档与 DUT 侧 changelog。
- `tests/` 放环境冒烟、模型单测和真实 DUT 场景用例。

## 目录说明

### 根目录

- `MemBlock_api.py`
  - DUT 创建、波形/覆盖率路径配置、`dut` fixture。
- `MemBlock_env.py`
  - 顶层 `MemBlockEnv`、bundle 定义、环境 facade。
- `request_apis.py`
  - 公共驱动 helper 和兼容型事务接口。
- `transactions.py`
  - `QueuePtr`、`LoadTxn`、`StoreTxn` 等事务对象。
- `memory_model.py`
  - 当前的 compare / store shadow / drain 收尾模型编排层。
- `CHANGELOG.md`
  - Python 验证环境自身的重构与维护变更记录。
- `README.md`
  - 当前文件，作为目录入口说明。

### 子目录

- `agents/`
  - active driver agents，包括 CSR、commit、LSQ enqueue、issue。
- `model/`
  - 已从 `MemoryModel` 中拆出的公共组件，例如 `RefMemory`、`TransportResponder`。
- `tests/`
  - 测试集合。
- `docs/`
  - 详细设计文档与 DUT changelog。
- `webui/`
  - LSQ 可视化相关资源。

## `docs/` 文档列表

- `verification_env_design.md`
  - 重构后的验证环境总设计，涵盖 `MemBlockEnv`、`transactions`、`agents`、`RefMemory`、`TransportResponder` 的分层关系、设计思路、实现方案和未来演进方向。
- `memory_model_design.md`
  - `MemoryModel` 当前保留职责、load commit-boundary compare、store drain 校验和继续向 scoreboard 拆分的背景。
- `dut_port_behavior.md`
  - DUT 输入输出端口行为说明，覆盖 `enqLsq`、`intIssue`、`mem_to_ooo`、TileLink 和 store shadow 观测口。
- `test_sequence_and_extension_guide.md`
  - 用事务与场景组织方式说明如何编写 load/store 用例以及如何扩展环境。
- `DUT_CHANGELOG-20260331.md`
  - 一次针对 RTL `mem/` 目录版本变化的详细对比与验证影响分析。

## 阅读建议

如果是首次接触当前环境，建议按以下顺序阅读：

1. `README.md`
2. `CHANGELOG.md`
3. `docs/verification_env_design.md`
4. `docs/memory_model_design.md`
5. `docs/dut_port_behavior.md`
6. `docs/test_sequence_and_extension_guide.md`
7. `docs/DUT_CHANGELOG-20260331.md`
8. `tests/test_MemBlock_random_load.py`
9. `tests/test_MemBlock_random_store.py`

## 文档定位

- 根目录 `CHANGELOG.md` 记录的是 Python 验证环境自身的演进。
- `docs/DUT_CHANGELOG-20260331.md` 记录的是一次 DUT / RTL 侧变化及其对验证环境的影响。

两者不要混淆：前者关注 testbench 结构，后者关注被测对象变化。
