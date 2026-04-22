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
import org.chipsalliance.cde.config.Parameters
import utils.EnumUInt
import xiangshan.XSCoreParamsKey
import xiangshan.frontend.PrunedAddr
import xiangshan.frontend.bpu.BranchAttribute
import xiangshan.frontend.bpu.BranchInfo
import xiangshan.frontend.bpu.SaturateCounter
import xiangshan.frontend.bpu.SaturateCounterFactory
import xiangshan.frontend.bpu.TargetCarry
import xiangshan.frontend.bpu.WriteReqBundle

object TakenCounter extends SaturateCounterFactory {
  def width(implicit p: Parameters): Int =
    p(XSCoreParamsKey).frontendParameters.bpuParameters.mbtbParameters.TakenCntWidth
}

// Not support indirect branch
object ShortAttribute {
  object BranchType extends EnumUInt(4) {
    def None:        UInt = 0.U(width.W)
    def Conditional: UInt = 1.U(width.W)
    def DirectCall:  UInt = 2.U(width.W)
    def OtherDirect: UInt = 3.U(width.W)
  }
}

class ShortAttribute extends Bundle {
  val branchType: UInt = ShortAttribute.BranchType()

  def isNone:        Bool = branchType === ShortAttribute.BranchType.None
  def isConditional: Bool = branchType === ShortAttribute.BranchType.Conditional
  def isDirectCall:  Bool = branchType === ShortAttribute.BranchType.DirectCall
  def isOtherDirect: Bool = branchType === ShortAttribute.BranchType.OtherDirect

  def fromBranchAttribute(attr: BranchAttribute): Unit =
    branchType := Mux1H(Seq(
      attr.isNone                     -> ShortAttribute.BranchType.None,
      attr.isConditional              -> ShortAttribute.BranchType.Conditional,
      (attr.isDirect && attr.isCall)  -> ShortAttribute.BranchType.DirectCall,
      (attr.isDirect && !attr.isCall) -> ShortAttribute.BranchType.OtherDirect
    ))
  def toBranchAttribute: BranchAttribute = {
    val attr = Wire(new BranchAttribute)
    attr := Mux1H(Seq(
      isNone        -> BranchAttribute.None,
      isConditional -> BranchAttribute.Conditional,
      isOtherDirect -> BranchAttribute.OtherDirect,
      isDirectCall  -> BranchAttribute.DirectCall
    ))
    attr
  }
}

class MonitorBtbEntry(implicit p: Parameters) extends MainBtbBundle {
  val fusion: Bool = Bool()
  val tag:    UInt = UInt(TagWidth.W)
}

class MonitorBtbShortSlot(implicit p: Parameters) extends MainBtbBundle {
  val valid:           Bool           = Bool()
  val attr:            ShortAttribute = new ShortAttribute
  val position:        UInt           = UInt(CfiAlignedPositionWidth.W)
  val targetLowerBits: UInt           = UInt(ShortTargetWidth.W)
}

class MonitorBtbLongSlot(implicit p: Parameters) extends MainBtbBundle {
  val valid:           Bool            = Bool()
  val attribute:       BranchAttribute = new BranchAttribute
  val position:        UInt            = UInt(CfiAlignedPositionWidth.W)
  val targetCarry:     TargetCarry     = new TargetCarry
  val targetLowerBits: UInt            = UInt(TargetWidth.W)
  val targetIndex:     UInt            = UInt(TargetIndexWidth.W)

  def fromShortSlots(slots: Vec[MonitorBtbShortSlot]): Unit =
    this := slots.asTypeOf(new MonitorBtbLongSlot)

  def toShortSlots: Vec[MonitorBtbShortSlot] = this.asTypeOf(Vec(NumSlots, new MonitorBtbShortSlot))
}

class PageBtbEntry(implicit p: Parameters) extends MainBtbBundle {
  val valid:    Bool = Bool()
  val vpnLower: UInt = UInt(VpnLowerWidth.W)
  val vpnIndex: UInt = UInt(VpnIndexWidth.W)
}

class RegionBtbEntry(implicit p: Parameters) extends MainBtbBundle {
  val valid:    Bool = Bool()
  val vpnUpper: UInt = UInt(VpnUpperWidth.W)
}

class MonitorBtbEntrySramWriteReq(implicit p: Parameters) extends WriteReqBundle with HasMainBtbParameters {
  val setIdx:   UInt                     = UInt(SetIdxLen.W)
  val entry:    MonitorBtbEntry          = new MonitorBtbEntry
  val slots:    Vec[MonitorBtbShortSlot] = Vec(NumSlots, new MonitorBtbShortSlot)
  val slotMask: UInt                     = UInt(NumSlots.W)
  val retagged: Bool                     = Bool()
  val effectiveShortSlot: MonitorBtbShortSlot = new MonitorBtbShortSlot
  val effectivePosition: UInt                 = UInt(CfiAlignedPositionWidth.W)
  val compareKey: UInt                        = UInt((TagWidth + CfiAlignedPositionWidth).W)

  def getEffectiveLongSlot: MonitorBtbLongSlot = {
    val longSlot = Wire(new MonitorBtbLongSlot)
    longSlot.fromShortSlots(slots)
    longSlot
  }

  def getEffectiveShortSlot: MonitorBtbShortSlot = {
    val retagged = slotMask.andR
    Mux(
      retagged,
      slots(0), // retagged to slot 0
      Mux1H(slotMask, slots)
    )
  }

  def getEffectivePosition: UInt =
    Mux(entry.fusion, getEffectiveLongSlot.position, getEffectiveShortSlot.position)

  def genEffectiveFields(): Unit = {
    retagged           := slotMask.andR
    effectiveShortSlot := getEffectiveShortSlot
    effectivePosition  := Mux(entry.fusion, getEffectiveLongSlot.position, effectiveShortSlot.position)
    compareKey         := Cat(entry.tag, effectivePosition)
  }

  def fromRawWrite(
      setIdx:   UInt,
      entry:    MonitorBtbEntry,
      slots:    Vec[MonitorBtbShortSlot],
      slotMask: UInt
  ): Unit = {
    this.setIdx   := setIdx
    this.entry    := entry
    this.slots    := slots
    this.slotMask := slotMask
    genEffectiveFields()
  }

  override def tag: Option[UInt] = Some(compareKey)
}

class MonitorBtbCounterSramWriteReq(implicit p: Parameters) extends MainBtbBundle {
  val setIdx:   UInt                 = UInt(SetIdxLen.W)
  val wayMask:  UInt                 = UInt((NumWay * NumSlots).W)
  val counters: Vec[SaturateCounter] = Vec(NumWay * NumSlots, TakenCounter())
}

class MonitorBtbMetaEntry(implicit p: Parameters) extends MainBtbBundle {
  val rawHit:    Bool            = Bool()
  val fused:     Bool            = Bool()
  val position:  UInt            = UInt(CfiPositionWidth.W)
  val attribute: BranchAttribute = new BranchAttribute
  val counter:   SaturateCounter = TakenCounter()

  def hit(branch: BranchInfo): Bool =
    rawHit && position === branch.cfiPosition

  def hitAttr(branch: BranchInfo): Bool =
    hit(branch) && attribute === branch.attribute
}

class MonitorBtbMeta(implicit p: Parameters) extends MainBtbBundle {
  val entries: Vec[Vec[MonitorBtbMetaEntry]] = Vec(NumAlignBanks, Vec(NumWay * NumSlots, new MonitorBtbMetaEntry))
}

class MBTBTrace(implicit p: Parameters) extends MainBtbBundle {
  val startPc:             PrunedAddr = PrunedAddr(VAddrBits)
  val trainBranchMask:     UInt       = UInt(ResolveEntryBranchNumber.W)
  val trainCondMask:       UInt       = UInt(ResolveEntryBranchNumber.W)
  val trainTakenMask:      UInt       = UInt(ResolveEntryBranchNumber.W)
  val trainMispredictMask: UInt       = UInt(ResolveEntryBranchNumber.W)

  val mispredictValid:       Bool            = Bool()
  val mispredictCfiPosition: UInt            = UInt(CfiPositionWidth.W)
  val mispredictTaken:       Bool            = Bool()
  val mispredictTarget:      PrunedAddr      = PrunedAddr(VAddrBits)
  val mispredictAttribute:   BranchAttribute = new BranchAttribute

  val bankStartPc:        PrunedAddr  = PrunedAddr(VAddrBits)
  val writeAlignBankMask: UInt        = UInt(NumAlignBanks.W)
  val writeAlignBankIdx:  UInt        = UInt(AlignBankIdxLen.W)
  val writeSetIdx:        UInt        = UInt(SetIdxLen.W)
  val writeInternalIdx:   UInt        = UInt(InternalBankIdxLen.W)
  val canUseShortSlot:    Bool        = Bool()
  val updateIsFused:      Bool        = Bool()
  val targetCarry:        TargetCarry = new TargetCarry

  val hit:            Bool = Bool()
  val longSlotHit:    Bool = Bool()
  val longSlotHitOH:  UInt = UInt(NumWay.W)
  val shortSlotHit:   Bool = Bool()
  val shortSlotHitOH: UInt = UInt((NumWay * NumSlots).W)

  val entryNeedWrite:   Bool                     = Bool()
  val updateAction:     UInt                     = UInt(log2Ceil(6).W)
  val updateActionWay:  UInt                     = UInt(log2Ceil(NumWay).W)
  val updateActionSlot: UInt                     = UInt(log2Ceil(NumSlots).W)
  val updateActionRrpv: UInt                     = UInt(RrpvWidth.W)
  val writeWayMask:     UInt                     = UInt(NumWay.W)
  val writeSlotMask:    UInt                     = UInt(NumSlots.W)
  val writeEntry:       MonitorBtbEntry          = new MonitorBtbEntry
  val writeSlots:       Vec[MonitorBtbShortSlot] = Vec(NumSlots, new MonitorBtbShortSlot)
  val writeRrpvs:       Vec[UInt]                = Vec(NumWay * NumSlots, UInt(RrpvWidth.W))

  val counterWriteValidMask: UInt                 = UInt(NumAlignBanks.W)
  val counterWriteMask:      UInt                 = UInt((NumAlignBanks * NumWay * NumSlots).W)
  val counterValues:         Vec[SaturateCounter] = Vec(NumAlignBanks * NumWay * NumSlots, TakenCounter())

  val pageSetIdx:  UInt           = UInt(log2Ceil(NumPageBtbSets).W)
  val pageHit:     Bool           = Bool()
  val pageWay:     UInt           = UInt(log2Ceil(NumPageBtbWays).W)
  val pageWrite:   Bool           = Bool()
  val pageEntry:   PageBtbEntry   = new PageBtbEntry
  val regionHit:   Bool           = Bool()
  val regionWay:   UInt           = UInt(log2Ceil(NumRegionBtbWays).W)
  val regionWrite: Bool           = Bool()
  val regionEntry: RegionBtbEntry = new RegionBtbEntry
}
