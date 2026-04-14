# coding=utf-8
"""
Thin vector facade on top of the unified backend plan runtime.
"""

from transactions import BackendSendPlan, VectorEnqueueStep, VectorIssueStep, VectorMemTxn, VectorWaitStep


class VectorBackendFacade:
    """Expose vector-friendly defaults without creating a second plan system."""

    def __init__(self, env) -> None:
        self.env = env

    def default_plan(self, txn: VectorMemTxn) -> BackendSendPlan:
        return BackendSendPlan.from_steps(
            VectorEnqueueStep.from_txn(txn),
            VectorIssueStep.from_txn(txn),
            VectorWaitStep(req_id=txn.req_id, event="complete_or_trap"),
        )

    def send(self, txn: VectorMemTxn):
        return self.env.backend.execute(self.default_plan(txn))

    def execute(self, request):
        if isinstance(request, VectorMemTxn):
            return self.send(request)
        return self.env.backend.execute(request)

    def wait_complete(self, req_id: int, max_cycles: int = 200):
        return self.env._run_async(
            self.env.vector_monitor.wait_event_async(
                req_id,
                event="complete",
                max_cycles=max_cycles,
            )
        )

    def wait_trap(self, req_id: int, max_cycles: int = 200):
        return self.env._run_async(
            self.env.vector_monitor.wait_event_async(
                req_id,
                event="trap",
                max_cycles=max_cycles,
            )
        )
