# coding=utf-8
"""
ROB coverage collector 冒烟测试。
"""

def test_api_MemBlock_rob_coverage_attached(env):
    """验证 env fixture 会自动挂载 ROB coverage collector。"""

    assert hasattr(env, "rob_coverage")
    groups = env.rob_coverage.all_groups()
    assert len(groups) == 4
    assert [group.name for group in groups] == [
        "MemBlock.ROB.ObservedBehavior",
        "MemBlock.ROB.CurrentModel",
        "MemBlock.ROB.GapObserved",
        "MemBlock.ROB.KnownModelGaps",
    ]


def test_api_MemBlock_rob_coverage_smoke_sample(env):
    """验证 coverage collector 能跟随真实 DUT 采样并导出结构化结果。"""

    env.reset(cycles=2, settle_cycles=5)
    env.advance_cycles(2)
    env.rob_coverage.finalize()
    reports = env.rob_coverage.as_dict()
    assert len(reports) == 4
    assert reports[0]["name"] == "MemBlock.ROB.ObservedBehavior"
    assert reports[3]["name"] == "MemBlock.ROB.KnownModelGaps"
    known_gap_names = {point["name"] for point in reports[3]["points"]}
    assert "known_gap_redirect_cancel_not_modelled" in known_gap_names
    assert "known_gap_backend_feedback_credit_not_modelled" not in known_gap_names
    if env.commit_agent.models_pending_ptr_next:
        assert "known_gap_pending_ptr_next_not_modelled" not in known_gap_names
    else:
        assert "known_gap_pending_ptr_next_not_modelled" in known_gap_names
    if env.commit_agent.models_commit_bool:
        assert "known_gap_commit_bool_not_modelled" not in known_gap_names
    else:
        assert "known_gap_commit_bool_not_modelled" in known_gap_names


def test_api_MemBlock_rob_coverage_samples_new_rob_frontier_points(env):
    """验证 coverage collector 能从 ROB agent 统计中采到 blocker/readiness 点。"""

    env.backend.insert_non_mem_blocker(0, 4)
    env.backend.note_store_allocated(
        sq_idx_flag=0,
        sq_idx_value=3,
        rob_idx_flag=0,
        rob_idx_value=5,
    )
    env.backend.mark_store_commit_ready(0, 3, ready=True)
    env.backend.queue_store_commit(1)
    env.rob_agent.drive()
    env.backend.release_non_mem_blocker(0, 4)
    env.rob_agent.drive()
    env.rob_coverage.finalize()
    assert env.rob_coverage._box("non_mem_blocker_inserted_observed").value == 1
    assert env.rob_coverage._box("non_mem_blocker_released_observed").value == 1


def test_api_MemBlock_rob_coverage_allows_mmio_outer_drain_but_still_marks_exclusion(env):
    """MMIO outer drain 可见时，collector 仍应把“未进入 final compare”记成 exclusion 命中。"""

    env.reset(cycles=2, settle_cycles=5)

    env.memory.note_store_allocated(
        sq_idx_flag=0,
        sq_idx_value=0,
        rob_idx_flag=0,
        rob_idx_value=0,
    )
    env.memory.note_store_request(
        sq_idx=0,
        addr=0x1000,
        data=0x1122_3344_5566_7788,
        mask=0xFF,
    )
    env.memory.scoreboard.observe_store_addr(
        sq_idx=0,
        paddr=0x1000,
        miss=False,
        mask=0xFF,
        nc=False,
    )
    env.memory.scoreboard.observe_store_addr_re(
        sq_idx=0,
        mmio=True,
    )
    env.memory.scoreboard.observe_store_data(
        sq_idx=0,
        data=0x1122_3344_5566_7788,
        width_bytes=16,
    )
    env.memory.scoreboard.mark_store_committed(0)
    env.memory.scoreboard.observe_sbuffer_write(
        lane_idx=0,
        addr=0x8000_0000,
        data=0x8877_6655_4433_2211,
        mask=0xFF,
        width_bytes=8,
    )
    env.memory.drain_log.append(
        {
            "channel": "outer",
            "addr": 0x1000,
            "data": 0x1122_3344_5566_7788,
            "mask": 0xFF,
            "width_bytes": 8,
            "cycle": 0,
        }
    )
    env.rob_coverage._latch("flushsb_command_observed")

    env.rob_coverage.finalize()

    assert env.rob_coverage._box("mmio_store_shadow_observed").value == 1
    assert env.rob_coverage._box("outer_drain_observed").value == 1
    assert env.rob_coverage._box("mmio_store_excluded_from_drain_observed").value == 1
