# ROB TODO

## 1. 文档定位

本文档只保留 `src/test/python/MemBlock/` 当前 **尚未完成** 的 ROB 相关工作项。

此前已经完成的内容，例如：

- `non-mem blocker`
- `store commit readiness`
- `load/store/non_mem` 三类 entry 的当前提交规则
- `BackendSendPlan` 下的 ROB 语义步骤与用法

都不再在这里重复维护，而是以下列正式文档为准：

- `src/test/python/MemBlock/docs/rob_model.md`
- `src/test/python/MemBlock/docs/backend_request_model_design.md`
- `src/test/python/MemBlock/docs/backend_rob_cookbook.md`
- `src/test/python/MemBlock/README.md`

如果后续需要确认“当前实现已经支持什么、推荐怎么用”，优先看上述文档；`rob_todo.md` 只回答“还有什么没做”。

## 2. 当前剩余未完成项

截至当前版本，ROB 半模型仍明确缺少以下两类能力：

1. `redirect / flush / cancel` 建模
2. `backend feedback / credit` 半模型

这两项也是当前 `rob_coverage` 仍保留的已知缺口来源：

- `redirect_cancel_not_modelled`
- `backend_feedback_credit_not_modelled`

## 3. TODO 1：redirect / flush / cancel 建模

### 3.1 目标

让 ROB 半模型在遇到 redirect / flush / cancel 时，能正确处理：

- 已登记但尚未提交的 ROB entry 清理
- `pendingPtr / pendingPtrNext / commit packet` 的恢复
- younger mem / non-mem entry 在 redirect 后不再错误保留

### 3.2 当前缺口

目前模型可以表达程序序阻塞，但还不能表达“程序序被 redirect 重建”：

- `non_mem` blocker 可以阻塞 / release
- `store readiness` 可以控制 head store 是否允许提交
- 但一旦真实 DUT 侧出现 redirect / flush / cancel，测试侧 ROB 队列还没有对齐恢复路径

因此当前模型更接近“稳定程序序下的 ROB frontier 半模型”，还不是“可恢复程序序”的 ROB 半模型。

### 3.3 后续实现要求

后续推进这项时，至少需要明确：

- 由哪些 DUT 观测事实触发 cancel / redirect 恢复
- 恢复时如何裁剪测试侧 `_entries`
- `pending_ptr_next` 在恢复拍与恢复后下一拍应如何定义
- 已登记的 store readiness / non-mem blocker 状态如何同步清理

### 3.4 最小验收

至少应补齐：

- unit test：redirect 后 younger pending entry 被移除
- unit test：flush/cancel 后 `pendingPtr` / `pendingPtrNext` 不残留旧 frontier
- directed case：真实 DUT 上出现 redirect 后，ROB 半模型不再把已取消 mem 继续当成可提交项

## 4. TODO 2：backend feedback / credit 半模型

### 4.1 目标

把当前 ROB/MemBlock 前向边界模型，继续扩展到更接近真实 backend <-> MemBlock 双向契约的形态，至少补齐：

- 对 `lqDeq/sqDeq/lqDeqPtr/sqDeqPtr` 相关反馈语义的系统化建模
- 对 backend 侧 credit / feedback 约束的最小代理解释

### 4.2 当前缺口

当前模型主要覆盖的是：

- `ROB -> MemBlock` 的前向提交边界
- 以及 load compare / store committed 所需的程序序语义

但对 `MemBlock -> Backend` 的反馈影响，目前仍主要停留在 DUT 观测与 testcase 间接消费层面，还没有形成一个明确的测试侧 feedback / credit 半模型。

### 4.3 后续实现要求

后续推进这项时，至少需要先回答：

- 哪些 feedback / credit 语义真的会影响当前 Python 侧验证正确性
- 哪些只是 DUT 内部性能/资源行为，不值得在测试侧额外建模
- 如果要建模，应该放在 `rob_agent`、`commit_agent`，还是单独 feedback agent

### 4.4 最小验收

至少应补齐：

- 一份明确的 feedback / credit 语义边界说明
- 对应的 function coverage 观察点
- 至少一组会因 feedback / credit 建模缺失而失真的真实 DUT directed case

## 5. 文档维护约定

除非未来又出现新的 ROB 级未完成项，否则本文件不再回写已经完成的 P0/P1 计划细节。

已完成能力的真源文档固定为：

- `src/test/python/MemBlock/docs/rob_model.md`
- `src/test/python/MemBlock/docs/backend_request_model_design.md`
- `src/test/python/MemBlock/docs/backend_rob_cookbook.md`

本文件只允许追加新的“剩余缺口”或收缩现有未完成项，不再重复保存已经落地实现的历史计划。
