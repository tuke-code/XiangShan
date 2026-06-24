package xiangshan.backend.vector.vagq

import chisel3._
import chisel3.util._
import org.chipsalliance.cde.config.Parameters
import xiangshan._
import xiangshan.backend.rob.RobPtr
import xiangshan.frontend.ftq.FtqPtr
import xiangshan.mem.{LqPtr, SqPtr}

import VAGQConstants._

class VAGQEntryTable(implicit p: Parameters) extends VAGQModule {
  val io = IO(new VAGQEntryTableIO)

  private val emptyEntry = 0.U.asTypeOf(new VAGQEntry)
  private val entries = RegInit(VecInit(Seq.fill(vagqSize)(emptyEntry)))

  private val entriesNext = WireInit(entries)

  entries := entriesNext
  io.entries := entries
}

class VAGQEntryTableIO(implicit p: Parameters) extends VAGQBundle {
  val addrUop = Flipped(Decoupled(new VAGQAddrSideUop))
  val dataUop = Flipped(Decoupled(new VAGQDataSideUop))
  val maskInfo = Input(new VAGQMaskInfo)
  val entries = Output(Vec(vagqSize, new VAGQEntry))
  val redirect = Flipped(Valid(new Redirect))
}

class VAGQMaskInfo(implicit p: Parameters) extends VAGQBundle {
  val elemActiveMask = UInt(vagqFlowBytes.W)
  val agnosticMask = UInt(vagqFlowBytes.W)
}

class VAGQEntryMeta(implicit p: Parameters) extends VAGQBundle {
  val valid = Bool()
  val meta = new VAGQMeta
  val uopType = UInt(3.W)
  val robIdx = new RobPtr
  val pdest = UInt(VfPhyRegIdxWidth.W)
  val psrc2 = UInt(VfPhyRegIdxWidth.W)

  val baseAddr = UInt(XLEN.W)
  val op2Data = UInt(VLEN.W)

  val ieew = UInt(EewWidth.W)
  val deew = UInt(EewWidth.W)
  val useVstart = Bool()
  val vma = Bool()
  val vta = Bool()
  val uopIdx = UInt(UopIdxWidth.W)
  val elemActiveMask = UInt(vagqFlowBytes.W)
  val agnosticMask = UInt(vagqFlowBytes.W)

  val nf = UInt(NfWidth.W)

  val reqSent = UInt(vagqFlowBytes.W)
  val reqAck = UInt(vagqFlowBytes.W)

  val exceptionNumber = UInt(ExceptionNumberWidth.W)
  val faultElemIdx = UInt(vagqFlowByteWidth.W)
  val state = UInt(3.W)
}

class VAGQEntry(implicit p: Parameters) extends VAGQEntryMeta

object VAGQEntryState {
  val waitA  = "b001".U(3.W)
  val waitSI = "b010".U(3.W)
  val split  = "b011".U(3.W)
  val merge  = "b100".U(3.W)
  val wb     = "b101".U(3.W)
  val excp   = "b110".U(3.W)
}
