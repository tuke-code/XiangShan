# MemBlock 标量 Load/Store Pipeline 白盒功能验证方案与实施路线

## 1. 文档目的与目标

本文档面向 `src/main/scala/xiangshan/mem/pipeline/` 下的标量 load/store 流水线，设计一套适配当前真实 MemBlock DUT 验证环境的白盒功能验证方案。这里强调“白盒”，并不是要在 Python 侧重写一份 RTL 等价模型，而是要围绕流水线阶段职责、排序关系、状态可见性和 replay/violation 机制，构造比传统“发请求、等写回、看结果”更细粒度、可定位、可扩展的验证体系。

当前环境已经具备若干关键基础能力：

1. 真实 DUT 驱动，而非 mock-only memory model。
2. load 以 commit-boundary 视图做在线 compare。
3. store 采用 deferred visibility，不要求每条 store 执行后立刻更新最终 golden memory。
4. 测试结束时可以通过 `flush_store_buffers_and_wait()` 收尾，并以 drain log 与最终 golden memory 做一致性核查。
5. 已经具备 FF、DM、NC、RAW、RAR 等 replay 场景的真实 DUT 冒烟测试能力。
6. 已经有 sequence、agent、monitor、scoreboard、transport responder 等基础设施，可支撑更系统的白盒验证扩展。

当前已经落地、可直接参考的 probe 级 directed case 见：

- `src/test/python/MemBlock/docs/scalar_load_pipeline_probe_cases.md`

因此，本方案的重点不是再造一个新 testbench，而是在现有验证环境之上，建立一套面向标量 load/store pipeline 的功能点划分、场景矩阵、白盒观测、实施阶段和验收标准。目标是：

- 尽可能覆盖标量 load/store pipeline 已完成的核心功能；
- 让回归失败能够快速定位到具体流水段、具体 replay 原因或具体排序窗口；
- 让 testcase 组织、sequence 抽象、scoreboard 判定和最终收尾之间形成闭环；
- 为后续增量补强 misalign、partial overlap、复杂 mixed traffic、更多 replay cause 等场景提供稳定骨架。

本文档只聚焦标量 load/store 功能验证，不把向量访存、原子指令、完整 CBO/prefetch、性能计数与带宽评估纳入本轮主体方案。它们可以在未来复用同样的方法论单独展开，但不应干扰当前标量 pipeline 的主验证闭环。

## 2. 验证对象范围与设计背景

### 2.1 主要 RTL 范围

本方案的主验证对象固定为以下三个模块：

- `src/main/scala/xiangshan/mem/pipeline/NewLoadUnit.scala`
- `src/main/scala/xiangshan/mem/pipeline/StoreUnit.scala`
- `src/main/scala/xiangshan/mem/pipeline/StdExeUnit.scala`

其中：

- `NewLoadUnit` 是当前标量 load 主流水线实现，承担请求仲裁、TLB/DCache 路径处理、forward、replay、违例恢复、写回等职责。
- `StoreUnit` 是标量 store 地址流水线，实现地址计算、TLB/PMA/PBMT 处理、与 load 的 st/ld ordering 交互、SQ addr-ready 标记与地址侧反馈。
- `StdExeUnit` 是标量 store 数据流水线，实现 store data 写入 StoreQueue、data-ready 标记、STA/STD 分离执行的时序闭环。

虽然老版本的 `LoadUnit.scala` 仍存在，但从当前 MemBlock 架构和已有环境兼容工作来看，主 load 路径已经由 `NewLoadUnit` 主导。因此，本文档以 `NewLoadUnit` 为主参考对象，不再围绕历史 load 实现组织章节。这样做的原因很简单：验证方案如果围绕旧模块逻辑展开，会直接导致白盒观测点与当前 DUT 真正的失效边界脱节。

### 2.2 相关协同模块

虽然本文档的主对象是 pipeline 目录下的三个标量模块，但要形成完整验证闭环，仍需把以下协同模块视为语义背景：

- LSQ：包括 LoadQueue、StoreQueue、LoadQueueReplay、LoadQueueRAR、LoadQueueRAW 等；
- SBuffer：承担已提交 cacheable store 的合并、缓冲和向外写出；
- Uncache/MMIO 通路：承担 NC/MMIO 请求的特殊语义与外部事务；
- DCache/TL 通道：命中、miss、refill、release/probe 影响 load/store 的返回路径与违例检测；
- TLB/PMP/PMA/PBMT：决定请求地址属性、命中/缺失、cacheable/uncache/mmio 分流；
- Misalign buffer：承接标量非对齐访问的重放与分裂处理。

这些模块不是本文档的主体，但标量 pipeline 的很多关键功能点恰恰是通过这些边界暴露出来的。例如，`DM` 与 `DR` 的差别体现在 DCache miss 接受与否，`FF` 取决于 older store 的 addr/data 就绪状态，`RAR` 依赖 load queue 中更老/更年轻 load 的交互与 release/probe 事件。因此，白盒验证必须把“pipeline 本体”与“边界协同语义”一起纳入观察。

## 3. 为什么需要白盒而不是只做黑盒

如果只从黑盒角度验证标量 load/store，最直观的做法是：发一个 load/store，请求跑完，看最终 writeback 或最终内存是否正确。这样的验证当然必要，但远远不够，原因主要有五类。

### 3.1 replay 机制不是最终结果可直接区分的

许多 load 即便中途经历了 replay，最终仍然会写回正确数据。如果只看最后的 data compare，那么：

- `FF`、`DM`、`NC`、`RAW`、`RAR` 这些路径全部可能被“最终成功”掩盖；
- replay cause 错了、优先级错了、source 错了、重发窗口错了，都可能完全看不出来；
- 某些 bug 会以“偶尔多拍”“偶尔回滚”形式出现，黑盒很难定位是 replay 条件误判还是 replay 恢复机制错误。

### 3.2 store 是地址与数据分离执行的

XiangShan 的标量 store 不等于一个单拍原子事务，而是地址流水与数据流水分开执行，STA/STD 的先后排列会直接影响：

- younger load 是否可以前递；
- older store 是否触发 `FF`；
- load compare 的 architectural view 什么时候应该推进；
- 最终 drain 时真实写出的 data 与 golden memory 是否一致。

如果只看“store 最后有没有写出去”，就无法覆盖 store data 侧尚未准备、地址已准备但数据未准备、store committed 但尚未 drain 等关键时序窗口。

### 3.3 ordering/violation 类问题必须看程序序与内部事件

RAW、RAR、NK 这一类问题的本质不是“某条 load 的数据错了”这么简单，而是“程序序、提交序、全局内存可见性与微结构执行顺序之间出现了不一致”。这类问题只看最终值是不够的，因为：

- 错误可能在回滚判定，而不是在第一次返回数据；
- older/younger 谁先写回、谁被 nuke、谁被 replay，本身就是需要验证的对象；
- 某些 case 只有在存在 probe/release、load-wait、waitForRobIdx 等特殊窗口时才暴露。

### 3.4 cacheable / NC / MMIO / misalign 路径不共享同一套“结果语义”

cacheable load/store 的正确性不能完全套用 NC/MMIO 的规则，原因包括：

- cacheable store 的最终对外可见性可能滞后于 commit；
- NC/MMIO 更依赖 outer path、强顺序或专门 buffer；
- misalign 标量访问可能经过 buffer、split 或 replay，而不是一个普通 hit/miss 流程。

如果用一套统一的“issue 后立刻更新 golden memory，再比最终值”模型去判定，既容易误报，也容易漏报。

### 3.5 回归维护需要定位能力

当前 MemBlock 验证环境已经在向“可持续维护的真实 DUT 回归平台”演进。如果只保留黑盒断言，一旦大版本 RTL 更新，失败现象通常只剩下：

- 某条 load 超时没写回；
- 某个 store 最后没写出去；
- 某个随机混合流在数百拍后 mismatch。

这种信息量对于调试几乎不够。白盒验证的目标不是替代最终语义检查，而是为每类失效提供可归因的中间事实：请求走了哪条路径、在哪个阶段 replay、是否被 probe/release 干扰、older store 是否 materialized、drain 是否真正覆盖了期望地址集合。

## 4. 白盒验证方法论

### 4.1 核心原则：白盒观测，黑盒收口

本方案不建议把 Python memory model 做成 RTL 的逐级翻版，而是采用“白盒观测，黑盒收口”的结构：

1. **白盒观测**：记录真实 DUT 在关键边界上暴露的事实，例如 replay cause、store shadow、writeback、release、nuke query、outer put、sbuffer drain。
2. **架构判定**：load 仍以 golden memory 的 architectural view 在线 compare，store 仍以最终 drain 与 golden memory 的一致性收口。
3. **语义归因**：白盒事件用于解释“为什么会这样”，而不是成为唯一通过标准。

这样做有三个好处：

- 不需要维护一份高成本、易漂移的“软件版完整 LSU”；
- 仍能利用 RTL 已经暴露的调试/状态端口快速定位；
- 当 DUT 内部局部重构但边界语义不变时，环境更容易维护。

### 4.2 三条主线：事务、排序、可见性

为了避免白盒验证变成零散信号采样，本文档把所有标量 load/store 场景统一投影到三条主线上。

#### 4.2.1 事务主线

事务主线回答“这条请求实际经历了什么路径”：

- issue 到哪个 lane；
- 经过哪类路径返回数据；
- 是否发生 replay、miss、release、nuke、drain；
- 最终在何处完成写回或写出。

这条主线主要服务于路径定位与 replay 归因。

#### 4.2.2 排序主线

排序主线回答“这条请求与其它请求的程序序关系如何影响结果”：

- robIdx/lqIdx/sqIdx 的相对顺序；
- older store 是否已经 committed、addr/data 是否 ready；
- older load 是否因 load-wait、waitForRobIdx 或 RAR 约束而暂停；
- younger load/store 是否因为 RAW/RAR/NK 触发 replay 或 nuke。

这条主线主要服务于 violation/replay/ordering 场景。

#### 4.2.3 可见性主线

可见性主线回答“在某个时刻，哪一层 memory view 应该被拿来判定正确性”：

- load writeback compare 时使用的是 commit-boundary architectural view；
- store 执行后不要求立刻进入最终 memory image；
- flush/drain 后，用真实 drain-out write data 与最终 architectural view 做一致性核对；
- MMIO 不纳入一般 cacheable golden memory compare 范围，但要单独验证事务完成与顺序语义。

这条主线决定了 memory model 与最终判断逻辑的边界，是防止误判的关键。

## 5. 功能点划分与划分原因

本方案不按“测试文件”或“接口名”来划分功能点，而是按“流水级职责 + 容易出错的控制边界 + 可收口的语义”划分。这样可以把复杂功能拆成可管理的验证域，同时保留跨域场景的组合空间。

### 5.1 功能域 A：Load 发射与前半流水

覆盖重点：

- s0 请求来源仲裁：普通标量 load、replay load、NC/MMIO、misalign 流入等；
- 地址生成与低位 offset 处理；
- TLB 请求形成与命中/缺失分流；
- dcache 请求是否正确建立；
- 初始 wakeup/cancel 的边界。

划分原因：

许多问题在请求还没真正读到数据前就已经发生，例如 lane 发错、地址偏移错、TLB/属性分流错、请求根本没有进入期望通道。若把这些问题都压到最终写回才看，就会把“请求形成错误”和“返回数据错误”混在一起，定位成本很高。

### 5.2 功能域 B：Load s1/s2 数据来源选择与 replay 生成

覆盖重点：

- dcache hit 返回；
- SQ forward、SBuffer forward、outer/nc path、miss path；
- bank conflict、MSHR 接收与否；
- replay cause：`C_MA`、`C_TM`、`C_FF`、`C_DR`、`C_DM`、`C_WF`、`C_BC`、`C_RAR`、`C_RAW`、`C_NK`、`C_MF`；
- replay 优先级与观测 source。

划分原因：

这是标量 load 最复杂、也最容易出现“最终成功但中间语义错”的区域。尤其是 forward/replay 的判定，单看最终 data 很难发现问题；必须把 replay cause 和数据来源路径当成一等公民。

### 5.3 功能域 C：Load s3 写回、取消唤醒与违例恢复

覆盖重点：

- 正常 writeback 数据与 robIdx 对应关系；
- load 在线 compare 的时机；
- replay 后写回、写回前 kill、cancel wakeup；
- memory violation 上报；
- RAR/RAW/nuke 响应后的恢复路径。

划分原因：

很多缺陷不是“读到了错误数据”，而是“在应该取消时没取消”“在应该回滚时直接提交了”“第一次写回错地算成最终完成”。这类问题只看 replay 本身不够，必须看写回与最终 compare 的边界。

### 5.4 功能域 D：Store 地址流水

覆盖重点：

- s0 地址形成与来源仲裁；
- s1 TLB 响应、PMA/PBMT/MMIO/NC 分类；
- stld nuke query 与 load side ordering 检查；
- SQ addr-valid 标记；
- store 地址侧 feedback 与重发语义。

划分原因：

store 地址路径既影响自身最终写出，也影响 younger load 是否 replay。许多看似“load 出错”的现象，本质可能是 older store 地址侧状态没正确进入 SQ，因此必须把 StoreUnit 作为独立验证对象。

### 5.5 功能域 E：Store 数据流水

覆盖重点：

- `StdExeUnit` 何时把 data 写入 SQ；
- datavalid 何时置位；
- STA 先于 STD、STD 先于 STA、同拍到达等排列；
- store data 宽度、mask、partial byte lane；
- older store data 未就绪触发 `FF` 的条件。

划分原因：

这部分功能表面上像“只是写个数据”，但实际上决定了 cacheable store->load forwarding 的可用性，也决定了 deferred store visibility 的推进边界。把它单独划出，才能系统覆盖 FF 与部分重叠写入问题。

### 5.6 功能域 F：Store 提交、SBuffer/Outer Drain 与最终可见性

覆盖重点：

- committed/completed 的 shadow 观测；
- sbuffer enqueue 与对外 drain；
- NC store 通过 outer Put* 写出；
- flushSb 收尾、sbIsEmpty、settle 窗口；
- drain_log 与最终 architectural memory 一致性。

划分原因：

store 的“执行完成”和“对外真正可见”不是同一件事。若不把 commit/drain 这一层单独建模，就无法验证 cacheable store 延迟可见、flush 收尾和 outer/nc 写出路径。

### 5.7 功能域 G：地址空间与属性分流

覆盖重点：

- cacheable / NC / MMIO；
- TLB miss / PMA / PMP / PBMT；
- misaligned scalar 访问；
- 不同地址空间对 compare/drain 规则的影响。

划分原因：

地址属性直接改变路径选择和判定规则。例如 MMIO store 不该被普通 cacheable golden memory compare 直接处理，NC load/store 的数据来源也不走普通 dcache 命中语义。因此该域必须作为全局横切维度纳入所有场景库。

### 5.8 功能域 H：顺序一致性、违例检测与恢复

覆盖重点：

- store->load same-addr、partial overlap、unrelated；
- load->load same-addr 及 RAR violation；
- older unresolved store 导致 younger load replay；
- waitForRobIdx、load-wait、release/probe 干扰；
- RAW、RAR、NK 等顺序相关 replay/violation。

划分原因：

这是标量 LSU 最能体现“微结构正确性”的区域。很多 bug 并不表现为单条指令结果错误，而是表现为程序序与内存序在特殊窗口下失配。因此该域必须独立成章，并贯穿 load/store 两条流水线。

## 6. 当前验证环境能力映射

当前 `src/test/python/MemBlock/` 下的真实 DUT 环境已经具备可观的基础设施，本方案应明确复用这些能力，而不是跳过它们重写一套新系统。

### 6.1 环境组织能力

当前环境已经分为：

- `MemBlockEnv` facade；
- active agents：issue、commit、csr、lsq；
- passive monitors：writeback、store、mem status；
- model 组件：`RefMemory`、`TransportResponder`、`Scoreboard`；
- sequences 与 request API。

这意味着新的 pipeline 白盒验证主要需要增加的是：

- 更系统的场景组织；
- 更明确的功能域覆盖；
- 少量必要的新观测 helper；
- 针对缺口的 directed sequence。

而不需要重写 env 顶层结构。

### 6.2 参考模型与收口能力

当前 memory model/scoreboard 已经具备以下关键语义：

1. load writeback compare 使用 commit-boundary architectural view。
2. store 不按 drain 时序即时推进 golden memory，而是在 load compare 边界推进 older committed store。
3. 测试结束后可通过 flush/drain 统一收口，检查真实 drain-out write data 与最终 golden memory 的一致性。
4. MMIO、NC、cacheable store 已有不同路径处理框架。

这为“load 在线 compare + store 末尾统一 drain 校验”提供了稳定基础，是本文档所有场景矩阵的判定前提。

### 6.3 已有 sequence 能力

当前环境已具备以下可直接复用的 sequence 类型：

- `ScalarLoadSequence`
- `ScalarStoreSequence`
- `ScalarStoreThenLoadSequence`
- `ScalarMixedTrafficSequence`
- `ScalarForwardFailReplaySequence`
- `ScalarCacheMissReplaySequence`
- `ScalarNcReplaySequence`
- `ScalarRawReplaySequence`
- `ScalarRarViolationSequence`

这些 sequence 已经覆盖了单笔 primitive、store/load 组合、mixed traffic、典型 replay 冒烟等高频场景。本方案建议在此基础上继续扩展，而不是把 testcase 重新降级为 pin-level 驱动脚本。

### 6.4 已有白盒观测接口

当前 env 已暴露的 replay/白盒观测接口非常关键，包括但不限于：

- `sample_replay_state()`
- `wait_replay_event()`
- `wait_nc_replay_or_nc_out()`
- `collect_replay_window()`
- `wait_nuke_query_backpressure()`
- `wait_release_event()`
- `wait_rar_nuke_response()`
- `wait_load_writeback_observed()`

这些接口的价值在于：它们让 testcase 可以围绕“语义事件”来断言，而不是直接绑定大量脆弱端口名。因此，后续若要补新白盒观测，也应优先沿用“env facade 输出结构化事件”的方式。

### 6.5 当前缺口

虽然已有基础很好，但要覆盖“尽可能完整的标量 load/store pipeline 功能”，仍存在以下缺口：

1. 宽度与掩码覆盖仍偏少，B/H/W/D、partial overlap 的 directed case 需要系统化。
2. STA/STD 组合主要服务于 FF 冒烟，尚未形成成套矩阵。
3. cacheable store->load same-addr、多条 store 覆盖、mixed ld/st 小规模窗口还可以继续丰富。
4. misaligned scalar 的明确覆盖仍不足，需要单独章节与阶段性策略。
5. 部分 replay cause 如 `DR`、`TM`、`NK`、`MF` 尚未形成稳定专项；`BC` 已有基础 smoke 与 probe 级专题，但仍未形成完整矩阵。
6. queue occupancy/backpressure 与 release/probe 干扰可以更系统地纳入场景库。

因此，方案重点是“体系化补全”，而不是“推倒重来”。

## 7. 白盒观测点与判定闭环

### 7.1 issue 与事务标识

每个场景必须首先保证请求的事务身份是可追踪的：

- `robIdx` 用于程序序与 commit-boundary compare；
- `lqIdx`/`sqIdx` 用于 LSQ ordering、RAR/RAW、store shadow 绑定；
- `req_id` 用于 testcase/sequence 层标识；
- 地址、宽度、mask 用于最终 compare 与 overlap 归因。

原则上，任何复杂场景都不应只凭“我大概发了第几个请求”来判定 replay/violation，而应保留事务标识到最终断言。

### 7.2 load 在线 compare

load 的最终正确性仍然通过在线 compare 收口，但 compare 规则固定如下：

1. `expect_load()` 登记的是结构化期望，而不是 issue 当下直接预读的 expected_data。
2. 当观测到对应 writeback 时，先按该 load 的 rob 边界把所有 older committed 且 addr/data 有效的 store 合并进 architectural memory。
3. 再从更新后的 architectural view 中读出期望值，与实际写回结果比对。

采用这一规则的原因在于：cacheable store 的最终对外 drain 时机不等于该 store 对后续 younger load 的可见时机。只要 older store 已达到提交条件，它就应该体现在 younger load 的架构视图里，而不应等到 sbuffer 真正把它写到外部。

### 7.3 replay correctness

对于 replay 类场景，仅验证“最终写回正确”是不够的，必须同时验证：

- replay 事件确实发生；
- cause 匹配设计预期；
- source 合理；
- 关键关联字段如 robIdx/sqIdx/paddr 合理；
- replay 后事务最终能够恢复并完成；
- 恢复后的最终 compare 仍然通过。

例如：

- `FF` 场景需要证明 older store data 尚未 ready 时 younger load 进入 replay；
- `DM` 场景需要证明 cold cacheable load 先 miss/replay，再经 dcache response 完成；
- `RAW` 场景需要证明 backpressure 与 replay 都出现；
- `RAR` 场景需要证明 release/probe 窗口下 older load 被 nuke/replay，并最终读到更新值。

### 7.4 store final visibility

store 的最终正确性收口不在 issue 或 committed 当拍，而在 flush/drain 之后：

1. 记录 cacheable sbuffer drain 事件；
2. 记录 NC outer Put* 事件；
3. 区分 MMIO store，不把它直接纳入普通 memory image compare；
4. 在测试结束后显式调用 `flush_store_buffers_and_wait()`；
5. 对 drain log 重放后形成的 `drained_mem` 与最终 `architectural_mem` 在被覆盖地址集合上做一致性核对；
6. 检查所有应当 drain 的 non-MMIO committed store 都至少出现一次 drain 记录。

这样做可以覆盖“store 执行完成但从未真正写出”“drain data 为零或错位”“NC/outer path 记账缺失”等典型问题。

### 7.5 结构化白盒事件优先于散端口读取

本方案强调：新的 testcase 应尽量通过 env facade 和 monitor 输出的结构化事件来断言，不建议直接在测试文件里零散读取 DUT 端口。原因有三点：

1. 端口命名在大版本 RTL 更新中最容易变；
2. 结构化事件更适合复用和输出上下文；
3. monitor 层统一采样可以减少 testcase 中的时序竞态。

如果后续确有必要增加新可观测点，也应优先扩展 env helper，再由 testcase 使用 helper，而不是把测试文件写成局部波形脚本。

## 8. 场景矩阵设计

为了避免“只要有几个 directed case 就算覆盖”，本文档要求后续实现围绕统一矩阵组织场景。

### 8.1 第一维：地址空间

- cacheable
- NC
- MMIO

这是最基本的路径划分，因为它们决定了请求经过 dcache、outer、uncache/MMIO 哪条链路，以及最终 compare/drain 规则如何定义。

### 8.2 第二维：访问类型

- 纯 load
- 纯 store
- store->load
- load->load
- load/store mixed

这样区分是为了把单指令功能、排序功能、混合流量功能从一开始就拆开，避免所有问题都堆到“大随机”里。

### 8.3 第三维：宽度、掩码与对齐

- B/H/W/D 标量宽度；
- aligned；
- 16B 内 misaligned；
- partial overlap；
- partial byte mask。

该维度必须显式覆盖，因为许多 store/load bug 只在低位 offset、子字节 lane 或重叠区域触发。

### 8.4 第四维：数据来源或写出路径

load 侧：

- dcache hit；
- dcache miss/refill；
- SQ forward；
- SBuffer forward；
- outer/nc path。

store 侧：

- SQ materialize；
- sbuffer drain；
- outer Put*；
- MMIO 完成路径。

没有这个维度，场景只会验证“最后对不对”，无法证明路径是否真正被覆盖。

### 8.5 第五维：排序关系

- independent；
- same-addr；
- partial overlap；
- older unresolved store；
- older paused load；
- queue pressure/backpressure。

排序关系是标量 LSU 的核心风险源。尤其是 same-addr 与 overlap，必须区分 cacheline 相同但字节掩码不同的情况。

### 8.6 第六维：replay/异常类型

优先覆盖：

- `FF`
- `DM`
- `NC`
- `RAW`
- `RAR`

后续扩展：

- `DR`
- `TM`
- `BC`
- `NK`
- `MA/MF`

这里不要求第一阶段把所有 cause 都一次性吃完，但文档必须把它们纳入目标矩阵，避免后续扩展缺少总视图。

## 9. 详细测试族设计

### 9.1 基础 load 功能组

目标是验证标量 load 在不同宽度、不同地址属性、不同返回路径下的最基本正确性。

建议至少包含：

1. 单条 aligned byte/half/word/dword load 命中。
2. 同一 cacheline 下不同 offset 的多条 load。
3. cold cacheable load 触发 `DM` replay，随后 refill 完成。
4. 单条 NC load，通过 outer path 完成。
5. 若当前环境具备条件，可加入 MMIO load 冒烟；若暂未具备，也应在文档中显式标记为后续阶段。

设计原因：

- 这是整个 pipeline 的基线，若基础 load 都不能稳定覆盖，后续所有混合或 replay 场景都缺乏解释力。
- B/H/W/D 与 offset 场景可以尽早暴露 mask/low bits/pack/unpack 错误。

### 9.2 基础 store 功能组

目标是验证标量 store 在 commit、materialize、flush、drain 过程中的基本正确性。

建议至少包含：

1. 单条 cacheable dword store -> flush -> drain；
2. byte/half/word/dword store 的字节掩码正确性；
3. STA 与 STD 同拍、STA 先 STD、STD 先 STA；
4. 单条 NC store 外发；
5. 单条 MMIO store 冒烟。

设计原因：

- store 的问题常常只在 drain 阶段暴露，因此必须从第一组就把 flush/drain 带上。
- STA/STD 排列是 FF、forward 和最终 materialize 的前提，不能只用一种时序覆盖。

### 9.3 store->load 组合组

这是最重要的一组之一，负责验证 older store 如何影响 younger load。

建议至少包含：

1. 单条 cacheable store -> same-addr load；
2. 两条或多条顺序 store 覆盖后 load 读取最终值；
3. partial overlap store/load，例如 older store 写低字节、younger load 读取半字或字；
4. unrelated load，不应被无关 store 错误影响；
5. older store 地址未 ready 或数据未 ready 时 younger load 的 replay 行为。

设计原因：

- 这组场景能同时覆盖 deferred store visibility、forward/replay 条件、mask 合并与 architectural view 推进；
- 它们是最能直接检验“store 执行与 load 可见性语义是否一致”的场景。

### 9.4 load->load 与 RAR 顺序组

建议至少包含：

1. 同地址 older/younger load 的普通顺序访问；
2. 更老 load 因精确 load-wait 暂停、更年轻同地址 load 先完成；
3. release/probe 介入后触发真实 RAR violation；
4. waitForRobIdx 或相关等待条件控制 older load 的恢复时机。

设计原因：

- 这组场景的核心不是“两个 load 都读对”，而是“程序序与外部一致性事件交织时，系统是否还能做出正确回滚”。
- 它对 load queue、release 观察、writeback 先后与 violation 通知都有要求，是白盒验证不可缺的一部分。

### 9.5 replay 专项组

建议把 replay 场景单独作为一类回归，而不是分散到各种随机 case 中。

第一批稳定项：

- `FF`
- `DM`
- `NC`
- `RAW`
- `RAR`

第二批补强项：

- `DR`
- `TM`
- `BC`
- `NK`
- `MA/MF`

每个 replay 场景都必须同时检查两件事：

1. 预期 replay 事件是否发生，cause/source/关键标识是否正确；
2. replay 后请求是否最终正确恢复并通过 compare。

这样可以避免“只要看到 replay 就算通过”或“最终通过就算 replay 正确”的片面验证。

### 9.6 mixed load/store 组

建议分成两层：

1. **小规模 deterministic mixed**：用固定模板串联数条 load/store，明确控制 address alias、宽度、older/younger 关系；
2. **小规模 constrained-random mixed**：随机生成若干标量 load/store，但控制地址池、宽度池、最大 outstanding、是否允许 overlap、是否强制收尾 flush。

在线判定规则固定为：

- 运行时只在线比对 load；
- store 不要求当拍更新最终 memory image；
- 测试尾部统一 flush/drain，再做 store 一致性收口。

设计原因：

- deterministic mixed 负责可调试、可复现的逻辑覆盖；
- constrained-random mixed 负责探索组合状态空间；
- 二者结合才能让回归既有定位能力又有发现能力。

### 9.7 边界与 fail-fast 组

为了避免 silent mismatch，文档要求对当前不纳入主方案的特性采用明确策略：

- vector store/load：当前标量方案不覆盖；
- wline/store line 类请求：当前不纳入普通 scalar compare；
- atomics、完整 CBO、复杂 prefetch：暂不纳入；
- 若测试过程中误触发这些路径，应 fail-fast 并给出明确提示，而不是让 memory model 默默忽略。

## 10. 实施路线

### 10.1 Phase 1：补全标量基础矩阵

目标：

- 用现有 env 和现有 memory model 框架，先补齐基础 load/store、宽度、地址属性、单笔组合场景；
- 优先形成稳定、可重复、可快速回归的 directed case。

工作项：

1. 补 B/H/W/D load/store 的真实 DUT case；
2. 补单条 cacheable store->load same-addr case；
3. 补多条 store 覆盖同地址的 case；
4. 补小规模 deterministic mixed ld/st case；
5. 补 MMIO/NC 基础 store 和 load 冒烟；
6. 统一收尾 flush/drain 验证流程。

验收标准：

- 所有基础 case 都通过 load compare 与 store drain 收口；
- 回归失败能明确区分“在线 compare 错”“replay cause 错”“drain 错”。

### 10.2 Phase 2：补强排序、前递与 replay 白盒覆盖

目标：

- 让 replay/violation 类场景从“能冒烟”升级为“能系统化归类”；
- 让 STA/STD 时序、partial overlap、release/probe 干扰、older paused load 等复杂窗口具备稳定用例。

工作项：

1. 把 FF/RAW/RAR 场景扩成矩阵，而不是单个 smoke；
2. 补 DR/TM/BC/NK 等 replay cause 的定向触发场景；
3. 针对 misalign scalar 明确策略：要么补真实 case，要么在文档中明确阶段性缺口与入口条件；
4. 按功能域把场景组织进专门 testcase 文件与 sequence 类别；
5. 必要时增加少量 env helper 输出更稳定的结构化白盒事件。

验收标准：

- 每个已宣称支持的 replay cause 至少有一个稳定 directed case；
- 每类场景都同时具备白盒事件命中与最终语义正确两层断言；
- testcase 不直接散读大量端口，仍以 env facade 为主。

### 10.3 Phase 3：混合流、回归分层与覆盖收敛

目标：

- 把所有 directed case 与 constrained-random case 组织成可持续维护的回归层次；
- 建立 smoke / directed / replay / stress 四档回归模型。

工作项：

1. 把基础 directed case 划入 smoke；
2. 把 replay/ordering 专项放入 replay；
3. 把小规模 constrained-random mixed 放入 stress；
4. 给每档回归定义时长、收敛条件、失败输出要求；
5. 按功能域汇总覆盖缺口，决定下一轮补强优先级。

验收标准：

- 任何一次回归失败都能快速落到某个功能域；
- mixed/random 不再是唯一发现问题的手段，而是对 directed coverage 的补充；
- 已完成功能点的回归结构清晰，不依赖单个“大而全”测试文件。

## 11. 用例与 sequence 组织建议

为了让方案真正可实施，本文档建议未来 testcase 和 sequence 按层次组织。

### 11.1 testcase 分类建议

建议拆分为以下类别：

1. `test_MemBlock_scalar_load_pipeline.py`
   - 基础 load、miss、NC、基础 replay/返回路径。
2. `test_MemBlock_scalar_store_pipeline.py`
   - 基础 store、flush、drain、STA/STD 组合。
3. `test_MemBlock_scalar_ordering.py`
   - store->load、load->load、partial overlap、wait/load-wait、RAR/RAW 特殊顺序场景。
4. `test_MemBlock_replay.py`
   - replay 专项，保留并继续扩展。
5. `test_MemBlock_scalar_mixed.py`
   - deterministic mixed 与 constrained-random mixed。

这样拆分的目的是让失败归因更自然，不必在一个文件里混合所有语义。

### 11.2 sequence 分层建议

建议把 sequence 分为三层：

1. **primitive sequences**
   - 单笔 load/store issue、commit pulse、flush、preload、等待 writeback/quiet。
2. **composite sequences**
   - store-then-load、STA/STD split、cache miss replay、release 注入、RAR/RAW 触发。
3. **campaign sequences**
   - mixed traffic、coverage sweep、replay matrix、stress loop。

sequence 负责组织事务和时序，scoreboard 负责判定，monitor/env 负责观测。不建议把 compare 逻辑散落到 sequence 或 testcase 中。

## 12. 覆盖目标与验收标准

### 12.1 功能覆盖目标

每个功能域至少达到如下层级：

- 一个 smoke；
- 一个或多个 directed；
- 至少一个 small stress/random；
- 若当前阶段无法完成，必须在文档中明确列为缺口，而不是默认为“以后再说”。

### 12.2 replay 覆盖目标

对于当前已具备基础能力的 replay cause，至少做到：

- `FF`：稳定 directed；
- `DM`：稳定 directed；
- `NC`：稳定 directed；
- `RAW`：稳定 directed；
- `RAR`：稳定 directed。

对于下一阶段目标：

- `DR`、`TM`、`BC`、`NK`、`MA/MF` 至少要形成触发策略与计划中的 testcase 骨架。

### 12.3 宽度与属性覆盖目标

- B/H/W/D 的 load/store 全覆盖；
- cacheable / NC / MMIO 至少各有基础冒烟；
- aligned 与典型 misaligned/overlap 至少各有代表性 directed case。

### 12.4 最终通过标准

所有用例必须满足：

1. 所有 load online compare 通过；
2. 所有预期 replay/violation 事件均被观测到，且 cause/关键字段匹配；
3. 所有 non-MMIO committed store 在 flush 后都能由 drain log 闭环到最终 golden memory；
4. 测试结束后不存在 outstanding transport、未完成 compare 或未清空 buffer；
5. 不支持特性必须显式 fail-fast，不允许 silent mismatch。

## 13. 风险、边界与非目标

### 13.1 主要风险

1. **RTL 端口结构继续变化**：需要尽量通过 monitor/env facade 屏蔽 testcase。
2. **某些 replay cause 难以稳定触发**：需要先形成触发条件分析，再决定是否增加专门 helper。
3. **misalign 与 MMIO/NC 语义复杂**：在阶段性实现中必须清楚界定已覆盖和未覆盖边界。
4. **mixed/random 容易吞噬定位能力**：必须坚持“先 directed，后 random”的路线。

### 13.2 非目标

以下内容不纳入本轮主方案：

- 向量 LSU 功能验证；
- AtomicsUnit 完整功能验证；
- 完整 CBO/prefetch 功能；
- 面向性能的延迟、吞吐、带宽 benchmark；
- 复制 RTL 级细节的一一对应软件模型。

## 14. 结论

对于当前 XiangShan MemBlock 标量 load/store pipeline，最合适的验证路线不是继续扩充黑盒随机测试，也不是在 Python 侧再建一个复杂的新 LSU 模型，而是基于现有真实 DUT 环境，把验证能力提升到“功能域明确、路径可观测、语义能收口、实施可分阶段推进”的白盒体系。

这套方案的核心价值在于：

1. 它充分尊重当前 RTL 的真实复杂度，尤其是 replay、forward、ordering、deferred store visibility 等标量 LSU 关键问题；
2. 它复用了现有 env、scoreboard、sequence、transport 能力，不会把环境推回单体式脚本；
3. 它把“为什么这样测”和“后续怎么落地”同时写清楚，能直接指导 testcase 与 helper 的后续实现；
4. 它让回归从“发现一个不知所云的 mismatch”升级为“能定位到某个功能域、某类 replay、某类排序窗口的结构化问题”。

后续实施时，应严格遵循本文给出的功能划分、场景矩阵和阶段路线：先把基础矩阵补齐，再系统补强 replay/ordering 白盒场景，最后用 mixed/stress 做组合空间探索。只有这样，才能在当前验证环境中尽可能完整地覆盖标量 load/store pipeline 的功能，同时保持回归的可维护性和解释力。
