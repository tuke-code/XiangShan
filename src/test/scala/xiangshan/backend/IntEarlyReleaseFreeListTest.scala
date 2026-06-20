package xiangshan.backend

import chisel3._
import chisel3.simulator.scalatest.ChiselSim
import chisel3.util.log2Ceil
import org.chipsalliance.cde.config.Parameters
import org.scalatest.flatspec.AnyFlatSpec
import org.scalatest.matchers.should.Matchers
import top.DefaultConfig
import utility.{LogUtilsOptions, LogUtilsOptionsKey, PerfCounterOptions, PerfCounterOptionsKey, XSPerfLevel}
import xiangshan._
import xiangshan.backend.Bundles.EnqRobUop
import xiangshan.backend.rename.freelist.MEFreeList
import xiangshan.backend.rob.RenameBuffer
import xiangshan.backend.regfile.IntPregParams

class IntERMEFreeListProbe(size: Int)(implicit p: Parameters) extends XSModule {
  val io = IO(new Bundle {
    val redirect = Input(Bool())
    val walk = Input(Bool())
    val doAllocate = Input(Bool())
    val allocateReq = Input(Vec(RenameWidth, Bool()))
    val walkReq = Input(Vec(RabCommitWidth, Bool()))
    val freeReq = Input(Vec(RabCommitWidth, Bool()))
    val freePhyReg = Input(Vec(RabCommitWidth, UInt(PhyRegIdxWidth.W)))
    val earlyFreeReq = Input(Vec(IntEREarlyFreeWidth, Bool()))
    val earlyFreePhyReg = Input(Vec(IntEREarlyFreeWidth, UInt(PhyRegIdxWidth.W)))
    val canAllocate = Output(Bool())
    val allocatePhyReg = Output(Vec(RenameWidth, UInt(PhyRegIdxWidth.W)))
  })

  val freeList = Module(new MEFreeList(size, RabCommitWidth))

  freeList.io.redirect := io.redirect
  freeList.io.walk := io.walk
  freeList.io.doAllocate := io.doAllocate
  freeList.io.allocateReq := io.allocateReq
  freeList.io.walkReq := io.walkReq
  freeList.io.freeReq := io.freeReq
  freeList.io.freePhyReg := io.freePhyReg
  freeList.io.commit.doCommit := false.B
  freeList.io.commit.archAlloc := 0.U.asTypeOf(freeList.io.commit.archAlloc)
  freeList.io.snpt := 0.U.asTypeOf(freeList.io.snpt)
  freeList.io.debug_rat.foreach(_ := 0.U.asTypeOf(freeList.io.debug_rat.get))

  val earlyReq = freeList.io.elements.get("earlyFreeReq")
  val earlyPhyReg = freeList.io.elements.get("earlyFreePhyReg")
  require(
    earlyReq.isDefined && earlyPhyReg.isDefined,
    "integer MEFreeList must expose early-free lanes when Int ER is enabled"
  )
  earlyReq.get.asInstanceOf[Vec[Bool]] := io.earlyFreeReq
  earlyPhyReg.get.asInstanceOf[Vec[UInt]] := io.earlyFreePhyReg

  io.canAllocate := freeList.io.canAllocate
  io.allocatePhyReg := freeList.io.allocatePhyReg
}

class IntERRabCommitProbe(implicit p: Parameters) extends XSModule {
  val io = IO(new Bundle {
    val enqValid = Input(Bool())
    val redefValid = Input(Bool())
    val trackId = Input(UInt(IntERTrackIdWidth.W))
    val trackGen = Input(UInt(IntERTrackGenBits.W))
    val oldPdest = Input(UInt(PhyRegIdxWidth.W))
    val redefRobIdxValue = Input(UInt(log2Ceil(RobSize).W))

    val commitValid = Output(Bool())
    val commitRedefValid = Output(Bool())
    val commitTrackId = Output(UInt(IntERTrackIdWidth.W))
    val commitTrackGen = Output(UInt(IntERTrackGenBits.W))
    val commitOldPdest = Output(UInt(PhyRegIdxWidth.W))
    val commitRedefRobIdxValue = Output(UInt(log2Ceil(RobSize).W))
  })

  val rab = Module(new RenameBuffer(RabSize))

  rab.io.redirect.valid := false.B
  rab.io.redirect.bits := 0.U.asTypeOf(rab.io.redirect.bits)
  rab.io.fromRob.walkSize := 0.U
  rab.io.fromRob.walkEnd := true.B
  rab.io.fromRob.commitSize := io.enqValid.asUInt
  rab.io.fromRob.vecLoadExcp.valid := false.B
  rab.io.fromRob.vecLoadExcp.bits := 0.U.asTypeOf(rab.io.fromRob.vecLoadExcp.bits)
  rab.io.snpt := 0.U.asTypeOf(rab.io.snpt)

  for (i <- 0 until RenameWidth) {
    rab.io.req(i).valid := io.enqValid && i.U === 0.U
    rab.io.req(i).bits := 0.U.asTypeOf(new EnqRobUop)
    rab.io.req(i).bits.ldest := 5.U
    rab.io.req(i).bits.pdest := 21.U
    rab.io.req(i).bits.rfWen := true.B
    rab.io.req(i).bits.firstUop := true.B
    rab.io.req(i).bits.lastUop := true.B
    rab.io.req(i).bits.intER.get.redef.valid := io.redefValid
    rab.io.req(i).bits.intER.get.redef.trackId := io.trackId
    rab.io.req(i).bits.intER.get.redef.trackGen := io.trackGen
    rab.io.req(i).bits.intER.get.redef.oldPdest := io.oldPdest
    rab.io.req(i).bits.robIdx.flag := false.B
    rab.io.req(i).bits.robIdx.value := io.redefRobIdxValue
  }

  io.commitValid := rab.io.commits.commitValid(0)
  io.commitRedefValid := rab.io.commits.info(0).intERCommitRedef.get.valid
  io.commitTrackId := rab.io.commits.info(0).intERCommitRedef.get.bits.trackId
  io.commitTrackGen := rab.io.commits.info(0).intERCommitRedef.get.bits.trackGen
  io.commitOldPdest := rab.io.commits.info(0).intERCommitRedef.get.bits.oldPdest
  io.commitRedefRobIdxValue := rab.io.commits.info(0).intERCommitRedef.get.bits.redefinerRobIdx.value
}

class IntEarlyReleaseFreeListTest extends AnyFlatSpec with Matchers with ChiselSim {
  behavior of "Int ER integer free list integration"

  private def configWith(params: IntEarlyReleaseParams): Parameters = {
    val defaultConfig = new DefaultConfig
    defaultConfig.alterPartial({
      case XSCoreParamsKey => defaultConfig(XSTileKey).head.copy(
        DecodeWidth = 1,
        RenameWidth = 1,
        CommitWidth = 1,
        RobCommitWidth = 1,
        RabCommitWidth = 1,
        RobSize = 16,
        RabSize = 16,
        RenameSnapshotNum = 1,
        intPreg = IntPregParams(numEntries = 64, numBank = 1, numRead = None, numWrite = None),
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

  private def clearInputs(dut: IntERMEFreeListProbe): Unit = {
    dut.io.redirect.poke(false.B)
    dut.io.walk.poke(false.B)
    dut.io.doAllocate.poke(false.B)
    for (i <- dut.io.allocateReq.indices) {
      dut.io.allocateReq(i).poke(false.B)
    }
    for (i <- dut.io.walkReq.indices) {
      dut.io.walkReq(i).poke(false.B)
      dut.io.freeReq(i).poke(false.B)
      dut.io.freePhyReg(i).poke(0.U)
    }
    for (i <- dut.io.earlyFreeReq.indices) {
      dut.io.earlyFreeReq(i).poke(false.B)
      dut.io.earlyFreePhyReg(i).poke(0.U)
    }
  }

  private def resetDut(dut: IntERMEFreeListProbe): Unit = {
    clearInputs(dut)
    dut.reset.poke(true.B)
    dut.clock.step(2)
    dut.reset.poke(false.B)
    dut.clock.step(2)
    clearInputs(dut)
  }

  it should "merge early-free lanes into later integer allocations" in {
    val config = configWith(IntEarlyReleaseParams(enable = true, observeOnly = false, trackEntries = 2))

    simulate(new IntERMEFreeListProbe(size = 8)(config)) { dut =>
      resetDut(dut)

      dut.io.earlyFreeReq(0).poke(true.B)
      dut.io.earlyFreePhyReg(0).poke(55.U)
      dut.clock.step()
      clearInputs(dut)

      var sawEarlyFreedReg = false
      for (_ <- 0 until 12) {
        dut.io.allocateReq(0).poke(true.B)
        dut.io.doAllocate.poke(true.B)
        val allocated = dut.io.allocatePhyReg(0).peek().litValue
        sawEarlyFreedReg ||= dut.io.canAllocate.peek().litToBoolean && allocated == BigInt(55)
        dut.clock.step()
        clearInputs(dut)
      }

      sawEarlyFreedReg shouldBe true
    }
  }

  it should "fail fast on same-cycle conventional and early release of one integer preg" in {
    val config = configWith(IntEarlyReleaseParams(enable = true, observeOnly = false, trackEntries = 2))

    assertThrows[Exception] {
      simulate(new IntERMEFreeListProbe(size = 8)(config)) { dut =>
        resetDut(dut)

        dut.io.freeReq(0).poke(true.B)
        dut.io.freePhyReg(0).poke(7.U)
        dut.io.earlyFreeReq(0).poke(true.B)
        dut.io.earlyFreePhyReg(0).poke(7.U)
        dut.clock.step()
      }
    }
  }

  it should "preserve exact commit suppress identity metadata through RAB commit info" in {
    val config = configWith(IntEarlyReleaseParams(enable = true, observeOnly = false, trackEntries = 2))

    simulate(new IntERRabCommitProbe()(config)) { dut =>
      dut.io.enqValid.poke(false.B)
      dut.io.redefValid.poke(false.B)
      dut.io.trackId.poke(0.U)
      dut.io.trackGen.poke(0.U)
      dut.io.oldPdest.poke(0.U)
      dut.io.redefRobIdxValue.poke(0.U)
      dut.reset.poke(true.B)
      dut.clock.step(2)
      dut.reset.poke(false.B)
      dut.clock.step()

      dut.io.enqValid.poke(true.B)
      dut.io.redefValid.poke(true.B)
      dut.io.trackId.poke(1.U)
      dut.io.trackGen.poke(3.U)
      dut.io.oldPdest.poke(17.U)
      dut.io.redefRobIdxValue.poke(6.U)
      dut.clock.step()

      dut.io.enqValid.poke(false.B)
      dut.io.commitValid.expect(true.B)
      dut.io.commitRedefValid.expect(true.B)
      dut.io.commitTrackId.expect(1.U)
      dut.io.commitTrackGen.expect(3.U)
      dut.io.commitOldPdest.expect(17.U)
      dut.io.commitRedefRobIdxValue.expect(6.U)
    }
  }
}
