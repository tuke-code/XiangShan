# Replay Functional Points

本文件描述 MemBlock replay / 违例恢复的功能验证点，采用 IPO (Input-Process-Output) 模式组织。

---

### SCN-RPL-001: FF (forward fail replay)

在乱序执行的处理器中，store 指令的操作分为地址计算 (STA) 和数据准备 (STD) 两个阶段，两者可能不同拍到达 Store Queue (SQ)。当一条 younger load 访问的地址与 SQ 中 older store 的地址匹配、但该 store 的数据尚未就绪（dataInvalid=1）时，硬件无法完成 store-to-load forwarding，从而触发 forward fail (FF) replay。FF replay 是一种 fast replay 机制，load 不需要进入 replay queue，而是在检测到 dataInvalid 信号后直接取消当前拍的处理并在后续拍重新发出。本验证点通过控制 STA 先于 STD 到达的时序关系，构造地址匹配但数据未就绪的条件，验证 SQ forwarding 逻辑中 dataInvalid 检测和 FF replay cause 上报的正确性。关键观测点是 replay 后 load 最终从就绪的 store 获取正确数据并完成写回，验证 replay 恢复路径的完整性。边界条件包括同一地址有多条 older store 时、以及 store 数据宽度与 load 不对等时的 forwarding 行为。

| 项目 | 描述 |
|------|------|
| 输入 | older store 的 STA 先于 STD 到达 (地址有效、数据无效)；younger load 访问同地址。 |
| 处理 | SQ forward 检测到 dataInvalid=1 (地址匹配但数据未就绪) → 产生 forward fail (FF) replay；younger load 进入 replay，等 older store STD 就绪后重发。 |
| 输出 | 观测 FF replay cause；SQ forward dataInvalid=1；replay 后 load 正确写回。 |
| 验证 | 观测信号/事件，与参考模型比对 |

---

### SCN-RPL-002: DM (dcache miss replay)

当一条 cacheable load 访问的地址在 DCache 中缺失 (tag mismatch) 时，硬件需要暂停该 load 的处理，向外部存储器发出 AcquireBlock 请求以获取缓存行数据，待 refill 完成后重新执行 load。这个过程称为 dcache miss (DM) replay。DM replay 的实现路径涉及 LoadUnit 在流水线 s3 阶段检测 tag miss 后产生 replay 请求，load 进入 replay queue 等待 refill 完成信号，当 refill 数据写回 DCache SRAM 后 load 从 replay queue 弹出并重发。本验证点通过发射 cold cacheable load（首次访问某 cacheline）来触发完整的 DM replay 闭环：从 AcquireBlock 请求发出、到 outer 返回 GrantData、再到 DCache tag 更新和 load 重发命中。验证的关键观测点包括 DM replay cause 信号的正确置位、AcquireBlock 到 GrantData 事务的完整性和时序合规性。边界条件包括 refill 期间同地址的后续 load 是否合并处理、以及多个 miss load 同时进入 replay queue 时的排队行为。

| 项目 | 描述 |
|------|------|
| 输入 | cold cacheable load (首次访问该 cacheline)。 |
| 处理 | DCache tag 不命中 → AcquireBlock 到 outer memory；load 在 s3 收到 miss 信号 → DM replay；refill 完成后 load 重发并命中。 |
| 输出 | 观测 DM replay cause；DCache AcquireBlock → GrantData 闭环；replay 后 load 正确写回。 |
| 验证 | 观测信号/事件，与参考模型比对 |

---

### SCN-RPL-003: NC (uncache/nc path replay)

非缓存 (uncache/NC) load 包括 IO 地址空间（物理地址低于 0x80000000）的访问以及通过 PBMT (Page-Based Memory Type) 标记为 NC 或 MMIO 的页面访问。这类 load 不走 DCache 路径，而是通过 outer 总线直接发出 TL-A GET 请求获取数据。当 outer 响应延迟较高或总线拥堵时，load 在流水线中无法及时获得返回数据，需要触发 NC replay 机制暂停当前 load 的处理。NC replay 与 DM replay 的区别在于它不涉及 cacheline refill，仅等待 outer 的单次数据响应。本验证点验证 NC replay cause 的正确产生和 nc_out 路径选择信号的准确性。验证需要通过地址范围（低于 0x80000000）或页表属性（PBMT=NC/MMIO）两种方式构造 uncache load，覆盖 NC 属性的两种来源。关键观测点是 replay 后 outer 响应到达并正确写回，验证 uncache 路径下 load 的完整生命周期。边界条件包括 PBMT 属性与 PMA 属性冲突时的优先级行为和 NC replay queue 满时的 backpressure 处理。

| 项目 | 描述 |
|------|------|
| 输入 | IO/uncache load (addr < 0x80000000 或 PBMT=NC/MMIO)。 |
| 处理 | LoadUnit 识别 uncache 属性 → 走 outer TL-A GET 路径；若 outer 响应未及时就绪 → NC replay；响应到达后 load 重发。 |
| 输出 | 观测 NC replay cause 或 nc_out 信号；outer 路径闭环；replay 后 load 正确写回。 |
| 验证 | 观测信号/事件，与参考模型比对 |

---

### SCN-RPL-004: RAW (read-after-write violation)

RAW (Read-After-Write) violation 发生在 older store 尚未完成（STA 或 STD 缺失）而 younger load 已进入 Load Queue (LQ) 且无法确认 forwarding 结果的场景。LQ 中的 RAW 检测逻辑 (LQRAW) 会跟踪每个 load 对应的 older store 的完成状态，当较链的 store 在 load 发射后仍未就绪时，LQRAW 项被填满，触发 RAW violation nuke。RAW nuke 是一种 pipeline 冲刷机制，不同于 fast replay，它会冲刷 younger load 及其后续所有指令，恢复执行后 load 重新发射。本验证点通过构造 store 缺少 STA 或 STD 的场景，使 younger load 进入 LQRAW 满的状态，验证 RAW violation 的触发条件和 nuke 恢复流程。验证需覆盖 nuke 信号的正确置位、LQRAW backpressure 信号的产生条件以及 nuke 后 load 重发并正确获取数据。边界条件包括 committed 与 uncommitted store 窗口对 RAW 检测行为的影响，store 完全覆盖与部分覆盖 load 地址时的细分行为。

| 项目 | 描述 |
|------|------|
| 输入 | older store 缺少 STA/STD (未完成地址/数据就绪)；younger load 访问同地址，LQRAW 检测到 store 缺失 → RAW violation。 |
| 处理 | LQRAW 填满 (older store 占位但未完成) → younger load 无法确认 forwarding → RAW violation nuke；冲刷 younger load 及后续指令；older store 完成后 younger load 重发。 |
| 输出 | 观测 RAW violation nuke；LQRAW backpressure；replay 后 load 数据正确。 |
| 验证 | 观测信号/事件，与参考模型比对 |

---

### SCN-RPL-005: RAR (read-after-read violation)

RAR (Read-After-Read) violation 是 RISC-V 内存模型为保证 load-load ordering 而设计的违例恢复机制。当一条 older load A 因 load-wait 或其他原因暂停在流水线中，而一条同地址的 younger load B 先于 A 完成写回时，如果后续发生了 probe release（外部 snoop 使该缓存行失效），则 A 和 B 的相对顺序被破坏，需要触发 RAR violation nuke。RAR nuke 会冲刷 younger load B 及其后续指令，确保 older load A 完成后 younger load B 重新获取最新数据。本验证点通过精确控制两条同地址 load 的发射和完成时序，构造 A 暂停而 B 先完成的条件，再注入 probe release 事件触发 RAR 检测逻辑。验证的关键观测点包括 RAR violation nuke 信号的正确产生、probe release 与 load 完成时序的因果关系、以及 nuke 后 load 数据的正确性。边界条件包括 release 在 younger load 完成前或完成后到达的不同时序组合，以及 replay queue 满和 commit 阻塞等 backpressure 条件的叠加。

| 项目 | 描述 |
|------|------|
| 输入 | older load A 因 load-wait 暂停 (精确等待某 robIdx)；younger load B 访问同地址，先于 A 完成；probe release 后检测到 ld-ld ordering violation。 |
| 处理 | LQ RAR 检测到 younger 同地址 load 先完成 → RAR violation nuke；冲刷 younger load 及后续指令；older load A 完成后 younger load B 重发。 |
| 输出 | 观测 RAR violation nuke；probe release 事件；replay 后 load 数据正确。 |
| 验证 | 观测信号/事件，与参考模型比对 |

---

### SCN-RPL-006: BC (bank conflict)

DCache 通常被划分为多个 bank 以支持并行访问，每个 bank 是独立的 SRAM 端口。当两条 load 在同拍或相邻拍访问同一 DCache bank 的不同 cacheline 时，会产生 bank conflict，导致其中一条 load 无法在该拍获得 bank 服务。Bank conflict (BC) replay 是一种高效的 fast replay 机制——冲突 load 在检测到 bank conflict hit 后被取消（ldCancel=1），不进入 slow replay queue，直接在下一拍重新尝试发射。本验证点通过构造并发 load 或背靠背 load 访问同 bank 不同 cacheline 的场景，验证 BC replay cause 和 fastReplay 信号的正确产生。BC replay 不涉及外部总线事务，是 DCache 内部 bank 仲裁的微架构事件，因此恢复延迟极短。验证需覆盖不同 bank 之间的并行无冲突场景作为对比基线，确保 BC replay 仅在真实 bank 冲突时触发。关键观测点包括 BC replay cause 置位、ldCancel 取消信号、fastReplay 标记、以及无 slow replay queue enqueue 的快速恢复路径。

| 项目 | 描述 |
|------|------|
| 输入 | 两条 load 同拍或相邻拍访问同一 DCache bank 的不同 cacheline。 |
| 处理 | DCache bank 冲突 → 低优先级 load 收到 bank conflict hit → BC fast replay (ldCancel=1) → 下一拍重发命中。 |
| 输出 | 观测 BC replay cause + fastReplay + ldCancel；无 slow replay queue enqueue；replay 后 load 写回正确。 |
| 验证 | 观测信号/事件，与参考模型比对 |

---

### SCN-RPL-007: NK (pipeline st-ld nuke)

当一条 older store 的数据已就绪 (STD 已发送) 但其地址尚未发出 (STA 未发送) 时，如果一条 younger load 在此期间发射并访问了同地址，就会产生 store-to-load (st-ld) hazard。由于 STD 先到达而 STA 后到达，SQ 在 load 发射时尚未建立地址匹配的 forwarding 通道；当 STA 在 load 之后补充时，pipeline 检测到这一 hazard 并触发 NK (nuke) 冲刷。NK nuke 会清除 younger load 及其后续指令，待 store 的 STA 完成并建立正确的 forwarding 关系后，load 重新发射并正确获取数据。本验证点通过精确构造 STA 晚于 STD 到达的时序，验证 NK nuke 的触发条件和恢复路径。关键区别在于 NK nuke 发生在 store 的数据已完成但地址延迟到达的场景，这与 RAW violation（store 整体未完成）和 FF replay（地址已匹配但数据未就绪）的触发条件不同。验证需观测 STA 到达时序与 nuke 事件的因果关系、nuke 信号的正确置位、以及重新发射后 load 的正确写回。

| 项目 | 描述 |
|------|------|
| 输入 | older store 数据已就绪 (STD 已发) 但地址未发 (STA 未发)；younger load issue 后 older store 补充 STA。 |
| 处理 | 在 younger load 已发射后 older store STA 到达 → pipeline 检测到 st-ld hazard → NK (nuke) 冲刷 younger load 及后续 → younger load 重发。 |
| 输出 | 观测 NK nuke 事件；STA 到达时序与 nuke 的因果关系正确；replay 后 load 写回正确。 |
| 验证 | 观测信号/事件，与参考模型比对 |

---

### SCN-RPL-008: BC + dataInvalid + nuke 组合

在真实处理器运行中，不同的 replay 和 nuke 原因可能同时或交替发生，验证各机制的独立性和组合收敛性是关键。本验证点构造同时涉及 DCache bank conflict、SQ dataInvalid 和 pipeline st-ld nuke 三重因素的复合场景。具体设计是：多条 load/store 相互作用，其中一条 younger load 同时面临同 bank 冲突、older store 数据未就绪、以及 st-ld hazard 等多重约束。验证的核心目标是确认 BC fast replay、FF replay 和 NK nuke 三种恢复机制在组合场景下互不干扰，各自正确触发并按优先级顺序完成恢复。DUT 行为路径上，load 首拍可能因 bank conflict 被取消 (BC)，次拍重发后发现 dataInvalid (FF)，再等待 store 数据就绪后最终获取数据。关键观测点是全部 load 最终正确写回，验证组合恢复路径的闭环完整性。边界条件包括三种机制的触发顺序重叠和恢复资源（replay queue entry、nuke credit）竞争场景。

| 项目 | 描述 |
|------|------|
| 输入 | 构造同时触发 bank conflict、SQ dataInvalid=1/matchInvalid=0、pipeline st-ld nuke 的复合场景。 |
| 处理 | 三条 load/store 相互作用：dcache hit + bank conflict + SQ forward dataInvalid + st-ld nuke 同时或顺序触发。各 replay/nuke 机制独立运作、组合收敛。 |
| 输出 | 观测 BC + dataInvalid + nuke 三重事件；全部 load 最终正确写回。 |
| 验证 | 观测信号/事件，与参考模型比对 |

---

### SCN-RPL-009: SQ dataInvalid + matchInvalid + nuke

当处理器的 satp（页表基址寄存器）切换后，同一虚拟地址 (VA) 可能被重新映射到不同的物理页面（PPN 改变）。此时，SQ 中仍保留着在旧映射下入队的 older store 信息，而新发射的 younger load 使用新映射进行地址翻译。SQ forward 检测发现 VA 地址匹配但 PPN 不匹配，同时置位 dataInvalid=1（数据无效）和 matchInvalid=1（映射不匹配）。这一条件会触发 memoryViolation 和指令重定向 (redirect)，不同于普通的 FF replay。本验证点通过 satp 切换构造 VA 重映射场景，验证 matchInvalid 检测逻辑的正确性。DUT 行为路径上，younger load 在地址翻译后进入 SQ forward 检查，发现匹配但 PPN 不同时产生 memoryViolation 异常，触发 redirect 冲刷流水线并跳转到异常处理程序。关键观测点包括 dataInvalid=1 和 matchInvalid=1 的同时出现、memoryViolation 异常上报、以及 redirect 流程的完整性。当前该功能点仅部分覆盖，主要难点在于 satp 切换的精确时序控制和异常恢复路径的观测能力。

| 项目 | 描述 |
|------|------|
| 输入 | satp 切换后同一 VA 映射到不同物理页；older store 在旧映射下入 SQ，younger load 在新映射下访问同 VA。 |
| 处理 | SQ forward 地址匹配 (同 VA) 但 PPN 不匹配 → dataInvalid=1 + matchInvalid=1；younger load 触发 memoryViolation+redirect。 |
| 输出 | (部分覆盖) 观测 dataInvalid=1, matchInvalid=1, memoryViolation, redirect；replay 后 load 正确写回。 |
| 验证 | 观测信号/事件，与参考模型比对 |

---

### SCN-RPL-010: FF + DM + NC 小规模混合流

在验证单个 replay 机制的独立正确性之后，需要验证多个 replay 类型在流水线中共存时的行为。本验证点以固定的场景模板串联触发 FF、DM 和 NC 三种 replay：先发射一条触发 FF 的 load（依赖 older store 数据未就绪），再发射一条冷缓存 load 触发 DM，最后发射一条 IO 地址 load 触发 NC。三条 load 依次进入 replay queue，各自独立的 replay 条件被解决后重新发射。验证的核心目标是三类 replay cause 信号各自正确置位、replay queue 的多 entry 管理正确（包括 enqueue 顺序和 dequeue 优先级）、以及三类恢复流程互不干扰。DUT 行为路径上，replay 调度器需要处理不同 replay 类型的优先级——fast replay（如 BC）优先于 slow replay（如 DM/NC），而 DM 和 NC 之间按进入顺序公平调度。关键观测点是所有 load 最终正确写回，确保 replay queue 不会因混合负载而出现死锁或优先级反转。此场景也是验证 replay queue 容量和 backpressure 机制的基础负载。

| 项目 | 描述 |
|------|------|
| 输入 | 以固定模板串联 FF、DM、NC 三类 replay：先发一条 FF 场景 (store+load)，再发一条 DM 场景 (cold load)，再发一条 NC 场景 (IO load)。 |
| 处理 | 三类 replay 各自独立触发和收敛；不相互干扰；共用 replay queue 时优先级正确。 |
| 输出 | 观测 FF、DM、NC 三类 replay cause；全部 load 正确写回。 |
| 验证 | 观测信号/事件，与参考模型比对 |

---

### SCN-RPL-011: RAW replay 细分 (full/partial overlap)

当前该功能点尚未覆盖 (marked 未覆盖)，其验证需求是在已实现的 RAW violation 框架下进一步细分 overlap 类型。RAW violation 的具体行为取决于 older store 与 younger load 地址重叠的程度：full overlap 指 store 完全覆盖 load 的访问地址范围（例如 8 字节 store 覆盖 4 字节 load），此时 LQRAW 的全部地址位匹配，触发标准的 RAW violation nuke。Partial overlap 指 store 仅部分覆盖 load 的地址范围（例如 4 字节 store 仅覆盖 8 字节 load 的低 4 字节），此时行为可能取决于硬件是否支持地址部分匹配检测。验证矩阵需组合 committed（已提交）与 uncommitted（未提交）两种 store 窗口状态，以及 full overlap 与 partial overlap 两种重叠程度，构成 2x2 的覆盖矩阵。当前未被覆盖的主要原因是 partial overlap 场景的硬件行为在微架构层面可能存在设计未明确定义的分支，验证环境的参考模型也缺少对应的精细建模。对于未覆盖项，本文件将保留其 IPO 框架并标注未覆盖状态，待硬件设计明确后再补充定向测试。

| 项目 | 描述 |
|------|------|
| 输入 | (未覆盖) 构造 full overlap (store 完全覆盖 load 地址) 和 partial overlap (store 部分覆盖) 的 RAW 场景。 |
| 处理 | full overlap: LQRAW 全部地址位匹配 → RAW violation。partial overlap: 部分地址位匹配 → 细分行为 (当前矩阵未打全)。 |
| 输出 | 期望覆盖 committed/uncommitted window × full/partial overlap 矩阵。 |
| 验证 | 观测信号/事件，与参考模型比对 |

---

### SCN-RPL-012: RAR violation 细分 (release / backpressure)

当前该功能点尚未覆盖 (marked 未覆盖)，其验证需求是在已实现的 RAR violation 通用框架下扩展 release 时序和 backpressure 条件的组合矩阵。Release 时序维度上，probe release 信号可能在 younger load 完成之前或之后到达，前者会直接使 younger load 的缓存行失效而触发 RAR 检测，后者可能在 load 已写回后才收到 release，此时是否需要 RAR nuke 取决于内存模型的 ordering 要求。Backpressure 维度上，replay queue 满、commit 阻塞、ROB 满等 pipeline 反压条件可能影响 RAR nuke 的触发时机和恢复路径。验证矩阵需要覆盖 release 时序（完成前/后）与 backpressure 条件（正常/满/阻塞）的各种组合。当前未被覆盖的主要原因是精确控制 probe release 的到达时序在验证环境中需要较复杂的协调机制（需要同时控制 TL-C/D 通道的时序和 LoadUnit 内部的 load-wait 状态），对验证序列的时序精度要求很高。此外，backpressure 条件的构造需要将 pipeline 填充到接近满的状态，测试序列开销较大。

| 项目 | 描述 |
|------|------|
| 输入 | (未覆盖) 构造不同 release 时序和 backpressure 条件下的 RAR violation。 |
| 处理 | release 时序: probe release 在 younger load 完成前/后到达。backpressure: replay queue 满、commit 阻塞等条件下的 RAR 行为。 |
| 输出 | 期望覆盖 release 时序 × backpressure 组合矩阵。 |
| 验证 | 观测信号/事件，与参考模型比对 |

---

### SCN-RPL-013: load-wait / waitForRobIdx 定向

当前该功能点尚未覆盖 (marked 未覆盖)，其验证需求是构造 load 进入 load-wait 状态的定向场景。Load-wait 是当 load 遇到特定条件（如等待 older 指令完成、等待清空缓冲区）时进入的一种暂停状态，此时 load 已完成 issue 但暂停在流水线中等待 waitForRobIdx 信号或 ROB 指针条件解除。本验证点要求发射一条 load 并配置其等待特定的 robIdx 完成，观测 load 在 issue 后注入 load-wait 状态的信号行为，等待解除后继续完成写回。当前未被覆盖的主要原因是 load-wait 的触发条件在 MemBlock 验证环境中缺少直接的微架构控制接口。Load-wait 通常由 LoadUnit 内部状态机根据多种条件综合判断是否进入等待，验证环境难以独立断言 load 确实处于 wait 状态而不依赖于仿真模型的内部可见性。解决这一覆盖缺口需要增强验证环境的观测能力，或通过增加 DUT 内部信号的暴露 (internal signal observation) 来直接采样 load-wait 状态。

| 项目 | 描述 |
|------|------|
| 输入 | (未覆盖) 发送一条 load 并指示其等待特定 robIdx (load-wait)；或等待 waitForRobIdx 信号。 |
| 处理 | load 在 issue 后进入 load-wait 状态，暂停 s3 写回；等 older robIdx 的指令完成后 load-wait 解除；load 继续完成写回。 |
| 输出 | 期望观测 load-wait 状态、waitForRobIdx 信号、wait 解除后正常写回。 |
| 验证 | 观测信号/事件，与参考模型比对 |
