# VAGQ 设计进度

> 最后更新: 2026-06-29

---

## 当前状态

- [x] 旧 VLSU 架构调研 (VLSplit, VSSplit, VLMergeBuffer, VSMergeBuffer, VSegmentUnit, MisalignBuffer)
- [x] RISC-V V Extension spec 语义分析 (constant-stride, indexed, segment, mask, tail, vstart)
- [x] 5 个关键设计决策全部敲定
- [x] VAGQ 设计方案完成 (`docs/design/vagq-technical-plan.md`)
- [x] VAGQ 设计规则沉淀到 Codex skill (`~/.codex/skills/XiangShan/xiangshan-vagq/SKILL.md`)
- [x] VAGQ 核心 Chisel 框架已落地到 `src/main/scala/xiangshan/backend/vector/vagq/`
- [x] 已添加独立生成入口 `xiangshan.backend.vector.vagq.VAGQMain`
- [ ] 后端 issue/dispatch 到 VAGQ 的上游接入
- [ ] VAGQ 到 MemBlock/LSQ/LSU/STA/VRF/ROB 的下游接入
- [ ] store data 通路接入
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

### 核心能力

- `VLEN` 当前固定为 128 bit，单个 VAGQ flow 为 16 byte。
- `VAGQSize` 当前常量为 8，entry index 宽度由 `VAGQConstants.VAGQSize` 推导。
- 每个 entry 使用 `reqSent` / `reqAck` 两个 16 bit bitmap 跟踪每个 byte lane 的请求状态。
- entry 状态已覆盖 `waitA`、`waitSI`、`split`、`merge`、`wb`、`excp`。
- 地址侧和数据侧可以乱序到达同一个 entry：只有地址侧先到进入 `waitSI`，只有数据侧先到进入 `waitA`，两侧齐备后进入 `split`。
- `SplitCtrl` 会优先发 active LSU 请求；没有 active pending 时才发 LSQ-empty 请求。
- ordered indexed entry 在已有 active 请求未 ACK 时会阻止继续发新的 active 请求。
- `MergeCtrl` 只接受 `entryIdx` 有效且 `robIdx` 匹配当前 live entry 的响应，避免旧响应误更新新 entry。
- load merge 当前生成完整 128 bit 写回数据，并用 `~elemActiveMask` 作为 VRF 写 mask。
- 异常路径会记录本 uop 内的 fault byte offset，并在 ROB 写回时换算为架构元素序号 `faultVstart`。
- redirect 会在 entry 表、split 候选、merge/writeback 候选路径上过滤或清除被冲刷 entry。

---

## 当前未完成项

- 当前仓库中还没有 `VAGQUpstreamAdapter`、`VAGQMemBlockAdapter`、`VAGQMemInterface` 等接入文件；VAGQ 仍是 backend/vector 下的核心模块，尚未接入真实后端和访存流水线。
- `Dispatch.scala`、issue queue payload、`VecRegionModule`、`MemBlock.scala` 当前未携带或仲裁 VAGQ 请求相关信号。
- `SplitCtrl` 中 store 请求的 `data` 仍为 `0.U // todo: storeData`，store data 侧尚未真正接入。
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

---

## 下一步建议

1. 先打通最小上游路径：在 dispatch/issue payload 中携带 `entryIdx/uopIdx/psrc2`，把 strided/indexed vector memory uop 送入 VAGQ。
2. 接入最小下游路径：把 `VAGQLsuReq` 转成现有 LDU/STA 可消费的请求，并把 ACK/NACK/exception 转回 `VAGQResp`。
3. 补上 store data：由 VStd 或额外 VRF read 路径提供 store 元素数据，替换当前 `SplitCtrl` 里的 `0.U`。
4. 验证 load-only constant-stride，再扩展到 indexed unordered、indexed ordered、store、mask/tail/vstart、exception。
5. 更换 firtool 版本后重新跑 `VAGQMain`，确认至少能生成 standalone RTL。

---

## 经验教训

- VAGQ.d2 是当前的拓扑图，不要误认为是旧的
- Constant-Stride 和 Unit-Stride 是不同的 RISC-V 指令，不可混淆
- Segment 是与访存类型正交的维度，原 VSegmentUnit 是设计失误
- 设计文档本身是交付物，但进度文档必须区分“设计目标”和“当前代码已实现”
- skill 里的快照可能比当前工作树更超前，落文档前必须用 `rg` 核对仓库实际文件

(待补充)
