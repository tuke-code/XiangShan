# coding=utf-8
"""
Issue active agent.
"""


LSU_OP_LD = 0x3
LSU_OP_SD = 0x3


def _set_optional_signal(dut, signal_name: str, value: int) -> None:
    signal = getattr(dut, signal_name, None)
    if signal is not None:
        signal.value = value


class IssueAgent:
    """负责 issue lane 握手与驱动。"""

    def __init__(self, env) -> None:
        self.env = env

    def _issue_until_fire(self, lane: int, drive_inputs, max_cycles: int = 50) -> None:
        issue = self.env.issue[lane]

        for _ in range(max_cycles):
            if int(self.env.dut.io_reset_backend.value):
                raise RuntimeError(f"等待 `issue[{lane}]` 握手时 backend 进入 reset")
            drive_inputs()
            if int(issue.ready.value):
                self.env.Step(1)
                self.env.idle_inputs()
                return
            self.env.Step(1)

        self.env.idle_inputs()
        raise TimeoutError(f"等待 `issue[{lane}]` 完成握手超时")

    def issue_scalar_load(
        self,
        req_id: int,
        addr: int,
        lq_ptr,
        sq_ptr,
        lane: int = 0,
        store_set_hit: int = 0,
        load_wait_bit: int = 0,
        load_wait_strict: int = 0,
        wait_for_rob_idx_flag: int | None = None,
        wait_for_rob_idx_value: int | None = None,
    ) -> None:
        def _drive() -> None:
            issue = self.env.issue[lane]
            prefix = f"io_ooo_to_mem_intIssue_{lane}_0_bits_"
            issue.valid.value = 1
            issue.bits_fuOpType.value = LSU_OP_LD
            issue.bits_src_0.value = addr
            issue.bits_robIdx_flag.value = (req_id >> 9) & 0x1
            issue.bits_robIdx_value.value = req_id & 0x1FF
            issue.bits_sqIdx_flag.value = sq_ptr.flag
            issue.bits_sqIdx_value.value = sq_ptr.value

            _set_optional_signal(self.env.dut, f"{prefix}imm", 0)
            _set_optional_signal(self.env.dut, f"{prefix}pdest", req_id % 64)
            _set_optional_signal(self.env.dut, f"{prefix}rfWen", 1)
            _set_optional_signal(self.env.dut, f"{prefix}pc", 0x80000000 + req_id * 4)
            _set_optional_signal(self.env.dut, f"{prefix}ftqIdx_flag", 0)
            _set_optional_signal(self.env.dut, f"{prefix}ftqIdx_value", req_id & 0x3F)
            _set_optional_signal(self.env.dut, f"{prefix}ftqOffset", 0)
            _set_optional_signal(self.env.dut, f"{prefix}loadWaitBit", int(load_wait_bit))
            _set_optional_signal(
                self.env.dut,
                f"{prefix}waitForRobIdx_flag",
                0 if wait_for_rob_idx_flag is None else int(wait_for_rob_idx_flag),
            )
            _set_optional_signal(
                self.env.dut,
                f"{prefix}waitForRobIdx_value",
                0 if wait_for_rob_idx_value is None else int(wait_for_rob_idx_value),
            )
            _set_optional_signal(self.env.dut, f"{prefix}storeSetHit", int(store_set_hit))
            _set_optional_signal(self.env.dut, f"{prefix}loadWaitStrict", int(load_wait_strict))
            _set_optional_signal(self.env.dut, f"{prefix}ssid", 0)
            _set_optional_signal(self.env.dut, f"{prefix}lqIdx_flag", lq_ptr.flag)
            _set_optional_signal(self.env.dut, f"{prefix}lqIdx_value", lq_ptr.value)

        self._issue_until_fire(lane, _drive)
        self.env.backend.note_load_issued((req_id >> 9) & 0x1, req_id & 0x1FF)

    def issue_scalar_std(self, req_id: int, sq_ptr, data: int, lane: int = 5) -> None:
        def _drive() -> None:
            issue = self.env.issue[lane]
            issue.valid.value = 1
            issue.bits_fuOpType.value = LSU_OP_SD
            issue.bits_src_0.value = data
            issue.bits_robIdx_flag.value = (req_id >> 9) & 0x1
            issue.bits_robIdx_value.value = req_id & 0x1FF
            issue.bits_sqIdx_flag.value = sq_ptr.flag
            issue.bits_sqIdx_value.value = sq_ptr.value

        self._issue_until_fire(lane, _drive)

    def issue_scalar_sta(self, req_id: int, sq_ptr, addr: int, lane: int = 3) -> None:
        def _drive() -> None:
            issue = self.env.issue[lane]
            prefix = f"io_ooo_to_mem_intIssue_{lane}_0_bits_"
            issue.valid.value = 1
            issue.bits_fuOpType.value = LSU_OP_SD
            issue.bits_src_0.value = addr
            issue.bits_robIdx_flag.value = (req_id >> 9) & 0x1
            issue.bits_robIdx_value.value = req_id & 0x1FF
            issue.bits_sqIdx_flag.value = sq_ptr.flag
            issue.bits_sqIdx_value.value = sq_ptr.value

            _set_optional_signal(self.env.dut, f"{prefix}imm", 0)
            _set_optional_signal(self.env.dut, f"{prefix}isFirstIssue", 1)
            _set_optional_signal(self.env.dut, f"{prefix}pdest", 0)
            _set_optional_signal(self.env.dut, f"{prefix}rfWen", 0)
            _set_optional_signal(self.env.dut, f"{prefix}isRVC", 0)
            _set_optional_signal(self.env.dut, f"{prefix}ftqIdx_flag", 0)
            _set_optional_signal(self.env.dut, f"{prefix}ftqIdx_value", req_id & 0x3F)
            _set_optional_signal(self.env.dut, f"{prefix}ftqOffset", 0)
            _set_optional_signal(self.env.dut, f"{prefix}storeSetHit", 0)
            _set_optional_signal(self.env.dut, f"{prefix}ssid", 0)

        self._issue_until_fire(lane, _drive)
