# MemBlock 验证环境设计文档

本文档目录面向 `src/test/python/MemBlock/` 下的 Python 验证环境，覆盖环境结构、时序语义、在线比对模型和现有测试组织方式。

## 文档列表

- `verification_env_design.md`
  - 说明 `MemBlockEnv` 的组成、Bundle 分层、时钟推进规则、复位语义，以及 load/store 用例如何与环境交互。
- `memory_model_design.md`
  - 说明 `MemoryModel` 的职责边界、outer/dcache 事务建模、load commit-boundary compare、store drain 校验和当前限制。
- `dut_port_behavior.md`
  - 按 DUT 端口分组解读验证环境中的输入输出语义，包含 `enqLsq`、`intIssue`、`mem_to_ooo`、TileLink 和 store shadow 观测口行为说明。
- `test_sequence_and_extension_guide.md`
  - 用事务序列、Mermaid 图和调试清单说明如何编写 load/store 用例，以及扩展环境时需要遵守的规则。

## 适用范围

本文档基于以下实现整理：

- `src/test/python/MemBlock/MemBlock_env.py`
- `src/test/python/MemBlock/MemBlock_api.py`
- `src/test/python/MemBlock/request_apis.py`
- `src/test/python/MemBlock/memory_model.py`
- `src/test/python/MemBlock/tests/`

文档关注的是 Python 侧验证环境设计，而不是 MemBlock RTL 本身的微结构细节。

## 环境目标

该验证环境的核心目标有三点：

1. 为真实 DUT 提供可复用的 Python 测试入口。
2. 在不依赖完整 SoC 外围的前提下，为 LSU/MemBlock 提供最小闭环外设行为。
3. 在测试运行时完成 load 结果在线校验，并在测试结束时完成 store drain 一致性校验。

## 阅读建议

如果是首次接触该环境，建议按以下顺序阅读：

1. `verification_env_design.md`
2. `memory_model_design.md`
3. `dut_port_behavior.md`
4. `test_sequence_and_extension_guide.md`
5. `src/test/python/MemBlock/tests/test_MemBlock_random_load.py`
6. `src/test/python/MemBlock/tests/test_MemBlock_random_store.py`

## 文档规模

当前 `docs/` 目录下的文档被设计为详细设计资料，而不是简短说明。

- 文档总行数目标不少于 1000 行。
- 内容允许包含 DUT 端口行为解读。
- 内容允许包含 Mermaid 图用于表达路径、握手顺序和模块关系。
