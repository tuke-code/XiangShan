# coding=utf-8
"""
MemStatusMonitor: 观测 mem_status 并桥接 commit/scoreboard。
"""


class MemStatusMonitor:
    def __init__(self, mem_status, memory, commit_agent) -> None:
        self.mem_status = mem_status
        self.memory = memory
        self.commit_agent = commit_agent

    def after_cycle(self) -> None:
        self.memory.note_load_commits(int(self.mem_status.lqDeq.value))
        for rob_idx in self.memory.drain_completed_robs():
            self.commit_agent.note_load_completed(rob_idx.flag, rob_idx.value)
