# MMU 功能点

> 本文档使用 Input-Process-Output (IPO) 模式描述 MMU 相关功能点。

---

### BAS-MM-001: Sv39 地址空间安装与翻译命中

本功能点验证 Sv39 分页机制下 DTLB 缺失后页表遍历（PTW）的完整流程以及翻译命中后 load 数据通路的正确性。DUT 的 MMU 包含 DTLB（data TLB）、ITLB（instruction TLB）和共享的 PTW（page table walker）引擎。当 translated load 发射后，DTLB 查找虚拟地址未命中，触发 PTW 通过 TileLink 总线逐级读取多级页表——从非叶子节点 PTE（non-leaf）到叶子节点 PTE（leaf）。PTW 完成遍历后将翻译结果（物理页号 PPN + 页内偏移）写入 DTLB，load 重试时 DTLB 命中，地址翻译完成。白盒观测信号包括 PTW 的 TileLink 请求序列（验证页表遍历路径的正确性）和 DUT 输出的 debug_paddr（验证翻译结果的正确性）。验证的边界条件包括多级页表中任意一级 PTE 的权限位组合是否正确传递，以及 satp.MODE 寄存器在不同模式（Bare/Sv39/Sv48）下翻译逻辑的正确切换。

| 项目 | 描述 |
|------|------|
| 输入 | 通过 env.mmu facade 安装 Sv39 4KB 页表 (多级 non-leaf + leaf PTE)，启用 Sv39 translation mode (satp.MODE=Sv39)，发送 translated load。 |
| 处理 | DTLB miss 触发 PTW (page table walker) 遍历页表；PTW 通过 TileLink 读取 non-leaf PTE -> leaf PTE；DTLB refill 后 TLB hit，返回翻译后的物理地址 (PPN+offset)。 |
| 输出 | load 走 DCache 或 outer 路径，debug_paddr 为翻译后的物理地址；writeback 数据与 HPA 处 preload 值一致。 |
| 验证 | 观测 writeback / drain / event 等，与参考模型比对 |

---

### BAS-MM-002: DTLB fill + replacement

本功能点验证 DTLB 在超出容量时的 entry 替换行为，确保替换算法（LRU 或随机）不会导致翻译信息的丢失或错误。DUT 的 DTLB 是一个容量有限的 fully-associative 或 set-associative 结构。当大量不同 4KB 页的 load 连续发射时，DTLB 依次经历 miss->PTW->fill 的完整周期；TLB 填满后，新页的访问迫使旧 entry 被逐出（eviction）；被逐出的旧页再次访问时重新触发 miss->refill。白盒观测信号包括 TLB miss 计数、TLB fill 事件和 eviction 事件的对应关系。验证的核心挑战是确保 entry 被逐出后，其页表信息被正确丢弃，不会用过期数据误导后续翻译；同时 refill 后新 entry 的 PPN 与页内偏移拼接必须精确无误。本功能点与 BAS-MM-001 的关系是：001 验证单次翻译的完整性，002 验证替换压力下翻译系统的鲁棒性。

| 项目 | 描述 |
|------|------|
| 输入 | 发送大量不同 4KB 页的 translated load，超出 DTLB 容量。 |
| 处理 | 初始 load TLB miss -> PTW -> DTLB fill；后续同页 load TLB hit；当 DTLB 满时旧 entry 被替换 (LRU/随机)；旧页再次访问时重新 miss -> refill。 |
| 输出 | 观测到 miss->fill->hit->eviction->re-miss 完整生命周期；每次 load writeback 数据正确，证明翻译链在 replacement 后仍正确。 |
| 验证 | 观测 writeback / drain / event 等，与参考模型比对 |

---

### BAS-MM-003: PMP CSR 编程 (all 32 entries)

本功能点验证物理内存保护（PMP）模块的全部 32 个 entry 的 CSR 编程路径正确性。PMP 是 RISC-V 中物理内存访问权限控制的核心机制，包含 8 个配置 CSR（pmpcfg0-7，每个覆盖 4 个 entry）和 32 个地址 CSR（pmpaddr0-31）。编程时 pmpcfg 寄存器的跨字打包（即两个 pmpcfg 字中的 8 个 entry 的 cfg 字段如何紧凑排列）是常见的出错点。白盒观测信号包括每个 pmpcfg 和 pmpaddr CSR 的 read-back 值是否与写入值一致。验证的边界包括 TOR（top-of-range）、NAPOT（naturally-aligned power-of-two）和 OFF 三种地址匹配模式的 cfg 字段编码正确性，以及 R/W/X 权限位和 L（lock）位的组合写入是否被正确处理。非 0-31 索引的非法 entry 访问应被拒绝。本功能点与 CMB-MM-004（store-side PMP deny）的关联是：本功能点验证 PMP 的配置正确性，后者验证 PMP 在 store 路径上能否正确触发 deny。

| 项目 | 描述 |
|------|------|
| 输入 | 通过 env.mmu.program_pmp_entry() 编程 32 个 PMP entry，覆盖 pmpcfg 字的跨字打包。 |
| 处理 | PMP CSR 写 (pmpcfg0-7, pmpaddr0-31)；验证 TOR/NAPOT 模式、R/W/X 权限位、L 锁定位的 CSR 编程正确性。 |
| 输出 | PMP entry 的值 read-back 验证；验证非法 index (非 0-31) 被拒绝。 |
| 验证 | 观测 writeback / drain / event 等，与参考模型比对 |

---

### BAS-MM-004: H-extension two-stage translation load

本功能点验证 RISC-V Hypervisor 扩展（H-extension）中两级地址翻译——VS-stage（虚拟化阶段）和 G-stage（guest 阶段）——串联工作的正确性。DUT 中虚拟化翻译由 vsatp（guest 虚拟地址到 guest 物理地址）和 hgatp（guest 物理地址到 hypervisor 物理地址）两套页表共同控制。当 VS-stage TLB 未命中时，PTW 首先遍历 vsatp 指向的 VS 页表，将 VS VA 翻译为 GPA（guest physical address）；若 G-stage TLB 也缺失，PTW 继续遍历 hgatp 指向的 G 页表，将 GPA 翻译为最终的 HPA（hypervisor physical address）。两级翻译的结果最终被合并写入 TLB（合并后的 VA->HPA 映射或分级的 VA->GPA 和 GPA->HPA 映射）。白盒观测信号包括 PTW 在两级页表遍历时的 TileLink 请求序列（以区分当前处于哪一级翻译）以及最终输出的 debug_paddr 是否等于 HPA。验证的边界条件包括两套 satp 寄存器的 mode 组合（如 VS-stage Sv39 + G-stage Sv39，或其中一个为 Bare）、以及同一页面在两级的权限不同时如何取小者作为最终权限。

| 项目 | 描述 |
|------|------|
| 输入 | 启用 vsatp (VS-stage Sv39) + hgatp (G-stage Sv39)，安装两级页表 (VS VA->GPA + GPA->HPA)，发送 translated load。 |
| 处理 | VS-stage TLB/PTW 将 VS VA 翻译为 GPA；G-stage TLB/PTW 将 GPA 翻译为 HPA；两级翻译串联完成。 |
| 输出 | debug_paddr 为最终 HPA；load writeback 数据与 HPA 处 preload 值一致。 |
| 验证 | 观测 writeback / drain / event 等，与参考模型比对 |

---

### BAS-MM-005: H-extension two-stage fault

本功能点验证 H-extension 两级翻译下，在不同阶段发生的异常是否能被正确识别和报告。DUT 中可能的 fault 场景包括：VS-stage 叶子 PTE 无效（PTE.V=0）触发 GuestPageFault、G-stage 叶子 PTE 无效同样触发 GuestPageFault、以及 G-stage 翻译成功后 PMP 拒绝触发 access fault。VS-stage fault 的 gpaddr（guest physical address）为 VS leaf PTE 的 PPN 左移 12 位；G-stage fault 的 gpaddr 为 VS-stage 翻译产出的 GPA。PMP deny 仅在 G-stage 翻译成功后才会进行——若 G-stage 本身已 fault，则不会检查 PMP。白盒观测信号包括 exception 向量和 gpaddr 字段的精确值。本功能点与 BAS-MM-004 的对比关系是：004 验证两级翻译的成功路径，005 验证两级翻译的 fault 路径，二者共同覆盖 H-extension 翻译的完整正确性空间。与 CMB-MM-002（permission fault）的区别在于 005 关注的是页表结构异常（PTE.V=0），而 002 关注的是权限位不匹配（U=1 + S-mode + sum=0）。

| 项目 | 描述 |
|------|------|
| 输入 | 构造 VS-stage leaf invalid (guest page fault) 或 G-stage leaf invalid (guest page fault)。 |
| 处理 | VS-stage fault: VS PTE.V=0 触发 GuestPageFault，gpaddr 为 VS leaf PTE 的 PPN<<12。G-stage fault: G-stage PTE.V=0 触发 GuestPageFault，gpaddr 为 VS-stage 的 GPA。PMP deny 在 G-stage 成功后触发 access fault。 |
| 输出 | exception 位与 gpaddr 正确；PMP deny 场景收口到 access fault 而非 guest page fault。 |
| 验证 | 观测 writeback / drain / event 等，与参考模型比对 |

---

### BAS-MM-006: ITLB PTW smoke (frontend-side)

本功能点验证前端 ITLB（instruction TLB）缺失后通过共享 PTW 完成翻译的路径，确保指令获取侧和 LSU 数据侧的翻译一致性。DUT 中 frontend（取指单元）通过 frontendBridge 向 mem-side 发送 ITLB 翻译请求。当 ITLB miss 时，frontend 将请求转发到与 DTLB 共享的同一套 PTW responder，PTW 遍历页表后将翻译结果返回前端。白盒观测信号包括 frontend bridge 上的 TL 请求握手以及 ITLB fill 事件。验证的边界在于确认 ITLB 翻译使用的页表根指针与当前 satp 寄存器一致，且翻译产出的物理地址能够正确路由到 icache。与 BAS-MM-001（DTLB 翻译）的关系是：本功能点验证的是同一套 PTW 在前端侧的重用性——DTLB 和 ITLB 虽然各自独立 TLB 存储，但 PTW 引擎是共享资源。验证中还需确认前端 ITLB PTW 请求与后端 DTLB PTW 请求在共享 PTW 端口上的仲裁不会互相阻塞。

| 项目 | 描述 |
|------|------|
| 输入 | 通过 frontendBridge 发出一条 ITLB/icache TL 请求，翻译背景与 DTLB 共用。 |
| 处理 | ITLB miss 触发 PTW，复用同一套 PTW responder；翻译成功后 icache 请求穿桥到 mem-side，D 响应返回前端。 |
| 输出 | TL 请求完成握手；ITLB PTW 翻译与 DTLB 一致。 |
| 验证 | 观测 writeback / drain / event 等，与参考模型比对 |

---

### CMB-MM-001: B/H/W/D load fault size 矩阵

本功能点验证在同一 MMU fault 背景下（如 translation fault 或 PMP deny），不同数据宽度的 load 指令是否均能正确触发异常。DUT 的 LoadUnit 在 s1/s2 阶段检测到 TLB/PMP 异常信号，无论 load 的 size 是 byte、half、word 还是 doubleword，异常检测逻辑应与数据宽度无关——fault 由虚拟地址页面的属性决定，而非 load 的数据宽度。白盒观测信号为每种宽度下 intWriteback.exception 位的一致断言，以及异常的 type 字段（LOAD_PAGE_FAULT 或 LOAD_ACCESS_FAULT）的正确性。验证的关键边界是确保没有因宽度不同而产生 false negative（某些宽度应该 fault 但未 fault）或 false positive（某些宽度不应该 fault 但触发了 fault）。本功能点与 CMB-MM-002（permission fault mixed size）的区别在于：001 关注的是同一 fault 背景下不同 size 的一致性，002 关注的是 permission fault 在 TLB miss 路径下的表现，前者验证 size 无关性，后者验证权限判定的正确性。

| 项目 | 描述 |
|------|------|
| 输入 | 在同一 fault 背景 (translation fault 或 PMP deny) 下，用 byte/half/word/doubleword 四种宽度逐一发送 load。 |
| 处理 | 每种宽度的 load 在 s1/s2 收到同一 fault 信号；异常写回携带对应 size 信息。 |
| 输出 | 四种宽度的 exception bit 均正确；无宽度相关的 false negative/positive。 |
| 验证 | 观测 writeback / drain / event 等，与参考模型比对 |

---

### CMB-MM-002: mixed size permission fault (TLB miss)

本功能点验证 S-mode 下访问 U-page 时，不同数据宽度的 load 在 TLB miss 后 PTW refill 完成但权限判定失败时的行为。DUT 中当 load 地址所在页的 PTE 权限位为 U=1（user page）而当前运行模式为 S-mode 且 sum=0（supervisor user-memory access disabled），TLB 在 refill 后检测到 permission deny。无论 load 的 size 是 byte、half、word 还是 doubleword，permission fault 判定应与 size 无关——所有宽度均应产生 LOAD_PAGE_FAULT（permission 类型）。白盒观测信号包括 PTW 是否成功完成了从 non-leaf 到 leaf 的完整页表遍历（以确认 refill 成功）、以及 TLB hit 后在 s1/s2 阶段是否正确 raise 了 permission fault。与 BAS-MM-001（翻译命中）的区别是正常路径返回物理地址，fault 路径返回异常编码；与 CMB-MM-001（fault size 矩阵）的区别是 001 使用预置 fault 背景（如直接 PMP deny），002 则需要 PTW 先触发 refill 再在 TLB hit 后进行权限判定，路径更长且涉及 TLB 状态机。

| 项目 | 描述 |
|------|------|
| 输入 | S-mode 背景下访问 U-page (sum=0)，用 byte/half/word/double 四种宽度逐一发送 load，TLB miss 后 PTW 返回 leaf PTE。 |
| 处理 | PTW refill 后 TLB 获得 leaf PTE (U=1, sum=0, S-mode->permission deny)；TLB hit 后返回 permission fault。 |
| 输出 | 四种宽度均产生 LOAD_PAGE_FAULT (permission)；PTW 行为正确 (refill 成功但权限不足)。 |
| 验证 | 观测 writeback / drain / event 等，与参考模型比对 |

---

### CMB-MM-003: 2MB / 1GB 大页翻译

本功能点验证 Sv39 分页中 2MB 大页（megapage）和 1GB 巨页（gigapage）的翻译正确性。大页翻译与 4KB 基础页的关键区别在于页表遍历的终止条件：当 PTW 在非最后一级页表遇到叶子 PTE（isSuperPage=1）时，遍历提前终止，不再继续下一级。对于 2MB megapage，leaf PTE 位于 level-1 页表；对于 1GB gigapage，leaf PTE 位于 level-0 页表。TLB 在 fill 时需根据 isSuperPage 标志将 superpage 的 PPN 与页内偏移（offset）正确拼接——megapage 的 offset 为 21 位，gigapage 的 offset 为 30 位。白盒观测信号包括 PTW 遍历的级数（应在发生 superpage leaf 时停止而非走到 level-2）和 debug_paddr 中 PPN 与 offset 的正确拼接。验证的边界条件包括 2MB/1GB 对齐要求——大页的物理基地址必须对齐到其页面大小，非对齐的大页应被 PTW 视为无效 PTE。

| 项目 | 描述 |
|------|------|
| 输入 | 安装 Sv39 2MB megapage (level-1 leaf) 或 1GB gigapage (level-0 leaf) 页表，发送 translated load。 |
| 处理 | PTW 遍历时在 non-last level 遇到 leaf PTE (isSuperPage=1)，停止遍历；TLB 填入 superpage entry；翻译后的 PPN 按 superpage offset 拼接。 |
| 输出 | load debug_paddr 正确 (superpage PPN + offset)；isSuperPage 路径验证。 |
| 验证 | 观测 writeback / drain / event 等，与参考模型比对 |

---

### CMB-MM-004: store-side PMP deny

本功能点验证 store 在地址翻译后因 PMP 权限检查被拒绝时的异常处理路径。DUT 中 store 的 STA（store address）阶段会根据翻译后的物理地址检查 PMP 权限，若物理地址落入 PMP deny region（如 R/W/X 权限位显示该区域不允许写入），STA 阶段产生 PMP access fault，store 将不会提交（commit）而是以异常收口。白盒观测信号包括 PMP fault 信号的断言和 store 异常回滚的正确性。需要注意的是，store-side PMP deny 的验证完整度目前为部分覆盖（标记为黄色）：与 load-side 相比，store 在异常发生后不会通过 writeback 通道报告（因为 store 不产生 writeback），因此观测路径更为间接。本功能点与 BAS-MM-003（PMP CSR 编程）的关联在于：003 确保 PMP 配置正确性，本功能点在此基础上验证 PMP 在 store 数据通路上的实际执行效果。与 CMB-MM-001（load fault size 矩阵）的区别在于 store 的异常处理路径与 load 完全不同——load 使用 writeback.exception 报告，store 使用回滚/重试机制。

| 项目 | 描述 |
|------|------|
| 输入 | translated store 的最终 HPA 落入 PMP deny region。 |
| 处理 | store 的 STA 阶段检测到 PMP access fault；store 不 commit，以异常收口。 |
| 输出 | (部分覆盖) 观测 PMP fault 信号；store 异常处理正确；当前 store-side deny 仍缺稳定 fault 收口，标记 🟡。 |
| 验证 | 观测 writeback / drain / event 等，与参考模型比对 |
