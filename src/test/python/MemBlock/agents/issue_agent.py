# coding=utf-8
"""
Issue active agent.
"""

from transactions import IssueCyclePlan, IssueOp


def _set_optional_signal(dut, signal_name: str, value: int) -> None:
    signal = getattr(dut, signal_name, None)
    if signal is not None:
        signal.value = value


class IssueAgent:
    """负责 issue lane 握手与驱动。"""

    def __init__(self, env) -> None:
        self.env = env

    def _idle_issue_lane(self, lane: int) -> None:
        self.env.issue[int(lane)].drive_idle()

    def _note_issued_ops(self, ops) -> None:
        for op in ops:
            if op.kind == "load":
                self.env.backend.note_load_issued(op.resolved_rob_idx_flag, op.resolved_rob_idx_value)

    def _current_cycle(self):
        current_cycle = getattr(self.env, "_current_cycle", None)
        if callable(current_cycle):
            try:
                return current_cycle()
            except Exception:
                return None
        return None

    def _format_issue_op(self, op: IssueOp) -> str:
        return (
            f"{op.kind}@lane{int(op.lane)}"
            f"/rob=({int(op.resolved_rob_idx_flag)},{int(op.resolved_rob_idx_value)})"
            f"/lq=({int(op.lq_ptr.flag)},{int(op.lq_ptr.value)})"
            f"/sq=({int(op.sq_ptr.flag)},{int(op.sq_ptr.value)})"
            f"/addr={int(getattr(op, 'addr', 0)):#x}"
        )

    def _format_issue_lane_state(self, lane: int) -> str:
        issue = self.env.issue[int(lane)]
        return (
            f"lane{int(lane)}:"
            f"v={int(issue.valid.value)}"
            f"/r={int(issue.ready.value)}"
            f"/rob=({int(issue.bits_robIdx_flag.value)},{int(issue.bits_robIdx_value.value)})"
            f"/src0={int(issue.bits_src_0.value):#x}"
        )

    def _print_elastic_debug(self, event: str, **fields) -> None:
        cycle = self._current_cycle()
        prefix = "[IssueAgent][elastic]"
        if cycle is not None:
            prefix += f"[cycle={int(cycle)}]"
        detail = " ".join(f"{key}={value}" for key, value in fields.items())
        if detail:
            print(f"{prefix} {event} {detail}")
        else:
            print(f"{prefix} {event}")

    def _drive_scalar_std(
        self,
        *,
        op: IssueOp,
        sq_ptr,
        data: int,
        lane: int,
    ) -> None:
        issue = self.env.issue[lane]
        issue.valid.value = 1
        issue.bits_fuOpType.value = op.store_fu_op_type
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
            )
            return
        if op.kind == "std":
            self._drive_scalar_std(
                op=op,
                sq_ptr=op.sq_ptr,
                data=op.data,
                lane=int(op.lane),
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
        issue.bits_fuOpType.value = op.load_fu_op_type
        issue.bits_src_0.value = addr
        issue.bits_robIdx_flag.value = op.resolved_rob_idx_flag
        issue.bits_robIdx_value.value = op.resolved_rob_idx_value
        issue.bits_sqIdx_flag.value = sq_ptr.flag
        issue.bits_sqIdx_value.value = sq_ptr.value

        _set_optional_signal(self.env.dut, f"{prefix}imm", 0)
        _set_optional_signal(self.env.dut, f"{prefix}pdest", op.resolved_pdest)
        _set_optional_signal(self.env.dut, f"{prefix}rfWen", 0 if int(op.fp_wen) else 1)
        _set_optional_signal(self.env.dut, f"{prefix}fpWen", int(op.fp_wen))
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
    ) -> None:
        issue = self.env.issue[lane]
        prefix = f"io_ooo_to_mem_intIssue_{lane}_0_bits_"
        issue.valid.value = 1
        issue.bits_fuOpType.value = op.store_fu_op_type
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

        pending_ops = {int(op.lane): op for op in plan.ops} if plan.handshake_mode == "elastic" else None
        for _ in range(plan.max_cycles):
            if int(self.env.dut.io_reset_backend.value):
                if plan.handshake_mode == "elastic":
                    raise RuntimeError(f"等待 issue 批次握手时 backend 进入 reset: lanes={lanes}")
                raise RuntimeError(f"等待同拍 issue 握手时 backend 进入 reset: lanes={lanes}")

            if plan.handshake_mode == "strict":
                if all(int(self.env.issue[lane].ready.value) for lane in lanes):
                    for op in plan.ops:
                        self._drive_issue_op(op)
                    if hasattr(self.env, "refresh_comb"):
                        self.env.refresh_comb()
                    await self.env._step_async(1)
                    self._note_issued_ops(plan.ops)
                    self.env.idle_inputs()
                    if hasattr(self.env, "refresh_comb"):
                        self.env.refresh_comb()
                    return

                await self.env._step_async(1)
                continue

            self._print_elastic_debug(
                "loop",
                pending=tuple(self._format_issue_op(pending_ops[lane]) for lane in sorted(pending_ops)),
            )
            for lane in lanes:
                if lane in pending_ops:
                    self._drive_issue_op(pending_ops[lane])
                else:
                    self._idle_issue_lane(lane)
            if hasattr(self.env, "refresh_comb"):
                self.env.refresh_comb()
            self._print_elastic_debug(
                "driven",
                lanes=tuple(self._format_issue_lane_state(lane) for lane in lanes),
            )

            await self.env._step_async(1)

            candidate_lanes = [
                lane
                for lane in tuple(pending_ops)
                if int(self.env.issue[lane].valid.value) and int(self.env.issue[lane].ready.value)
            ]
            self._print_elastic_debug("candidate", lanes=tuple(int(lane) for lane in candidate_lanes))
            self._print_elastic_debug(
                "post-step",
                candidates=tuple(int(lane) for lane in candidate_lanes),
            )
            for lane in candidate_lanes:
                op = pending_ops.pop(lane)
                self._note_issued_ops((op,))
                self._print_elastic_debug("fire", lane=int(lane), op=self._format_issue_op(op))

            for lane in lanes:
                if lane in pending_ops:
                    self._drive_issue_op(pending_ops[lane])
                else:
                    self._idle_issue_lane(lane)
            if hasattr(self.env, "refresh_comb"):
                self.env.refresh_comb()
            if not pending_ops:
                self._print_elastic_debug("done", lanes=tuple(int(lane) for lane in lanes))
                self.env.idle_inputs()
                if hasattr(self.env, "refresh_comb"):
                    self.env.refresh_comb()
                return

        self.env.idle_inputs()
        if hasattr(self.env, "refresh_comb"):
            self.env.refresh_comb()
        if plan.handshake_mode == "elastic":
            self._print_elastic_debug(
                "timeout",
                pending=tuple(sorted(int(lane) for lane in pending_ops)),
            )
            raise TimeoutError(f"等待 issue 批次完成握手超时: lanes={lanes}, pending_lanes={sorted(pending_ops)}")
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

    def issue_script(self, plans) -> None:
        for plan in plans:
            self.issue_cycle(plan)
