# MemBlock 当前覆盖率结果总结

## 1. 文档目的

本文档记录 `src/test/python/MemBlock/tests/` 当前真实 DUT 测试集在 toffee 机制下生成的最新代码覆盖率与功能覆盖率结果，并对这些结果做面向 MemBlock/LSU 标量 ld/st 验证的分析。本文档的目标不是简单抄录百分比，而是回答以下问题：

1. 当前测试到底覆盖到了多少真实 DUT 代码。
2. 对“标量 load/store pipeline 白盒功能验证”来说，当前覆盖率是高还是低。
3. 哪些模块已经被充分激活，哪些模块仍然存在明显空洞。
4. 覆盖率结果如何映射到后续用例设计优先级。

## 2. 记录时间与版本

- 记录时间：2026-04-11（Asia/Shanghai，partial-store 补强后）
- 覆盖率执行环境：`/nfs/share/unitychip/`
- Git branch：`memblock_ut`
- Git commit：`9fab671f59f62f06c5b91ea0739efc69a48babb0`
- Commit 摘要：`refactor(memblock): remove legacy public clock apis`

### 2.0 当前 full 主口径说明

本文后续若无特殊说明，默认使用当前 full 主报告：

- `src/test/python/MemBlock/data/toffee_report_full/line_dat/index.html`
- `src/test/python/MemBlock/data/toffee_report_full/line_dat/rtl/index.html`

当前主报告时间戳为：

- `2026-04-29 08:46:21`

这意味着本文中的历史 snapshot 和定向 campaign 仍然保留，但它们的定位是：

- `2026-04-11/12`：说明 partial-store 与 store-misalign 补强时的历史收益
- `2026-04-27`：说明 `NewStoreQueue` 定向 campaign 的推进上限与剩余 blocker
- `toffee_report_full/line_dat`：说明当前 DCache 与 MemBlock 的默认 full 覆盖率现状

### 2.1 2026-04-12 增量快照（store-misalign 独立用例补强后）

在保留本篇 2026-04-11 详细分析主体不变的前提下，补记一轮 2026-04-12 的完整回归 coverage snapshot，便于追踪这次独立 `store-misalign` 补强的真实收益。

- 覆盖率命令：

```bash
python3 -m pytest -q src/test/python/MemBlock/tests \
  --toffee-report \
  --report-dir src/test/python/MemBlock/data/toffee_report_full_serial_20260412_store_misalign_cov \
  --report-name memblock_full_serial_20260412_store_misalign_cov \
  --report-dump-json
```

- 测试结果：`95 PASSED, 1 XFAIL`
- 报告产物：
  - `src/test/python/MemBlock/data/toffee_report_full_serial_20260412_store_misalign_cov/toffee_report.json`
  - `src/test/python/MemBlock/data/toffee_report_full_serial_20260412_store_misalign_cov/line_dat/index.html`
- 全局代码覆盖率：
  - line `65.3%`（`193043 / 295581`，相对 2026-04-11 `+71` hit）
  - branch `57.7%`（`907150 / 1573403`，相对 2026-04-11 `+560` hit）
- `StoreMisalignBuffer.sv`：
  - line `58.8%`（`325 / 553`）
  - branch `37.0%`（`1258 / 3404`）

这说明本轮新增的 cross-16B store-misalign testcase 与配套环境修复，已经把“可验证性”和“路径稳定性”补上；但从模块级百分比看，`StoreMisalignBuffer` 仍未被明显拉升，后续还需要继续命中更深的状态组合，而不是只补基础 offset/width 矩阵。

### 2.2 2026-04-12 focused cross-page 跟进（未重跑 full coverage）

在上述 full coverage snapshot 之后，又补做了一轮 focused cross-page store-misalign 验证，但**尚未**重新执行整套 coverage 回归，因此这里先只记录功能状态，不改写 2.1 的百分比数据。

- 聚焦命令：

```bash
python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_store_misalign.py
```

- 聚焦结果：`5 passed, 2 xfailed`
- 新增状态：
  - 已新增 2 条 cross-page scalar store-misalign case（`SD + 0xFFD`、`SH + 0xFFF`）
  - 这两条 case 都能把请求送进 `StoreMisalignBuffer`，并观测到：
    - store shadow materialize；
    - 首段 split mask 与页尾低半段字节数一致；
    - 两侧窗口 load compare 成功。
  - 但两条 case 最终都卡在 `flushSb` 后 `sbIsEmpty` 不归零，因此当前以 `strict xfail` 形式保留。

这说明当前环境对 **cross-page 正常路径的刺激与基础观测** 已经够用，至少能证明“不是 testcase 完全没打到路径”；但当前 DUT 仍未把 cross-page scalar store-misalign 的 drain/收口走通，所以它还不能被算作一个已经闭环的 coverage 补点。

### 2.3 2026-04-27 NewStoreQueue 定向补强快照（仍未达到 80%）

本轮围绕 `NewStoreQueue.sv` 做了一次更聚焦的 directed campaign，目标不是重跑整套 MemBlock，而是验证“补更多 SQ 深状态 testcase 后，这个模块到底能推到哪里”。本轮新增并验证的 testcase 主要包括：

- `test_api_MemBlock_store_queue_two_wave_commit_frontier_residency_directed`
- `test_api_MemBlock_cross16b_partial_store_burst_batched_commit_directed`
- `test_api_MemBlock_vector_unit_stride_store_masked_inactive_flush_regression`
- `test_api_MemBlock_vector_unit_stride_store_nonzero_vstart_flush_regression`

其中：

- `scalar_store_pipeline.py` 聚焦回归结果：`22 passed, 7 xfailed`
- `vector_store.py` 聚焦回归结果：`3 xfailed`

覆盖率统计采用 `docs/coverage_report_workflow.md` 中记录的手工 LCOV 合并口径，而不是等待 `toffee-report` 末尾的 HTML 收口：

- store/replay/uncache/order campaign 手工合并产物：
  - `src/test/python/MemBlock/data/toffee_report_sq_push_extended_manual_20260427/line_dat/merged.info`
- 再叠加本轮新增 vector-store testcase 后的合并产物：
  - `src/test/python/MemBlock/data/toffee_report_sq_push_extended_plus_vector_newcases_manual_20260427/line_dat/merged.info`

量化结果如下：

- store/replay/uncache/order campaign：
  - 全局 line `67.4589%`（`188721 / 279757`）
  - `NewStoreQueue.sv` line `77.1043%`（`9362 / 12142`）
  - `NewStoreQueue.sv` branch `67.8647%`（`35084 / 51697`）
- 再叠加 2 条新增 vector-store control-path xfail 后：
  - 全局 line `67.6630%`（`189292 / 279757`）
  - `NewStoreQueue.sv` line `77.1207%`（`9364 / 12142`）

这一轮结论很明确：

1. 仅靠标量 partial-store / delayed-flush / replay / ordering 补强，`NewStoreQueue` 已经能从历史文档里的 `58.4%` 级别推进到当前 campaign 的 `77.1%` 左右。
2. 但要继续冲过 `80%`，剩余缺口已经不再是“再堆几条标量 partial smoke”能解决的问题。
3. 当前平台的主要瓶颈已经收敛为：
   - vector-store 控制路径（`vecInactive` / `vecMbCommit` / split dequeue）覆盖不足；
   - 部分 cross-16B / cross-page / vector drain 在真实 DUT 上仍会卡在 `flushSb -> sbIsEmpty`；
   - 非 `cbo.zero` 的 CBO 状态机路径仍缺 directed 入口。

## 3. 覆盖率执行命令与产物

### 3.1 pytest + toffee 报告命令

```bash
source /nfs/share/unitychip/activate >/dev/null 2>&1 || true
pytest -q src/test/python/MemBlock/tests \
  --toffee-report \
  --report-dir src/test/python/MemBlock/data/toffee_report_full_serial_20260411_partial_store_cov \
  --report-name memblock_full_serial_20260411_partial_store_cov \
  --report-dump-json
```

### 3.2 行覆盖率 HTML 生成命令

由于首次 toffee 汇总阶段使用的 `genhtml` 缺少 `DateTime.pm`，导致 `toffee_report.json` 中 line coverage 一度显示为 `0/0`。在 home 目录补齐 Perl 模块并重新 `source ~/.profile` 后，使用下面命令对 toffee 已合并生成的 `merged.info` 重新跑了一次 `genhtml`：

```bash
source ~/.profile >/dev/null 2>&1 || true
genhtml --branch-coverage \
  src/test/python/MemBlock/data/toffee_report_full_serial_20260411_partial_store_cov/line_dat/merged.info \
  -o src/test/python/MemBlock/data/toffee_report_full_serial_20260411_partial_store_cov/line_dat
```

### 3.3 主要报告产物

- toffee HTML 报告：`src/test/python/MemBlock/data/toffee_report_full_serial_20260411_partial_store_cov/memblock_full_serial_20260411_partial_store_cov`
- toffee JSON 报告：`src/test/python/MemBlock/data/toffee_report_full_serial_20260411_partial_store_cov/toffee_report.json`
- DUT 行覆盖率 HTML：`src/test/python/MemBlock/data/toffee_report_full_serial_20260411_partial_store_cov/line_dat/index.html`
- DUT lcov 汇总：`src/test/python/MemBlock/data/toffee_report_full_serial_20260411_partial_store_cov/line_dat/merged.info`
- DUT 覆盖率摘要 JSON：`src/test/python/MemBlock/data/toffee_report_full_serial_20260411_partial_store_cov/line_dat/code_coverage.json`

## 4. 测试执行结果概况

- 执行用例数：`82`
- 用例状态：`81 PASSED, 1 XFAIL`

这说明当前覆盖率分析基于一轮完整通过的真实 DUT 回归，而不是基于中途失败或半收敛的测试数据。

与上一版覆盖率统计相比，本轮新增了 8 个用例：

- partial word store + aligned full load
- high-offset byte store + aligned full load
- same-dword multi-byte partial merge
- full store + high-offset partial overwrite
- interleaved partial stores across two addresses
- partial-store 请求模型单测 3 条

## 5. 功能覆盖率结果

### 5.1 总体结果

本轮 ROB 相关 function coverage 通过 toffee 官方 `set_func_coverage` 机制汇总，结果如下：

- Group：`2/4`
- Point：`25/29`
- Bin：`25/29`

与上一版完整回归相比，这三项结果保持不变。说明本轮 partial-store 补强主要提升的是 RTL 代码路径覆盖，而不是现有 ROB function coverage 定义中的新 point/bin 命中。

### 5.2 分组结果

#### `MemBlock.ROB.ObservedBehavior`

- 覆盖结果：`15/15`

命中点包括：

- `store_commit_frontier_observed`
- `load_commit_budget_observed`
- `load_writeback_observed`
- `flushsb_command_observed`
- `sbuffer_drain_observed`
- `outer_drain_observed`
- `mmio_busy_observed`
- `mmio_store_shadow_observed`
- `replay_event_observed`
- `memory_violation_observed`
- `raw_nuke_query_observed`
- `rar_nuke_query_observed`
- `rar_nuke_response_observed`
- `release_hint_observed`
- `backend_feedback_observed`

解读：

这说明当前真实 DUT 测试已经不是只打到 issue/writeback 这种浅路径，而是的确覆盖到了：

- store commit frontier
- load commit budget
- sbuffer drain
- outer write drain
- replay/violation
- RAW/RAR nuke query
- backend feedback 指针

从功能观测角度，ROB 半模型当前关心的核心 DUT 外显事实已经全部被命中过。

#### `MemBlock.ROB.CurrentModel`

- 覆盖结果：`5/7`

已命中：

- `same_addr_store_then_load_observed`
- `overwrite_store_then_load_observed`
- `unrelated_store_then_load_observed`
- `mixed_load_store_window_observed`
- `cacheable_store_flush_drain_observed`

未命中：

- `nc_store_flush_drain_observed`
- `mmio_store_excluded_from_drain_observed`

解读：

这一组最能反映“当前环境真正已经用真实 DUT 跑到哪里”。结果表明：

1. cacheable scalar store/load 的程序序与可见性验证已经明显打穿了。
2. 同地址覆盖写、无关地址 store/load 混合、小规模 mixed ld/st 等路径都已经进入真实覆盖。
3. 当前薄弱点主要在：
   - nc store 尾部 flush/drain
   - MMIO store 的“明确不进入最终 drain compare”闭环验证

也就是说，当前第一阶段的“cacheable scalar ld/st + replay 基础路径”已经基本立住，但 NC/MMIO 这一圈仍是明显空洞。

#### `MemBlock.ROB.GapObserved`

- 覆盖结果：`2/4`

已命中：

- `gap_backend_feedback_without_model`
- `gap_replay_without_redirect_cancel_model`

未命中：

- `gap_mmio_busy_without_commit_bool`
- `gap_mixed_commit_window_without_packet_model`

解读：

这组不是“功能通过”，而是“当前 DUT 已经暴露出这些真实窗口，但环境模型仍存在缺口”。当前结果说明：

- backend feedback（`sqDeq/lqDeqPtr/sqDeqPtr`）相关真实行为确实出现了。
- replay / violation / release 恢复窗口已经出现。

但：

- 当前测试尚未稳定把 `mmioBusy` 长窗口留成可重复命中的 directed case。
- 当前测试尚未明确命中“同拍混合 `lcommit + scommit` commit packet”这一缺口。

因此，ROB 模型的下一个增强点之一应继续围绕 mixed commit packet 设计 directed case。

#### `MemBlock.ROB.KnownModelGaps`

- 覆盖结果：`2/2`

命中项包括：

- `known_gap_redirect_cancel_not_modelled`
- `known_gap_backend_feedback_credit_not_modelled`

解读：

这组不是执行时“偶然命中”，而是用来把 `rob_model.md` 里已经确认的当前模型缺口，稳定挂入 toffee 功能覆盖报告。它保证阅读报告的人不会因为 `ObservedBehavior` 命中率高，就误以为 ROB forward model 已经接近真实后端。当前真实结论仍是：

- 当前 ROB 半模型已经支持：
  - `pending_ptr_next`
  - `commit bool`
  - mixed `lcommit/scommit` packet
  - `non-mem blocker`
  - `store commit readiness`
- 但还不是完整 ROB 模型，剩余缺口主要集中在：
  - redirect / flush / cancel 恢复
  - backend feedback / credit 闭环

## 6. DUT 行覆盖率结果

### 6.1 总体结果

基于 `code_coverage.json` 与新生成的 `genhtml` HTML 页面，当前 DUT 行/分支覆盖率为：

- Source files：`314`
- Line coverage：`65.3%`（`192972 / 295581`）
- Branch coverage：`57.6%`（`906590 / 1573403`）

这是当前最可信的 DUT 代码覆盖率结果。

与上一版完整 coverage 基线相比，本轮量化增量为：

- Line hit：`+13`
- Branch hit：`+294`

也就是说，本轮 partial-store 补强确实让真实 DUT 多走到了一些新路径，但当前增量仍属于“小幅、集中式提升”，而不是会显著改写全局百分比的大跳升。

### 6.2 与当前目标最相关模块的覆盖率

以下模块与当前“标量 load/store pipeline 白盒功能验证”最直接相关。

#### 主 load pipeline / execute 主路径

- `NewLoadUnit.sv`：line `90.54%`，branch `83.33%`
- `LoadPipe.sv`：line `92.88%`，branch `90.00%`
- `LoadUnitS0.sv`：line `94.77%`，branch `85.29%`
- `LoadUnitS1.sv`：line `91.30%`，branch `79.17%`
- `LoadUnitS2.sv`：line `92.75%`，branch `87.50%`
- `LoadUnitS3.sv`：line `93.30%`，branch `65.22%`
- `LoadUnitS4.sv`：line `92.16%`

解读：

这组结果说明，普通标量 load 主流水线已经被打得比较深，尤其是 cacheable scalar load 主路径。对于“load 真的在真实 DUT 里跑起来并覆盖了关键阶段状态”这一问题，当前覆盖率给出的答案是明确的：是。

#### 主 store pipeline / wrapper

- `NewStoreUnit.sv`：line `84.6%`，branch `68.7%`
- `MainPipe.sv`：line `90.7%`，branch `74.5%`
- `LsqWrapper.sv`：line `84.8%`，branch `74.2%`
- `MemBlock.sv`：line `81.3%`，branch `65.5%`

解读：

store execute 和 wrapper 主路径整体并不差，说明：

- STA/STD issue 到 store materialize 的基础路径已经被广泛激活；
- wrapper/顶层状态机的主流程也被有效覆盖；
- 但顶层 `MemBlock.sv` 仍然没有达到“几乎打透”的程度，说明系统级边角路径和若干未闭环功能簇仍在拖低总体覆盖率。

#### LSQ / replay / ordering 相关

- `LoadQueue.sv`：line `86.8%`，branch `72.1%`
- `LoadQueueRAW.sv`：line `59.5%`，branch `50.7%`
- `LoadQueueRAR.sv`：line `78.0%`，branch `66.2%`
- `LoadQueueReplay.sv`：line `75.4%`，branch `66.5%`
- `VirtualLoadQueue.sv`：line `63.7%`，branch `63.1%`
- `ForwardModule.sv`：line `83.4%`，branch `61.6%`
- `ExceptionInfoGen.sv`：line `86.1%`，branch `59.7%`

解读：

这组数字说明 replay/RAW/RAR 不是没测到，而是“已经覆盖到基础功能，但距离打透复杂组合还差很远”。这与当前已有：

- RAW replay smoke
- RAR violation smoke
- release / nuke query 相关 case
- small replay mix smoke

是吻合的。当前 replay 相关模块已经进入“有覆盖基础，但仍需继续深挖”的阶段。

#### store queue / sbuffer / misalign

- `NewStoreQueue.sv`：line `77.7%`，branch `67.4%`
- `Sbuffer.sv`：line `92.6%`，branch `78.8%`
- `SbufferData.sv`：line `81.4%`，branch `62.4%`
- `DCache.sv`：line `66.9%`，branch `67.8%`

解读：

与 `2026-04-11/12` 历史 snapshot 相比，这一组的口径已经明显变化：

- `NewStoreQueue` 与 `SbufferData` 不再是 `58%/56%` 级别的“当前低覆盖模块”
- 当前 full 主报告说明，标量 partial-store、batched commit、flush/drain 主路径已经把它们推到中高覆盖区
- 因而 DCache store-side 的主要瓶颈，已经从“基础 partial-store 根本没打到”转成“更深的 vector/CBO/misalign/known-bug gating path 没闭环”

当前环境已经覆盖了：

- cacheable store 基本路径
- store->load same-addr
- overwrite
- flushSb + drain

但明显还没有覆盖充分的包括：

- 向量 store control-path 与最终 drain
- 非 `cbo.zero` 的 CBO 状态机入口
- cross-page scalar store-misalign 的 drain 闭环
- 更复杂的 partial write / cross-line / exception / delayed drain 场景

需要特别说明的是：

- 历史 focused snapshot 中的 `StoreMisalignBuffer.sv` 仍然有参考价值，尤其是 `2026-04-12` store-misalign 补强后记录到的 `58.8% / 37.0%`
- 但当前 full 主报告已经不再导出独立 `StoreMisalignBuffer.sv` 统计，因此后续文档中再提到它时，应理解为“历史 misalign 功能簇标签”，而不是当前 full 报告中的独立模块项
- 当前 misalign 相关 gap 应主要通过 `DCache.sv`、`NewStoreQueue.sv`、`SbufferData.sv` 以及 `BUGS.md` 中的 cross-page reproducer 来解释

换句话说，当前已经不能再把 `NewStoreQueue/SbufferData` 简单归类为“当前最明显的数字短板”；真正仍需要单独追踪的是：

- DCache 内部仍未闭环的 vector/CBO/misalign 功能簇
- `DCache.sv` 整体 line coverage 仍只有 `66.9%`
- `CtrlUnit.sv` branch coverage 偏低
- 以及 uncache / NC / MMIO 的深状态与异常导出路径

#### DCache 主体 / 控制 / 一致性

- `DCache.sv`：line `66.9%`，branch `67.8%`
- `DCacheWrapper.sv`：line `85.6%`，branch `77.1%`
- `MainPipe.sv`：line `90.7%`，branch `74.5%`
- `MissQueue.sv`：line `89.7%`，branch `75.0%`
- `ProbeQueue.sv`：line `87.0%`，branch `56.3%`
- `ProbeEntry.sv`：line `98.0%`，branch `59.9%`
- `WritebackQueue.sv`：line `78.6%`，branch `65.9%`
- `CtrlUnit.sv`：line `69.7%`，branch `36.8%`

解读：

这组数字说明 DCache 主体并不是“只有 wrapper 被点亮、内部基本没跑”的状态。当前 full 回归已经实际打到了：

- cacheable load/store 主流水线
- miss/refill
- probe/release/writeback 基础路径
- store-side sbuffer / SQ 数据主路径

但也同样清楚地暴露出两类空洞：

1. `CtrlUnit` 等控制/仲裁分支仍有明显未覆盖组合。
2. vector store、CBO、cross-page misalign、PBMT-NC/MMIO 深语义这类路径虽然已有 testcase 触达，但功能闭环尚未完成。

#### uncache / MMIO / 外围路径

- `LoadQueueUncache.sv`：line `63.0%`，branch `60.7%`
- `Uncache.sv`：line `66.8%`，branch `46.6%`
- `UncacheEntry.sv`：line `86.6%`，branch `76.8%`

解读：

说明 uncache/MMIO 基础路径已经进入 DUT 覆盖，但深状态仍远不够。当前 smoke 级场景足以激活：

- 基本 request/response
- 基本 replay/uncache entry 生命周期

但对如下更复杂状态仍缺用例：

- 多 outstanding uncache entry
- mmioBusy 持续阻塞
- uncache 与普通 cacheable load/store 并发
- PBMT-NC store/load 的 debug 分类与最终 drain 一致性

#### TLB / PTW / PMP

- `TLBFA.sv`：line `85.8%`，branch `59.9%`
- `TLBFA_1.sv`：line `53.1%`，branch `34.4%`
- `TLBFA_2.sv`：line `44.5%`，branch `33.9%`
- `PMP.sv`：line `97.0%`，branch `87.6%`
- `PtwCache.sv`：line `81.3%`，branch `57.7%`
- `L2TLB.sv`：line `87.1%`，branch `71.3%`
- `LLPTW.sv`：line `70.0%`，branch `57.9%`

解读：

与历史文档里的早期数字相比，这一组当前已经不再是“几乎没碰到”的状态。当前 full 主报告说明：

- translation / permission 相关路径已经被真实 DUT 回归显著点亮；
- 但这并不等于所有 fault/deny/guest/NC 语义都已闭环；
- 当前未闭环点主要体现在 `BUGS.md` 已列出的 store-side PMP deny、PBMT-NC 分类与 paddr/shadow 导出等问题。

## 7. 当前覆盖率的总体判断

### 7.1 可以肯定的部分

根据本轮功能覆盖率与 DUT 行覆盖率，当前可以比较有把握地说：

1. 真实 DUT 下的标量 load 主流水线已经有较高覆盖，不再是简单 smoke。
2. cacheable scalar store 的基本功能闭环已经覆盖到：
   - store materialize
   - store->load same-addr
   - overwrite
   - flushSb + drain
3. replay / RAW / RAR 已经进入“有覆盖且有效”的阶段，而不是完全空白。
4. 这套环境确实在用真实 DUT 做验证，不是 mock-only scoreboard 自测。

### 7.2 当前最明显的短板

最明显的短板是：

1. DCache 内部未闭环的 vector store / CBO / cross-page misalign 功能簇
2. `CtrlUnit`、`ProbeQueue` 这类控制/一致性路径的深分支组合
3. `LoadQueueUncache` / `Uncache` 的复杂交互与 PBMT-NC/MMIO 深语义
4. `VirtualLoadQueue` 与 replay 深状态组合

其中，对当前“标量 ld/st pipeline 验证”最应该优先补的，不是 translation 大盘，而是前 3 组，尤其是那些 testcase 已经触达但仍未闭环的已知路径。

## 8. 当前结果对后续工作的直接指引

从覆盖率结果出发，后续最值得投入的方向依次是：

1. **store 深状态补强**
   - 当前 `NewStoreQueue` / `SbufferData` 已经不属于“低百分比基础短板”，后续重点应转向深状态和未闭环行为
   - 下一步优先补的仍是 cross-16B/cross-beat store、更深的 drain/backpressure，以及更复杂的 partial merge 顺序
   - 对 DCache 来说，更关键的是把 vector/CBO/misalign 这几条已知 gating path 从“触达但不闭环”推进到可稳定回归

2. **replay 精细化**
   - replay cause 分类
   - RAW/RAR 更多重叠形式
   - replay 与 release / backpressure / wait-bit 组合

3. **uncache/MMIO 深化**
   - 多 outstanding
   - mmioBusy 持续阻塞
   - PBMT-NC store/load 的 debug 分类、paddr/shadow 与尾部 flush/drain 一致性
   - MMIO store exclusion 的 final drain 语义

4. **request model 能力补齐**
   - 当前这块的基础能力已经补齐：`StoreTxn.mask` 可直接下沉为标量 `SB/SH/SW/SD` issue 宽度
   - 已新增 `0x0F` partial word 与高偏移 `0x01` byte store directed case，证明 partial-store 路径可稳定构造
   - 下一步不再是先补接口，而是把 partial-store 场景矩阵扩到多次 merge、full-store 覆写和多地址交织

5. **已知未闭环功能点转正**
   - vector store data path
   - `cbo.zero` / 非 `cbo.zero` CBO
   - cross-page scalar store-misalign
   - PBMT-NC store/load
   - store-side PMP deny/fault

详细用例补强清单见：`src/test/python/MemBlock/docs/coverage_todo.md`

## 9. 注意事项

1. `toffee_report.json` 中的 line coverage 在首次执行后曾显示 `0/0`，原因是当时 `genhtml` 缺失 `DateTime.pm`。这不是 DUT 没有生成覆盖率，而是 HTML 汇总阶段失败。
2. 在补齐 Perl 环境并重新执行 `genhtml` 后，`line_dat/index.html` 与 `code_coverage.json` 已能反映真实 DUT 覆盖率。
3. 后续若再次生成 toffee 报告，应确保 `~/.profile` 中的 Perl 本地模块环境已生效，以免再次出现 line coverage 汇总失败。
