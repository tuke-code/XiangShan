# XSPdb 调试工作流指南

XSPdb 是基于 Python `pdb` 的 RISC-V IP 调试器，专为香山处理器的 difftest 接口定制。它允许您像使用 GDB 调试软件一样，单步执行、设置断点、采集波形、查看寄存器，从而实现硬件信号与软件状态的联合观测。

本文档将以一个**完整的调试工作流**为主线，演示如何使用 XSPdb 调试香山核。

**目标：** 使用预编译的模拟器运行 `ready-to-run/microbench.bin`，在特定指令处暂停仿真，观察内部状态，并截取特定异常前后的波形。

---

## 0. 前置准备 (Prerequisites)

在开始运行前，必须确保您当前的终端正处于香山处理器的**项目根目录**。

**环境构建**：
要运行 XSPdb，您有两种前置准备方式（详情请参考根目录 `README.md` 中的 "Run with xspdb" 章节）：
1. **使用预编译二进制包 (Prebuilt Binaries)**：推荐没有修改硬件设计的纯软件调试场景使用。您只需具备标准 Python 环境。
2. **从源码编译 (Build from source code)**：需安装 `picker` 验证工具，并通过 `make pdb` 编译 XiangShan Python 包。

同时，教程中所使用的预编译二进制文件（例如测试程序 `ready-to-run/microbench.bin`）需要提前获取；如果项目中尚未包含这些文件，您需要先查阅相关的环境构建文档进行生成或下载。

## 1. 启动与加载

首先，进入香山根目录，启动 XSPdb 环境：

```bash
# 启动 XSPdb 进入交互式命令行 (会自动包含上述环境的 Python 库并载入模拟器)
make pdb-run
```

启动后，您将看到 `(XiangShan)` 提示符（行为类似于 `(Pdb)`）。
在执行仿真前，需要先加载待测试的二进制程序。XSPdb 支持将程序加载到内存或 flash 区域。

```text
# 加载到内存（默认 PMEM_BASE）
(XiangShan) xload ready-to-run/microbench.bin
[Info] [Running] ready-to-run/microbench.bin

# 也可以加载到 Flash
(XiangShan) xflash ready-to-run/flash.bin
```

> **提示：** 想找命令帮助？随时输入 `xcmds` 查看所有由 XSPdb 提供的高级命令列表，或 `xapis` 查看底层 API 方法。

---

## 2. 基础执行与状态检查

程序加载完成后，您可以执行基本的步进控制：

```text
# 推进 1000 个时钟周期
(XiangShan) xstep 1000
[Info] step 1000 cycles complete

# 或者按“指令”单步执行 (自动执行至下一条指令提交完成)
(XiangShan) xistep 1

# 查看当前软件执行的体系结构状态（PC）：
(XiangShan) xpc
PC[0]: 0x80000000    Instr: 0x00000093
...

# 反汇编特定地址的指令序列，可查验当前内存的真实指令上下文
(XiangShan) xdasm 0x80000000 4
0x80000000: 00000093  li      ra, 0
0x80000004: 0182b283  ld      t0, 24(t0)
...
```

> **提示：** 随时用 Tab 键自动补全冗长的信号名！

排查硬件模块的内部状态，可以使用 `xprint` 检查任意层次的信号值：

```text
# 查看全局 timer (支持 Tab 补全多级路径)
(XiangShan) xprint SimTop_top.SimTop.timer
value: 0x3e8  width: 64

# 动态修改内部信号的值以注入测试激励：
(XiangShan) xset SimTop_top.SimTop.timer 0
```

---

## 3. 基于波形文件的调试 (Waveform)

XSPdb 支持对底层 C++ 仿真器的波形生成进行动态控制（生成高压缩率的 `.fst` 格式波形）。通过仅在关键区间开启波形记录，能有效避免全局记录所导致的仿真性能退化和存储开销。

```text
# 开启波形（会在当前目录自动生成一个波形文件，如 wave_20261011_120000.fst）
(XiangShan) xwave_on

# 仿真推进 200 个周期，该区间的信号状态将被完整采样记录
(XiangShan) xstep 200

# 强制将波形缓冲区数据写入磁盘
(XiangShan) xwave_flush

# 调试观测结束，关闭波形记录进程
(XiangShan) xwave_off
```

> **提示：** 导出的 `.fst` 文件可以使用开源的波形查看工具 [GTKWave](http://gtkwave.sourceforge.net/) 打开查看：
> `gtkwave wave_20261011_120000.fst`

如果要在指定的波形文件里记录，可以使用：
`xwave_on /absolute/path/to/my_trace.fst`

此外，针对批量脚本，可以直接在启动命令里配置开启/关闭周期：
```bash
python3 scripts/pdb-run.py --image xxx.bin -b 1000 -e 2000
```
该参数在 1000 周期时开启波形，2000 周期时关闭波形。

---

## 4. 断点与自动停止 (Breakpoints)

在复杂的硬件调试场景中，通常需要在特定的错误事件发生前介入仿真。XSPdb 提供了层级丰富的断点系统。

### 4.1 简单的信号断点 (`xbreak`)

下面是一个通过设定计时器进行自动停止的示例：

```text
# 设置等于断点 (条件可以为 eq, ne, gt, lt 等)
(XiangShan) xbreak SimTop_top.SimTop.timer eq 5000

# 继续执行仿真，最多运行 10 万个周期
(XiangShan) xstep 100000
[Info] Find break point (xbreak-SimTop_top.SimTop.timer-eq-5000), break (step 3800 cycles) at cycle: 5000 (0x1388)
```

**命中断点后暂停：** 仿真器一旦命中条件，会立刻中止推进并返回终端！
您可以查询当前的断点：
```text
(XiangShan) xbreak_list
xbreak-SimTop_top.SimTop.timer-eq-5000(0x1388) eq 0x1388 hinted: False
```

当调试结束时，可以用 `xunbreak` 清除信号断点。

### 4.2 基于复杂表达式的触发器 (`xbreak_expr`)

如果您需要组合多个条件（类似 SystemVerilog assertion），XSPdb 提供了原生的 C++ 级表达式判别，性能远超纯 Python。

```text
# 仅当 PC 等于特定值，且前端 IFU 流水线 s2 阶段有效时触发：
(XiangShan) xbreak_expr SimTop_top.SimTop.cpu.l_soc.core_with_l2.core.backend.inner_ctrlBlock.rob.difftest_commit_pc == 0x80001000 and SimTop_top.SimTop.cpu.l_soc.core_with_l2.core.frontend.inner_ifu.s2_valid == 1

# 捕获任何通用异常 (异常排查首选)
(XiangShan) xbreak_expr SimTop_top.SimTop.cpu.l_soc.core_with_l2.core.backend.inner_ctrlBlock.rob.io_exception_valid_REG == 1
```

**基于时间窗口的时序匹配：**
某些诊断条件依赖于信号序列的历史状态。系统支持通过 `hold(周期, 表达式)` 和 `within(周期, 表达式)` 构建时序检测规则：

```text
# hold：全局计时器 timer 已经连续保持了 4 个周期大于 100 才会触发
(XiangShan) xbreak_expr hold(4, SimTop_top.SimTop.timer > 100)

# within：在过去 20 个周期内计时器曾达到过 500
# 举例：在过去 20 周期里发生过特定事件，且当前又遇到了异常有效信号即触发暂停
(XiangShan) xbreak_expr within(20, SimTop_top.SimTop.timer == 500) and SimTop_top.SimTop.cpu.l_soc.core_with_l2.core.backend.inner_ctrlBlock.rob.io_exception_valid_REG == 1
```

清理表达式断点使用：`xunbreak_expr <key>` (key 取自 `xbreak_expr_list` 列出的自动命名标签)。

---

## 5. 有限状态机触发器：FSM Trigger

对于需要跨越多个时钟周期匹配事件序列的复杂场景（例如特定的 cache miss/refill 序列），XSPdb 提供了一个基于有限状态机（FSM）引擎的特定序列探测器 `xbreak_fsm`。

您可以创建一个文本文件（如 `trigger.fsm`），利用类似于状态机的语法 (`state/if/elif/else/goto/trigger`) 来描述事件流转序列。
这里举一个追踪 PC 序列：`0x10000004` -> `0x10000008` -> `0x80000000` 跳转链的完整 FSM 示例：

```text
# trigger.fsm
start WAIT_PC_0

state WAIT_PC_0:
  # 等待序列的第 1 个 PC：0x10000004
  if SimTop_top.SimTop.cpu.l_soc.core_with_l2.core.backend.inner_ctrlBlock.rob.difftest_commit_pc == 0x10000004 goto WAIT_PC_4
  else goto WAIT_PC_0

state WAIT_PC_4:
  # 等待序列的第 2 个 PC：0x10000008
  if SimTop_top.SimTop.cpu.l_soc.core_with_l2.core.backend.inner_ctrlBlock.rob.difftest_commit_pc == 0x10000008 goto WAIT_PC_80
  # 如果看到 0x10000000，可能是新序列的起始附近，保持当前状态继续等待
  elif SimTop_top.SimTop.cpu.l_soc.core_with_l2.core.backend.inner_ctrlBlock.rob.difftest_commit_pc == 0x10000000 goto WAIT_PC_4
  else goto WAIT_PC_4

state WAIT_PC_80:
  # 等待最终目标的触发
  # 也可以在 action 中更新内部的 $counter 等全局变量：inc $my_counter
  if SimTop_top.SimTop.cpu.l_soc.core_with_l2.core.backend.inner_ctrlBlock.rob.difftest_commit_pc == 0x80000000 trigger
  elif SimTop_top.SimTop.cpu.l_soc.core_with_l2.core.backend.inner_ctrlBlock.rob.difftest_commit_pc == 0x10000000 goto WAIT_PC_4
  else goto WAIT_PC_80
```

然后在 XSPdb 中载入并运行：
```text
(XiangShan) xbreak_fsm ./trigger.fsm
(XiangShan) xstep 5000000
```
当该 FSM 转移至 `trigger` 动作时，XSPdb 会自动中断 `xstep`。同时可以使用命令检查状态机进度或进行清除：
```text
(XiangShan) xbreak_fsm_status
name=trigger.fsm state=WAIT_PC_80 triggered=True trigger_state=WAIT_PC_80
(XiangShan) xbreak_fsm_clear
```

---

## 6. 信号观察点 (Watchpoints)

对于持续监控信号值的变化（例如外设状态寄存器），可以使用 `xwatch`。仿真器会在受监视信号发生翻转时自动打印日志：

```text
(XiangShan) xwatch SimTop_top.SimTop.timer
(XiangShan) xstep 100
[Signal Change] SimTop_top.SimTop.timer 0x0 -> 0x1
...
(XiangShan) xunwatch SimTop_top.SimTop.timer
```

**指令提交 PC 监控：**
为了排查软件逻辑错误，XSPdb 提供了一个直接侦测特定指令提交的命令 `xwatch_commit_pc`：

```text
(XiangShan) xwatch_commit_pc 0x80000004
```
一旦提交指令的 PC 大于或等于该地址，仿真就会自动暂停（兼具断点效果）。由于不需要判断其它复杂的体系结构前端条件，这非常适合专门用来排查分支预测异常等控制流问题。

---

## 7. 历史波形回溯 (Fork Backup)

全程波形采集会显著降低仿真速度。为解决性能开销与故障前导波形保留之间的矛盾，XSPdb 提供了基于进程 Fork 的历史波形回溯机制（Fork Backup）。

该功能在后台定期创建一个挂起的仿真子进程（作为时间点快照）。主进程能够免受 I/O 开销的影响全速运行。当主进程命中异常断点时，唤醒最近的一个子进程。子进程将从快照点恢复运行，补齐到异常触发点之间的高精度波形文件。

```text
# 使用方法：开启 Fork 备份回溯窗口，保留前 5.0 秒（可自定义）的仿真状态，生成文件存入 ./ 目录，日志输出至 ./fork.log
(XiangShan) xfork_backup_on 5.0 . ./fork.log
(XiangShan) xfork_backup_status

# 接下来，正常设置你的故障侦测断点：
(XiangShan) xbreak SimTop_top.SimTop.cpu.l_soc.core_with_l2.core.backend.inner_ctrlBlock.rob.difftest_commit_pc eq 0x80008000

# 主进程将全速推进仿真，并跳过高开销的操作：
(XiangShan) xstep 1000000

# [触发中断] 主进程命中目标异常事件。随后 Fork Backup 会自动激活并在后台重构出切片波形。
# 此时可以通过 xwave_continue 拼接 fork 子进程生成的波形片段：
(XiangShan) xwave_on /absolute/path/to/my_debug.fst
(XiangShan) xwave_continue /absolute/path/to/wave_fork_backup_XXX.fst

# 分析完成之后记得关闭：
(XiangShan) xfork_backup_off
```

---

## 8. 高级应用模式

### 8.1 寄存器管理
手动配置指定的软件现场以辅助复现：
```text
(XiangShan) xset_ireg x1 0x1234
(XiangShan) xset_freg f0 0x3f800000
(XiangShan) xset_mpc 0x80000000
```
利用 `xparse_reg_file` / `xload_reg_file` 可以加载预先保存的完整寄存器快照文件。

### 8.2 Difftest 对照诊断
XSPdb 深入衔接了 NEMU 等测试基准。在需要核对运行时计算逻辑时可以使用联合仿真支持：
```bash
python3 scripts/pdb-run.py --image ready-to-run/microbench.bin --diff ./ready-to-run/riscv64-nemu-interpreter-so
```
当系统状态不一致时，执行 `xexpdiffstate ds` 将向 Python 局部命名空间实例化出一个包含详尽对参信息的句柄 `ds`，可以通过如 `p ds.regs.csr.mepc` 进行深层次检索。

### 8.3 数据导出 & 日志录制重放
如需将特定区间的内存导出并在其他工具链中解析：
```text
(XiangShan) xexport_bin 0x80100000 ./mem_dump
```
它会自动生成 `mem_dump_flash.bin` 和 `mem_dump_ram.bin` 。

您可以通过内建环境的反馈记录，重放历史命令及自动化流程：
```bash
python3 scripts/pdb-run.py --replay my_debug_log.log
```

### 8.4 脚本批处理 (Batch Scripts)
为了推行自动化的回归测试或验证，XSPdb 可以从外部脚本导入命令序列直接执行。例如将调试命令预设为文本，作为启动参数载入：
```bash
python3 scripts/pdb-run.py --script docs/XSPdb/examples/tutorial_script.txt
```

### 8.5 图形化界面 (TUI)
除命令行外，XSPdb 还提供基于文本终端的图形化工具。
输入：`xui`
XSPdb 包含基于 Textual 构建的高清文本界面，可同时查看代码、寄存器与终端输出。
- 使用 `xtheme cycle` 切换配色主题。
- 习惯布局后使用 `xui save` 下次打开可以直接恢复！

---

## 9. 命令速查表 (Cheat Sheet)

| 功能分类 | 常用命令 | 简述 |
|---------|---------|------|
| **基础控制** | `xcmds`, `xapis` | 显示所有指令或内部 API 列表 |
| | `xload`, `xload_script` | 载入 bin 固件 / 加载自动控制脚本 |
| | `xui` / `xreset` | 进入高级 TUI 界面 / 软复位 DUT |
| **步骤与监控** | `xstep N` / `xistep N` | 向前推进 N 时钟周期 / 向前推进 N 条提交指令 |
| | `xpc` / `xdasm` / `xdasmflash` | 打印当前核心正在提交的 PC / 将内存进行反汇编 |
| | `xwatch` / `xprint` | 监察某信号的连续变更 / 单次信号值打印 |
| | `xwatch_commit_pc` | 设置基于 PC 提交目标的触发器 |
| **断点触发系统**| `xbreak ... eq N`| 信号值匹配断点 |
| | `xbreak_expr ... and...`| 支持条件的复杂 C++ 级断点 |
| | `xbreak_expr_list` / `xunbreak` | 查看已设置的表达式触发器 / 清除信号断点 |
| | `xbreak_fsm` / `xbreak_fsm_status`| 载入状态机配置文件 / 查询状态机进度状态 |
| **内存与波形管理**| `xwave_on` / `off` / `flush` | 控制 .fst 波形的起/停/输出 |
| | `xwave_continue` | 拼接多段波形文件 |
| | `xfork_backup_on` / `status` | 启用波形 Fork 备份机制 / 查看备份状态 |
| | `xset_ireg` / `xset_freg` / `xset_mpc`| 设置指定体系结构寄存器现场 |
| | `xexport_bin` / `xexport_ram` | 导出整体或指定 RAM 区间数据的快照 |
| | `xexpdiffstate DS` | 输出 Difftest 协同状态到 Python 环境变量 |

通过掌握以上核心命令，您将能高效地分析并解决 RISC-V 硬件设计与底层软件的集成缺陷。
