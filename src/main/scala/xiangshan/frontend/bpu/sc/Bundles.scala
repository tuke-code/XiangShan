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

package xiangshan.frontend.bpu.sc

import chisel3._
import chisel3.util._
import org.chipsalliance.cde.config.Parameters
import xiangshan.XSCoreParamsKey
import xiangshan.frontend.PrunedAddr
import xiangshan.frontend.bpu.SaturateCounter
import xiangshan.frontend.bpu.SaturateCounterFactory
import xiangshan.frontend.bpu.SaturateCounterInit
import xiangshan.frontend.bpu.SignedSaturateCounter
import xiangshan.frontend.bpu.SignedSaturateCounterFactory
import xiangshan.frontend.bpu.WriteReqBundle
import xiangshan.frontend.bpu.history.commonhr.CommonHREntry

object Counter extends SignedSaturateCounterFactory {
  def width(implicit p: Parameters): Int =
    p(XSCoreParamsKey).frontendParameters.bpuParameters.scParameters.CtrWidth
}

object ThresholdCounter extends SaturateCounterFactory {
  def width(implicit p: Parameters): Int =
    p(XSCoreParamsKey).frontendParameters.bpuParameters.scParameters.ThresholdWidth

  def Init(implicit p: Parameters): SaturateCounter =
    SaturateCounterInit(width, p(XSCoreParamsKey).frontendParameters.bpuParameters.scParameters.ThresholdInit)
}

class ScEntry(implicit p: Parameters) extends ScBundle {
  val ctr: SignedSaturateCounter = Counter()
}

class ScTableSramWriteReq(val numSets: Int, val numWays: Int)(implicit p: Parameters) extends WriteReqBundle
    with HasScParameters {
  val setIdx:           UInt              = UInt(log2Ceil(numSets).W)
  override val wayMask: Option[Vec[Bool]] = Some(Vec(numWays, Bool()))
  override val wayData: Option[Vec[UInt]] = Some(Vec(numWays, UInt((new ScEntry).getWidth.W)))

}

class ScTableReq(val numSets: Int, val numWays: Int)(implicit p: Parameters) extends ScBundle {
  val setIdx:   UInt = UInt(log2Ceil(numSets).W)
  val bankMask: UInt = UInt(NumBanks.W)
}

class ScTableTrain(val numSets: Int, val numWays: Int)(implicit p: Parameters) extends ScBundle {
  val valid:    Bool         = Bool()
  val setIdx:   UInt         = UInt(log2Ceil(numSets).W)
  val bankMask: UInt         = UInt(NumBanks.W)
  val wayMask:  Vec[Bool]    = Vec(numWays, Bool())
  val entryVec: Vec[ScEntry] = Vec(numWays, new ScEntry())
}

class ScMetaEntry(implicit p: Parameters) extends ScBundle with HasScParameters {
  val scBiasLowerBits: UInt = UInt(BiasUseTageBitWidth.W)
  val scPred:          Bool = Bool()
  val tagePred:        Bool = Bool()
  val tageCtr:         UInt = UInt(TageTakenCtrWidth.W)
  val tagePredValid:   Bool = Bool()
  val useScPred:       Bool = Bool()
  val sumAboveThres:   Bool = Bool()

  val debug_scPathTaken:   Bool = Bool()
  val debug_scGlobalTaken: Bool = Bool()
  val debug_scBWTaken:     Bool = Bool()
  val debug_scImliTaken:   Bool = Bool()
  val debug_scBiasTaken:   Bool = Bool()
}

class ScMeta(implicit p: Parameters) extends ScBundle with HasScParameters {
  // NOTE: Seems ChiselDB has problem dealing with SInt, so we do not use ScEntry for scResp here
  // FIXME: is there a better way to do this?
  private def ScEntryWidth = (new ScEntry).getWidth
  val scBiasLowerBits: Vec[UInt] = Vec(NumWays, UInt(BiasUseTageBitWidth.W))
  val scPred:          Vec[Bool] = Vec(NumWays, Bool())
  val tagePred:        Vec[Bool] = Vec(NumBtbResultEntries, Bool())
  val tageCtr:         Vec[UInt] = Vec(NumBtbResultEntries, UInt(TageTakenCtrWidth.W))
  val tagePredValid:   Vec[Bool] = Vec(NumBtbResultEntries, Bool())
  val useScPred:       Vec[Bool] = Vec(NumWays, Bool())
  val sumAboveThres:   Vec[Bool] = Vec(NumWays, Bool())

  // for debug
  val debug_scPathTakenVec:   Option[Vec[Bool]] = Some(Vec(NumWays, Bool()))
  val debug_scGlobalTakenVec: Option[Vec[Bool]] = Some(Vec(NumWays, Bool()))
  val debug_scBWTakenVec:     Option[Vec[Bool]] = Some(Vec(NumWays, Bool()))
  val debug_scImliTakenVec:   Option[Vec[Bool]] = Some(Vec(NumWays, Bool()))
  val debug_scBiasTakenVec:   Option[Vec[Bool]] = Some(Vec(NumWays, Bool()))
  val debug_predPathIdx: Option[MixedVec[UInt]] =
    Some(MixedVec(PathTableInfos.map(info => UInt(log2Ceil(info.NumSets).W))))
  val debug_predGlobalIdx: Option[MixedVec[UInt]] =
    Some(MixedVec(GlobalTableInfos.map(info => UInt(log2Ceil(info.NumSets).W))))
  val debug_predBWIdx: Option[MixedVec[UInt]] =
    Some(MixedVec(BackwardTableInfos.map(info => UInt(log2Ceil(info.NumSets).W))))
  val debug_predImliIdx: Option[UInt] = Some(UInt(log2Ceil(ImliTableInfo.NumSets).W))
  val debug_predBiasIdx: Option[UInt] = Some(UInt(log2Ceil(BiasTableInfo.NumSets).W))

  def toEntries(implicit p: Parameters): Vec[ScMetaEntry] = {
    require(NumBtbResultEntries == NumWays)

    VecInit.tabulate(NumBtbResultEntries) { i =>
      val entry = Wire(new ScMetaEntry)

      entry.scBiasLowerBits := scBiasLowerBits(i)
      entry.scPred          := scPred(i)
      entry.tagePred        := tagePred(i)
      entry.tageCtr         := tageCtr(i)
      entry.tagePredValid   := tagePredValid(i)
      entry.useScPred       := useScPred(i)
      entry.sumAboveThres   := sumAboveThres(i)

      entry.debug_scPathTaken   := debug_scPathTakenVec.get(i)
      entry.debug_scGlobalTaken := debug_scGlobalTakenVec.get(i)
      entry.debug_scBWTaken     := debug_scBWTakenVec.get(i)
      entry.debug_scImliTaken   := debug_scImliTakenVec.get(i)
      entry.debug_scBiasTaken   := debug_scBiasTakenVec.get(i)

      entry
    }
  }

  def fromEntries(entries: Seq[ScMetaEntry])(implicit p: Parameters): Unit = {
    require(NumBtbResultEntries == NumWays)

    scBiasLowerBits := VecInit(entries.map(_.scBiasLowerBits))
    scPred          := VecInit(entries.map(_.scPred))
    tagePred        := VecInit(entries.map(_.tagePred))
    tageCtr         := VecInit(entries.map(_.tageCtr))
    tagePredValid   := VecInit(entries.map(_.tagePredValid))
    useScPred       := VecInit(entries.map(_.useScPred))
    sumAboveThres   := VecInit(entries.map(_.sumAboveThres))

    debug_scPathTakenVec.get   := VecInit(entries.map(_.debug_scPathTaken))
    debug_scGlobalTakenVec.get := VecInit(entries.map(_.debug_scGlobalTaken))
    debug_scBWTakenVec.get     := VecInit(entries.map(_.debug_scBWTaken))
    debug_scImliTakenVec.get   := VecInit(entries.map(_.debug_scImliTaken))
    debug_scBiasTakenVec.get   := VecInit(entries.map(_.debug_scBiasTaken))
  }
}

class ScConditionalBranchTrace(implicit p: Parameters) extends ScBundle with HasScParameters {
  private def ScEntryWidth = (new ScEntry).getWidth
  val startPc: PrunedAddr = PrunedAddr(VAddrBits)
  val cfiPc:   UInt       = UInt(VAddrBits.W)
  // tage provider info
  val providerValid: Bool = Bool()
  val providerTaken: Bool = Bool()
  val providerCtr:   UInt = UInt(TageTakenCtrWidth.W)
  // sc resp
  val pathResp:   Vec[UInt] = Vec(NumPathTables, UInt(ScEntryWidth.W))
  val globalResp: Vec[UInt] = Vec(NumGlobalTables, UInt(ScEntryWidth.W))
  val biasResp:   UInt      = UInt(ScEntryWidth.W)
  // sc pred
  val sumAboveThres: Bool = Bool()
  val scPred:        Bool = Bool()
  val useSc:         Bool = Bool()

  // actual
  val actualTaken: Bool = Bool()
  val mispredict:  Bool = Bool()

  val scCorrectTageWrong:   Bool = Bool()
  val scWrongTageCorrect:   Bool = Bool()
  val scCorrectTageCorrect: Bool = Bool()
  val scWrongTageWrong:     Bool = Bool()
  val scWrong:              Bool = Bool()
  val scCorrect:            Bool = Bool()
}
