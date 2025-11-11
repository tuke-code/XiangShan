package xiangshan.cache.dcache

import chisel3._
import chisel3.util._
import chisel3.util.random.LFSR
import freechips.rocketchip.util._
import freechips.rocketchip.util.property.cover
import org.chipsalliance.cde.config.Parameters
import xiangshan.cache.DCacheModule
import xiangshan.cache.DCacheBundle
import xiangshan.CSROpType.seti
import freechips.rocketchip.regmapper.RRTest0Map.de
import utility.{XSPerfAccumulate, XSPerfHistogram}

class PolicySelector(implicit p: Parameters) extends DCacheModule {
  val in = IO(Flipped(Vec(4, Valid(new Bundle {
    val belong_a = Bool()
    val belong_b = Bool()
    val hit = Bool()
    val miss = Bool()
  }))))
  val debug_in_repl = IO(Input(Bool()))
  val out = IO(new Bundle {
    val select_a = Bool()
  })
  // saturation counter
  val sat_cnt_bits = 10
  val sat_cnt_median = (1 << (sat_cnt_bits - 1)).U(sat_cnt_bits.W)
  // a hit + 1, a miss + 1
  // when hit > median and miss < median, choose a
  val satHit = RegInit(sat_cnt_median)
  val satMiss = RegInit(sat_cnt_median - 1.U)

  // period counter
  val period_cnt_bits = 15
  val clearCnt = RegInit(1.U(period_cnt_bits.W))
  val prdAHit, prdBHit, prdAMiss, prdBMiss = RegInit(VecInit(Seq.fill(2)(1.U(period_cnt_bits.W))))
  val ptr = RegInit(false.B)

  clearCnt := clearCnt + 1.U

  val aHit = PopCount(in.map{ case req => req.valid && req.bits.belong_a && req.bits.hit })
  val bHit = PopCount(in.map{ case req => req.valid && req.bits.belong_b && req.bits.hit })
  val aMiss = PopCount(in.map{ case req => req.valid && req.bits.belong_a && req.bits.miss })
  val bMiss = PopCount(in.map{ case req => req.valid && req.bits.belong_b && req.bits.miss })

  val updatePtr = clearCnt === 0.U ||
    aHit =/= 0.U && prdAHit(ptr)(period_cnt_bits - 1, 2).andR ||
    bHit =/= 0.U && prdBHit(ptr)(period_cnt_bits - 1, 2).andR ||
    aMiss =/= 0.U && prdAMiss(ptr)(period_cnt_bits - 1, 2).andR ||
    bMiss =/= 0.U && prdBMiss(ptr)(period_cnt_bits - 1, 2).andR

  when (updatePtr) {
    def shrink_cnt(cnt: UInt): Unit = cnt := (cnt >> (cnt.getWidth / 2)).max(1.U)
    shrink_cnt(prdAHit(ptr))
    shrink_cnt(prdAMiss(ptr))
    shrink_cnt(prdBHit(ptr))
    shrink_cnt(prdBMiss(ptr))
    prdAHit(!ptr) := aHit.max(1.U)
    prdAMiss(!ptr) := aMiss.max(1.U)
    prdBHit(!ptr) := bHit.max(1.U)
    prdBMiss(!ptr) := bMiss.max(1.U)
    ptr := !ptr
  }.otherwise {
    when (aHit =/= 0.U) { prdAHit(ptr) := prdAHit(ptr) + aHit }
    when (bHit =/= 0.U) { prdBHit(ptr) := prdBHit(ptr) + bHit }
    when (aMiss =/= 0.U) { prdAMiss(ptr) := prdAMiss(ptr) + aMiss }
    when (bMiss =/= 0.U) { prdBMiss(ptr) := prdBMiss(ptr) + bMiss }
  }

  satHit := satHit + Mux(satHit(sat_cnt_bits - 1, 2).andR, 0.U, aHit) -
    Mux(satHit(sat_cnt_bits - 1, 2) === 0.U, 0.U, bHit)
  satMiss := satMiss + Mux(satMiss(sat_cnt_bits - 1, 2).andR, 0.U, aMiss) -
    Mux(satMiss(sat_cnt_bits - 1, 2) === 0.U, 0.U, bMiss)

  val hitRatioSelectA = (prdAHit(ptr) + prdAHit(!ptr)) * (prdBMiss(ptr) + prdBMiss(!ptr)) >= 
    (prdBHit(ptr) + prdBHit(!ptr)) * (prdAMiss(ptr) + prdAMiss(!ptr))

  out.select_a := MuxCase(hitRatioSelectA, Seq(
      (satHit(sat_cnt_bits - 1) && !satMiss(sat_cnt_bits - 1)) -> true.B,
      (!satHit(sat_cnt_bits - 1) && satMiss(sat_cnt_bits - 1)) -> false.B
    ))

  val inRepl = debug_in_repl
  XSPerfHistogram("satHit_inrepl", satHit, inRepl, 0, 1 << sat_cnt_bits - 1, 1 << (sat_cnt_bits - 4))
  XSPerfHistogram("satMiss_inrepl", satMiss, inRepl, 0, 1 << sat_cnt_bits - 1, 1 << (sat_cnt_bits - 4))
  XSPerfHistogram("satHit_intotal", satHit, true.B, 0, 1 << sat_cnt_bits - 1, 1 << (sat_cnt_bits - 4))
  XSPerfHistogram("satMiss_intotal", satMiss, true.B, 0, 1 << sat_cnt_bits - 1, 1 << (sat_cnt_bits - 4))
  val aHitRatio = (100.U * (prdAHit(ptr) + prdAHit(!ptr))) /
    (prdAHit(ptr) + prdAHit(!ptr) + prdAMiss(ptr) + prdAMiss(!ptr))
  val bHitRatio = (100.U * (prdBHit(ptr) + prdBHit(!ptr))) /
    (prdBHit(ptr) + prdBHit(!ptr) + prdBMiss(ptr) + prdBMiss(!ptr))
  XSPerfHistogram("a_hit_ratio_inrepl", aHitRatio, inRepl, 0, 100, 5)
  XSPerfHistogram("b_hit_ratio_inrepl", bHitRatio, inRepl, 0, 100, 5)
  XSPerfHistogram("a_hit_ratio_intotal", aHitRatio, true.B, 0, 100, 5)
  XSPerfHistogram("b_hit_ratio_intotal", bHitRatio, true.B, 0, 100, 5)
  XSPerfHistogram("a_sub_b_ratio_inrepl", Mux(aHitRatio > bHitRatio, aHitRatio - bHitRatio, 0.U), inRepl, 0, 100, 5)
  XSPerfHistogram("a_sub_b_ratio_intotal", Mux(aHitRatio > bHitRatio, aHitRatio - bHitRatio, 0.U), true.B, 0, 100, 5)
  XSPerfHistogram("b_sub_a_ratio_inrepl", Mux(bHitRatio > aHitRatio, bHitRatio - aHitRatio, 0.U), inRepl, 0, 100, 5)
  XSPerfHistogram("b_sub_a_ratio_intotal", Mux(bHitRatio > aHitRatio, bHitRatio - aHitRatio, 0.U), true.B, 0, 100, 5)
  XSPerfAccumulate("select_a_use_sat", satHit(sat_cnt_bits - 1) && !satMiss(sat_cnt_bits - 1) && inRepl)
  XSPerfAccumulate("select_b_use_sat", !satHit(sat_cnt_bits - 1) && satMiss(sat_cnt_bits - 1) && inRepl)
  XSPerfAccumulate("select_a_use_period", (satHit(sat_cnt_bits - 1) === satMiss(sat_cnt_bits - 1)) && hitRatioSelectA && inRepl)
  XSPerfAccumulate("select_a_use_period", (satHit(sat_cnt_bits - 1) === satMiss(sat_cnt_bits - 1)) && !hitRatioSelectA && inRepl)
  XSPerfAccumulate("select_change_intotal", out.select_a && !RegNext(out.select_a))
  XSPerfAccumulate("select_change_by_ratio_intotal", hitRatioSelectA && !RegNext(hitRatioSelectA))
}

class LduAccess(implicit p: Parameters) extends DCacheBundle {
  val set = UInt(idxBits.W)
  val way = UInt(wayBits.W)
  val hit = Bool()
  val hwPft = Bool()
  val softPft = Bool()
  def isHit = hit
  def isMiss = !hit
  def nonPft = !hwPft && !softPft
}

class MPAccess(implicit p: Parameters) extends LduAccess {
  val repl = Bool() // if repl is true, ignore hit
  override def isHit = !repl && hit
  override def isMiss = !repl && !hit
  def isRepl = repl
}

class ReplReq(implicit p: Parameters) extends DCacheBundle {
  val set = UInt(idxBits.W)
}

class ReplResp(implicit p: Parameters) extends DCacheBundle {
  val way = UInt(wayBits.W)
}

class DIP(debug_mode: Boolean = false)(implicit p: Parameters) extends DCacheModule {
  val n_ways = cacheParams.nWays
  val n_sets = cacheParams.nSets
  val dedicated_sets_num = 16
  val state_bits = n_ways - 1
  val bipcnt_bits = 5

  val in = IO(Input(new Bundle {
    val lduAccess = Vec(LoadPipelineWidth, ValidIO(new LduAccess))
    val mpAccess = ValidIO(new MPAccess)
    val replReq = ValidIO(new ReplReq)
  }))

  val out = IO(Output(new Bundle{
    val replResp = new ReplResp
  }))

  // for unit test
  val debug = if (debug_mode) Some(IO(new Bundle {
    val w = Flipped(new Bundle {
      val bip_cnt = Valid(UInt(bipcnt_bits.W))
      val state = Valid(new Bundle {
        val set = UInt(idxBits.W)
        val value = UInt(state_bits.W)
      })
    })
    val r = new Bundle {
      val bip_cnt = UInt(bipcnt_bits.W)
      val state = new Bundle {
        val set = Input(UInt(idxBits.W))
        val value = UInt(state_bits.W)
      }
    }
  })) else None

  val state_vec = if (state_bits == 0) Reg(Vec(n_sets, UInt(0.W))) else RegInit(VecInit(Seq.fill(n_sets)(0.U(state_bits.W))))
  val bip_cnt = RegInit(0.U(bipcnt_bits.W))
  val psel = Module(new PolicySelector)

  private val insertLRU = 1.U(1.W)
  private val insertMRU = 0.U(1.W)

  //        1
  //      /
  //    1       0
  //   /         \
  //  1   0   1   0
  // /     \ /     \
  // 7 6 5 4 3 2 1 0: way
  def get_next_state(state: UInt, touch_way: UInt, insert_mode: UInt, tree_nways: Int, is_mainpipe: Option[Boolean]): UInt = {
    require(insert_mode.getWidth == 1)
    if (tree_nways > 2) {
      // we are at a branching node in the tree, so recurse
      val right_nways: Int = 1 << (log2Ceil(tree_nways) - 1)  // number of ways in the right sub-tree
      val left_nways:  Int = tree_nways - right_nways         // number of ways in the left sub-tree
      val msb = touch_way(log2Ceil(tree_nways)-1)
      val access_right        = !msb
      val set_left_older      = is_mainpipe match {
        case Some(true) => Mux(insert_mode === insertMRU, !msb, msb)
        case None | Some(false) => !msb
      }
      val left_subtree_state  = state.extract(tree_nways-3, right_nways-1)
      val right_subtree_state = state(right_nways-2, 0)

      if (left_nways > 1) {
        // we are at a branching node in the tree with both left and right sub-trees, so recurse both sub-trees
        Cat(set_left_older,
            Mux(access_right,
                left_subtree_state,  // if setting left sub-tree as older, do NOT recurse into left sub-tree
                get_next_state(left_subtree_state, touch_way.extract(log2Ceil(left_nways)-1,0), insert_mode, left_nways, is_mainpipe)),  // recurse left if newer
            Mux(access_right,
                get_next_state(right_subtree_state, touch_way(log2Ceil(right_nways)-1,0), insert_mode, right_nways, is_mainpipe),  // recurse right if newer
                right_subtree_state))  // if setting right sub-tree as older, do NOT recurse into right sub-tree
      } else {
        // we are at a branching node in the tree with only a right sub-tree, so recurse only right sub-tree
        assert(false, "unchecked path")
        Cat(set_left_older,
            Mux(access_right,
                get_next_state(right_subtree_state, touch_way(log2Ceil(right_nways)-1,0), insert_mode, right_nways, is_mainpipe),  // recurse right if newer
                right_subtree_state))  // if setting right sub-tree as older, do NOT recurse into right sub-tree
      }
    } else if (tree_nways == 2) {
      // we are at a leaf node at the end of the tree, so set the single state bit opposite of the lsb of the touched way encoded value
      is_mainpipe match {
        case Some(true) => Mux(insert_mode === insertMRU, !touch_way(0), touch_way(0))
        case None | Some(false) => !touch_way(0)
      }
      // Mux(insert_mode === insertMRU, touch_way(0), !touch_way(0))
    } else {  // tree_nways <= 1
      // we are at an empty node in an empty tree for 1 way, so return single zero bit for Chisel (no zero-width wires)
      0.U(1.W)
    }
  }

  def get_next_state(state: UInt, touch_way: UInt, insert_mode: UInt, is_mainpipe: Option[Boolean] = None): UInt = {
    val touch_way_sized = if (touch_way.getWidth < log2Ceil(n_ways)) touch_way.padTo  (log2Ceil(n_ways))
                                                                else touch_way.extract(log2Ceil(n_ways)-1,0)
    get_next_state(state, touch_way_sized, insert_mode, n_ways, is_mainpipe)
  }

  def get_next_state(state: UInt, valids: Seq[Bool], touch_ways: Seq[UInt], insert_modes: Seq[UInt], is_mainpipes: Seq[Option[Boolean]]): UInt = {
    (valids zip touch_ways zip insert_modes zip is_mainpipes).foldLeft(state) {
      case(prev, (((valid, touch_way), insert_mode), is_mainpipe)) =>
        Mux(valid, get_next_state(prev, touch_way, insert_mode, is_mainpipe), prev)
    }
  }

  def get_replace_way(state: UInt, tree_nways: Int): UInt = {
    require(state.getWidth == (tree_nways-1), s"wrong state bits width ${state.getWidth} for $tree_nways ways")

    // this algorithm recursively descends the binary tree, filling in the way-to-replace encoded value from msb to lsb
    if (tree_nways > 2) {
      // we are at a branching node in the tree, so recurse
      val right_nways: Int = 1 << (log2Ceil(tree_nways) - 1)  // number of ways in the right sub-tree
      val left_nways:  Int = tree_nways - right_nways         // number of ways in the left sub-tree
      val left_subtree_older  = state(tree_nways-2)
      val left_subtree_state  = state.extract(tree_nways-3, right_nways-1)
      val right_subtree_state = state(right_nways-2, 0)

      if (left_nways > 1) {
        // we are at a branching node in the tree with both left and right sub-trees, so recurse both sub-trees
        Cat(left_subtree_older,      // return the top state bit (current tree node) as msb of the way-to-replace encoded value
            Mux(left_subtree_older,  // if left sub-tree is older, recurse left, else recurse right
                get_replace_way(left_subtree_state,  left_nways),    // recurse left
                get_replace_way(right_subtree_state, right_nways)))  // recurse right
      } else {
        // we are at a branching node in the tree with only a right sub-tree, so recurse only right sub-tree
        Cat(left_subtree_older,      // return the top state bit (current tree node) as msb of the way-to-replace encoded value
            Mux(left_subtree_older,  // if left sub-tree is older, return and do not recurse right
                0.U(1.W),
                get_replace_way(right_subtree_state, right_nways)))  // recurse right
      }
    } else if (tree_nways == 2) {
      // we are at a leaf node at the end of the tree, so just return the single state bit as lsb of the way-to-replace encoded value
      state(0)
    } else {  // tree_nways <= 1
      // we are at an empty node in an unbalanced tree for non-power-of-2 ways, so return single zero bit as lsb of the way-to-replace encoded value
      0.U(1.W)
    }
  }

  def get_replace_way(state: UInt): UInt = get_replace_way(state, n_ways)

  def match_a(set: UInt) = {
    val set_bits = set.getWidth
    val dedicated_set_bits = log2Ceil(dedicated_sets_num)
    require((set_bits - dedicated_set_bits) > (dedicated_set_bits - 1), s"$set_bits, $dedicated_set_bits")
    set(set_bits - 1, set_bits - dedicated_set_bits) === set(dedicated_set_bits - 1, 0)
  }

  def match_b(set: UInt) = {
    val set_bits = set.getWidth
    val dedicated_set_bits = log2Ceil(dedicated_sets_num)
    require((set_bits - dedicated_set_bits) > (dedicated_set_bits - 1))
    set(set_bits - 1, set_bits - dedicated_set_bits) === ~(set(dedicated_set_bits - 1, 0))
  }

  def follower(set: UInt) = {
    !match_a(set) && !match_b(set)
  }

  def use_a = psel.out.select_a
  def use_b = !psel.out.select_a

  // hit: insert mru
  // miss: update psel
  // repl: insert lru or mru
  state_vec.zipWithIndex.foreach {
    case (state, setidx) =>
      val set = setidx.U(idxBits.W)
      val updateState = in.lduAccess.map(a => a.valid && a.bits.set === set).asUInt.orR || in.mpAccess.valid && in.mpAccess.bits.set === set
      val touchWaysSeq = in.lduAccess.map {a =>
        val valid = a.valid && a.bits.hit && a.bits.set === set
        val touchWay = a.bits.way
        val insertMode = insertMRU
        (valid, touchWay, insertMode, Some(false))
      } ++ Seq {
        val mpAccess = in.mpAccess.bits
        val valid = in.mpAccess.valid && in.mpAccess.bits.set === set && (mpAccess.isRepl || mpAccess.isHit)
        val touchWay = in.mpAccess.bits.way
        val isInsertMru = mpAccess.isHit || mpAccess.isRepl && 
          (match_a(set) || match_b(set) && bip_cnt === 0.U || follower(set) && (use_a || use_b && bip_cnt === 0.U))
        val insertMode = Mux(isInsertMru, insertMRU, insertLRU)
        (valid, touchWay, insertMode, Some(true))
      }
      when (updateState) {
        state := get_next_state(state, touchWaysSeq.map(_._1), touchWaysSeq.map(_._2), touchWaysSeq.map(_._3), touchWaysSeq.map(_._4))
      }
  }

  val accesses = in.lduAccess ++ Seq(in.mpAccess)
  (psel.in zip accesses).foreach {
    case (in, access) =>
      in.valid := access.valid
      in.bits.belong_a := match_a(access.bits.set)
      in.bits.belong_b := match_b(access.bits.set)
      in.bits.hit := access.bits.isHit
      in.bits.miss := access.bits.isMiss
  }
  psel.debug_in_repl := in.mpAccess.valid && in.mpAccess.bits.isRepl

  out.replResp.way := get_replace_way(state_vec(in.replReq.bits.set))
  val mpAccessSet = in.mpAccess.bits.set
  when (in.mpAccess.valid && in.mpAccess.bits.repl && (match_b(mpAccessSet) || follower(mpAccessSet) && use_b)) {
    bip_cnt := bip_cnt + 1.U
  }

  debug.foreach {
    case io =>
      io.r.bip_cnt := bip_cnt
      io.r.state.value := state_vec(io.r.state.set)

      when (io.w.bip_cnt.valid) {
        bip_cnt := io.w.bip_cnt.bits
      }
      when (io.w.state.valid) {
        state_vec(io.w.state.bits.set) := io.w.state.bits.value
      }
  }

  val debug_follower_repl_use_a = {
    val mpAccess = in.mpAccess.bits
    in.mpAccess.valid && mpAccess.isRepl && follower(mpAccess.set) && use_a
  }
  val debug_follower_repl_use_b = {
    val mpAccess = in.mpAccess.bits
    in.mpAccess.valid && mpAccess.isRepl && follower(mpAccess.set) && use_b
  }
  val debug_access_a_sdm = PopCount(
    in.lduAccess.map (a => a.valid && match_a(a.bits.set)) ++
    Seq(in.mpAccess.valid && match_a(in.mpAccess.bits.set)))
  val debug_access_b_sdm = PopCount(
    in.lduAccess.map (b => b.valid && match_b(b.bits.set)) ++
    Seq(in.mpAccess.valid && match_b(in.mpAccess.bits.set)))
  val debug_access_normal_sdm = PopCount(
    in.lduAccess.map (b => b.valid && follower(b.bits.set)) ++
    Seq(in.mpAccess.valid && follower(in.mpAccess.bits.set)))
  val debug_a_sdm_hit = PopCount(
    in.lduAccess.map (a => a.valid && match_a(a.bits.set) && a.bits.isHit) ++
    Seq(in.mpAccess.valid && match_a(in.mpAccess.bits.set) && in.mpAccess.bits.isHit))
  val debug_b_sdm_hit = PopCount(
    in.lduAccess.map (b => b.valid && match_b(b.bits.set) && b.bits.isHit) ++
    Seq(in.mpAccess.valid && match_b(in.mpAccess.bits.set) && in.mpAccess.bits.isHit))
  val debug_a_sdm_miss = PopCount(
    in.lduAccess.map (a => a.valid && match_a(a.bits.set) && a.bits.isMiss) ++
    Seq(in.mpAccess.valid && match_a(in.mpAccess.bits.set) && in.mpAccess.bits.isMiss))
  val debug_b_sdm_miss = PopCount(
    in.lduAccess.map (b => b.valid && match_b(b.bits.set) && b.bits.isMiss) ++
    Seq(in.mpAccess.valid && match_b(in.mpAccess.bits.set) && in.mpAccess.bits.isMiss))

  XSPerfAccumulate("follower_repl_use_a", debug_follower_repl_use_a)
  XSPerfAccumulate("follower_repl_use_b", debug_follower_repl_use_b)
  XSPerfAccumulate("access_a_sdm", debug_access_a_sdm)
  XSPerfAccumulate("access_b_sdm", debug_access_b_sdm)
  XSPerfAccumulate("access_normal_sdm", debug_access_normal_sdm)
  XSPerfAccumulate("a_sdm_hit", debug_a_sdm_hit)
  XSPerfAccumulate("b_sdm_hit", debug_b_sdm_hit)
  XSPerfAccumulate("a_sdm_miss", debug_a_sdm_miss)
  XSPerfAccumulate("b_sdm_miss", debug_b_sdm_miss)
}

class DRRIP(debug_mode: Boolean = false)(implicit p: Parameters) extends DCacheModule {
  val n_ways = cacheParams.nWays
  val n_sets = cacheParams.nSets
  val dedicated_sets_num = 16
  val state_bits = 2 * n_ways
  val bipcnt_bits = 5

  val in = IO(Input(new Bundle {
    val lduAccess = Vec(LoadPipelineWidth, ValidIO(new LduAccess))
    val mpAccess = ValidIO(new MPAccess)
    val replReq = ValidIO(new ReplReq)
  }))

  val out = IO(Output(new Bundle{
    val replResp = new ReplResp
  }))

  // for unit test
  val debug = if (debug_mode) Some(IO(new Bundle {
    val w = Flipped(new Bundle {
      val bip_cnt = Valid(UInt(bipcnt_bits.W))
      val state = Valid(new Bundle {
        val set = UInt(idxBits.W)
        val value = UInt(state_bits.W)
      })
    })
    val r = new Bundle {
      val bip_cnt = UInt(bipcnt_bits.W)
      val state = new Bundle {
        val set = Input(UInt(idxBits.W))
        val value = UInt(state_bits.W)
      }
    }
  })) else None

  val state_vec = if (state_bits == 0) Reg(Vec(n_sets, UInt(0.W))) else RegInit(VecInit(Seq.fill(n_sets)(0.U(state_bits.W))))
  val bip_cnt = RegInit(0.U(bipcnt_bits.W))
  val psel = Module(new PolicySelector)

  def get_next_state(state: UInt, touch_way: UInt, is_repl: Bool, insert_mode: UInt, tree_nways: Int, is_mainpipe: Option[Boolean]): UInt = {
        val State  = Wire(Vec(n_ways, UInt(2.W)))
    val nextState  = Wire(Vec(n_ways, UInt(2.W)))
    State.zipWithIndex.map { case (e, i) =>
      e := state(2*i+1,2*i)
    }
    // hit-Promotion, miss-Insertion & Aging
    val increcement = 3.U(2.W) - State(touch_way)
    // req_type[3]: 0-firstuse, 1-reuse; req_type[2]: 0-acquire, 1-release;
    // req_type[1]: 0-non-prefetch, 1-prefetch; req_type[0]: 0-not-refill, 1-refill
    // rrpv: non-pref_hit/non-pref_refill(miss)/non-pref_release_reuse = 0;
    // pref_hit do nothing; pref_refill = 1; non-pref_release_firstuse/pref_release = 2;
    nextState.zipWithIndex.map { case (e, i) =>
      e := Mux(i.U === touch_way,
        // for touch_way
        insert_mode,
        // for other ways
        is_mainpipe match {
          case Some(true) => Mux(is_repl, State(i)+increcement, State(i))
          case None | Some(false) => State(i)
        } 
      )
    }
    Cat(nextState.map(x=>x).reverse)
  }

  def get_next_state(state: UInt, touch_way: UInt, is_repl: Bool, insert_mode: UInt, is_mainpipe: Option[Boolean] = None): UInt = {
    val touch_way_sized = if (touch_way.getWidth < log2Ceil(n_ways)) touch_way.padTo  (log2Ceil(n_ways))
                                                                else touch_way.extract(log2Ceil(n_ways)-1,0)
    get_next_state(state, touch_way_sized, is_repl, insert_mode, n_ways, is_mainpipe)
  }

  def get_next_state(state: UInt, valids: Seq[Bool], touch_ways: Seq[UInt], is_repls:Seq[Bool], insert_modes: Seq[UInt], is_mainpipes: Seq[Option[Boolean]]): UInt = {
    (valids zip touch_ways zip is_repls zip insert_modes zip is_mainpipes).foldLeft(state) {
      case(prev, ((((valid, touch_way), is_repl), insert_mode), is_mainpipe)) =>
        Mux(valid, get_next_state(prev, touch_way, is_repl, insert_mode, is_mainpipe), prev)
    }
  }

  def get_replace_way(state: UInt, tree_nways: Int): UInt = {
    val RRPVVec = Wire(Vec(n_ways, UInt(2.W)))
    RRPVVec.zipWithIndex.map { case (e, i) =>
        e := state(2*i+1,2*i)
    }
    // scan each way's rrpv, find the least re-referenced way
    val lrrWayVec = Wire(Vec(n_ways,Bool()))
    lrrWayVec.zipWithIndex.map { case (e, i) =>
      val isLarger = Wire(Vec(n_ways,Bool()))
      for (j <- 0 until n_ways) {
        isLarger(j) := RRPVVec(j) > RRPVVec(i)
      }
      e := !(isLarger.contains(true.B))
    }
    PriorityEncoder(lrrWayVec)
  }

  def get_replace_way(state: UInt): UInt = get_replace_way(state, n_ways)

  def match_a(set: UInt) = {
    val set_bits = set.getWidth
    val dedicated_set_bits = log2Ceil(dedicated_sets_num)
    require((set_bits - dedicated_set_bits) > (dedicated_set_bits - 1), s"$set_bits, $dedicated_set_bits")
    set(set_bits - 1, set_bits - dedicated_set_bits) === set(dedicated_set_bits - 1, 0)
  }

  def match_b(set: UInt) = {
    val set_bits = set.getWidth
    val dedicated_set_bits = log2Ceil(dedicated_sets_num)
    require((set_bits - dedicated_set_bits) > (dedicated_set_bits - 1))
    set(set_bits - 1, set_bits - dedicated_set_bits) === ~(set(dedicated_set_bits - 1, 0))
  }

  def follower(set: UInt) = {
    !match_a(set) && !match_b(set)
  }

  def use_a = psel.out.select_a
  def use_b = !psel.out.select_a

  // hit: insert mru
  // miss: update psel
  // repl: insert lru or mru
  state_vec.zipWithIndex.foreach {
    case (state, setidx) =>
      val set = setidx.U(idxBits.W)
      val updateState = in.lduAccess.map(a => a.valid && a.bits.set === set).asUInt.orR || in.mpAccess.valid && in.mpAccess.bits.set === set
      val touchWaysSeq = in.lduAccess.map {a =>
        val valid = a.valid && a.bits.hit && (a.bits.softPft || a.bits.nonPft) && a.bits.set === set
        val touchWay = a.bits.way
        val isRepl = false.B
        val insertMode = 0.U
        (valid, touchWay, isRepl, insertMode, Some(false))
      } ++ Seq {
        val mpAccess = in.mpAccess.bits
        val valid = in.mpAccess.valid && in.mpAccess.bits.set === set && (mpAccess.isRepl || mpAccess.isHit)
        val touchWay = in.mpAccess.bits.way
        val isRepl = mpAccess.isRepl
        val insertMode = Mux1H(Seq(
          mpAccess.isHit -> 0.U,
          (mpAccess.isRepl && (mpAccess.nonPft || mpAccess.softPft) && 
            (match_a(mpAccess.set) || (follower(mpAccess.set) && use_a) || (match_b(mpAccess.set) || follower(mpAccess.set) && use_b) && bip_cnt === 0.U)) -> 1.U,
          (mpAccess.isRepl && mpAccess.hwPft && bip_cnt =/= 0.U &&
            (match_a(mpAccess.set) || (follower(mpAccess.set) && use_a) || (match_b(mpAccess.set) || follower(mpAccess.set) && use_b) && bip_cnt === 0.U)) -> 2.U,
          (mpAccess.isRepl && (match_b(mpAccess.set) || follower(mpAccess.set) && use_b) && bip_cnt =/= 0.U) -> 3.U
        ))
        (valid, touchWay, isRepl, insertMode, Some(true))
      }
      when (updateState) {
        state := get_next_state(state, touchWaysSeq.map(_._1), touchWaysSeq.map(_._2), touchWaysSeq.map(_._3), touchWaysSeq.map(_._4), touchWaysSeq.map(_._5))
      }
  }

  val accesses = in.lduAccess ++ Seq(in.mpAccess)
  (psel.in zip accesses).foreach {
    case (in, access) =>
      in.valid := access.valid
      in.bits.belong_a := match_a(access.bits.set)
      in.bits.belong_b := match_b(access.bits.set)
      in.bits.hit := access.bits.isHit
      in.bits.miss := access.bits.isMiss
  }
  psel.debug_in_repl := in.mpAccess.valid && in.mpAccess.bits.isRepl

  out.replResp.way := get_replace_way(state_vec(in.replReq.bits.set))
  val mpAccessSet = in.mpAccess.bits.set
  when (in.mpAccess.valid && in.mpAccess.bits.repl && (match_b(mpAccessSet) || follower(mpAccessSet) && use_b)) {
    bip_cnt := bip_cnt + 1.U
  }

  debug.foreach {
    case io =>
      io.r.bip_cnt := bip_cnt
      io.r.state.value := state_vec(io.r.state.set)

      when (io.w.bip_cnt.valid) {
        bip_cnt := io.w.bip_cnt.bits
      }
      when (io.w.state.valid) {
        state_vec(io.w.state.bits.set) := io.w.state.bits.value
      }
  }

  val debug_follower_repl_use_a = {
    val mpAccess = in.mpAccess.bits
    in.mpAccess.valid && mpAccess.isRepl && follower(mpAccess.set) && use_a
  }
  val debug_follower_repl_use_b = {
    val mpAccess = in.mpAccess.bits
    in.mpAccess.valid && mpAccess.isRepl && follower(mpAccess.set) && use_b
  }
  val debug_access_a_sdm = PopCount(
    in.lduAccess.map (a => a.valid && match_a(a.bits.set)) ++
    Seq(in.mpAccess.valid && match_a(in.mpAccess.bits.set)))
  val debug_access_b_sdm = PopCount(
    in.lduAccess.map (b => b.valid && match_b(b.bits.set)) ++
    Seq(in.mpAccess.valid && match_b(in.mpAccess.bits.set)))
  val debug_access_normal_sdm = PopCount(
    in.lduAccess.map (b => b.valid && follower(b.bits.set)) ++
    Seq(in.mpAccess.valid && follower(in.mpAccess.bits.set)))
  val debug_a_sdm_hit = PopCount(
    in.lduAccess.map (a => a.valid && match_a(a.bits.set) && a.bits.isHit) ++
    Seq(in.mpAccess.valid && match_a(in.mpAccess.bits.set) && in.mpAccess.bits.isHit))
  val debug_b_sdm_hit = PopCount(
    in.lduAccess.map (b => b.valid && match_b(b.bits.set) && b.bits.isHit) ++
    Seq(in.mpAccess.valid && match_b(in.mpAccess.bits.set) && in.mpAccess.bits.isHit))
  val debug_a_sdm_miss = PopCount(
    in.lduAccess.map (a => a.valid && match_a(a.bits.set) && a.bits.isMiss) ++
    Seq(in.mpAccess.valid && match_a(in.mpAccess.bits.set) && in.mpAccess.bits.isMiss))
  val debug_b_sdm_miss = PopCount(
    in.lduAccess.map (b => b.valid && match_b(b.bits.set) && b.bits.isMiss) ++
    Seq(in.mpAccess.valid && match_b(in.mpAccess.bits.set) && in.mpAccess.bits.isMiss))

  XSPerfAccumulate("follower_repl_use_a", debug_follower_repl_use_a)
  XSPerfAccumulate("follower_repl_use_b", debug_follower_repl_use_b)
  XSPerfAccumulate("access_a_sdm", debug_access_a_sdm)
  XSPerfAccumulate("access_b_sdm", debug_access_b_sdm)
  XSPerfAccumulate("access_normal_sdm", debug_access_normal_sdm)
  XSPerfAccumulate("a_sdm_hit", debug_a_sdm_hit)
  XSPerfAccumulate("b_sdm_hit", debug_b_sdm_hit)
  XSPerfAccumulate("a_sdm_miss", debug_a_sdm_miss)
  XSPerfAccumulate("b_sdm_miss", debug_b_sdm_miss)
}