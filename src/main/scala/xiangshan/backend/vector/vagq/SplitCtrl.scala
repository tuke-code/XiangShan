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
  private val canSplit       = Wire(Vec(numEntries, Bool()))

  for (i <- 0 until numEntries) {
    val entry = io.in(i).entry
    canSplit(i) := entry.valid && entry.state === VAGQEntryState.split && !entry.robIdx.needFlush(io.redirect)
    activePending(i) := Mux(
      canSplit(i),
      ~entry.reqSent & ~entry.reqAck & entry.elemActiveMask,
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

  private val selectedValid = hasReq
  private val selectedIssueActive = issueActive
  private val selectedOH = UIntToOH(selectedIdx, numEntries) & Fill(numEntries, hasReq)
  private val selectedInput = Mux(issueActive, io.in(activeSel), io.in(emptySel))
  private val selectedPending = Mux(issueActive, activePending(activeSel), emptyPending(emptySel))
  private val selectedByteOffset = PriorityEncoder(selectedPending)

  io.lsuReq.valid := false.B
  io.lsuReq.bits  := 0.U.asTypeOf(io.lsuReq.bits)

  io.lsqEmptyReq.valid := false.B
  io.lsqEmptyReq.bits  := 0.U.asTypeOf(io.lsqEmptyReq.bits)

  io.update.valid := io.lsuReq.fire || io.lsqEmptyReq.fire
  io.update.bits  := 0.U.asTypeOf(io.update.bits)
}

class SplitCtrlIO(numEntries: Int)(implicit p: Parameters) extends VAGQBundle {
  val in          = Input(Vec(numEntries, new SplitCtrlInput))
  val redirect    = Flipped(Valid(new Redirect))
  val lsuReq      = Decoupled(new VAGQLsuReq)
  val lsqEmptyReq = Decoupled(new VAGQLsqEmptyReq)
  val update      = Valid(new VAGQReqBitmapUpdate)
}

class SplitCtrlInput(implicit p: Parameters) extends VAGQBundle {
  val entryIdx = UInt(vagqEntryIdxWidth.W)
  val entry = new VAGQEntryMeta
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
