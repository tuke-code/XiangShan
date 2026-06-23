package xiangshan.backend.vector.vagq

import chisel3._
import chisel3.util._
import org.chipsalliance.cde.config.Parameters
import utility._
import xiangshan._
import xiangshan.backend.Bundles._

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

abstract class VAGQModule(implicit p: Parameters) extends XSModule with HasVAGQParameters


