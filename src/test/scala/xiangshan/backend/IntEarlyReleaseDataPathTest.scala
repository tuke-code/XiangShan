package xiangshan.backend

import chisel3._
import chisel3.simulator.scalatest.ChiselSim
import chisel3.util.{Valid, ValidIO}
import circt.stage.ChiselStage
import org.chipsalliance.cde.config.Parameters
import org.scalatest.flatspec.AnyFlatSpec
import org.scalatest.matchers.should.Matchers
import top.DefaultConfig
import xiangshan._
import xiangshan.backend.datapath.{DataPathIO, DataPathIntEROps, DataSource}
import xiangshan.backend.issue.{IntScheduler, SchdBlockParams}

class IntERDataPathIOShapeProbe(expectedER: Boolean)(implicit p: Parameters) extends XSModule {
  private implicit val dataPathBackendParams: BackendParams = p(XSCoreParamsKey).backendParams
  for ((exu, idx) <- dataPathBackendParams.allExuParams.zipWithIndex) {
    exu.bindBackendParam(dataPathBackendParams)
    exu.updateIQWakeUpConfigs(dataPathBackendParams.iqWakeUpParams)
    exu.updateExuIdx(idx)
  }
  dataPathBackendParams.allSchdParams.foreach(_.bindBackendParam(dataPathBackendParams))
  dataPathBackendParams.allIssueParams.foreach { issue =>
    issue.bindBackendParam(dataPathBackendParams)
    issue.allExuParams.foreach(_.bindBackendParam(dataPathBackendParams))
    issue.exuBlockParams.foreach(_.bindIssueBlockParam(issue))
  }
  private implicit val dataPathSchdParams: SchdBlockParams = dataPathBackendParams.schdParams(IntScheduler())

  val bundle = Wire(new DataPathIO()(p, dataPathBackendParams, dataPathSchdParams))
  bundle := DontCare

  val readDone = bundle.elements.get("intERReadDone")
  require(readDone.isDefined == expectedER, "DataPath read observation IO presence must follow Int ER enable")
  readDone.foreach { value =>
    require(
      value.asInstanceOf[Vec[ValidIO[IntERSrcValueReadDone]]].length == IntERReadDoneWidth,
      "DataPath read observation IO width must follow backend issue dequeue topology"
    )
  }
}

class IntERDataPathReadDoneProbe(localSrc: Int, replayProne: Boolean)(implicit p: Parameters) extends XSModule {
  require(localSrc > 0, "read observation probe needs at least one source")

  val io = IO(new Bundle {
    val s1Valid = Input(Bool())
    val og1Failed = Input(Bool())
    val uncertainReadPath = Input(Bool())
    val robIdx = Input(new rob.RobPtr)
    val srcValid = Input(Vec(localSrc, Bool()))
    val srcTrackId = Input(Vec(localSrc, UInt(IntERTrackIdWidth.W)))
    val srcTrackGen = Input(Vec(localSrc, UInt(IntERTrackGenBits.W)))
    val srcIdx = Input(Vec(localSrc, UInt(IntERSrcIdxWidth.W)))
    val psrc = Input(Vec(localSrc, UInt(PhyRegIdxWidth.W)))
    val dataSources = Input(Vec(localSrc, DataSource()))
    val out = Output(Valid(new IntERSrcValueReadDone))
  })

  val shadow = Wire(Vec(localSrc, new IntERSrcTrack))
  for (src <- 0 until localSrc) {
    shadow(src).valid := io.srcValid(src)
    shadow(src).trackId := io.srcTrackId(src)
    shadow(src).trackGen := io.srcTrackGen(src)
    shadow(src).srcIdx := io.srcIdx(src)
    shadow(src).psrc := io.psrc(src)
  }

  DataPathIntEROps.emitReadDoneObservation(
    out = io.out,
    robIdx = io.robIdx,
    srcShadow = shadow,
    dataSources = io.dataSources,
    s1Valid = io.s1Valid,
    og1Failed = io.og1Failed,
    replayPronePath = replayProne.B,
    uncertainReadPath = io.uncertainReadPath
  )
}

class IntEarlyReleaseDataPathTest extends AnyFlatSpec with Matchers with ChiselSim {
  behavior of "Int early-release DataPath observation"

  private def configWith(params: IntEarlyReleaseParams): Parameters = {
    val defaultConfig = new DefaultConfig
    defaultConfig.alterPartial({
      case XSCoreParamsKey => defaultConfig(XSTileKey).head.copy(
        intEarlyRelease = params
      )
    })
  }

  private def setRobPtr(ptr: rob.RobPtr, value: Int): Unit = {
    ptr.flag.poke(false.B)
    ptr.value.poke(value.U)
  }

  private def clearProbe(dut: IntERDataPathReadDoneProbe): Unit = {
    dut.io.s1Valid.poke(false.B)
    dut.io.og1Failed.poke(false.B)
    dut.io.uncertainReadPath.poke(false.B)
    setRobPtr(dut.io.robIdx, 0)
    for (src <- dut.io.srcValid.indices) {
      dut.io.srcValid(src).poke(false.B)
      dut.io.srcTrackId(src).poke(0.U)
      dut.io.srcTrackGen(src).poke(0.U)
      dut.io.srcIdx(src).poke(src.U)
      dut.io.psrc(src).poke(0.U)
      dut.io.dataSources(src).value.poke(DataSource.zero)
    }
  }

  private def setTrackedSource(
    dut: IntERDataPathReadDoneProbe,
    localSrc: Int,
    logicalSrc: Int,
    trackId: Int,
    trackGen: Int,
    psrc: Int,
    dataSource: UInt
  ): Unit = {
    dut.io.srcValid(localSrc).poke(true.B)
    dut.io.srcTrackId(localSrc).poke(trackId.U)
    dut.io.srcTrackGen(localSrc).poke(trackGen.U)
    dut.io.srcIdx(localSrc).poke(logicalSrc.U)
    dut.io.psrc(localSrc).poke(psrc.U)
    dut.io.dataSources(localSrc).value.poke(dataSource)
  }

  it should "gate DataPath read observation IO with Int ER enable" in {
    ChiselStage.elaborate(new IntERDataPathIOShapeProbe(expectedER = false)(configWith(IntEarlyReleaseParams())))
    ChiselStage.elaborate(new IntERDataPathIOShapeProbe(expectedER = true)(configWith(IntEarlyReleaseParams(enable = true, trackEntries = 2))))
  }

  it should "emit readDone for supported register and regcache reads after final success" in {
    simulate(new IntERDataPathReadDoneProbe(localSrc = 2, replayProne = false)(configWith(IntEarlyReleaseParams(enable = true, trackEntries = 2)))) { dut =>
      clearProbe(dut)
      dut.io.s1Valid.poke(true.B)
      setRobPtr(dut.io.robIdx, 7)
      setTrackedSource(dut, localSrc = 0, logicalSrc = 1, trackId = 1, trackGen = 3, psrc = 21, dataSource = DataSource.reg)
      dut.io.out.valid.expect(true.B)
      dut.io.out.bits.fallback.expect(false.B)
      dut.io.out.bits.reason.expect(IntERFallbackReason.none)
      dut.io.out.bits.robIdx.value.expect(7.U)
      dut.io.out.bits.src(0).valid.expect(false.B)
      dut.io.out.bits.src(1).valid.expect(true.B)
      dut.io.out.bits.src(1).trackId.expect(1.U)
      dut.io.out.bits.src(1).trackGen.expect(3.U)
      dut.io.out.bits.src(1).srcIdx.expect(1.U)
      dut.io.out.bits.src(1).psrc.expect(21.U)

      clearProbe(dut)
      dut.io.s1Valid.poke(true.B)
      setRobPtr(dut.io.robIdx, 8)
      setTrackedSource(dut, localSrc = 1, logicalSrc = 0, trackId = 0, trackGen = 4, psrc = 22, dataSource = DataSource.regcache)
      dut.io.out.valid.expect(true.B)
      dut.io.out.bits.fallback.expect(false.B)
      dut.io.out.bits.robIdx.value.expect(8.U)
      dut.io.out.bits.src(0).valid.expect(true.B)
      dut.io.out.bits.src(0).trackGen.expect(4.U)
      dut.io.out.bits.src(0).psrc.expect(22.U)
      dut.io.out.bits.src(1).valid.expect(false.B)
    }
  }

  it should "suppress observations for cancelled admissions and og1 failures" in {
    simulate(new IntERDataPathReadDoneProbe(localSrc = 1, replayProne = false)(configWith(IntEarlyReleaseParams(enable = true, trackEntries = 2)))) { dut =>
      clearProbe(dut)
      setTrackedSource(dut, localSrc = 0, logicalSrc = 0, trackId = 1, trackGen = 3, psrc = 21, dataSource = DataSource.reg)
      dut.io.s1Valid.poke(false.B)
      dut.io.og1Failed.poke(false.B)
      dut.io.out.valid.expect(false.B)

      dut.io.s1Valid.poke(true.B)
      dut.io.og1Failed.poke(true.B)
      dut.io.out.valid.expect(false.B)
    }
  }

  it should "emit keyed fallback for unsupported read paths and replay-prone paths" in {
    simulate(new IntERDataPathReadDoneProbe(localSrc = 2, replayProne = false)(configWith(IntEarlyReleaseParams(enable = true, trackEntries = 2)))) { dut =>
      clearProbe(dut)
      dut.io.s1Valid.poke(true.B)
      setRobPtr(dut.io.robIdx, 9)
      setTrackedSource(dut, localSrc = 0, logicalSrc = 0, trackId = 1, trackGen = 3, psrc = 21, dataSource = DataSource.forward)
      setTrackedSource(dut, localSrc = 1, logicalSrc = 1, trackId = 0, trackGen = 4, psrc = 22, dataSource = DataSource.reg)
      dut.io.out.valid.expect(true.B)
      dut.io.out.bits.fallback.expect(true.B)
      dut.io.out.bits.reason.expect(IntERFallbackReason.unsupportedReadPath)
      dut.io.out.bits.robIdx.value.expect(9.U)
      dut.io.out.bits.src(0).valid.expect(true.B)
      dut.io.out.bits.src(0).trackId.expect(1.U)
      dut.io.out.bits.src(1).valid.expect(true.B)
      dut.io.out.bits.src(1).trackGen.expect(4.U)
    }

    simulate(new IntERDataPathReadDoneProbe(localSrc = 1, replayProne = true)(configWith(IntEarlyReleaseParams(enable = true, trackEntries = 2)))) { dut =>
      clearProbe(dut)
      dut.io.s1Valid.poke(true.B)
      setRobPtr(dut.io.robIdx, 10)
      setTrackedSource(dut, localSrc = 0, logicalSrc = 0, trackId = 1, trackGen = 5, psrc = 23, dataSource = DataSource.reg)
      dut.io.out.valid.expect(true.B)
      dut.io.out.bits.fallback.expect(true.B)
      dut.io.out.bits.reason.expect(IntERFallbackReason.unsupportedReadPath)
      dut.io.out.bits.src(0).valid.expect(true.B)
      dut.io.out.bits.src(0).trackGen.expect(5.U)
    }
  }

  it should "emit keyed fallback for uncertain-latency supported read paths" in {
    simulate(new IntERDataPathReadDoneProbe(localSrc = 2, replayProne = false)(configWith(IntEarlyReleaseParams(enable = true, trackEntries = 2)))) { dut =>
      clearProbe(dut)
      dut.io.s1Valid.poke(true.B)
      dut.io.uncertainReadPath.poke(true.B)
      setRobPtr(dut.io.robIdx, 11)
      setTrackedSource(dut, localSrc = 0, logicalSrc = 0, trackId = 1, trackGen = 5, psrc = 24, dataSource = DataSource.reg)
      setTrackedSource(dut, localSrc = 1, logicalSrc = 1, trackId = 0, trackGen = 6, psrc = 25, dataSource = DataSource.regcache)
      dut.io.out.valid.expect(true.B)
      dut.io.out.bits.fallback.expect(true.B)
      dut.io.out.bits.reason.expect(IntERFallbackReason.unsupportedReadPath)
      dut.io.out.bits.robIdx.value.expect(11.U)
      dut.io.out.bits.src(0).valid.expect(true.B)
      dut.io.out.bits.src(0).trackId.expect(1.U)
      dut.io.out.bits.src(0).trackGen.expect(5.U)
      dut.io.out.bits.src(0).srcIdx.expect(0.U)
      dut.io.out.bits.src(0).psrc.expect(24.U)
      dut.io.out.bits.src(1).valid.expect(true.B)
      dut.io.out.bits.src(1).trackId.expect(0.U)
      dut.io.out.bits.src(1).trackGen.expect(6.U)
      dut.io.out.bits.src(1).srcIdx.expect(1.U)
      dut.io.out.bits.src(1).psrc.expect(25.U)
    }
  }
}
