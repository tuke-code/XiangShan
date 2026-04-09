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

    def _drive_scalar_load(
        self,
        *,
        req_id: int,
        addr: int,
        lq_ptr,
        sq_ptr,
        lane: int,
        store_set_hit: int = 0,
        load_wait_bit: int = 0,
        load_wait_strict: int = 0,
        wait_for_rob_idx_flag: int | None = None,
        wait_for_rob_idx_value: int | None = None,
    ) -> None:
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

    def _drive_scalar_sta(
        self,
        *,
        req_id: int,
        sq_ptr,
        addr: int,
        lane: int,
    ) -> None:
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

    async def _issue_until_fire_async(self, lane: int, drive_inputs, max_cycles: int = 50) -> None:
        issue = self.env.issue[lane]

        for _ in range(max_cycles):
            if int(self.env.dut.io_reset_backend.value):
                raise RuntimeError(f"等待 `issue[{lane}]` 握手时 backend 进入 reset")
            drive_inputs()
            if int(issue.ready.value):
                await self.env._step_async(1)
                self.env.idle_inputs()
                return
            await self.env._step_async(1)

        self.env.idle_inputs()
        raise TimeoutError(f"等待 `issue[{lane}]` 完成握手超时")

    async def _issue_scalar_load_batch_same_cycle_async(self, txns, max_cycles: int = 50) -> None:
        lanes = [int(txn.issue_lane) for txn in txns]
        if len(set(lanes)) != len(lanes):
            raise ValueError(f"同拍 load issue 需要 lane 唯一: lanes={lanes}")

        for _ in range(max_cycles):
            if int(self.env.dut.io_reset_backend.value):
                raise RuntimeError("等待同拍 load issue 握手时 backend 进入 reset")

            if all(int(self.env.issue[lane].ready.value) for lane in lanes):
                for txn in txns:
                    self._drive_scalar_load(
                        req_id=txn.req_id,
                        addr=txn.addr,
                        lq_ptr=txn.lq_ptr,
                        sq_ptr=txn.sq_ptr,
                        lane=int(txn.issue_lane),
                        store_set_hit=txn.store_set_hit,
                        load_wait_bit=txn.load_wait_bit,
                        load_wait_strict=txn.load_wait_strict,
                        wait_for_rob_idx_flag=txn.wait_for_rob_idx_flag,
                        wait_for_rob_idx_value=txn.wait_for_rob_idx_value,
                    )
                await self.env._step_async(1)
                self.env.idle_inputs()
                return

            await self.env._step_async(1)

        self.env.idle_inputs()
        raise TimeoutError(f"等待同拍 load issue 完成握手超时: lanes={lanes}")

    async def _issue_scalar_load_batch_with_sta_same_cycle_async(
        self,
        txns,
        *,
        sta_req_id: int,
        sta_sq_ptr,
        sta_addr: int,
        sta_lane: int,
        max_cycles: int = 50,
    ) -> None:
        load_lanes = [int(txn.issue_lane) for txn in txns]
        if len(set(load_lanes)) != len(load_lanes):
            raise ValueError(f"同拍 load issue 需要 lane 唯一: lanes={load_lanes}")
        if int(sta_lane) in load_lanes:
            raise ValueError(f"STA lane 与 load lanes 冲突: sta_lane={sta_lane}, load_lanes={load_lanes}")

        target_lanes = tuple(load_lanes) + (int(sta_lane),)
        for _ in range(max_cycles):
            if int(self.env.dut.io_reset_backend.value):
                raise RuntimeError("等待同拍 load+STA issue 握手时 backend 进入 reset")

            if all(int(self.env.issue[lane].ready.value) for lane in target_lanes):
                for txn in txns:
                    self._drive_scalar_load(
                        req_id=txn.req_id,
                        addr=txn.addr,
                        lq_ptr=txn.lq_ptr,
                        sq_ptr=txn.sq_ptr,
                        lane=int(txn.issue_lane),
                        store_set_hit=txn.store_set_hit,
                        load_wait_bit=txn.load_wait_bit,
                        load_wait_strict=txn.load_wait_strict,
                        wait_for_rob_idx_flag=txn.wait_for_rob_idx_flag,
                        wait_for_rob_idx_value=txn.wait_for_rob_idx_value,
                    )
                self._drive_scalar_sta(
                    req_id=sta_req_id,
                    sq_ptr=sta_sq_ptr,
                    addr=sta_addr,
                    lane=int(sta_lane),
                )
                await self.env._step_async(1)
                self.env.idle_inputs()
                return

            await self.env._step_async(1)

        self.env.idle_inputs()
        raise TimeoutError(
            "等待同拍 load+STA issue 完成握手超时: "
            f"load_lanes={load_lanes}, sta_lane={sta_lane}"
        )

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
            self._drive_scalar_load(
                req_id=req_id,
                addr=addr,
                lq_ptr=lq_ptr,
                sq_ptr=sq_ptr,
                lane=lane,
                store_set_hit=store_set_hit,
                load_wait_bit=load_wait_bit,
                load_wait_strict=load_wait_strict,
                wait_for_rob_idx_flag=wait_for_rob_idx_flag,
                wait_for_rob_idx_value=wait_for_rob_idx_value,
            )

        self.env._run_async(self._issue_until_fire_async(lane, _drive))
        self.env.backend.note_load_issued((req_id >> 9) & 0x1, req_id & 0x1FF)

    def issue_scalar_load_batch_same_cycle(self, txns, max_cycles: int = 50) -> None:
        self.env._run_async(self._issue_scalar_load_batch_same_cycle_async(txns, max_cycles=max_cycles))
        for txn in txns:
            self.env.backend.note_load_issued((txn.req_id >> 9) & 0x1, txn.req_id & 0x1FF)

    def issue_scalar_load_batch_with_sta_same_cycle(
        self,
        txns,
        *,
        sta_req_id: int,
        sta_sq_ptr,
        sta_addr: int,
        sta_lane: int = 3,
        max_cycles: int = 50,
    ) -> None:
        self.env._run_async(
            self._issue_scalar_load_batch_with_sta_same_cycle_async(
                txns,
                sta_req_id=sta_req_id,
                sta_sq_ptr=sta_sq_ptr,
                sta_addr=sta_addr,
                sta_lane=sta_lane,
                max_cycles=max_cycles,
            )
        )
        for txn in txns:
            self.env.backend.note_load_issued((txn.req_id >> 9) & 0x1, txn.req_id & 0x1FF)

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

        self.env._run_async(self._issue_until_fire_async(lane, _drive))

    def issue_scalar_sta(self, req_id: int, sq_ptr, addr: int, lane: int = 3) -> None:
        def _drive() -> None:
            self._drive_scalar_sta(
                req_id=req_id,
                sq_ptr=sq_ptr,
                addr=addr,
                lane=lane,
            )

        self.env._run_async(self._issue_until_fire_async(lane, _drive))
