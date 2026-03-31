# coding=utf-8
"""
Commit-boundary active agent.
"""


class CommitAgent:
    """负责 pendingPtr 与 store commit 驱动。"""

    def __init__(self, env) -> None:
        self.env = env
        self.driver = env.pending_ptr

    def reset(self) -> None:
        self.driver.reset()

    def drive(self) -> None:
        self.driver.drive()

    def note_load_issued(self, rob_idx_flag: int, rob_idx_value: int) -> None:
        self.driver.note_issued(rob_idx_flag, rob_idx_value)

    def note_load_completed(self, rob_idx_flag: int, rob_idx_value: int) -> None:
        self.driver.note_completed(rob_idx_flag, rob_idx_value)

    def advance(self) -> None:
        self.driver.advance()

    def queue_store_commit(self, count: int = 1) -> None:
        self.driver.queue_store_commit(count)
