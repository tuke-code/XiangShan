# XiangShan DCache `BankedDataArray` / `SramedDataArray` 前置知识

本文整理 `src/main/scala/xiangshan/cache/dcache/data/BankedDataArray.scala` 与其调用侧的关键事实，作为后续 AI 修改 DCache data array 相关逻辑时的前置输入。

重点：

- 区分“对外暴露的 bank”和“内部物理 single-port SRAM”
- 明确 SRAM 的数量、大小、读写粒度
- 明确 `BankedDataArray` 与 `SramedDataArray` 的差异
- 明确 `bankConflict` 的判断方法，以及冲突后流水线如何处理

## 1. 先固定几个代码事实

来自 `src/main/scala/xiangshan/cache/dcache/DCacheWrapper.scala`:

- `DCacheSetDiv = 2`
- `DCacheBanks = 8`
- `DCacheSRAMRowBits = 64`
- `DCacheWays = cacheParams.nWays`，是参数化的，不应写死
- `addr_to_dcache_bank(addr)` 取 cache line 内哪个 8B 槽位
- `addr_to_dcache_div(addr)` 取 set 被切分后的低位部分
- `addr_to_dcache_div_set(addr)` 取切分后每块 SRAM 内部使用的 set index

因此：

- 对外部逻辑来说，一个 cache line 固定按 `8 x 8B` 看成 8 个 bank
- 对物理存储来说，set 维度又被 `DCacheSetDiv = 2` 切成两半
- 每块物理 SRAM 的深度是 `DCacheSets / DCacheSetDiv`
- 每块物理 SRAM 的有效数据宽度是 `64b = 8B`
- 如果开了 Data ECC，真实 SRAM 宽度是 `encDataBits`，即 `8B + ECC bits`

结论：

- “bank”是对外的数据分块抽象
- “SRAM”是内部真实的 single-port 存储实例
- 这两个概念不能混用

## 2. 外部 bank 的含义

对外接口总是暴露 8 个 bank。

一个 cache line 是 64B，被分成：

- `bank0`: byte `[7:0]`
- `bank1`: byte `[15:8]`
- ...
- `bank7`: byte `[63:56]`

load 通过地址低位决定自己落在哪个 bank：

- 64-bit load: 命中 1 个 bank
- 128-bit load: 命中相邻 2 个 bank

对应代码在 `LoadPipe.scala`:

- `s0_bank_oh_64 = UIntToOH(addr_to_dcache_bank(s0_vaddr))`
- `s0_bank_oh_128 = (s0_bank_oh_64 << 1.U) | s0_bank_oh_64`

所以外部看到的 “bank conflict” 本质上是在竞争这些 8B 槽位对应的底层读取资源。

## 3. 物理 SRAM 的组织

### 3.1 `BankedDataArray`

`BankedDataArray` 表面上实例化的是：

- `data_banks = Seq.tabulate(DCacheSetDiv, DCacheBanks)(...)`

看起来像 `2 x 8 = 16` 个 bank 模块，但每个 `DataSRAMBank` 内部又包含：

- `DCacheWays` 个 `SRAMTemplate(... way = 1, singlePort = true)`

也就是说，真实物理 single-port SRAM 数量是：

- `DCacheSetDiv * DCacheBanks * DCacheWays`

每块 SRAM 的大小是：

- 深度：`DCacheSets / DCacheSetDiv`
- 宽度：`encDataBits`，raw data 部分为 64b

所以：

- 如果是你提示里的 4-way 场景：`2 * 8 * 4 = 64` 块 SRAM

注意：代码是参数化的，后续改动不要把 “64 块 SRAM” 写成永真事实。

### 3.2 `SramedDataArray`

`SramedDataArray` 直接实例化：

- `List.tabulate(DCacheSetDiv)(...)`
- 内层是 `List.tabulate(DCacheBanks)(i => List.tabulate(DCacheWays)(j => Module(new DataSRAM(i, j))))`

所以它的物理 SRAM 数量和 `BankedDataArray` 完全一样，仍然是：

- `DCacheSetDiv * DCacheBanks * DCacheWays`

差别不在“有多少块 SRAM”，而在“load 读的时候一次会启动哪些 SRAM”。

## 4. 两种实现的本质差别

### 4.1 `BankedDataArray` 的最小读单位是 bank

`BankedDataArray` 中，某个 `(div, bank)` 的读由一个 `DataSRAMBank` 统一发起。

这个 bank 一旦读：

- 同一个 set
- 同一个 bank
- 所有 `way`

对应的 `DCacheWays` 块物理 SRAM 会同时被读出。

所以对一个 load 来说：

- 64-bit load: 启动 1 个外部 bank，实际读 `DCacheWays` 块 SRAM
- 128-bit load: 启动 2 个外部 bank，实际读 `2 * DCacheWays` 块 SRAM

例如 4-way 场景下：

- 64-bit load 读 1 bank = 实际读 4 块 SRAM
- 128-bit load 读 2 bank = 实际读 8 块 SRAM

### 4.2 `SramedDataArray` 的最小读单位是单块 SRAM

`SramedDataArray` 中，load 读时除了 bank，还会看 `way_en`。

只要 `way_en` 是 one-hot，load 只会启动：

- 命中的 bank
- 命中的 div
- 命中的那个 way

因此对一个 load 来说：

- 64-bit load: 启动 1 块 SRAM
- 128-bit load: 启动 2 块 SRAM

这就是它在启用路预测后能减少读功耗、减少冲突的根本原因。

### 4.3 关键提醒

`SramedDataArray` 只是在 load 读路径上把粒度从 “bank 内所有 way” 缩到 “单个 bank + 单个 way”。

它不是把物理 SRAM 数量变少了。

## 5. 读请求分别会启动哪些 bank / SRAM

## 5.1 load 读

### `BankedDataArray`

load 读条件核心是：

- 同一个 `div`
- bank 命中 `bankMask`
- 如果是 128-bit load，还会多读 `bank + 1`

每个命中的 `(div, bank)` 会启动 1 个 `DataSRAMBank`，而这个 bank 内部会同时读出所有 `way` 的 SRAM。

因此：

- 64-bit load: `1 bank -> DCacheWays` 块 SRAM
- 128-bit load: `2 bank -> 2 * DCacheWays` 块 SRAM

### `SramedDataArray`

load 读条件核心是：

- 同一个 `div`
- bank 命中 `bankMask`
- `way_en(way_index)` 为真

因此：

- 64-bit load: `1 bank + 1 way -> 1` 块 SRAM
- 128-bit load: `2 bank + 1 way -> 2` 块 SRAM

这正是它比 `BankedDataArray` 更细粒度的地方。

## 5.2 readline 读

`readline` 是主流水线读整条 cache line 的接口。

### `BankedDataArray`

当前默认 `ReduceReadlineConflict = false`，因此只要 `div` 命中，就会对该 `div` 的所有 8 个 bank 发起读。

每个 bank 又会把所有 `way` 一起读出。

因此一次 `readline` 会启动：

- `DCacheBanks * DCacheWays`

块物理 SRAM，且都在同一个 `div` 下。

### `SramedDataArray`

虽然它的 load 读更细，但当前实现里：

- `line_way_en = Fill(DCacheWays, 1.U)`
- `ReduceReadlineConflict = false`

所以 `readline` 仍然会在选中的 `div` 上把：

- 所有 bank
- 所有 way

全部读起来。

也就是说，当前实现中 `SramedDataArray` 并没有优化 `readline` 的 SRAM 激活范围。

这个点后续改动很容易看错。

## 5.3 write 写

`io.write` 携带：

- `wmask`: 哪些 bank 要写
- `data`: 每个 bank 的 64b 数据

`io.write_dup(bank)` 为每个 bank 复制了控制信息：

- `way_en`
- `addr`

它主要是为降低扇出，不表示每个 bank 有不同地址。

### `BankedDataArray`

每个命中的 `(div, bank)` 会进入对应 `DataSRAMBank`，再由 `way_en` 选中其中 1 个 way 的 SRAM 执行写入。

### `SramedDataArray`

每个命中的 `(div, bank, way)` 会直接写到对应 `DataSRAM`。

### 写粒度总结

两种实现的物理写粒度本质上都一样：

- 每个被写 bank 最终只写 1 块 way SRAM

如果一个 store / refill 写了 `N` 个 bank，那么会写：

- `N` 块物理 SRAM

## 6. single-port SRAM 的约束

无论是 `DataSRAM` 还是 `DataSRAMBank` 内部的 per-way SRAM，底层都是：

- `singlePort = true`

所以一块物理 SRAM 在一个周期内不能同时读写。

后续所有冲突判定，本质上都是在避免多个请求同时落到同一批 single-port SRAM 上。

## 7. `bankConflict` 的判断方法

下面按代码里的 4 类冲突来记。

## 7.1 `rr_bank_conflict`: load vs load

### `BankedDataArray`

判定条件：

- 两个 load 都 valid
- `div_addrs(x) === div_addrs(y)`
- `(bankMask_x & bankMask_y) =/= 0`
- `set_addrs(x) =/= set_addrs(y)`

含义：

- 同一个 `div`
- 访问了重叠 bank
- 但读的是不同 set

则冲突。

为什么不看 `way`：

- 因为 `BankedDataArray` 的 bank 读会把该 bank 的所有 way 一起读出来
- 即便两个 load 命中不同 way，只要是同一 bank、不同 set，也还是在竞争同一组 bank 内 SRAM

### `SramedDataArray`

判定条件比上面多一项：

- `io.read(x).bits.way_en === io.read(y).bits.way_en`

完整条件：

- 两个 load 都 valid
- `div_addrs(x) === div_addrs(y)`
- `(bankMask_x & bankMask_y) =/= 0`
- `way_en_x === way_en_y`
- `set_addrs(x) =/= set_addrs(y)`

含义：

- 同一个 `div`
- 重叠 bank
- 同一个 way
- 但读的是不同 set

才冲突。

为什么多了 `same way`：

- 因为 `SramedDataArray` 的 load 只读 1 个 way 的 SRAM
- 如果两个 load 虽然同 bank，但 way 不同，那么它们访问的是不同物理 SRAM，可以并行

### “同 bank、同 set”为何不冲突

两种实现里 `rr_bank_conflict` 都要求：

- `set_addrs(x) =/= set_addrs(y)`

也就是说：

- 同 bank
- 同 div
- 同 set

默认不算冲突。

原因是这类请求可以共享同一次读出的数据：

- `BankedDataArray`: 同一次 bank 读会把该 set 的所有 way 数据都拿出来
- `SramedDataArray`: 代码只把“同 bank、同 way、不同 set”视为真实冲突；当同 bank 且同 set 时，请求可以共享同一个 set 的读结果，最终按各自 latched 的 `way_en` 选出需要的 way 数据

### 谁赢谁输

代码不会简单地把 `rr` 冲突通过 `ready` 挡住。

做法是：

- 从冲突的 load 里选最老的一个保留
- 其余更年轻的请求标记为 `rr_bank_conflict_oldest`
- 年轻请求本周期不真正发 SRAM 读
- 下一拍通过 `bank_conflict_slow` 触发 replay

所以：

- `rr` 冲突主要是“年轻 load replay”
- 不是“直接 backpressure 所有 load read handshake”

这个语义后续改动必须保住。

## 7.2 `rrl_bank_conflict`: load vs readline

### `BankedDataArray`

当前默认 `ReduceReadlineConflict = false`，判定条件退化为：

- load valid
- `div_addrs(i) === line_div_addr`
- `io.readline.valid`

也就是只要 load 和 readline 落在同一个 `div`，就判冲突。

这里默认不看：

- bank 是否重叠
- set 是否相同

这是偏保守的做法，因为当前 `readline` 默认会把该 `div` 的所有 bank 都读起来。

### `SramedDataArray`

当前默认 `ReduceReadlineConflict = false` 时，判定条件是：

- load valid
- `line_div_addr === div_addrs(i)`
- `line_set_addr =/= set_addrs(i)`
- `io.readline.valid`

比 `BankedDataArray` 多了一个：

- `set` 必须不同

这说明在 `SramedDataArray` 中：

- 同 `div`
- 同 `set`

的 load 和 readline 默认允许共存，因为它们可以共享同一个 set 的读。

### 如果以后打开 `ReduceReadlineConflict`

两种实现都会额外检查：

- `io.readline.bits.rmask & io.read(i).bits.bankMask`

也就是把冲突从“整个 div”缩小到“readline 实际要读的 bank 子集”。

但当前默认没有打开，改代码时不能先入为主地按“只看 bank overlap”理解现状。

## 7.3 `wr_bank_conflict`: load vs write

### `BankedDataArray`

判定条件：

- load valid
- `write_valid_reg`
- `div_addrs(x) === write_div_addr_dup_reg.head`
- 写 bank mask 与 load bank 重叠

不看 `way`。

原因：

- `BankedDataArray` 的 load 读一个 bank 时会读出所有 `way`
- 即使 write 只写该 bank 的某 1 个 way，也会和这个 bank 读互斥

### `SramedDataArray`

判定条件多一项：

- `way_en(x) === write_wayen_dup_reg.head`

完整理解：

- 同一 `div`
- bank 重叠
- 同一 `way`

才冲突。

如果 bank 相同但写的是另一个 way，则读写访问的是不同物理 SRAM，可以并行。

### 这类冲突如何处理

`wr_bank_conflict` 会直接影响：

- `io.read.ready := !(wr_bank_conflict(i) || rrhazard)`

也就是说 `wr` 冲突是直接 backpressure load read 入口，而不是像 `rr` 那样先 handshake 再 replay。

## 7.4 `wrl_bank_conflict`: readline vs write

两种实现当前都写成：

- `io.readline.valid && write_valid_reg && line_div_addr === write_div_addr_dup_reg.head`

也就是：

- 只要同一个 `div`

就冲突。

然后：

- `io.readline.ready := !wrl_bank_conflict`

因此 `readline` 会被直接挡住。

对当前代码来说，这是合理的，因为两种实现下 `readline` 默认都会在该 `div` 大范围读 SRAM。

## 8. 流水线可见语义

`LoadPipe` 对冲突的消费方式很重要：

- `bank_conflict_slow` 会让 load 在 s2 报 `replay`
- `disable_ld_fast_wakeup` 会提前关闭 load fast wakeup

也就是说：

- `disable_ld_fast_wakeup` 是早期、保守的快路径抑制
- `bank_conflict_slow` 是晚一拍、最终决定 replay 的慢路径结果

后续若改冲突判定或读使能，必须同时检查这两个信号的语义是否还一致。

## 9. 当前代码下最重要的认知结论

1. 外部一直是 8 个 bank，这个抽象在 `BankedDataArray` 和 `SramedDataArray` 里都没变。
2. 两种实现的物理 single-port SRAM 数量相同，都是 `DCacheSetDiv * DCacheBanks * DCacheWays`。
3. `BankedDataArray` 的 load 读粒度是 “bank 内所有 way 一起读”。
4. `SramedDataArray` 的 load 读粒度是 “单个 bank + 单个预测/命中 way”。
5. `SramedDataArray` 的优化点是减少 load 读启动的 SRAM 数量，并把部分 bank conflict 从 “同 bank” 缩小为 “同 bank 且同 way”。
6. 当前实现中，`SramedDataArray` 并没有把 `readline` 也缩到单 way；`readline` 仍然会读满一个 `div` 的所有 bank 和 way。
7. 你的“4 way -> 64 块 SRAM”理解只适用于某个具体配置；当前代码是参数化的，默认 `nWays = 8` 时应是 128 块物理 SRAM。

## 10. 后续改动时的特别注意事项

### 10.1 不要混淆 bank 数量和 SRAM 数量

错误写法：

- “DCache 内部只有 8 个 bank，所以只有 8 块 SRAM”

正确理解：

- 对外是 8 个 bank
- 对内是 `2 x 8 x DCacheWays` 块 single-port SRAM

### 10.2 不要把 `BankedDataArray` 的冲突规则直接照搬到 `SramedDataArray`

尤其是：

- `rr_bank_conflict`
- `wr_bank_conflict`

在 `SramedDataArray` 里都多了 way 维度。

### 10.3 不要误以为 `SramedDataArray` 的 `readline` 已经 way-select

当前并没有。

如果后续要让 `SramedDataArray` 真正更“sramed”，`readline` 路径也许需要单独评估。

### 10.4 注意 `addr_dup` 只在 `BankedDataArray` 的 load 读里被显式使用

`BankedDataArray` 有：

- `DuplicatedQueryBankSeq = Seq(0, 1, 2, 3)`

对这些 bank 会用 `addr_dup` 路径选 `bank_set_addr_dup`。

而 `SramedDataArray` 当前 load 读路径没有对应的 `addr_dup` 读地址选择逻辑。

后续如果把某些 `BankedDataArray` 改动同步到 `SramedDataArray`，这一点必须重新核对，不能机械移植。

## 11. 适合后续 AI 直接引用的简短总结

一句话版：

- `BankedDataArray` 和 `SramedDataArray` 对外都暴露 8 个 8B bank，内部真实物理资源都是 `DCacheSetDiv * DCacheBanks * DCacheWays` 块 single-port SRAM；前者 load 读一个 bank 时会把该 bank 的所有 way SRAM 一起读出，后者只读命中的那个 way SRAM，所以后者减少的是 load 读激活的 SRAM 数量和同 bank 不同 way 的冲突，而不是减少 SRAM 总数。

## 12. 本次提示词

```text
[BankedDataArray.scala](src/main/scala/xiangshan/cache/dcache/data/BankedDataArray.scala) 学习一下 BankedDataArray，重点学习 bank 和 sram 的组织方式和 bankConflict 的判断方法，在 BankedDataArray 中，对外部暴漏出来的是 8 个 bank（一个 cache line，按 8 byte 分为了 8 个 bank，所有 set 每个 way 的 cache cline 的相同位置的 8 byte 为一个 bank），但内部是 64 块 sram（set 维度切分为两个 bank（DCacheSetDiv 为 2），一半 set 的某一 way 按 8 byte 做切分作为一块 sram 存放，一个 way 的 cache line 可以被切分为 8 个 8 byte，set 维度切分一半，一共 4 way，因此存在 8 * 4 * 2 = 64 块 sram，每一块 sram 的大小是 128 set * 8B），sram 的特点是读写不能同时进行，一次只能读一个 set 的所有数据。外部 load 请求通过判断它自己位于 cache line 的哪个 8 byte 位置来确定自己坐在的 bank 位置，load 一次性读出对应 set 的 4 way 的所有 8B 数据，因此一次读会启动 1 个 bank，数据分布在 4 块 sram 中，load 落在不同 bank 互不冲突，同 bank 只有在同 set 的情况下不冲突。

SramedDataArray 与 BankedDataArray 类似，是启用路预测后使用的，它的 sram 数量也是一样，只是 load 读时只会读一个 set 的某一个 way，能够减少启动的 sram 数量和 bank 冲突数，类比地对 SramedDataArray 的逻辑进行学习，SramedDataArray  默认不启用，但是在后续改动中需要对应设配进行改动。

确保理解 bank 和 sram 的区别，sram 的大小，数量，bank 冲突的计算方法，读和写启动的 bank 和 sram 的情形，写一个前置知识文档到 .codex/docs 下，作为 AI 后续改进的输入，文档包含本次的提示词
```
