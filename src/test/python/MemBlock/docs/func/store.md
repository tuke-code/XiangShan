# Store 功能点

> 本文档使用 Input-Process-Output (IPO) 模式描述 store 相关功能点。

---

### BAS-ST-001: 基础 cacheable store 提交 + flush + drain

本功能点验证 cacheable store 从发射到最终写回 memory 的完整生命周期，是 store 验证的起点。DUT 中 store 指令由 STA（store address）和 STD（store data）两拍分别发射到 store pipeline：STD 将数据写入 SQ data RAM，STA 将地址写入 SQ addr RAM。store 在 commit 后进入 sbuffer（store buffer），等待 flushSb 触发后将数据通过 outer TL-C 协议写回到 cache/memory 层次。白盒观测的关键信号包括 StoreMonitor 记录的 SQ enqueue、addr、data、mask 和 commit 事件，以及 sbuffer drain 时的写请求握手。验证的最终依据是 flush drain 后 sbuffer 日志与 golden memory 的比对。本功能点与 BAS-ST-002（MMIO store）的区别在于 cacheable store 需要经过 sbuffer 延迟写回，而 MMIO store 直接走 uncache 路径。

| 项目 | 描述 |
|------|------|
| 输入 | 通过 backend.send() 发送 StoreTxn (cacheable 地址)，STA/STD 分两拍 issue 到 store pipeline。 |
| 处理 | STD 写 SQ data，STA 写 SQ addr；store commit 后进入 sbuffer；flushSb 触发 sbuffer drain -> outer TL-C 写回。 |
| 输出 | StoreMonitor 观测 SQ enqueue/addr/data/mask/commit 事件；drain 后 MemoryModel 比对 sbuffer drain 日志与 golden memory。 |
| 验证 | 观测 writeback / drain / event 等，与参考模型比对 |

---

### BAS-ST-002: MMIO store smoke

本功能点验证 MMIO（memory-mapped IO）空间 store 的直写路径正确性。当 store 地址落在 MMIO 空间时（由 PMA 属性或 PBMT=IO 决定），DUT 的 store pipeline 不经过 sbuffer，而是直接在 commit 后将数据通过 TL-A PutFullData 协议发送到 outer 总线。MMIO store 没有延迟写回语义，因此 sbuffer drain 日志中不应包含该 store 条目。白盒观测信号包括 outer uncache write 事件和 TL-A 通道上的请求握手。在 MemoryModel 中进行 final compare 时，MMIO 字节区域需要被显式排除以避免误报。本功能点的验证边界包括 MMIO 空间边界的精确判定——地址落在 cacheable 和 MMIO 的边界附近时不应发生路径误判，以及 MMIO 地址段的完整覆盖。

| 项目 | 描述 |
|------|------|
| 输入 | StoreTxn 地址落在 MMIO 空间 (PBMT=IO 或 PMA=MMIO)。 |
| 处理 | STD+STA issue 后，store 直接走 outer/uncache TL-A PutFullData 路径，不进入 sbuffer。 |
| 输出 | 观测 outer uncache write 事件；sbuffer drain 日志中无此 store；MemoryModel 从 final compare 中排除 MMIO 字节。 |
| 验证 | 观测 writeback / drain / event 等，与参考模型比对 |

---

### BAS-ST-003: CBO (cbo.zero / clean / flush / inval)

本功能点验证 cache-block operation 指令在 DUT store pipeline 中的处理路径。cbo.zero 是零清除指令，其行为是：DCache 执行 AcquireBlock 获取 cacheline，将数据全零填充后发送 CBOAck，生成 wline（write-line）记录到 sbuffer，最终 flush 时将全零 cacheline 写回，从而完成清零语义。cbo.clean、cbo.flush 和 cbo.inval 属于非零操作：它们产生 CBOAck 但不产生 sbuffer drain，因此 memory 内容不发生变化——clean 只保证 cacheline 已写回，flush 保证写回并失效，inval 只做失效。白盒观测信号包括 CBOAck 的握手、sbuffer 中 wline drain 事件（仅 cbo.zero）、以及 flush 完成后的 memory 内容验证。本功能点与 BAS-ST-001 的本质区别在于 store 是写入指定数据，而 CBO 是操控整个 cacheline 的生命周期状态。

| 项目 | 描述 |
|------|------|
| 输入 | StoreTxn 携带 cbo 操作码 (cbo_zero/cbo_clean/cbo_flush/cbo_inval)，地址为 cacheline 对齐。 |
| 处理 | cbo.zero: DCacheAcquireBlock -> 数据全零 -> CBOAck -> wline drain 到 sbuffer -> flush 清零 cacheline。cbo.clean/flush/inval: 非零 CBO，产生 CBOAck 但不产生 sbuffer drain。 |
| 输出 | cbo.zero: sbuffer wline drain 事件，flush 后 cacheline 全零。cbo.clean/flush/inval: CBOAck 观测，无 sbuffer drain，memory 不变。 |
| 验证 | 观测 writeback / drain / event 等，与参考模型比对 |

---

### BAS-ST-004: SBufferData entry/quarter/byte 定向

本功能点验证 sbuffer 中数据条目到物理写端口的精确映射关系，是确保 sbuffer 数据布局正确性的基础设施测试。sbuffer 包含 16 个 entry（由 addr[6:4] 索引），每个 entry 包含 4 个 quarter（由 addr[3:2] 索引），每个 quarter 包含 8 个 byte。通过 prewarm（预热）控制 cacheline 到 wvec（write vector）的映射关系后，可以精确预测每个 store 在 sbuffer 中的 entry、quarter 和 byte 位置。白盒观测信号是两个写端口 writeReq_0/writeReq_1 上发出的 (entry, offset, byte) 三元组是否与硬件映射预期匹配。验证的最终结论是 flush drain 后 sbuffer 中的数据与 golden memory 完全一致。本功能点与 CMB-ST-004（partial mask）的关联在于：掩码信息决定了 byte lane 的使能，而本功能点则关注 entry/quarter 级别的地址映射精度。

| 项目 | 描述 |
|------|------|
| 输入 | 精确构造 store 使其映射到 sbuffer 特定 entry (0-15)、特定 quarter (0-3)、特定 byte lane。 |
| 处理 | 通过 prewarm 控制 cacheline->wvec 映射关系；store 的 addr[6:4] 决定 entry，addr[3:2] 决定 quarter，mask 决定 byte。 |
| 输出 | 观测 writeReq_0/writeReq_1 的 (entry, offset, byte) 三元组匹配预期；flush 后数据与 golden memory 一致。 |
| 验证 | 观测 writeback / drain / event 等，与参考模型比对 |

---

### CMB-ST-001: 随机 store (cacheable + MMIO 混合)

本功能点通过随机混合 cacheable 和 MMIO store 来验证 store pipeline 在多路径下的并发正确性。DUT 中 cacheable store 先入 SQ 再 commit 到 sbuffer，而 MMIO store 不经过 sbuffer 直接走 outer 路径，两种路径在时序和资源占用上相互独立却又共享 store pipeline 前端的发射与提交逻辑。验证的核心挑战在于 flush 后的统一 drain 过程中，MemoryModel 需要正确区分 MMIO outer drain 事件和 cacheable sbuffer drain 事件，并分别与 golden memory 比对。白盒观测信号为两类 drain 事件的完整记录。本功能点与单一路径测试（BAS-ST-001 和 BAS-ST-002）的组合价值在于：混合路径可能暴露两类 store 在共享 ROB 提交带宽时的仲裁问题，以及 sbuffer flush 过程中对 MMIO store 的路径隔离是否正确。

| 项目 | 描述 |
|------|------|
| 输入 | 6 条随机 store 混合 cacheable 和 MMIO 地址空间。 |
| 处理 | cacheable store 走 sbuffer 路径；MMIO store 走 outer 路径；flush 后统一 drain。 |
| 输出 | MMIO outer drain 与 cacheable sbuffer drain 均被正确记录；MemoryModel final compare 正确区分两类路径。 |
| 验证 | 观测 writeback / drain / event 等，与参考模型比对 |

---

### CMB-ST-002: Batched store commit

本功能点验证多个 store 在同一批次中 commit 时 sbuffer 的多 entry 写入能力。在 DUT 中，ROB 支持批量提交（batch commit），允许最多 16 条 store 同时 commit 并进入 sbuffer。此时 SBufferData 需要同时接收多个 entry 的写入，利用两个写端口 writeReq_0 和 writeReq_1 在多个周期内完成数据搬迁。白盒观测信号包括 writeReq 端口上覆盖的 entry 范围是否与 batch commit 的 store 范围一致。flush 后的 drain 阶段需要验证所有受影响的 cacheline 均被正确写回，且数据与 golden memory 一致。本功能点的验证边界包括 sbuffer 容量极限（16 entry 全满时的写入行为）、以及 batch 边界处（如 8 条 store 一批，剩余 8 条下一批）的 entry 分配连续性。与 BAS-ST-001 按条提交的区别在于 batch commit 考验的是 sbuffer 的批量写入吞吐而非单条写入的原子正确性。

| 项目 | 描述 |
|------|------|
| 输入 | 16 条 cacheable store 分波次 enqueue + issue，统一 batch commit。 |
| 处理 | 多个 store 同批 commit 后进入 sbuffer；SBufferData 的多 entry 同时被写入；flush 后统一 drain。 |
| 输出 | writeReq 在两个写端口上覆盖多个 entry；drain 后全部 cacheline 与 golden memory 一致。 |
| 验证 | 观测 writeback / drain / event 等，与参考模型比对 |

---

### CMB-ST-003: SBuffer forward (sbuffer 内 store->load)

本功能点验证 sbuffer 内已 commit 但尚未 flush 的 store 数据是否可以正确前递（forward）给 younger load。在 DUT 中，store 进入 sbuffer 后虽已脱离 SQ 但尚未持久化到全局可见点。当 younger load 访问同一地址时，DCache 可能 miss，此时 load 会查询 sbuffer 的 forward 逻辑——sbuffer 比较地址并返回 committed store 的 vtag、data 和 mask，forward 逻辑将合并后的数据直接返回给 load，避免 load 从 outer memory 获取过时的数据。白盒观测的关键信号是 sbuffer forward hit 事件以及 forward 返回的 mask/data 是否与 committed store 完全一致。本功能点与 CMB-ORD-001（ordering 中的 store->load same-addr）的关联是：前者验证 sbuffer forward 的硬件数据通路正确性，后者验证 store 间覆盖后再 load 的语义一致性，二者共同覆盖 store->load 前递的完整验证空间。

| 项目 | 描述 |
|------|------|
| 输入 | 先 commit 若干条 store 到 sbuffer (未 flush)，再发 younger load 访问相同地址。 |
| 处理 | younger load 在 DCache miss 后查 sbuffer forward，命中 committed store 的 vtag/data/mask；forward 返回合并后的数据；load 直接写回，不走 outer。 |
| 输出 | 观测 sbuffer forward hit 事件；forward 的 mask/data 与 older store 一致；load writeback 数据正确。 |
| 验证 | 观测 writeback / drain / event 等，与参考模型比对 |

---

### CMB-ST-004: partial-mask store 矩阵 (B/H/W/D)

本功能点验证 store 在非全宽掩码（partial mask）场景下的字节级合并正确性。RISC-V 架构允许 store 只更新目标地址的部分字节，由 mask 字段指示哪些字节有效——例如 sb（store byte）的 mask=0x01，sh（store half）的 mask=0x03，sw（store word）的 mask=0x0F，sd（store doubleword）的 mask=0xFF。在 DUT 中，partial store 在 SQ 中以 mask 记录有效字节范围；commit 后 sbuffer 执行 byte-level merge，将新数据与 sbuffer 中原有 cacheline 数据按字节进行合并。白盒观测信号为 StoreMonitor 记录的非全宽 mask 值以及 flush drain 后对应字节与 golden memory 的一致性。验证的关键边界是未被 mask 覆盖的字节值在 flush 后应保持不变，不能被错误地清零或篡改。本功能点与 BAS-ST-004（sbuffer 定向）的组合可验证：在任意 entry/quarter/byte 位置上的 partial mask 是否正确生效。

| 项目 | 描述 |
|------|------|
| 输入 | StoreTxn 携带非全宽 mask：0x01(SB)、0x03(SH)、0x0F(SW) 等，addr 含不同 offset。 |
| 处理 | partial store 在 SQ 中以 mask 记录有效字节；commit 后 sbuffer 执行 byte-level merge；younger load 或 flush 后观察到 merge 后的数据。 |
| 输出 | StoreMonitor 观测到非全宽 mask；flush drain 后对应字节与 golden 一致，未覆盖字节不受影响。 |
| 验证 | 观测 writeback / drain / event 等，与参考模型比对 |

---

### CMB-ST-005: cross-16B / cross-beat store

本功能点验证 store 地址范围跨越 16B 边界时的拆分逻辑正确性。DUT 中 StoreMisalignBuffer 负责将 misaligned store 拆分为两个对齐的子请求——每个子请求分别对应一个 16B 块。例如 sd（8 字节）从 addr+0xD 开始，跨越 16B 边界，拆分为第一个 16B 块内的 3 字节和第二个 16B 块内的 5 字节。每个子请求独立入 SQ、独立 commit，flush drain 时覆盖两个 cacheline。白盒观测信号包括拆分后的 mask 分布（如 SD 拆为 3B+5B），以及当 younger load 访问跨 16B 区域时，两个半窗口是否均返回正确的 merge 数据。验证的关键边界是 16B 边界处——即 addr[3:0]=0xF 时 store 只有 1 字节落在第一块；以及 addr[3:0]=0x0 时 store 完全对齐则不应拆分。本功能点与 CMB-ST-007（cross-page）的区别在于 16B 拆分是 store pipeline 内的硬件机制，而 4KB 跨页拆分涉及页表翻译的连续性。

| 项目 | 描述 |
|------|------|
| 输入 | StoreTxn 的 addr+size 跨越 16B 边界 (如 sd at addr+0xD，产生 3B+5B 拆分)。 |
| 处理 | StoreMisalignBuffer 将 misaligned store 拆分为两个对齐子请求；两个子请求各自入 SQ、各自 commit；flush drain 覆盖两个 cacheline。 |
| 输出 | 观测到 split mask (如 SD->3B+5B)；overlap load 在两半窗口均返回正确数据；flush drain 覆盖两个 cacheline。 |
| 验证 | 观测 writeback / drain / event 等，与参考模型比对 |

---

### CMB-ST-006: SQ backpressure + delayed drain

本功能点验证 store queue 在空间压力下的 backpressure 机制以及分批 drain 的正确性。DUT 中 SQ 的容量有限，当 store 不断入队而 flush 尚未触发时，SQ 可能被填满，产生 backpressure 阻止新的 store issue。本功能点使用两波 store 构造该场景：第一波 store 先 commit 并触发 flushSb，此时第一波数据从 SQ 移入 sbuffer 并开始 drain；第二波 store 仍停留在 SQ 中等待 commit。第一波 drain 完成后释放 SQ entry，第二波 store 随后 commit 并执行第二次 flushSb。白盒观测的关键信号是 StoreMonitor 中的 sqCommitPtr 和 sqDeqPtr 的变化轨迹，它们共同反映了 SQ 的入队/出队水位。验证的最终结论是两次 drain 的 golden memory 比对均正确，且两次 flush 之间的 SQ 状态转换无数据丢失。本功能点与 BAS-ST-001 的区别在于引入了 SQ 资源的竞争，验证的是 SQ 在压力下的正确性而非单条 store 的基础功能。

| 项目 | 描述 |
|------|------|
| 输入 | 两波 store 入队：第一波 commit + flush；第二波仍留在 SQ 中未 commit；随后第二波 commit + flush。 |
| 处理 | 第一波 store drain 时第二波仍占据 SQ entry；第二波 commit 后 SQ entry 释放；delayed drain 正确覆盖两波数据。 |
| 输出 | StoreMonitor 观测 sqCommitPtr/sqDeqPtr 变化；两次 drain 的 golden memory 比对均正确。 |
| 验证 | 观测 writeback / drain / event 等，与参考模型比对 |

---

### CMB-ST-007: cross-page scalar store-misalign

本功能点验证 store 地址范围跨越 4KB 页边界时的 DUT 行为。当 store 的起始地址和结束地址落在不同物理页时（例如 sd 从 0x1FFD 开始，拆分为 3 字节在页面 N，5 字节在页面 N+1），StoreMisalignBuffer 需要将 store 拆分为跨页的两个子请求。目前该场景是一个已知的 DUT bug（以 xfail 标记）：flushSb 无法将 sbuffer drain 到空，表明跨页 store 在 sbuffer 处理中存在缺陷。白盒观测信号包括拆分后的跨页 mask 分布、两页内 younger overlap load 是否返回正确数据、以及 flush drain 是否能够完成。验证的预期行为是 flush drain 最终能正确写回两页数据，使 memory 状态与 golden 一致。本功能点与 CMB-ST-005（cross-16B）的关键区别：16B 拆分不涉及页表翻译，是 store pipeline 内部的纯数据拆分；而 4KB 跨页拆分涉及两页的独立权限和翻译属性，问题复杂度显著更高。

| 项目 | 描述 |
|------|------|
| 输入 | StoreTxn 的 addr+size 跨越 4KB 页边界 (如 sd at 0x1FFD，split 为 3B+5B 跨页)。 |
| 处理 | (已知 DUT bug) StoreMisalignBuffer 拆分跨页 store；flushSb 无法将 sbuffer drain 到空。 |
| 输出 | 当前以 xfail 记录；期望观测 split mask、两页 overlap load 正确、flush drain 完成。 |
| 验证 | 观测 writeback / drain / event 等，与参考模型比对 |
