# coding=utf-8
"""
IssueAgent 单元测试。
"""

import asyncio

from agents.issue_agent import IssueAgent
from transactions import AtomicTxn, IssueCyclePlan, IssueOp, QueuePtr, RobIndex


class _FakeSignal:
    def __init__(self, value: int = 0) -> None:
        self.value = int(value)


class _FakeIssueBundle:
    def __init__(self, ready: int = 1) -> None:
        self.ready = _FakeSignal(ready)
        self.valid = _FakeSignal(0)
        self.bits_fuType = _FakeSignal(0)
        self.bits_fuOpType = _FakeSignal(0)
        self.bits_src_0 = _FakeSignal(0)
        self.bits_robIdx_flag = _FakeSignal(0)
        self.bits_robIdx_value = _FakeSignal(0)
        self.bits_sqIdx_flag = _FakeSignal(0)
        self.bits_sqIdx_value = _FakeSignal(0)

    def drive_idle(self) -> None:
        self.valid.value = 0
        self.bits_fuType.value = 0
        self.bits_fuOpType.value = 0
        self.bits_src_0.value = 0
        self.bits_robIdx_flag.value = 0
        self.bits_robIdx_value.value = 0
        self.bits_sqIdx_flag.value = 0
        self.bits_sqIdx_value.value = 0


class _FakeDut:
    def __init__(self) -> None:
        self.io_reset_backend = _FakeSignal(0)


class _FakeBackend:
    def __init__(self) -> None:
        self.load_issued = []

    def note_load_issued(self, rob_idx_flag: int, rob_idx_value: int) -> None:
        self.load_issued.append((int(rob_idx_flag), int(rob_idx_value)))


class _FakeIssueEnv:
    def __init__(
        self,
        ready_schedule: dict[int, list[int]],
        *,
        accepted_schedule: dict[int, list[int]] | None = None,
        debug_visible_schedule: list[set[int]] | None = None,
    ) -> None:
        max_lane = max(ready_schedule) if ready_schedule else 0
        self.dut = _FakeDut()
        self.backend = _FakeBackend()
        self.issue = [_FakeIssueBundle() for _ in range(max_lane + 1)]
        self._ready_schedule = {int(lane): [int(v) for v in values] for lane, values in ready_schedule.items()}
        self._accepted_schedule = (
            {}
            if accepted_schedule is None
            else {int(lane): [int(v) for v in values] for lane, values in accepted_schedule.items()}
        )
        self._debug_visible_schedule = [set(values) for values in (debug_visible_schedule or [])]
        self._cycle = 0
        self.valid_history = []
        self.handshakes = []
        for lane, bundle in enumerate(self.issue):
            bundle.ready.value = self._scheduled_ready(lane, cycle=0)

    def _scheduled_ready(self, lane: int, *, cycle: int) -> int:
        values = self._ready_schedule.get(int(lane), [1])
        idx = min(int(cycle), len(values) - 1)
        return int(values[idx])

    def _scheduled_accepted(self, lane: int, *, cycle: int) -> int | None:
        values = self._accepted_schedule.get(int(lane))
        if values is None:
            return None
        idx = min(int(cycle), len(values) - 1)
        return int(values[idx])

    def refresh_comb(self) -> None:
        for lane, bundle in enumerate(self.issue):
            bundle.ready.value = self._scheduled_ready(lane, cycle=self._cycle)

    def sample_issue_accept_state(self) -> dict:
        return {
            "cycle": self._cycle,
            "lanes": tuple(
                {
                    "cycle": self._cycle,
                    "lane": lane,
                    "ready": int(bundle.ready.value),
                    "accepted": self._scheduled_accepted(lane, cycle=self._cycle),
                }
                for lane, bundle in enumerate(self.issue)
            ),
        }

    def sample_load_debug_state(self) -> dict:
        if not self._debug_visible_schedule:
            return None
        idx = min(self._cycle, len(self._debug_visible_schedule) - 1) if self._debug_visible_schedule else -1
        visible = set() if idx < 0 else self._debug_visible_schedule[idx]
        return {
            "cycle": self._cycle,
            "lanes": tuple(
                {
                    "cycle": self._cycle,
                    "lane": lane,
                    "s1_rob_idx_value": lane if lane in visible else -1,
                    "s2_rob_idx_value": -1,
                    "s3_rob_idx_value": -1,
                }
                for lane, _ in enumerate(self.issue)
            ),
        }

    async def _step_async(self, cycles: int = 1) -> None:
        for _ in range(cycles):
            active_lanes = []
            for lane, bundle in enumerate(self.issue):
                if int(bundle.valid.value):
                    active_lanes.append(lane)
                accepted = self._scheduled_accepted(lane, cycle=self._cycle)
                if accepted is None:
                    accepted = int(bundle.ready.value)
                if int(bundle.valid.value) and int(accepted):
                    self.handshakes.append((self._cycle, lane, int(bundle.bits_src_0.value)))
            self.valid_history.append(tuple(active_lanes))
            self._cycle += 1
            for lane, bundle in enumerate(self.issue):
                accepted = self._scheduled_accepted(lane, cycle=self._cycle - 1)
                if accepted is None:
                    bundle.ready.value = self._scheduled_ready(lane, cycle=self._cycle)
                else:
                    # Model xcomm/picker style visibility: the just-finished cycle's
                    # accept result becomes observable after stepping the clock.
                    bundle.ready.value = int(accepted)

    def _run_async(self, coro):
        return asyncio.run(coro)

    def idle_inputs(self) -> None:
        for bundle in self.issue:
            bundle.drive_idle()


def _load_op(*, lane: int, addr: int) -> IssueOp:
    return IssueOp.load(
        req_id=lane + 1,
        addr=addr,
        lq_ptr=QueuePtr(flag=0, value=lane),
        sq_ptr=QueuePtr(flag=0, value=0),
        lane=lane,
        rob_idx=RobIndex(flag=0, value=lane),
        pdest=lane,
        ftq_idx_flag=0,
        ftq_idx_value=lane,
        pc=0x80000000 + lane * 4,
    )


def test_api_issue_agent_strict_mode_waits_for_all_lanes_ready():
    env = _FakeIssueEnv(
        {
            0: [1, 1, 1],
            1: [0, 0, 1],
        }
    )
    agent = IssueAgent(env)

    agent.issue_cycle(
        IssueCyclePlan.from_ops(
            _load_op(lane=0, addr=0x1000),
            _load_op(lane=1, addr=0x2000),
        )
    )

    assert env.valid_history == [(), (), (0, 1)]
    assert env.handshakes == [
        (2, 0, 0x1000),
        (2, 1, 0x2000),
    ]
    assert env.backend.load_issued == [(0, 0), (0, 1)]
    assert env.issue[0].valid.value == 0
    assert env.issue[1].valid.value == 0


def test_api_issue_agent_elastic_mode_allows_per_lane_backpressure():
    env = _FakeIssueEnv(
        {
            0: [1, 1, 1],
            1: [0, 1, 1],
        },
        accepted_schedule={
            0: [1, 1, 1],
            1: [0, 1, 1],
        },
    )
    agent = IssueAgent(env)

    agent.issue_cycle(
        IssueCyclePlan.from_ops(
            _load_op(lane=0, addr=0x3000),
            _load_op(lane=1, addr=0x4000),
            handshake_mode="elastic",
        )
    )

    assert env.valid_history == [(0, 1), (1,)]
    assert env.handshakes == [
        (0, 0, 0x3000),
        (1, 1, 0x4000),
    ]
    assert env.backend.load_issued == [(0, 0), (0, 1)]
    assert env.issue[0].valid.value == 0
    assert env.issue[1].valid.value == 0


def test_api_issue_agent_elastic_mode_keeps_valid_asserted_while_backpressured():
    env = _FakeIssueEnv(
        {
            0: [0, 0, 1, 1],
        },
        accepted_schedule={
            0: [0, 0, 1, 1],
        },
    )
    agent = IssueAgent(env)

    agent.issue_cycle(
        IssueCyclePlan.from_ops(
            _load_op(lane=0, addr=0x7000),
            handshake_mode="elastic",
        )
    )

    assert env.valid_history == [(0,), (0,), (0,)]
    assert env.handshakes == [
        (2, 0, 0x7000),
    ]
    assert env.backend.load_issued == [(0, 0)]
    assert env.issue[0].valid.value == 0


def test_api_issue_agent_elastic_mode_retires_immediately_after_handshake():
    env = _FakeIssueEnv(
        {
            0: [1, 1, 1],
            1: [1, 1, 1],
        },
        accepted_schedule={
            0: [1, 1, 1],
            1: [1, 1, 1],
        },
    )
    agent = IssueAgent(env)

    agent.issue_cycle(
        IssueCyclePlan.from_ops(
            _load_op(lane=0, addr=0x5000),
            _load_op(lane=1, addr=0x6000),
            handshake_mode="elastic",
        )
    )

    assert env.valid_history == [(0, 1)]
    assert env.handshakes == [
        (0, 0, 0x5000),
        (0, 1, 0x6000),
    ]
    assert env.backend.load_issued == [(0, 0), (0, 1)]


def test_api_issue_agent_elastic_mode_does_not_retry_after_successful_handshake():
    env = _FakeIssueEnv(
        {
            0: [1, 1, 1],
        },
        accepted_schedule={
            0: [1, 1, 1],
        },
    )
    agent = IssueAgent(env)

    agent.issue_cycle(
        IssueCyclePlan.from_ops(
            _load_op(lane=0, addr=0x8000),
            handshake_mode="elastic",
        )
    )

    assert env.valid_history == [(0,)]
    assert env.handshakes == [
        (0, 0, 0x8000),
    ]
    assert env.backend.load_issued == [(0, 0)]


def test_api_issue_agent_load_drives_fp_wen_and_size_specific_fu_op():
    env = _FakeIssueEnv({0: [1, 1]})
    prefix = "io_ooo_to_mem_intIssue_0_0_bits_"
    setattr(env.dut, f"{prefix}rfWen", _FakeSignal(0))
    setattr(env.dut, f"{prefix}fpWen", _FakeSignal(0))
    setattr(env.dut, f"{prefix}pdest", _FakeSignal(0))
    setattr(env.dut, f"{prefix}pc", _FakeSignal(0))
    setattr(env.dut, f"{prefix}ftqIdx_flag", _FakeSignal(0))
    setattr(env.dut, f"{prefix}ftqIdx_value", _FakeSignal(0))
    setattr(env.dut, f"{prefix}lqIdx_flag", _FakeSignal(0))
    setattr(env.dut, f"{prefix}lqIdx_value", _FakeSignal(0))
    agent = IssueAgent(env)

    op = IssueOp.load(
        req_id=1,
        addr=0x1000,
        lq_ptr=QueuePtr(flag=0, value=1),
        sq_ptr=QueuePtr(flag=0, value=2),
        lane=0,
        size=4,
        fp_wen=1,
        rob_idx=RobIndex(flag=0, value=3),
        pdest=5,
        ftq_idx_flag=0,
        ftq_idx_value=7,
        pc=0x80000020,
    )
    agent._drive_issue_op(op)

    assert env.issue[0].bits_fuOpType.value == 0x2
    assert getattr(env.dut, f"{prefix}rfWen").value == 0
    assert getattr(env.dut, f"{prefix}fpWen").value == 1
    assert getattr(env.dut, f"{prefix}pdest").value == 5
    assert getattr(env.dut, f"{prefix}lqIdx_value").value == 1


def test_api_issue_agent_integer_load_drives_size_specific_fu_op():
    env = _FakeIssueEnv({0: [1, 1]})
    prefix = "io_ooo_to_mem_intIssue_0_0_bits_"
    setattr(env.dut, f"{prefix}rfWen", _FakeSignal(0))
    setattr(env.dut, f"{prefix}fpWen", _FakeSignal(0))
    agent = IssueAgent(env)

    op = IssueOp.load(
        req_id=2,
        addr=0x2000,
        lq_ptr=QueuePtr(flag=0, value=2),
        sq_ptr=QueuePtr(flag=0, value=3),
        lane=0,
        size=2,
        mask=0x03,
        rob_idx=RobIndex(flag=0, value=4),
        pdest=6,
        ftq_idx_flag=0,
        ftq_idx_value=8,
        pc=0x80000024,
    )
    agent._drive_issue_op(op)

    assert env.issue[0].bits_fuOpType.value == 0x1
    assert getattr(env.dut, f"{prefix}rfWen").value == 1
    assert getattr(env.dut, f"{prefix}fpWen").value == 0


def test_api_issue_agent_drives_atomic_fu_type_and_sta_pdest():
    env = _FakeIssueEnv({3: [1], 5: [1]})
    sta_prefix = "io_ooo_to_mem_intIssue_3_0_bits_"
    std_prefix = "io_ooo_to_mem_intIssue_5_0_bits_"
    setattr(env.dut, f"{sta_prefix}fuType", _FakeSignal(0))
    setattr(env.dut, f"{sta_prefix}pdest", _FakeSignal(0))
    setattr(env.dut, f"{sta_prefix}rfWen", _FakeSignal(0))
    setattr(env.dut, f"{sta_prefix}ftqIdx_flag", _FakeSignal(0))
    setattr(env.dut, f"{sta_prefix}ftqIdx_value", _FakeSignal(0))
    setattr(env.dut, f"{std_prefix}fuType", _FakeSignal(0))
    agent = IssueAgent(env)
    txn = AtomicTxn(
        req_id=0x71,
        sq_ptr=QueuePtr(flag=0, value=3),
        addr=0x8000,
        operand=0x55,
        opcode="amoadd",
        pdest=9,
    )
    txn.assigned_rob_idx = RobIndex(flag=0, value=7)
    txn.assigned_ftq_idx_flag = 0
    txn.assigned_ftq_idx_value = 5
    txn.assigned_pc = 0x80000020

    agent.issue_cycle(
        IssueCyclePlan.from_ops(
            IssueOp.atomic_sta(txn),
            IssueOp.atomic_std(txn),
        )
    )

    assert getattr(env.dut, f"{sta_prefix}fuType").value == txn.fu_type
    assert getattr(env.dut, f"{sta_prefix}pdest").value == txn.resolved_pdest
    assert getattr(env.dut, f"{sta_prefix}rfWen").value == 1
    assert getattr(env.dut, f"{std_prefix}fuType").value == txn.fu_type
