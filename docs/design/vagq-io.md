# VAGQ IO 接口说明

> 最后更新: 2026-07-06
>
> 对应代码: `src/main/scala/xiangshan/backend/vector/vagq/`

---

## 0. 当前范围

本文描述当前仓库中已经落地的 VAGQ core、本地 MemBlock 接入接口，以及 LSQ empty mark 相关 IO，包括:

- `VAGQ`
- `VAGQEntryTable`
- `MaskGen`
- `AddrGen`
- `SplitCtrl`
- `MergeCtrl`
- `VAGQDownstreamAdapter`
- `VAGQMemPipelineMeta`
- `LSQWrapper` / LQ / SQ empty mark 接口

当前仍未落地的上游 issue/dispatch 接入、VAGQ VRF 系统级读写接入、ROB writeback/release 接入只在本文“当前系统级接入状态”中说明，不作为完整接口方案。

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
| `ActiveIssueWidth` | 2 | VAGQ active `lsuReq` 发射 lane 数量 |
| `LduRespWidth` | 3 | LDU 返回给 VAGQ 的 response lane 数量，对应 3 个 load unit |
| `StaRespWidth` | 2 | STA 返回给 VAGQ 的 response lane 数量，对应 2 个 store unit |
| `ActiveRespWidth` | 5 | active response 总路数，`LduRespWidth + StaRespWidth` |
| `VrfWriteWidth` | 1 | VAGQ core 当前定义的 VRF write 宽度，目前顶层 IO 是单路 `ValidIO` |
| `SplitUpdateWidth` | 2 | SplitCtrl 到 EntryTable 的状态更新路数：active update + empty update |
| `MergeRespWidth` | 6 | MergeCtrl 接收的 response lane 数量，等于 3 个 `lduResp` + 2 个 `staResp` + 1 个 `lsqEmptyResp` |

当前硬约束:

- `VLEN == 128`
- `VDataBytes == FlowBytes`
- `VAGQSize == 4 || VAGQSize == 8`

---

## 2. 公共 Bundle

### 2.1 `VAGQMeta`

定义位置: `Vagq.scala`

VAGQ entry 中随指令保存的元信息，用于后续写回或异常报告。

| 字段 | 类型 | 含义 |
|---|---|---|
| `pc` | `UInt(VAddrBits.W)` | 指令 PC |
| `isRVC` | `Bool` | 是否为 RVC 压缩指令 |
| `ftqPtr` | `FtqPtr` | FTQ 指针 |
| `ftqOffset` | `UInt(FetchBlockInstOffsetWidth.W)` | fetch block 内指令偏移 |
| `lqIdx` | `LqPtr` | LoadQueue 预留表项指针 |
| `sqIdx` | `SqPtr` | StoreQueue 预留表项指针 |
| `trigger` | `TriggerAction()` | trigger/debug 相关信息 |
| `perfDebugInfo` | `PerfDebugInfo` | 性能调试信息 |
| `debug_seqNum` | `InstSeqNum()` | difftest/debug 序号 |

### 2.2 `VAGQAddrSideUop`

地址侧 uop，进入 `VAGQ.io.addrUop` / `VAGQEntryTable.io.addrUop`。

| 字段 | 类型 | 含义 |
|---|---|---|
| `meta` | `VAGQMeta` | 写回和异常路径需要的原始指令元信息 |
| `entryIdx` | `UInt(vagqEntryIdxWidth.W)` | 目标 VAGQ entry |
| `uopType` | `UInt(3.W)` | VAGQ 操作类型，见 `VAGQUopType` |
| `robIdx` | `RobPtr` | ROB index，用于 redirect 过滤、response 匹配、写回 |
| `pdest` | `UInt(VfPhyRegIdxWidth.W)` | load 目标向量物理寄存器 |
| `baseAddr` | `UInt(XLEN.W)` | base 地址 |
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

数据侧 uop，进入 `VAGQ.io.dataUop` / `VAGQEntryTable.io.dataUop`。

| 字段 | 类型 | 含义 |
|---|---|---|
| `entryIdx` | `UInt(vagqEntryIdxWidth.W)` | 目标 VAGQ entry，需要与地址侧 uop 一致 |
| `robIdx` | `RobPtr` | ROB index，用于 redirect 过滤和 response 匹配 |
| `op2Data` | `UInt(VLEN.W)` | stride 值或 indexed offset vector 原始数据；由数据侧写入 entry |
| `psrc2` | `UInt(VfPhyRegIdxWidth.W)` | load merge 读旧 `vd` 的源寄存器；store data 的源寄存器 |

### 2.4 `VAGQReqBase`

`VAGQLsuReq` 的公共基础字段。`VAGQLsqEmptyReq` 当前是独立 bundle，不继承该 base。

| 字段 | 类型 | 含义 |
|---|---|---|
| `entryIdx` | `UInt(vagqEntryIdxWidth.W)` | 请求所属 VAGQ entry |
| `robIdx` | `RobPtr` | 请求所属 ROB index，response 返回后必须匹配 live entry |
| `lqIdx` | `LqPtr` | 对应 LoadQueue 预留表项 |
| `sqIdx` | `SqPtr` | 对应 StoreQueue 预留表项 |

### 2.5 `VAGQLsuReq`

active byte/element 对应的真实访存请求，继承 `VAGQReqBase`。

| 字段 | 类型 | 含义 |
|---|---|---|
| `isLoad` | `Bool` | 当前请求来自 load 类 VAGQ uop |
| `isStore` | `Bool` | 当前请求来自 store 类 VAGQ uop |
| `byteOffset` | `UInt(vagqFlowByteWidth.W)` | 16B flow 内被选中的第一个 byte offset |
| `elemIdx` | `UInt(vagqFlowByteWidth.W)` | 16B flow 内元素 index，当前由 `byteOffset >> deew` 生成 |
| `mask` | `UInt(vagqFlowBytes.W)` | 当前 active 元素覆盖的 byte mask |
| `alignedType` | `UInt(AlignedTypeWidth.W)` | 对齐/访问宽度类型，当前由 `deew` 填入 |
| `vaddr` | `UInt(XLEN.W)` | `AddrGen` 生成后的虚拟地址 |
| `data` | `UInt(VLEN.W)` | store data；由 `SplitCtrl` 的 store-data 读路径缓存后填入 |
| `pdest` | `UInt(VfPhyRegIdxWidth.W)` | load 目标向量物理寄存器 |
| `nf` | `UInt(NfWidth.W)` | segment field，当前透传 |

### 2.6 `VAGQLsqEmptyReq`

inactive/prestart/tail byte 对应的空请求。它不进入 AddrGen/LDU/STA，只用于让 LSQ 标记已经预留但没有真实访存的 LQ/SQ 项。

| 字段 | 类型 | 含义 |
|---|---|---|
| `entryIdx` | `UInt(vagqEntryIdxWidth.W)` | 请求所属 VAGQ entry |
| `robIdx` | `RobPtr` | 请求所属 ROB index，response 返回后必须匹配 live entry |
| `isLoad` | `Bool` | 标记 LoadQueue 项 |
| `isStore` | `Bool` | 标记 StoreQueue 项 |
| `lqIdx` | `LqPtr` | LoadQueue 预留表项 base 指针 |
| `sqIdx` | `SqPtr` | StoreQueue 预留表项 base 指针 |
| `emptyMask` | `UInt(vagqFlowBytes.W)` | VAGQ byte 级 mask，用于 response ACK/NACK 后更新 `reqSent/reqAck` |
| `entryMask` | `UInt(vagqFlowBytes.W)` | LSQ entry/元素级 mask，由 `emptyMask` 按 `deew` 转换得到，用于标记 `lqIdx/sqIdx + i` |

### 2.7 `VAGQLsqEmptyResp`

LSQ empty mark 的反馈。VAGQ 顶层会把它转换为内部 `VAGQResp` 后送入 `MergeCtrl`。

| 字段 | 类型 | 含义 |
|---|---|---|
| `entryIdx` | `UInt(vagqEntryIdxWidth.W)` | response 对应的 VAGQ entry |
| `robIdx` | `RobPtr` | response 对应 ROB index |
| `isLoad` | `Bool` | response 来自 LoadQueue empty mark |
| `isStore` | `Bool` | response 来自 StoreQueue empty mark |
| `mask` | `UInt(vagqFlowBytes.W)` | ACK/NACK 覆盖的 VAGQ byte mask，通常等于 request 的 `emptyMask` |
| `isNACK` | `Bool` | LSQ 没有成功标记，VAGQ 需要清除 `reqSent` 并重试 |
| `exception` | `Bool` | 当前 LSQ empty mark 不产生架构异常，通常为 0 |
| `exceptionNumber` | `UInt(ExceptionNumberWidth.W)` | 当前通常为 0 |

### 2.8 `VAGQResp`

VAGQ active 请求的反馈，也是 `MergeCtrl` 内部统一处理 active response 和转换后 empty response 的格式。

| 字段 | 类型 | 含义 |
|---|---|---|
| `entryIdx` | `UInt(vagqEntryIdxWidth.W)` | response 对应的 VAGQ entry |
| `robIdx` | `RobPtr` | response 对应 ROB index；必须和 entry 内 `robIdx` 匹配才会被接受 |
| `isLoad` | `Bool` | response 来自 load 请求 |
| `isStore` | `Bool` | response 来自 store 请求 |
| `byteOffset` | `UInt(vagqFlowByteWidth.W)` | response 对应的 16B flow 内 byte offset |
| `mask` | `UInt(vagqFlowBytes.W)` | ACK/NACK/exception 覆盖的 byte mask |
| `data` | `UInt(VLEN.W)` | load 返回数据；当前 `MergeCtrl` 未收集该字段到 entry |
| `isNACK` | `Bool` | 请求需要重发 |
| `exception` | `Bool` | 请求产生异常 |
| `exceptionNumber` | `UInt(ExceptionNumberWidth.W)` | 异常号 |

### 2.9 VRF 和 ROB Bundle

| Bundle | 字段 | 含义 |
|---|---|---|
| `VAGQVRFReadReq` | `entryIdx`, `robIdx`, `psrc` | `SplitCtrl` 为 store 读 store data，或 `MergeCtrl` 为 load merge 读旧 `vd` |
| `VAGQVRFReadResp` | `entryIdx`, `robIdx`, `data` | VRF 读返回数据 |
| `VAGQVRFWriteReq` | `entryIdx`, `pdest`, `data`, `mask` | merge 后写回非 active byte；当前没有 `robIdx` 字段 |
| `VAGQWritebackReq` | `meta`, `entryIdx`, `robIdx`, `exception`, `exceptionNumber`, `faultElemIdx`, `faultVstart` | VAGQ 正常完成或异常完成写回 |

`faultElemIdx` 是当前 16B flow 内的 byte offset。`faultVstart` 是换算后的架构元素序号。

### 2.10 entry 和控制 Bundle

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
| `dataUop` | input decoupled | `Flipped(Decoupled[VAGQDataSideUop])` | 数据侧 uop 输入。fire 后写入目标 entry 的 `op2Data`、`psrc2` 等字段 |
| `lsuReq` | output decoupled vec | `Vec(ActiveIssueWidth, Decoupled[VAGQLsuReq])` | active 元素对应的访存请求，一拍最多两路 |
| `lduResp` | input valid vec | `Flipped(Vec(LduRespWidth, Valid[VAGQResp]))` | LDU 返回的 active load ACK/NACK/exception response |
| `staResp` | input valid vec | `Flipped(Vec(StaRespWidth, Valid[VAGQResp]))` | STA 返回的 active store ACK/NACK/exception response |
| `lsqEmptyReq` | output decoupled | `Decoupled[VAGQLsqEmptyReq]` | 非 active byte 对应的空请求 |
| `lsqEmptyResp` | input valid | `Flipped(Valid[VAGQLsqEmptyResp])` | 空请求的 ACK/NACK response |
| `vrfReadReq` | output decoupled | `Decoupled[VAGQVRFReadReq]` | VRF 读请求，由 `SplitCtrl` store-data read 和 `MergeCtrl` old-vd read 仲裁后输出 |
| `vrfReadResp` | input valid | `Flipped(Valid[VAGQVRFReadResp])` | VRF 读返回数据，同时送给 `SplitCtrl` 和 `MergeCtrl`，各自用 pending entry 过滤 |
| `vrfWriteReq` | output valid | `ValidIO[VAGQVRFWriteReq]` | load merge 阶段写回非 active byte；当前没有 ready 反压 |
| `robWriteback` | output decoupled | `Decoupled[VAGQWritebackReq]` | VAGQ uop 正常完成或异常完成后写回 |
| `redirect` | input valid | `Flipped(Valid[Redirect])` | redirect/flush 信号，用于过滤请求、响应和清除 entry |

顶层内部连线关系:

- `addrUop/dataUop` 直接接入 `VAGQEntryTable`。
- `VAGQEntryTable` 内部实例化 `MaskGen`，地址侧 fire 时写入 `elemActiveMask/elemAgnosticMask`。
- `VAGQEntryTable.entries` 同时供 `SplitCtrl` 和 `MergeCtrl` 观察。
- `SplitCtrl` 生成 `lsuReq`、`lsqEmptyReq`、store-data `vrfReadReq` 和 `splitUpdate`。
- 顶层把 `VAGQLsqEmptyResp` 转换为内部 `VAGQResp`，再交给 `MergeCtrl.lsqEmptyResp`。
- `MergeCtrl` 消费 `lduResp`、`staResp`、`lsqEmptyResp`、`vrfReadResp`，生成 `reqUpdate`、`stateUpdate`、merge `vrfReadReq`、`vrfWriteReq`、`robWriteback`。
- 顶层用 `lastVrfReadGrantSplit` 在 `SplitCtrl.vrfReadReq` 和 `MergeCtrl.vrfReadReq` 之间做轮转仲裁。
- `VAGQEntryTable` 是 entry 寄存器的唯一写入点，统一合并 `splitUpdate`、`mergeReqUpdate` 和 `mergeStateUpdate`。

---

## 4. 内部模块 IO

### 4.1 `MaskGen`

定义位置: `MaskGen.scala`

| 信号 | 方向 | 类型 | 说明 |
|---|---|---|---|
| `in` | input | `MaskGenInput` | 地址侧 uop 的 mask 相关字段 |
| `out` | output | `VAGQMaskInfo` | 写入 entry 的 byte mask 信息 |

`MaskGenInput` 字段:

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

`VAGQMaskInfo` 字段:

| 字段 | 类型 | 说明 |
|---|---|---|
| `elemActiveMask` | `UInt(vagqFlowBytes.W)` | active byte mask；SplitCtrl 只对这些 byte 发真实请求 |
| `elemAgnosticMask` | `UInt(vagqFlowBytes.W)` | inactive/tail 且允许 agnostic 的 byte mask；MergeCtrl 用它决定是否写全 1 |

mask 判定规则:

- `prestart`: `byteIdx < uvstartByte`
- `tail`: 非 prestart 且 `byteIdx >= uvlByte`
- `inactive`: 非 prestart、非 tail，且 `vm=0` 且对应 v0 mask bit 为 0
- `active`: 非 prestart、非 tail，且 `vm=1` 或对应 v0 mask bit 为 1
- `elemAgnosticMask = inactiveBits & vma | tailBits & vta`

### 4.2 `AddrGen`

定义位置: `AddrGen.scala`

| 信号 | 方向 | 类型 | 说明 |
|---|---|---|---|
| `in` | input | `AddrGenInput` | entry 中地址生成所需字段和当前 selected byte offset |
| `out` | output | `AddrGenOutput` | 当前元素的地址、元素序号和 byte mask |

`AddrGenInput` 字段:

| 字段 | 类型 | 说明 |
|---|---|---|
| `uopType` | `UInt(3.W)` | 判断 stride 还是 indexed |
| `baseAddr` | `UInt(XLEN.W)` | base 地址 |
| `op2Data` | `UInt(VLEN.W)` | stride 值或 indexed offset vector |
| `uopIdx` | `UInt(vagqUopIdxWidth.W)` | 当前 uop slice index |
| `byteOffset` | `UInt(vagqFlowByteWidth.W)` | 当前被选择的 byte offset |
| `deew` | `UInt(EewWidth.W)` | data element width |
| `ieew` | `UInt(EewWidth.W)` | indexed element width |

`AddrGenOutput` 字段:

| 字段 | 类型 | 说明 |
|---|---|---|
| `vaddr` | `UInt(XLEN.W)` | `baseAddr + offset` |
| `byteOffset` | `UInt(vagqFlowByteWidth.W)` | 透传输入 byte offset |
| `elemIdx` | `UInt(vagqFlowByteWidth.W)` | 当前 16B flow 内元素 index，`byteOffset >> deew` |
| `elemMask` | `UInt(vagqFlowBytes.W)` | 当前元素覆盖的 byte mask |

地址生成规则:

- `elemBytes = 1 << deew`
- `elemIdx = byteOffset >> deew`
- `elemOrdFromInst = (uopIdx << elemNum(deew)) | elemIdx`
- stride: `offset = op2Data(XLEN-1,0).asSInt * elemOrdFromInst.asSInt`
- indexed: 从 `op2Data` 里按 `ieew` 和 `elemIdx` 选出 index offset，当前实现零扩展到 `XLEN`
- `elemMask` 覆盖 `[byteOffset, byteOffset + elemBytes)` 范围内的 byte

### 4.3 `VAGQEntryTable`

定义位置: `EntryTable.scala`

| 信号 | 方向 | 类型 | 说明 |
|---|---|---|---|
| `addrUop` | input decoupled | `Flipped(Decoupled[VAGQAddrSideUop])` | 地址侧 uop 写 entry |
| `dataUop` | input decoupled | `Flipped(Decoupled[VAGQDataSideUop])` | 数据侧 uop 写 entry |
| `entries` | output | `Vec(vagqSize, VAGQEntry)` | 当前 entry 表快照，给 SplitCtrl/MergeCtrl 观察 |
| `splitUpdate` | input valid vec | `Vec(SplitUpdateWidth, Valid[VAGQReqBitmapUpdate])` | SplitCtrl 发请求后置位 `reqSent`，当前分 active update 和 empty update |
| `mergeReqUpdate` | input valid vec | `Vec(MergeRespWidth, Valid[VAGQReqBitmapUpdate])` | MergeCtrl 根据 response 更新 `reqAck/reqSent/exception` |
| `mergeStateUpdate` | input valid | `Valid[VAGQEntryStateUpdate]` | MergeCtrl 推进状态或释放 entry |
| `redirect` | input valid | `Flipped(Valid[Redirect])` | 清除被 redirect flush 的 live entry |

ready 条件:

- `addrUop.ready = entryIdx valid && (entry 空闲 || entry.state == waitA) && entry 未被 redirect flush`
- `dataUop.ready = entryIdx valid && (entry 空闲 || entry.state == waitSI) && entry 未被 redirect flush`

写入和状态行为:

- 地址侧先到空 entry: 写地址字段和 mask，状态进入 `waitSI`。
- 数据侧先到空 entry: 写 `psrc2` 等数据侧字段，状态进入 `waitA`。
- 两侧同拍到达同一个 entry，或后一侧补齐等待状态: 状态进入 `split`。
- `splitUpdate` / `mergeReqUpdate` 统一更新 `reqSent`、`reqAck` 和异常字段。
- `mergeStateUpdate.clearValid` 为真时释放 entry。
- redirect 命中 live entry 时清空整个 entry。

`VAGQEntryMeta` 主要字段:

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

entry 状态编码:

| 状态 | 编码 | 含义 |
|---|---|---|
| `waitA` | `001` | 已有数据侧，等待地址侧 |
| `waitSI` | `010` | 已有地址侧，等待数据侧 |
| `split` | `011` | 两侧齐备，正在拆成 byte/element 请求 |
| `merge` | `100` | load 请求完成，正在 merge 旧 `vd` |
| `wb` | `101` | 等待正常写回 |
| `excp` | `110` | 等待异常写回 |

### 4.4 `SplitCtrl`

定义位置: `SplitCtrl.scala`

| 信号 | 方向 | 类型 | 说明 |
|---|---|---|---|
| `in` | input | `Vec(numEntries, CtrlInput)` | 从 EntryTable 观察所有 entry |
| `redirect` | input valid | `Flipped(Valid[Redirect])` | 过滤被冲刷 entry |
| `lsuReq` | output decoupled vec | `Vec(ActiveIssueWidth, Decoupled[VAGQLsuReq])` | active 请求输出，当前两路 |
| `lsqEmptyReq` | output decoupled | `Decoupled[VAGQLsqEmptyReq]` | inactive/prestart/tail 空请求输出 |
| `vrfReadReq` | output decoupled | `Decoupled[VAGQVRFReadReq]` | store active 请求缺少 store data 时，读 `psrc2` |
| `vrfReadResp` | input valid | `Flipped(Valid[VAGQVRFReadResp])` | store data 读返回 |
| `update` | output valid vec | `Vec(SplitUpdateWidth, Valid[VAGQReqBitmapUpdate])` | 请求 fire 后回写 EntryTable，置位 `reqSent` |

选择逻辑:

- `canSplit = entry.valid && state == split && !needFlush`
- `orderedBlocked = entry.isOrdered && (reqSent & ~reqAck & elemActiveMask).orR`
- `activePending = ~reqSent & elemActiveMask`，但 ordered blocked 时强制为 0
- `emptyPending = ~reqSent & ~reqAck & ~elemActiveMask`
- active 请求和 empty 请求使用独立选择路径，分别从 `activePending` 和 `emptyPending` 中选 entry。
- active entry 和 empty entry 都使用 `oldestEntryOH` 选择最老 entry，比较顺序为 `robIdx`，同 ROB 内比较 `uopIdx`，再用 entry index 打破平局。
- active lane0 选择当前 active mask 的最低位元素；active lane1 从 lane0 剩余 active mask 中选择最高位元素。
- empty 请求一次发送所选 entry 当前所有 `emptyPending` byte，并通过 `entryMask` 转换成 LSQ entry 标记。

store data 逻辑:

- store active 请求需要 `selectedStoreDataReady` 才能发 `lsuReq`。
- 若选中 store active 请求且 store data 不 ready，`SplitCtrl` 通过 `vrfReadReq` 读取 `entry.psrc2`。
- `vrfReadResp` 匹配 `entryIdx + robIdx` 且 entry 仍 alive 时，数据进入 `storeData` 寄存器。
- 后续同一 entry/robIdx 的 store active 请求用 `storeData` 填 `lsuReq.bits.data`。
- 当前只有一个 pending store-data read 和一个 cached store-data slot。

输出行为:

- `lsuReq(0).valid = hasActiveReq && activeReqDataReady`
- `lsuReq(1).valid = hasActiveReq && activeHasTwoReq && activeReqDataReady`
- `lsqEmptyReq.valid = hasEmptyReq`
- `update(0)` 汇总两路 active lane 的 fire mask，置位 active byte 的 `reqSent`
- `update(1)` 在 `lsqEmptyReq.fire` 时置位 empty byte 的 `reqSent`
- `lsuReq/lsqEmptyReq` 都携带 `lqIdx/sqIdx`，来自 `entry.meta`

当前限制:

- 当前 active 路径固定写成两路 `activeAddrGen(0/1)`，若未来修改 `ActiveIssueWidth`，需要同步泛化 `SplitCtrl`。
- 当前 empty 路径只有一路 `lsqEmptyReq`，但一次 request 可以覆盖所选 entry 的多个 non-active byte/entry。

### 4.5 `MergeCtrl`

定义位置: `MergeCtrl.scala`

| 信号 | 方向 | 类型 | 说明 |
|---|---|---|---|
| `entry` | input | `Vec(numEntries, CtrlInput)` | 从 EntryTable 观察所有 entry |
| `lduResp` | input valid vec | `Flipped(Vec(LduRespWidth, Valid[VAGQResp]))` | active load 请求 response |
| `staResp` | input valid vec | `Flipped(Vec(StaRespWidth, Valid[VAGQResp]))` | active store 请求 response |
| `lsqEmptyResp` | input valid | `Flipped(Valid[VAGQResp])` | 空请求 response，VAGQ 顶层已从 `VAGQLsqEmptyResp` 转换到该格式 |
| `reqUpdate` | output valid vec | `Vec(MergeRespWidth, Valid[VAGQReqBitmapUpdate])` | response 转换成 bitmap/异常更新 |
| `stateUpdate` | output valid | `Valid[VAGQEntryStateUpdate]` | split done、merge done、写回后的状态推进 |
| `vrfReadReq` | output decoupled | `Decoupled[VAGQVRFReadReq]` | merge 阶段读旧 `vd` |
| `vrfReadResp` | input valid | `Flipped(Valid[VAGQVRFReadResp])` | 旧 `vd` 返回 |
| `vrfWriteReq` | output valid | `ValidIO[VAGQVRFWriteReq]` | merge 阶段写回非 active byte；当前无 ready |
| `robWriteback` | output decoupled | `Decoupled[VAGQWritebackReq]` | 正常完成或异常完成写回 |
| `redirect` | input valid | `Flipped(Valid[Redirect])` | 过滤被冲刷 entry 和 pending merge response |

response 接受条件:

- response lane valid
- `entryIdx` 命中一个 entry
- entry valid
- response `robIdx` 等于该 entry 当前 `robIdx`

response 到 bitmap update 的映射:

| response 类型 | `setReqAck` | `clearReqSent` | 状态影响 |
|---|---|---|---|
| 正常 ACK | `resp.mask` | 0 | byte lane 完成 |
| NACK 且无异常 | 0 | `resp.mask` | 允许之后重发 |
| exception | `resp.mask` | 0 | 记录异常号和 `faultElemIdx`，entry 进入 `excp` |

状态推进优先级:

| 优先级 | 条件 | 行为 |
|---:|---|---|
| 1 | split entry 的 `reqAck.andR` 且本拍没有该 entry exception hit | store 或可跳过 merge 时进入 `wb`，否则进入 `merge` |
| 2 | VRF merge write valid | entry 进入 `wb` |
| 3 | writeback fire | `clearValid := true`，释放 entry |

merge 行为:

- `merge` 状态 entry 会通过 `vrfReadReq` 读取 `psrc2` 指向的旧 `vd`。
- VRF response 必须匹配 `entryIdx` 和 `robIdx`，并且 entry 仍处于 live merge 状态。
- `skipMerge = !splitDoneNonActiveMask.orR`，也就是没有任何非 active byte 需要 merge 时，split done 后直接进 `wb`。
- `nonActiveMask = ~elemActiveMask`。
- `mergeWriteData = oldVd`，但 `elemAgnosticMask & nonActiveMask` 覆盖的 byte 写成全 1。
- `vrfWriteReq.mask = nonActiveMask`，即只写非 active byte。
- 当前 `MergeCtrl` 没有把 `VAGQResp.data` 收集进 entry，因此 active load data 合并路径还不完整。

异常更新:

- `MergeCtrl` 将 `lduResp ++ staResp ++ lsqEmptyResp` 转成 `reqUpdate`，总共 `MergeRespWidth=6` 路。
- `EntryTable` 对 bitmap 字段做 OR 合并，但 `exceptionNumber/faultElemIdx` 只能写一份；当前用 `PriorityMux` 按 update lane 顺序选择一个异常更新。
- 这个优先级选择保证同拍多路异常命中同一 entry 时写入确定，但不等价于自动选择最小 `faultElemIdx`。

异常写回行为:

- `excp` entry 只有在 `faultElemIdx` 之前的 older in-flight byte 都不再 pending 后才允许写回。
- `robWriteback.exception = true` 时携带 `exceptionNumber`、`faultElemIdx`、`faultVstart`。
- `faultVstart = (uopIdx << elemNum(deew)) + (faultElemIdx >> deew)`。

---

## 5. 握手约定

### 5.1 Decoupled

`Decoupled` 信号只有 `valid && ready` 同时为真才 fire。

| 接口 | 方向 | fire 后含义 |
|---|---|---|
| `addrUop` | VAGQ input | 地址侧字段写入 entry |
| `dataUop` | VAGQ input | 数据侧字段写入 entry |
| `lsuReq` | VAGQ output | active 请求发出，EntryTable 置位 `reqSent` |
| `lsqEmptyReq` | VAGQ output | empty 请求发出，EntryTable 置位 `reqSent` |
| `vrfReadReq` | VAGQ output | store-data read 或 load merge read 请求被接受 |
| `robWriteback` | VAGQ output | 写回被接受，entry 释放 |

### 5.2 Valid

`Valid` 信号没有 ready，接收端必须在 valid 当拍采样或自行过滤。

| 接口 | 说明 |
|---|---|
| `lduResp` | active load 请求 response，MergeCtrl 用 `entryIdx + robIdx` 过滤 |
| `staResp` | active store 请求 response，MergeCtrl 用 `entryIdx + robIdx` 过滤 |
| `lsqEmptyResp` | empty 请求 response，MergeCtrl 用 `entryIdx + robIdx` 过滤 |
| `vrfReadResp` | VRF read response，SplitCtrl/MergeCtrl 分别用 pending read entry 和 `robIdx` 过滤 |
| `vrfWriteReq` | merge 写回请求，当前是 `ValidIO`，没有 ready 反压；`MergeCtrl` 在 valid 当拍推进 entry 到 `wb` |
| `redirect` | redirect/flush 广播，EntryTable/SplitCtrl/MergeCtrl 都会过滤 |
| `splitUpdate` | SplitCtrl 到 EntryTable 的 bitmap 更新 |
| `mergeReqUpdate` | MergeCtrl 到 EntryTable 的 response bitmap 更新 |
| `mergeStateUpdate` | MergeCtrl 到 EntryTable 的状态更新 |
| `reqUpdate` | MergeCtrl 内部输出给 EntryTable |
| `stateUpdate` | MergeCtrl 内部输出给 EntryTable |

### 5.3 `entryIdx + robIdx` 匹配

VAGQ entry 可能被释放后重新分配，因此 response 不能只依赖 `entryIdx`。

当前 `MergeCtrl` 接受 response 的条件中同时检查:

- `entryIdx` 命中合法 entry
- entry 当前 `valid`
- entry 当前 `robIdx == resp.robIdx`

`SplitCtrl` 和 `MergeCtrl` 的 VRF read response 路径也保存 pending `entryIdx + robIdx`，用来防止旧响应污染新 entry。

### 5.4 `reqSent / reqAck` byte 状态

每个 byte lane 的请求状态由两个 bit 表示。

| 状态 | `reqSent` | `reqAck` | 含义 |
|---|---:|---:|---|
| IDLE | 0 | 0 | 未发请求 |
| SENT | 1 | 0 | 已发请求，等待 ACK/NACK/exception |
| DONE | X | 1 | 已完成 |

状态转移:

- `SplitCtrl` 发出请求后置位 `reqSent`。
- 正常 ACK 置位 `reqAck`。
- NACK 清除 `reqSent`，允许后续重发。
- exception 记录异常信息，并使 entry 进入 `excp`。

### 5.5 LSQ empty mark 语义

`lsqEmptyReq` 只用于 non-active byte，包括 prestart、mask-off inactive 和 tail。active byte 必须走 `lsuReq`，不应该走 empty mark。

`emptyMask` 和 `entryMask` 的职责不同:

| 字段 | 粒度 | 用途 |
|---|---|---|
| `emptyMask` | VAGQ byte 级 | response 返回给 VAGQ 后更新 `reqSent/reqAck` |
| `entryMask` | LSQ entry/元素级 | LSQ 标记 `lqIdx/sqIdx + i` 对应的预留项 |

LSQWrapper 会根据 `isLoad/isStore` 分发:

- load empty mark 进入 LoadQueue，成功后置对应 LQ entry 的 `committed`。
- store empty mark 进入 StoreQueue，成功后置对应 SQ entry 的 `vecInactive`。
- LQ/SQ 都会检查目标项真的 match；如果索引错误、entry 已释放、`robIdx` 或 base `lqIdx/sqIdx` 不匹配、或被 flush/cancel，则 `emptyMarkSuccess=false`。
- `LSQWrapper` 用 `isNACK := !emptyMarkSuccess` 返回 VAGQ。VAGQ 收到 NACK 后清除对应 `reqSent`，允许后续重试。
- 当前 `MemBlock.scala` 已通过 `VAGQDownstreamAdapter` 将 `vagq.io.lsqEmptyReq` 接到 `lsq.io.lsqEmptyReq`，并把 `lsq.io.lsqEmptyResp` 返回 VAGQ。

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

辅助判断:

- `isLoad = !uopType(0)`
- `isStore = uopType(0)`
- `isStride = !uopType(2) && !uopType(1)`
- `isIndexed = uopType(2)`
- `isOrdered = uopType(1)`

---

## 7. 当前系统级接入状态

### 7.1 `VAGQDownstreamAdapter`

定义位置: `mem/vector/VAGQDownstreamAdapter.scala`

| 信号 | 方向 | 类型 | 说明 |
|---|---|---|---|
| `vagqLsuReq` | input decoupled vec | `Flipped(Vec(ActiveIssueWidth, Decoupled[VAGQLsuReq]))` | 来自 VAGQ 的 active req，目前只处理其中的 load req |
| `vagqLduResp` | output valid vec | `Vec(LduRespWidth, Valid[VAGQResp])` | 返回 VAGQ 的 LDU response，当前直接透传 `lduResp` |
| `vagqStaResp` | output valid vec | `Vec(StaRespWidth, Valid[VAGQResp])` | 返回 VAGQ 的 STA response，当前全部置 invalid |
| `issueLda` | input decoupled mixed vec | `Flipped(MixedVec(loadParams.map(Decoupled[ExuInput])))` | 普通 LduIQ 发往 LDU 的请求 |
| `lduReq` | output decoupled mixed vec | `MixedVec(loadParams.map(Decoupled[ExuInput]))` | 仲裁后送入 `NewLoadUnit.io.ldin` 的请求 |
| `lduReqMeta` | output | `Vec(loadParams.length, VAGQMemPipelineMeta)` | 送入 LoadUnit 的 VAGQ metadata；普通 load 为 0 |
| `lduResp` | input valid vec | `Flipped(Vec(LduRespWidth, Valid[VAGQResp]))` | 来自各 LoadUnit 的 VAGQ response |
| `vagqLsqEmptyReq` | input decoupled | `Flipped(Decoupled[VAGQLsqEmptyReq])` | 来自 VAGQ 的 LSQ-empty req |
| `vagqLsqEmptyResp` | output valid | `Valid[VAGQLsqEmptyResp]` | 返回 VAGQ 的 LSQ-empty resp |
| `lsqEmptyReq` | output decoupled | `Decoupled[VAGQLsqEmptyReq]` | 发给 `LSQWrapper` 的 LSQ-empty req |
| `lsqEmptyResp` | input valid | `Flipped(Valid[VAGQLsqEmptyResp])` | 来自 `LSQWrapper` 的 LSQ-empty resp |

当前 adapter 行为:

- 对 lane0/lane1，`activeReq.valid && activeReq.bits.isLoad` 才参与 load 仲裁。
- `selectActive = activeLoadValid && (!issueLda(i).valid || isAfter(issueLda(i).bits.robIdx, activeReq.bits.robIdx))`。
- 也就是说 VAGQ active load req0 只和普通 `issueLda0` 仲裁，VAGQ active load req1 只和普通 `issueLda1` 仲裁。
- `issueLda2` 不参与 VAGQ 仲裁，直接透传到 `LoadUnit2`。
- 被选中的 VAGQ load 被转成 `FuType.vldu` 的 `ExuInput`，`src(0)=vaddr`，`fuOpType` 由 `alignedType` 映射成 `vle8/vle16/vle32/vle64`，并填入 `robIdx/pdest/lqIdx/sqIdx/vecWen`。
- `lduReqMeta` 同拍携带 `entryIdx/robIdx/isLoad/byteOffset/mask`，LoadUnit 后续用它生成 `VAGQResp`。
- store active req 当前没有转成 STA input，因此 VAGQ store req 会因为没有对应 ready/resp 而无法端到端完成。
- LSQ-empty req/resp 当前只做透传，真正 match/ACK/NACK 由 `LSQWrapper`、LoadQueue、StoreQueue 完成。

### 7.2 `VAGQMemPipelineMeta`

定义位置: `mem/pipeline/Bundles.scala`

| 字段 | 类型 | 说明 |
|---|---|---|
| `valid` | `Bool` | 当前 LoadUnit/StoreUnit pipeline entry 是否来自 VAGQ |
| `entryIdx` | `UInt(VAGQEntryIdxWidth.W)` | VAGQ entry index |
| `robIdx` | `RobPtr` | VAGQ request 对应 ROB index |
| `isLoad` | `Bool` | request 是否为 load |
| `isStore` | `Bool` | request 是否为 store |
| `byteOffset` | `UInt(FlowByteWidth.W)` | active 元素在 16B flow 内的 byte offset |
| `mask` | `UInt(FlowBytes.W)` | active 元素覆盖的 byte mask |

### 7.3 MemBlock 当前连线

定义位置: `mem/MemBlock.scala`

- `MemBlock` 当前例化 `val vagq = Module(new VAGQ)` 和 `val vagqDownstream = Module(new VAGQDownstreamAdapter(ldaParams))`。
- `vagq.io.addrUop.valid := false.B`，`vagq.io.dataUop.valid := false.B`，所以上游尚未向 VAGQ 注入真实 uop。
- `vagq.io.vrfReadReq.ready := false.B`，`vagq.io.vrfReadResp.valid := false.B`，所以 VAGQ store-data read 和 load merge old-vd read 尚未接入系统 VRF。
- `vagq.io.robWriteback.ready := false.B`，所以 VAGQ 完成/异常写回尚未接入 ROB/release。
- `vagqDownstream.io.vagqLsuReq <> vagq.io.lsuReq`，active req 进入 downstream adapter。
- `vagq.io.lduResp := vagqDownstream.io.vagqLduResp`，LDU response 返回 VAGQ。
- `vagq.io.staResp := vagqDownstream.io.vagqStaResp`，但 adapter 当前把 STA response 全部置 invalid。
- `vagqDownstream.io.vagqLsqEmptyReq <> vagq.io.lsqEmptyReq`，LSQ-empty req 进入 downstream adapter。
- `lsq.io.lsqEmptyReq <> vagqDownstream.io.lsqEmptyReq`，LSQ-empty path 已接到 LSQWrapper。
- `vagqDownstream.io.lsqEmptyResp := lsq.io.lsqEmptyResp`，LSQ-empty ACK/NACK 已返回 VAGQ。

### 7.4 LoadUnit 当前 VAGQ 行为

定义位置: `mem/pipeline/NewLoadUnit.scala`

- LoadUnit S0 将 `io.vagqReqMeta` 写入 pipeline bundle 的 `vagq` 字段。
- `isVAGQ = in.vagq.valid` 时，普通 vector load ROB writeback 被禁止，完成信息通过 `VAGQResp` 返回 VAGQ。
- VAGQ load 出现 replay/fast replay/RAR/matchInvalid 时不会走普通 LoadQueueReplay，而是返回 `isNACK=true`，由 VAGQ 清除 `reqSent` 后重发。
- VAGQ load 成功时会通过现有普通 vector load VRF 写口写 active 数据；数据会左移 `byteOffset * 8` 对齐到 16B flow 内位置。
- `VAGQResp.data` 当前也填入对齐后的 load data，但 VAGQ core 的 `MergeCtrl` 不收集 active load data。
- VAGQ load 成功且无异常时会让 LQ 更新地址有效；异常时 `exceptionNumber` 来自 `ExceptionNO.priorities`。

### 7.5 StoreUnit 当前 VAGQ 行为

定义位置: `mem/pipeline/NewStoreUnit.scala`

- StoreUnit pipeline 已有 `vagqReqMeta` 输入和 `vagqResp` 输出。
- S3 能根据 `in.vagq.valid` 生成 VAGQ store response，NACK 条件包括 TLB miss 或 RS replay，异常号来自 store-side exception vector。
- 当前 `MemBlock` 仍将 `stu.io.vagqReqMeta := 0.U.asTypeOf(stu.io.vagqReqMeta)`，`VAGQDownstreamAdapter` 也没有输出 STA request，所以这段 StoreUnit VAGQ 支持尚未端到端启用。

---

## 8. 当前实现注意点

- `VAGQDataSideUop` 携带 `op2Data` 和 `psrc2`，其中 `op2Data` 是 stride/index 地址生成使用的第二操作数数据。
- store data 当前由 `SplitCtrl` 通过 VAGQ 顶层 `vrfReadReq/vrfReadResp` 读取 `psrc2` 得到。
- `VAGQLsuReq` 当前没有携带完整 `DynInst`；当前 load adapter 只重建了 LDU 所需的最小 `ExuInput` 字段，STA/debug/trigger/完整异常上下文仍可能需要继续补齐。
- `VAGQResp.data` 当前定义存在，但核心 `MergeCtrl` 没有把 active load data 收集进 entry；当前 merge 只处理非 active byte 的旧 `vd` / agnostic 写回。
- `nf` 当前只在地址侧 uop、entry、`VAGQLsuReq` 中透传，没有驱动 segment 维度的额外拆分。
- `SplitCtrl` 当前 active 路径是两路固定实现，empty 路径是一条 `lsqEmptyReq`，不是完全参数化的任意 lane split issue。
- `faultElemIdx` 命名上像元素序号，但当前存的是 16B flow 内 byte offset；`faultVstart` 才是元素级架构序号。
- `vrfWriteReq` 是 `ValidIO`，没有 ready 反压；系统级 VRF 写口接入时需要确认不会丢请求或需要另加 buffer/ready 协议。
- `EntryTable` 多路异常同拍命中时按 update lane 优先级选择异常，不保证选择最小 `faultElemIdx`。
- 当前 VAGQ 在 MemBlock 中已实例化，但上游、VRF、ROB 都 tie-off，因此还不是完整可执行的端到端通路。
