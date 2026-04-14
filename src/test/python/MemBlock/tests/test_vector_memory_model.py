# coding=utf-8
"""
VectorMemoryModel pure-Python tests.
"""

from model.ref_memory import RefMemory
from model.vector_memory_model import VectorMemoryModel
from transactions import QueuePtr, VectorMemTxn


def _txn(**kwargs):
    defaults = dict(
        req_id=0x21,
        is_load=True,
        opcode_class="unit_stride",
        base_addr=0x1000,
        lq_ptr=QueuePtr(0, 1),
        sq_ptr=QueuePtr(0, 2),
        vl=3,
        element_count=5,
        sew_bits=32,
        vstart=1,
        mask_bits=(1, 1, 0, 1, 1),
    )
    defaults.update(kwargs)
    return VectorMemTxn(**defaults)


def test_api_vector_memory_model_expand_classifies_active_prestart_tail_and_mask():
    ref_memory = RefMemory()
    for idx, value in enumerate((0x11, 0x22, 0x33, 0x44, 0x55)):
        ref_memory.preload_bytes(0x1000 + idx * 4, value.to_bytes(4, "little"))
    model = VectorMemoryModel(ref_memory)

    accesses = model.expand(_txn())

    assert [item.is_prestart for item in accesses] == [True, False, False, False, False]
    assert [item.is_tail for item in accesses] == [False, False, False, True, True]
    assert [item.active for item in accesses] == [False, True, False, False, False]
    assert [item.should_access_memory for item in accesses] == [False, True, False, False, False]
    assert accesses[1].expected_load_data == 0x22


def test_api_vector_memory_model_expand_stride_uses_signed_stride():
    ref_memory = RefMemory()
    ref_memory.preload_bytes(0x2010, (0xAA).to_bytes(2, "little"))
    ref_memory.preload_bytes(0x200E, (0xBB).to_bytes(2, "little"))
    model = VectorMemoryModel(ref_memory)

    accesses = model.expand(
        _txn(
            base_addr=0x2010,
            opcode_class="stride",
            stride=-2,
            sew_bits=16,
            vl=2,
            element_count=2,
            vstart=0,
            mask_bits=(1, 1),
        )
    )

    assert [item.addr for item in accesses] == [0x2010, 0x200E]
    assert [item.expected_load_data for item in accesses] == [0xAA, 0xBB]


def test_api_vector_memory_model_predict_store_updates_refmem_and_outstanding():
    ref_memory = RefMemory()
    ref_memory.preload_bytes(0x3000, bytes(range(16)))
    model = VectorMemoryModel(ref_memory)
    txn = _txn(
        req_id=0x31,
        is_load=False,
        base_addr=0x3000,
        sew_bits=16,
        vl=3,
        element_count=4,
        vstart=0,
        mask_bits=(1, 0, 1, 1),
        store_data=(0xCAFE, 0xDEAD, 0xBEEF, 0x1234),
    )

    predicted = model.predict_store(txn)

    assert model.outstanding_expected_count == 1
    assert predicted.read(0x3000, 2) == 0xCAFE
    assert predicted.read(0x3002, 2) == ref_memory.read(0x3002, 2)
    assert predicted.read(0x3004, 2) == 0xBEEF
    model.mark_completed(txn.req_id)
    assert model.outstanding_expected_count == 0
