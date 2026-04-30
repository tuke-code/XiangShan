# coding=utf-8
"""Shared scalar issue-lane layout for MemBlock env and tests."""

LOAD_ISSUE_LANES = (0, 1, 2)
STA_ISSUE_LANES = (3, 4)
STD_ISSUE_LANES = (5, 6)

ISSUE_LANE_KIND = {
    **{lane: "load" for lane in LOAD_ISSUE_LANES},
    **{lane: "sta" for lane in STA_ISSUE_LANES},
    **{lane: "std" for lane in STD_ISSUE_LANES},
}


def issue_lane_kind(lane: int) -> str:
    lane = int(lane)
    kind = ISSUE_LANE_KIND.get(lane)
    if kind is None:
        raise ValueError(f"unsupported MemBlock issue lane: {lane}")
    return kind


def assert_issue_lane_kind(lane: int, expected_kind: str) -> None:
    actual_kind = issue_lane_kind(lane)
    if actual_kind != expected_kind:
        raise ValueError(
            f"issue lane {int(lane)} is `{actual_kind}`, expected `{expected_kind}`"
        )
