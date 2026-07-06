package xiangshan.mem

import chisel3._
import chisel3.util._
import org.chipsalliance.cde.config.Parameters
import xiangshan._
import xiangshan.backend.vector.vagq._

class VAGQDownstreamAdapterIO(implicit p: Parameters) extends XSBundle {
  val vagqLsuReq  = Flipped(Vec(VAGQConstants.ActiveIssueWidth, Decoupled(new VAGQLsuReq)))
  val vagqLduResp = Vec(VAGQConstants.LduRespWidth, Valid(new VAGQResp))
  val vagqStaResp = Vec(VAGQConstants.StaRespWidth, Valid(new VAGQResp))

  val vagqLsqEmptyReq  = Flipped(Decoupled(new VAGQLsqEmptyReq))
  val vagqLsqEmptyResp = Valid(new VAGQLsqEmptyResp)

  val lsqEmptyReq  = Decoupled(new VAGQLsqEmptyReq)
  val lsqEmptyResp = Flipped(Valid(new VAGQLsqEmptyResp))
}

class VAGQDownstreamAdapter(implicit p: Parameters) extends XSModule {
  val io = IO(new VAGQDownstreamAdapterIO)

  // Active req to LDU/STA is intentionally left disconnected for now.
  io.vagqLsuReq.foreach(_.ready := false.B)
  io.vagqLduResp.foreach { resp =>
    resp.valid := false.B
    resp.bits  := 0.U.asTypeOf(resp.bits)
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
