# MemBlock 覆盖率补强待办清单

## 1. 文档目的

本文档根据 `coverage_summary.md` 中记录的当前 toffee 覆盖率结果，为 `src/test/python/MemBlock/` 规划下一批最值得补的真实 DUT 用例。原则不是“哪里低就盲补哪里”，而是优先补那些：

1. 与标量 ld/st pipeline 验证目标直接相关；
2. 当前覆盖率明显偏低；
3. 可能真正提升白盒验证价值，而不只是堆代码行数；
4. 能在现有真实 DUT 环境中比较自然落地。

## 2. 优先级说明

- `P0`：当前最该补，直接影响标量 ld/st 验证深度
- `P1`：紧接着应补，能显著完善 replay / uncache / ordering 语义
- `P2`：中期补强，更偏系统级外围与复杂翻译

## 3. P0：store 深状态与 sbuffer/drain 补强

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

虽然已经有普通 8B store、overwrite、flushSb + drain，但目前对不同 byte-mask 组合覆盖明显不足。`SbufferData.sv` 与 `NewStoreQueue.sv` 覆盖率偏低，很可能意味着：

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
  - `NewStoreQueue` / `SbufferData` / `StoreMisalignBuffer` 仍基本横盘
- 因此这一组下一步的重点，不再是继续堆“窗口内 partial overwrite smoke”，而是要主动构造：
  - cross-16B / cross-beat partial-store
  - 更深的多 store 并存与 merge 顺序
  - 与 backpressure / delayed drain 叠加的 partial-store

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

RAW 已有 smoke，但更像“证明机制存在”，还不是“把 cause/窗口打透”。

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

- env 已新增 `enable_svpbmt()` 与 `install_sv39_gigapage_mapping(..., pbmt=...)`，测试现在可以显式请求 `cacheable / ncio / mmio` 三类 translated 地址属性。
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
- 已新增 older MMIO store + younger cacheable load 的 directed capability probe，但当前 DUT 仍未稳定给出“older MMIO store 拉高 `mmioBusy` 并拖住 younger retire”的可重复窗口，因此该 probe 目前以精确 `xfail` 保留。

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

#### 建议新增用例

1. deterministic TLB miss -> refill -> replay 成功
2. 不同层级 TLB hit/miss 组合
3. refill 后重复访问应命中

#### 预期收益

- 提升 TLB/PTW 行覆盖率；
- 让 replay 覆盖更贴近真实翻译路径。

### P2-2 PMP allow/deny / access fault directed

#### 目标模块

- `PMP.sv`
- `ExceptionInfoGen.sv`
- `LoadUnitS*`
- `StoreUnit.sv`

#### 当前动机

当前异常相关覆盖有基础，但权限类 fault 场景明显不足。

#### 建议新增用例

1. load deny
2. store deny
3. allow/deny 切换
4. fault 后 replay/writeback/exceptionVec 一致性

#### 预期收益

- 提升异常与权限路径的白盒可信度；
- 增强 `ExceptionInfoGen` 分支覆盖。

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
   - `NewStoreQueue.sv` line coverage 提升到 `65%+`
   - `SbufferData.sv` line coverage 提升到 `60%+`
   - `StoreMisalignBuffer.sv` line coverage 提升到 `70%+`
   - `LoadQueueUncache.sv` line coverage 提升到 `70%+`
3. 回归结构：
   - 新增一批 real DUT directed case，而不是只扩 random

## 9. 备注

本清单是基于当前这轮报告形成的“覆盖率驱动待办”，不替代更大范围的验证规划文档，如：

- `src/test/python/MemBlock/docs/rob_model.md`
- `src/test/python/MemBlock/docs/vp_pipeline_plan.md`

它的定位更接近“下一批最值得写的 case 列表”，便于直接排期与落地。
