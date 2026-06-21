# XiangShan Integer Early Register Release Sparse UCA Spec

本文档给出在当前 XiangShan 后端上实现 **int-only non-speculative early register release** 的详细规格。设计基于 `mydocs/new-er/draft/int-early-register-release.md`，目标是在不修改 FP/Vector/V0/VL 释放协议的前提下，为 integer physical register file 增加 Tempranillo-like 的提前释放能力。

本文档是 RTL 实现前的 spec，不表示当前源码已经存在这些 Bundle 或 IO。文中给出的类名、字段名和端口名是推荐命名，实际实现时应尽量保持这些名字，方便 review、验证和后续性能分析。

## 1. 设计范围

### 1.1 必须实现的范围

- 只处理 integer architectural register 与 integer physical register，即 `Reg_I` / `IntPhyRegs`。
- 每个 tracked entry 绑定一个 integer destination allocation，因此最多维护 `L = IntERTrackEntries` 个 `UserCounterReg`。
- `src0/src1` 只决定 consumer source match、increment、read-done decrement、squash decrement 的事件数量，不决定 `UserCounterReg` 数量。
- 真实释放动作只能进入 `Rename.scala` 内的 integer free-list 路径，最终由 `MEFreeList` 接收。
- Difftest 的 commit timing 不提前。`DiffInstrCommit` 仍由 ROB commit 产生。
- Difftest 的 integer architectural register state 与 integer commit data 必须从 direct logical shadow 或 committed write-data 生成，不能再依赖 `pregs_xrf + rat_xrf` 或 `pregs_xrf[wpdest]`。

### 1.2 第一版不实现的范围

- 不处理 FP、Vector、V0、VL 物理寄存器提前释放。
- 不引入 value backup 或 late restore 机制。任何不能证明安全的场景都必须 fallback 到 conventional release。
- 不让 DataPath、IssueQueue 或 ROB 直接写 free list。
- 不要求第一版提前处理所有 memory resolved 场景。Load/store/fence/CSR/flush-sensitive 指令可以保守到 commit 才视为 non-speculative，或者直接使相关 tracked producer fallback。
- 不要求第一版支持所有 move elimination 优化。与 move elimination 相关的 early release 可以先 fallback，但 Difftest shadow RF 必须仍能正确处理 eliminated move commit。

### 1.3 与当前 XiangShan 的关键约束

当前源码中的关键事实：

- `Rename.scala` 使用 `MEFreeList(IntPhyRegs, RabCommitWidth)` 管理 integer physical register。
- conventional free 当前由 `RenameTableWrapper` 输出的 `rat.io.int_need_free` 和 `rat.io.int_old_pdest` 驱动，最终在 `Rename.scala` 连接到 `intFreeList.io.freeReq/freePhyReg`。
- `MEFreeList` 不是普通 FIFO。它维护 `headPtr`、`tailPtr`、`archHeadPtr`、snapshot，并有基于 arch RAT/free-list 关系的 debug invariant。
- `Rename.scala` 中最终 `psrc` 会经过同周期 rename bypass 修正；ER source match 必须使用最终 `io.out(i).bits.psrc`，不能使用 bypass 前的 `uops(i).psrc`。
- `psrc(0)`、`psrc(1)` 当前可以来自 integer RAT；`psrc(2)` 当前不从 integer RAT 读。RTL 不应硬编码 `2`，但第一版可以 `require(backendParams.numIntRegSrc == 2)`。
- STD IQ 的 store data source 由 `Region.scala` 将 STA 的 `src1` 复制到 STD 本地 `src0`。ER source metadata 必须一起复制，并保留原始 source index。
- `DataPath.scala` 中 `s0.fire && !s1_flush && !s0_ldCancel` 只表示进入 s1；更保守的 read-done 边界应结合 `og1resp.finalSuccess` 或等价 final-success 事件。
- `Rob.scala` 已有 `debug_exuData(robIdx)`，但它不能被无条件当作 direct diff xrf 的唯一数据源，因为 move elimination、skip-diff、fake write、exception、MMIO/NCIO 等路径需要单独定义。
- `difftest/src/main/scala/Preprocess.scala` 当前会从 `DiffPhyIntRegState("pregs_xrf")` 和 `DiffArchIntRenameTable("rat_xrf")` 合成 `xrf`，并用 `phyInts(coreID).value(c.wpdest)` 生成 `commit_data`。ER 开启后这两条路径都不能作为 integer architectural compare 的来源。

## 2. 正确性定义

### 2.1 释放条件

对某个 integer physical register allocation `p`，提前释放必须满足：

```text
RC4: 该 allocation 的所有正确路径 consumer 都已经获得该 source value，或者错误路径 consumer 已经被 squash 撤销。
RC3: 该 allocation 的 redefiner 已经 non-speculative，也就是 redefiner 以及所有更老指令不会再导致恢复到需要旧 p 值的状态。
```

ER 释放条件为：

```text
earlyRelease(e) =
  e.valid &&
  e.state === Counting &&
  !e.fallback &&
  e.producedReady &&
  e.redefinerNS &&
  e.userCounter === 0.U &&
  !e.earlyFreeIssued
```

其中 `userCounter === 0` 表示：

- all counted consumer source uses have readDone 或已被 squash 撤销；
- redefiner guard 已经由 speculation tracker 消耗。

### 2.2 UserCounter 语义

每个 tracked producer/result allocation 一个 counter：

```text
entry allocated: UC := 1
consumer source match and counted: UC += 1
consumer source read-done: UC -= 1
consumer source squash before read-done: UC -= 1
redefiner becomes non-speculative: UC -= 1
```

初始 `1` 是 redefiner guard，不是 consumer count。

同一条 uop 的多个 integer source 指向同一个 `psrc` 时，只能对同一个 tracked entry increment 一次。推荐规则：

```text
src0/src1 both match same trackId/gen:
  only lower srcIdx owns counted source metadata
  higher srcIdx.track.valid := false
```

### 2.3 Fallback 语义

`fallback` 表示该 tracked allocation 不再允许 early release，但仍要保持足够状态直到其 conventional release 边界：

- `fallback` entry 不再响应新的 source match。
- `fallback` entry 不产生 earlyFree。
- 如果已经记录 redefiner，等 redefiner commit 时由 conventional release 释放 old pdest。
- 如果 producer 或 redefiner 被 squash，entry 按 redirect/walk 清理。

进入 fallback 的条件包括但不限于：

- UC 饱和。
- 该 allocation 的 consumer source 走了第一版不支持的 read path，例如无法确认 final-success 的 replay-prone path。
- consumer 是 move elimination 且会把 source physical register 变成新的 architectural mapping。
- tracked source 出现在第一版不支持的 split/multi-uop/特殊提交路径。

以下场景在当前第一版不是 UCA fallback event，也不是稳定 fallback counter：

- producer-not-ready is not a first-version fallback event. UCA 在收到 guardDec 后保留 entry，并等待 producerReady 与 `UC == 0` 同时成立；int_er_fallback_producer_not_ready is deliberately unsupported in the current implementation.
- pending interrupt/trap/flush is handled as an ST stop/no-guardDec condition. ST 在这些边界前停止，不发 guardDec，也不修改 UCA entry；int_er_fallback_pending_interrupt is deliberately unsupported in the current implementation.

## 3. 配置参数

建议在 `XSCoreParams` 或 backend feature config 中加入以下参数。第一版默认全部关闭。

```scala
case class IntEarlyReleaseParams(
  enable: Boolean = false,
  observeOnly: Boolean = true,
  trackEntries: Int = 16,
  counterBits: Int = 4,
  genBits: Int = 4,
  earlyFreeWidth: Int = 1,
  stWalkWidth: Int = 2,
  readDoneQueueDepth: Int = 16,
  eventQueueDepth: Int = 16,
  allowSameCycleRenameBypassMatch: Boolean = true,
  allowNonIntSchedulerConsumers: Boolean = true,
  conservativeRedirectKill: Boolean = false,
  enableDiffShadowXRF: Boolean = true
)
```

推荐派生参数：

```scala
def IntERTrackEntries     = intER.trackEntries
def IntERTrackIdWidth     = log2Ceil(IntERTrackEntries)
def IntERCounterWidth     = intER.counterBits
def IntERTrackGenBits     = intER.genBits
def IntEREarlyFreeWidth   = intER.earlyFreeWidth
def IntERSTWalkWidth      = intER.stWalkWidth
def IntERMaxIntSrc        = backendParams.numIntRegSrc
def IntERRenameSrcWidth   = RenameWidth * backendParams.numIntRegSrc
```

第一版建议的 elaboration-time 检查：

```scala
require(IntERTrackEntries > 0)
require(isPow2(IntERTrackEntries))
require(IntERCounterWidth >= 3)
require(IntERTrackGenBits >= 3)
require(IntEREarlyFreeWidth >= 1)
require(backendParams.numIntRegSrc == 2, "current int ER assumes at most src0/src1 are integer RAT sources")
```

`IntERCounterWidth` 是 saturating counter 位宽，不需要覆盖理论上所有 consumer 数量。counter 饱和时 entry 进入 fallback，因此 3-bit/4-bit 都是合法设计点；推荐先用 4-bit 观察饱和比例，再决定是否扩大。

`observeOnly = true` 时：

- 完整建模 source match、UC update、ST opportunity、Difftest risk counters。
- 不向 `MEFreeList` 发出 early-free。
- 不 suppress conventional free。
- 用于阶段 0 收益与风险测量。

## 4. 新增公共 Bundle

建议新增文件：

```text
src/main/scala/xiangshan/backend/IntEarlyReleaseBundles.scala
```

或者放在 `xiangshan.backend.rename` 下，但 Bundle 会被 Rename、Dispatch、Issue、Region、DataPath、ROB 共同使用，放在 `xiangshan.backend` 包更方便。

### 4.1 `IntERSrcTrack`

consumer source 随 uop 向后端传递的 metadata。

```scala
class IntERSrcTrack(implicit p: Parameters) extends XSBundle {
  val valid    = Bool()
  val trackId  = UInt(IntERTrackIdWidth.W)
  val trackGen = UInt(IntERTrackGenBits.W)
  val srcIdx   = UInt(log2Ceil(backendParams.numSrc).W)
  val psrc     = UInt(PhyRegIdxWidth.W) // debug/assert only
}
```

语义：

- `valid` 表示该 source 已经在 rename 成功 increment 某个 UC。
- `trackId/trackGen` 是 read-done decrement 的唯一功能 key。
- `srcIdx` 必须是 original renamed uop 的 source index。STD IQ 本地 `src0` 复制自 STA `src1` 时，`srcIdx` 仍应保持 `1.U`。
- `psrc` 只用于 debug/assert，不用于功能匹配，避免 ABA。

### 4.2 `IntERSrcRobState`

ROB 中保存的 source 状态，支持 readDone 记录与 precise squash。

```scala
class IntERSrcRobState(implicit p: Parameters) extends IntERSrcTrack {
  val readDone = Bool()
}
```

语义：

- enqueue 时从 `IntERSrcTrack` 拷贝，`readDone := false.B`。
- ROB 收到 readDone event 且 `trackId/gen/srcIdx` 匹配时置位。
- ROB walk/squash 时，`valid && !readDone` 的 source 需要产生 squash decrement。

如果第一版选择 `conservativeRedirectKill = true`，则 ROB 可以不产生逐 source squash decrement，但仍建议保留 `readDone` debug 状态，便于后续切换到 precise recovery。

### 4.3 `IntERDestTrack`

producer allocation 的 metadata。

```scala
class IntERDestTrack(implicit p: Parameters) extends XSBundle {
  val valid    = Bool()
  val trackId  = UInt(IntERTrackIdWidth.W)
  val trackGen = UInt(IntERTrackGenBits.W)
  val pdest    = UInt(PhyRegIdxWidth.W)
}
```

语义：

- `valid` 表示当前 uop 的 integer destination allocation 被 sparse UCA 跟踪。
- `pdest` 必须是 final rename output `io.out(i).bits.pdest`，即 move elimination 和 same-cycle bypass 后的值。
- `isMove` 的 uop 第一版不得产生 `IntERDestTrack.valid`。

### 4.4 `IntERRedefTrack`

redefiner 与 old tracked pdest 的关联。

```scala
class IntERRedefTrack(implicit p: Parameters) extends XSBundle {
  val valid    = Bool()
  val trackId  = UInt(IntERTrackIdWidth.W)
  val trackGen = UInt(IntERTrackGenBits.W)
  val oldPdest = UInt(PhyRegIdxWidth.W)
}
```

语义：

- 若当前 uop 分配 integer destination，并且它的 old physical destination 命中某个 tracked entry，则 `valid := true`。
- ROB/ST 用它在 redefiner non-speculative 时消耗 guard。
- RAB/commit path 用它 suppress conventional free，避免 oldPdest 重复入 free list。

### 4.5 `IntERUopMeta`

推荐作为 uop bundle 中统一携带的 ER 字段。

```scala
class IntERUopMeta(implicit p: Parameters) extends XSBundle {
  val src    = Vec(backendParams.numSrc, new IntERSrcTrack)
  val dest   = new IntERDestTrack
  val redef  = new IntERRedefTrack
  val eligible = Bool() // debug/perf only: this uop was considered by ER
}
```

需要加入以下现有 Bundle：

- `RenameOutUop`
- `EnqRobUop`
- `DynInst`
- `DispatchOutBaseUop`
- `DispatchUpdateUop`
- `RegionInUop`
- `IssueQueuePayload` 或 `EntryBundles.Status/SrcStatus`
- `IssueQueueDeqOg1Payload`
- `RobEntryBundle`
- `RobCommitEntryBundle`，如果 commit/debug 需要直接观察
- `RabCommitInfo` 或与 `RabCommitIO` 平行的 `Vec(RabCommitWidth, IntERRedefTrack)`

实现上不一定真的加入完整 `IntERUopMeta` 到每个 Bundle；可以拆分。但所有字段必须沿路径保持语义一致。

实现风险约束：

- `IntERUopMeta.src = Vec(backendParams.numSrc, ...)` 只表示 rename/ROB 侧的逻辑全集，不应无条件塞进所有局部 bundle。
- 当前 XiangShan 的 source 宽度是分层参数化的：`RegionInUop` 使用 `IssueBlockParams.numSrc`，`EntryBundles.SrcStatus` 与 `IssueQueueDeqOg1Payload` 使用 `numRegSrc`/`ExeUnitParams.numRegSrc`，不能用固定 `backendParams.numSrc` 破坏 `connectSamePort` 或扩大所有 IQ entry。
- 推荐提供局部 helper，例如 `def IntERSrcTrackVec(n: Int) = Vec(n, new IntERSrcTrack)`。局部 Vec 的长度跟随所在 bundle 的 source 数；`srcIdx` 字段保存 original rename source index，用于 STD copy 和 ROB 回写定位。
- `RobEntryBundle` 若采用 ROB-local source 状态，第一版可使用 full logical source slots；但必须只允许 `firstUop && lastUop && numUops === 1` 的 tracked uop 写入，避免 compressed ROB entry 无法一一对应 source metadata。

### 4.6 `IntERSrcValueReadDone`

DataPath/Region 在确认 integer source value 已被 consumer 获得后产生的 raw observation；功能 decrement 仍必须由 ROB 验证后输出。

```scala
class IntERSrcValueReadDone(implicit p: Parameters) extends XSBundle {
  val robIdx   = new RobPtr
  val src      = Vec(backendParams.numSrc, new Bundle {
    val valid    = Bool()
    val trackId  = UInt(IntERTrackIdWidth.W)
    val trackGen = UInt(IntERTrackGenBits.W)
    val srcIdx   = UInt(log2Ceil(backendParams.numSrc).W)
    val psrc     = UInt(PhyRegIdxWidth.W) // debug/assert only
  })
  val fallback = Bool()
  val reason   = UInt(IntERFallbackReasonWidth.W)
}
```

语义：

- `src(i).valid` 只对 `SrcType.isXp` 且 source metadata valid 的 source 置位。
- `fallback` 表示该 uop 的 tracked source 使用了第一版不支持的 read path 或 replay path。UCA 收到后对对应 entry 置 fallback，而不是 decrement 后 early-free。
- 同一 uop 两个 source 指向同一个 tracked entry 时，DataPath 必须只发一次有效 source event。推荐直接继承 rename 阶段的 duplicate-source 去重结果。
- `fallback = true` 时也必须携带所有受影响 tracked source 的 `trackId/gen/srcIdx`。如果 unsupported consumer 在 Rename 阶段已经识别，则直接对 matched producer entry 置 fallback；不要生成没有 `src.valid` 的空 fallback event，否则 UCA 无法知道应 fallback 哪个 entry。

### 4.7 `IntERSquashSource`

ROB walk/squash 对未 readDone source 撤销 rename-time increment。

```scala
class IntERSquashSource(implicit p: Parameters) extends XSBundle {
  val robIdx   = new RobPtr
  val src      = Vec(backendParams.numSrc, new IntERSrcTrack)
}
```

语义：

- 每个 `src(i).valid` 表示该 source 需要 `UC -= 1`。
- 只有 `RobEntry.intER.src(i).valid && !readDone` 的 source 可以出现在该事件中。
- 如果 `conservativeRedirectKill = true`，可以不产生该事件；UCA 在 redirect/walk 时 invalid 所有未 early-release entry。

### 4.8 `IntERProducerReady`

writeback 侧通知 tracked producer 已经产生值。

```scala
class IntERProducerReady(implicit p: Parameters) extends XSBundle {
  val valid  = Bool()
  val robIdx = new RobPtr
  val pdest  = UInt(PhyRegIdxWidth.W)
}
```

语义：

- 只对 integer writeback 且 `rfWen` 为真的 producer 有效。
- 需要使用 redirect-filtered writeback，不能让被更老 redirect 杀掉的 writeback 设置 `producedReady`。
- `pdest` 用于 debug/assert，功能匹配以 `robIdx` 或 `trackId/gen` 为主。

### 4.9 `IntERSTGuardDec`

ROB speculation tracker 发现 redefiner non-speculative 后发给 UCA 的 guard decrement 事件。

```scala
class IntERSTGuardDec(implicit p: Parameters) extends XSBundle {
  val valid    = Bool()
  val robIdx   = new RobPtr
  val trackId  = UInt(IntERTrackIdWidth.W)
  val trackGen = UInt(IntERTrackGenBits.W)
  val oldPdest = UInt(PhyRegIdxWidth.W)
  val fallback = Bool()
  val reason   = UInt(IntERFallbackReasonWidth.W)
}
```

语义：

- `fallback = false` 时，UCA 对该 tracked entry 消耗 guard，即 `UC -= 1`，并置 `redefinerNS := true`。
- `fallback = true` 时，UCA 不 early-release，该 entry 等 conventional release。
- 若 `trackGen` 不匹配，事件必须被忽略并触发 debug assertion。

### 4.10 `IntEREarlyFreeReq`

UCA 发给 `MEFreeList` 的提前释放请求。

```scala
class IntEREarlyFreeReq(implicit p: Parameters) extends XSBundle {
  val valid    = Bool()
  val pdest    = UInt(PhyRegIdxWidth.W)
  val trackId  = UInt(IntERTrackIdWidth.W)
  val trackGen = UInt(IntERTrackGenBits.W)
  val redefRobIdx = new RobPtr
}
```

语义：

- `pdest = 0` 永远非法。
- early-free 发出后，该 entry 进入 `ReleasedWaitCommit`，不能立即复用，直到 redefiner commit suppress conventional free。
- `observeOnly = true` 时只更新 perf/debug，不驱动 `MEFreeList`。

### 4.11 `IntERCommitSuppress`

commit path 抑制 conventional free 的事件。

```scala
class IntERCommitSuppress(implicit p: Parameters) extends XSBundle {
  val suppress = Bool()
  val oldPdest = UInt(PhyRegIdxWidth.W)
  val trackId  = UInt(IntERTrackIdWidth.W)
  val trackGen = UInt(IntERTrackGenBits.W)
}
```

语义：

- 与 `RabCommitWidth` 的 commit lane 对齐。
- `suppress = true` 时，`Rename.scala` 必须屏蔽该 lane 的 `int_need_free`。
- `suppress = true` 的唯一合法原因是 oldPdest 已经通过 `IntEREarlyFreeReq` 进入 `MEFreeList`。

## 5. 新模块：`IntSparseUCA`

### 5.1 实例化位置

第一版推荐在 `Rename.scala` 内实例化：

```scala
val intER = if (EnableIntEarlyRegRelease) Some(Module(new IntSparseUCA)) else None
```

理由：

- Rename 能看到 final rename output `io.out(i).bits.psrc/pdest`，包括同周期 bypass 和 move elimination。
- Rename 拥有 `MEFreeList` 实例，early-free 可以在同一个 ownership boundary 内接入。
- Rename 已经接收 `RabCommitIO`、redirect、snapshot/walk 等恢复相关输入。
- ROB/DataPath 只需要把 ordered event 发回 UCA，不直接修改 free list。

### 5.2 IO

推荐 IO：

```scala
class IntSparseUCAIO(implicit p: Parameters) extends XSBundle {
  val redirect = Flipped(ValidIO(new Redirect))
  val rabCommits = Flipped(new RabCommitIO)

  // Rename side: final post-bypass uops.
  val rename = new Bundle {
    val fire = Input(Vec(RenameWidth, Bool()))
    val uop  = Input(Vec(RenameWidth, new RenameOutUop))
    val srcMatch = Output(Vec(RenameWidth, Vec(backendParams.numSrc, new IntERSrcTrack)))
    val destTrack = Output(Vec(RenameWidth, new IntERDestTrack))
    val redefTrack = Output(Vec(RenameWidth, new IntERRedefTrack))
    val fallbackMark = Output(Vec(RenameWidth, Bool())) // debug/perf
  }

  // Writeback/read-done/ST events from CtrlBlock/ROB.
  // readDone must be ROB-validated/de-duplicated, not raw DataPath events.
  val producerReady = Input(Vec(params.numPregWb(IntData()), ValidIO(new IntERProducerReady)))
  val readDone = Input(Vec(IntERReadDoneWidth, ValidIO(new IntERSrcValueReadDone)))
  val squash = Input(Vec(IntERSquashWidth, ValidIO(new IntERSquashSource)))
  val stGuardDec = Input(Vec(IntERSTWalkWidth, ValidIO(new IntERSTGuardDec)))

  // Commit lane conventional-free suppress.
  val commitOldPdest = Input(Vec(RabCommitWidth, UInt(PhyRegIdxWidth.W)))
  val commitNeedFree = Input(Vec(RabCommitWidth, Bool()))
  val commitSuppress = Output(Vec(RabCommitWidth, new IntERCommitSuppress))

  // Free-list side.
  val earlyFree = Output(Vec(IntEREarlyFreeWidth, ValidIO(new IntEREarlyFreeReq)))

  // Optional debug/perf.
  val debug = Output(new IntERDebugBundle)
}
```

如果 `IntSparseUCA` 放在 `CtrlBlock` 而不是 `Rename`，则必须额外把 `earlyFree` 与 `commitSuppress` 送回 `Rename`，并确保不会绕过 `MEFreeList` ownership。本文档后续按 Rename 内实例化描述。

`readDone` 所有权约束：

- DataPath/Region 只能产生 raw read-done observation。
- ROB 必须先按 `robIdx + srcIdx + trackId/gen` 校验、去重并设置 `RobEntry.intER.src.readDone`。
- 只有 ROB 输出的 validated decrement event 可以进入 UCA。UCA 不应同时接收 raw DataPath event 和 ROB squash/readDone 状态，否则 redirect/walk corner 下可能出现 readDone decrement 与 squash decrement 双减。

### 5.3 Entry 状态

```scala
object IntEREntryState {
  val Invalid = 0.U(2.W)
  val Counting = 1.U(2.W)
  val FallbackWaitCommit = 2.U(2.W)
  val ReleasedWaitCommit = 3.U(2.W)
}

class IntEREntry(implicit p: Parameters) extends XSBundle {
  val state = UInt(2.W)
  val pdest = UInt(PhyRegIdxWidth.W)
  val producerRobIdx = new RobPtr
  val redefinerRobIdx = new RobPtr
  val userCounter = UInt(IntERCounterWidth.W)
  val gen = UInt(IntERTrackGenBits.W)

  val fallback = Bool()
  val redefinerSeen = Bool()
  val redefinerNS = Bool()
  val producedReady = Bool()
  val earlyFreeIssued = Bool()
}
```

`ReleasedWaitCommit` 的含义：

- physical register 已经进入 `MEFreeList`。
- entry 不再参与 source CAM match。
- entry 仍保留 `trackId/gen/redefinerRobIdx/pdest`，等待 redefiner commit 时 suppress conventional free。
- commit suppress 完成后 entry 才能回到 `Invalid` 并递增 `gen`。

保留 entry 到 redefiner commit 的好处是避免额外 `pendingSuppressQueue`，也避免 physical register 被复用后只靠 `pdest` suppress 造成 ABA。代价是 L-entry capacity 会低于理想论文模型。第一版优先 correctness。

### 5.4 Rename source match

source match 使用 final post-bypass rename uop：

```text
for each rename slot i with io.out(i).fire:
  for each source srcIdx:
    if SrcType.isXp(io.out(i).bits.srcType(srcIdx)):
      match active entries where:
        entry.state === Counting
        !entry.fallback
        entry.pdest === io.out(i).bits.psrc(srcIdx)
```

同周期 older producer allocation bypass：

- 若 `allowSameCycleRenameBypassMatch = true`，source CAM 的候选集合必须包含本周期更老 rename slot 新分配的 tracked dest。
- 若该逻辑压 timing，可以在第一版设 `allowSameCycleRenameBypassMatch = false`。此时同周期 producer allocation 若被更年轻 source bypass 使用，推荐让该 producer allocation untracked，或者对该 source 命中的 old entry fallback。不能漏记 source 后仍允许 producer early-release。

duplicate source 去重：

```text
if src0 and src1 match same trackId/gen in same uop:
  count only src0
  src1.track.valid := false
```

unsupported consumer：

- 如果 consumer uop 是 `isMove`、`numUops =/= 1`、`!firstUop || !lastUop`、has frontend/decode exception、singleStep、flushPipe、waitForward/blockBackward，且其 integer source 命中 tracked entry：
  - 对命中的 producer entry 置 `fallback := true`；
  - 不产生 counted source metadata。

这样可以避免 move elimination 或特殊路径把 tracked source 变成新的 architectural reference。

### 5.5 Rename producer allocation

eligible producer：

```text
rename.fire &&
uop.rfWen &&
uop.ldest =/= 0 &&
!uop.isMove &&
uop.firstUop &&
uop.lastUop &&
uop.numUops === 1 &&
!uop.hasException &&
!uop.singleStep &&
!uop.flushPipe
```

allocation 规则：

```text
if eligible && free IntEREntry exists:
  allocate lowest/free circular entry
  state := Counting
  pdest := uop.pdest
  producerRobIdx := uop.robIdx
  userCounter := 1
  fallback := false
  redefinerSeen := false
  redefinerNS := false
  producedReady := false
  earlyFreeIssued := false
  gen := gen + 1 when reusing entry
  output destTrack.valid := true
else:
  output destTrack.valid := false
```

队列 full 时：

- 新 producer/result allocation 不被追踪，走 conventional release。
- 已经 tracked 的 older producer 若被该 uop source 命中，仍必须按 source match 规则 increment 或 fallback。

### 5.6 Redefiner track

当当前 uop 分配 integer dest 时，它会重定义 logical register，old physical destination 是 RAT 当前 mapping。`IntSparseUCA` 需要判断 oldPdest 是否命中 active tracked entry。

在 `Rename.scala` 中，oldPdest 可用来源：

- final rename/bypass 前的 `psrcIntForMove` 或 int RAT read result，用于 move elimination。
- 对 ordinary rfWen uop，oldPdest 是当前 logical destination 的 old physical mapping。当前代码 conventional release 是 commit 时由 arch RAT 输出 `old_pdest`，但 ER 需要在 rename 时知道 oldPdest 是否 tracked。

推荐新增 rename-time old integer destination read：

```scala
val intOldPdestForER = Wire(Vec(RenameWidth, UInt(PhyRegIdxWidth.W)))
intOldPdestForER(i) := int RAT read of ldest before speculative write/bypass
```

若当前已有 `psrcIntForMove` 只对应 `lsrc(0)`，不能直接拿它当 old destination。需要明确从 int RAT 读 `ldest`，或在 rename table wrapper 增加 ER 专用 old-dest read port。

实现风险约束：

- `intOldPdestForER` 必须和 `RenameTable` 当前同步读、`hold`、T0/T1 bypass、redirect/snapshot 恢复语义一致，不能旁路维护另一份简化 RAT。
- 同一 rename group 内若更老 uop 已经重定义同一个 `ldest`，更年轻 uop 的 oldPdest 必须看到更老 uop的新 `pdest`，与正常 rename mapping 顺序一致。
- redirect 或 `hold` 期间不得把 stale oldPdest 用于 redefiner match。若无法证明 old-dest read 与最终 rename output 同拍同序，第一版必须保持 `observeOnly` 或禁用 early-free，而不能只关闭断言。

redefiner match：

```text
if rename.fire && uop.rfWen && !uop.isMove && ldest != 0:
  oldPdest = intOldPdestForER(i)
  if oldPdest matches entry.state === Counting/FallbackWaitCommit:
    entry.redefinerSeen := true
    entry.redefinerRobIdx := uop.robIdx
    output redefTrack := trackId/gen/oldPdest
```

如果 oldPdest 命中 `ReleasedWaitCommit`，说明同一个 old allocation 已经被提前释放但还没等到其 redefiner commit，又出现了新的 redefiner 关系。第一版必须断言失败或让相关路径 fallback，因为这通常表示 ROB/RAT 顺序或 tracking generation 错误。

### 5.7 UC update 聚合

同周期可能出现的事件：

- rename source increment；
- readDone decrement；
- squash decrement；
- ST guard decrement；
- fallback set；
- producerReady；
- earlyFree issue；
- commit suppress clear；
- redirect invalidation。

推荐把事件先聚合为每个 entry 的 delta：

```scala
class IntEREntryUpdate extends Bundle {
  val inc = UInt(IntERCounterWidth.W)
  val dec = UInt(IntERCounterWidth.W)
  val setFallback = Bool()
  val setProducedReady = Bool()
  val setRedefinerNS = Bool()
  val issueEarlyFree = Bool()
  val clearAtCommit = Bool()
  val invalidate = Bool()
}
```

优先级从高到低：

1. redirect/walk invalidate 或 commit suppress clear；
2. fallback set；
3. producerReady/redefinerNS 状态更新；
4. UC inc/dec 聚合；
5. earlyFree 判断与输出。

UC 饱和规则：

```text
nextUC = oldUC + inc - dec
if oldUC + inc would exceed maxCounter:
  setFallback := true
  do not earlyFree
```

generation 规则：

- 所有 readDone/squash/ST/commitSuppress 事件都必须匹配 `trackId && trackGen`。
- generation mismatch 不改变 UC 或 entry state。
- generation mismatch 在 debug 下记录 `int_er_late_event_gen_mismatch`。

### 5.8 Redirect 与 recovery

推荐实现两个模式。

#### 模式 A：precise source recovery

这是完整设计。

- ROB walk/special walk 对每个被 squash entry 输出 `IntERSquashSource`。
- UCA 对每个 `valid && gen match` source 做 `UC -= 1`。
- Producer entry 若 `producerRobIdx.needFlush(redirect)` 且尚未 earlyFree，则 invalidate。
- Redefiner 若被 squash 且 ST 尚未越过，不需要 guard restore。
- Redefiner 若被 squash 但对应 entry 已 `redefinerNS` 或 `earlyFreeIssued`，必须断言失败，因为 ST 不应越过可能被后续 redirect squash 的 instruction。

#### 模式 B：conservative redirect kill

这是第一版可选降级，用来降低恢复实现风险。

- 任意 redirect 或 ROB walk 开始时：
  - 所有 `Counting` 和 `FallbackWaitCommit` entry 直接 invalidate；
  - 所有未发 earlyFree 的机会放弃，之后走 conventional release；
  - `ReleasedWaitCommit` entry 保留，等待 redefiner commit suppress。
- 不需要逐 source squash decrement。
- late readDone 通过 generation/state mismatch 忽略。

该模式收益低于模式 A，但实现风险明显更低。若启用该模式，必须保证 ST 不会产生可能被 redirect squash 的 `ReleasedWaitCommit` entry；否则断言失败。

## 6. `Rename.scala` 修改规格

### 6.1 新增 IO

`Rename` 需要接收来自 CtrlBlock/ROB 的 ER event。Region/DataPath 的 raw readDone 必须先进入 ROB，再由 ROB 输出 validated decrement：

```scala
class RenameIntERIO(implicit p: Parameters) extends XSBundle {
  val producerReady = Vec(params.numPregWb(IntData()), Flipped(ValidIO(new IntERProducerReady)))
  val readDone      = Vec(IntERReadDoneWidth, Flipped(ValidIO(new IntERSrcValueReadDone)))
  val squash        = Vec(IntERSquashWidth, Flipped(ValidIO(new IntERSquashSource)))
  val stGuardDec    = Vec(IntERSTWalkWidth, Flipped(ValidIO(new IntERSTGuardDec)))
}
```

在 `Rename.io` 增加：

```scala
val intER = Option.when(EnableIntEarlyRegRelease)(new RenameIntERIO)
```

### 6.2 Rename pipeline 插入点

ER source match 必须在以下逻辑之后：

- `uops(i).psrc` 从 RAT 读出；
- fused rs2 修正；
- `io.out(i).bits.psrc` 的 same-cycle pdest bypass；
- move elimination `pdest := psrcIntForMove`；
- LUI source 修正。

推荐插入点在当前 `io.out(i).bits.pdest := ...` 和最终 `io.out` valid/ready 逻辑之后，但在 `io.out.fire` 相关 perf/assert 之前。

### 6.3 `RenameOutUop` 字段

在 `RenameOutUop` 中增加：

```scala
val intER = Option.when(EnableIntEarlyRegRelease)(new IntERUopMeta)
```

赋值：

```scala
io.out(i).bits.intER.get.src   := intER.io.rename.srcMatch(i)
io.out(i).bits.intER.get.dest  := intER.io.rename.destTrack(i)
io.out(i).bits.intER.get.redef := intER.io.rename.redefTrack(i)
io.out(i).bits.intER.get.eligible := erEligible(i)
```

`isMove` uop：

- `dest.valid := false`。
- 若 source 命中 tracked entry，matched entry fallback。
- `redef.valid := false`，保持现有 `MEFreeList` move elimination 语义。

### 6.4 Conventional free suppress

当前代码：

```scala
intFreeList.io.freeReq(i) := int_need_free(i)
intFreeList.io.freePhyReg(i) := RegNext(int_old_pdest(i))
```

ER 开启后改为：

```scala
val erSuppress = intER.io.commitSuppress(i).suppress
intFreeList.io.freeReq(i) := int_need_free(i) && !erSuppress
intFreeList.io.freePhyReg(i) := RegNext(int_old_pdest(i))
```

注意：

- `int_need_free` 与 `int_old_pdest` 当前有寄存器延迟关系，`erSuppress` 必须与它们同拍对齐。
- 如果 `commitSuppress` 在 UCA 内基于 `commitOldPdest` 和 `rabCommits.info(i).intER.redef` 计算，也需要同样延迟。
- `suppress` 不能只按 physical register bit 判断，必须带 `trackId/gen` 或 `redefinerRobIdx`，避免 early-released preg 被年轻 allocation 复用后误 suppress。
- 推荐显式生成 `erSuppressReg(i)`，与最终驱动 `intFreeList.io.freeReq(i)` 的 `int_need_free(i)`、`RegNext(int_old_pdest(i))` 对齐；不要把组合 `commitSuppress` 直接接到已寄存器化的 free lane。
- 增加断言：当 `erSuppressReg(i)` 为真时，延迟后的 `int_old_pdest` 必须等于对应 `IntERRedefTrack.oldPdest`，并且 `trackId/gen/redefinerRobIdx` 匹配 `ReleasedWaitCommit` entry。

### 6.5 RenameTableWrapper 需求

ER 需要 rename-time old integer destination mapping。推荐在 `RenameTableWrapper` 中增加：

```scala
val intOldDestReadPorts = Vec(RenameWidth, Flipped(new RatReadPort(log2Ceil(IntLogicRegs))))
```

或者复用已有 read port，但必须保证：

- 读地址是 `uop.ldest`，不是 `lsrc(0)`。
- 读值是本条 uop rename 前的 old mapping，并正确处理同周期 older uop 对同一 logical dest 的 bypass。

如果该 old-dest read 增加 rename critical path，第一版可以不做 redefiner match，转为 observation-only；真正 early release 前必须补齐。

## 7. `MEFreeList` 修改规格

### 7.1 IO

只对 integer `MEFreeList` 增加 early-free 输入。可以加到 `MEFreeList`，不建议影响所有 `StdFreeList`。

```scala
class MEFreeList(...) {
  val io = IO(new Bundle {
    ...
    val earlyFreeReq = Input(Vec(IntEREarlyFreeWidth, Bool()))
    val earlyFreePhyReg = Input(Vec(IntEREarlyFreeWidth, UInt(PhyRegIdxWidth.W)))
    val erPendingSuppress = Input(Vec(IntPhyRegs, Bool())) // debug only, optional
  })
}
```

如果为复用而加到 `BaseFreeList`，FP/Vec/V0/VL 必须绑 0，不能改变它们行为。

### 7.2 Deallocation 合流

当前 `freePtr` 宽度为 `commitWidth`。ER 后应改为：

```scala
val allFreeReq = io.freeReq ++ io.earlyFreeReq
val allFreePhyReg = io.freePhyReg ++ io.earlyFreePhyReg
val totalFreeWidth = commitWidth + IntEREarlyFreeWidth
```

写 freeList：

```text
for free lane k in totalFreeWidth:
  freePtr(k) = tailPtr + PopCount(allFreeReq.take(k))
  if allFreeReq(k): freeList(freePtr(k).value) := allFreePhyReg(k)
tailPtr := tailPtr + PopCount(allFreeReq)
```

`freeRegCnt` 必须使用合流后的 `tailPtrNext`。

### 7.3 `archHeadPtr` 语义

`archHeadPtr` 仍只由 commit-time architectural allocation 推进：

```scala
val numArchAllocate = PopCount(io.commit.archAlloc)
archHeadPtr := Mux(doCommit, archHeadPtr + numArchAllocate, archHeadPtr)
```

early-free 不能推进 `archHeadPtr`，因为 redefiner 还没有 commit，arch RAT 可能仍指向 old physical register。

### 7.4 Debug invariant 修改

现有 invariant：

```text
Integer physical register should be in either arch RAT or arch free list
```

ER 开启后该 invariant 不再成立。一个 early-released preg 在 redefiner commit 前可能同时满足：

- stale arch RAT 仍指向它；
- 它已经进入 free list；
- 它可能已经被年轻 instruction 重新 allocation。

必须改成 ER-aware debug 检查。最低要求：

- `earlyFreePhyReg` 不为 0。
- 同周期 `allFreeReq` 中没有重复 preg。
- `commit free` 与 `early free` 同周期不能释放同一个 preg。
- `suppress conventional free` 时，对应 oldPdest 必须曾经由 `IntEREarlyFreeReq` 释放。
- 未 suppress 的 conventional free 不得释放已经处于 `ReleasedWaitCommit` 的 trackId/gen。
- early-free preg 对应 entry 必须 `producedReady && redefinerNS && UC == 0`。

完整 debug 可维护 `erPendingSuppressSet[IntPhyRegs]`：

```text
set when earlyFree fires
clear when matching redefiner commit suppresses conventional free
```

但不要只用这个 bit 决定功能 suppress。功能 suppress 必须用 `trackId/gen/redefiner`，bit set 只能作为 debug 辅助。

## 8. Dispatch 修改规格

### 8.1 Bundle 传播

以下 Bundle 需要增加 ER 字段或由 `connectSamePort` 自动传播：

- `DispatchOutBaseUop`
- `DispatchOutUop`
- `DispatchUpdateUop`
- `DynInst`
- `RegionInUop`

推荐字段：

```scala
val intER = Option.when(EnableIntEarlyRegRelease)(new IntERUopMeta)
```

Dispatch 不改变 ER metadata，只负责传播。

### 8.2 fromRenameUpdate

当前 `fromRenameUpdate(i).bits` 由 `connectSamePort(fromRename(i).bits)` 得到。ER 字段加入同名 Bundle 后应自动复制。仍需要显式处理：

- 如果 Dispatch 将某 source `srcType` 改为 `SrcType.no` 或 `SrcType.imm`，对应 `intER.src(srcIdx).valid` 必须清 0，或者在 Rename 阶段提前不给这类 source 计数。
- `isDropAmocasSta`、singleStep、hasException 导致不入 IQ 的 uop，不应保留需要 DataPath readDone 的 counted source。第一版推荐在 Rename 阶段将这些 source 命中的 producer fallback。

### 8.3 BusyTable 与 RegCache

Dispatch 的 BusyTable、RegCacheTagTable 行为不变。ER source metadata 不参与 initial readiness 计算。

需要检查：

- `useRegCache` 与 `regCacheIdx` 是 `Vec(backendParams.numIntRegSrc, ...)`。
- ER metadata 建议用 `Vec(numSrc, ...)`，因为 Issue/Region/DataPath 的 source index 是 `numSrc` 语义。
- 对 `backendParams.numIntRegSrc` 与 `numSrc` 的 index 映射必须在 Dispatch/Region 保持一致。
- 但 `RegionInUop` 的 `numSrc` 来自 `IssueBlockParams`，不同 IQ 可能不同；实现时应在 `RegionInUop(params)` 内使用 `params.numSrc` 长度的 `IntERSrcTrackVec`，而不是直接嵌入 full `IntERUopMeta`。

## 9. IssueQueue 与 EntryBundles 修改规格

### 9.1 `SrcStatus`

在 `EntryBundles.SrcStatus` 增加：

```scala
val intER = Option.when(EnableIntEarlyRegRelease)(new IntERSrcTrack)
```

enqueue 时从 `RegionInUop.intER.src(idx)` 复制。

注意：`SrcStatus` 是 `Vec(params.numRegSrc, ...)`，只能保存该 IQ 实际可读寄存器 source 的 metadata。若某 source 在 dispatch/issue 参数中被裁剪或转成非寄存器 source，Rename 阶段必须不计数或对命中的 producer fallback，不能在后级丢 metadata 后仍允许 early release。

### 9.2 `IssueQueueDeqOg1Payload`

在 `IssueQueueDeqOg1Payload` 增加：

```scala
val intER = Option.when(EnableIntEarlyRegRelease)(Vec(params.numRegSrc, new IntERSrcTrack))
```

`EntryBundle.toDeqOg1Payload` 必须复制：

```scala
deqOg1Payload.intER.get.zipWithIndex.foreach {
  case (dst, idx) => dst := status.srcStatus(idx).intER.get
}
```

### 9.3 Entry clear/trans/replay

IssueQueue 本身不负责 UC decrement。它只需要保证：

- source metadata 与 `psrc/srcType/dataSources` 同步更新。
- entry 被 `common.clear` 清掉时，不私自发 decrement；squash 由 ROB 统一处理。
- 若 source 因 load cancel、og0/og1 cancel、transSel 等原因重发，不能产生 readDone。readDone 只由 DataPath final-success 边界产生。

## 10. Region 修改规格

### 10.1 Region IO

在 `RegionIO` 增加：

```scala
val intERReadDone = Option.when(EnableIntEarlyRegRelease)(
  Output(Vec(regionIntERReadDoneWidth, ValidIO(new IntERSrcValueReadDone)))
)
```

`regionIntERReadDoneWidth` 可取该 Region 内所有 issue deq port 数：

```scala
params.issueBlockParams.map(_.numDeq).sum
```

所有 Region 都可能存在 `SrcType.isXp` consumer。第一版若不支持 FP/Vec Region 中的 integer source readDone，则 Rename source match 必须在这些 consumer 命中 tracked entry 时让 producer fallback，不能漏计。

### 10.2 STA -> STD copy

当前 `Region.scala` 对 STD 做：

```scala
stdIQEnq.bits.srcState(0) := staIQEnq.bits.srcState(1)
stdIQEnq.bits.srcLoadDependency(0) := staIQEnq.bits.srcLoadDependency(1)
stdIQEnq.bits.srcType(0) := staIQEnq.bits.srcType(1)
stdIQEnq.bits.psrc(0) := staIQEnq.bits.psrc(1)
stdIQEnq.bits.useRegCache(0) := staIQEnq.bits.useRegCache(1)
stdIQEnq.bits.regCacheIdx(0) := staIQEnq.bits.regCacheIdx(1)
```

ER 必须增加：

```scala
stdIQEnq.bits.intER.get.src(0) := staIQEnq.bits.intER.get.src(1)
```

不要把 `srcIdx` 改成 0。它必须保持 original source index `1`，这样 DataPath readDone 能回到 ROB entry 的原 source slot。

### 10.3 Cross-region DeqOg1Payload

`RegionIO` 已有 `fromIntIQDeqOg1Payload`、`fromFpIQDeqOg1Payload`、`fromVecIQDeqOg1Payload` 跨 region 传递。ER metadata 加入 `IssueQueueDeqOg1Payload` 后，这些 MixedVec 会自动携带，但必须检查 `connectSamePort` 不因 Option 或 width mismatch 漏连。

当前 `IssueQueueDeqOg1Payload` 使用 `ExeUnitParams.numRegSrc`，而 `RegionInUop`/`IssueQueuePayload` 使用 `IssueBlockParams.numSrc`。如果跨 region payload 只携带 `numRegSrc` 个 metadata，必须保证每个 metadata 的 `srcIdx` 保留 original slot；不能假设 local index 就是 ROB 中的 source index。

## 11. DataPath 修改规格

### 11.1 DataPath IO

在 `DataPathIO` 增加：

```scala
val intERReadDone = Option.when(EnableIntEarlyRegRelease)(
  Output(Vec(dataPathReadDoneWidth, ValidIO(new IntERSrcValueReadDone)))
)
```

`dataPathReadDoneWidth` 建议等于 `fromIQ.flatten.size`，每个 issue/deq port 一个事件。

### 11.2 Read-done 生成点

DataPath 内当前关键状态：

- `s0 = fromIQ(i)(j)`
- `s1_flush = s0.bits.robIdx.needFlush(Seq(io.flush, flushReg))`
- `s0_ldCancel = LoadShouldCancel(s0.bits.loadDependency, io.ldCancel)`
- `s0.fire && !s1_flush && !s0_ldCancel` 设置 `s1_valid`
- `og1resp.finalSuccess` 对非 memory IQ 可由 `s1_toExuValid && !og1FailedVec2` 产生

ER readDone 第一版推荐：

```text
candidate capture:
  s0.fire && !s1_flush && !s0_ldCancel
  capture robIdx/srcType/psrc/dataSources/intER into readDoneShadow

emit readDone:
  corresponding og1resp.finalSuccess &&
  !og1resp.failed &&
  source intER.valid &&
  source dataSources.readReg || source dataSources.readRegCache
```

对于以下情况，第一版不直接 decrement：

- `dataSources.readForward` 且无法证明 final-success 后不会 replay；
- load/store/memory IQ 的 finalSuccess 需要 LSQ 反馈；
- uncertain wakeup EXU；
- `og0FailedVec2` 或 `og1FailedVec2`；
- load cancel、forward cancel、memory violation replay；
- vector memory 或特殊 split uop。

这些情况如果 source metadata valid，DataPath 应发 `fallback` event，或者 Rename 阶段提前把对应 producer fallback。

实现风险约束：

- 当前 `Og1InUop`/`ExuInput` 默认不携带 `psrc`，`s1_toExuData` 也只能保留被 `connectSamePort` 接上的字段。若仅在 `IssueQueueDeqOg1Payload` 增加 ER metadata，DataPath 在 `og1resp.finalSuccess` 同拍未必还能拿到完整 `psrc/srcType/dataSources/intER`。
- 推荐在 DataPath 内建立 per deq port 的 `readDoneShadow`，在 successful `s0 -> s1` capture 时保存 `robIdx/srcType/psrc/dataSources/intER`；最终 readDone event 从 `readDoneShadow` 生成。
- 另一种可选方案是在 `Og0InUop`、`Og1InUop`、必要的 `ExuInput` 全链路增加对应字段，但这会扩大 EXU 输入和 bypass 侧 bundle，面积/时序风险更高。第一版推荐 shadow。
- 当前 DataPath 对 `LdAddrIQ/StAddrIQ/StdIQ/HyAddrIQ/inVfSchd` 的 `og1resp.finalSuccess` 置 false，这些路径不会通过普通 DataPath readDone 产生 decrement。若 Rename 允许这些 consumer source 计数，必须在 Region/LSQ/memory final-success 处产生等价 readDone，或者在 Rename 阶段对命中的 producer fallback。

### 11.3 ReadDone event 内容

每个 deq port 产生：

```scala
readDone.valid := finalSuccess && anyTrackedIntSrc
readDone.bits.robIdx := readDoneShadow(i)(j).robIdx
for srcIdx <- 0 until numRegSrc:
  readDone.bits.src(srcIdx).valid :=
    finalSuccess &&
    SrcType.isXp(readDoneShadow(i)(j).srcType(srcIdx)) &&
    readDoneShadow(i)(j).intER(srcIdx).valid &&
    supportedDataSource(srcIdx)
  readDone.bits.src(srcIdx).trackId := ...
  readDone.bits.src(srcIdx).trackGen := ...
  readDone.bits.src(srcIdx).srcIdx := original srcIdx from metadata
  readDone.bits.src(srcIdx).psrc := readDoneShadow(i)(j).psrc(srcIdx)
```

需要注意：

- source value 来自 RegCache 时，只要 final-success 证明该 value 被 consumer 获得，就视为 readDone。
- `psrc` 只是 debug；功能 decrement 用 `trackId/gen`。
- 同一 readDone event 同一个 `trackId/gen` 只能出现一次。

## 12. ROB 与 RAB 修改规格

### 12.1 `EnqRobUop`

`EnqRobUop` 继承 `RenameOutUop`，因此加入 `RenameOutUop.intER` 后可由 `connectSamePort` 传播。仍需检查：

```scala
def connectEnqRobUop(source: RenameOutUop): Unit = {
  connectSamePort(this, source)
  ...
}
```

确保 Option 字段同名且同宽。

### 12.2 `RobEntryBundle`

增加：

```scala
val intER = Option.when(EnableIntEarlyRegRelease)(new Bundle {
  val src = Vec(backendParams.numSrc, new IntERSrcRobState)
  val dest = new IntERDestTrack
  val redef = new IntERRedefTrack
  val resolved = Bool()
})
```

enqueue 初始化：

```scala
robEntry.intER.get.src(idx).valid := robEnq.intER.get.src(idx).valid
robEntry.intER.get.src(idx).readDone := false.B
robEntry.intER.get.dest := robEnq.intER.get.dest
robEntry.intER.get.redef := robEnq.intER.get.redef
robEntry.intER.get.resolved := initialResolved(robEnq)
```

如果 ROB entry compression 导致一个 `RobEntryBundle` 不能唯一代表一个 uop 的 source metadata，第一版必须限制 ER eligibility：

```text
only track uop if firstUop && lastUop && numUops === 1
```

并断言 tracked source/dest 不出现在 multi-uop compressed entry 中。

### 12.3 RAB metadata

`RabCommitInfo` 当前只有：

```scala
ldest, pdest, rfWen, fpWen, vecWen, v0Wen, vlWen, isMove
```

ER 需要 commit lane 知道 redefiner 对应的 tracked oldPdest。因此新增：

```scala
val intERRedef = Option.when(EnableIntEarlyRegRelease)(new IntERRedefTrack)
```

RAB enqueue 时从 `EnqRobUop.intER.redef` 保存。

`RabCommitIO.info(i).intERRedef` 与 `commitValid(i)/walkValid(i)` 同 lane 输出给 Rename。

### 12.4 ROB readDone 处理

ROB 新增输入：

```scala
val intERReadDone = Option.when(EnableIntEarlyRegRelease)(
  Vec(IntERReadDoneWidth, Flipped(ValidIO(new IntERSrcValueReadDone)))
)
```

ROB 新增输出给 Rename/UCA：

```scala
val intERReadDoneDec = Option.when(EnableIntEarlyRegRelease)(
  Vec(IntERReadDoneWidth, ValidIO(new IntERSrcValueReadDone))
)
```

`intERReadDone` 是来自 Region/DataPath 的 raw observation；`intERReadDoneDec` 是 ROB 校验、去重、标记 `readDone` 后的唯一功能 decrement 来源。

处理：

```text
for each readDone event:
  idx = event.robIdx.value
  if robEntries(idx).valid:
    for each source event s:
      original = s.srcIdx
      if robEntries(idx).intER.src(original).valid &&
         trackId/gen match &&
         !robEntries(idx).intER.src(original).readDone:
        robEntries(idx).intER.src(original).readDone := true
        emit corresponding source in intERReadDoneDec
```

如果 event `fallback = true`，ROB 仍可置 readDone，也可不置；功能上 UCA 会 fallback，不再 early-release。推荐置位并记录 reason，避免 later squash 产生无意义 decrement。

ROB 必须过滤以下 raw event：

- `robIdx` 指向 invalid entry；
- `robIdx` 已被当前或更老 redirect flush；
- `srcIdx` 超过 ROB 保存的 source slot；
- `trackId/gen` 与 ROB 保存 metadata 不匹配；
- 该 source 的 `readDone` 已经置位。

被过滤的 event 不得进入 UCA，只能增加 debug/perf 计数。

### 12.5 ROB squash 输出

如果使用 precise source recovery，ROB 在 walk/special walk 时产生：

```scala
val intERSquash = Option.when(EnableIntEarlyRegRelease)(
  Output(Vec(IntERSquashWidth, ValidIO(new IntERSquashSource)))
)
```

生成条件：

```text
io.commits.isWalk && io.commits.walkValid(lane)
or RAB/ROB special_walk lane
```

每个被 walk 的 ROB entry：

```text
for each source:
  if intER.src.valid && !intER.src.readDone:
    emit squash source decrement
```

若 `conservativeRedirectKill = true`：

- ROB 可不输出逐 source squash；
- 但必须向 UCA 发送 `redirect/walk start`，让 UCA invalid non-released entries。

### 12.6 Speculation Tracker

新增 ROB 内部模块或逻辑：`IntERSpecTracker`。

推荐 IO：

```scala
class IntERSpecTrackerIO(implicit p: Parameters) extends XSBundle {
  val redirect = Flipped(ValidIO(new Redirect))
  val flushOut = Flipped(ValidIO(new Redirect))
  val robStateWalk = Input(Bool())
  val pendingInterrupt = Input(Bool())
  val pendingTrapOrFlush = Input(Bool())

  val enq = Vec(RenameWidth, Flipped(ValidIO(new EnqRobUop)))
  val writeback = Flipped(params.genWrite2RobBundles)
  val commit = Flipped(new RobCommitIO)

  val guardDec = Output(Vec(IntERSTWalkWidth, ValidIO(new IntERSTGuardDec)))
}
```

内部状态：

```scala
val resolved = RegInit(VecInit.fill(RobSize)(false.B))
val specHead = RegInit(0.U.asTypeOf(new RobPtr))
```

enqueue：

```text
resolved(robIdx) := initialResolved(uop)
```

`initialResolved` 第一版建议非常保守：

```text
true only if:
  no exception
  !flushPipe
  !waitForward
  !blockBackward
  !CommitType.isLoadStore(commitType)
  !FuType.isBranch/fence/csr/mem/vls/amo
  !replayInst
```

更保守也可以全部置 false，并在 writeback/commit 设置 true。

writeback 设置 resolved：

- 普通 ALU/mul/div 等无异常 writeback 后 resolved。
- branch/jump 必须在 writeback 且没有更老 redirect 杀掉后 resolved；如果 branch 产生 redirect，被 flush 处理，不应 ST 越过。
- load/store/CSR/fence/flushPipe 第一版到 commit 才 resolved，或让相关 redefiner fallback。

ST stop 条件：

```text
stop if:
  io.redirect.valid
  io.flushOut.valid
  ROB state == s_walk
  pending interrupt/trap/critical error
  deqNeedFlush/deqHasException/deqHasFlushPipe pending
  trace blockRobCommit blocks precise commit and its interaction not proven
```

walk：

```text
while budget < IntERSTWalkWidth and !stop:
  entry = robEntries(specHead)
  if !entry.valid or !resolved(specHead): stop
  else:
    if entry.intER.redef.valid:
      emit guardDec(trackId/gen/oldPdest)
    specHead += 1
```

guardDec policy：

ST 输出 guardDec 前只负责确认全局 non-speculative 边界：

- pending interrupt/flush stop 条件为 false。
- `redef` 来自 ROB/RAB 保存的 `IntERRedefTrack`，不是重新按 `oldPdest` CAM。
- ST 已经越过的 entry 不会再被后续 redirect/walk squash。

UCA 才拥有 tracked entry 的 `state/producedReady/fallback/gen`。因此默认设计中 ST 不应组合查询 UCA 的 `producedReady` 或 entry state；UCA 收到 guardDec 后执行：

ST does not combinationally query UCA produced-ready state.

```text
if trackId/gen mismatch or entry not Counting:
  ignore and count/debug assert
else:
  redefinerNS := true
  UC -= 1
  earlyFree waits until producedReady && UC == 0
```

如果未来实现希望把 producer-not-ready 变成独立 fallback reason，必须定义独立的 UCA query/response，并至少打一拍返回，避免 ROB/ST 到 Rename/UCA 的长组合路径。当前第一版不实现这条路径，UCA 只等待 producerReady。

### 12.7 Producer ready

ROB 或 CtrlBlock 从 redirect-filtered writeback 产生 `IntERProducerReady`。

推荐在 `CtrlBlock.scala` 使用 `delayedNotFlushedWriteBack` 或同等已过滤路径：

```text
valid when:
  wb.valid
  wb.bits.rfWen
  wb writes integer physical reg
  !killedByOlderRedirect
```

然后发送给 `Rename.intER.producerReady`。

ROB 也可以在 writeback 更新自身 debug data 时产生同样事件，但必须保证与 UCA 的 entry matching 不受 killed writeback 影响。

### 12.8 Committed integer write data

ROB 需要为 Difftest direct xrf 捕获 committed integer write data：

```scala
val dtIntWdata = Reg(Vec(RobSize, UInt(XLEN.W)))
val dtIntWdataValid = RegInit(VecInit.fill(RobSize)(false.B))
```

writeback：

```text
if wb.valid && wb.bits.rfWen:
  dtIntWdata(wb.robIdx) := wb.bits.data(0)
  dtIntWdataValid(wb.robIdx) := true
```

move elimination：

- eliminated move 可能没有 ordinary writeback。
- 需要在 ROB/RAB/debug metadata 中保存 `moveSrcLReg`，commit 时用 shadow xrf 的 `moveSrcLReg` 值更新 `wdest`。
- 或者在 move 被消除时通过额外数据路径捕获 source value。第一版推荐保存 `moveSrcLReg`，因为 direct shadow RF 本来在 ROB commit 顺序维护。

异常/skip/fake write：

- `rfwen` false 的 commit 不更新 shadow xrf。
- `rfwen` true 但 skip-diff 的普通 integer write仍应更新 shadow xrf，因为 architectural state 仍变化；skip 只影响 ref compare 事件策略。
- x0 永远不更新。

## 13. CtrlBlock 与 Backend 连接

### 13.1 CtrlBlock 内连接

新增连接路径：

```text
Region/DataPath intERReadDone
  -> Backend aggregate
  -> CtrlBlock.io.fromRegionIntERReadDone
  -> ROB.intERReadDone
  -> ROB.intERReadDoneDec
  -> Rename.intER.readDone

CtrlBlock redirect-filtered writeback
  -> Rename.intER.producerReady

ROB.IntERSpecTracker.guardDec
  -> Rename.intER.stGuardDec

ROB.intERSquash
  -> Rename.intER.squash
```

需要在 `CtrlBlockIO` 增加：

```scala
val intER = Option.when(EnableIntEarlyRegRelease)(new Bundle {
  val readDoneFromRegions = Flipped(Vec(TotalIntERReadDoneWidth, ValidIO(new IntERSrcValueReadDone)))
})
```

或者在 `Backend.scala` 内直接把 Region 输出接到 `CtrlBlock` 内部 IO。

### 13.2 Backend 内连接

`Backend.scala` 已连接 CtrlBlock、int/fp/vec Region、MemBlock、writeback、CSR 等。ER 需要：

- 汇总三个 Region 的 `intERReadDone`。
- 将所有 writeback 中 integer writeback 的 producerReady 送给 Rename/UCA。
- 确保 Region 输出 readDone 的 valid 被 flush/redirect 延迟正确过滤，或者在 ROB 用 `robIdx.needFlush` 二次过滤；UCA 不接收未经过 ROB 的 raw readDone。

## 14. Difftest 修改规格

### 14.1 问题定义

ER 开启后，以下路径不再正确：

```scala
archReg.value(logical) := preg.value(rat.value(logical))
commit_data.data := phyInts(coreID).value(c.wpdest)
```

错误场景：

```text
A writes x1 -> p1
B redefines x1, p1 early-free after B becomes non-speculative
C allocates p1 and writes new value
A commits
```

如果 Difftest 在 A commit 附近通过 `rat_xrf[x1] -> p1 -> pregs_xrf[p1]` 读值，会读到 C 的值，而不是 A 的 architectural commit 值。

### 14.2 Direct integer architectural shadow RF

新增 ROB-side module：

```scala
class DiffIntArchShadowRF(implicit p: Parameters) extends XSModule {
  val io = IO(new Bundle {
    val hartId = Input(UInt(8.W))
    val commit = Input(Vec(CommitWidth, new Bundle {
      val valid = Bool()
      val rfwen = Bool()
      val wdest = UInt(LogicRegsWidth.W)
      val wdata = UInt(XLEN.W)
      val isMove = Bool()
      val moveSrcLReg = UInt(LogicRegsWidth.W)
    }))
    val xrf = Output(Vec(32, UInt(XLEN.W)))
  })
}
```

内部：

```text
regs[0] := 0
for commit lane in order:
  if valid && rfwen && wdest != 0:
    if isMove:
      regs[wdest] := regs[moveSrcLReg]
    else:
      regs[wdest] := wdata
```

同周期多条 commit 写同一 logical register 时，按 commit lane 顺序最后写获胜。

### 14.3 Difftest bundle 公开化

当前 `DiffArchIntRegState` 和 `DiffCommitData` 是 `private[difftest]`。ER 需要至少一种方式输出 direct xrf 与 per-commit data：

方案 A：公开已有 bundle

```scala
class DiffArchIntRegState extends ArchIntRegState with DifftestBundle
class DiffCommitData extends CommitData with DifftestBundle with DifftestWithIndex
```

方案 B：新增 public bundle

```scala
class DiffDirectArchIntRegState extends ArchIntRegState with DifftestBundle {
  override val desiredCppName = "xrf"
}

class DiffDirectCommitData extends CommitData with DifftestBundle with DifftestWithIndex {
  override val desiredCppName = "commit_data"
}
```

推荐方案 A，减少 C++ 侧命名变化。

### 14.4 ROB 输出 direct `xrf`

当 `EnableIntEarlyRegRelease && enableDiffShadowXRF && (EnableDifftest || AlwaysBasicDiff)`：

- ROB 实例化 direct xrf difftest module：

```scala
val diffXrf = DifftestModule(new DiffArchIntRegState, delay = sameAsCommit)
diffXrf.coreid := io.hartId
diffXrf.value := diffIntArchShadowRF.io.xrf
```

- `RenameTableWrapper` 可以继续输出 `DiffArchIntRenameTable("rat_xrf")` 作为 debug，但 `Preprocess` 不得用它合成 architectural `xrf`。
- `DataPath` 可以继续输出 `DiffPhyIntRegState("pregs_xrf")` 作为 physical debug mirror，但它不能参与 integer arch compare 或 integer commit_data。

### 14.5 ROB 输出 direct `commit_data`

当 ER 开启：

```scala
for i <- 0 until CommitWidth:
  val cd = DifftestModule(new DiffCommitData, delay = commitDelay)
  cd.coreid := io.hartId
  cd.index := i.U
  cd.valid := difftest.valid && (difftest.rfwen || difftest.fpwen)
  cd.data := Mux(difftest.fpwen, fpCommitData, intCommitData)
```

integer `intCommitData` 来源：

- ordinary integer write：`dtIntWdata(commitRobIdx)`；
- move elimination：`diffShadowXRF(moveSrcLReg)`；
- x0：0；
- unsupported/fake write：必须按 commit event 语义明确定义，不能回读 `pregs_xrf[wpdest]`。

FP commit data 可以继续使用现有 physical path，除非后续 FP 也实现 ER。

### 14.6 `Preprocess.scala` 修改

`Preprocess.replaceRegs` 当前逻辑需要 ER-aware：

```text
if direct xrf bundle already exists:
  do not synthesize integer xrf from pregs_xrf + rat_xrf
else:
  existing behavior

if direct commit_data already exists:
  do not synthesize integer commit_data from phyInts(coreID).value(c.wpdest)
else if ER enabled:
  error or require direct commit_data
else:
  existing behavior
```

推荐实现：

```scala
val hasDirectIntXrf = bundles.exists(_.desiredCppName == "xrf")
val hasDirectCommitData = bundles.exists(_.desiredCppName == "commit_data")
```

在 `getArchRegs` 中，如果 `hasDirectIntXrf`，过滤掉 integer `DiffPhyIntRegState("pregs_xrf")` 对应的 arch synthesis，但保留 FP/Vec synthesis。

在 `replaceRegs` 中：

- `commitDatas` 生成时，如果 `hasDirectCommitData`，不要再生成重复 `commit_data`。
- 若 `EnableIntEarlyRegRelease` 对 difftest preprocess 可见，应禁止 `phyInts(coreID).value(c.wpdest)` integer data path。

实现顺序要求：

- 必须先让 `Preprocess.getArchRegs/replaceRegs` 能识别 direct `xrf` 与 direct `commit_data`，再在 ROB/DataPath 同时输出 direct `xrf`、`pregs_xrf`、`rat_xrf`。
- 当前 preprocess 在 physical reg synthesis 时有 `require(!bundles.exists(_.isInstanceOf[archTarget.type]))`，若未先修改，direct `xrf` 与 `pregs_xrf` 同时存在可能直接 elaboration 失败。
- `pregs_xrf/rat_xrf` 在 ER 下只能保留为 physical/debug bundle；functional arch compare 和 integer `commit_data` 必须来自 direct path。

### 14.7 C++ fallback 修改

`difftest/src/test/csrc/difftest/diffstate.cpp` 当前：

```cpp
#ifdef CONFIG_DIFFTEST_PHYINTREGSTATE
  return state->pregs_xrf.value[state->commit[index].wpdest];
#else
  return state->regs.xrf.value[state->commit[index].wdest];
#endif
```

ER 开启时即使 `CONFIG_DIFFTEST_PHYINTREGSTATE` 存在，也不能走 physical path。推荐新增宏：

```cpp
#if defined(CONFIG_DIFFTEST_INT_ER_DIRECT_XRF)
  return state->regs.xrf.value[state->commit[index].wdest];
#elif defined(CONFIG_DIFFTEST_PHYINTREGSTATE)
  return state->pregs_xrf.value[state->commit[index].wpdest];
#else
  return state->regs.xrf.value[state->commit[index].wdest];
#endif
```

但正常情况下应始终生成 `CONFIG_DIFFTEST_COMMITDATA`，让 `get_commit_data` 优先使用 direct per-commit data。

## 15. Move Elimination 处理

### 15.1 Functional ER 策略

第一版策略：

- `isMove` 不分配 new tracked producer entry。
- 若 `isMove` source 命中 tracked producer，则该 producer entry fallback。
- `isMove` redefiner 不参与 early release。
- conventional `MEFreeList` 行为保持现状。

原因：

- Move elimination 会让 destination logical register 直接 alias source physical register。
- 对 tracked source 来说，move 不是普通“读完就不再需要”的 consumer，而是可能延长该 physical register architectural lifetime 的 redefiner。
- 若按普通 readDone decrement 处理，可能提前释放仍被 architectural mapping 需要的 preg。

### 15.2 Difftest 策略

即使 functional ER 对 move fallback，direct diff shadow RF 仍必须支持 move commit：

- 在 decode/rename/ROB debug metadata 中保存 `moveSrcLReg`。
- move commit 时：

```text
shadowXRF[ldest] := shadowXRF[moveSrcLReg]
```

如果无法保存 `moveSrcLReg`，则必须通过其他 committed value path 提供 move 的 architectural data。不能依赖 `pregs_xrf[wpdest]`。

## 16. 中断、异常、flush 与 ST 停止条件

XiangShan ROB 中有：

- `intrBitSetReg`
- `interrupt_safe`
- `deqNeedFlush`
- `deqHasException`
- `deqHasFlushPipe`
- `flushOut`
- `state === s_walk`
- trace `blockRobCommit`
- vector exception merge/special walk 等路径

ST 必须把 `redefinerNS` 定义为：

```text
redefiner 以及所有更老指令不会再导致 architectural cut-point 回到需要 oldPdest 值的状态。
```

第一版 stop 条件：

```text
if intrBitSetReg || io.flushOut.valid || deqNeedFlush || deqHasException ||
   deqHasFlushPipe || state === s_walk || redirect.valid ||
   criticalError || vectorExceptionMergePending:
  ST stop
```

pending interrupt 期间不能产生新的 early-free。这样比论文假设更保守，但符合当前 XiangShan 的 interrupt/flush 实现风险。

pending interrupt/trap/flush is handled as an ST stop/no-guardDec condition. 该策略不产生 UCA fallback event，也不要求 `int_er_fallback_pending_interrupt` counter。

## 17. 验证与断言

### 17.1 UCA 断言

- 同一 `trackId` 不能同时处于两个非 Invalid state。
- `entry.state === Counting` 时 `pdest =/= 0`。
- `UC` decrement 不得 underflow。
- `UC` saturate 后必须 set fallback。
- `fallback` entry 不得 early-free。
- `earlyFree` 只能在 `producedReady && redefinerNS && UC == 0` 时发出。
- `ReleasedWaitCommit` entry 不得参与 source CAM match。
- `trackGen` mismatch event 不得改变 state。
- 同一 uop 同一 `trackId/gen` 不得双 increment 或双 decrement。

### 17.2 FreeList 断言

- early-free preg 不为 0。
- commit free lanes 与 early-free lanes 无重复 preg。
- suppress conventional free 时，必须能找到 matching `ReleasedWaitCommit` entry。
- 未 suppress 的 conventional free 不得释放 matching `ReleasedWaitCommit` entry。
- early-free 后到 suppress 前，该 preg 可以仍在 arch RAT；这不是错误。
- 原有 “arch RAT or arch free list 二选一” invariant 在 ER 开启时必须替换或禁用。

### 17.3 ROB/ST 断言

- ST 不越过 unresolved entry。
- ST stop 条件为真时不输出 guardDec。
- ST 输出 guardDec 的 `redef` 必须来自 ROB/RAB 保存的 `IntERRedefTrack`。
- 如果 redirect squash 了已经 `redefinerNS` 的 redefiner，断言失败。
- readDone event 的 `robIdx` 必须指向 valid ROB entry，或者被 redirect 过滤为 stale event。
- readDone `srcIdx` 必须小于 `backendParams.numSrc`。
- STD readDone 的 `srcIdx` 必须是 original `1`，不是 local `0`。

### 17.4 Difftest 断言

- ER 开启时，integer `xrf` 不得由 `pregs_xrf + rat_xrf` 合成。
- ER 开启时，integer `commit_data` 不得由 `pregs_xrf[commit.wpdest]` 生成。
- direct shadow xrf 的 x0 始终为 0。
- 同 commit cycle 多 lane 写同一 logical reg 时，shadow xrf 最终值等于 commit order 最后一个 writer。
- move elimination commit 时，shadow xrf 更新值等于 `moveSrcLReg` 的 commit 前 architectural value。

## 18. Perf/Debug 计数器

建议在 `IntSparseUCA`、`Rename`、`ROB` 和 `MEFreeList` 分别增加：

```text
int_er_entry_alloc
int_er_entry_full_untracked
int_er_source_match
int_er_source_duplicate
int_er_uc_inc
int_er_uc_dec_read
int_er_uc_dec_squash
int_er_uc_guard_dec
int_er_uc_saturated_fallback
int_er_fallback_move
int_er_fallback_unsupported_consumer
int_er_fallback_unsupported_read_path
int_er_producer_ready
int_er_redefiner_seen
int_er_redefiner_ns
int_er_early_free
int_er_suppress_commit_free
int_er_late_event_gen_mismatch
int_er_active_counting_entries
int_er_active_released_wait_commit_entries
int_er_redirect_kill_entries
```

当前第一版不提供 `int_er_fallback_producer_not_ready` 或 `int_er_fallback_pending_interrupt` 稳定 counter。int_er_fallback_producer_not_ready is deliberately unsupported because UCA waits for producerReady after guardDec. int_er_fallback_pending_interrupt is deliberately unsupported because pending interrupt/trap/flush stops ST before guardDec.

阶段 0 observe-only 必须至少输出：

- potential early-free 次数；
- early-free 时 oldPdest 是否仍在 arch RAT；
- 若使用旧 difftest physical synthesis 会发生的 potential A/B/C mismatch；
- fallback reason 分布；
- `L` entry full 比例；
- pending interrupt/flush 导致 ST stop 的比例。

## 19. 实现阶段建议

### 阶段 0：observe-only

目标：

- 不改 `MEFreeList` 行为。
- 不 suppress conventional free。
- 建模 L-entry sparse UCA、source match、UC update、ST opportunity。
- 统计 Difftest physical synthesis 风险。

必须实现：

- Rename source/dest/redef match。
- DataPath readDone observation。
- ROB/ST opportunity observation。
- perf counters。

### 阶段 1：correctness-first early release

目标：

- 真正向 `MEFreeList` early-free integer preg。
- direct diff shadow xrf 生效。
- conservative resolved 策略。
- move/memory/uncertain/replay-prone path fallback。

建议配置：

```text
observeOnly = false
conservativeRedirectKill = true 或 precise recovery 已验证后 false
earlyFreeWidth = 1
stWalkWidth = 1 or 2
```

### 阶段 2：precise recovery 与更宽覆盖

目标：

- 打开 precise source squash decrement。
- 支持更多 ordinary int consumer read paths。
- 减少 redirect kill 带来的机会损失。
- 保持 Difftest direct path。

### 阶段 3：LSQ/memory resolved 优化

目标：

- load/store 不必全部到 commit 才 resolved。
- 接入 LSQ 已完成地址翻译、violation probe、TLB exception、store-load ordering 等信号。
- 逼近 Tempranillo 论文的主要收益。

第一版不建议同时做。

## 20. Directed Test 列表

功能测试：

- 单 producer 多 consumer，consumer out-of-order issue。
- `add x3, x1, x1` 同源重复，只计一次 UC。
- `L=1` 或小 L 时 entry full，新 producer untracked。
- UC 饱和 fallback。
- consumer 全部 readDone 后等待 redefinerNS。
- redefinerNS 后等待 long-latency consumer readDone。
- producer 未 writeback 时 ST 到达 redefiner，UCA 等待 producerReady，不产生 producer-not-ready fallback。
- branch mispredict 后 source squash decrement，或 conservative redirect kill。
- load cancel/replay 不得产生 readDone。
- STD source 从 STA `src1` 复制到 STD `src0`，readDone 回写 original `srcIdx=1`。
- move elimination source 命中 tracked entry 时 fallback。
- x0 不参与 tracking 或 early-free。
- pending interrupt/trap/flush 时 ST 不前进。

Difftest 测试：

- A/B/C physical reuse mismatch directed test。
- 同 commit group 多条写同一 xreg，shadow xrf commit order 正确。
- move elimination commit 更新 shadow xrf 正确。
- skip-diff instruction 不破坏 shadow xrf。
- `pregs_xrf` 保留时不参与 integer architectural compare。

FreeList 测试：

- early-free 与 conventional free 同周期不同 preg。
- early-free 后 redefiner commit suppress conventional free。
- early-free 后 preg 被 younger instruction 复用，suppress 仍按 `trackId/gen` 精确匹配旧 allocation。
- redirect 后未 early-free entries 被 invalidate，不产生 free-list 泄漏。

## 21. 主要实现风险

1. `MEFreeList` debug invariant 必须重写。旧 invariant 与 early release 语义冲突。
2. Rename old destination read 可能增加 RAT port 或 bypass timing 压力。
3. Source CAM 是 `RenameWidth * numIntSrc * L`，同周期 bypass match 可能在 rename 关键路径上。
4. ROB entry compression/multi-uop 会让 per-source metadata 变复杂；第一版必须限制 eligibility 或建立独立 source metadata buffer。
5. DataPath readDone 边界不能过早。错误 decrement 比不 decrement 更危险。
6. Move elimination 既影响 ER correctness，也影响 direct Difftest xrf。
7. Difftest preprocess 当前会自动合成 xrf/commit_data，需要显式 ER-aware，否则会出现伪 mismatch。
8. pending interrupt/trap/flush 的 non-speculative 边界必须保守，否则会释放 interrupt architectural cut-point 仍需要的值。
9. `ReleasedWaitCommit` entry 保留到 redefiner commit 会降低 L 的有效容量，但第一版可接受。
10. Source metadata 宽度不能按全局 `backendParams.numSrc` 无差别接入所有 bundle；`RegionInUop`、`SrcStatus`、`IssueQueueDeqOg1Payload` 必须跟随各自 params，并用 `srcIdx` 保留 original slot。
11. Raw DataPath readDone 不能直接进入 UCA；必须经 ROB 校验、去重、置 `readDone` 后再输出唯一 decrement，否则 redirect/walk 下存在双减风险。
12. DataPath final-success 同拍未必保存完整 `psrc/intER` metadata；需要 `readDoneShadow` 或显式扩展 `Og0/Og1/ExuInput`，第一版推荐 shadow。
13. Memory、STD、HyAddr、VF scheduler 的普通 `og1resp.finalSuccess` 当前为 false；若这些 consumer 被计数，必须有 Region/LSQ final-success readDone，或在 Rename fallback。
14. ST 不应组合查询 UCA 的 `producedReady/state`。默认由 UCA 接收 guardDec 后自行等待 `producedReady`，否则会形成 ROB/ST 到 Rename/UCA 的长路径。
15. Rename-time old-dest read 必须精确复现 RenameTable 的 hold、T0/T1 bypass、redirect/snapshot 和同组 older uop 顺序；做不到时不能开启 functional early-free。
16. conventional free suppress 必须与 `int_need_free`、`RegNext(int_old_pdest)` 同拍，且带 `trackId/gen/redefinerRobIdx` 校验，不能只按 `oldPdest` bit suppress。
17. Direct difftest `xrf/commit_data` 与现有 `pregs_xrf/rat_xrf` 并存前，必须先修改 preprocess；否则可能 elaboration 失败或生成重复 arch state。
18. Fallback event 必须携带受影响 tracked source key；空 fallback event 不能作为功能事件。

## 22. 推荐结论

当前 XiangShan 上实现 int-only early register release 的推荐结构是：

```text
Rename 内 IntSparseUCA:
  source CAM match
  L-entry tracked producer allocation
  L 个 UserCounterReg
  early-free request
  conventional-free suppress

ROB 内 IntERSpecTracker:
  resolved/specHead
  redefiner non-speculative 判断
  readDone 标记
  squash recovery event
  committed write data capture
  direct diff shadow xrf

Region/DataPath:
  携带 per-source IntERSrcTrack
  在 final-success 边界产生 IntERSrcValueReadDone

MEFreeList:
  接收 early-free stream
  聚合 conventional free 与 early free
  ER-aware duplicate/debug invariant

Difftest:
  direct integer architectural xrf
  direct per-commit integer commit_data
  不再使用 pregs_xrf + rat_xrf 合成 integer architectural state
```

该方案保留 `L` 个 UserCounterReg 的面积目标，同时承认 consumer source metadata、ROB/ST event、DataPath readDone 和 Difftest direct shadow 是 XiangShan 落地时不可绕过的结构成本。第一版应以 correctness-first 为目标：小 `L`、保守 ST、unsupported path fallback、direct diff shadow xrf 先落地，再逐步扩大 readDone 与 memory resolved 覆盖范围。
