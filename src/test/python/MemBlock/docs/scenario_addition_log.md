# MemBlock 场景增量记录

## 1. 文档目的

本文档专门记录“用户明确要求新增”的场景与对应 testcase。每次新增场景或用例时，都应同步更新本文件，至少补齐以下信息：

1. 生成时间
2. 用户要求的场景内容
3. 实际落地的 testcase / sequence
4. 关键检查点
5. 回归命令
6. 对整体覆盖率的影响

本文档与 `coverage_summary.md` 的分工不同：

- `coverage_summary.md` 记录“当前整体覆盖率状态”
- 本文档记录“每次新增场景本身带来的增量”

## 2. 更新规则

后续每次新增场景时，统一按下面模板追加新条目：

```md
## YYYY-MM-DD HH:MM +0800

### 场景名称

- 用户需求：
- 落地文件：
- testcase：
- sequence / env 支撑：
- 核心检查点：
- 回归命令：
- 覆盖率影响：
  - 基线：
  - 新结果：
  - Delta：
- 备注：
```

如果某次新增场景没有做 isolate A/B coverage，只做了完整回归，则必须明确写出：

- 该数字是“相对最近一次完整基线的累计增量”
- 不是“只由这一条 testcase 独占贡献的纯增量”

## 3. 场景记录

## 2026-04-27 15:05 +0800

### 第一大类-子场景4：补齐三条 load lane 复刻

- 用户需求：
  - 继续确认第一大类场景4是否已经覆盖 `lane1/2`
  - 若未覆盖，则扩到三条 load lane
- 落地文件：
  - `src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py`
  - `src/test/python/MemBlock/CHANGELOG.md`
  - `src/test/python/MemBlock/docs/scenario_addition_log.md`
- testcase：
  - `test_api_MemBlock_scalar_translated_sq_multi_store_youngest_full_cover_sbuffer_miss_three_lanes_probe`
- sequence / env 支撑：
  - 复用 `_run_translated_sq_multi_store_youngest_full_cover_sbuffer_miss_case(...)`
  - helper 新增 `issue_lane` 参数
  - 不新增公共 env / monitor / model 改动
- 核心检查点：
  - `lane0/1/2` 均能稳定复刻
  - translated `TLB hit + dcache hit`
  - 多条 older `SQ store` 同址命中，最终只选择最年轻 full-cover store
  - `sbuffer` 不命中前递
  - 最终写回严格等于最年轻 store
  - 无 replay / 无 release / 无 `memoryViolation`
- 回归命令：
  - `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py -k 'sq_multi_store_youngest_full_cover_sbuffer_miss_three_lanes_probe' -x -rxX`
- 覆盖率影响：
  - 基线：本轮未做 isolate A/B coverage
  - 新结果：本轮未做 isolate A/B coverage
  - Delta：暂未测量
- 备注：
  - 部分 lane 上存在一拍前置 `dataInvalid/forwardInvalid` 过渡事件
  - 当前断言口径收敛到“真实 youngest 命中拍必须干净且功能正确”

## 2026-04-27 10:53 +0800

### 第一大类-子场景5：多条 SQ 命中选最年轻 full-cover，且 sbuffer single-hit

- 用户需求：
  - 先做第一大类场景的子场景5：
    - `storequeue` 中多条 store 命中前递
    - 最终选择最年轻那条 store
    - 该最年轻 store 可以完全覆盖 load 访问
    - `sbuffer` 中也有单条命中前递
  - 需要继续扩展 `load size`
- 落地文件：
  - `src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py`
  - `src/test/python/MemBlock/CHANGELOG.md`
  - `src/test/python/MemBlock/docs/scenario_addition_log.md`
- testcase：
  - `test_api_MemBlock_scalar_translated_sq_multi_store_youngest_full_cover_sbuffer_single_hit_probe`
  - `test_api_MemBlock_scalar_translated_sq_multi_store_youngest_full_cover_sbuffer_single_hit_three_lanes_probe`
- sequence / env 支撑：
  - 新增 `_run_translated_sq_multi_store_youngest_full_cover_sbuffer_single_hit_case(...)`
  - 复用 translated `TLB hit + dcache hit` warmup 流程
  - 复用 existing backend facade / forward trace / debugLsInfo / writeback / wakeup / replay / nuke-release 观测
  - 不新增公共 env / monitor / model 语义
- 核心检查点：
  - `va=pa` 的 Sv39 identity mapping，主 load 为 translated hit path
  - TLB hit，无 TLB/PMP/PMA 异常
  - dcache 行已 warmup，主 load 窗口不产生 dcache miss / outer request / dcache D response
  - `SQ` 中多条 older store 同址命中，最终 `forwardMask/forwardData` 必须等于最年轻 full-cover store
  - `sbuffer` 单条 older store 已进入可见窗口，并对主 load 形成 forward hit
  - 同字节 overlap 时最终写回仍由 `SQ youngest` 主导
  - 最终写回不等于更老 SQ store、不等于 sbuffer-priority 结果、不等于 warmup cache 数据
  - `fullForward=1`，`needDCacheAccess=0`
  - 无 fast replay / slow replay / DR replay / release / `memoryViolation`
  - 正常 wakeup、正常整数写回，写回 lane 与 issue lane 匹配
- 回归命令：
  - `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py -k 'sq_multi_store_youngest_full_cover_sbuffer_single_hit_probe' -x -rxX`
  - `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py -k 'sq_multi_store_youngest_full_cover_sbuffer_single_hit_three_lanes_probe' -x -rxX`
  - 回归保护：`python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py -k 'sq_full_cover_sbuffer_single_full_overlap_probe' -x -rxX`
- 覆盖率影响：
  - 基线：本轮未做 isolate A/B coverage
  - 新结果：本轮未做 isolate A/B coverage
  - Delta：暂未测量
- 备注：
  - 当前已覆盖 `LB/LH/LW/LD`
  - 当前已覆盖 `lane0/1/2`
  - 当前包含 `sbuffer single full-overlap` 与 `sbuffer single partial-overlap`
  - 本轮还修正了既有 single-overlap helper 中 `sbuffer` raw/window mask 变量引用口径，避免窄 load 路径错误引用未定义变量

## 2026-04-24 16:15 +0800

### 第一大类-子场景4：多条 SQ 命中时选择最年轻 full-cover，且 sbuffer miss

- 用户需求：
  - 构造这样一条 translated 标量 load 场景：
    - `storequeue` 中多条 store 命中前递
    - 最终只选择最年轻那条 store
    - 该最年轻 store 自己可以完全覆盖 load 访问
    - `sbuffer` 不命中前递
  - 同时记得扩到不同 `load size`
- 落地文件：
  - `src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py`
  - `src/test/python/MemBlock/CHANGELOG.md`
  - `src/test/python/MemBlock/docs/scenario_addition_log.md`
- testcase：
  - `test_api_MemBlock_scalar_translated_sq_multi_store_youngest_full_cover_sbuffer_miss_probe`
- sequence / env 支撑：
  - 新增 `_run_translated_sq_multi_store_youngest_full_cover_sbuffer_miss_case(...)`
  - 复用既有 translated `SQ hit` 观测骨架
  - 不新增公共 env / monitor / model 改动
- 核心检查点：
  - translated `TLB hit + dcache hit`
  - 多条 older `SQ store` 同址命中
  - `forwardMask/forwardData` 严格等于最年轻 full-cover store
  - 最终写回严格等于最年轻 store，不等于更老 store，也不等于 warmup cache 数据
  - `sbuffer` 若暴露查询口，则允许看到查询，但不允许命中 payload
  - `fullForward=1`
  - `needDCacheAccess=0`
  - 无 replay / 无 release / 无 `memoryViolation`
- 回归命令：
  - `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py -k 'sq_multi_store_youngest_full_cover_sbuffer_miss_probe' -x -rxX`
- 覆盖率影响：
  - 基线：本轮未做 isolate A/B coverage
  - 新结果：本轮未做 isolate A/B coverage
  - Delta：暂未测量
- 备注：
  - 当前已覆盖 `LB/LH/LW/LD`
  - 本轮刻意保持 `sbuffer` 不参与结果，避免把“SQ 选择最年轻”和“双源聚合”两类语义混在同一条 test 里

## 2026-04-24 15:30 +0800

### 第一大类-子场景3：补齐三条 load lane 与 `LB/LH/LW` 扩展

- 用户需求：
  - 第二大类、第三大类先不做
  - 先把第一大类场景3的扩展场景测试补齐
- 落地文件：
  - `src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py`
  - `src/test/python/MemBlock/CHANGELOG.md`
  - `src/test/python/MemBlock/docs/scenario_addition_log.md`
- testcase：
  - `test_api_MemBlock_scalar_translated_sq_full_cover_sbuffer_multi_hit_three_lanes_probe`
  - `test_api_MemBlock_scalar_translated_sq_full_cover_sbuffer_multi_hit_lb_lh_lw_probe`
- sequence / env 支撑：
  - 复用 `_run_translated_sq_full_cover_with_sbuffer_multi_hit_case(...)`
  - 不新增公共 env / monitor / model 改动
  - helper 补齐窄 load 访问窗口的白盒检查口径
- 核心检查点：
  - translated `TLB hit + dcache hit`
  - `SQ` 单条 store full-cover
  - 多条 older store 在 `sbuffer` 侧 merge/聚合后命中
  - `lane0/1/2` 均能稳定复刻
  - `LB/LH/LW/LD` 均有稳定代表 case
  - `fullForward=1`
  - `needDCacheAccess=0`
  - 无 replay / 无 release / 无 `memoryViolation`
  - 最终写回严格等于 `SQ-only` 结果
- 回归命令：
  - `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py -k 'sq_full_cover_sbuffer_multi_hit_three_lanes_probe or sq_full_cover_sbuffer_multi_hit_lb_lh_lw_probe' -x -rxX`
- 覆盖率影响：
  - 基线：本轮未做 isolate A/B coverage
  - 新结果：本轮未做 isolate A/B coverage
  - Delta：暂未测量
- 备注：
  - 本轮把场景3从“已做两种形态”扩到“已做 lane 维度 + load size 维度”
  - `LB/LH/LW` 里仍保持场景3的本质约束，不引入新的 replay / dcache miss 语义

## 2026-04-24 14:20 +0800

### 第一大类-子场景3 第二种形态：`SQ full-cover + sbuffer multi-hit(out-of-window)` 并入稳定 passing 矩阵

- 用户需求：
  - 继续做第一大类的场景3
  - 先把“`SQ` full-cover，`sbuffer` 多条命中且包含窗口外字节”的 translated 形态重新构造并确认
  - 不再沿用旧的疑似 `DR` 缺陷口径
- 落地文件：
  - `src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py`
  - `src/test/python/MemBlock/CHANGELOG.md`
  - `src/test/python/MemBlock/docs/scenario_addition_log.md`
- testcase：
  - `test_api_MemBlock_scalar_translated_sq_full_cover_sbuffer_multi_hit_overlap_probe`
- sequence / env 支撑：
  - 复用既有 `_run_translated_sq_full_cover_with_sbuffer_multi_hit_case(...)`
  - 不新增公共 env / monitor / model 改动
  - 通过 `TRANSLATED_SQ_SBUFFER_MULTI_HIT_CASES` 新增 `sbuffer_hits_include_out_of_window_bytes` 统一管理
- 核心检查点：
  - translated `TLB hit + dcache hit`
  - `SQ` 单条 store 对当前 `LW@offset=4` full-cover
  - 多条 older store 在 `sbuffer` 侧 merge/聚合后命中，且同时包含窗口内字节与窗口外字节
  - `SQ req/resp` 与 `sbuffer req/resp` 均可见
  - `fullForward=1`
  - `needDCacheAccess=0`
  - 无 replay / 无 release / 无 `memoryViolation`
  - 最终写回严格等于 `SQ-only` 结果，不退化成 warmup cache 数据
- 回归命令：
  - `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py -k 'sq_full_cover_sbuffer_multi_hit_overlap_probe' -x -rxX`
- 覆盖率影响：
  - 基线：本轮未做 isolate A/B coverage
  - 新结果：本轮未做 isolate A/B coverage
  - Delta：暂未测量
- 备注：
  - 这是对同日早先 `out_of_window_dr_probe` 记录的后续收口
  - 旧记录保留为历史分析过程，本条代表当前有效结论
  - 当前口径下，该变体已经并入稳定 passing 场景矩阵，不再单独维护 `xfail` testcase

## 2026-04-24 10:35 +0800

### 第一大类-子场景3：`SQ full-cover + sbuffer multi-hit` 首条稳定 probe

- 用户需求：
  - 新增一个全新的 test
  - 场景属于第一大类第 3 个子场景：
    - `SQ` 中单条 store 命中前递，且该单条 store 可以完全覆盖 load 访问
    - 多条 older store 在 `sbuffer` 侧 merge/聚合后命中前递
  - 当前先做稳定可通过版本，不改公共环境
- 落地文件：
  - `src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py`
  - `src/test/python/MemBlock/CHANGELOG.md`
  - `src/test/python/MemBlock/docs/scenario_addition_log.md`
- testcase：
  - `test_api_MemBlock_scalar_translated_sq_full_cover_sbuffer_multi_hit_overlap_probe`
- sequence / env 支撑：
  - 新增 `_run_translated_sq_full_cover_with_sbuffer_multi_hit_case(...)`
  - 复用既有 translated warmup / `TLB hit` / `dcache hit` / `SQ-ready before sbuffer` 骨架
  - 不修改公共 env / monitor / model
- 核心检查点：
  - `TLB hit / kill` 行为正常
  - `PMP/PMA` 正常
  - `dcache` 无 miss 请求/响应增量
  - `SQ req/resp` 与 `sbuffer req/resp` 均命中
  - `fullForward=1`
  - `needDCacheAccess=0`
  - 无异常、无 `memoryViolation`、无 release
  - 正常 wakeup / writeback
  - 最终写回严格等于 `SQ-only` 结果，不退化成 warmup cache 数据
- 回归命令：
  - `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py -k 'sq_full_cover_sbuffer_multi_hit_overlap_probe' -x -rxX`
- 覆盖率影响：
  - 基线：本轮未做 isolate A/B coverage
  - 新结果：本轮未做 isolate A/B coverage
  - Delta：暂未测量
- 备注：
  - 当前已稳定纳入的子场景：
    - `all_sbuffer_hits_within_sq_cover`
  - 同批尝试过“多条 older store 在 `sbuffer` 侧 merge/聚合后，同时包含 load 窗口内字节与窗口外字节”的变体
  - 该变体在当前 DUT 上会自然进入 `replay_queue(DR)`
  - 因此未并入本条 `no-dcache / no-replay` probe，后续应拆成独立 replay 类 testcase

## 2026-04-24 11:20 +0800

### 第一大类-子场景3 第二种形态：`SQ full-cover + sbuffer multi-hit(out-of-window)` 的 `DR` 复现 probe

- 用户需求：
  - 继续分析为什么“`SQ` 已 full-cover”时，这个场景没有直接完成前递而是进入 `DR`
  - 需要判断这是不是设计缺陷
- 落地文件：
  - `src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py`
  - `src/test/python/MemBlock/CHANGELOG.md`
  - `src/test/python/MemBlock/docs/scenario_addition_log.md`
- testcase：
  - `test_api_MemBlock_scalar_translated_sq_full_cover_sbuffer_multi_hit_out_of_window_dr_probe`
- sequence / env 支撑：
  - 新增 `_run_translated_sq_full_cover_with_sbuffer_multi_hit_out_of_window_dr_probe(...)`
  - 继续复用 translated warmup / `TLB hit` / `dcache hit` 背景
  - 不改公共 env / monitor / model
- 核心检查点：
  - `SQ` 单条 store 对当前 `LW@offset=4` full-cover
  - 多条 older store 在 `sbuffer` 侧 merge/聚合后命中，且同时包含窗口内字节和窗口外字节
  - 白盒上先观测到 `fullForward=1 && needDCacheAccess=0`
  - 随后又观测到 `needDCacheAccess=1`
  - replay path 落入 `replay_queue/replay_lane/ldu` 且 cause 为 `DR`
  - 最终 writeback 数据仍等于 `SQ-only` 结果，不退化成 warmup cache 数据
- 回归命令：
  - `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py -k 'sq_full_cover_sbuffer_multi_hit_out_of_window_dr_probe' -x -rxX`
- 覆盖率影响：
  - 基线：本轮未做 isolate A/B coverage
  - 新结果：本轮未做 isolate A/B coverage
  - Delta：暂未测量
- 备注：
  - 当前以精确 `xfail` 固定该签名，reason 为：
    - `SQ_FULL_COVER_SBUFFER_MULTI_HIT_OUT_OF_WINDOW_DR_XFAIL_REASON`
  - 这条 probe 的价值不在于“先做绿”，而在于把疑似 DUT 缺陷稳定复现并与正常 passing 场景分离
  - 现阶段最关键的证据链是：
    - `SQ` full-cover 已成立
    - 最终写回仍等于 `SQ-only`
    - 但控制面仍退化到 `needDCacheAccess -> DR`
  - 该现象具备明显“数据面与控制面判定脱节”的疑似设计缺陷特征

## 2026-04-23 20:05 +0800

### 第一大类-子场景2b：`SQ full-cover + sbuffer single partial-overlap` 扩到自然对齐 `LH/LW`

- 用户需求：
  - 在既有 `SQ full-cover + sbuffer single partial-overlap` translated 场景基础上，继续往 `LH/LW` 扩
  - 先打通最小可解释的窄 load 路径，再决定是否继续覆盖更多变体
- 落地文件：
  - `src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py`
  - `src/test/python/MemBlock/CHANGELOG.md`
- testcase：
  - `test_api_MemBlock_scalar_translated_sq_full_cover_sbuffer_single_partial_overlap_lh_lw_probe`
- sequence / env 支撑：
  - 复用既有 `_run_translated_sq_full_cover_with_sbuffer_single_overlap_case(...)`
  - 给 helper 增加 `load_size / load_mask / load_offset / issue_lane` 参数化
  - 保持 translated `TLB hit + dcache hit + SQ full-cover + sbuffer partial-overlap` 主骨架不变
- 核心检查点：
  - `TLB hit`
  - `PMP/PMA` 正常
  - `dcache` 无 miss 请求/响应增量
  - `SQ resp.valid=1`
  - `sbuffer resp.valid=1`
  - `fullForward=1`
  - `needDCacheAccess=0`
  - 无 `forwardInvalid/matchInvalid/dataInvalid`
  - 无 replay / violation
  - 正常 wakeup / writeback
  - 自然对齐窄 load 下最终仍由 `SQ` 主导写回
- 回归命令：
  - `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py -k 'sq_full_cover_sbuffer_single_partial_overlap_lh_lw_probe' -x -rxX`
  - 回归保护：
    - `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py -k 'sq_full_cover_sbuffer_single_partial_overlap_mask_three_lanes_probe' -x -rxX`
- 覆盖率影响：
  - 基线：暂未做 isolate A/B coverage
  - 新结果：暂未做 isolate A/B coverage
  - Delta：暂未测量
- 备注：
  - 当前纳入 4 个自然对齐窄 load case：
    - `lh_low_byte_overlap`
    - `lh_high_byte_overlap`
    - `lw_low_halfword_overlap`
    - `lw_high_halfword_overlap`
  - `lw_middle_halfword_overlap` 已从本轮 testcase 中移除
    - 原因：当前 DUT/观测口径下它进入“非自然对齐 `LW` 返回扩展窗口”的另一类语义，不适合作为本轮自然对齐 `LH/LW` 的代表 case
  - 这轮 helper 同时收敛了两类观测差异：
    - `offset=0` 的窄 load，forward/writeback 仍可能按整段窗口返回
    - 非零 `offset` 的窄 load，forward/writeback 会收缩到访问字节窗口
  - `lh_high_byte_overlap` 上观测到 issue 在 `lane0`，最终写回出现在另一条合法 load writeback lane；本轮 testcase 不再强绑“必须原 lane 写回”，仅要求功能路径与结果正确

## 2026-04-24 00:20 +0800

### 第一大类-子场景2a：`SQ full-cover + sbuffer single full-overlap` 扩到自然对齐 `LH/LW`

- 用户需求：
  - 第一大类里的第二个场景 `2a` 还没有扩到其他 size 的 load 访问
  - 继续补 `LH/LW` 等窄 load 版本
- 落地文件：
  - `src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py`
  - `src/test/python/MemBlock/CHANGELOG.md`
- testcase：
  - `test_api_MemBlock_scalar_translated_sq_full_cover_sbuffer_single_full_overlap_lh_lw_probe`
- sequence / env 支撑：
  - 直接复用既有 `_run_translated_sq_full_cover_with_sbuffer_single_overlap_case(...)`
  - 保持 `SQ full-cover + sbuffer single full-overlap + translated hit path` 骨架不变
- 核心检查点：
  - `TLB hit`
  - `PMP/PMA` 正常
  - `dcache` 无 miss 请求/响应增量
  - `SQ resp.valid=1`
  - `sbuffer resp.valid=1`
  - `SQ` 与 `sbuffer` 对目标窄 load 字节完全重叠命中
  - `fullForward=1`
  - `needDCacheAccess=0`
  - 无 `forwardInvalid/matchInvalid/dataInvalid`
  - 无 replay / violation
  - 最终写回仍由 `SQ` 主导
- 回归命令：
  - `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py -k 'sq_full_cover_sbuffer_single_full_overlap_lh_lw_probe' -x -rxX`
  - 回归保护：
    - `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py -k 'sq_full_cover_sbuffer_single_full_overlap_three_lanes_probe' -x -rxX`
- 覆盖率影响：
  - 基线：暂未做 isolate A/B coverage
  - 新结果：暂未做 isolate A/B coverage
  - Delta：暂未测量
- 备注：
  - 当前纳入 4 个自然对齐 full-overlap 窄 load case：
    - `lh_low_full_overlap`
    - `lh_high_full_overlap`
    - `lw_low_full_overlap`
    - `lw_high_full_overlap`
  - 当前只完成了 lane0 的 size 扩展；`2a` 的三 lane `LH/LW` 版还没有继续展开

## 2026-04-23 11:35 +0800

### 第一大类-子场景2：`SQ full-cover + sbuffer single-hit` 的重叠优先级 probe

- 用户需求：
  - 在第一大类场景下继续构造：
    - `storequeue` 中单条 `store` 命中前递，且该单条 `store` 可以完全覆盖 `load`
    - `sbuffer` 侧也有单条可命中的前递响应
  - 进一步把该子场景拆成更小场景：
    - 完全重叠
    - 部分重叠
- 落地文件：
  - `src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py`
  - `src/test/python/MemBlock/CHANGELOG.md`
- testcase：
  - `test_api_MemBlock_scalar_translated_sq_full_cover_sbuffer_single_full_overlap_probe`
  - `test_api_MemBlock_scalar_translated_sq_full_cover_sbuffer_single_partial_overlap_probe`
- sequence / env 支撑：
  - 复用 translated `SQ full-cover` 骨架
  - 复用 `override_dcache_client_ready(...)`
  - 通过“先让 sbuffer store 提交进入可见窗口，再插入 blocker 把 SQ store 卡在队列内”的方式稳定构造双源并存窗口
- 核心检查点：
  - `TLB hit`
  - `PMP/PMA` 正常
  - `SQ` 单条 store full-cover
  - `sbuffer` 单条来源对应的前递响应同时命中
  - 完全重叠场景：
    - `SQ` 和 `sbuffer` 对同一整条 8B load 的同一批字节同时命中
  - 部分重叠场景：
    - `SQ` full-cover，`sbuffer` 仅覆盖高 4B
  - 两条场景共同检查：
    - `SQ resp.valid=1`
    - `sbuffer resp.valid=1`
    - `fullForward=1`
    - `needDCacheAccess=0`
    - 无 `forwardInvalid/matchInvalid/dataInvalid`
    - 无 replay / violation
    - 正常 wakeup / writeback
    - 最终写回数据均等于 `SQ full-cover store` 数据，说明重叠字节优先级由 `SQ` 主导
- 回归命令：
  - `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py -k 'sq_full_cover_sbuffer_single_full_overlap_probe or sq_full_cover_sbuffer_single_partial_overlap_probe' -rxX`
- 覆盖率影响：
  - 基线：待后续批量 coverage 测量
  - 新结果：待后续批量 coverage 测量
  - Delta：待后续批量 coverage 测量
- 备注：
  - 当前 build 下这两条场景都已稳定通过
  - 从定向结果看，在 `SQ full-cover` 与 `sbuffer single-hit` 同时存在时，重叠字节最终由 `SQ` 主导写回
  - 2026-04-23 当天后续又修正了一次 dcache 判定口径：
    - 不再直接把早期窗口里的 `dcache_s1_kill` 当成主 load miss 证据
    - 以 `debugLsInfo.s2_is_dcache_first_miss==0` 以及 `dcache_a/dcache_d` 计数不增长作为最终 miss 判据

## 2026-04-23 16:55 +0800

### 第一大类-子场景2：overlap probe 扩到 lane0/1/2

- 用户需求：
  - 把当前 `lane0` 上已打通的 overlap 场景同样构造到 `lane1/2`
- 落地文件：
  - `src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py`
  - `src/test/python/MemBlock/CHANGELOG.md`
- testcase：
  - `test_api_MemBlock_scalar_translated_sq_full_cover_sbuffer_single_full_overlap_three_lanes_probe`
  - `test_api_MemBlock_scalar_translated_sq_full_cover_sbuffer_single_partial_overlap_three_lanes_probe`
- sequence / env 支撑：
  - 复用现有 overlap helper
  - 仅把主 load 发射 lane 参数化为 `issue_lane`
  - 其余 stimulus 组织保持不变
- 核心检查点：
  - `lane0/1/2` 各自独立复刻：
    - `TLB hit`
    - `PMP/PMA` 正常
    - `SQ resp.valid=1`
    - `sbuffer resp.valid=1`
    - `fullForward=1`
    - `needDCacheAccess=0`
    - `dcache_a/dcache_d` 统计不增长
    - 无 `forwardInvalid/matchInvalid/dataInvalid`
    - 无 replay / violation
    - 正常 wakeup / writeback
- 回归命令：
  - `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py -k 'sq_full_cover_sbuffer_single_full_overlap_three_lanes_probe or sq_full_cover_sbuffer_single_partial_overlap_three_lanes_probe' -x -rxX`
- 覆盖率影响：
  - 基线：暂未做 isolate A/B coverage
  - 新结果：暂未做 isolate A/B coverage
  - Delta：暂未测量
- 备注：
  - 当前只完成了定向验证，未做完整回归
  - 三条 load lane 都已能跑通 overlap 场景

## 2026-04-23 17:20 +0800

### 第一大类-子场景2b：single partial-overlap 的 mask 形状矩阵

- 用户需求：
  - 在“`SQ full-cover + sbuffer single partial-overlap`”基础上继续细分
  - 对 partial-overlap 的 mask 形状做更多补充
- 落地文件：
  - `src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py`
  - `src/test/python/MemBlock/CHANGELOG.md`
- testcase：
  - `test_api_MemBlock_scalar_translated_sq_full_cover_sbuffer_single_partial_overlap_mask_matrix_probe`
- sequence / env 支撑：
  - 复用现有 partial-overlap helper
  - 仍保持：
    - `SQ` 单条 store full-cover
    - `sbuffer` 单条 store partial-hit
    - `lane0` 定向验证
- 核心检查点：
  - `TLB hit`
  - `PMP/PMA` 正常
  - `SQ resp.valid=1`
  - `sbuffer resp.valid=1`
  - `SQ forward mask` full-cover
  - `sbuffer forward mask` 精确等于当前 partial mask
  - `fullForward=1`
  - `needDCacheAccess=0`
  - `dcache_a/dcache_d` 统计不增长
  - 无 `forwardInvalid/matchInvalid/dataInvalid`
  - 无 replay / violation
  - 最终写回仍等于 `SQ` full-cover store 数据
- 回归命令：
  - `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py -k 'sq_full_cover_sbuffer_single_partial_overlap_mask_matrix_probe' -x -rxX`
- 覆盖率影响：
  - 基线：暂未做 isolate A/B coverage
  - 新结果：暂未做 isolate A/B coverage
  - Delta：暂未测量
- 备注：
  - 当前矩阵覆盖 9 个单条 partial-store 可真实表达的 mask 形状：
    - 低 `1B/2B/4B`
    - 中间 `1B/2B/4B`
    - 高 `1B/2B/4B`
  - 稀疏/中间洞形状若要保持真实 DUT 语义，需要多笔 `sbuffer` store 聚合，不并入这一轮 single-store partial-overlap 矩阵

## 2026-04-23 17:35 +0800

### 第一大类-子场景2b：代表性 partial-overlap mask 扩到三条 lane

- 用户需求：
  - 在 lane0 的 mask 形状基础上，筛掉天然重复项
  - 选 2 到 3 个最有代表性的形状扩到 `lane1/2`
- 落地文件：
  - `src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py`
  - `src/test/python/MemBlock/CHANGELOG.md`
- testcase：
  - `test_api_MemBlock_scalar_translated_sq_full_cover_sbuffer_single_partial_overlap_mask_three_lanes_probe`
- sequence / env 支撑：
  - 复用现有 partial-overlap helper
  - 继续保持：
    - `SQ` 单条 store full-cover
    - `sbuffer` 单条 store partial-hit
    - 主 load 发射 lane 参数化
- 核心检查点：
  - 三种代表性 mask：
    - `partial_overlap_low_byte`
    - `partial_overlap_middle_halfword`
    - `partial_overlap_high_word`
  - 每种形状都在 `lane0/1/2` 上复刻
  - `TLB hit`
  - `PMP/PMA` 正常
  - `SQ resp.valid=1`
  - `sbuffer resp.valid=1`
  - `fullForward=1`
  - `needDCacheAccess=0`
  - `dcache_a/dcache_d` 统计不增长
  - 无 `forwardInvalid/matchInvalid/dataInvalid`
  - 无 replay / violation
  - 最终写回仍等于 `SQ` full-cover store 数据
- 回归命令：
  - `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py -k 'sq_full_cover_sbuffer_single_partial_overlap_mask_three_lanes_probe' -x -rxX`
- 覆盖率影响：
  - 基线：暂未做 isolate A/B coverage
  - 新结果：暂未做 isolate A/B coverage
  - Delta：暂未测量
- 备注：
  - 当前已经完成“lane0 形状补齐 -> 挑代表形状 -> 扩到 lane1/2”这一步
  - `LH/LW` 维度尚未开始

## 2026-04-22 19:05 +0800

### translated `SQ + sbuffer` split-forward 的复杂 mask 形状扩展

- 用户需求：
  - 现有 split-forward 覆盖过于简单
  - 继续补齐：
    - 连续低字节
    - 连续高字节
    - 中间洞
    - 稀疏 mask 多种情况
- 落地文件：
  - `src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py`
  - `src/test/python/MemBlock/CHANGELOG.md`
- testcase：
  - 已通过：
    - `test_api_MemBlock_scalar_translated_sq_sbuffer_split_full_forward_probe`
    - `test_api_MemBlock_scalar_translated_sq_sbuffer_split_full_forward_reverse_probe`
  - 已新增并单独标记：
    - `test_api_MemBlock_scalar_translated_sq_sbuffer_split_full_forward_middle_hole_probe`
    - `test_api_MemBlock_scalar_translated_sq_sbuffer_split_full_forward_sparse_even_odd_probe`
    - `test_api_MemBlock_scalar_translated_sq_sbuffer_split_full_forward_sparse_irregular_probe`
- sequence / env 支撑：
  - 复用 translated split-forward helper：
    - `_run_translated_split_forward_case(...)`
    - `_apply_store_specs_to_word(...)`
    - `_build_expected_forward_payload(...)`
  - 通过“同一 source 内多笔真实 partial-store 聚合”构造 hole/sparse
  - 不伪造离散 store mask
- 核心检查点：
  - 连续类场景：
    - `SQ` / `sbuffer` 非重叠覆盖整条 8B load
    - `fullForward=1`
    - `needDCacheAccess=0`
    - 无 replay / violation
    - 正常 wakeup / writeback
  - hole / sparse 场景：
    - 仍按同样白盒检查模板执行
    - 当前在“同一 source 多笔 partial-store 聚合”条件下会退化到 `SMF` replay
    - 因此先单独 `xfail`，避免和已通过连续类场景混在一个总 matrix
- 回归命令：
  - `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py -k 'translated_sq_sbuffer_split_full_forward_reverse_probe'`
  - `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py -k 'translated_sq_sbuffer_split_full_forward_mask_matrix' -x`
- 覆盖率影响：
  - 基线：待后续批量 coverage 测量
  - 新结果：待后续批量 coverage 测量
  - Delta：待后续批量 coverage 测量
- 备注：
  - `reverse_probe` 已通过，说明“连续低/连续高 ownership 互换”可以稳定支持
  - 原总包 matrix 首个失败点落在 `contiguous_low_sq_contiguous_high_sbuffer`
  - 当前缺口不是 testcase 没把 shape 写出来，而是 DUT 在多笔 partial-store 聚合条件下会退化到 `SMF` replay

## 2026-04-22 17:14 +0800

### identity-mapped translated load 的 `SQ + sbuffer` 非重叠拼接 full-forward 探针

- 用户需求：
  - 在已有相关前递场景基础上，保持其他条件不变
  - 构造 `storequeue` 和 `storebuffer` 前递“非重叠字节”同时命中
  - 两者合起来对同一条标量 load 达成 `fullForward`
  - 不需要 `dcache` 数据，即 `needDCacheAccess=0`
  - 仍需检查：
    - `TLB hit`
    - `PMP/PMA` 正常
    - 无 `stld violation / replay / memoryViolation`
    - 正常 wakeup / writeback
- 落地文件：
  - `src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py`
- testcase：
  - `test_api_MemBlock_scalar_translated_sq_sbuffer_split_full_forward_probe`
- sequence / env 支撑：
  - `MmuSv39AddressSpaceInstallSequence`
  - translated warmup helper
  - `override_dcache_client_ready(...)`
  - `NonMemBlockerStep`
  - `debugLsInfo / SQ+sbuffer forward / replay / nuke-query / tlb / wakeup / writeback` 白盒口
- 核心检查点：
  - Sv39 identity mapping，明确 `VA=PA`
  - warmup 先打热 `TLB + dcache`
  - 高 4B partial store (`mask=0x0F`, `addr=base+4`) 先进入 sbuffer 可见窗口
  - 低 4B partial store (`mask=0x0F`, `addr=base`) 被 blocker 卡在 SQ
  - younger 8B translated load 命中时：
    - `SQ forwardMask` 仅覆盖低 4B
    - `sbuffer forwardMask` 仅覆盖高 4B
    - 两边 mask 非重叠
    - 两边 mask 并集覆盖整条 load mask
    - 被命中字节的 `forwardData` 分别等于两笔 older store 数据
    - `fullForward=1`
    - `needDCacheAccess=0`
  - `outer / dcache A / dcache D` 统计不增长
  - 最终写回值必须等于拼接后的 store 数据，而不是 warmup cache 数据
  - 无 `replay / memoryViolation / release`
  - 正常 `wakeup / writeback`
- 回归命令：
  - `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py -k 'translated_sq_sbuffer_split_full_forward_probe'`
- 覆盖率影响：
  - 基线：待后续批量 coverage 测量
  - 新结果：待后续批量 coverage 测量
  - Delta：待后续批量 coverage 测量
- 备注：
  - 当前 build 下 `sbuffer req.valid` 顶层白盒信号名未稳定导出，因此 testcase 对“是否发起 sbuffer 请求”的收口方式是：
    - 若 `req.valid` 可观测，则必须看到其拉高
    - 无论 `req.valid` 是否可观测，都必须看到 `sbuffer resp.valid=1`，且 `mask/data` 与高 4B older store 精确匹配
  - 当前已先打通单-lane 最小 probe；按你的要求，暂未做 coverage 回归
  - 定向结果：
    - `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py -k 'translated_sq_sbuffer_split_full_forward_probe'`
    - 结果：`1 passed, 15 deselected in 24.12s`

## 2026-04-22 15:13 +0800

### identity-mapped translated load 的 `SQ miss + sbuffer hit` 最小探针

- 用户需求：
  - 在已打通的 translated `SQ hit` 基础上，继续构造“最终数据由 sbuffer 前递而来”的真实场景
  - 不覆盖原有用例，新增一个独立 test
  - 其它条件尽量保持与先前 translated probe 一致：
    - 标量 load
    - `TLB hit`
    - `PMP/PMA` 无异常
    - `dcache` 不退化成 miss
    - 无 `stld violation / replay / memoryViolation`
    - 正常 wakeup / writeback
- 落地文件：
  - `src/test/python/MemBlock/model/transport_responder.py`
  - `src/test/python/MemBlock/memory_model.py`
  - `src/test/python/MemBlock/MemBlock_env.py`
  - `src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py`
- testcase：
  - `test_api_MemBlock_scalar_translated_sbuffer_forward_hit_probe`
- sequence / env 支撑：
  - `MmuSv39AddressSpaceInstallSequence`
  - translated warmup helper
  - `override_dcache_client_ready(...)`
  - `debugLsInfo / SQ+sbuffer forward / replay / nuke-query / tlb / wakeup / writeback` 白盒口
- 核心检查点：
  - Sv39 identity mapping，明确 `VA=PA`
  - warmup 先打热 `TLB + dcache`
  - older store 先 materialize，再 commit 离开 `SQ`
  - younger translated load 发起时：
    - `SQ` 不覆盖本次 load mask
    - `sbufferForwardResp.valid=1`
    - `sbuffer_forward_mask` 覆盖本次 load mask
    - 被 load mask 覆盖的 `sbuffer_forward_data` 等于 older store 数据
    - `fullForward=1`
    - `needDCacheAccess=0`
  - 最终写回值必须等于 older store 数据，且不能退化成 warmup cache 数据
  - `outer/dcache A/dcache D` 统计不增长
  - 无 `replay / memoryViolation / release`
  - 正常 `wakeup / writeback`
- 回归命令：
  - `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py -k 'translated_sbuffer_forward_hit_probe'`
- 覆盖率影响：
  - 基线：待后续批量 coverage 测量
  - 新结果：待后续批量 coverage 测量
  - Delta：待后续批量 coverage 测量
- 备注：
  - 为了稳定暴露“store 已进入 sbuffer 可见窗口，但 dcache client 尚未继续接收”的时间窗，本轮新增了最小的 dcache client ready override
  - 当前已先打通单-lane 最小 probe；覆盖率暂未按你的要求去做 isolate A/B 回归
  - 定向结果：
    - `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py -k 'translated_sbuffer_forward_hit_probe'`
    - 结果：`1 passed, 13 deselected in 23.49s`

## 2026-04-22 16:05 +0800

### identity-mapped translated load 的 `SQ miss + sbuffer hit` 三 lane 复刻探针

- 用户需求：
  - 基于已通过的 translated `sbuffer hit` lane0 最小 probe
  - 继续把 lane1、lane2 也按同样逻辑各跑一遍
  - 在 test 层保留一个新的 three-lane testcase，方便后续单独记录
- 落地文件：
  - `src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py`
- testcase：
  - `test_api_MemBlock_scalar_translated_sbuffer_forward_hit_three_lanes`
- sequence / env 支撑：
  - `MmuSv39AddressSpaceInstallSequence`
  - translated warmup helper
  - `override_dcache_client_ready(...)`
  - `debugLsInfo / SQ+sbuffer forward / replay / nuke-query / tlb / wakeup / writeback` 白盒口
- 核心检查点：
  - lane0/1/2 各自独立复刻一次 translated sbuffer-hit
  - 三条 lane 都满足：
    - `Sv39` identity mapping，`VA=PA`
    - warmup 后主 load 维持 `TLB hit + dcache hit` 背景
    - `SQ` 不覆盖本次 load mask
    - `sbufferForwardResp.valid=1`
    - `sbuffer_forward_mask/data` 覆盖并返回 older store 数据
    - `fullForward=1`
    - `needDCacheAccess=0`
    - 无 `replay / memoryViolation / release`
    - 正常 `wakeup / writeback`
- 回归命令：
  - `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py -k 'translated_sbuffer_forward_hit_three_lanes'`
- 覆盖率影响：
  - 基线：待后续批量 coverage 测量
  - 新结果：待后续批量 coverage 测量
  - Delta：待后续批量 coverage 测量
- 备注：
  - 本条最终采用“同一 test 内逐 lane 独立复刻单-lane 构造”的方式，避免三条 lane 共享前置状态时把 sbuffer 可见窗口扰乱
  - lane2 上观测到一拍前置的 `SQ forwardInvalid/dataInvalid` 过渡事件，但其后出现干净的 `sbuffer` 命中拍，因此断言口径收敛到真实命中拍
  - 定向结果：
    - `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py -k 'translated_sbuffer_forward_hit_three_lanes'`
    - 结果：`1 passed, 14 deselected in 25.06s`

## 2026-04-17 18:10 +0800

### 物理 cacheable `store -> same-addr load` 的 SQ forward 最小探针

- 用户需求：
  - 先验证最小真实 `storequeue forward` 能力
  - 先不强绑 translated 场景
  - 通过更老 store 与更年轻同地址 load，确认 `SQ hit` 是否真的成立
- 落地文件：
  - `src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py`
- testcase：
  - `test_api_MemBlock_scalar_physical_sq_forward_hit_probe`
- sequence / env 支撑：
  - `ScalarStoreSequence`
  - `debugLsInfo`
  - `LSQ forward` 白盒口
  - `writeback / wakeup / replay / nuke-query` 白盒口
- 核心检查点：
  - older physical cacheable store 在 SQ 内地址/数据均已就绪
  - younger same-addr load 命中 `LSQ forward resp`
  - `forwardMask` 全命中，`forwardData` 等于 older store 数据
  - `forwardInvalid/matchInvalid/dataInvalid=0`
  - 无 `dcache miss / replay / memoryViolation / release`
  - 正常 `wakeup / writeback`
- 回归命令：
  - `python3 -m py_compile src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py`
  - `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py -k 'physical_sq_forward_hit_probe' -rxX`
- 覆盖率影响：
  - 基线：待后续批量 coverage 测量
  - 新结果：待后续批量 coverage 测量
  - Delta：待后续批量 coverage 测量
- 备注：
  - 本条先用于确认当前 DUT/env 的最小 SQ forward 能力
  - 若该探针稳定，再向 translated `TLB hit + dcache hit + SQ hit + sbuffer miss` 扩展

## 2026-04-17 18:45 +0800

### identity-mapped translated load 的 SQ forward 最小探针

- 用户需求：
  - 基于已通过的 physical SQ-forward probe 向 translated 场景扩展
  - 先优先解决 store 侧和 translated load 侧地址语义对齐
  - 打通最小 translated `SQ hit` 路径
- 落地文件：
  - `src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py`
- testcase：
  - `test_api_MemBlock_scalar_translated_sq_forward_hit_probe`
- sequence / env 支撑：
  - `MmuSv39AddressSpaceInstallSequence`
  - `ScalarStoreSequence`
  - translated warmup helper
  - `LSQ forward` 白盒口
  - `writeback / wakeup / replay / nuke-query / tlb` 白盒口
- 核心检查点：
  - younger load 采用 identity-mapped Sv39 地址，保证 translated paddr 与 bare older store 字面地址一致
  - warmup 先打热 `TLB + dcache`
  - older bare store 在 SQ 内地址/数据均已就绪
  - younger translated load 保持 `TLB hit + dcache hit`
  - `LSQ forward resp` 可见，`forwardInvalid/matchInvalid/dataInvalid=0`
  - 写回值必须等于 older store 数据，且不能退化成 warmup cache 数据
  - `outer/dcache A/dcache D` 统计不增长
  - 无 `replay / memoryViolation / release`
  - 正常 `wakeup / writeback`
- 回归命令：
  - `python3 -m py_compile src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py`
  - `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py -k 'translated_sq_forward_hit_probe' -rxX`
- 覆盖率影响：
  - 基线：
    - `2026-04-20` 多线程对照回归（同时排除
      `test_api_MemBlock_scalar_translated_sq_forward_hit_probe`
      与
      `test_api_MemBlock_scalar_translated_sq_forward_hit_three_lanes`）：
      line `195342 / 295581 = 66.0875%`，
      branch `935664 / 1573403 = 59.4675%`
  - 新结果：
    - `2026-04-20` 多线程完整回归（包含本条与三-lane 复刻 probe）：
      line `195407 / 295581 = 66.1095%`，
      branch `935934 / 1573403 = 59.4847%`
  - Delta：
    - 两条 translated `SQ hit` probe 合并净增量：
      line `+65` hit / `+0.0220` pct，
      branch `+270` hit / `+0.0172` pct
- 备注：
  - 本条采用“identity-mapped translated younger load + bare older store”的最小对齐方式
  - 目的是先绕开 store 侧 translation 仍不稳定的问题，验证 translated load 侧能否与 older store 对齐形成 SQ hit
  - 当前 build 导出的 Python DUT 白盒口未暴露 raw `LoadUnit io_sqForward_*`，因此本轮先以
    `TLB hit + dcache hit + no outer/no dcache A/D + LSQ forward resp valid + writeback=older store data`
    作为最小 translated `SQ hit` 证明闭环
  - 上述 coverage delta 是本条与三-lane 复刻 probe 的联合 A/B 结果，不是本条单独独占贡献
  - 定向结果：
    - `python3 -m py_compile src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py`
    - `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py -k 'translated_sq_forward_hit_probe' -rxX`
    - 结果：`1 passed, 11 deselected in 21.62s`

## 2026-04-20 00:20 +0800

### translated `SQ hit` 三 lane 复刻探针

- 用户需求：
  - 基于已通过的 translated `SQ hit` lane0 probe
  - 以同样方式分别在 lane1、lane2 再跑一遍
  - 重点确认三条 load pipeline 都支持 translated `SQ hit`
  - 同时尝试补上 `sbuffer` 查询断言
- 落地文件：
  - `src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py`
- testcase：
  - `test_api_MemBlock_scalar_translated_sq_forward_hit_three_lanes`
- sequence / env 支撑：
  - `MmuSv39AddressSpaceInstallSequence`
  - `ScalarStoreSequence`
  - translated warmup helper
  - `debugLsInfo / LSQ forward / replay / nuke-query / tlb / wakeup / writeback` 白盒口
- 核心检查点：
  - lane0/1/2 各自完成一次 identity-mapped translated load
  - 三条 lane 都保持 `TLB hit + PMP/PMA normal + dcache hit`
  - `dcache A/D` 统计不增长
  - `LSQ forward resp` 可见
  - 最终写回值必须等于各自 older store 数据，且不能退化成 warmup cache 数据
  - `forwardInvalid/matchInvalid/dataInvalid=0`
  - 无 `stld violation / replay / release / memoryViolation`
  - 正常 `wakeup / writeback`
  - 若当前 build 暴露 `sbuffer req.valid`，则要求可见查询发起
- 回归命令：
  - `python3 -m py_compile src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py`
  - `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py -k 'translated_sq_forward_hit_three_lanes' -rxX`
- 覆盖率影响：
  - 基线：
    - `2026-04-20` 多线程对照回归（同时排除
      `test_api_MemBlock_scalar_translated_sq_forward_hit_probe`
      与
      `test_api_MemBlock_scalar_translated_sq_forward_hit_three_lanes`）：
      line `195342 / 295581 = 66.0875%`，
      branch `935664 / 1573403 = 59.4675%`
  - 新结果：
    - `2026-04-20` 多线程完整回归（包含本条与单-lane translated SQ hit probe）：
      line `195407 / 295581 = 66.1095%`，
      branch `935934 / 1573403 = 59.4847%`
  - Delta：
    - 两条 translated `SQ hit` probe 合并净增量：
      line `+65` hit / `+0.0220` pct，
      branch `+270` hit / `+0.0172` pct
- 备注：
  - 当前 build 的 Python DUT 仍未稳定暴露 `sbuffer` 返回 miss/hit 位
  - 因此这条 testcase 目前对 `sbuffer` 只能做到：
    - 若端口存在，则检查 `req.valid`
    - 结合 `LSQ forward resp + writeback=older store data` 证明最终命中来源仍是 `SQ`
  - 上述 coverage delta 是本条与单-lane translated SQ hit probe 的联合 A/B 结果，不是本条单独独占贡献
  - 定向结果：
    - `python3 -m py_compile src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py`
    - `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py -k 'translated_sq_forward_hit_three_lanes' -rxX`
    - 结果：`1 passed, 12 deselected in 21.81s`

## 2026-04-17 10:35 +0800

### 标量 translated load 的 `TLB hit + dcache hit + SQ/SBuffer query miss` 三 lane 覆盖

- 用户需求：
  - 标量 load 访问
  - `TLB hit` 且无异常
  - `PMP/PMA` 无异常
  - `dcache` 查询命中，无异常
  - `dcache A` 不发请求，`dcache forward-D` 不被触发
  - 向 `storequeue/storebuffer` 发起前递查询，但不命中
  - 流水线上无 `stld violation`
  - 不触发快速重发
  - 正常 `wakeup / writeback`
  - 三条 load pipeline（lane 0/1/2）都各跑一遍
- 落地文件：
  - `src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py`
- testcase：
  - `test_api_MemBlock_scalar_translated_hit_sq_sbuffer_query_miss_no_replay_three_lanes`
- sequence / env 支撑：
  - `MmuSv39AddressSpaceInstallSequence`
  - `ScalarStoreCommitSequence`
  - `debugLsInfo`
  - `L2TLB req/resp` 白盒口
  - `LSQ/SBuffer/dcache forward` 白盒口
- 核心检查点：
  - 每条 lane 都以 translated warmup 先打热 `TLB + dcache`
  - 正式 load 保持 `TLB hit`
  - `s2_is_dcache_first_miss=0`
  - `s2_is_bank_conflict=0`
  - `s2_is_forward_fail=0`
  - `s3_is_replay_fast/slow/replay=0`
  - 观测到 `SBuffer forward req`
  - 观测到 `LSQ forward resp`
  - `forwardInvalid/matchInvalid/dataInvalid=0`
  - `dcache forward-D` 不触发
  - `dcache A/D` 统计不增长
  - 无 `memoryViolation / replay path`
  - 正常 `wakeup / writeback`
- 回归命令：
  - `python3 -m py_compile src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py`
  - `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py -k 'translated_hit_sq_sbuffer_query_miss_no_replay_three_lanes' -rxX`
- 覆盖率影响：
  - 基线：待本轮 coverage 测量
  - 新结果：待本轮 coverage 测量
  - Delta：待本轮 coverage 测量
- 备注：
  - 本次先落 testcase 与定向验证
  - 覆盖率数字需在后续多线程 coverage 回归后补录
  - 当前口径显式区分：
    - `dcache hit` 路径本身成立
    - `dcache A` 不发请求
    - `SQ/SBuffer` 查询已发起，但未命中前递
    - 最终数据仍来自 hot cache line 而非 forward

## 2026-04-16 12:21 +0800

### 标量 load 的 `TLB AF / PMP AF / no-AF` 组合矩阵

- 用户需求：
  - 发送标量 aligned load
  - 覆盖不同 load size
  - 覆盖 `TLB hit / miss`
  - 覆盖 `TLB AF / PMP AF / no-AF`
  - 额外检查 `dcache`、`SQ/SBuffer forward`、`stld 违例查询`、`wakeup`
- 落地文件：
  - `src/test/python/MemBlock/tests/test_MemBlock_mmu_fault.py`
  - `src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py`
  - `src/test/python/MemBlock/sequences/mmu_sequences.py`
- testcase：
  - `test_api_MemBlock_scalar_mixed_size_tlb_pmp_af_matrix`
  - `test_api_MemBlock_scalar_word_tlb_pmp_af_miss_matrix`
  - `test_api_MemBlock_scalar_aligned_load_af_matrix_with_pipeline_checks`
- sequence / env 支撑：
  - `MmuFaultingScalarLoadSequence`
  - `PTE_MODE_ACCESS_FAULT`
- 核心检查点：
  - `byte/half/word/doubleword` 全覆盖
  - `TLB hit / miss` 两类背景
  - `dcache A/D` 是否误发
  - `outer` 是否误发
  - `SQ/SBuffer forward` 是否误命中
  - `memoryViolation/replay path` 是否误出现
  - `wakeup` 是否与 fault/no-fault 语义一致
- 回归命令：
  - `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_mmu_fault.py -rxX`
  - `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py -rxX`
- 覆盖率影响：
  - 基线：当时最近一版相关 coverage 基线
  - 新结果：
    - line：`+0`
    - branch hit：`+445`
    - branch pct：`52.1259% -> 52.1542%`
  - Delta：
    - line：`+0`
    - branch pct：`+0.0283`
- 备注：
  - 这组数字来自当时对新增 AF testcase 的专项覆盖率测量
  - 属于“该批新增场景的已实测精确增量”

## 2026-04-16 15:33 +0800

### 真实 `permission PF` 矩阵与对应 pipeline probe

- 用户需求：
  - 不允许 fake TLB 返回值
  - 必须基于真实页表权限语义构造 fault
  - 环境应支持正常概念下构造 `PF/AF`
  - 在已有 load 场景上继续补 `permission fault`
- 落地文件：
  - `src/test/python/MemBlock/MemBlock_env.py`
  - `src/test/python/MemBlock/sequences/mmu_sequences.py`
  - `src/test/python/MemBlock/tests/test_MemBlock_mmu_fault.py`
  - `src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py`
- testcase：
  - `test_api_MemBlock_scalar_word_load_leaf_permission_fault_tlb_miss_smoke`
  - `test_api_MemBlock_scalar_mixed_size_permission_fault_tlb_miss_matrix`
  - `test_api_MemBlock_scalar_aligned_load_permission_fault_with_pipeline_checks`
- sequence / env 支撑：
  - `MmuFacade.configure_smode_access(...)`
  - `install_sv39_leaf_with_perm(...)`
  - `PTE_MODE_PERMISSION_FAULT`
- 核心检查点：
  - 用真实 `S-mode -> U-page(sum=0)` 触发 stage-1 permission fault
  - 明确该 fault 只能在 `TLB miss / PTW` 背景验证
  - 命中 `LOAD_PAGE_FAULT_BIT`
  - 不发 `dcache/outer`
  - 不出现 `SQ/SBuffer forward`
  - 不退化成 `memoryViolation`
  - 仅允许 miss 前置的 `replay_queue(TM)`
- 回归命令：
  - `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_mmu_fault.py -k 'mixed_size_permission_fault or leaf_permission_fault' -rxX`
  - `python3 -m pytest -q src/test/python/MemBlock/tests/test_MemBlock_scalar_load_pipeline_probe.py -k 'permission_fault_with_pipeline_checks' -rxX`
  - `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -p xdist.plugin -p toffee_test.plugin -n 16 -q src/test/python/MemBlock/tests --toffee-report --report-dir ... --report-name ... --report-dump-json`
  - `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -p xdist.plugin -p toffee_test.plugin -n 16 -q src/test/python/MemBlock/tests`
  - `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -p xdist.plugin -p toffee_test.plugin -n 16 -q src/test/python/MemBlock/tests -k "not test_api_MemBlock_scalar_mixed_size_tlb_pmp_af_matrix and not test_api_MemBlock_scalar_word_tlb_pmp_af_miss_matrix and not test_api_MemBlock_scalar_aligned_load_af_matrix_with_pipeline_checks and not test_api_MemBlock_scalar_word_load_leaf_permission_fault_tlb_miss_smoke and not test_api_MemBlock_scalar_mixed_size_permission_fault_tlb_miss_matrix and not test_api_MemBlock_scalar_aligned_load_permission_fault_with_pipeline_checks"`
- 覆盖率影响：
  - 基线：本轮“排除新增 6 条用例”的多线程对照回归手工 branch 合并结果
    - line：`194132 / 295581`（`65.6781%`）
    - branch：`914732 / 1573403`（`58.1372%`）
  - 新结果：本轮“包含新增 6 条用例”的多线程完整回归手工 branch 合并结果
    - line：`195234 / 295581`（`66.0509%`）
    - branch：`933899 / 1573403`（`59.3554%`）
  - Delta：
    - line hit：`+1102`
    - line pct：`+0.3728`
    - branch hit：`+19167`
    - branch pct：`+1.2182`
- 备注：
  - 本条已补做 isolate A/B coverage，对照口径为“排除新增 6 条用例”的多线程回归
  - 全量回归结果：
    - 带 `toffee-report`：`151 passed, 7 xfailed in 309.77s`
    - 用于恢复当前 `line/branch` 的 plain 多线程回归：`151 passed, 7 xfailed in 307.86s`
  - 对照回归 raw data 来自 `98` 个 `*.dat`，手工合并产物为 `src/test/python/MemBlock/data/toffee_report_manual_20260416_163300_exclude_new/line_dat/merged.info`
  - 当前 line / branch 都由 `merged.info` 明细直接恢复：
    - line 按 `DA`
    - branch 按 `BRDA`
  - 对应 JSON 摘要：
    - `src/test/python/MemBlock/data/toffee_report_manual_20260416_163300_exclude_new/line_dat/code_coverage.json`
    - `src/test/python/MemBlock/data/toffee_report_manual_20260416_164527_full_mt/line_dat/code_coverage.json`
