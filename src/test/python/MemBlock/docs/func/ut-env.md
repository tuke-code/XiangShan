# UT Env Functional Points

本文件描述 MemBlock 验证环境 (UT Env) 的功能验证点，采用 IPO (Input-Process-Output) 模式组织。

---

### UT-ENV-001: env fixture (bootstrap / bundle / clock / reset)

本验证点属于验证环境基础设施层，不依赖真实 DUT 功能，仅验证 Python 侧的 env fixture 生命周期管理。UT-ENV-001 验证 MemBlockEnv 创建时能否正确初始化所有核心 Bundle 分组（包括 lsq、issue、commit、mem_to_ooo、frontend、dcache、outer、csr 共 8 组），确保每组 Bundle 的 DUT 信号映射完整且默认值正确。时钟和复位推进逻辑通过 idle_inputs 恢复默认值来验证，确保每次 step 后所有驱动信号回到已知状态。验证还覆盖 DFT (Design For Test) SRAM 和 reset passthrough 信号在 env 中的透传行为、after_step_callback 的注册和执行、assert_no_outstanding 在 step 后对向量预期的检查、以及 CSR mock 默认返回 M-mode 的基础配置。Mock 策略上，CSR 信号不依赖真实 CSR 文件，而是通过 Python 端的固定值占位模拟。共 29 个测试用例覆盖 env 从 bootstrap 创建、运行 step、到 cleanup 销毁的完整路径，验证 env 作为测试框架骨架的稳定性和完备性。

| 项目 | 描述 |
|------|------|
| 输入 | pytest fixture 创建 MemBlockEnv + DUT 实例。 |
| 处理 | 验证 env 创建成功；核心 Bundle 分组齐全 (lsq/issue/commit/mem_to_ooo/frontend/dcache/outer/csr)；clock/reset 推进正确；idle_inputs 恢复默认值；DFT SRAM/reset passthrough 透传；after_step_callback 执行；assert_no_outstanding 检查 vector expectations；CSR mock 默认 M-mode。 |
| 输出 | 29 个测试覆盖 env 生命周期的完整 bootstrap→运行→cleanup 路径。 |
| 验证 | 观测信号/事件，与参考模型比对 |

---

### UT-ENV-002: backend facade (send / execute / prepare / credit)

本验证点属于验证环境的 facade 接口层，不依赖真实 DUT 功能，仅验证 backend 对各类事务类型的处理框架。Backend facade 是测试序列和 DUT 之间的中间层，负责将高级事务（LoadTxn、StoreTxn、AtomicTxn、CBO txn、VectorMemTxn）转换为 DUT 驱动的信号时序。验证覆盖 send 方法正确委托给 backend、execute 方法路由到对应的 Plan 子类（EnqueueLoadCyclePlan、IssueCyclePlan、NonMemBlockerStep、StoreCommitReadyStep）、以及 prepare 方法在 send 前进行事务参数绑定。Elastic 模式下的每 lane backpressure 测试验证了在多 lane 发射时各 lane 独立反压而互不阻塞的能力。Credit 管理测试验证 STA feedback/replay 场景下 credit 正确释放和重用的逻辑。CBO 操作码映射测试确保 flush、clean、zero 等 CBO 类型的编码路径正确。Vector 事务的 execute 使用共享的 plan runtime，与其他事务类型的资源隔离和共享也得到验证。Mock 策略上，backend facade 的 DUT 信号通过 Python 端的 fake bundle 驱动，不依赖真实硬件仿真。共 40 个测试覆盖 backend facade 的完整接口面。

| 项目 | 描述 |
|------|------|
| 输入 | 构造各类事务 (LoadTxn, StoreTxn, AtomicTxn, CBO txn, VectorMemTxn) 并调用 backend.send/execute/prepare。 |
| 处理 | send 委托给 backend；execute 路由 EnqueueLoadCyclePlan / IssueCyclePlan / NonMemBlockerStep / StoreCommitReadyStep；prepare 在 send 前绑定 txn；elastic mode 每 lane backpressure；STA feedback/replay + credit 释放；CBO 操作码正确映射；vector execute 使用共享 plan runtime；事务验证 (mask/size 一致性、runtime binding 检查)。 |
| 输出 | 40 个测试覆盖 backend facade 的完整接口面。 |
| 验证 | 观测信号/事件，与参考模型比对 |

---

### UT-ENV-003: MemoryModel store logic (unit test)

本验证点属于验证环境的参考模型层，不依赖真实 DUT 功能，仅验证 MemoryModel 及附属组件的 store 行为建模正确性。验证使用纯 Python fake 信号模拟 store 相关的各类事件，包括：StoreMonitor 的事件驱动、sbuffer 的 drain 日志、CBO wline 全行清零 drain、outer 总线 drain、MMIO 空间 drain。具体验证细节包括：Scoreboard 组件需正确处理跨 16B 边界的 store split drain、浮点 load 的 boxing 操作、以及窄 load 的符号扩展逻辑。RefMemory 是 MemoryModel 的底层存储模型，验证其 mask 写入和读取的正确性、clone 隔离（确保不同克隆实例互不干扰）、apply_store 方法中掩码宽度的解码映射、apply_cbo_zero 方法的全行清零语义、以及 with_store 预测写入行为。StoreMonitor 层面验证 addr_re（地址重新使能）的消费逻辑、sqidx（store queue index）的正确区分和 rearm 机制。MemoryModel 整体层面验证 commit-boundary compare 的延迟匹配、flush drain 与 golden memory 的一致性比对。Mock 策略是所有 store 事件通过 Python 方法调用模拟，不涉及实际 DUT 信号。共 23 个测试覆盖 MemoryModel 的全部 store 逻辑路径。

| 项目 | 描述 |
|------|------|
| 输入 | 纯 Python fake 信号模拟 store 行为 (StoreMonitor 事件、sbuffer drain 日志、CBO wline drain、outer drain、MMIO drain)。 |
| 处理 | Scoreboard: 跨 16B store split drain、fp load boxing、窄 load 符号扩展。RefMemory: mask 写/读、clone 隔离、apply_store 掩码宽度解码、apply_cbo_zero 清零、with_store 预测。StoreMonitor: addr_re 消费、sqidx 区分、rearm 机制。MemoryModel: commit-boundary compare 延迟、flush drain 与 golden memory 比对、CBO zero wline drain、非零 CBO 无 drain compare、MMIO exclusion。 |
| 输出 | 23 个测试覆盖 MemoryModel/Scoreboard/RefMemory/StoreMonitor 全部逻辑。不依赖真实 DUT。 |
| 验证 | 观测信号/事件，与参考模型比对 |

---

### UT-ENV-004: VectorMemoryModel element expansion

本验证点属于验证环境的向量内存参考模型层，不依赖真实 DUT 功能，仅验证 VectorMemoryModel 的 element expansion 逻辑。向量 load/store 的地址计算不同于标量——一条向量内存指令携带 vl（向量长度）、vstart（起始位置）、mask（掩码）、sew（元素宽度）和 stride（步长）等参数，需要通过 expand() 方法展开为独立的 element 序列。验证覆盖 expand 方法按 vl 将事务展开为 element 序列的正确性，每个 element 根据其在向量中的位置被分类为 active、prestart、tail 或 masked 四种状态之一。Stride 展开使用带符号 stride 值，验证正步长和负步长（反向访问）两种模式下的地址序列正确性。Predict_store 方法验证更新 refmem 和 outstanding 信息的正确性，确保预测写入不影响实际的 golden memory 状态。Mask 源派生验证区分 mask 来自 CSR 隐式传递和来自显式 src3 寄存器覆盖两种路径。VUop 索引验证 vuop_idx 默认值为 0 且拒绝越界索引输入。Mock 策略上，VectorMemTxn 对象直接通过 Python 构造，不涉及 DUT 信号。共 7 个测试覆盖 VectorMemoryModel 的 element expansion 全部路径。

| 项目 | 描述 |
|------|------|
| 输入 | VectorMemTxn 携带 vl/vstart/mask/sew/stride 参数，调用 expand() 进行 element 展开。 |
| 处理 | expand 按 vl 展开为 element 序列，分类为 active/prestart/tail/masked；stride 展开使用带符号 stride；predict_store 更新 refmem 和 outstanding；vuop_idx 默认 0 且拒绝越界；mask 源派生与显式 src3 覆盖。 |
| 输出 | 7 个测试覆盖 VectorMemoryModel 的 element expansion 全部路径。 |
| 验证 | 观测信号/事件，与参考模型比对 |

---

### UT-ENV-005: ROB agent (blocker / readiness / commit)

本验证点属于验证环境的 agent 驱动层，不依赖真实 DUT 功能，仅验证 ROB (Reorder Buffer) agent 对提交 (commit) 信号的驱动逻辑。ROB agent 负责驱动 commit packet 中的关键信号，包括 lcommit（load 提交）、scommit（store 提交）、commit（通用提交计数）和 pendingPtr（待处理指针）。验证覆盖的基本场景包括：单条 load 提交时 lcommit 信号正确置位；store 提交需要同时满足 token 和 ready 条件才能发出 scommit；混合 commit packet 在同拍同时提交 load 和 store 的信号组合。阻塞和 readiness 机制是验证的重点：head store unready 会阻塞更年轻的指令提交、non-mem blocker 通过阻塞/release frontier 控制提交流、显式 ready override 可以绕过 readiness 检查、ready store 在无 token 时仍需保持阻塞。Completion before issue 缓冲验证了当完成信号早于 issue 信号到达时的数据保持能力。PendingPtr wrap 种子边界测试了当 pendingPtr 在 wrap 边界附近的种子初始化行为。Mock 策略上，ROB agent 不通过真实 DUT 的 commit 信号驱动，而是由测试代码直接调用 agent 接口注入 commit 事件。共 10 个测试覆盖 ROB agent 的完整状态机行为。

| 项目 | 描述 |
|------|------|
| 输入 | 通过 ROB agent 驱动 commit packet (lcommit/scommit/commit/pendingPtr)。 |
| 处理 | single load → lcommit；store 需 token+ready → scommit；混合 commit packet (load+store 同拍)；head store unready 阻塞 younger；non-mem blocker 阻塞/release frontier；显式 ready override；ready store 无 token 仍阻塞；completion before issue 缓冲；pendingPtr wrap 边界 seed；拒绝有 outstanding 时的 seed。 |
| 输出 | 10 个测试覆盖 ROB agent 的 commit/pendingPtr/blocker/readiness 全部状态机。 |
| 验证 | 观测信号/事件，与参考模型比对 |

---

### UT-ENV-006: ROB function coverage collector

本验证点属于验证环境的覆盖率收集层，不依赖真实 DUT 功能，仅验证 ROB function coverage collector 的挂载和采样逻辑。Coverage collector 是连接 env fixture 和覆盖率数据库的桥梁，负责在每个仿真 step 后自动采样关键信号的覆盖点。ROB coverage collector 由 4 组覆盖点构成：ObservedBehavior（观测到的实际行为）、CurrentModel（当前模型状态）、GapObserved（观察到的模型差距）和 KnownModelGaps（已知模型缺陷）。验证通过 env fixture 自动挂载 collector，验证其在 after_step_callback 中被正确注册和调用。Collector 从 ROB agent 统计采样 blocker 和 readiness 的覆盖率点，确保每种 roblock 和 store 阻塞状态都有对应的 coverpoint。MMIO outer drain 可见时 collector 仍能正确标记 exclusion 命中（表示该覆盖点被合法排除而非漏采）。验证对比采样点的结构化输出与预期格式的匹配度，确保覆盖率数据可被下游工具正确解析。Mock 策略上，采集的覆盖点来自 Python 端的 agent 统计而非真实 DUT 信号。共 4 个测试验证 collector 的挂载、采样、输出和 exclusion 标记四个维度。

| 项目 | 描述 |
|------|------|
| 输入 | env fixture 自动挂载 ROB coverage collector (4 组: ObservedBehavior/CurrentModel/GapObserved/KnownModelGaps)。 |
| 处理 | collector 跟随 DUT after_step_callback 自动采样；从 ROB agent 统计采样 blocker/readiness 点；MMIO outer drain 可见时仍标记 exclusion 命中。 |
| 输出 | 4 个测试验证 collector 挂载、采样、结构化输出、exclusion 标记。 |
| 验证 | 观测信号/事件，与参考模型比对 |

---

### UT-ENV-007: IssueAgent (lane shapes / fuType binding)

本验证点属于验证环境的 agent 驱动层，不依赖真实 DUT 功能，仅验证 IssueAgent 对发射 (issue) 信号的驱动逻辑。IssueAgent 负责驱动 intIssue bundle，将 IssueOp（包含 load、sta、std 三种 kind）转换为对应 lane 的发射信号。验证覆盖 strict mode 下需等待全部 lane 就绪后才能发起握手的全局同步行为，以及 elastic mode 下每 lane 独立 backpressure、保持 valid 信号、握手后退役不退试的低功耗模式。Load 类型驱动的验证包括 fpWen（浮点写使能）的正确置位和 size-specific fuOp（根据访问宽度选择功能单元操作码）的编码正确性。STA/STD 类型驱动的验证确认二者分别驱动对应的 lane shape 和时序约束。Atomic 操作验证 fuType 和 pdest（目标物理寄存器）的正确编码。拒绝测试验证了将 kind 与 lane 不匹配的 IssueOp 输入时 agent 的正确错误响应。Mock 策略上，IssueAgent 不通过真实 DUT 的 issue 信号驱动，而是由测试代码构造 IssueOp 对象直接调用 agent 接口。共 9 个测试覆盖 IssueAgent 的 lane shapes、握手模式、fuType 绑定全部逻辑。

| 项目 | 描述 |
|------|------|
| 输入 | 构造 IssueOp (load/sta/std 三种 kind)，调用 IssueAgent 驱动 intIssue bundle。 |
| 处理 | strict mode 等待全部 lane ready；elastic mode 每 lane backpressure、保持 valid、握手后退役不退试；load 驱动 fpWen + size-specific fuOp；sta/std 驱动对应 lane shape；atomic 驱动 fuType + pdest；拒绝 kind-lane 不匹配。 |
| 输出 | 9 个测试覆盖 IssueAgent 的 lane shapes、握手模式、fuType 绑定全部逻辑。 |
| 验证 | 观测信号/事件，与参考模型比对 |
