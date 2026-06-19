package xiangshan.backend

import chisel3._
import chisel3.simulator.scalatest.ChiselSim
import org.chipsalliance.cde.config.Parameters
import org.scalatest.flatspec.AnyFlatSpec
import org.scalatest.matchers.should.Matchers
import top.DefaultConfig
import xiangshan._

class IntSparseUCATest extends AnyFlatSpec with Matchers with ChiselSim {
  behavior of "IntSparseUCA"

  private def configWith(params: IntEarlyReleaseParams): Parameters = {
    val defaultConfig = new DefaultConfig
    defaultConfig.alterPartial({
      case XSCoreParamsKey => defaultConfig(XSTileKey).head.copy(
        intEarlyRelease = params
      )
    })
  }

  private def setRobPtr(ptr: xiangshan.backend.rob.RobPtr, value: Int): Unit = {
    ptr.flag.poke(false.B)
    ptr.value.poke(value.U)
  }

  private def clearInputs(dut: IntSparseUCA): Unit = {
    dut.io.redirectKill.poke(false.B)

    for (i <- dut.io.rename.source.indices) {
      dut.io.rename.sourceFallback(i).poke(false.B)
      dut.io.rename.alloc(i).valid.poke(false.B)
      dut.io.rename.alloc(i).bits.pdest.poke(0.U)
      setRobPtr(dut.io.rename.alloc(i).bits.robIdx, 0)
      dut.io.rename.redef(i).valid.poke(false.B)
      dut.io.rename.redef(i).bits.oldPdest.poke(0.U)
      setRobPtr(dut.io.rename.redef(i).bits.robIdx, 0)
      for (s <- dut.io.rename.source(i).indices) {
        dut.io.rename.source(i)(s).valid.poke(false.B)
        dut.io.rename.source(i)(s).psrc.poke(0.U)
        dut.io.rename.source(i)(s).srcIdx.poke(s.U)
      }
    }

    for (event <- dut.io.producerReady) {
      event.valid.poke(false.B)
      event.bits.valid.poke(false.B)
      setRobPtr(event.bits.robIdx, 0)
      event.bits.pdest.poke(0.U)
    }

    for (event <- dut.io.readDone) {
      event.valid.poke(false.B)
      setRobPtr(event.bits.robIdx, 0)
      event.bits.fallback.poke(false.B)
      event.bits.reason.poke(IntERFallbackReason.none)
      for (s <- event.bits.src.indices) {
        event.bits.src(s).valid.poke(false.B)
        event.bits.src(s).trackId.poke(0.U)
        event.bits.src(s).trackGen.poke(0.U)
        event.bits.src(s).srcIdx.poke(s.U)
        event.bits.src(s).psrc.poke(0.U)
      }
    }

    for (event <- dut.io.squash) {
      event.valid.poke(false.B)
      setRobPtr(event.bits.robIdx, 0)
      for (s <- event.bits.src.indices) {
        event.bits.src(s).valid.poke(false.B)
        event.bits.src(s).trackId.poke(0.U)
        event.bits.src(s).trackGen.poke(0.U)
        event.bits.src(s).srcIdx.poke(s.U)
        event.bits.src(s).psrc.poke(0.U)
      }
    }

    for (event <- dut.io.stGuardDec) {
      event.valid.poke(false.B)
      event.bits.valid.poke(false.B)
      setRobPtr(event.bits.robIdx, 0)
      event.bits.trackId.poke(0.U)
      event.bits.trackGen.poke(0.U)
      event.bits.oldPdest.poke(0.U)
      event.bits.fallback.poke(false.B)
      event.bits.reason.poke(IntERFallbackReason.none)
    }

    for (i <- dut.io.commitNeedFree.indices) {
      dut.io.commitNeedFree(i).poke(false.B)
      dut.io.commitOldPdest(i).poke(0.U)
    }
  }

  private def resetDut(dut: IntSparseUCA): Unit = {
    clearInputs(dut)
    dut.reset.poke(true.B)
    dut.clock.step()
    dut.reset.poke(false.B)
    dut.clock.step()
    clearInputs(dut)
  }

  private def allocate(dut: IntSparseUCA, pdest: Int, robIdx: Int, lane: Int = 0): Unit = {
    dut.io.rename.alloc(lane).valid.poke(true.B)
    dut.io.rename.alloc(lane).bits.pdest.poke(pdest.U)
    setRobPtr(dut.io.rename.alloc(lane).bits.robIdx, robIdx)
  }

  private def redef(dut: IntSparseUCA, oldPdest: Int, robIdx: Int, lane: Int = 0): Unit = {
    dut.io.rename.redef(lane).valid.poke(true.B)
    dut.io.rename.redef(lane).bits.oldPdest.poke(oldPdest.U)
    setRobPtr(dut.io.rename.redef(lane).bits.robIdx, robIdx)
  }

  private def producerReady(dut: IntSparseUCA, pdest: Int, robIdx: Int, lane: Int = 0): Unit = {
    dut.io.producerReady(lane).valid.poke(true.B)
    dut.io.producerReady(lane).bits.valid.poke(true.B)
    dut.io.producerReady(lane).bits.pdest.poke(pdest.U)
    setRobPtr(dut.io.producerReady(lane).bits.robIdx, robIdx)
  }

  private def guardDec(dut: IntSparseUCA, trackId: Int = 0, gen: Int = 1, lane: Int = 0): Unit = {
    dut.io.stGuardDec(lane).valid.poke(true.B)
    dut.io.stGuardDec(lane).bits.valid.poke(true.B)
    dut.io.stGuardDec(lane).bits.trackId.poke(trackId.U)
    dut.io.stGuardDec(lane).bits.trackGen.poke(gen.U)
  }

  private def readDone(dut: IntSparseUCA, trackId: Int = 0, gen: Int = 1, srcSlot: Int = 0): Unit = {
    dut.io.readDone(0).valid.poke(true.B)
    dut.io.readDone(0).bits.src(srcSlot).valid.poke(true.B)
    dut.io.readDone(0).bits.src(srcSlot).trackId.poke(trackId.U)
    dut.io.readDone(0).bits.src(srcSlot).trackGen.poke(gen.U)
    dut.io.readDone(0).bits.src(srcSlot).srcIdx.poke(srcSlot.U)
  }

  it should "track L entries and leave extra producers untracked" in {
    val config = configWith(IntEarlyReleaseParams(trackEntries = 1))
    simulate(new IntSparseUCA()(config)) { dut =>
      resetDut(dut)

      allocate(dut, pdest = 5, robIdx = 1)
      dut.io.rename.destTrack(0).valid.expect(true.B)
      dut.io.rename.destTrack(0).trackId.expect(0.U)
      dut.io.rename.destTrack(0).trackGen.expect(1.U)
      dut.clock.step()
      clearInputs(dut)
      dut.io.debug.activeCount.expect(1.U)
      dut.io.debug.entries(0).pdest.expect(5.U)

      allocate(dut, pdest = 6, robIdx = 2)
      dut.io.rename.destTrack(0).valid.expect(false.B)
      dut.clock.step()
      clearInputs(dut)
      dut.io.debug.activeCount.expect(1.U)
      dut.io.debug.fullUntrackedCount.expect(1.U)
    }
  }

  it should "count duplicate source matches only once per uop" in {
    val config = configWith(IntEarlyReleaseParams(trackEntries = 2))
    simulate(new IntSparseUCA()(config)) { dut =>
      resetDut(dut)

      allocate(dut, pdest = 5, robIdx = 1)
      dut.clock.step()
      clearInputs(dut)

      dut.io.rename.source(0)(0).valid.poke(true.B)
      dut.io.rename.source(0)(0).psrc.poke(5.U)
      dut.io.rename.source(0)(0).srcIdx.poke(0.U)
      dut.io.rename.source(0)(1).valid.poke(true.B)
      dut.io.rename.source(0)(1).psrc.poke(5.U)
      dut.io.rename.source(0)(1).srcIdx.poke(1.U)
      dut.io.rename.srcMatch(0)(0).valid.expect(true.B)
      dut.io.rename.srcMatch(0)(1).valid.expect(false.B)
      dut.clock.step()
      clearInputs(dut)

      dut.io.debug.entries(0).userCounter.expect(2.U)
      dut.io.debug.sourceMatchCount.expect(1.U)
      dut.io.debug.sourceDuplicateCount.expect(1.U)
    }
  }

  it should "record observe-only opportunity without issuing early free" in {
    val config = configWith(IntEarlyReleaseParams(trackEntries = 2, observeOnly = true))
    simulate(new IntSparseUCA()(config)) { dut =>
      resetDut(dut)

      allocate(dut, pdest = 5, robIdx = 1)
      dut.clock.step()
      clearInputs(dut)

      redef(dut, oldPdest = 5, robIdx = 7)
      producerReady(dut, pdest = 5, robIdx = 1)
      guardDec(dut)
      dut.io.earlyFree(0).valid.expect(false.B)
      dut.clock.step()
      clearInputs(dut)

      dut.io.debug.entries(0).state.expect(IntEREntryState.counting)
      dut.io.debug.entries(0).userCounter.expect(0.U)
      dut.io.debug.entries(0).earlyFreeIssued.expect(true.B)
      dut.io.debug.earlyFreeOpportunityCount.expect(1.U)
      dut.io.debug.earlyFreeCount.expect(0.U)
    }
  }

  it should "issue early free and suppress the matching conventional free" in {
    val config = configWith(IntEarlyReleaseParams(trackEntries = 2, observeOnly = false))
    simulate(new IntSparseUCA()(config)) { dut =>
      resetDut(dut)

      allocate(dut, pdest = 5, robIdx = 1)
      dut.clock.step()
      clearInputs(dut)

      redef(dut, oldPdest = 5, robIdx = 7)
      producerReady(dut, pdest = 5, robIdx = 1)
      guardDec(dut)
      dut.io.earlyFree(0).valid.expect(true.B)
      dut.io.earlyFree(0).bits.pdest.expect(5.U)
      dut.io.earlyFree(0).bits.trackId.expect(0.U)
      dut.io.earlyFree(0).bits.trackGen.expect(1.U)
      dut.clock.step()
      clearInputs(dut)

      dut.io.debug.entries(0).state.expect(IntEREntryState.releasedWaitCommit)
      dut.io.rename.source(0)(0).valid.poke(true.B)
      dut.io.rename.source(0)(0).psrc.poke(5.U)
      dut.io.rename.srcMatch(0)(0).valid.expect(false.B)
      clearInputs(dut)

      dut.io.commitNeedFree(0).poke(true.B)
      dut.io.commitOldPdest(0).poke(5.U)
      dut.io.commitSuppress(0).suppress.expect(true.B)
      dut.io.commitSuppress(0).trackId.expect(0.U)
      dut.io.commitSuppress(0).trackGen.expect(1.U)
      dut.clock.step()
      clearInputs(dut)
      dut.io.debug.activeCount.expect(0.U)
      dut.io.debug.commitSuppressCount.expect(1.U)
    }
  }

  it should "fallback on counter saturation and ignore generation-mismatched events" in {
    val config = configWith(IntEarlyReleaseParams(trackEntries = 2, counterBits = 3))
    simulate(new IntSparseUCA()(config)) { dut =>
      resetDut(dut)

      allocate(dut, pdest = 5, robIdx = 1)
      dut.clock.step()
      clearInputs(dut)

      for (i <- dut.io.rename.source.indices) {
        dut.io.rename.source(i)(0).valid.poke(true.B)
        dut.io.rename.source(i)(0).psrc.poke(5.U)
        dut.io.rename.source(i)(0).srcIdx.poke(0.U)
      }
      dut.clock.step()
      clearInputs(dut)
      dut.io.debug.entries(0).state.expect(IntEREntryState.fallbackWaitCommit)
      dut.io.debug.entries(0).fallback.expect(true.B)
      dut.io.debug.fallbackCount.expect(1.U)

      readDone(dut, trackId = 0, gen = 2)
      dut.clock.step()
      clearInputs(dut)
      dut.io.debug.entries(0).state.expect(IntEREntryState.fallbackWaitCommit)
      dut.io.debug.genMismatchCount.expect(1.U)
    }
  }

  it should "kill counting entries on redirect and keep released entries until suppress" in {
    val countingConfig = configWith(IntEarlyReleaseParams(trackEntries = 2))
    simulate(new IntSparseUCA()(countingConfig)) { dut =>
      resetDut(dut)

      allocate(dut, pdest = 5, robIdx = 1)
      dut.clock.step()
      clearInputs(dut)
      dut.io.redirectKill.poke(true.B)
      dut.clock.step()
      clearInputs(dut)
      dut.io.debug.activeCount.expect(0.U)
      dut.io.debug.redirectKillCount.expect(1.U)
    }

    val releaseConfig = configWith(IntEarlyReleaseParams(trackEntries = 2, observeOnly = false))
    simulate(new IntSparseUCA()(releaseConfig)) { dut =>
      resetDut(dut)

      allocate(dut, pdest = 5, robIdx = 1)
      dut.clock.step()
      clearInputs(dut)
      redef(dut, oldPdest = 5, robIdx = 7)
      producerReady(dut, pdest = 5, robIdx = 1)
      guardDec(dut)
      dut.clock.step()
      clearInputs(dut)

      dut.io.redirectKill.poke(true.B)
      dut.clock.step()
      clearInputs(dut)
      dut.io.debug.activeCount.expect(1.U)
      dut.io.debug.entries(0).state.expect(IntEREntryState.releasedWaitCommit)
    }
  }

  it should "fail fast on invalid counter and destination conditions" in {
    val config = configWith(IntEarlyReleaseParams(trackEntries = 2, observeOnly = false))

    assertThrows[Exception] {
      simulate(new IntSparseUCA()(config)) { dut =>
        resetDut(dut)

        allocate(dut, pdest = 5, robIdx = 1)
        dut.clock.step()
        clearInputs(dut)

        readDone(dut)
        guardDec(dut)
        dut.clock.step()
      }
    }

    assertThrows[Exception] {
      simulate(new IntSparseUCA()(config)) { dut =>
        resetDut(dut)

        allocate(dut, pdest = 0, robIdx = 1)
        dut.clock.step()
        clearInputs(dut)
        dut.clock.step()
      }
    }
  }
}
