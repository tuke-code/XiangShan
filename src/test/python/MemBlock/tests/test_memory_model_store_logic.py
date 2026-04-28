# coding=utf-8
"""
MemoryModel store/deferred-compare 纯 Python 单元测试。
"""

from memory_model import (
    MemoryModel,
    TL_A_PUT_FULL,
    TL_A_PUT_PARTIAL,
)
from model.ref_memory import RefMemory
from model.scoreboard import Scoreboard


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
    store_addr = FakeReadBundle(valid=1, sqIdx_value=0, paddr=0x1000, miss=0, nc=0)
    store_mask = FakeReadBundle(valid=1, sqIdx_value=0, mask=0x00FF)
    store_data = FakeReadBundle(valid=1, sqIdx_value=0, fuType=1 << 17, fuOpType=0x3, data=0x8877665544332211)

    model = _create_model(
        writebacks=[writeback],
        store_addr_inputs=[store_addr],
        store_mask_inputs=[store_mask],
        store_data_inputs=[store_data],
    )

    model.preload_u64(0x1000, 0x1122334455667788)
    model.note_store_allocated(sq_idx_flag=0, sq_idx_value=0, rob_idx_flag=0, rob_idx_value=0)
    model.note_store_request(sq_idx=0, addr=0x1000, data=0x8877665544332211, mask=0xFF)
    model.scoreboard.mark_store_committed(0)
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
        sbuffer_writes=[sbuffer_write],
    )

    model.note_store_allocated(sq_idx_flag=0, sq_idx_value=0, rob_idx_flag=0, rob_idx_value=3)
    model.note_store_request(sq_idx=0, addr=0x2000, data=0xAABBCCDDEEFF0011, mask=0xFF)
    model.scoreboard.mark_store_completed(0)
    model.after_cycle()
    result = model.finalize_and_check_drain()

    assert result["drain_event_count"] == 1
    assert model.read(0x2000, 8) == 0xAABBCCDDEEFF0011


def test_memory_model_finalize_accepts_cbo_zero_wline_drain_and_zeroes_cacheline():
    store_addr = FakeReadBundle(valid=1, sqIdx_value=0, paddr=0x2040, miss=0, nc=0)
    store_mask = FakeReadBundle(valid=1, sqIdx_value=0, mask=0xFFFF)
    store_data = FakeReadBundle(valid=1, sqIdx_value=0, fuType=1 << 17, fuOpType=0x7, data=0)
    sbuffer_write = FakeReadBundle(
        valid=1,
        ready=1,
        addr=0x2040,
        data=0,
        mask=0,
        wline=1,
        vecValid=1,
    )

    model = _create_model(
        store_addr_inputs=[store_addr],
        store_mask_inputs=[store_mask],
        store_data_inputs=[store_data],
        sbuffer_writes=[sbuffer_write],
    )
    model.preload_bytes(0x2040, bytes(range(64)))

    model.note_store_allocated(sq_idx_flag=0, sq_idx_value=0, rob_idx_flag=0, rob_idx_value=4)
    model.note_store_request(sq_idx=0, addr=0x2040, data=0, mask=0xFF, opcode="cbo_zero")
    model.scoreboard.mark_store_completed(0)
    model.after_cycle()
    result = model.finalize_and_check_drain()

    assert result["drain_event_count"] == 1
    assert model.drain_log[-1]["wline"] is True
    assert model.drain_log[-1]["kind"] == "cbo_zero"
    assert model.read_cacheline(0x2040) == bytes(64)


def test_memory_model_records_outer_put_partial_as_drain_event():
    model = _create_model(outer_delay=0)

    model.drive_pre_step(0)
    model.outer_a.valid.value = 1
    model.outer_a.bits_opcode.value = TL_A_PUT_PARTIAL
    model.outer_a.bits_size.value = 3
    model.outer_a.bits_source.value = 2
    model.outer_a.bits_address.value = 0x3000
    model.outer_a.bits_mask.value = 0x0F
    model.outer_a.bits_data.value = 0xFFEEDDCCBBAA9988

    model.capture_on_rise(1)
    model.drive_pre_step(1)

    assert model.stats["outer_write_request_count"] == 1
    assert model.drain_log[-1]["channel"] == "outer"
    assert model.drain_log[-1]["addr"] == 0x3000
    assert model.outer_d.valid.value == 1


def test_memory_model_finalize_ignores_mmio_outer_drain_in_golden_compare():
    mmio_addr = FakeReadBundle(valid=1, sqIdx_value=0, paddr=0x1000, miss=0, nc=0)
    cacheable_addr = FakeReadBundle(valid=1, sqIdx_value=1, paddr=0x2000, miss=0, nc=0)
    mmio_addr_re = FakeReadBundle(updateAddrValid=1, sqIdx_value=0, nc=0, mmio=1, memBackTypeMM=0, hasException=0)
    mmio_mask = FakeReadBundle(valid=1, sqIdx_value=0, mask=0x00FF)
    cacheable_mask = FakeReadBundle(valid=1, sqIdx_value=1, mask=0x00FF)
    mmio_data = FakeReadBundle(valid=1, sqIdx_value=0, fuType=1 << 17, fuOpType=0x3, data=0x1020304050607080)
    cacheable_data = FakeReadBundle(valid=1, sqIdx_value=1, fuType=1 << 17, fuOpType=0x3, data=0xAABBCCDDEEFF0011)
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
        outer_delay=0,
        store_addr_inputs=[mmio_addr, cacheable_addr],
        store_addr_re_inputs=[mmio_addr_re],
        store_mask_inputs=[mmio_mask, cacheable_mask],
        store_data_inputs=[mmio_data, cacheable_data],
        sbuffer_writes=[sbuffer_write],
    )

    model.note_store_allocated(sq_idx_flag=0, sq_idx_value=0, rob_idx_flag=0, rob_idx_value=2)
    model.note_store_request(sq_idx=0, addr=0x1000, data=0x1020304050607080, mask=0xFF)
    model.scoreboard.mark_store_completed(0)
    model.note_store_allocated(sq_idx_flag=0, sq_idx_value=1, rob_idx_flag=0, rob_idx_value=3)
    model.note_store_request(sq_idx=1, addr=0x2000, data=0xAABBCCDDEEFF0011, mask=0xFF)
    model.scoreboard.mark_store_completed(1)
    model.drive_pre_step(0)
    model.after_cycle()
    model.outer_a.valid.value = 1
    model.outer_a.bits_opcode.value = TL_A_PUT_FULL
    model.outer_a.bits_size.value = 3
    model.outer_a.bits_source.value = 1
    model.outer_a.bits_address.value = 0x1000
    model.outer_a.bits_data.value = 0x1020304050607080

    model.capture_on_rise(1)
    model.drive_pre_step(1)
    result = model.finalize_and_check_drain()

    assert result["drain_event_count"] == 2
    assert model.read(0x1000, 8) == 0x0
    assert model.read(0x2000, 8) == 0xAABBCCDDEEFF0011


def test_scoreboard_cross_16b_store_request_and_split_drain_pair():
    refmem = RefMemory()
    low_window_addr = 0x4008
    high_window_addr = 0x4010
    store_addr = 0x400D
    store_data = 0xA1A2A3A4A5A6A7A8

    refmem.preload_u64(low_window_addr, 0x1111222233334444)
    refmem.preload_u64(high_window_addr, 0x5555666677778888)

    scoreboard = Scoreboard(refmem, rob_size=64, store_queue_size=16)
    scoreboard.note_store_allocated(
        sq_idx_flag=0,
        sq_idx_value=0,
        rob_idx_flag=0,
        rob_idx_value=0,
    )
    scoreboard.note_store_request(
        sq_idx=0,
        addr=store_addr,
        data=store_data,
        mask=0xFF,
    )
    scoreboard.observe_store_addr(
        sq_idx=0,
        paddr=store_addr,
        miss=False,
        mask=0xE000,
        nc=False,
    )
    scoreboard.observe_store_mask(sq_idx=0, mask=0xE000)
    scoreboard.observe_store_data(sq_idx=0, data=store_data)
    scoreboard.mark_store_committed(0)

    combined_data = 221522368320484764596627788725951374501
    combined_mask = 0xE01F
    scoreboard.observe_sbuffer_write(
        lane_idx=0,
        addr=0x4000,
        data=combined_data,
        mask=combined_mask,
        width_bytes=16,
    )
    scoreboard.observe_sbuffer_write(
        lane_idx=1,
        addr=0x4010,
        data=combined_data,
        mask=combined_mask,
        width_bytes=16,
    )

    result = scoreboard.finalize_and_check_drain()

    assert result["drain_event_count"] == 2
    assert refmem.read(low_window_addr, 8) == 0xA6A7A82233334444
    assert refmem.read(high_window_addr, 8) == 0x555566A1A2A3A4A5


def test_ref_memory_masked_write_and_read():
    refmem = RefMemory()

    refmem.preload_u64(0x1000, 0x1122334455667788)
    refmem.apply_masked_write(0x1000, 0xAABBCCDDEEFF0011, 0x0F, 8)

    assert refmem.read(0x1000, 8) == 0x11223344EEFF0011
    assert refmem.read_masked(0x1000, 0x0F, width_bytes=8) == 0xEEFF0011


def test_ref_memory_clone_isolated_from_mutations():
    refmem = RefMemory()

    refmem.preload_u64(0x1000, 0x1122334455667788)
    cloned = refmem.clone()
    cloned.apply_masked_write(0x1000, 0xAABBCCDDEEFF0011, 0x0F, 8)

    assert refmem.read(0x1000, 8) == 0x1122334455667788
    assert cloned.read(0x1000, 8) == 0x11223344EEFF0011


def test_ref_memory_with_masked_write_returns_forked_view():
    refmem = RefMemory()

    refmem.preload_u64(0x1000, 0x0)
    predicted = refmem.with_masked_write(0x1004, 0x1122334455667788, 0xFF, 8)

    assert refmem.read(0x1000, 8) == 0x0
    assert predicted.read(0x1000, 8) == 0x5566778800000000
    assert predicted.read(0x1008, 8) == 0x0000000011223344


def test_ref_memory_apply_store_decodes_mask_width():
    refmem = RefMemory()

    refmem.preload_u64(0x1000, 0x1122334455667788)
    refmem.apply_store(0x1006, 0xA1A2, 0x03)

    assert refmem.read(0x1000, 8) == 0xA1A2334455667788


def test_ref_memory_apply_cbo_zero_zeroes_entire_cacheline():
    refmem = RefMemory()

    refmem.preload_bytes(0x1040, bytes((index * 3) & 0xFF for index in range(64)))
    refmem.apply_cbo_zero(0x1058)

    assert refmem.read_cacheline(0x1040) == bytes(64)


def test_ref_memory_with_store_returns_predicted_view():
    refmem = RefMemory()

    refmem.preload_u64(0x1000, 0x0)
    predicted = refmem.with_store(0x1004, 0x1122334455667788)

    assert refmem.read(0x1000, 8) == 0x0
    assert predicted.read(0x1000, 8) == 0x5566778800000000
    assert predicted.read(0x1008, 8) == 0x0000000011223344


def test_memory_model_can_fork_current_ref_memory():
    model = _create_model()

    model.preload_u64(0x1000, 0x1122334455667788)
    forked = model.fork_ref_memory()
    forked.apply_masked_write(0x1000, 0xAABBCCDDEEFF0011, 0x0F, 8)

    assert model.read(0x1000, 8) == 0x1122334455667788
    assert forked.read(0x1000, 8) == 0x11223344EEFF0011


def test_memory_model_predict_store_returns_forked_view():
    model = _create_model()

    model.preload_u64(0x1000, 0x0)
    predicted = model.predict_store(0x1004, 0x1122334455667788)

    assert model.read(0x1000, 8) == 0x0
    assert predicted.read(0x1000, 8) == 0x5566778800000000
    assert predicted.read(0x1008, 8) == 0x0000000011223344


def test_scoreboard_can_be_unit_tested_without_transport():
    writeback = FakeWriteback(
        valid=1,
        ready=1,
        data_0=0x1122334455667788,
        pdest=7,
        intWen=1,
        robIdx_flag=0,
        robIdx_value=1,
        isFromLoadUnit=1,
        exception_bits=[0] * 24,
    )
    refmem = RefMemory()
    scoreboard = Scoreboard(
        refmem,
        rob_size=512,
        store_queue_size=56,
    )

    refmem.preload_u64(0x1000, 0x1122334455667788)
    scoreboard.expect_load(rob_idx_flag=0, rob_idx_value=1, pdest=7, addr=0x1000, size=8, mask=0xFF)
    scoreboard.note_load_issued(0, 1)
    scoreboard.note_load_commits(1)
    scoreboard.observe_load_writeback(
        data=writeback.read("data_0", 0),
        pdest=writeback.read("pdest", 0),
        int_wen=writeback.read("intWen", 0),
        fp_wen=writeback.read("fpWen", 0),
        rob_idx_flag=writeback.read("robIdx_flag", 0),
        rob_idx_value=writeback.read("robIdx_value", 0),
        exception_bits=writeback.read_exception_bits(),
    )

    assert scoreboard.completed_loads == 1
    assert scoreboard.outstanding_expected_count == 0


def test_scoreboard_supports_fp_load_writeback_boxing():
    writeback = FakeWriteback(
        valid=1,
        ready=1,
        data_0=0xFFFFFFFF55667788,
        pdest=9,
        intWen=0,
        fpWen=1,
        robIdx_flag=0,
        robIdx_value=2,
        isFromLoadUnit=1,
        exception_bits=[0] * 24,
    )
    refmem = RefMemory()
    scoreboard = Scoreboard(
        refmem,
        rob_size=512,
        store_queue_size=56,
    )

    refmem.preload_u64(0x2000, 0x1122334455667788)
    scoreboard.expect_load(rob_idx_flag=0, rob_idx_value=2, pdest=9, addr=0x2000, size=4, mask=0x0F, fp_wen=1)
    scoreboard.note_load_issued(0, 2)
    scoreboard.note_load_commits(1)
    scoreboard.observe_load_writeback(
        data=writeback.read("data_0", 0),
        pdest=writeback.read("pdest", 0),
        int_wen=writeback.read("intWen", 0),
        fp_wen=writeback.read("fpWen", 0),
        rob_idx_flag=writeback.read("robIdx_flag", 0),
        rob_idx_value=writeback.read("robIdx_value", 0),
        exception_bits=writeback.read_exception_bits(),
    )

    assert scoreboard.completed_loads == 1
    assert scoreboard.outstanding_expected_count == 0


def test_scoreboard_sign_extends_narrow_scalar_loads():
    writeback = FakeWriteback(
        valid=1,
        ready=1,
        data_0=0xFFFFFFFFD566F788,
        pdest=10,
        intWen=1,
        fpWen=0,
        robIdx_flag=0,
        robIdx_value=3,
        isFromLoadUnit=1,
        exception_bits=[0] * 24,
    )
    refmem = RefMemory()
    scoreboard = Scoreboard(
        refmem,
        rob_size=512,
        store_queue_size=56,
    )

    refmem.preload_u64(0x3000, 0x11223344D566F788)
    scoreboard.expect_load(rob_idx_flag=0, rob_idx_value=3, pdest=10, addr=0x3000, size=4, mask=0x0F)
    scoreboard.note_load_issued(0, 3)
    scoreboard.note_load_commits(1)
    scoreboard.observe_load_writeback(
        data=writeback.read("data_0", 0),
        pdest=writeback.read("pdest", 0),
        int_wen=writeback.read("intWen", 0),
        fp_wen=writeback.read("fpWen", 0),
        rob_idx_flag=writeback.read("robIdx_flag", 0),
        rob_idx_value=writeback.read("robIdx_value", 0),
        exception_bits=writeback.read_exception_bits(),
    )

    assert scoreboard.completed_loads == 1
    assert scoreboard.outstanding_expected_count == 0
