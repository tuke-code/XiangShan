package xiangshan.backend.vector.vagq

import chisel3._
import chisel3.util._
import org.chipsalliance.cde.config.Parameters
import xiangshan._
import xiangshan.backend.rob.RobPtr

import VAGQConstants._

class SplitCtrl(numEntries: Int)(implicit p: Parameters) extends VAGQModule {
  val io = IO(new SplitCtrlIO(numEntries))

  private val activePending  = Wire(Vec(numEntries, UInt(vagqFlowBytes.W)))
  private val emptyPending   = Wire(Vec(numEntries, UInt(vagqFlowBytes.W)))
  private val orderedBlocked = Wire(Vec(numEntries, Bool()))
  private val canSplit       = Wire(Vec(numEntries, Bool()))

  for (i <- 0 until numEntries) {
    val entry = io.in(i).entry
    canSplit(i) := entry.valid && entry.state === VAGQEntryState.split && !entry.robIdx.needFlush(io.redirect)
    orderedBlocked(i) := entry.isOrdered && ((entry.reqSent & ~entry.reqAck & entry.elemActiveMask).orR)
    activePending(i) := Mux(
      canSplit(i) && !orderedBlocked(i),
      ~entry.reqSent & entry.elemActiveMask,
      0.U(vagqFlowBytes.W)
    )
    emptyPending(i) := Mux(
      canSplit(i),
      ~entry.reqSent & ~entry.reqAck & ~entry.elemActiveMask,
      0.U(vagqFlowBytes.W)
    )
  }

  private val activeEntryHasReq = VecInit(activePending.map(_.orR))
  private val emptyEntryHasReq  = VecInit(emptyPending.map(_.orR))

  val activeHasReq = activeEntryHasReq.asUInt
  val emptyHasReq  = emptyEntryHasReq.asUInt
  val hasActiveReq = activeHasReq.orR
  val hasEmptyReq  = emptyHasReq.orR
  val issueActive  = hasActiveReq
  val activeSel    = PriorityEncoder(activeHasReq)
  val emptySel     = PriorityEncoder(emptyHasReq)
  val selectedIdx  = Mux(issueActive, activeSel, emptySel)
  val hasReq       = hasActiveReq | hasEmptyReq

  private val selectedValid       = hasReq
  private val selectedIssueActive = issueActive
  private val selectedOH          = UIntToOH(selectedIdx, numEntries) & Fill(numEntries, hasReq)
  private val selectedInput       = Mux(issueActive, io.in(activeSel), io.in(emptySel))
  private val selectedPending     = Mux(issueActive, activePending(activeSel), emptyPending(emptySel))
  private val selectedByteOffset  = PriorityEncoder(selectedPending)

  private val addrGen = Module(new AddrGen)
  addrGen.in.uopType    := selectedInput.entry.uopType
  addrGen.in.baseAddr   := selectedInput.entry.baseAddr
  addrGen.in.op2Data    := selectedInput.entry.op2Data
  addrGen.in.uopIdx     := selectedInput.entry.uopIdx
  addrGen.in.byteOffset := selectedByteOffset
  addrGen.in.deew       := selectedInput.entry.deew
  addrGen.in.ieew       := selectedInput.entry.ieew

  private val issueMask = addrGen.out.elemMask & selectedPending
  private val selectedAlignedType = selectedInput.entry.deew

  private val storeReadValid    = RegInit(false.B)
  private val storeReadEntryIdx = RegInit(0.U(vagqEntryIdxWidth.W))
  private val storeReadRobIdx   = RegInit(0.U.asTypeOf(new RobPtr))
  private val storeReadEntry    = io.in(storeReadEntryIdx)
  private val storeReadAlive    = storeReadValid &&
                                  storeReadEntry.entry.state === VAGQEntryState.split &&
                                  storeReadEntry.entry.isStore &&
                                  storeReadEntry.entry.robIdx === storeReadRobIdx &&
                                  entryAlive(storeReadEntry.entry, io.redirect)

  private val storeDataValid    = RegInit(false.B)
  private val storeDataEntryIdx = RegInit(0.U(vagqEntryIdxWidth.W))
  private val storeDataRobIdx   = RegInit(0.U.asTypeOf(new RobPtr))
  private val storeData         = Reg(UInt(VLEN.W))
  private val storeDataEntry    = io.in(storeDataEntryIdx)
  private val storeDataAlive    = storeDataValid &&
                                  storeDataEntry.entry.state === VAGQEntryState.split &&
                                  storeDataEntry.entry.isStore &&
                                  storeDataEntry.entry.robIdx === storeDataRobIdx &&
                                  entryAlive(storeDataEntry.entry, io.redirect)
  private val selectedStoreDataReady = storeDataAlive &&
                                      storeDataEntryIdx === selectedInput.entryIdx &&
                                      storeDataRobIdx === selectedInput.entry.robIdx

  private val selectedNeedsStoreData = hasReq && issueActive && selectedInput.entry.isStore && !selectedStoreDataReady
  private val activeReqDataReady = !selectedInput.entry.isStore || selectedStoreDataReady

  io.vrfReadReq.valid         := selectedNeedsStoreData && !storeReadValid
  io.vrfReadReq.bits          := 0.U.asTypeOf(io.vrfReadReq.bits)
  io.vrfReadReq.bits.entryIdx := selectedInput.entryIdx
  io.vrfReadReq.bits.robIdx   := selectedInput.entry.robIdx
  io.vrfReadReq.bits.psrc     := selectedInput.entry.psrc2

  private val storeReadRespAlive = io.vrfReadResp.valid &&
    storeReadAlive &&
    io.vrfReadResp.bits.entryIdx === storeReadEntryIdx &&
    io.vrfReadResp.bits.robIdx === storeReadRobIdx

  when(storeReadValid) {
    when(!storeReadAlive || storeReadRespAlive) {
      storeReadValid := false.B
    }
  }.elsewhen(io.vrfReadReq.fire) {
    storeReadValid    := true.B
    storeReadEntryIdx := io.vrfReadReq.bits.entryIdx
    storeReadRobIdx   := io.vrfReadReq.bits.robIdx
  }

  when(storeDataValid && !storeDataAlive) {
    storeDataValid := false.B
  }

  when(storeReadRespAlive) {
    storeDataValid    := true.B
    storeDataEntryIdx := io.vrfReadResp.bits.entryIdx
    storeDataRobIdx   := io.vrfReadResp.bits.robIdx
    storeData         := io.vrfReadResp.bits.data
  }

  io.lsuReq.valid            := hasReq && issueActive && activeReqDataReady
  io.lsuReq.bits             := 0.U.asTypeOf(io.lsuReq.bits)
  io.lsuReq.bits.entryIdx    := selectedInput.entryIdx
  io.lsuReq.bits.robIdx      := selectedInput.entry.robIdx
  io.lsuReq.bits.isLoad      := selectedInput.entry.isLoad
  io.lsuReq.bits.isStore     := selectedInput.entry.isStore
  io.lsuReq.bits.byteOffset  := selectedByteOffset
  io.lsuReq.bits.elemIdx     := addrGen.out.elemIdx
  io.lsuReq.bits.vaddr       := addrGen.out.vaddr
  io.lsuReq.bits.mask        := issueMask
  io.lsuReq.bits.data        := Mux(selectedStoreDataReady, storeData, 0.U(VLEN.W))
  io.lsuReq.bits.pdest       := selectedInput.entry.pdest
  io.lsuReq.bits.alignedType := selectedAlignedType
  io.lsuReq.bits.nf          := selectedInput.entry.nf

  io.lsqEmptyReq.valid            := hasReq && !issueActive
  io.lsqEmptyReq.bits             := 0.U.asTypeOf(io.lsqEmptyReq.bits)
  io.lsqEmptyReq.bits.entryIdx    := selectedInput.entryIdx
  io.lsqEmptyReq.bits.robIdx      := selectedInput.entry.robIdx
  io.lsqEmptyReq.bits.isLoad      := selectedInput.entry.isLoad
  io.lsqEmptyReq.bits.isStore     := selectedInput.entry.isStore
  io.lsqEmptyReq.bits.byteOffset  := selectedByteOffset
  io.lsqEmptyReq.bits.elemIdx     := addrGen.out.elemIdx
  io.lsqEmptyReq.bits.mask        := issueMask
  io.lsqEmptyReq.bits.alignedType := selectedAlignedType

  io.update.valid           := io.lsuReq.fire || io.lsqEmptyReq.fire
  io.update.bits            := 0.U.asTypeOf(io.update.bits)
  io.update.bits.entryIdx   := selectedInput.entryIdx
  io.update.bits.setReqSent := issueMask
}

class SplitCtrlIO(numEntries: Int)(implicit p: Parameters) extends VAGQBundle {
  val in          = Input(Vec(numEntries, new CtrlInput))
  val redirect    = Flipped(Valid(new Redirect))
  val lsuReq      = Decoupled(new VAGQLsuReq)
  val lsqEmptyReq = Decoupled(new VAGQLsqEmptyReq)
  val vrfReadReq  = Decoupled(new VAGQVRFReadReq)
  val vrfReadResp = Flipped(Valid(new VAGQVRFReadResp))
  val update      = Valid(new VAGQReqBitmapUpdate)
}

class VAGQReqBitmapUpdate(implicit p: Parameters) extends VAGQBundle {
  val entryIdx        = UInt(vagqEntryIdxWidth.W)
  val setReqSent      = UInt(vagqFlowBytes.W)
  val clearReqSent    = UInt(vagqFlowBytes.W)
  val setReqAck       = UInt(vagqFlowBytes.W)
  val exception       = Bool()
  val exceptionNumber = UInt(ExceptionNumberWidth.W)
  val faultElemIdx    = UInt(vagqFlowByteWidth.W)
}
