/***************************************************************************************
* Copyright (c) 2020-2021 Institute of Computing Technology, Chinese Academy of Sciences
* Copyright (c) 2020-2021 Peng Cheng Laboratory
*
* XiangShan is licensed under Mulan PSL v2.
* You can use this software according to the terms and conditions of the Mulan PSL v2.
* You may obtain a copy of Mulan PSL v2 at:
*          http://license.coscl.org.cn/MulanPSL2
*
* THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
* EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
* MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
*
* See the Mulan PSL v2 for more details.
***************************************************************************************/

package xiangshan.mem

import org.chipsalliance.cde.config._
import chisel3._
import chisel3.util._
import utils._
import utility._
import xiangshan._
import xiangshan.frontend.ftq.FtqPtr
import xiangshan.backend.rob.RobPtr
import xiangshan.backend.Bundles.DynInst
import xiangshan.mem.mdp._
import xiangshan.mem.mdp.NewMdp.{MdpUpdate, SmbWrongBypassReason}
import xiangshan.mem.Bundles._
import xiangshan.cache._
import xiangshan.mem.mdp.NewMdp.MdpUpdateType

class LoadQueueRAW(implicit p: Parameters) extends XSModule
  with HasDCacheParameters
  with HasCircularQueuePtrHelper
  with HasLoadHelper
  with HasPerfEvents
{
  val io = IO(new Bundle() {
    // control
    val redirect = Flipped(ValidIO(new Redirect))

    // violation query
    val query = Vec(LoadPipelineWidth, Flipped(new LoadNukeQueryIO))

    // from store unit s1
    val storeIn = Vec(StorePipelineWidth, Flipped(Valid(new LsPipelineBundle)))

    // global rollback flush
    val rollback = Vec(StorePipelineWidth,Output(Valid(new Redirect)))
    
    // mdp update 
    val mdpUpdateOldest = Output(Valid(new MdpUpdate))

    // to LoadQueueReplay
    val stAddrReadySqPtr = Input(new SqPtr)
    val stIssuePtr       = Input(new SqPtr)
    val lqFull           = Output(Bool())
  })

  private def PartialPAddrWidth: Int = 24
  private def paddrOffset: Int = DCacheVWordOffset
  private def genPartialPAddr(paddr: UInt) = {
    paddr(DCacheVWordOffset + PartialPAddrWidth - 1, paddrOffset)
  }
  private def storeLoadAddrMaskMatch(
    loadPaddr: UInt,
    loadMask: UInt,
    storePaddr: UInt,
    storeMask: UInt,
    storeCheckLine: Bool
  ): Bool = {
    val loadPartialPAddr = genPartialPAddr(loadPaddr)
    val storePartialPAddr = genPartialPAddr(storePaddr)
    val addrMatch = if (DCacheLineOffset > paddrOffset) {
      val cacheLineOffset = DCacheLineOffset - paddrOffset
      val cacheLineHit = loadPartialPAddr(PartialPAddrWidth - 1, cacheLineOffset) ===
        storePartialPAddr(PartialPAddrWidth - 1, cacheLineOffset)
      val lowAddrHit = loadPartialPAddr(cacheLineOffset - 1, 0) ===
        storePartialPAddr(cacheLineOffset - 1, 0)
      cacheLineHit && (storeCheckLine || lowAddrHit)
    } else {
      loadPartialPAddr === storePartialPAddr
    }
    val maskMatch = (storeMask & loadMask).orR
    addrMatch && maskMatch
  }

  println("LoadQueueRAW: size " + LoadQueueRAWSize)
  //  LoadQueueRAW field
  //  +-------+--------+-------+-------+-----------+
  //  | Valid |  uop   |PAddr  | Mask  | Datavalid |
  //  +-------+--------+-------+-------+-----------+
  //
  //  Field descriptions:
  //  Allocated   : entry has been allocated already
  //  MicroOp     : inst's microOp
  //  PAddr       : physical address.
  //  Mask        : data mask
  //  Datavalid   : data valid
  //
  val allocated = RegInit(VecInit(List.fill(LoadQueueRAWSize)(false.B))) // The control signals need to explicitly indicate the initial value
  val uop = Reg(Vec(LoadQueueRAWSize, new DynInst))
  val paddrModule = Module(new LqPAddrModule(
    gen = UInt(PartialPAddrWidth.W),
    numEntries = LoadQueueRAWSize,
    numRead = LoadPipelineWidth,
    numWrite = LoadPipelineWidth,
    numWBank = LoadQueueNWriteBanks,
    numWDelay = 2,
    numCamPort = StorePipelineWidth,
    enableCacheLineCheck = true,
    paddrOffset = paddrOffset
  ))
  paddrModule.io := DontCare
  val maskModule = Module(new LqMaskModule(
    gen = UInt((VLEN/8).W),
    numEntries = LoadQueueRAWSize,
    numRead = LoadPipelineWidth,
    numWrite = LoadPipelineWidth,
    numWBank = LoadQueueNWriteBanks,
    numWDelay = 2,
    numCamPort = StorePipelineWidth
  ))
  maskModule.io := DontCare
  val datavalid = RegInit(VecInit(List.fill(LoadQueueRAWSize)(false.B)))
  val smbConsumed = RegInit(VecInit(List.fill(LoadQueueRAWSize)(false.B)))
  val smbVerifyReadyAtConsume = RegInit(VecInit(List.fill(LoadQueueRAWSize)(false.B)))
  val smbVerifyPassAtConsume = RegInit(VecInit(List.fill(LoadQueueRAWSize)(false.B)))
  val smbEnqLateMismatchPending = RegInit(VecInit(List.fill(LoadQueueRAWSize)(false.B)))
  val smbDirectVerifyFailIssued = RegInit(VecInit(List.fill(LoadQueueRAWSize)(false.B)))
  val smbDirectVerifyFailInFlight = RegInit(false.B)
  // stAddrReadySqPtr may lead the corresponding storeIn pulse by a small bounded skew.
  // Keep SMB-tracked entries alive across that skew, but do not let them survive indefinitely.
  private val SmbPredStoreGraceCycles = 2
  private val SmbPredStoreGraceWidth = log2Ceil(SmbPredStoreGraceCycles + 1)
  val smbPredStoreObserved = RegInit(VecInit(List.fill(LoadQueueRAWSize)(false.B)))
  val smbPredStoreGrace = RegInit(VecInit(List.fill(LoadQueueRAWSize)(0.U(SmbPredStoreGraceWidth.W))))

  // freeliset: store valid entries index.
  // +---+---+--------------+-----+-----+
  // | 0 | 1 |      ......  | n-2 | n-1 |
  // +---+---+--------------+-----+-----+
  val freeList = Module(new FreeList(
    size = LoadQueueRAWSize,
    allocWidth = LoadPipelineWidth,
    freeWidth = 4,
    enablePreAlloc = true,
    moduleName = "LoadQueueRAW freelist"
  ))
  freeList.io := DontCare

  //  LoadQueueRAW enqueue
  val canEnqueue = io.query.map(_.req.valid)
  val cancelEnqueue = io.query.map(_.req.bits.uop.robIdx.needFlush(io.redirect))
  val allAddrCheck = io.stIssuePtr === io.stAddrReadySqPtr
  val hasAddrInvalidStore = io.query.map(_.req.bits.uop.sqIdx).map(sqIdx => {
    Mux(!allAddrCheck, isBefore(io.stAddrReadySqPtr, sqIdx), false.B)
  })
  val needSmbTracking = io.query.map(_.req.bits.smbConsumed)
  val needEnqueue = canEnqueue.zip(hasAddrInvalidStore).zip(needSmbTracking).zip(cancelEnqueue).map {
    case (((v, rawTrack), smbTrack), cancel) => v && (rawTrack || smbTrack) && !cancel
  }

  // Allocate logic
  val acceptedVec = Wire(Vec(LoadPipelineWidth, Bool()))
  val enqIndexVec = Wire(Vec(LoadPipelineWidth, UInt(log2Up(LoadQueueRAWSize).W)))
  val fastEnqWrongBypassValidVec = WireInit(VecInit(Seq.fill(LoadPipelineWidth)(false.B)))
  val fastEnqWrongBypassReasonVec = WireInit(VecInit(Seq.fill(LoadPipelineWidth)(SmbWrongBypassReason.none)))
  val fastEnqWrongBypassLqIdxVec = WireInit(VecInit(Seq.fill(LoadPipelineWidth)(0.U(log2Up(LoadQueueRAWSize).W))))

  // Enqueue
  for ((enq, w) <- io.query.map(_.req).zipWithIndex) {
    acceptedVec(w) := false.B
    paddrModule.io.wen(w) := false.B
    maskModule.io.wen(w) := false.B
    freeList.io.doAllocate(w) := false.B

    freeList.io.allocateReq(w) := true.B

    //  Allocate ready
    val offset = PopCount(needEnqueue.take(w))
    val canAccept = freeList.io.canAllocate(offset)
    val enqIndex = freeList.io.allocateSlot(offset)
    val enqPredictedStoreRobIdx = enq.bits.uop.loadPred.bits.getWaitStoreRobIdx(enq.bits.uop.robIdx)
    val enqVerifyFail = enq.bits.smbConsumed &&
      enq.bits.smbVerifyReadyAtConsume &&
      !enq.bits.smbVerifyPassAtConsume
    val enqPredictedStoreWriteback = enq.bits.smbConsumed && VecInit((0 until StorePipelineWidth).map(storePipe =>
      io.storeIn(storePipe).valid &&
      !io.storeIn(storePipe).bits.miss &&
      (io.storeIn(storePipe).bits.uop.robIdx === enqPredictedStoreRobIdx)
    )).asUInt.orR
    val enqPredictedStoreLateMismatch = enq.bits.smbConsumed && !enqVerifyFail && VecInit((0 until StorePipelineWidth).map(storePipe =>
      io.storeIn(storePipe).valid &&
      !io.storeIn(storePipe).bits.miss &&
      (io.storeIn(storePipe).bits.uop.robIdx === enqPredictedStoreRobIdx) &&
      !storeLoadAddrMaskMatch(
        enq.bits.paddr,
        enq.bits.mask,
        io.storeIn(storePipe).bits.paddr,
        io.storeIn(storePipe).bits.mask,
        io.storeIn(storePipe).bits.wlineflag
      )
    )).asUInt.orR
    enq.ready := Mux(needEnqueue(w), canAccept, true.B)

    enqIndexVec(w) := enqIndex
    when (needEnqueue(w) && enq.ready) {
      acceptedVec(w) := true.B

      freeList.io.doAllocate(w) := true.B

      //  Allocate new entry
      allocated(enqIndex) := true.B

      //  Write paddr
      paddrModule.io.wen(w) := true.B
      paddrModule.io.waddr(w) := enqIndex
      paddrModule.io.wdata(w) := genPartialPAddr(enq.bits.paddr)

      //  Write mask
      maskModule.io.wen(w) := true.B
      maskModule.io.waddr(w) := enqIndex
      maskModule.io.wdata(w) := enq.bits.mask

      //  Fill info
      uop(enqIndex) := enq.bits.uop
      datavalid(enqIndex) := enq.bits.data_valid
      smbConsumed(enqIndex) := enq.bits.smbConsumed
      smbVerifyReadyAtConsume(enqIndex) := enq.bits.smbVerifyReadyAtConsume
      smbVerifyPassAtConsume(enqIndex) := enq.bits.smbVerifyPassAtConsume
      smbEnqLateMismatchPending(enqIndex) := enqPredictedStoreLateMismatch
      smbDirectVerifyFailIssued(enqIndex) := enqVerifyFail
      smbPredStoreObserved(enqIndex) := enqPredictedStoreWriteback
      smbPredStoreGrace(enqIndex) := 0.U
      fastEnqWrongBypassValidVec(w) := enqVerifyFail || enqPredictedStoreLateMismatch
      fastEnqWrongBypassReasonVec(w) := Mux(enqPredictedStoreLateMismatch,
        SmbWrongBypassReason.lateMismatch,
        SmbWrongBypassReason.verifyFail)
      fastEnqWrongBypassLqIdxVec(w) := enqIndex
      when(enq.bits.smbConsumed) {
        assert(enq.bits.uop.loadPred.valid && enq.bits.uop.loadPred.bits.smbEnable && enq.bits.uop.loadPred.bits.loadWait,
          s"LoadQueueRAW enqueue SMB-consumed entry on pipe ${w} requires dependent SMB prediction")
      }
    }
    when(enq.bits.smbConsumed && enq.valid && !cancelEnqueue(w)) {
      assert(needEnqueue(w), s"LoadQueueRAW SMB-consumed request on pipe ${w} must allocate late-verify tracking")
    }
    val debug_robIdx = enq.bits.uop.robIdx.asUInt
    XSError(needEnqueue(w) && enq.ready && allocated(enqIndex), p"LoadQueueRAW: You can not write an valid entry! check: ldu $w, robIdx $debug_robIdx")
  }

  for ((query, w) <- io.query.map(_.resp).zipWithIndex) {
    query.valid := RegNext(io.query(w).req.valid)
    query.bits.rep_frm_fetch := RegNext(false.B)
  }

  //  LoadQueueRAW deallocate
  val freeMaskVec = Wire(Vec(LoadQueueRAWSize, Bool()))

  // init
  freeMaskVec.map(e => e := false.B)

  // when the stores that "older than" current load address were ready.
  // current load will be released.
  for (i <- 0 until LoadQueueRAWSize) {
    val deqNotBlock = Mux(!allAddrCheck, !isBefore(io.stAddrReadySqPtr, uop(i).sqIdx), true.B)
    val needCancel = uop(i).robIdx.needFlush(io.redirect)
    val predictedStoreRobIdx = uop(i).loadPred.bits.getWaitStoreRobIdx(uop(i).robIdx)
    val smbPredictedStoreWriteback = VecInit((0 until StorePipelineWidth).map(w =>
      io.storeIn(w).valid &&
      !io.storeIn(w).bits.miss &&
      (io.storeIn(w).bits.uop.robIdx === predictedStoreRobIdx)
    )).asUInt.orR
    val smbDirectWrongBypassPending =
      (smbVerifyReadyAtConsume(i) && !smbVerifyPassAtConsume(i) && !smbDirectVerifyFailIssued(i)) ||
      smbEnqLateMismatchPending(i)
    // Hold one extra cycle after predicted-store writeback so the store-driven
    // wrong-bypass detector can still see this SMB-consumed entry.
    val smbStoreObserved = smbPredStoreObserved(i)
    val smbStoreVisibleNow = smbPredStoreObserved(i) || smbPredictedStoreWriteback
    val smbGraceExpired = smbPredStoreGrace(i) === SmbPredStoreGraceCycles.U
    val releaseTrackedSmb = smbConsumed(i) && !smbDirectWrongBypassPending &&
      (smbStoreObserved || (deqNotBlock && smbGraceExpired))
    val shouldRelease = Mux(smbConsumed(i), releaseTrackedSmb || needCancel, deqNotBlock || needCancel)

    when (allocated(i) && smbConsumed(i) && smbPredictedStoreWriteback) {
      smbPredStoreObserved(i) := true.B
    }
    when (allocated(i) && smbConsumed(i)) {
      when (releaseTrackedSmb || needCancel || !deqNotBlock || smbPredictedStoreWriteback) {
        smbPredStoreGrace(i) := 0.U
      } .elsewhen (deqNotBlock && !smbStoreVisibleNow && !smbGraceExpired) {
        smbPredStoreGrace(i) := smbPredStoreGrace(i) + 1.U
      }
    }

    when (allocated(i) && shouldRelease) {
      when(smbConsumed(i)) {
        assert(releaseTrackedSmb || needCancel,
          s"LoadQueueRAW SMB-tracked entry ${i} may only clear after predicted store observation, bounded grace expiry, or redirect cancellation")
      }
      allocated(i) := false.B
      smbConsumed(i) := false.B
      smbVerifyReadyAtConsume(i) := false.B
      smbVerifyPassAtConsume(i) := false.B
      smbEnqLateMismatchPending(i) := false.B
      smbDirectVerifyFailIssued(i) := false.B
      smbPredStoreObserved(i) := false.B
      smbPredStoreGrace(i) := 0.U
      freeMaskVec(i) := true.B
    }
  }

  // if need replay deallocate entry
  val lastCanAccept = GatedValidRegNext(acceptedVec)
  val lastAllocIndex = GatedRegNext(enqIndexVec)
  val willRevoke = WireInit(VecInit(List.fill(LoadQueueRAWSize)(false.B)))

  for ((revoke, w) <- io.query.map(_.revoke).zipWithIndex) {
    val revokeValid = revoke && lastCanAccept(w)
    val revokeIndex = lastAllocIndex(w)

    when (allocated(revokeIndex) && revokeValid) {
      allocated(revokeIndex) := false.B
      smbConsumed(revokeIndex) := false.B
      smbVerifyReadyAtConsume(revokeIndex) := false.B
      smbVerifyPassAtConsume(revokeIndex) := false.B
      smbEnqLateMismatchPending(revokeIndex) := false.B
      smbDirectVerifyFailIssued(revokeIndex) := false.B
      smbPredStoreObserved(revokeIndex) := false.B
      smbPredStoreGrace(revokeIndex) := 0.U
      freeMaskVec(revokeIndex) := true.B
      willRevoke(revokeIndex) := true.B
    }
  }
  freeList.io.free := freeMaskVec.asUInt

  (0 until LoadQueueRAWSize).foreach { i =>
    when(smbConsumed(i)) {
      assert(allocated(i), s"LoadQueueRAW SMB-tracked entry ${i} must remain allocated while tracked")
      assert(uop(i).loadPred.valid && uop(i).loadPred.bits.smbEnable && uop(i).loadPred.bits.loadWait,
        s"LoadQueueRAW SMB-tracked entry ${i} must preserve dependent SMB ownership")
    }
  }

  io.lqFull := freeList.io.empty

  /**
    * Store-Load Memory violation detection
    * Scheme 1(Current scheme): flush the pipeline then re-fetch from the load instruction (like old load queue).
    * Scheme 2                : re-fetch instructions from the first instruction after the store instruction.
    *
    * When store writes back, it searches LoadQueue for younger load instructions
    * with the same load physical address. They loaded wrong data and need re-execution.
    *
    * Cycle 0: Store Writeback
    *   Generate match vector for store address with rangeMask(stPtr, enqPtr).
    * Cycle 1: Select oldest load from select group.
    * Cycle x: Redirect Fire
    *   Choose the oldest load from LoadPipelineWidth oldest loads.
    *   Prepare redirect request according to the detected violation.
    *   Fire redirect request (if valid)
    */
  //              SelectGroup 0         SelectGroup 1          SelectGroup y
  // stage 0:       lq  lq  lq  ......    lq  lq  lq  .......    lq  lq  lq
  //                |   |   |             |   |   |              |   |   |
  // stage 1:       lq  lq  lq  ......    lq  lq  lq  .......    lq  lq  lq
  //                 \  |  /    ......     \  |  /    .......     \  |  /
  // stage 2:           lq                    lq                     lq
  //                     \  |  /  .......  \  |  /   ........  \  |  /
  // stage 3:               lq                lq                  lq
  //                                          ...
  //                                          ...
  //                                           |
  // stage x:                                  lq
  //                                           |
  //                                       rollback req

  // select logic
  val SelectGroupSize = RollbackGroupSize
  val lgSelectGroupSize = log2Ceil(SelectGroupSize)
  val TotalSelectCycles = scala.math.ceil(log2Ceil(LoadQueueRAWSize).toFloat / lgSelectGroupSize).toInt + 1

  def selectPartialOldest[T <: XSBundleWithMicroOp](valid: Seq[Bool], bits: Seq[T]): (Seq[Bool], Seq[T]) = {
    assert(valid.length == bits.length)
    if (valid.length == 0 || valid.length == 1) {
      (valid, bits)
    } else if (valid.length == 2) {
      val res = Seq.fill(2)(Wire(ValidIO(chiselTypeOf(bits(0)))))
      for (i <- res.indices) {
        res(i).valid := valid(i)
        res(i).bits := bits(i)
      }
      val oldest = Mux(valid(0) && valid(1), Mux(isAfter(bits(0).uop.robIdx, bits(1).uop.robIdx), res(1), res(0)), Mux(valid(0) && !valid(1), res(0), res(1)))
      (Seq(oldest.valid), Seq(oldest.bits))
    } else {
      val left = selectPartialOldest(valid.take(valid.length / 2), bits.take(bits.length / 2))
      val right = selectPartialOldest(valid.takeRight(valid.length - (valid.length / 2)), bits.takeRight(bits.length - (bits.length / 2)))
      selectPartialOldest(left._1 ++ right._1, left._2 ++ right._2)
    }
  }

  def selectOldest[T <: XSBundleWithMicroOp](valid: Seq[Bool], bits: Seq[T]): (Seq[Bool], Seq[T]) = {
    assert(valid.length == bits.length)
    val numSelectGroups = scala.math.ceil(valid.length.toFloat / SelectGroupSize).toInt

    // group info
    val selectValidGroups = valid.grouped(SelectGroupSize).toList
    val selectBitsGroups = bits.grouped(SelectGroupSize).toList
    // select logic
    if (valid.length <= SelectGroupSize) {
      val (selValid, selBits) = selectPartialOldest(valid, bits)
      val selValidNext = GatedValidRegNext(selValid(0))
      val selBitsNext = RegEnable(selBits(0), selValid(0))
      (Seq(selValidNext && !selBitsNext.uop.robIdx.needFlush(RegNext(io.redirect))), Seq(selBitsNext))
    } else {
      val select = (0 until numSelectGroups).map(g => {
        val (selValid, selBits) = selectPartialOldest(selectValidGroups(g), selectBitsGroups(g))
        val selValidNext = RegNext(selValid(0))
        val selBitsNext = RegEnable(selBits(0), selValid(0))
        (selValidNext && !selBitsNext.uop.robIdx.needFlush(io.redirect) && !selBitsNext.uop.robIdx.needFlush(RegNext(io.redirect)), selBitsNext)
      })
      selectOldest(select.map(_._1), select.map(_._2))
    }
  }

  val storeIn = io.storeIn
  class FastEnqWrongBypassEntry extends XSBundleWithMicroOp {
    val lqIdx = UInt(log2Up(LoadQueueRAWSize).W)
    val smbWrongBypassReason = UInt(2.W)
  }
  val fastEnqWrongBypassEntries = (0 until LoadPipelineWidth).map(w => {
    val entry = Wire(new FastEnqWrongBypassEntry)
    entry.uop := io.query(w).req.bits.uop
    entry.lqIdx := fastEnqWrongBypassLqIdxVec(w)
    entry.smbWrongBypassReason := fastEnqWrongBypassReasonVec(w)
    entry
  })
  val fastEnqWrongBypassSelect = selectPartialOldest(fastEnqWrongBypassValidVec, fastEnqWrongBypassEntries)
  val fastEnqWrongBypassRedirect = Wire(Valid(new Redirect))
  fastEnqWrongBypassRedirect.valid := fastEnqWrongBypassSelect._1(0)
  fastEnqWrongBypassRedirect.bits := DontCare
  fastEnqWrongBypassRedirect.bits.isRVC := fastEnqWrongBypassSelect._2(0).uop.isRVC
  fastEnqWrongBypassRedirect.bits.robIdx := fastEnqWrongBypassSelect._2(0).uop.robIdx
  fastEnqWrongBypassRedirect.bits.ftqIdx := fastEnqWrongBypassSelect._2(0).uop.ftqPtr
  fastEnqWrongBypassRedirect.bits.ftqOffset := fastEnqWrongBypassSelect._2(0).uop.ftqOffset
  fastEnqWrongBypassRedirect.bits.stFtqIdx := 0.U.asTypeOf(new FtqPtr)
  fastEnqWrongBypassRedirect.bits.stFtqOffset := 0.U
  fastEnqWrongBypassRedirect.bits.level := RedirectLevel.flush
  fastEnqWrongBypassRedirect.bits.target := fastEnqWrongBypassSelect._2(0).uop.pc
  fastEnqWrongBypassRedirect.bits.debug_runahead_checkpoint_id := fastEnqWrongBypassSelect._2(0).uop.perfDebugInfo.runahead_checkpoint_id
  when(fastEnqWrongBypassRedirect.valid) {
    assert(fastEnqWrongBypassSelect._2(0).uop.loadPred.valid &&
      fastEnqWrongBypassSelect._2(0).uop.loadPred.bits.smbEnable &&
      !fastEnqWrongBypassSelect._2(0).uop.loadPred.bits.static,
      "LoadQueueRAW fast enqueue wrong-bypass redirect requires dynamic SMB prediction")
    assert(fastEnqWrongBypassSelect._2(0).uop.loadPred.bits.smbProviderHandle.valid,
      "LoadQueueRAW fast enqueue wrong-bypass redirect requires provider handle")
  }
  val smbDirectVerifyFailPendingSelVec = VecInit((0 until LoadQueueRAWSize).map(j =>
    smbVerifyReadyAtConsume(j) &&
    !smbVerifyPassAtConsume(j) &&
    !smbDirectVerifyFailIssued(j)
  ))
  val smbEnqLateMismatchPendingSelVec = VecInit((0 until LoadQueueRAWSize).map(j =>
    smbEnqLateMismatchPending(j)
  ))
  val smbDirectWrongBypassSelVec = VecInit((0 until LoadQueueRAWSize).map(j =>
    allocated(j) &&
    datavalid(j) &&
    smbConsumed(j) &&
    (smbDirectVerifyFailPendingSelVec(j) || smbEnqLateMismatchPendingSelVec(j)) &&
    !smbDirectVerifyFailInFlight &&
    !uop(j).robIdx.needFlush(io.redirect) &&
    !willRevoke(j)
  ))
  class DirectVerifyFailEntry extends XSBundleWithMicroOp {
    val lqIdx = UInt(log2Up(LoadQueueRAWSize).W)
  }
  val directVerifyFailEntries = (0 until LoadQueueRAWSize).map(j => {
    val entry = Wire(new DirectVerifyFailEntry)
    entry.uop := uop(j)
    entry.lqIdx := j.U
    entry
  })
  val directSmbLaunch = smbDirectWrongBypassSelVec.asUInt.orR && !smbDirectVerifyFailInFlight
  val directSmbLaunchDone = DelayN(directSmbLaunch, TotalSelectCycles)
  when(directSmbLaunch) {
    smbDirectVerifyFailInFlight := true.B
  }
  when(directSmbLaunchDone) {
    smbDirectVerifyFailInFlight := false.B
  }
  val directSmbSelect = selectOldest(smbDirectWrongBypassSelVec, directVerifyFailEntries)
  val directSmbRedirect = Wire(Valid(new Redirect))
  directSmbRedirect.valid := directSmbSelect._1(0)
  directSmbRedirect.bits := DontCare
  directSmbRedirect.bits.isRVC := directSmbSelect._2(0).uop.isRVC
  directSmbRedirect.bits.robIdx := directSmbSelect._2(0).uop.robIdx
  directSmbRedirect.bits.ftqIdx := directSmbSelect._2(0).uop.ftqPtr
  directSmbRedirect.bits.ftqOffset := directSmbSelect._2(0).uop.ftqOffset
  directSmbRedirect.bits.stFtqIdx := 0.U.asTypeOf(new FtqPtr)
  directSmbRedirect.bits.stFtqOffset := 0.U
  directSmbRedirect.bits.level := RedirectLevel.flush
  directSmbRedirect.bits.target := directSmbSelect._2(0).uop.pc
  directSmbRedirect.bits.debug_runahead_checkpoint_id := directSmbSelect._2(0).uop.perfDebugInfo.runahead_checkpoint_id

  def detectRollback(i: Int) = {
    class RollbackEntry extends XSBundleWithMicroOp {
      val wrongBypass = Bool()
    }
    paddrModule.io.violationMdata(i) := genPartialPAddr(RegEnable(storeIn(i).bits.paddr, storeIn(i).valid))
    paddrModule.io.violationCheckLine.get(i) := RegEnable(storeIn(i).bits.wlineflag, storeIn(i).valid)
    maskModule.io.violationMdata(i) := RegEnable(storeIn(i).bits.mask, storeIn(i).valid)

    val addrMaskMatch = paddrModule.io.violationMmask(i).asUInt & maskModule.io.violationMmask(i).asUInt
    val storeRobIdx = RegEnable(storeIn(i).bits.uop.robIdx, storeIn(i).valid)
    val entryNeedCheck = GatedValidRegNext(VecInit((0 until LoadQueueRAWSize).map(j => {
      allocated(j) && storeIn(i).valid && isAfter(uop(j).robIdx, storeIn(i).bits.uop.robIdx) && datavalid(j) && !uop(j).robIdx.needFlush(io.redirect) && !willRevoke(j)
    })))
    val entryStillValid = VecInit((0 until LoadQueueRAWSize).map(j =>
      !uop(j).robIdx.needFlush(io.redirect) && !willRevoke(j)
    ))
    val lqViolationSelVec = VecInit((0 until LoadQueueRAWSize).map(j => {
      addrMaskMatch(j) && entryNeedCheck(j) && entryStillValid(j)
    }))
    val smbWrongBypassSelVec = VecInit((0 until LoadQueueRAWSize).map(j => {
      val predictedStoreRobIdx = uop(j).loadPred.bits.getWaitStoreRobIdx(uop(j).robIdx)
      entryNeedCheck(j) &&
      entryStillValid(j) &&
      smbConsumed(j) &&
      (predictedStoreRobIdx === storeRobIdx) &&
      !addrMaskMatch(j)
    }))
    val hasAnyLqViolation = lqViolationSelVec.asUInt.orR
    val hasAnySmbWrongBypass = smbWrongBypassSelVec.asUInt.orR
    val hasRollbackSourceOverlap = hasAnyLqViolation && hasAnySmbWrongBypass
    (0 until LoadQueueRAWSize).foreach { j =>
      when(entryNeedCheck(j) && smbConsumed(j)) {
        assert(uop(j).loadPred.valid && uop(j).loadPred.bits.smbEnable && !uop(j).loadPred.bits.static,
          s"LoadQueueRAW SMB-tracked entry ${j} must come from a dynamic SMB prediction")
      }
      when(smbWrongBypassSelVec(j)) {
        assert(uop(j).loadPred.bits.getWaitStoreRobIdx(uop(j).robIdx) === storeRobIdx,
          s"LoadQueueRAW wrongBypass entry ${j} must match the predicted wait-store ROB")
        assert(!addrMaskMatch(j),
          s"LoadQueueRAW wrongBypass entry ${j} requires a late verify mismatch")
        assert(!uop(j).robIdx.needFlush(io.redirect) && !willRevoke(j),
          s"LoadQueueRAW wrongBypass entry ${j} must not come from a flushed or revoked SMB-tracked load")
      }
    }
    val rollbackSelVec = VecInit((0 until LoadQueueRAWSize).map(j => {
      lqViolationSelVec(j) || smbWrongBypassSelVec(j)
    }))

    val lqViolationSelUopExts = uop.zip(smbWrongBypassSelVec).map { case (uop, wrongBypass) =>
      val wrapper = Wire(new RollbackEntry)
      wrapper.uop := uop
      wrapper.wrongBypass := wrongBypass
      wrapper
    }

    // select logic
    val lqSelect = selectOldest(rollbackSelVec, lqViolationSelUopExts)

    // select one inst
    val lqViolation = lqSelect._1(0)
    val lqViolationUop = lqSelect._2(0).uop
    val lqWrongBypass = lqSelect._2(0).wrongBypass
    // lqSelect is produced by the multi-cycle oldest-select tree. Keep the
    // triggering store ROB aligned to that select latency before checking the
    // store-driven wrong-bypass ownership invariant.
    val selectedStoreRobIdx = DelayNWithValid(storeIn(i).bits.uop.robIdx, storeIn(i).valid, TotalSelectCycles)._2

    XSDebug(
      lqViolation,
      "need rollback (ld wb before store) pc %x robidx %d target %x\n",
      storeIn(i).bits.uop.pc, storeIn(i).bits.uop.robIdx.asUInt, lqViolationUop.robIdx.asUInt
    )
    when(lqViolation && lqWrongBypass) {
      assert(lqViolationUop.loadPred.valid && lqViolationUop.loadPred.bits.smbEnable && !lqViolationUop.loadPred.bits.static,
        s"LoadQueueRAW wrongBypass on store pipe ${i} requires a dynamic SMB-predicted load")
      assert(lqViolationUop.loadPred.bits.getWaitStoreRobIdx(lqViolationUop.robIdx) === selectedStoreRobIdx,
        s"LoadQueueRAW wrongBypass on store pipe ${i} must target the triggering store")
    }

    (lqViolation, lqViolationUop, lqWrongBypass, hasAnyLqViolation, hasAnySmbWrongBypass, hasRollbackSourceOverlap)
  }

  // select rollback (part1) and generate rollback request
  // rollback check
  // Lq rollback seq check is done in s3 (next stage), as getting rollbackLq MicroOp is slow
  val rollbackLqWb = Wire(Vec(StorePipelineWidth, Valid(new DynInst)))
  val rollbackWrongBypass = Wire(Vec(StorePipelineWidth, Bool()))
  val rollbackHasNormalSource = Wire(Vec(StorePipelineWidth, Bool()))
  val rollbackHasSmbSource = Wire(Vec(StorePipelineWidth, Bool()))
  val rollbackHasSourceOverlap = Wire(Vec(StorePipelineWidth, Bool()))
  val stFtqIdx = Wire(Vec(StorePipelineWidth, new FtqPtr))
  val stFtqOffset = Wire(Vec(StorePipelineWidth, UInt(FetchBlockInstOffsetWidth.W)))
  val stRobIdx = Wire(Vec(StorePipelineWidth, new RobPtr))
  for (w <- 0 until StorePipelineWidth) {
    val detectedRollback = detectRollback(w)
    rollbackLqWb(w).valid := detectedRollback._1 && DelayN(storeIn(w).valid && !storeIn(w).bits.miss, TotalSelectCycles)
    rollbackLqWb(w).bits  := detectedRollback._2
    rollbackWrongBypass(w) := detectedRollback._3 && DelayN(storeIn(w).valid && !storeIn(w).bits.miss, TotalSelectCycles)
    rollbackHasNormalSource(w) := DelayN(detectedRollback._4 && storeIn(w).valid && !storeIn(w).bits.miss, TotalSelectCycles)
    rollbackHasSmbSource(w) := DelayN(detectedRollback._5 && storeIn(w).valid && !storeIn(w).bits.miss, TotalSelectCycles)
    rollbackHasSourceOverlap(w) := DelayN(detectedRollback._6 && storeIn(w).valid && !storeIn(w).bits.miss, TotalSelectCycles)
    stFtqIdx(w) := DelayNWithValid(storeIn(w).bits.uop.ftqPtr, storeIn(w).valid, TotalSelectCycles)._2
    stFtqOffset(w) := DelayNWithValid(storeIn(w).bits.uop.ftqOffset, storeIn(w).valid, TotalSelectCycles)._2
    stRobIdx(w) := DelayNWithValid(storeIn(w).bits.uop.robIdx, storeIn(w).valid, TotalSelectCycles)._2
  }

  // select rollback (part2), generate rollback request, then fire rollback request
  // Note that we use robIdx - 1.U to flush the load instruction itself.
  // Thus, here if last cycle's robIdx equals to this cycle's robIdx, it still triggers the redirect.

  // select uop in parallel

  val storeDrivenRedirects = (0 until StorePipelineWidth).map(i => {
    val redirect = Wire(Valid(new Redirect))
    redirect.valid := rollbackLqWb(i).valid
    redirect.bits             := DontCare
    redirect.bits.isRVC       := rollbackLqWb(i).bits.isRVC
    redirect.bits.robIdx      := rollbackLqWb(i).bits.robIdx
    redirect.bits.ftqIdx      := rollbackLqWb(i).bits.ftqPtr
    redirect.bits.ftqOffset   := rollbackLqWb(i).bits.ftqOffset
    redirect.bits.stFtqIdx    := stFtqIdx(i)
    redirect.bits.stFtqOffset := stFtqOffset(i)
    redirect.bits.level       := RedirectLevel.flush
    redirect.bits.target      := rollbackLqWb(i).bits.pc
    redirect.bits.debug_runahead_checkpoint_id := rollbackLqWb(i).bits.perfDebugInfo.runahead_checkpoint_id
    redirect
  })
  val allRedirectCandidates = storeDrivenRedirects :+ directSmbRedirect :+ fastEnqWrongBypassRedirect
  val oldestOH = Redirect.selectOldestRedirect(allRedirectCandidates)
  val hasAnyRedirect = allRedirectCandidates.map(_.valid).reduce(_ || _)
  val directSmbWinsOldest = directSmbRedirect.valid && hasAnyRedirect && oldestOH(allRedirectCandidates.length - 2)
  val fastEnqWinsOldest = fastEnqWrongBypassRedirect.valid && hasAnyRedirect && oldestOH(allRedirectCandidates.length - 1)
  val oldestRedirect = Wire(Valid(new Redirect))
  oldestRedirect.valid := hasAnyRedirect
  oldestRedirect.bits := DontCare
  when(hasAnyRedirect) {
    oldestRedirect := Mux1H(oldestOH, allRedirectCandidates)
  }
  when(directSmbWinsOldest) {
    when(smbEnqLateMismatchPending(directSmbSelect._2(0).lqIdx)) {
      smbEnqLateMismatchPending(directSmbSelect._2(0).lqIdx) := false.B
    } .otherwise {
      smbDirectVerifyFailIssued(directSmbSelect._2(0).lqIdx) := true.B
    }
  }
  when(fastEnqWinsOldest &&
    fastEnqWrongBypassSelect._2(0).smbWrongBypassReason === SmbWrongBypassReason.lateMismatch) {
    smbEnqLateMismatchPending(fastEnqWrongBypassSelect._2(0).lqIdx) := false.B
  }
  io.rollback.foreach { redirect =>
    redirect.valid := false.B
    redirect.bits := DontCare
  }
  io.rollback(0) := oldestRedirect

  dontTouch(rollbackLqWb)
  dontTouch(rollbackWrongBypass)
  val storeDrivenMdpUpdates = (0 until StorePipelineWidth).map(i => {
    val update = Wire(Valid(new MdpUpdate))
    val predictFromStatic = rollbackLqWb(i).bits.loadPred.bits.static
    update.valid       := rollbackLqWb(i).valid
    update.bits.pc     := rollbackLqWb(i).bits.pc
    update.bits.ftqIdx := rollbackLqWb(i).bits.ftqPtr
    update.bits.ftqOffset := rollbackLqWb(i).bits.ftqOffset
    update.bits.updateType := Mux(predictFromStatic, MdpUpdateType.M_WZ , MdpUpdateType.M_AS)
    update.bits.distance   := update.bits.getDistance(rollbackLqWb(i).bits.robIdx,stRobIdx(i))
    update.bits.smbPredicted := rollbackLqWb(i).bits.loadPred.bits.smbEnable
    update.bits.foundBypassOpportunity := false.B
    update.bits.canBypass := false.B
    update.bits.wrongBypass := rollbackWrongBypass(i)
    update.bits.smbWrongBypassReason := Mux(rollbackWrongBypass(i),
      SmbWrongBypassReason.lateMismatch,
      SmbWrongBypassReason.none)
    update.bits.smbProviderHandle := rollbackLqWb(i).bits.loadPred.bits.smbProviderHandle
    when(update.valid && update.bits.wrongBypass) {
      assert(update.bits.smbPredicted && !predictFromStatic, s"LoadQueueRAW wrongBypass on pipe ${i} requires dynamic SMB prediction")
      assert(rollbackLqWb(i).bits.loadPred.bits.smbProviderHandle.valid,
        s"LoadQueueRAW wrongBypass on pipe ${i} requires provider handle")
      assert(rollbackLqWb(i).bits.loadPred.bits.getWaitStoreRobIdx(rollbackLqWb(i).bits.robIdx) === stRobIdx(i),
        s"LoadQueueRAW wrongBypass on pipe ${i} must point to the triggering store")
    }
    update
  })
  val directSmbUpdate = Wire(Valid(new MdpUpdate))
  directSmbUpdate.valid := directSmbSelect._1(0)
  directSmbUpdate.bits := DontCare
  directSmbUpdate.bits.pc := directSmbSelect._2(0).uop.pc
  directSmbUpdate.bits.ftqIdx := directSmbSelect._2(0).uop.ftqPtr
  directSmbUpdate.bits.ftqOffset := directSmbSelect._2(0).uop.ftqOffset
  directSmbUpdate.bits.updateType := MdpUpdateType.M_AS
  directSmbUpdate.bits.distance := directSmbSelect._2(0).uop.loadPred.bits.distance
  directSmbUpdate.bits.smbPredicted := true.B
  directSmbUpdate.bits.foundBypassOpportunity := false.B
  directSmbUpdate.bits.canBypass := false.B
  directSmbUpdate.bits.wrongBypass := true.B
  directSmbUpdate.bits.smbWrongBypassReason := Mux(
    smbEnqLateMismatchPending(directSmbSelect._2(0).lqIdx),
    SmbWrongBypassReason.lateMismatch,
    SmbWrongBypassReason.verifyFail
  )
  directSmbUpdate.bits.smbProviderHandle := directSmbSelect._2(0).uop.loadPred.bits.smbProviderHandle
  when(directSmbUpdate.valid) {
    assert(directSmbSelect._2(0).uop.loadPred.valid && directSmbSelect._2(0).uop.loadPred.bits.smbEnable && !directSmbSelect._2(0).uop.loadPred.bits.static,
      "LoadQueueRAW direct SMB verify fail requires dynamic SMB prediction")
    assert(directSmbUpdate.bits.distance.orR, "LoadQueueRAW direct SMB verify fail requires dependent distance")
    assert(directSmbUpdate.bits.smbProviderHandle.valid, "LoadQueueRAW direct SMB verify fail requires provider handle")
  }
  val fastEnqWrongBypassUpdate = Wire(Valid(new MdpUpdate))
  fastEnqWrongBypassUpdate.valid := fastEnqWrongBypassSelect._1(0)
  fastEnqWrongBypassUpdate.bits := DontCare
  fastEnqWrongBypassUpdate.bits.pc := fastEnqWrongBypassSelect._2(0).uop.pc
  fastEnqWrongBypassUpdate.bits.ftqIdx := fastEnqWrongBypassSelect._2(0).uop.ftqPtr
  fastEnqWrongBypassUpdate.bits.ftqOffset := fastEnqWrongBypassSelect._2(0).uop.ftqOffset
  fastEnqWrongBypassUpdate.bits.updateType := MdpUpdateType.M_AS
  fastEnqWrongBypassUpdate.bits.distance := fastEnqWrongBypassSelect._2(0).uop.loadPred.bits.distance
  fastEnqWrongBypassUpdate.bits.smbPredicted := true.B
  fastEnqWrongBypassUpdate.bits.foundBypassOpportunity := false.B
  fastEnqWrongBypassUpdate.bits.canBypass := false.B
  fastEnqWrongBypassUpdate.bits.wrongBypass := true.B
  fastEnqWrongBypassUpdate.bits.smbWrongBypassReason := fastEnqWrongBypassSelect._2(0).smbWrongBypassReason
  fastEnqWrongBypassUpdate.bits.smbProviderHandle := fastEnqWrongBypassSelect._2(0).uop.loadPred.bits.smbProviderHandle
  when(fastEnqWrongBypassUpdate.valid) {
    assert(fastEnqWrongBypassSelect._2(0).uop.loadPred.valid &&
      fastEnqWrongBypassSelect._2(0).uop.loadPred.bits.smbEnable &&
      !fastEnqWrongBypassSelect._2(0).uop.loadPred.bits.static,
      "LoadQueueRAW fast enqueue wrong-bypass update requires dynamic SMB prediction")
    assert(fastEnqWrongBypassUpdate.bits.distance.orR,
      "LoadQueueRAW fast enqueue wrong-bypass update requires dependent distance")
    assert(fastEnqWrongBypassUpdate.bits.smbProviderHandle.valid,
      "LoadQueueRAW fast enqueue wrong-bypass update requires provider handle")
  }
  val mdpOldestOH = oldestOH.take(storeDrivenMdpUpdates.length + 2)
  io.mdpUpdateOldest.valid := false.B
  io.mdpUpdateOldest.bits := DontCare
  when(VecInit(mdpOldestOH).asUInt.orR) {
    io.mdpUpdateOldest := Mux1H(mdpOldestOH, storeDrivenMdpUpdates :+ directSmbUpdate :+ fastEnqWrongBypassUpdate)
  }
  XSPerfAccumulate("mdp_update_from_loadQRAW",io.mdpUpdateOldest.valid)
  XSPerfAccumulate("mdp_update_from_loadQRAW_WriteZero",io.mdpUpdateOldest.valid && io.mdpUpdateOldest.bits.updateIsWriteZero)
  XSPerfAccumulate("mdp_update_from_loadQRAW_AllocateStrong",io.mdpUpdateOldest.valid && io.mdpUpdateOldest.bits.updateIsAllocateStrong)
  XSPerfAccumulate("loadQRAW_lq_violation_source_any", PopCount(rollbackHasNormalSource))
  XSPerfAccumulate("loadQRAW_smb_wrong_bypass_verify_fail_source_any",
    PopCount(VecInit((0 until LoadQueueRAWSize).map(j =>
      smbDirectWrongBypassSelVec(j) && smbDirectVerifyFailPendingSelVec(j)
    ))) +
    PopCount(VecInit((0 until LoadPipelineWidth).map(w =>
      fastEnqWrongBypassValidVec(w) &&
      fastEnqWrongBypassReasonVec(w) === SmbWrongBypassReason.verifyFail
    ))))
  XSPerfAccumulate("loadQRAW_smb_wrong_bypass_late_mismatch_source_any",
    PopCount(rollbackHasSmbSource) +
    PopCount(VecInit((0 until LoadQueueRAWSize).map(j =>
      smbDirectWrongBypassSelVec(j) && smbEnqLateMismatchPendingSelVec(j)
    ))) +
    PopCount(VecInit((0 until LoadPipelineWidth).map(w =>
      fastEnqWrongBypassValidVec(w) &&
      fastEnqWrongBypassReasonVec(w) === SmbWrongBypassReason.lateMismatch
    ))))
  XSPerfAccumulate("loadQRAW_rollback_source_overlap", PopCount(rollbackHasSourceOverlap))
  XSPerfAccumulate("loadQRAW_smb_wrong_bypass_oldest",
    io.mdpUpdateOldest.valid && io.mdpUpdateOldest.bits.wrongBypass)
  XSPerfAccumulate("loadQRAW_normal_violation_oldest_with_smb_source",
    io.mdpUpdateOldest.valid && !io.mdpUpdateOldest.bits.wrongBypass && rollbackHasSmbSource.asUInt.orR)
  XSPerfAccumulate("loadQRAW_smb_hold_until_predicted_store_wb", PopCount(VecInit((0 until LoadQueueRAWSize).map(i => {
    val deqNotBlock = Mux(!allAddrCheck, !isBefore(io.stAddrReadySqPtr, uop(i).sqIdx), true.B)
    val needCancel = uop(i).robIdx.needFlush(io.redirect)
    val predictedStoreRobIdx = uop(i).loadPred.bits.getWaitStoreRobIdx(uop(i).robIdx)
    val smbPredictedStoreWriteback = VecInit((0 until StorePipelineWidth).map(w =>
      io.storeIn(w).valid &&
      !io.storeIn(w).bits.miss &&
      (io.storeIn(w).bits.uop.robIdx === predictedStoreRobIdx)
    )).asUInt.orR
    allocated(i) && smbConsumed(i) && deqNotBlock && !smbPredStoreObserved(i) && !smbPredictedStoreWriteback && !needCancel
  }))))
  XSPerfAccumulate("loadQRAW_smb_tracked_entry_active", PopCount(smbConsumed))

  // perf cnt
  val canEnqCount = PopCount(io.query.map(_.req.fire))
  val validCount = freeList.io.validCount
  val allowEnqueue = validCount <= (LoadQueueRAWSize - LoadPipelineWidth).U
  val rollbaclValid = io.rollback.map(_.valid).reduce(_ || _).asUInt

  QueuePerf(LoadQueueRAWSize, validCount, !allowEnqueue)
  XSPerfAccumulate("enqs", canEnqCount)
  XSPerfAccumulate("stld_rollback", rollbaclValid)
  val perfEvents: Seq[(String, UInt)] = Seq(
    ("enq ", canEnqCount),
    ("stld_rollback", rollbaclValid),
  )
  generatePerfEvent()
  // end

  
}
