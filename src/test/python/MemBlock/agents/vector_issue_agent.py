# coding=utf-8
"""
Vector issue active agent.
"""

from transactions import VectorMemTxn


class VectorIssueAgent:
    """Drive the dedicated vector issue ports."""

    def __init__(self, env) -> None:
        self.env = env

    def _candidate_ports(self, preferred_port: int) -> tuple[int, ...]:
        ports = list(range(len(self.env.vector_issue)))
        preferred_port = int(preferred_port)
        if preferred_port in ports:
            ports.remove(preferred_port)
            ports.insert(0, preferred_port)
        return tuple(ports)

    def _write_optional(self, port: int, signal_name: str, value: int) -> None:
        signal = getattr(self.env.dut, f"io_ooo_to_mem_vecIssue_{int(port)}_0_{signal_name}", None)
        if signal is not None:
            signal.value = int(value)

    def _mask_vec_gen(self, txn: VectorMemTxn) -> int:
        """Build the 16-byte active-byte mask expected by the vector split pipeline."""

        mask = 0
        active_limit = min(int(txn.vl), int(txn.element_count))
        size_bytes = txn.size_bytes
        for element_idx in range(int(txn.element_count)):
            if element_idx < int(txn.vstart) or element_idx >= active_limit:
                continue
            if txn.mask_bits is not None and not int(txn.mask_bits[element_idx]):
                continue
            byte_base = element_idx * size_bytes
            byte_limit = min(16, byte_base + size_bytes)
            for byte_idx in range(byte_base, byte_limit):
                mask |= 1 << byte_idx
        return mask

    def _drive(self, txn: VectorMemTxn, port: int) -> None:
        issue = self.env.vector_issue[int(port)]
        vsew = {8: 0, 16: 1, 32: 2, 64: 3}[txn.sew_bits]
        veew = int(txn.veew)
        if veew == 0 and txn.sew_bits != 8:
            veew = vsew
        issue.write("valid", 1)
        issue.write("bits_fuType", txn.fu_type)
        issue.write("bits_fuOpType", txn.fu_op_type)
        issue.write("bits_src_0", txn.issue_src_0)
        issue.write("bits_src_1", txn.issue_src_1)
        issue.write("bits_src_2", txn.src_2)
        issue.write("bits_src_3", txn.resolved_mask_source)
        issue.write("bits_vl", txn.vl)
        issue.write("bits_robIdx_flag", txn.rob_idx_flag)
        issue.write("bits_robIdx_value", txn.rob_idx_value)
        issue.write("bits_pdest", txn.resolved_pdest)
        issue.write("bits_pdestVl", txn.pdest_vl)
        issue.write("bits_vecWen", 1 if txn.is_load else 0)
        issue.write("bits_v0Wen", 0)
        issue.write("bits_vlWen", 0)
        issue.write("bits_vpu_vsew", vsew)
        issue.write("bits_vpu_vlmul", txn.lmul)
        issue.write("bits_vpu_vm", 1 if txn.vm else 0)
        issue.write("bits_vpu_vstart", txn.vstart)
        # `vuopIdx` is the vector micro-op index inside one instruction, not the testcase req_id.
        issue.write("bits_vpu_vuopIdx", txn.resolved_vuop_idx)
        issue.write("bits_vpu_lastUop", 1 if txn.last_uop else 0)
        issue.write("bits_vpu_vmask", txn.resolved_vmask)
        issue.write("bits_vpu_nf", txn.nf)
        issue.write("bits_vpu_veew", veew)
        issue.write("bits_vpu_isVleff", 1 if txn.is_vleff else 0)
        issue.write("bits_ftqIdx_flag", 0)
        issue.write("bits_ftqIdx_value", txn.req_id & 0x3F)
        issue.write("bits_ftqOffset", 0)
        issue.write("bits_numLsElem", txn.resolved_num_ls_elem)
        issue.write("bits_lqIdx_flag", txn.lq_ptr.flag)
        issue.write("bits_lqIdx_value", txn.lq_ptr.value)
        issue.write("bits_sqIdx_flag", txn.sq_ptr.flag)
        issue.write("bits_sqIdx_value", txn.sq_ptr.value)
        self._write_optional(port, "bits_vpu_vill", 0)
        self._write_optional(port, "bits_vpu_vma", 1)
        self._write_optional(port, "bits_vpu_vta", 1)
        self._write_optional(port, "bits_vpu_specVill", 0)
        self._write_optional(port, "bits_vpu_specVma", 1)
        self._write_optional(port, "bits_vpu_specVta", 1)
        self._write_optional(port, "bits_vpu_specVsew", vsew)
        self._write_optional(port, "bits_vpu_specVlmul", txn.lmul)
        self._write_optional(port, "bits_vpu_frm", 0)
        self._write_optional(port, "bits_vpu_fpu_isFpToVecInst", 0)
        self._write_optional(port, "bits_vpu_fpu_isFP32Instr", 0)
        self._write_optional(port, "bits_vpu_fpu_isFP64Instr", 0)
        self._write_optional(port, "bits_vpu_fpu_isReduction", 0)
        self._write_optional(port, "bits_vpu_fpu_isFoldTo1_2", 0)
        self._write_optional(port, "bits_vpu_fpu_isFoldTo1_4", 0)
        self._write_optional(port, "bits_vpu_fpu_isFoldTo1_8", 0)
        self._write_optional(port, "bits_vpu_vxrm", 0)
        self._write_optional(port, "bits_vpu_isReverse", 0)
        self._write_optional(port, "bits_vpu_isExt", 0)
        self._write_optional(port, "bits_vpu_isNarrow", 0)
        self._write_optional(port, "bits_vpu_isDstMask", 0)
        self._write_optional(port, "bits_vpu_isOpMask", 0)
        self._write_optional(port, "bits_vpu_isMove", 0)
        self._write_optional(port, "bits_vpu_isDependOldVd", 0)
        self._write_optional(port, "bits_vpu_isWritePartVd", 0)
        self._write_optional(port, "bits_vpu_maskVecGen", self._mask_vec_gen(txn))
        self._write_optional(port, "bits_vpu_sew8", 1 if txn.sew_bits == 8 else 0)
        self._write_optional(port, "bits_vpu_sew16", 1 if txn.sew_bits == 16 else 0)
        self._write_optional(port, "bits_vpu_sew32", 1 if txn.sew_bits == 32 else 0)
        self._write_optional(port, "bits_vpu_sew64", 1 if txn.sew_bits == 64 else 0)

    async def _issue_async(self, txn: VectorMemTxn, max_cycles: int = 50) -> None:
        candidate_ports = self._candidate_ports(txn.issue_port)
        per_port_cycles = max(1, max_cycles // max(1, len(candidate_ports)))
        for port in candidate_ports:
            issue = self.env.vector_issue[int(port)]
            for _ in range(per_port_cycles):
                if int(self.env.dut.io_reset_backend.value):
                    raise RuntimeError("等待 vector issue 握手时 backend 进入 reset")
                self._drive(txn, port)
                if issue.read("ready", 0):
                    await self.env._step_async(1)
                    self.env.idle_inputs()
                    return
                await self.env._step_async(1)
        self.env.idle_inputs()
        raise TimeoutError(f"等待 vector issue 完成握手超时: preferred_port={txn.issue_port}")

    def issue(self, txn: VectorMemTxn, max_cycles: int = 50) -> None:
        self.env._run_async(self._issue_async(txn, max_cycles=max_cycles))
