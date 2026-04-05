# coding=utf-8
"""
ROB coverage collector 冒烟测试。
"""

from MemBlock_api import api_MemBlock_reset


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

    api_MemBlock_reset(env, cycles=2, max_cycles=20)
    env.Step(2)
    env.rob_coverage.finalize()
    reports = env.rob_coverage.as_dict()
    assert len(reports) == 4
    assert reports[0]["name"] == "MemBlock.ROB.ObservedBehavior"
    assert reports[3]["name"] == "MemBlock.ROB.KnownModelGaps"
    known_gap_names = {point["name"] for point in reports[3]["points"]}
    assert "known_gap_pending_ptr_next_not_modelled" in known_gap_names
    assert "known_gap_non_mem_blocker_not_modelled" in known_gap_names
