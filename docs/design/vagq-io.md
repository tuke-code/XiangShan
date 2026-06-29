# VAGQ IO 接口说明

> 最后更新: 2026-06-29
>
> 对应代码: `src/main/scala/xiangshan/backend/vector/vagq/`

---

## 0. 当前范围

本文只描述当前工作树中已经存在的 VAGQ 核心模块接口：

- `VAGQ`
- `VAGQEntryTable`
- `MaskGen`
- `AddrGen`
- `SplitCtrl`
- `MergeCtrl`

当前工作树中尚未存在 `SegmentCtrl.scala`、`VAGQUpstreamAdapter.scala`、`VAGQMemInterface.scala`、`VAGQMemBlockAdapter.scala`。因此本文不会把这些 adapter 接口描述成已实现状态。

---

## 1. 全局参数和宽度

定义位置: `Vagq.scala`

| 常量 | 当前值 | 含义 |
|---|---:|---|
| `VAGQSize` | 8 | VAGQ entry 数量 |
| `VAGQEntryIdxWidth` | 3 | entry index 宽度，`log2Ceil(VAGQSize)` |
| `FlowBytes` | 16 | 单个 VAGQ flow / uop slice 的 byte 数 |
| `FlowByteWidth` | 4 | 16B flow 内 byte offset 宽度 |
| `UvlByteWidth` | 5 | flow 内有效 byte 数宽度，可表示 0 到 16 |
| `UopIdxWidth` | 3 | 当前指令内 uop slice index 宽度 |
| `FaultVstartWidth` | 7 | 异常元素序号宽度，`UopIdxWidth + FlowByteWidth` |
| `EewWidth` | 2 | EEW 编码宽度，`0/1/2/3` 对应 8/16/32/64 bit |
| `AlignedTypeWidth` | 3 | 请求对齐类型宽度，当前由 `deew` 填入 |
| `NfWidth` | 3 | segment field `nf` 宽度 |
| `ExceptionNumberWidth` | 6 | 异常号宽度 |
| `LsuRespWidth` | 2 | LSU response lane 数量 |
| `MergeRespWidth` | 3 | MergeCtrl 接收的 response lane 数量，等于 2 个 LSU response + 1 个 LSQ-empty response |

当前实现有三个硬约束：

- `VLEN == 128`
- `VDataBytes == 16`
- `VAGQSize == 4 || VAGQSize == 8`

---

## 2. 公共 Bundle

### 2.1 `VAGQMeta`

VAGQ 写回 ROB 或下游异常处理时需要保留的原始指令元信息。

| 字段 | 类型 | 方向/来源 | 含义 |
|---|---|---|---|
| `pc` | `UInt(VAddrBits.W)` | 上游输入 | 指令 PC |
| `isRVC` | `Bool` | 上游输入 | 是否为 RVC 压缩指令 |
| `ftqPtr` | `FtqPtr` | 上游输入 | FTQ 指针 |
| `ftqOffset` | `UInt(FetchBlockInstOffsetWidth.W)` | 上游输入 | fetch block 内指令偏移 |
| `lqIdx` | `LqPtr` | 上游输入 | LoadQueue 预留表项指针 |
| `sqIdx` | `SqPtr` | 上游输入 | StoreQueue 预留表项指针 |
| `trigger` | `TriggerAction()` | 上游输入 | trigger/debug 相关信息 |
| `perfDebugInfo` | `PerfDebugInfo` | 上游输入 | 性能调试信息 |
| `debug_seqNum` | `InstSeqNum()` | 上游输入 | difftest/debug 序号 |

### 2.2 `VAGQAddrSideUop`

地址侧 uop，进入 `VAGQEntryTable.addrUop` / `VAGQ.io.addrUop`。

| 字段 | 类型 | 含义 |
|---|---|---|
| `meta` | `VAGQMeta` | 写回和异常路径需要的原始指令元信息 |
| `entryIdx` | `UInt(vagqEntryIdxWidth.W)` | 目标 VAGQ entry |
| `uopType` | `UInt(3.W)` | VAGQ 操作类型，见 `VAGQUopType` |
| `robIdx` | `RobPtr` | ROB index，用于 redirect 过滤、response 匹配、写回 |
| `pdest` | `UInt(VfPhyRegIdxWidth.W)` | load 目标向量物理寄存器 |
| `baseAddr` | `UInt(XLEN.W)` | base 地址，stride/indexed 地址生成都以它为基址 |
| `op2Data` | `UInt(VLEN.W)` | stride 值或 indexed offset vector 原始数据 |
| `uvlByte` | `UInt(5.W)` | 当前 uop slice 内 `vl` 覆盖到的有效 byte 数 |
| `vstart` | `UInt((CSRConfig.VlWidth-1).W)` | 架构 `vstart`，只在 `useVstart` 为真时参与 mask 生成 |
| `useVstart` | `Bool` | 是否考虑 `vstart` prestart 区域 |
| `vm` | `Bool` | vector mask enable，`1` 表示不受 `v0Mask` 抑制 |
| `v0Mask` | `UInt(vagqFlowBytes.W)` | 当前 16B flow 对应的 v0 mask 位 |
| `deew` | `UInt(EewWidth.W)` | data element width 编码 |
| `ieew` | `UInt(EewWidth.W)` | indexed element width 编码 |
| `vma` | `Bool` | mask agnostic 策略位 |
| `vta` | `Bool` | tail agnostic 策略位 |
| `uopIdx` | `UInt(UopIdxWidth.W)` | 当前 uop slice 在原始 vector 指令内的序号 |
| `nf` | `UInt(NfWidth.W)` | segment field，当前仅透传 |

### 2.3 `VAGQDataSideUop`

数据侧 uop，进入 `VAGQEntryTable.dataUop` / `VAGQ.io.dataUop`。

| 字段 | 类型 | 含义 |
|---|---|---|
| `entryIdx` | `UInt(vagqEntryIdxWidth.W)` | 目标 VAGQ entry，需要与地址侧 uop 一致 |
| `robIdx` | `RobPtr` | ROB index，用于 redirect 过滤和 response 匹配 |
| `psrc2` | `UInt(VfPhyRegIdxWidth.W)` | load merge 读取旧 `vd` 的源寄存器，store 场景也预留给 store data 源 |

### 2.4 `VAGQReqBase`

active LSU 请求和 LSQ-empty 请求的公共字段。

| 字段 | 类型 | 含义 |
|---|---|---|
| `entryIdx` | `UInt(vagqEntryIdxWidth.W)` | 请求所属 VAGQ entry |
| `robIdx` | `RobPtr` | 请求所属 ROB index，response 返回后必须匹配 live entry |
| `isLoad` | `Bool` | 当前请求来自 load 类 VAGQ uop |
| `isStore` | `Bool` | 当前请求来自 store 类 VAGQ uop |
| `byteOffset` | `UInt(vagqFlowByteWidth.W)` | 16B flow 内被选中的第一个 byte offset |
| `elemIdx` | `UInt(vagqFlowByteWidth.W)` | 16B flow 内元素 index，当前由 `byteOffset >> deew` 生成 |
| `mask` | `UInt(vagqFlowBytes.W)` | 当前元素覆盖的 byte mask |
| `alignedType` | `UInt(AlignedTypeWidth.W)` | 对齐/访问宽度类型，当前由 `deew` 填入 |

### 2.5 `VAGQLsuReq`

active byte 对应的真实 LSU/STA 请求，继承 `VAGQReqBase`。

| 额外字段 | 类型 | 含义 |
|---|---|---|
| `vaddr` | `UInt(XLEN.W)` | 生成后的虚拟地址 |
| `data` | `UInt(VLEN.W)` | store data；当前 `SplitCtrl` 中仍接 `0.U`，未真正实现 |
| `pdest` | `UInt(VfPhyRegIdxWidth.W)` | load 目标向量物理寄存器 |
| `nf` | `UInt(NfWidth.W)` | segment field，当前透传 |

### 2.6 `VAGQLsqEmptyReq`

inactive/prestart/tail byte 对应的 LSQ 空项消费请求，只包含 `VAGQReqBase` 字段。

它的作用不是发真实访存，而是让已经预留的 LSQ 位置被消费/确认，避免 active mask 之外的元素阻塞整条 VAGQ uop 完成。

### 2.7 `VAGQResp`

来自 LSU response lane 或 LSQ-empty response 的反馈。

| 字段 | 类型 | 含义 |
|---|---|---|
| `entryIdx` | `UInt(vagqEntryIdxWidth.W)` | response 对应的 VAGQ entry |
| `robIdx` | `RobPtr` | response 对应 ROB index；必须和 entry 内 `robIdx` 匹配才会被接受 |
| `isLoad` | `Bool` | response 来自 load 请求 |
| `isStore` | `Bool` | response 来自 store 请求 |
| `byteOffset` | `UInt(vagqFlowByteWidth.W)` | response 对应的 16B flow 内 byte offset |
| `mask` | `UInt(vagqFlowBytes.W)` | ACK/NACK/exception 覆盖的 byte mask |
| `data` | `UInt(VLEN.W)` | load 返回数据；当前核心内未真正收集 active load data |
| `isNACK` | `Bool` | 请求需要重发 |
| `exception` | `Bool` | 请求产生异常 |
| `exceptionNumber` | `UInt(ExceptionNumberWidth.W)` | 异常号 |

### 2.8 VRF 和 ROB Bundle

| Bundle | 字段 | 含义 |
|---|---|---|
| `VAGQVRFReadReq` | `entryIdx`, `robIdx`, `psrc` | merge 阶段读取旧 `vd` |
| `VAGQVRFReadResp` | `entryIdx`, `robIdx`, `data` | VRF 返回旧 `vd` 数据 |
| `VAGQVRFWriteReq` | `entryIdx`, `robIdx`, `pdest`, `data`, `mask` | merge 后写回非 active byte |
| `VAGQWritebackReq` | `meta`, `entryIdx`, `robIdx`, `exception`, `exceptionNumber`, `faultElemIdx`, `faultVstart` | 向 ROB 写回正常完成或异常完成 |

`faultElemIdx` 是当前 16B flow 内的 byte offset。`faultVstart` 是换算后的架构元素序号，用于异常恢复时更新 `vstart`。

### 2.9 entry 和控制 Bundle

| Bundle | 字段 | 含义 |
|---|---|---|
| `CtrlInput` | `entryIdx`, `entry` | 给 `SplitCtrl` / `MergeCtrl` 观察 entry 表项 |
| `VAGQMaskInfo` | `elemActiveMask`, `elemAgnosticMask` | `MaskGen` 输出的 active byte mask 和 agnostic byte mask |
| `VAGQEntryStateUpdate` | `entryIdx`, `stateNext`, `clearValid` | `MergeCtrl` 对 entry 状态或 valid 位的更新 |
| `VAGQReqBitmapUpdate` | `entryIdx`, `setReqSent`, `clearReqSent`, `setReqAck`, `exception`, `exceptionNumber`, `faultElemIdx` | `SplitCtrl` / `MergeCtrl` 对 `reqSent/reqAck/exception` 的更新 |

---

## 3. 顶层 `VAGQ` IO

定义位置: `Vagq.scala`

| 信号 | 方向 | 类型 | 说明 |
|---|---|---|---|
| `addrUop` | input decoupled | `Flipped(Decoupled[VAGQAddrSideUop])` | 地址侧 uop 输入。fire 后写入目标 entry 的地址、mask、类型、ROB 等字段 |
| `dataUop` | input decoupled | `Flipped(Decoupled[VAGQDataSideUop])` | 数据侧 uop 输入。fire 后写入目标 entry 的 `psrc2` 等字段 |
| `lsuReq` | output decoupled | `Decoupled[VAGQLsuReq]` | active 元素对应的真实 LSU/STA 请求 |
| `lsuResp` | input valid vec | `Flipped(Vec(LsuRespWidth, Valid[VAGQResp]))` | LSU/STA 返回的 ACK/NACK/exception response，当前有 2 lane |
| `lsqEmptyReq` | output decoupled | `Decoupled[VAGQLsqEmptyReq]` | 非 active byte 对应的 LSQ 空项消费请求 |
| `lsqEmptyResp` | input valid | `Flipped(Valid[VAGQResp])` | LSQ-empty 请求的 ACK/NACK/exception response |
| `vrfReadReq` | output decoupled | `Decoupled[VAGQVRFReadReq]` | load merge 阶段读取旧 `vd` |
| `vrfReadResp` | input valid | `Flipped(Valid[VAGQVRFReadResp])` | VRF 读返回旧 `vd` 数据 |
| `vrfWriteReq` | output decoupled | `Decoupled[VAGQVRFWriteReq]` | load merge 阶段写回非 active byte |
| `robWriteback` | output decoupled | `Decoupled[VAGQWritebackReq]` | VAGQ uop 正常完成或异常完成后写回 ROB |
| `redirect` | input valid | `Flipped(Valid[Redirect])` | redirect/flush 信号，用于过滤请求、响应和清除 entry |

顶层内部连线关系：

- `addrUop` 同时驱动 `MaskGen` 和 `VAGQEntryTable`。
- `VAGQEntryTable.entries` 同时供 `SplitCtrl` 和 `MergeCtrl` 观察。
- `SplitCtrl` 生成 `lsuReq`、`lsqEmptyReq` 和 `splitUpdate`。
- `MergeCtrl` 消费 `lsuResp`、`lsqEmptyResp`、`vrfReadResp`，生成 `reqUpdate`、`stateUpdate`、`vrfReadReq`、`vrfWriteReq`、`robWriteback`。
- `VAGQEntryTable` 是 entry 寄存器的唯一写入点，统一合并 `splitUpdate`、`mergeReqUpdate` 和 `mergeStateUpdate`。

---

## 4. 内部模块 IO

### 4.1 `MaskGen`

定义位置: `MaskGen.scala`

模块 IO：

| 信号 | 方向 | 类型 | 说明 |
|---|---|---|---|
| `in` | input | `MaskGenInput` | 地址侧 uop 的 mask 相关字段 |
| `out` | output | `VAGQMaskInfo` | 写入 entry 的 byte mask 信息 |

`MaskGenInput` 字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `uopIdx` | `UInt(vagqUopIdxWidth.W)` | 当前 uop slice 序号 |
| `useVstart` | `Bool` | 是否启用 `vstart` prestart 逻辑 |
| `vstart` | `UInt((CSRConfig.VlWidth-1).W)` | 架构 `vstart` |
| `uvlByte` | `UInt(5.W)` | 当前 slice 内 `vl` 覆盖的 byte 上界 |
| `vm` | `Bool` | mask enable，`1` 表示所有非 prestart/tail byte 都 active |
| `v0Mask` | `UInt(vagqFlowBytes.W)` | 当前 flow 对应的 v0 mask |
| `deew` | `UInt(EewWidth.W)` | data element width |
| `vma` | `Bool` | inactive byte 是否 agnostic |
| `vta` | `Bool` | tail byte 是否 agnostic |

`VAGQMaskInfo` 字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `elemActiveMask` | `UInt(vagqFlowBytes.W)` | active byte mask；SplitCtrl 只对这些 byte 发真实 LSU 请求 |
| `elemAgnosticMask` | `UInt(vagqFlowBytes.W)` | inactive/tail 且允许 agnostic 的 byte mask；MergeCtrl 用它决定是否写全 1 |

mask 判定规则：

- `prestart`: `byteIdx < uvstartByte`
- `tail`: 非 prestart 且 `byteIdx >= uvlByte`
- `inactive`: 非 prestart、非 tail，且 `vm=0` 且对应 v0 mask bit 为 0
- `active`: 非 prestart、非 tail，且 `vm=1` 或对应 v0 mask bit 为 1
- `elemAgnosticMask = inactiveBits & vma | tailBits & vta`

### 4.2 `AddrGen`

定义位置: `AddrGen.scala`

模块 IO：

| 信号 | 方向 | 类型 | 说明 |
|---|---|---|---|
| `in` | input | `AddrGenInput` | entry 中地址生成所需字段和当前 selected byte offset |
| `out` | output | `AddrGenOutput` | 当前元素的地址、元素序号和 byte mask |

`AddrGenInput` 字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `uopType` | `UInt(3.W)` | 判断 stride 还是 indexed |
| `baseAddr` | `UInt(XLEN.W)` | base 地址 |
| `op2Data` | `UInt(VLEN.W)` | stride 值或 indexed offset vector |
| `uopIdx` | `UInt(vagqUopIdxWidth.W)` | 当前 uop slice index |
| `byteOffset` | `UInt(vagqFlowByteWidth.W)` | 当前被选择的 byte offset |
| `deew` | `UInt(EewWidth.W)` | data element width |
| `ieew` | `UInt(EewWidth.W)` | indexed element width |

`AddrGenOutput` 字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `vaddr` | `UInt(XLEN.W)` | `baseAddr + offset` |
| `byteOffset` | `UInt(vagqFlowByteWidth.W)` | 透传输入 byte offset |
| `elemIdx` | `UInt(vagqFlowByteWidth.W)` | 当前 16B flow 内元素 index，`byteOffset >> deew` |
| `elemMask` | `UInt(vagqFlowBytes.W)` | 当前元素覆盖的 byte mask |

地址生成规则：

- `elemBytes = 1 << deew`
- `elemIdx = byteOffset >> deew`
- `elemOrdFromInst = (uopIdx << elemNum(deew)) | elemIdx`
- stride: `offset = sign_extend(op2Data[XLEN-1:0]) * elemOrdFromInst`
- indexed: 从 `op2Data` 里按 `ieew` 和 `elemIdx` 选出 index offset，当前实现做零扩展
- `elemMask` 覆盖 `[byteOffset, byteOffset + elemBytes)` 范围内的 byte

### 4.3 `VAGQEntryTable`

定义位置: `EntryTable.scala`

模块 IO：

| 信号 | 方向 | 类型 | 说明 |
|---|---|---|---|
| `addrUop` | input decoupled | `Flipped(Decoupled[VAGQAddrSideUop])` | 地址侧 uop 写 entry |
| `dataUop` | input decoupled | `Flipped(Decoupled[VAGQDataSideUop])` | 数据侧 uop 写 entry |
| `maskInfo` | input | `VAGQMaskInfo` | 与 `addrUop.bits` 同拍组合产生的 mask 信息 |
| `entries` | output | `Vec(vagqSize, VAGQEntry)` | 当前 entry 表快照，给 SplitCtrl/MergeCtrl 观察 |
| `splitUpdate` | input valid | `Valid[VAGQReqBitmapUpdate]` | SplitCtrl 发请求后置位 `reqSent` |
| `mergeReqUpdate` | input valid vec | `Vec(MergeRespWidth, Valid[VAGQReqBitmapUpdate])` | MergeCtrl 根据 response 更新 `reqAck/reqSent/exception` |
| `mergeStateUpdate` | input valid | `Valid[VAGQEntryStateUpdate]` | MergeCtrl 推进状态或释放 entry |
| `redirect` | input valid | `Flipped(Valid[Redirect])` | 清除被 redirect flush 的 live entry |

ready 条件：

- `addrUop.ready = entryIdx valid && entry 空闲或处于 waitA && entry 未被 redirect flush`
- `dataUop.ready = entryIdx valid && entry 空闲或处于 waitSI && entry 未被 redirect flush`

写入和状态行为：

- 地址侧先到空 entry: 写地址字段和 mask，状态进入 `waitSI`。
- 数据侧先到空 entry: 写 `psrc2` 等数据侧字段，状态进入 `waitA`。
- 两侧同拍到达同一个 entry，或后一侧补齐等待状态: 状态进入 `split`。
- `splitUpdate` / `mergeReqUpdate` 统一更新 `reqSent`、`reqAck` 和异常字段。
- `mergeStateUpdate.clearValid` 为真时释放 entry。
- redirect 命中 live entry 时清空整个 entry。

`VAGQEntryMeta` 主要字段：

| 字段 | 含义 |
|---|---|
| `valid` | entry 是否有效 |
| `meta` | 原始指令元信息 |
| `uopType` | stride/indexed/load/store/ordered 类型 |
| `robIdx` | ROB index |
| `pdest` | load 目标向量物理寄存器 |
| `psrc2` | merge 读旧 `vd` 或 store data 源 |
| `baseAddr` | base 地址 |
| `op2Data` | stride 或 indexed offset 数据 |
| `ieew`, `deew` | index/data element width |
| `useVstart`, `vma`, `vta` | mask/tail/vstart 策略 |
| `uopIdx` | 当前指令内 uop slice 序号 |
| `elemActiveMask` | active byte mask |
| `elemAgnosticMask` | agnostic byte mask |
| `nf` | segment field，当前透传 |
| `reqSent` | byte lane 是否已经发出请求 |
| `reqAck` | byte lane 是否已经完成 |
| `exceptionNumber` | 记录的异常号 |
| `faultElemIdx` | 记录的 fault byte offset |
| `state` | 当前 entry 状态 |

entry 状态编码：

| 状态 | 编码 | 含义 |
|---|---|---|
| `waitA` | `001` | 已有数据侧，等待地址侧 |
| `waitSI` | `010` | 已有地址侧，等待数据侧 |
| `split` | `011` | 两侧齐备，正在拆成 byte/element 请求 |
| `merge` | `100` | load 请求完成，正在 merge 旧 `vd` |
| `wb` | `101` | 等待 ROB 正常写回 |
| `excp` | `110` | 等待 ROB 异常写回 |

### 4.4 `SplitCtrl`

定义位置: `SplitCtrl.scala`

模块 IO：

| 信号 | 方向 | 类型 | 说明 |
|---|---|---|---|
| `in` | input | `Vec(numEntries, CtrlInput)` | 从 EntryTable 观察所有 entry |
| `redirect` | input valid | `Flipped(Valid[Redirect])` | 过滤被冲刷 entry |
| `lsuReq` | output decoupled | `Decoupled[VAGQLsuReq]` | active 请求输出 |
| `lsqEmptyReq` | output decoupled | `Decoupled[VAGQLsqEmptyReq]` | inactive/prestart/tail 空项请求输出 |
| `update` | output valid | `Valid[VAGQReqBitmapUpdate]` | 请求 fire 后回写 EntryTable，置位 `reqSent` |

选择逻辑：

- `canSplit = entry.valid && state == split && !needFlush`
- `orderedBlocked = entry.isOrdered && (reqSent & ~reqAck & elemActiveMask).orR`
- `activePending = ~reqSent & elemActiveMask`，但 ordered blocked 时强制为 0
- `emptyPending = ~reqSent & ~reqAck & ~elemActiveMask`
- active 请求优先级高于 empty 请求
- entry 选择使用 `PriorityEncoder`
- byte 选择使用 `PriorityEncoder(selectedPending)`

输出行为：

- `lsuReq.valid = hasReq && hasActiveReq`
- `lsqEmptyReq.valid = hasReq && !hasActiveReq`
- `update.valid = lsuReq.fire || lsqEmptyReq.fire`
- `update.bits.setReqSent = issueMask`
- `issueMask = addrGen.out.elemMask & selectedPending`

当前限制：

- 当前 `SplitCtrl` 只有一个 active/empty 选择通道，不是多 lane issue。
- `lsuReq.bits.data` 当前固定为 `0.U`，store data 尚未接入。

### 4.5 `MergeCtrl`

定义位置: `MergeCtrl.scala`

模块 IO：

| 信号 | 方向 | 类型 | 说明 |
|---|---|---|---|
| `entry` | input | `Vec(numEntries, CtrlInput)` | 从 EntryTable 观察所有 entry |
| `lsuResp` | input valid vec | `Flipped(Vec(LsuRespWidth, Valid[VAGQResp]))` | LSU/STA response |
| `lsqEmptyResp` | input valid | `Flipped(Valid[VAGQResp])` | LSQ-empty response |
| `reqUpdate` | output valid vec | `Vec(MergeRespWidth, Valid[VAGQReqBitmapUpdate])` | response 转换成 bitmap/异常更新 |
| `stateUpdate` | output valid | `Valid[VAGQEntryStateUpdate]` | split done、merge done、ROB 写回后的状态推进 |
| `vrfReadReq` | output decoupled | `Decoupled[VAGQVRFReadReq]` | merge 阶段读旧 `vd` |
| `vrfReadResp` | input valid | `Flipped(Valid[VAGQVRFReadResp])` | 旧 `vd` 返回 |
| `vrfWriteReq` | output decoupled | `Decoupled[VAGQVRFWriteReq]` | merge 阶段写回非 active byte |
| `robWriteback` | output decoupled | `Decoupled[VAGQWritebackReq]` | 正常完成或异常完成写回 ROB |
| `redirect` | input valid | `Flipped(Valid[Redirect])` | 过滤被冲刷 entry 和 pending merge response |

response 接受条件：

- response lane valid
- `entryIdx` 命中一个 entry
- entry valid
- response `robIdx` 等于该 entry 当前 `robIdx`

response 到 bitmap update 的映射：

| response 类型 | `setReqAck` | `clearReqSent` | 状态影响 |
|---|---|---|---|
| 正常 ACK | `resp.mask` | 0 | byte lane 完成 |
| NACK 且无异常 | 0 | `resp.mask` | 允许之后重发 |
| exception | `resp.mask` | 0 | 记录异常号和 `faultElemIdx`，entry 进入 `excp` |

状态推进优先级：

| 优先级 | 条件 | 行为 |
|---:|---|---|
| 1 | split entry 的 `reqAck.andR` 且本拍没有该 entry exception hit | store 或可跳过 merge 时进入 `wb`，否则进入 `merge` |
| 2 | VRF merge write fire | entry 进入 `wb` |
| 3 | ROB writeback fire | `clearValid := true`，释放 entry |

merge 行为：

- `merge` 状态 entry 会通过 `vrfReadReq` 读取 `psrc2` 指向的旧 `vd`。
- VRF response 必须匹配 `entryIdx` 和 `robIdx`，并且 entry 仍处于 live merge 状态。
- `nonActiveMask = ~elemActiveMask`。
- `mergeWriteData = oldVd`，但 `elemAgnosticMask & nonActiveMask` 覆盖的 byte 写成全 1。
- `vrfWriteReq.mask = nonActiveMask`，即只写非 active byte。

异常写回行为：

- `excp` entry 只有在 `faultElemIdx` 之前的 older in-flight byte 都不再 pending 后才允许 ROB 写回。
- `robWriteback.exception = true` 时携带 `exceptionNumber`、`faultElemIdx`、`faultVstart`。
- `faultVstart = (uopIdx << elemNum(deew)) + (faultElemIdx >> deew)`。

---

## 5. 关键握手约定

### 5.1 Decoupled

`Decoupled` 信号只有 `valid && ready` 同时为真才 fire。

当前使用 Decoupled 的接口：

| 接口 | 方向 | fire 后含义 |
|---|---|---|
| `addrUop` | VAGQ input | 地址侧字段写入 entry |
| `dataUop` | VAGQ input | 数据侧字段写入 entry |
| `lsuReq` | VAGQ output | active 请求发出，EntryTable 置位 `reqSent` |
| `lsqEmptyReq` | VAGQ output | empty 请求发出，EntryTable 置位 `reqSent` |
| `vrfReadReq` | VAGQ output | merge 读旧 `vd` 请求被接受 |
| `vrfWriteReq` | VAGQ output | merge 写回被接受，entry 进入 `wb` |
| `robWriteback` | VAGQ output | ROB 写回被接受，entry 释放 |

### 5.2 Valid

`Valid` 信号没有 ready，接收端必须在 valid 当拍采样或自行过滤。

当前使用 Valid 的接口：

| 接口 | 说明 |
|---|---|
| `lsuResp` | LSU/STA response，MergeCtrl 用 `entryIdx + robIdx` 过滤 |
| `lsqEmptyResp` | LSQ-empty response，MergeCtrl 用 `entryIdx + robIdx` 过滤 |
| `vrfReadResp` | VRF read response，MergeCtrl 用 pending read entry 和 `robIdx` 过滤 |
| `redirect` | redirect/flush 广播，EntryTable/SplitCtrl/MergeCtrl 都会过滤 |
| `splitUpdate` | SplitCtrl 到 EntryTable 的 bitmap 更新 |
| `mergeReqUpdate` | MergeCtrl 到 EntryTable 的 response bitmap 更新 |
| `mergeStateUpdate` | MergeCtrl 到 EntryTable 的状态更新 |
| `reqUpdate` | MergeCtrl 内部输出给 EntryTable |
| `stateUpdate` | MergeCtrl 内部输出给 EntryTable |

### 5.3 `entryIdx + robIdx` 匹配

VAGQ entry 可能被释放后重新分配，因此 response 不能只依赖 `entryIdx`。

当前 `MergeCtrl` 接受 response 的条件中同时检查：

- `entryIdx` 命中合法 entry
- entry 当前 `valid`
- entry 当前 `robIdx == resp.robIdx`

这可以避免旧请求的 late response 错误更新复用后的新 entry。

### 5.4 `reqSent / reqAck` byte 状态

每个 byte lane 的请求状态由两个 bit 表示。

| 状态 | `reqSent` | `reqAck` | 含义 |
|---|---:|---:|---|
| IDLE | 0 | 0 | 未发请求 |
| SENT | 1 | 0 | 已发请求，等待 ACK/NACK/exception |
| DONE | X | 1 | 已完成 |

状态转移：

- `SplitCtrl` 发出请求后置位 `reqSent`。
- 正常 ACK 置位 `reqAck`。
- NACK 清除 `reqSent`，允许后续重发。
- exception 记录异常信息，并使 entry 进入 `excp`。

---

## 6. 操作类型编码

定义位置: `EntryTable.scala`

| 类型 | 编码 | 含义 |
|---|---|---|
| `strideLoad` | `000` | constant-stride load |
| `strideStore` | `001` | constant-stride store |
| `indexedUnorderedLoad` | `100` | unordered indexed load |
| `indexedUnorderedStore` | `101` | unordered indexed store |
| `indexedOrderedLoad` | `110` | ordered indexed load |
| `indexedOrderedStore` | `111` | ordered indexed store |

辅助判断：

- `isLoad = !uopType(0)`
- `isStore = uopType(0)`
- `isStride = !uopType(2) && !uopType(1)`
- `isIndexed = uopType(2)`
- `isOrdered = uopType(1)`

---

## 7. 当前实现注意点

- `VAGQDataSideUop` 当前只携带 `psrc2`，没有直接携带 store data。
- `VAGQLsuReq.data` 当前在 `SplitCtrl` 中固定为 0，store data 通路仍是 TODO。
- `VAGQResp.data` 当前定义存在，但核心 `MergeCtrl` 没有把 active load data 收集进 entry；当前 merge 只处理非 active byte 的旧 `vd` / agnostic 写回。
- `nf` 当前只在地址侧 uop、entry、`VAGQLsuReq` 中透传，没有驱动 segment 维度的额外拆分。
- `SplitCtrl` 当前实现是单请求选择逻辑，不是多 lane split issue。
- `faultElemIdx` 命名上像元素序号，但当前存的是 16B flow 内 byte offset；`faultVstart` 才是元素级架构序号。
