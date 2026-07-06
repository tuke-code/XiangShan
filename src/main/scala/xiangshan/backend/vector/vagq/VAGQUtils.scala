package xiangshan.backend.vector.vagq

import chisel3._
import chisel3.util._
import utility.HasCircularQueuePtrHelper
import xiangshan._
import xiangshan.mem.{genVSData, genVWdata}

trait HasVAGQHelper extends HasCircularQueuePtrHelper { this: HasVAGQParameters =>
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

  protected def lowBit(mask: UInt): UInt = PriorityEncoder(mask)

  protected def highBit(mask: UInt): UInt = (vagqFlowBytes - 1).U - PriorityEncoder(Reverse(mask))

  protected def bitMask(idx: UInt): UInt = UIntToOH(idx, vagqFlowBytes)

  protected def storeFlowData(data: UInt, elemIdx: UInt, alignedType: UInt): UInt = {
    genVWdata(genVSData(data, elemIdx, alignedType), alignedType)
  }

  protected def byteMaskToEntryMask(mask: UInt, deew: UInt): UInt = {
    VecInit((0 until vagqFlowBytes).map { elem =>
      VecInit((0 until vagqFlowBytes).map { byte =>
        mask(byte) && ((byte.U(vagqFlowByteWidth.W) >> deew) === elem.U(vagqFlowByteWidth.W))
      }).asUInt.orR
    }).asUInt
  }

  protected def mergeEntryAt(entries: Vec[CtrlInput], idx: UInt, numEntries: Int): CtrlInput = {
    Mux1H(idxHitSeq(idx, numEntries), entries)
  }

  protected def oldestEntryOH(valids: Seq[Bool], entries: Vec[CtrlInput], numEntries: Int): UInt = {
    require(valids.length == numEntries)
    VecInit((0 until numEntries).map { i =>
      val thisEntry = entries(i).entry
      val olderThanAll = (0 until numEntries).map { j =>
        if (i == j) {
          true.B
        } else {
          val thatEntry = entries(j).entry
          val sameRob = thatEntry.robIdx === thisEntry.robIdx
          val thisOlderSameRob = sameRob && (
            thisEntry.uopIdx < thatEntry.uopIdx ||
              (thisEntry.uopIdx === thatEntry.uopIdx && (i < j).B)
          )
          !valids(j) || isAfter(thatEntry.robIdx, thisEntry.robIdx) || thisOlderSameRob
        }
      }.reduce(_ && _)
      valids(i) && olderThanAll
    }).asUInt
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

  protected def enterSplit(entry: VAGQEntryMeta): Unit = {
    entry.state   := VAGQEntryState.split
    entry.reqSent := 0.U
    entry.reqAck  := 0.U
    entry.exceptionNumber := 0.U
    entry.faultElemIdx    := 0.U
  }

  protected def initPending(entry: VAGQEntryMeta, stateNext: UInt): Unit = {
    entry.valid := true.B
    entry.state := stateNext
    entry.reqSent := 0.U
    entry.reqAck  := 0.U
    entry.exceptionNumber := 0.U
    entry.faultElemIdx    := 0.U
  }
}
