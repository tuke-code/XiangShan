//=========================================================
//File name    : memblock_env.sv
//Author       : OpenAI_Codex
//Module name  : memblock_env
//Discribution : memblock_env : environment top
//Date         : 2026-04-12
//=========================================================
`ifndef MEMBLOCK_ENV__SV
`define MEMBLOCK_ENV__SV

class memblock_env  extends tcnt_env_base;

    memblock_env_cfg cfg;

    backendToTopBypass_agent_agent  u_backendToTopBypass_agent_agent    ;
    uvm_tlm_analysis_fifo #(backendToTopBypass_agent_agent_xaction) backendToTopBypass_agent_mon2rm_fifo;

    fence_agent_agent  u_fence_agent_agent    ;
    uvm_tlm_analysis_fifo #(fence_agent_agent_xaction) fence_agent_mon2rm_fifo;

    csr_ctrl_agent_agent  u_csr_ctrl_agent_agent    ;
    uvm_tlm_analysis_fifo #(csr_ctrl_agent_agent_xaction) csr_ctrl_agent_mon2rm_fifo;

    lsqcommit_agent_agent  u_lsqcommit_agent_agent    ;
    uvm_tlm_analysis_fifo #(lsqcommit_agent_agent_xaction) lsqcommit_agent_mon2rm_fifo;

    lsqenq_agent_agent  u_lsqenq_agent_agent    ;
    uvm_tlm_analysis_fifo #(lsqenq_agent_agent_xaction) lsqenq_agent_mon2rm_fifo;

    lintsissue_agent_agent  u_lintsissue_agent_agent    ;
    uvm_tlm_analysis_fifo #(lintsissue_agent_agent_xaction) lintsissue_agent_mon2rm_fifo;

    vecissue_agent_agent  u_vecissue_agent_agent    ;
    uvm_tlm_analysis_fifo #(vecissue_agent_agent_xaction) vecissue_agent_mon2rm_fifo;

    redirect_agent_agent  u_redirect_agent_agent    ;
    uvm_tlm_analysis_fifo #(redirect_agent_agent_xaction) redirect_agent_mon2rm_fifo;

    sbuffer_agent_agent  u_sbuffer_agent_agent    ;
    uvm_tlm_analysis_fifo #(sbuffer_agent_agent_xaction) sbuffer_agent_mon2rm_fifo;

    dcache_agent_agent  u_dcache_agent_agent    ;
    uvm_tlm_analysis_fifo #(dcache_agent_agent_xaction) dcache_agent_mon2rm_fifo;

    int_sink_agent_agent  u_int_sink_agent_agent    ;
    uvm_tlm_analysis_fifo #(int_sink_agent_agent_xaction) int_sink_agent_mon2rm_fifo;

    itlb_agent_agent  u_itlb_agent_agent    ;
    uvm_tlm_analysis_fifo #(itlb_agent_agent_xaction) itlb_agent_mon2rm_fifo;

    prefetch_agent_agent  u_prefetch_agent_agent    ;
    uvm_tlm_analysis_fifo #(prefetch_agent_agent_xaction) prefetch_agent_mon2rm_fifo;

    io_mem_to_ooo_ctrl_agent_agent  u_io_mem_to_ooo_ctrl_agent_agent    ;
    uvm_tlm_analysis_fifo #(io_mem_to_ooo_ctrl_agent_agent_xaction) io_mem_to_ooo_ctrl_agent_mon2rm_fifo;

    io_mem_to_ooo_int_wb_agent_agent  u_io_mem_to_ooo_int_wb_agent_agent    ;
    uvm_tlm_analysis_fifo #(io_mem_to_ooo_int_wb_agent_agent_xaction) io_mem_to_ooo_int_wb_agent_mon2rm_fifo;

    io_mem_to_ooo_vec_wb_agent_agent  u_io_mem_to_ooo_vec_wb_agent_agent    ;
    uvm_tlm_analysis_fifo #(io_mem_to_ooo_vec_wb_agent_agent_xaction) io_mem_to_ooo_vec_wb_agent_mon2rm_fifo;

    io_mem_to_ooo_wakeup_agent_agent  u_io_mem_to_ooo_wakeup_agent_agent    ;
    uvm_tlm_analysis_fifo #(io_mem_to_ooo_wakeup_agent_agent_xaction) io_mem_to_ooo_wakeup_agent_mon2rm_fifo;

    io_mem_to_ooo_iq_feedback_agent_agent  u_io_mem_to_ooo_iq_feedback_agent_agent    ;
    uvm_tlm_analysis_fifo #(io_mem_to_ooo_iq_feedback_agent_agent_xaction) io_mem_to_ooo_iq_feedback_agent_mon2rm_fifo;

    other_ctrl_agent_agent  u_other_ctrl_agent_agent    ;
    uvm_tlm_analysis_fifo #(other_ctrl_agent_agent_xaction) other_ctrl_agent_mon2rm_fifo;

    uvm_tlm_analysis_fifo #(memblock_common_xaction) rm2scb_exp_fifo;
    uvm_tlm_analysis_fifo #(memblock_common_xaction) rm2scb_act_fifo;

    memblock_rm  rm;
    //aa_test_reg_model	reg_model;
    tcnt_scb_base #(memblock_common_xaction) scb;

    `uvm_component_utils(memblock_env)

    extern         function      new(string name , uvm_component parent);
    extern virtual function void build_phase(uvm_phase phase);
    extern virtual function void connect_phase(uvm_phase phase);
endclass

function memblock_env::new(string name , uvm_component parent);
    super.new(name, parent);
endfunction

function void memblock_env::build_phase(uvm_phase phase);
    super.build_phase(phase);

    if(!uvm_config_db#(memblock_env_cfg)::get(this,"","cfg",this.cfg)) begin
        cfg = memblock_env_cfg::type_id::create("cfg",this);
        void'(this.cfg.randomize());
        `uvm_info(get_type_name(),$sformatf("build_phase: env cfg is not set, create and randomize by self!!!"),UVM_NONE);
    end else begin
        `uvm_info(get_type_name(),$sformatf("build_phase: get_cfg !!!"),UVM_DEBUG);
    end

    backendToTopBypass_agent_mon2rm_fifo = new($sformatf("backendToTopBypass_agent_mon2rm_fifo"),this) ;
    this.u_backendToTopBypass_agent_agent = backendToTopBypass_agent_agent::type_id::create("u_backendToTopBypass_agent_agent",this);
    uvm_config_db#(backendToTopBypass_agent_agent_cfg)::set(this,"u_backendToTopBypass_agent_agent","cfg",this.cfg.u_backendToTopBypass_agent_agent_cfg) ;

    fence_agent_mon2rm_fifo = new($sformatf("fence_agent_mon2rm_fifo"),this) ;
    this.u_fence_agent_agent = fence_agent_agent::type_id::create("u_fence_agent_agent",this);
    uvm_config_db#(fence_agent_agent_cfg)::set(this,"u_fence_agent_agent","cfg",this.cfg.u_fence_agent_agent_cfg) ;

    csr_ctrl_agent_mon2rm_fifo = new($sformatf("csr_ctrl_agent_mon2rm_fifo"),this) ;
    this.u_csr_ctrl_agent_agent = csr_ctrl_agent_agent::type_id::create("u_csr_ctrl_agent_agent",this);
    uvm_config_db#(csr_ctrl_agent_agent_cfg)::set(this,"u_csr_ctrl_agent_agent","cfg",this.cfg.u_csr_ctrl_agent_agent_cfg) ;

    lsqcommit_agent_mon2rm_fifo = new($sformatf("lsqcommit_agent_mon2rm_fifo"),this) ;
    this.u_lsqcommit_agent_agent = lsqcommit_agent_agent::type_id::create("u_lsqcommit_agent_agent",this);
    uvm_config_db#(lsqcommit_agent_agent_cfg)::set(this,"u_lsqcommit_agent_agent","cfg",this.cfg.u_lsqcommit_agent_agent_cfg) ;

    lsqenq_agent_mon2rm_fifo = new($sformatf("lsqenq_agent_mon2rm_fifo"),this) ;
    this.u_lsqenq_agent_agent = lsqenq_agent_agent::type_id::create("u_lsqenq_agent_agent",this);
    uvm_config_db#(lsqenq_agent_agent_cfg)::set(this,"u_lsqenq_agent_agent","cfg",this.cfg.u_lsqenq_agent_agent_cfg) ;

    lintsissue_agent_mon2rm_fifo = new($sformatf("lintsissue_agent_mon2rm_fifo"),this) ;
    this.u_lintsissue_agent_agent = lintsissue_agent_agent::type_id::create("u_lintsissue_agent_agent",this);
    uvm_config_db#(lintsissue_agent_agent_cfg)::set(this,"u_lintsissue_agent_agent","cfg",this.cfg.u_lintsissue_agent_agent_cfg) ;

    vecissue_agent_mon2rm_fifo = new($sformatf("vecissue_agent_mon2rm_fifo"),this) ;
    this.u_vecissue_agent_agent = vecissue_agent_agent::type_id::create("u_vecissue_agent_agent",this);
    uvm_config_db#(vecissue_agent_agent_cfg)::set(this,"u_vecissue_agent_agent","cfg",this.cfg.u_vecissue_agent_agent_cfg) ;

    redirect_agent_mon2rm_fifo = new($sformatf("redirect_agent_mon2rm_fifo"),this) ;
    this.u_redirect_agent_agent = redirect_agent_agent::type_id::create("u_redirect_agent_agent",this);
    uvm_config_db#(redirect_agent_agent_cfg)::set(this,"u_redirect_agent_agent","cfg",this.cfg.u_redirect_agent_agent_cfg) ;

    sbuffer_agent_mon2rm_fifo = new($sformatf("sbuffer_agent_mon2rm_fifo"),this) ;
    this.u_sbuffer_agent_agent = sbuffer_agent_agent::type_id::create("u_sbuffer_agent_agent",this);
    uvm_config_db#(sbuffer_agent_agent_cfg)::set(this,"u_sbuffer_agent_agent","cfg",this.cfg.u_sbuffer_agent_agent_cfg) ;

    dcache_agent_mon2rm_fifo = new($sformatf("dcache_agent_mon2rm_fifo"),this) ;
    this.u_dcache_agent_agent = dcache_agent_agent::type_id::create("u_dcache_agent_agent",this);
    uvm_config_db#(dcache_agent_agent_cfg)::set(this,"u_dcache_agent_agent","cfg",this.cfg.u_dcache_agent_agent_cfg) ;

    int_sink_agent_mon2rm_fifo = new($sformatf("int_sink_agent_mon2rm_fifo"),this) ;
    this.u_int_sink_agent_agent = int_sink_agent_agent::type_id::create("u_int_sink_agent_agent",this);
    uvm_config_db#(int_sink_agent_agent_cfg)::set(this,"u_int_sink_agent_agent","cfg",this.cfg.u_int_sink_agent_agent_cfg) ;

    itlb_agent_mon2rm_fifo = new($sformatf("itlb_agent_mon2rm_fifo"),this) ;
    this.u_itlb_agent_agent = itlb_agent_agent::type_id::create("u_itlb_agent_agent",this);
    uvm_config_db#(itlb_agent_agent_cfg)::set(this,"u_itlb_agent_agent","cfg",this.cfg.u_itlb_agent_agent_cfg) ;

    prefetch_agent_mon2rm_fifo = new($sformatf("prefetch_agent_mon2rm_fifo"),this) ;
    this.u_prefetch_agent_agent = prefetch_agent_agent::type_id::create("u_prefetch_agent_agent",this);
    uvm_config_db#(prefetch_agent_agent_cfg)::set(this,"u_prefetch_agent_agent","cfg",this.cfg.u_prefetch_agent_agent_cfg) ;

    io_mem_to_ooo_ctrl_agent_mon2rm_fifo = new($sformatf("io_mem_to_ooo_ctrl_agent_mon2rm_fifo"),this) ;
    this.u_io_mem_to_ooo_ctrl_agent_agent = io_mem_to_ooo_ctrl_agent_agent::type_id::create("u_io_mem_to_ooo_ctrl_agent_agent",this);
    uvm_config_db#(io_mem_to_ooo_ctrl_agent_agent_cfg)::set(this,"u_io_mem_to_ooo_ctrl_agent_agent","cfg",this.cfg.u_io_mem_to_ooo_ctrl_agent_agent_cfg) ;

    io_mem_to_ooo_int_wb_agent_mon2rm_fifo = new($sformatf("io_mem_to_ooo_int_wb_agent_mon2rm_fifo"),this) ;
    this.u_io_mem_to_ooo_int_wb_agent_agent = io_mem_to_ooo_int_wb_agent_agent::type_id::create("u_io_mem_to_ooo_int_wb_agent_agent",this);
    uvm_config_db#(io_mem_to_ooo_int_wb_agent_agent_cfg)::set(this,"u_io_mem_to_ooo_int_wb_agent_agent","cfg",this.cfg.u_io_mem_to_ooo_int_wb_agent_agent_cfg) ;

    io_mem_to_ooo_vec_wb_agent_mon2rm_fifo = new($sformatf("io_mem_to_ooo_vec_wb_agent_mon2rm_fifo"),this) ;
    this.u_io_mem_to_ooo_vec_wb_agent_agent = io_mem_to_ooo_vec_wb_agent_agent::type_id::create("u_io_mem_to_ooo_vec_wb_agent_agent",this);
    uvm_config_db#(io_mem_to_ooo_vec_wb_agent_agent_cfg)::set(this,"u_io_mem_to_ooo_vec_wb_agent_agent","cfg",this.cfg.u_io_mem_to_ooo_vec_wb_agent_agent_cfg) ;

    io_mem_to_ooo_wakeup_agent_mon2rm_fifo = new($sformatf("io_mem_to_ooo_wakeup_agent_mon2rm_fifo"),this) ;
    this.u_io_mem_to_ooo_wakeup_agent_agent = io_mem_to_ooo_wakeup_agent_agent::type_id::create("u_io_mem_to_ooo_wakeup_agent_agent",this);
    uvm_config_db#(io_mem_to_ooo_wakeup_agent_agent_cfg)::set(this,"u_io_mem_to_ooo_wakeup_agent_agent","cfg",this.cfg.u_io_mem_to_ooo_wakeup_agent_agent_cfg) ;

    io_mem_to_ooo_iq_feedback_agent_mon2rm_fifo = new($sformatf("io_mem_to_ooo_iq_feedback_agent_mon2rm_fifo"),this) ;
    this.u_io_mem_to_ooo_iq_feedback_agent_agent = io_mem_to_ooo_iq_feedback_agent_agent::type_id::create("u_io_mem_to_ooo_iq_feedback_agent_agent",this);
    uvm_config_db#(io_mem_to_ooo_iq_feedback_agent_agent_cfg)::set(this,"u_io_mem_to_ooo_iq_feedback_agent_agent","cfg",this.cfg.u_io_mem_to_ooo_iq_feedback_agent_agent_cfg) ;

    other_ctrl_agent_mon2rm_fifo = new($sformatf("other_ctrl_agent_mon2rm_fifo"),this) ;
    this.u_other_ctrl_agent_agent = other_ctrl_agent_agent::type_id::create("u_other_ctrl_agent_agent",this);
    uvm_config_db#(other_ctrl_agent_agent_cfg)::set(this,"u_other_ctrl_agent_agent","cfg",this.cfg.u_other_ctrl_agent_agent_cfg) ;

    rm2scb_exp_fifo = new($sformatf("rm2scb_exp_fifo"),this) ;
    rm2scb_act_fifo = new($sformatf("rm2scb_act_fifo"),this) ;

    this.rm = memblock_rm::type_id::create("rm", this);
    uvm_config_db#(memblock_env_cfg)::set(this,"rm","cfg",this.cfg) ;
    this.scb = tcnt_scb_base#(memblock_common_xaction)::type_id::create("scb", this);

endfunction

function void memblock_env::connect_phase(uvm_phase phase);
    super.connect_phase(phase);
    //rm.reg_model = this.reg_model;

    this.u_backendToTopBypass_agent_agent.mon_item_port.connect(this.backendToTopBypass_agent_mon2rm_fifo.analysis_export);
    this.rm.backendToTopBypass_agent_mon_item_port.connect(this.backendToTopBypass_agent_mon2rm_fifo.blocking_get_export);

    this.u_fence_agent_agent.mon_item_port.connect(this.fence_agent_mon2rm_fifo.analysis_export);
    this.rm.fence_agent_mon_item_port.connect(this.fence_agent_mon2rm_fifo.blocking_get_export);

    this.u_csr_ctrl_agent_agent.mon_item_port.connect(this.csr_ctrl_agent_mon2rm_fifo.analysis_export);
    this.rm.csr_ctrl_agent_mon_item_port.connect(this.csr_ctrl_agent_mon2rm_fifo.blocking_get_export);

    this.u_lsqcommit_agent_agent.mon_item_port.connect(this.lsqcommit_agent_mon2rm_fifo.analysis_export);
    this.rm.lsqcommit_agent_mon_item_port.connect(this.lsqcommit_agent_mon2rm_fifo.blocking_get_export);

    this.u_lsqenq_agent_agent.mon_item_port.connect(this.lsqenq_agent_mon2rm_fifo.analysis_export);
    this.rm.lsqenq_agent_mon_item_port.connect(this.lsqenq_agent_mon2rm_fifo.blocking_get_export);

    this.u_lintsissue_agent_agent.mon_item_port.connect(this.lintsissue_agent_mon2rm_fifo.analysis_export);
    this.rm.lintsissue_agent_mon_item_port.connect(this.lintsissue_agent_mon2rm_fifo.blocking_get_export);

    this.u_vecissue_agent_agent.mon_item_port.connect(this.vecissue_agent_mon2rm_fifo.analysis_export);
    this.rm.vecissue_agent_mon_item_port.connect(this.vecissue_agent_mon2rm_fifo.blocking_get_export);

    this.u_redirect_agent_agent.mon_item_port.connect(this.redirect_agent_mon2rm_fifo.analysis_export);
    this.rm.redirect_agent_mon_item_port.connect(this.redirect_agent_mon2rm_fifo.blocking_get_export);

    this.u_sbuffer_agent_agent.mon_item_port.connect(this.sbuffer_agent_mon2rm_fifo.analysis_export);
    this.rm.sbuffer_agent_mon_item_port.connect(this.sbuffer_agent_mon2rm_fifo.blocking_get_export);

    this.u_dcache_agent_agent.mon_item_port.connect(this.dcache_agent_mon2rm_fifo.analysis_export);
    this.rm.dcache_agent_mon_item_port.connect(this.dcache_agent_mon2rm_fifo.blocking_get_export);

    this.u_int_sink_agent_agent.mon_item_port.connect(this.int_sink_agent_mon2rm_fifo.analysis_export);
    this.rm.int_sink_agent_mon_item_port.connect(this.int_sink_agent_mon2rm_fifo.blocking_get_export);

    this.u_itlb_agent_agent.mon_item_port.connect(this.itlb_agent_mon2rm_fifo.analysis_export);
    this.rm.itlb_agent_mon_item_port.connect(this.itlb_agent_mon2rm_fifo.blocking_get_export);

    this.u_prefetch_agent_agent.mon_item_port.connect(this.prefetch_agent_mon2rm_fifo.analysis_export);
    this.rm.prefetch_agent_mon_item_port.connect(this.prefetch_agent_mon2rm_fifo.blocking_get_export);

    this.u_io_mem_to_ooo_ctrl_agent_agent.mon_item_port.connect(this.io_mem_to_ooo_ctrl_agent_mon2rm_fifo.analysis_export);
    this.rm.io_mem_to_ooo_ctrl_agent_mon_item_port.connect(this.io_mem_to_ooo_ctrl_agent_mon2rm_fifo.blocking_get_export);

    this.u_io_mem_to_ooo_int_wb_agent_agent.mon_item_port.connect(this.io_mem_to_ooo_int_wb_agent_mon2rm_fifo.analysis_export);
    this.rm.io_mem_to_ooo_int_wb_agent_mon_item_port.connect(this.io_mem_to_ooo_int_wb_agent_mon2rm_fifo.blocking_get_export);

    this.u_io_mem_to_ooo_vec_wb_agent_agent.mon_item_port.connect(this.io_mem_to_ooo_vec_wb_agent_mon2rm_fifo.analysis_export);
    this.rm.io_mem_to_ooo_vec_wb_agent_mon_item_port.connect(this.io_mem_to_ooo_vec_wb_agent_mon2rm_fifo.blocking_get_export);

    this.u_io_mem_to_ooo_wakeup_agent_agent.mon_item_port.connect(this.io_mem_to_ooo_wakeup_agent_mon2rm_fifo.analysis_export);
    this.rm.io_mem_to_ooo_wakeup_agent_mon_item_port.connect(this.io_mem_to_ooo_wakeup_agent_mon2rm_fifo.blocking_get_export);

    this.u_io_mem_to_ooo_iq_feedback_agent_agent.mon_item_port.connect(this.io_mem_to_ooo_iq_feedback_agent_mon2rm_fifo.analysis_export);
    this.rm.io_mem_to_ooo_iq_feedback_agent_mon_item_port.connect(this.io_mem_to_ooo_iq_feedback_agent_mon2rm_fifo.blocking_get_export);

    this.u_other_ctrl_agent_agent.mon_item_port.connect(this.other_ctrl_agent_mon2rm_fifo.analysis_export);
    this.rm.other_ctrl_agent_mon_item_port.connect(this.other_ctrl_agent_mon2rm_fifo.blocking_get_export);

    this.rm.rm_item_exp_port.connect(this.rm2scb_exp_fifo.analysis_export);
    this.scb.exp_port.connect(this.rm2scb_exp_fifo.blocking_get_export);
    this.rm.rm_item_act_port.connect(this.rm2scb_act_fifo.analysis_export);
    this.scb.act_port.connect(this.rm2scb_act_fifo.blocking_get_export);

endfunction

`endif

