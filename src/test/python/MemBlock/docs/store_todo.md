# MemBlock 标量 Store 覆盖率讨论与补强 TODO

## 1. 文档目的

本文档用于收口当前围绕 MemBlock 标量 store 覆盖率的讨论结论。目标不是重复抄录 `coverage_summary.md` 与 `coverage_todo.md` 的全部内容，而是把与 store 直接相关的现状、短板、以及后续补强建议单独整理出来，便于后续围绕真实 DUT 回归按专题推进。

本文档聚焦：

1. 当前标量 store 到底已经覆盖到了哪里；
2. 哪些模块说明 store 主路径已经立住；
3. 哪些模块仍然代表 store 验证明显不够；
4. 下一步应优先往哪些 testcase 文件里补哪些场景。

## 2. 当前 store 验证现状

### 2.1 当前报告基线

- 报告来源：
  - `src/test/python/MemBlock/data/toffee_report_full/toffee_report.json`
  - `src/test/python/MemBlock/data/toffee_report_full/line_dat/code_coverage.json`
- 本轮 full regression 时间：
  - `2026-04-14 19:18:35` ~ `2026-04-14 19:40:12`
- 总体结果：
  - `129 PASSED, 8 XFAIL`

与标量 store 直接相关的 testcase 分布如下：

- `tests/test_MemBlock_scalar_store_pipeline.py`
  - `9 PASSED`
- `tests/test_MemBlock_random_store.py`
  - `6 PASSED`
- `tests/test_MemBlock_store_misalign.py`
  - `5 PASSED, 2 XFAIL`
- `tests/test_MemBlock_uncache_semantics.py`
  - `1 PASSED, 4 XFAIL`
- `tests/test_MemBlock_scalar_ordering.py`
  - `3 PASSED`

这说明：

- cacheable scalar store 主路径已经有稳定的真实 DUT regression；
- misalign 与 uncache/MMIO store 相关路径已经进入验证视野，但还没有完全闭环。

### 2.2 store 相关关键模块覆盖率

以下模块最能反映当前标量 store 覆盖状态：

- `StoreUnit.sv`
  - line `88.8%` (`710/800`)
  - branch `86.2%` (`131/152`)
- `NewStoreQueue.sv`
  - line `58.4%` (`7542/12910`)
  - branch `39.4%` (`2083/5292`)
- `Sbuffer.sv`
  - line `85.2%` (`6115/7181`)
  - branch `56.8%` (`341/600`)
- `SbufferData.sv`
  - line `56.2%` (`8578/15270`)
  - branch `54.0%` (`1143/2116`)
- `StoreMisalignBuffer.sv`
  - line `59.0%` (`326/553`)
  - branch `48.7%` (`38/78`)
- `Uncache.sv`
  - line `64.4%` (`1360/2111`)
  - branch `53.8%` (`326/606`)
- 参考外围：
  - `DCache.sv`：line `63.4%`，branch `77.4%`

### 2.3 对这些数字的解读

#### `StoreUnit.sv` 已经较稳

`StoreUnit.sv` 的 line/branch 都比较高，说明下面这些主路径已经被广泛激活：

- STA/STD issue
- store materialize
- 基础 commit
- 基础 flush/drain 收尾

也就是说，当前 store 回归已经不是“只证明 store 请求能发出去”的阶段，而是确实覆盖到了主执行路径。

#### `NewStoreQueue.sv` 是最明显的深状态短板

`NewStoreQueue.sv` 的 branch 只有 `39.4%`，这是当前标量 store 覆盖最值得优先关注的数字之一。它更像是在提示：

- 多 store 并存窗口还不够；
- readiness / token / commit 组合还不够；
- flush 前后、drain 前后、younger load 穿插等深状态时序还没打透。

#### `Sbuffer.sv` 与 `SbufferData.sv` 说明“经过了，但没打透”

- `Sbuffer.sv` line 高，说明 sbuffer 路径经常被经过；
- `SbufferData.sv` 明显偏低，说明 data merge / mask 组织 / drain 细节仍然不充分。

这通常意味着当前用例已经能让 store 走到 sbuffer，但对以下内容还不够：

- partial write merge 顺序
- 多次覆盖写后的数据拼接
- flush/drain 时 touched-byte 组织
- 与 delayed drain / backpressure 叠加的行为

#### `StoreMisalignBuffer.sv` 已进入验证，但仍未闭环

当前已经有 cross-16B scalar store misalign directed case，也已经有 cross-page case 的精确 `xfail`。这说明：

- 当前不是“完全没测到 misalign”；
- 而是“基础 split 已经能打到，但 deeper split state 与最终 drain 收口仍然不足”。

#### `Uncache.sv` 仍代表 store 边界语义没有完全收口

当前 store 相关 function coverage 仍缺两项：

- `nc_store_flush_drain_observed`
- `mmio_store_excluded_from_drain_observed`

同时 `tests/test_MemBlock_uncache_semantics.py` 里仍有 4 条 `XFAIL`。这说明：

- cacheable store 基础闭环已经立住；
- 但 non-cacheable / MMIO store 的最终 flush-drain 语义还没有完全跑通。

## 3. 当前已经覆盖到的 store 能力

从 testcase 分布、覆盖率和现有讨论可以比较有把握地说，当前已经覆盖到：

- cacheable scalar store 的基础 issue / materialize / commit
- same-addr store -> load 可见性
- overwrite 场景
- flushSb + drain 基础闭环
- 部分 partial-store directed matrix
- 一部分 cross-16B scalar store misalign
- store burst 后统一 flush 的最小闭环

当前已经落地的代表性 directed case 包括：

- misaligned store + dual overlap load
- store burst + interleaved load + final flush
- MMIO + cacheable store mixed-path smoke
- partial word store + aligned full load
- high-offset byte store + aligned full load
- same-dword multi-byte partial merge
- full store + high-offset partial overwrite
- interleaved partial stores across two addresses
- cross-16B `SD/SW/SH` scalar store misalign

## 4. 当前最明显的 store 覆盖空洞

### 4.1 partial-mask store 还没打透

虽然已有一些 partial-store 用例，但从 `NewStoreQueue.sv` 和 `SbufferData.sv` 的数字看，还明显缺：

- 更深的 partial merge 顺序
- 同一地址多次 partial 覆写
- 多地址交织 partial store
- partial-store 与 flush/drain/backpressure 的组合

### 4.2 misalign store 目前更像“可验证性刚建立”

`StoreMisalignBuffer.sv` 当前数字仍然偏低，说明 cross-16B 基础矩阵还不够。尤其是：

- cross-page normal path 目前仍有 `xfail`
- split 后的更深状态停留与 drain 收口还没打透
- exception / boundary 行为还没有成体系回归

### 4.3 flush/drain 场景偏顺畅路径

当前已有 flush/drain regression，但仍偏向：

- ready 常高
- 响应顺畅
- 最后统一 drain 成功

真正缺的是：

- delayed drain
- committed 但未及时 drain 的停留窗口
- flush 期间 younger load/store 穿插
- sbuffer 深状态与最终 closure 的组合

### 4.4 NC/MMIO store 闭环仍不够

当前最清晰的边界缺口就是：

- `nc_store_flush_drain_observed` 未命中
- `mmio_store_excluded_from_drain_observed` 未命中

这意味着当前还不能说：

- NC store 的最终 outer drain 一致性已经被真实 DUT 证明；
- MMIO store 已经稳定证明“可见但不纳入 final drain compare”。

## 5. 后续补强矩阵

### 5.1 `tests/test_MemBlock_scalar_store_pipeline.py`

优先承接 cacheable scalar store 的深状态补强。

#### 建议新增

1. 单 cache line 内连续 partial-byte store
   - 如同一 8B 字依次写 `0x01`、`0x02`、`0x04`、`0x08`
2. `partial -> partial -> full load`
3. `full store -> partial overwrite -> partial overwrite -> full load`
4. 两个地址交织 partial store，再分别验证 load 结果
5. 连续 3~4 条 committed store，延后统一 flush
6. older committed store 存在时插入 younger load，再检查最终 drain
7. flush 后延迟 drain 返回，观察 younger op 是否异常

#### 主要目标

- `NewStoreQueue.sv`
- `SbufferData.sv`
- `Sbuffer.sv`

### 5.2 `tests/test_MemBlock_store_misalign.py`

优先承接 split / misalign / cross-page 收口。

#### 建议新增

1. cross-page `SW`
2. cross-page `SB`
3. split 后 aligned low-window load
4. split 后 aligned high-window load
5. split 后 overlap load
6. normal path 与 exception path 分离回归

#### 当前策略

- 继续保留现有 cross-page 精确 `xfail`
- 优先缩小“是 DUT 缺陷还是验证观测不够”的边界

#### 主要目标

- `StoreMisalignBuffer.sv`
- `StoreUnit.sv`
- `ExceptionInfoGen.sv`

### 5.3 `tests/test_MemBlock_uncache_semantics.py`

优先承接 NC/MMIO store 边界语义。

#### 建议新增

1. 单条 `PBMT=NC` store + flushSb + outer drain
2. 两条 NC store burst 后统一 flush
3. NC store 与 cacheable load/store 混合
4. 单条 MMIO store，只证明 outer write/shadow 可见且不纳入 final compare
5. MMIO + cacheable mixed flush，只比较 cacheable/non-MMIO 结果
6. older MMIO store 拉高 `mmioBusy` 多拍，观察 younger cacheable op

#### 主要目标

- `Uncache.sv`
- store 相关 function coverage 未命中点

## 6. 推荐优先级

### P0

1. partial-mask store 深矩阵
2. `NewStoreQueue` 深状态
3. cross-page / deeper split misalign
4. flush/drain 非顺畅窗口

### P1

1. `nc store flush/drain`
2. `MMIO exclusion`
3. `mmioBusy` 长窗口

### P2

1. 更外围的 DCache store 相关复杂组合
2. store 与更复杂 replay / ordering / release 的交叉场景

## 7. 验收建议

后续每轮补强后，建议至少复核以下指标：

- `NewStoreQueue.sv` branch 高于当前 `39.4%`
- `StoreMisalignBuffer.sv` branch 高于当前 `48.7%`
- `Uncache.sv` branch 高于当前 `53.8%`
- function coverage 补中：
  - `nc_store_flush_drain_observed`
  - `mmio_store_excluded_from_drain_observed`

同时继续把状态结论同步回：

- `src/test/python/MemBlock/docs/coverage_summary.md`
- `src/test/python/MemBlock/docs/coverage_todo.md`

避免 store 专题文档与 coverage 主口径分叉。
