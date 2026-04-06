# MemBlock 当前覆盖率结果总结

## 1. 文档目的

本文档记录 `src/test/python/MemBlock/tests/` 当前真实 DUT 测试集在 toffee 机制下生成的最新代码覆盖率与功能覆盖率结果，并对这些结果做面向 MemBlock/LSU 标量 ld/st 验证的分析。本文档的目标不是简单抄录百分比，而是回答以下问题：

1. 当前测试到底覆盖到了多少真实 DUT 代码。
2. 对“标量 load/store pipeline 白盒功能验证”来说，当前覆盖率是高还是低。
3. 哪些模块已经被充分激活，哪些模块仍然存在明显空洞。
4. 覆盖率结果如何映射到后续用例设计优先级。

## 2. 记录时间与版本

- 记录时间：2026-04-06（Asia/Shanghai）
- 覆盖率执行环境：`/nfs/share/unitychip/`
- Git branch：`memblock_ut`
- Git commit：`1c9e965b26fd0318129f39239985253eeeb62c2e`
- Commit 摘要：`test(memblock): add ROB function coverage via toffee`

## 3. 覆盖率执行命令与产物

### 3.1 pytest + toffee 报告命令

```bash
source /nfs/share/unitychip/activate >/dev/null 2>&1 || true
pytest -q src/test/python/MemBlock/tests \
  --toffee-report \
  --report-dir src/test/python/MemBlock/data/toffee_report_run \
  --report-name memblock_rob_cov \
  --report-dump-json
```

### 3.2 行覆盖率 HTML 生成命令

由于首次 toffee 汇总阶段使用的 `genhtml` 缺少 `DateTime.pm`，导致 `toffee_report.json` 中 line coverage 一度显示为 `0/0`。在 home 目录补齐 Perl 模块并重新 `source ~/.profile` 后，使用下面命令对 toffee 已合并生成的 `merged.info` 重新跑了一次 `genhtml`：

```bash
source ~/.profile >/dev/null 2>&1 || true
genhtml --branch-coverage \
  src/test/python/MemBlock/data/toffee_report_run/line_dat/merged.info \
  -o src/test/python/MemBlock/data/toffee_report_run/line_dat
```

### 3.3 主要报告产物

- toffee HTML 报告：`src/test/python/MemBlock/data/toffee_report_run/memblock_rob_cov`
- toffee JSON 报告：`src/test/python/MemBlock/data/toffee_report_run/toffee_report.json`
- DUT 行覆盖率 HTML：`src/test/python/MemBlock/data/toffee_report_run/line_dat/index.html`
- DUT lcov 汇总：`src/test/python/MemBlock/data/toffee_report_run/line_dat/merged.info`
- DUT 覆盖率摘要 JSON：`src/test/python/MemBlock/data/toffee_report_run/line_dat/code_coverage.json`

## 4. 测试执行结果概况

- 执行用例数：`48`
- 用例状态：`48/48 PASSED`

这说明当前覆盖率分析基于一轮完整通过的真实 DUT 回归，而不是基于中途失败或半收敛的测试数据。

## 5. 功能覆盖率结果

### 5.1 总体结果

本轮 ROB 相关 function coverage 通过 toffee 官方 `set_func_coverage` 机制汇总，结果如下：

- Group：`2/4`
- Point：`29/32`
- Bin：`29/32`

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

- 覆盖结果：`3/4`

已命中：

- `gap_mmio_busy_without_commit_bool`
- `gap_backend_feedback_without_model`
- `gap_replay_without_redirect_cancel_model`

未命中：

- `gap_mixed_commit_window_without_packet_model`

解读：

这组不是“功能通过”，而是“当前 DUT 已经暴露出这些真实窗口，但环境模型仍存在缺口”。当前结果说明：

- MMIO busy 相关窗口确实出现了。
- backend feedback（`sqDeq/lqDeqPtr/sqDeqPtr`）相关真实行为确实出现了。
- replay / violation / release 恢复窗口已经出现。

但：

- 当前测试尚未明确命中“同拍混合 `lcommit + scommit` commit packet”这一缺口。

因此，ROB 模型的下一个增强点之一应继续围绕 mixed commit packet 设计 directed case。

#### `MemBlock.ROB.KnownModelGaps`

- 覆盖结果：`6/6`

命中项包括：

- `known_gap_pending_ptr_next_not_modelled`
- `known_gap_commit_bool_not_modelled`
- `known_gap_non_mem_blocker_not_modelled`
- `known_gap_mixed_lcommit_scommit_not_modelled`
- `known_gap_redirect_cancel_not_modelled`
- `known_gap_backend_feedback_credit_not_modelled`

解读：

这组不是执行时“偶然命中”，而是用来把 `rob_model.md` 里已经确认的当前模型缺口，稳定挂入 toffee 功能覆盖报告。它保证阅读报告的人不会因为 `ObservedBehavior` 命中率高，就误以为 ROB forward model 已经接近真实后端。当前真实结论仍是：

- 基础 mem-only commit frontier proxy 可用；
- 但还不是完整 ROB 模型。

## 6. DUT 行覆盖率结果

### 6.1 总体结果

基于 `code_coverage.json` 与重新生成的 `genhtml` HTML 页面，当前 DUT 行/分支覆盖率为：

- Source files：`314`
- Line coverage：`63.6%`（`187993 / 295581`）
- Branch coverage：`55.9%`（`878905 / 1573403`）

这是当前最可信的 DUT 代码覆盖率结果。

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

- `StoreUnit.sv`：line `86.38%`，branch `73.03%`
- `MainPipe.sv`：line `89.26%`，branch `64.71%`
- `LsqWrapper.sv`：line `80.18%`，branch `93.33%`
- `MemBlock.sv`：line `68.50%`，branch `57.61%`

解读：

store execute 和 wrapper 主路径整体并不差，说明：

- STA/STD issue 到 store materialize 的基础路径已经被广泛激活；
- wrapper/顶层状态机的主流程也被有效覆盖；
- 但顶层 `MemBlock.sv` 仍然存在明显空洞，说明还有不少系统级边角路径没有被覆盖。

#### LSQ / replay / ordering 相关

- `LoadQueue.sv`：line `79.38%`
- `LoadQueueRAW.sv`：line `65.01%`，branch `71.86%`
- `LoadQueueRAR.sv`：line `62.28%`，branch `77.66%`
- `LoadQueueReplay.sv`：line `65.59%`，branch `70.49%`
- `VirtualLoadQueue.sv`：line `58.38%`，branch `42.89%`
- `ForwardModule.sv`：line `83.01%`，branch `70.00%`
- `ExceptionInfoGen.sv`：line `84.57%`，branch `75.00%`

解读：

这组数字说明 replay/RAW/RAR 不是没测到，而是“已经覆盖到基础功能，但距离打透复杂组合还差很远”。这与当前已有：

- RAW replay smoke
- RAR violation smoke
- release / nuke query 相关 case
- small replay mix smoke

是吻合的。当前 replay 相关模块已经进入“有覆盖基础，但仍需继续深挖”的阶段。

#### store queue / sbuffer / misalign

- `NewStoreQueue.sv`：line `58.13%`，branch `39.34%`
- `Sbuffer.sv`：line `84.42%`，branch `54.50%`
- `SbufferData.sv`：line `55.46%`，branch `52.79%`
- `StoreMisalignBuffer.sv`：line `58.77%`，branch `48.72%`

解读：

这是当前最值得关注的短板区。

当前环境已经覆盖了：

- cacheable store 基本路径
- store->load same-addr
- overwrite
- flushSb + drain

但明显还没有覆盖充分的包括：

- store queue 深层状态组合
- 更复杂的 sbuffer data merge / drain 组合
- misaligned scalar store
- 更复杂的 partial write / cross-line / exception / delay drain 场景

如果未来只想提升一个方向的覆盖价值，这一组是最高优先级。

#### uncache / MMIO / 外围路径

- `LoadQueueUncache.sv`：line `61.66%`，branch `51.45%`
- `Uncache.sv`：line `64.19%`，branch `53.80%`
- `UncacheEntry.sv`：line `82.74%`，branch `86.84%`

解读：

说明 uncache/MMIO 基础路径已经进入 DUT 覆盖，但深状态仍远不够。当前 smoke 级场景足以激活：

- 基本 request/response
- 基本 replay/uncache entry 生命周期

但对如下更复杂状态仍缺用例：

- 多 outstanding uncache entry
- mmioBusy 持续阻塞
- uncache 与普通 cacheable load/store 并发
- nc store 最终 drain 一致性

#### TLB / PTW / PMP

- `TLBFA.sv`：line `47.00%`，branch `40.49%`
- `TLBFA_1.sv`：line `45.56%`，branch `40.22%`
- `TLBFA_2.sv`：line `38.42%`，branch `42.66%`
- `PMP.sv`：line `50.77%`，branch `54.68%`
- `PtwCache.sv`：line `61.35%`，branch `28.11%`
- `L2TLB.sv`：line `48.97%`，branch `50.56%`
- `LLPTW.sv`：line `34.80%`，branch `43.45%`

解读：

这一组偏低并不意外，因为当前测试集重点不在 page walk / PMP / 复杂 TLB refill，而在 LSU 标量 ld/st 功能本身。对于“当前是否已完成完整的地址翻译与权限相关覆盖”这个问题，答案显然是否定的。但对于“当前是否已把标量 LSU 主功能覆盖起来”来说，这组低覆盖并不构成对当前阶段目标的直接否定。

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

1. `NewStoreQueue` / `SbufferData` / `StoreMisalignBuffer`
2. `VirtualLoadQueue` 与 replay 深状态组合
3. `LoadQueueUncache` / `Uncache` 的复杂交互
4. TLB / PTW / PMP 这类外围大块

其中，对当前“标量 ld/st pipeline 验证”最应该优先补的，不是最后一组，而是前两组半。

## 8. 当前结果对后续工作的直接指引

从覆盖率结果出发，后续最值得投入的方向依次是：

1. **store 深状态补强**
   - partial write
   - misaligned scalar store
   - cross-line store
   - deeper sbuffer/drain/backpressure

2. **replay 精细化**
   - replay cause 分类
   - RAW/RAR 更多重叠形式
   - replay 与 release / backpressure / wait-bit 组合

3. **uncache/MMIO 深化**
   - 多 outstanding
   - mmioBusy 持续阻塞
   - nc store 尾部 flush/drain 一致性

4. **translation / permission 子系统覆盖**
   - TLB miss / refill
   - PTW cache 分支
   - PMP allow/deny
   - page fault/access fault

详细用例补强清单见：`src/test/python/MemBlock/docs/coverage_todo.md`

## 9. 注意事项

1. `toffee_report.json` 中的 line coverage 在首次执行后曾显示 `0/0`，原因是当时 `genhtml` 缺失 `DateTime.pm`。这不是 DUT 没有生成覆盖率，而是 HTML 汇总阶段失败。
2. 在补齐 Perl 环境并重新执行 `genhtml` 后，`line_dat/index.html` 与 `code_coverage.json` 已能反映真实 DUT 覆盖率。
3. 后续若再次生成 toffee 报告，应确保 `~/.profile` 中的 Perl 本地模块环境已生效，以免再次出现 line coverage 汇总失败。
