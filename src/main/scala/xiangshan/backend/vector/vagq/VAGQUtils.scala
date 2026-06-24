package xiangshan.backend.vector.vagq

import chisel3._
import chisel3.util._


trait HasVAGQHelper { this: HasVAGQParameters =>
  protected def entryAt(entries: Vec[VAGQEntry], idx: UInt): VAGQEntry = {
    Mux1H((0 until vagqSize).map(i => (idx === i.U(vagqEntryIdxWidth.W)) -> entries(i)))
  }

  protected def idxValid(idx: UInt): Bool = idx < vagqSize.U

  protected def uopByteRangeLen(totalElem: UInt, deew: UInt, uopIdx: UInt): UInt = {
    val byteCountWidth = log2Up(VLEN) + 1 + 3
    val totalBytes = (Cat(0.U(3.W), totalElem) << deew)(byteCountWidth - 1, 0)
    val byteQuot = totalBytes(byteCountWidth - 1, vagqFlowByteWidth)
    val byteRem = totalBytes(vagqFlowByteWidth - 1, 0)

    Mux1H(Seq(
      (uopIdx < byteQuot)   -> vagqFlowBytes.U(vagqUvlByteWidth.W),
      (uopIdx === byteQuot) -> byteRem,
      (uopIdx > byteQuot)   -> 0.U(vagqUvlByteWidth.W)
    ))
  }

  protected def elemMaskBit(byteIdx: Int, deew: UInt, v0Mask: UInt): Bool = {
    MuxLookup(deew, v0Mask(byteIdx))(Seq(
      0.U -> v0Mask(byteIdx),
      1.U -> v0Mask(byteIdx / 2),
      2.U -> v0Mask(byteIdx / 4),
      3.U -> v0Mask(byteIdx / 8),
    ))
  }
}
