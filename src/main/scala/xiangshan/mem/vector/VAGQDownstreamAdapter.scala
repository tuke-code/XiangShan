package xiangshan.mem

import chisel3._
import chisel3.util._
import org.chipsalliance.cde.config.Parameters
import utility.HasCircularQueuePtrHelper
import xiangshan._
import xiangshan.backend.Bundles.ExuInput
import xiangshan.backend.exu.ExeUnitParams
import xiangshan.backend.fu.FuType
import xiangshan.backend.vector.vagq._

class VAGQDownstreamAdapterIO(loadParams: Seq[ExeUnitParams])(implicit p: Parameters) extends XSBundle {
  val vagqLsuReq  = Flipped(Vec(VAGQConstants.ActiveIssueWidth, Decoupled(new VAGQLsuReq)))
  val vagqLduResp = Vec(VAGQConstants.LduRespWidth, Valid(new VAGQResp))
  val vagqStaResp = Vec(VAGQConstants.StaRespWidth, Valid(new VAGQResp))

  val issueLda = Flipped(MixedVec(loadParams.map(param => DecoupledIO(new ExuInput(param)))))
  val lduReq = MixedVec(loadParams.map(param => DecoupledIO(new ExuInput(param))))
  val lduReqMeta = Output(Vec(loadParams.length, new VAGQMemPipelineMeta))
  val lduResp = Flipped(Vec(VAGQConstants.LduRespWidth, Valid(new VAGQResp)))

  val vagqLsqEmptyReq  = Flipped(Decoupled(new VAGQLsqEmptyReq))
  val vagqLsqEmptyResp = Valid(new VAGQLsqEmptyResp)

  val lsqEmptyReq  = Decoupled(new VAGQLsqEmptyReq)
  val lsqEmptyResp = Flipped(Valid(new VAGQLsqEmptyResp))
}

class VAGQDownstreamAdapter(loadParams: Seq[ExeUnitParams])(implicit p: Parameters)
  extends XSModule
  with HasCircularQueuePtrHelper {
  require(loadParams.length >= VAGQConstants.LduRespWidth)

  val io = IO(new VAGQDownstreamAdapterIO(loadParams))

  private def vagqLoadFuOpType(alignedType: UInt): UInt = {
    MuxLookup(alignedType(1, 0), LSUOpType.vle8.asUInt)(Seq(
      0.U -> LSUOpType.vle8.asUInt,
      1.U -> LSUOpType.vle16.asUInt,
      2.U -> LSUOpType.vle32.asUInt,
      3.U -> LSUOpType.vle64.asUInt,
    ))
  }

  io.vagqLduResp.zip(io.lduResp).foreach { case (toVagq, fromLdu) =>
    toVagq := fromLdu
  }

  for (i <- loadParams.indices) {
    if (i < VAGQConstants.ActiveIssueWidth) {
      val activeReq = io.vagqLsuReq(i)
      val activeLoadValid = activeReq.valid && activeReq.bits.isLoad
      val selectActive = activeLoadValid &&
        (!io.issueLda(i).valid || isAfter(io.issueLda(i).bits.robIdx, activeReq.bits.robIdx))

      val activeLdin = Wire(chiselTypeOf(io.lduReq(i).bits))
      activeLdin := 0.U.asTypeOf(activeLdin)
      activeLdin.fuType   := FuType.vldu.U
      activeLdin.fuOpType := vagqLoadFuOpType(activeReq.bits.alignedType)
      activeLdin.src(0)   := activeReq.bits.vaddr
      activeLdin.imm      := 0.U
      activeLdin.robIdx   := activeReq.bits.robIdx
      activeLdin.pdest    := activeReq.bits.pdest
      activeLdin.vecWen.foreach(_ := true.B)
      activeLdin.lqIdx.foreach(_ := activeReq.bits.lqIdx)
      activeLdin.sqIdx.foreach(_ := activeReq.bits.sqIdx)

      val activeMeta = Wire(chiselTypeOf(io.lduReqMeta(i)))
      activeMeta := 0.U.asTypeOf(activeMeta)
      activeMeta.valid      := true.B
      activeMeta.entryIdx   := activeReq.bits.entryIdx
      activeMeta.robIdx     := activeReq.bits.robIdx
      activeMeta.isLoad     := activeReq.bits.isLoad
      activeMeta.isStore    := false.B
      activeMeta.byteOffset := activeReq.bits.byteOffset
      activeMeta.mask       := activeReq.bits.mask

      io.lduReq(i).valid := Mux(selectActive, activeLoadValid, io.issueLda(i).valid)
      io.lduReq(i).bits  := Mux(selectActive, activeLdin, io.issueLda(i).bits)
      io.issueLda(i).ready := !selectActive && io.lduReq(i).ready
      activeReq.ready := selectActive && io.lduReq(i).ready
      io.lduReqMeta(i) := Mux(selectActive, activeMeta, 0.U.asTypeOf(io.lduReqMeta(i)))
    } else {
      io.lduReq(i) <> io.issueLda(i)
      io.lduReqMeta(i) := 0.U.asTypeOf(io.lduReqMeta(i))
    }
  }

  io.vagqStaResp.foreach { resp =>
    resp.valid := false.B
    resp.bits  := 0.U.asTypeOf(resp.bits)
  }

  io.lsqEmptyReq.valid := io.vagqLsqEmptyReq.valid
  io.lsqEmptyReq.bits  := io.vagqLsqEmptyReq.bits
  io.vagqLsqEmptyReq.ready := io.lsqEmptyReq.ready
  io.vagqLsqEmptyResp := io.lsqEmptyResp
}
