# DCache Bank/SRAM Reorg 实施文档

## 0. 前提

本文按你这次 feature 的目标写：

- 逻辑 bank 粒度从 `8B` 改成 `4B`
- 逻辑 bank 数从 `8` 改成 `16`
- 物理 SRAM 数从 `64` 改成 `32`
- 单块 SRAM row 从 `8B` 改成 `16B`

注意：

- 这组数字对应的是“4-way 目标”的组织方式
- 当前仓库默认 `nWays = 8`，所以实现时要么参数化，要么明确只在目标配置下启用

## 1. Feature 理解

核心目标不是简单把 `8` 改成 `16`，而是把三层概念拆开：

- `logical bank`：对外的 4B 粒度
- `physical SRAM`：内部真正的单口存储
- `8B word`：load/store 逻辑里仍然常用的外部数据单位

### 1.1 旧设计

```
cache line 64B
| bank0 8B | bank1 8B | bank2 8B | bank3 8B | bank4 8B | bank5 8B | bank6 8B | bank7 8B |

per bank:
  one DataSRAMBank
  -> 4 ways x 1 SRAM/way
  -> 4 SRAMs, each 8B

per div:
  8 banks x 4 SRAMs = 32 SRAMs

2 div:
  64 SRAMs total
```

### 1.2 新设计

```
cache line 64B
| b0 4B | b1 4B | b2 4B | b3 4B | ... | b15 4B |

per logical bank:
  1 SRAM
  -> stores all ways' 4B slices at the same offset
  -> 16B row (4 ways x 4B)

per div:
  16 banks x 1 SRAM = 16 SRAMs

2 div:
  32 SRAMs total
```

### 1.3 关键结论

- 外部 bank 变细了，冲突会更友好
- 物理 SRAM 变少了，启动功耗也更低
- `bankConflict` 的判定方法保持和旧逻辑一致，只是每个请求占用的逻辑 bank 数变多了

## 2. 前后设计对比

| 项目 | 旧设计 | 新设计 |
|---|---|---|
| 逻辑 bank 粒度 | 8B | 4B |
| 逻辑 bank 数 | 8 | 16 |
| 物理 SRAM 组织 | 1 bank -> 4 个 way SRAM | 1 bank -> 1 个 packed SRAM |
| 物理 SRAM row | 8B | 16B |
| 8B load | 1 bank | 2 banks |
| 16B load | 2 banks | 4 banks |
| 1 次 load 启动的 SRAM 数 | 4-way target 下 4 / 8 | 2 / 4 |
| bank 冲突判定 | overlap + same div + diff set | overlap + same div + diff set，只是 mask 更细、占用 bank 更多 |

## 3. 读写行为对比

### 3.1 load

旧设计：

- 8B load 读 1 个 bank
- 读出该 bank 下所有 way 的 8B 数据
- 再由 tag/way 选择最终结果

新设计：

- 8B load 读 2 个 4B bank
- 每个 bank 直接返回所有 way 的 4B lane
- 组合成 8B 结果后再做 way 选择

示意：

```
8B load
old:  bank3 -> 4 ways x 8B
new:  bank6 + bank7 -> 2 x (4 ways x 4B)
```

### 3.2 write

旧设计：

- 以 8B bank 为单位写
- 一个 bank 内由 waymask 选中 1 个 way SRAM

新设计：

- 以 4B bank 为单位写
- 写入前先读出对应 set 的旧 16B row
- 覆盖新数据后，整行写回 packed SRAM

`SRAMTemplate` 不必依赖 `useBitmask`，直接走 read-modify-write 就够了。

### 3.3 readline

建议保持外部语义尽量不变：

- 仍以 line word 为外部返回单位
- 但内部读取按 4B bank 执行

否则 `MainPipe` / `Sbuffer` / `LoadPipe` 会被一次性放大很多。

## 4. bankConflict 逻辑

旧规则：

- 同 `div`
- bank overlap
- 不同 set

新规则：

- 同 `div`
- bank overlap
- 不同 set
- 只是现在一个请求可能占用更多逻辑 bank，所以 `bankMask` 要按 4B 粒度重算

可理解成：

```text
mask(high) & mask(low) != 0
```

更完整地说，`bankConflict` 仍然是旧逻辑那种对称 overlap 判断，只是参与判断的 bank 变细了。

### 4.1 例子

```
high: need banks {0,1}      // 8B load
low : need banks {1,2}      // another 8B load

same set:
  no conflict, low can reuse bank1 data from high

high: need banks {0,1,2,3}  // 16B load
low : need banks {2,3}      // 8B load

same set:
  no conflict, low can reuse the overlapping banks from high
```

## 5. 改动范围

### 5.1 主改动

- `src/main/scala/xiangshan/cache/dcache/data/BankedDataArray.scala`

### 5.2 必须同步的上下游

- `src/main/scala/xiangshan/cache/dcache/mainpipe/MainPipe.scala`
- `src/main/scala/xiangshan/cache/dcache/loadpipe/LoadPipe.scala`
- `src/main/scala/xiangshan/mem/sbuffer/Sbuffer.scala`
- `src/main/scala/xiangshan/mem/sbuffer/FakeSbuffer.scala`
- `src/main/scala/xiangshan/cache/dcache/DCacheWrapper.scala`
- `src/main/scala/xiangshan/cache/dcache/CtrlUnit.scala`

### 5.3 后续适配

- `SramedDataArray`

## 6. 实施步骤

### Step 1: 拆分“逻辑 bank”和“外部 word”

先把当前隐含的等式拆开：

- bank 粒度 = 4B
- 外部 word 仍然以 8B 为主
- `bankMask` / `rmask` / `wmask` 改成逻辑 bank 宽度
- `read_resp` / `readline_resp` 仍保留 word 级结果

### Step 2: 重建 `BankedDataArray` 的 SRAM 组织

目标结构：

- 每个 `(div, logicalBank)` 只有 1 块 SRAM
- 这块 SRAM 的 row 存所有 way 的 4B lane

关键点：

- 读取时按逻辑 bank 读
- 写入时按 way lane 局部更新
- 响应时再把 4B lane 拼回 8B word

### Step 3: 重写读路径

load/readline 都要重新生成 bank 选择：

- 8B load -> 2 个 4B bank
- 16B load -> 4 个 4B bank

同时把 conflict 检查改成与旧逻辑一致的 mask overlap，只是 mask 宽度和占用 bank 数变了。

### Step 4: 重写写路径和 mask 传播

`MainPipe` / `Sbuffer` 侧要把 mask 从 8B bank 粒度拓宽到 4B bank 粒度：

- store / AMO / refill 的 write mask
- `banked_store_rmask`
- `banked_wmask`
- `mergePutData`

`BankedDataArray` 侧按 16B row 做 read-modify-write，不需要 `useBitmask`。

### Step 5: 再做 `SramedDataArray`

先保证 `BankedDataArray` 正确，再把同样的 4B bank 组织、mask 宽度、冲突规则迁移到 `SramedDataArray`。

## 7. 风险

### 7.1 最大风险：概念混用

当前代码把这些东西绑得太紧：

- logical bank 数
- line word 数
- SRAM row 宽度
- response vector 宽度

新设计如果不先拆抽象，很容易在 `DCacheBanks`、`DCacheSRAMRowBits`、`VLEN/DCacheSRAMRowBits` 之间打架。

### 7.2 ECC 风险

当前 `data ECC` 是按旧 `64b` 语义在走的。

新 row 变成 `16B` 后：

- 如果继续开 ECC，必须重新定义 ECC 粒度
- 否则先按 `EnableDataEcc = false` 的主路径落地更稳

### 7.3 MBIST / pseudo error 风险

这些接口都直接绑了 bank width / row width：

- `CtrlUnitSignalingBundle`
- `pseudo_error`
- `mbistSramPorts`

需要一起重算宽度。

### 7.4 时序风险

`bankConflict` 仍然是 overlap，但 `bankMask` 变宽、参与比较的位数更多，逻辑可能更重。

要警惕：

- `LoadPipe` 的组合路径
- `MainPipe` 的 write mask 生成路径

### 7.5 Store/AMO/unaligned 风险

4B bank 粒度下，store/AMO 的 mask 展开会更碎。

要特别确认：

- 8B store
- 16B store
- AMO/CAS
- misalign split

都能正确覆盖到 4B bank。

## 8. 校验和自检

### 8.1 编译级

- 跑一次全量 elaboration
- 确认 `BankedDataArray` / `SramedDataArray` 都能生成
- 确认 `wmask/rmask/bankMask` 宽度一致

### 8.2 结构级

检查是否还有残留的 8B bank 假设：

- `FillInterleaved(8, ...)`
- `UIntToOH(addr_to_dcache_bank(...))` 还在按 3-bit bank 做理解
- `Vec(DCacheBanks, ...)` 里某些地方其实是“word 数”不是“logical bank 数”

### 8.3 功能级

至少做这些定向例子：

1. 8B load 只启动 2 个 4B bank。
2. 16B load 只启动 4 个 4B bank。
3. 同 `div`、同 `set`、bankMask 不重叠时，不 replay。
4. 同 `div`、不同 `set`、bankMask 重叠时，仍然冲突。
5. write 只更新被 mask 命中的 4B lane。
6. `readline` 返回的 8B word 与旧实现对齐。

### 8.4 性能级

看这些计数器是否符合预期：

- `data_read_counter` 下降
- bank conflict 下降
- 8B load 的 SRAM 启动数显著少于旧设计

## 9. 推荐执行顺序

1. 先改 `BankedDataArray`
2. 再改 `MainPipe` / `Sbuffer` 的 mask 传播
3. 然后补 `LoadPipe` / `DCacheWrapper` / `CtrlUnit` 的宽度适配
4. 最后做 `SramedDataArray`

## 10. 本次提示词

```text
接下来需要实现一个 feature：把 sram 的组织方式和 bank 的逻辑分区进行修改，将原本的 64 块 sram 数量改为 32 块，sram 的大小由 128 * 8B 改为 128 * 16B。

以下以 BankedDataArray 为例

物理 sram 存储的数据的区别：
在原始的设计中，一块 sram 存储的是 **某一 way** cache line 按 8B 偏移切分出的 8 部分中的其中一部分 8B 数据，set 存储 128 项。当前需要改动的 feature 希望一块 sram 存储：**所有 way** cache line 按 4B 偏移切分出的 16 部分中相同偏移的 4 way * 4B = 16B 的数据。

逻辑 bank 区别：
原始设计中，逻辑 bank 数量为 8，粒度为 8B，当前需要改动的 feature 改为 16，粒度为  4B

读写启动的物理 sram 的区别：
以一个 8B load 读为例，一次 load 需要根据虚拟地址读出对应 set 所有 way 的对应位置的数据，在原始的设计中，需要在一个 bank 内启动 4 块 sram（4 way）读取数据，得到所有 way 中对应偏移位置的 8B 数据，经过后续 tag match 后选择需要的数据。当前设计下，一个 8B load读会启动两个 4B 粒度的 bank，由于一块 sram 就存储了所有 way 的对应偏移位置的数据，因此一个 8B load 读只需要启动 2 块 sram 即可拼接出所有 way 的对应偏移位置的 8B 数据，因此当前设计的优势为一次 load 启动的 sram 数量少，功耗低，且当前 bank 粒度 4B 比 8B 更细，对 bank Conflict 更友好。readline 和 write（一次 write 当前设计需要启动的 sram 更多） 可以做类比。

bank Conflict 判断的区别：
当前设计下，一个 8B load 会占据 2 个逻辑 bank，16B load 会占据 4 个逻辑 bank，不同 bank 下的请求互不影响，相同 bank 的两个请求下仅在高优先级请求读出的数据可以给低优先级请求使用的情况下才不发生 bank 冲突

改动的范围：
绝大部分改动都只在 BankedDataArray.scala 中，尽量做到对外部不可见，但是考虑到当前设计下逻辑 bank 数增大一倍，sbuffer [Sbuffer.scala](src/main/scala/xiangshan/mem/sbuffer/Sbuffer.scala) 经过 [MainPipe.scala](src/main/scala/xiangshan/cache/dcache/mainpipe/MainPipe.scala) 对 data 的写入 mask 需要对应拓宽来适配更好的性能。优先保证 BankedDataArray 的改动实施和正确性，之后对 SramedDataArray 做对应适配

理解以上需求，结合前置知识和具体的代码逻辑，形成一个实施文档，包含feature理解，前后设计详细对比（用便于理解的对比图），改动范围，步骤，风险，校验自检方法。文档包含本次提示词
```
