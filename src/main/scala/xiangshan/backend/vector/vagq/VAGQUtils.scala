package xiangshan.backend.vector.vagq

import chisel3._
import chisel3.util._
import xiangshan._

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

  protected def idxHitSeq(idx: UInt, numEntries: Int): Seq[Bool] = {
    (0 until numEntries).map(i => idx === i.U)
  }

  protected def respMatchesEntry(resp: VAGQResp, entries: Vec[CtrlInput], numEntries: Int): Bool = {
    val hit = idxHitSeq(resp.entryIdx, numEntries)
    VecInit(hit).asUInt.orR && Mux1H(hit, entries.map(x => x.entry.valid && x.entry.robIdx === resp.robIdx))
  }

  protected def entryAlive(entry: VAGQEntryMeta, redirect: ValidIO[Redirect]): Bool = {
    entry.valid && !entry.robIdx.needFlush(redirect)
  }

  protected def prefixMask(limit: UInt): UInt = {
    VecInit((0 until vagqFlowBytes).map(i => i.U < limit)).asUInt
  }

  protected def mergeEntryAt(entries: Vec[CtrlInput], idx: UInt, numEntries: Int): CtrlInput = {
    Mux1H(idxHitSeq(idx, numEntries), entries)
  }

  protected def mergeBytes(oldData: UInt, newData: UInt, mask: UInt): UInt = {
    VecInit((0 until vagqFlowBytes).map(i =>
      Mux(mask(i), newData(8 * (i + 1) - 1, 8 * i), oldData(8 * (i + 1) - 1, 8 * i))
    )).asUInt
  }

  protected def elemNum(deew: UInt): UInt = {
    MuxLookup(deew, 4.U(3.W))(Seq(
      0.U -> 4.U,
      1.U -> 3.U,
      2.U -> 2.U,
      3.U -> 1.U
    ))
  }

  protected def faultVstart(entry: VAGQEntryMeta): UInt = {
    val elemIdx = entry.faultElemIdx >> entry.deew
    ((entry.uopIdx << elemNum(entry.deew)) + elemIdx)(VAGQConstants.FaultVstartWidth - 1, 0)
  }
}
