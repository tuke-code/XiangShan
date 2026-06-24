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
  val addrUop     = Flipped(Decoupled(new VAGQAddrSideUop))
  val dataUop     = Flipped(Decoupled(new VAGQDataSideUop))
  val maskInfo    = Input(new VAGQMaskInfo)
  val entries     = Output(Vec(vagqSize, new VAGQEntry))
  val splitUpdate = Input(Valid(new VAGQReqBitmapUpdate))
  val redirect    = Flipped(Valid(new Redirect))
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

  def isLoad: Bool    = VAGQUopType.isLoad(uopType)
  def isStore: Bool   = VAGQUopType.isStore(uopType)
  def isStride: Bool  = VAGQUopType.isStride(uopType)
  def isIndexed: Bool = VAGQUopType.isIndexed(uopType)
  def isOrdered: Bool = VAGQUopType.isOrdered(uopType)
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

object VAGQUopType {
  val strideLoad            = "b000".U(3.W)
  val strideStore           = "b001".U(3.W)
  val indexedUnorderedLoad  = "b100".U(3.W)
  val indexedUnorderedStore = "b101".U(3.W)
  val indexedOrderedLoad    = "b110".U(3.W)
  val indexedOrderedStore   = "b111".U(3.W)

  def isLoad(uopType: UInt): Bool    = !uopType(0)
  def isStore(uopType: UInt): Bool   =  uopType(0)
  def isStride(uopType: UInt): Bool  = !uopType(2) && !uopType(1)
  def isIndexed(uopType: UInt): Bool =  uopType(2)
  def isOrdered(uopType: UInt): Bool =  uopType(1)
}
