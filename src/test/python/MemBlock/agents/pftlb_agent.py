# coding=utf-8
"""
pfTLB (L2 prefetcher TLB) request agent。

驱动 `io_l2_tlb_req_*` 端口，模拟 L2 Cache prefetcher (BestOffsetPrefetch)
发起的 TLB 查询请求。

TLBFA_2 (`pftlb_storage_fa`) 由该路径独占驱动：
  MemBlock.io_l2_tlb_req → dtlb_reqs(L2toL1DTLBPortIndex) → dtlb_prefetch → TLBFA_2

TLBFA_2 是 NonBlock TLB：请求后下一拍一定返回 resp_valid=1 的单拍脉冲。
因此 wait_response 不能在 advance 之后再用轮询等 resp_valid，而应该在
advance 之前注册 after_step_callback，在下一拍 combo 更新后立即捕获。
同时需要结合 resp_miss / exception 信号确认响应有效。
"""

from __future__ import annotations

from dataclasses import dataclass

from frontend_facade import _OptionalSignalBundle

# TlbCmd values from MMUConst
TLB_CMD_READ = 0
TLB_CMD_WRITE = 1
TLB_CMD_EXEC = 2
TLB_CMD_ATOMIC = 3


# ---------------------------------------------------------------------------
# 信号 bundle
# ---------------------------------------------------------------------------

class _PftlbReqBundle(_OptionalSignalBundle):
    SIGNAL_MAP = {
        "valid": "valid",
        "bits_vaddr": "bits_vaddr",
        "bits_fullva": "bits_fullva",
        "bits_checkfullva": "bits_checkfullva",
        "bits_cmd": "bits_cmd",
        "bits_hyperinst": "bits_hyperinst",
        "bits_hlvx": "bits_hlvx",
        "bits_kill": "bits_kill",
        "bits_no_translate": "bits_no_translate",
        "bits_memidx_is_ld": "bits_memidx_is_ld",
        "bits_memidx_is_st": "bits_memidx_is_st",
        "bits_memidx_idx": "bits_memidx_idx",
        "bits_pmp_addr": "bits_pmp_addr",
        "bits_debug_robIdx_flag": "bits_debug_robIdx_flag",
        "bits_debug_robIdx_value": "bits_debug_robIdx_value",
    }

    def drive_idle(self) -> None:
        for name in self.SIGNAL_MAP:
            self.write(name, 0)


class _PftlbRespBundle(_OptionalSignalBundle):
    SIGNAL_MAP = {
        "valid": "valid",
        "bits_miss": "bits_miss",
        "bits_paddr_0": "bits_paddr_0",
        "bits_paddr_1": "bits_paddr_1",
        "bits_gpaddr_0": "bits_gpaddr_0",
        "bits_gpaddr_1": "bits_gpaddr_1",
        "bits_fullva": "bits_fullva",
        "bits_pbmt_0": "bits_pbmt_0",
        "bits_pbmt_1": "bits_pbmt_1",
        "bits_ptw": "bits_ptw",
        "bits_fast": "bits_fast",
        "bits_memidx_is_ld": "bits_memidx_is_ld",
        "bits_memidx_is_st": "bits_memidx_is_st",
        "bits_memidx_idx": "bits_memidx_idx",
        "bits_excp_0_is": "bits_excp_0_is",
        "bits_excp_0_pf_ld": "bits_excp_0_pf_ld",
        "bits_excp_0_pf_st": "bits_excp_0_pf_st",
        "bits_excp_0_pf_instr": "bits_excp_0_pf_instr",
        "bits_excp_0_af_ld": "bits_excp_0_af_ld",
        "bits_excp_0_af_st": "bits_excp_0_af_st",
        "bits_excp_0_af_instr": "bits_excp_0_af_instr",
        "bits_excp_0_gpf_ld": "bits_excp_0_gpf_ld",
        "bits_excp_0_gpf_st": "bits_excp_0_gpf_st",
        "bits_excp_0_gpf_instr": "bits_excp_0_gpf_instr",
        "bits_excp_0_va": "bits_excp_0_va",
        "bits_excp_1_va": "bits_excp_1_va",
    }


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------

@dataclass
class PftlbReq:
    """pfTLB 请求描述。"""
    vaddr: int                      # full virtual address (49:0)
    cmd: int = TLB_CMD_READ         # TlbCmd: 0=read, 1=write, 2=exec
    is_prefetch: bool = True
    no_translate: bool = False
    kill: bool = False
    memidx_is_ld: bool = True
    memidx_is_st: bool = False
    memidx_idx: int = 0
    check_fullva: bool = True
    hlvx: bool = False
    hyperinst: bool = False


@dataclass
class PftlbResp:
    """pfTLB 响应描述。"""
    miss: bool = True
    paddr_0: int = 0
    paddr_1: int = 0
    gpaddr_0: int = 0
    gpaddr_1: int = 0
    pbmt_0: int = 0
    pbmt_1: int = 0
    fullva: int = 0
    fast: bool = False
    ptw: bool = False
    excp_is: bool = False
    excp_pf_ld: bool = False
    excp_af_ld: bool = False
    excp_gpf_ld: bool = False
    excp_va: int = 0
    memidx_is_ld: bool = False
    memidx_is_st: bool = False
    memidx_idx: int = 0

    @property
    def any_exception(self) -> bool:
        return self.excp_is or self.excp_pf_ld or self.excp_af_ld or self.excp_gpf_ld

    @property
    def terminal(self) -> bool:
        """翻译已终态化：hit（miss==0）或出现异常。

        NonBlock TLB 的 resp_valid 只表示端口握手完成，不表示翻译完成。
        terminal=True 才表示翻译结果已确定（命中或异常终结）。
        """
        return not self.miss or self.any_exception


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class PftlbAgent:
    """驱动 pfTLB 请求并采集响应。

    TLBFA_2 是 NonBlock TLB：请求送出后下一拍 resp_valid 一定为 1
    且为单拍脉冲。

    三层完成语义（由浅到深）：

    1. resp.fire — 端口握手完成
       req_valid=1 → 下一拍 resp_valid=1（单拍脉冲）。
       send_and_wait() / translated_load() 在此层返回。
       PftlbResp 的 miss/hit 可能尚未确定。

    2. terminal — 翻译终态
       miss==0（命中）或 any_exception（异常终结）。
       blocking_translated_load() 持续拉 req_valid，
       逐拍检查 PftlbResp.terminal，直到 terminal=True 才返回。

    3. PTW trace — 额外闭环证据
       PTW A/D 通道活动独立于前两者，由 ptw_responder.trace 观测。
       不与 resp.fire 或 terminal 混绑。

    用法::

        agent = env.pftlb_agent

        # 端口握手完成（不保证 terminal）
        resp = agent.translated_load(0x42000000)
        assert resp.miss  # NonBlock 首次一定 miss

        # 等待翻译终态（terminal）
        resp = agent.blocking_translated_load(0x42000000)
        assert not resp.miss  # PTW refill 后命中
    """

    _REQ_PREFIX = "io_l2_tlb_req_req_"
    _RESP_PREFIX = "io_l2_tlb_req_resp_"

    def __init__(self, env) -> None:
        self.env = env
        self.req = _PftlbReqBundle(self._REQ_PREFIX, env.dut)
        self.resp = _PftlbRespBundle(self._RESP_PREFIX, env.dut)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def reset(self) -> None:
        self.req.drive_idle()

    def drive_idle(self) -> None:
        self.req.drive_idle()

    # ------------------------------------------------------------------
    # 请求驱动 (底层)
    # ------------------------------------------------------------------

    def _drive_req(self, req: PftlbReq) -> None:
        va = int(req.vaddr) & ((1 << 50) - 1)
        self.req.write("bits_vaddr", va)
        self.req.write("bits_fullva", va)
        self.req.write("bits_checkfullva", 1 if req.check_fullva else 0)
        self.req.write("bits_cmd", int(req.cmd) & 0x7)
        self.req.write("bits_kill", 1 if req.kill else 0)
        self.req.write("bits_no_translate", 1 if req.no_translate else 0)
        self.req.write("bits_memidx_is_ld", 1 if req.memidx_is_ld else 0)
        self.req.write("bits_memidx_is_st", 1 if req.memidx_is_st else 0)
        self.req.write("bits_memidx_idx", int(req.memidx_idx) & 0xFF)
        self.req.write("bits_hyperinst", 1 if req.hyperinst else 0)
        self.req.write("bits_hlvx", 1 if req.hlvx else 0)

    def drive_request(self, *, valid: int = 1, **fields: int) -> None:
        """底层：直接写 req 信号。"""
        for name, value in fields.items():
            self.req.write(name, int(value))
        self.req.write("valid", int(valid))

    def clear_request(self) -> None:
        self.req.drive_idle()

    # ------------------------------------------------------------------
    # 响应判定
    # ------------------------------------------------------------------

    def _resp_fired(self) -> bool:
        """端口握手完成：resp_valid=1 且 resp_miss 信号可用。

        NonBlock TLB 在下一拍 resp_valid 一定为 1（单拍脉冲）。
        仅表示端口级事务完成，不表示翻译已终态化。
        翻译终态参见 PftlbResp.terminal。
        """
        if not self.resp.read("valid", 0):
            return False
        miss = self.resp.read("bits_miss", None)
        if miss is None:
            return False
        return True

    # ------------------------------------------------------------------
    # 请求→响应组合方法 (正确处理 NonBlock TLB 时序)
    # ------------------------------------------------------------------

    def send_and_wait(self, req: PftlbReq, *, max_cycles: int = 256) -> PftlbResp:
        """发送 pfTLB 请求并等待响应。正确处理 NonBlock TLB 单拍脉冲时序。

        流程：
        1. 注册 after_step_callback 用于捕获下一拍 resp_valid 脉冲
        2. 驱动 req 信号并 pulse req_valid
        3. advance 1 拍触发 callback 采样
        4. 解析响应
        """
        captured = [None]

        def _capture():
            if captured[0] is not None:
                return
            if self._resp_fired():
                captured[0] = self.resp.snapshot()

        self.env.add_after_step_callback(_capture)
        try:
            self._drive_req(req)
            self.req.write("valid", 1)
            if hasattr(self.env, "refresh_comb"):
                self.env.refresh_comb()
            self.env.advance_cycles(1)
            self.req.write("valid", 0)
            if hasattr(self.env, "refresh_comb"):
                self.env.refresh_comb()

            # NonBlock TLB 下一拍一定给出响应；若 callback 已捕获则直接返回
            if captured[0] is not None:
                return self._parse_resp(captured[0])

            # fallback: 逐拍轮询（不应走到这里，但保留容错）
            remaining = int(max_cycles) - 1
            if remaining > 0:
                self.env.wait_until(
                    lambda: captured[0] is not None,
                    max_cycles=remaining,
                    timeout_message="等待 pfTLB 响应超时",
                )
                return self._parse_resp(captured[0])

            raise TimeoutError("pfTLB 响应超时：NonBlock TLB 应在下一拍返回 resp")
        finally:
            self.env.remove_after_step_callback(_capture)

    def translated_load(self, vaddr: int, *, max_cycles: int = 256) -> PftlbResp:
        """发送 read TLB 查询，等待返回解析后的响应。"""
        req = PftlbReq(
            vaddr=int(vaddr), cmd=TLB_CMD_READ,
            is_prefetch=True, memidx_is_ld=True,
        )
        return self.send_and_wait(req, max_cycles=int(max_cycles))

    def batch_translated_loads(self, vaddrs: list[int], *,
                               settle_cycles: int = 0,
                               max_cycles: int = 256) -> list[PftlbResp]:
        """逐条发送 pfTLB 查询并收集响应。"""
        responses = []
        for va in vaddrs:
            resp = self.translated_load(int(va), max_cycles=int(max_cycles))
            responses.append(resp)
        if settle_cycles > 0:
            self.env.advance_cycles(int(settle_cycles))
        return responses

    def blocking_translated_load(self, vaddr: int, *,
                                 max_cycles: int = 4096,
                                 poll_interval: int = 1) -> PftlbResp:
        """Blocking 风格 pfTLB 查询：持续拉 req_valid 直到 terminal。

        每拍 NonBlock TLB 都返回 resp（端口握手完成），
        但只有 resp.terminal=True 才表示翻译已终态化：
        - resp_miss=0 → PTW refill 完成，命中
        - any_exception → 异常终结

        调用方阻塞直到 terminal，模拟 blocking TLB 行为。
        """
        req = PftlbReq(
            vaddr=int(vaddr), cmd=TLB_CMD_READ,
            is_prefetch=True, memidx_is_ld=True,
        )
        self._drive_req(req)

        captured = [None]

        def _capture():
            if captured[0] is not None:
                return
            if self.resp.read("valid", 0):
                raw = self.resp.snapshot()
                resp = self._parse_resp(raw)
                if resp.terminal:
                    captured[0] = raw

        self.env.add_after_step_callback(_capture)
        try:
            self.req.write("valid", 1)
            if hasattr(self.env, "refresh_comb"):
                self.env.refresh_comb()
            elapsed = 0
            while elapsed < int(max_cycles):
                self.env.advance_cycles(int(poll_interval))
                elapsed += int(poll_interval)
                if captured[0] is not None:
                    break
            self.req.write("valid", 0)
            if hasattr(self.env, "refresh_comb"):
                self.env.refresh_comb()
        finally:
            self.env.remove_after_step_callback(_capture)

        if captured[0] is None:
            raise TimeoutError(
                f"blocking pfTLB 查询超时: va=0x{int(vaddr):x}, "
                f"在 {max_cycles} cycles 内未等到 hit 或 exception"
            )
        return self._parse_resp(captured[0])

    # ------------------------------------------------------------------
    # 底层 API（兼容之前接口）
    # ------------------------------------------------------------------

    def send_request(self, req: PftlbReq, *, advance_cycles: int = 1) -> dict[str, int]:
        """发送请求并返回 req snapshot。不负责等待响应。

        调用方需自行通过 wait_response() 或 try_read_response() 取响应。
        """
        self._drive_req(req)
        self.req.write("valid", 1)
        if hasattr(self.env, "refresh_comb"):
            self.env.refresh_comb()
        snapshot = self.req.snapshot()
        if advance_cycles > 0:
            self.env.advance_cycles(int(advance_cycles))
            self.req.write("valid", 0)
        if hasattr(self.env, "refresh_comb"):
            self.env.refresh_comb()
        return snapshot

    def try_read_response(self) -> PftlbResp | None:
        if self._resp_fired():
            return self._parse_resp(self.resp.snapshot())
        return None

    def wait_response(self, *, max_cycles: int = 256) -> PftlbResp:
        """等待 pfTLB 响应（供 send_request 底层流程使用）。

        用 after_step_callback 捕捉 resp_valid 脉冲，
        并结合 resp_miss/exception 判定响应有效。
        """
        captured = [None]

        def _capture():
            if captured[0] is not None:
                return
            if self._resp_fired():
                captured[0] = self.resp.snapshot()

        self.env.add_after_step_callback(_capture)
        try:
            self.env.wait_until(
                lambda: captured[0] is not None,
                max_cycles=int(max_cycles),
                timeout_message="等待 pfTLB 响应超时",
            )
        finally:
            self.env.remove_after_step_callback(_capture)

        return self._parse_resp(captured[0])

    # ------------------------------------------------------------------
    # 响应解析
    # ------------------------------------------------------------------

    def _parse_resp(self, raw: dict) -> PftlbResp:
        return PftlbResp(
            miss=bool(raw.get("bits_miss", 1)),
            paddr_0=int(raw.get("bits_paddr_0", 0)),
            paddr_1=int(raw.get("bits_paddr_1", 0)),
            gpaddr_0=int(raw.get("bits_gpaddr_0", 0)),
            gpaddr_1=int(raw.get("bits_gpaddr_1", 0)),
            pbmt_0=int(raw.get("bits_pbmt_0", 0)),
            pbmt_1=int(raw.get("bits_pbmt_1", 0)),
            fullva=int(raw.get("bits_fullva", 0)),
            fast=bool(raw.get("bits_fast", 0)),
            ptw=bool(raw.get("bits_ptw", 0)),
            excp_is=bool(raw.get("bits_excp_0_is", 0)),
            excp_pf_ld=bool(raw.get("bits_excp_0_pf_ld", 0)),
            excp_af_ld=bool(raw.get("bits_excp_0_af_ld", 0)),
            excp_gpf_ld=bool(raw.get("bits_excp_0_gpf_ld", 0)),
            excp_va=int(raw.get("bits_excp_0_va", 0)),
            memidx_is_ld=bool(raw.get("bits_memidx_is_ld", 0)),
            memidx_is_st=bool(raw.get("bits_memidx_is_st", 0)),
            memidx_idx=int(raw.get("bits_memidx_idx", 0)),
        )

    # ------------------------------------------------------------------
    # 状态 dump
    # ------------------------------------------------------------------

    def dump_state(self) -> dict:
        """dump 当前 pfTLB 请求/响应全部可读信号。"""
        state = {}
        for name in self.req.SIGNAL_MAP:
            state[f"req_{name}"] = self.req.read(name, 0)
        for name in self.resp.SIGNAL_MAP:
            state[f"resp_{name}"] = self.resp.read(name, 0)
        return state
