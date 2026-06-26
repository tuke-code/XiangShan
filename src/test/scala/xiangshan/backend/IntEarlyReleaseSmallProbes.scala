package xiangshan.backend

import chisel3._
import chisel3.util.log2Ceil
import org.chipsalliance.cde.config.Parameters
import xiangshan._
import xiangshan.TopDownCounters._
import xiangshan.backend.decode.{DecodeStage, DecodeStageIO}
import xiangshan.backend.rename.{RatReadPort, Reg_I, RenameTable}

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

class DecodeOldDestHoldProbe(implicit p: Parameters) extends XSModule {
  private val intLogicWidth = log2Ceil(IntLogicRegs)

  val io = IO(new Bundle {
    val inValid = Input(Bool())
    val outReady = Input(Bool())
    val instr = Input(UInt(32.W))
    val writeValid = Input(Bool())
    val writeAddr = Input(UInt(intLogicWidth.W))
    val writeData = Input(UInt(PhyRegIdxWidth.W))
    val oldDestAddr = Output(UInt(intLogicWidth.W))
    val oldDestHold = Output(Bool())
    val oldDestData = Output(UInt(PhyRegIdxWidth.W))
    val outValid = Output(Bool())
    val outLdest = Output(UInt(LogicRegsWidth.W))
    val inReady = Output(Bool())
  })

  val decode = Module(new DecodeStage)
  val intRat = Module(new RenameTable(Reg_I, 0))

  decode.io.redirect.valid := false.B
  decode.io.redirect.bits := 0.U.asTypeOf(new Redirect)
  decode.io.csrCtrl := 0.U.asTypeOf(decode.io.csrCtrl)
  decode.io.fromCSR := 0.U.asTypeOf(decode.io.fromCSR)
  decode.io.fusion := 0.U.asTypeOf(decode.io.fusion)
  decode.io.fromRob := 0.U.asTypeOf(decode.io.fromRob)
  decode.io.vsetvlVType := 0.U.asTypeOf(decode.io.vsetvlVType)
  decode.io.vstart := 0.U.asTypeOf(decode.io.vstart)
  decode.io.stallReason.in.reason.foreach(_ := NoStall.id.U)
  decode.io.debugOutValid.foreach(_ := 0.U.asTypeOf(decode.io.debugOutValid.get))

  for (i <- 0 until DecodeWidth) {
    decode.io.in(i).valid := io.inValid && i.U === 0.U
    decode.io.in(i).bits := 0.U.asTypeOf(decode.io.in(i).bits)
    decode.io.in(i).bits.instr := io.instr
    decode.io.in(i).bits.isLastInFtqEntry := true.B
    decode.io.out(i).ready := io.outReady

    decode.io.fpRat(i).foreach(_.data := 0.U)
    decode.io.vecRat(i).foreach(_.data := 0.U)
    decode.io.v0Rat(i).data := 0.U
    decode.io.vlRat(i).data := 0.U
  }

  intRat.io.redirect := false.B
  intRat.io.snpt := 0.U.asTypeOf(intRat.io.snpt)
  intRat.io.specWritePorts.foreach(_ := 0.U.asTypeOf(intRat.io.specWritePorts.head))
  intRat.io.archWritePorts.foreach(_ := 0.U.asTypeOf(intRat.io.archWritePorts.head))
  intRat.io.specWritePorts(0).wen := io.writeValid
  intRat.io.specWritePorts(0).addr := io.writeAddr
  intRat.io.specWritePorts(0).data := io.writeData
  intRat.io.diffWritePorts.foreach(_ := 0.U.asTypeOf(intRat.io.diffWritePorts.get))

  val readPorts = decode.io.intRat.flatten ++ decode.io.intOldDestRat.get
  require(readPorts.length == intRat.io.readPorts.length, "decode old-dest hold probe must cover all integer RAT reads")
  for ((decodePort, ratPort) <- readPorts.zip(intRat.io.readPorts)) {
    decodePort <> ratPort
  }

  io.oldDestAddr := decode.io.intOldDestRat.get(0).addr
  io.oldDestHold := decode.io.intOldDestRat.get(0).hold
  io.oldDestData := decode.io.intOldDestRat.get(0).data
  io.outValid := decode.io.out(0).valid
  io.outLdest := decode.io.out(0).bits.ldest
  io.inReady := decode.io.in(0).ready
}
