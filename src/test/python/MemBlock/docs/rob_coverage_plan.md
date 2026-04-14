# ROB Coverage Model

本文档记录 `src/test/python/MemBlock/model/rob_coverage.py` 中 ROB 建模相关函数覆盖的设计落点，目标是把 [`rob_model.md`](/nfs/home/majiuyue/XiangShan/src/test/python/MemBlock/docs/rob_model.md) 中已经覆盖、部分覆盖、以及当前明确未建模的项，都映射到统一的 DUT-monitor-based coverage 视图中。

## 1. 设计原则

覆盖模型遵循三条约束：

1. 不根据 testcase 名称或 sequence 类型手工打点。
2. 主要依赖真实 DUT 导出信号、env monitor、scoreboard 的 shadow/drain 状态。
3. 对当前明确未建模的项，不与正向功能点混在一起，而是单独作为 `KnownModelGaps` 报告。

其中 scoreboard 允许参与覆盖推导，是因为它的 store shadow、drain log、load compare 完成状态本身就是从 DUT 端口和内部观测口采样得到，而不是 testcase 人工声明。

## 2. 覆盖组划分

### 2.1 `MemBlock.ROB.ObservedBehavior`

这一组只回答一个问题：当前 testcase 中，真实 DUT 是否出现了某类 ROB/LSQ 交互事实。

当前落地的点包括：

- `store_commit_frontier_observed`
  - 依据 SQ shadow 的 `committed` 位。
- `load_commit_budget_observed`
  - 依据 `io_mem_to_ooo_lqDeq`。
- `load_writeback_observed`
  - 依据 `intWriteback` 有效 load writeback。
- `flushsb_command_observed`
  - 依据 `io_ooo_to_mem_flushSb`。
- `sbuffer_drain_observed`
  - 依据 sbuffer drain log。
- `outer_drain_observed`
  - 依据 outer TL-A Put drain log。
- `mmio_busy_observed`
  - 依据 `lsq_status.mmioBusy`。
- `mmio_store_shadow_observed`
  - 依据 SQ shadow / store shadow 中的 `mmio` 标记。
- `replay_event_observed`
  - 依据 replay lane / load replay / ncOut / violation 相关 DUT 信号。
- `memory_violation_observed`
  - 依据 `memoryViolation_valid`。
- `raw_nuke_query_observed`
  - 依据 RAW nuke query req。
- `rar_nuke_query_observed`
  - 依据 RAR nuke query req。
- `rar_nuke_response_observed`
  - 依据 RAR nuke response。
- `release_hint_observed`
  - 依据 `MemBlock_inner_lsq_io_release_valid`。
- `backend_feedback_observed`
  - 依据 `sqDeq` 以及 `lqDeqPtr/sqDeqPtr` 推进。

### 2.2 `MemBlock.ROB.CurrentModel`

这一组对应 [`rob_model.md`](/nfs/home/majiuyue/XiangShan/src/test/python/MemBlock/docs/rob_model.md) 中“当前模型已经能支撑的场景”，重点反映当前真实 DUT 回归是否真的跑到了这些 ordering / drain 闭环。

当前点包括：

- `same_addr_store_then_load_observed`
  - 依据 retired store 的字节窗口，与 load writeback `debug_paddr` 重叠。
- `overwrite_store_then_load_observed`
  - 同一 load 之前存在两条及以上 older retired same-address stores。
- `unrelated_store_then_load_observed`
  - older retired store 存在，但与 load 访问字节不重叠。
- `mixed_load_store_window_observed`
  - testcase 中同时出现 retired store 与 load writeback。
- `cacheable_store_flush_drain_observed`
  - 观测到 `flushSb` 且后续出现 sbuffer drain。
- `nc_store_flush_drain_observed`
  - 观测到 `flushSb`。
  - 至少存在一条 `store.nc == True && !store.mmio` 的 retired store。
  - 后续 outer drain 与该 NC store 的 touched-byte 窗口发生重叠。
- `mmio_store_excluded_from_drain_observed`
  - 观测到 `flushSb`。
  - 至少存在一条 MMIO store shadow。
  - 最终 drain log 未覆盖该 MMIO store 的 touched-byte 窗口。

### 2.3 `MemBlock.ROB.GapObserved`

这一组不是“功能通过”，而是“真实 DUT 已经暴露出这类窗口，但当前 ROB 建模仍是半模型”。它把 `rob_model.md` 里已经分析出的短板挂到真实回归事实上。

当前点包括：

- `gap_mmio_busy_without_commit_bool`
  - testcase 中出现了 `mmioBusy`，但环境没有完整 `commit` bool / mmioBusy forward model。
- `gap_backend_feedback_without_model`
  - testcase 中已经出现 `sqDeq/lqDeqPtr/sqDeqPtr`，但环境还没有 backend credit/resource half-model。
- `gap_replay_without_redirect_cancel_model`
  - testcase 中已经出现 replay / violation / release 等恢复窗口，但环境仍缺 redirect/cancel frontier 修正。
- `gap_mixed_commit_window_without_packet_model`
  - testcase 中出现了 `scommit` 与 DUT `lqDeq` 同拍窗口，但环境没有真实 mixed commit packet。

### 2.4 `MemBlock.ROB.KnownModelGaps`

这一组是“已知缺口清单”，用于把 `rob_model.md` 的结论稳定带进报告，而不是依赖回归执行到某个 testcase 才记得这项 gap 还存在。

当前固定列出：

- `redirect_cancel_not_modelled`
- `backend_feedback_credit_not_modelled`

## 3. 与 `rob_model.md` 的映射关系

### 3.1 当前已覆盖的路径

`rob_model.md` 中当前已覆盖或基础可用的项，主要落到 `CurrentModel` 组：

- 基础 store committed frontier
- load compare 使用 DUT 导出的 commit budget 收口
- same-addr store/load 可见性
- overwrite 可见性
- unrelated ordering
- flushSb 后 cacheable / nc drain 闭环
- MMIO store 不进入最终 golden drain compare

### 3.2 当前不能覆盖或覆盖不完整的路径

`rob_model.md` 中“不能覆盖或覆盖不完整”的部分，当前按两类处理：

1. 若真实回归已经能观测到对应现象，则进入 `GapObserved`
   - 例如 `mmioBusy`
   - replay / violation / release
   - backend feedback `sqDeq/lqDeqPtr/sqDeqPtr`
   - `scommit + lqDeq` 同拍窗口
2. 若当前环境根本还没有对应 forward model，则进入 `KnownModelGaps`
   - redirect / cancel
   - backend credit/resource model

## 4. 接入方式

覆盖 collector 已接入 [`MemBlock_env.py`](/nfs/home/majiuyue/XiangShan/src/test/python/MemBlock/MemBlock_env.py) 的 `env` fixture 生命周期：

1. fixture 创建 `MemBlockEnv`
2. 创建 `env.rob_coverage`
3. 将 `collector.sample` 注册到 `after_step_callback`
4. 用例结束时调用 `collector.finalize()`
5. 通过 `set_func_coverage(request, collector.all_groups())` 汇报

因此现有真实 DUT 用例不需要额外改写，只要走 `env.advance_cycles()` / 现有 sequence，就会自动累积这套覆盖。

## 5. 当前边界

这版覆盖模型仍有明确边界：

- same-address / unrelated load-store 关系依赖 load writeback `debug_paddr`。
- 默认按标量 8B load 访问窗口判断重叠，后续若要扩展到更多标量宽度，需要补 `fuOpType -> size` 解码或补更强的 DUT debug。
- `KnownModelGaps` 是“模型状态声明”，不是“功能已覆盖”的意思，阅读报告时应与 `ObservedBehavior` / `CurrentModel` 区分。

这正好符合当前阶段目标：先把 ROB 半模型的“已验证能力”和“已知缺口”一起显式化，再决定下一阶段是否把 `pendingPtrNext`、non-mem blocker、redirect/cancel、backend feedback credit model 继续补成更完整的 forward model。
