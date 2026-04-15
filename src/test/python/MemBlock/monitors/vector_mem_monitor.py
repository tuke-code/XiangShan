# coding=utf-8
"""
Vector memory writeback/completion monitor.
"""

from __future__ import annotations

from transactions import RobIndex, VectorMemResult, make_rob_index


def _req_id_from_rob(rob_idx_flag: int, rob_idx_value: int) -> int:
    return (int(rob_idx_flag) << 9) | int(rob_idx_value)


class VectorMemMonitor:
    """Collect vector writeback facts and provide wait helpers."""

    def __init__(self, env, writebacks) -> None:
        self.env = env
        self.writebacks = tuple(writebacks)
        self._seen_events: set[tuple] = set()
        self._writebacks_by_req: dict[int, list[dict]] = {}
        self._results_by_req: dict[int, VectorMemResult] = {}
        self._req_by_rob: dict[RobIndex, int] = {}

    def drive_ready(self) -> None:
        for bundle in self.writebacks:
            if hasattr(bundle, "set_ready"):
                bundle.set_ready(1)

    def reset_runtime_state(self) -> None:
        self._seen_events.clear()
        self._writebacks_by_req.clear()
        self._results_by_req.clear()
        self._req_by_rob.clear()
        self.drive_ready()

    def register_req(
        self,
        req_id: int,
        rob_idx: RobIndex | None = None,
        rob_idx_flag: int | None = None,
        rob_idx_value: int | None = None,
    ) -> None:
        normalized_rob_idx = make_rob_index(
            rob_idx=rob_idx,
            rob_idx_flag=rob_idx_flag,
            rob_idx_value=rob_idx_value,
        )
        if normalized_rob_idx is None:
            raise ValueError("register_req requires a rob_idx")
        self._req_by_rob[normalized_rob_idx] = int(req_id)

    def sample(self) -> None:
        self.drive_ready()
        for lane, bundle in enumerate(self.writebacks):
            if not bundle.connected("valid") or bundle.read("valid", 0) == 0:
                continue
            if bundle.connected("ready") and bundle.read("ready", 1) == 0:
                continue
            rob_idx_flag = bundle.read("robIdx_flag", 0)
            rob_idx_value = bundle.read("robIdx_value", 0)
            req_id = self._req_by_rob.get(
                RobIndex(flag=int(rob_idx_flag), value=int(rob_idx_value)),
                _req_id_from_rob(rob_idx_flag, rob_idx_value),
            )
            exception_bits = bundle.read_exception_bits()
            event = {
                "lane": lane,
                "req_id": req_id,
                "rob_idx_flag": rob_idx_flag,
                "rob_idx_value": rob_idx_value,
                "data": bundle.read("data_0", 0),
                "pdest": bundle.read("pdest", 0),
                "pdest_vl": bundle.read("pdestVl", 0),
                "vec_wen": bundle.read("vecWen", 0),
                "v0_wen": bundle.read("v0Wen", 0),
                "vl_wen": bundle.read("vlWen", 0),
                "observed_vstart": bundle.read("vls_vpu_vstart", None),
                "observed_vl": bundle.read("vls_vpu_vl", None),
                "last_uop": bundle.read("vls_vpu_lastUop", 0),
                "is_strided": bundle.read("vls_isStrided", 0),
                "is_vec_load": bundle.read("vls_isVecLoad", 0),
                "debug_is_mmio": bundle.read("debug_isMMIO", 0),
                "debug_is_ncio": bundle.read("debug_isNCIO", 0),
                "debug_paddr": bundle.read("debug_paddr", 0),
                "debug_vaddr": bundle.read("debug_vaddr", 0),
                "exception_bits": exception_bits,
            }
            event_key = (
                lane,
                req_id,
                event["data"],
                event["pdest"],
                event["pdest_vl"],
                event["vec_wen"],
                event["v0_wen"],
                event["vl_wen"],
                event["observed_vstart"],
                event["observed_vl"],
                event["last_uop"],
                tuple(exception_bits),
            )
            if event_key in self._seen_events:
                continue
            self._seen_events.add(event_key)
            self._writebacks_by_req.setdefault(req_id, []).append(event)
            trapped = any(int(bit) for bit in exception_bits)
            completed = trapped or bool(event["last_uop"])
            if completed:
                self._results_by_req[req_id] = VectorMemResult(
                    req_id=req_id,
                    completed=not trapped,
                    trapped=trapped,
                    observed_vl=event["observed_vl"],
                    observed_vstart=event["observed_vstart"],
                    observed_writebacks=tuple(self._writebacks_by_req.get(req_id, ())),
                    observed_exception={"exception_bits": tuple(exception_bits)} if trapped else None,
                )

    def get_result(self, req_id: int) -> VectorMemResult | None:
        self.sample()
        return self._results_by_req.get(int(req_id))

    async def wait_event_async(
        self,
        req_id: int,
        *,
        event: str = "complete_or_trap",
        max_cycles: int = 200,
    ) -> VectorMemResult:
        for _ in range(max_cycles):
            result = self.get_result(req_id)
            if result is not None:
                if event == "complete" and result.completed:
                    return result
                if event == "trap" and result.trapped:
                    return result
                if event == "complete_or_trap":
                    return result
            await self.env._step_async(1)
        raise TimeoutError(f"等待 vector req_id={req_id} {event} 超时")
