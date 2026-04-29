# coding=utf-8
"""
MemBlock DCache CtrlUnit facade.
"""

from dataclasses import dataclass


DCACHE_CTRL_BASE_ADDR = 0x38022000
DCACHE_CTRL_REG_WIDTH_BYTES = 8
DCACHE_CTRL_CTRL_OFFSET = 0x0
DCACHE_CTRL_DELAY_OFFSET = DCACHE_CTRL_CTRL_OFFSET + DCACHE_CTRL_REG_WIDTH_BYTES
DCACHE_CTRL_MASK_OFFSET = DCACHE_CTRL_DELAY_OFFSET + DCACHE_CTRL_REG_WIDTH_BYTES
DCACHE_CTRL_COMPONENT_TAG = 0
DCACHE_CTRL_COMPONENT_DATA = 1
HARDWARE_ERROR_BIT = 19


@dataclass(frozen=True)
class DCacheCtrlConfig:
    component: str = "tag"
    bank_mask: int = 0x1
    toggle_mask: int = 0x1
    delay: int = 0
    delay_enable: bool = False
    persist: bool = False
    enable: bool = True


class DCacheCtrlFacade:
    """L1 DCache CtrlUnit control-plane helper."""

    BASE_ADDR = DCACHE_CTRL_BASE_ADDR
    CTRL_OFFSET = DCACHE_CTRL_CTRL_OFFSET
    DELAY_OFFSET = DCACHE_CTRL_DELAY_OFFSET
    MASK_OFFSET = DCACHE_CTRL_MASK_OFFSET
    REG_WIDTH_BYTES = DCACHE_CTRL_REG_WIDTH_BYTES

    def __init__(self, env) -> None:
        self.env = env

    def ctrl_addr(self) -> int:
        return self.BASE_ADDR + self.CTRL_OFFSET

    def delay_addr(self) -> int:
        return self.BASE_ADDR + self.DELAY_OFFSET

    def mask_addr(self, bank_index: int = 0) -> int:
        if int(bank_index) < 0:
            raise ValueError(f"bank_index must be non-negative, got {bank_index}")
        return self.BASE_ADDR + self.MASK_OFFSET + int(bank_index) * self.REG_WIDTH_BYTES

    def component_code(self, component: str | int) -> int:
        if isinstance(component, str):
            normalized = component.strip().lower()
            if normalized == "tag":
                return DCACHE_CTRL_COMPONENT_TAG
            if normalized == "data":
                return DCACHE_CTRL_COMPONENT_DATA
            raise ValueError(f"unsupported dcache ctrl component: {component}")
        code = int(component)
        if code not in (DCACHE_CTRL_COMPONENT_TAG, DCACHE_CTRL_COMPONENT_DATA):
            raise ValueError(f"unsupported dcache ctrl component code: {component}")
        return code

    def encode_control(
        self,
        *,
        component: str | int,
        bank_mask: int,
        enable: bool = True,
        persist: bool = False,
        delay_enable: bool = False,
    ) -> int:
        component_code = self.component_code(component)
        return (
            (int(bank_mask) << 4)
            | (component_code << 3)
            | (int(bool(delay_enable)) << 2)
            | (int(bool(persist)) << 1)
            | int(bool(enable))
        )

    def encode_config(self, config: DCacheCtrlConfig) -> dict[str, int]:
        return {
            "ctrl": self.encode_control(
                component=config.component,
                bank_mask=config.bank_mask,
                enable=config.enable,
                persist=config.persist,
                delay_enable=config.delay_enable,
            ),
            "delay": int(config.delay),
            "mask": int(config.toggle_mask),
        }

    def decode_control(self, value: int) -> dict[str, int | bool | str]:
        raw = int(value)
        component_code = (raw >> 3) & 0x1
        return {
            "raw": raw,
            "enable": bool(raw & 0x1),
            "persist": bool((raw >> 1) & 0x1),
            "delay_enable": bool((raw >> 2) & 0x1),
            "component_code": component_code,
            "component": "data" if component_code == DCACHE_CTRL_COMPONENT_DATA else "tag",
            "bank_mask": raw >> 4,
        }

    def sample_error_state(self) -> dict[str, int]:
        return {
            "cycle": self.env._current_cycle(),
            "dcache_error_valid": self.env._read_optional_dut_signal("io_dcacheError_ecc_error_valid", 0),
            "dcache_error_paddr": self.env._read_optional_dut_signal("io_dcacheError_ecc_error_bits", 0),
            "backend_nmi_31": self.env._read_optional_dut_signal(
                "io_mem_to_ooo_topToBackendBypass_externalInterrupt_nmi_nmi_31",
                0,
            ),
            "backend_nmi_43": self.env._read_optional_dut_signal(
                "io_mem_to_ooo_topToBackendBypass_externalInterrupt_nmi_nmi_43",
                0,
            ),
            "outer_cpu_critical_error": self.env._read_optional_dut_signal("io_outer_cpu_critical_error", 0),
        }

    def _read_internal_reg(self, *candidates: str) -> int | None:
        for name in candidates:
            value = self.env._read_optional_internal_signal(name, default=None)
            if value is not None:
                return int(value)
        return None

    def sample_internal_state(self) -> dict[str, int | None]:
        return {
            "ctrl": self._read_internal_reg(
                "MemBlock_top.MemBlock.inner_dcache.dcache.cacheCtrlOpt.ctrlRegs_0",
                "cacheCtrlOpt.ctrlRegs_0",
                "inner_dcache.dcache.cacheCtrlOpt.ctrlRegs_0",
                "inner_dcache.cacheCtrlOpt.ctrlRegs_0",
            ),
            "delay": self._read_internal_reg(
                "MemBlock_top.MemBlock.inner_dcache.dcache.cacheCtrlOpt.delayRegs_0",
                "cacheCtrlOpt.delayRegs_0",
                "inner_dcache.dcache.cacheCtrlOpt.delayRegs_0",
                "inner_dcache.cacheCtrlOpt.delayRegs_0",
            ),
            "counter": self._read_internal_reg(
                "MemBlock_top.MemBlock.inner_dcache.dcache.cacheCtrlOpt.counterRegs_0",
                "inner_dcache.dcache.cacheCtrlOpt.counterRegs_0",
                "inner_dcache.cacheCtrlOpt.counterRegs_0",
            ),
            "mask0": self._read_internal_reg(
                "MemBlock_top.MemBlock.inner_dcache.dcache.cacheCtrlOpt.maskRegs_0",
                "cacheCtrlOpt.maskRegs_0",
                "inner_dcache.dcache.cacheCtrlOpt.maskRegs_0",
                "inner_dcache.cacheCtrlOpt.maskRegs_0",
            ),
        }

    def sample_fault_debug(self) -> dict[str, int | None]:
        return {
            "ctrl": self._read_internal_reg(
                "MemBlock_top.MemBlock.inner_dcache.dcache.cacheCtrlOpt.ctrlRegs_0",
            ),
            "counter": self._read_internal_reg(
                "MemBlock_top.MemBlock.inner_dcache.dcache.cacheCtrlOpt.counterRegs_0",
            ),
            "tag_pipe_valid": self._read_internal_reg(
                "MemBlock_top.MemBlock.inner_dcache.dcache.cacheCtrlOpt.CtrlUnitPseudoErrorPipelineConnect0.valid",
            ),
            "tag_pipe_fire": self._read_internal_reg(
                "MemBlock_top.MemBlock.inner_dcache.dcache.cacheCtrlOpt.CtrlUnitPseudoErrorPipelineConnect0.io_rightOutFire",
            ),
            "data_pipe_valid": self._read_internal_reg(
                "MemBlock_top.MemBlock.inner_dcache.dcache.cacheCtrlOpt.CtrlUnitPseudoErrorPipelineConnect1.valid",
            ),
            "data_pipe_fire": self._read_internal_reg(
                "MemBlock_top.MemBlock.inner_dcache.dcache.cacheCtrlOpt.CtrlUnitPseudoErrorPipelineConnect1.io_rightOutFire",
            ),
            "load0_troublemaker": self._read_internal_reg(
                "MemBlock_top.MemBlock.inner_LoadUnit_0.s2.troubleMaker",
            ),
            "load1_troublemaker": self._read_internal_reg(
                "MemBlock_top.MemBlock.inner_LoadUnit_1.s2.troubleMaker",
            ),
            "load2_troublemaker": self._read_internal_reg(
                "MemBlock_top.MemBlock.inner_LoadUnit_2.s2.troubleMaker",
            ),
            "data_read_error_delayed_0": self._read_internal_reg(
                "MemBlock_top.MemBlock.inner_dcache.dcache.bankedDataArray.io_read_error_delayed_0_0",
            ),
            "data_read_error_delayed_1": self._read_internal_reg(
                "MemBlock_top.MemBlock.inner_dcache.dcache.bankedDataArray.io_read_error_delayed_1_0",
            ),
            "data_read_error_delayed_2": self._read_internal_reg(
                "MemBlock_top.MemBlock.inner_dcache.dcache.bankedDataArray.io_read_error_delayed_2_0",
            ),
            "dcache_error_valid_pipe0": self._read_internal_reg(
                "MemBlock_top.MemBlock.inner_dcacheError_pipMod.valid_REG",
            ),
            "dcache_error_valid_pipe1": self._read_internal_reg(
                "MemBlock_top.MemBlock.inner_dcacheError_pipMod.valid_REG_1",
            ),
        }

    def wait_counter_zero(self, *, max_cycles: int = 200) -> dict[str, int | None]:
        def _counter_zero():
            state = self.sample_internal_state()
            if state["counter"] != 0:
                return None
            return state

        return self.env.wait_until(
            _counter_zero,
            max_cycles=max_cycles,
            timeout_message="等待 dcache ctrl counter 归零超时",
        )

    def wait_error_event(self, *, expected_paddr: int | None = None, max_cycles: int = 200) -> dict[str, int]:
        def _error_seen():
            state = self.sample_error_state()
            if state["dcache_error_valid"] == 0:
                return None
            if expected_paddr is not None and state["dcache_error_paddr"] != int(expected_paddr):
                return None
            return state

        detail = "" if expected_paddr is None else f", expected_paddr=0x{int(expected_paddr):x}"
        return self.env.wait_until(
            _error_seen,
            max_cycles=max_cycles,
            timeout_message=f"等待 dcache ctrl error event 超时{detail}",
        )

    def wait_backend_nmi31_asserted(self, *, max_cycles: int = 200) -> dict[str, int]:
        return self.env.wait_until(
            lambda: self.sample_error_state() if self.sample_error_state()["backend_nmi_31"] else None,
            max_cycles=max_cycles,
            timeout_message="等待 backend nmi_31 拉高超时",
        )
