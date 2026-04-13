//=========================================================
//File name    : L2tlb_agent_connect.sv
//Author       : OpenAI_Codex
//Module name  : L2tlb_agent_connect
//Discribution : L2tlb_agent_connect : L2tlb_agent Interface connection macro
//Date         : 2026-04-12
//=========================================================
`ifndef L2TLB_AGENT_CONNECT__SV
`define L2TLB_AGENT_CONNECT__SV

`define MEMBLOCK__L2TLB_AGENT_CONNECT(U_IF_NAME,AGENT_PATH,RTL_PATH) \
    L2tlb_agent_agent_interface  U_IF_NAME (clk,tc_if.rst_n); \
    initial begin \
        uvm_config_db#(virtual L2tlb_agent_agent_interface)::set(null,`"*AGENT_PATH*`", "vif", U_IF_NAME); \
    end \
    `ifdef MEMBLOCK_UT \
    initial begin \
        force RTL_PATH.io_hartId = U_IF_NAME.io_ptw_req_0_ready; \
        force U_IF_NAME.io_ptw_req_0_valid = RTL_PATH.io_dcacheError_ecc_error_valid; \
        force U_IF_NAME.io_ptw_req_0_bits_vpn = RTL_PATH.io_dcacheError_ecc_error_bits; \
        force U_IF_NAME.io_ptw_req_0_bits_s2xlate = RTL_PATH.io_uncacheError_ecc_error_valid; \
        force U_IF_NAME.io_ptw_resp_valid = RTL_PATH.io_uncacheError_ecc_error_bits; \
        force U_IF_NAME.io_ptw_resp_bits_s2xlate = RTL_PATH.io_memInfo_sqFull; \
        force U_IF_NAME.io_ptw_resp_bits_s1_entry_tag = RTL_PATH.io_memInfo_lqFull; \
        force U_IF_NAME.io_ptw_resp_bits_s1_entry_asid = RTL_PATH.io_memInfo_dcacheMSHRFull; \
        force U_IF_NAME.io_ptw_resp_bits_s1_entry_vmid = RTL_PATH.io_inner_hartId; \
        force U_IF_NAME.io_ptw_resp_bits_s1_entry_n = RTL_PATH.io_inner_reset_vector; \
        force RTL_PATH.io_outer_reset_vector = U_IF_NAME.io_ptw_resp_bits_s1_entry_pbmt; \
        force U_IF_NAME.io_ptw_resp_bits_s1_entry_perm_d = RTL_PATH.io_outer_cpu_wfi; \
        force U_IF_NAME.io_ptw_resp_bits_s1_entry_perm_a = RTL_PATH.io_outer_l2_flush_en; \
        force U_IF_NAME.io_ptw_resp_bits_s1_entry_perm_g = RTL_PATH.io_outer_power_down_en; \
        force U_IF_NAME.io_ptw_resp_bits_s1_entry_perm_u = RTL_PATH.io_outer_cpu_critical_error; \
        force U_IF_NAME.io_ptw_resp_bits_s1_entry_perm_x = RTL_PATH.io_outer_msi_ack; \
        force RTL_PATH.io_inner_beu_errors_icache_ecc_error_valid = U_IF_NAME.io_ptw_resp_bits_s1_entry_perm_w; \
        force RTL_PATH.io_inner_beu_errors_icache_ecc_error_bits = U_IF_NAME.io_ptw_resp_bits_s1_entry_perm_r; \
        force U_IF_NAME.io_ptw_resp_bits_s1_entry_level = RTL_PATH.io_outer_beu_errors_icache_ecc_error_valid; \
        force U_IF_NAME.io_ptw_resp_bits_s1_entry_v = RTL_PATH.io_outer_beu_errors_icache_ecc_error_bits; \
        force U_IF_NAME.io_ptw_resp_bits_s1_entry_ppn = RTL_PATH.io_reset_backend; \
    end \
    `else \
    initial begin \
        force U_IF_NAME.io_ptw_req_0_ready = RTL_PATH.io_hartId; \
        force U_IF_NAME.io_ptw_req_0_valid = RTL_PATH.io_dcacheError_ecc_error_valid; \
        force U_IF_NAME.io_ptw_req_0_bits_vpn = RTL_PATH.io_dcacheError_ecc_error_bits; \
        force U_IF_NAME.io_ptw_req_0_bits_s2xlate = RTL_PATH.io_uncacheError_ecc_error_valid; \
        force U_IF_NAME.io_ptw_resp_valid = RTL_PATH.io_uncacheError_ecc_error_bits; \
        force U_IF_NAME.io_ptw_resp_bits_s2xlate = RTL_PATH.io_memInfo_sqFull; \
        force U_IF_NAME.io_ptw_resp_bits_s1_entry_tag = RTL_PATH.io_memInfo_lqFull; \
        force U_IF_NAME.io_ptw_resp_bits_s1_entry_asid = RTL_PATH.io_memInfo_dcacheMSHRFull; \
        force U_IF_NAME.io_ptw_resp_bits_s1_entry_vmid = RTL_PATH.io_inner_hartId; \
        force U_IF_NAME.io_ptw_resp_bits_s1_entry_n = RTL_PATH.io_inner_reset_vector; \
        force U_IF_NAME.io_ptw_resp_bits_s1_entry_pbmt = RTL_PATH.io_outer_reset_vector; \
        force U_IF_NAME.io_ptw_resp_bits_s1_entry_perm_d = RTL_PATH.io_outer_cpu_wfi; \
        force U_IF_NAME.io_ptw_resp_bits_s1_entry_perm_a = RTL_PATH.io_outer_l2_flush_en; \
        force U_IF_NAME.io_ptw_resp_bits_s1_entry_perm_g = RTL_PATH.io_outer_power_down_en; \
        force U_IF_NAME.io_ptw_resp_bits_s1_entry_perm_u = RTL_PATH.io_outer_cpu_critical_error; \
        force U_IF_NAME.io_ptw_resp_bits_s1_entry_perm_x = RTL_PATH.io_outer_msi_ack; \
        force U_IF_NAME.io_ptw_resp_bits_s1_entry_perm_w = RTL_PATH.io_inner_beu_errors_icache_ecc_error_valid; \
        force U_IF_NAME.io_ptw_resp_bits_s1_entry_perm_r = RTL_PATH.io_inner_beu_errors_icache_ecc_error_bits; \
        force U_IF_NAME.io_ptw_resp_bits_s1_entry_level = RTL_PATH.io_outer_beu_errors_icache_ecc_error_valid; \
        force U_IF_NAME.io_ptw_resp_bits_s1_entry_v = RTL_PATH.io_outer_beu_errors_icache_ecc_error_bits; \
        force U_IF_NAME.io_ptw_resp_bits_s1_entry_ppn = RTL_PATH.io_reset_backend; \
    end \
    `endif

`endif
