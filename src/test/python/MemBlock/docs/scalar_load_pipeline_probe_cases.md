# `scalar load pipeline probe` 用例说明

## 1. 文档目的

本文档说明 `test_MemBlock_scalar_load_pipeline_probe.py` 这组 probe 型用例要验证什么、依赖哪些 sequence、以及它们与普通 replay smoke 的区别。

对应实现位于：

- sequence: `src/test/python/MemBlock/sequences/load_pipeline_probe_sequences.py`
- testcase: `src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py`

相关专题文档：

- `src/test/python/MemBlock/docs/sq_bank_conflict_replay.md`
- `src/test/python/MemBlock/docs/sq_matchinvalid_nuke_case_analysis.md`
- `src/test/python/MemBlock/docs/mmu_fault_directed_cases.md`

## 2. 什么叫 probe 型用例

这里的 probe 不等于“只看几个内部信号点亮”。当前这组 testcase 的目标是：

1. 用白盒事件把 load pipeline 的中间路径钉住。
2. 仍然要求最终写回、compare、store commit/drain 能够收敛。
3. 尽量排除“其实走的是别的路径，但最后也回写了”的误判。

因此它们比 replay smoke 更窄，也更强调路径纯度。它们依赖的观测主要包括：

1. `debugLsInfo` 导出的 `bankConflict/replayCause/isReplayFast/isReplaySlow`
2. `mem_to_ooo` 写回与 wakeup
3. `replay_state` 中的 `replay_queue` / `replay_lane` / `memory_violation`
4. `store shadow` 与最终 drain 事实

## 3. 当前覆盖的场景簇

### 3.1 fault matrix with pipeline checks

这一组复用 `MmuFaultingScalarLoadSequence`，但在 fault 语义之外再加两类限制：

1. fault 不应退化成 `memoryViolation`
2. fault 不应额外带出 outer request / dcache miss / dcache error

它的价值是把“MMU fault”与“load pipeline 其它异常路径”明确分开。

### 3.2 bank conflict hit, no forward, no violation

这条用例对应最纯的 `BC` hit case。它要求：

1. 两到三条 cacheable load 同拍发射
2. 命中 `bank conflict`
3. 看到 `fast replay + ldCancel`
4. replay cause 只包含 `BC`
5. 不允许混入 `SQ/SBuffer forward`、`memoryViolation` 或 replay queue 路径

这条场景相当于 `BC` 组合场景的最小基线。

### 3.3 matchInvalid proxy probe

这条用例不是在重新证明 MMU 能工作，而是在钉住一条特殊边界：

1. older store 只保留地址相关性
2. younger translated load 走 dcache hit
3. SQ forward 返回 `dataInvalid=1 + matchInvalid=1`
4. younger load 在 `memoryViolation` 上以 `level=1` 收口

因为当前 env 没有独立公开 `vp_match_fail` 顶层信号，所以 testcase 明确使用：

- `matchInvalid + dataInvalid + violation`

作为 `vp_match_fail` 代理口径。

当前这条用例仍保留精确 `xfail` 条件：若同一 ROB 同时还从 replay queue/replay lane 暴露 replay 去路，则落到已知 DUT bug `DUTBUG-matchinvalid-redirect-replay-dual-path`。

### 3.4 fast replay 被更高优先级 replay 抢占，改落 replay queue

这条场景以 `BC` 为前提，但目标不是再次证明 `BC` 本身，而是证明：

1. 原本应该走 `fast replay` 的 bank-conflict load
2. 当 s3 同拍出现更高优先级 replayHiPrio 请求
3. fast replay 会被取消
4. 请求改落 `replay queue/replay lane`
5. 最终 compare/writeback 仍能全部收敛

当前 testcase 对这条路径已经收紧到“真正验证”口径，不再只看 replay queue 落点，而是同时要求：

1. preemptor writeback 全收齐
2. bank-conflict loads 的 final writeback / wakeup 全收齐
3. `completed_load_count == expected_completed_load_count`

并且这套断言对：

- `forward_dchannel` preemptor
- `nc_replay` preemptor

两种高优先级来源都成立。

### 3.5 late-STA store-load violation over bank conflict

这条场景的目标是把两层行为叠起来：

1. younger load 先落入 `BC fast replay`
2. older store 稍后才发出 `STA`
3. 之后 younger victim load 必须触发 `memoryViolation`

当前 testcase 不再接受“older store 只是 materialize”这种宽松证明，而是明确要求：

1. younger victim 写回与 violation ROB 对齐
2. older store `completed`
3. older store `committed`
4. 最后 `flush/drain` 真正收敛

因此这条用例现在证明的已经不是“晚到 STA 似乎影响了 younger load”，而是“晚到 STA 触发了真实 violation，且 older store 本身能完成 commit + drain 收尾”。

### 3.6 预留的三重叠加组合

文件末尾还保留了一个占位用例：

1. bank conflict
2. matchInvalid proxy
3. st-ld violation

当前只保留最小骨架，还没有升级成完整收口场景。它的意义是给后续更复杂的组合验证留入口，而不是假装当前已经打通。

## 4. 复用 sequence 的职责划分

当前 `load_pipeline_probe_sequences.py` 里主要有三类可复用骨架。

### 4.1 `ScalarBankConflictLoadClusterSequence`

它负责构造最基础的 bank-conflict load cluster，并统一管理：

1. cache warmup
2. 同拍 enqueue/issue
3. `debugLsInfo` 里的 bank-conflict / replay 采样
4. 最终 writeback / wakeup / completed-load-count 收尾

因此 testcase 不需要自己一边写拍级 plan，一边再本地拼 debug trace。

### 4.2 `ScalarFastReplayCancelledByReplayHiPrioSequence`

它在 bank-conflict 基线外，再叠加一组 preemptor load，用来证明：

1. fast replay 被高优先级请求抢占
2. replay queue 重定向发生
3. 最终每类 load 都有自己的 writeback/compare 闭环

当前 result 中显式返回：

1. `preemptor_txns`
2. `preemptor_writebacks`
3. `bank_conflict_result`
4. `expected_completed_load_count`

便于 testcase 直接做最终收敛断言。

### 4.3 `ScalarLateStaStoreLoadViolationSequence`

它负责把：

1. older store 的 `STD/STA` 分离时序
2. younger bank-conflict cluster
3. 后续 violation 恢复

揉成一个稳定 sequence。当前它会把 testcase 最关心的收尾事实显式返回：

1. `violation_event`
2. `younger_writeback`
3. `committed_store_view`

这样 testcase 可以围绕语义对象断言，而不是自己扫描所有 trace。

## 5. 当前断言风格

这批 probe 型用例的断言风格有三个特点。

### 5.1 先钉路径，再看结果

例如：

1. `BC` 用例先断言 replay cause、fast replay、ldCancel
2. `matchInvalid` 用例先断言 `dataInvalid + matchInvalid + violation`
3. hi-prio case 先断言 replay queue redirection

然后才要求最终 writeback/compare 收敛。

### 5.2 对“非目标路径”做排除

多条用例都会检查：

1. `outer_request_count` 不增长
2. `dcache_a_request_count` / `dcache_d_response_count` 不增长
3. `dcache_miss_signal == 0`
4. `dcache_error_valid == 0`

这能显著减少“其实走了 miss/outer/error，只是结果碰巧也收敛”的误判。

### 5.3 不把 probe 降级成波形点亮测试

当前 testcase 不接受只看到某个白盒点亮就算通过，而是仍然要求：

1. load writeback 真正出现
2. `completed_load_count` 达到预期
3. store commit/drain 真正发生
4. `env.assert_no_outstanding()`

这也是为什么这批用例在近期被进一步收紧，以去掉原先的两个 XPASS。

## 6. 与已有专题文档的关系

这篇文档不替代以下两篇：

1. `sq_bank_conflict_replay.md`
   - 它解释的是最基础、最纯的 `BC replay smoke`
2. `sq_matchinvalid_nuke_case_analysis.md`
   - 它解释的是 `matchInvalid + nuke` 这条路径为什么要用“bare older store + translated younger load”来稳定构造

本文档关注的是：

1. 当前新增的 `pipeline probe` testcase 如何把这些基础构件重新组织成更细粒度的 probe 场景
2. probe 型 testcase 的断言现在到底收紧到了什么程度

## 7. 推荐阅读顺序

如果读者是第一次接触这批场景，建议按以下顺序阅读：

1. `src/test/python/MemBlock/docs/sq_bank_conflict_replay.md`
2. `src/test/python/MemBlock/docs/sq_matchinvalid_nuke_case_analysis.md`
3. `src/test/python/MemBlock/docs/scalar_load_pipeline_probe_cases.md`
4. `src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py`

这样会先建立单一机制的直觉，再去理解 probe 组合场景。
