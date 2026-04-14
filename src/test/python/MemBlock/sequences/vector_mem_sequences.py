# coding=utf-8
"""
Minimal vector memory sequence helpers for Phase 1.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VectorSequenceResult:
    txn: object
    expected: object
    backend_result: object
    vector_result: object


class VectorLoadSequence:
    def __init__(self, txn) -> None:
        self.txn = txn

    def run(self, env) -> VectorSequenceResult:
        expected = env.memory.vector.expect_load(self.txn)
        backend_result = env.vector_backend.send(self.txn)
        vector_result = backend_result.get_vector_result(self.txn.req_id)
        env.memory.vector.mark_completed(self.txn.req_id)
        return VectorSequenceResult(
            txn=self.txn,
            expected=expected,
            backend_result=backend_result,
            vector_result=vector_result,
        )


class VectorStoreSequence:
    def __init__(self, txn) -> None:
        self.txn = txn

    def run(self, env) -> VectorSequenceResult:
        expected = env.memory.vector.predict_store(self.txn)
        backend_result = env.vector_backend.send(self.txn)
        vector_result = backend_result.get_vector_result(self.txn.req_id)
        env.memory.vector.mark_completed(self.txn.req_id)
        return VectorSequenceResult(
            txn=self.txn,
            expected=expected,
            backend_result=backend_result,
            vector_result=vector_result,
        )
