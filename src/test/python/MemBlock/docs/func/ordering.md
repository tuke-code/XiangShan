# Ordering 功能点

> 本文档使用 Input-Process-Output (IPO) 模式描述 ordering 相关功能点。

---

### CMB-ORD-001: two stores -> load (same-addr)

本功能点验证 store-to-load 排序语义中，younger load 在两条 older store 依次写入同一地址后应读到最新 store 的数据。这是处理器存储排序中最基础的 RAW（read-after-write）正确性要求。DUT 的行为路径为：store0 先 commit 并将数据写入 sbuffer（或 SQ forward），store1 后 commit 覆盖同一地址的数据，younger load 在 store1 commit 后发射，通过 sbuffer forward（若 DCache miss）或 DCache 命中（若 sbuffer 已 drain）获取数据。白盒观测信号包括 forward 源的选择——为确保得到 store1 而非 store0 的数据，load 必须匹配 sbuffer 中最新 committed store 的 vtag。验证不仅对比 load writeback 的即时数据等于 store1 值，还要在 flush drain 后检查 memory 中地址 A 的值是否为 store1 的最终写入值。本功能点与 CMB-ORD-003（有序列）的区别在于只关注两条 store 一条 load 的最小排序用例，而 003 使用更长的指令序列来验证排序的传递性（transitivity）。

| 项目 | 描述 |
|------|------|
| 输入 | 两条 older cacheable store 写入同一地址 A (第二次覆盖第一次)；一条 younger load 读取地址 A。 |
| 处理 | store0 commit；store1 commit 覆盖 store0 数据；younger load 在 store1 commit 后 issue，DCache 或 sbuffer forward 返回 store1 的数据。 |
| 输出 | load writeback 数据等于 store1 的数据 (非 store0)；flush drain 后地址 A 的值为 store1 的数据。 |
| 验证 | 观测 writeback / drain / event 等，与参考模型比对 |

---

### CMB-ORD-002: store -> unrelated load (无污染)

本功能点验证 store 与 load 在不同地址上操作时，load 不应被无关的 store 数据污染——这是存储排序中隔离性（isolation）的核心要求。DUT 中 older store 将数据写入地址 A 并 commit 到 sbuffer，younger load 访问无关地址 B。由于地址 A 与 B 不相等，load 不应触发 sbuffer forward，而应走正常的 DCache/outer 路径返回 B 处的旧值。白盒观测信号包括 sbuffer forward 比较逻辑的结果——load 的地址与 sbuffer 中所有 committed store 的地址逐一比较，若全部失配则 forward 不被触发。验证的最终结论包含两个维度：即时维度上 load writeback 数据应等于 preload 阶段写入 B 地址的初始值（而非 store 写入 A 的值）；持久维度上 flush drain 后地址 A 的数据应为 store 写入值、地址 B 的数据应保持不变。本功能点与 CMB-ORD-001 的对比关系是：001 验证 forward 的命中路径，002 验证 forward 的未命中路径，二者共同覆盖 sbuffer forward 比较逻辑的全集。

| 项目 | 描述 |
|------|------|
| 输入 | older cacheable store 写入地址 A；younger load 读取无关地址 B。 |
| 处理 | store commit 到 sbuffer；younger load 访问无关地址，不触发 sbuffer forward，走正常 DCache/outer 路径。 |
| 输出 | load writeback 正确 (等于 preload 值，不被 store 污染)；store drain 后地址 A 数据正确。 |
| 验证 | 观测 writeback / drain / event 等，与参考模型比对 |

---

### CMB-ORD-003: 定向混合 ld/st 序列 (覆盖/重读)

本功能点通过一个精心编排的 load/store 混合序列来验证 DUT 在多步操作间维护排序一致性的能力——包括数据覆盖后的正确读取、交替地址的独立写入、以及最终 flush 后的持久数据正确性。序列固定为：store A、load A、store B、load A、store A（覆盖）、load A、load B——每次 load 的期望值由当前已完成 commit 的 store 们共同决定。DUT 在每条 store commit 后更新 SQ 和 sbuffer 状态，每条 load 在 store 间穿插发射时依赖 sbuffer forward 或 DCache 返回最新值。白盒观测信号是每次 load 的写回数据是否与序列语义一致，特别是 store A 被覆盖后后续 load A 应返回新值而非旧值。本功能点与 CMB-ORD-001 的区别在于序列化验证更全面的排序语义：001 只验证了 store-store-load 的最小排序单元，而 003 通过多次地址切换和覆盖验证了排序的传递性和地址独立性。与 CMB-ORD-002 的关联是：002 验证了不同地址的隔离性，003 在此基础上的 load A 和 load B 实质上是对隔离开销的进一步检验。

| 项目 | 描述 |
|------|------|
| 输入 | 按固定序列执行 store A, load A, store B, load A, store A(覆盖), load A, load B。 |
| 处理 | 每条 store commit 后更新 SQ/sbuffer；每条 load 在 store 间穿插 issue；sbuffer forward 或 DCache 返回最新值。 |
| 输出 | 每次 load 的 writeback 数据与序列语义一致；最终 flush drain 后地址 A/B 的 golden memory 验证正确。 |
| 验证 | 观测 writeback / drain / event 等，与参考模型比对 |
