# XiangShan Codex 规则

## AI 文档路径

本项目与 `mem_ut` 相关的 AI 说明文档统一放在：

- [AI_DOC](/nfs/home/lixiangrui/work/memblock_ut/XiangShan/AI_DOC)

当前 `mem_ut` 远端编译仿真方案与流程文档为：

- [mem_ut远端编译仿真方案与流程.md](/nfs/home/lixiangrui/work/memblock_ut/XiangShan/AI_DOC/mem_ut远端编译仿真方案与流程.md)

后续处理 `mem_ut` 环境时，应优先阅读该文档。

## mem_ut UVM 路径规则

`mem_ut` 当前位于：

- `XiangShan/mem_ut`

memblock UVM 环境位于：

- `mem_ut/ver/ut/memblock`

主仿真目录位于：

- `mem_ut/ver/ut/memblock/sim`

处理 memblock UVM 环境时，优先从以下目录运行命令：

```bash
/nfs/home/lixiangrui/work/memblock_ut/XiangShan/mem_ut/ver/ut/memblock/sim
```

## 远端编译仿真规则

本项目采用双节点 flow：

- 当前节点：编辑代码、分析日志、触发命令
- `eda01`：真实执行 `vcs` / `verdi` 编译仿真

不要默认假设本地能直接运行 `vcs`。

应优先使用 `sim/Makefile` 中的远端目标：

- `make eda_compile tc=tc_sanity mode=base_fun`
- `make eda_run tc=tc_sanity mode=base_fun`
- `make eda_run_bg tc=tc_sanity mode=base_fun`
- `make eda_status tc=tc_sanity mode=base_fun`
- `make eda_tail tc=tc_sanity mode=base_fun`

这些目标会调用：

- `mem_ut/ver/ut/memblock/sim/remote_eda_make.sh`
- `mem_ut/ver/ut/memblock/sim/eda01_entry.sh`

## 远端环境初始化规则

当前默认远端 bootstrap 为：

```bash
source /usr/share/Modules/init/bash >/dev/null 2>&1 || true; \
module load synopsys/vcs/Q-2020.03-SP2 license; \
module load synopsys/verdi/R-2020.12-SP1 license
```

如果再次出现 `vcs: command not found`，优先检查远端 `module` 初始化与
工具版本，不要直接回退到本地 `make compile`。

## MEMBLOCK_PROJECT 规则

必须保证：

```text
$MEMBLOCK_PROJECT/XiangShan/build_memblock/rtl/filelist.f
```

路径有效。

因此 `MEMBLOCK_PROJECT` 必须指向 `XiangShan` 的上一级目录，而不是
`XiangShan` 本身。

## 当前已验证状态

当前已经验证：

- 在 `XiangShan/mem_ut` 新位置下远端编译链可用
- `eda01` 上可以成功拉起 `vcs`
- 当前环境问题已解决
- 当前剩余问题是源码编译错误，而不是远端方案错误

当前已知编译阻塞点：

- `mem_ut/ver/ut/memblock/tb/dut_inst.sv`
- 约第 `2531` 行
- VCS 语法错误：
  `.auto_inner_frontendBridge_instr_uncache_in_a_ready(...)`

因此，对当前环境的正确验证标准是：

- 能稳定复现到这个源码错误，说明远端编译方案正常

## Git 规则

处理该 flow 时：

- 不要把 `mem_ut` 改动和 XiangShan 其他无关脏改动混在一起提交
- 优先只 stage 目标 `mem_ut/**`
- 远端编译基础设施变更应与 `mem_ut` 环境一起维护
