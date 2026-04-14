# MemBlock 已知 DUT 问题

本文档记录当前 MemBlock Python 验证环境已经确认、且会影响 testcase 判定口径的 DUT 问题。

- 这里记录的 commit hash 默认是 `src/main/` 路径对应的 DUT 提交，而不是当前验证环境文档或 testcase 自身的提交。
- 当前记录方式使用：
  - `git log -1 --format=%H -- src/main`
  - `git log -1 --format='%H %ci %s' -- src/main`

## DUTBUG-vector-store-data-path-disconnected

- 状态：open
- 当前 pytest 处置：精确条件 `xfail`
- DUT `src/main/` commit hash：`03bc924c72cb055ccb8146a2eecd750ead0b4d7b`
- DUT `src/main/` commit 摘要：`03bc924c72cb055ccb8146a2eecd750ead0b4d7b 2026-03-31 13:50:09 +0800 top: make MemBlockTop wrapper for memblock only`
- 关联 testcase：`src/test/python/MemBlock/tests/test_MemBlock_vector_store.py`
- 关联 RTL 位置：
  - `src/main/scala/xiangshan/mem/MemBlock.scala`
  - `src/main/scala/xiangshan/mem/vector/VSplit.scala`

问题描述：

- 当前 unit-stride vector store 已能走到 vector completion metadata。
- 但顶层 `MemBlock.scala` 仍把 `vsSplit(i).io.vstd.get` 直接置为 `DontCare`，导致 `VSSplitBuffer` 生成的 store data 没有真正送入 SQ。
- 已稳定观测到的故障签名是：
  - SQ 条目地址正常，但 `data=0`
  - 没有新的 `dcache A/D` transport
  - `flushSb` 一直等不到 `sbIsEmpty`
  - `drain_log` 保持为空

验证口径：

- testcase 不做整例无条件 `xfail`。
- 只有当真实 DUT 同时表现出：
  - completion metadata 已到；
  - SQ 零数据条目可见；
  - `flushSb` 超时且没有 drain；
  - `dcache A/D` transport 仍为 0；
  才调用 `pytest.xfail(...)`。
- 如果后续 DUT 修复了这条路径，该用例会自动退回硬断言，继续检查 drain 与最终 memory effect。

后续动作：

- DUT 侧需要把 vector store data 路径真正接到 SQ/store pipeline。
- 修复后移除 testcase 中对应的 `xfail` 分支，恢复为普通 real-DUT regression。

## DUTBUG-matchinvalid-redirect-replay-dual-path

- 状态：open
- 当前 pytest 处置：`xfail`
- DUT `src/main/` commit hash：`03bc924c72cb055ccb8146a2eecd750ead0b4d7b`
- DUT `src/main/` commit 摘要：`03bc924c72cb055ccb8146a2eecd750ead0b4d7b 2026-03-31 13:50:09 +0800 top: make MemBlockTop wrapper for memblock only`
- 关联 testcase：`src/test/python/MemBlock/tests/test_MemBlock_replay.py`
- 关联分析文档：`src/test/python/MemBlock/docs/sq_matchinvalid_nuke_case_analysis.md`

问题描述：

- 场景为 `dataInvalid=1` + `matchInvalid=1` + `memoryViolation.level=1`。
- 期望语义是同一条 younger load 触发 flush 级 redirect 之后，不应再向 LSQ 建立 replay 去路。
- 当前 DUT 上仍可继续观测到同一 ROB 的 `FF` replay 事件，来源包括 `replay_queue`、`replay_lane`、`ldu`。

验证口径：

- 该问题在 testcase 中不是整例无条件 `xfail`。
- 只有当用例真实观测到上述 replay-path 事件时，才触发：
  - `pytest.xfail("DUTBUG-matchinvalid-redirect-replay-dual-path ...")`
- 这样已知 DUT 缺陷不会把回归直接打红，但其余不相关断言仍然保持硬失败能力。

后续动作：

- 等待 DUT 修复 redirect 与 replay 双去路问题。
- 修复后移除 testcase 中对应的 `xfail`，恢复为硬失败判定。
