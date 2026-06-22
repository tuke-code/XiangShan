package xiangshan.backend

import chisel3._
import chisel3.simulator.scalatest.ChiselSim
import chisel3.util._
import org.chipsalliance.cde.config.Parameters
import org.scalatest.flatspec.AnyFlatSpec
import org.scalatest.matchers.should.Matchers
import top.DefaultConfig
import utility.{LogUtilsOptions, LogUtilsOptionsKey, PerfCounterOptions, PerfCounterOptionsKey, XSPerfLevel}
import xiangshan._
import xiangshan.backend.Bundles.EnqRobUop
import xiangshan.backend.rob.RobBundles.RobEntryBundle
import xiangshan.backend.rob.{RobBundles, RobIntDiffOps, RobIntEROps, RobPtr}

import java.nio.file.{Path, Paths}
import scala.io.Source

class IntERRobReadDoneValidationProbe(implicit p: Parameters) extends XSModule {
  private val entryCount = 4
  private val trackedRobIdx = 1
  private val trackedSrc = 1
  require(EnableIntEarlyRegRelease, "probe requires Int ER metadata")
  require(backendParams.numSrc > trackedSrc, "probe requires at least two logical sources")

  val io = IO(new Bundle {
    val load = Input(Bool())
    val loadEntryValid = Input(Bool())
    val loadSrcValid = Input(Bool())
    val loadReadDone = Input(Bool())
    val loadTrackId = Input(UInt(IntERTrackIdWidth.W))
    val loadTrackGen = Input(UInt(IntERTrackGenBits.W))
    val loadPsrc = Input(UInt(PhyRegIdxWidth.W))
    val redirect = Input(Valid(new Redirect))
    val raw = Input(Vec(IntERReadDoneWidth, Valid(new IntERSrcValueReadDone)))
    val dec = Output(Vec(IntERReadDoneWidth, Valid(new IntERSrcValueReadDone)))
    val status = Output(Vec(IntERReadDoneWidth, new IntERRobReadDoneStatus))
    val selectedReadDone = Output(Bool())
  })

  private val entries = RegInit(VecInit.fill(entryCount)(0.U.asTypeOf(new RobEntryBundle)))
  private val marks = Wire(Vec(entryCount, Vec(backendParams.numSrc, Bool())))

  RobIntEROps.validateReadDoneEvents(
    out = io.dec,
    markReadDone = marks,
    raw = io.raw,
    redirect = io.redirect,
    entries = entries,
    status = Some(io.status)
  )

  for (entry <- 0 until entryCount) {
    for (src <- 0 until backendParams.numSrc) {
      when(marks(entry)(src)) {
        entries(entry).intER.get.src(src).readDone := true.B
      }
    }
  }

  when(io.load) {
    entries(trackedRobIdx) := 0.U.asTypeOf(new RobEntryBundle)
    entries(trackedRobIdx).valid := io.loadEntryValid
    entries(trackedRobIdx).intER.get.src(trackedSrc).valid := io.loadSrcValid
    entries(trackedRobIdx).intER.get.src(trackedSrc).trackId := io.loadTrackId
    entries(trackedRobIdx).intER.get.src(trackedSrc).trackGen := io.loadTrackGen
    entries(trackedRobIdx).intER.get.src(trackedSrc).srcIdx := trackedSrc.U
    entries(trackedRobIdx).intER.get.src(trackedSrc).psrc := io.loadPsrc
    entries(trackedRobIdx).intER.get.src(trackedSrc).readDone := io.loadReadDone
  }

  io.selectedReadDone := entries(trackedRobIdx).intER.get.src(trackedSrc).readDone
}

class IntERRobSTGuardProbe(implicit p: Parameters) extends XSModule {
  private val entryCount = 4
  require(EnableIntEarlyRegRelease, "probe requires Int ER metadata")

  val io = IO(new Bundle {
    val load = Input(Bool())
    val loadIdx = Input(UInt(log2Ceil(entryCount).W))
    val loadValid = Input(Bool())
    val loadUopNum = Input(UInt(log2Up(MaxUopSize + 1).W))
    val loadNeedFlush = Input(Bool())
    val loadRedefValid = Input(Bool())
    val loadResolved = Input(Bool())
    val loadGuardEmitted = Input(Bool())
    val loadTrackId = Input(UInt(IntERTrackIdWidth.W))
    val loadTrackGen = Input(UInt(IntERTrackGenBits.W))
    val loadOldPdest = Input(UInt(PhyRegIdxWidth.W))
    val stop = Input(Bool())
    val cursorValue = Input(UInt(log2Ceil(RobSize).W))
    val guard = Output(Vec(IntERSTWalkWidth, Valid(new IntERSTGuardDec)))
    val markGuardEmitted = Output(Vec(entryCount, Bool()))
    val advance = Output(UInt(log2Ceil(IntERSTWalkWidth + 1).W))
  })

  private val entries = RegInit(VecInit.fill(entryCount)(0.U.asTypeOf(new RobEntryBundle)))
  private val safeToCross = Wire(Vec(entryCount, Bool()))
  for (entry <- 0 until entryCount) {
    safeToCross(entry) := entries(entry).intER.get.resolved
  }
  private val cursor = Wire(new RobPtr)
  cursor.flag := false.B
  cursor.value := io.cursorValue

  io.advance := RobIntEROps.emitSTGuardDec(
    out = io.guard,
    markGuardEmitted = io.markGuardEmitted,
    cursor = cursor,
    stop = io.stop || io.load,
    entries = entries,
    safeToCross = safeToCross
  )

  for (entry <- 0 until entryCount) {
    when(io.markGuardEmitted(entry)) {
      entries(entry).intER.get.guardEmitted := true.B
    }
  }

  for (entry <- 0 until entryCount) {
    when(io.load && io.loadIdx === entry.U) {
      entries(entry) := 0.U.asTypeOf(new RobEntryBundle)
      entries(entry).valid := io.loadValid
      entries(entry).uopNum := io.loadUopNum
      entries(entry).needFlush := io.loadNeedFlush
      entries(entry).intER.get.redef.valid := io.loadRedefValid
      entries(entry).intER.get.redef.trackId := io.loadTrackId
      entries(entry).intER.get.redef.trackGen := io.loadTrackGen
      entries(entry).intER.get.redef.oldPdest := io.loadOldPdest
      entries(entry).intER.get.resolved := io.loadResolved
      entries(entry).intER.get.guardEmitted := io.loadGuardEmitted
    }
  }
}

class IntERRobGuardEmittedRedefFlushProbe(implicit p: Parameters) extends XSModule {
  require(EnableIntEarlyRegRelease, "probe requires Int ER metadata")

  val io = IO(new Bundle {
    val entryValid = Input(Bool())
    val redefValid = Input(Bool())
    val guardEmitted = Input(Bool())
    val invalidatedByRedirect = Input(Bool())
    val alive = Output(Bool())
  })

  private val entry = Wire(new RobEntryBundle)
  entry := 0.U.asTypeOf(entry)
  entry.valid := io.entryValid
  entry.intER.get.redef.valid := io.redefValid
  entry.intER.get.guardEmitted := io.guardEmitted

  RobIntEROps.assertGuardEmittedRedefNotFlushed(
    entry = entry,
    invalidatedByRedirect = io.invalidatedByRedirect
  )

  io.alive := entry.valid
}

class IntERRobMultiUopRejectProbe(implicit p: Parameters) extends XSModule {
  require(EnableIntEarlyRegRelease, "probe requires Int ER metadata")

  val io = IO(new Bundle {
    val firstUop = Input(Bool())
    val lastUop = Input(Bool())
    val numUops = Input(UInt(log2Ceil(MaxUopSize).W))
    val srcValid = Input(Bool())
    val destValid = Input(Bool())
    val redefValid = Input(Bool())
    val entrySrcValid = Output(Bool())
  })

  private val enq = Wire(new EnqRobUop)
  private val entry = Wire(new RobEntryBundle)

  enq := 0.U.asTypeOf(enq)
  entry := 0.U.asTypeOf(entry)

  enq.firstUop := io.firstUop
  enq.lastUop := io.lastUop
  enq.numUops := io.numUops
  enq.numWB := enq.numUops
  enq.intER.get.src(0).valid := io.srcValid
  enq.intER.get.src(0).trackId := 1.U
  enq.intER.get.src(0).trackGen := 3.U
  enq.intER.get.src(0).srcIdx := 0.U
  enq.intER.get.src(0).psrc := 21.U
  enq.intER.get.dest.valid := io.destValid
  enq.intER.get.dest.trackId := 1.U
  enq.intER.get.dest.trackGen := 3.U
  enq.intER.get.dest.pdest := 22.U
  enq.intER.get.redef.valid := io.redefValid
  enq.intER.get.redef.trackId := 1.U
  enq.intER.get.redef.trackGen := 3.U
  enq.intER.get.redef.oldPdest := 23.U

  RobBundles.connectEnq(entry, enq)
  io.entrySrcValid := entry.intER.get.src(0).valid
}

class IntERRobDirectDiffShadowProbe(implicit p: Parameters) extends XSModule {
  private val lanes = 3

  val io = IO(new Bundle {
    val load = Input(Bool())
    val loadAddr = Input(UInt(log2Ceil(IntLogicRegs).W))
    val loadData = Input(UInt(XLEN.W))
    val valid = Input(Vec(lanes, Bool()))
    val skip = Input(Vec(lanes, Bool()))
    val rfWen = Input(Vec(lanes, Bool()))
    val isMove = Input(Vec(lanes, Bool()))
    val ldest = Input(Vec(lanes, UInt(LogicRegsWidth.W)))
    val moveSrc = Input(Vec(lanes, UInt(LogicRegsWidth.W)))
    val writeData = Input(Vec(lanes, UInt(XLEN.W)))
    val instrSkip = Output(Vec(lanes, Bool()))
    val shadow = Output(Vec(IntLogicRegs, UInt(XLEN.W)))
    val commitData = Output(Vec(lanes, UInt(XLEN.W)))
  })

  private val shadow = RegInit(VecInit.fill(IntLogicRegs)(0.U(XLEN.W)))
  io.instrSkip := io.skip
  val currentShadow = Wire(Vec(IntLogicRegs, UInt(XLEN.W)))
  currentShadow := shadow
  when(io.load) {
    currentShadow(io.loadAddr) := io.loadData
  }

  val nextShadow = Wire(Vec(IntLogicRegs, UInt(XLEN.W)))
  RobIntDiffOps.updateShadow(
    current = currentShadow,
    next = nextShadow,
    commitData = io.commitData,
    valid = io.valid,
    rfWen = io.rfWen,
    isMove = io.isMove,
    ldest = io.ldest,
    moveSrc = io.moveSrc,
    writeData = io.writeData
  )

  shadow := nextShadow
  io.shadow := shadow
}

class IntEarlyReleaseRobTest extends AnyFlatSpec with Matchers with ChiselSim {
  behavior of "IntEarlyRelease ROB readDone validation"

  private def sourceText(path: String): String = {
    val source = Source.fromFile(repoPath(path).toFile)
    try {
      source.mkString
    } finally {
      source.close()
    }
  }

  private def repoPath(path: String): Path = {
    val relative = Paths.get(path)
    Iterator.iterate(Paths.get("").toAbsolutePath)(_.getParent)
      .takeWhile(_ != null)
      .map(_.resolve(relative))
      .find(path => java.nio.file.Files.exists(path))
      .getOrElse(relative)
  }

  private def configWith(params: IntEarlyReleaseParams): Parameters = {
    val defaultConfig = new DefaultConfig
    defaultConfig.alterPartial({
      case XSCoreParamsKey => defaultConfig(XSTileKey).head.copy(
        intEarlyRelease = params
      )
    }).alter((site, here, up) => {
      case DebugOptionsKey => up(DebugOptionsKey).copy(
        AlwaysBasicDiff = false,
        EnableDifftest = false,
        EnablePerfDebug = false,
        EnableDebug = false
      )
      case LogUtilsOptionsKey => LogUtilsOptions(
        here(DebugOptionsKey).EnableDebug,
        here(DebugOptionsKey).EnablePerfDebug,
        here(DebugOptionsKey).FPGAPlatform,
        here(DebugOptionsKey).EnableXMR
      )
      case PerfCounterOptionsKey => PerfCounterOptions(
        enablePerfPrint = false,
        enablePerfDB = false,
        perfLevel = XSPerfLevel.withName(here(DebugOptionsKey).PerfLevel),
        perfDBHartID = 0
      )
    })
  }

  private def setRobPtr(ptr: RobPtr, value: Int): Unit = {
    ptr.flag.poke(false.B)
    ptr.value.poke(value.U)
  }

  private def clearRaw(dut: IntERRobReadDoneValidationProbe): Unit = {
    for (lane <- 0 until dut.io.raw.length) {
      dut.io.raw(lane).valid.poke(false.B)
      setRobPtr(dut.io.raw(lane).bits.robIdx, 0)
      dut.io.raw(lane).bits.fallback.poke(false.B)
      dut.io.raw(lane).bits.reason.poke(IntERFallbackReason.none)
      for (src <- 0 until dut.io.raw(lane).bits.src.length) {
        dut.io.raw(lane).bits.src(src).valid.poke(false.B)
        dut.io.raw(lane).bits.src(src).trackId.poke(0.U)
        dut.io.raw(lane).bits.src(src).trackGen.poke(0.U)
        dut.io.raw(lane).bits.src(src).srcIdx.poke(src.U)
        dut.io.raw(lane).bits.src(src).psrc.poke(0.U)
      }
    }
    dut.io.load.poke(false.B)
    dut.io.loadEntryValid.poke(false.B)
    dut.io.loadSrcValid.poke(false.B)
    dut.io.loadReadDone.poke(false.B)
    dut.io.loadTrackId.poke(0.U)
    dut.io.loadTrackGen.poke(0.U)
    dut.io.loadPsrc.poke(0.U)
    dut.io.redirect.valid.poke(false.B)
    dut.io.redirect.bits.poke(0.U.asTypeOf(dut.io.redirect.bits))
  }

  private def clearSTGuard(dut: IntERRobSTGuardProbe): Unit = {
    dut.io.load.poke(false.B)
    dut.io.loadIdx.poke(0.U)
    dut.io.loadValid.poke(false.B)
    dut.io.loadUopNum.poke(0.U)
    dut.io.loadNeedFlush.poke(false.B)
    dut.io.loadRedefValid.poke(false.B)
    dut.io.loadResolved.poke(false.B)
    dut.io.loadGuardEmitted.poke(false.B)
    dut.io.loadTrackId.poke(0.U)
    dut.io.loadTrackGen.poke(0.U)
    dut.io.loadOldPdest.poke(0.U)
    dut.io.stop.poke(false.B)
    dut.io.cursorValue.poke(0.U)
  }

  private def loadSTEntry(
    dut: IntERRobSTGuardProbe,
    idx: Int,
    valid: Boolean = true,
    writebacked: Boolean = true,
    needFlush: Boolean = false,
    redefValid: Boolean = true,
    resolved: Boolean = false,
    guardEmitted: Boolean = false,
    trackId: Int = 1,
    trackGen: Int = 3,
    oldPdest: Int = 21
  ): Unit = {
    clearSTGuard(dut)
    dut.io.load.poke(true.B)
    dut.io.loadIdx.poke(idx.U)
    dut.io.loadValid.poke(valid.B)
    dut.io.loadUopNum.poke((if (writebacked) 0 else 1).U)
    dut.io.loadNeedFlush.poke(needFlush.B)
    dut.io.loadRedefValid.poke(redefValid.B)
    dut.io.loadResolved.poke(resolved.B)
    dut.io.loadGuardEmitted.poke(guardEmitted.B)
    dut.io.loadTrackId.poke(trackId.U)
    dut.io.loadTrackGen.poke(trackGen.U)
    dut.io.loadOldPdest.poke(oldPdest.U)
    dut.clock.step()
    clearSTGuard(dut)
  }

  private def loadTrackedSource(
    dut: IntERRobReadDoneValidationProbe,
    entryValid: Boolean = true,
    srcValid: Boolean = true,
    readDone: Boolean = false,
    trackId: Int = 1,
    trackGen: Int = 3,
    psrc: Int = 21
  ): Unit = {
    clearRaw(dut)
    dut.io.load.poke(true.B)
    dut.io.loadEntryValid.poke(entryValid.B)
    dut.io.loadSrcValid.poke(srcValid.B)
    dut.io.loadReadDone.poke(readDone.B)
    dut.io.loadTrackId.poke(trackId.U)
    dut.io.loadTrackGen.poke(trackGen.U)
    dut.io.loadPsrc.poke(psrc.U)
    dut.clock.step()
    clearRaw(dut)
  }

  private def driveReadDone(
    dut: IntERRobReadDoneValidationProbe,
    lane: Int,
    robIdx: Int = 1,
    srcSlot: Int = 1,
    trackId: Int = 1,
    trackGen: Int = 3,
    psrc: Int = 21,
    fallback: Boolean = false,
    reason: UInt = IntERFallbackReason.none
  ): Unit = {
    dut.io.raw(lane).valid.poke(true.B)
    setRobPtr(dut.io.raw(lane).bits.robIdx, robIdx)
    dut.io.raw(lane).bits.fallback.poke(fallback.B)
    dut.io.raw(lane).bits.reason.poke(reason)
    dut.io.raw(lane).bits.src(srcSlot).valid.poke(true.B)
    dut.io.raw(lane).bits.src(srcSlot).trackId.poke(trackId.U)
    dut.io.raw(lane).bits.src(srcSlot).trackGen.poke(trackGen.U)
    dut.io.raw(lane).bits.src(srcSlot).srcIdx.poke(srcSlot.U)
    dut.io.raw(lane).bits.src(srcSlot).psrc.poke(psrc.U)
  }

  private def driveRedirect(
    dut: IntERRobReadDoneValidationProbe,
    robIdx: Int,
    flushSelf: Boolean
  ): Unit = {
    dut.io.redirect.valid.poke(true.B)
    dut.io.redirect.bits.poke(0.U.asTypeOf(dut.io.redirect.bits))
    setRobPtr(dut.io.redirect.bits.robIdx, robIdx)
    dut.io.redirect.bits.level.poke(flushSelf.B)
  }

  it should "fail fast on out-of-range readDone source indexes before stored-source lookup" in {
    val robBundlesSource = sourceText("src/main/scala/xiangshan/backend/rob/RobBundles.scala")

    robBundlesSource should include("ROB ER readDone source index out of range")
    robBundlesSource should include("val safeSrcIdx = Mux(srcIdxInRange, srcEvent.srcIdx, 0.U)")
    robBundlesSource should include("val stored = robEntry.intER.get.src(safeSrcIdx)")
    robBundlesSource should not include "val stored = robEntry.intER.get.src(srcEvent.srcIdx)"
  }

  it should "emit one validated decrement and suppress later duplicates" in {
    val config = configWith(IntEarlyReleaseParams(enable = true, trackEntries = 2))

    simulate(new IntERRobReadDoneValidationProbe()(config)) { dut =>
      clearRaw(dut)
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      loadTrackedSource(dut)
      driveReadDone(dut, lane = 0)

      dut.io.dec(0).valid.expect(true.B)
      dut.io.dec(0).bits.src(1).valid.expect(true.B)
      dut.io.dec(0).bits.src(1).trackId.expect(1.U)
      dut.io.dec(0).bits.src(1).trackGen.expect(3.U)
      dut.io.dec(0).bits.src(1).srcIdx.expect(1.U)
      dut.clock.step()

      clearRaw(dut)
      dut.io.selectedReadDone.expect(true.B)
      driveReadDone(dut, lane = 0)
      dut.io.dec(0).valid.expect(false.B)
    }
  }

  it should "filter mismatched and invalid events while preserving keyed fallback" in {
    val config = configWith(IntEarlyReleaseParams(enable = true, trackEntries = 2))

    simulate(new IntERRobReadDoneValidationProbe()(config)) { dut =>
      clearRaw(dut)
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      loadTrackedSource(dut)
      driveReadDone(dut, lane = 0, trackGen = 4)
      dut.io.dec(0).valid.expect(false.B)
      dut.clock.step()

      loadTrackedSource(dut)
      driveReadDone(dut, lane = 0, srcSlot = 0)
      dut.io.dec(0).valid.expect(false.B)
      dut.clock.step()

      loadTrackedSource(dut, srcValid = false)
      driveReadDone(dut, lane = 0)
      dut.io.dec(0).valid.expect(false.B)
      dut.clock.step()

      loadTrackedSource(dut)
      driveReadDone(dut, lane = 0, robIdx = 2)
      dut.io.dec(0).valid.expect(false.B)
      dut.clock.step()

      loadTrackedSource(dut)
      driveReadDone(dut, lane = 0)
      driveReadDone(dut, lane = 1)
      dut.io.dec(0).valid.expect(true.B)
      dut.io.dec(1).valid.expect(false.B)
      dut.clock.step()

      loadTrackedSource(dut)
      driveReadDone(
        dut,
        lane = 0,
        fallback = true,
        reason = IntERFallbackReason.unsupportedReadPath
      )
      dut.io.dec(0).valid.expect(true.B)
      dut.io.dec(0).bits.fallback.expect(true.B)
      dut.io.dec(0).bits.reason.expect(IntERFallbackReason.unsupportedReadPath)
      dut.io.dec(0).bits.src(1).valid.expect(true.B)
      dut.clock.step()

      clearRaw(dut)
      dut.io.selectedReadDone.expect(true.B)
    }
  }

  it should "filter raw readDone killed by a redirect to an older entry" in {
    val config = configWith(IntEarlyReleaseParams(enable = true, trackEntries = 2))

    simulate(new IntERRobReadDoneValidationProbe()(config)) { dut =>
      clearRaw(dut)
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      loadTrackedSource(dut)
      driveReadDone(dut, lane = 0, robIdx = 1)
      driveRedirect(dut, robIdx = 0, flushSelf = false)

      dut.io.dec(0).valid.expect(false.B)
      dut.clock.step()

      clearRaw(dut)
      dut.io.selectedReadDone.expect(false.B)
    }
  }

  it should "filter raw readDone on the redirecting entry when redirect flushes itself" in {
    val config = configWith(IntEarlyReleaseParams(enable = true, trackEntries = 2))

    simulate(new IntERRobReadDoneValidationProbe()(config)) { dut =>
      clearRaw(dut)
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      loadTrackedSource(dut)
      driveReadDone(dut, lane = 0, robIdx = 1)
      driveRedirect(dut, robIdx = 1, flushSelf = true)

      dut.io.dec(0).valid.expect(false.B)
      dut.clock.step()

      clearRaw(dut)
      dut.io.selectedReadDone.expect(false.B)
    }
  }

  it should "classify accepted stale fallback and duplicate readDone events for perf" in {
    val config = configWith(IntEarlyReleaseParams(enable = true, trackEntries = 2))

    simulate(new IntERRobReadDoneValidationProbe()(config)) { dut =>
      clearRaw(dut)
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      loadTrackedSource(dut)
      driveReadDone(dut, lane = 0)
      driveReadDone(dut, lane = 1)
      dut.io.status(0).sawRaw.expect(true.B)
      dut.io.status(0).accepted.expect(true.B)
      dut.io.status(0).fallback.expect(false.B)
      dut.io.status(0).stale.expect(false.B)
      dut.io.status(0).duplicate.expect(false.B)
      dut.io.status(1).sawRaw.expect(true.B)
      dut.io.status(1).accepted.expect(false.B)
      dut.io.status(1).duplicate.expect(true.B)

      clearRaw(dut)
      driveReadDone(dut, lane = 0, trackGen = 4)
      dut.io.status(0).sawRaw.expect(true.B)
      dut.io.status(0).accepted.expect(false.B)
      dut.io.status(0).stale.expect(true.B)
      dut.io.status(0).duplicate.expect(false.B)

      clearRaw(dut)
      driveReadDone(dut, lane = 0, fallback = true, reason = IntERFallbackReason.unsupportedReadPath)
      dut.io.status(0).sawRaw.expect(true.B)
      dut.io.status(0).accepted.expect(true.B)
      dut.io.status(0).fallback.expect(true.B)
      dut.io.status(0).stale.expect(false.B)
    }
  }

  it should "emit guard decrement only across resolved ROB entries" in {
    val config = configWith(IntEarlyReleaseParams(enable = true, trackEntries = 2, stWalkWidth = 2))

    simulate(new IntERRobSTGuardProbe()(config)) { dut =>
      clearSTGuard(dut)
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      loadSTEntry(dut, idx = 0, resolved = true, trackId = 1, trackGen = 3, oldPdest = 21)
      loadSTEntry(dut, idx = 1, writebacked = false, trackId = 0, trackGen = 1, oldPdest = 31)

      dut.io.cursorValue.poke(0.U)
      dut.io.guard(0).valid.expect(true.B)
      dut.io.guard(0).bits.valid.expect(true.B)
      dut.io.guard(0).bits.trackId.expect(1.U)
      dut.io.guard(0).bits.trackGen.expect(3.U)
      dut.io.guard(0).bits.oldPdest.expect(21.U)
      dut.io.guard(1).valid.expect(false.B)
      dut.io.markGuardEmitted(0).expect(true.B)
      dut.io.markGuardEmitted(1).expect(false.B)
      dut.io.advance.expect(1.U)
      dut.clock.step()

      clearSTGuard(dut)
      dut.io.cursorValue.poke(0.U)
      dut.io.guard(0).valid.expect(false.B)
    }
  }

  it should "stop guard decrement at explicit stop or unresolved older entry" in {
    val config = configWith(IntEarlyReleaseParams(enable = true, trackEntries = 2, stWalkWidth = 2))

    simulate(new IntERRobSTGuardProbe()(config)) { dut =>
      clearSTGuard(dut)
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      loadSTEntry(dut, idx = 0, writebacked = true, resolved = false, trackId = 1, trackGen = 3, oldPdest = 21)
      loadSTEntry(dut, idx = 1, trackId = 0, trackGen = 1, oldPdest = 31)

      dut.io.cursorValue.poke(0.U)
      dut.io.guard(0).valid.expect(false.B)
      dut.io.guard(1).valid.expect(false.B)
      dut.io.advance.expect(0.U)

      loadSTEntry(dut, idx = 0, resolved = true, trackId = 1, trackGen = 3, oldPdest = 21)
      dut.io.cursorValue.poke(0.U)
      dut.io.guard(0).valid.expect(true.B)
      dut.io.guard(0).bits.trackId.expect(1.U)
      dut.io.guard(0).bits.trackGen.expect(3.U)
      dut.io.guard(0).bits.oldPdest.expect(21.U)
      dut.io.guard(1).valid.expect(false.B)
      dut.io.advance.expect(1.U)
      dut.clock.step()
      clearSTGuard(dut)
      dut.io.cursorValue.poke(0.U)
      dut.io.guard(0).valid.expect(false.B)

      loadSTEntry(dut, idx = 0, resolved = true, trackId = 1, trackGen = 3, oldPdest = 21)
      dut.io.cursorValue.poke(0.U)
      dut.io.stop.poke(true.B)
      dut.io.guard(0).valid.expect(false.B)
      dut.io.guard(1).valid.expect(false.B)
      dut.io.advance.expect(0.U)
    }
  }

  it should "fail fast if redirect flushes a guard-emitted ER redefiner" in {
    val robSource = sourceText("src/main/scala/xiangshan/backend/rob/Rob.scala")
    val robBundlesSource = sourceText("src/main/scala/xiangshan/backend/rob/RobBundles.scala")

    robSource should include("RobIntEROps.assertGuardEmittedRedefNotFlushed")
    robSource should include("val invalidatedByRedirect = !commitCond && !enqCond && needFlush")
    robBundlesSource should include("ROB ER guard-emitted redefiner flushed by redirect")
    robBundlesSource should include("entry.valid && invalidatedByRedirect && entry.intER.get.redef.valid && entry.intER.get.guardEmitted")
    robBundlesSource should include("!guardEmittedRedefFlushed")
  }

  it should "not assert for safe guard-emitted redefiner redirect combinations" in {
    val config = configWith(IntEarlyReleaseParams(enable = true, trackEntries = 2))

    simulate(new IntERRobGuardEmittedRedefFlushProbe()(config)) { dut =>
      Seq(
        (false, true, true, true),
        (true, false, true, true),
        (true, true, false, true),
        (true, true, true, false)
      ).foreach { case (entryValid, redefValid, guardEmitted, invalidated) =>
        dut.io.entryValid.poke(entryValid.B)
        dut.io.redefValid.poke(redefValid.B)
        dut.io.guardEmitted.poke(guardEmitted.B)
        dut.io.invalidatedByRedirect.poke(invalidated.B)
        dut.clock.step()
        dut.io.alive.expect(entryValid.B)
      }
    }
  }

  it should "assert when a guard-emitted ER redefiner is invalidated by redirect" in {
    val config = configWith(IntEarlyReleaseParams(enable = true, trackEntries = 2))

    assertThrows[Exception] {
      simulate(new IntERRobGuardEmittedRedefFlushProbe()(config)) { dut =>
        dut.io.entryValid.poke(true.B)
        dut.io.redefValid.poke(true.B)
        dut.io.guardEmitted.poke(true.B)
        dut.io.invalidatedByRedirect.poke(true.B)
        dut.clock.step()
      }
    }
  }

  it should "reject tracked ER metadata on multi-uop ROB entries" in {
    val config = configWith(IntEarlyReleaseParams(enable = true, trackEntries = 2))

    def driveEntry(
      dut: IntERRobMultiUopRejectProbe,
      firstUop: Boolean,
      lastUop: Boolean,
      numUops: Int,
      srcValid: Boolean,
      destValid: Boolean,
      redefValid: Boolean
    ): Unit = {
      dut.io.firstUop.poke(firstUop.B)
      dut.io.lastUop.poke(lastUop.B)
      dut.io.numUops.poke(numUops.U)
      dut.io.srcValid.poke(srcValid.B)
      dut.io.destValid.poke(destValid.B)
      dut.io.redefValid.poke(redefValid.B)
    }

    def expectReject(
      firstUop: Boolean,
      lastUop: Boolean,
      numUops: Int,
      srcValid: Boolean,
      destValid: Boolean,
      redefValid: Boolean
    ): Unit = {
      assertThrows[Exception] {
        simulate(new IntERRobMultiUopRejectProbe()(config)) { dut =>
          driveEntry(dut, firstUop, lastUop, numUops, srcValid, destValid, redefValid)
          dut.clock.step()
        }
      }
    }

    def expectPass(
      firstUop: Boolean,
      lastUop: Boolean,
      numUops: Int,
      srcValid: Boolean,
      destValid: Boolean,
      redefValid: Boolean
    ): Unit = {
      noException should be thrownBy {
        simulate(new IntERRobMultiUopRejectProbe()(config)) { dut =>
          driveEntry(dut, firstUop, lastUop, numUops, srcValid, destValid, redefValid)
          dut.clock.step()
          dut.io.entrySrcValid.expect(srcValid.B)
        }
      }
    }

    expectReject(firstUop = true, lastUop = false, numUops = 2, srcValid = true, destValid = false, redefValid = false)
    expectReject(firstUop = true, lastUop = false, numUops = 2, srcValid = false, destValid = true, redefValid = false)
    expectReject(firstUop = true, lastUop = false, numUops = 2, srcValid = false, destValid = false, redefValid = true)
    expectReject(firstUop = true, lastUop = false, numUops = 2, srcValid = true, destValid = true, redefValid = true)
    expectReject(firstUop = false, lastUop = true, numUops = 2, srcValid = true, destValid = true, redefValid = true)
    expectReject(firstUop = true, lastUop = true, numUops = 2, srcValid = true, destValid = true, redefValid = true)

    expectPass(firstUop = true, lastUop = false, numUops = 2, srcValid = false, destValid = false, redefValid = false)
    expectPass(firstUop = true, lastUop = true, numUops = 1, srcValid = true, destValid = true, redefValid = true)
  }

  it should "update direct integer diff shadow in commit-lane order with move source value and x0 zeroed" in {
    val config = configWith(IntEarlyReleaseParams(enable = true, trackEntries = 2))

    simulate(new IntERRobDirectDiffShadowProbe()(config)) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      def clear(): Unit = {
        dut.io.load.poke(false.B)
        dut.io.loadAddr.poke(0.U)
        dut.io.loadData.poke(0.U)
        for (lane <- 0 until dut.io.valid.length) {
          dut.io.valid(lane).poke(false.B)
          dut.io.skip(lane).poke(false.B)
          dut.io.rfWen(lane).poke(false.B)
          dut.io.isMove(lane).poke(false.B)
          dut.io.ldest(lane).poke(0.U)
          dut.io.moveSrc(lane).poke(0.U)
          dut.io.writeData(lane).poke(0.U)
        }
      }

      def loadReg(addr: Int, data: BigInt): Unit = {
        clear()
        dut.io.load.poke(true.B)
        dut.io.loadAddr.poke(addr.U)
        dut.io.loadData.poke(data.U)
        dut.clock.step()
      }

      clear()
      loadReg(1, BigInt("1111", 16))
      loadReg(2, BigInt("2222", 16))
      loadReg(3, BigInt("3333", 16))
      loadReg(5, BigInt("5555", 16))
      clear()

      dut.io.valid(0).poke(true.B)
      dut.io.rfWen(0).poke(true.B)
      dut.io.ldest(0).poke(2.U)
      dut.io.writeData(0).poke(BigInt("aaaa", 16).U)

      dut.io.valid(1).poke(true.B)
      dut.io.rfWen(1).poke(true.B)
      dut.io.isMove(1).poke(true.B)
      dut.io.ldest(1).poke(4.U)
      dut.io.moveSrc(1).poke(2.U)
      dut.io.writeData(1).poke(BigInt("dead", 16).U)

      dut.io.valid(2).poke(true.B)
      dut.io.rfWen(2).poke(true.B)
      dut.io.ldest(2).poke(2.U)
      dut.io.writeData(2).poke(BigInt("bbbb", 16).U)

      dut.io.commitData(0).expect(BigInt("aaaa", 16).U)
      dut.io.commitData(1).expect(BigInt("aaaa", 16).U)
      dut.io.commitData(2).expect(BigInt("bbbb", 16).U)
      dut.clock.step()

      clear()
      dut.io.shadow(0).expect(0.U)
      dut.io.shadow(2).expect(BigInt("bbbb", 16).U)
      dut.io.shadow(4).expect(BigInt("aaaa", 16).U)

      dut.io.valid(0).poke(true.B)
      dut.io.rfWen(0).poke(true.B)
      dut.io.ldest(0).poke(0.U)
      dut.io.writeData(0).poke(BigInt("ffff", 16).U)
      dut.clock.step()

      clear()
      dut.io.shadow(0).expect(0.U)
    }
  }

  it should "update direct integer diff shadow for skipped integer commits" in {
    val config = configWith(IntEarlyReleaseParams(enable = true, trackEntries = 2))

    simulate(new IntERRobDirectDiffShadowProbe()(config)) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      def clear(): Unit = {
        dut.io.load.poke(false.B)
        dut.io.loadAddr.poke(0.U)
        dut.io.loadData.poke(0.U)
        for (lane <- 0 until dut.io.valid.length) {
          dut.io.valid(lane).poke(false.B)
          dut.io.skip(lane).poke(false.B)
          dut.io.rfWen(lane).poke(false.B)
          dut.io.isMove(lane).poke(false.B)
          dut.io.ldest(lane).poke(0.U)
          dut.io.moveSrc(lane).poke(0.U)
          dut.io.writeData(lane).poke(0.U)
        }
      }

      clear()
      dut.io.valid(0).poke(true.B)
      dut.io.skip(0).poke(true.B)
      dut.io.rfWen(0).poke(true.B)
      dut.io.ldest(0).poke(7.U)
      dut.io.writeData(0).poke(BigInt("7777", 16).U)
      dut.io.instrSkip(0).expect(true.B)
      dut.io.commitData(0).expect(BigInt("7777", 16).U)
      dut.clock.step()

      clear()
      dut.io.shadow(7).expect(BigInt("7777", 16).U)
    }
  }
}
