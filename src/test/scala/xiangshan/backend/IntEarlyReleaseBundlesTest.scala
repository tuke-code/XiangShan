package xiangshan.backend

import chisel3._
import circt.stage.ChiselStage
import org.chipsalliance.cde.config.Parameters
import org.scalatest.flatspec.AnyFlatSpec
import org.scalatest.matchers.should.Matchers
import top.DefaultConfig
import xiangshan._

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

class IntEarlyReleaseBundlesTest extends AnyFlatSpec with Matchers {
  behavior of "IntEarlyReleaseParams and IntEarlyReleaseBundles"

  private def configWith(params: IntEarlyReleaseParams): Parameters = {
    val defaultConfig = new DefaultConfig
    defaultConfig.alterPartial({
      case XSCoreParamsKey => defaultConfig(XSTileKey).head.copy(
        intEarlyRelease = params
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
}
