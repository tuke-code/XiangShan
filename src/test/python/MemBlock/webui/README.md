# 香山处理器 LSQ 监控可视化方案

[![XiangShan](https://img.shields.io/badge/XiangShan-LSQ-blue)](https://github.com/OpenXiangShan/XiangShan)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

基于香山处理器(XiangShan)设计文档的LSQ(Load-Store Queue)监控可视化系统。

## 在线演示

**访问地址**: https://ca35zreqdbgfa.ok.kimi.link

## 项目简介

本项目深入分析香山处理器访存队列(LSQ)的架构设计，提供了一套完整的Web可视化监控方案，帮助理解处理器访存单元的工作原理，辅助性能调优和架构验证。

### 核心特性

- 实时监控LSQ 5个子队列状态
- 可视化展示11种Load重发原因
- 实时性能图表(吞吐率/违例统计)
- 数据流动画展示
- 事件日志记录
- 交互式仿真模拟

## 快速开始

### 在线使用

直接访问: https://ca35zreqdbgfa.ok.kimi.link

1. 点击"开始模拟"启动仿真
2. 观察各队列状态变化
3. 查看实时图表和事件日志
4. 点击队列项查看详细信息

### 本地部署

```bash
# 克隆代码
git clone https://github.com/your-repo/lsq-monitor.git
cd lsq-monitor

# 启动HTTP服务器
python3 -m http.server 8080

# 浏览器访问
open http://localhost:8080
```

## LSQ架构概览

```
LSQ (Load-Store Queue)
├── LoadQueue
│   ├── VirtualLoadQueue (64项)     - Load指令顺序维护
│   ├── LoadQueueRAR (32项)         - 读后读违例检测
│   ├── LoadQueueRAW (32项)         - 写后读违例检测
│   ├── LoadQueueReplay (48项)      - Load重发队列
│   ├── LoadQueueUncache            - MMIO/Noncacheable处理
│   └── LoadExceptionBuffer         - 异常处理
└── StoreQueue (48项)               - Store指令队列
```

## 可视化功能

### 1. LSQ架构概览面板

- 显示各子队列实时占用率
- 进度条显示容量使用情况
- 颜色编码区分不同队列

### 2. VirtualLoadQueue监控

- 展示load指令生命周期
- Allocated/Committed状态可视化
- 入队/出队速率统计
- 平均执行延迟

### 3. LoadQueueReplay监控

- 重发队列状态跟踪
- 11种重发原因分布
  - C_MA: Store-Load预测违例
  - C_TM: TLB Miss
  - C_FF: Store数据未准备好
  - C_DR: DCache Miss无法分配MSHR
  - C_DM: DCache Miss
  - C_WF: 路预测器错误
  - C_BC: Bank冲突
  - C_RAR: LQRAR无空间
  - C_RAW: LQRAW无空间
  - C_NK: ST-LD违例
  - C_MF: MisalignBuffer满

### 4. StoreQueue监控

- Store指令状态跟踪
- 数据前递(Forward)统计
- MMIO/NonCacheable Store计数
- AddrValid/DataValid/Committed状态

### 5. 违例检测监控

**LoadQueueRAR (读后读违例)**
- 多核环境下的load-to-load违例检测
- Release标记跟踪
- 违例次数统计

**LoadQueueRAW (写后读违例)**
- Store-to-load forwarding违例检测
- 等待Store地址计数
- 违例次数统计

### 6. 数据流可视化

- SVG动画展示指令流动
- 不同颜色区分Load/Store/Forward/违例检测
- 实时更新活跃路径

### 7. 事件日志

- 记录所有关键操作
- 支持按类型筛选
- 时间戳显示
- 彩色高亮不同类型事件

## 技术栈

- **前端**: HTML5, CSS3, Tailwind CSS
- **图表**: Chart.js
- **图标**: Font Awesome
- **字体**: JetBrains Mono, Inter

## 与香山处理器集成

### 硬件接口

```verilog
module lsq_monitor (
    input clk,
    input rst,
    // VirtualLoadQueue接口
    output [63:0] vlq_allocated,
    output [63:0] vlq_committed,
    // LoadQueueReplay接口
    output [47:0] lqr_allocated,
    output [47:0] lqr_scheduled,
    // StoreQueue接口
    output [47:0] sq_allocated,
    output [47:0] sq_addrvalid,
    output [47:0] sq_datavalid,
    // ...
);
```

### WebSocket数据传输

```javascript
const ws = new WebSocket('ws://fpga-host:8080/lsq');

ws.onmessage = (event) => {
    const data = JSON.parse(event.data);
    updateQueueStatus(data);
    updateCharts(data);
};
```

## 应用场景

### 1. 处理器性能调优

- 识别性能瓶颈
- 分析违例原因
- 优化程序访存模式

### 2. 架构设计验证

- 验证LSQ设计正确性
- 测试边界情况
- 评估队列容量

### 3. 教学演示

- 直观展示处理器内部工作原理
- 帮助学生理解乱序执行
- 演示违例检测机制

### 4. 程序行为分析

- 分析程序访存模式
- 识别不良访存行为
- 指导程序优化

## 文档

- [详细技术文档](LSQ_Visualization_Solution.md) - 完整的设计和实现文档
- [总结报告](总结报告.md) - 项目总结和分析

## 参考资源

### 香山处理器文档

1. [LSQ整体设计文档](https://docs.xiangshan.cc/projects/design/zh-cn/latest/memblock/LSU/LSQ/)
2. [LoadQueueReplay详细设计](https://docs.xiangshan.cc/projects/design/zh-cn/latest/memblock/LSU/LSQ/LoadQueueReplay/)
3. [StoreQueue详细设计](https://docs.xiangshan.cc/projects/design/zh-cn/latest/memblock/LSU/LSQ/StoreQueue/)
4. [VirtualLoadQueue详细设计](https://docs.xiangshan.cc/projects/design/zh-cn/latest/memblock/LSU/LSQ/VirtualLoadQueu/)
5. [LoadQueueRAR详细设计](https://docs.xiangshan.cc/projects/design/zh-cn/latest/memblock/LSU/LSQ/LoadQueueRAR/)
6. [LoadQueueRAW详细设计](https://docs.xiangshan.cc/projects/design/zh-cn/latest/memblock/LSU/LSQ/LoadQueueRAW/)

### 开源代码

- [香山处理器](https://github.com/OpenXiangShan/XiangShan)
- [设计文档](https://github.com/OpenXiangShan/XiangShan-Design-Doc)

## 项目文件

```
.
├── lsq_visualization.html          # Web可视化系统
├── LSQ_Visualization_Solution.md   # 详细技术文档
├── lsq_architecture_diagram.png    # 架构概览图
├── lsq_state_machines.png          # 状态机图
├── lsq_dataflow_timing.png         # 数据流时序图
├── 总结报告.md                      # 项目总结报告
└── README.md                        # 本文件
```

## 贡献

欢迎提交Issue和Pull Request！

## 许可证

MIT License

## 致谢

感谢香山处理器团队的开源贡献！

---

**基于**: 香山处理器设计文档 (Kunminghu-V2R2-alpha1)

**文档版本**: 1.0
