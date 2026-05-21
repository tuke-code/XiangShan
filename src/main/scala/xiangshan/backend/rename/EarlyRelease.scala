package xiangshan.backend.rename

import org.chipsalliance.cde.config.Parameters
import chisel3._
import chisel3.util._
import xiangshan._
import xiangshan.RabCommitInfo
import xiangshan.backend.issue.SchdBlockParams

object EarlyReleaseOwner {
  private def robIdxWidth(implicit p: Parameters): Int = log2Ceil(p(XSCoreParamsKey).RobSize)
  def generationBits: Int = 40
  def width(implicit p: Parameters): Int = robIdxWidth + generationBits
}

object NewErPerfDebug {
  def countWhenEnabled(enable: Bool, events: Seq[Bool]): UInt = {
    val count = if (events.isEmpty) 0.U(1.W) else PopCount(events)
    Mux(enable, count, 0.U(count.getWidth.W))
  }

  def duplicateReleaseError(enable: Bool, duplicate: Bool): Bool =
    enable && duplicate

  def releaseOverflowError(enable: Bool, overflow: Bool): Bool =
    enable && overflow

  def rejectedDecrement(rejected: UserCountTableRejects): Bool =
    rejected.readComplete || rejected.cancelSourceUse || rejected.nonSpecRedefine

  def underflowError(enable: Bool, underflow: Bool): Bool =
    enable && underflow
}

class EarlyReleaseSrcInfo(implicit p: Parameters) extends XSBundle {
  val valid = Bool()
  val preg = UInt(PhyRegIdxWidth.W)
  val producerOwner = UInt(EarlyReleaseOwner.width.W)
  val readDone = Bool()
}

class EarlyReleaseMetadata(implicit p: Parameters) extends XSBundle {
  def numIntSrc = backendParams.numIntRegSrc

  val oldIntPdestValid = Bool()
  val oldIntPdest = UInt(PhyRegIdxWidth.W)
  val redefinerRobIdx = UInt(EarlyReleaseOwner.width.W)
  val countedIntSrcs = Vec(numIntSrc, new EarlyReleaseSrcInfo)
  val earlyReleasedOldPdest = Bool()
  val suppressTraditionalRelease = Bool()
  val commitOnlyFallback = Bool()
}

class EarlyReleaseReleaseKey(val pregWidth: Int, val ownerWidth: Int) extends Bundle {
  val preg = UInt(pregWidth.W)
  val owner = UInt(ownerWidth.W)
}

class EarlyReleaseReleaseArbiter(
  earlyWidth: Int,
  traditionalWidth: Int,
  outputWidth: Int,
  pregWidth: Int,
  ownerWidth: Int,
  ledgerEntries: Int,
  killWidth: Int,
  allocWidth: Int,
  suppressionPregs: Int = -1
) extends Module {
  require(earlyWidth > 0)
  require(traditionalWidth > 0)
  require(outputWidth > 0)
  require(pregWidth > 0)
  require(ownerWidth > 0)
  require(ledgerEntries > 0)
  require(killWidth > 0)
  require(allocWidth > 0)
  require(suppressionPregs == -1 || suppressionPregs > 0)
  require(suppressionPregs == -1 || suppressionPregs <= (1 << pregWidth))

  private val keyType = new EarlyReleaseReleaseKey(pregWidth, ownerWidth)

  val io = IO(new Bundle {
    val early = Input(Vec(earlyWidth, Valid(keyType)))
    val traditional = Input(Vec(traditionalWidth, Valid(keyType)))
    val alloc = Input(Vec(allocWidth, Valid(UInt(pregWidth.W))))
    val killOwner = Input(Vec(killWidth, Valid(UInt(ownerWidth.W))))
    val release = Output(Vec(outputWidth, Valid(keyType)))
    val releaseIsEarly = Output(Vec(outputWidth, Bool()))
    val earlyAccepted = Output(Vec(earlyWidth, Bool()))
    val traditionalSuppressed = Output(Vec(traditionalWidth, Bool()))
    val traditionalAccepted = Output(Vec(traditionalWidth, Bool()))
    val duplicate = Output(Bool())
    val overflow = Output(Bool())
  })

  private val ledgerValid = RegInit(VecInit(Seq.fill(ledgerEntries)(false.B)))
  private val ledger = RegInit(VecInit(Seq.fill(ledgerEntries)(0.U.asTypeOf(keyType))))
  private val traditionalLedgerValid = RegInit(VecInit(Seq.fill(ledgerEntries)(false.B)))
  private val traditionalLedger = RegInit(VecInit(Seq.fill(ledgerEntries)(0.U.asTypeOf(keyType))))

  private def sameKey(lhs: EarlyReleaseReleaseKey, rhs: EarlyReleaseReleaseKey): Bool =
    lhs.preg === rhs.preg && lhs.owner === rhs.owner

  private def samePreg(lhs: EarlyReleaseReleaseKey, rhs: EarlyReleaseReleaseKey): Bool =
    lhs.preg === rhs.preg

  private def prefixCounts(valids: Seq[Bool]): Vec[UInt] = {
    val countWidth = log2Ceil(valids.length + 1).max(1)
    val counts = Wire(Vec(valids.length + 1, UInt(countWidth.W)))
    counts(0) := 0.U
    for (idx <- valids.indices) {
      counts(idx + 1) := counts(idx) + valids(idx).asUInt
    }
    counts
  }

  private val physicalSuppressionEntries = ledgerEntries + earlyWidth
  private val earlyReleasedPregPendingValid = RegInit(VecInit(Seq.fill(physicalSuppressionEntries)(false.B)))
  private val earlyReleasedPregPendingPreg = RegInit(VecInit(Seq.fill(physicalSuppressionEntries)(0.U(pregWidth.W))))
  private val postAllocSuppressionTail0Valid = RegInit(VecInit(Seq.fill(physicalSuppressionEntries)(false.B)))
  private val postAllocSuppressionTail0Preg = RegInit(VecInit(Seq.fill(physicalSuppressionEntries)(0.U(pregWidth.W))))
  private val postAllocSuppressionTail1Valid = RegInit(VecInit(Seq.fill(physicalSuppressionEntries)(false.B)))
  private val postAllocSuppressionTail1Preg = RegInit(VecInit(Seq.fill(physicalSuppressionEntries)(0.U(pregWidth.W))))

  private def allocPregMatch(preg: UInt): Bool =
    VecInit(io.alloc.map(alloc => alloc.valid && alloc.bits === preg)).asUInt.orR

  private def pendingSuppressionMatch(preg: UInt): Bool =
    VecInit((0 until physicalSuppressionEntries).map { entryIdx =>
      earlyReleasedPregPendingValid(entryIdx) &&
        !allocPregMatch(earlyReleasedPregPendingPreg(entryIdx)) &&
        earlyReleasedPregPendingPreg(entryIdx) === preg
    }).asUInt.orR

  private def physicalSuppressionMatch(preg: UInt): Bool = {
    val pendingHit = VecInit((0 until physicalSuppressionEntries).map { entryIdx =>
      earlyReleasedPregPendingValid(entryIdx) && earlyReleasedPregPendingPreg(entryIdx) === preg
    }).asUInt.orR
    val tail0Hit = VecInit((0 until physicalSuppressionEntries).map { entryIdx =>
      postAllocSuppressionTail0Valid(entryIdx) && postAllocSuppressionTail0Preg(entryIdx) === preg
    }).asUInt.orR
    val tail1Hit = VecInit((0 until physicalSuppressionEntries).map { entryIdx =>
      postAllocSuppressionTail1Valid(entryIdx) && postAllocSuppressionTail1Preg(entryIdx) === preg
    }).asUInt.orR
    pendingHit || tail0Hit || tail1Hit
  }

  private val pendingSuppressionSurvives = Wire(Vec(physicalSuppressionEntries, Bool()))
  private val pendingSuppressionFreeSlot = Wire(Vec(physicalSuppressionEntries, Bool()))
  for (entryIdx <- 0 until physicalSuppressionEntries) {
    pendingSuppressionSurvives(entryIdx) := earlyReleasedPregPendingValid(entryIdx) &&
      !allocPregMatch(earlyReleasedPregPendingPreg(entryIdx))
    pendingSuppressionFreeSlot(entryIdx) := !pendingSuppressionSurvives(entryIdx)
  }
  private val pendingSuppressionFreeSlotCount = PopCount(pendingSuppressionFreeSlot)

  private val pendingCapacity = earlyWidth + traditionalWidth
  private val pendingValid = RegInit(VecInit(Seq.fill(pendingCapacity)(false.B)))
  private val pendingBits = RegInit(VecInit(Seq.fill(pendingCapacity)(0.U.asTypeOf(keyType))))
  private val pendingIsEarly = RegInit(VecInit(Seq.fill(pendingCapacity)(false.B)))

  private def ownerKilled(owner: UInt): Bool =
    VecInit(io.killOwner.map(kill => kill.valid && kill.bits === owner)).asUInt.orR

  private val pendingLiveValid = Wire(Vec(pendingCapacity, Bool()))
  for (idx <- 0 until pendingCapacity) {
    pendingLiveValid(idx) := pendingValid(idx) && !(pendingIsEarly(idx) && ownerKilled(pendingBits(idx).owner))
  }
  private val pendingLiveCount = PopCount(pendingLiveValid)

  private val ledgerMatch = Wire(Vec(traditionalWidth, Bool()))
  private val ledgerHit = Wire(Vec(traditionalWidth, Vec(ledgerEntries, Bool())))
  private val pendingPregMatch = Wire(Vec(traditionalWidth, Bool()))
  private val tradPreNoSameCycle = Wire(Vec(traditionalWidth, Bool()))

  for (tradIdx <- 0 until traditionalWidth) {
    for (entryIdx <- 0 until ledgerEntries) {
      ledgerHit(tradIdx)(entryIdx) := io.traditional(tradIdx).valid &&
        ledgerValid(entryIdx) &&
        sameKey(ledger(entryIdx), io.traditional(tradIdx).bits)
    }
    ledgerMatch(tradIdx) := ledgerHit(tradIdx).asUInt.orR
    pendingPregMatch(tradIdx) := io.traditional(tradIdx).valid &&
      physicalSuppressionMatch(io.traditional(tradIdx).bits.preg)
    tradPreNoSameCycle(tradIdx) := io.traditional(tradIdx).valid &&
      !ledgerMatch(tradIdx) &&
      !pendingPregMatch(tradIdx)
  }

  private val clearLedger = Wire(Vec(ledgerEntries, Bool()))
  private val clearTraditionalLedger = Wire(Vec(ledgerEntries, Bool()))
  for (entryIdx <- 0 until ledgerEntries) {
    val consumed = VecInit((0 until traditionalWidth).map(tradIdx => ledgerHit(tradIdx)(entryIdx))).asUInt.orR
    val killed = VecInit(io.killOwner.map(kill =>
      kill.valid && ledgerValid(entryIdx) && ledger(entryIdx).owner === kill.bits
    )).asUInt.orR
    clearLedger(entryIdx) := consumed || killed
    val traditionalKilled = VecInit(io.killOwner.map(kill =>
      kill.valid && traditionalLedgerValid(entryIdx) && traditionalLedger(entryIdx).owner === kill.bits
    )).asUInt.orR
    val consumedByLateEarly = VecInit(io.early.map(early =>
      early.valid && traditionalLedgerValid(entryIdx) && sameKey(traditionalLedger(entryIdx), early.bits)
    )).asUInt.orR
    val traditionalAllocatedPreg =
      traditionalLedgerValid(entryIdx) && allocPregMatch(traditionalLedger(entryIdx).preg)
    clearTraditionalLedger(entryIdx) := traditionalKilled || consumedByLateEarly || traditionalAllocatedPreg
  }

  private val ledgerValidAfterClear = Wire(Vec(ledgerEntries, Bool()))
  private val traditionalLedgerValidAfterClear = Wire(Vec(ledgerEntries, Bool()))
  for (entryIdx <- 0 until ledgerEntries) {
    ledgerValidAfterClear(entryIdx) := ledgerValid(entryIdx) && !clearLedger(entryIdx)
    traditionalLedgerValidAfterClear(entryIdx) := traditionalLedgerValid(entryIdx) && !clearTraditionalLedger(entryIdx)
  }

  private val freeSlot = Wire(Vec(ledgerEntries, Bool()))
  for (entryIdx <- 0 until ledgerEntries) {
    freeSlot(entryIdx) := !ledgerValidAfterClear(entryIdx)
  }
  private val freeSlotCount = PopCount(freeSlot)

  private val currentCapacity = (pendingCapacity + outputWidth).U - pendingLiveCount
  private val tradReserveCount = PopCount(tradPreNoSameCycle)
  private val tradReserveFits = tradReserveCount <= currentCapacity
  private val tradReserveUsed = Mux(tradReserveFits, tradReserveCount, currentCapacity)
  private val earlyAcceptCapacity = currentCapacity - tradReserveUsed

  private val earlyPreAccept = Wire(Vec(earlyWidth, Bool()))
  private val earlyAccepted = Wire(Vec(earlyWidth, Bool()))
  private val earlyNeedsSuppressionSlot = Wire(Vec(earlyWidth, Bool()))
  private val earlySuppressionSlotPrefixCount = prefixCounts((0 until earlyWidth).map(earlyNeedsSuppressionSlot(_)))
  private val earlyConsumesTraditional = Wire(Vec(earlyWidth, Bool()))
  private val earlyLedgerPrefixCount = prefixCounts((0 until earlyWidth).map { idx =>
    earlyPreAccept(idx) && !earlyConsumesTraditional(idx)
  })
  private val earlyPreAcceptPrefixCount = prefixCounts((0 until earlyWidth).map(earlyPreAccept(_)))
  for (earlyIdx <- 0 until earlyWidth) {
    val alreadyTracked = VecInit((0 until ledgerEntries).map { entryIdx =>
      ledgerValidAfterClear(entryIdx) && sameKey(ledger(entryIdx), io.early(earlyIdx).bits)
    }).asUInt.orR
    val traditionalAlreadyWon = VecInit((0 until ledgerEntries).map { entryIdx =>
      traditionalLedgerValid(entryIdx) && sameKey(traditionalLedger(entryIdx), io.early(earlyIdx).bits)
    }).asUInt.orR
    val consumesCurrentTraditional = VecInit((0 until traditionalWidth).map { tradIdx =>
      tradPreNoSameCycle(tradIdx) && sameKey(io.early(earlyIdx).bits, io.traditional(tradIdx).bits)
    }).asUInt.orR
    val ledgerOrdinal = earlyLedgerPrefixCount(earlyIdx)
    val hasLedgerSlot = consumesCurrentTraditional || ledgerOrdinal < freeSlotCount
    val alreadyPendingSuppressed = pendingSuppressionMatch(io.early(earlyIdx).bits.preg)
    val hasSuppressionSlot = alreadyPendingSuppressed || earlySuppressionSlotPrefixCount(earlyIdx) < pendingSuppressionFreeSlotCount

    earlyConsumesTraditional(earlyIdx) := consumesCurrentTraditional
    earlyNeedsSuppressionSlot(earlyIdx) := io.early(earlyIdx).valid &&
      !ownerKilled(io.early(earlyIdx).bits.owner) &&
      !alreadyTracked &&
      !traditionalAlreadyWon &&
      hasLedgerSlot &&
      !alreadyPendingSuppressed
    earlyPreAccept(earlyIdx) := io.early(earlyIdx).valid &&
      !ownerKilled(io.early(earlyIdx).bits.owner) &&
      !alreadyTracked &&
      !traditionalAlreadyWon &&
      hasLedgerSlot &&
      hasSuppressionSlot
    earlyAccepted(earlyIdx) := earlyPreAccept(earlyIdx) && earlyPreAcceptPrefixCount(earlyIdx) < earlyAcceptCapacity
    io.earlyAccepted(earlyIdx) := earlyAccepted(earlyIdx)
  }

  private val sameCycleEarlyMatch = Wire(Vec(traditionalWidth, Bool()))
  private val traditionalAccepted = Wire(Vec(traditionalWidth, Bool()))
  for (tradIdx <- 0 until traditionalWidth) {
    sameCycleEarlyMatch(tradIdx) := VecInit((0 until earlyWidth).map { earlyIdx =>
      earlyAccepted(earlyIdx) && samePreg(io.early(earlyIdx).bits, io.traditional(tradIdx).bits)
    }).asUInt.orR
    io.traditionalSuppressed(tradIdx) := ledgerMatch(tradIdx) || pendingPregMatch(tradIdx) || sameCycleEarlyMatch(tradIdx)
    traditionalAccepted(tradIdx) := tradPreNoSameCycle(tradIdx) && !sameCycleEarlyMatch(tradIdx) && tradReserveFits
    io.traditionalAccepted(tradIdx) := traditionalAccepted(tradIdx)
  }

  private val combinedCount = pendingCapacity + earlyWidth + traditionalWidth
  private val combinedValid = Wire(Vec(combinedCount, Bool()))
  private val combinedBits = Wire(Vec(combinedCount, keyType))
  private val combinedIsEarly = Wire(Vec(combinedCount, Bool()))

  for (idx <- 0 until pendingCapacity) {
    combinedValid(idx) := pendingLiveValid(idx)
    combinedBits(idx) := pendingBits(idx)
    combinedIsEarly(idx) := pendingIsEarly(idx)
  }
  for (idx <- 0 until earlyWidth) {
    val combinedIdx = pendingCapacity + idx
    combinedValid(combinedIdx) := earlyAccepted(idx)
    combinedBits(combinedIdx) := io.early(idx).bits
    combinedIsEarly(combinedIdx) := true.B
  }
  for (idx <- 0 until traditionalWidth) {
    val combinedIdx = pendingCapacity + earlyWidth + idx
    combinedValid(combinedIdx) := traditionalAccepted(idx)
    combinedBits(combinedIdx) := io.traditional(idx).bits
    combinedIsEarly(combinedIdx) := false.B
  }

  for (outIdx <- 0 until outputWidth) {
    io.release(outIdx).valid := false.B
    io.release(outIdx).bits := 0.U.asTypeOf(keyType)
    io.releaseIsEarly(outIdx) := false.B
  }
  private val combinedValidPrefixCount = prefixCounts((0 until combinedCount).map(combinedValid(_)))
  for (idx <- 0 until combinedCount) {
    val outputIdx = combinedValidPrefixCount(idx)
    for (outIdx <- 0 until outputWidth) {
      when(combinedValid(idx) && outputIdx === outIdx.U) {
        io.release(outIdx).valid := true.B
        io.release(outIdx).bits := combinedBits(idx)
        io.releaseIsEarly(outIdx) := combinedIsEarly(idx)
      }
    }
  }

  private val keepValid = Wire(Vec(combinedCount, Bool()))
  for (idx <- 0 until combinedCount) {
    keepValid(idx) := combinedValid(idx) && combinedValidPrefixCount(idx + 1) > outputWidth.U
  }

  private val pendingNextValid = Wire(Vec(pendingCapacity, Bool()))
  private val pendingNextBits = Wire(Vec(pendingCapacity, keyType))
  private val pendingNextIsEarly = Wire(Vec(pendingCapacity, Bool()))
  for (idx <- 0 until pendingCapacity) {
    pendingNextValid(idx) := false.B
    pendingNextBits(idx) := 0.U.asTypeOf(keyType)
    pendingNextIsEarly(idx) := false.B
  }
  for (idx <- 0 until combinedCount) {
    val keepOrdinal = combinedValidPrefixCount(idx) - outputWidth.U
    val pendingNextIndex = keepOrdinal(log2Ceil(pendingCapacity) - 1, 0)
    when(keepValid(idx)) {
      pendingNextValid(pendingNextIndex) := true.B
      pendingNextBits(pendingNextIndex) := combinedBits(idx)
      pendingNextIsEarly(pendingNextIndex) := combinedIsEarly(idx)
    }
  }

  pendingValid := pendingNextValid
  pendingBits := pendingNextBits
  pendingIsEarly := pendingNextIsEarly

  io.overflow := !tradReserveFits
  val earlyDuplicate = (0 until earlyWidth).flatMap { i =>
    ((i + 1) until earlyWidth).map { j =>
      earlyAccepted(i) && earlyAccepted(j) && samePreg(io.early(i).bits, io.early(j).bits)
    }
  }
  val traditionalDuplicate = (0 until traditionalWidth).flatMap { i =>
    ((i + 1) until traditionalWidth).map { j =>
      traditionalAccepted(i) &&
        traditionalAccepted(j) &&
        samePreg(io.traditional(i).bits, io.traditional(j).bits)
    }
  }
  val earlyTraditionalDuplicate = (0 until earlyWidth).flatMap { earlyIdx =>
    (0 until traditionalWidth).map { tradIdx =>
      earlyAccepted(earlyIdx) &&
        traditionalAccepted(tradIdx) &&
        samePreg(io.early(earlyIdx).bits, io.traditional(tradIdx).bits)
    }
  }
  io.duplicate := VecInit(earlyDuplicate ++ traditionalDuplicate ++ earlyTraditionalDuplicate).asUInt.orR

  private val addEarly = Wire(Vec(earlyWidth, Bool()))
  for (earlyIdx <- 0 until earlyWidth) {
    addEarly(earlyIdx) := earlyAccepted(earlyIdx) && !earlyConsumesTraditional(earlyIdx)
  }

  private val ledgerNextValid = Wire(Vec(ledgerEntries, Bool()))
  private val ledgerNext = Wire(Vec(ledgerEntries, keyType))
  for (entryIdx <- 0 until ledgerEntries) {
    ledgerNextValid(entryIdx) := ledgerValidAfterClear(entryIdx)
    ledgerNext(entryIdx) := ledger(entryIdx)
  }

  private val addEarlyPrefixCount = prefixCounts((0 until earlyWidth).map(addEarly(_)))
  private val freeSlotPrefixCount = prefixCounts((0 until ledgerEntries).map(freeSlot(_)))
  for (earlyIdx <- 0 until earlyWidth) {
    val slotOrdinal = addEarlyPrefixCount(earlyIdx)
    for (entryIdx <- 0 until ledgerEntries) {
      val freeOrdinal = freeSlotPrefixCount(entryIdx)
      when(addEarly(earlyIdx) && freeSlot(entryIdx) && freeOrdinal === slotOrdinal) {
        ledgerNextValid(entryIdx) := true.B
        ledgerNext(entryIdx) := io.early(earlyIdx).bits
      }
    }
  }

  ledgerValid := ledgerNextValid
  ledger := ledgerNext

  private val postAllocSuppressionTail0NextValid = Wire(Vec(physicalSuppressionEntries, Bool()))
  private val postAllocSuppressionTail0NextPreg = Wire(Vec(physicalSuppressionEntries, UInt(pregWidth.W)))
  private val earlyReleasedPregPendingNextValid = Wire(Vec(physicalSuppressionEntries, Bool()))
  private val earlyReleasedPregPendingNextPreg = Wire(Vec(physicalSuppressionEntries, UInt(pregWidth.W)))
  for (entryIdx <- 0 until physicalSuppressionEntries) {
    postAllocSuppressionTail0NextValid(entryIdx) := earlyReleasedPregPendingValid(entryIdx) &&
      allocPregMatch(earlyReleasedPregPendingPreg(entryIdx))
    postAllocSuppressionTail0NextPreg(entryIdx) := earlyReleasedPregPendingPreg(entryIdx)
    earlyReleasedPregPendingNextValid(entryIdx) := pendingSuppressionSurvives(entryIdx)
    earlyReleasedPregPendingNextPreg(entryIdx) := earlyReleasedPregPendingPreg(entryIdx)
  }
  private val earlyAddsSuppression = Wire(Vec(earlyWidth, Bool()))
  for (earlyIdx <- 0 until earlyWidth) {
    earlyAddsSuppression(earlyIdx) := earlyAccepted(earlyIdx) &&
      !pendingSuppressionMatch(io.early(earlyIdx).bits.preg)
  }
  private val earlyAddsSuppressionPrefixCount = prefixCounts((0 until earlyWidth).map(earlyAddsSuppression(_)))
  private val pendingSuppressionFreeSlotPrefixCount =
    prefixCounts((0 until physicalSuppressionEntries).map(pendingSuppressionFreeSlot(_)))
  for (earlyIdx <- 0 until earlyWidth) {
    val slotOrdinal = earlyAddsSuppressionPrefixCount(earlyIdx)
    for (entryIdx <- 0 until physicalSuppressionEntries) {
      when(
        earlyAddsSuppression(earlyIdx) &&
          pendingSuppressionFreeSlot(entryIdx) &&
          pendingSuppressionFreeSlotPrefixCount(entryIdx) === slotOrdinal
      ) {
        earlyReleasedPregPendingNextValid(entryIdx) := true.B
        earlyReleasedPregPendingNextPreg(entryIdx) := io.early(earlyIdx).bits.preg
      }
    }
  }
  earlyReleasedPregPendingValid := earlyReleasedPregPendingNextValid
  earlyReleasedPregPendingPreg := earlyReleasedPregPendingNextPreg
  postAllocSuppressionTail0Valid := postAllocSuppressionTail0NextValid
  postAllocSuppressionTail0Preg := postAllocSuppressionTail0NextPreg
  postAllocSuppressionTail1Valid := postAllocSuppressionTail0Valid
  postAllocSuppressionTail1Preg := postAllocSuppressionTail0Preg

  private val addTraditional = Wire(Vec(traditionalWidth, Bool()))
  for (tradIdx <- 0 until traditionalWidth) {
    val alreadyTracked = VecInit((0 until ledgerEntries).map { entryIdx =>
      traditionalLedgerValidAfterClear(entryIdx) && sameKey(traditionalLedger(entryIdx), io.traditional(tradIdx).bits)
    }).asUInt.orR
    addTraditional(tradIdx) := traditionalAccepted(tradIdx) && !alreadyTracked
  }

  private val traditionalFreeSlot = Wire(Vec(ledgerEntries, Bool()))
  for (entryIdx <- 0 until ledgerEntries) {
    traditionalFreeSlot(entryIdx) := !traditionalLedgerValidAfterClear(entryIdx)
  }

  private val traditionalLedgerNextValid = Wire(Vec(ledgerEntries, Bool()))
  private val traditionalLedgerNext = Wire(Vec(ledgerEntries, keyType))
  for (entryIdx <- 0 until ledgerEntries) {
    traditionalLedgerNextValid(entryIdx) := traditionalLedgerValidAfterClear(entryIdx)
    traditionalLedgerNext(entryIdx) := traditionalLedger(entryIdx)
  }

  private val addTraditionalPrefixCount = prefixCounts((0 until traditionalWidth).map(addTraditional(_)))
  private val traditionalFreeSlotPrefixCount = prefixCounts((0 until ledgerEntries).map(traditionalFreeSlot(_)))
  for (tradIdx <- 0 until traditionalWidth) {
    val slotOrdinal = addTraditionalPrefixCount(tradIdx)
    for (entryIdx <- 0 until ledgerEntries) {
      val freeOrdinal = traditionalFreeSlotPrefixCount(entryIdx)
      when(addTraditional(tradIdx) && traditionalFreeSlot(entryIdx) && freeOrdinal === slotOrdinal) {
        traditionalLedgerNextValid(entryIdx) := true.B
        traditionalLedgerNext(entryIdx) := io.traditional(tradIdx).bits
      }
    }
  }

  traditionalLedgerValid := traditionalLedgerNextValid
  traditionalLedger := traditionalLedgerNext
}

object EarlyReleaseTraditionalReleaseOwner {
  def select(actualRobIdx: Option[UInt], metadataRobIdx: UInt): UInt =
    metadataRobIdx
}

object EarlyReleaseTraditionalReleasePipeline {
  def align(valid: Bool, preg: UInt, owner: UInt): ValidIO[EarlyReleaseReleaseKey] = {
    val out = Wire(Valid(new EarlyReleaseReleaseKey(preg.getWidth, owner.getWidth)))
    out.valid := RegNext(valid, false.B)
    out.bits.preg := RegEnable(preg, valid)
    out.bits.owner := RegEnable(owner, valid)
    out
  }

  def alignRenameTable(needFree: Bool, oldPdest: UInt, commitOwner: UInt): ValidIO[EarlyReleaseReleaseKey] = {
    align(
      valid = needFree && oldPdest =/= 0.U,
      preg = oldPdest,
      owner = commitOwner
    )
  }
}

object EarlyReleaseTraditionalAliasRisk {
  def align(realCommitLane: Bool, renameTableNeedFree: Bool, oldPdest: UInt): ValidIO[UInt] = {
    val commitStage1 = RegNext(realCommitLane, false.B)
    val candidateValid = RegNext(commitStage1, false.B)
    val candidateOldPdest = RegEnable(oldPdest, 0.U(oldPdest.getWidth.W), commitStage1)
    val outValid = RegNext(candidateValid, false.B)
    val outOldPdest = RegEnable(candidateOldPdest, 0.U(oldPdest.getWidth.W), candidateValid)
    val outNeedFree = RegEnable(renameTableNeedFree, false.B, candidateValid)
    val out = Wire(Valid(UInt(oldPdest.getWidth.W)))

    out.valid := outValid && !outNeedFree && outOldPdest =/= 0.U
    out.bits := outOldPdest
    out
  }
}

class EarlyReleaseReadCompleteEvent(implicit p: Parameters) extends XSBundle {
  val preg = UInt(PhyRegIdxWidth.W)
  val producerOwner = UInt(EarlyReleaseOwner.width.W)
  val robIdx = UInt(log2Ceil(RobSize).W)
  val owner = UInt(EarlyReleaseOwner.width.W)
  val srcIdx = UInt(log2Ceil(p(XSCoreParamsKey).backendParams.numIntRegSrc).max(1).W)
}

class EarlyReleaseReadComplete(implicit p: Parameters) extends XSBundle {
  val events = Vec(EarlyReleaseReadCompleteEvents.backendEventWidth, ValidIO(new EarlyReleaseReadCompleteEvent))
}

class EarlyReleaseRecoveryReadState(val entries: Int, val numIntSrc: Int, val ownerWidth: Int) extends Bundle {
  val valid = Bool()
  val owner = UInt(ownerWidth.W)
  val readDone = Vec(numIntSrc, Bool())
}

object EarlyReleaseReadCompleteEvents {
  def backendEventWidth(implicit p: Parameters): Int = {
    val backendParams = p(XSCoreParamsKey).backendParams
    val readCompleteDeqWidth = backendParams.allIssueParams.map(_.numDeq).sum
    (readCompleteDeqWidth * backendParams.numIntRegSrc).max(1)
  }

  def sourceEventVecType(implicit p: Parameters): Vec[ValidIO[EarlyReleaseReadCompleteEvent]] =
    Vec(p(XSCoreParamsKey).backendParams.numIntRegSrc, ValidIO(new EarlyReleaseReadCompleteEvent))

  private def connectFirstNValid[T <: Data](sinks: Seq[ValidIO[T]], sources: Seq[ValidIO[T]]): Unit = {
    require(sinks.nonEmpty)

    for (sink <- sinks) {
      sink.valid := false.B
      sink.bits := 0.U.asTypeOf(sink.bits)
    }

    if (sources.nonEmpty) {
      val countWidth = log2Ceil(sources.length + 1).max(1)
      val validPrefixCount = Wire(Vec(sources.length + 1, UInt(countWidth.W)))
      validPrefixCount(0) := 0.U
      for (idx <- sources.indices) {
        validPrefixCount(idx + 1) := validPrefixCount(idx) + sources(idx).valid.asUInt
      }

      for (sourceIdx <- sources.indices) {
        for (sinkIdx <- sinks.indices) {
          when(sources(sourceIdx).valid && validPrefixCount(sourceIdx) === sinkIdx.U) {
            sinks(sinkIdx).valid := true.B
            sinks(sinkIdx).bits := sources(sourceIdx).bits
          }
        }
      }
    }
  }

  def first(events: Seq[ValidIO[EarlyReleaseReadCompleteEvent]])(implicit p: Parameters): EarlyReleaseReadComplete = {
    val out = WireDefault(0.U.asTypeOf(new EarlyReleaseReadComplete))
    connectFirstNValid(out.events, events)
    out
  }

  def merge(events: Seq[EarlyReleaseReadComplete])(implicit p: Parameters): EarlyReleaseReadComplete = {
    val out = WireDefault(0.U.asTypeOf(new EarlyReleaseReadComplete))
    connectFirstNValid(out.events, events.flatMap(_.events))
    out
  }

  def build(
    readFire: Bool,
    flushed: Bool,
    loadCanceled: Bool,
    robIdx: UInt,
    owner: UInt,
    metadata: EarlyReleaseMetadata
  )(implicit p: Parameters): Vec[ValidIO[EarlyReleaseReadCompleteEvent]] = {
    val events = WireDefault(0.U.asTypeOf(sourceEventVecType))
    val readComplete = readFire && !flushed && !loadCanceled

    for (idx <- 0 until p(XSCoreParamsKey).backendParams.numIntRegSrc) {
      val src = metadata.countedIntSrcs(idx)
      val valid = readComplete && src.valid && !src.readDone
      events(idx).valid := valid
      events(idx).bits.preg := Mux(valid, src.preg, 0.U)
      events(idx).bits.producerOwner := Mux(valid, src.producerOwner, 0.U)
      events(idx).bits.robIdx := Mux(valid, robIdx, 0.U)
      events(idx).bits.owner := Mux(valid, owner, 0.U)
      events(idx).bits.srcIdx := idx.U
    }

    events
  }
}

class EarlyReleaseRecoveryTracker(
  entries: Int,
  walkWidth: Int,
  commitWidth: Int,
  readCompleteWidth: Int,
  cancelWidth: Int
)(implicit p: Parameters) extends Module {
  require(entries > 1)
  require(walkWidth > 0)
  require(commitWidth > 0)
  require(readCompleteWidth > 0)
  require(cancelWidth > 0)

  private val idxWidth = log2Ceil(entries)
  private val numIntSrc = p(XSCoreParamsKey).backendParams.numIntRegSrc
  private val pregWidth = p(XSCoreParamsKey).intPreg.addrWidth

  val io = IO(new Bundle {
    val readComplete = Input(Vec(readCompleteWidth, Valid(new EarlyReleaseReadCompleteEvent)))
    val commit = Input(Vec(commitWidth, Valid(UInt(idxWidth.W))))
    val walk = Input(Vec(walkWidth, Valid(new xiangshan.backend.rob.EarlyReleaseRecoveryEvent(idxWidth))))
    val killed = Input(Vec(walkWidth, Valid(new xiangshan.backend.rob.EarlyReleaseKilledRecoveryEvent(idxWidth))))
    val firstReadComplete = Output(Vec(readCompleteWidth, Valid(new EarlyReleaseReadCompleteEvent)))
    val cancelSourceUse = Output(Vec(cancelWidth, Valid(new UserCountTableSourceEvent(pregWidth, EarlyReleaseOwner.width))))
    val killOwner = Output(Vec(walkWidth, Valid(UInt(EarlyReleaseOwner.width.W))))
  })

  private val zeroState = 0.U.asTypeOf(new EarlyReleaseRecoveryReadState(entries, numIntSrc, EarlyReleaseOwner.width))
  private val table = RegInit(VecInit(Seq.fill(entries)(zeroState)))

  private val tableNext = WireDefault(table)

  for ((out, eventWithIdx) <- io.firstReadComplete.zip(io.readComplete.zipWithIndex)) {
    val (event, eventIdx) = eventWithIdx
    val readState = table(event.bits.robIdx)
    val sameGeneration = readState.valid && readState.owner === event.bits.owner
    val srcAlreadyDone = VecInit((0 until numIntSrc).map { srcIdx =>
      event.bits.srcIdx === srcIdx.U && sameGeneration && readState.readDone(srcIdx)
    }).asUInt.orR
    val sameCycleOlderHit = if (eventIdx == 0) {
      false.B
    } else {
      VecInit(io.readComplete.take(eventIdx).map { older =>
        older.valid &&
          older.bits.robIdx === event.bits.robIdx &&
          older.bits.owner === event.bits.owner &&
          older.bits.srcIdx === event.bits.srcIdx
      }).asUInt.orR
    }
    val first = event.valid && !srcAlreadyDone && !sameCycleOlderHit

    out.valid := first
    out.bits := event.bits
  }

  for (event <- io.readComplete) {
    when(event.valid) {
      tableNext(event.bits.robIdx).valid := true.B
      tableNext(event.bits.robIdx).owner := event.bits.owner
      for (srcIdx <- 0 until numIntSrc) {
        when(event.bits.srcIdx === srcIdx.U) {
          tableNext(event.bits.robIdx).readDone(srcIdx) := true.B
        }
      }
    }
  }

  for (idx <- 0 until entries) {
    val commitHit = VecInit(io.commit.map(commit => commit.valid && commit.bits === idx.U)).asUInt.orR
    when(commitHit) {
      tableNext(idx) := zeroState
    }
  }

  // ROB/RAB walk replays the surviving prefix for RAT/freelist recovery; it is
  // not a killed-entry stream. Only the explicit redirect-killed stream below
  // is allowed to undo UCT source-use accounting or release-ledger ownership.
  private val sameCycleReadDone = Wire(Vec(walkWidth, Vec(numIntSrc, Bool())))
  for (killIdx <- 0 until walkWidth) {
    val killed = io.killed(killIdx)
    for (srcIdx <- 0 until numIntSrc) {
      sameCycleReadDone(killIdx)(srcIdx) := VecInit(io.readComplete.map { read =>
        read.valid &&
          read.bits.robIdx === killed.bits.robIdx &&
          read.bits.owner === killed.bits.metadata.redefinerRobIdx &&
          read.bits.srcIdx === srcIdx.U
      }).asUInt.orR
    }
  }

  for (outIdx <- 0 until cancelWidth) {
    val killIdx = outIdx / numIntSrc
    val srcIdx = outIdx % numIntSrc
    if (killIdx < walkWidth) {
      val killed = io.killed(killIdx)
      val src = killed.bits.metadata.countedIntSrcs(srcIdx)
      val sameGeneration = table(killed.bits.robIdx).valid &&
        table(killed.bits.robIdx).owner === killed.bits.metadata.redefinerRobIdx
      val tableReadDone = sameGeneration && table(killed.bits.robIdx).readDone(srcIdx)
      val alreadyRead = src.readDone || tableReadDone || sameCycleReadDone(killIdx)(srcIdx)
      val cancel = killed.valid && src.valid && !alreadyRead
      io.cancelSourceUse(outIdx).valid := cancel
      io.cancelSourceUse(outIdx).bits.preg := Mux(cancel, src.preg, 0.U)
      io.cancelSourceUse(outIdx).bits.owner := Mux(cancel, src.producerOwner, 0.U)
    } else {
      io.cancelSourceUse(outIdx).valid := false.B
      io.cancelSourceUse(outIdx).bits := 0.U.asTypeOf(io.cancelSourceUse(outIdx).bits)
    }
  }

  for (walkIdx <- 0 until walkWidth) {
    val killed = io.killed(walkIdx)
    val valid = killed.valid && killed.bits.metadata.oldIntPdestValid
    io.killOwner(walkIdx).valid := valid
    io.killOwner(walkIdx).bits := Mux(valid, killed.bits.metadata.redefinerRobIdx, 0.U)
    val sameGeneration = table(killed.bits.robIdx).valid &&
      table(killed.bits.robIdx).owner === killed.bits.metadata.redefinerRobIdx
    when(killed.valid && sameGeneration) {
      tableNext(killed.bits.robIdx) := zeroState
    }
  }

  table := tableNext
}

object EarlyReleaseWalkProducerCleanup {
  def build(
    walkValid: Bool,
    info: RabCommitInfo,
    pregWidth: Int,
    ownerWidth: Int
  )(implicit p: Parameters): ValidIO[UserCountTableClear] = {
    val clear = Wire(Valid(new UserCountTableClear(pregWidth, ownerWidth)))
    // RAB walk writes surviving producers back into the speculative RAT; their
    // UCT producer entries must remain live for later consumers.
    clear.valid := false.B
    clear.bits.preg := 0.U
    clear.bits.owner := 0.U
    clear
  }
}

object EarlyReleaseRenameMetadata {
  def attachSourceOwners(
    metadata: EarlyReleaseMetadata,
    sourceOwners: Seq[ValidIO[UInt]]
  )(implicit p: Parameters): EarlyReleaseMetadata = {
    require(sourceOwners.length == p(XSCoreParamsKey).backendParams.numIntRegSrc)

    val out = WireDefault(metadata)
    for (idx <- sourceOwners.indices) {
      val owner = sourceOwners(idx)
      out.countedIntSrcs(idx).valid := metadata.countedIntSrcs(idx).valid && owner.valid
      out.countedIntSrcs(idx).preg := Mux(owner.valid, metadata.countedIntSrcs(idx).preg, 0.U)
      out.countedIntSrcs(idx).producerOwner := Mux(owner.valid, owner.bits, 0.U)
      out.countedIntSrcs(idx).readDone := metadata.countedIntSrcs(idx).readDone && owner.valid
    }

    out
  }

  def build(
    enable: Bool,
    srcTypes: Seq[UInt],
    psrcs: Seq[UInt],
    oldIntPdestValid: Bool,
    oldIntPdest: UInt,
    redefinerRobIdx: UInt
  )(implicit p: Parameters): EarlyReleaseMetadata = {
    require(srcTypes.length == psrcs.length)
    require(srcTypes.length == p(XSCoreParamsKey).backendParams.numIntRegSrc)

    val metadata = WireDefault(0.U.asTypeOf(new EarlyReleaseMetadata))

    metadata.oldIntPdestValid := enable && oldIntPdestValid && oldIntPdest =/= 0.U
    metadata.oldIntPdest := Mux(enable, oldIntPdest, 0.U)
    metadata.redefinerRobIdx := Mux(enable, redefinerRobIdx, 0.U)

    for (idx <- srcTypes.indices) {
      val duplicate = if (idx == 0) {
        false.B
      } else {
        VecInit((0 until idx).map { prev =>
          metadata.countedIntSrcs(prev).valid && metadata.countedIntSrcs(prev).preg === psrcs(idx)
        }).asUInt.orR
      }

      val counted = enable && SrcType.isXp(srcTypes(idx)) && psrcs(idx) =/= 0.U && !duplicate
      metadata.countedIntSrcs(idx).valid := counted
      metadata.countedIntSrcs(idx).preg := Mux(counted, psrcs(idx), 0.U)
      metadata.countedIntSrcs(idx).producerOwner := 0.U
      metadata.countedIntSrcs(idx).readDone := false.B
    }

    metadata
  }
}
