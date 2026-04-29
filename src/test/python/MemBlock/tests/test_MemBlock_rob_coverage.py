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
