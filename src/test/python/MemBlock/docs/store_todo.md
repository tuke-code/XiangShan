# MemBlock 标量 Store 实施地图

## 1. 文档目的

本文档不再维护独立 coverage 数字，也不重复充当第二份 `coverage_todo.md`。它的职责收敛为：

1. 作为 store 方向的专题入口；
2. 把当前 store 补强任务映射到具体 testcase 文件；
3. 明确每个 testcase 文件下一轮应承接什么能力。

当前覆盖率状态与百分比真源始终是：

- `src/test/python/MemBlock/docs/coverage_summary.md`
- `src/test/python/MemBlock/docs/coverage_todo.md`

已知 DUT bug / `xfail` 真源始终是：

- `src/test/python/MemBlock/docs/BUGS.md`
- testcase 内的精确 `xfail`

## 2. 当前 store 方向的真实现状

从当前 testcase 分布可以确认三件事：

1. cacheable scalar store 主路径已经立住；
2. store misalign 已经具备基础 cross-16B stimulus 和 cross-page 精确 `xfail`；
3. translated NC/MMIO store 已进入真实 DUT 回归，但边界语义仍未完全闭环。

当前已存在的代表性证明点包括：

- `tests/test_MemBlock_scalar_store_pipeline.py`
  - partial word store + aligned load
  - high-offset byte store + aligned load
  - same-dword multi-byte partial merge
  - full store + partial overwrite
  - interleaved partial stores across two addresses
  - store burst + interleaved younger load + final flush
- `tests/test_MemBlock_store_misalign.py`
  - cross-16B `SD/SW/SH` 基础 split
  - cross-page strict `xfail`
- `tests/test_MemBlock_random_store.py`
  - mixed MMIO + cacheable store flush exclusion 的正常回归证明点
- `tests/test_MemBlock_uncache_semantics.py`
  - translated PBMT=NC store smoke
  - `mmioBusy` 阻塞 younger cacheable load retire

需要特别注意：

1. `mmio_store_excluded_from_drain_observed` 是否在最新 full coverage 中命中，必须以后续完整 coverage 回归为准，而不是仅凭 testcase 存在与否判断。
2. cross-page `SB` 在语义上不会跨页，因此不应把它列为 cross-page misalign TODO；cross-page 宽度矩阵应只围绕 `SH/SW/SD`。

## 3. testcase 文件到任务的映射

### 3.1 `tests/test_MemBlock_scalar_store_pipeline.py`

本文件只承接 cacheable scalar store 深状态，不碰 MMIO/NC/translated 语义。

下一轮应优先补：

1. 链式 partial merge
   - `4B partial -> 高偏移 1B overwrite -> full load`
   - 同一 dword 连续 byte-lane partial store
2. 更深的 queue / flush 组合
   - 继续围绕 committed store 停留窗口、younger load 穿插、最终统一 flush
3. 不重复补已经存在的基础矩阵
   - 如单条 partial word、单条 high-offset byte、full+partial overwrite、interleaved dual-address partial store

主目标模块：

- `NewStoreQueue.sv`
- `SbufferData.sv`
- `Sbuffer.sv`

### 3.2 `tests/test_MemBlock_store_misalign.py`

本文件只承接 split / misalign / cross-page 收口，不承担 exception 专题。

下一轮应优先补：

1. cross-page `SW` 矩阵
   - 至少覆盖 `+0xFFD` / `+0xFFE`
2. 继续保留现有 cross-page strict `xfail`
   - 目标是稳定证明“shadow/load 可见已成立，但 flush/drain 仍卡在 DUT”
3. 不在这里混入 load-misalign 或异常专题

主目标模块：

- `StoreMisalignBuffer.sv`
- `StoreUnit.sv`
- `ExceptionInfoGen.sv`

### 3.3 `tests/test_MemBlock_uncache_semantics.py`

本文件承接 translated PBMT 场景下的 NC/MMIO store 语义。

下一轮应优先补：

1. translated `PBMT=NC` store burst + flush
2. translated `PBMT=MMIO` store 与 translated cacheable store 的 mixed flush
3. 更长窗口的 `mmioBusy`
4. capability gap 仍允许精确 `xfail`
   - 前提是 testcase 仍然表达真实语义，而不是放宽断言

不在本文件重复补：

1. bare MMIO + cacheable mixed flush exclusion
   - 这已由 `tests/test_MemBlock_random_store.py` 提供正常回归证明点

主目标模块：

- `Uncache.sv`
- store 相关 function coverage 未命中点

## 4. 实施顺序

推荐按下面顺序推进，而不是并发散补：

### P0

1. `scalar_store_pipeline`
   - partial 深矩阵和 committed-store 停留窗口
2. `store_misalign`
   - cross-page `SW` 扩展
3. `uncache_semantics`
   - translated NC burst / translated MMIO mixed flush

### P1

1. 更长窗口的 `mmioBusy`
2. flush/drain 非顺畅窗口
3. store 与更复杂 replay / ordering 的交叉

## 5. 验收规则

每轮补强至少要满足：

1. 新增正常路径 testcase 全部 `PASS`
2. 仍有 DUT 缺陷的场景保持精确 `xfail`
3. 不把 testcase 降级成“只点亮一个白盒事件”
4. 状态同步回：
   - `src/test/python/MemBlock/docs/coverage_summary.md`
   - `src/test/python/MemBlock/docs/coverage_todo.md`

定向回归建议固定为：

```bash
python3 -m pytest -q \
  src/test/python/MemBlock/tests/test_MemBlock_scalar_store_pipeline.py \
  src/test/python/MemBlock/tests/test_MemBlock_store_misalign.py \
  src/test/python/MemBlock/tests/test_MemBlock_uncache_semantics.py
```
