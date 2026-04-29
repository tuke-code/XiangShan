# MemBlock DUT CHANGELOG

日期：2026-04-28

目标 merge：`e30cc6fe6e26b43aa9a949a78a0af6ee56020a08`

对比基线：`34964c573`

分析范围：以 `src/main/scala/xiangshan/mem/` 为主，补充 `src/main/scala/xiangshan/backend/` 中直接改变 MemBlock 对外 contract 的配套更新。

本文档从 MemBlock Python 验证环境维护视角，总结上述 merge 相对其一号父提交引入的 DUT 更新。重点不在于罗列全仓库 diff，而在于回答三个问题：

1. MemBlock 顶层 I/O contract 哪些变了。
2. MemBlock 内部 store/load/LSQ 数据通路哪些重构会击穿既有验证假设。
3. 当前 Python 验证环境已经适配了什么，还需要继续补强什么。

## 1. 整体结论

这次 merge 对 MemBlock 不是“小修小补”，而是一次明显的 store-path、writeback-contract 和 backend binding 联动更新。验证侧最关键的结论有五条：

1. `io.mem_to_ooo.intWriteback` 从 `DecoupledIO[NewExuOutput]` 改成了新的 `MemWriteBack` contract。
2. 标量 store 执行单元从旧 `StoreUnit` 切到 `NewStoreUnit`，store issue 到 LSQ 的连接语义随之调整。
3. 顶层不再实例化 `StoreMisalignBuffer`，misalign/unalign 路径更多下沉到 `NewStoreUnit` 与 `NewStoreQueue`。
4. LSQ store 侧原先的一批辅助导出被删除或降级，验证模型更需要依赖 `sqDeqPtr`、store writeback 和 sbuffer 事件来重建生命周期。
5. backend 侧为了接住新的 mem writeback contract 做了同步改造，因此这次变化不是单个模块内部重构，而是横跨 `mem/` 与 `backend/` 的接口升级。

对 Python 环境来说，真正需要立即跟进的不是“所有内部命名变化”，而是：顶层 writeback contract、store 地址与 mask 的权威来源、store commit/completion 的事实源，以及后续对 `isHyper` 这类新语义位的接入策略。

## 2. MemBlock I/O 变化

这一章只谈 MemBlock 对验证环境和外围模块可见的 I/O contract 变化，不混入内部实现细节。

### 2.1 `io.mem_to_ooo.intWriteback`：`NewExuOutput` 改为 `MemWriteBack`

这是本次 merge 对验证环境影响最大的顶层 I/O 变化。

旧合同是：

- `MixedVec[MixedVec[DecoupledIO[NewExuOutput]]]`

新合同变成：

- `MixedVec[MixedVec[MemWriteBack]]`

这意味着 wave / toffee 绑定层看到的字段不再是单层平铺的：

- `valid`
- `bits_data_0`
- `bits_intWen`
- `bits_robIdx_*`

而是分裂成三块语义：

- `toRob`
- `toIntRf`
- `toFpRf`

对应到当前顶层信号名，重点变成：

- `*_toRob_valid`
- `*_toRob_bits_robIdx_*`
- `*_toRob_bits_lqIdx_*`
- `*_toRob_bits_sqIdx_*`
- `*_toIntRf_valid`
- `*_toIntRf_bits`
- `*_toFpRf_valid`
- `*_toFpRf_bits`

验证含义非常直接：

1. load 是否真正产生写回，不能再只看顶层 `valid`。
2. store/mmio/cbo 这类只写 ROB 的返回，也不能再假设一定携带 `data_0/intWen`。
3. 若工具仍只绑定旧平铺字段，会把“DUT 已有 writeback”误判成“没有 writeback”。

### 2.2 `io.mem_to_ooo.sqDeqPtr` 仍保留，但其重要性上升

本次 merge 后，`sqDeqPtr` 仍然从 MemBlock 顶层导出；但与旧环境常见假设不同，它已经不再只是“可有可无的调试口”，而是 store completion 的关键事实源之一。

与之相对，旧版验证环境更容易依赖的一些辅助导出没有继续作为顶层稳定 contract 保留：

- `sqDeqRobIdx`
- `sqDeqUopIdx`
- 以及此前在 LSQWrapper 中转出的 `sqCommitPtr/sqCommitRobIdx/sqCommitUopIdx`

这意味着顶层 contract 更偏“给出队列推进指针”，而不是“替验证模型把 store 生命周期解释好”。后续环境若要继续稳定，应该把 store 生命周期重建建立在：

- `sqDeqPtr`
- store writeback
- sbuffer write
- shadow / addr / data 观测

之上，而不是继续期待顶层提供完整的 per-store 解释信号。

### 2.3 `io.mem_to_ooo.lsqio.mmioBusy` 仍是关键状态口

这次 merge 没有像更早版本那样再改一次 `mmioBusy` 的命名，但它的验证价值进一步提高了。原因是 store/mmio/uncache 路径这轮重构后，很多旧的 per-lane 或私有内部状态更不稳定，`mmioBusy` 这种顶层聚合状态口反而更适合作为 directed case 的稳定事实源。

因此文档上应把 `mmioBusy` 明确归类为：

- 命名保持稳定；
- 但在新 store-path 架构下重要性上升的顶层验证状态口。

### 2.4 `outer_cpu_halt` 改为 `outer_cpu_wfi`

`MemBlock.scala` 的外层接口把：

- `outer_cpu_halt`

改成了：

- `outer_cpu_wfi`

backend 侧也同步把：

- `cpuHalted`

改成：

- `cpuWfi`

这不是 MemBlock Python env 当前主消费接口，但它属于实打实的外部 contract 漂移。若后续有顶层集成脚本、波形模板或旁路探针仍依赖旧 `cpu_halt` 命名，需要同步迁移。

### 2.5 本章小结

对验证环境必须立即对齐的硬接口变化主要有：

1. `intWriteback -> MemWriteBack`
2. `sqDeqPtr` 成为更核心的 store completion 事实源
3. `sqCommit* / sqDeqRobIdx / sqDeqUopIdx` 不再适合作为顶层依赖

而 `outer_cpu_wfi` 这类变化更偏外围集成 contract 漂移，当前 MemBlock Python env 可以只在文档中登记，不必因此修改 testcase 主流程。

## 3. 内部实现与数据通路变化

这一章描述会影响验证假设的内部重构。

### 3.1 `NewStoreUnit` 替代旧 `StoreUnit`

`MemBlock.scala` 中：

- 旧：`Module(new StoreUnit(...))`
- 新：`Module(new NewStoreUnit(...))`

这不是简单重命名。顶层接线语义也一起变了：

- `io.toLsq` -> `io.toSqAddr`
- `io.toLsqRe` -> `io.toSqAddrRe`
- `io.toStoreUnalignQueue` -> `io.toUnalignQueue`
- `feedback_slow` -> `feedBackSlow`

对验证环境的影响是：任何依赖旧 `StoreUnit` 层级名或旧 lane 接线名的白盒脚本，都会在这次 merge 后漂移。

### 3.2 顶层不再实例化 `StoreMisalignBuffer`

本次 merge 删除了 `MemBlock.scala` 中顶层 `StoreMisalignBuffer` 的实例和相关接线，包括：

- 对 ROB redirect/commit/pendingPtr 的绑定
- 连接到 `lsq.io.maControl`
- store writeback 仲裁中的 misalign buffer writeback 分支

这说明当前 DUT 不再把“顶层可见的 store misalign buffer”当作主架构事实。验证上的直接含义是：

1. misalign/unalign 行为不能再以“顶层独立 misalign buffer 一定可见”为前提。
2. 后续应把关注点转到 `unalignQueueReq`、`storeAddrInRe`、`NewStoreQueue` 和最终 writeback/sbuffer 结果。

### 3.3 `StaIO` / `StoreAddrIO` contract 内移

`LSQBundle.scala` 中几项变化值得单独点名：

1. `StaIO.storeMaskIn` 被删除。
2. `StoreAddrIO` 删除 `af`，新增 `isHyper`。
3. `StoreQueueIO.writeBack` 从 `DecoupledIO[NewExuOutput]` 改成 `DecoupledIO[MemToRob]`。

这些变化意味着：

- store mask 的权威事实源不再是单独一条 `storeMaskIn` 通道；
- hypervisor 语义现在已经进入 store 地址/异常传播链路；
- mmio/cbo/store-only writeback 更明确地走 `toRob` 风格 contract。

### 3.4 `LSQWrapper` 不再导出旧 commit 辅助口

`LSQWrapper.scala` 删除了：

- `sqCommitPtr`
- `sqCommitUopIdx`
- `sqCommitRobIdx`

同时继续保留：

- `sqDeqPtr`

这进一步说明当前 DUT 设计更倾向于：

- 对外给出真实的队列推进与 writeback 事件；
- 而不是对验证环境导出一套专门的 store commit 解释口。

### 3.5 `NewStoreQueue` 的 store/load overlap 与 uncache 语义变化

`NewStoreQueue.scala` 本次 diff 较大，但对验证最相关的变化集中在三类：

1. store-load overlap / cross-16B 判定逻辑重写；
2. `fromUnalignQueue` 从 `ValidIO` 改成 `DecoupledIO`；
3. `memBackTypeMM` 的设置从“NC 或 PBMT IO”改成更明确的 `MemoryType.isMemoryRegion()` 分类。

这三点意味着：

- 旧 testcase 若用非常细的白盒观察去解释 replay / overlap，可能会与新实现脱节；
- unalign 路径更像正式握手通道，而不是“打一拍就过”的旁路；
- `memBackTypeMM` 已不宜再被简单解读成“不是 cacheable 就是 main-memory back type”。

### 3.6 backend 侧为新 writeback contract 做了同步改造

`backend/Bundles.scala` 新增：

- `MemDebugBundle`
- `MemToRob`
- `MemToIntRf`
- `MemToFpRf`
- `MemWriteBack`

`backend/Backend.scala` 再把 `MemWriteBack` 转回 backend 内部继续使用的 `NewExuOutput` 语义。

这说明新 contract 不是“只在 MemBlock 里临时拼一下”，而是 MemBlock 与 backend 间的正式边界升级。对验证环境来说，这也解释了为什么顶层波形名和写回观测语义会整体变一层，而不只是某几个字段改名。

## 4. 对验证环境的影响与需要引入的修改

### 4.1 当前环境已体现的适配

当前 `src/test/python/MemBlock/` 里已经可以确认有几项适配完成：

1. `MemBlock_env.py` 的 `IntWritebackBundle` 已支持新版 writeback 命名。
   - `bits_toRob_valid`
   - `bits_toRob_bits_robIdx_*`
   - `bits_toIntRf_bits`
   - `bits_toFpRf_bits`

2. `monitors/writeback_monitor.py` 已按新版语义区分 load/store writeback。
   - 对 store-only writeback，会优先走 `toRob_valid` / `isFromLoadUnit`
   - 对 load writeback，会兼容 `toIntRf` / `toFpRf`

3. `monitors/store_monitor.py` 已把 `sqDeqPtr` 作为 store completion 的主要事实源之一。

4. `lsq_webui_backend.py` 已消费：
   - `sqDeqPtr`
   - `uopIdx`
   - 新版 writeback 和 queue 内部状态

5. `MemBlock_env.py` / `rob_coverage.py` 已继续使用 `lsq_status.mmioBusy`，因此在顶层聚合状态口这一点上，环境没有被这次 merge 击穿。

换句话说，这轮 merge 并不是“当前 Python env 全面失效”；更准确的说法是：环境已经对最致命的顶层 writeback contract 做了兼容，但还有若干历史兼容层需要继续清理和升级。

### 4.2 仍需补强：弱化对 `sqCommitPtr` 的依赖

`store_monitor.py` 当前仍有：

- `_observe_sq_commit_ptr()`

并直接读取：

- `MemBlock_inner_lsq_io_sqCommitPtr`

但这次 merge 之后，LSQWrapper 已不再把 `sqCommitPtr/sqCommitRobIdx/sqCommitUopIdx` 作为稳定对外导出 contract。即便内部层级短期还在，长期也不应该再把它当作验证主事实源。

建议后续把 store commit 语义进一步收敛到：

1. SQ shadow `committed` 位变化；
2. store writeback / ROB 推进事件；
3. `sqDeqPtr` 与 sbuffer write 的联合解释。

### 4.3 仍需补强：收缩 `storeMaskIn` 兼容层

当前环境里仍保留：

- `StoreMaskInputBundle`
- `store_mask_inputs`
- `StoreMonitor._observe_store_mask()`

而 DUT 侧这次 merge 已删除 `StaIO.storeMaskIn`。这说明该路径已经从“正式 contract”退化成“历史兼容壳”。

建议后续把 store mask 的权威来源切到：

1. `storeAddrIn.bits_mask`
2. sbuffer write `mask`

并把 `storeMaskInputBundle` 降级为 fallback，避免后续 DUT 再清一轮旧口时，环境继续被动修补。

### 4.4 仍需补强：不要再假设 `sqDeqRobIdx/sqDeqUopIdx` 可见

这次 merge 之后，旧版容易使用的：

- `sqDeqRobIdx`
- `sqDeqUopIdx`

已经不应再被视为可靠 contract。

因此后续无论是 scoreboard、coverage，还是 WebUI / trace 工具，都应按“指针 + shadow + writeback”联合重建 store 生命周期，而不是等待 DUT 帮验证环境直接导出 `robIdx/uopIdx` 对应关系。

### 4.5 仍需补强：misalign/unalign 建模应转向 `NewStoreQueue`

由于顶层 `StoreMisalignBuffer` 已删除，misalign 专题后续不宜继续以“顶层 misalign buffer 可见”为前提。

更稳妥的建模方向是围绕：

- `unalignQueueReq`
- `storeAddrInRe`
- `NewStoreQueue`
- 最终 `toRob` / sbuffer / 异常结果

来验证行为。这样即使内部实现继续演进，也更接近当前 DUT 的正式语义边界。

### 4.6 仍需补强：接入 `StoreAddrIO.isHyper`

`StoreAddrIO` 与 `ExceptionInfoGen` 现在都已经显式传播 `isHyper`。但当前 Python env 仍主要围绕：

- `nc`
- `mmio`
- `memBackTypeMM`
- `hasException`

来解释 store 地址重写和异常属性。

如果后续要补 H extension / hypervisor store 场景，环境应该把 `isHyper` 纳入观测面。否则会出现“DUT 已经区分 hyper/non-hyper，但验证环境仍把两者混成一类”的问题。

### 4.7 仍需补强：WebUI / 文档口径同步去旧

`lsq_webui_backend.py` 目前仍会读：

- `MemBlock_inner_lsq_io_sqCommitPtr`
- `MemBlock_inner_lsq_io_sqCommitRobIdx`
- `MemBlock_inner_lsq_io_sqCommitUopIdx`

这在当前 build 下也许还能工作，但从 contract 演进方向看，这些字段已经不适合作为长期稳定依赖。

建议后续把 WebUI 的 store 生命周期展示逐步迁到：

- `sqDeqPtr`
- shadow `allocated/committed/completed`
- writeback lane
- sbuffer event

同时，`docs/dut_port_behavior.md` 中对 `StoreMaskInputBundle` 的描述也应在后续同步修正，避免文档继续把历史兼容口当作主接口。

## 5. 推荐的后续维护策略

基于这次 merge 的特征，后续每次 MemBlock 大版本更新后，建议优先检查以下几类文件：

1. `src/main/scala/xiangshan/mem/MemBlock.scala`
2. `src/main/scala/xiangshan/mem/lsqueue/LSQBundle.scala`
3. `src/main/scala/xiangshan/mem/lsqueue/LSQWrapper.scala`
4. `src/main/scala/xiangshan/mem/lsqueue/NewStoreQueue.scala`
5. `src/main/scala/xiangshan/backend/Bundles.scala`
6. `src/main/scala/xiangshan/backend/Backend.scala`

对应到 Python env 的维护原则，应继续收敛到四条：

1. 顶层 writeback 优先绑定正式 contract，不依赖旧平铺字段。
2. store 生命周期优先依据可见事件重建，不依赖私有内部数组命名。
3. store mask / memory-type / exception 属性优先绑定当前正式地址通路，而不是历史旁路。
4. 对 H extension、多微操作和复杂 store 路径，逐步把 `uopIdx` 与 `isHyper` 视为一等语义字段。

## 6. 结论

相对基线 `34964c573`，merge `e30cc6fe6e26b43aa9a949a78a0af6ee56020a08` 给 MemBlock 带来的关键变化，不是“某个端口小改名”，而是：

- 顶层 mem writeback contract 升级；
- store 执行与 LSQ 对接方式更新；
- misalign/unalign 路径下沉；
- 顶层可见的 store 生命周期辅助口收缩。

当前 Python 验证环境已经对最核心的 writeback contract 变化完成第一阶段适配，但还需要继续摆脱：

- 对 `sqCommitPtr` 的强依赖；
- 对 `storeMaskIn` 历史兼容口的依赖；
- 对旧 `sqDeqRobIdx/sqDeqUopIdx` 导出的依赖。

把这些补强完成后，环境才能更稳地跟随后续的 MemBlock store-path 演进，而不是每次 DUT 重构都被旧 probe 假设反复击穿。
