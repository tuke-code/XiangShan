package xiangshan.backend.rob

import org.chipsalliance.cde.config.Parameters
import chisel3._
import chisel3.util._
import xiangshan._
import xiangshan.backend.fu.FuType
import xiangshan.backend.rename.EarlyReleaseMetadata

class SpeculationTrackerEnqueue(val idxWidth: Int)(implicit p: Parameters) extends XSBundle {
  val robIdx = UInt(idxWidth.W)
  val owner = UInt(xiangshan.backend.rename.EarlyReleaseOwner.width.W)
  val initiallySafe = Bool()
  val emitNonSpecRedefine = Bool()
  val oldIntPdestValid = Bool()
  val oldIntPdest = UInt(PhyRegIdxWidth.W)
}

class EarlyReleaseNonSpecRedefineEvent(val idxWidth: Int)(implicit p: Parameters) extends XSBundle {
  val robIdx = UInt(idxWidth.W)
  val owner = UInt(xiangshan.backend.rename.EarlyReleaseOwner.width.W)
  val oldIntPdest = UInt(PhyRegIdxWidth.W)
}

class EarlyReleaseRecoveryEvent(val idxWidth: Int)(implicit p: Parameters) extends XSBundle {
  val robIdx = UInt(idxWidth.W)
  val metadata = new EarlyReleaseMetadata
}

class EarlyReleaseKilledRecoveryEvent(val idxWidth: Int)(implicit p: Parameters) extends XSBundle {
  val robIdx = UInt(idxWidth.W)
  val metadata = new EarlyReleaseMetadata
}

class EarlyReleaseKilledRecoveryBuffer(
  entries: Int,
  outputWidth: Int
)(implicit p: Parameters) extends Module {
  require(entries > 1)
  require(outputWidth > 0)

  private val idxWidth = log2Ceil(entries)
  private val countWidth = log2Ceil(entries + 1).max(1)

  val io = IO(new Bundle {
    val capture = Input(Vec(entries, Valid(new EarlyReleaseMetadata)))
    val out = Output(Vec(outputWidth, Valid(new EarlyReleaseKilledRecoveryEvent(idxWidth))))
    val busy = Output(Bool())
  })

  private val zeroEvent = 0.U.asTypeOf(new EarlyReleaseKilledRecoveryEvent(idxWidth))
  private val pendingValid = RegInit(VecInit(Seq.fill(entries)(false.B)))
  private val pendingBits = Reg(Vec(entries, new EarlyReleaseKilledRecoveryEvent(idxWidth)))
  private val deferredValid = RegInit(VecInit(Seq.fill(entries)(false.B)))
  private val deferredBits = Reg(Vec(entries, new EarlyReleaseKilledRecoveryEvent(idxWidth)))

  val pendingCount = PopCount(pendingValid)
  val pendingPrefix = Wire(Vec(entries, UInt(countWidth.W)))
  for (idx <- 0 until entries) {
    pendingPrefix(idx) := (if (idx == 0) 0.U else pendingPrefix(idx - 1) + pendingValid(idx - 1).asUInt)
  }

  for (outIdx <- 0 until outputWidth) {
    io.out(outIdx).valid := pendingCount > outIdx.U
    val select = VecInit((0 until entries).map(idx =>
      pendingValid(idx) && pendingPrefix(idx) === outIdx.U(countWidth.W)
    ))
    io.out(outIdx).bits := Mux(io.out(outIdx).valid, Mux1H(select, pendingBits), zeroEvent)
  }

  val captureValid = VecInit(io.capture.map(_.valid))
  val captureCount = PopCount(captureValid)
  val captureBits = Wire(Vec(entries, new EarlyReleaseKilledRecoveryEvent(idxWidth)))
  for (idx <- 0 until entries) {
    captureBits(idx).robIdx := idx.U
    captureBits(idx).metadata := io.capture(idx).bits
  }

  val drainValid = VecInit((0 until entries).map { idx =>
    if (outputWidth >= entries) pendingValid(idx) else pendingValid(idx) && pendingPrefix(idx) < outputWidth.U(countWidth.W)
  })
  val remainingValid = VecInit((0 until entries).map(idx => pendingValid(idx) && !drainValid(idx)))
  val hasPending = pendingCount =/= 0.U
  val hasRemaining = remainingValid.asUInt.orR
  val hasDeferred = deferredValid.asUInt.orR
  val captureToDeferred = hasPending
  val deferredCollision = VecInit((0 until entries).map(idx =>
    captureToDeferred && captureValid(idx) && deferredValid(idx)
  )).asUInt.orR
  assert(hasPending || !hasDeferred, "New-ER killed recovery buffer deferred batch without current batch")
  assert(!deferredCollision, "New-ER killed recovery buffer overflow")

  val mergedDeferredValid = Wire(Vec(entries, Bool()))
  val mergedDeferredBits = Wire(Vec(entries, new EarlyReleaseKilledRecoveryEvent(idxWidth)))
  val nextPendingValid = Wire(Vec(entries, Bool()))
  val nextPendingBits = Wire(Vec(entries, new EarlyReleaseKilledRecoveryEvent(idxWidth)))
  val nextDeferredValid = Wire(Vec(entries, Bool()))
  val nextDeferredBits = Wire(Vec(entries, new EarlyReleaseKilledRecoveryEvent(idxWidth)))
  for (idx <- 0 until entries) {
    val captureIntoDeferred = captureToDeferred && captureValid(idx)
    mergedDeferredValid(idx) := deferredValid(idx) || captureIntoDeferred
    mergedDeferredBits(idx) := Mux(deferredValid(idx), deferredBits(idx), captureBits(idx))

    nextPendingValid(idx) := false.B
    nextPendingBits(idx) := zeroEvent
    nextDeferredValid(idx) := false.B
    nextDeferredBits(idx) := zeroEvent

    when(hasRemaining) {
      nextPendingValid(idx) := remainingValid(idx)
      nextPendingBits(idx) := Mux(remainingValid(idx), pendingBits(idx), zeroEvent)
      nextDeferredValid(idx) := mergedDeferredValid(idx)
      nextDeferredBits(idx) := Mux(mergedDeferredValid(idx), mergedDeferredBits(idx), zeroEvent)
    }.otherwise {
      when(hasPending) {
        nextPendingValid(idx) := mergedDeferredValid(idx)
        nextPendingBits(idx) := Mux(mergedDeferredValid(idx), mergedDeferredBits(idx), zeroEvent)
      }.otherwise {
        nextPendingValid(idx) := captureValid(idx)
        nextPendingBits(idx) := Mux(captureValid(idx), captureBits(idx), zeroEvent)
      }
    }
  }

  pendingValid := nextPendingValid
  pendingBits := nextPendingBits
  deferredValid := nextDeferredValid
  deferredBits := nextDeferredBits

  io.busy := hasPending || hasDeferred || captureCount =/= 0.U
}

object RobEarlyReleaseEnqueuePolicy {
  def canAccept(baseCanAccept: Bool, killedRecoveryBusy: Bool): Bool =
    baseCanAccept && !killedRecoveryBusy

  def canAcceptForDispatch(baseCanAcceptForDispatch: Bool, killedRecoveryBusy: Bool): Bool =
    baseCanAcceptForDispatch && !killedRecoveryBusy
}

object RobEarlyReleaseRedirectPolicy {
  def producerWritebackKilledByRedirect(robIdx: RobPtr, redirect: Valid[Redirect]): Bool =
    robIdx.needFlush(redirect)

  def allowProducerWriteback(
    enable: Bool,
    valid: Bool,
    intWen: Bool,
    pdestNonZero: Bool,
    robIdx: RobPtr,
    redirect: Valid[Redirect]
  ): Bool =
    enable &&
      valid &&
      intWen &&
      pdestNonZero &&
      !producerWritebackKilledByRedirect(robIdx, redirect)
}

object RobEarlyReleaseSpeculationPolicy {
  def needsExplicitConfirmation(commitType: UInt, fuType: UInt): Bool =
    CommitType.isLoadStore(commitType) || FuType.isBJU(fuType)

  def confirmationMatchesOwner(valid: Bool, writebackOwner: UInt, owner: UInt)(implicit p: Parameters): Bool =
    valid && writebackOwner === owner

  def markSafe(controlFlowSafe: Bool, memorySafe: Bool): Bool =
    controlFlowSafe || memorySafe

  def killEntry(redirectKilled: Bool, commitCond: Bool): Bool =
    redirectKilled || commitCond
}

class SpeculationTrackerEntry(val idxWidth: Int)(implicit p: Parameters) extends XSBundle {
  val valid = Bool()
  val safe = Bool()
  val owner = UInt(xiangshan.backend.rename.EarlyReleaseOwner.width.W)
  val emitNonSpecRedefine = Bool()
  val oldIntPdestValid = Bool()
  val oldIntPdest = UInt(PhyRegIdxWidth.W)
}

class SpeculationTracker(
  entries: Int,
  enqWidth: Int,
  scanWidth: Int
)(implicit p: Parameters) extends Module {
  require(entries > 1)
  require(enqWidth > 0)
  require(scanWidth > 0)
  require(scanWidth <= entries)

  private val idxWidth = log2Ceil(entries)
  private def wrapAdd(base: UInt, offset: UInt): UInt = {
    val sum = base +& offset
    Mux(sum >= entries.U(sum.getWidth.W), sum - entries.U(sum.getWidth.W), sum)(idxWidth - 1, 0)
  }

  val io = IO(new Bundle {
    val enq = Input(Vec(enqWidth, Valid(new SpeculationTrackerEnqueue(idxWidth))))
    val markSafe = Input(Vec(entries, Bool()))
    val kill = Input(Vec(entries, Bool()))
    val clear = Input(Vec(entries, Bool()))
    val scanEnable = Input(Bool())
    val events = Output(Vec(scanWidth, Valid(new EarlyReleaseNonSpecRedefineEvent(idxWidth))))
    val scanned = Output(Vec(scanWidth, Bool()))
    val shPtr = Output(UInt(idxWidth.W))
  })

  private val table = RegInit(0.U.asTypeOf(Vec(entries, new SpeculationTrackerEntry(idxWidth))))
  private val shPtr = RegInit(0.U(idxWidth.W))

  private val tableAfterStatus = WireDefault(table)
  for (idx <- 0 until entries) {
    when(io.markSafe(idx)) {
      tableAfterStatus(idx).safe := true.B
    }
    when(io.kill(idx) && table(idx).valid) {
      tableAfterStatus(idx) := 0.U.asTypeOf(tableAfterStatus(idx))
      tableAfterStatus(idx).valid := true.B
      tableAfterStatus(idx).safe := true.B
    }
    when(io.clear(idx)) {
      tableAfterStatus(idx) := 0.U.asTypeOf(tableAfterStatus(idx))
    }
  }

  private val scanEntries = Wire(Vec(scanWidth, new SpeculationTrackerEntry(idxWidth)))
  private val scanIdxs = Wire(Vec(scanWidth, UInt(idxWidth.W)))
  private val prefixScannable = Wire(Vec(scanWidth, Bool()))

  for (slot <- 0 until scanWidth) {
    val scanIdx = wrapAdd(shPtr, slot.U)
    scanIdxs(slot) := scanIdx
    scanEntries(slot) := tableAfterStatus(scanIdx)
    val thisScannable = tableAfterStatus(scanIdx).valid && tableAfterStatus(scanIdx).safe
    val priorScannable = if (slot == 0) {
      true.B
    } else {
      VecInit((0 until slot).map(prefixScannable(_))).asUInt.andR
    }
    prefixScannable(slot) := io.scanEnable && thisScannable && priorScannable

    io.scanned(slot) := prefixScannable(slot)
    io.events(slot).valid := prefixScannable(slot) &&
      scanEntries(slot).emitNonSpecRedefine &&
      scanEntries(slot).oldIntPdestValid
    io.events(slot).bits.robIdx := scanIdxs(slot)
    io.events(slot).bits.owner := Mux(io.events(slot).valid, scanEntries(slot).owner, 0.U)
    io.events(slot).bits.oldIntPdest := Mux(io.events(slot).valid, scanEntries(slot).oldIntPdest, 0.U)
    assert(!(io.kill(scanIdxs(slot)) && io.events(slot).valid), "New-ER killed entry emitted non-spec redefine")
  }

  private val scanCount = PopCount(prefixScannable)
  private val tableAfterScan = WireDefault(tableAfterStatus)
  for (slot <- 0 until scanWidth) {
    when(prefixScannable(slot)) {
      tableAfterScan(scanIdxs(slot)) := 0.U.asTypeOf(tableAfterScan(scanIdxs(slot)))
    }
  }

  private val tableNext = WireDefault(tableAfterScan)
  for (lane <- 0 until enqWidth) {
    when(io.enq(lane).valid) {
      tableNext(io.enq(lane).bits.robIdx) := 0.U.asTypeOf(tableNext(io.enq(lane).bits.robIdx))
      tableNext(io.enq(lane).bits.robIdx).valid := true.B
      tableNext(io.enq(lane).bits.robIdx).safe := io.enq(lane).bits.initiallySafe
      tableNext(io.enq(lane).bits.robIdx).owner := io.enq(lane).bits.owner
      tableNext(io.enq(lane).bits.robIdx).emitNonSpecRedefine := io.enq(lane).bits.emitNonSpecRedefine
      tableNext(io.enq(lane).bits.robIdx).oldIntPdestValid := io.enq(lane).bits.oldIntPdestValid
      tableNext(io.enq(lane).bits.robIdx).oldIntPdest := io.enq(lane).bits.oldIntPdest
    }
  }

  table := tableNext
  shPtr := wrapAdd(shPtr, scanCount)
  io.shPtr := shPtr
}
