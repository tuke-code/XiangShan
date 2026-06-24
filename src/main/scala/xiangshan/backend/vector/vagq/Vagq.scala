package xiangshan.backend.vector.vagq

import chisel3._
import chisel3.util._
import org.chipsalliance.cde.config.Parameters
import utility._
import xiangshan._
import xiangshan.backend.Bundles._
import xiangshan.backend.fu.NewCSR.CSRConfig
import xiangshan.backend.rob.RobPtr
import xiangshan.frontend.ftq.FtqPtr
import xiangshan.mem.{LqPtr, SqPtr}

import VAGQConstants._

object VAGQConstants {
  val VAGQSize = 8
  val VAGQEntryIdxWidth = log2Ceil(VAGQSize)
  val FlowBytes = 16
  val FlowByteWidth = log2Ceil(FlowBytes)
  val UvlByteWidth = FlowByteWidth + 1
  val UopIdxWidth = 3
  val FaultVstartWidth = UopIdxWidth + FlowByteWidth
  val EewWidth = 2
  val AlignedTypeWidth = EewWidth + 1
  val NfWidth = 3
  val ExceptionNumberWidth = 6

  val LsuRespWidth = 2
  val MergeRespWidth = LsuRespWidth + 1
}

trait HasVAGQParameters extends HasXSParameter {
  import VAGQConstants._

  def vagqSize: Int = VAGQSize
  def vagqEntryIdxWidth: Int = VAGQEntryIdxWidth
  def vagqFlowBytes: Int = FlowBytes
  def vagqFlowByteWidth: Int = FlowByteWidth
  def vagqUvlByteWidth: Int = UvlByteWidth
  def vagqUopIdxWidth: Int = UopIdxWidth

  require(VLEN == 128, s"VAGQ currently assumes VLEN=128, got VLEN=$VLEN")
  require(VDataBytes == FlowBytes, s"VAGQ FlowBytes must match VDataBytes, got $FlowBytes and $VDataBytes")
  require(vagqSize == 4 || vagqSize == 8, s"VAGQSize must be 4 or 8, got $vagqSize")
}

abstract class VAGQBundle(implicit p: Parameters) extends XSBundle with HasVAGQParameters

abstract class VAGQModule(implicit p: Parameters) extends XSModule with HasVAGQParameters with HasVAGQHelper

class VAGQMeta(implicit p: Parameters) extends VAGQBundle {
  val pc = UInt(VAddrBits.W)
  val isRVC = Bool()
  val ftqPtr = new FtqPtr
  val ftqOffset = UInt(FetchBlockInstOffsetWidth.W)
  val lqIdx = new LqPtr
  val sqIdx = new SqPtr
  val trigger = TriggerAction()
  val perfDebugInfo = new PerfDebugInfo
  val debug_seqNum = InstSeqNum()
}

class VAGQAddrSideUop(implicit p: Parameters) extends VAGQBundle {
  val meta = new VAGQMeta
  val entryIdx = UInt(vagqEntryIdxWidth.W)
  val uopType = UInt(3.W)
  val robIdx = new RobPtr
  val pdest = UInt(VfPhyRegIdxWidth.W)
  val baseAddr = UInt(XLEN.W)
  val op2Data = UInt(VLEN.W)
  val vl = UInt(CSRConfig.VlWidth.W)
  val vstart = UInt((CSRConfig.VlWidth-1).W)
  val useVstart = Bool()
  val vm = Bool()
  val v0Mask = UInt(vagqFlowBytes.W)
  val deew = UInt(EewWidth.W)
  val ieew = UInt(EewWidth.W)
  val vma = Bool()
  val vta = Bool()
  val uopIdx = UInt(UopIdxWidth.W)
  val nf = UInt(NfWidth.W)
}

class VAGQDataSideUop(implicit p: Parameters) extends VAGQBundle {
  val entryIdx = UInt(vagqEntryIdxWidth.W)
  val robIdx = new RobPtr
  val psrc2 = UInt(VfPhyRegIdxWidth.W)
}

class VAGQLsuReq(implicit p: Parameters) extends VAGQBundle {
  val entryIdx = UInt(vagqEntryIdxWidth.W)
  val robIdx = new RobPtr
  val isLoad = Bool()
  val isStore = Bool()
  val byteOffset = UInt(vagqFlowByteWidth.W)
  val vaddr = UInt(XLEN.W)
  val mask = UInt(vagqFlowBytes.W)
  val data = UInt(VLEN.W)
  val pdest = UInt(VfPhyRegIdxWidth.W)
  val alignedType = UInt(AlignedTypeWidth.W)  // deew
  val nf = UInt(NfWidth.W)
}

class VAGQLsqEmptyReq(implicit p: Parameters) extends VAGQBundle {
  val entryIdx = UInt(vagqEntryIdxWidth.W)
  val robIdx = new RobPtr
  val isLoad = Bool()
  val isStore = Bool()
  val byteOffset = UInt(vagqFlowByteWidth.W)
  val mask = UInt(vagqFlowBytes.W)
  val alignedType = UInt(AlignedTypeWidth.W)  // deew
}

class VAGQResp(implicit p: Parameters) extends VAGQBundle {
  val entryIdx = UInt(vagqEntryIdxWidth.W)
  val robIdx = new RobPtr
  val isLoad = Bool()
  val isStore = Bool()
  val byteOffset = UInt(vagqFlowByteWidth.W)
  val mask = UInt(vagqFlowBytes.W)
  val data = UInt(VLEN.W)
  val isNACK = Bool()
  val exception = Bool()
  val exceptionNumber = UInt(ExceptionNumberWidth.W)
}

class VAGQVRFReadReq(implicit p: Parameters) extends VAGQBundle {
  val entryIdx = UInt(vagqEntryIdxWidth.W)
  val robIdx = new RobPtr
  val psrc = UInt(VfPhyRegIdxWidth.W)
}

class VAGQVRFReadResp(implicit p: Parameters) extends VAGQBundle {
  val entryIdx = UInt(vagqEntryIdxWidth.W)
  val robIdx = new RobPtr
  val data = UInt(VLEN.W)
}

class VAGQVRFWriteReq(implicit p: Parameters) extends VAGQBundle {
  val entryIdx = UInt(vagqEntryIdxWidth.W)
  val robIdx = new RobPtr
  val pdest = UInt(VfPhyRegIdxWidth.W)
  val data = UInt(VLEN.W)
  val mask = UInt(vagqFlowBytes.W)
}

class VAGQWritebackReq(implicit p: Parameters) extends VAGQBundle {
  val meta = new VAGQMeta
  val entryIdx = UInt(vagqEntryIdxWidth.W)
  val robIdx = new RobPtr
  val exception = Bool()
  val exceptionNumber = UInt(ExceptionNumberWidth.W)
  val faultElemIdx = UInt(vagqFlowByteWidth.W)
  val faultVstart = UInt(VAGQConstants.FaultVstartWidth.W)
}

class VAGQIO(implicit p: Parameters) extends VAGQBundle {
  // from iq
  val addrUop = Flipped(Decoupled(new VAGQAddrSideUop))
  val dataUop = Flipped(Decoupled(new VAGQDataSideUop))
  // active req to ldu
  val lsuReq = Decoupled(new VAGQLsuReq)
  val lsuResp = Flipped(Vec(VAGQConstants.LsuRespWidth, Valid(new VAGQResp)))
  // unactive req to lsq
  val lsqEmptyReq = Decoupled(new VAGQLsqEmptyReq)
  val lsqEmptyResp = Flipped(Valid(new VAGQResp))
  // load read oldvd, store read storeData
  val vrfReadReq = Decoupled(new VAGQVRFReadReq)
  val vrfReadResp = Flipped(Valid(new VAGQVRFReadResp))
  // write vrf with mask
  val vrfWriteReq = Decoupled(new VAGQVRFWriteReq)
  // to rob
  val robWriteback = Decoupled(new VAGQWritebackReq)
  val redirect = Flipped(Valid(new Redirect))
}

class VAGQ(implicit p: Parameters) extends VAGQModule {
  val io = IO(new VAGQIO)

  private val addrMaskGen = Module(new MaskGen)
  addrMaskGen.in.uopIdx    := io.addrUop.bits.uopIdx
  addrMaskGen.in.useVstart := io.addrUop.bits.useVstart
  addrMaskGen.in.vstart    := io.addrUop.bits.vstart
  addrMaskGen.in.vl        := io.addrUop.bits.vl
  addrMaskGen.in.vm        := io.addrUop.bits.vm
  addrMaskGen.in.v0Mask    := io.addrUop.bits.v0Mask
  addrMaskGen.in.deew      := io.addrUop.bits.deew
  addrMaskGen.in.vma       := io.addrUop.bits.vma
  addrMaskGen.in.vta       := io.addrUop.bits.vta

  private val entryTable = Module(new VAGQEntryTable)
  entryTable.io.addrUop <> io.addrUop
  entryTable.io.dataUop <> io.dataUop
  entryTable.io.maskInfo := addrMaskGen.out
  entryTable.io.redirect := io.redirect

  val entries = entryTable.io.entries
}
