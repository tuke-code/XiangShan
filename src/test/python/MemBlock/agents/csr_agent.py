# coding=utf-8
"""
CSR active agent.
"""


class CsrAgent:
    """负责维护 MemBlock CSR 相关输入。"""

    MODE_M = 3

    def __init__(self, env) -> None:
        self.env = env
        self.tlb_csr = env.tlb_csr
        self.csr_ctrl = env.csr_ctrl

    def reset(self) -> None:
        self._drive_zero()
        self.set_m_mode()

    def set_m_mode(self) -> None:
        self.tlb_csr.priv_virt.value = 0
        self.tlb_csr.priv_virt_changed.value = 0
        self.tlb_csr.priv_imode.value = self.MODE_M
        self.tlb_csr.priv_dmode.value = self.MODE_M

    def _drive_zero(self) -> None:
        for _, signal in self.tlb_csr.all_signals():
            signal.value = 0
        for _, signal in self.csr_ctrl.all_signals():
            signal.value = 0
