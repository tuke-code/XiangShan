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

package xiangshan.frontend.bpu.mbtb

import chisel3._
import chisel3.util._
import utils.AddrField
import xiangshan.HasXSParameter
import xiangshan.frontend.PrunedAddr
import xiangshan.frontend.bpu.CompareMatrix
import xiangshan.frontend.bpu.CrossPageHelper
import xiangshan.frontend.bpu.HalfAlignHelper
import xiangshan.frontend.bpu.TargetFixHelper

trait Helpers extends HasMainBtbParameters
    with HasXSParameter with TargetFixHelper with HalfAlignHelper with CrossPageHelper {

  val addrFields = AddrField(
    Seq(
      ("alignOffset", FetchBlockAlignWidth),
      ("alignBankIdx", AlignBankIdxLen),
      ("internalBankIdx", InternalBankIdxLen),
      ("setIdx", SetIdxLen),
      ("tag", TagWidth)
    ),
    maxWidth = Option(VAddrBits),
    extraFields = Seq(
      ("replacerSetIdx", FetchBlockSizeWidth, SetIdxLen),
      ("targetLower", instOffsetBits, TargetWidth),
      ("vpnLower", instOffsetBits + TargetWidth, VpnLowerWidth),
      ("position", instOffsetBits, CfiAlignedPositionWidth),
      ("cfiPosition", instOffsetBits, CfiPositionWidth)
    )
  )

  def getSetIndex(pc: PrunedAddr): UInt =
    addrFields.extract("setIdx", pc)

  def getReplacerSetIndex(pc: PrunedAddr): UInt =
    addrFields.extract("replacerSetIdx", pc)

  def getAlignBankIndex(pc: PrunedAddr): UInt =
    addrFields.extract("alignBankIdx", pc)

  def getAlignBankIndexFromPosition(cfiPosition: UInt): UInt =
    addrFields.extractFrom("cfiPosition", "alignBankIdx", cfiPosition)

  def getTargetUpper(pc: PrunedAddr): UInt =
    pc(pc.length - 1, addrFields.getEnd("targetLower") + 1)

  def getTargetLowerBits(target: PrunedAddr): UInt =
    addrFields.extract("targetLower", target)

  def getInternalBankIndex(pc: PrunedAddr): UInt =
    addrFields.extract("internalBankIdx", pc)

  def getTag(pc: PrunedAddr): UInt =
    addrFields.extract("tag", pc)

  def getVpnLower(target: PrunedAddr): UInt =
    addrFields.extract("vpnLower", target)

  def getVpnUpper(target: PrunedAddr): UInt =
    target(target.length - 1, addrFields.getEnd("vpnLower") + 1)

  // TODO: use hash for better distribution
  def getPageBtbSetIndex(target: PrunedAddr): UInt =
    getVpnLower(target)(log2Ceil(NumPageBtbSets) - 1, 0)

  def genTargetIndex(wayIdx: UInt, setIdx: UInt): UInt =
    Cat(wayIdx, setIdx)

  def getPageBtbSetIndexFromTargetIndex(targetIndex: UInt): UInt =
    targetIndex(log2Ceil(NumPageBtbSets) - 1, 0)

  def getPageBtbWayIndexFromTargetIndex(targetIndex: UInt): UInt =
    targetIndex(log2Ceil(NumPageBtbEntries) - 1, log2Ceil(NumPageBtbSets))

  // detect multi-hit, return a mask indicating which way has multi-hit
  def detectMultiHit(hitMask: IndexedSeq[Bool], position: IndexedSeq[UInt]): UInt = {
    require(hitMask.length == position.length)
    require(hitMask.length >= 2)
    val multiHitMask = VecInit(Seq.fill(NumWay)(false.B))
    for {
      i <- 0 until NumWay
      j <- i + 1 until NumWay
    } {
      val bothHit      = hitMask(i) && hitMask(j)
      val samePosition = position(i) === position(j)
      when(bothHit && samePosition) {
        multiHitMask(j) := true.B
      }
    }
    PriorityEncoderOH(multiHitMask.asUInt)
  }

  def rrpvAging(rrpvs: Vec[UInt], matrix: CompareMatrix): Vec[UInt] = {
    val maxRrpvOH = matrix.getLeastElementOH(VecInit.fill(rrpvs.length)(true.B))
    val maxRrpv   = Mux1H(maxRrpvOH, rrpvs)
    val incValue  = MaxRrpv.U - maxRrpv
    VecInit(rrpvs.map(r => r + incValue))
  }
}
