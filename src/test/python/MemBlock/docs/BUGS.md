# MemBlock 已知与潜在 DUT 问题

本文档记录当前 MemBlock Python 验证环境中：

- 已确认、会影响 testcase 判定口径的 DUT 问题
- 已有稳定复现签名、但仍需要进一步波形/RTL 收口的潜在 DUT 问题
- 已明确归因于 testcase 构造或 env 使用前提的问题，不放在本文件中长期跟踪

- 这里记录的 commit hash 默认是 `src/main/` 路径对应的 DUT 提交，而不是当前验证环境文档或 testcase 自身的提交。
- 当前记录方式使用：
  - `git log -1 --format=%H -- src/main`
  - `git log -1 --format='%H %ci %s' -- src/main`

## 状态说明

- `open`
  - 已有较高置信度确认是 DUT 缺陷，pytest 已按已知 bug 口径处置。
- `suspected`
  - 已有稳定 testcase 签名，静态 RTL 检查也能看到可疑点，但还需要进一步波形或更小粒度 probe 来最终闭环。

## 覆盖率文档口径约束

`coverage_summary.md` 与 `coverage_todo.md` 在描述“当前未支持/未闭环功能点”时，应优先以本文件中已有 testcase 触达、但真实 DUT 仍不能闭环的条目为依据，而不是把它们写成单纯“没测到”。当前应统一按未闭环功能簇理解的条目包括：

- `DUTBUG-vector-store-data-path-disconnected`
- `DUTBUG-cbo-zero-missing-wline-drain`
- `DUTBUG-store-misalign-cross-page-flush-stall`
- `DUTBUG-svpbmt-nc-load-debug-classification-lost`
- `DUTBUG-svpbmt-nc-store-zero-paddr-shadow`
- `DUTBUG-store-side-pmp-deny-missing-fault`

这几类问题的共同特征是：

- testcase 已经能把请求送进真实 DUT 的相关路径；
- 但数据通路、debug 分类、drain 收尾或 fault 收口仍然缺失；
- 因而它们在 coverage 文档里应被写成“当前未支持/未闭环”，而不是“当前还没有 testcase 入口”。

## DUTBUG-store-side-pmp-deny-missing-fault

- 状态：suspected
- 当前 pytest 处置：精确条件 `strict xfail`
- DUT `src/main/` commit hash：`03bc924c72cb055ccb8146a2eecd750ead0b4d7b`
- DUT `src/main/` commit 摘要：`03bc924c72cb055ccb8146a2eecd750ead0b4d7b 2026-03-31 13:50:09 +0800 top: make MemBlockTop wrapper for memblock only`
- 关联 testcase：
  - `src/test/python/MemBlock/tests/test_MemBlock_pmp_runtime.py`
- 关联场景：
  - `test_api_MemBlock_pmp_runtime_reprogram_all_entries_store`
  - `test_api_MemBlock_pmp_napot_boundary_store`
  - `test_api_MemBlock_pmp_tor_boundary_store`
- 关联 RTL 位置：
  - `src/main/scala/xiangshan/mem/pipeline/StoreUnit.scala`
  - `src/main/scala/xiangshan/mem/MemBlock.scala`

问题描述：

- 当前 translated store 在 `PMP allow` 背景下可以稳定 materialize，说明 Sv39/PTW/PMP 编程链路和 store requestor 本身已打通。
- 但同一条地址空间在运行时切到 `PMP off`、`NAPOT deny` 或 `TOR deny` 后，store 仍会继续表现为普通 shadow：
  - `allocated = 1`
  - `completed = 1`
  - `has_exception = 0`
  - 既没有稳定 fault 标记，也没有被拦在 materialize 之前
- 与之对照，load-side 在同样的 reprogram/boundary 背景下可以稳定收口到 `LOAD_ACCESS_FAULT_BIT`，因此当前缺口更像 store-side fault 收口没有真正穿出，而不是 testcase 本地配置错误。

验证口径：

- testcase 不把整个 `test_MemBlock_pmp_runtime.py` 无差别挂起。
- 只有 store-side PMP deny/off/TOR 边界这三组 directed case 按精确条件 `strict xfail`。
- load-side all-entry/runtime/lock/boundary 仍保持硬断言，以避免把 PMP 控制面本身的回归能力一起掩盖。

后续动作：

- 继续补最小波形/白盒观测，确认 store-side `pmp.st`、exception propagation 和 store shadow `hasException` 之间哪一段丢失。
- 若后续 DUT 修复该问题，移除对应 testcase 上的 `xfail`，恢复 store-side PMP deny 为普通 real-DUT regression。

## DUTBUG-cbo-zero-missing-wline-drain

- 状态：open
- 当前 pytest 处置：精确条件 `xfail`
- DUT `src/main/` commit hash：`03bc924c72cb055ccb8146a2eecd750ead0b4d7b`
- DUT `src/main/` commit 摘要：`03bc924c72cb055ccb8146a2eecd750ead0b4d7b 2026-03-31 13:50:09 +0800 top: make MemBlockTop wrapper for memblock only`
- 关联 testcase：
  - `src/test/python/MemBlock/tests/test_MemBlock_scalar_store_pipeline.py`
- 关联场景：
  - `test_api_MemBlock_cbo_zero_flush_zeroes_entire_cacheline`
- 关联 RTL 位置：
  - `src/main/scala/xiangshan/mem/lsqueue/NewStoreQueue.scala`

问题描述：

- 当前 `cbo.zero` 在真实 DUT 上已经能够稳定进入 `StoreQueue`，并在 store shadow 中表现为：
  - `allocated = 1`
  - `addr` 正确
  - `data = 0`
  - `completed = 1`
- 但在当前 flush 窗口内仍无法观测到对应的 `wline` sbuffer drain：
  - `sbuffer_drain_count` 不增长
  - `drain_log` 中没有新的 `wline` 事件
  - `flushSb` 最终超时，等不到 `sbIsEmpty`
- 从静态 RTL 看，`cbo.zero` 的设计意图是 `idle -> writeZero -> flushSb -> writeback`，且 `writeZero` 依赖 `io.writeToSbuffer.req.head.fire`；但普通 `toSbufferValid` 生成又会被 `ctrlEntry.isCbo` 对应的 `cboStall` 挡住，这条路径存在明显可疑点。

验证口径：

- testcase 不做整文件或整组 CBO 用例的无条件 `xfail`。
- 只有 `test_api_MemBlock_cbo_zero_flush_zeroes_entire_cacheline` 这一条真实 DUT 定向场景按已确认缺陷挂起。
- 纯 Python model / facade 单测保持硬断言，以确保 env 侧 `cbo.zero` 语义不会被 DUT 缺陷掩盖。

后续动作：

- DUT 侧需要继续确认：
  - `cbo.zero` 是否真的走到 `CboState.writeZero`
  - `io.writeToSbuffer.req.head.fire` 为何没有形成
  - `cbo.zero` 到 sbuffer 的写零路径是否被 `cboStall` 意外挡住
- 修复后移除该 testcase 上的 `xfail`，恢复为普通 real-DUT regression。

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

## DUTBUG-store-misalign-cross-page-flush-stall

- 状态：open
- 当前 pytest 处置：`strict xfail`
- DUT `src/main/` commit hash：`03bc924c72cb055ccb8146a2eecd750ead0b4d7b`
- DUT `src/main/` commit 摘要：`03bc924c72cb055ccb8146a2eecd750ead0b4d7b 2026-03-31 13:50:09 +0800 top: make MemBlockTop wrapper for memblock only`
- 关联 testcase：
  - `src/test/python/MemBlock/tests/test_MemBlock_store_misalign.py`
- 关联场景：
  - `test_api_MemBlock_store_misalign_sd_offset_d_cross_page`
  - `test_api_MemBlock_store_misalign_sh_offset_f_cross_page`

问题描述：

- 两条 cross-page scalar store misalign 场景都能稳定走到：
  - store shadow 可见；
  - younger load / 可观测内存侧状态已进入收尾阶段；
- 但最终 `flushSb` 无法把 `sbuffer` drain 到 empty。
- 该签名已经跨 store 宽度与页尾偏移复现，不像 testcase 单点时序误差。

验证口径：

- 当前两条 cross-page case 都使用 `strict xfail`。
- 若后续 DUT 修复，该类用例会自动转成 `XPASS`，从而提醒移除挂起状态并恢复硬断言。

后续动作：

- DUT 侧需要继续排查 cross-page store-misalign split 进入 flush/drain 收尾阶段后的 sbuffer/outer 闭环。
- 修复后把对应 xfail 去掉，恢复普通 real-DUT regression。

## DUTBUG-svpbmt-nc-load-debug-classification-lost

- 状态：suspected
- 当前 pytest 处置：精确条件 `xfail`
- DUT `src/main/` commit hash：`03bc924c72cb055ccb8146a2eecd750ead0b4d7b`
- DUT `src/main/` commit 摘要：`03bc924c72cb055ccb8146a2eecd750ead0b4d7b 2026-03-31 13:50:09 +0800 top: make MemBlockTop wrapper for memblock only`
- 关联 testcase：`src/test/python/MemBlock/tests/test_MemBlock_uncache_semantics.py`
- 关联场景：`test_api_MemBlock_sv39_pbmt_ncio_load_smoke`
- 关联 RTL 位置：
  - `src/main/scala/xiangshan/mem/pipeline/NewLoadUnit.scala`
  - `src/main/scala/xiangshan/mem/lsqueue/LSQCommon.scala`

问题描述：

- `PBMT=NC` translated load 已能完成，且 transport 统计显示它走了 outer 路径。
- 但 load writeback 上的 debug 分类仍表现为：
  - `debug_is_mmio = 0`
  - `debug_is_ncio = 0`
- 从静态 RTL 看，`ldout.debug.isNCIO := in.isNCReplay() && in.pmp.get.mmio`，这会把 PBMT-NC 且 `pmp.mmio=0` 的场景错误压成非 NCIO。

验证口径：

- testcase 先要求 load 必须真实完成。
- 只有在完成后 debug 分类仍不是 `NCIO` 时，才触发 `xfail`。

后续动作：

- 优先确认 `NewLoadUnit` 上述 debug 分类条件是否应直接改为 `in.isNCReplay()` 或等价语义。
- 修复后用当前 testcase 直接回归，确认 `debug_is_ncio=1` 且 `debug_is_mmio=0`。

## DUTBUG-svpbmt-nc-store-zero-paddr-shadow

- 状态：suspected
- 当前 pytest 处置：精确条件 `xfail`
- DUT `src/main/` commit hash：`03bc924c72cb055ccb8146a2eecd750ead0b4d7b`
- DUT `src/main/` commit 摘要：`03bc924c72cb055ccb8146a2eecd750ead0b4d7b 2026-03-31 13:50:09 +0800 top: make MemBlockTop wrapper for memblock only`
- 关联 testcase：`src/test/python/MemBlock/tests/test_MemBlock_uncache_semantics.py`
- 关联场景：`test_api_MemBlock_sv39_pbmt_ncio_store_flush_smoke`
- 关联 RTL 位置：
  - `src/main/scala/xiangshan/mem/pipeline/StoreUnit.scala`
  - `src/main/scala/xiangshan/mem/lsqueue/NewStoreQueue.scala`

问题描述：

- `PBMT=NC` translated store 当前能够稳定表现出：
  - `store.nc = True`
  - `store.mmio = False`
- 但同一条 store shadow 的 `addr` 却是 `0`，而不是期望的 translated paddr。
- 当前复核里，这个 `addr=0` 状态可在 commit 之后持续多个 cycle 保持稳定，而不是瞬时过渡态。
- env/scoreboard 这边只是被动读取 DUT 导出的 store-address 通路；当前看到的 `addr=0` 更像 DUT 在 NC translated store 的 paddr/shadow 导出存在缺口，而不是 env 自己生成了错误地址。

验证口径：

- testcase 会先要求看到真实 store shadow。
- 只有当 `addr != translated_paddr` 或 shadow 分类不符合 `NC && !MMIO` 时，才触发 `xfail`。

后续动作：

- 优先对照：
  - `StoreUnit` 写回的 `s3_out.debug.paddr`
  - `NewStoreQueue` 记录的 `debugPaddr`
  - store-address monitor 实际采样的 `paddr`
- 若这三者中前两者已正确、只有 monitor 输入为 0，再回头检查 env；在那之前先按 DUT 候选缺口处理。
