# coding=utf-8
"""
MemBlock frontend-facing 最小 facade。

本文件收口：
  1. `frontendBridge` 三条 TileLink 通路的最小 A/D 驱动与观测 helper
  2. `fetch_to_mem.itlb` 顶层请求/响应 helper
"""


class _OptionalSignalBundle:
    """为可裁剪导出的信号提供统一 `connected/read/write` 接口。"""

    SIGNAL_MAP = {}

    def __init__(self, prefix: str, dut) -> None:
        self._prefix = prefix
        self._dut = dut
        for attr_name, suffix in self.SIGNAL_MAP.items():
            setattr(self, attr_name, getattr(dut, f"{prefix}{suffix}", None))

    def connected(self, name: str) -> bool:
        return getattr(self, name, None) is not None

    def read(self, name: str, default=None):
        signal = getattr(self, name, None)
        if signal is None:
            return default
        return int(signal.value)

    def write(self, name: str, value: int) -> None:
        signal = getattr(self, name, None)
        if signal is not None:
            signal.value = int(value)

    def snapshot(self) -> dict[str, int]:
        return {name: self.read(name, 0) for name in self.SIGNAL_MAP if self.connected(name)}


class GenericTLABundle(_OptionalSignalBundle):
    """最小 TileLink A 通道观测/驱动封装，允许字段按 DUT 实际导出裁剪。"""

    SIGNAL_MAP = {
        "ready": "ready",
        "valid": "valid",
        "bits_opcode": "bits_opcode",
        "bits_param": "bits_param",
        "bits_size": "bits_size",
        "bits_source": "bits_source",
        "bits_address": "bits_address",
        "bits_mask": "bits_mask",
        "bits_data": "bits_data",
        "bits_corrupt": "bits_corrupt",
        "bits_user_alias": "bits_user_alias",
        "bits_user_memBackType_MM": "bits_user_memBackType_MM",
        "bits_user_memPageType_NC": "bits_user_memPageType_NC",
        "bits_user_reqSource": "bits_user_reqSource",
    }

    def set_ready(self, value: int = 1) -> None:
        if self.ready is not None:
            self.ready.value = int(value)

    def drive_idle(self) -> None:
        for name in self.SIGNAL_MAP:
            if name == "ready":
                continue
            self.write(name, 0)


class GenericTLDBundle(_OptionalSignalBundle):
    """最小 TileLink D 通道观测/驱动封装，允许字段按 DUT 实际导出裁剪。"""

    SIGNAL_MAP = {
        "ready": "ready",
        "valid": "valid",
        "bits_opcode": "bits_opcode",
        "bits_param": "bits_param",
        "bits_size": "bits_size",
        "bits_source": "bits_source",
        "bits_sink": "bits_sink",
        "bits_denied": "bits_denied",
        "bits_data": "bits_data",
        "bits_corrupt": "bits_corrupt",
    }

    def set_ready(self, value: int = 1) -> None:
        if self.ready is not None:
            self.ready.value = int(value)

    def drive_idle(self) -> None:
        for name in self.SIGNAL_MAP:
            if name == "ready":
                continue
            self.write(name, 0)


class FetchToMemITlbReqBundle(_OptionalSignalBundle):
    SIGNAL_MAP = {
        "ready": "ready",
        "valid": "valid",
        "bits_vpn": "bits_vpn",
        "bits_s2xlate": "bits_s2xlate",
    }

    def drive_idle(self) -> None:
        self.write("valid", 0)
        self.write("bits_vpn", 0)
        self.write("bits_s2xlate", 0)


class FetchToMemITlbRespBundle(_OptionalSignalBundle):
    SIGNAL_MAP = {
        "ready": "ready",
        "valid": "valid",
        "bits_s2xlate": "bits_s2xlate",
        "bits_s1_entry_tag": "bits_s1_entry_tag",
        "bits_s1_entry_asid": "bits_s1_entry_asid",
        "bits_s1_entry_vmid": "bits_s1_entry_vmid",
        "bits_s1_entry_n": "bits_s1_entry_n",
        "bits_s1_entry_pbmt": "bits_s1_entry_pbmt",
        "bits_s1_entry_perm_r": "bits_s1_entry_perm_r",
        "bits_s1_entry_perm_w": "bits_s1_entry_perm_w",
        "bits_s1_entry_perm_x": "bits_s1_entry_perm_x",
        "bits_s1_entry_perm_u": "bits_s1_entry_perm_u",
        "bits_s1_entry_perm_g": "bits_s1_entry_perm_g",
        "bits_s1_entry_perm_a": "bits_s1_entry_perm_a",
        "bits_s1_entry_perm_d": "bits_s1_entry_perm_d",
        "bits_s1_entry_level": "bits_s1_entry_level",
        "bits_s1_entry_prefetch": "bits_s1_entry_prefetch",
        "bits_s1_entry_v": "bits_s1_entry_v",
        "bits_s1_entry_ppn": "bits_s1_entry_ppn",
        "bits_s1_addr_low": "bits_s1_addr_low",
        "bits_s1_pf": "bits_s1_pf",
        "bits_s1_af": "bits_s1_af",
        "bits_s2_gpf": "bits_s2_gpf",
        "bits_s2_gaf": "bits_s2_gaf",
    }

    def set_ready(self, value: int = 1) -> None:
        if self.ready is not None:
            self.ready.value = int(value)


class FrontendBridgePath:
    """一条 frontendBridge TL 通路的最小测试 facade。"""

    def __init__(self, env, *, name: str, frontend_a, frontend_d, mem_a, mem_d) -> None:
        self.env = env
        self.name = name
        self.frontend_a = frontend_a
        self.frontend_d = frontend_d
        self.mem_a = mem_a
        self.mem_d = mem_d

    def drive_idle(self) -> None:
        self.frontend_a.drive_idle()
        self.frontend_d.set_ready(0)
        self.mem_a.set_ready(0)
        self.mem_d.drive_idle()

    def drive_frontend_a(self, **fields: int) -> None:
        for name, value in fields.items():
            self.frontend_a.write(name, value)

    def clear_frontend_a(self) -> None:
        self.frontend_a.drive_idle()

    def set_mem_a_ready(self, value: int = 1) -> None:
        self.mem_a.set_ready(value)

    def set_frontend_d_ready(self, value: int = 1) -> None:
        self.frontend_d.set_ready(value)

    def drive_mem_d(self, **fields: int) -> None:
        for name, value in fields.items():
            self.mem_d.write(name, value)

    def clear_mem_d(self) -> None:
        self.mem_d.drive_idle()

    def wait_mem_a_fire(self, *, max_cycles: int = 64) -> dict[str, int]:
        return self.env.wait_until(
            lambda: self.mem_a.snapshot()
            if self.mem_a.read("valid", 0) and self.mem_a.read("ready", 0)
            else False,
            max_cycles=max_cycles,
            timeout_message=f"等待 frontendBridge `{self.name}` mem-side A 握手超时",
        )

    def wait_frontend_d_fire(self, *, max_cycles: int = 64) -> dict[str, int]:
        return self.env.wait_until(
            lambda: self.frontend_d.snapshot()
            if self.frontend_d.read("valid", 0) and self.frontend_d.read("ready", 0)
            else False,
            max_cycles=max_cycles,
            timeout_message=f"等待 frontendBridge `{self.name}` frontend-side D 握手超时",
        )


class FrontendBridgeFacade:
    """统一收口 `frontendBridge` 的三条 TL 通道。"""

    def __init__(self, env) -> None:
        self.env = env
        self.icache = FrontendBridgePath(
            env,
            name="icache",
            frontend_a=GenericTLABundle("auto_inner_frontendBridge_icache_in_a_", env.dut),
            frontend_d=GenericTLDBundle("auto_inner_frontendBridge_icache_in_d_", env.dut),
            mem_a=GenericTLABundle("auto_inner_frontendBridge_icache_out_a_", env.dut),
            mem_d=GenericTLDBundle("auto_inner_frontendBridge_icache_out_d_", env.dut),
        )
        self.instr_uncache = FrontendBridgePath(
            env,
            name="instr_uncache",
            frontend_a=GenericTLABundle("auto_inner_frontendBridge_instr_uncache_in_a_", env.dut),
            frontend_d=GenericTLDBundle("auto_inner_frontendBridge_instr_uncache_in_d_", env.dut),
            mem_a=GenericTLABundle("auto_inner_frontendBridge_instr_uncache_out_a_", env.dut),
            mem_d=GenericTLDBundle("auto_inner_frontendBridge_instr_uncache_out_d_", env.dut),
        )
        self.icachectrl = FrontendBridgePath(
            env,
            name="icachectrl",
            frontend_a=GenericTLABundle("auto_inner_frontendBridge_icachectrl_in_a_", env.dut),
            frontend_d=GenericTLDBundle("auto_inner_frontendBridge_icachectrl_in_d_", env.dut),
            mem_a=GenericTLABundle("auto_inner_frontendBridge_icachectrl_out_a_", env.dut),
            mem_d=GenericTLDBundle("auto_inner_frontendBridge_icachectrl_out_d_", env.dut),
        )

    def drive_idle(self) -> None:
        self.icache.drive_idle()
        self.instr_uncache.drive_idle()
        self.icachectrl.drive_idle()


class FetchToMemFacade:
    """统一收口 `fetch_to_mem.itlb` 顶层测试入口。"""

    def __init__(self, env) -> None:
        self.env = env
        self.itlb_req = FetchToMemITlbReqBundle("io_fetch_to_mem_itlb_req_0_", env.dut)
        self.itlb_resp = FetchToMemITlbRespBundle("io_fetch_to_mem_itlb_resp_", env.dut)

    def drive_idle(self) -> None:
        self.itlb_req.drive_idle()
        self.itlb_resp.set_ready(0)

    def drive_itlb_request(self, *, vpn: int, s2xlate: int = 0, valid: int = 1) -> None:
        self.itlb_req.write("bits_vpn", vpn)
        self.itlb_req.write("bits_s2xlate", s2xlate)
        self.itlb_req.write("valid", valid)

    def clear_itlb_request(self) -> None:
        self.itlb_req.drive_idle()

    def set_resp_ready(self, value: int = 1) -> None:
        self.itlb_resp.set_ready(value)

    def wait_req_fire(self, *, max_cycles: int = 64) -> dict[str, int]:
        return self.env.wait_until(
            lambda: self.itlb_req.snapshot()
            if self.itlb_req.read("valid", 0) and self.itlb_req.read("ready", 0)
            else False,
            max_cycles=max_cycles,
            timeout_message="等待 fetch_to_mem.itlb 请求握手超时",
        )

    def wait_resp_fire(self, *, max_cycles: int = 256) -> dict[str, int]:
        return self.env.wait_until(
            lambda: self.itlb_resp.snapshot()
            if self.itlb_resp.read("valid", 0) and self.itlb_resp.read("ready", 0)
            else False,
            max_cycles=max_cycles,
            timeout_message="等待 fetch_to_mem.itlb 响应握手超时",
        )
