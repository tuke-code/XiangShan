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
import xiangshan.frontend.bpu.HasBpuParameters

case class MainBtbParameters(
    NumEntries: Int = 8192,
    NumWay:     Int = 4,
    // Lowest level banks, each bank is a physical SRAM
    // This banking is used to resolve read-write conflicts and reduce SRAM power
    NumInternalBanks: Int = 4,
    // Highest level banks
    // This banking is used to resolve the alignement restriction of the BTB
    // When using align banking, the BTB can provide at most banks - 1 / banks * predict width wide prediction
    NumAlignBanks:    Int = 2,
    NumSlots:         Int = 2,
    TagWidth:         Int = 16,
    TargetWidth:      Int = 11,
    ShortTargetWidth: Int = 8,
    WriteBufferSize:  Int = 4,
    // Base table
    TakenCntWidth: Int = 2,
    // Page Btb
    NumPageBtbEntries: Int = 256,
    NumPageBtbWays:    Int = 16,
    VpnLowerWidth:     Int = 16,
    // Region Btb
    NumRegionBtbEntries: Int = 4,
    NumRegionBtbWays:    Int = 4,
    // Replacer, SRRIP
    RrpvWidth: Int = 2,
    // Mbtb write trace
    EnableMainbtbTrace: Boolean = false
) {}

// TODO: expose this to Parameters.scala / XSCore.scala
trait HasMainBtbParameters extends HasBpuParameters {
  def mbtbParameters: MainBtbParameters = bpuParameters.mbtbParameters

  def NumEntries:       Int = mbtbParameters.NumEntries
  def NumWay:           Int = mbtbParameters.NumWay
  def NumSlots:         Int = mbtbParameters.NumSlots
  def NumInternalBanks: Int = mbtbParameters.NumInternalBanks
  def NumAlignBanks:    Int = FetchBlockSize / FetchBlockAlignSize
  // NumSets is the number of sets in one bank, a bank corresponds to a physical SRAM
  def NumSets:            Int = NumEntries / NumWay / NumInternalBanks / NumAlignBanks
  def TagWidth:           Int = mbtbParameters.TagWidth
  def TargetWidth:        Int = mbtbParameters.TargetWidth
  def ShortTargetWidth:   Int = mbtbParameters.ShortTargetWidth
  def SetIdxLen:          Int = log2Ceil(NumSets)
  def InternalBankIdxLen: Int = log2Ceil(NumInternalBanks)
  def AlignBankIdxLen:    Int = log2Ceil(NumAlignBanks)
  def WriteBufferSize:    Int = mbtbParameters.WriteBufferSize

  // Base table
  def TakenCntWidth: Int = mbtbParameters.TakenCntWidth

  // Page Btb
  def NumPageBtbEntries: Int = mbtbParameters.NumPageBtbEntries
  def NumPageBtbWays:    Int = mbtbParameters.NumPageBtbWays
  def NumPageBtbSets:    Int = NumPageBtbEntries / NumPageBtbWays
  def VpnLowerWidth:     Int = mbtbParameters.VpnLowerWidth
  def TargetIndexWidth:  Int = log2Ceil(NumPageBtbEntries)

  // Region Btb
  def NumRegionBtbEntries: Int = mbtbParameters.NumRegionBtbEntries
  def NumRegionBtbWays:    Int = mbtbParameters.NumRegionBtbWays
  def NumRegionBtbSets:    Int = NumRegionBtbEntries / NumRegionBtbWays
  def VpnUpperWidth:       Int = VAddrBits - TargetWidth - VpnLowerWidth
  def VpnIndexWidth:       Int = log2Ceil(NumRegionBtbEntries)

  // Replacer
  def RrpvWidth: Int = mbtbParameters.RrpvWidth
  def MaxRrpv:   Int = (1 << RrpvWidth) - 1

  // Used in any aligned-addr-indexed predictor, indicates the position relative to the aligned start addr
  def CfiAlignedPositionWidth: Int = CfiPositionWidth - AlignBankIdxLen

  def EnableMainbtbTrace: Boolean = mbtbParameters.EnableMainbtbTrace
}
