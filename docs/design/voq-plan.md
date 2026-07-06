# VecOrderQueue 设计方案

> **状态**: Draft v1
> **创建日期**: 2026-06-08
> **目标**: 为向量执行资源（VAGQ / Gather）提供指令级顺序化准入控制，支持精确异常和死锁避免

---

## 1. 背景与动机

### 1.1 问题

向量非连续访存指令在 VAGQ 中处理，vrgather 等指令在 Gather 运算器中处理。这些资源容量有限（VAGQ: `VAGQSize` 项，Gather: `GatherSize` 项）。

若允许年轻指令先于年老指令进入执行资源，存在两个问题：

1. **潜在死锁**：年轻指令占满资源并触发异常，年老指令无法进入，异常无法作为 ROB 最老项被处理
2. **无效执行**：年老指令若后续触发异常或 redirect，年轻指令已执行的 work 被浪费

### 1.2 解决方案

VecOrderQueue 在 dispatch 阶段维护一个指令级 FIFO 窗口，确保：

- 只有前 N 条**向量执行指令**的 uop 能离开发射队列进入执行资源
- 按 ROB 年龄顺序串行化资源分配
- 资源配额用完时阻塞后续指令

---

## 2. 模块定位

```
Dispatch ──→ VecOrderQueue (Enq) ──→ IQ wakeup (仅前 N 条指令的 uop 可发射)
                 │
                 ↓ (Deq)
              ROB commit 时更新窗口
```

VecOrderQueue 位于 Dispatch 与 Issue Queue 之间：

- 仅处理**使用向量执行资源**的指令（VAGQ 或 Gather），不处理 unit-stride/whole-register 等
- 不存储 uop，仅记录指令级元信息
- 通过唤醒 IQ 中指定 robIdx 范围的 uop 来控制准入

---

## 3. 数据结构

### 3.1 Queue Entry

容量 32 项，Enq/Deq 指针维护 FIFO：

| 字段 | 位宽 | 说明 |
|---|---|---|
| `valid` | 1 | 项有效 |
| `robIdx` | 8 | 首 uop 的 ROB 序号 |
| `uopCnt` | 4 | 该指令产生的 uop 数 |
| `useVAGQ` | 1 | 需要 VAGQ 表项 |
| `useGather` | 1 | 需要 Gather 运算器 |
| `entryIdx` | 3 | VAGQ 表项索引（useVAGQ=1 时有效） |

### 3.2 全局状态

| 信号 | 说明 |
|---|---|
| `enqPtr` | 下一个可分配的 entry 索引 |
| `deqPtr` | 窗口起始 entry 索引 |
| `vagqUsage` | 窗口内累计 VAGQ 项数 |
| `gatherUsage` | 窗口内累计 Gather 项数 |

---

## 4. 操作流程

### 4.1 Dispatch 阶段 (Enq)

```
1. 译码确定指令类型
2. 若指令使用 VAGQ 或 Gather:
   a. 检查 Queue 是否满 (enqPtr == deqPtr 且 valid[enqPtr])
   b. 分配 entry:
      - robIdx   = 首 uop 的 robIdx
      - uopCnt   = LMUL（或 max(emul, lmul)）
      - useVAGQ  = (isStride || isIndexed)
      - useGather = (isGather || isSegment)
   c. enqPtr++
3. 若指令不使用向量执行资源: 不分配 entry，直接通过
```

### 4.2 唤醒计算

#### 4.2.1 VAGQ 表项分配

VecOrderQueue 维护 `VAGQSize` 位的 `vagqEntryBitmap`，标记空闲/占用。

**分配** (Enq 时)：

```
if useVAGQ:
  entryIdx = find_first_zero(vagqEntryBitmap)
  if entryIdx == NOT_FOUND:
    stall dispatch  // VAGQ 满
  vagqEntryBitmap[entryIdx] = 1
```

**释放** (Deq 时)：

```
vagqEntryBitmap[entryIdx] = 0  // 由 ROB commit 触发
```

`entryIdx` 写入该指令的全部 uop，uop 发射时携带。

#### 4.2.2 唤醒计算

```
vagqUsed  = 0
gatherUsed = 0
wake.valid = false
wake.robIdx = ***
wake.entryIdx = ***

for i from deqPtr to enqPtr-1:
  if !valid[i]: continue
  newVAGQ  = vagqUsed  + (useVAGQ[i]  ? uopCnt[i] : 0)
  newGather = gatherUsed + (useGather[i] ? uopCnt[i] : 0)
  if newVAGQ > VAGQSize OR newGather > GatherSize:
    break   // 资源配额耗尽，停止唤醒
  vagqUsed   = newVAGQ
  gatherUsed = newGather
  if (entry[i].valid &&
     !entry[i].wake &&
     !flush         &&
      entry[i].req <= vagqEntryBitmap's left &&
      这条指令的所有uop都在IQ里)
    wake.valid = true
    wake.robIdx = entry[i].robIdx
    wake.entryIdx = entry[i].entryIdx
```

向 IQ 发送唤醒信号：允许 `wake.robIdx` 的 uop 发射。

### 4.3 ROB 提交阶段 (Deq)

```
ROB commit 信号到达 (committedRobIdx):
  while valid[deqPtr] AND robIdx[deqPtr] + uopCnt[deqPtr] - 1 < committedRobIdx:
    valid[deqPtr] = 0
    deqPtr++
```

---

## 5. 与 VAGQ / Gather 的关系

```
┌── useVAGQ  ──→ VAGQ (表项分配)
VecOrderQueue ────┤
                  └── useGather ──→ Gather (运算器分配)
```

VecOrderQueue 仅控制**准入顺序**，不参与实际的表项/运算器分配。VAGQ 和 Gather 各自独立管理内部资源，但 VecOrderQueue 保证进入这些资源的指令窗口是 ROB 连续的。

**共享资源的指令**（如 `vluxseg` 既用 VAGQ 又用 Gather）同时消耗两边配额，VecOrderQueue 在唤醒计算中一并考虑。

---

## 6. 容量约束

| 参数 | 值 | 说明 |
|---|---|---|
| `QueueDepth` | 32 | Queue entry 数，覆盖足够大的 ROB 窗口 |
| `VAGQSize` |  8 | VAGQ 表项数 |
| `GatherSize` | TBD | Gather 运算器容量 |

`QueueDepth` 需要 ≥ ROB 中可能积压的向量指令数，32 对应当前香山 ROB 深度（~256）绰绰有余。

---

## 7. 设计决策

| # | 问题 | 决策 | 理由 |
|---|---|---|---|
| 1 | Queue 粒度 | 指令级（非 uop 级） | 一条指令对应一个执行资源分配单位，uop 级过于细碎 |
| 2 | 容量限制方式 | 累计 uop 数 vs 独立容量 | VAGQ 和 Gather 容量独立，各自累计 |
| 3 | 唤醒方式 | 向 IQ 发送 robIdx 范围 | 不改 IQ 内部逻辑，IQ 自行匹配放行 |
| 4 | 非向量指令 | 不分配 entry，不阻塞 | 标量指令不受影响 |
| 5 | VAGQ 表项分配 | VOQ wakeup时分配 entryIdx，写入 uop | uop 按索引直接写入表项 |
| 6 | VAGQ 表项释放 | ROB commit 时释放 bitmap | 与 VecOrderQueue Deq 同步 |

---

## 8. 参考资料

- [VAGQ 设计方案](../VAGQ/plan.md)
- RISC-V V Extension Spec v1.0
