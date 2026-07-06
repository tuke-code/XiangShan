# VAGQ 设计进度

> 最后更新: 2026-07-06

---

## 当前状态

- [x] 旧 VLSU 架构调研 (VLSplit, VSSplit, VLMergeBuffer, VSMergeBuffer, VSegmentUnit, MisalignBuffer)
- [x] RISC-V V Extension spec 语义分析 (constant-stride, indexed, segment, mask, tail, vstart)
- [x] 5 个关键设计决策全部敲定
- [x] VAGQ 设计方案完成 (`docs/design/vagq-plan.md`)
- [x] VAGQ 设计规则沉淀到 Codex skill (`~/.codex/skills/XiangShan/xiangshan-vagq/SKILL.md`)
- [x] VAGQ 核心 Chisel 框架已落地到 `src/main/scala/xiangshan/backend/vector/vagq/`
- [x] 已添加独立生成入口 `xiangshan.backend.vector.vagq.VAGQMain`
- [x] `SplitCtrl` active 路径已扩展为 2 路 `VAGQLsuReq`，一拍最多发两个 active 元素请求
- [x] `SplitCtrl` 已实现 LSQ-empty 请求，使用 `emptyMask` 跟踪 VAGQ byte 状态，使用 `entryMask` 定位 LSQ entry
- [x] LSQWrapper / LoadQueue / StoreQueue 已添加 empty mark 接口，LSQ 侧会确认目标 entry match 后 ACK，否则 NACK
- [x] `SplitCtrl` store active 请求已通过 VAGQ VRF read 路径读取 `psrc2` 作为 store data
- [x] `MemBlock` 已例化 `VAGQ` 和 `VAGQDownstreamAdapter`
- [x] VAGQ LSQ-empty req/resp 已通过 `VAGQDownstreamAdapter` 接到 `LSQWrapper`
- [x] VAGQ active load req 已在 `VAGQDownstreamAdapter` 中转成普通 vector load `ExuInput`
- [x] VAGQ active load req0/req1 已分别与 `issueLda0/issueLda1` 按 `robIdx` 仲裁，送入 `LoadUnit0/1`
- [x] `LoadUnit` 已支持 VAGQ load metadata、VAGQ response、NACK 重发语义和 active load 数据写 VRF
- [ ] 后端 issue/dispatch 到 VAGQ 的上游接入
- [ ] VAGQ active store req 到 STA/SQ 的下游适配与仲裁
- [ ] VAGQ VRF/ROB 写回与 release 的系统级接入
- [ ] segment 访存完整语义验证
- [ ] standalone RTL 完整生成
- [ ] 仿真验证

---

## 当前代码快照

### 已实现文件

| 文件 | 当前职责 |
|---|---|
| `Vagq.scala` | VAGQ 常量、Bundle、顶层连线，以及 `VAGQMain` 独立生成入口 |
| `EntryTable.scala` | VAGQ entry 寄存器表，地址侧/数据侧配对，状态与 bitmap 更新 |
| `MaskGen.scala` | 根据 `vl/vstart/vm/v0/vma/vta/deew/uopIdx` 生成 active 和 agnostic byte mask |
| `AddrGen.scala` | 根据 stride/indexed 类型生成元素地址、元素 byte mask、`elemIdx` |
| `SplitCtrl.scala` | 从 `split` 状态 entry 中挑选 pending byte，发出 active LSU 请求或 LSQ-empty 请求 |
| `MergeCtrl.scala` | 处理 ACK/NACK/exception，load merge 旧 `vd`，ROB 写回和异常写回 |
| `VAGQUtils.scala` | entry 选择、mask、merge、`faultVstart` 等公共 helper |

### 相关 MemBlock / LSU 文件

| 文件 | 当前职责 |
|---|---|
| `mem/vector/VAGQDownstreamAdapter.scala` | VAGQ active load req 与普通 `issueLda` 的 `robIdx` 仲裁；把 active load req 转成 LDU `ExuInput`；透传 LDU response 和 LSQ-empty req/resp |
| `mem/pipeline/Bundles.scala` | 定义 `VAGQMemPipelineMeta`，在 load/store pipeline 内携带 `entryIdx/robIdx/byteOffset/mask` 等 VAGQ 元信息 |
| `mem/pipeline/NewLoadUnit.scala` | 识别 VAGQ load，生成 `VAGQResp`，NACK 时交由 VAGQ 清 `reqSent` 重发，成功时通过普通 vector load VRF 写口写 active 数据 |
| `mem/pipeline/NewStoreUnit.scala` | 已有 VAGQ metadata 和 `VAGQResp` 支持，但当前 MemBlock 尚未向 STA 送 VAGQ active store req |
| `mem/MemBlock.scala` | 例化 VAGQ 和 downstream adapter；连接 LDU active load、LDU response、LSQ-empty path；上游和 VRF/ROB 仍 tie-off |

### 相关 LSQ 文件

| 文件 | 当前职责 |
|---|---|
| `mem/lsqueue/LSQBundle.scala` | 定义 `LqEmptyMarkReq` / `SqEmptyMarkReq`，以及 SQ empty mark 端口 |
| `mem/lsqueue/LSQWrapper.scala` | 接收 `VAGQLsqEmptyReq`，分发到 LQ/SQ，并把 mark success 转成 `VAGQLsqEmptyResp.isNACK` |
| `mem/lsqueue/LoadQueue.scala` / `VirtualLoadQueue.scala` | 接收 load empty mark，确认 `allocated/isvec/robIdx/lqBaseIdx/!needCancel` 后置 `committed` |
| `mem/lsqueue/NewStoreQueue.scala` | 接收 store empty mark，确认 `allocated/isVec/robIdx/sqBaseIdx/!needCancel/!targetDeqCancel` 后置 `vecInactive` |
| `mem/MemBlock.scala` | 当前已将 `vagqDownstream.io.lsqEmptyReq` 接入 `lsq.io.lsqEmptyReq`，并把 `lsq.io.lsqEmptyResp` 返回 VAGQ |

### 核心能力

- `VLEN` 当前固定为 128 bit，单个 VAGQ flow 为 16 byte。
- `VAGQSize` 当前常量为 8，entry index 宽度由 `VAGQConstants.VAGQSize` 推导。
- `ActiveIssueWidth=2`，`LduRespWidth=3`，`StaRespWidth=2`，`MergeRespWidth=6`。
- 每个 entry 使用 `reqSent` / `reqAck` 两个 16 bit bitmap 跟踪每个 byte lane 的请求状态。
- entry 状态已覆盖 `waitA`、`waitSI`、`split`、`merge`、`wb`、`excp`。
- 地址侧和数据侧可以乱序到达同一个 entry：只有地址侧先到进入 `waitSI`，只有数据侧先到进入 `waitA`，两侧齐备后进入 `split`。
- `SplitCtrl` active/empty 入口都通过 `oldestEntryOH` 按 `robIdx/uopIdx/entryIdx` 选择最老 entry；active lane0 选最低位 active 元素，lane1 从剩余 active mask 中选最高位 active 元素。
- `SplitCtrl` empty 路径会把一个 entry 中尚未发送、尚未 ACK 的非 active byte 合成一个 `lsqEmptyReq`，用于 prestart/inactive/tail 的 LSQ 空项标记。
- active 路径和 empty 路径各自有独立 `splitUpdate`，同拍 fire 时分别置位对应 `reqSent` bitmap。
- ordered indexed entry 在已有 active 请求未 ACK 时会阻止继续发新的 active 请求。
- `MergeCtrl` 接受 3 路 LDU response、2 路 STA response 和 1 路 LSQ-empty response；只接受 `entryIdx` 有效且 `robIdx` 匹配当前 live entry 的响应，避免旧响应误更新新 entry。
- `EntryTable` 对多路 exception update 使用 `PriorityMux` 选出唯一异常写入 entry；当前优先级来自 update lane 顺序，不额外比较 `faultElemIdx`。
- load merge 当前生成完整 128 bit 写回数据，并用 `~elemActiveMask` 作为 VRF 写 mask，只覆盖非 active byte；active load data 由 `LoadUnit` 使用普通 vector load VRF 写口写回。
- 异常路径会记录本 uop 内的 fault byte offset，并在 ROB 写回时换算为架构元素序号 `faultVstart`。
- redirect 会在 entry 表、split 候选、merge/writeback 候选路径上过滤或清除被冲刷 entry。
- LSQ empty mark 成功条件会确认目标 LQ/SQ entry 仍分配、是 vector 项、`robIdx` 和 base `lqIdx/sqIdx` 匹配，且未被 flush/cancel；失败时返回 NACK，VAGQ 清除对应 `reqSent` 并允许重试。
- `VAGQDownstreamAdapter` 当前只处理 load active req：lane0/lane1 分别和 `issueLda0/issueLda1` 比较 `robIdx`，更老者进入 `LoadUnit0/1`；`issueLda2` 直接进入 `LoadUnit2`。
- `VAGQDownstreamAdapter` 当前不处理 store active req，`vagqStaResp` 全部置 invalid。
- `MemBlock` 当前把 `vagq.io.addrUop`、`dataUop`、`vrfReadReq/Resp`、`robWriteback.ready` 仍 tie-off，因此 VAGQ 尚未端到端执行真实指令。

---

## 当前未完成项

- 当前仓库还没有后端 issue/dispatch 到 VAGQ 的上游接入；`MemBlock` 中 `addrUop/dataUop` 仍置 invalid。
- `VAGQLsuReq` 当前只携带精简请求字段，没有完整 `DynInst`；load adapter 只补了 LDU 当前能跑通的 `ExuInput` 字段，后续若 STA/异常/debug 需要更多上下文还要继续补齐。
- active load 下游已部分接入，但只覆盖 `vagqLsuReq(0/1)` 到 `LoadUnit0/1`；没有实现“从所有普通 load + active load 中选全局最老 3 个”的完整仲裁。
- active store 下游未接入：adapter 没有把 store req 转成 STA input，`MemBlock` 也仍给 `StoreUnit.vagqReqMeta` 接 0。
- `SplitCtrl` 已能通过 VAGQ VRF read 读取 store data，但 `MemBlock` 当前没有连接 VAGQ VRF read/write 接口，store data read 和 load non-active merge 在系统级不可用。
- `VAGQ.robWriteback` 当前在 `MemBlock` 中 `ready := false.B`，VAGQ 完成/异常写回和 release 尚未系统级接入。
- 当前没有 `SegmentCtrl.scala`；`nf` 只在 VAGQ bundle/request 中透传，segment zip/unzip 或按 segment 字段的完整语义还没有实现验证。
- standalone RTL 生成入口已经加入，但当前工作树没有保留 `vagq/VAGQ.fir` 或 `VAGQ.sv` 产物。此前使用 `/nfs/home/share/firtool-1.74.0/bin/firtool` 时会卡在 Chisel 7 `layer Verification` FIRRTL 语法，需换用匹配 Chisel 7 的更新版 firtool 后再验证。
- 尚未看到针对 VAGQ 的单元测试、随机测试或完整仿真回归记录。

---

## 决策记录

| # | 决策 | 日期 |
|---|---|---|
| 1 | 复用 StaIQ + StdIQ/VStdIQ，vlsa/vlss 配对 | 2026-06-04 |
| 2 | N 可配置，bitmap (16b) 追踪至多 16 flow | 2026-06-04 |
| 3 | Unit-Stride 不进 VAGQ，保留原 VLSU 通路 | 2026-06-04 |
| 4 | Segment 合并到 VAGQ 作为正交维度 (nf=2~8) | 2026-06-04 |
| 5 | VAGQ 不处理非对齐/跨页 | 2026-06-04 |
| 6 | 当前实现先落地 VAGQ 核心模块，暂未完成上游/下游流水线集成 | 2026-06-29 |
| 7 | 当前实现中 `faultElemIdx` 是 16B flow 内 byte offset，ROB 写回时再换算为元素级 `faultVstart` | 2026-06-29 |
| 8 | 当前实现与早期计划不同：尚未实现 decode 阶段显式拆成地址侧/数据侧 uop 的全链路方案 | 2026-06-29 |
| 9 | VAGQ LSQ-empty req 使用 `emptyMask` 维护 VAGQ byte 状态，使用 `entryMask` 标记 LSQ entry；LSQ match 失败返回 NACK | 2026-07-02 |
| 10 | active req 当前一拍最多发两路，`SplitUpdateWidth=2` 表示 active update 和 empty update 两路状态更新源 | 2026-07-02 |
| 11 | 当前 VAGQ 下游接入采用 `VAGQDownstreamAdapter`：active load req0/1 分别与普通 LDU0/1 请求按 `robIdx` 仲裁，LDU2 不参与 VAGQ 仲裁 | 2026-07-06 |
| 12 | 当前 response 宽度拆成 `LduRespWidth=3` 和 `StaRespWidth=2`；`MergeCtrl` 加上 LSQ-empty response 后共处理 6 路 update | 2026-07-06 |
| 13 | 当前 active store 的 STA 接入、VAGQ VRF 接入、ROB 写回/release 接入仍未完成 | 2026-07-06 |

---

## 下一步建议

1. 先打通最小上游路径：在 dispatch/issue payload 中携带 `entryIdx/uopIdx/psrc2`，把 strided/indexed vector memory uop 送入 VAGQ。
2. 接入 VAGQ VRF read/write：让 store data read、load old-vd merge 和非 active byte masked write 真正连到 VRF。
3. 接入 ROB 写回和 release：让 `robWriteback` 被系统消费，并在完成/异常/flush 后释放对应 VAGQ entry。
4. 接入 active store 下游路径：把 `VAGQLsuReq` store 转成 STA input，连接 `StoreUnit.vagqReqMeta` 和 `vagqStaResp`。
5. 评估是否需要把 load 仲裁从当前 lane-by-lane `robIdx` 仲裁升级为全局最老 3 个请求选择。
6. 验证 load-only constant-stride，再扩展到 indexed unordered、indexed ordered、store、mask/tail/vstart、exception。
7. 更换 firtool 版本后重新跑 `VAGQMain`，确认至少能生成 standalone RTL。

---

## 经验教训

- VAGQ.d2 是当前的拓扑图，不要误认为是旧的
- Constant-Stride 和 Unit-Stride 是不同的 RISC-V 指令，不可混淆
- Segment 是与访存类型正交的维度，原 VSegmentUnit 是设计失误
- 设计文档本身是交付物，但进度文档必须区分“设计目标”和“当前代码已实现”
- skill 里的快照可能比当前工作树更超前，落文档前必须用 `rg` 核对仓库实际文件

(待补充)
