# coding=utf-8
"""
Commit-boundary active agent.
"""

from agents.rob_agent import RobAgent


class CommitAgent:
    """兼容旧接口的 ROB commit agent 壳层。"""

    def __init__(self, env) -> None:
        self.env = env
        self.driver = getattr(env, "rob_agent", None)
        if self.driver is None:
            self.driver = RobAgent(env.dut, rob_size=env.config.rob_size)
            env.rob_agent = self.driver
        env.pending_ptr = self.driver

    def reset(self) -> None:
        self.driver.reset()

    def drive(self) -> None:
        self.driver.drive()

    def note_load_issued(self, rob_idx_flag: int, rob_idx_value: int) -> None:
        self.driver.note_load_issued(rob_idx_flag, rob_idx_value)

    def note_store_allocated(
        self,
        rob_idx_flag: int,
        rob_idx_value: int,
        *,
        sq_idx_flag: int | None = None,
        sq_idx_value: int | None = None,
    ) -> None:
        self.driver.note_store_allocated(
            rob_idx_flag,
            rob_idx_value,
            sq_idx_flag=sq_idx_flag,
            sq_idx_value=sq_idx_value,
        )

    def note_load_completed(self, rob_idx_flag: int, rob_idx_value: int) -> None:
        self.driver.note_load_completed(rob_idx_flag, rob_idx_value)

    def note_non_mem_issued(self, rob_idx_flag: int, rob_idx_value: int) -> None:
        self.driver.note_non_mem_issued(rob_idx_flag, rob_idx_value)

    def release_non_mem(self, rob_idx_flag: int, rob_idx_value: int) -> None:
        self.driver.release_non_mem(rob_idx_flag, rob_idx_value)

    def mark_store_addr_ready(self, sq_idx_flag: int, sq_idx_value: int) -> None:
        self.driver.mark_store_addr_ready(sq_idx_flag, sq_idx_value)

    def mark_store_data_ready(self, sq_idx_flag: int, sq_idx_value: int) -> None:
        self.driver.mark_store_data_ready(sq_idx_flag, sq_idx_value)

    def mark_store_commit_ready(self, sq_idx_flag: int, sq_idx_value: int, ready: bool = True) -> None:
        self.driver.mark_store_commit_ready(sq_idx_flag, sq_idx_value, ready=ready)

    def advance(self) -> None:
        self.driver.advance()

    def queue_store_commit(self, count: int = 1) -> None:
        self.driver.queue_store_commit(count)

    @property
    def latest_commit_packet(self):
        return self.driver.latest_commit_packet

    @property
    def pending_ptr(self):
        return self.driver.pending_ptr

    @property
    def pending_ptr_next(self):
        return self.driver.pending_ptr_next

    @property
    def signal_support(self) -> dict[str, bool]:
        return self.driver.signal_support

    @property
    def supports_full_rob_lsqio(self) -> bool:
        return self.driver.supports_full_rob_lsqio

    @property
    def models_pending_ptr_next(self) -> bool:
        return self.driver.models_pending_ptr_next

    @property
    def models_commit_bool(self) -> bool:
        return self.driver.models_commit_bool

    @property
    def models_mixed_commit_packet(self) -> bool:
        return self.driver.models_mixed_commit_packet

    @property
    def stats(self) -> dict[str, int]:
        return self.driver.stats
