# coding=utf-8
"""
MemoryModel store/deferred-compare 纯 Python 单元测试。
"""

from memory_model import (
    MemoryModel,
    TL_A_PUT_PARTIAL,
)
from model.ref_memory import RefMemory


class FakeSignal:
    def __init__(self, value: int = 0) -> None:
        self.value = value


class FakeOuterA:
    def __init__(self) -> None:
        self.ready = FakeSignal(0)
        self.valid = FakeSignal(0)
        self.bits_opcode = FakeSignal(0)
        self.bits_param = FakeSignal(0)
        self.bits_size = FakeSignal(0)
        self.bits_source = FakeSignal(0)
        self.bits_address = FakeSignal(0)
        self.bits_mask = FakeSignal(0)
        self.bits_data = FakeSignal(0)
        self.bits_corrupt = FakeSignal(0)
        self.bits_user_memBackType_MM = FakeSignal(0)
        self.bits_user_memPageType_NC = FakeSignal(0)


class FakeOuterD:
    def __init__(self) -> None:
        self.ready = FakeSignal(1)
        self.valid = FakeSignal(0)
        self.bits_opcode = FakeSignal(0)
        self.bits_param = FakeSignal(0)
        self.bits_size = FakeSignal(0)
        self.bits_source = FakeSignal(0)
        self.bits_sink = FakeSignal(0)
        self.bits_denied = FakeSignal(0)
        self.bits_data = FakeSignal(0)
        self.bits_corrupt = FakeSignal(0)

    def drive_idle(self) -> None:
        self.valid.value = 0


class FakeDcacheA:
    def __init__(self) -> None:
        self.ready = FakeSignal(0)
        self.valid = FakeSignal(0)
        self.bits_opcode = FakeSignal(0)
        self.bits_param = FakeSignal(0)
        self.bits_size = FakeSignal(0)
        self.bits_source = FakeSignal(0)
        self.bits_address = FakeSignal(0)
        self.bits_mask = FakeSignal(0)
        self.bits_data = FakeSignal(0)
        self.bits_corrupt = FakeSignal(0)
        self.bits_user_alias = FakeSignal(0)
        self.bits_user_memPageType_NC = FakeSignal(0)
        self.bits_user_memBackType_MM = FakeSignal(0)
        self.bits_user_vaddr = FakeSignal(0)
        self.bits_user_reqSource = FakeSignal(0)
        self.bits_user_needHint = FakeSignal(0)
        self.bits_echo_isKeyword = FakeSignal(0)


class FakeDcacheB:
    def __init__(self) -> None:
        self.ready = FakeSignal(1)
        self.valid = FakeSignal(0)
        self.bits_opcode = FakeSignal(0)
        self.bits_param = FakeSignal(0)
        self.bits_size = FakeSignal(0)
        self.bits_source = FakeSignal(0)
        self.bits_address = FakeSignal(0)
        self.bits_mask = FakeSignal(0)
        self.bits_data = FakeSignal(0)
        self.bits_corrupt = FakeSignal(0)

    def drive_idle(self) -> None:
        self.valid.value = 0


class FakeDcacheC:
    def __init__(self) -> None:
        self.ready = FakeSignal(0)
        self.valid = FakeSignal(0)
        self.bits_opcode = FakeSignal(0)
        self.bits_param = FakeSignal(0)
        self.bits_size = FakeSignal(0)
        self.bits_source = FakeSignal(0)
        self.bits_address = FakeSignal(0)
        self.bits_data = FakeSignal(0)
        self.bits_corrupt = FakeSignal(0)
        self.bits_user_alias = FakeSignal(0)
        self.bits_user_memPageType_NC = FakeSignal(0)
        self.bits_user_memBackType_MM = FakeSignal(0)
        self.bits_user_vaddr = FakeSignal(0)
        self.bits_user_reqSource = FakeSignal(0)
        self.bits_user_needHint = FakeSignal(0)
        self.bits_echo_isKeyword = FakeSignal(0)


class FakeDcacheD:
    def __init__(self) -> None:
        self.ready = FakeSignal(1)
        self.valid = FakeSignal(0)
        self.bits_opcode = FakeSignal(0)
        self.bits_param = FakeSignal(0)
        self.bits_size = FakeSignal(0)
        self.bits_source = FakeSignal(0)
        self.bits_sink = FakeSignal(0)
        self.bits_denied = FakeSignal(0)
        self.bits_echo_isKeyword = FakeSignal(0)
        self.bits_data = FakeSignal(0)
        self.bits_corrupt = FakeSignal(0)

    def drive_idle(self) -> None:
        self.valid.value = 0


class FakeDcacheE:
    def __init__(self) -> None:
        self.ready = FakeSignal(0)
        self.valid = FakeSignal(0)
        self.bits_sink = FakeSignal(0)


class FakeDut:
    def __init__(self) -> None:
        self.reset = FakeSignal(0)
        self.io_reset_backend = FakeSignal(0)


class FakeReadBundle:
    def __init__(self, **values) -> None:
        self.values = dict(values)

    def read(self, name: str, default=None):
        return self.values.get(name, default)

    def connected(self, name: str) -> bool:
        return name in self.values


class FakeWriteback(FakeReadBundle):
    def set_ready(self, value: int = 1) -> None:
        self.values["ready"] = value

    def read_exception_bits(self) -> list[int]:
        return list(self.values.get("exception_bits", [0] * 24))


def _create_model(**kwargs):
    return MemoryModel(
        FakeDut(),
        FakeOuterA(),
        FakeOuterD(),
        FakeDcacheA(),
        FakeDcacheB(),
        FakeDcacheC(),
        FakeDcacheD(),
        FakeDcacheE(),
        **kwargs,
    )


def test_memory_model_defers_load_compare_until_commit_boundary():
    writeback = FakeWriteback(
        valid=0,
        ready=1,
        data_0=0,
        pdest=0,
        intWen=1,
        robIdx_flag=0,
        robIdx_value=0,
        isFromLoadUnit=1,
        exception_bits=[0] * 24,
    )
    sq_shadow = FakeReadBundle(
        allocated=1,
        addrvalid=1,
        datavalid=1,
        committed=1,
        completed=0,
        nc=0,
        robIdx_flag=0,
        robIdx_value=0,
    )
    store_addr = FakeReadBundle(valid=1, sqIdx_value=0, paddr=0x1000, miss=0, nc=0)
    store_mask = FakeReadBundle(valid=1, sqIdx_value=0, mask=0x00FF)
    store_data = FakeReadBundle(valid=1, sqIdx_value=0, fuType=1 << 17, fuOpType=0x3, data=0x8877665544332211)

    model = _create_model(
        writebacks=[writeback],
        store_addr_inputs=[store_addr],
        store_mask_inputs=[store_mask],
        store_data_inputs=[store_data],
        sq_shadow_entries=[sq_shadow],
    )

    model.preload_u64(0x1000, 0x1122334455667788)
    model.expect_load(rob_idx_flag=0, rob_idx_value=1, pdest=7, addr=0x1000, size=8, mask=0xFF)
    model.note_load_issued(0, 1)

    model.after_cycle()

    writeback.values.update(
        {
            "valid": 1,
            "data_0": 0x8877665544332211,
            "pdest": 7,
            "robIdx_flag": 0,
            "robIdx_value": 1,
        }
    )
    model.after_cycle()

    assert model.outstanding_expected_count == 1
    assert model.completed_loads == 0

    writeback.values["valid"] = 0
    model.note_load_commits(1)

    assert model.outstanding_expected_count == 0
    assert model.completed_loads == 1
    assert model.read(0x1000, 8) == 0x8877665544332211


def test_memory_model_finalize_checks_sbuffer_drain_against_goldenmem():
    sq_shadow = FakeReadBundle(
        allocated=1,
        addrvalid=1,
        datavalid=1,
        committed=1,
        completed=1,
        nc=0,
        robIdx_flag=0,
        robIdx_value=3,
    )
    store_addr = FakeReadBundle(valid=1, sqIdx_value=0, paddr=0x2000, miss=0, nc=0)
    store_mask = FakeReadBundle(valid=1, sqIdx_value=0, mask=0x00FF)
    store_data = FakeReadBundle(valid=1, sqIdx_value=0, fuType=1 << 17, fuOpType=0x3, data=0xAABBCCDDEEFF0011)
    sbuffer_write = FakeReadBundle(
        valid=1,
        ready=1,
        addr=0x2000,
        data=0xAABBCCDDEEFF0011,
        mask=0x00FF,
        wline=0,
        vecValid=1,
    )

    model = _create_model(
        store_addr_inputs=[store_addr],
        store_mask_inputs=[store_mask],
        store_data_inputs=[store_data],
        sq_shadow_entries=[sq_shadow],
        sbuffer_writes=[sbuffer_write],
    )

    model.after_cycle()
    result = model.finalize_and_check_drain()

    assert result["drain_event_count"] == 1
    assert model.read(0x2000, 8) == 0xAABBCCDDEEFF0011


def test_memory_model_records_outer_put_partial_as_drain_event():
    model = _create_model(outer_delay=0)

    model.outer_a.valid.value = 1
    model.outer_a.bits_opcode.value = TL_A_PUT_PARTIAL
    model.outer_a.bits_size.value = 3
    model.outer_a.bits_source.value = 2
    model.outer_a.bits_address.value = 0x3000
    model.outer_a.bits_mask.value = 0x0F
    model.outer_a.bits_data.value = 0xFFEEDDCCBBAA9988

    model.on_memory_edge(1)

    assert model.stats["outer_write_request_count"] == 1
    assert model.drain_log[-1]["channel"] == "outer"
    assert model.drain_log[-1]["addr"] == 0x3000
    assert model.outer_d.valid.value == 1


def test_ref_memory_masked_write_and_read():
    refmem = RefMemory()

    refmem.preload_u64(0x1000, 0x1122334455667788)
    refmem.apply_masked_write(0x1000, 0xAABBCCDDEEFF0011, 0x0F, 8)

    assert refmem.read(0x1000, 8) == 0x11223344EEFF0011
    assert refmem.read_masked(0x1000, 0x0F, width_bytes=8) == 0xEEFF0011
