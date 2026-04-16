# MemBlock Replay / 违例场景补强清单

## 1. 文档目的

本文档基于当前 `src/test/python/MemBlock/tests/test_MemBlock_replay.py`、现有 replay/violation sequence，以及已有设计文档，对 MemBlock 真实 DUT 的 replay / 违例检查能力做一次收口，回答四个问题：

1. 当前 DUT 已经有哪些稳定可回归的 replay / 违例场景。
2. 这些场景当前分别验证到了什么粒度。
3. 还有哪些 replay / 违例窗口需要补强或新增支持。
4. 下一批 replay testcase 应该如何实施，才能保持 sequence / env / testcase 三层边界清晰。

本文档是 `coverage_todo.md` 中 replay 相关条目的专题化展开，重点面向标量 ld/st 的 replay、nuke、memory violation 与恢复路径验证。

## 2. 当前已稳定支持的 replay / 违例场景

下表中的“已支持”指：当前已有真实 DUT testcase + sequence，且具备稳定白盒观测和收尾检查。

| 场景 | 目标原因 / 违例 | 当前状态 | 现有 testcase / sequence | 当前已验证的关键点 |
| --- | --- | --- | --- | --- |
| Forward Fail | `FF` | stable | `test_api_MemBlock_scalar_forward_fail_replay_smoke` / `ScalarForwardFailReplaySequence` | `SQ dataInvalid=1`、`matchInvalid=0`、`forwardInvalid=0`、目标 load 命中 `FF` replay、恢复后读到 store 数据 |
| Cache Miss Replay | `DM` | stable | `test_api_MemBlock_scalar_cache_miss_replay_smoke` / `ScalarCacheMissReplaySequence` | cold cacheable load 先 `DM` replay，再正常 writeback，transport 只走 dcache refill |
| NC Replay | `NC` / `nc_out` | stable | `test_api_MemBlock_scalar_nc_replay_smoke` / `ScalarNcReplaySequence` | NC/IO load 命中 `NC` replay 或 `nc_out`，transport 走 outer，不走 dcache refill |
| RAW Replay | `RAW` | stable | `test_api_MemBlock_scalar_raw_replay_smoke` / `ScalarRawReplaySequence` | older store 长时间未补全，观测 `rawNukeQuery` backpressure 与 `RAW` replay，恢复后 younger loads 全部完成 |
| RAR Violation | `RAR` / `memoryViolation` 相关恢复 | stable | `test_api_MemBlock_scalar_rar_violation_smoke` / `ScalarRarViolationSequence` | older load load-wait 暂停、younger load 先写回旧值、probe/release 后 older load 被 nuke 并读到新值 |
| Bank Conflict Replay | `BC` | stable | `test_api_MemBlock_scalar_bank_conflict_replay_smoke` / `ScalarBankConflictReplaySequence` | 两条 cache-hit load 同拍 issue，victim load 只命中纯 `BC`，不混入 `FF` / `NK` |
| Pipeline ST-LD Nuke | `NK` | stable | `test_api_MemBlock_scalar_pipeline_stld_nuke_smoke` / `ScalarPipelineStldNukeSequence` | older store 先有 data、younger load issue 后补 `STA`，目标 load 命中纯 `NK`，恢复后读到 store 数据 |
| Bank Conflict + SQ dataInvalid + NK 组合 | `BC + FF + NK` | stable | `test_api_MemBlock_scalar_bankconflict_sq_datainvalid_nuke_smoke` / `ScalarBankConflictSqDataInvalidNukeSequence` | victim load 同时命中 `BC` / `FF`，并能稳定观测到 transient `NK` |
| MatchInvalid Trigger + Redirect | `matchInvalid + memoryViolation(level=1)` | stable with known DUT bug | `test_api_MemBlock_scalar_sq_datainvalid_matchinvalid_nuke_smoke` / `ScalarSqDataInvalidMatchInvalidTriggerSequence` | `SQ dataInvalid=1 + matchInvalid=1`、younger load 命中 flush-level `memoryViolation`、恢复后读到 store 数据；但存在已知 dual-path replay bug |

## 3. 当前已经验证到的检查口径

现有 replay testcase 已经基本形成四类稳定检查口径：

### 3.1 触发前提检查

- older / younger 的 LQ/SQ 关系明确。
- `STA` / `STD` 的先后顺序可控。
- cache warmup、TLB 背景、probe/release、load-wait 等前提可由 sequence 稳定构造。

### 3.2 白盒事件检查

- replay path：`wait_replay_event()`、`wait_nc_replay_or_nc_out()`
- SQ forward path：`dataInvalid` / `matchInvalid` / `forwardInvalid`
- nuke path：`wait_nuke_query_backpressure()`、`wait_rar_nuke_response()`
- violation path：`memoryViolation_*`
- debug path：`sample_load_debug_state()` / `load_debug_trace`

### 3.3 恢复结果检查

- replay / violation 后最终 writeback 数据正确。
- commit-boundary compare 正常完成。
- store materialize / committed 状态正确。
- testcase 收尾后 `assert_no_outstanding()` 成立。

### 3.4 互斥关系检查

- 纯 `BC` 不应混入 `FF` / `NK`
- 纯 `NK` 不应混入 `BC` / `FF`
- `FF` 场景不应退化为 `matchInvalid` / `forwardInvalid`
- `matchInvalid + memoryViolation` 场景在正确实现下不应再向 LSQ replay path 建立第二条恢复去路

## 4. 当前需要补强的场景

这些场景不是“从零开始支持”，而是“当前已有基线 smoke，但还没有把 replay cause / 窗口 / 恢复边界打透”。

### 4.1 RAW replay 细分

目标：把 `RAW` 从“证明机制存在”补强到“把窗口与恢复语义打透”。

建议补充：

1. same-addr full-overlap RAW
2. partial-overlap RAW
3. older store `addr ready`、`data late`
4. older store `committed` 与 `not committed` 两窗口
5. replay 恢复后二次执行命中正确值

关键检查：

- `rawNukeQuery` 是否仍稳定出现
- replay cause 是否保持为 `RAW`
- younger loads 恢复后是否全部完成且不遗留 outstanding

### 4.2 RAR violation / release / nuke 细分

目标：把 `RAR` 从单个基础 smoke 扩成“release 时序 + overlap 形态 + query/response 行为”的组合矩阵。

建议补充：

1. same-line same-addr
2. same-line partial-overlap
3. release early / late 两种时序
4. `rar_nuke_response` backpressure
5. older load `loadWaitBit` 与 younger replay 组合

关键检查：

- younger load 是否先写回旧值
- older load 是否仅在 release 后被 nuke 并重写回
- `memoryViolation`、`rar_nuke_response` 与 writeback 顺序是否一致

### 4.3 FF 细分

目标：把 `FF` 从单一 full-width 同地址场景扩到 mask / lane / 恢复闭环。

建议补充：

1. full-overlap 与 partial-mask FF
2. 不同 issue lane 下的 FF
3. `dataInvalid=1` 且 `matchInvalid=0` / `forwardInvalid=0` 的稳定性回归
4. replay 恢复后首次 writeback 直接命中新 store 数据

关键检查：

- SQ forward event 是否稳定指向 older store
- replay cause 是否保持为 `FF`
- 不退化成 `matchInvalid` / `forwardInvalid`

### 4.4 BC 细分

目标：把 `BC` 从“单一 victim lane1”扩到“lane 选择 + 地址关系”。

建议补充：

1. lane0 -> lane1 与 lane0 -> lane2 的 victim 选择
2. same-bank different-addr
3. same-bank partial-overlap
4. `BC` 单因子与 `BC+FF` 组合解耦

关键检查：

- victim load 身份是否稳定
- `BC` 是否仍然可被构造成纯单因子基线
- 组合场景失败时能否快速判断是 `BC` 基线失效，还是叠加时序失效

### 4.5 NK 细分

目标：把 `NK` 从单一 “STD early / STA late” 场景扩到更清晰的边界检查。

建议补充：

1. `STA late + STD early` 纯 `NK`
2. `STA late + STD later` 与 `FF-only spin` 边界
3. `NK` 与 `BC`
4. `NK` 与 `FF`

关键检查：

- `NK` 是否能稳定单独出现
- `NK` 作为 transient cause 时，后续恢复路径是否可预期

### 4.6 MatchInvalid / memoryViolation 边界

目标：把 “flush-level redirect 是否独占恢复路径” 检查做扎实。

建议补充：

1. `matchInvalid=1` 后 replay path 必须清空
2. redirect 后只允许目标 load 走正确恢复路径
3. 整个恢复窗口不应额外触发 dcache refill
4. 整个恢复窗口不应误走 outer/uncache 路径

关键检查：

- `memoryViolation_bits_level`
- `memoryViolation_bits_target`
- replay path 残留事件
- transport 统计量是否保持干净

## 5. 当前还需要新增支持的场景

这些场景要么当前 environment 已有部分接口但尚未形成稳定 directed case，要么现有 testcase 还没有真正覆盖。

### 5.1 load-wait / waitForRobIdx 定向场景

建议新增：

1. older load precise load-wait 暂停，younger load 不应越过
2. `waitForRobIdx` 命中
3. `waitForRobIdx` 不命中
4. `waitForRobIdx + replay/release`
5. 多条 load 串联等待

目标收益：

- 打通更接近真实后端调度约束的 replay 场景
- 为后续 ROB 模型增强提供更稳定的事实基础

### 5.2 `forwardInvalid` 专项

当前已有关联接口与检查口径，但现有 replay testcase 基本都在证明：

- `forwardInvalid = 0`

因此需要明确两件事：

1. DUT 当前是否支持稳定构造 `forwardInvalid=1`
2. 当前环境是否有足够观测和时序控制去稳定构造该场景

如果能稳定构造，应新增 dedicated testcase；如果不能稳定构造，应在本文件中显式标注为环境缺口或 DUT 缺口，而不是继续隐含跳过。

### 5.3 `memoryViolation` level / target 深化

当前稳定命中的主要是：

- flush-level `memoryViolation(level=1)`

后续需要确认：

1. 是否存在其他 `level`
2. 不同 `target` 编码是否可稳定构造
3. 各级 violation 与 replay / redirect 的边界是否一致

### 5.4 TLB / translation replay

这部分建议作为 replay 第二阶段实施，不与当前标量 replay 清单硬耦合。

建议新增：

1. deterministic TLB miss -> refill -> replay success
2. satp 切换后 replay 恢复仍命中正确 PA
3. translation replay 与普通 `DM` / `NC` replay 的区分

## 6. 推荐 replay 用例清单

推荐把后续 testcase 分成三层，避免 `test_MemBlock_replay.py` 继续增长成一个不可维护的大杂烩文件。

### 6.1 第一层：单因子基线

- `FF`：full-overlap / partial-mask / lane variant
- `DM`：cache miss replay base
- `NC`：nc replay base
- `RAW`：full-overlap / partial-overlap / committed-window
- `RAR`：same-addr / partial-overlap / release early/late
- `BC`：lane variant / pure BC
- `NK`：pure NK / FF boundary

### 6.2 第二层：组合因子

- `BC + FF`
- `BC + NK`
- `FF + NK`
- `BC + FF + NK`
- `matchInvalid + memoryViolation`
- `loadWaitBit + RAR`

### 6.3 第三层：调度 / 翻译扩展

- `waitForRobIdx` hit / miss
- multi-load wait chain
- TLB miss / refill / replay
- `forwardInvalid`
- `memoryViolation` level / target 扩展

## 7. 实施方案

### 7.1 先做场景矩阵收口

先在 replay 文档中明确三类状态：

- `stable`：已有 testcase + sequence + 真实 DUT 稳定通过
- `partial`：已有 smoke，但细分窗口未打透
- `missing`：当前无稳定 directed case
- `known DUT bug`：已有 testcase，但已确认 DUT 行为与预期不符

后续新增 testcase 时，优先补 `partial`，再补 `missing`。

### 7.2 sequence 层先补模板

优先新增或扩展以下 sequence，而不是直接在 testcase 里堆 pin-level 时序：

- `ScalarRawReplayVariantsSequence`
- `ScalarRarViolationVariantsSequence`
- `ScalarForwardFailVariantsSequence`
- `ScalarLoadWaitReplaySequence`
- `ScalarTlbReplaySequence`（第二阶段）

原则：

- sequence 负责事务编排、默认等待、恢复闭环
- testcase 只保留场景意图与断言

### 7.3 env facade / monitor 层补稳定观测

补强方向：

- replay event 过滤能力：按 `cause` / `source` / `lane` / `rob` / `sq`
- `memoryViolation` 过滤能力：按 `level` / `target`
- `nuke query/response` backpressure 观测
- `load_debug_trace` 的目标 load 过滤 helper

原则：

- 能通过 env facade 或 monitor 表达的行为，不回退到 testcase 中手工扫 DUT 私有信号

### 7.4 testcase 分阶段落地

#### Phase A：补单因子细分

- `RAW`
- `RAR`
- `FF`
- `BC`
- `NK`

#### Phase B：补组合因子

- `BC + FF`
- `BC + NK`
- `FF + NK`
- `matchInvalid + memoryViolation`

#### Phase C：补调度 / 翻译扩展

- `waitForRobIdx`
- `forwardInvalid`
- `memoryViolation` level/target
- TLB replay

### 7.5 已知 DUT bug 的处理策略

当前已知问题：

- `matchinvalid-redirect-replay-dual-path`

处理原则：

- 保持最小粒度 `xfail`
- 只对已确认的触发条件打标，不把整组 replay 用例整体放成预期失败
- testcase 仍然保留完整断言，用于持续观测 DUT 行为是否修复

## 8. 验收标准

每个新增 replay testcase 至少满足以下标准：

1. 命中目标 replay / violation 事件
2. 命中目标白盒 cause，且禁因子不出现
3. 恢复后 writeback 数据正确
4. commit-boundary compare 完成
5. store/load 收尾后 `assert_no_outstanding()`
6. 若为 redirect / violation 场景，transport 统计量无额外副作用

## 9. 当前结论

当前 MemBlock 真实 DUT replay 回归已经不再停留在“只有 cache miss smoke”的阶段，而是已经稳定覆盖：

- `FF`
- `DM`
- `NC`
- `RAW`
- `RAR`
- `BC`
- `NK`
- `BC + FF + NK`
- `matchInvalid + memoryViolation`

下一阶段 replay 补强的重点，不是继续堆更多“机制存在”的 smoke，而是把：

- replay cause 细分
- nuke / violation 边界
- redirect 独占恢复路径
- replay 恢复后的最终语义

这四类检查真正做透。
