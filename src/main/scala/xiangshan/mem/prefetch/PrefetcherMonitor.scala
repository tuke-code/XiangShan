package xiangshan.mem.prefetch

import org.chipsalliance.cde.config.Parameters
import chisel3._
import chisel3.util._
import utils._
import utility._
import xiangshan._
import xiangshan.cache.HasDCacheParameters
import xiangshan.mem.L1PrefetchReq
import xiangshan.mem.Bundles.LsPrefetchTrainBundle
import xiangshan.mem.HasL1PrefetchSourceParameter
import xiangshan.backend.rob.RobDebugRollingIO

class PrefetchControlBundle()(implicit p: Parameters) extends XSBundle with HasStreamPrefetchHelper {
  val l1_depth = UInt(DEPTH_BITS.W)
  val l2_depth = UInt(DEPTH_BITS.W)
  val flush = Bool()
  val enable = Bool()
  val confidence = UInt(1.W)
}

class LoadPrefetchStatBundle()(implicit p: Parameters) extends XSBundle with HasL1PrefetchSourceParameter {
  val total_prefetch = Bool() // from loadpipe s2, pf req sent
  val pf_late_in_cache = Bool() // from loadpipe s2, pf req sent but hit
  val nack_prefetch = Bool() // from loadpipe s2, pf req miss but nack
  val pf_source = UInt(L1PfSourceBits.W)

  val hit_pf_in_cache = Bool() // from loadpipe s3, pf block hit by demand, clear pf flag
  val hit_source = UInt(L1PfSourceBits.W)

  val demand_miss = Bool() // from loadpipe s2, demand req miss
  val pollution = Bool() // from loadpipe s2, bloom filter speculate pollution
}

class MainPrefetchStatBundle()(implicit p: Parameters) extends XSBundle with HasL1PrefetchSourceParameter {
  val pf_useless = Bool() // from mainpipe replace, prefetch block but not accessed
  val pf_source_useless = UInt(L1PfSourceBits.W)

  val hit_pf_in_cache = Bool() // from mainpipe, refill accessed pf block | store req hit pf block
  val hit_pf_source_in_cache = UInt(L1PfSourceBits.W)
}

class MissPrefetchStatBundle()(implicit p: Parameters) extends XSBundle with HasL1PrefetchSourceParameter with HasDCacheParameters {
  val pf_late_in_mshr = Bool() // from missqueue, pf req match a existing mshr
  val prefetch_miss = Bool() // from missqueue, pf req allocate a new mshr
  val pf_source = UInt(L1PfSourceBits.W)

  val hit_pf_in_mshr = Bool() // from missqueue, demand miss match a existing pf mshr, then clear pf flag
  val hit_pf_source_in_mshr = UInt(L1PfSourceBits.W) // from missqueue, the pf source of demand miss matched
  val load_miss = Bool() // from missqueue, load demand miss allocate a new mshr

  val refill_valid = Bool()
  val refill_latency = UInt(LATENCY_WIDTH.W)
  val refill_hit = Bool()
}

class RefillPrefetchStatDB(implicit p: Parameters) extends XSBundle with HasDCacheParameters {
  val refill_valid = Bool()
  val refill_latency = UInt(LATENCY_WIDTH.W)
  val refill_hit = Bool()
}

class PrefetcherMonitorBundle()(implicit p: Parameters) extends XSBundle with HasL1PrefetchSourceParameter {
  val loadinfo = Input(Vec(LoadPipelineWidth, new LoadPrefetchStatBundle))
  val missinfo = Input(new MissPrefetchStatBundle)
  val maininfo = Input(new MainPrefetchStatBundle)

  val clear_flag = Input(Vec(LoadPipelineWidth, Bool()))

  val pf_ctrl = Output(Vec(L1PrefetcherNum, new PrefetchControlBundle))

  val debugRolling = Flipped(new RobDebugRollingIO)
}

class PrefetcherMonitor()(implicit p: Parameters) extends XSModule with HasStreamPrefetchHelper {
  val io = IO(new PrefetcherMonitorBundle)

  val prefetch_info = Wire(new L1PrefetchStatisticBundle)
  prefetch_info.loadinfo := io.loadinfo 
  prefetch_info.missinfo := io.missinfo
  prefetch_info.maininfo := io.maininfo

  for (i <- 0 until LoadPipelineWidth) {
    when(io.clear_flag(i)) {
      prefetch_info.loadinfo(i).hit_pf_in_cache := false.B
    }
  }

  val StreamMonitor = Module(new L1PrefetchMonitor(PrefetcherMonitorParam.fromString("stream")))
  val StrideMonitor = Module(new L1PrefetchMonitor(PrefetcherMonitorParam.fromString("stride")))

  StreamMonitor.io.prefetch_info:= prefetch_info
  StrideMonitor.io.prefetch_info := prefetch_info
  
  // stream 0, stride 1
  io.pf_ctrl(0) := StreamMonitor.io.pf_ctrl
  io.pf_ctrl(1) := StrideMonitor.io.pf_ctrl

  // ldu 0, 1, 2 can only have one prefetch request at a time
  val total_prefetch = io.loadinfo.map(t => t.total_prefetch).reduce(_ || _)
  val nack_prefetch_raw = io.loadinfo.map(t => t.nack_prefetch).reduce(_ || _)
  val pf_late_in_cache = io.loadinfo.map(t => t.pf_late_in_cache).reduce(_ || _)
  val pf_late_in_mshr = io.missinfo.pf_late_in_mshr
  val nack_prefetch = nack_prefetch_raw && !pf_late_in_mshr
  val pf_late = pf_late_in_cache.asUInt + pf_late_in_mshr.asUInt
  // demand accesses from different ldu may hit different prefetch blocks
  val hit_pf_in_cache = PopCount(prefetch_info.loadinfo.map(t => t.hit_pf_in_cache) ++ Seq(prefetch_info.maininfo.hit_pf_in_cache))
  val hit_pf_in_mshr = io.missinfo.hit_pf_in_mshr
  val hit_pf = hit_pf_in_cache + hit_pf_in_mshr.asUInt
  val pf_useless = io.maininfo.pf_useless
  val prefetch_miss = io.missinfo.prefetch_miss
  val load_miss_to_mshr = io.missinfo.load_miss
  // ldu 0, 1, 2 can have multiple demand accesses at a time
  val demand_miss_in_ldu = PopCount(io.loadinfo.map(t => t.demand_miss))
  val pollution = PopCount(io.loadinfo.map(t => t.pollution))

  val refill_prefetch_stat_db = Wire(new RefillPrefetchStatDB)
  refill_prefetch_stat_db.refill_valid := io.missinfo.refill_valid
  refill_prefetch_stat_db.refill_latency := io.missinfo.refill_latency
  refill_prefetch_stat_db.refill_hit := io.missinfo.refill_hit

  val refill_prefetch_stat_table = ChiselDB.createTable(
    s"L1RefillPrefetchStat_hart${p(XSCoreParamsKey).HartId}",
    new RefillPrefetchStatDB,
    basicDB = true
  )
  refill_prefetch_stat_table.log(refill_prefetch_stat_db, io.missinfo.refill_valid, "PrefetcherMonitor", clock, reset)
  
  XSPerfAccumulate("l1DemandMiss", demand_miss_in_ldu)
  XSPerfAccumulate("l1prefetchSent", total_prefetch)
  XSPerfAccumulate("l1prefetchHit", hit_pf)
  XSPerfAccumulate("l1prefetchHitInCache", hit_pf_in_cache)
  XSPerfAccumulate("l1prefetchHitInMSHR", hit_pf_in_mshr)
  XSPerfAccumulate("l1prefetchLate", pf_late)
  XSPerfAccumulate("l1prefetchLateInCache", pf_late_in_cache)
  XSPerfAccumulate("l1prefetchLateInMSHR", pf_late_in_mshr)
  XSPerfAccumulate("l1prefetchUseless", pf_useless)
  XSPerfAccumulate("l1prefetchDropByNack", nack_prefetch)
  XSPerfAccumulate("mshr_count_Prefetch", prefetch_miss)
  XSPerfAccumulate("mshr_count_CPU", load_miss_to_mshr)
  XSPerfAccumulate("cache_pollution", pollution)

  // rolling by instr
  XSPerfRolling(
    "L1PrefetchAccuracyIns",
    hit_pf_in_cache, total_prefetch,
    1000, io.debugRolling.robTrueCommit, clock, reset
  )
  
  XSPerfRolling(
    "L1PrefetchLatenessIns",
    hit_pf_in_mshr, prefetch_miss,
    1000, io.debugRolling.robTrueCommit, clock, reset
  )

  XSPerfRolling(
    "L1PrefetchPollutionIns",
    pollution, demand_miss_in_ldu,
    1000, io.debugRolling.robTrueCommit, clock, reset
  )

  XSPerfRolling(
    "IPCIns",
    io.debugRolling.robTrueCommit, 1.U,
    1000, io.debugRolling.robTrueCommit, clock, reset
  )
}

class L1PrefetchStatisticBundle()(implicit p: Parameters) extends XSBundle {
  val loadinfo = Vec(LoadPipelineWidth, new LoadPrefetchStatBundle)
  val missinfo = new MissPrefetchStatBundle
  val maininfo = new MainPrefetchStatBundle
}

class L1PrefetchMonitorBundle()(implicit p: Parameters) extends XSBundle {
  val prefetch_info = Input(new L1PrefetchStatisticBundle)

  val pf_ctrl = Output(new PrefetchControlBundle)
}

class L1PrefetchMonitor(param : PrefetcherMonitorParam)(implicit p: Parameters) extends XSModule with HasStreamPrefetchHelper {
  val io = IO(new L1PrefetchMonitorBundle)

  val l1_depth = Reg(UInt(DEPTH_BITS.W))
  val l2_depth = Reg(UInt(DEPTH_BITS.W))
  val flush = RegInit(false.B)
  val enable = RegInit(true.B)
  val confidence = RegInit(param.confidence.U(1.W))

  // TODO: mshr number
  // mshr full && load miss && load send mshr req && !load match,  -> decr nmax prefetch
  // mshr free

  val l1_depth_const = Wire(UInt(DEPTH_BITS.W))
  l1_depth_const := Constantin.createRecord(s"${param.name}_l1depth${p(XSCoreParamsKey).HartId}", initValue = 64)
  val l2_depth_const = Wire(UInt(DEPTH_BITS.W))
  l2_depth_const := Constantin.createRecord(s"${param.name}_l2depth${p(XSCoreParamsKey).HartId}", initValue = 512)

  io.pf_ctrl.l1_depth := l1_depth
  io.pf_ctrl.l2_depth := l2_depth
  io.pf_ctrl.flush := flush
  io.pf_ctrl.enable := enable
  io.pf_ctrl.confidence := confidence

  val hit_pf_in_cache_cnt = RegInit(0.U((log2Up(param.VALIDITY_CHECK_INTERVAL) + 1).W))
  val pf_useless_cnt = RegInit(0.U((log2Up(param.VALIDITY_CHECK_INTERVAL) + 1).W))
  val back_off_cnt = RegInit(0.U((log2Up(param.BACK_OFF_INTERVAL) + 1).W))
  val low_conf_cnt = RegInit(0.U((log2Up(param.LOW_CONF_INTERVAL) + 1).W))

  val validity_reset = (hit_pf_in_cache_cnt + pf_useless_cnt) >= param.VALIDITY_CHECK_INTERVAL.U
  val back_off_reset = back_off_cnt === param.BACK_OFF_INTERVAL.U
  val conf_reset = low_conf_cnt === param.LOW_CONF_INTERVAL.U
  val trigger_disable = validity_reset && (pf_useless_cnt >= param.DISABLE_THRESHOLD.U)

  val total_prefetch = io.prefetch_info.loadinfo.map(t => t.total_prefetch && param.isMyType(t.pf_source)).reduce(_ || _)
  val pf_late_in_cache = io.prefetch_info.loadinfo.map(t => t.pf_late_in_cache && param.isMyType(t.pf_source)).reduce(_ || _)
  val nack_prefetch_raw = io.prefetch_info.loadinfo.map(t => t.nack_prefetch && param.isMyType(t.pf_source)).reduce(_ || _)
  val pf_late_in_mshr = io.prefetch_info.missinfo.pf_late_in_mshr && param.isMyType(io.prefetch_info.missinfo.pf_source)
  val nack_prefetch = nack_prefetch_raw && !pf_late_in_mshr
  val hit_pf_in_cache = PopCount(io.prefetch_info.loadinfo.map(t => t.hit_pf_in_cache && param.isMyType(t.hit_source)) ++ Seq(io.prefetch_info.maininfo.hit_pf_in_cache && param.isMyType(io.prefetch_info.maininfo.hit_pf_source_in_cache)))
  val pf_useless = io.prefetch_info.maininfo.pf_useless && param.isMyType(io.prefetch_info.maininfo.pf_source_useless)
  val prefetch_miss = io.prefetch_info.missinfo.prefetch_miss && param.isMyType(io.prefetch_info.missinfo.pf_source)
  val hit_pf_in_mshr = io.prefetch_info.missinfo.hit_pf_in_mshr && param.isMyType(io.prefetch_info.missinfo.hit_pf_source_in_mshr)
  val hit_pf = hit_pf_in_cache + hit_pf_in_mshr.asUInt
  val pf_late = pf_late_in_cache.asUInt + pf_late_in_mshr.asUInt

  hit_pf_in_cache_cnt := Mux(validity_reset, 0.U, hit_pf_in_cache_cnt + hit_pf_in_cache)
  pf_useless_cnt := Mux(validity_reset, 0.U, pf_useless_cnt + pf_useless)
  back_off_cnt := Mux(back_off_reset, 0.U, back_off_cnt + !enable)
  low_conf_cnt := Mux(conf_reset, 0.U, low_conf_cnt + !confidence.asBool)

  flush := Mux(flush, false.B, flush)
  enable := Mux(back_off_reset, true.B, enable)
  confidence := Mux(conf_reset, 1.U(1.W), confidence)

  when(trigger_disable) {
    confidence := 0.U(1.W)
    enable := false.B
    flush := true.B
  }

  // Moving-average refill latency. MissQueue encodes overflow as latency 0,
  // so ignore zero-latency samples instead of learning them as fast refills.
  val refill_l2hit_latency = RegInit(160.U(LATENCY_WIDTH.W))
  val refill_l2miss_latency = RegInit(1200.U(LATENCY_WIDTH.W))

  val refill_sample_valid = io.prefetch_info.missinfo.refill_valid && io.prefetch_info.missinfo.refill_latency =/= 0.U
  val refill_valid_s1 = RegNext(refill_sample_valid, false.B)
  val refill_latency_s1 = RegEnable(io.prefetch_info.missinfo.refill_latency, 0.U, refill_sample_valid)
  val refill_hit_s1 = RegEnable(io.prefetch_info.missinfo.refill_hit, false.B, refill_sample_valid)

  val ema_avg_s1 = Mux(refill_hit_s1, refill_l2hit_latency, refill_l2miss_latency)
  val ema_incr_s1 = refill_latency_s1 >= ema_avg_s1
  val ema_delta_s1 = Mux(ema_incr_s1, refill_latency_s1 - ema_avg_s1, ema_avg_s1 - refill_latency_s1)
  val ema_step_s1 = ema_delta_s1 >> 3

  val ema_valid_s2 = RegNext(refill_valid_s1, false.B)
  val ema_hit_s2 = RegEnable(refill_hit_s1, false.B, refill_valid_s1)
  val ema_incr_s2 = RegEnable(ema_incr_s1, false.B, refill_valid_s1)
  val ema_step_s2 = RegEnable(ema_step_s1, 0.U, refill_valid_s1)

  val refill_l2hit_latency_next = Mux(ema_incr_s2, refill_l2hit_latency + ema_step_s2, refill_l2hit_latency - ema_step_s2)
  val refill_l2miss_latency_next = Mux(ema_incr_s2, refill_l2miss_latency + ema_step_s2, refill_l2miss_latency - ema_step_s2)

  when(ema_valid_s2 && ema_hit_s2) {
    refill_l2hit_latency := refill_l2hit_latency_next
  }
  when(ema_valid_s2 && !ema_hit_s2) {
    refill_l2miss_latency := refill_l2miss_latency_next
  }
  val refill_latency_updated = ema_valid_s2

  def ceilMapDepth(target: UInt, table: Seq[Int]): UInt = {
    table.foldRight(table.last.U(DEPTH_BITS.W)) { case (depth, mapped) =>
      Mux(target <= depth.U, depth.U(DEPTH_BITS.W), mapped)
    }
  }

  // D1 = ceil(3 * (10 + l2hit_latency) / 8)
  // D2 = ceil(3 * (5 + l2hit_latency + l2miss_latency) / 8)
  val depth_calc_en_s0 = refill_latency_updated
  val depth_hit_latency_s0 = RegEnable(refill_l2hit_latency, 160.U, depth_calc_en_s0)
  val depth_miss_latency_s0 = RegEnable(refill_l2miss_latency, 1200.U, depth_calc_en_s0)
  val depth_calc_en_s1 = RegNext(depth_calc_en_s0, false.B)

  val depth_hit_x2_s1 = RegEnable(depth_hit_latency_s0 << 1, 0.U, depth_calc_en_s1)
  val depth_miss_x2_s1 = RegEnable(depth_miss_latency_s0 << 1, 0.U, depth_calc_en_s1)
  val depth_hit_latency_s1 = RegEnable(depth_hit_latency_s0, 0.U, depth_calc_en_s1)
  val depth_miss_latency_s1 = RegEnable(depth_miss_latency_s0, 0.U, depth_calc_en_s1)
  val depth_calc_en_s2 = RegNext(depth_calc_en_s1, false.B)

  val depth_hit_x3_s2 = RegEnable(depth_hit_x2_s1 + depth_hit_latency_s1, 0.U, depth_calc_en_s2)
  val depth_miss_x3_s2 = RegEnable(depth_miss_x2_s1 + depth_miss_latency_s1, 0.U, depth_calc_en_s2)
  val depth_calc_en_s3 = RegNext(depth_calc_en_s2, false.B)

  val l1_depth_sum_s3 = RegEnable(depth_hit_x3_s2 + 37.U, 0.U, depth_calc_en_s3)
  val l2_depth_partial_s3 = RegEnable(depth_hit_x3_s2 + depth_miss_x3_s2, 0.U, depth_calc_en_s3)
  val depth_calc_en_s4 = RegNext(depth_calc_en_s3, false.B)

  val l1_depth_sum_s4 = RegEnable(l1_depth_sum_s3, 0.U, depth_calc_en_s4)
  val l2_depth_sum_s4 = RegEnable(l2_depth_partial_s3 + 22.U, 0.U, depth_calc_en_s4)
  val depth_calc_en_s5 = RegNext(depth_calc_en_s4, false.B)

  val l1_depth_target = RegEnable(l1_depth_sum_s4 >> 3, 64.U, depth_calc_en_s5)
  val l2_depth_target = RegEnable(l2_depth_sum_s4 >> 3, 512.U, depth_calc_en_s5)

  val depth_update_en = RegNext(depth_calc_en_s5, false.B)
  val l1_depth_dynamic = RegEnable(ceilMapDepth(l1_depth_target, Seq(4, 8, 16, 24, 32, 48, 64)), 64.U, depth_update_en)
  val l2_depth_dynamic = RegEnable(ceilMapDepth(l2_depth_target, Seq(32, 64, 128, 192, 256, 384, 512)), 512.U, depth_update_en)
  val depth_write_en = RegNext(depth_update_en, false.B)

  val enableDynamicPrefetcher_const = Constantin.createRecord(s"${param.name}_enableDynamicPrefetcher${p(XSCoreParamsKey).HartId}", initValue = 1)
  val enableDynamicPrefetcher = (enableDynamicPrefetcher_const === 1.U)

  when(!enableDynamicPrefetcher) {
    l1_depth := l1_depth_const
    l2_depth := l2_depth_const
    flush := false.B
    enable := true.B
    confidence := 1.U
  }.elsewhen(depth_write_en) {
    l1_depth := l1_depth_dynamic
    l2_depth := l2_depth_dynamic
  }

  when(reset.asBool) {
    l1_depth := l1_depth_const
    l2_depth := l2_depth_const
  }

  XSPerfAccumulate(s"l1prefetchSent${param.name}", total_prefetch)
  XSPerfAccumulate(s"l1prefetchHit${param.name}", hit_pf)
  XSPerfAccumulate(s"l1prefetchHitInCache${param.name}", hit_pf_in_cache)
  XSPerfAccumulate(s"l1prefetchHitInMSHR${param.name}", hit_pf_in_mshr)
  XSPerfAccumulate(s"l1prefetchLate${param.name}", pf_late)
  XSPerfAccumulate(s"l1prefetchLateInCache${param.name}", pf_late_in_cache)
  XSPerfAccumulate(s"l1prefetchLateInMSHR${param.name}", pf_late_in_mshr)
  XSPerfAccumulate(s"l1prefetchUseless${param.name}", pf_useless)
  XSPerfAccumulate(s"l1prefetchDropByNack${param.name}", nack_prefetch)
  XSPerfAccumulate(s"mshr_count_Prefetch${param.name}", prefetch_miss)
  for(t <- Seq(4, 8, 16, 24, 32, 48, 64)) {
    XSPerfAccumulate(s"${param.name}_l1depth${t}", l1_depth === t.U)
  }
  for(t <- Seq(32, 64, 128, 192, 256, 384, 512)) {
    XSPerfAccumulate(s"${param.name}_l2depth${t}", l2_depth === t.U)
  }
  XSPerfAccumulate(s"${param.name}_trigger_disable", trigger_disable)
  XSPerfAccumulate(s"${param.name}_disable_time", !enable)
}

abstract class PrefetcherMonitorParam {
  val name: String
  def isMyType(value: UInt): Bool

  val VALIDITY_CHECK_INTERVAL = 1000
  val BACK_OFF_INTERVAL = 100000
  val LOW_CONF_INTERVAL = 200000

  val DISABLE_THRESHOLD = 900

  val confidence = 1
}

object PrefetcherMonitorParam {
  def fromString(s: String): PrefetcherMonitorParam = s.toLowerCase match {
    case "stream" => new StreamMonitorParam()
    case "stride" => new StrideMonitorParam()
    case t => throw new IllegalArgumentException(s"unknown Prefetcher type $t")
  }
}

class StreamMonitorParam extends PrefetcherMonitorParam with HasL1PrefetchSourceParameter {
  override val name: String = "Stream"
  override def isMyType(value: UInt) = value === L1_HW_PREFETCH_STREAM
}

class StrideMonitorParam extends PrefetcherMonitorParam with HasL1PrefetchSourceParameter {
  override val name: String = "Stride"
  override def isMyType(value: UInt) = value === L1_HW_PREFETCH_STRIDE

  override val VALIDITY_CHECK_INTERVAL = 800
  override val DISABLE_THRESHOLD = 700
}
