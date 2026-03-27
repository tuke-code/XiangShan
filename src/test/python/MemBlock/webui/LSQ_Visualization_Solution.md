# 香山处理器 LSQ 监控可视化方案

## 1. 概述

本文档基于香山处理器(XiangShan)设计文档中LSQ(访存队列)的架构设计，提供了一个完整的Web可视化监控方案。

### 1.1 LSQ架构回顾

LSQ(Load-Store Queue)是香山处理器访存单元(LSU)的核心组件，包含以下子模块：

| 子模块 | 功能描述 | 容量 |
|--------|----------|------|
| VirtualLoadQueue | Load指令顺序维护队列，跟踪执行状态 | 64项 |
| LoadQueueRAR | 读后读违例检查队列(RAR: Read-After-Read) | 32项 |
| LoadQueueRAW | 写后读违例检查队列(RAW: Read-After-Write) | 32项 |
| LoadQueueReplay | Load指令调度重发队列 | 48项 |
| LoadQueueUncache | MMIO/Noncacheable load处理队列 | - |
| LoadExceptionBuffer | Load指令异常处理缓冲 | - |
| StoreQueue | Store指令队列，支持数据前递 | 48项 |

### 1.2 关键状态信号

#### VirtualLoadQueue状态
- **allocated**: 该项是否分配了load指令
- **committed**: 该项是否已提交
- **isvec**: 该指令是否是向量load指令

#### LoadQueueReplay状态
- **allocated**: 是否已分配
- **scheduled**: 是否已被调度
- **blocking**: 是否正在被阻塞
- **cause**: 重发原因(C_MA, C_TM, C_FF, C_DR, C_DM等)
- **strict**: 是否需要等待之前所有store指令执行完毕

#### StoreQueue状态
- **allocated**: 该项是否有效
- **addrvalid**: 物理地址是否有效
- **datavalid**: 数据是否已准备好
- **committed**: 是否已被ROB提交
- **pending**: 是否是MMIO空间的store
- **nc**: 是否是NonCacheable store

#### LoadQueueRAR状态
- **allocated**: entry是否有效
- **released**: 该指令访问的cacheline是否被release
- **paddr**: 压缩后的物理地址(16bits)

#### LoadQueueRAW状态
- **allocated**: entry是否有效
- **paddr**: 压缩后的物理地址(24bits)
- **mask**: 数据掩码

---

## 2. 可视化方案设计

### 2.1 系统架构

```
┌─────────────────────────────────────────────────────────────┐
│                    LSQ Monitor Dashboard                     │
├─────────────────────────────────────────────────────────────┤
│  ┌──────────────┐  ┌─────────────────────────────────────┐  │
│  │   LSQ架构    │  │         实时性能监控图表             │  │
│  │   概览面板   │  │    (吞吐率/违例统计/队列利用率)      │  │
│  └──────────────┘  └─────────────────────────────────────┘  │
├─────────────────────────────────────────────────────────────┤
│  ┌─────────────────────────┐  ┌─────────────────────────┐   │
│  │   VirtualLoadQueue      │  │    LoadQueueReplay      │   │
│  │   状态可视化            │  │    重发原因分析         │   │
│  └─────────────────────────┘  └─────────────────────────┘   │
├─────────────────────────────────────────────────────────────┤
│  ┌─────────────────────────┐  ┌─────────────────────────┐   │
│  │      StoreQueue         │  │    违例检测监控         │   │
│  │   (Forward数据流)       │  │  (RAR/RAW违例详情)      │   │
│  └─────────────────────────┘  └─────────────────────────┘   │
├─────────────────────────────────────────────────────────────┤
│                    数据流可视化 (SVG动画)                     │
├─────────────────────────────────────────────────────────────┤
│                      事件日志面板                             │
└─────────────────────────────────────────────────────────────┘
```

### 2.2 可视化组件详解

#### 2.2.1 LSQ架构概览面板

**功能**: 显示各子队列的实时占用情况

**展示内容**:
- 队列名称和当前占用率
- 进度条显示容量使用情况
- 颜色编码:
  - 蓝色: VirtualLoadQueue
  - 紫色: LoadQueueReplay
  - 绿色: StoreQueue
  - 黄色: LoadQueueRAR
  - 红色: LoadQueueRAW

**实现要点**:
```javascript
// 队列状态监控
const queueStatus = {
    vlq: { size: 64, allocated: 45, committed: 30 },
    lqr: { size: 48, allocated: 12, scheduled: 8 },
    sq: { size: 48, allocated: 20, committed: 15 },
    lqrar: { size: 32, allocated: 8, released: 3 },
    lqraw: { size: 32, allocated: 5 }
};
```

#### 2.2.2 VirtualLoadQueue可视化

**功能**: 展示load指令的生命周期

**状态表示**:
- 蓝色方块: Allocated但未Committed
- 绿色方块: 已Committed
- 灰色: 空闲项

**性能指标**:
- 入队速率 (instr/cycle)
- 出队速率 (instr/cycle)
- 平均执行延迟 (cycles)

#### 2.2.3 LoadQueueReplay可视化

**功能**: 监控load重发队列状态和原因分析

**状态表示**:
- 蓝色: 已分配但未调度
- 绿色: 已调度(正在重发)
- 红色: 被阻塞等待唤醒

**重发原因分布**:
| 原因代码 | 描述 | 颜色 |
|----------|------|------|
| C_MA | Store-Load预测违例 | 红色 |
| C_TM | TLB Miss | 橙色 |
| C_FF | Store数据未准备好 | 黄色 |
| C_DR | DCache Miss无法分配MSHR | 蓝色 |
| C_DM | DCache Miss | 紫色 |
| C_WF | 路预测器预测错误 | 粉色 |
| C_BC | Bank冲突 | 青色 |
| C_RAR | LoadQueueRAR无空间 | 灰色 |
| C_RAW | LoadQueueRAW无空间 | 深灰 |
| C_NK | Store-to-Load违例 | 棕色 |
| C_MF | LoadMisalignBuffer无空间 | 深红 |

#### 2.2.4 StoreQueue可视化

**功能**: 监控store指令状态和数据前递

**状态表示**:
- 单个方块显示多个状态位:
  - A: AddrValid (地址有效)
  - D: DataValid (数据有效)
  - C: Committed (已提交)

**数据统计**:
- Forward请求次数
- Forward成功次数
- MMIO Store数量
- NonCacheable Store数量

#### 2.2.5 违例检测监控

**LoadQueueRAR监控**:
- 已分配项数
- Released标记项数
- 违例发生次数
- 实时RAR队列状态图

**LoadQueueRAW监控**:
- 已分配项数
- 等待Store地址的项数
- 违例发生次数
- 实时RAW队列状态图

#### 2.2.6 数据流可视化

**功能**: SVG动画展示指令在LSQ中的流动

**节点**:
- Dispatch (指令分发)
- VLQ (VirtualLoadQueue)
- SQ (StoreQueue)
- LQR (LoadQueueReplay)
- RAW (LoadQueueRAW检查)
- LoadUnit (执行单元)

**流动线**:
- 蓝色线: Load指令流
- 绿色线: Store指令流
- 黄色线: Forward数据
- 红色线: 违例检测信号

**动画效果**:
- 虚线流动表示数据传输
- 粒子动画表示指令移动
- 线条高亮表示当前活跃路径

#### 2.2.7 事件日志

**功能**: 记录和显示关键事件

**事件类型**:
- 入队事件 (蓝色)
- 违例事件 (红色)
- 重发事件 (紫色)
- 数据前递 (绿色)

**格式**:
```
[000123] VirtualLoadQueue: Load指令入队 entry=5
[000124] LoadQueueRAR: 检测到Release entry=3
[000125] StoreQueue: 数据前递成功
[000126] LoadQueueRAW: 写后读违例 detected!
```

---

## 3. 实时数据更新机制

### 3.1 数据采集接口

在实际硬件中，需要通过以下方式采集数据:

```verilog
// 示例: 从香山处理器采集LSQ状态
module lsq_monitor (
    input clk,
    input rst,
    // VirtualLoadQueue接口
    output [63:0] vlq_allocated,
    output [63:0] vlq_committed,
    // LoadQueueReplay接口
    output [47:0] lqr_allocated,
    output [47:0] lqr_scheduled,
    output [47:0] lqr_blocking,
    output [10:0] lqr_cause [47:0],
    // StoreQueue接口
    output [47:0] sq_allocated,
    output [47:0] sq_addrvalid,
    output [47:0] sq_datavalid,
    output [47:0] sq_committed,
    // ... 其他接口
);
```

### 3.2 数据传输协议

**WebSocket实时传输**:
```javascript
// 前端WebSocket连接
const ws = new WebSocket('ws://fpga-host:8080/lsq');

ws.onmessage = (event) => {
    const data = JSON.parse(event.data);
    updateQueueStatus(data);
    updateCharts(data);
    addLogEntry(data.event);
};
```

**数据格式**:
```json
{
    "cycle": 12345,
    "vlq": {
        "allocated": [0,1,1,0,1,...],
        "committed": [0,0,1,0,1,...]
    },
    "lqr": {
        "allocated": [0,1,0,0,...],
        "scheduled": [0,1,0,0,...],
        "blocking": [0,0,0,0,...],
        "causes": ["", "C_DM", "", "", ...]
    },
    "sq": {
        "allocated": [1,0,1,1,...],
        "addrvalid": [1,0,1,0,...],
        "datavalid": [0,0,1,1,...],
        "committed": [0,0,0,1,...]
    },
    "events": [
        {"type": "enqueue", "queue": "vlq", "entry": 5}
    ]
}
```

### 3.3 性能优化

**增量更新**:
- 只传输变化的状态位
- 使用位图压缩减少数据量
- 采样率可配置(每N个周期更新一次)

**前端优化**:
- 使用requestAnimationFrame批量更新DOM
- Canvas渲染图表而非SVG(大量数据点)
- 虚拟滚动显示大量队列项

---

## 4. 交互功能

### 4.1 控制面板

**模拟控制**:
- 开始/暂停模拟
- 重置系统
- 调整模拟速度

**视图控制**:
- 缩放队列视图
- 筛选事件类型
- 配置显示选项

### 4.2 详细信息查看

**点击队列项**:
- 显示完整状态信息
- 显示指令历史
- 显示相关依赖关系

**违例详情**:
- RAR违例: 显示涉及的load指令和release时机
- RAW违例: 显示store和load的地址、数据依赖

### 4.3 数据分析

**历史趋势**:
- 队列占用率趋势
- 违例发生频率
- 重发原因统计

**性能报告**:
- 平均执行延迟
- Forward成功率
- 各类违例占比

---

## 5. 部署和使用

### 5.1 在线演示

系统已部署至: **https://ca35zreqdbgfa.ok.kimi.link**

### 5.2 本地部署

```bash
# 1. 克隆代码
git clone https://github.com/your-repo/lsq-monitor.git
cd lsq-monitor

# 2. 启动HTTP服务器
python3 -m http.server 8080

# 3. 浏览器访问
open http://localhost:8080
```

### 5.3 与香山处理器集成

**步骤1**: 在香山RTL中添加监控模块
```verilog
// 在LSQ模块中实例化监控接口
lsq_monitor u_monitor (
    .clk(clock),
    .rst(reset),
    .vlq_allocated(vlq.io.allocated),
    // ... 其他信号
);
```

**步骤2**: 使用Firesim或FPGA采集数据
```python
# 通过Firesim仿真获取数据
from firesim import FireSim

sim = FireSim()
sim.run()
lsq_data = sim.get_lsq_state()
```

**步骤3**: 启动WebSocket服务器转发数据
```python
import asyncio
import websockets
import json

async def lsq_server(websocket, path):
    while True:
        data = read_lsq_state_from_fpga()
        await websocket.send(json.dumps(data))
        await asyncio.sleep(0.001)  # 1ms采样

start_server = websockets.serve(lsq_server, "0.0.0.0", 8080)
asyncio.get_event_loop().run_until_complete(start_server)
asyncio.get_event_loop().run_forever()
```

---

## 6. 扩展功能

### 6.1 高级分析

**依赖图可视化**:
- 显示load-store之间的依赖关系
- 关键路径分析
- 性能瓶颈识别

**预测分析**:
- 基于历史数据预测违例
- 重发概率估计
- 队列溢出预警

### 6.2 调试支持

**断点设置**:
- 在特定违例发生时暂停
- 条件触发(如特定地址访问)
- 单步执行模式

**状态快照**:
- 保存特定时刻的完整状态
- 支持状态回放
- 对比不同执行路径

### 6.3 多核支持

**多核监控**:
- 同时显示多个核心的LSQ状态
- 跨核违例检测
- 缓存一致性可视化

---

## 7. 总结

本可视化方案提供了对香山处理器LSQ的全面监控能力，包括:

1. **实时状态监控**: 5个子队列的实时状态显示
2. **性能分析**: 吞吐率、延迟、违例统计
3. **数据流可视化**: 指令在LSQ中的流动动画
4. **事件追踪**: 详细的操作日志记录
5. **交互式调试**: 丰富的交互功能支持深度分析

该方案可用于:
- 处理器性能调优
- 程序行为分析
- 教学演示
- 架构设计验证

---

## 参考文档

1. [香山处理器LSQ设计文档](https://docs.xiangshan.cc/projects/design/zh-cn/latest/memblock/LSU/LSQ/)
2. [LoadQueueReplay详细设计](https://docs.xiangshan.cc/projects/design/zh-cn/latest/memblock/LSU/LSQ/LoadQueueReplay/)
3. [StoreQueue详细设计](https://docs.xiangshan.cc/projects/design/zh-cn/latest/memblock/LSU/LSQ/StoreQueue/)
4. [VirtualLoadQueue详细设计](https://docs.xiangshan.cc/projects/design/zh-cn/latest/memblock/LSU/LSQ/VirtualLoadQueu/)
5. [LoadQueueRAR详细设计](https://docs.xiangshan.cc/projects/design/zh-cn/latest/memblock/LSU/LSQ/LoadQueueRAR/)
6. [LoadQueueRAW详细设计](https://docs.xiangshan.cc/projects/design/zh-cn/latest/memblock/LSU/LSQ/LoadQueueRAW/)
