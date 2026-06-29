# VAGQ Requirements

来源：`VAGQ.md`、`vagq_skill.md`

## 范围与定位

- REQ-001: VAGQ 必须作为向量非连续访存微指令拆分与合并的统一模块。
- REQ-002: VAGQ 必须处理 constant-stride load/store 指令，包括 `vlse.v` 和 `vsse.v`。
- REQ-003: VAGQ 必须处理 indexed unordered load/store 指令，包括 `vluxei.v` 和 `vsuxei.v`。
- REQ-004: VAGQ 必须处理 indexed ordered load/store 指令，包括 `vloxei.v` 和 `vsoxei.v`。
- REQ-005: VAGQ 必须支持上述非连续访存指令的 segment 变体。
- REQ-006: VAGQ 不得处理 unit-stride load/store 指令，包括 `vle.v` 和 `vse.v`。
- REQ-007: VAGQ 不得处理 mask load/store 指令，包括 `vlm.v` 和 `vsm.v`。
- REQ-008: VAGQ 不得处理 whole-register load/store 指令，包括 `vlr.v` 和 `vsr.v`。
- REQ-009: VAGQ 不得处理 FOF load 指令 `vleff.v`。
- REQ-010: 不进入 VAGQ 的连续向量 load 必须走 LduIQ 到 LDU 的通路。
- REQ-011: 不进入 VAGQ 的连续向量 store 必须走 StaIQ 到 STA 的通路。
- REQ-012: VAGQ 必须建立后端流水线与访存流水线之间的元素粒度请求桥接关系。
- REQ-013: VAGQ 生成的访存请求必须能够利用现有 LDU 和 STA 流水线。

## 参数与容量

- REQ-014: VAGQ 的 `VLEN` 必须按 128 bit 设计。
- REQ-015: VAGQ 的 `VLENB` 必须等于 `VLEN / 8`，即 16 byte。
- REQ-016: VAGQ 的 `uvlWidth` 必须等于 `log2(VLENB)`，即 4。
- REQ-017: VAGQ 表项数 `VAGQSize` 必须是可配置参数。
- REQ-018: `VAGQSize` 的建议配置必须支持 4 或 8 个表项。
- REQ-019: 每个 VAGQ 表项必须能够追踪最多 16 个字节粒度访存请求。

## 微指令拆分

- REQ-020: 所有进入 VAGQ 的向量访存指令必须在 dispatch 阶段拆分为地址侧微指令和数据侧微指令。
- REQ-021: Constant-stride load 必须拆分为地址侧 `vlsa` 和数据侧 `vlss`。
- REQ-022: Constant-stride store 必须拆分为地址侧 `vssa` 和数据侧 `vsss`。
- REQ-023: Indexed load 必须拆分为地址侧 `vlxa` 和数据侧 `vlxi`。
- REQ-024: Indexed store 必须拆分为地址侧 `vsxa` 和数据侧 `vsxi`。
- REQ-025: 地址侧微指令必须进入 StaIQ。
- REQ-026: Constant-stride 数据侧微指令必须进入 StdIQ。
- REQ-027: Indexed 数据侧微指令必须进入 VStdIQ。
- REQ-028: 地址侧微指令必须携带 `rs1` 基地址源信息。
- REQ-029: 地址侧微指令必须携带 `v0` 掩码源信息。
- REQ-030: 地址侧微指令必须携带 `vl` 信息。
- REQ-031: 地址侧微指令必须携带 `vtype` 信息。
- REQ-032: 地址侧微指令必须携带 `nf` 信息。
- REQ-033: 地址侧微指令必须携带 `uopIdx` 信息。
- REQ-034: 地址侧微指令必须携带 `entryIdx` 信息。
- REQ-035: Constant-stride 数据侧微指令必须携带 `rs2` stride 数据源信息。
- REQ-036: Indexed 数据侧微指令必须携带 `vs2` index 数据源信息。
- REQ-037: 数据侧微指令必须携带 `vs3` 数据源信息。
- REQ-038: 数据侧微指令必须携带 `entryIdx` 信息。
- REQ-039: 地址侧和数据侧微指令必须独立追踪各自操作数依赖。
- REQ-040: 一条向量访存指令必须先拆分为 `LMUL` 或 `max(emul, lmul)` 条 uop。
- REQ-041: 每条 uop 必须覆盖 `VLEN / deew` 个元素。
- REQ-042: 每条 uop 必须对应一个 VAGQ 表项。

## VecOrderQueue 与表项分配

- REQ-043: VecOrderQueue 必须在 dispatch 阶段为每条 VAGQ uop 预分配 VAGQ 表项。
- REQ-044: VecOrderQueue 必须为每条 VAGQ uop 分配 `entryIdx`。
- REQ-045: `entryIdx` 必须写入同一 VAGQ uop 派生出的全部地址侧和数据侧微指令。
- REQ-046: VAGQ 微指令发射时必须携带 `entryIdx`。
- REQ-047: VAGQ 必须使用 `entryIdx` 直接索引目标表项。
- REQ-048: VAGQ 表项释放必须由 ROB commit 触发。
- REQ-049: VecOrderQueue 必须随 VAGQ 表项释放同步回收对应 `entryIdx`。
- REQ-050: 当 VAGQ 表项不足时，前端准入或相关 IQ 必须被反压。

## 表项字段

- REQ-051: VAGQ 表项必须包含 `valid` 字段表示表项是否有效。
- REQ-052: VAGQ 表项必须包含 `uopType` 字段区分 stride load、stride store、indexed unordered load、indexed unordered store、indexed ordered load、indexed ordered store。
- REQ-053: VAGQ 表项必须包含 `robIdx` 字段用于仲裁、flush 和提交相关判断。
- REQ-054: VAGQ 表项必须包含 `pdest` 字段表示 load 的目标 Vec Regfile 物理寄存器。
- REQ-055: Store 表项中的 `pdest` 字段可为 don't care。
- REQ-056: VAGQ 表项必须包含 `psrc2` 字段表示 `vs3` 对应的物理寄存器号。
- REQ-057: Load 表项中的 `psrc2` 必须表示旧 `vd`，用于合并 prestart、inactive 和 tail 字节。
- REQ-058: Store 表项中的 `psrc2` 必须表示待存储数据源。
- REQ-059: VAGQ 表项必须包含 64 bit `baseAddr` 字段。
- REQ-060: `baseAddr` 必须保存完整 XLEN 基地址，高位不得被舍弃。
- REQ-061: VAGQ 表项必须包含 128 bit `op2Data` 字段。
- REQ-062: Constant-stride 表项的 `op2Data[63:0]` 必须保存 `rs2` stride 值。
- REQ-063: Indexed 表项的 `op2Data` 必须保存 `vs2` index 向量数据。
- REQ-064: 第二操作数字段必须命名为 `op2Data`，不得命名为 `stride`。
- REQ-065: VAGQ 表项必须包含 `ieew` 字段表示 indexed element width。
- REQ-066: `ieew` 不得与数据元素宽度混用。
- REQ-067: VAGQ 表项必须包含 `deew` 字段表示 data element width。
- REQ-068: `deew` 不得与 CSR `sew` 混用命名。
- REQ-069: VAGQ 表项必须包含 `uvlByte` 字段表示本 uop 内有效字节数。
- REQ-070: `uvlByte` 的取值范围必须覆盖 0 到 `VLENB`。
- REQ-071: VAGQ 表项必须包含 `useVstart` 字段。
- REQ-072: `useVstart=1` 时，MaskGen 必须从 CSR 读取 `vstart`。
- REQ-073: `useVstart=0` 时，MaskGen 必须按 `vstart=0` 处理。
- REQ-074: VAGQ 表项不得存储 `vstart` 本身。
- REQ-075: VAGQ 表项必须包含 `vma` 字段。
- REQ-076: VAGQ 表项必须包含 `vta` 字段。
- REQ-077: VAGQ 表项必须包含 `uopIdx` 字段。
- REQ-078: VAGQ 表项必须包含 16 bit `elemActiveMask` 字段。
- REQ-079: `elemActiveMask` 必须是预计算的字节掩码。
- REQ-080: VAGQ 表项必须包含 `nf` 字段。
- REQ-081: VAGQ 表项必须包含 16 bit `reqSent` 字段。
- REQ-082: VAGQ 表项必须包含 16 bit `reqAck` 字段。
- REQ-083: VAGQ 表项必须包含 `exceptionNumber` 字段。
- REQ-084: `exceptionNumber=0` 必须表示无异常。
- REQ-085: VAGQ 表项必须包含 `faultElemIdx` 字段。
- REQ-086: VAGQ 表项必须包含 `state` 字段。

## 表项状态机

- REQ-087: `valid=0` 必须表示 VAGQ 表项空闲。
- REQ-088: 当 `valid=0` 时，表项 `state` 编码必须视为无效或 don't care。
- REQ-089: `state` 仅在 `valid=1` 时有效。
- REQ-090: VAGQ 表项状态机必须支持 `s_waitA` 状态。
- REQ-091: `s_waitA` 必须表示等待地址侧微指令。
- REQ-092: VAGQ 表项状态机必须支持 `s_waitSI` 状态。
- REQ-093: `s_waitSI` 必须表示等待 stride/index 数据侧微指令。
- REQ-094: VAGQ 表项状态机必须支持 `s_split` 状态。
- REQ-095: `s_split` 必须表示地址侧和数据侧微指令已收齐且正在拆分请求。
- REQ-096: VAGQ 表项状态机必须支持 `s_merge` 状态。
- REQ-097: `s_merge` 仅用于 load 表项。
- REQ-098: `s_merge` 必须表示全部请求已 ACK 且正在读取旧 `vd` 进行非 active 字节合并。
- REQ-099: VAGQ 表项状态机必须支持 `s_wb` 状态。
- REQ-100: `s_wb` 必须表示 load 合并写回或 store 指令完成写回 ROB。
- REQ-101: VAGQ 表项状态机必须支持 `s_excp` 状态。
- REQ-102: `s_excp` 必须表示收到异常 ACK 后停止拆分并等待异常写回。
- REQ-103: 空闲表项收到地址侧微指令且未收到数据侧微指令时，必须进入 `s_waitSI` 或等价的“等待数据侧”状态。
- REQ-104: 空闲表项收到数据侧微指令且未收到地址侧微指令时，必须进入 `s_waitA` 或等价的“等待地址侧”状态。
- REQ-105: 空闲表项同时收到地址侧和数据侧微指令时，必须进入 `s_split`。
- REQ-106: 等待地址侧的表项收到地址侧微指令后，必须进入 `s_split`。
- REQ-107: 等待数据侧的表项收到数据侧微指令后，必须进入 `s_split`。
- REQ-108: Load 表项在 `s_split` 且 `reqAck == 16'hFFFF` 时，必须进入 `s_merge` 或在无需 merge 时直接进入 `s_wb`。
- REQ-109: Store 表项在 `s_split` 且 `reqAck == 16'hFFFF` 时，必须进入 `s_wb`。
- REQ-110: 表项在 `s_split` 收到 LSU 异常 ACK 时，必须进入 `s_excp`。
- REQ-111: 表项在 `s_merge` 收到 VRF 读数据后，必须进入 `s_wb`。
- REQ-112: 表项在 `s_wb` 完成写回后，必须清除 `valid`。
- REQ-113: 表项在 `s_excp` 完成异常写回后，必须清除 `valid`。
- REQ-114: redirect 命中表项时，必须清除该表项 `valid`。

## 字节级请求状态

- REQ-115: 每个字节的请求状态必须由 `reqSent` 和 `reqAck` 两个 bitmap 编码。
- REQ-116: 字节请求状态必须支持 IDLE、SENT 和 DONE 三种状态。
- REQ-117: IDLE 状态必须编码为 `reqSent=0` 且 `reqAck=0`。
- REQ-118: SENT 状态必须编码为 `reqSent=1` 且 `reqAck=0`。
- REQ-119: DONE 状态必须编码为 `reqAck=1`，且 `reqSent` 为 don't care。
- REQ-120: 表项进入 `s_split` 时，所有字节的 `reqSent` 必须初始化为 0。
- REQ-121: 表项进入 `s_split` 时，所有字节的 `reqAck` 必须初始化为 0。
- REQ-122: Active 字节被选出发送给 LSU 时，对应字节必须从 IDLE 转为 SENT。
- REQ-123: 非 active 字节被选出发送给 LSQ 空项标记路径时，对应字节必须从 IDLE 转为 SENT。
- REQ-124: SENT 字节收到 LSU 或 LSQ ACK 时，必须转为 DONE。
- REQ-125: SENT 字节收到 LSU 或 LSQ NACK 时，必须清除对应 `reqSent` 并转回 IDLE。
- REQ-126: NACK 后的 IDLE 字节必须允许后续自动重发。
- REQ-127: 完成判定必须使用 `reqAck == 16'hFFFF`。
- REQ-128: LSU 和 LSQ 路径必须统一使用 `reqSent` 和 `reqAck` 进行状态追踪。
- REQ-129: `reqSent` 必须表示对应字节的请求已经发送至 LSU 或 LSQ。
- REQ-130: `reqAck` 必须表示对应字节的请求已经由 LSU 或 LSQ 确认完成。
- REQ-131: ADONE 和 NDONE 类型完成都必须表现为对应 `reqAck=1`。

## SplitCtrl

- REQ-132: SplitCtrl 必须负责控制元素或字节粒度请求拆分节奏。
- REQ-133: SplitCtrl 不得随 `VAGQSize` 复制。
- REQ-134: SplitCtrl 必须能够暂停发射以响应下游 LDU 或 STA busy。
- REQ-135: SplitCtrl 必须优先编码 `~reqSent & elemActiveMask` 选择下一个发往 LSU 或 STA 的 active 请求。
- REQ-136: SplitCtrl 必须优先编码 `~elemActiveMask & ~reqSent & ~reqAck` 选择下一个发往 LSQ 的非 active 空项标记请求。
- REQ-137: SplitCtrl 必须将非 active tail 字节也作为 LSQ 空项标记请求处理。
- REQ-138: SplitCtrl 在收到 NACK 后必须允许对应字节重新进入待发送集合。
- REQ-139: VAGQ 同时活跃处理的 entry 数量目标必须限制为 1 到 2 个，以共享 SplitCtrl 和 MergeCtrl。

## Ordered Indexed 保序

- REQ-140: `vloxei.v` 必须按元素顺序向内存发起访问。
- REQ-141: `vsoxei.v` 必须按元素顺序向内存发起访问。
- REQ-142: Ordered indexed 表项在 `(reqSent & ~reqAck & elemActiveMask) != 0` 时，必须暂停向 LSU 或 STA 发射新的 active 请求。
- REQ-143: Ordered indexed 表项必须依靠优先级编码保证从低元素到高元素的访问顺序。
- REQ-144: Ordered indexed 保序不得依赖额外计数器。

## MaskGen 与 active mask

- REQ-145: MaskGen 必须生成 16 bit 字节粒度 `elemActiveMask`。
- REQ-146: MaskGen 必须是组合逻辑。
- REQ-147: MaskGen 不得随 `VAGQSize` 复制。
- REQ-148: `vm` 只能在 MaskGen 阶段使用，不得存入 VAGQ 表项。
- REQ-149: `v0` 只能在 MaskGen 阶段使用，不得存入 VAGQ 表项。
- REQ-150: 地址侧微指令携带的 `v0` 必须锁存一拍用于 MaskGen 计算。
- REQ-151: `elemActiveMask` 生成后，VAGQ 必须不再依赖 `vm` 和 `v0`。
- REQ-152: MaskGen 必须根据 `vl`、`vstart`、`vm` 和 `v0` 一次性计算 `elemActiveMask`。
- REQ-153: MaskGen 必须优先处理 prestart 字节。
- REQ-154: MaskGen 必须在 prestart 之后处理 tail 字节。
- REQ-155: MaskGen 必须在 prestart 和 tail 之后处理 mask inactive 字节。
- REQ-156: prestart 字节必须在 `elemActiveMask` 中置为 0。
- REQ-157: tail 字节必须在 `elemActiveMask` 中置为 0。
- REQ-158: 当 `vm=1` 且字节不属于 prestart 或 tail 时，该字节必须在 `elemActiveMask` 中置为 1。
- REQ-159: 当 `vm=0` 且字节不属于 prestart 或 tail 时，该字节 active 状态必须由 `v0Mask[byteIdx / (deew/8)]` 决定。
- REQ-160: Store 指令只能对 `elemActiveMask=1` 的字节发起真实 store 请求。

## uvlByte 与 uvstartByte

- REQ-161: `uvlByte` 必须表示 `vl` 对应总字节范围与当前 uop 字节区间的交集长度。
- REQ-162: `vlByte` 必须按 `vl * (deew/8)` 计算。
- REQ-163: 当 `uopIdx` 小于 `vlByte` 的商部分时，`uvlByte` 必须等于 `VLENB`。
- REQ-164: 当 `uopIdx` 等于 `vlByte` 的商部分时，`uvlByte` 必须等于 `vlByte mod VLENB`。
- REQ-165: 当 `uopIdx` 大于 `vlByte` 的商部分时，`uvlByte` 必须等于 0。
- REQ-166: `uvlByte` 计算必须使用 bit 切片获得商和余数，不得使用除法器。
- REQ-167: `uvstartByte` 必须表示 `vstart` 对应字节位置与当前 uop 字节区间的交集长度。
- REQ-168: 当 `useVstart=1` 时，`vstartByte` 必须按 `CSR.vstart * (deew/8)` 计算。
- REQ-169: 当 `useVstart=0` 时，`vstartByte` 必须按 0 处理。
- REQ-170: 当 `uopIdx` 小于 `vstartByte` 的商部分时，`uvstartByte` 必须等于 `VLENB`。
- REQ-171: 当 `uopIdx` 等于 `vstartByte` 的商部分时，`uvstartByte` 必须等于 `vstartByte mod VLENB`。
- REQ-172: 当 `uopIdx` 大于 `vstartByte` 的商部分时，`uvstartByte` 必须等于 0。
- REQ-173: `uvstartByte` 计算必须与 `uvlByte` 同构。
- REQ-174: `uvstartByte` 计算必须使用 bit 切片获得商和余数，不得使用除法器。

## 地址生成

- REQ-175: AddrGen 必须是组合逻辑。
- REQ-176: AddrGen 不得随 `VAGQSize` 复制。
- REQ-177: Constant-stride 地址生成必须支持以原始 `baseAddr=x[rs1]` 计算地址的方案。
- REQ-178: Constant-stride 原始 base 方案中，offset 必须按 `op2Data * (uopIdx * elemNum + elemIdx)` 计算。
- REQ-179: Constant-stride 原始 base 方案中，地址必须按 `baseAddr + offset` 计算。
- REQ-180: Constant-stride 地址生成必须支持预计算 `baseAddr` 的方案。
- REQ-181: Constant-stride 预计算 base 方案中，`baseAddr` 必须按 `x[rs1] + x[rs2] * uopIdx * elemNum` 计算。
- REQ-182: Constant-stride 预计算 base 方案中，offset 必须按 `op2Data * elemIdx` 计算。
- REQ-183: Constant-stride 预计算 base 方案中，地址必须按 `baseAddr + offset` 计算。
- REQ-184: `elemNum` 必须等于 `VLEN / deew`。
- REQ-185: Indexed 地址生成必须从 `op2Data` 中按 `elemIdx` 选择 index offset。
- REQ-186: Indexed 地址生成在 `ieew < 64` 时必须将 offset zero-extend 到 64 bit。
- REQ-187: Indexed 地址生成在 `ieew = 64` 时必须直接使用 64 bit offset。
- REQ-188: Indexed 地址生成必须按 `baseAddr + offset` 计算地址。
- REQ-189: Indexed 地址加法器必须执行 64 bit 加法。
- REQ-190: Stride 地址加法器必须执行 64 bit 加法。

## Segment 处理

- REQ-191: Segment 指令的 `nf=2..8` 必须在 VAGQ 中与非 segment 指令使用相同处理流程。
- REQ-192: VAGQ 不得按 `nf` 对 segment 指令展开额外循环。
- REQ-193: VAGQ 不得为 segment 指令引入双层迭代逻辑。
- REQ-194: VAGQ 必须按元素粒度拆分 segment 请求。
- REQ-195: `nf` 字段在 VAGQ 表项中只允许存储和透传。
- REQ-196: VAGQ 不得使用 `nf` 改变拆分流程。
- REQ-197: Segment 的内存交错布局到寄存器分散布局转换必须由下游 Gather 阵列负责。
- REQ-198: VAGQ 不得负责 segment zip 或 unzip。

## Load 数据路径与合并

- REQ-199: Active load 元素的数据必须由 LDU pipeline 直接写入 Vec Regfile。
- REQ-200: LDU 写入 active load 数据时必须使用 `pdest` 和对应 byte mask。
- REQ-201: VAGQ 不得缓存 active load 数据。
- REQ-202: 非 active load 字节必须由 VAGQ 在 merge 阶段处理。
- REQ-203: 非 active load 字节必须包括 prestart、mask-inactive 和 tail 字节。
- REQ-204: Load 表项进入 merge 阶段时，VAGQ 必须向 Vec Regfile 发起旧 `vd` 读取请求。
- REQ-205: 旧 `vd` 读取必须使用 `psrc2` 指向的物理寄存器。
- REQ-206: MergeCtrl 必须仅对 `elemActiveMask=0` 的字节生成 VAGQ 写回 mask。
- REQ-207: MergeCtrl 必须对 `elemActiveMask=1` 的字节禁止 VAGQ 写入，避免覆盖 LDU 已写数据。
- REQ-208: MergeCtrl 对非 active 字节必须使用旧 `vd` 数据写入 `pdest`。
- REQ-209: VAGQ 对非 active 字节的写入 mask 必须与 LDU active 写入 mask 互补。
- REQ-210: 当 `vma=0` 且 `vta=0` 且无 prestart 字节时，VAGQ 必须允许跳过 merge 阶段。
- REQ-211: Load merge 完成后，表项必须进入写回 ROB 阶段。

## Store 数据路径

- REQ-212: Store active 元素必须由 STA 将 store 数据写入 StoreQueue。
- REQ-213: Store 数据必须来自 `vs3`。
- REQ-214: VAGQ 在拆分 store 请求时必须从 Vec Regfile 读取待存储 `vs3` 数据。
- REQ-215: VAGQ 必须按元素拆分 store 数据并随每个 store 请求发送。
- REQ-216: Store 表项不得进入 merge 阶段。
- REQ-217: Store 表项必须在所有请求完成后标记 uop 完成并写回 ROB。

## LSU/STA 交互

- REQ-218: VAGQ load 请求必须与 LduIQ 发出的标量 load 和连续向量 load 请求在 LoadUnit S0 前仲裁。
- REQ-219: Load 仲裁必须按 `robIdx` 选择更老请求。
- REQ-220: VAGQ store 请求必须与 StdIQ 发出的标量 store 和连续向量 store 请求在 StoreUnit S0 前仲裁。
- REQ-221: Store 仲裁必须按 `robIdx` 选择更老请求。
- REQ-222: LDU 完成 active load 请求后，必须向 VAGQ 返回 ACK。
- REQ-223: STA 或 StoreUnit 完成 active store 请求后，必须向 VAGQ 返回 ACK。
- REQ-224: LDU 或 STA 返回 ACK 时，VAGQ 必须置位对应 `reqAck`。
- REQ-225: LDU 或 STA 返回 NACK 时，VAGQ 必须清除对应 `reqSent`。
- REQ-226: TLB miss 或 replay 必须通过 NACK 触发 VAGQ 重发。

## LSQ 交互

- REQ-227: VAGQ load uop 必须在 dispatch 阶段预留 `VLEN / deew` 个 LoadQueue 表项。
- REQ-228: VAGQ store uop 必须在 dispatch 阶段预留 `VLEN / deew` 个 StoreQueue 表项。
- REQ-229: LoadQueue 表项不足时，dispatch 必须 stall。
- REQ-230: StoreQueue 表项不足时，dispatch 必须 stall。
- REQ-231: 非 active load 字节必须向 LoadQueue 发送空项标记请求。
- REQ-232: 非 active store 字节必须向 StoreQueue 发送空项标记请求。
- REQ-233: LSQ 空项标记请求必须消费 dispatch 阶段预留的对应 LSQ 表项。
- REQ-234: LSQ 空项标记请求不得发起真实访存。
- REQ-235: LSQ 空项标记 ACK 时，VAGQ 必须置位对应 `reqAck`。
- REQ-236: LSQ 空项标记 NACK 时，VAGQ 必须清除对应 `reqSent` 并允许重发。
- REQ-237: VAGQ 必须保证所有预留 LSQ 表项最终被真实访存请求或空项标记请求消费。

## 异常、非对齐与 redirect

- REQ-238: VAGQ 必须负责单条 uop 内的异常汇集。
- REQ-239: VAGQ 收到 LSU 或 StoreUnit 异常响应后，必须记录 `exceptionNumber`。
- REQ-240: VAGQ 收到 LSU 或 StoreUnit 异常响应后，必须记录 `faultElemIdx`。
- REQ-241: VAGQ 收到异常响应后，必须停止拆分新的请求。
- REQ-242: VAGQ 收到异常响应后，必须等待已发射请求完成。
- REQ-243: VAGQ 异常写回时必须向 ROB 或异常收集路径提供异常信息。
- REQ-244: Load page fault 必须使 VAGQ 记录 fault element 并在写回时设置 CSR `vstart = faultElemIdx`。
- REQ-245: Store page fault 必须按与 load page fault 同类机制处理。
- REQ-246: Access fault 必须按异常请求机制处理。
- REQ-247: VAGQ 不得处理 address misalign。
- REQ-248: Address misalign 必须由 LSU MisalignBuffer 或 Store Misalign 相关路径处理。
- REQ-249: VAGQ 不得处理跨 4KB 页拆分。
- REQ-250: 跨 4KB 页必须由 StoreUnit 和 StoreQueue 等 LSU/LSQ 路径处理。
- REQ-251: VAGQ 发出的请求必须携带完整虚拟地址。
- REQ-252: VAGQ 发出的请求必须携带 byte mask。
- REQ-253: redirect 到达时，VAGQ 必须冲刷所有 `robIdx >= redirectRobIdx` 的表项。
- REQ-254: redirect 冲刷表项必须通过清除 `valid` 实现。
- REQ-255: 已发射到 LSU 或 STA 的请求必须由 LSU 或 STA 侧根据 `robIdx` 匹配冲刷。
- REQ-256: 若 redirect 只影响部分 uop，VAGQ 必须只清除对应表项。

## ROB 与写回

- REQ-257: VAGQ 每条 uop 必须在 `s_wb` 状态向 ROB 发送写回信号。
- REQ-258: ROB 必须记录一条 VAGQ 向量访存指令需要的 uop 写回次数。
- REQ-259: ROB 必须在收齐该指令全部 `LMUL` 个 uop 写回后标记指令完成。
- REQ-260: ROB 标记指令完成后才允许该向量访存指令 commit。
- REQ-261: Store uop 完成所有请求后必须写回 ROB，但不得写 Vec Regfile。
- REQ-262: Load uop 完成 active 数据写入和必要 merge 后必须写回 ROB。

## 子模块与接口约束

- REQ-263: VAGQ 必须包含 `VAGQEntry` 表项数组。
- REQ-264: VAGQ 必须包含 AddrGen 子模块或等价组合逻辑。
- REQ-265: VAGQ 必须包含 MaskGen 子模块或等价组合逻辑。
- REQ-266: VAGQ 必须包含 SplitCtrl 控制逻辑。
- REQ-267: VAGQ 必须包含 MergeCtrl 控制逻辑。
- REQ-268: VAGQ 必须包含 VRFReadIF 或等价 VRF 读接口。
- REQ-269: VAGQ 必须包含 VRFWriteIF 或等价 VRF 写接口。
- REQ-270: VAGQ 必须与 VecOrderQueue 交互以接收或使用 `entryIdx`。
- REQ-271: VAGQ 必须与 StaIQ 交互以接收地址侧微指令。
- REQ-272: VAGQ 必须与 StdIQ 交互以接收 stride 数据侧微指令。
- REQ-273: VAGQ 必须与 VStdIQ 交互以接收 indexed 数据侧微指令。
- REQ-274: VAGQ 必须与 LDU 交互以发送 load 请求并接收 ACK、NACK、异常响应。
- REQ-275: VAGQ 必须与 STA 或 StoreUnit 交互以发送 store 请求并接收 ACK、NACK、异常响应。
- REQ-276: VAGQ 必须与 LoadQueue 交互以预留和消费 load queue 表项。
- REQ-277: VAGQ 必须与 StoreQueue 交互以预留和消费 store queue 表项。
- REQ-278: VAGQ 必须与 Vec Regfile 交互以读旧 `vd`、读 store `vs3` 或 indexed `vs2`、写 merge 结果。
- REQ-279: VAGQ 必须与 ROB 交互以提交 uop 写回和异常信息。

## 流控与反压

- REQ-280: VAGQ 表项满时，必须反压能够向 VAGQ 分配或写入表项的上游路径。
- REQ-281: 下游 LDU busy 时，VAGQ 必须暂停发射新的 load 请求。
- REQ-282: 下游 STA busy 时，VAGQ 必须暂停发射新的 store 请求。
- REQ-283: LSQ 空闲项不足时，dispatch 必须 stall 而不是允许 VAGQ 后续拆分中途耗尽 LSQ 表项。
- REQ-284: VAGQ 与 MemBlock 之间的请求交互必须支持滑动窗口式多请求在途。
