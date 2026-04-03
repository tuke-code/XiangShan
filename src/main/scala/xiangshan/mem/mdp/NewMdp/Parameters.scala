// Copyright (c) 2024-2025 Beijing Institute of Open Source Chip (BOSC)
// Copyright (c) 2020-2025 Institute of Computing Technology, Chinese Academy of Sciences
// Copyright (c) 2020-2021 Peng Cheng Laboratory
//
// XiangShan is licensed under Mulan PSL v2.
// You can use this software according to the terms and conditions of the Mulan PSL v2.
// You may obtain a copy of Mulan PSL v2 at:
//          https://license.coscl.org.cn/MulanPSL2
//
// THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
// EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
// MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
//
// See the Mulan PSL v2 for more details.

package xiangshan.mem.mdp.NewMdp

import chisel3._
import chisel3.util._
import utils._
import math.min
import xiangshan.frontend.PrunedAddr
import xiangshan.frontend.bpu.HalfAlignHelper
import xiangshan.frontend.bpu.HasBpuParameters
import xiangshan.backend.rob.RobPtr
import utility.HasCircularQueuePtrHelper
import freechips.rocketchip.util.SeqToAugmentedSeq
import xiangshan.frontend.bpu.FoldedHistoryInfo
import xiangshan.frontend.bpu.history.phr.PhrAllFoldedHistories

abstract class NamedTuple[T <: Product] {
  protected def asTuple: T

  // override equals and hashCode to allow comparison and Set[NamedTuple] de-duplication
  override def equals(obj: Any): Boolean =
    obj != null &&
      this.getClass == obj.getClass &&
      obj.asInstanceOf[NamedTuple[T]].asTuple == this.asTuple

  override def hashCode(): Int =
    asTuple.hashCode()

  override def toString: String =
    s"${this.getClass.getSimpleName}${asTuple.toString}"
}

class MdpTageTableInfo(
    val Size:          Int,
    val NumWays:       Int,
    val HistoryLength: Int
) extends NamedTuple[(Int, Int, Int)] {
  require(Size > 0, "Size must be > 0")
  require(NumWays > 0, "NumWays must be > 0")
  require(HistoryLength >= 0, "HistoryLength must be >= 0")

  def asTuple: (Int, Int, Int) =
    (Size, NumWays, HistoryLength)

  def getNumSets(numBanks: Int): Int = {
    require(numBanks > 0, "numBanks must be > 0")
    Size / NumWays / numBanks
  }

  def getFoldedHistoryInfoSet(numBanks: Int, tagWidth: Int): Set[FoldedHistoryInfo] = {
    require(numBanks > 0, "numBanks must be > 0")
    require(tagWidth > 0, "tagWidth must be > 0")
    if (HistoryLength > 0)
      Set( // FoldedHistoryInfo(unfolded history length, folded history length)
        new FoldedHistoryInfo(HistoryLength, min(HistoryLength, log2Ceil(getNumSets(numBanks)))),
        new FoldedHistoryInfo(HistoryLength, min(HistoryLength, tagWidth)),
        new FoldedHistoryInfo(HistoryLength, min(HistoryLength, tagWidth - 1))
      )
    else
      Set[FoldedHistoryInfo]()
  }

  def getTageFoldedHistoryInfo(numBanks: Int, tagWidth: Int): List[FoldedHistoryInfo] = {
    require(numBanks > 0, "numBanks must be > 0")
    require(tagWidth > 0, "tagWidth must be > 0")
    if (HistoryLength > 0)
      List( // FoldedHistoryInfo(unfolded history length, folded history length)
        new FoldedHistoryInfo(HistoryLength, min(HistoryLength, log2Ceil(getNumSets(numBanks)))),
        new FoldedHistoryInfo(HistoryLength, min(HistoryLength, tagWidth)),
        new FoldedHistoryInfo(HistoryLength, min(HistoryLength, tagWidth - 1))
      )
    else
      List[FoldedHistoryInfo]()
  }
}

trait TopHelper extends HasMdpTageTableParameters {
  def getFoldedHist(allFoldedPathHist: PhrAllFoldedHistories): Vec[MdpTageFoldedHist] =
    VecInit(TableInfos.map { implicit tableInfo =>
      val mdpTageFoldedHist = tableInfo.getTageFoldedHistoryInfo(NumBanks, TagWidth).map { histInfo =>
        allFoldedPathHist.getHistWithInfo(histInfo).foldedHist
      }
      val foldedHist = Wire(new MdpTageFoldedHist)
      foldedHist.forIdx := mdpTageFoldedHist.head
      foldedHist.forTag := mdpTageFoldedHist(1) ^ Cat(mdpTageFoldedHist(2), 0.U(1.W))
      foldedHist
    })
  def getLongestHistTableOH(hitTableMask: Seq[Bool]): Seq[Bool] =
    PriorityEncoderOH(hitTableMask.reverse).reverse

  def getLongestHistTableIdx(hitTableMask: Seq[Bool]): UInt =
    OHToUInt(PriorityEncoderOH(hitTableMask.reverse).reverse)

  def getFirstNonFullTableOH(longestHistTableOH: Seq[Bool], wayFullMask: Seq[Bool]): (UInt, Bool, Bool) = {
    val longerMask = Wire(UInt(longestHistTableOH.length.W))
    when (longestHistTableOH.asUInt.orR) {
      longerMask := ~(longestHistTableOH.asUInt - 1.U) << 1.U
    } .otherwise {
      longerMask := ~0.U(longestHistTableOH.length.W)
    }
    val candidateMask = longerMask & (~wayFullMask.asUInt) 
    val allocateOH = Mux(
      candidateMask.orR,
      PriorityEncoderOH(candidateMask),
      0.U //未定义行为，不分配
    ) 
    val canAllocate = candidateMask.orR
    val nextTableOH = Mux(
      longestHistTableOH.asUInt.orR,
      longestHistTableOH.asUInt << 1.U,
      0.U(longestHistTableOH.length.W)
    )
    val nextTableAllocatable = (nextTableOH & (~wayFullMask.asUInt)).orR
    (allocateOH, canAllocate, nextTableAllocatable)
  }
}

trait TableHelper extends TopHelper { // extends TopHelper for getBankIndex
  // varies between different tables
  implicit val info: MdpTageTableInfo

  val addrFields = AddrField(
    Seq(
      ("instOffset", instOffsetBits),
      ("bankIdx", BankIdxWidth),
      ("setIdx", SetIdxWidth),
      ("tag", TagWidth)
    ),
    maxWidth = Option(VAddrBits)
  )

  def getBankIndex(pc: PrunedAddr): UInt =
    addrFields.extract("bankIdx", pc)

  def getSetIndex(pc: PrunedAddr, hist: UInt): UInt =
    addrFields.extract("setIdx", pc) ^ hist

  def getRawTag(pc: PrunedAddr, hist: UInt): UInt =
    addrFields.extract("tag", pc) ^ hist
}


case class MdpTageTableParameters(
  TableInfos: Seq[MdpTageTableInfo] = Seq(
    new MdpTageTableInfo(512, 4, 2  ),
    new MdpTageTableInfo(512, 4, 4  ),
    new MdpTageTableInfo(512, 4, 8  ),
    new MdpTageTableInfo(512, 4, 16 ),
    new MdpTageTableInfo(256, 4, 32 ),
    new MdpTageTableInfo(128, 4, 64 ),
    new MdpTageTableInfo(128, 4, 128)
  ),
  NumBanks:            Int = 4, // to alleviate read-write conflicts in single-port SRAM
  NumWays:             Int = 4,
  TagWidth:            Int = 13,
  WriteBufferSize:     Int = 8,

  TakenCtrWidth:       Int = 3,
  UsefulCtrWidth:      Int = 3,
  UsefulCtrInitValue:  Int = 0,
){}

case class MdpBaseTableParameters( //fromMainBtb
  NumEntries: Int = 1024,
  NumWay:     Int = 4,
  // Lowest level banks, each bank is a physical SRAM
  // This banking is used to resolve read-write conflicts and reduce SRAM power
  NumInternalBanks: Int = 4,
  // Highest level banks
  // This banking is used to resolve the alignement restriction of the BTB
  // When using align banking, the BTB can provide at most banks - 1 / banks * predict width wide prediction
  NumAlignBanks:   Int = 2,
  TagWidth:        Int = 16,
  TargetWidth:     Int = 20,       // 2B aligned
  WriteBufferSize: Int = 8,
  Replacer:        String = "Lru", // "Lru" or "Plru"
  // Mbtb write trace
){}

trait HasMdpParameters extends HasBpuParameters{
  def mdpTageTableParameters: MdpTageTableParameters = bpuParameters.mdpTageTableParameters
  def mdpBaseTableParameters: MdpBaseTableParameters = bpuParameters.mdpBaseTableParameters
  def NumMdpResultEntries: Int = mdpBaseTableParameters.NumWay * mdpBaseTableParameters.NumAlignBanks
  //fromBpu
  def Shamt = bpuParameters.phrParameters.Shamt
  //
  def FetchMdpWidth: Int = 8
  //train
  def MdpResolveQueueSize: Int = 16
  def RobDistance: Int = (new RobPtr()).PTR_WIDTH
  def ResolveEntryLoadNumbers: Int = mdpBaseTableParameters.NumWay * mdpBaseTableParameters.NumAlignBanks
}

trait HasMdpTageTableParameters extends HasMdpParameters {
  def TableInfos: Seq[MdpTageTableInfo] = mdpTageTableParameters.TableInfos
  def NumBanks:           Int = mdpTageTableParameters.NumBanks
  def NumSets(implicit info:     MdpTageTableInfo): Int = info.getNumSets(NumBanks)
  def SetIdxWidth(implicit info: MdpTageTableInfo): Int = log2Ceil(NumSets)
  def NumWays(implicit info:     MdpTageTableInfo): Int = info.NumWays
  def WayIdxWidth(implicit info: MdpTageTableInfo): Int = log2Ceil(NumWays)
  def BankIdxWidth:       Int = log2Ceil(NumBanks)
  def TagWidth:           Int = mdpTageTableParameters.TagWidth
  def TakenCtrWidth:      Int = mdpTageTableParameters.TakenCtrWidth
  def UsefulCtrWidth:     Int = mdpTageTableParameters.UsefulCtrWidth
  def UsefulCtrInitValue: Int = mdpTageTableParameters.UsefulCtrInitValue
  def WriteBufferSize:    Int = mdpTageTableParameters.WriteBufferSize

  def NumTables:     Int = TableInfos.length
  def TableIdxWidth: Int = log2Ceil(NumTables)

  def MaxNumSets:     Int = TableInfos.map(_.getNumSets(NumBanks)).max
  def MaxSetIdxWidth: Int = log2Ceil(MaxNumSets)

  def MaxNumWays:     Int = TableInfos.map(_.NumWays).max
  def MaxWayIdxWidth: Int = log2Ceil(MaxNumWays)
}


trait HasMdpBaseTableParameters extends HasMdpParameters{
  def BaseNumEntries:       Int = mdpBaseTableParameters.NumEntries
  def BaseNumWays:          Int = mdpBaseTableParameters.NumWay
  def BaseNumInternalBanks: Int = mdpBaseTableParameters.NumInternalBanks
  def BaseNumAlignBanks:    Int = FetchBlockSize / FetchBlockAlignSize
  // NumSets is the number of sets in one bank, a bank corresponds to a physical SRAM
  def BaseNumSets:            Int    = BaseNumEntries / BaseNumWays / BaseNumInternalBanks / BaseNumAlignBanks
  def BaseTagWidth:           Int    = mdpBaseTableParameters.TagWidth
  def BaseTargetWidth:        Int    = mdpBaseTableParameters.TargetWidth
  def BaseSetIdxLen:          Int    = log2Ceil(BaseNumSets)
  def BaseInternalBankIdxLen: Int    = log2Ceil(BaseNumInternalBanks)
  def BaseAlignBankIdxLen:    Int    = log2Ceil(BaseNumAlignBanks)
  def BaseWriteBufferSize:    Int    = mdpBaseTableParameters.WriteBufferSize
  def BaseReplacer:           String = mdpBaseTableParameters.Replacer
}

object HasNewMdp {
  val Enable = true
}

object MdpPredictStatuses {
  val NULL     = 0.U
  val INDEPEND = 1.U //predict dependence,real independence
  val DEPEND   = 2.U //predict dependence,real dependence
  val DEPENDOT = 3.U //predict dependence,real dependence other addr

  val width = 2 
}

object MdpUpdateType{
  val width = 3
  val NULL = 0.U(width.W)
  val M_WZ = 1.U(width.W) //WriteZero
  val M_AW = 2.U(width.W) //AllocateWeak Ni+1 & Weak Ni
  val M_AS = 3.U(width.W) //AllocateStrong Ni+1 & Weak Ni
  val M_IS = 4.U(width.W) //Ni Strong
  val M_IW = 5.U(width.W) //Ni Weak
}
