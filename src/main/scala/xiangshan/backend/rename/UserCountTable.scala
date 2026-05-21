package xiangshan.backend.rename

import chisel3._
import chisel3.util._
import org.chipsalliance.cde.config.Parameters
import utility.XSPerfAccumulate

class UserCountTableAlloc(val pregWidth: Int, val ownerWidth: Int) extends Bundle {
  val preg = UInt(pregWidth.W)
  val owner = UInt(ownerWidth.W)
}

class UserCountTableRedefine(val pregWidth: Int, val ownerWidth: Int) extends Bundle {
  val preg = UInt(pregWidth.W)
  val owner = UInt(ownerWidth.W)
}

class UserCountTableClear(val pregWidth: Int, val ownerWidth: Int) extends Bundle {
  val preg = UInt(pregWidth.W)
  val owner = UInt(ownerWidth.W)
}

class UserCountTableSourceEvent(val pregWidth: Int, val ownerWidth: Int) extends Bundle {
  val preg = UInt(pregWidth.W)
  val owner = UInt(ownerWidth.W)
}

class UserCountTableProducerWriteback(val pregWidth: Int, val ownerWidth: Int) extends Bundle {
  val preg = UInt(pregWidth.W)
  val owner = UInt(ownerWidth.W)
}

class UserCountTableEntry(val pregWidth: Int, val countWidth: Int, val ownerWidth: Int) extends Bundle {
  val valid = Bool()
  val preg = UInt(pregWidth.W)
  val count = UInt(countWidth.W)
  val fallback = Bool()
  val consumerSeen = Bool()
  val owner = UInt(ownerWidth.W)
  val producerWritten = Bool()
  val redefineSeen = Bool()
  val releaseOwner = UInt(ownerWidth.W)
  val releaseSent = Bool()
}

class UserCountTableFree(val pregWidth: Int, val ownerWidth: Int) extends Bundle {
  val preg = UInt(pregWidth.W)
  val owner = UInt(ownerWidth.W)
}

class UserCountTableRejects extends Bundle {
  val sourceUse = Bool()
  val readComplete = Bool()
  val cancelSourceUse = Bool()
  val nonSpecRedefine = Bool()
}

class UserCountTable(
  entries: Int,
  ownerWidth: Int,
  pregWidthOverride: Int = -1,
  earlyFreeCandidateWidth: Int = -1,
  countWidth: Int = 3,
  allocWidth: Int = 1,
  sourceUseWidth: Int = 1,
  sourceFallbackWidth: Int = 1,
  readCompleteWidth: Int = 1,
  nonSpecRedefineWidth: Int = 1,
  clearWidth: Int = 1,
  cancelSourceUseWidth: Int = 1,
  moveAliasWidth: Int = 1,
  traditionalAliasRiskWidth: Int = 1,
  producerWritebackWidth: Int = 1
)(implicit p: Parameters) extends Module {
  require(entries > 0)
  require(ownerWidth > 0)
  require(pregWidthOverride == -1 || pregWidthOverride >= log2Ceil(entries).max(1))
  require(countWidth > 0)
  require(allocWidth > 0)
  require(sourceUseWidth > 0)
  require(sourceFallbackWidth > 0)
  require(readCompleteWidth > 0)
  require(nonSpecRedefineWidth > 0)
  require(clearWidth > 0)
  require(cancelSourceUseWidth > 0)
  require(moveAliasWidth > 0)
  require(traditionalAliasRiskWidth > 0)
  require(producerWritebackWidth > 0)

  private val releaseCandidateWidth = if (earlyFreeCandidateWidth == -1) entries else earlyFreeCandidateWidth
  require(releaseCandidateWidth > 0)
  require(releaseCandidateWidth <= entries)

  private val pregWidth = if (pregWidthOverride == -1) log2Ceil(entries).max(1) else pregWidthOverride
  private val maxCount = ((1 << countWidth) - 1).U(countWidth.W)
  private val zeroEntry = 0.U.asTypeOf(new UserCountTableEntry(pregWidth, countWidth, ownerWidth))
  private def prefixCounts(valids: Seq[Bool]): Vec[UInt] = {
    val countWidth = log2Ceil(valids.length + 1).max(1)
    val counts = Wire(Vec(valids.length + 1, UInt(countWidth.W)))
    counts(0) := 0.U
    for (idx <- valids.indices) {
      counts(idx + 1) := counts(idx) + valids(idx).asUInt
    }
    counts
  }
  private def ownerNotOlder(owner: UInt, producerOwner: UInt): Bool = {
    val delta = owner -% producerOwner
    delta === 0.U || !delta(ownerWidth - 1)
  }

  val io = IO(new Bundle {
    val alloc = Flipped(Vec(allocWidth, Valid(new UserCountTableAlloc(pregWidth, ownerWidth))))
    val allocAccepted = Output(Vec(allocWidth, Bool()))
    val sourceUse = Flipped(Vec(sourceUseWidth, Valid(UInt(pregWidth.W))))
    val sourceFallback = Flipped(Vec(sourceFallbackWidth, Valid(UInt(pregWidth.W))))
    val sourceUseOwner = Output(Vec(sourceUseWidth, Valid(UInt(ownerWidth.W))))
    val readComplete = Flipped(Vec(readCompleteWidth, Valid(new UserCountTableSourceEvent(pregWidth, ownerWidth))))
    val cancelSourceUse = Flipped(Vec(cancelSourceUseWidth, Valid(new UserCountTableSourceEvent(pregWidth, ownerWidth))))
    val moveAlias = Flipped(Vec(moveAliasWidth, Valid(UInt(pregWidth.W))))
    val traditionalAliasRisk = Flipped(Vec(traditionalAliasRiskWidth, Valid(UInt(pregWidth.W))))
    val producerWriteback =
      Flipped(Vec(producerWritebackWidth, Valid(new UserCountTableProducerWriteback(pregWidth, ownerWidth))))
    val nonSpecRedefine = Flipped(Vec(nonSpecRedefineWidth, Valid(new UserCountTableRedefine(pregWidth, ownerWidth))))
    val clear = Flipped(Vec(clearWidth, Valid(new UserCountTableClear(pregWidth, ownerWidth))))
    val traditionalRelease =
      Flipped(Vec(clearWidth, Valid(new UserCountTableClear(pregWidth, ownerWidth))))
    val fallbackAll = Input(Bool())
    val lookup = Input(UInt(pregWidth.W))

    val state = Output(new UserCountTableEntry(pregWidth, countWidth, ownerWidth))
    val earlyFree = Output(Valid(new UserCountTableFree(pregWidth, ownerWidth)))
    val earlyFreeCandidates = Output(Vec(releaseCandidateWidth, Valid(new UserCountTableFree(pregWidth, ownerWidth))))
    val earlyFreeCandidateAccepted = Input(Vec(releaseCandidateWidth, Bool()))
    val rejected = Output(new UserCountTableRejects)
    val underflow = Output(Bool())
    val saturationFallback = Output(Bool())
    val deadCodeFallback = Output(Bool())
  })

  private val table = RegInit(VecInit(Seq.fill(entries)(zeroEntry)))
  private val traditionalSeenValid = RegInit(VecInit(Seq.fill(entries)(false.B)))
  private val traditionalSeenOwner = RegInit(VecInit(Seq.fill(entries)(0.U(ownerWidth.W))))
  private val nextTable = WireInit(table)
  private val nextTraditionalSeenValid = WireInit(traditionalSeenValid)
  private val nextTraditionalSeenOwner = WireInit(traditionalSeenOwner)
  private val releasePulseValid = WireInit(VecInit(Seq.fill(entries)(false.B)))
  private val releasePulseBits = Wire(Vec(entries, new UserCountTableFree(pregWidth, ownerWidth)))
  private val releaseReadyValid = WireInit(VecInit(Seq.fill(entries)(false.B)))
  private val releaseReadyBits = Wire(Vec(entries, new UserCountTableFree(pregWidth, ownerWidth)))
  private val releaseAccepted = WireInit(VecInit(Seq.fill(entries)(false.B)))

  releasePulseBits.foreach(_ := 0.U.asTypeOf(new UserCountTableFree(pregWidth, ownerWidth)))
  releaseReadyBits.foreach(_ := 0.U.asTypeOf(new UserCountTableFree(pregWidth, ownerWidth)))
  io.earlyFreeCandidates.foreach { candidate =>
    candidate.valid := false.B
    candidate.bits := 0.U.asTypeOf(new UserCountTableFree(pregWidth, ownerWidth))
  }

  private val lookupHits = VecInit(table.map(entry => entry.valid && entry.preg === io.lookup))
  io.state := Mux(lookupHits.asUInt.orR, PriorityMux(lookupHits, table), zeroEntry)
  io.earlyFree.valid := false.B
  io.earlyFree.bits := 0.U.asTypeOf(new UserCountTableFree(pregWidth, ownerWidth))
  io.rejected := 0.U.asTypeOf(new UserCountTableRejects)
  io.underflow := false.B
  io.saturationFallback := false.B
  io.deadCodeFallback := false.B
  io.sourceUseOwner.foreach(_ := 0.U.asTypeOf(Valid(UInt(ownerWidth.W))))

  private def firstOneHot(valids: Seq[Bool]): UInt =
    PriorityEncoderOH(VecInit(valids).asUInt)

  private val allocationTarget = Wire(Vec(allocWidth, UInt(entries.W)))
  private val allocationHasTarget = Wire(Vec(allocWidth, Bool()))
  private val allocatedByEarlier = Wire(Vec(allocWidth + 1, UInt(entries.W)))
  allocatedByEarlier(0) := 0.U
  for (allocIdx <- 0 until allocWidth) {
    val alloc = io.alloc(allocIdx)
    val samePregHits = (0 until entries).map(idx => table(idx).valid && table(idx).preg === alloc.bits.preg)
    val freeHits = (0 until entries).map(idx => !table(idx).valid && !allocatedByEarlier(allocIdx)(idx))
    val samePregValid = VecInit(samePregHits).asUInt.orR
    val freeValid = VecInit(freeHits).asUInt.orR
    val target = Mux(samePregValid, firstOneHot(samePregHits), firstOneHot(freeHits))
    allocationHasTarget(allocIdx) := alloc.valid && (samePregValid || freeValid)
    allocationTarget(allocIdx) := Mux(allocationHasTarget(allocIdx), target, 0.U(entries.W))
    allocatedByEarlier(allocIdx + 1) := allocatedByEarlier(allocIdx) | Mux(allocationHasTarget(allocIdx), target, 0.U(entries.W))
    io.allocAccepted(allocIdx) := allocationHasTarget(allocIdx)
  }

  private def samePregAsEntry(preg: UInt, idx: Int): Bool =
    table(idx).valid && table(idx).preg === preg

  private def samePregAsAlloc(preg: UInt, idx: Int): Bool =
    VecInit((0 until allocWidth).map { allocIdx =>
      allocationHasTarget(allocIdx) && allocationTarget(allocIdx)(idx) && io.alloc(allocIdx).bits.preg === preg
    }).asUInt.orR

  private def trackedThisCycle(preg: UInt): Bool =
    VecInit((0 until entries).map(idx => samePregAsEntry(preg, idx) || samePregAsAlloc(preg, idx))).asUInt.orR

  private val pendingAliasRiskEntries = entries * 2
  private val pendingAliasRiskValid = RegInit(VecInit(Seq.fill(pendingAliasRiskEntries)(false.B)))
  private val pendingAliasRiskPreg = RegInit(VecInit(Seq.fill(pendingAliasRiskEntries)(0.U(pregWidth.W))))
  private val pendingAliasOverflow = RegInit(false.B)
  private val aliasRiskPregs = io.moveAlias.map(_.bits) ++ io.traditionalAliasRisk.map(_.bits)
  private val aliasRiskValids = io.moveAlias.map(_.valid) ++ io.traditionalAliasRisk.map(_.valid)
  private val pendingAliasClear = Wire(Vec(pendingAliasRiskEntries, Bool()))
  for (entryIdx <- 0 until pendingAliasRiskEntries) {
    pendingAliasClear(entryIdx) := pendingAliasRiskValid(entryIdx) &&
      VecInit((0 until allocWidth).map { allocIdx =>
        allocationHasTarget(allocIdx) && io.alloc(allocIdx).bits.preg === pendingAliasRiskPreg(entryIdx)
      }).asUInt.orR
  }
  private val pendingAliasRiskValidAfterClear = Wire(Vec(pendingAliasRiskEntries, Bool()))
  private val pendingAliasRiskPregAfterClear = Wire(Vec(pendingAliasRiskEntries, UInt(pregWidth.W)))
  for (entryIdx <- 0 until pendingAliasRiskEntries) {
    pendingAliasRiskValidAfterClear(entryIdx) := pendingAliasRiskValid(entryIdx) && !pendingAliasClear(entryIdx)
    pendingAliasRiskPregAfterClear(entryIdx) := pendingAliasRiskPreg(entryIdx)
  }
  private def pendingAliasRiskHit(preg: UInt): Bool =
    VecInit((0 until pendingAliasRiskEntries).map { entryIdx =>
      pendingAliasRiskValid(entryIdx) && pendingAliasRiskPreg(entryIdx) === preg
    }).asUInt.orR
  private val pendingAliasNew = Wire(Vec(aliasRiskPregs.length, Bool()))
  for (aliasIdx <- aliasRiskPregs.indices) {
    val preg = aliasRiskPregs(aliasIdx)
    val alreadyPending = VecInit((0 until pendingAliasRiskEntries).map { entryIdx =>
      pendingAliasRiskValidAfterClear(entryIdx) && pendingAliasRiskPregAfterClear(entryIdx) === preg
    }).asUInt.orR
    val earlierNewSame = if (aliasIdx == 0) {
      false.B
    } else {
      VecInit((0 until aliasIdx).map { earlierIdx =>
        pendingAliasNew(earlierIdx) && aliasRiskPregs(earlierIdx) === preg
      }).asUInt.orR
    }
    pendingAliasNew(aliasIdx) := aliasRiskValids(aliasIdx) &&
      !trackedThisCycle(preg) &&
      !alreadyPending &&
      !earlierNewSame
  }
  private val pendingAliasFreeSlot = Wire(Vec(pendingAliasRiskEntries, Bool()))
  for (entryIdx <- 0 until pendingAliasRiskEntries) {
    pendingAliasFreeSlot(entryIdx) := !pendingAliasRiskValidAfterClear(entryIdx)
  }
  private val pendingAliasNewPrefixCount = prefixCounts((0 until aliasRiskPregs.length).map(pendingAliasNew(_)))
  private val pendingAliasFreePrefixCount = prefixCounts((0 until pendingAliasRiskEntries).map(pendingAliasFreeSlot(_)))
  private val pendingAliasNewCount = pendingAliasNewPrefixCount(aliasRiskPregs.length)
  private val pendingAliasFreeSlotCount = pendingAliasFreePrefixCount(pendingAliasRiskEntries)
  private val pendingAliasOverflowSet = pendingAliasNewCount > pendingAliasFreeSlotCount
  private val pendingAliasRiskNextValid = Wire(Vec(pendingAliasRiskEntries, Bool()))
  private val pendingAliasRiskNextPreg = Wire(Vec(pendingAliasRiskEntries, UInt(pregWidth.W)))
  for (entryIdx <- 0 until pendingAliasRiskEntries) {
    pendingAliasRiskNextValid(entryIdx) := pendingAliasRiskValidAfterClear(entryIdx)
    pendingAliasRiskNextPreg(entryIdx) := pendingAliasRiskPregAfterClear(entryIdx)
  }
  for (aliasIdx <- aliasRiskPregs.indices) {
    val slotOrdinal = pendingAliasNewPrefixCount(aliasIdx)
    for (entryIdx <- 0 until pendingAliasRiskEntries) {
      when(
        pendingAliasNew(aliasIdx) &&
          pendingAliasFreeSlot(entryIdx) &&
          pendingAliasFreePrefixCount(entryIdx) === slotOrdinal
      ) {
        pendingAliasRiskNextValid(entryIdx) := true.B
        pendingAliasRiskNextPreg(entryIdx) := aliasRiskPregs(aliasIdx)
      }
    }
  }

  for (source <- io.sourceUse) {
    when(source.valid && !trackedThisCycle(source.bits)) {
      io.rejected.sourceUse := true.B
    }
  }
  for (source <- io.sourceFallback) {
    when(source.valid && !trackedThisCycle(source.bits)) {
      io.rejected.sourceUse := true.B
    }
  }
  for (read <- io.readComplete) {
    when(read.valid && !trackedThisCycle(read.bits.preg)) {
      io.rejected.readComplete := true.B
    }
  }
  for (cancel <- io.cancelSourceUse) {
    when(cancel.valid && !trackedThisCycle(cancel.bits.preg)) {
      io.rejected.cancelSourceUse := true.B
    }
  }

  for (idx <- 0 until entries) {
    val allocHits = VecInit((0 until allocWidth).map(allocIdx =>
      allocationHasTarget(allocIdx) && allocationTarget(allocIdx)(idx)
    ))
    val entryPreg = Mux(allocHits.asUInt.orR, PriorityMux(allocHits, io.alloc.map(_.bits.preg)), table(idx).preg)
    val clearHit = VecInit(io.clear.map(clear =>
      clear.valid &&
        table(idx).valid &&
        table(idx).preg === clear.bits.preg &&
        (table(idx).owner === clear.bits.owner || table(idx).releaseOwner === clear.bits.owner)
    )).asUInt.orR
    val traditionalReleaseHits = VecInit(io.traditionalRelease.map(release =>
      release.valid &&
        table(idx).valid &&
        table(idx).preg === release.bits.preg
    ))
    val traditionalReleaseHit = traditionalReleaseHits.asUInt.orR
    val firstTraditionalRelease = Mux(
      traditionalReleaseHit,
      PriorityMux(traditionalReleaseHits, io.traditionalRelease.map(_.bits)),
      0.U.asTypeOf(new UserCountTableClear(pregWidth, ownerWidth))
    )
    val matchingTraditionalReleaseHit = traditionalReleaseHit &&
        table(idx).valid &&
        (table(idx).owner === firstTraditionalRelease.owner || table(idx).releaseOwner === firstTraditionalRelease.owner)
    val sourceHits = VecInit(io.sourceUse.map(source => source.valid && source.bits === entryPreg))
    val sourceFallbackHits =
      VecInit(io.sourceFallback.map(source => source.valid && source.bits === entryPreg))
    val rawReadHits = VecInit(io.readComplete.map(read =>
      read.valid && read.bits.preg === entryPreg
    ))
    val rawCancelHits = VecInit(io.cancelSourceUse.map(cancel =>
      cancel.valid && cancel.bits.preg === entryPreg
    ))
    val moveAliasHits = VecInit(io.moveAlias.map(alias => alias.valid && alias.bits === entryPreg))
    val traditionalAliasRiskHits = VecInit(io.traditionalAliasRisk.map(alias =>
      alias.valid && alias.bits === entryPreg
    ))
    val producerWritebackHits = VecInit(io.producerWriteback.map(wb =>
      wb.valid &&
        wb.bits.preg === entryPreg
    ))
    val rawRedefineHits = VecInit(io.nonSpecRedefine.map(redefine =>
      redefine.valid && redefine.bits.preg === entryPreg
    ))
    val redefineSuppressedByTraditional = VecInit(io.nonSpecRedefine.map(redefine =>
      redefine.valid &&
        redefine.bits.preg === entryPreg &&
        traditionalSeenValid(idx) &&
        traditionalSeenOwner(idx) === redefine.bits.owner
    ))
    val redefineSuppressedHit = redefineSuppressedByTraditional.asUInt.orR
    val allocHit = allocHits.asUInt.orR
    val sourceHit = sourceHits.asUInt.orR
    val sourceFallbackHit = sourceFallbackHits.asUInt.orR
    val moveAliasHit = moveAliasHits.asUInt.orR
    val traditionalAliasRiskHit = traditionalAliasRiskHits.asUInt.orR
    val producerWritebackHit = producerWritebackHits.asUInt.orR
    val sourceHitCount = PopCount(sourceHits)
    val firstAlloc = Mux(allocHit, PriorityMux(allocHits, io.alloc.map(_.bits)), 0.U.asTypeOf(new UserCountTableAlloc(pregWidth, ownerWidth)))
    val firstProducerWriteback = Mux(
      producerWritebackHit,
      PriorityMux(producerWritebackHits, io.producerWriteback.map(_.bits)),
      0.U.asTypeOf(new UserCountTableProducerWriteback(pregWidth, ownerWidth))
    )

    val baseEntry = WireInit(table(idx))
    when(clearHit) {
      baseEntry := zeroEntry
    }
    when(redefineSuppressedHit || allocHit) {
      nextTraditionalSeenValid(idx) := false.B
    }
    when(traditionalReleaseHit && !allocHit) {
      nextTraditionalSeenValid(idx) := true.B
      nextTraditionalSeenOwner(idx) := firstTraditionalRelease.owner
    }
    when(matchingTraditionalReleaseHit) {
      baseEntry.fallback := true.B
    }
    when(allocHit) {
      val allocated = WireInit(zeroEntry)
      allocated.valid := true.B
      allocated.preg := firstAlloc.preg
      allocated.count := 1.U
      allocated.owner := firstAlloc.owner
      allocated.releaseOwner := firstAlloc.owner
      val pendingAliasHit = pendingAliasRiskHit(firstAlloc.preg)
      allocated.fallback := pendingAliasHit || pendingAliasOverflow
      baseEntry := allocated
    }

    val validFallback = baseEntry.valid && baseEntry.fallback
    val active = baseEntry.valid && !baseEntry.fallback
    val redefineHits = VecInit((0 until nonSpecRedefineWidth).map { redefineIdx =>
      val redefine = io.nonSpecRedefine(redefineIdx)
      rawRedefineHits(redefineIdx) &&
        !redefineSuppressedByTraditional(redefineIdx) &&
        active &&
        ownerNotOlder(redefine.bits.owner, baseEntry.owner)
    })
    val redefineHit = redefineHits.asUInt.orR
    val redefineHitCount = PopCount(redefineHits)
    val firstRedefine =
      Mux(redefineHit, PriorityMux(redefineHits, io.nonSpecRedefine.map(_.bits)), 0.U.asTypeOf(new UserCountTableRedefine(pregWidth, ownerWidth)))
    for ((source, sourceIdx) <- io.sourceUse.zipWithIndex) {
      when(source.valid && source.bits === entryPreg && active && baseEntry.count =/= 0.U) {
        io.sourceUseOwner(sourceIdx).valid := true.B
        io.sourceUseOwner(sourceIdx).bits := baseEntry.owner
      }
    }
    val readHits = VecInit(rawReadHits.zip(io.readComplete).map {
      case (hit, read) => hit && baseEntry.valid && baseEntry.owner === read.bits.owner
    })
    val cancelHits = VecInit(rawCancelHits.zip(io.cancelSourceUse).map {
      case (hit, cancel) => hit && baseEntry.valid && baseEntry.owner === cancel.bits.owner
    })
    val readHit = readHits.asUInt.orR
    val cancelHit = cancelHits.asUInt.orR
    val rawReadHit = rawReadHits.asUInt.orR
    val rawCancelHit = rawCancelHits.asUInt.orR
    val readHitCount = PopCount(readHits)
    val cancelHitCount = PopCount(cancelHits)
    val sourceFallbackActive = sourceFallbackHit && active
    val aliasFallback = (moveAliasHit || traditionalAliasRiskHit) && active
    val producerWritebackAccepted = producerWritebackHit &&
      baseEntry.valid &&
      baseEntry.owner === firstProducerWriteback.owner
    val producerWrittenAfterWriteback = baseEntry.producerWritten || producerWritebackAccepted
    val sourceRejected = sourceHit && !validFallback && (!active || baseEntry.count === 0.U)
    val sourceAccepted = sourceHit && active && baseEntry.count =/= 0.U
    val consumerSeenAfterSource = baseEntry.consumerSeen || sourceAccepted

    val deadCodeFallback = redefineHit && active && baseEntry.count === 1.U && !consumerSeenAfterSource
    val readCandidate = readHit && active && baseEntry.count =/= 0.U && !deadCodeFallback
    val cancelCandidate = cancelHit &&
      active &&
      baseEntry.count =/= 0.U &&
      !deadCodeFallback &&
      (baseEntry.count > 1.U || baseEntry.redefineSeen || redefineHit)
    val redefineCandidate = redefineHit && active && baseEntry.count =/= 0.U && !deadCodeFallback
    val zeroCountDecrement = (readHit || cancelHit || redefineHit) && active && baseEntry.count === 0.U
    val incCount = Mux(sourceAccepted, sourceHitCount, 0.U)
    val readDecCount = Mux(readCandidate, readHitCount, 0.U)
    val cancelDecCount = Mux(cancelCandidate, cancelHitCount, 0.U)
    val redefineDecCount = Mux(redefineCandidate, redefineHitCount, 0.U)
    val decCount = readDecCount +& cancelDecCount +& redefineDecCount
    val countAfterIncrement = Cat(0.U(2.W), baseEntry.count) + incCount
    val underflow = zeroCountDecrement || decCount > countAfterIncrement
    val readAccepted = readCandidate && !underflow
    val cancelAccepted = cancelCandidate && !underflow
    val redefineAccepted = redefineCandidate && !underflow
    val acceptedDecCount =
      Mux(readAccepted, readHitCount, 0.U) +&
        Mux(cancelAccepted, cancelHitCount, 0.U) +&
        Mux(redefineAccepted, redefineHitCount, 0.U)
    val nextCountExt = countAfterIncrement - acceptedDecCount
    val reachesSaturation = sourceAccepted && !underflow && nextCountExt >= Cat(0.U(2.W), maxCount)

    val nextEntry = WireInit(baseEntry)
    when(sourceRejected) {
      io.rejected.sourceUse := true.B
    }
    when(rawReadHit && !validFallback &&
      (!active || baseEntry.count === 0.U || deadCodeFallback || underflow || !readHit)) {
      io.rejected.readComplete := true.B
    }
    when(rawCancelHit && !validFallback &&
      (!active || baseEntry.count === 0.U || deadCodeFallback || !cancelCandidate || underflow || !cancelHit)) {
      io.rejected.cancelSourceUse := true.B
    }
    when(redefineHit && !validFallback && active && (baseEntry.count === 0.U || underflow)) {
      io.rejected.nonSpecRedefine := true.B
    }
    when(underflow) {
      io.underflow := true.B
    }

    when(io.fallbackAll && active) {
      nextEntry.fallback := true.B
    }.elsewhen(sourceFallbackActive || aliasFallback) {
      nextEntry.fallback := true.B
    }.elsewhen(deadCodeFallback) {
      nextEntry.fallback := true.B
      io.deadCodeFallback := true.B
    }.elsewhen(reachesSaturation) {
      nextEntry.consumerSeen := true.B
      nextEntry.fallback := true.B
      nextEntry.count := maxCount
      io.saturationFallback := true.B
    }.elsewhen(!underflow) {
      nextEntry.consumerSeen := consumerSeenAfterSource
      when(redefineAccepted) {
        nextEntry.redefineSeen := true.B
        nextEntry.releaseOwner := firstRedefine.owner
      }
      nextEntry.producerWritten := producerWrittenAfterWriteback
      nextEntry.count := nextCountExt(countWidth - 1, 0)
      val canRelease =
        nextEntry.valid &&
          !nextEntry.fallback &&
          nextCountExt === 0.U &&
          consumerSeenAfterSource &&
          nextEntry.producerWritten &&
          (acceptedDecCount =/= 0.U || producerWritebackAccepted)
      when(canRelease) {
        releasePulseValid(idx) := true.B
        releasePulseBits(idx).preg := nextEntry.preg
        releasePulseBits(idx).owner := Mux(redefineAccepted, firstRedefine.owner, baseEntry.releaseOwner)
      }
    }

    val readyToRelease =
      nextEntry.valid &&
        !nextEntry.fallback &&
        nextEntry.count === 0.U &&
        nextEntry.consumerSeen &&
        nextEntry.producerWritten &&
        !nextEntry.releaseSent
    when(readyToRelease) {
      releaseReadyValid(idx) := true.B
      releaseReadyBits(idx).preg := nextEntry.preg
      releaseReadyBits(idx).owner := nextEntry.releaseOwner
    }

    nextTable(idx) := nextEntry
  }

  table := nextTable
  pendingAliasRiskValid := pendingAliasRiskNextValid
  pendingAliasRiskPreg := pendingAliasRiskNextPreg
  pendingAliasOverflow := (pendingAliasOverflow || pendingAliasOverflowSet) && !io.fallbackAll
  traditionalSeenValid := nextTraditionalSeenValid
  traditionalSeenOwner := nextTraditionalSeenOwner

  if (releaseCandidateWidth == entries) {
    io.earlyFreeCandidates.zip(releaseReadyValid).zip(releaseReadyBits).zipWithIndex.foreach {
      case (((candidate, valid), bits), idx) =>
        candidate.valid := valid
        candidate.bits := bits
        releaseAccepted(idx) := valid && io.earlyFreeCandidateAccepted(idx)
    }
  } else if (releaseCandidateWidth == 1) {
    val releaseOh = PriorityEncoderOH(releaseReadyValid)
    io.earlyFreeCandidates(0).valid := releaseReadyValid.asUInt.orR
    io.earlyFreeCandidates(0).bits := Mux1H(releaseOh, releaseReadyBits)
    releaseAccepted.zip(releaseReadyValid).zipWithIndex.foreach {
      case ((accepted, valid), idx) =>
        accepted := valid && releaseOh(idx) && io.earlyFreeCandidateAccepted(0)
    }
  } else {
    val releasePrefixCount = prefixCounts((0 until entries).map(releaseReadyValid(_)))
    for (idx <- 0 until entries) {
      val candidateIdx = releasePrefixCount(idx)
      for (outIdx <- 0 until releaseCandidateWidth) {
        when(releaseReadyValid(idx) && candidateIdx === outIdx.U) {
          io.earlyFreeCandidates(outIdx).valid := true.B
          io.earlyFreeCandidates(outIdx).bits := releaseReadyBits(idx)
          releaseAccepted(idx) := io.earlyFreeCandidateAccepted(outIdx)
        }
      }
    }
  }
  for (idx <- 0 until entries) {
    when(releaseAccepted(idx)) {
      nextTable(idx).releaseSent := true.B
    }
  }

  val releaseOh = PriorityEncoderOH(releasePulseValid)
  io.earlyFree.valid := releasePulseValid.asUInt.orR
  io.earlyFree.bits := Mux1H(releaseOh, releasePulseBits)

  XSPerfAccumulate("new_er_saturation_fallback", io.saturationFallback)
  XSPerfAccumulate("new_er_dead_code_fallback", io.deadCodeFallback)
  XSPerfAccumulate("new_er_uct_underflow", io.underflow)
  XSPerfAccumulate(
    "new_er_uct_rejected_decrement",
    NewErPerfDebug.rejectedDecrement(io.rejected)
  )
}
