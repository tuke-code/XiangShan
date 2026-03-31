# coding=utf-8
"""
WritebackMonitor: 观测 intWriteback 并发布到 scoreboard。
"""


class WritebackMonitor:
    def __init__(self, writebacks, scoreboard) -> None:
        self.writebacks = list(writebacks or [])
        self.scoreboard = scoreboard

    def attach_writebacks(self, writebacks) -> None:
        self.writebacks = list(writebacks)

    def drive_ready(self) -> None:
        for bundle in self.writebacks:
            if hasattr(bundle, "set_ready"):
                bundle.set_ready(1)

    def after_cycle(self) -> None:
        for bundle in self.writebacks:
            if not getattr(bundle, "connected", lambda name: False)("valid"):
                continue
            if bundle.read("valid", 0) == 0:
                continue
            if bundle.connected("ready") and bundle.read("ready", 0) == 0:
                continue

            if bundle.connected("isFromLoadUnit") and bundle.read("isFromLoadUnit", 0) == 0:
                self.scoreboard.observe_store_writeback(
                    sq_idx=bundle.read("sqIdx_value", -1),
                    rob_idx_flag=bundle.read("robIdx_flag", None)
                    if bundle.connected("robIdx_flag")
                    else None,
                    rob_idx_value=bundle.read("robIdx_value", None)
                    if bundle.connected("robIdx_value")
                    else None,
                )
                continue
            if bundle.connected("toRob_valid") and bundle.read("toRob_valid", 0) == 0:
                continue
            if (
                not bundle.connected("data_0")
                or not bundle.connected("pdest")
                or not bundle.connected("intWen")
                or not bundle.connected("robIdx_flag")
                or not bundle.connected("robIdx_value")
            ):
                self.scoreboard.observe_store_writeback(
                    sq_idx=bundle.read("sqIdx_value", -1),
                    rob_idx_flag=bundle.read("robIdx_flag", None)
                    if bundle.connected("robIdx_flag")
                    else None,
                    rob_idx_value=bundle.read("robIdx_value", None)
                    if bundle.connected("robIdx_value")
                    else None,
                )
                continue

            self.scoreboard.observe_load_writeback(
                data=bundle.read("data_0", 0),
                pdest=bundle.read("pdest", 0),
                int_wen=bundle.read("intWen", 0),
                rob_idx_flag=bundle.read("robIdx_flag", 0),
                rob_idx_value=bundle.read("robIdx_value", 0),
                exception_bits=bundle.read_exception_bits(),
            )
