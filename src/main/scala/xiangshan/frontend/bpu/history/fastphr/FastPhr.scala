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

package xiangshan.frontend.bpu.history.fastphr

import chisel3._
import chisel3.util._
import org.chipsalliance.cde.config.Parameters
import xiangshan.frontend.bpu.history.phr.PhrAllFoldedHistories

// FastPhr: a fast short shift-register cache of the predicted path history, giving pTAGE
// resident folded-history registers. two recovery paths sit above the steady advance, taken
// in priority order redirect > override > steady; see FastPhrIO for the recovery contract.
class FastPhr(implicit p: Parameters) extends FastPhrModule with HasFastPhrParameters with Helpers {
  val io: FastPhrIO = IO(new FastPhrIO)

  private val phr = RegInit(0.U(WindowLength.W)) // bit0 is newest
  private val foldedHist =
    RegInit(0.U.asTypeOf(new PhrAllFoldedHistories(FastFoldedHistoryInfo, MaxUpdateNum)))

  // insertNum only ever takes 3 discrete values (0, Shamt, 2*Shamt), so the shift is realized
  // as a select over 3 fixed-shamt shifters instead of a runtime barrel shift. shared by the
  // steady advance and an override's corrected replay, which apply the identical shift to
  // different base windows
  private def advance(base: UInt, token: UInt, numBlocksOH: Vec[Bool]): UInt = Mux1H(
    numBlocksOH,
    Seq(
      base,
      ((base << Shamt).asUInt | token(Shamt - 1, 0))(WindowLength - 1, 0),
      ((base << (2 * Shamt)).asUInt | token(2 * Shamt - 1, 0))(WindowLength - 1, 0)
    )
  )

  private def steadyPhrNext:    UInt                  = advance(phr, io.token, io.numBlocksOH)
  private def steadyFoldedNext: PhrAllFoldedHistories = foldStep(foldedHist, phr, io.token, io.numBlocksOH)

  // 2-deep pipeline of the pre-update {phr, foldedHist}, captured on every steady advance;
  // snapS3 is the state an override recovery replays from, i.e. as it stood 2 valid-advances
  // ago. kept gated purely by io.valid: a redirect/override cycle does not itself shift this
  // pipeline, so the snapshot lineage tracks the steady-advance history independently of
  // whichever recovery, if any, wins that same cycle's phr/foldedHist update below
  private val snapS2 = RegInit(0.U.asTypeOf(new FastPhrSnapshot))
  private val snapS3 = RegInit(0.U.asTypeOf(new FastPhrSnapshot))
  when(io.valid) {
    snapS3        := snapS2
    snapS2.phr    := phr
    snapS2.folded := foldedHist
  }

  private val overridePhrNext    = advance(snapS3.phr, io.overrideToken, io.overrideNumBlocksOH)
  private val overrideFoldedNext = foldStep(snapS3.folded, snapS3.phr, io.overrideToken, io.overrideNumBlocksOH)

  phr := MuxCase(
    phr,
    Seq(
      io.redirect.valid -> io.redirect.phr,
      io.overrideValid  -> overridePhrNext,
      io.valid          -> steadyPhrNext
    )
  )
  foldedHist := MuxCase(
    foldedHist,
    Seq(
      io.redirect.valid -> refold(io.redirect.phr),
      io.overrideValid  -> overrideFoldedNext,
      io.valid          -> steadyFoldedNext
    )
  )

  io.foldedHist := foldedHist
}
