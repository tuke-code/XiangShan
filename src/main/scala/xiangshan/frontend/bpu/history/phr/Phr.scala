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

package xiangshan.frontend.bpu.history.phr

import chisel3._
import chisel3.util._
import org.chipsalliance.cde.config.Parameters
import utility.XSError
import utility.XSPerfAccumulate
import utility.XSWarn
import xiangshan.frontend.PrunedAddr
import xiangshan.frontend.bpu.BpuTrain

// PHR: Predicted History Register
class Phr(implicit p: Parameters) extends PhrModule with HasPhrParameters with Helpers {
  class PhrIO(implicit p: Parameters) extends PhrBundle with HasPhrParameters {
    val s0_foldedPhr:   PhrAllFoldedHistories = Output(new PhrAllFoldedHistories(AllFoldedHistoryInfo))
    val s1_foldedPhr:   PhrAllFoldedHistories = Output(new PhrAllFoldedHistories(AllFoldedHistoryInfo))
    val s2_foldedPhr:   PhrAllFoldedHistories = Output(new PhrAllFoldedHistories(AllFoldedHistoryInfo))
    val s3_foldedPhr:   PhrAllFoldedHistories = Output(new PhrAllFoldedHistories(AllFoldedHistoryInfo))
    val phr:            Vec[Bool]             = Output(Vec(PhrHistoryLength, Bool()))
    val phrMeta:        PhrMeta               = Output(new PhrMeta)
    val train:          PhrUpdate             = Input(new PhrUpdate)       // redirect from backend
    val s1Train:        S1Train               = Input(new S1Train)
    val s3Train:        S3Train               = Input(new S3Train)
    val commit:         Valid[BpuTrain]       = Input(Valid(new BpuTrain)) // train bp data from resolve
    val trainFoldedPhr: PhrAllFoldedHistories = Output(new PhrAllFoldedHistories(AllFoldedHistoryInfo))
  }
  val io: PhrIO = IO(new PhrIO)

  private val phr    = RegInit(0.U.asTypeOf(Vec(PhrHistoryLength, Bool())))
  private val phrPtr = RegInit(0.U.asTypeOf(new PhrPtr))

  private def getPhr(ptr: PhrPtr): UInt =
    (Cat(phr.asUInt, phr.asUInt) >> (ptr.value + 1.U))(PhrHistoryLength - 1, 0)

  private def getRedirectPhr(phrMeta: PhrMeta): UInt = {
    val redirectErrorPhr = getPhr(phrMeta.phrPtr)
    Cat(redirectErrorPhr(PhrHistoryLength - 1, PathHashHighWidth), phrMeta.phrLowBits)
  }

  /*
   * PHR train from redirect/s1_prediction/s3_prediction
   */

  private val s0_stall = io.train.s0_stall
  private val s1_valid = io.s1Train.valid
  private val s0_fire  = io.train.stageCtrl.s0_fire
  private val s1_fire  = io.train.stageCtrl.s1_fire
  private val s2_fire  = io.train.stageCtrl.s2_fire
  private val s3_fire  = io.train.stageCtrl.s3_fire

  private val histFoldedPhr = WireInit(0.U.asTypeOf(new PhrAllFoldedHistories(AllFoldedHistoryInfo))) // for diff
  private val s0_foldedPhr  = WireInit(0.U.asTypeOf(new PhrAllFoldedHistories(AllFoldedHistoryInfo)))
  private val s0_foldedPhrReg =
    RegEnable(s0_foldedPhr, 0.U.asTypeOf(new PhrAllFoldedHistories(AllFoldedHistoryInfo)), !s0_stall)
  private val s1_foldedPhrReg =
    RegEnable(s0_foldedPhr, 0.U.asTypeOf(new PhrAllFoldedHistories(AllFoldedHistoryInfo)), s0_fire)
  private val s2_foldedPhrReg =
    RegEnable(s1_foldedPhrReg, 0.U.asTypeOf(new PhrAllFoldedHistories(AllFoldedHistoryInfo)), s1_fire)
  private val s3_foldedPhrReg =
    RegEnable(s2_foldedPhrReg, 0.U.asTypeOf(new PhrAllFoldedHistories(AllFoldedHistoryInfo)), s2_fire)

  private val s0_phrPtr  = WireInit(0.U.asTypeOf(new PhrPtr))
  private val s1_phrPtr  = RegEnable(s0_phrPtr, 0.U.asTypeOf(new PhrPtr), s0_fire)
  private val s1_phrMeta = WireInit(0.U.asTypeOf(new PhrMeta))
  s1_phrMeta.phrPtr     := s1_phrPtr
  s1_phrMeta.phrLowBits := getPhr(s1_phrPtr)(PathHashHighWidth - 1, 0)
  s1_phrMeta.predFoldedHist.foreach(_ := s1_foldedPhrReg)
  private val s2_phrMeta = RegEnable(s1_phrMeta, 0.U.asTypeOf(new PhrMeta), s1_fire)
  private val s3_phrMeta = RegEnable(s2_phrMeta, 0.U.asTypeOf(new PhrMeta), s2_fire)

  private val s1_phrValue = getPhr(s1_phrPtr)
  private val phrValue    = getPhr(phrPtr)

  private val s1AbtbUpdateData   = VecInit.fill(NumAheadBtbPredictionEntries)(0.U.asTypeOf(new PhrUpdateData))
  private val s1UbtbUpdateData   = WireInit(0.U.asTypeOf(new PhrUpdateData))
  private val redirectData       = WireInit(0.U.asTypeOf(new PhrUpdateData))
  private val s3_override        = WireInit(false.B)
  private val s3MbtbUpdateData   = VecInit.fill(NumBtbResultEntries)(0.U.asTypeOf(new PhrUpdateData))
  private val s3IttageUpdateData = VecInit.fill(NumBtbResultEntries)(0.U.asTypeOf(new PhrUpdateData))
  private val s3RasUpdateData    = VecInit.fill(NumBtbResultEntries)(0.U.asTypeOf(new PhrUpdateData))

  private val redirectS0PhrPtr     = WireInit(0.U.asTypeOf(new PhrPtr))
  private val redirectS0PhrLowBits = WireInit(0.U(PathHashHighWidth.W))
  private val redirectS0FoldedPhr  = WireInit(0.U.asTypeOf(new PhrAllFoldedHistories(AllFoldedHistoryInfo)))
  private val redirectUpdate       = WireInit(0.U.asTypeOf(new PhrUpdateResult))
  private val s3S0PhrPtr           = WireInit(0.U.asTypeOf(new PhrPtr))
  private val s3S0FoldedPhr        = WireInit(0.U.asTypeOf(new PhrAllFoldedHistories(AllFoldedHistoryInfo)))
  private val s3Update             = WireInit(0.U.asTypeOf(new PhrUpdateResult))
  private val s1S0PhrPtr           = WireInit(0.U.asTypeOf(new PhrPtr))
  private val s1S0PhrLowBits       = WireInit(0.U(PathHashHighWidth.W))
  private val s1S0FoldedPhr        = WireInit(0.U.asTypeOf(new PhrAllFoldedHistories(AllFoldedHistoryInfo)))
  private val s1Update             = WireInit(0.U.asTypeOf(new PhrUpdateResult))
  private val redirectPhr          = WireInit(0.U(PhrHistoryLength.W))

  // Organize the input data into the structure required for PHR updates

  redirectData.valid   := io.train.redirect.valid
  redirectData.taken   := io.train.redirect.bits.taken
  redirectData.cfiPc   := io.train.redirect.bits.cfiPc
  redirectData.target  := io.train.redirect.bits.target
  redirectData.phrMeta := io.train.redirect.bits.meta.phr

  s3_override := io.s3Train.valid
  private val s3RasTarget    = io.s3Train.rasTarget
  private val s3IttageTarget = io.s3Train.ittageTarget
  private val s3IttageHit    = io.s3Train.ittageHit
  private val s3UseRas       = VecInit(io.s3Train.s3Prediction.map(pred => pred.valid && pred.bits.attribute.isReturn))
  private val s3UseIttage =
    VecInit(io.s3Train.s3Prediction.map(pred => pred.valid && pred.bits.attribute.needIttage && s3IttageHit))
  s3MbtbUpdateData.zip(s3IttageUpdateData).zip(s3RasUpdateData).zip(
    io.s3Train.s3Prediction.zip(io.s3Train.cfiPc)
  ).foreach {
    case (((mbtbData, ittageData), rasData), (pred, cfiPc)) =>
      mbtbData.valid   := pred.valid
      mbtbData.taken   := pred.valid
      mbtbData.cfiPc   := cfiPc
      mbtbData.target  := pred.bits.target
      mbtbData.phrMeta := io.s3Train.phrMetaDup(0)

      ittageData.valid   := pred.valid
      ittageData.taken   := pred.valid
      ittageData.cfiPc   := cfiPc
      ittageData.target  := s3IttageTarget
      ittageData.phrMeta := io.s3Train.phrMetaDup(1)

      rasData.valid   := pred.valid
      rasData.taken   := pred.valid
      rasData.cfiPc   := cfiPc
      rasData.target  := s3RasTarget
      rasData.phrMeta := io.s3Train.phrMetaDup(2)
  }

  s1AbtbUpdateData.zip(io.s1Train.abtbPrediction).foreach { case (data, pred) =>
    data.valid := s1_valid && pred.valid
    // Since S1 does not require recovery, and because the "taken" signal provided by abtbPrediction may not reflect the actual branch outcome,
    // we always assume "taken" when computing s1NextPhr and s1NextFoldedPhr.
    // If the branch is actually not taken, we simply leave Phr unchanged
    data.taken              := s1_valid && pred.valid
    data.cfiPc              := getCfiPcFromPosition(io.s1Train.startPc, pred.bits.cfiPosition)
    data.target             := pred.bits.target
    data.phrMeta.phrPtr     := s1_phrPtr
    data.phrMeta.phrLowBits := s1_phrValue(PathHashHighWidth - 1, 0)
  }

  s1UbtbUpdateData.valid  := s1_valid
  s1UbtbUpdateData.taken  := io.s1Train.ubtbPrediction.bits.taken
  s1UbtbUpdateData.cfiPc  := getCfiPcFromPosition(io.s1Train.startPc, io.s1Train.ubtbPrediction.bits.cfiPosition)
  s1UbtbUpdateData.target := io.s1Train.ubtbPrediction.bits.target
  s1UbtbUpdateData.phrMeta.phrPtr     := s1_phrPtr
  s1UbtbUpdateData.phrMeta.phrLowBits := s1_phrValue(PathHashHighWidth - 1, 0)

  // Compute all ShiftBits values and the high bits of the hash
  private val redirectHashComponents = getPathHashComponents(redirectData.cfiPc, redirectData.target)
  private val s3MbtbHashComponents   = s3MbtbUpdateData.map(data => getPathHashComponents(data.cfiPc, data.target))
  private val s3IttageHashComponents = s3IttageUpdateData.map(data => getPathHashComponents(data.cfiPc, data.target))
  private val s3RasHashComponents    = s3RasUpdateData.map(data => getPathHashComponents(data.cfiPc, data.target))
  private val s1UbtbHashComponents   = getPathHashComponents(s1UbtbUpdateData.cfiPc, s1UbtbUpdateData.target)
  private val s1AbtbHashComponents   = s1AbtbUpdateData.map(data => getPathHashComponents(data.cfiPc, data.target))

  private val redirectShiftBits = redirectHashComponents._1
  private val redirectHashHigh  = redirectHashComponents._2

  // Compute all phrPtr and phrLowBits for updates
  redirectUpdate := getUpdatePtrs(redirectData, redirectHashHigh)
  private val s3MbtbUpdateCandidates = s3MbtbUpdateData.zip(s3MbtbHashComponents).map { case (data, hash) =>
    getUpdatePtrs(data, hash._2)
  }
  private val s3IttageUpdateCandidates = s3IttageUpdateData.zip(s3IttageHashComponents).map { case (data, hash) =>
    getUpdatePtrs(data, hash._2)
  }
  private val s3RasUpdateCandidates = s3RasUpdateData.zip(s3RasHashComponents).map { case (data, hash) =>
    getUpdatePtrs(data, hash._2)
  }
  private val s3UpdateCandidates: Seq[PhrUpdateResult] = s3MbtbUpdateCandidates.indices.map { i =>
    MuxCase(
      s3MbtbUpdateCandidates(i),
      Seq(
        s3UseRas(i)    -> s3RasUpdateCandidates(i),
        s3UseIttage(i) -> s3IttageUpdateCandidates(i)
      )
    )
  }
  private val s3RecoverResult = WireInit(0.U.asTypeOf(new PhrUpdateResult))
  s3RecoverResult.phrPtr     := io.s3Train.phrMetaDup(0).phrPtr
  s3RecoverResult.phrLowBits := io.s3Train.phrMetaDup(0).phrLowBits

  s3Update := Mux(io.s3Train.taken, Mux1H(io.s3Train.firstTakenBrOH, s3UpdateCandidates), s3RecoverResult)

  private val s1UbtbUpdate = getUpdatePtrs(s1UbtbUpdateData, s1UbtbHashComponents._2)
  private val s1AbtbUpdateCandidates = s1AbtbUpdateData.zip(s1AbtbHashComponents).map { case (data, hash) =>
    getUpdatePtrs(data, hash._2)
  }
  private val s1AbtbUpdateOH = Mux1H(io.s1Train.abtbFirstTakenBrOH, s1AbtbUpdateCandidates)

  s1Update := Mux(io.s1Train.abtbValid, s1AbtbUpdateOH, s1UbtbUpdate)
  private val s1AbtbShiftBits = Mux1H(io.s1Train.abtbFirstTakenBrOH, s1AbtbHashComponents.map(_._1))
  private val s1ShiftBits     = Mux(io.s1Train.abtbValid, s1AbtbShiftBits, s1UbtbHashComponents._1)

  redirectS0PhrPtr     := redirectUpdate.phrPtr
  redirectS0PhrLowBits := redirectUpdate.phrLowBits
  s3S0PhrPtr           := s3Update.phrPtr
  s1S0PhrPtr     := Mux(io.s1Train.taken, s1Update.phrPtr, s1_phrPtr)
  s1S0PhrLowBits := Mux(io.s1Train.taken, s1Update.phrLowBits, s1_phrValue(PathHashHighWidth - 1, 0))

  phrPtr := MuxCase(
    phrPtr,
    Seq(
      redirectData.valid -> redirectS0PhrPtr,
      s3_override        -> s3S0PhrPtr,
      s1_valid           -> s1S0PhrPtr
    )
  )
  s0_phrPtr := MuxCase(
    phrPtr,
    Seq(
      redirectData.valid -> redirectS0PhrPtr,
      s3_override        -> s3S0PhrPtr,
      s1_valid           -> s1S0PhrPtr
    )
  )
  redirectPhr := getRedirectPhr(redirectData.phrMeta)
  private val redirectTakenPhr = Cat(
    Cat(redirectPhr(PhrHistoryLength - 1, PathHashHighWidth), redirectHashHigh ^ redirectData.phrMeta.phrLowBits),
    redirectShiftBits
  )(PhrHistoryLength - 1, 0)

  redirectS0FoldedPhr := Mux(
    redirectData.valid && redirectData.taken,
    computeAllFoldedPhr(redirectTakenPhr),
    computeAllFoldedPhr(redirectPhr)
  )

  private val s2_oldestBits = Wire(new PhrAllFoldedHistoryOldestBits(AllFoldedHistoryInfo))
  private val s2_phr        = getRedirectPhr(s2_phrMeta)
  s2_oldestBits.read(VecInit(s2_phr.asBools), s2_phrMeta.phrPtr)
  private val s3_oldestBits =
    RegEnable(s2_oldestBits, 0.U.asTypeOf(new PhrAllFoldedHistoryOldestBits(AllFoldedHistoryInfo)), s2_fire)

  XSError(s3_fire && s3_phrMeta.asUInt =/= io.s3Train.phrMetaDup(1).asUInt, f"s3_phrMeta mismatch!\n")

  private val s3MbtbS0FoldedPhrCandidates: Seq[PhrAllFoldedHistories] =
    s3MbtbUpdateData.zip(s3MbtbHashComponents).map { case (data, hash) =>
      getNextFoldedPhr(data, s3_oldestBits, s3_foldedPhrReg, hash._2, hash._1)
    }
  private val s3IttageS0FoldedPhrCandidates: Seq[PhrAllFoldedHistories] =
    s3IttageUpdateData.zip(s3IttageHashComponents).map { case (data, hash) =>
      getNextFoldedPhr(data, s3_oldestBits, s3_foldedPhrReg, hash._2, hash._1)
    }
  private val s3RasS0FoldedPhrCandidates: Seq[PhrAllFoldedHistories] =
    s3RasUpdateData.zip(s3RasHashComponents).map { case (data, hash) =>
      getNextFoldedPhr(data, s3_oldestBits, s3_foldedPhrReg, hash._2, hash._1)
    }
  private val s3S0FoldedPhrCandidates: Seq[PhrAllFoldedHistories] = s3MbtbS0FoldedPhrCandidates.indices.map { i =>
    MuxCase(
      s3MbtbS0FoldedPhrCandidates(i),
      Seq(
        s3UseRas(i)    -> s3RasS0FoldedPhrCandidates(i),
        s3UseIttage(i) -> s3IttageS0FoldedPhrCandidates(i)
      )
    )
  }

  private val s3MbtbS0FoldedPhrCandidatesTest: Seq[PhrAllFoldedHistories] =
    s3MbtbUpdateData.zip(s3MbtbHashComponents).map { case (data, hash) =>
      getNextFoldedPhr(data, s3_foldedPhrReg, getRedirectPhr(data.phrMeta), hash._2, hash._1)
    }
  private val s3IttageS0FoldedPhrCandidatesTest: Seq[PhrAllFoldedHistories] =
    s3IttageUpdateData.zip(s3IttageHashComponents).map { case (data, hash) =>
      getNextFoldedPhr(data, s3_foldedPhrReg, getRedirectPhr(data.phrMeta), hash._2, hash._1)
    }
  private val s3RasS0FoldedPhrCandidatesTest: Seq[PhrAllFoldedHistories] =
    s3RasUpdateData.zip(s3RasHashComponents).map { case (data, hash) =>
      getNextFoldedPhr(data, s3_foldedPhrReg, getRedirectPhr(data.phrMeta), hash._2, hash._1)
    }
  private val s3S0FoldedPhrCandidatesTest: Seq[PhrAllFoldedHistories] =
    s3MbtbS0FoldedPhrCandidatesTest.indices.map { i =>
      MuxCase(
        s3MbtbS0FoldedPhrCandidatesTest(i),
        Seq(
          s3UseRas(i)    -> s3RasS0FoldedPhrCandidatesTest(i),
          s3UseIttage(i) -> s3IttageS0FoldedPhrCandidatesTest(i)
        )
      )
    }

  private val s3S0FoldedPhrCandidatesDiff =
    s3S0FoldedPhrCandidates.zip(s3S0FoldedPhrCandidatesTest).map { case (a, b) =>
      a.asUInt =/= b.asUInt
    }.reduce(_ || _)
  XSError(
    s3_fire && s3S0FoldedPhrCandidatesDiff,
    f"s3 next folded PHR logic has inconsistency between two implementations!"
  )

  s3S0FoldedPhr := Mux1H(io.s3Train.firstTakenBrOH, s3S0FoldedPhrCandidates)

  private val s1UbtbS0FoldedPhr = getNextFoldedPhr(
    s1UbtbUpdateData,
    s1_foldedPhrReg,
    getRedirectPhr(s1UbtbUpdateData.phrMeta),
    s1UbtbHashComponents._2,
    s1UbtbHashComponents._1
  )
  private val s1AbtbS0FoldedPhrCandidates = s1AbtbUpdateData.zip(s1AbtbHashComponents).map { case (data, hash) =>
    getNextFoldedPhr(
      data,
      s1_foldedPhrReg,
      getRedirectPhr(data.phrMeta),
      hash._2,
      hash._1
    )
  }
  private val s1AbtbS0FoldedPhr = Mux1H(io.s1Train.abtbFirstTakenBrOH, s1AbtbS0FoldedPhrCandidates)
  s1S0FoldedPhr := Mux(io.s1Train.abtbValid, s1AbtbS0FoldedPhr, s1UbtbS0FoldedPhr)

  private val redirectNextPhr =
    getNextPhr(phr, redirectData.phrMeta.phrPtr, redirectData.taken, redirectS0PhrLowBits, redirectShiftBits)
  private val s3MbtbNextPhrCandidates =
    s3MbtbUpdateData.zip(s3MbtbUpdateCandidates).zip(s3MbtbHashComponents).map { case ((data, update), hash) =>
      getNextPhr(phr, data.phrMeta.phrPtr, true.B, update.phrLowBits, hash._1)
  }
  private val s3IttageNextPhrCandidates =
    s3IttageUpdateData.zip(s3IttageUpdateCandidates).zip(s3IttageHashComponents).map { case ((data, update), hash) =>
      getNextPhr(phr, data.phrMeta.phrPtr, true.B, update.phrLowBits, hash._1)
    }
  private val s3RasNextPhrCandidates =
    s3RasUpdateData.zip(s3RasUpdateCandidates).zip(s3RasHashComponents).map { case ((data, update), hash) =>
      getNextPhr(phr, data.phrMeta.phrPtr, true.B, update.phrLowBits, hash._1)
    }
  private val s3NextPhrCandidates: Seq[Vec[Bool]] = s3MbtbNextPhrCandidates.indices.map { i =>
    MuxCase(
      s3MbtbNextPhrCandidates(i),
      Seq(
        s3UseRas(i)    -> s3RasNextPhrCandidates(i),
        s3UseIttage(i) -> s3IttageNextPhrCandidates(i)
      )
    )
  }
  private val s3RecoverNextPhr =
    getNextPhr(phr, io.s3Train.phrMetaDup(0).phrPtr, false.B, s3RecoverResult.phrLowBits, 0.U(Shamt.W))
  private val s3NextPhr =
    Mux(io.s3Train.taken, Mux1H(io.s3Train.firstTakenBrOH, s3NextPhrCandidates), s3RecoverNextPhr)
  private val s1NextPhr =
    getNextPhr(phr, s1_phrPtr, io.s1Train.taken, s1S0PhrLowBits, s1ShiftBits)

  phr := MuxCase(
    phr,
    Seq(
      redirectData.valid -> redirectNextPhr,
      s3_override        -> s3NextPhr,
      s1_valid           -> s1NextPhr
    )
  )

  /*
   * PHR folded history select
   */
  s0_foldedPhr := MuxCase(
    s0_foldedPhrReg,
    Seq(
      redirectData.valid                -> redirectS0FoldedPhr,
      (s3_override && io.s3Train.taken) -> s3S0FoldedPhr,
      s3_override                       -> s3_foldedPhrReg,
      (s1_valid && io.s1Train.taken)    -> s1S0FoldedPhr
    )
  )

  AllFoldedHistoryInfo.foreach { info =>
    histFoldedPhr.getHistWithInfo(info).foldedHist :=
      computeFoldedHist(phrValue, info.FoldedLength)(info.HistoryLength)
  }

  /*
   * bpu training folded phr compute
   */
  private val bpTrain       = io.commit.bits
  private val predictHist   = getRedirectPhr(bpTrain.meta.phr)
  private val metaPhrFolded = WireInit(0.U.asTypeOf(new PhrAllFoldedHistories(AllFoldedHistoryInfo)))
  AllFoldedHistoryInfo.foreach { info =>
    metaPhrFolded.getHistWithInfo(info).foldedHist :=
      computeFoldedHist(predictHist, info.FoldedLength)(info.HistoryLength)
  }

  io.phrMeta.phrPtr     := s1_phrPtr
  io.phrMeta.phrLowBits := s1_phrValue(PathHashHighWidth - 1, 0)
  io.phrMeta.predFoldedHist.foreach(_ := s1_foldedPhrReg)
  io.phr            := phr
  io.s0_foldedPhr   := s0_foldedPhr
  io.s1_foldedPhr   := s1_foldedPhrReg
  io.s2_foldedPhr   := s2_foldedPhrReg
  io.s3_foldedPhr   := s3_foldedPhrReg
  io.trainFoldedPhr := metaPhrFolded

  // TODO: Currently unavailable, waiting for ftq commit info
  // commit time phr checker
  if (EnableCommitGHistDiff) {
    val commitValid   = RegNext(io.commit.valid)
    val commit        = RegEnable(io.commit.bits, io.commit.valid)
    val commitHist    = RegInit(0.U.asTypeOf(Vec(PhrHistoryLength, Bool())))
    val commitHistPtr = RegInit(0.U.asTypeOf(new PhrPtr))

    // FIXME: getPhr logic has changed
    def getCommitHist(ptr: PhrPtr): UInt =
      (Cat(commitHist.asUInt, commitHist.asUInt) >> (ptr.value + 1.U))(PhrHistoryLength - 1, 0)

    def shiftCommitBits(pc: PrunedAddr): UInt =
      (((pc >> 1) ^ (pc >> 3)) ^ ((pc >> 5) ^ (pc >> 7)))(Shamt - 1, 0)

    val commitTaken = commit.branches(0).bits.taken
    val commitTakenPc = Mux(
      commitValid && commit.branches(0).bits.mispredict.asBools.reduce(_ || _),
      commit.startPc,
      getCfiPcFromPosition(commit.startPc, commit.branches(0).bits.cfiPosition)
    )
    val commitShiftBits = shiftCommitBits(commitTakenPc)

    when(commitValid && commitTaken) {
      commitHist(commitHistPtr.value)         := commitShiftBits(1)
      commitHist((commitHistPtr - 1.U).value) := commitShiftBits(0)
      commitHistPtr                           := commitHistPtr - 2.U
    }

    val commitHistValue        = commitHist.asUInt
    val commitTrueHist         = getCommitHist(commitHistPtr)
    val commitFDiffPredictFVec = WireInit(0.U.asTypeOf(Vec(AllFoldedHistoryInfo.size, Bool())))
    AllFoldedHistoryInfo.zipWithIndex foreach { case (info, i) =>
      val commitTrueFHist = computeFoldedHist(commitTrueHist, info.FoldedLength)(info.HistoryLength)
      val predictFHist    = computeFoldedHist(predictHist, info.FoldedLength)(info.HistoryLength)
      commitFDiffPredictFVec(i) := commitTrueFHist =/= predictFHist
      XSWarn(
        commitValid && commitFDiffPredictFVec(i),
        p"predict time ghist: ${predictFHist} is different from commit time: ${commitTrueFHist}\n"
      )
    }
    val predictFHist_diff_commitTrueFHist = commitValid && commitFDiffPredictFVec.reduce(_ || _)
    val predictHist_diff_commitHist =
      commitValid && predictHist(MaxHistLens - 1, 0) =/= commitTrueHist(MaxHistLens - 1, 0)
    val histFolded_diff_s0Folded = histFoldedPhr.asUInt =/= s0_foldedPhrReg.asUInt
    when(s0_fire) {
      assert(
        !histFolded_diff_s0Folded,
        f"The history of on-site folding is inconsistent with the updated results of folding history"
      )
    }

    XSPerfAccumulate(f"predictFHist_diff_commitTrueFHist", predictFHist_diff_commitTrueFHist)
    XSPerfAccumulate(f"predictHist_diff_commitHist", predictHist_diff_commitHist)
    dontTouch(commitHistValue)
    dontTouch(commitTrueHist)
    dontTouch(commitShiftBits)
    dontTouch(predictHist)
    dontTouch(commitHistPtr)
    dontTouch(predictFHist_diff_commitTrueFHist)
    dontTouch(predictHist_diff_commitHist)
    dontTouch(commitFDiffPredictFVec.asUInt)
    dontTouch(commitTakenPc)
    dontTouch(histFolded_diff_s0Folded)
  }

  if (io.commit.bits.meta.phr.predFoldedHist.isDefined) {
    val debug_predFoldedHist = io.commit.bits.meta.phr.predFoldedHist.get
    require(
      debug_predFoldedHist.hist.length == metaPhrFolded.hist.length,
      "pred folded hist length mismatch"
    )
    val predictFHist_diff_trainFHist = io.commit.valid && debug_predFoldedHist.asUInt =/= metaPhrFolded.asUInt
    XSPerfAccumulate(f"predictFHist_diff_trainFHist", predictFHist_diff_trainFHist)
  }

  // TODO: remove dontTouch
  dontTouch(s0_foldedPhr)
  dontTouch(s1_foldedPhrReg)
  dontTouch(s2_foldedPhrReg)
  dontTouch(phrValue)
  dontTouch(histFoldedPhr)
  dontTouch(redirectPhr)
  dontTouch(s0_phrPtr)
  dontTouch(s1_phrPtr)
}
