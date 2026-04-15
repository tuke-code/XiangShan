# coding=utf-8
"""
Issue active agent.
"""

from transactions import IssueCyclePlan, IssueOp, scalar_store_fu_op_type_from_mask


LSU_OP_LD = 0x3


def _set_optional_signal(dut, signal_name: str, value: int) -> None:
    signal = getattr(dut, signal_name, None)
    if signal is not None:
        signal.value = value


class IssueAgent:
    """负责 issue lane 握手与驱动。"""

    def __init__(self, env) -> None:
        self.env = env

    def _drive_scalar_std(
        self,
        *,
        op: IssueOp,
        sq_ptr,
        data: int,
        lane: int,
        mask: int = 0xFF,
    ) -> None:
        issue = self.env.issue[lane]
        issue.valid.value = 1
        issue.bits_fuOpType.value = scalar_store_fu_op_type_from_mask(mask)
        issue.bits_src_0.value = data
        issue.bits_robIdx_flag.value = op.resolved_rob_idx_flag
        issue.bits_robIdx_value.value = op.resolved_rob_idx_value
        issue.bits_sqIdx_flag.value = sq_ptr.flag
        issue.bits_sqIdx_value.value = sq_ptr.value

    def _drive_issue_op(self, op: IssueOp) -> None:
        if op.kind == "load":
            self._drive_scalar_load(
                op=op,
                addr=op.addr,
                lq_ptr=op.lq_ptr,
                sq_ptr=op.sq_ptr,
                lane=int(op.lane),
                store_set_hit=op.store_set_hit,
                load_wait_bit=op.load_wait_bit,
                load_wait_strict=op.load_wait_strict,
                wait_for_rob_idx_flag=op.wait_for_rob_idx_flag,
                wait_for_rob_idx_value=op.wait_for_rob_idx_value,
            )
            return
        if op.kind == "sta":
            self._drive_scalar_sta(
                op=op,
                sq_ptr=op.sq_ptr,
                addr=op.addr,
                lane=int(op.lane),
                mask=op.mask,
            )
            return
        if op.kind == "std":
            self._drive_scalar_std(
                op=op,
                sq_ptr=op.sq_ptr,
                data=op.data,
                lane=int(op.lane),
                mask=op.mask,
            )
            return
        raise ValueError(f"unsupported issue op kind: {op.kind}")

    def _drive_scalar_load(
        self,
        *,
        op: IssueOp,
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
        issue.bits_robIdx_flag.value = op.resolved_rob_idx_flag
        issue.bits_robIdx_value.value = op.resolved_rob_idx_value
        issue.bits_sqIdx_flag.value = sq_ptr.flag
        issue.bits_sqIdx_value.value = sq_ptr.value

        _set_optional_signal(self.env.dut, f"{prefix}imm", 0)
        _set_optional_signal(self.env.dut, f"{prefix}pdest", op.resolved_pdest)
        _set_optional_signal(self.env.dut, f"{prefix}rfWen", 1)
        _set_optional_signal(self.env.dut, f"{prefix}pc", op.resolved_pc)
        _set_optional_signal(self.env.dut, f"{prefix}ftqIdx_flag", op.resolved_ftq_idx_flag)
        _set_optional_signal(self.env.dut, f"{prefix}ftqIdx_value", op.resolved_ftq_idx_value)
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
        op: IssueOp,
        sq_ptr,
        addr: int,
        lane: int,
        mask: int = 0xFF,
    ) -> None:
        issue = self.env.issue[lane]
        prefix = f"io_ooo_to_mem_intIssue_{lane}_0_bits_"
        issue.valid.value = 1
        issue.bits_fuOpType.value = scalar_store_fu_op_type_from_mask(mask)
        issue.bits_src_0.value = addr
        issue.bits_robIdx_flag.value = op.resolved_rob_idx_flag
        issue.bits_robIdx_value.value = op.resolved_rob_idx_value
        issue.bits_sqIdx_flag.value = sq_ptr.flag
        issue.bits_sqIdx_value.value = sq_ptr.value

        _set_optional_signal(self.env.dut, f"{prefix}imm", 0)
        _set_optional_signal(self.env.dut, f"{prefix}isFirstIssue", 1)
        _set_optional_signal(self.env.dut, f"{prefix}pdest", 0)
        _set_optional_signal(self.env.dut, f"{prefix}rfWen", 0)
        _set_optional_signal(self.env.dut, f"{prefix}isRVC", 0)
        _set_optional_signal(self.env.dut, f"{prefix}ftqIdx_flag", op.resolved_ftq_idx_flag)
        _set_optional_signal(self.env.dut, f"{prefix}ftqIdx_value", op.resolved_ftq_idx_value)
        _set_optional_signal(self.env.dut, f"{prefix}ftqOffset", 0)
        _set_optional_signal(self.env.dut, f"{prefix}storeSetHit", 0)
        _set_optional_signal(self.env.dut, f"{prefix}ssid", 0)

    async def _issue_cycle_async(self, plan: IssueCyclePlan) -> None:
        lanes = [int(op.lane) for op in plan.ops]
        if len(set(lanes)) != len(lanes):
            raise ValueError(f"同拍 issue 需要 lane 唯一: lanes={lanes}")

        for _ in range(plan.max_cycles):
            if int(self.env.dut.io_reset_backend.value):
                raise RuntimeError(f"等待同拍 issue 握手时 backend 进入 reset: lanes={lanes}")

            if all(int(self.env.issue[lane].ready.value) for lane in lanes):
                for op in plan.ops:
                    self._drive_issue_op(op)
                await self.env._step_async(1)
                self.env.idle_inputs()
                return

            await self.env._step_async(1)

        self.env.idle_inputs()
        raise TimeoutError(f"等待同拍 issue 完成握手超时: lanes={lanes}")

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
        rob_idx_flag: int | None = None,
        rob_idx_value: int | None = None,
        pdest: int | None = None,
        ftq_idx_flag: int | None = None,
        ftq_idx_value: int | None = None,
        pc: int | None = None,
    ) -> None:
        self.issue_cycle(
            IssueCyclePlan.from_ops(
                IssueOp(
                    kind="load",
                    req_id=req_id,
                    lane=lane,
                    sq_ptr=sq_ptr,
                    addr=addr,
                    lq_ptr=lq_ptr,
                    store_set_hit=store_set_hit,
                    load_wait_bit=load_wait_bit,
                    load_wait_strict=load_wait_strict,
                    wait_for_rob_idx_flag=wait_for_rob_idx_flag,
                    wait_for_rob_idx_value=wait_for_rob_idx_value,
                    rob_idx_flag=rob_idx_flag,
                    rob_idx_value=rob_idx_value,
                    pdest=pdest,
                    ftq_idx_flag=ftq_idx_flag,
                    ftq_idx_value=ftq_idx_value,
                    pc=pc,
                )
            )
        )

    def issue_scalar_load_batch_same_cycle(self, txns, max_cycles: int = 50) -> None:
        txns = tuple(txns)
        self.issue_cycle(
            IssueCyclePlan(
                ops=tuple(IssueOp.load_from_txn(txn) for txn in txns),
                max_cycles=max_cycles,
            )
        )

    def issue_scalar_load_batch_with_sta_same_cycle(
        self,
        txns,
        *,
        sta_req_id: int,
        sta_sq_ptr,
        sta_addr: int,
        sta_lane: int = 3,
        sta_mask: int = 0xFF,
        max_cycles: int = 50,
    ) -> None:
        txns = tuple(txns)
        self.issue_cycle(
            IssueCyclePlan(
                ops=tuple(IssueOp.load_from_txn(txn) for txn in txns)
                + (
                    IssueOp.sta(
                        req_id=sta_req_id,
                        sq_ptr=sta_sq_ptr,
                        addr=sta_addr,
                        lane=sta_lane,
                        mask=sta_mask,
                    ),
                ),
                max_cycles=max_cycles,
            )
        )

    def issue_scalar_std(
        self,
        req_id: int,
        sq_ptr,
        data: int,
        lane: int = 5,
        mask: int = 0xFF,
        rob_idx_flag: int | None = None,
        rob_idx_value: int | None = None,
        ftq_idx_flag: int | None = None,
        ftq_idx_value: int | None = None,
    ) -> None:
        self.issue_cycle(
            IssueCyclePlan.from_ops(
                IssueOp.std(
                    req_id=req_id,
                    sq_ptr=sq_ptr,
                    data=data,
                    lane=lane,
                    mask=mask,
                    rob_idx_flag=rob_idx_flag,
                    rob_idx_value=rob_idx_value,
                    ftq_idx_flag=ftq_idx_flag,
                    ftq_idx_value=ftq_idx_value,
                )
            )
        )

    def issue_scalar_sta(
        self,
        req_id: int,
        sq_ptr,
        addr: int,
        lane: int = 3,
        mask: int = 0xFF,
        rob_idx_flag: int | None = None,
        rob_idx_value: int | None = None,
        ftq_idx_flag: int | None = None,
        ftq_idx_value: int | None = None,
    ) -> None:
        self.issue_cycle(
            IssueCyclePlan.from_ops(
                IssueOp.sta(
                    req_id=req_id,
                    sq_ptr=sq_ptr,
                    addr=addr,
                    lane=lane,
                    mask=mask,
                    rob_idx_flag=rob_idx_flag,
                    rob_idx_value=rob_idx_value,
                    ftq_idx_flag=ftq_idx_flag,
                    ftq_idx_value=ftq_idx_value,
                )
            )
        )

    def issue_cycle(self, plan: IssueCyclePlan) -> None:
        self.env._run_async(self._issue_cycle_async(plan))
        for op in plan.ops:
            if op.kind == "load":
                self.env.backend.note_load_issued(op.resolved_rob_idx_flag, op.resolved_rob_idx_value)

    def issue_script(self, plans) -> None:
        for plan in plans:
            self.issue_cycle(plan)
