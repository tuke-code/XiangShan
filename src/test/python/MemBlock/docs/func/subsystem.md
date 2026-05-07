# Subsystem Functional Points

本文件描述 MemBlock 子系统级功能验证点，采用 IPO (Input-Process-Output) 模式组织。

---

### SCN-SYS-001: DCache CtrlUnit (ECC error injection / recovery)

DCache CtrlUnit 是 DCache 的控制寄存器接口，通过 MMIO 地址空间暴露配置和状态寄存器。本验证点关注 DCacheCtrlConfig 寄存器支持的 ECC 错误注入功能，包括 tag ECC one-shot 注入和 data ECC delayed+persist 注入两种模式。Tag ECC one-shot 模式下，写入配置寄存器后仅在下一次 tag 读取时注入单次 ECC 错误，ese (hardware error) bit 在下一拍自动清除。Data ECC delayed+persist 模式下，ECC 错误持续注入，ese bit 保持置位状态直到软件清除。验证需要通过 MMIO 编程写入 DCacheCtrlConfig 的特定 bit 域，然后发送 load 访问触发 ECC 校验路径，观测硬件错误检测和 ese bit 的行为。此外，bank_mask 寄存器设置为 0 时可以禁用所有 DCache bank，此时 load 应不受影响（因为 load 命中本来就不依赖被禁用的 bank）。本验证点不要求注入真实的物理 ECC 错误，而是通过配置寄存器模拟 ECC 校验失败的硬件信号，验证错误检测和状态上报路径的正确性。

| 项目 | 描述 |
|------|------|
| 输入 | 通过 MMIO 接口编程 DCacheCtrlConfig 寄存器，注入 tag ECC error (one-shot) 或 data ECC error (delayed+persist)；或设置 bank_mask=0 禁用 bank。 |
| 处理 | tag error one-shot: 注入一次 ECC error，触发 ese (hardware error) bit，下一拍自动清除。data error delayed+persist: 持续注入 ECC error，ese bit 保持。bank_mask=0: 所有 bank 禁用。 |
| 输出 | 观测 ese bit 的 one-shot/persist 行为；bank_mask=0 时 load 不受影响 (bank 禁用无 effect)。 |
| 验证 | 观测信号/事件，与参考模型比对 |

---

### SCN-SYS-002: VLQ coverage (8-port enqueue / redirect / wrap)

VLQ (Load Queue 的向量化版本) 是 MemBlock 中管理 in-flight load 指令的核心结构。本验证点覆盖 VLQ 的多个关键行为维度：8 端口并行入队、高提交率批量出队、redirect 冲刷和指针 wrap-around。8-port enqueue 验证在同拍通过全部 8 个 enqueue 端口向 VLQ 写入 entry 时，每个端口的 entryCanEnqSeq 信号正确且 entry 分配无冲突。高提交率场景通过少量标量 load（如 16 条）连续发射后一次性批量 commit（模拟高 commitCount），验证 lqDeq 的峰值吞吐和 dequeue 指针的推进逻辑。Redirect 场景通过注入 pipeline 冲刷信号验证 needCancel 和 redirectCancelCount 的正确计数，确保被冲刷的 load 正确释放 VLQ entry。Wrap-around 是验证的重点和难点——通过 72 条标量 load 连续发射并提交，推动 VLQ 的 enqueue 指针完成一轮完整的跨边界循环 (enqCrossLoop)，覆盖 entry 从 0 到 71 的完整生命周期。本验证点的核心价值在于覆盖 VLQ 指针管理的边界状态，包括 enqueue 指针反超 dequeue 指针时的满队列检测、以及 wrap-around 后 entry 重新分配的正确性。

| 项目 | 描述 |
|------|------|
| 输入 | 8 条标量 load 同拍通过 8 个 enqueue 端口入队 VLQ；或 16 条 load 连续入队后统一 drain (高 commitCount)；或 redirect 冲刷 in-flight load；或 72 条 load 推动 VLQ 指针 wrap-around。 |
| 处理 | 8-port enqueue: 每个端口独立 enqueue，VLQ entry 分配正确。高 commitCount: 批量 commit 后 lqDeq 峰值。redirect: needCancel/redirectCancelCount 正确。wrap-around: enqCrossLoop 跨边界路径覆盖。 |
| 输出 | 8 端口 entryCanEnqSeq 命中；commitCount 与 lqDeq 匹配；redirect 取消计数正确；wrap-around 覆盖 entry 0-71 完整生命周期。 |
| 验证 | 观测信号/事件，与参考模型比对 |

---

### SCN-SYS-003: frontendBridge (icache / instr_uncache / ctrl)

FrontendBridge 是连接处理器前端取指单元和 MemBlock 存储子系统的桥接模块，负责转发 icache 的缓存请求和指令 uncache 请求到 outer 存储层次。本验证点验证 frontendBridge 三种 TL 请求的穿桥正确性：icache 的 AcquireBlock 请求通过桥接器到达 DCache 侧，然后由 DCache 或 outer 响应后经 D 通道返回前端；instr_uncache 请求直接通过 outer 总线发出 Get 事务，响应数据从 outer 返回后经桥接器返回前端；icachectrl 控制面请求（如 fence.i 同步操作）在控制端口暴露时进行往返验证。FrontendBridge 的核心角色是 TL 通道的协议转换和转发，需要正确处理 mem-side 到 frontend-side 的 A 通道请求映射和 D 通道响应返回。验证的关键观测点包括：三类请求均能成功穿桥、TL 通道的地址和数据完整性在穿桥过程中保持不变、以及并发请求在桥接器中的仲裁正确性。边界条件包括 bridge 满负荷时的 backpressure 处理、以及 mem-side 和 frontend-side 时钟域或握手机制差异的处理。

| 项目 | 描述 |
|------|------|
| 输入 | 通过 frontendBridge 发送 icache TL 请求 (AcquireBlock)、instr_uncache TL 请求、或 icachectrl 控制面请求。 |
| 处理 | icache: 桥接前端 TL-A → mem-side DCache → D 响应返回前端。instr_uncache: 桥接前端 uncache TL-A → outer → D 响应返回。icachectrl: 控制面往返 (若端口暴露)。 |
| 输出 | 三类 TL 请求均穿桥成功并返回正确响应；mem-side 到 frontend-side 的 D 通道正确转发。 |
| 验证 | 观测信号/事件，与参考模型比对 |
