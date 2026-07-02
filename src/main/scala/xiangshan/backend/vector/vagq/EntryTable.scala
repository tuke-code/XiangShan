package xiangshan.backend.vector.vagq

import chisel3._
import chisel3.util._
import org.chipsalliance.cde.config.Parameters
import xiangshan._
import xiangshan.backend.Bundles._
import xiangshan.backend.rob.RobPtr
import xiangshan.frontend.ftq.FtqPtr
import xiangshan.mem.{LqPtr, SqPtr}

import VAGQConstants._

class VAGQEntryTable(implicit p: Parameters) extends VAGQModule {
  val io = IO(new VAGQEntryTableIO)

  private val addrMaskGen = Module(new MaskGen)
  addrMaskGen.in.uopIdx    := io.addrUop.bits.uopIdx
  addrMaskGen.in.useVstart := io.addrUop.bits.useVstart
  addrMaskGen.in.vstart    := io.addrUop.bits.vstart
  addrMaskGen.in.uvlByte   := io.addrUop.bits.uvlByte
  addrMaskGen.in.vm        := io.addrUop.bits.vm
  addrMaskGen.in.v0Mask    := io.addrUop.bits.v0Mask
  addrMaskGen.in.deew      := io.addrUop.bits.deew
  addrMaskGen.in.vma       := io.addrUop.bits.vma
  addrMaskGen.in.vta       := io.addrUop.bits.vta

  private val emptyEntry = 0.U.asTypeOf(new VAGQEntry)
  private val entries = RegInit(VecInit(Seq.fill(vagqSize)(emptyEntry)))

  private val entriesNext = WireInit(entries)

  private val addrEntry = entryAt(entries, io.addrUop.bits.entryIdx)
  private val dataEntry = entryAt(entries, io.dataUop.bits.entryIdx)
  private val addrIdxValid = idxValid(io.addrUop.bits.entryIdx)
  private val dataIdxValid = idxValid(io.dataUop.bits.entryIdx)
  private val addrEntryFlush = addrEntry.valid && addrEntry.robIdx.needFlush(io.redirect)
  private val dataEntryFlush = dataEntry.valid && dataEntry.robIdx.needFlush(io.redirect)
  private val addrCanAccept = !addrEntry.valid || addrEntry.state === VAGQEntryState.waitA
  private val dataCanAccept = !dataEntry.valid || dataEntry.state === VAGQEntryState.waitSI

  io.addrUop.ready := addrIdxValid && addrCanAccept && !addrEntryFlush
  io.dataUop.ready := dataIdxValid && dataCanAccept && !dataEntryFlush

  private val addrFire = io.addrUop.fire && !io.addrUop.bits.robIdx.needFlush(io.redirect)
  private val dataFire = io.dataUop.fire && !io.dataUop.bits.robIdx.needFlush(io.redirect)

  for (i <- 0 until vagqSize) {
    val idx = i.U(vagqEntryIdxWidth.W)
    val updateVec = io.splitUpdate.toSeq ++ io.mergeReqUpdate.toSeq
    val updateHits = updateVec.map(update => update.valid && update.bits.entryIdx === idx)
    val setReqSent = updateVec.zip(updateHits).map { case (update, hit) =>
      Mux(hit, update.bits.setReqSent, 0.U)
    }.reduce(_ | _)
    val clearReqSent = updateVec.zip(updateHits).map { case (update, hit) =>
      Mux(hit, update.bits.clearReqSent, 0.U)
    }.reduce(_ | _)
    val setReqAck = updateVec.zip(updateHits).map { case (update, hit) =>
      Mux(hit, update.bits.setReqAck, 0.U)
    }.reduce(_ | _)
    val exceptionHits = updateVec.zip(updateHits).map { case (update, hit) =>
      hit && update.bits.exception
    }
    val hasExceptionUpdate = exceptionHits.reduce(_ || _)
    val exceptionUpdate = Mux1H(exceptionHits, updateVec.map(_.bits))

    when(updateHits.reduce(_ || _)) {
      entriesNext(i).reqSent := (entries(i).reqSent | setReqSent) & ~clearReqSent
      entriesNext(i).reqAck  := entries(i).reqAck | setReqAck
    }
    when(hasExceptionUpdate) {
      entriesNext(i).exceptionNumber := exceptionUpdate.exceptionNumber
      entriesNext(i).faultElemIdx    := exceptionUpdate.faultElemIdx
      entriesNext(i).state           := VAGQEntryState.excp
    }

    when(io.mergeStateUpdate.valid && io.mergeStateUpdate.bits.entryIdx === idx) {
      when(io.mergeStateUpdate.bits.clearValid) {
        entriesNext(i).valid := false.B
      }.otherwise {
        entriesNext(i).state := io.mergeStateUpdate.bits.stateNext
      }
    }

    val addrFireThis = addrFire && io.addrUop.bits.entryIdx === idx
    val dataFireThis = dataFire && io.dataUop.bits.entryIdx === idx

    when(addrFireThis) {
      connectSamePort(entriesNext(i), io.addrUop.bits)
      connectSamePort(entriesNext(i), addrMaskGen.out)
    }
    when(dataFireThis) {
      connectSamePort(entriesNext(i), io.dataUop.bits)
    }

    when(addrFireThis && dataFireThis) {
      enterSplit(entriesNext(i))
    }.elsewhen(addrFireThis) {
      when(entries(i).valid && entries(i).state === VAGQEntryState.waitA) {
        enterSplit(entriesNext(i))
      }.otherwise {
        initPending(entriesNext(i), VAGQEntryState.waitSI)
      }
    }.elsewhen(dataFireThis) {
      when(entries(i).valid && entries(i).state === VAGQEntryState.waitSI) {
        enterSplit(entriesNext(i))
      }.otherwise {
        initPending(entriesNext(i), VAGQEntryState.waitA)
      }
    }

    when(entries(i).valid && entries(i).robIdx.needFlush(io.redirect)) {
      entriesNext(i) := emptyEntry
    }
  }

  entries := entriesNext
  io.entries := entries
}

class VAGQEntryTableIO(implicit p: Parameters) extends VAGQBundle {
  val addrUop          = Flipped(Decoupled(new VAGQAddrSideUop))
  val dataUop          = Flipped(Decoupled(new VAGQDataSideUop))
  val entries          = Output(Vec(vagqSize, new VAGQEntry))
  val splitUpdate      = Input(Vec(VAGQConstants.SplitUpdateWidth, Valid(new VAGQReqBitmapUpdate)))
  val mergeReqUpdate   = Flipped(Vec(VAGQConstants.MergeRespWidth, Valid(new VAGQReqBitmapUpdate)))
  val mergeStateUpdate = Flipped(Valid(new VAGQEntryStateUpdate))
  val redirect         = Flipped(Valid(new Redirect))
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
  val elemAgnosticMask = UInt(vagqFlowBytes.W)

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

class VAGQEntryStateUpdate(implicit p: Parameters) extends VAGQBundle {
  val entryIdx   = UInt(vagqEntryIdxWidth.W)
  val stateNext  = UInt(3.W)
  val clearValid = Bool()
}

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
