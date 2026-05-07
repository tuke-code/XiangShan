# Load 功能点

> 本文档使用 Input-Process-Output (IPO) 模式描述 load 相关功能点。

---

### BAS-LD-001: 基础 cacheable load (burst / 饱和)

本功能点验证 cacheable load 在 DCache 命中场景下的基础数据通路正确性，是 load 验证的起点。DUT 中 load 请求从 IntIssue 端口发射到 LoadUnit 的 load pipeline lane，经过地址生成后发送 GetBlock 请求到 DCache。DCache 命中时返回 2-beat GrantData，LoadUnit 在 s3 阶段完成多拍数据的合并与符号扩展，最终通过 mem_to_ooo.intWriteback 将结果写回 ROB。白盒观测的关键信号包括 writeback 通道上的 robIdx、pdest 和 data，以及 load 在 pipeline 各阶段的 valid/ready 握手。Burst 场景下多条 load 可同拍或连续拍发射，用于验证 pipeline 的吞吐能力与 backpressure 机制是否按预期工作。本功能点与后续 CMB-LD-002 等组合场景的区别在于不引入 bank conflict、forward 或 violation 等干扰，保证基础路径的纯度。

| 项目 | 描述 |
|------|------|
| 输入 | 通过 enqLsq 入队一条或多条 cacheable load 请求 (LoadTxn)，addr 落在 cacheable 空间 (>0x80000000)。通过 IntIssue 端口发射到 load pipeline lane。 |
| 处理 | LoadUnit 将请求发送到 DCache AcquireBlock；DCache 命中后返回 2-beat GrantData；LoadUnit s3 阶段完成数据合并与符号扩展，产生 intWriteback。 |
| 输出 | mem_to_ooo.intWriteback 输出 robIdx/pdest/data；MemoryModel 观测 writeback 后触发 commit-boundary compare。burst 场景下多条 load 同拍或连续拍 issue，验证 pipeline 吞吐与 backpressure。 |
| 验证 | 观测 writeback / drain / event 等，与参考模型比对 |

---

### BAS-LD-002: 标量 load 宽度/掩码 (lb/lh/lw/ld)

本功能点验证标量 load 在不同数据宽度下的数据裁剪与符号/零扩展逻辑的正确性，是 load 指令正确性的基础之一。LoadUnit 在 decode 阶段根据 LoadTxn 携带的 size 参数（byte/half/word/doubleword）选择对应的 fuOp 类型，并在 s3 阶段对 DCache 返回的原始数据按 width 进行掩码提取。对于有符号扩展的类型（lb/lh/lw），数据高位按符号位填充；对于无符号扩展的类型（lbu/lhu/lwu），高位补零；ld 则直接传递 64 位全宽数据。白盒观测信号为 intWriteback.data 的低位内容与高位的扩展值，以及 fpWen 是否为零。验证的关键边界包括 size 与 mask 的精确匹配、符号扩展在高位填充时的正确性、以及每种宽度在地址非对齐时的行为是否与 spec 一致。本功能点与 BAS-LD-003 fp load 的区别在于后者关心浮点 boxing 而非整数扩展语义。

| 项目 | 描述 |
|------|------|
| 输入 | LoadTxn 携带 size 参数 (byte/half/word/doubleword)，对应 mask 为单字节/双字节/四字节/八字节。 |
| 处理 | LoadUnit 按 size 产生对应 fuOp (lb/lh/lw/ld)，s3 阶段根据 size 做符号扩展 (lb/lh/lw) 或零扩展 (lbu/lhu/lwu) 或全宽 (ld)，合并到 64-bit writeback 数据。 |
| 输出 | intWriteback.data 的低位按 size/mask 裁剪，高位符号或零扩展；fpWen=0。验证 writeback 数据与 preload golden 值按 size/mask 匹配。 |
| 验证 | 观测 writeback / drain / event 等，与参考模型比对 |

---

### BAS-LD-003: 标量 fp load (fpWen boxed)

本功能点验证浮点标量 load（flh/flw/fld）在 DCache 命中场景下的数据 boxing 语义正确性。与整数 load 不同，浮点 load 在 LoadUnit s3 阶段需将 DCache 返回的原始数据按照 IEEE 754 标准的 NaN-boxing 规则进行封装：flh 将 16 位数据放入 64 位寄存器的低 16 位并将高 48 位置 1，flw 将 32 位数据放低 32 位并将高 32 位置 1，fld 则将 64 位数据直接传递。写回时 intWriteback.fpWen=1 通知 ROB 该写回面向浮点寄存器堆（FRF），intWen=0 表示不写入整数寄存器。白盒观测信号包括 fpWen 位、writeback 数据的 boxed bit-pattern、以及 frf 侧的正确接收。本功能点的验证边界包括浮点 load 与整数 load 共享同一 DCache 数据通路时的信号隔离，以及在 boxing 边界上（如 flw 后高 32 位是否为 NaN-boxing 模式）的精确检查。

| 项目 | 描述 |
|------|------|
| 输入 | LoadTxn 携带 fpWen=1，size 为 half/word/doubleword。 |
| 处理 | LoadUnit 在 s3 阶段将 DCache 返回的数据按 IEEE 754 格式 box 到对应宽度（flh->16bit boxed, flw->32bit boxed, fld->64bit），写回时 fpWen=1, intWen=0。 |
| 输出 | intWriteback.fpWen=1，data 为 boxed bit-pattern。验证 fp_wen 位与 boxed 数据正确性。 |
| 验证 | 观测 writeback / drain / event 等，与参考模型比对 |

---

### CMB-LD-001: 随机 load (IO + cacheable 混合路径)

本功能点验证 load 在随机地址空间下的多路径覆盖能力，确保 IO 空间与 cacheable 空间的 load 路径均被充分测试。DUT 中的 LoadUnit 根据地址的 PMA/PBMT 属性决定数据通路：IO 空间 load（地址<0x80000000）走 outer/uncache 路径，通过 TL-A GET 请求到 outer D 响应返回；cacheable 空间 load（地址>0x80000000）走 DCache AcquireBlock/GrantData 路径。MemoryModel 按路径分类跟踪每条 load 的期望 writeback，确保无论是 IO 还是 cacheable 路径，所有 load 的 robIdx、pdest 和 data 均能在 commit-boundary compare 中被正确校验。白盒观测的全局信号包括 writeback、drain 和各种 load event。本功能点与单一路径测试（BAS-LD-001 只覆盖 cacheable）的区别在于通过随机混合验证了 DUT 在路径选择逻辑上的无漏检，同时也能暴露 IO 路径与 cacheable 路径在 backpressure 交互时可能出现的死锁问题。

| 项目 | 描述 |
|------|------|
| 输入 | 1000 条随机地址 load，addr 随机落在 IO 空间 (<0x80000000) 或 cacheable 空间 (>0x80000000)。 |
| 处理 | IO load 走 outer/uncache 路径 (TL-A GET -> outer D 响应)；cacheable load 走 DCache A/D/E 路径。MemoryModel 按路径分类 track 期望的 writeback。 |
| 输出 | 全部 writeback 的 robIdx/pdest/data 通过 commit-boundary compare 校验；IO 和 cacheable 路径均无遗漏。 |
| 验证 | 观测 writeback / drain / event 等，与参考模型比对 |

---

### CMB-LD-002: bank conflict hit (无 forward / 无 violation)

本功能点验证 DCache 在同一拍收到来自不同 load lane 的请求、且二者访问同一 DCache bank 时，bank conflict 检测与 fast replay 机制的正确性。DUT 的 DCache 在每个 bank 入口检测到 address conflict 后，对低优先级的 load 触发 fast replay——设置 ldCancel=1 并在 replay cause 中标记 BC（bank conflict）而不携带 forward 或 memoryViolation 位。被取消的 load 不会写回，而是重新进入 load pipeline 再次尝试。高优先级 load 正常走完 s3 阶段并写回。白盒观测的关键信号是 fastReplay 与 ldCancel 的同时断言，以及 replay cause 的精确编码。验证的边界条件包括两个请求访问同一 bank 的不同 cacheline 时仍应触发 BC，而访问不同 bank 时不应产生 BC。本功能点与 CMB-LD-004 的区别在于不引入更高优先级的 replay 类型来抢占 BC fast replay，保持 BC 路径的测试纯度。

| 项目 | 描述 |
|------|------|
| 输入 | 两条 cacheable load 同拍 issue 到不同 lane，访问同一 dcache bank 的不同 cacheline。 |
| 处理 | DCache 返回 bank conflict hit；低优先级 load 触发 fast replay (ldCancel=1)，replay cause 仅为 BC。高优先级 load 正常 s3 写回；低优先级 load 重发后完成。 |
| 输出 | 观测到 fastReplay + ldCancel，replay cause 不包含 forward/memoryViolation；两条 load 最终均完成 writeback 且数据正确。 |
| 验证 | 观测 writeback / drain / event 等，与参考模型比对 |

---

### CMB-LD-003: matchInvalid proxy nuke

本功能点验证当 SQ（store queue）中存在 older store 但数据尚未就绪时（dataInvalid=1），younger load 访问同一地址时 SQ forward 的异常处理路径。在 DUT 中，store 的地址解码（STA）可能先于数据到达（STD），因此 SQ 中可能存在地址有效但数据无效的 entry。当 younger translated load 访问同一地址时，SQ 比较地址后返回 dataInvalid=1 和 matchInvalid=1，此时 load 不能直接使用 forward 数据，而需在 memoryViolation 检测逻辑中以 violation level=1 收口，触发 redirect 冲刷流水线。白盒观测的信号包括 matchInvalid 标记、memoryViolation 信号和随后的 redirect 事件。重定向后 younger load 重新发射并正确完成写回。本功能点的验证边界在于区分 dataInvalid 场景（数据暂不可用）与真正的 memoryViolation 场景（地址冲突），以及确保 redirect 后 load 能够正确重试并最终获得有效数据。

| 项目 | 描述 |
|------|------|
| 输入 | older store 已入 SQ 但仅地址有效、数据无效 (dataInvalid=1)；younger translated load 访问同一地址，dcache hit。 |
| 处理 | SQ forward 返回 dataInvalid=1 + matchInvalid=1；younger load 在 memoryViolation 上以 level=1 收口，触发 redirect 冲刷流水线。 |
| 输出 | 观测 matchInvalid=1、memoryViolation 信号、redirect；最终 younger load 重发并正确写回。 |
| 验证 | 观测 writeback / drain / event 等，与参考模型比对 |

---

### CMB-LD-004: hi-prio replay 抢占 fast replay

本功能点验证 LoadUnit 中不同优先级 replay 请求的仲裁机制，确保高优先级 replay（如 forward_dchannel 或 nc_replay）能够抢占正在执行的低优先级 fast replay（如 bank conflict replay）。DUT 的 LoadUnit 维护多个 replay 源，当一条 load 因 bank conflict 进入 fast replay 流程时，另一条更高优先级的 load 可能在同一拍或后续拍发起 replay。此时仲裁逻辑将 BC fast replay 取消，将其重新排入 replay queue，优先处理 hi-prio replay 请求。白盒观测信号包括 fastReplay 被取消时的握手信号、replay queue 的 enqueue 操作、以及 replayHiPrio 的源标识。验证的关键边界是确保被抢占的 BC load 最终仍能完成——它从 fast replay 降级为普通 replay queue 中的条目后不应被遗漏。本功能点与 CMB-LD-002 的关系是：后者验证 BC replay 的基础功能，前者验证 BC replay 在优先级竞争场景下的正确降级与最终收敛。

| 项目 | 描述 |
|------|------|
| 输入 | 三条 load：一条触发 bank conflict (BC) fast replay，同拍或后续拍另有一条触发更高优先级 replay (forward_dchannel 或 nc_replay)。 |
| 处理 | BC fast replay 被 hi-prio replayHiPrio 请求取消，改落 replay queue；两条 hi-prio load 先完成写回，BC load 最后完成。 |
| 输出 | 观测到 fastReplay 被取消 + replay queue enqueue；replayHiPrio 源 (forward_dchannel/nc) 和 BC 全部收敛。 |
| 验证 | 观测 writeback / drain / event 等，与参考模型比对 |

---

### CMB-LD-005: late-STA store-load violation

本功能点验证当 older store 的地址（STA）晚于 younger load 到达时，store-load memory violation 检测的时序正确性。在 DUT 执行流程中，younger load 首先发射并访问 DCache，因 bank conflict 触发 BC fast replay。此时 older store 的 STA 尚未发出，因此首次 replay 不会检测到地址冲突。older store 的 STA 稍后到达 SQ，其地址与 younger load 地址重叠，触发 st-ld memoryViolation 检测。younger load 在 replay 过程中收到 violation 信号后再次被重定向，第三次发射时才获得正确数据。白盒观测的关键信号是 BC fast replay 事件与 memoryViolation 事件的先后出现顺序，以及 younger victim load 在 violation 后的重试过程。本功能点的验证边界在于时序窗口的精确性——如果 STA 在 load 完成前到达，则 violation 应被检测到；如果 STA 在 load 完成后才到达，则不构成 violation（此时 store 应走 forward 路径）。与 CMB-LD-003 的区别是：003 是 dataInvalid forward 的 violation，005 是 late-STA 的时序竞争 violation。

| 项目 | 描述 |
|------|------|
| 输入 | younger load 先 issue 并命中 DCache bank conflict (BC fast replay)；older store 稍后才发 STA，地址与 younger load 重叠。 |
| 处理 | younger load 先落 BC fast replay；older store STA 到达后触发 st-ld memoryViolation；younger victim load 重发后完成；older store commit + drain。 |
| 输出 | 观测 BC fast replay + memoryViolation 双重事件；store 正常提交 drain；younger load 最终数据正确。 |
| 验证 | 观测 writeback / drain / event 等，与参考模型比对 |

---

### CMB-LD-006: MMU fault pipeline probe (fault 纯度检查)

本功能点验证 load 在 MMU 地址翻译阶段产生异常时，LoadUnit 是否能够干净地处理异常，不产生任何与数据访问相关的副作用。DUT 中 LoadUnit 在 s1/s2 阶段接收来自 TLB 或 PMP 的异常信号（如 LOAD_PAGE_FAULT 或 LOAD_ACCESS_FAULT），此时 pipeline 应立即停止向 DCache 发送 outer request，也不应尝试 SQ forward，更不应产生 memoryViolation 或 ECC error。白盒观测的核心信号是 intWriteback.exception 位的正确设置，以及各异常 type 字段的精确编码。同时需要确认 DCache outer request、SQ forward match、memoryViolation 和 ECC error 等信号在此场景下均为非活跃状态。唯一允许的重放机制是 TM（transaction memory）replay_queue。本功能点与正常 load 路径的本质区别在于验证 fault 场景下的路径纯洁性——异常 load 不应在 cache/memory 层面留下任何痕迹，确保异常处理的隔离性。

| 项目 | 描述 |
|------|------|
| 输入 | 构造 translation fault / access fault / permission fault 背景下的 load 请求，TLB hit 或 miss。 |
| 处理 | LoadUnit 在 s1/s2 阶段收到 TLB/PMP 异常信号，不发出 DCache outer request，不产生 memoryViolation，直接以异常写回。 |
| 输出 | intWriteback.exception 位正确 (LOAD_PAGE_FAULT / LOAD_ACCESS_FAULT)；无 dcache/outer request、无 SQ forward、无 violation、无 ECC error；仅 TM replay_queue 允许。 |
| 验证 | 观测 writeback / drain / event 等，与参考模型比对 |

---

### CMB-LD-007: 标量 load misalign

本功能点验证标量 load 在地址非对齐到访问宽度时 DUT 的行为。RISC-V 规范对 misaligned load 的支持是可选的，XiangShan 微架构中 load misalign buffer 已被移除，因此 misalign 的处理退化为依赖 replay cause（BC/MF）机制或异常路径。在 DUT 当前实现中，misaligned load 可能触发 bank conflict（BC）replay 或 merge false（MF）replay，也可能由软件通过异常处理程序模拟完成。白盒观测信号为 replay cause 编码中是否出现与 misalign 相关的标记位。本功能点的验证难点在于当前尚无稳定可复现的定向 case 来精确触发 misalign 语义。与 CMB-LD-002（bank conflict）的区别是：misalign 可能因跨 bank 访问而表现为 BC replay，但其根本原因是地址非对齐而非同 bank 竞争，两者的 replay cause 编码需要被仔细区分。

| 项目 | 描述 |
|------|------|
| 输入 | load 地址非对齐到访问宽度 (如 ld 从 addr+0x7 开始)。 |
| 处理 | (当前未覆盖) DUT 中 load misalign buffer 已删除，misalign 语义通过 replay cause (BC/MF) 和异常路径保留。 |
| 输出 | 期望观测到 misalign 相关的 replay cause 或异常；当前尚无稳定 case。 |
| 验证 | 观测 writeback / drain / event 等，与参考模型比对 |
