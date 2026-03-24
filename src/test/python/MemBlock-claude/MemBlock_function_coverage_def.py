# coding=utf-8
"""
MemBlock 功能覆盖率模型

本文件定义针对 XiangShan MemBlock 的功能覆盖率组（CovGroup）、
功能点（watch_point）以及各功能点的检查函数（Bins）。

覆盖组划分：
  FG-REDIRECT     : 流水线重定向（刷新）功能
  FG-LSQ-ENQ      : LSQ（Load/Store Queue）入队分配功能
  FG-MEM-ISSUE    : 整数/向量访存指令发射功能
  FG-WB           : 写回与内存违例检测功能
  FG-TL-DCACHE    : DCache TileLink 总线请求/响应功能

使用方式：
  from MemBlock_function_coverage_def import get_coverage_groups
  groups = get_coverage_groups(dut)
"""

import toffee.funcov as fc

# ──────────────────────────────────────────────────────────────────────────────
# 覆盖组实例（模块级，避免重复创建）
# ──────────────────────────────────────────────────────────────────────────────
_cov_redirect   = fc.CovGroup("FG-REDIRECT")
_cov_lsq_enq    = fc.CovGroup("FG-LSQ-ENQ")
_cov_mem_issue  = fc.CovGroup("FG-MEM-ISSUE")
_cov_wb         = fc.CovGroup("FG-WB")
_cov_tl_dcache  = fc.CovGroup("FG-TL-DCACHE")

_ALL_GROUPS = [
    _cov_redirect,
    _cov_lsq_enq,
    _cov_mem_issue,
    _cov_wb,
    _cov_tl_dcache,
]

_initialized = False  # 防止对同一 dut 重复绑定


# ──────────────────────────────────────────────────────────────────────────────
# FG-REDIRECT：流水线重定向功能
# ──────────────────────────────────────────────────────────────────────────────
def _init_cov_redirect(g: fc.CovGroup, dut) -> None:
    """
    覆盖 MemBlock 接收后端/执行单元发来的重定向（流水线刷新）场景。

    重定向信号由 io_redirect_valid 标志，level 表示刷新范围：
      0 = 分支预测错误（轻量刷新）
      1 = 异常/中断（重量刷新）
    """
    g.add_watch_point(
        dut,
        {
            # 基础重定向触发
            "CK-REDIRECT-VALID":
                lambda x: x.io_redirect_valid.value == 1,

            # level=0：分支预测重定向
            "CK-REDIRECT-BRANCH":
                lambda x: (x.io_redirect_valid.value == 1
                           and x.io_redirect_bits_level.value == 0),

            # level=1：异常/中断重定向（全局刷新）
            "CK-REDIRECT-EXCEPTION":
                lambda x: (x.io_redirect_valid.value == 1
                           and x.io_redirect_bits_level.value == 1),
        },
        name="FC-REDIRECT-CTRL",
    )


# ──────────────────────────────────────────────────────────────────────────────
# FG-LSQ-ENQ：LSQ 入队分配功能
# ──────────────────────────────────────────────────────────────────────────────
def _init_cov_lsq_enq(g: fc.CovGroup, dut) -> None:
    """
    覆盖 MemBlock 接收后端派遣的 Load/Store 指令入队 LSQ 的场景。

    XiangShan 支持 8 路同时入队（port 0~7），
    本覆盖组关注 port 0 作为代表性通道；
    同时关注队列满（sqFull / lqFull）状态。
    """
    # port 0 入队请求
    g.add_watch_point(
        dut,
        {
            "CK-ENQ-REQ-VALID-P0":
                lambda x: x.io_ooo_to_mem_enqLsq_req_0_valid.value == 1,

            "CK-ENQ-STORE-P0":
                lambda x: (x.io_ooo_to_mem_enqLsq_req_0_valid.value == 1
                           and x.io_ooo_to_mem_enqLsq_req_0_bits_isStore.value == 1),

            "CK-ENQ-LOAD-P0":
                lambda x: (x.io_ooo_to_mem_enqLsq_req_0_valid.value == 1
                           and x.io_ooo_to_mem_enqLsq_req_0_bits_isStore.value == 0),
        },
        name="FC-ENQ-REQ",
    )

    # 队列满状态
    g.add_watch_point(
        dut,
        {
            "CK-SQ-FULL":
                lambda x: x.io_mem_to_ooo_sqFull.value == 1,

            "CK-LQ-FULL":
                lambda x: x.io_mem_to_ooo_lqFull.value == 1,

            "CK-BOTH-FULL":
                lambda x: (x.io_mem_to_ooo_sqFull.value == 1
                           and x.io_mem_to_ooo_lqFull.value == 1),
        },
        name="FC-ENQ-FULL",
    )


# ──────────────────────────────────────────────────────────────────────────────
# FG-MEM-ISSUE：访存指令发射功能
# ──────────────────────────────────────────────────────────────────────────────
def _init_cov_mem_issue(g: fc.CovGroup, dut) -> None:
    """
    覆盖后端向 MemBlock 发射整数访存指令（intIssue）和
    向量访存指令（vecIssue）的场景。

    XiangShan 有 7 条整数访存发射通道（port 0~6），
    2 条向量访存发射通道（port 0~1），
    本覆盖组以 port 0 为代表。
    """
    # 整数发射
    g.add_watch_point(
        dut,
        {
            "CK-INT-ISSUE-VALID-P0":
                lambda x: x.io_ooo_to_mem_intIssue_0_valid.value == 1,

            "CK-INT-ISSUE-READY-P0":
                lambda x: x.io_ooo_to_mem_intIssue_0_ready.value == 1,

            "CK-INT-ISSUE-FIRE-P0":
                lambda x: (x.io_ooo_to_mem_intIssue_0_valid.value == 1
                           and x.io_ooo_to_mem_intIssue_0_ready.value == 1),
        },
        name="FC-INT-ISSUE",
    )

    # 向量发射
    g.add_watch_point(
        dut,
        {
            "CK-VEC-ISSUE-VALID-P0":
                lambda x: x.io_ooo_to_mem_vecIssue_0_valid.value == 1,

            "CK-VEC-ISSUE-FIRE-P0":
                lambda x: (x.io_ooo_to_mem_vecIssue_0_valid.value == 1
                           and x.io_ooo_to_mem_vecIssue_0_ready.value == 1),
        },
        name="FC-VEC-ISSUE",
    )


# ──────────────────────────────────────────────────────────────────────────────
# FG-WB：写回与内存违例检测功能
# ──────────────────────────────────────────────────────────────────────────────
def _init_cov_wb(g: fc.CovGroup, dut) -> None:
    """
    覆盖 MemBlock 向后端写回结果（intWriteback）以及
    检测到内存顺序违例（memoryViolation）的场景。

    XiangShan 有 7 条整数写回通道（port 0~6）。
    memoryViolation 在 Load-Store / Load-Load 违例时拉高。
    """
    # 整数写回（port 0）
    g.add_watch_point(
        dut,
        {
            "CK-INT-WB-VALID-P0":
                lambda x: x.io_mem_to_ooo_intWriteback_0_valid.value == 1,

            "CK-INT-WB-VALID-P1":
                lambda x: x.io_mem_to_ooo_intWriteback_1_valid.value == 1,
        },
        name="FC-INT-WB",
    )

    # 内存违例检测
    g.add_watch_point(
        dut,
        {
            "CK-MEM-VIOLATION":
                lambda x: x.io_mem_to_ooo_memoryViolation_valid.value == 1,
        },
        name="FC-MEM-VIOLATION",
    )

    # Store Buffer 排空
    g.add_watch_point(
        dut,
        {
            "CK-SB-EMPTY":
                lambda x: x.io_mem_to_ooo_sbIsEmpty.value == 1,
        },
        name="FC-SB-EMPTY",
    )


# ──────────────────────────────────────────────────────────────────────────────
# FG-TL-DCACHE：DCache TileLink 总线功能
# ──────────────────────────────────────────────────────────────────────────────
def _init_cov_tl_dcache(g: fc.CovGroup, dut) -> None:
    """
    覆盖 DCache 通过 TileLink 协议向 L2 Cache 发送请求（A 通道）
    以及接收 L2 响应（D 通道）的场景。

    A 通道 opcode 含义（TileLink 规范）：
      4 = AcquireBlock（缺失时请求整个 Cache 行）
      5 = AcquirePerm  （权限升级）
    D 通道 opcode 含义：
      4 = GrantData    （含数据的授权响应）
      5 = Grant        （不含数据的授权响应）
    """
    # A 通道请求（DCache → L2）
    g.add_watch_point(
        dut,
        {
            "CK-TL-A-VALID":
                lambda x: x.auto_inner_buffers_out_a_valid.value == 1,

            "CK-TL-A-FIRE":
                lambda x: (x.auto_inner_buffers_out_a_valid.value == 1
                           and x.auto_inner_buffers_out_a_ready.value == 1),

            "CK-TL-A-ACQUIRE-BLOCK":
                lambda x: (x.auto_inner_buffers_out_a_valid.value == 1
                           and x.auto_inner_buffers_out_a_bits_opcode.value == 4),
        },
        name="FC-TL-A-REQ",
    )

    # D 通道响应（L2 → DCache）
    g.add_watch_point(
        dut,
        {
            "CK-TL-D-VALID":
                lambda x: x.auto_inner_buffers_out_d_valid.value == 1,

            "CK-TL-D-FIRE":
                lambda x: (x.auto_inner_buffers_out_d_valid.value == 1
                           and x.auto_inner_buffers_out_d_ready.value == 1),

            "CK-TL-D-GRANT-DATA":
                lambda x: (x.auto_inner_buffers_out_d_valid.value == 1
                           and x.auto_inner_buffers_out_d_bits_opcode.value == 4),
        },
        name="FC-TL-D-RESP",
    )


# ──────────────────────────────────────────────────────────────────────────────
# 主接口函数
# ──────────────────────────────────────────────────────────────────────────────
def get_coverage_groups(dut) -> list:
    """
    获取 MemBlock 功能覆盖率组列表。

    本函数在首次调用时初始化所有覆盖组的检查点；
    后续调用（同一 dut 对象）直接返回已初始化的列表，避免重复绑定。

    Args:
        dut: DUTMemBlock 实例（由 create_dut 或 dut fixture 提供）；
             传入 None 时仅返回覆盖组框架（不绑定检查点），
             用于覆盖组结构查询。

    Returns:
        List[fc.CovGroup]: 包含 5 个功能覆盖组的列表。
    """
    global _initialized

    if dut is None:
        return _ALL_GROUPS

    if not _initialized:
        _init_cov_redirect(_cov_redirect, dut)
        _init_cov_lsq_enq(_cov_lsq_enq, dut)
        _init_cov_mem_issue(_cov_mem_issue, dut)
        _init_cov_wb(_cov_wb, dut)
        _init_cov_tl_dcache(_cov_tl_dcache, dut)
        _initialized = True

    return _ALL_GROUPS
