# coding=utf-8
"""
LSQ enqueue active agent.
"""

from transactions import EnqueueLoadCyclePlan, QueuePtr


FU_TYPE_LDU = 1 << 16
FU_TYPE_STU = 1 << 17


class LsqAgent:
    """负责 load/store 的 enqLsq 驱动。"""

    def __init__(self, env) -> None:
        self.env = env

    async def _wait_load_enq_ready_async(self, max_cycles: int = 200) -> None:
        for _ in range(max_cycles):
            if int(self.env.dut.io_reset_backend.value):
                raise RuntimeError("等待 load `enqLsq` ready 时 backend 进入 reset")
            if self.env.lsq_enq_meta.canAccept.value and self.env.lsq_status.lqCanAccept.value:
                return
            await self.env._step_async(1)
        raise TimeoutError("等待 load `enqLsq` ready 超时")

    def wait_load_enq_ready(self, max_cycles: int = 200) -> None:
        self.env._run_async(self._wait_load_enq_ready_async(max_cycles=max_cycles))

    async def _wait_store_enq_ready_async(self, max_cycles: int = 200) -> None:
        for _ in range(max_cycles):
            if int(self.env.dut.io_reset_backend.value):
                raise RuntimeError("等待 store `enqLsq` ready 时 backend 进入 reset")
            if self.env.lsq_enq_meta.canAccept.value and self.env.lsq_status.sqCanAccept.value:
                return
            await self.env._step_async(1)
        raise TimeoutError("等待 store `enqLsq` ready 超时")

    def wait_store_enq_ready(self, max_cycles: int = 200) -> None:
        self.env._run_async(self._wait_store_enq_ready_async(max_cycles=max_cycles))

    async def _enqueue_scalar_load_async(
        self,
        req_id: int,
        lq_ptr: QueuePtr,
        sq_ptr: QueuePtr,
        enq_port: int = 0,
    ) -> None:
        await self._wait_load_enq_ready_async()

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

        await self.env._step_and_idle_async(1)

    def enqueue_scalar_load(
        self,
        req_id: int,
        lq_ptr: QueuePtr,
        sq_ptr: QueuePtr,
        enq_port: int = 0,
    ) -> None:
        self.env._run_async(
            self._enqueue_scalar_load_async(
                req_id=req_id,
                lq_ptr=lq_ptr,
                sq_ptr=sq_ptr,
                enq_port=enq_port,
            )
        )

    async def _enqueue_load_cycle_async(self, plan: EnqueueLoadCyclePlan) -> None:
        ports = [int(step.enq_port) for step in plan.steps]
        if len(set(ports)) != len(ports):
            raise ValueError(f"同拍 load enqueue 需要 port 唯一: ports={ports}")

        for _ in range(plan.max_cycles):
            if int(self.env.dut.io_reset_backend.value):
                raise RuntimeError(f"等待同拍 load enqueue 握手时 backend 进入 reset: ports={ports}")
            if self.env.lsq_enq_meta.canAccept.value and self.env.lsq_status.lqCanAccept.value:
                for step in plan.steps:
                    req = self.env.lsq_enq_req[step.enq_port]
                    self.env.lsq_enq_meta.need_alloc[step.enq_port].value = 1
                    req.valid.value = 1
                    req.bits_fuType.value = FU_TYPE_LDU
                    req.bits_uopIdx.value = step.req_id & 0x7F
                    req.bits_robIdx_flag.value = (step.req_id >> 9) & 0x1
                    req.bits_robIdx_value.value = step.req_id & 0x1FF
                    req.bits_lqIdx_flag.value = step.lq_ptr.flag
                    req.bits_lqIdx_value.value = step.lq_ptr.value
                    req.bits_sqIdx_flag.value = step.sq_ptr.flag
                    req.bits_sqIdx_value.value = step.sq_ptr.value
                    req.bits_numLsElem.value = 1
                await self.env._step_and_idle_async(1)
                return
            await self.env._step_async(1)

        self.env.idle_inputs()
        raise TimeoutError(f"等待同拍 load enqueue 完成握手超时: ports={ports}")

    def enqueue_load_cycle(self, plan: EnqueueLoadCyclePlan) -> None:
        self.env._run_async(self._enqueue_load_cycle_async(plan))

    async def _enqueue_scalar_store_async(
        self,
        req_id: int,
        sq_ptr: QueuePtr,
        enq_port: int = 0,
    ) -> QueuePtr:
        await self._wait_store_enq_ready_async()

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

        await self.env._step_async(1)
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

    def enqueue_scalar_store(
        self,
        req_id: int,
        sq_ptr: QueuePtr,
        enq_port: int = 0,
    ) -> QueuePtr:
        return self.env._run_async(
            self._enqueue_scalar_store_async(
                req_id=req_id,
                sq_ptr=sq_ptr,
                enq_port=enq_port,
            )
        )
