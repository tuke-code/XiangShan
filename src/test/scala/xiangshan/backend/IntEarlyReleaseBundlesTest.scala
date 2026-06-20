package xiangshan.backend

import chisel3._
import chisel3.simulator.scalatest.ChiselSim
import chisel3.util.log2Ceil
import circt.stage.ChiselStage
import org.chipsalliance.cde.config.Parameters
import org.scalatest.flatspec.AnyFlatSpec
import org.scalatest.matchers.should.Matchers
import top.DefaultConfig
import utility.{LogUtilsOptions, LogUtilsOptionsKey}
import xiangshan._
import xiangshan.backend.Bundles.RenameOutUop
import xiangshan.backend.decode.DecodeStageIO
import xiangshan.backend.rename.{RatReadPort, Reg_I, RenameIntEROps, RenameTable, RenameTableWrapper}

class IntEarlyReleaseBundleProbe(
  localSrc: Int,
  expectedTrackIdWidth: Int,
  expectedLogicalSrcWidth: Int,
  expectedRenameWidth: Int,
  expectedCommitWidth: Int,
  expectedIntPhyRegs: Int,
  expectedRobSize: Int
)(implicit p: Parameters) extends XSModule {
  require(localSrc > 0, "localSrc must be positive")

  val logical = Wire(IntERBundleHelper.logicalSrcVec)
  val local = Wire(IntERBundleHelper.localSrcVec(localSrc))
  val robSrc = Wire(IntERBundleHelper.logicalRobSrcVec)
  val readDone = Wire(new IntERSrcValueReadDone)
  val squash = Wire(new IntERSquashSource)
  val stGuard = Wire(new IntERSTGuardDec)
  val earlyFree = Wire(new IntEREarlyFreeReq)
  val suppress = Wire(new IntERCommitSuppress)
  val src = Wire(new IntERSrcTrack)

  logical := 0.U.asTypeOf(logical)
  local := 0.U.asTypeOf(local)
  robSrc := 0.U.asTypeOf(robSrc)
  readDone := 0.U.asTypeOf(readDone)
  squash := 0.U.asTypeOf(squash)
  stGuard := 0.U.asTypeOf(stGuard)
  earlyFree := 0.U.asTypeOf(earlyFree)
  suppress := 0.U.asTypeOf(suppress)
  src := 0.U.asTypeOf(src)

  require(!EnableIntEarlyRegRelease, "default round-0 Int ER config must stay disabled")
  require(IntERObserveOnly, "round-0 Int ER defaults to observe-only when later enabled")
  require(IntERTrackIdWidth == expectedTrackIdWidth, "track id width must follow trackEntries")
  require(logical.length == expectedLogicalSrcWidth, "logical helper must use full backend source width")
  require(local.length == localSrc, "local helper must use caller-provided local source width")
  require(src.srcIdx.getWidth == IntERBundleHelper.sourceIndexWidth(expectedLogicalSrcWidth), "srcIdx width must preserve original logical source slot")
  require(IntERFallbackReason.width == 4, "fallback reason encoding width changed unexpectedly")
  require(RenameWidth == expectedRenameWidth, "IntER trackEntries must not change RenameWidth")
  require(CommitWidth == expectedCommitWidth, "IntER trackEntries must not change CommitWidth")
  require(IntPhyRegs == expectedIntPhyRegs, "IntER trackEntries must not change integer physical register count")
  require(RobSize == expectedRobSize, "IntER trackEntries must not change ROB size")
}

class DecodeOldDestRatPortShapeProbe(
  expectedOldDestPort: Boolean
)(implicit p: Parameters) extends XSModule {
  val decode = IO(new DecodeStageIO)

  require(decode.intRat.length == RenameWidth, "decode int RAT lane count must follow RenameWidth")
  require(decode.intRat.head.length == backendParams.numIntRegSrc, "decode int RAT source ports must follow backend topology")

  val oldDestPort = decode.elements.get("intOldDestRat")
  require(oldDestPort.isDefined == expectedOldDestPort, "decode old-dest RAT port presence must follow Int ER enable")
  oldDestPort.foreach { port =>
    require(port.asInstanceOf[Vec[RatReadPort]].length == RenameWidth, "decode old-dest RAT port must have one entry per rename lane")
  }
}

class IntRatReadPortCountProbe(
  expectedPortsPerLane: Int
)(implicit p: Parameters) extends RenameTable(
  Reg_I,
  p(XSCoreParamsKey).RabCommitWidth * p(XSCoreParamsKey).MaxUopSize
) {
  require(
    io.readPorts.length == RenameWidth * expectedPortsPerLane,
    "integer RAT read-port count must follow source plus old-dest topology"
  )
}

class RenameTableWrapperOldDestPortShapeProbe(
  expectedOldDestPort: Boolean
)(implicit p: Parameters) extends RenameTableWrapper {
  require(io.intReadPorts.length == RenameWidth, "wrapper int RAT lane count must follow RenameWidth")
  require(io.intReadPorts.head.length == backendParams.numIntRegSrc, "wrapper int RAT source ports must follow backend topology")

  val oldDestPort = io.elements.get("intOldDestReadPorts")
  require(oldDestPort.isDefined == expectedOldDestPort, "wrapper old-dest RAT port presence must follow Int ER enable")
  oldDestPort.foreach { port =>
    require(port.asInstanceOf[Vec[RatReadPort]].length == RenameWidth, "wrapper old-dest RAT port must have one entry per rename lane")
  }
}

class RenameOutUopERMetaShapeProbe(
  expectedERMeta: Boolean
)(implicit p: Parameters) extends XSModule {
  val uop = Wire(new RenameOutUop)
  uop := 0.U.asTypeOf(uop)

  val erMeta = uop.elements.get("intER")
  require(erMeta.isDefined == expectedERMeta, "rename output ER metadata presence must follow Int ER enable")
  erMeta.foreach { meta =>
    val typed = meta.asInstanceOf[IntERUopMeta]
    require(typed.src.length == backendParams.numSrc, "rename output ER metadata must use full logical source width")
  }
}

class RenameOldDestBypassProbe(implicit p: Parameters) extends XSModule {
  val io = IO(new Bundle {
    val base = Input(Vec(RenameWidth, UInt(PhyRegIdxWidth.W)))
    val ldest = Input(Vec(RenameWidth, UInt(log2Ceil(IntLogicRegs).W)))
    val wen = Input(Vec(RenameWidth, Bool()))
    val data = Input(Vec(RenameWidth, UInt(PhyRegIdxWidth.W)))
    val out = Output(Vec(RenameWidth, UInt(PhyRegIdxWidth.W)))
  })

  io.out := RenameIntEROps.sameGroupOldDest(io.base, io.ldest, io.wen, io.data)
}

class IntEarlyReleaseBundlesTest extends AnyFlatSpec with Matchers with ChiselSim {
  behavior of "IntEarlyReleaseParams and IntEarlyReleaseBundles"

  private def configWith(params: IntEarlyReleaseParams): Parameters = {
    val defaultConfig = new DefaultConfig
    defaultConfig.alterPartial({
      case XSCoreParamsKey => defaultConfig(XSTileKey).head.copy(
        intEarlyRelease = params
      )
    }).alter((site, here, up) => {
      case LogUtilsOptionsKey => LogUtilsOptions(
        here(DebugOptionsKey).EnableDebug,
        here(DebugOptionsKey).EnablePerfDebug,
        here(DebugOptionsKey).FPGAPlatform,
        here(DebugOptionsKey).EnableXMR
      )
    })
  }

  private def elaborateProbe(params: IntEarlyReleaseParams, localSrc: Int, expectedTrackIdWidth: Int): Unit = {
    val config = configWith(params)
    val coreParams = config(XSCoreParamsKey)
    ChiselStage.elaborate(
      new IntEarlyReleaseBundleProbe(
        localSrc = localSrc,
        expectedTrackIdWidth = expectedTrackIdWidth,
        expectedLogicalSrcWidth = coreParams.backendParams.numSrc,
        expectedRenameWidth = coreParams.RenameWidth,
        expectedCommitWidth = coreParams.CommitWidth,
        expectedIntPhyRegs = coreParams.intPreg.numEntries,
        expectedRobSize = coreParams.RobSize
      )(config)
    )
  }

  private def elaborateOldDestRatProbe(
    params: IntEarlyReleaseParams,
    expectedOldDestPort: Boolean,
    expectedPortsPerLane: Int
  ): Unit = {
    val config = configWith(params)
    ChiselStage.elaborate(new DecodeOldDestRatPortShapeProbe(expectedOldDestPort)(config))
    ChiselStage.elaborate(new IntRatReadPortCountProbe(expectedPortsPerLane)(config))
    ChiselStage.elaborate(new RenameTableWrapperOldDestPortShapeProbe(expectedOldDestPort)(config))
  }

  private def elaborateRenameOutUopERMetaProbe(params: IntEarlyReleaseParams, expectedERMeta: Boolean): Unit = {
    ChiselStage.elaborate(new RenameOutUopERMetaShapeProbe(expectedERMeta)(configWith(params)))
  }

  it should "reject illegal trackEntries values" in {
    assertThrows[IllegalArgumentException] {
      IntEarlyReleaseParams(trackEntries = 0)
    }
    assertThrows[IllegalArgumentException] {
      IntEarlyReleaseParams(trackEntries = 3)
    }
  }

  it should "elaborate legal parameterized trackEntries values" in {
    Seq(
      IntEarlyReleaseParams(trackEntries = 1) -> 1,
      IntEarlyReleaseParams(trackEntries = 2) -> 1,
      IntEarlyReleaseParams(trackEntries = 16) -> 4
    ).foreach { case (params, expectedTrackIdWidth) =>
      elaborateProbe(params, localSrc = 1, expectedTrackIdWidth)
    }
  }

  it should "distinguish full logical and local source vector widths" in {
    val params = IntEarlyReleaseParams(trackEntries = 2)

    Seq(1, 2).foreach { localSrc =>
      elaborateProbe(params, localSrc, expectedTrackIdWidth = 1)
    }
  }

  it should "keep default feature config disabled" in {
    elaborateProbe(IntEarlyReleaseParams(), localSrc = 1, expectedTrackIdWidth = 4)
  }

  it should "gate old-destination integer RAT read ports with feature enable" in {
    elaborateOldDestRatProbe(
      IntEarlyReleaseParams(),
      expectedOldDestPort = false,
      expectedPortsPerLane = 2
    )
    elaborateOldDestRatProbe(
      IntEarlyReleaseParams(enable = true, trackEntries = 2),
      expectedOldDestPort = true,
      expectedPortsPerLane = 3
    )
  }

  it should "gate rename output ER metadata with feature enable" in {
    elaborateRenameOutUopERMetaProbe(IntEarlyReleaseParams(), expectedERMeta = false)
    elaborateRenameOutUopERMetaProbe(IntEarlyReleaseParams(enable = true, trackEntries = 2), expectedERMeta = true)
  }

  it should "select same-group old destination from older final integer RAT writes" in {
    val config = configWith(IntEarlyReleaseParams(enable = true, trackEntries = 2))

    simulate(new RenameOldDestBypassProbe()(config)) { dut =>
      for (i <- 0 until dut.io.base.length) {
        dut.io.base(i).poke((10 + i).U)
        dut.io.ldest(i).poke((i + 1).U)
        dut.io.wen(i).poke(false.B)
        dut.io.data(i).poke((40 + i).U)
      }

      dut.io.ldest(0).poke(5.U)
      dut.io.wen(0).poke(true.B)
      dut.io.data(0).poke(21.U)
      dut.io.ldest(1).poke(6.U)
      dut.io.wen(1).poke(true.B)
      dut.io.data(1).poke(31.U)
      dut.io.ldest(2).poke(5.U)
      dut.io.wen(2).poke(true.B)
      dut.io.data(2).poke(41.U)
      dut.io.ldest(3).poke(5.U)

      dut.io.out(0).expect(10.U)
      dut.io.out(1).expect(11.U)
      dut.io.out(2).expect(21.U)
      dut.io.out(3).expect(41.U)
    }
  }
}
