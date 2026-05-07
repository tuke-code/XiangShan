# MemBlock 覆盖率补强待办清单

## 1. 文档目的

本文档根据 `coverage_summary.md` 中记录的当前 toffee 覆盖率结果，为 `src/test/python/MemBlock/` 规划下一批最值得补的真实 DUT 用例。原则不是“哪里低就盲补哪里”，而是优先补那些：

1. 与标量 ld/st pipeline 验证目标直接相关；
2. 当前覆盖率明显偏低；
3. 可能真正提升白盒验证价值，而不只是堆代码行数；
4. 能在现有真实 DUT 环境中比较自然落地。

当前若无特殊说明，优先级判断默认跟随当前 full 主报告：

- `src/test/python/MemBlock/data/toffee_report_full/line_dat/rtl/index.html`

历史定向 campaign 和 focused snapshot 仍然保留，但它们的定位是解释“某一轮定向补强打到了哪里”，而不是覆盖当前 full 主报告中的所有事实。文中若继续引用 `StoreMisalignBuffer.sv`，默认应理解为历史 misalign 功能簇标签；当前 full 主报告已经不再提供该独立模块统计，后续应更多按行为 gap 而不是旧模块名理解该项待办。

当前 atomic/AMO 口径补充说明：

- single-uop AMO smoke 已补入 `amoadd/amoswap/amoxor`；当前 real-DUT 用例会在 `dcache_a_request_count == 0 && writeback_events == 0` 时定向 `xfail`，记录 standalone `intIssue` contract gap
- 剩余 atomic gap 主要收敛到 LR/SC、AMOCAS 和更深 atomic replay/exception 语义

## 2. 优先级说明

- `P0`：当前最该补，直接影响标量 ld/st 验证深度
- `P1`：紧接着应补，能显著完善 replay / uncache / ordering 语义
- `P2`：中期补强，更偏系统级外围与复杂翻译

## 3. P0：store 深状态与 sbuffer/drain 补强

### 3.0 2026-04-27 NewStoreQueue 定向 campaign 快照

本轮已经新增并验证的 directed case：

1. `store_queue_two_wave_commit_frontier_residency_directed`
2. `cross16b_partial_store_burst_batched_commit_directed`
3. `vector_unit_stride_store_masked_inactive_flush_regression`
4. `vector_unit_stride_store_nonzero_vstart_flush_regression`

配套量化结果（采用 `coverage_report_workflow.md` 中的手工 LCOV 合并口径）：

- `NewStoreQueue.sv` line `77.1043%`（store/replay/uncache/order campaign）
- `NewStoreQueue.sv` line `77.1207%`（再叠加新增 vector-store control-path xfail）

这说明当前 `>80%` 的剩余缺口已经基本不是“再补几条标量 partial-store”问题，而是下面 3 类特定路径：

1. vector store control-path
   - `vecInactive`
   - `vecMbCommit`
   - split dequeue / pointer move
2. 已知 DUT flush stall
   - batched cross-16B scalar store
   - vector store drain
   - cross-page scalar store-misalign
3. CBO 深路径
   - non-zero CBO 已有 `flushSb -> sendReq -> waitResp -> writeback` directed smoke
   - 仍缺 `cbo.zero` 多 entry drain 与更深 cache-state 语义验证

因此本清单从本轮开始需要把“vector/CBO/known-bug gating path”放到和标量 partial-store 同级的优先级，而不是继续默认 `NewStoreQueue` 的主瓶颈仍在基础 byte-mask 矩阵。

### 3.0 2026-04-11 进展快照

本轮已经落地并验证通过的 directed case：

1. misaligned store + dual overlap load
2. store burst + interleaved load + final flush
3. MMIO + cacheable store mixed-path smoke
4. partial word store + aligned full load
5. high-offset byte store + aligned full load
6. same-dword multi-byte partial merge
7. full store + high-offset partial overwrite
8. interleaved partial stores across two addresses

这些用例已经把 store 深状态往前推进了一步，但仍未打完当前 P0 区域。下面各项继续保留，只是优先级有所收敛：

- `P0-2` / `P0-3` 由“完全未覆盖”变成“已有基础 directed case，但还没打透”
- `P0-1` 仍然是最缺的一块，但当前障碍已经从“接口能力缺失”收敛为“场景矩阵还没铺开”

### P0-1 标量 partial-mask store 覆盖组

#### 目标模块

- `NewStoreQueue.sv`
- `SbufferData.sv`
- `Sbuffer.sv`
- `StoreUnit.sv`

#### 当前动机

虽然已经有普通 8B store、overwrite、flushSb + drain，但目前对不同 byte-mask 组合的深状态覆盖仍然不足。当前 full 主报告里，`SbufferData.sv` 与 `NewStoreQueue.sv` 已经分别达到 `81.4%` / `77.7%` line coverage，因此这里的核心矛盾已经不再是“基础 partial-store 根本没打到”，而是：

- byte mask 不同分布没有打全；
- 同一 cache line 内多次 partial write merge 没有打透；
- 同地址不同掩码覆盖顺序没有充分验证。

当前环境已经把 `StoreTxn.mask` 下沉到标量 `SB/SH/SW/SD` issue 宽度，并补入了 `mask=0x0F` 与高偏移 `mask=0x01` 两条 directed smoke。因此，这一组后续的重点不再是“先把接口打通”，而是把 partial-store 的覆盖矩阵真正做完整。

#### 建议新增用例

1. 单 cache line 内连续 partial-byte store
   - 如同一 8B 字上依次写 `0x01`、`0x02`、`0x04`、`0x08` mask
   - 之后 load 验证合并结果
2. partial store overwrite full load
   - 先 partial store，再 full-width load
3. full store 后 partial overwrite
   - 验证老值部分保留、新值部分覆盖
4. 多地址交织 partial store
   - 避免只覆盖同一地址 merge

#### 预期收益

- 提升 `NewStoreQueue` 与 `SbufferData` 行/分支覆盖率；
- 增强 memory model 对 masked write 语义的真实 DUT 对应性；
- 提升 store->load compare 的说服力。

#### 当前状态

- 仍是 `P0` 第一优先级，但当前已从“接口缺失”推进到“已有基础矩阵、仍需继续补更多 merge 组合”
- 2026-04-11 新一轮全量 coverage 量化结果显示：
  - 全局仅增加 `+13` line hit、`+294` branch hit
  - 增量主要落在 `DCache` 与 `Sbuffer`
  - 当时 `NewStoreQueue` / `SbufferData` / `StoreMisalignBuffer` 仍基本横盘
- 但当前 full 主报告已经显示：
  - `NewStoreQueue.sv` line `77.7%`、branch `67.4%`
  - `SbufferData.sv` line `81.4%`、branch `62.4%`
- 因此这一组下一步的重点，不再是追求“把基础 partial-store 百分比从很低拉起来”，而是继续把新增增量推向更深的 merge、cross-beat 与 backpressure 组合
- 因此这一组下一步的重点，不再是继续堆“窗口内 partial overwrite smoke”，而是要主动构造：
  - cross-16B / cross-beat partial-store
  - 更深的多 store 并存与 merge 顺序
  - 与 backpressure / delayed drain 叠加的 partial-store

### P0-0 vector store control-path / CBO non-zero 状态机

#### 目标模块

- `NewStoreQueue.sv`

#### 当前动机

`2026-04-27` 的实测结果表明，标量 store/replay/uncache/order 已经把 `NewStoreQueue.sv` 推到 `77.1%` 左右，但继续加标量 case 收益已经明显变小。剩余缺口更像是：

- vector store 的 `vecInactive` / `vecMbCommit` 控制流没有被真正打热；
- non-zero CBO 的最小 directed case 已补齐，但更深 CBO 语义仍未打透；
- 一部分路径虽然“已经触达”，但最终卡在已知 `flushSb` stall，导致覆盖提升受限。

#### 建议新增用例

1. masked inactive vector unit-stride store
2. nonzero `vstart` vector unit-stride store
3. multi-uop / larger `numLsElem` vector store
4. `cbo.zero` 多 entry / richer CBO semantic matrix

#### 当前状态

- 前两条 vector case 已新增，但目前都只稳定推进到已知 vector drain bug 前的精确 `xfail`
- non-zero `cbo.clean/flush/inval` real-DUT smoke 已新增并转为硬断言通过
- 当前 CBO 剩余 gap 已从“缺 testcase 入口”收敛到“`cbo.zero` 多 entry drain + 更深 cache-state 语义”
- 当前 full 主报告里 `NewStoreQueue.sv` 已到 line `77.7%` / branch `67.4%`，因此这里的推进目标不应再写成“把 SQ 从很低的基线拉起来”，而应明确成“把剩余覆盖增量集中打到 vector/CBO/known-bug gating path”
- 因而下一步优先级应从“继续扩大标量 partial 矩阵”转向：
  - 提高 vector control-path 的可观测性
  - 深化 CBO 的 multi-entry / richer semantic coverage
  - 把当前 `flushSb` stall 类缺口拆成独立 DUT bug 跟踪项

### P0-2 cross-line / cross-beat scalar store

#### 目标模块

- `NewStoreQueue.sv`
- `SbufferData.sv`
- `StoreMisalignBuffer.sv`
- `DCache.sv`

#### 当前动机

当前普通 aligned store 已有覆盖，但 cross-line / cross-beat 场景显然不足，尤其对 `StoreMisalignBuffer` 和 sbuffer data 组织影响很大。

本轮已经补入“misaligned store + overlap load”与“store burst + final flush”两条 directed case，因此这里不再是完全空白；但真正跨 16B / 跨 beat 的 store 仍然缺失。

#### 建议新增用例

1. 末尾地址触发跨 16B 写窗口
2. 跨 32B beat 的 scalar store
3. 接近 64B line 末尾的 scalar store，再 flushSb
4. cross-line store 后 same-addr / overlap load

#### 预期收益

- 提升 `StoreMisalignBuffer.sv` 覆盖；
- 验证 drain 数据切分与最终合并一致性；
- 暴露可能的 mask/addr/data 对齐错误。

#### 当前状态

- 已部分覆盖，下一步重点从“同 16B 窗口 misalign”推进到“跨 16B / 跨 beat”
- 2026-04-12 已新增独立 `tests/test_MemBlock_store_misalign.py`，补入 5 条 cross-16B scalar store directed case，并同步修复环境对 split retire / sbuffer drain pair 的建模
- 但完整 coverage 重跑后，`StoreMisalignBuffer.sv` 仍停在 line `58.8%`、branch `37.0%`，说明当前新增 case 主要补上了验证闭环，还没有打到该模块更深层的状态组合
- 需要注意：上述 `StoreMisalignBuffer.sv` 数字来自历史 focused snapshot。当前 full 主报告已不再导出该独立模块，因此这部分后续优先级应按“cross-16B / cross-page misalign 行为 gap”理解，而不是按当前 full 报告中的单模块低百分比理解
- 同日继续补入 2 条 cross-page scalar store-misalign case（`SD + 0xFFD`、`SH + 0xFFF`），结果显示：
  - 当前 env 已足够把请求送入 `StoreMisalignBuffer`，并完成 shadow/load 级观测；
  - 但 real DUT 会卡在 `flushSb -> sbIsEmpty` 不清空，因此当前只能以 `strict xfail` 形式保留；
  - 这更像是当前 DUT 的 cross-page unalign 功能缺口，而不是 testcase 断言过严。

### P0-3 misaligned scalar store/load directed

#### 目标模块

- `StoreMisalignBuffer.sv`
- `StoreUnit.sv`
- `LoadUnitS*`
- `ExceptionInfoGen.sv`

#### 当前动机

`StoreMisalignBuffer.sv` 覆盖显著偏低，说明 misaligned scalar store 基本没有形成有体系的 directed 回归。

本轮已经补入一条 misaligned store 后接 dual overlap load 的 directed case，证明该路径可稳定构造；但覆盖率提升仍然有限，说明单条 same-window misalign 还不足以真正打透模块。

#### 建议新增用例

1. misaligned store 同 cache line
2. misaligned store 跨 line
3. misaligned store 后 aligned load
4. misaligned store 后 overlap load
5. 必要时加入会触发异常的 misalign case，与正常 case 分开

#### 预期收益

- 打开当前明显缺失的一整块路径；
- 帮助判断 misaligned scalar store 目前到底是“没测”还是“逻辑本来就简化”。

#### 当前状态

- 已有基础 directed case，仍保持 `P0`
- 2026-04-12 的独立 store-misalign 文件已经把 `SD(+0xD/+0xE/+0xF)`、`SW(+0xD)`、`SH(+0xF)` 的 cross-16B 基础矩阵补齐
- 当前结论从“没有体系化 cross-16B testcase”升级为“已有体系化 directed case，但 `StoreMisalignBuffer` 百分比仍横盘”
- 这里的“`StoreMisalignBuffer` 百分比仍横盘”同样应理解为历史 misalign 功能簇的 focused 观察，而不是当前 full 报告中的独立模块统计
- 因而下一步重点不再是继续堆基础 offset/width 组合，而是优先命中：
  - cross-page / exception overwrite
  - 更深的 split 状态停留与 dequeue 组合
  - 可能受 `pendingPtrNext` 影响的 next-boundary 路径
- 其中 cross-page normal path 已经有可重复 xfail 复现器，说明下一步可以拆成两条线并行：
  - 一条线继续追 DUT 修复，使现有 xfail 转正；
  - 另一条线补环境/观测，为 exception overwrite 与 redirect/cancel 提供更强定位能力。

### P0-4 store queue backpressure + delayed drain

#### 目标模块

- `NewStoreQueue.sv`
- `Sbuffer.sv`
- `SbufferData.sv`
- `MemBlock.sv`

#### 当前动机

当前 flush/drain 主要是顺畅路径，缺少：

- sbuffer/outer backpressure
- delayed drain
- committed store 与未及时 drain 并存

本轮的 store burst + interleaved load 用例已经覆盖了“多条 committed store 先存在，再让 younger load 通过，最后统一 flush/drain”的最小闭环，但还没有触到真正 backpressure / ready 抖动。

#### 建议新增用例

1. 连续 store burst，暂时不 flush，末尾统一 drain
2. flushSb 拉起前刻意让 younger load 穿插
3. drain 过程中再观测 store/load 是否出现异常行为
4. 如果环境支持，增加 outer/内部 ready 抖动

#### 预期收益

- 提升 sbuffer 深状态覆盖；
- 更接近真实“store committed != 已对外写出”的窗口；
- 对最终 drain 校验价值很高。

#### 当前状态

- 已有最小 directed 闭环；后续继续补 backpressure / ready 抖动

## 4. P1：replay / RAW / RAR / wait-bit 精细化补强

### P1-1 RAW replay 细分覆盖

#### 目标模块

- `LoadQueueRAW.sv`
- `LoadQueueReplay.sv`
- `VirtualLoadQueue.sv`

#### 当前动机

RAW 已有 smoke，但更像”证明机制存在”，还不是”把 cause/窗口打透”。

#### 当前进展 (2026-04-30)

- `VirtualLoadQueue.sv` 已补入 4 条定向 testcase（`tests/test_MemBlock_vlq_coverage.py`）：
  1. 同拍 8 端口 enqueue（命中 enqueue 端口 4-7 逻辑）
  2. 16 条 cacheable load 批量入队+集中 drain（命中高 commitCount 分支）
  3. redirect 取消在途 load（命中 needCancel / redirectCancelCount 路径）
  4. 分批 72 条 wrap-around 闭环（推动指针跨边界，覆盖 entry 0-71 的分配路径）
- 首轮覆盖提升已量化：line 63.7% → 68.1%（+299 行），branch 47.7% → 49.5%（+93 分支）
- 同时新增 `MemBlock_env.sample_vlq_state()` 为 VLQ 状态提供白盒观测口
- RAW replay 细分用例（同地址 full/partial overlap、older store committed/uncommitted 窗口等）仍待后续补入

#### 建议新增用例

1. 同地址 full overlap RAW
2. partial overlap RAW
3. older store addr ready 但 data 慢到
4. older store committed 与未 committed 两种窗口
5. replay 后再次执行命中正确值

#### 预期收益

- 提升 RAW replay cause 的分支覆盖；
- 提升 `VirtualLoadQueue` 的状态组合覆盖；
- 检查 replay 后 scoreboard/commit-boundary 语义是否仍正确。

### P1-2 RAR violation / release / nuke 细分覆盖

#### 目标模块

- `LoadQueueRAR.sv`
- `LoadQueueReplay.sv`
- `LoadQueue.sv`

#### 当前动机

RAR 基础 smoke 已有，但从覆盖率看仍属于“中等覆盖，明显未打透”。

#### 建议新增用例

1. 两条 load same line 同地址
2. 两条 load partial overlap
3. release 晚到/早到组合
4. nuke response backpressure
5. older load wait / younger load replay 组合

#### 预期收益

- 提升 RAR 相关 query/response 路径；
- 补 release 与 violation 的时序组合；
- 更好利用已有白盒 monitor。

### P1-3 load-wait / waitForRobIdx 定向场景

#### 目标模块

- `LoadQueueReplay.sv`
- `LoadQueue.sv`
- `LoadUnitS*`

#### 当前动机

现有环境已支持 `loadWaitBit` 与 `waitForRobIdx` 驱动，但目前用例数量有限。覆盖率也表明 replay 深状态仍需强化。

#### 建议新增用例

1. older load precise load-wait 暂停，younger load 不应越过
2. waitForRobIdx 命中与不命中两种情况
3. waitForRobIdx 与 replay/release 组合
4. 多条 load 串联等待

#### 预期收益

- 补足 load pipeline 中更接近真实后端调度约束的路径；
- 为后续 ROB 模型增强保留验证基础。

## 5. P1：uncache / MMIO 深状态补强

### P1-4 nc store flush/drain 闭环

#### 目标模块

- `LoadQueueUncache.sv`
- `Uncache.sv`
- `NewStoreQueue.sv`
- outer TL-A/TL-D 相关路径

#### 当前动机

function coverage 中 `nc_store_flush_drain_observed` 未命中，是当前最清晰的缺口之一。

本轮新增了 MMIO + cacheable mixed-path smoke，但并没有把真正的 `nc_store_flush_drain_observed` 打中。这说明当前缺口不是“只差一条简单混合用例”，而是仍需要找到可稳定构造的 non-cacheable store 场景，或先厘清当前 DUT/环境对 NC store 的可见区分方式。

当前进展补记：

- env 已新增 `enable_svpbmt()` 与 `install_sv39_mapping(..., page_size="4k", pbmt=..., page_table_page_addrs=...)`，测试现在可以显式请求 `cacheable / ncio / mmio` 三类 translated 地址属性。
- ROB coverage 中 `nc_store_flush_drain_observed` 的 collector 口径已收紧：不再把“任意 outer drain”当成 NC 命中，而要求 outer drain 与 `store.nc == True && !store.mmio` 的窗口真实重叠。
- 但当前 real DUT 下，`PBMT=NC` store 仍未稳定给出“translated paddr + stable NC shadow + flush closure”的完整闭环，因此新增 testcase 先以 capability probe + 精确 `xfail` 固化。

#### 建议新增用例

1. 单条 nc store + flushSb + outer drain compare
2. 多条 nc store burst + 最终 drain
3. nc store 与普通 cacheable load/store 混合
4. nc store 与 replay/commit 窗口组合

#### 预期收益

- 直接补命中 `nc_store_flush_drain_observed`
- 提升 `LoadQueueUncache.sv` / `Uncache.sv` 覆盖；
- 提升当前 memory model 关于 drain_log 的闭环说服力。

### P1-5 MMIO store exclusion 闭环

#### 目标模块

- `LoadQueueUncache.sv`
- `Uncache.sv`
- `NewStoreQueue.sv`

#### 当前动机

function coverage 中 `mmio_store_excluded_from_drain_observed` 未命中，说明虽然已有 MMIO smoke，但还没有把“MMIO 不进入最终 golden drain compare”这件事用真实 DUT 场景闭环证明出来。

前一轮曾确认一个环境语义缺口：如果先产生 MMIO outer write，再直接走 `flush_store_buffers_and_wait()` 的最终 drain compare，scoreboard 会把该 outer write 一并拿去和 non-MMIO goldenmem 比较。这个缺口现已修复：

- `finalize_and_check_drain()` 现在会基于已观测的 MMIO store 窗口，排除对应 outer drain 字节，不再把它们纳入 non-MMIO golden compare
- `test_api_MemBlock_mmio_then_cacheable_store_flush_excludes_mmio_from_final_compare` 已从问题固化场景升级为正常回归
- 因此当前 mixed-path 相关用例已经同时覆盖：
  - MMIO outer drain 可见
  - cacheable sbuffer drain 可见
  - mixed flush 下只对 cacheable/non-MMIO 结果执行最终 golden closure

#### 建议新增用例

1. 单条 mmio store，确认 shadow/mmio 标记存在
2. 结束时 flushSb/drain，不应把该 MMIO store 纳入最终一致性比较
3. MMIO store + cacheable store 混合，确保只比较后者

#### 预期收益

- 直接补中 function coverage 缺口；
- 避免未来 MMIO 语义 silently regress。

#### 当前状态

- 仍是 `P1` 高优先级，但下一步实现前需要先明确它是 DUT 行为缺口还是 env/scoreboard 语义缺口
- 当前 collector 口径已与文档对齐：只有 `flushSb` 存在且 MMIO touched-byte 窗口未进入最终 drain，才算命中该点。
- 现有 mixed flush regression 仍是主证明点；若后续 full regression 中该点仍未命中，应优先检查 real DUT 是否真的导出了稳定的 MMIO shadow/drain 事实，而不是先放宽 collector。

### P1-6 mmioBusy 长窗口 / commit-stuck 风格场景

#### 目标模块

- `LoadQueueUncache.sv`
- `UncacheEntry*`
- `MemBlock.sv`

#### 当前动机

当前 `gap_mmio_busy_without_commit_bool` 已被命中，说明真实 DUT 已暴露出这类窗口，但测试还没真正把这条路径设计成成体系回归。

当前进展补记：

- env 已新增 `wait_mmio_busy(expected=...)`，可以稳定把 `mmioBusy` 作为 testcase 的一等观测条件。
- 已确认上一版 directed probe 的主要失败根因不是 DUT，而是 testcase 把较大的 `req_id` 直接映射成较大的 `robIdx`，却没有把 `pendingPtr` 推到对应 ROB 位置；对 MMIO/uncache 路径，这会直接让请求永远到不了真正执行窗口。
- 在把该 testcase 改成贴近 `pendingPtr` 的低 `req_id` 之后，basic directed case 已能稳定观测到：
  - older MMIO store 拉高 `mmioBusy`
  - younger cacheable load 可先写回
  - younger compare/retire 不会在 older MMIO store 收尾前过早完成
- 因此这里后续重点不再是“先证明 mmioBusy 能不能拉高”，而是继续拉长窗口、补 flush/drain 组合、以及收紧 mixed-path 语义验证。

#### 建议新增用例

1. 让 mmioBusy 拉高多个周期
2. younger cacheable load/store 在该窗口下的表现
3. mmioBusy 与 flushSb / drain 组合

#### 预期收益

- 有助于后续 ROB model 中 `commit` bool / mmioBusy 语义增强；
- 对 uncache/MMIO 行为验证价值高。

## 6. P2：translation / permission / PTW 相关补强

### P2-1 TLB miss / refill directed

#### 目标模块

- `TLBFA*`
- `L2TLB.sv`
- `PtwCache.sv`
- `LLPTW.sv`

#### 当前动机

这些模块覆盖率明显偏低，但它们并不是当前标量 ld/st 主功能的第一优先级缺口，因此更适合作为第二阶段专题推进。

2026-05-06 已新增 `tests/test_MemBlock_mmu_tlbfa_deep.py`，包含 8 条定向 testcase 覆盖 `TLBFA_2.sv` 的主要缺口：

- `sfence.vma` 四种 rs1/rs2 组合分支（已覆盖 rs2=asid、rs2=0 selective、rs1=addr、rs1+rs2 精准冲刷）
- 64 页批量填充 + sfence refill 循环（覆盖 entry 0-47 写路径）
- 三端口同拍 TLB miss（覆盖 io_r_req_0/1/2 各自 touch_ways / hit_vec）
- store-side TLB refill（sta/std 两 requestor）
- 2MB megapage 探针（skip，待 PTW 能力开放）

剩余仍缺的方向：

- `L2TLB.sv` / `PtwCache.sv` / `LLPTW.sv` 的独立 hit/miss/flush 命中点
- `hfence.vvma` / `hfence.gvma` 对 `TLBFA*` 的 flush-directed case（需两阶段翻译环境）
- 2MB/1G 大页路径（待 PTW 能力开放）

2026-04-22 已新增一条基础 capacity/replacement directed case：

- `tests/test_MemBlock_env_mmu_smoke.py::test_api_MemBlock_env_mmu_sv39_dtlb_fill_and_replacement`
- 场景为 `prime 4 pages -> rehit 4 pages -> sweep many more pages -> reprobe old working set until one page misses again`
- 当前已能稳定证明：
  - 首访会触发 PTW refill
  - 二次访问不再重新 page walk
  - 容量溢出后旧页会重新 miss/refill

因此，这一项后续重点不再是“有没有基础 TLB miss/hit case”，而是继续向更深的 replacement policy、更多层级 hit/miss 组合和跨 root/sfence 行为推进。

#### 建议新增用例

1. deterministic TLB miss -> refill -> replay 成功
2. 不同层级 TLB hit/miss 组合
3. refill 后重复访问应命中
4. H extension 下 VS-only / two-stage miss-hit 对照组
5. `hfence.vvma` / `hfence.gvma` 对 `TLBFA*` / `LLPTW` / `PtwCache` 的 flush-directed case

#### 预期收益

- 提升 TLB/PTW 行覆盖率；
- 让 replay 覆盖更贴近真实翻译路径。
- 为 `TLBFA*`、`LLPTW.sv`、`PtwCache.sv` 在两阶段翻译下补上独立 hit/miss/flush 命中点。

### P2-2 PMP allow/deny / access fault directed

#### 目标模块

- `PMP.sv`
- `ExceptionInfoGen.sv`
- `LoadUnitS*`
- `StoreUnit.sv`

#### 当前动机

当前异常相关覆盖已不再是“完全空白”。

当前进展补记：

- 已新增 `tests/test_MemBlock_mmu_fault.py`
  - 覆盖 `load PMP deny`
  - 覆盖 `translation fault + PMP deny overlap`
  - 覆盖 `byte/half/word/doubleword` 四种 size/mask 的 load-side fault matrix
- `tests/test_MemBlock_scalar_load_pipeline_probe.py` 还额外把同一 fault matrix 叠加到 load pipeline probe 口径上，确认 fault 不会退化成 `memoryViolation`、outer request 或 dcache error
- 已新增 `tests/test_MemBlock_pmp_runtime.py`
  - 覆盖 `entry0..31` 的 runtime `allow/off/allow` load directed
  - 覆盖 `entry0..31` 的 locked-allow overwrite load/store directed
  - 覆盖跨 `pmpcfg` 字边界 entry 的 `NAPOT` / `TOR` load boundary directed
  - store-side `deny/off/TOR` 已按精确条件挂为 `xfail`，保留为 capability gap
- 因此当前缺口已经从“权限类 fault 场景明显不足”收敛成“load-side all-entry/runtime/lock/boundary 已有，store-side deny 仍缺稳定 fault 收口”

#### 建议新增用例

1. store deny 从当前 `xfail` 收口为稳定 fault
2. 同一地址空间下更长窗口的 load/store 混合 deny
3. store-side fault 的 writeback/exceptionVec/retire 一致性
4. TOR lock 影响前一 entry lower-bound 的更细粒度交叉

#### 预期收益

- 提升异常与权限路径的白盒可信度；
- 增强 `ExceptionInfoGen` 分支覆盖。

### P2-3 frontendBridge / fetch_to_mem / frontend-side PTW

#### 目标模块

- `FrontendBridge`
- `MemBlock.fetch_to_mem.itlb`
- `LLPTW.sv`
- `L2TLB.sv`

#### 当前动机

截至 2026-04-26，Python env 已补齐：

- `env.frontend_bridge`
  - 可对 `icache` / `icachectrl` / `instr_uncache` 做最小 TL A/D 往返 smoke
- `env.fetch_to_mem`
  - 可驱动 frontend `itlb.req`，并等待 MemBlock 返回 `itlb.resp`
- `tests/test_MemBlock_frontend_bridge.py`
  - 已覆盖三条 bridge 通路的最小 passthrough
- `tests/test_MemBlock_env_mmu_smoke.py::test_api_MemBlock_fetch_to_mem_itlb_ptw_smoke`
  - 已覆盖 frontend ITLB top-level request handshake，并在当前 build 下尽量观测 PTW/顶层 `itlb.resp`

但这仍只是“可通路的最小证明”，还不是 frontend memory subsystem 的完整验证。

#### 建议新增用例

1. frontend-side PTW repeated hit/miss 对照
2. frontend `itlb` 在 VS-only / two-stage 背景下的 smoke
3. `frontendBridge.icache` 与 `fetch_to_mem.itlb` 交叉的最小取指翻译专题场景
4. `instr_uncache` / `icachectrl` 更深的 backpressure 组合

#### 预期收益

- 让 `frontendBridge` 不再停留在“只有 RTL 连线、没有 Python 验证入口”的状态；
- 为 frontend-side PTW/TLB 路径提供最小真实 DUT 证明点；
- 为后续是否需要引入更完整 frontend mock/monitor 提供基线。

## 7. 建议实施顺序

建议按下面顺序推进，而不是分散地“这里补一个那里补一个”：

1. `P0-1` partial-mask store
2. `P0-2` cross-line / cross-beat store
3. `P1-4` nc store flush/drain
4. `P1-5` MMIO store exclusion
5. `P1-1` / `P1-2` replay 精细化
6. `P1-3` load-wait / waitForRobIdx
7. `P2-*` translation / permission

## 8. 短期验收目标

下一轮覆盖率补强后，建议至少达成以下可量化目标：

1. function coverage：
   - `MemBlock.ROB.CurrentModel` 从 `5/7` 提升到 `7/7`
2. 代码覆盖率：
   - `NewStoreQueue.sv` line coverage 保持在 `77%+`，且新增增量主要落在 vector/CBO/known-bug gating path，而不是只重复基础 partial-store
   - `SbufferData.sv` line coverage 保持在 `80%+`，并新增至少一类更深的 merge/backpressure 组合命中
   - `LoadQueueUncache.sv` / `Uncache.sv` 在多 outstanding、mmioBusy、PBMT-NC 深状态上取得实质增量，而不是只重复基础 smoke
   - 至少有 1 条当前已知未闭环路径从 `xfail`/capability probe 推进到可硬断言回归，优先候选为：
     - cross-page scalar store-misalign
     - PBMT-NC store/load
     - vector store drain
     - `cbo.zero` 多 entry drain / 更深 CBO 语义
3. 回归结构：
   - 新增一批 real DUT directed case，而不是只扩 random
   - 2026-04-30 已补入 `test_MemBlock_vlq_coverage.py`（3 条 VLQ 定向用例），完整覆盖率收益待下一轮 full regression 量化

## 9. 备注

本清单是基于当前这轮报告形成的“覆盖率驱动待办”，不替代更大范围的验证规划文档，如：

- `src/test/python/MemBlock/docs/rob_model.md`
- `src/test/python/MemBlock/docs/vp_pipeline_plan.md`

它的定位更接近“下一批最值得写的 case 列表”，便于直接排期与落地。
