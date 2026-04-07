# coding=utf-8
"""
LSQ enqueue active agent.
"""

from transactions import QueuePtr


FU_TYPE_LDU = 1 << 16
FU_TYPE_STU = 1 << 17


class LsqAgent:
    """负责 load/store 的 enqLsq 驱动。"""

    def __init__(self, env) -> None:
        self.env = env

    def wait_load_enq_ready(self, max_cycles: int = 200) -> None:
        for _ in range(max_cycles):
            if int(self.env.dut.io_reset_backend.value):
                raise RuntimeError("等待 load `enqLsq` ready 时 backend 进入 reset")
            if self.env.lsq_enq_meta.canAccept.value and self.env.lsq_status.lqCanAccept.value:
                return
            self.env.Step(1)
        raise TimeoutError("等待 load `enqLsq` ready 超时")

    def wait_store_enq_ready(self, max_cycles: int = 200) -> None:
        for _ in range(max_cycles):
            if int(self.env.dut.io_reset_backend.value):
                raise RuntimeError("等待 store `enqLsq` ready 时 backend 进入 reset")
            if self.env.lsq_enq_meta.canAccept.value and self.env.lsq_status.sqCanAccept.value:
                return
            self.env.Step(1)
        raise TimeoutError("等待 store `enqLsq` ready 超时")

    def enqueue_scalar_load(
        self,
        req_id: int,
        lq_ptr: QueuePtr,
        sq_ptr: QueuePtr,
        enq_port: int = 0,
    ) -> None:
        self.wait_load_enq_ready()

        req = self.env.lsq_enq_req[enq_port]
        self.env.lsq_enq_meta.need_alloc[enq_port].value = 1
        req.valid.value = 1
        req.bits_fuType.value = FU_TYPE_LDU
        req.bits_uopIdx.value = req_id & 0x7F
        req.bits_robIdx_flag.value = (req_id >> 9) & 0x1
        req.bits_robIdx_value.value = req_id & 0x1FF
        req.bits_lqIdx_flag.value = lq_ptr.flag
        req.bits_lqIdx_value.value = lq_ptr.value
        req.bits_sqIdx_flag.value = sq_ptr.flag
        req.bits_sqIdx_value.value = sq_ptr.value
        req.bits_numLsElem.value = 1

        self.env.Step(1)
        self.env.idle_inputs()

    def enqueue_scalar_store(
        self,
        req_id: int,
        sq_ptr: QueuePtr,
        enq_port: int = 0,
    ) -> QueuePtr:
        self.wait_store_enq_ready()

        req = self.env.lsq_enq_req[enq_port]
        self.env.lsq_enq_meta.need_alloc[enq_port].value = 2
        req.valid.value = 1
        req.bits_fuType.value = FU_TYPE_STU
        req.bits_uopIdx.value = req_id & 0x7F
        req.bits_robIdx_flag.value = (req_id >> 9) & 0x1
        req.bits_robIdx_value.value = req_id & 0x1FF
        req.bits_lqIdx_flag.value = 0
        req.bits_lqIdx_value.value = 0
        req.bits_sqIdx_flag.value = sq_ptr.flag
        req.bits_sqIdx_value.value = sq_ptr.value
        req.bits_numLsElem.value = 1

        self.env.Step(1)
        allocated_sq_ptr = QueuePtr(
            flag=int(self.env.lsq_enq_resp[enq_port].sqIdx_flag.value),
            value=int(self.env.lsq_enq_resp[enq_port].sqIdx_value.value),
        )
        self.env.backend.note_store_allocated(
            sq_idx_flag=allocated_sq_ptr.flag,
            sq_idx_value=allocated_sq_ptr.value,
            rob_idx_flag=(req_id >> 9) & 0x1,
            rob_idx_value=req_id & 0x1FF,
        )
        self.env.idle_inputs()
        return allocated_sq_ptr
