# MemBlock DUT CHANGELOG

日期：2026-03-31

对比基线：`91885d0d6e611a2c5573bef1e98cbda925575cb3`

对比范围：`src/main/scala/xiangshan/mem/`

本文档从验证与接口维护视角，总结当前版本 MemBlock 相对基线版本在 `mem/` 目录下的结构重构、端口语义变化、内部状态导出变化，以及这些变化对 Python 验证环境和 DUT 回归造成的直接影响。此次对比不是简单罗列文件 diff，而是试图回答三个问题：一是 MemBlock 在架构上发生了什么变化；二是这些变化为什么会导致旧验证环境失效；三是哪些变化在后续维护中需要被持续关注。

## 1. 整体变化概览

从 `git diff --stat` 看，本次变更规模非常大，`src/main/scala/xiangshan/mem/` 下共涉及 33 个文件，新增约 6451 行，删除约 5684 行。它不是局部修补，而是一次明显的 LSU/MemBlock 内部重构。最核心的两条主线是：

1. load/store 主流水线和 LSQ 结构被重新模块化，旧模块被整批替换。
2. top-level 和 LSQ 内部 bundle 语义明显变化，导致上层验证绑定与内部 probe 方案同时失效。

从文件层面可以看到几个非常明确的替换关系：

- `pipeline/NewLoadUnit.scala` 新增，而旧 `pipeline/LoadUnit.scala` 虽然还在，但主体职责已经被新模块取代。
- `lsqueue/NewStoreQueue.scala` 新增，而旧 `lsqueue/StoreQueue.scala` 与 `lsqueue/StoreQueueData.scala` 被删除。
- `lsqueue/ExceptionInfoGen.scala`、`lsqueue/LSQBundle.scala`、`lsqueue/LSQCommon.scala` 新增，说明 LSQ 的状态定义、异常生成和共享 bundle 被单独抽离。
- `lsqueue/LoadMisalignBuffer.scala`、`lsqueue/LoadExceptionBuffer.scala` 被删除，表示旧版 load misalign/exception 的实现路径已经不再沿用。

对验证环境来说，这意味着不能再假设“端口名大致不变、内部层级只做小修”。本次更新实际上同时改变了顶层 IO 形状、LSQ 内部事件传播方式和写回包格式。

## 2. 顶层接口与 `mem_to_ooo` / `ooo_to_mem` 语义变化

### 2.1 `lsqio` 状态口由细粒度观测收缩为 busy 状态

基线版本的 `mem_to_ooo.lsqio` 输出中，除了 `vaddr/vstart/vl/gpaddr/isForVSnonLeafPTE` 这些状态外，还导出了：

- `mmio: Vec(LoadPipelineWidth, Bool())`
- `uop: Vec(LoadPipelineWidth, DynInst)`

当前版本中，这两组导出已经被移除，替换为单 bit：

- `mmioBusy: Bool()`

这意味着验证环境无法再像旧版本那样通过 `lsqio.mmio[i]` 或 `lsqio.uop[i]` 直接观察每条 load lane 的细节状态，而只能拿到“当前是否存在 MMIO busy”的聚合信息。这正是 Python 环境里 `LsqStatusBundle.mmio` 绑定失效、必须改成 `mmioBusy` 的根因。

### 2.2 `intWriteback` 从 `ExuOutput` 切换到 `NewExuOutput`

基线版本：

- `intWriteback: MixedVec[MixedVec[DecoupledIO[ExuOutput]]]`

当前版本：

- `intWriteback: MixedVec[MixedVec[DecoupledIO[NewExuOutput]]]`

这不是纯类型别名变化。结合 `MemBlock.scala`、`AtomicsUnit.scala`、`NewLoadUnit.scala`、`StoreUnit.scala` 和新引入的 `LSQBundle.scala` 可以看出，新版写回被拆成了 `toRob`、`toIntRf`、`toFpRf` 等子结构。旧验证环境默认按平铺字段读取：

- `bits_data_0`
- `bits_intWen`
- `bits_robIdx_flag/value`
- `bits_lqIdx_*`
- `bits_sqIdx_*`

而新版在多个 lane 上改成：

- `bits_toRob_valid`
- `bits_toRob_bits_robIdx_*`
- `bits_toIntRf_valid`
- `bits_toIntRf_bits`

这也是为什么旧版 `MemoryModel.check_writebacks()` 在 DUT 实际已经完成 load 返回时仍然看不到 writeback，最终在 `drain_writebacks()` 超时。验证环境后续必须长期维护这层“双格式兼容”，否则每次后端 writeback bundle 重构都会再次击穿测试。

### 2.3 反馈接口也发生了收缩与重命名

旧版 `mem_to_ooo` 有：

- `ldaIqFeedback`
- `staIqFeedback`
- `hyuIqFeedback`
- `vstuIqFeedback`
- `vlduIqFeedback`

当前版本中 `ldaIqFeedback` 已从顶层删除，load 侧更多通过 `ldCancel/wakeup` 与新 load 单元自身路径完成反馈。这说明旧验证环境如果依赖 `ldaIqFeedback` 去理解 load issue 反馈节拍，将不再适用。

## 3. LSQ Wrapper 重构

`LSQWrapper.scala` 是本次最关键的变更之一。基线版本中：

- load 侧还在使用 `stld_nuke_query`、`ldld_nuke_query`
- store 地址输入还是 `LsPipelineBundle`
- sbuffer、forward、uncache、mmioStout 这些口型都基于旧 bundle
- store queue 使用的是旧 `StoreQueue`

当前版本中，LSQWrapper 明显改成了新 LSQ 基础设施：

- load nuke query 被拆成 `LoadRAWNukeQuery` 和 `LoadRARNukeQuery`
- store 地址输入从泛化的 `LsPipelineBundle` 改成专门的 `StoreAddrIO`
- `sta` 侧新增 `unalignQueueReq`
- sbuffer 改成 `SbufferWriteIO`
- forward 改成 `SQForward`
- `mmioStout` 改成 `DecoupledIO[NewExuOutput]`
- `storeQueue` 从旧 `StoreQueue` 切换到 `NewStoreQueue`

对验证环境最致命的影响有两个。

第一，旧 store shadow 依赖的很多内部信号来自老 `StoreQueue` 的实现细节，例如 `storeQueue_allocated`、`storeQueue_committed`、`storeQueue_uop_robIdx` 等；而现在 LSQWrapper 下挂的是 `NewStoreQueue`，旧内部命名整体不可复用。

第二，`StoreAddrIO` 把地址、mask、mmio、nc、异常等属性变成了更明确的类型化字段。旧环境原来还单独监听 `storeMaskIn`，但现在部分 mask 信息已经随 `storeAddrIn.bits_mask` 一起传播，因此原来的观测路径并不再是唯一可信来源。

## 4. Store Queue 重写：`StoreQueue` -> `NewStoreQueue`

这是本次更新中对验证冲击最大的一项。旧 `StoreQueue.scala`、`StoreQueueData.scala` 已被删除，新版引入了 `NewStoreQueue.scala`，同时在 `LSQBundle.scala` 里补出完整的新 store queue IO 定义。

### 4.1 分配入口变化

新版 `StoreQueueEnqIO.FromDispatchReq` 不再只是简单接收 dispatch 的 DynInst，而是明确包含：

- `needAlloc`
- `uop`
- `uop.robIdx`
- `uop.numLsElem`
- `uop.sqIdx`
- `uop.lastUop`
- `uop.fuType`
- `uop.fuOpType`
- `uop.uopIdx`
- `loadWaitBit/loadWaitStrict/ssid/storeSetHit`

这解释了为什么旧 Python 环境里 `enqLsq_req` 上那些 `fuOpType/rfWen/vpu_vstart/vpu_vl/pdest/exceptionVec` 等字段已经不再适合作为顶层稳定绑定，而新的 `uopIdx` 反而变成必须补上的字段。

### 4.2 提交/出队可见性变化

`NewStoreQueue` 明确导出了：

- `sqDeqPtr`
- `sqDeqUopIdx`
- `sqDeqRobIdx`
- `toRob.mmioBusy`

而旧 Python 环境原先更依赖静态 shadow 数组。新版结构更偏“事件/指针驱动”，因此验证模型需要顺着 `sqCommitPtr`、`sqDeqPtr` 和 store writeback 事件去重建 store 生命周期，而不是继续试图从不存在的内部数组上直接读 `allocated/committed/completed` 位。

### 4.3 sbuffer 路径的输出格式变化

新版 `writeToSbuffer` 用的是 `SbufferWriteIO`，而且从真实 DUT 导出结果看，`sbuffer_req` 已经直接携带 16-byte 粒度的 `addr/data/mask`。旧模型按 8-byte 做二次切片和 `addr bit3` 归一化，这在新版上会直接把 mask 算错，导致 flush 之后 `touched_byte_count` 为 0。换言之，DUT 本身并没有漏写，而是验证模型仍在按旧版编码解释新版 sbuffer 请求。

## 5. Load 路径重构：`LoadUnit` 被 `NewLoadUnit` 主导

`MemBlock.scala` 中最醒目的变化之一，是：

- 旧版：`loadUnits = Seq.tabulate(...)(i => Module(new LoadUnit(...)))`
- 当前：`newLoadUnits = Seq.tabulate(...)(i => Module(new NewLoadUnit(...)))`

同时，旧版一大段围绕：

- `misalign_allow_spec`
- `ldld/stld nuke query`
- `tl_d_channel`
- `forward_mshr`
- `LoadMisalignBuffer`

的连接逻辑被替换掉，新版更偏向：

- `loadWakeup`
- `bypass`
- `raw/rar nuke query`
- 新的数据/异常写回路径

从验证视角看，这意味着旧 load 行为仍然遵守“enqueue -> issue -> memory -> writeback”的总流程，但中间的 replay、misalign、uncache、wakeup 和异常信息拼装路径都已经换了一遍。因此老测试里只要对某个内部 `LoadQueueReplay` 字段、或者 `LoadMisalignBuffer` 相关信号有硬编码，很容易在当前版本完全失效。

## 6. 异常与 misalign 处理路径变化

本次新增了 `ExceptionInfoGen.scala`，并删除了旧 `LoadExceptionBuffer.scala` 与 `LoadMisalignBuffer.scala`。这说明异常的汇聚和 oldest 选择逻辑从“分散在旧 buffer 内部”变成“由统一的 exception info 生成器管理”。同时 `StoreUnit.scala` 仍保留 `hd_misalign_st_enable`，但在 `mem/` 目录内已经找不到与旧 load misalign enable 对应的同名 load 控制路径，这与实际顶层导出中 `hd_misalign_ld_enable` 缺失是一致的。

对测试环境而言，这类变化的结果通常不是“功能错了”，而是“原来以为能直接看到的异常上下文现在换地方了”。如果环境继续假设异常位、触发位和 misalign 路径仍从旧 bundle 导出，最终就会在 compare 时得到错误结论。

## 7. 向量与分裂路径也有配套调整

虽然本轮 Python 修复的主战场是标量 load/store，但从代码上看，向量相关路径并不是静止的。`VSplit.scala`、`VMergeBuffer.scala`、`VSegmentUnit.scala`、`VecBundle.scala`、`VfofBuffer.scala` 都有改动，而且多个地方显式强化了 `uopIdx` 语义。这与新增的 `UopIdx` 传播、`ExceptionInfoGen` 中按 `robIdx + uopIdx` 排序 oldest exception 的逻辑是一致的。也就是说，新版 MemBlock 比旧版更强调“同一 ROB 下多微操作”的精确区分。

这对验证的含义是：以后只拿 `robIdx` 作为唯一事务标识会越来越不稳，尤其是 vector、AMO、misalign split 或多 micro-op store/load 的路径。后续环境如果要进一步扩展覆盖，最好把 `uopIdx` 视为一等关键字。

## 8. 对 Python 验证环境的直接影响总结

结合本次实际修复经验，可以把新版本对 Python 环境的影响归纳为四类：

1. 顶层 bundle 改名或字段收缩。
   典型例子是 `lsqio.mmio[] -> mmioBusy`，`intWriteback: ExuOutput -> NewExuOutput`。

2. LSQ/StoreQueue 内部实现整体重写。
   旧 `storeQueue_*` 内部 probe 大面积失效，不能再作为稳定验证接口。

3. store/load 事件更偏事件化和指针化。
   `sqDeqPtr/sqCommitPtr/sqDeqRobIdx/sqDeqUopIdx` 这类信号的重要性上升，适合做 shadow reconstruction。

4. `uopIdx` 成为更核心的区分字段。
   它已经深入 dispatch、store queue、vector split、exception oldest 选择等多条路径。

## 9. 结论

相对于基线 commit `91885d0d6e611a2c5573bef1e98cbda925575cb3`，当前版本的 MemBlock 不是“局部修正版”，而是一次较彻底的 LSU/LSQ 内部架构升级。验证环境之所以会在本轮更新后集中出现编译失败、fixture 失配、load writeback 超时和 store shadow 失效，根本原因不在于 Python 侧脆弱，而在于 DUT 本身已经从旧 `LoadUnit + StoreQueue + 旧 ExuOutput` 组合，迁移到了 `NewLoadUnit + NewStoreQueue + NewExuOutput + 新 LSQBundle/ExceptionInfoGen` 的新体系。

因此，后续维护策略应明确转向：

- 顶层绑定优先依赖稳定输出口，而不是私有内部命名；
- store/load shadow 优先依据 visible event 和队列指针重建；
- writeback compare 必须兼容 `toRob/toIntRf` 结构；
- 对多微操作事务，应显式纳入 `uopIdx` 维度；
- 每次 DUT 大版本更新后，优先先检查 `MemBlock.scala`、`LSQWrapper.scala`、`LSQBundle.scala`、`NewLoadUnit.scala`、`NewStoreQueue.scala` 这几个文件，再决定 Python 环境的修补点。

这份 changelog 的目的，是把“为什么会坏”记录清楚，避免下次看到相同现象时又从测试脚本本身开始误诊。

## 10. 按文件分类展开

前面的章节已经给出了高层结论，但如果后续维护者要继续追问题，只知道“NewLoadUnit/NewStoreQueue 替换了旧模块”还不够，还需要知道哪些文件是接口层，哪些文件是行为层，哪些文件是会直接改变验证绑定点的“危险文件”。因此这里按文件类别再展开一层。

### 10.1 `Bundles.scala`

`Bundles.scala` 的变化虽然行数不如 `NewLoadUnit`、`NewStoreQueue` 夸张，但它的影响面很广，因为这里定义的是多个跨模块共享的 bundle 语义。对比基线版本，可以看到几个方向：

- `LsPipelineBundle` 相关的职责进一步外移，一些过去挂在 load/store pipeline bundle 上的字段不再沿用旧形态。
- 新增了 `toExuOutput(param: ExeUnitParams): ExuOutput` 之类的转换逻辑，说明内部结果对象和外部写回对象之间的转换被显式化。
- 新增了多组 store forward / sbuffer forward / uncache bypass request-response bundle，例如 `StoreForwardReqS0`、`StoreForwardReqS1`、`SbufferForwardResp`、`SQForwardRespS1/S2`、`UncacheBypassReqS0/RespS1/RespS2`。
- 新增 `LoadNukeQueryReq/Resp`、`LoadRARNukeQuery`、`LoadRAWNukeQuery`，替代旧的泛化 nuke query 形式。
- `DiffStoreIO` 的 `pmaStore` 载荷也从旧 `DCacheWordReqWithVaddrAndPfFlag` 改成了新的 `DifftestPmaStoreIO`。

这些变化的验证含义是：顶层虽然还保留“load/store/forward/uncache”这些概念，但具体事件的携带信息已经更细化、更类型化。旧环境里凡是偷懒直接把多个阶段都当成同一类 bundle 去处理的代码，未来都容易再次失效。特别是 load/store forward 和 uncache bypass 这种方向，新的 bundle 名称已经非常明确，后续如果要加更深的检查，应该直接按新 bundle 语义建模，而不是复用老的 `LsPipelineBundle` 假设。

### 10.2 `MemBlock.scala`

`MemBlock.scala` 体现的是顶层拼装结果，所以它最适合作为“本次大版本变化的总目录”。这里至少有六类关键变化：

1. `mem_to_ooo.lsqio` 由多 lane 细粒度导出变成了 `mmioBusy` 聚合状态。
2. `intWriteback` 类型从 `ExuOutput` 换成 `NewExuOutput`。
3. load 主体实例从 `LoadUnit` 切到 `NewLoadUnit`。
4. 引入 `ExceptionInfoGen`，说明异常选择和汇聚逻辑不再散落在旧模块。
5. 顶层 `io.mem_to_ooo.lsTopdownInfo` 连接从旧 load unit 的 `lsTopdownInfo` 切到了 `newLoadUnits(i).io.topDownInfo`。
6. 旧围绕 `LoadMisalignBuffer`、`tl_d_channel`、`forward_mshr`、`misalign_allow_spec` 的 wiring 被明显替换。

对 Python 验证环境最直接的教训是：每次 MemBlock 回归失效时，最先看的文件应该就是 `MemBlock.scala`。因为只要这里出现 `Output(Vec(...))` 变成 `Output(Bool())`、`DecoupledIO[ExuOutput]` 变成 `DecoupledIO[NewExuOutput]`、或者某个模块实例名从老模块换成新模块，几乎就已经可以预判到 fixture 绑定、writeback 解析或内部探针会出问题。

### 10.3 `MemCommon.scala`

`MemCommon.scala` 本轮有新增内容，但它不像 `MemBlock.scala` 那样直接改顶层端口，也不像 `LSQBundle.scala` 那样直接改 bundle 形状。它更偏向共享常量、公共类型和通用逻辑的补强。虽然它通常不是第一现场，但这种文件常常决定多个子模块在宽度、字段编码、helper 函数上的一致性。

对验证而言，这类文件的作用主要体现在两个方面：

- 某些字段位宽、枚举、辅助函数可能被统一迁移到这里，导致旧环境里硬编码的位宽和含义逐渐过时。
- 如果后续要继续做更深的自动化 probe 生成或 UI backend 解析，`MemCommon.scala` 往往是恢复“字段权威定义”的好入口。

### 10.4 `lsqueue/LSQBundle.scala` 与 `lsqueue/LSQCommon.scala`

这两个文件是本次 LSQ 重构的重要信号。基线版本里，很多 store/load queue 相关的 IO 定义分散在旧 `StoreQueue.scala`、`LSQWrapper.scala`、或者其他 pipeline 文件中；当前版本则把它们显式抽到：

- `LSQBundle.scala`
- `LSQCommon.scala`

其中 `LSQBundle.scala` 新增内容尤其关键，里面集中定义了：

- `StoreQueueEnqIO`
- `StoreAddrIO`
- `StoreQueueDataWrite`
- `StaIO`
- `SbufferCtrlIO`
- `StoreQueueToLoadQueueIO`
- `SbufferWriteIO`
- `StoreQueueIO`
- `toRobIO`

这意味着 store queue 与 LSQ 的边界接口已经被重新整理过，验证环境不应该再把这些 IO 当成“旧 StoreQueue 私有实现细节”，而应该把它们视为新版 DUT 的半稳定契约。也正因为如此，本次 Python 修复里对 `StoreAddrIO`、`SbufferWriteIO`、`sqDeqPtr`、`mmioBusy` 的观察都更可靠，而旧的 `storeQueue_allocated_*` 探针反而不再可靠。

### 10.5 `lsqueue/LSQWrapper.scala`

如果说 `MemBlock.scala` 是顶层总目录，那么 `LSQWrapper.scala` 就是 load/store 验证最应重点盯防的桥接层。当前版本的 LSQWrapper 相比基线有几个非常清晰的变化：

- `ldu.stld_nuke_query/ldld_nuke_query` 被 `rawNukeQuery/rarNukeQuery` 替代。
- `sta.storeAddrIn/storeAddrInRe` 从 `LsPipelineBundle` 改成 `StoreAddrIO`。
- 新增 `sta.unalignQueueReq`。
- 新增 `bypass`，替换旧部分 uncached/bypass 路径。
- `sbuffer` 改成 `SbufferWriteIO`。
- `forward` 改成 `Vec(LoadPipelineWidth, SQForward)`。
- `mmioStout` 从 `ExuOutput` 改成 `NewExuOutput`。
- storeQueue 实例直接从 `StoreQueue` 改成 `NewStoreQueue`。

此外，还有一个容易被忽略但很重要的细节：当前版本里 `io.sqCommitRobIdx`、`io.sqCommitUopIdx`、`io.sqCommitPtr` 在 LSQWrapper 中实际接的是 `storeQueue.io.sqDeqRobIdx`、`storeQueue.io.sqDeqUopIdx`、`storeQueue.io.sqDeqPtr`。这说明“commit pointer”这个概念在对外导出层面已经不再保持旧版那种独立、直观的语义。对验证模型来说，这正好解释了为什么不能再简单用旧 shadow 中的 `committed` 位做唯一依据，而要结合实际的 pointer/event 一起理解。

### 10.6 `lsqueue/NewStoreQueue.scala`

这是本轮改动里最值得单独展开的文件。它超过 2000 行新增，等价于整个 store queue 设计被重写过。与基线版 `StoreQueue.scala` 相比，可以观察到以下方向：

- store queue 入口直接使用新 `StoreQueueEnqIO`，dispatch 侧信息被细化。
- `uopIdx`、`needAlloc`、`numLsElem` 等字段成为标准输入的一部分。
- `writeBack.toRob`、`io.toRob.mmioBusy`、`io.exceptionInfo` 等事件导出更规范。
- 明确导出 `sqDeqPtr/sqDeqUopIdx/sqDeqRobIdx`。
- sbuffer、uncache、forward、difftest、异常输出都被系统性接入。

从验证角度看，旧 `StoreQueue.scala` 常见的“直接取内部数组位图”方法已经不再可持续。`NewStoreQueue` 更像一个以 event 和 pointer 驱动的状态机系统。也就是说，测试环境应该优先依赖：

- enqueue 分配结果
- storeAddr/storeData/storeMask 事件
- mmio/nc/exception 属性更新
- sqDeqPtr 与 writeback 事件
- sbuffer drain 事件

来重建状态，而不是依赖一个“永远存在的内部 shadow bit array”。

### 10.7 `lsqueue/LoadQueue.scala`、`LoadQueueRAW.scala`、`LoadQueueRAR.scala`、`LoadQueueReplay.scala`、`LoadQueueUncache.scala`

这几份文件没有像 `StoreQueue` 那样被整文件删除，但它们都发生了和新体系兼容的改动。从关键词可以看到：

- `LoadQueueReplay` 更强调和 `sqDeqPtr`、`uopIdx`、vector feedback 的联动。
- `LoadQueueRAW` / `LoadQueueRAR` 配合新的 nuke query 语义变化。
- `LoadQueueUncache` 明确承担 `mmioBusy` 和 exception 输出的一部分职责。
- `LoadQueue` 本体继续承担 virtual load queue、replay、uncache、violation 检查等总控职责，但其外围交互对象已经不是旧版那一套。

验证层面的结论是：虽然这些文件名字没大改，但其外部协作关系已经不同于基线。如果后续某个随机 load 失败，不要因为文件名没变就按旧版理解其输入输出，要先回到当前版本 `LoadQueue` 和 `LSQWrapper` 的 wiring 重新建立认知。

### 10.8 `lsqueue/ExceptionInfoGen.scala`

这个新文件的意义在于把异常 oldest 选择从分散逻辑中抽了出来，而且明确引入 `uopIdx` 参与排序。也就是说，当前 LSU 不再默认“同一个 ROB 里只有单一 mem 异常候选”，而是允许更细粒度地比较：

- `robIdx`
- `uopIdx`

谁更老、谁应该被优先上报。

对测试的启发是两点：

- 如果未来需要做更精确的异常用例，不应只按 `robIdx` 校验。
- 如果某次改动导致异常上下文不匹配，第一怀疑对象应该是“oldest exception 选择规则变化”，而不是立即怀疑 DUT 本身功能错。

### 10.9 `pipeline/NewLoadUnit.scala`

`NewLoadUnit.scala` 是与 `NewStoreQueue.scala` 同等级的重要新增文件。它大约 2100 多行，实质上承接了基线版 `LoadUnit` 的主功能，并重新组织了：

- toRob / toIntRf 写回
- bypass / uncache / wakeup
- exception 输出
- topDown/perf 统计
- replay / cancel / load wakeup 路径

从本次实际回归经验看，最关键的变化不是“load 能不能发出去”，而是“写回结果不再以旧 ExuOutput 方式出现”。这就是为什么旧环境起初并不是 issue 阶段失败，而是在最后 `drain_writebacks()` 一直等不到 compare 收敛。换言之，NewLoadUnit 对验证最直接的影响，是写回结果的组织方式发生了结构性改变。

### 10.10 `pipeline/LoadUnit.scala`

旧 `LoadUnit.scala` 还保留在当前版本里，但 diff 显示其主体被大幅削减。它更像是历史兼容或某些残余逻辑承载点，而不是当前 MemBlock 主 load 路径的唯一权威实现。因此，验证侧如果未来再读这个文件，应避免犯一个常见错误：看到文件还在，就默认当前顶层仍走它的全部旧路径。对当前版本来说，真正优先级更高的应该是 `MemBlock.scala` 里的实例化关系和 `NewLoadUnit.scala` 的实际 wiring。

### 10.11 `pipeline/StoreUnit.scala` 与 `pipeline/StdExeUnit.scala`

这两个文件本轮没有被整体替换，但也都发生了重要变化。

`StoreUnit.scala` 方面：

- 与 `sqCommitPtr/sqCommitRobIdx/sqCommitUopIdx` 的交互更明确。
- `stout.toRob` 组织方式向新写回结构靠拢。
- misalign、cross-page、exception 输出仍然存在，但处于新体系中。

`StdExeUnit.scala` 方面：

- 明确输出 `toRob` 与 `sqData`。
- 标量 store data 路径与 vector store data 路径的仲裁关系更清晰。

对测试而言，这说明 store 并不是只由 `NewStoreQueue` 决定，`StoreUnit` 和 `StdExeUnit` 仍然是地址、数据、写回事件的重要来源。也正因为如此，本次 Python 修复中才需要同时观察：

- `storeAddrIn`
- `storeDataIn`
- store writeback
- sbuffer request

而不能只盯住单一模块。

### 10.12 `pipeline/AtomicsUnit.scala`

AMO 路径虽然不是本次修复主线，但 `AtomicsUnit.scala` 也配套切到了新写回语义：

- `io.out.toRob`
- `io.out.toIntRf`
- `exceptionInfo`

并且内部注释明确强调了 `uopIdx` 在 AMOCAS、LR/SC 等多 uop 场景下的意义。这个信号对于后续扩展 AMO 用例非常重要，因为它再次说明 `uopIdx` 已经不是 vector 专属概念，而是内存子系统里普遍强化的事务区分维度。

### 10.13 `pipeline/Bundles.scala` 与 `pipeline/package.scala`

这两个新增文件说明 pipeline 层自己的 bundle 和辅助逻辑也被拆出来了。虽然本轮 Python 环境没有直接绑定这些文件里的定义，但它们反映出一个趋势：原先很多散落在单模块里的 IO 和 helper 正在被系统性抽象。对验证来说，好处是未来更容易找到“某个字段的权威定义”；风险是如果继续用旧文件名、旧路径去猜字段归属，会越来越不准确。

### 10.14 `sbuffer/Sbuffer.scala`

Sbuffer 本轮也有实质修改，结合顶层 `SbufferWriteIO` 的引入，可以看出其入口协议已经与旧版不同。虽然从功能上它仍然承担“缓存 store drain 请求并向外写出”的职责，但验证环境不应该再假设：

- drain 请求一定是 8-byte 规约格式
- ready 一定从旧内部名导出
- 旧 `bits_vecValid` 等控制位保持完全相同的语义

本次修复里对 sbuffer 的适配本质上就是一次经验教训：即便 `addr/data/mask` 三个字段名字没变，编码粒度也可能已经变了。

### 10.15 vector 相关文件

`VMergeBuffer.scala`、`VSegmentUnit.scala`、`VSplit.scala`、`VecBundle.scala`、`VecCommon.scala`、`VfofBuffer.scala` 均有改动。虽然这些变化并未直接引发本次标量用例失败，但它们强化了两个重要方向：

- `uopIdx`、`vdIdxInField`、`elemIdx` 等细粒度向量索引语义在当前版本中更重要。
- 向量 load/store 与标量 LSU 更深地共享了 replay、writeback 和 exception 语义。

这对后续验证扩展很关键。如果未来要做 vector MemBlock 回归，建议从当前版本新增的 `LSQBundle`、`NewLoadUnit`、`NewStoreQueue`、`VecBundle` 一起建模，而不是沿用旧版对 vector path 的简化理解。

## 11. 建议的后续维护顺序

为了让这份扩展版 changelog 更可执行，最后再给出一份建议的排查顺序。以后只要 MemBlock 再次升级并导致 Python 环境失效，优先按这个顺序看代码：

1. 先看 `src/main/scala/xiangshan/mem/MemBlock.scala`
   目标：判断顶层实例关系和 IO 类型是否变了。

2. 再看 `src/main/scala/xiangshan/mem/lsqueue/LSQWrapper.scala`
   目标：判断 load/store/uncache/sbuffer/forward 的桥接方式是否变了。

3. 然后看 `src/main/scala/xiangshan/mem/lsqueue/LSQBundle.scala`
   目标：确认当前权威 bundle 定义，不要再靠旧 probe 猜字段。

4. load 问题优先看 `src/main/scala/xiangshan/mem/pipeline/NewLoadUnit.scala`
   目标：确认写回与 bypass/wakeup/exception 的新组织方式。

5. store 问题优先看 `src/main/scala/xiangshan/mem/lsqueue/NewStoreQueue.scala`
   目标：确认 enqueue、commit、deq、sbuffer、mmio 和异常的可见事件。

6. 若涉及 oldest exception 或多微操作排序，再看 `src/main/scala/xiangshan/mem/lsqueue/ExceptionInfoGen.scala`
   目标：确认 `robIdx + uopIdx` 的优先级规则。

如果按照这个顺序建立上下文，后续遇到同类问题时，定位速度会比从 Python 测试栈回溯快得多。


# 验证环境更新说明

本文档记录 2026-03-31 这轮 MemBlock Python 验证环境随 DUT 新版本演进所做的适配、修复与回归情况。此次更新的背景是：MemBlock DUT 在接口定义、内部可见调试信号、写回结构以及 store 相关状态导出方式上都发生了明显变化，导致原有 Python 验证环境虽然整体框架仍可复用，但在端口绑定、事件观测和在线 compare 逻辑上出现了大面积失配。最初的直接表现是 `pytest -v src/test/python/MemBlock/tests/ -n 16` 场景下出现大量 fixture/setup 阶段错误，以及后续真实 DUT load/store 用例的运行时失败。

本次更新首先处理的是构建与内部信号导出层。`scripts/generate_memblock_internal_yaml.py` 中旧版本针对 replay 和 storeQueue 的若干内部探针已经无法匹配新 RTL，因此进行了系统清理和替换。具体来说，去掉了已经不存在的 `LoadQueueReplay.uopIdx`、`vecReplay.is128bit` 等旧探针，同时补入当前 DUT 中真实存在的 replay 字段，如 `elemIdx`、`alignedType`、`mbIndex`、`elemIdxInsideVd`、`reg_offset`、`mask` 等；另外 replay `cause` 的位宽也从旧版的 11 bit 修正到新版的 13 bit。更关键的一点是，旧版依赖的 `storeQueue` 自定义内部导出路径在新版本里已经失去稳定性，因此生成逻辑中不再强行构造那套过时的 storeQueue shadow probe，避免构建时因为无效信号名直接失败。

配套地，`Makefile` 中的 `make memblock` 流程也做了修正，以保证 Python 侧总能拿到当前 DUT 对应的正确共享库。更新后会重新创建 `build-memblock/pylib`，使用可写的本地 `ccache` 目录，清理陈旧的 `build-memblock/pylib/MemBlock` 导出结果，并在构建完成后显式检查 `build-memblock/pylib/MemBlock/libUTMemBlock.so` 是否存在，再拷贝到 `build-memblock/pylib/libUTMemBlock.so`。这一步的意义在于把“旧 so 被误复用”的风险降到最低，保证 Python fixture 绑定的确实是这次更新后的 RTL 版本。

在环境绑定层，`src/test/python/MemBlock/MemBlock_env.py` 做了较大范围的接口对齐。首先是 `CsrCtrlBundle` 删除了已经不存在的 `hd_misalign_ld_enable`；`LsqStatusBundle` 把旧的 `mmio[3]` 数组改成了新版顶层实际导出的 `mmioBusy` 单 bit 状态；`IntIssueBundle` 去掉了不再存在的 `bits_fuType`；`LsqEnqReqBundle` 则从旧版复杂的请求描述收缩为新版真正保留的字段集合，即保留 `valid`、`bits_fuType`、`bits_uopIdx`、`bits_robIdx_*`、`bits_lqIdx_*`、`bits_sqIdx_*`、`bits_numLsElem`，删除了 `bits_fuOpType`、`bits_rfWen`、`bits_vpu_vstart`、`bits_vpu_vl`、`bits_lastUop`、`bits_pdest`、`bits_exception_vec` 等已经从 enq 侧移除的字段。这样做之后，基础 fixture 已经可以重新绑定 DUT，不再在对象构造阶段因为属性缺失而失败。

`request_apis.py` 的修改重点在于驱动逻辑与新 DUT 口型保持一致。load/store enqueue 不再写旧的 `fuOpType` 等字段，而是正确写入 `bits_uopIdx`；`issue_scalar_load()` 与 `issue_scalar_sta()` 对新版直接导出、但不同 lane 并不一定同时存在的字段，增加了 `_set_optional_signal()` 方式的可选驱动，避免用 `getattr(...).value = ...` 对缺失信号硬写导致异常。与此同时，issue 侧不再写已经消失的 `bits_fuType`。这部分改动的价值不是简单“绕过报错”，而是让请求构造行为与当前 DUT 的真实输入语义一致，从而保证后续 pipeline 流程和 scoreboarding 的基础前提是正确的。

接下来是本轮修复的核心：`memory_model.py` 对新版 MemBlock 行为的重新适配。最初负载最大的问题出现在 load writeback 观测逻辑上。旧环境默认认为 `intWriteback` 会平铺导出 `data_0/intWen/robIdx` 等字段，但新版 DUT 的多个 lane 已经改为 `toRob` 与 `toIntRf` 两层结构，导致测试环境明明已经收到了真实 DUT 的 load 返回，却因为绑定不到旧字段而误判为“没有 writeback”，最终在 `drain_writebacks()` 中超时。本次修复在 `IntWritebackBundle` 中增加了双格式兼容：对 `robIdx` 同时支持旧的 `bits_robIdx_*` 和新的 `bits_toRob_bits_robIdx_*`，对数据同时支持 `bits_data_0` 和 `bits_toIntRf_bits`，对整型写使能同时支持 `bits_intWen` 和 `bits_toIntRf_valid`，异常位也支持 `bits_exceptionVec_*` 与 `bits_toRob_bits_exceptionVec_*` 两套命名。`MemoryModel.check_writebacks()` 也据此改为能同时理解老格式和平铺格式，从而恢复 load 在线 compare。

store 路径的问题更加复杂。旧版本 MemoryModel 很依赖 `MemBlock_inner_lsq_storeQueue_*` 这一套内部 shadow 信号来判定某个 store 是否已经 allocated、committed、completed，并据此决定什么时候把 older store 写入 golden memory。但在新版 DUT 中，这套 direct probe 已经不再稳定存在。为此，本次更新把 store 跟踪从“依赖旧内部状态快照”改为“根据可见事件重建状态”。具体做法包括：在 `enqueue_scalar_store()` 完成后调用 `env.note_store_allocated()`，显式把 `sq_idx/rob_idx` 登记进 MemoryModel；通过 `sqCommitPtr` 与 `sqDeqPtr` 的推进去推断 committed/completed，而不是等待旧版 shadow 直接给出布尔位；通过 `storeAddrIn` 捕获真实地址和新版已经直接携带的 `mask`；通过 `storeDataIn` 捕获数据；通过 `storeAddrInRe` 的新版 `mmio/nc/memBackTypeMM/hasException` 导出恢复 MMIO 与异常属性，即便新端口不再携带旧的 `updateAddrValid` 和 `sqIdx`，也可以结合 lane 对位关系与已有 `storeAddrIn` 观测补齐状态。

针对 sbuffer，本次也修正了观测前缀和数据解释方式。旧环境使用的是 `MemBlock_inner_lsq_io_sbuffer_*` 风格的前缀，并假设 drain 记录是按 8-byte 粒度编码的；新版 DUT 顶层实际对外稳定暴露的是 `MemBlock_inner_lsq_io_sbuffer_req_*`，而且从实际抓到的信号看，`addr/data/mask` 已经可直接按 16-byte 宽度记录，不再需要旧版那套基于 `addr bit3` 的二次切片与归一化。因此 `MemoryModel._observe_sbuffer_writes()` 已改为直接记录 16-byte 的 `addr/data/mask`，确保最终 `flush_store_buffers_and_wait()` 后的 `drain_log` 与 golden memory 对比覆盖到正确字节范围。

测试侧还同步更新了 `test_MemBlock_env_fixture.py`，把若干最小 smoke check 改到新接口上，例如 enq smoke 改写 `bits_uopIdx`，status smoke 改读 `mmioBusy`。这些更新虽然不复杂，但它们是后续更大规模随机用例能否快速定位问题的基础，因为 fixture smoke 本身就是对环境契约的一层门槛检查。

从验证结果看，本轮修复已经恢复了 MemBlock 真实 DUT 的关键测试路径。`test_MemBlock_env_fixture.py` 全部通过，说明环境对象构造、关键 bundle 绑定、mock 外设驱动都已与新版 DUT 对齐；单条预加载 load 检查已通过，说明新写回结构下的在线 compare 恢复正常；`test_MemBlock_random_store.py` 整组通过，说明 MMIO store、cacheable store、older store 对 younger load 可见性、以及 flush 后 drain 校验等关键 store 行为都重新成立；small mixed load/store random 也通过，说明 load 与 store 混合场景下基于 commit boundary 的 golden memory 更新逻辑已经重新闭环。

需要特别记录的一点是：在当前 Codex 沙箱里运行 pytest 时，必须设置 `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`。原因不是用例本身有问题，而是某些 pytest 插件在沙箱环境下会触发 socket 或 xdist 相关限制，影响结果稳定性。因此本轮的本地验证命令统一使用 `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest ...`。这属于运行环境约束，而不是 MemBlock DUT 或 Python 验证代码本身的功能缺陷。

总体来看，这次关于 MemBlock 的代码更新不是一次局部补丁，而是一轮针对 DUT 新版本接口和可见性变化的系统性适配。其结果是：构建链路恢复可重复性，Python 验证环境重新能够绑定当前 DUT，load writeback compare 恢复，store shadow 从旧内部命名迁移到基于真实可见事件的重建方案，最终使关键真实 DUT 回归重新可跑、可比较、可收敛。后续如果 MemBlock 再次调整内部 debug 导出，优先建议继续沿用“顶层稳定端口 + 事件重建”的方向，而不要重新回退到强依赖私有内部命名的老方案，这样验证环境会更稳，也更容易跨版本维护。
