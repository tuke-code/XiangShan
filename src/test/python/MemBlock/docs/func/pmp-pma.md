# PMP / PMA Functional Points

本文件描述 PMP (Physical Memory Protection) 和 PMA (Physical Memory Attributes) 模块的功能验证点，采用 IPO (Input-Process-Output) 模式组织。

---

### SCN-PMP-001: PMP runtime: 32-entry 重编程 + lock/freeze

PMP (Physical Memory Protection) 是 RISC-V 处理器中用于提供物理内存访问权限控制的硬件机制。本验证点关注 PMP 配置寄存器 pmpcfg 和 pmpaddr 在运行时动态重编程的正确性，覆盖全部 32 个 PMP entry 从 allow 到 off 再到 allow 的完整切换序列。验证的关键观测点包括：每个 entry 独立编程后 load/store 的权限判断是否正确、locked entry 尝试 overwrite 时 CSR 写操作是否被硬件忽略以确保权限不可逃逸。此外，pmpcfg 寄存器按字分组（每 4 个 entry 共享一个 cfg 字），跨 cfg 字边界 entry 的打包读写需要验证打包语义的正确性。DUT 行为路径上，load 经过地址翻译后进入 PMP 检查单元，根据当前 entry 的配置决定是否产生访问异常（load access fault）。边界条件包括 lock bit 置位后任何模式下均不可修改、以及 pmpcfg 跨字边界打包时 cfg 位域映射的原子性。

| 项目 | 描述 |
|------|------|
| 输入 | 对全部 32 个 PMP entry 执行 runtime 重编程：allow → off → allow 切换；lock 后 overwrite 尝试。 |
| 处理 | 每个 entry 独立编程 (pmpcfg + pmpaddr)；allow 时 load 正常写回；off 时 load fault；lock 后 CSR 写被忽略 (权限不变)；覆盖 cfg-word 边界 entry 的跨字打包。 |
| 输出 | 32 entry allow/off/allow 切换全部正确；locked entry 的 overwrite 无效；跨 pmpcfg 字边界打包正确。 |
| 验证 | 观测信号/事件，与参考模型比对 |

---

### SCN-PMP-002: PMP NAPOT/TOR boundary 矩阵 (load + store)

本验证点聚焦于 PMP 地址匹配模式的边界精度验证，覆盖 NAPOT (Naturally Aligned Power-Of-Two) 和 TOR (Top Of Range) 两种编码方式。NAPOT 模式通过 pmpaddr 中连续低位 1 的数量编码区域大小，TOR 模式则通过相邻 entry 的 pmpaddr 值定义上下界范围。验证策略是在每种模式下构造精确的边界内地址和边界外地址，分别发送 load 和 store 事务，检查 PMP 访问违例判决的精确性。关键观测点包括：NAPOT 区域内部地址应触发 deny fault、外部紧邻地址应正常通行；TOR 模式下低于下界和高于上界的地址正常、范围内地址报 fault。由于 load 和 store 在 PMP 检查路径上可能共享或分离的检查逻辑，两种操作类型需要分别覆盖。边界选点策略上，优先选取 2^n - 1、2^n、2^n + 1 等典型边界偏移，以及跨 4K 页边界的地址组合。

| 项目 | 描述 |
|------|------|
| 输入 | 设置 NAPOT deny region 或 TOR deny range，在边界内/外发送 translated load 和 store。 |
| 处理 | NAPOT: 边界内地址 → PMP deny fault；边界外 → 正常。TOR: 下界以下 → 正常；范围内 → fault；上界以上 → 正常。load 和 store 分别验证。 |
| 输出 | load fault/正常边界精确；store fault/正常边界精确；NAPOT 和 TOR 两种模式均覆盖。 |
| 验证 | 观测信号/事件，与参考模型比对 |

---

### SCN-PMP-003: PMA runtime 切换 (cacheable↔mmio)

PMA (Physical Memory Attributes) 定义了物理地址区域的属性特征，包括是否可缓存 (cacheable)、是否支持原子操作等。本验证点验证 PMA 属性在运行时切换后，load/store 事务能否正确选择相应的硬件处理路径。当 PMA 标记为 cacheable 时，load 会走 DCache 缓存路径，利用 tag 查找和数据 SRAM 读取进行快速响应；当 PMA 切换为 mmio (或 uncache) 时，load 需旁路 DCache 直接通过 outer 总线发出 Get 请求。验证的核心覆盖点是 PMA 属性切换瞬间的路径选举正确性，即切换前的缓存路径不会残留影响切换后的非缓存路径。DUT 行为路径上，load 在发出地址后经过 PMA 查询单元，根据属性位选择不同的 Writeback 来源——来自 DCache refill 或来自 outer 响应。边界条件包括 PMA 表中同时存在 cacheable 和 mmio 区域且边界相邻的场景。

| 项目 | 描述 |
|------|------|
| 输入 | 对安全 PMA entry 子集执行 cacheable → mmio → cacheable 属性切换。 |
| 处理 | PMA cacheable 时 load 走 DCache 路径；PMA mmio 时 load 走 outer/uncache 路径；切换后路径正确变更。 |
| 输出 | DCache vs outer 路径按 PMA 属性正确选择；load writeback 数据正确。 |
| 验证 | 观测信号/事件，与参考模型比对 |

---

### SCN-PMP-004: PMA NAPOT/TOR boundary 矩阵

本验证点将 PMA 地址匹配的 NAPOT 和 TOR 两种模式纳入边界测试矩阵，与 PMP 的边界验证互补。PMA 的 region 配置决定了物理地址区域是 cacheable（走 DCache）还是 MMIO（走 outer uncache 路径）。验证策略是配置一个明确的 PMA region（区域内 MMIO，区域外 cacheable），然后在边界内和边界外分别发送 load 和 store，检查路径选择的正确性。关键区别在于 PMA 不影响权限（access fault 与否），仅影响属性——即 MMIO 和 cacheable 路径下的数据写回来源不同但数据本身应一致。验证需覆盖 NAPOT 模式的自然对齐幂次区域边界和 TOR 模式的上下界范围边界。组合维度上，需同时覆盖 load 和 store 两种操作类型，以及 NAPOT 和 TOR 两种配置模式，构成 2x2 的边界验证矩阵。

| 项目 | 描述 |
|------|------|
| 输入 | 设置 PMA NAPOT/TOR region (区域内 MMIO，区域外 cacheable)，在边界内/外发送 load 和 store。 |
| 处理 | 区域内 → MMIO 路径；区域外 → cacheable 路径。NAPOT 和 TOR 两种模式分别验证 load 和 store。 |
| 输出 | MMIO/cacheable 路径选择按 PMA 区域边界正确切换；数据正确。 |
| 验证 | 观测信号/事件，与参考模型比对 |
