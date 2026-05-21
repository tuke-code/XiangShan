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

package xiangshan.backend.rename.freelist

import org.chipsalliance.cde.config.Parameters
import chisel3._
import chisel3.util._
import xiangshan._
import utils._
import utility._

object MEFreeListDebugAccounting {
  private def pregIndex[T <: Data](preg: T, maskWidth: Int): UInt = {
    val indexWidth = log2Ceil(maskWidth)
    if (indexWidth == 0) {
      0.U
    } else {
      val raw = preg.asUInt
      if (raw.getWidth <= indexWidth) raw else raw(indexWidth - 1, 0)
    }
  }

  private def pregMask[T <: Data](
    valid: Vec[Bool],
    preg: Vec[T],
    maskWidth: Int
  ): UInt = {
    val masks = (0 until valid.length).map { idx =>
      Mux(valid(idx), UIntToOH(pregIndex(preg(idx), maskWidth), maskWidth), 0.U(maskWidth.W))
    }
    masks.reduce(_ | _)
  }

  def uniqueArchRat[T <: Data](archRat: Vec[T]): Vec[Bool] =
    VecInit(Seq.tabulate(archRat.length) {
      case 0 => true.B
      case idx => !VecInit((0 until idx).map(prev => archRat(prev).asUInt === archRat(idx).asUInt)).asUInt.orR
    })

  def archRatFreeListOverlap[T <: Data, U <: Data](
    uniqueArchRat: Vec[Bool],
    archRat: Vec[T],
    freeValid: Vec[Bool],
    freePreg: Vec[U],
    allowOverlap: Bool
  ): UInt = {
    val overlap = VecInit((0 until archRat.length).map { idx =>
      val freeHit = VecInit((0 until freeValid.length).map { freeIdx =>
        freeValid(freeIdx) && freePreg(freeIdx).asUInt === archRat(idx).asUInt
      }).asUInt.orR
      allowOverlap && uniqueArchRat(idx) && freeHit
    })
    PopCount(overlap)
  }

  def archFreePlusRatTotal[T <: Data, U <: Data](
    archFreeCount: UInt,
    uniqueArchRat: Vec[Bool],
    archRat: Vec[T],
    freeValid: Vec[Bool],
    freePreg: Vec[U],
    allowOverlap: Bool
  ): UInt =
    archFreeCount +& PopCount(uniqueArchRat) - archRatFreeListOverlap(
      uniqueArchRat = uniqueArchRat,
      archRat = archRat,
      freeValid = freeValid,
      freePreg = freePreg,
      allowOverlap = allowOverlap
    )

  def archRatMaskOverlap[T <: Data](
    uniqueArchRat: Vec[Bool],
    archRat: Vec[T],
    overlapMask: UInt,
    allowOverlap: Bool,
    maskWidth: Int
  ): UInt = {
    val overlap = VecInit((0 until archRat.length).map { idx =>
      val archRatHit = (overlapMask & UIntToOH(pregIndex(archRat(idx), maskWidth), maskWidth)).orR
      allowOverlap && uniqueArchRat(idx) && archRatHit
    })
    PopCount(overlap)
  }

  def archFreePlusRatTotalWithOverlapMask[T <: Data](
    archFreeCount: UInt,
    uniqueArchRat: Vec[Bool],
    archRat: Vec[T],
    overlapMask: UInt,
    allowOverlap: Bool,
    maskWidth: Int
  ): UInt =
    archFreeCount +& PopCount(uniqueArchRat) - archRatMaskOverlap(
      uniqueArchRat = uniqueArchRat,
      archRat = archRat,
      overlapMask = overlapMask,
      allowOverlap = allowOverlap,
      maskWidth = maskWidth
    )

  def nextPendingEarlyReleaseCreditCount(
    currentCount: UInt,
    releaseCount: UInt,
    resolveCount: UInt
  ): UInt = {
    val countBeforeResolve = currentCount +& releaseCount
    Mux(resolveCount >= countBeforeResolve, 0.U, countBeforeResolve - resolveCount)
  }

  def archFreePlusRatTotalWithPendingEarlyReleaseCredits(
    archFreeCount: UInt,
    uniqueArchRat: Vec[Bool],
    pendingCreditCount: UInt,
    allowOverlap: Bool
  ): UInt = {
    val baseTotal = archFreeCount +& PopCount(uniqueArchRat)
    val pendingCredits = Mux(allowOverlap, pendingCreditCount, 0.U)
    baseTotal - pendingCredits
  }

  def nextEarlyReleaseOverlapMask[T <: Data, U <: Data, V <: Data](
    currentMask: UInt,
    releaseValid: Vec[Bool],
    releasePreg: Vec[T],
    allocateValid: Vec[Bool],
    allocatePreg: Vec[U],
    archRat: Vec[V],
    maskWidth: Int
  ): UInt = {
    val releaseMask = pregMask(releaseValid, releasePreg, maskWidth)
    val allocateMask = pregMask(allocateValid, allocatePreg, maskWidth)
    val archRatMask = pregMask(VecInit(Seq.fill(archRat.length)(true.B)), archRat, maskWidth)

    ((currentMask | releaseMask) & archRatMask) & ~allocateMask
  }

  def ptrRangeMask[T <: Data, U <: Data](
    tailPtrFlag: Bool,
    tailPtrValue: T,
    headPtrFlag: Bool,
    headPtrValue: U,
    maskWidth: Int
  ): UInt = {
    val tail = pregIndex(tailPtrValue, maskWidth)
    val head = pregIndex(headPtrValue, maskWidth)
    val bits = VecInit(Seq.tabulate(maskWidth) { idx =>
      val idxUInt = idx.U(log2Ceil(maskWidth).W)
      Mux(
        tailPtrFlag === headPtrFlag,
        idxUInt >= head && idxUInt < tail,
        idxUInt >= head || idxUInt < tail
      )
    })
    bits.asUInt
  }

  def nextPendingEarlyReleaseCreditMask[T <: Data, U <: Data](
    currentMask: UInt,
    releaseValid: Vec[Bool],
    releasePreg: Vec[T],
    resolveValid: Vec[Bool],
    resolvePreg: Vec[U],
    allocateValid: Vec[Bool],
    allocatePreg: Vec[U],
    maskWidth: Int
  ): UInt = {
    val releaseMask = pregMask(releaseValid, releasePreg, maskWidth)
    val resolveMask = pregMask(resolveValid, resolvePreg, maskWidth)
    val allocateMask = pregMask(allocateValid, allocatePreg, maskWidth)
    val pendingBeforeAllocate = (currentMask | releaseMask) & ~resolveMask
    pendingBeforeAllocate & ~allocateMask
  }

  def pendingEarlyReleaseCreditMaskClearedByAllocate[T <: Data, U <: Data](
    currentMask: UInt,
    releaseValid: Vec[Bool],
    releasePreg: Vec[T],
    resolveValid: Vec[Bool],
    resolvePreg: Vec[U],
    allocateValid: Vec[Bool],
    allocatePreg: Vec[U],
    maskWidth: Int
  ): UInt = {
    val releaseMask = pregMask(releaseValid, releasePreg, maskWidth)
    val resolveMask = pregMask(resolveValid, resolvePreg, maskWidth)
    val allocateMask = pregMask(allocateValid, allocatePreg, maskWidth)
    val pendingBeforeAllocate = (currentMask | releaseMask) & ~resolveMask
    pendingBeforeAllocate & allocateMask
  }

  def pendingEarlyReleaseCreditMaskClearedByResolve[T <: Data, U <: Data](
    currentMask: UInt,
    releaseValid: Vec[Bool],
    releasePreg: Vec[T],
    resolveValid: Vec[Bool],
    resolvePreg: Vec[U],
    maskWidth: Int
  ): UInt = {
    val releaseMask = pregMask(releaseValid, releasePreg, maskWidth)
    val resolveMask = pregMask(resolveValid, resolvePreg, maskWidth)
    (currentMask | releaseMask) & resolveMask
  }

  def pendingEarlyReleaseArchOverlap[T <: Data, U <: Data](
    pendingMask: UInt,
    uniqueArchRat: Vec[Bool],
    archRat: Vec[T],
    archFreeValid: Vec[Bool],
    archFreePreg: Vec[U],
    allowOverlap: Bool,
    maskWidth: Int
  ): UInt = {
    val archFreeMask = pregMask(archFreeValid, archFreePreg, maskWidth)
    val pendingMaskFixed = Wire(UInt(maskWidth.W))
    pendingMaskFixed := pendingMask
    val pendingOverlapMask = pendingMaskFixed & archFreeMask

    Mux(allowOverlap, PopCount(pendingOverlapMask), 0.U)
  }

  def pendingEarlyReleaseCreditNeedMask[T <: Data, U <: Data](
    uniqueArchRat: Vec[Bool],
    archRat: Vec[T],
    archFreeValid: Vec[Bool],
    archFreePreg: Vec[U],
    allowOverlap: Bool,
    maskWidth: Int
  ): UInt = {
    val archFreeMask = pregMask(archFreeValid, archFreePreg, maskWidth)
    val archRatMask = pregMask(uniqueArchRat, archRat, maskWidth)
    val seenArchFreeMask = Wire(Vec(archFreeValid.length + 1, UInt(maskWidth.W)))
    val duplicateArchFreeMaskVec = Wire(Vec(archFreeValid.length, UInt(maskWidth.W)))
    seenArchFreeMask(0) := 0.U
    for (idx <- 0 until archFreeValid.length) {
      val currentMask = Mux(
        archFreeValid(idx),
        UIntToOH(pregIndex(archFreePreg(idx), maskWidth), maskWidth),
        0.U(maskWidth.W)
      )
      duplicateArchFreeMaskVec(idx) := currentMask & seenArchFreeMask(idx)
      seenArchFreeMask(idx + 1) := seenArchFreeMask(idx) | currentMask
    }
    val duplicateArchFreeMask = duplicateArchFreeMaskVec.reduce(_ | _)
    val neededByDelayedView = duplicateArchFreeMask | (archFreeMask & archRatMask)

    Mux(allowOverlap, neededByDelayedView, 0.U(maskWidth.W))
  }

  def archFreePlusRatTotalWithPendingEarlyReleaseMask[T <: Data, U <: Data](
    archFreeCount: UInt,
    uniqueArchRat: Vec[Bool],
    archRat: Vec[T],
    archFreeValid: Vec[Bool],
    archFreePreg: Vec[U],
    pendingMask: UInt,
    allowOverlap: Bool,
    maskWidth: Int
  ): UInt =
    archFreeCount +& PopCount(uniqueArchRat) - pendingEarlyReleaseArchOverlap(
      pendingMask = pendingMask,
      uniqueArchRat = uniqueArchRat,
      archRat = archRat,
      archFreeValid = archFreeValid,
      archFreePreg = archFreePreg,
      allowOverlap = allowOverlap,
      maskWidth = maskWidth
    )

  def archFreePlusRatTotalWithSplitPendingEarlyReleaseMasks[T <: Data, U <: Data](
    archFreeCount: UInt,
    uniqueArchRat: Vec[Bool],
    archRat: Vec[T],
    archFreeValid: Vec[Bool],
    archFreePreg: Vec[U],
    activePendingMask: UInt,
    heldPendingMask: UInt,
    resolvedPendingMask: UInt,
    allowOverlap: Bool,
    maskWidth: Int
  ): UInt = {
    val resolvedPendingMaskDisjoint = resolvedPendingMask & ~(activePendingMask | heldPendingMask)

    archFreeCount +& PopCount(uniqueArchRat) -
      pendingEarlyReleaseArchOverlap(
        pendingMask = activePendingMask,
        uniqueArchRat = uniqueArchRat,
        archRat = archRat,
        archFreeValid = archFreeValid,
        archFreePreg = archFreePreg,
        allowOverlap = allowOverlap,
        maskWidth = maskWidth
      ) -
      pendingEarlyReleaseArchOverlap(
        pendingMask = heldPendingMask,
        uniqueArchRat = uniqueArchRat,
        archRat = archRat,
        archFreeValid = archFreeValid,
        archFreePreg = archFreePreg,
        allowOverlap = allowOverlap,
        maskWidth = maskWidth
      ) -
      pendingEarlyReleaseArchOverlap(
        pendingMask = resolvedPendingMaskDisjoint,
        uniqueArchRat = uniqueArchRat,
        archRat = archRat,
        archFreeValid = archFreeValid,
        archFreePreg = archFreePreg,
        allowOverlap = allowOverlap,
        maskWidth = maskWidth
      )
  }

}

class MEFreeList(size: Int, commitWidth: Int)(implicit p: Parameters) extends BaseFreeList(size, commitWidth) with HasPerfEvents {
  val freeList = RegInit(VecInit(
    // originally {1, 2, ..., size - 1} are free. Register 0-31 are mapped to x0.
    Seq.tabulate(size - 1)(i => (i + 1).U(PhyRegIdxWidth.W)) :+ 0.U(PhyRegIdxWidth.W)))

  val tailPtr = RegInit(FreeListPtr(false, size - 1))

  val doWalkRename = io.walk && io.doAllocate && !io.redirect
  val doNormalRename = io.canAllocate && io.doAllocate && !io.redirect
  val doRename = doWalkRename || doNormalRename
  val doCommit = io.commit.doCommit

  val freeListVec = Wire(Vec(size, Vec(RenameWidth, UInt(PhyRegIdxWidth.W))))
  for (i <- 0 until size) {
    for (j <- 0 until RenameWidth) {
      if (i + j > (size - 1)) {
        freeListVec(i)(j) := freeList(i + j - size)
      } else {
        freeListVec(i)(j) := freeList(i + j)
      }
    }
  }

  /**
    * Allocation: from freelist (same as StdFreelist)
    */
  val phyRegCandidates = Mux1H(headPtrOHVec(0), freeListVec)
  for (i <- 0 until RenameWidth) {
    // enqueue instr, is move elimination
    io.allocatePhyReg(i) := phyRegCandidates(PopCount(io.allocateReq.take(i)))
  }
  // update arch head pointer
  val archAlloc = io.commit.archAlloc

  val numArchAllocate = PopCount(archAlloc)
  val archHeadPtrNew  = archHeadPtr + numArchAllocate
  val archHeadPtrNext = Mux(doCommit, archHeadPtrNew, archHeadPtr)
  archHeadPtr := archHeadPtrNext

  // update head pointer
  val numAllocate = Mux(io.walk, PopCount(io.walkReq), PopCount(io.allocateReq))
  val headPtrNew   = Mux(lastCycleRedirect, redirectedHeadPtr, headPtr + numAllocate)
  val headPtrOHNew = Mux(lastCycleRedirect, redirectedHeadPtrOH, headPtrOHVec(numAllocate))
  val headPtrNext   = Mux(doRename, headPtrNew, headPtr)
  val headPtrOHNext = Mux(doRename, headPtrOHNew, headPtrOH)
  headPtr   := headPtrNext
  headPtrOH := headPtrOHNext

  /**
    * Deallocation: when refCounter becomes zero, the register can be released to freelist
    */
  val freePtr = VecInit(Seq.tabulate(commitWidth)(i => tailPtr + PopCount(io.freeReq.take(i))))
  for (i <- 0 until size) {
    val freeReqOH = VecInit(io.freeReq.zipWithIndex.map { case (w, idx) =>
      w && freePtr(idx).value === i.U
    })
    val freePhyReg = Mux1H(freeReqOH, io.freePhyReg)
    when(freeReqOH.asUInt.orR) {
      freeList(i) := freePhyReg
    }
  }

  // update tail pointer
  val tailPtrNext = tailPtr + PopCount(io.freeReq)
  tailPtr := tailPtrNext

  val freeRegCnt = Mux(doWalkRename && !lastCycleRedirect, distanceBetween(tailPtrNext, headPtr) - PopCount(io.walkReq),
                   Mux(doNormalRename,                     distanceBetween(tailPtrNext, headPtr) - PopCount(io.allocateReq),
                                                           distanceBetween(tailPtrNext, headPtr)))
  val freeRegCntReg = RegNext(freeRegCnt)
  io.canAllocate := freeRegCntReg >= RenameWidth.U

  if(backendParams.debugEn){
    val debugArchHeadPtr = RegNext(RegNext(archHeadPtr, FreeListPtr(false, 0)), FreeListPtr(false, 0)) // two-cycle delay from refCounter
    val debugArchRAT = RegNext(RegNext(io.debug_rat.get, VecInit(Seq.fill(32)(0.U(PhyRegIdxWidth.W)))), VecInit(Seq.fill(32)(0.U(PhyRegIdxWidth.W))))
    val debugUniqPR = MEFreeListDebugAccounting.uniqueArchRat(debugArchRAT)
    val debugEarlyReleaseValid = VecInit((0 until commitWidth).map(i => io.freeReq(i) && io.debugEarlyRelease(i)))
    val debugEarlyReleaseResolveValid = VecInit((0 until commitWidth).map(i => io.debugEarlyReleaseResolve(i)))
    val debugAllocateValid = VecInit((0 until RenameWidth).map(i => doRename && Mux(io.walk, io.walkReq(i), io.allocateReq(i))))
    val debugPendingEarlyReleaseCreditMask = RegInit(0.U(size.W))
    val debugPendingEarlyReleaseCreditMaskNext = MEFreeListDebugAccounting.nextPendingEarlyReleaseCreditMask(
      currentMask = debugPendingEarlyReleaseCreditMask,
      releaseValid = debugEarlyReleaseValid,
      releasePreg = io.freePhyReg,
      resolveValid = debugEarlyReleaseResolveValid,
      resolvePreg = io.debugEarlyReleaseResolvePreg,
      allocateValid = debugAllocateValid,
      allocatePreg = io.allocatePhyReg,
      maskWidth = size
    )
    val debugAllocatedPendingEarlyReleaseCreditMask = MEFreeListDebugAccounting.pendingEarlyReleaseCreditMaskClearedByAllocate(
      currentMask = debugPendingEarlyReleaseCreditMask,
      releaseValid = debugEarlyReleaseValid,
      releasePreg = io.freePhyReg,
      resolveValid = debugEarlyReleaseResolveValid,
      resolvePreg = io.debugEarlyReleaseResolvePreg,
      allocateValid = debugAllocateValid,
      allocatePreg = io.allocatePhyReg,
      maskWidth = size
    )
    val debugResolvedPendingEarlyReleaseCreditMask = MEFreeListDebugAccounting.pendingEarlyReleaseCreditMaskClearedByResolve(
      currentMask = debugPendingEarlyReleaseCreditMask,
      releaseValid = debugEarlyReleaseValid,
      releasePreg = io.freePhyReg,
      resolveValid = debugEarlyReleaseResolveValid,
      resolvePreg = io.debugEarlyReleaseResolvePreg,
      maskWidth = size
    )
    val debugAllocatedPendingEarlyReleaseCreditMaskDelay1 = RegNext(debugAllocatedPendingEarlyReleaseCreditMask, 0.U(size.W))
    val debugAllocatedPendingEarlyReleaseCreditMaskDelay2 = RegNext(debugAllocatedPendingEarlyReleaseCreditMaskDelay1, 0.U(size.W))
    debugPendingEarlyReleaseCreditMask := debugPendingEarlyReleaseCreditMaskNext
    val debugFreeListAfterDealloc = WireInit(freeList)
    for (i <- 0 until commitWidth) {
      when (io.freeReq(i)) {
        val freePtr = tailPtr + PopCount(io.freeReq.take(i))
        debugFreeListAfterDealloc(freePtr.value) := io.freePhyReg(i)
      }
    }
    val debugArchFreeMask = MEFreeListDebugAccounting.ptrRangeMask(
      tailPtrFlag = tailPtrNext.flag,
      tailPtrValue = tailPtrNext.value,
      headPtrFlag = debugArchHeadPtr.flag,
      headPtrValue = debugArchHeadPtr.value,
      maskWidth = size
    )
    val debugPendingEarlyReleaseCreditNeedMask = MEFreeListDebugAccounting.pendingEarlyReleaseCreditNeedMask(
      uniqueArchRat = debugUniqPR,
      archRat = debugArchRAT,
      archFreeValid = VecInit(Seq.tabulate(size)(idx => debugArchFreeMask(idx))),
      archFreePreg = debugFreeListAfterDealloc,
      allowOverlap = backendParams.enableIntEarlyRelease.B,
      maskWidth = size
    )
    val debugAllocatedPendingEarlyReleaseCreditMaskHeld = RegInit(0.U(size.W))
    val debugAllocatedPendingEarlyReleaseCreditMaskHeldNext =
      (debugAllocatedPendingEarlyReleaseCreditMaskHeld |
        debugAllocatedPendingEarlyReleaseCreditMask |
        debugAllocatedPendingEarlyReleaseCreditMaskDelay1 |
        debugAllocatedPendingEarlyReleaseCreditMaskDelay2) &
        debugPendingEarlyReleaseCreditNeedMask
    debugAllocatedPendingEarlyReleaseCreditMaskHeld := debugAllocatedPendingEarlyReleaseCreditMaskHeldNext
    val debugResolvedPendingEarlyReleaseCreditMaskHeld = RegInit(0.U(size.W))
    val debugResolvedPendingEarlyReleaseCreditMaskHeldNext =
      (debugResolvedPendingEarlyReleaseCreditMaskHeld |
        debugResolvedPendingEarlyReleaseCreditMask) &
        debugPendingEarlyReleaseCreditNeedMask
    debugResolvedPendingEarlyReleaseCreditMaskHeld := debugResolvedPendingEarlyReleaseCreditMaskHeldNext
    val debugPendingEarlyReleaseCreditMaskForCheck = Wire(UInt(size.W))
    debugPendingEarlyReleaseCreditMaskForCheck :=
      debugPendingEarlyReleaseCreditMaskNext |
        debugAllocatedPendingEarlyReleaseCreditMask |
        debugAllocatedPendingEarlyReleaseCreditMaskDelay1 |
        debugAllocatedPendingEarlyReleaseCreditMaskDelay2 |
        debugAllocatedPendingEarlyReleaseCreditMaskHeldNext |
        debugResolvedPendingEarlyReleaseCreditMaskHeldNext
    val debugActivePendingEarlyReleaseCreditMaskForCheck = Wire(UInt(size.W))
    debugActivePendingEarlyReleaseCreditMaskForCheck :=
      debugPendingEarlyReleaseCreditMaskNext & debugPendingEarlyReleaseCreditNeedMask
    val debugResolvedPendingEarlyReleaseCreditMaskForCheck = Wire(UInt(size.W))
    debugResolvedPendingEarlyReleaseCreditMaskForCheck := debugResolvedPendingEarlyReleaseCreditMaskHeldNext
    val debugTotal = MEFreeListDebugAccounting.archFreePlusRatTotalWithSplitPendingEarlyReleaseMasks(
      archFreeCount = distanceBetween(tailPtrNext, debugArchHeadPtr),
      uniqueArchRat = debugUniqPR,
      archRat = debugArchRAT,
      archFreeValid = VecInit(Seq.tabulate(size)(idx => debugArchFreeMask(idx))),
      archFreePreg = debugFreeListAfterDealloc,
      activePendingMask = debugActivePendingEarlyReleaseCreditMaskForCheck,
      heldPendingMask = debugAllocatedPendingEarlyReleaseCreditMaskHeldNext,
      resolvedPendingMask = debugResolvedPendingEarlyReleaseCreditMaskForCheck,
      allowOverlap = backendParams.enableIntEarlyRelease.B,
      maskWidth = size
    )
    XSError(debugTotal =/= size.U, "Integer physical register should be in either arch RAT or arch free list\n")
  }

  QueuePerf(size = size, utilization = freeRegCntReg, full = freeRegCntReg === 0.U)

  XSPerfAccumulate("allocation_blocked_cycle", !io.canAllocate)
  XSPerfAccumulate("can_alloc_wrong", !io.canAllocate && freeRegCnt >= RenameWidth.U)

  val perfEvents = Seq(
    ("me_freelist_1_4_valid", freeRegCntReg <  (size / 4).U                                     ),
    ("me_freelist_2_4_valid", freeRegCntReg >= (size / 4).U && freeRegCntReg <= (size / 2).U    ),
    ("me_freelist_3_4_valid", freeRegCntReg >= (size / 2).U && freeRegCntReg <= (size * 3 / 4).U),
    ("me_freelist_4_4_valid", freeRegCntReg >= (size * 3 / 4).U                                 ),
  )
  generatePerfEvent()
}
