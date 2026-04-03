# Berti 预取实现说明

## 概览

Berti 是当前香山 L1D 侧的数据预取器之一，默认已在预取器列表中启用，并且 `modeStrideBerti` 的默认模式已调整为“仅开 Berti”。实现入口位于 `BertiPrefetcher`，核心链路为：

`Load/Refill Train -> NewTrainFilter -> Gem5LikeUnifiedTable -> DeltaPrefetchBuffer -> TLB/PMP -> L1/L2/L3`

相关代码：

- `src/main/scala/xiangshan/mem/prefetch/Berti.scala`
- `src/main/scala/xiangshan/mem/prefetch/PrefetcherWrapper.scala`
- `src/main/scala/xiangshan/cache/dcache/mainpipe/MissQueue.scala`

## 入口与使能

默认参数在 `XSCoreParameters.prefetcher` 中包含 `BertiParams()`。包装层通过 `PrefetcherWrapper` 实例化 Berti，并用以下条件共同控制使能：

- 全局 `l1D_pf_enable`
- CSR `berti_enable`
- `pf_enableBerti`
- `modeStrideBerti` 允许 Berti 工作

其中 `modeStrideBerti` 当前默认值为 `strideOffBertiOn`，即默认仅启用 Berti。

## 数据流

```text
s3_load train / refill_train
        |
        v
  NewTrainFilter
  - 以 cache line 去重
  - 按 ROB 顺序重排
        |
        +----------------------+
        |                      |
        v                      v
 Gem5LikeUnifiedTable     refill_train
 - access: demand miss    - demand refill
   或 berti pf hit        - 提供 pc/vaddr/latency
 - search: 从当前 PC
   历史窗口反查 timely delta
 - train: 读取 best/active delta
        |
        v
 DeltaPrefetchBuffer
 - 合并重复预取地址
 - TLB 翻译、PMP 过滤
 - 发往 L1/L2/L3
```

## 训练时序

### 1. TrainFilter

`NewTrainFilter` 从 load/store 训练口收集请求，按 `robIdx` 排序，再按 cache line 地址去重后入队。当前 Berti 虽然实例化时允许 load/store 两类训练口，但在 `PrefetcherWrapper` 中 store 训练被关闭，因此实际只有 load train 生效。

### 2. Gem5LikeUnifiedTable

当前主实现已经不是早期的 `HistoryTable + DeltaTable` 两级串接，而是按 PC 把历史窗口和 delta 候选收在同一个 entry 中。它内部仍然分成三类动作：

- `access`：当训练事件是 demand miss，或 demand load 命中此前由 Berti 预取进来的 cache line 时，记录 `{pcTag, addr, timestamp}`
- `search`：当 miss refill 返回，或 demand load 命中 Berti 预取块时，从历史窗口由新到旧反查 timely delta
- `train`：读取当前 entry 的 `bestDelta` 或全部 active delta，生成 `SourcePrefetchReq`

每个 entry 维护若干候选 delta：

- `coverageCnt` 记录某个 delta 被观察到的覆盖次数
- `status` 根据阈值映射为 `NO_PREF / L1_PREF / L2_PREF / L2_PREF_REPL`
- `bestDeltaIdx` 指向当前最优 delta

状态更新规则已对齐 GEM5 版本：

- `coverageCnt >= 2` 视为 `L2_PREF`
- `coverageCnt >= 4` 视为 `L1_PREF`
- `counter >= 6` 更新状态
- `counter >= 16` 清 coverage，但保留 status

## 预取发射时序

### 1. 生成虚拟地址预取请求

UnifiedTable 根据 `triggerVA + delta` 生成 `prefetchVA`。如果 `status` 是：

- `L1_PREF`：发到 L1
- `L2_PREF` 或 `L2_PREF_REPL`：发到 L2
- 其他高层状态：发到 L3

### 2. DeltaPrefetchBuffer

`DeltaPrefetchBuffer` 对新来的 `SourcePrefetchReq` 做两件事：

- 按虚拟 cache line 合并重复项，保留更靠近核心的目标层级
- 对待发项做 TLB 翻译和 PMP/属性过滤

下列情况会丢弃该预取项：

- TLB miss
- 页故障、访存异常
- MMIO 或 uncache 区域
- PMP 拒绝
- 等待翻译期间该表项被新的同索引请求覆盖

### 3. 发往目标层级

翻译成功后按 `target` 选择输出：

- L1：输出 `L1PrefetchReq`，`pf_source = L1_HW_PREFETCH_BERTI`
- L2：输出 `MemReqSource.Prefetch2L2Berti`
- L3：输出 `MemReqSource.Prefetch2L3Berti`

发送成功后，该 buffer 表项失效。

## 与 DCache 的配合

MissQueue 在 refill 完成时向 Berti 回传：

- `pc`
- `vaddr/paddr`
- `metaSource`
- `refillLatency`

其中 `metaSource` 用来区分这次 refill 是 demand 还是某种 L1 预取来源，`refillLatency` 则用于 HistoryTable 的时间关联学习。

## 当前实现注意点

### 1. 默认模式已偏向 Berti

当前仓库默认不是“Stride + Berti 并行”，而是“只开 Berti”。分析性能时要先确认 `modeStrideBerti` 与 CSR 是否被改写。

### 2. FIFO HistoryTable 的匹配语义较特殊

FIFO 模式下，HistoryTable 在 `access` 阶段按 `baseVAddr` 去重，但在 `search` 阶段按 `pcTag` 检索。这会让“不同 PC 访问同一 line”的场景存在学习偏差，需要后续验证其是否符合设计预期。

### 3. 目前缺少专门测试

仓库当前没有独立命名为 Berti 的测试用例。若后续修改训练条件、delta 阈值、或 TLB/PMP 丢弃逻辑，建议补单元测试或定向 microbench。

## 与 GEM5_Berti 的实现差异

对照 `/nfs/home/renboyang/workspace/GEM5_Berti` 中的 `src/mem/cache/prefetch/berti.hh/.cc`，两者名字相同，但实现并不等价，至少有以下几处实质差异。

### 1. 数据结构组织不同

GEM5 版本把“历史队列”和“delta 表”绑定在同一个 PC entry 上：

- 每个 `HistoryTableEntry` 同时包含 `history` 和 `deltas`
- 结构上更接近“按 PC 建一张带历史窗口的 delta 学习表”

当前分支里的 XiangShan 已经改成 `Gem5LikeUnifiedTable`：

- 历史窗口和 delta 候选也收进同一个 PC entry
- 但外侧仍然保留 `PrefetchBuffer`、TLB/PMP、发射仲裁这些硬件流水边界

这意味着两边的“学习表”形态已经接近，但 XiangShan 仍然保留更强的硬件化约束。

### 2. 训练来源与学习时机不同

GEM5 版本的训练入口在 `calculatePrefetch()` 和 `notifyFill()`：

- demand miss / prefetch first hit 时调用 `calculatePrefetch`
- refill 完成时在 `notifyFill` 中搜索 timely delta

XiangShan 版本则显式区分三类事件：

- `demandMiss`
- `demandPfHit`
- `demandRefill`

并通过 `TrainFilter` 与 `refillTrain` 两条硬件链路分别送入 Berti。它更偏向真实硬件流水和端口约束。

### 3. delta 学习策略不同

GEM5 版本一次搜索可以找到多个 timely delta，数量由 `max_deltafound` 控制，并把这些 delta 一次性合并进当前 entry 的候选集合。

当前分支里的 XiangShan 已经把这点对齐为“单次 search 可吸收多个 delta”：

- 历史窗口按新到旧扫描
- 一次最多吸收 `max_deltafound` 个 timely delta
- 统一合并进当前 entry 的 delta 候选集合

因此，这一条已经不再是当前代码和 GEM5 的主要差异。

### 4. 发射策略不同

GEM5 版本支持两种模式：

- `aggressive_pf = true` 时，把所有非 `NO_PREF` 的 delta 都拿来发预取
- 否则只发 `bestDelta`

XiangShan 版本当前只围绕 `bestDeltaIdx` 做单次预取决策，不支持一次发多个 delta。

### 5. 目标层级判定不同

GEM5 版本的 delta 状态只有三档：

- `L1_PREF`
- `L2_PREF`
- `NO_PREF`

XiangShan 版本多了一档 `L2_PREF_REPL`，并且支持直接把请求送到 L3。因此 XiangShan 在“同一 Berti 输出接多层 cache”这件事上更明确。

### 6. 地址语义与默认参数不同

GEM5 配置默认 `use_byte_addr = True`。

当前分支已把 `BertiParams.use_byte_addr` 的默认值改成 `true`，与 GEM5 默认保持一致。若后续切回 line 语义，需要同步重新评估 delta 阈值和训练覆盖率。

### 7. 替换与去重策略不同

GEM5 HistoryTable 是 set-associative + LRU，并带 `hysteresis` 机制；历史列表是每个 PC entry 内的 FIFO。

XiangShan 默认 `HistoryTable` 使用 FIFO 策略，且 `TrainFilter` 先对 cache line 做全局去重，`DeltaPrefetchBuffer` 再对待发预取做一次合并。也就是说，XiangShan 在“进入学习前”和“发射前”都做了更强的硬件去重。

### 小结

如果只看名字，两边都叫 Berti；但如果看真正算法形态：

- GEM5_Berti 更接近“研究/仿真版 Berti”，强调按 PC 维护历史窗口、一次发现多个 timely delta、支持 aggressive 发射
- XiangShan 更接近“硬件实现版 Berti”，强调流水拆分、端口约束、TLB/PMP 检查、跨 L1/L2/L3 的实际请求发射

因此，你的判断是对的：两者不是同一实现的小改版，而是同一思路下的两种不同落地。

## Gem5 近似化改造状态

如果当前分支的目标是“在 XiangShan 中尽量逼近 GEM5_Berti 的算法”，那么目前已经对齐的核心点包括：

- `pcHash` 改成与 GEM5 一致的 `pc >> 1`
- HistoryTable 改成“每个 PC entry 挂一个地址历史窗口”，并保留 `hysteresis`
- 训练时对重复地址做抑制，只在“命中旧 PC entry 且地址新鲜”时允许触发预取
- timely delta 搜索改成“从新到旧扫描历史”，并允许一次找到多个 delta
- delta 状态阈值改成与 GEM5 一致的 `>=2 -> L2`、`>=4 -> L1`
- 计数窗口改成“`counter >= 6` 更新状态，`counter >= 16` 清 coverage 但保留状态”
- 增加了“最近预取地址过滤”，避免反复发同一条预取

仍保留的主要差异包括：

- XiangShan 仍然保留 `UnifiedTable + PrefetchBuffer` 的硬件流水边界，不是 GEM5 那种纯软件容器式调用
- 目前只实现了 GEM5 默认使用的“best delta 单发射”模式，没有打开 `aggressive_pf`
- `trigger_pht` 与 `evictedBestDelta -> BOP` 的联动没有在 XiangShan 里复刻
- 历史表替换策略仍受现有参数控制，不是严格照搬 GEM5 的 set-associative + LRU

因此，更准确的表述应该是：当前实现正在收敛到“Gem5-like Berti 核心算法”，但还不是逐行为完全等价的复刻版。

## 调试计数器

如果下次 `*_err.txt` 里仍然看不到 Berti 发射，先看 unified table 新增的几组计数器：

- `access_*`：确认 demand miss / pf hit 是否真的进入并更新了历史窗口
- `search_hit / search_cand_found / search_cand_cnt / search_cand_overflow / search_status_update / search_best_active`：确认是否学到了 timely delta，以及是否被 13-bit delta 范围截掉
- `train_hit / train_active_delta_cnt / train_best_active / prefetch_req`：确认 train 阶段有没有读到 active delta，以及最终有没有真正生成 prefetch

## 快速定位

- Berti 参数与主逻辑：`src/main/scala/xiangshan/mem/prefetch/Berti.scala`
- Berti 接线与默认模式：`src/main/scala/xiangshan/mem/prefetch/PrefetcherWrapper.scala`
- L1 预取来源编码：`src/main/scala/xiangshan/mem/prefetch/L1PrefetchInterface.scala`
- Refill 训练回传：`src/main/scala/xiangshan/cache/dcache/mainpipe/MissQueue.scala`
