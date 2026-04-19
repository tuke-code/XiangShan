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
import utility.ChiselDB
import utility.XSPerfAccumulate
import utility.XSPerfHistogram
import utils.EnumUInt
import utils.VecRotate
import xiangshan.frontend.PrunedAddr
import xiangshan.frontend.PrunedAddrInit
import xiangshan.frontend.bpu.BasePredictor
import xiangshan.frontend.bpu.BasePredictorIO
import xiangshan.frontend.bpu.BranchAttribute
import xiangshan.frontend.bpu.CompareMatrix
import xiangshan.frontend.bpu.Prediction

class MonitorBtb(implicit p: Parameters) extends BasePredictor with HasMainBtbParameters with Helpers {
  class MonitorBtbIO(implicit p: Parameters) extends BasePredictorIO {
    // prediction specific bundle
    val result: Vec[Valid[Prediction]] = Output(Vec(NumBtbResultEntries, Valid(new Prediction)))
    val meta:   MonitorBtbMeta         = Output(new MonitorBtbMeta)

    // timing optimization: send positions earlier to TAGE
    val s1_positions: Vec[UInt] = Output(Vec(NumBtbResultEntries, UInt(CfiPositionWidth.W)))

    val s3_useFinalTarget: Bool       = Output(Bool())
    val s3_finalTarget:    PrunedAddr = Output(PrunedAddr(VAddrBits))
    // final s3_takenMask (mbtb + tage + sc), used to touch replacer accurately
    val s3_takenMask:          Vec[Bool] = Input(Vec(NumBtbResultEntries, Bool()))
    val s3_firstTakenBranchOH: Vec[Bool] = Input(Vec(NumBtbResultEntries, Bool()))
  }

  val io: MonitorBtbIO = IO(new MonitorBtbIO)

  // print params
  println(f"MonitorBtb:")
  println(f"  Size(set, way, align, internal): $NumSets * $NumWay * $NumAlignBanks * $NumInternalBanks = $NumEntries")
  println(f"  Address fields:")
  addrFields.show(indent = 4)

  // Slot assertion
  assert(
    (new MonitorBtbLongSlot).getWidth == Vec(NumSlots, new MonitorBtbShortSlot).getWidth,
    "Slot width mismatch"
  )

  /* *** submodules *** */
  private val alignBanks = Seq.tabulate(NumAlignBanks)(alignIdx =>
    Seq.tabulate(NumInternalBanks)(internalIdx =>
      Module(new MonitorBtbInternalBank(alignIdx, internalIdx))
    )
  )
  private val alignBankRrpvs = Seq.tabulate(NumAlignBanks)(_ =>
    RegInit(VecInit.fill(NumSets, NumWay, NumSlots)(MaxRrpv.U(RrpvWidth.W)))
  )

  private val pageBtb      = Module(new PageBtb)
  private val pageBtbRrpvs = RegInit(VecInit.fill(NumPageBtbSets, NumPageBtbWays)(MaxRrpv.U(RrpvWidth.W)))

  private val regionBtb      = Reg(Vec(NumRegionBtbSets, Vec(NumRegionBtbWays, new RegionBtbEntry)))
  private val regionBtbRrpvs = RegInit(VecInit.fill(NumRegionBtbSets, NumRegionBtbWays)(MaxRrpv.U(RrpvWidth.W)))

  io.sramResetDone := alignBanks.flatMap(_.map(_.io.sramResetDone)).reduce(_ && _)
  io.trainReady    := true.B

  private val s0_fire, s1_fire, s2_fire, s3_fire = Wire(Bool())
  /* *** s0 ***
   * calculate per-bank startPc and posHigherBits
   * send read request to alignBanks
   */
  s0_fire := io.stageCtrl.s0_fire && io.enable
  private val s0_startPc = io.startPc
  // rotate read addresses according to the first align bank index
  // e.g. if NumAlignBanks = 4, startPc locates in alignBank 1,
  // startPc + (i << FetchBlockAlignWidth) will be located in alignBank (1 + i) % 4,
  // i.e. we have VecInit.tabulate(...)'s alignBankIdx = (1, 2, 3, 0),
  // they always needs to goes to physical alignBank (0, 1, 2, 3),
  // so we need to rotate it right by 1.
  private val s0_rotator = VecRotate(getAlignBankIndex(s0_startPc))
  private val s0_startPcVec = s0_rotator.rotate(
    VecInit.tabulate(NumAlignBanks) { i =>
      if (i == 0)
        s0_startPc // keep lower bits for the first one
      else
        getAlignedPc(s0_startPc + (i << FetchBlockAlignWidth).U) // use aligned for others
    }
  )
  private val s0_posHigherBitsVec = s0_rotator.rotate(VecInit.tabulate(NumAlignBanks)(_.U(AlignBankIdxLen.W)))
  private val s0_crossPageVec     = s0_startPcVec.map(pc => isCrossPage(pc, s0_startPc))

  private val s0_setIdxVec           = s0_startPcVec.map(pc => getSetIndex(pc))
  private val s0_internalBankIdxVec  = s0_startPcVec.map(pc => getInternalBankIndex(pc))
  private val s0_internalBankMaskVec = s0_internalBankIdxVec.map(i => UIntToOH(i, NumInternalBanks))
  private val s0_alignBankIdxVec     = s0_startPcVec.map(pc => getAlignBankIndex(pc))

  alignBanks.zipWithIndex.foreach { case (alignBank, alignIdx) =>
    alignBank.zipWithIndex.foreach { case (internalBank, internalIdx) =>
      internalBank.io.read.req.valid       := s0_fire && s0_internalBankMaskVec(alignIdx)(internalIdx)
      internalBank.io.read.req.bits.setIdx := s0_setIdxVec(alignIdx)
    }
  }

  /* *** s1 ***
   * just wait alignBanks
   */
  s1_fire := io.stageCtrl.s1_fire && io.enable

  private val s1_startPcVec          = RegEnable(s0_startPcVec, s0_fire)
  private val s1_posHigherBitsVec    = RegEnable(s0_posHigherBitsVec, s0_fire)
  private val s1_crossPageVec        = RegEnable(VecInit(s0_crossPageVec), s0_fire)
  private val s1_internalBankMaskVec = RegEnable(VecInit(s0_internalBankMaskVec), s0_fire)

  private val s1_rawEntriesVec = VecInit.tabulate(NumAlignBanks) { alignIdx =>
    Mux1H(s1_internalBankMaskVec(alignIdx), alignBanks(alignIdx).map(_.io.read.resp.entries))
  }
  private val s1_rawSlotsVec = VecInit.tabulate(NumAlignBanks) { alignIdx =>
    Mux1H(s1_internalBankMaskVec(alignIdx), alignBanks(alignIdx).map(_.io.read.resp.slots))
  }
  private val s1_rawCountersVec = VecInit.tabulate(NumAlignBanks) { alignIdx =>
    Mux1H(s1_internalBankMaskVec(alignIdx), alignBanks(alignIdx).map(_.io.read.resp.counters))
  }

  private val s1_positionsVec = VecInit.tabulate(NumAlignBanks) { alignIdx =>
    val rawEntries = s1_rawEntriesVec(alignIdx)
    val rawSlots   = s1_rawSlotsVec(alignIdx)
    val positionsByWay = VecInit(rawEntries.zipWithIndex.map { case (e, way) =>
      val longSlot = Wire(new MonitorBtbLongSlot)
      longSlot.fromShortSlots(rawSlots(way))
      Mux(
        e.fusion,
        VecInit(Seq(longSlot.position, 0.U)),
        VecInit(rawSlots(way).map(_.position))
      )
    })
    VecInit(positionsByWay.flatten.map(pos => Cat(s1_posHigherBitsVec(alignIdx), pos)))
  }

  private val s1_compareMatrix = CompareMatrix(VecInit(s1_positionsVec.flatten))

  io.s1_positions := VecInit(s1_positionsVec.flatten)

  /* *** s2 ***
   * receive read response from alignBanks
   * send out prediction result and meta info
   */
  s2_fire := io.stageCtrl.s2_fire && io.enable
  private val s2_startPcVec          = RegEnable(s1_startPcVec, s1_fire)
  private val s2_posHigherBitsVec    = RegEnable(s1_posHigherBitsVec, s1_fire)
  private val s2_crossPageVec        = RegEnable(s1_crossPageVec, s1_fire)
  private val s2_internalBankMaskVec = RegEnable(s1_internalBankMaskVec, s1_fire)
  private val s2_rawEntriesVec       = RegEnable(s1_rawEntriesVec, s1_fire)
  private val s2_rawSlotsVec         = RegEnable(s1_rawSlotsVec, s1_fire)
  private val s2_rawCountersVec      = RegEnable(s1_rawCountersVec, s1_fire)
  private val s2_positionsVec        = RegEnable(s1_positionsVec, s1_fire)
  private val s2_compareMatrix       = RegEnable(s1_compareMatrix, s1_fire)

  private val s2_setIdxVec            = s2_startPcVec.map(pc => getSetIndex(pc))
  private val s2_tagVec               = s2_startPcVec.map(pc => getTag(pc))
  private val s2_alignedInstOffsetVec = s2_startPcVec.map(pc => getAlignedInstOffset(pc))
  private val s2_startPcFlatten = VecInit.tabulate(NumAlignBanks * NumWay * NumSlots) { i =>
    s2_startPcVec(i / (NumWay * NumSlots))
  }

  private val s2_rawLongSlots = VecInit.tabulate(NumAlignBanks) { alignIdx =>
    VecInit(s2_rawSlotsVec(alignIdx).zipWithIndex.flatMap { case (s, i) =>
      val longSlot = Wire(new MonitorBtbLongSlot)
      longSlot.fromShortSlots(s)
      Seq(longSlot, 0.U.asTypeOf(new MonitorBtbLongSlot))
    })
  }.flatten

  private val s2_metasVec       = Wire(Vec(NumAlignBanks, Vec(NumWay * NumSlots, new MonitorBtbMetaEntry)))
  private val s2_predictionsVec = Wire(Vec(NumAlignBanks, Vec(NumWay, Vec(NumSlots, Valid(new Prediction)))))

  Seq.tabulate(NumAlignBanks) { alignIdx =>
    Seq.tabulate(NumWay) { wayIdx =>
      val entry       = s2_rawEntriesVec(alignIdx)(wayIdx)
      val slots       = s2_rawSlotsVec(alignIdx)(wayIdx)
      val predictions = s2_predictionsVec(alignIdx)(wayIdx)
      val metas       = s2_metasVec(alignIdx)
      val wayBaseIdx  = wayIdx * NumSlots
      val isFused     = s2_rawEntriesVec(alignIdx)(wayIdx).fusion
      val counters    = VecInit(s2_rawCountersVec(alignIdx).slice(wayBaseIdx, wayBaseIdx + NumSlots))

      val longSlot = Wire(new MonitorBtbLongSlot)
      longSlot.fromShortSlots(slots)

      val valid  = Mux(isFused, longSlot.valid, slots.map(_.valid).reduce(_ || _))
      val rawHit = valid && entry.tag === s2_tagVec(alignIdx)

      Seq.tabulate(NumSlots) { slotIdx =>
        val meta = metas(wayBaseIdx + slotIdx)
        when(isFused) {
          if (slotIdx == 0) {
            val hit = rawHit && longSlot.position >= s2_alignedInstOffsetVec(alignIdx) && !s2_crossPageVec(alignIdx)
            predictions(slotIdx).valid            := hit
            predictions(slotIdx).bits.cfiPosition := Cat(s2_posHigherBitsVec(alignIdx), longSlot.position)
            predictions(slotIdx).bits.target := getFullTarget(
              s2_startPcVec(alignIdx),
              longSlot.targetLowerBits,
              Some(longSlot.targetCarry)
            )
            predictions(slotIdx).bits.attribute := longSlot.attribute
            predictions(slotIdx).bits.taken     := counters(slotIdx).isPositive

            meta.rawHit    := rawHit
            meta.fused     := true.B
            meta.position  := Cat(s2_posHigherBitsVec(alignIdx), longSlot.position)
            meta.attribute := longSlot.attribute
            meta.counter   := counters(slotIdx)
          } else {
            predictions(slotIdx).valid := false.B
            predictions(slotIdx).bits  := DontCare
            meta.rawHit                := false.B
            meta.fused                 := true.B
            meta.position              := 0.U
            meta.attribute             := 0.U.asTypeOf(new BranchAttribute)
            meta.counter               := 0.U.asTypeOf(TakenCounter())
          }
        }.otherwise {
          val hit = rawHit && slots(slotIdx).valid &&
            slots(slotIdx).position >= s2_alignedInstOffsetVec(alignIdx) && !s2_crossPageVec(alignIdx)
          val startPcUpper =
            s2_startPcVec(alignIdx)(s2_startPcVec(alignIdx).length - 1, ShortTargetWidth + instOffsetBits)
          predictions(slotIdx).valid            := hit
          predictions(slotIdx).bits.cfiPosition := Cat(s2_posHigherBitsVec(alignIdx), slots(slotIdx).position)
          predictions(slotIdx).bits.target :=
            Cat(startPcUpper, slots(slotIdx).targetLowerBits, 0.U(instOffsetBits.W))
          predictions(slotIdx).bits.attribute := slots(slotIdx).attr.toBranchAttribute
          predictions(slotIdx).bits.taken     := counters(slotIdx).isPositive

          meta.rawHit    := rawHit
          meta.fused     := false.B
          meta.position  := Cat(s2_posHigherBitsVec(alignIdx), slots(slotIdx).position)
          meta.attribute := slots(slotIdx).attr.toBranchAttribute
          meta.counter   := counters(slotIdx)
        }
      }
    }
  }

  private val s2_result    = s2_predictionsVec.flatten.flatten
  private val s2_fusedMask = s2_rawEntriesVec.flatten.flatMap(e => VecInit(Seq(e.fusion, false.B)))
  private val s2_longJumpMask = s2_result.zip(s2_fusedMask).zip(s2_rawLongSlots).map { case ((r, f), l) =>
    r.valid && f && l.targetCarry.isInvalid
  }
  private val s2_firstLongJumpOH      = s2_compareMatrix.getLeastElementOH(VecInit(s2_longJumpMask))
  private val s2_firstLongJumpSlot    = Mux1H(s2_firstLongJumpOH, s2_rawLongSlots)
  private val s2_firstLongJumpStartPc = Mux1H(s2_firstLongJumpOH, s2_startPcFlatten)

  private val s2_pageBtbSetIdx = getPageBtbSetIndexFromTargetIndex(s2_firstLongJumpSlot.targetIndex)
  private val s2_pageBtbWayIdx = getPageBtbWayIndexFromTargetIndex(s2_firstLongJumpSlot.targetIndex)

  pageBtb.io.readWay.req.valid        := s2_fire && s2_firstLongJumpOH.reduce(_ || _)
  pageBtb.io.readWay.req.bits.setIdx  := s2_pageBtbSetIdx
  pageBtb.io.readWay.req.bits.wayMask := UIntToOH(s2_pageBtbWayIdx, NumPageBtbWays)

  private val s2_hitMaskVec = Seq.tabulate(NumAlignBanks) { alignIdx =>
    VecInit(s2_predictionsVec(alignIdx).flatten.map(_.valid))
  }
  dontTouch(VecInit(s2_hitMaskVec.flatten))

  // we don't care about the order of alignBanks' responses,
  // (as s0_posHigherBitsVec is already computed and concatenated to each entry's posLowerBits)
  // (and we care about the full position when searching for a matching entry, not the bank it comes from)
  // so here we just flatten them, without rotating them back to the original order
  io.result := VecInit(s2_predictionsVec.flatten.flatten)
  // we don't need to flatten meta entries, keep the alignBank structure, anyway we just use them per alignBank
  io.meta.entries := s2_metasVec

  /* *** s3 ***
   * touch replacer using final takenMask (mbtb + tage + sc)
   */
  s3_fire := io.enable && io.stageCtrl.s3_fire
  // io.result is flattened, so is s3_takenMask from Bpu top, here we need to slice it back to alignBank structure
//  alignBanks.zipWithIndex.foreach { case (b, i) =>
//    b.io.s3_takenMask := io.s3_takenMask.slice(i * NumWay, (i + 1) * NumWay)
//  }
  private val s3_startPcVec           = RegEnable(s2_startPcVec, s2_fire)
  private val s3_firstLongJumpStartPc = RegEnable(s2_firstLongJumpStartPc, s2_fire)
  private val s3_firstLongJumpOH      = RegEnable(s2_firstLongJumpOH, s2_fire)
  private val s3_firstLongJumpSlot    = RegEnable(s2_firstLongJumpSlot, s2_fire)

  private val s3_pageBtbSetIdx = RegEnable(s2_pageBtbSetIdx, s2_fire)
  private val s3_pageBtbWayIdx = RegEnable(s2_pageBtbWayIdx, s2_fire)
  private val s3_pageBtbEntry  = pageBtb.io.readWay.resp.entry

  private val s3_regionBtbSetIdx = 0.U
  private val s3_regionBtbWayIdx = s3_pageBtbEntry.vpnIndex
  private val s3_regionBtbEntry  = regionBtb(s3_regionBtbSetIdx)(s3_regionBtbWayIdx)

  private val s3_firstLongJumpStartPcVpnUpper = getVpnUpper(s3_firstLongJumpStartPc)
  private val s3_firstLongJumpStartPcVpnLower = getVpnLower(s3_firstLongJumpStartPc)

  private val s3_firstLongJumpTaken =
    s3_firstLongJumpOH.zip(io.s3_firstTakenBranchOH).map { case (l, t) => l && t }.reduce(_ || _)

  private val s3_finalTarget = Cat(
    Mux(s3_regionBtbEntry.valid && s3_pageBtbEntry.valid, s3_regionBtbEntry.vpnUpper, s3_firstLongJumpStartPcVpnUpper),
    Mux(s3_pageBtbEntry.valid, s3_pageBtbEntry.vpnLower, s3_firstLongJumpStartPcVpnLower),
    s3_firstLongJumpSlot.targetLowerBits,
    0.U(instOffsetBits.W)
  )
  io.s3_useFinalTarget := s3_firstLongJumpTaken
  io.s3_finalTarget    := PrunedAddrInit(s3_finalTarget)

  // Align bank replacer update
  private val s3_fusedWayMaskVec =
    RegEnable(VecInit(s2_rawEntriesVec.map(entries => VecInit(entries.map(_.fusion)))), s2_fire)
  private val s3_replacerSetIdxVec = s3_startPcVec.map(pc => getReplacerSetIndex(pc))
  private val s3_rrpvsVec = alignBankRrpvs.zipWithIndex.map { case (rrpvs, alignIdx) =>
    rrpvs(s3_replacerSetIdxVec(alignIdx)).flatten
  }
  private val s3_victimMatrixVec =
    Seq.tabulate(NumAlignBanks)(alignIdx => CompareMatrix(VecInit(s3_rrpvsVec(alignIdx)), order = (a, b) => a > b))
  private val s3_agedRrpvsVec = Seq.tabulate(NumAlignBanks) { alignIdx =>
    rrpvAging(VecInit(s3_rrpvsVec(alignIdx)), s3_victimMatrixVec(alignIdx))
  }
  private val s3_writeRrpvsVec = Seq.tabulate(NumAlignBanks) { alignIdx =>
    val curAlignIdx = alignIdx * NumWay * NumSlots
    VecInit.tabulate(NumWay) { wayIdx =>
      val curWayIdx = wayIdx * NumSlots
      val wayTaken  = io.s3_takenMask.slice(curAlignIdx + curWayIdx, curAlignIdx + curWayIdx + NumSlots)
      val clearWay  = s3_fusedWayMaskVec(alignIdx)(wayIdx) && wayTaken.reduce(_ || _)
      VecInit.tabulate(NumSlots) { slotIdx =>
        Mux(
          clearWay || wayTaken(slotIdx),
          0.U,
          s3_agedRrpvsVec(alignIdx)(curWayIdx + slotIdx)
        )
      }
    }
  }
  alignBankRrpvs.zip(s3_replacerSetIdxVec).zip(s3_writeRrpvsVec).zipWithIndex.foreach {
    case (((rrpvs, setIdx), writeRrpvs), i) =>
      val alignSize = NumWay * NumSlots
      val takenMask = io.s3_takenMask.slice(i * alignSize, (i + 1) * alignSize)
      when(s3_fire && takenMask.reduce(_ || _)) {
        rrpvs(setIdx) := writeRrpvs
      }
  }

  // Page/Region Btb replacer update
  private val s3_pageBtbRrpvs        = pageBtbRrpvs(s3_pageBtbSetIdx)
  private val s3_pageBtbVictimMatrix = CompareMatrix(s3_pageBtbRrpvs, order = (a, b) => a > b)
  private val s3_pageBtbAgedRrpvs    = rrpvAging(s3_pageBtbRrpvs, s3_pageBtbVictimMatrix)
  private val s3_pageBtbWayMask      = UIntToOH(s3_pageBtbWayIdx, NumPageBtbWays)
  private val s3_pageBtbWriteRrpvs = s3_pageBtbAgedRrpvs.zipWithIndex.map { case (r, i) =>
    Mux(s3_firstLongJumpTaken && s3_pageBtbWayMask(i), 0.U, r)
  }

  private val s3_regionBtbRrpvs        = regionBtbRrpvs(s3_regionBtbSetIdx)
  private val s3_regionBtbVictimMatrix = CompareMatrix(s3_regionBtbRrpvs, order = (a, b) => a > b)
  private val s3_regionBtbAgedRrpvs    = rrpvAging(s3_regionBtbRrpvs, s3_regionBtbVictimMatrix)
  private val s3_regionBtbWayMask      = UIntToOH(s3_pageBtbEntry.vpnIndex, NumRegionBtbWays)
  private val s3_regionBtbWriteRrpvs = s3_regionBtbAgedRrpvs.zipWithIndex.map { case (r, i) =>
    Mux(s3_firstLongJumpTaken && s3_regionBtbWayMask(i), 0.U, r)
  }

  when(s3_fire && s3_firstLongJumpTaken) {
    pageBtbRrpvs(s3_pageBtbSetIdx)     := s3_pageBtbWriteRrpvs
    regionBtbRrpvs(s3_regionBtbSetIdx) := s3_regionBtbWriteRrpvs
  }

  /* *** t0 ***
   * receive training data and latch
   */
  private val t0_fire           = io.stageCtrl.t0_fire && io.enable
  private val t0_train          = io.train
  private val t0_mispredictInfo = t0_train.mispredictBranch
  private val t0_mispredNotCond =
    t0_mispredictInfo.valid && !t0_mispredictInfo.bits.attribute.isConditional

  private val t0_pageSetIdx = getPageBtbSetIndex(t0_mispredictInfo.bits.target)

  pageBtb.io.readSet.req.valid       := t0_fire && t0_mispredNotCond
  pageBtb.io.readSet.req.bits.setIdx := t0_pageSetIdx

  /* *** t1 ***
   * calculate write data and write to alignBanks
   */
  private val t1_fire  = RegNext(t0_fire, init = false.B) && io.enable
  private val t1_train = RegEnable(t0_train, t0_fire)

  private val t1_startPc = t1_train.startPc
  private val t1_rotator = VecRotate(getAlignBankIndex(t1_startPc))
  private val t1_startPcVec = t1_rotator.rotate(
    VecInit.tabulate(NumAlignBanks)(i => getAlignedPc(t1_startPc + (i << FetchBlockAlignWidth).U))
  )
  private val t1_setIdxVec           = t1_startPcVec.map(pc => getSetIndex(pc))
  private val t1_internalBankIdxVec  = t1_startPcVec.map(pc => getInternalBankIndex(pc))
  private val t1_internalBankMaskVec = t1_internalBankIdxVec.map(idx => UIntToOH(idx, NumInternalBanks))
  private val t1_alignBankIdxVec     = t1_startPcVec.map(pc => getAlignBankIndex(pc))

  private val t1_branches       = t1_train.branches
  private val t1_meta           = t1_train.meta.mbtb
  private val t1_mispredictInfo = t1_train.mispredictBranch

  private val t1_writeAlignBankIdx  = getAlignBankIndexFromPosition(t1_mispredictInfo.bits.cfiPosition)
  private val t1_writeAlignBankMask = t1_rotator.rotate(VecInit(UIntToOH(t1_writeAlignBankIdx).asBools))

  private val t1_bankStartPc = Mux1H(t1_writeAlignBankMask, t1_startPcVec)
  private val t1_bankMeta    = Mux1H(t1_writeAlignBankMask, t1_meta.entries)
  private val t1_bankRrpvs =
    Mux1H(t1_writeAlignBankMask, VecInit(alignBankRrpvs))(getReplacerSetIndex(t1_bankStartPc))
  private val t1_bankMetaByWay = Seq.tabulate(NumWay) { wayIdx =>
    t1_bankMeta.slice(wayIdx * NumSlots, (wayIdx + 1) * NumSlots)
  }

  private val t1_targetDiff    = t1_bankStartPc.addr ^ t1_mispredictInfo.bits.target.addr
  private val t1_hasTargetDiff = t1_targetDiff.orR
  private val t1_distance      = OHToUInt(Reverse(PriorityEncoderOH(Reverse(t1_targetDiff))))
  private val t1_targetCarry   = getTargetCarrySlow(t1_bankStartPc, t1_mispredictInfo.bits.target)

  private val t1_canUseShortSlot = t1_distance <= (ShortTargetWidth - 1).U
  private val t1_updateIsFused   = !t1_canUseShortSlot || t1_mispredictInfo.bits.attribute.isIndirect

  private val t1_vpnLower = getVpnLower(t1_mispredictInfo.bits.target)
  private val t1_vpnUpper = getVpnUpper(t1_mispredictInfo.bits.target)

  private val t1_pageBtbSetIdx   = getPageBtbSetIndex(t1_mispredictInfo.bits.target)
  private val t1_regionBtbSetIdx = 0.U

  private val t1_pageBtbEntries   = pageBtb.io.readSet.resp.entries
  private val t1_regionBtbEntries = regionBtb(t1_regionBtbSetIdx)

  private val t1_longSlotHitVec = VecInit(t1_bankMetaByWay.map(wayMeta =>
    wayMeta.head.hit(t1_mispredictInfo.bits) && wayMeta.head.fused
  ))
  private val t1_longSlotHit   = t1_longSlotHitVec.reduce(_ || _)
  private val t1_longSlotHitOH = VecInit(PriorityEncoderOH(t1_longSlotHitVec))

  private val t1_shortSlotHitVec = VecInit(t1_bankMeta.map(meta => meta.hit(t1_mispredictInfo.bits) && !meta.fused))
  private val t1_shortSlotHit    = t1_shortSlotHitVec.reduce(_ || _)
  private val t1_shortSlotHitOH  = VecInit(PriorityEncoderOH(t1_shortSlotHitVec))

  private val t1_hit = t1_longSlotHit || t1_shortSlotHit

  private val t1_regionBtbHitVec = t1_regionBtbEntries.map(e => e.valid && e.vpnUpper === t1_vpnUpper)
  private val t1_regionBtbHit    = t1_regionBtbHitVec.reduce(_ || _)
  private val t1_regionBtbHitOH  = VecInit(PriorityEncoderOH(t1_regionBtbHitVec))
  private val t1_regionBtbHitWay = OHToUInt(t1_regionBtbHitOH)

  private val t1_pageBtbHitVec = t1_pageBtbEntries.map(e =>
    e.valid && e.vpnLower === t1_vpnLower &&
      e.vpnIndex === t1_regionBtbHitWay && t1_regionBtbHit
  )
  private val t1_pageBtbHit    = t1_pageBtbHitVec.reduce(_ || _)
  private val t1_pageBtbHitOH  = VecInit(PriorityEncoderOH(t1_pageBtbHitVec))
  private val t1_pageBtbHitWay = OHToUInt(t1_pageBtbHitOH)

  private val t1_pageBtbRrpvs        = pageBtbRrpvs(t1_pageBtbSetIdx)
  private val t1_pageBtbVictimMatrix = CompareMatrix(t1_pageBtbRrpvs, order = (a, b) => a > b)
  private val t1_pageBtbVictimOH     = t1_pageBtbVictimMatrix.getLeastElementOH(VecInit.fill(NumPageBtbWays)(true.B))
  private val t1_pageBtbVictimWay    = OHToUInt(t1_pageBtbVictimOH)
  private val t1_pageBtbWay          = Mux(t1_pageBtbHit, t1_pageBtbHitWay, t1_pageBtbVictimWay)

  private val t1_pageBtbAgedRrpvs  = rrpvAging(t1_pageBtbRrpvs, t1_pageBtbVictimMatrix)
  private val t1_pageBtbHitRrpvs   = Wire(Vec(NumPageBtbWays, UInt(RrpvWidth.W)))
  private val t1_pageBtbMissRrpvs  = Wire(Vec(NumPageBtbWays, UInt(RrpvWidth.W)))
  private val t1_pageBtbWriteRrpvs = Mux(t1_pageBtbHit, t1_pageBtbHitRrpvs, t1_pageBtbMissRrpvs)
  t1_pageBtbHitRrpvs.zipWithIndex.foreach { case (r, i) =>
    r := Mux(t1_pageBtbHitOH(i), 0.U, t1_pageBtbAgedRrpvs(i))
  }
  t1_pageBtbMissRrpvs.zipWithIndex.foreach { case (r, i) =>
    r := Mux(t1_pageBtbVictimOH(i), (MaxRrpv - 1).U, t1_pageBtbAgedRrpvs(i))
  }

  private val t1_regionBtbRrpvs        = regionBtbRrpvs(t1_regionBtbSetIdx)
  private val t1_regionBtbVictimMatrix = CompareMatrix(t1_regionBtbRrpvs, order = (a, b) => a > b)
  private val t1_regionBtbVictimOH  = t1_regionBtbVictimMatrix.getLeastElementOH(VecInit.fill(NumRegionBtbWays)(true.B))
  private val t1_regionBtbVictimWay = OHToUInt(t1_regionBtbVictimOH)
  private val t1_regionBtbWay       = Mux(t1_regionBtbHit, t1_regionBtbHitWay, t1_regionBtbVictimWay)

  private val t1_regionBtbAgedRrpvs  = rrpvAging(t1_regionBtbRrpvs, t1_regionBtbVictimMatrix)
  private val t1_regionBtbHitRrpvs   = Wire(Vec(NumRegionBtbWays, UInt(RrpvWidth.W)))
  private val t1_regionBtbMissRrpvs  = Wire(Vec(NumRegionBtbWays, UInt(RrpvWidth.W)))
  private val t1_regionBtbWriteRrpvs = Mux(t1_regionBtbHit, t1_regionBtbHitRrpvs, t1_regionBtbMissRrpvs)
  t1_regionBtbHitRrpvs.zipWithIndex.foreach { case (r, i) =>
    r := Mux(t1_regionBtbHitOH(i), 0.U, t1_regionBtbAgedRrpvs(i))
  }
  t1_regionBtbMissRrpvs.zipWithIndex.foreach { case (r, i) =>
    r := Mux(t1_regionBtbVictimOH(i), (MaxRrpv - 1).U, t1_regionBtbAgedRrpvs(i))
  }

  // Write entry only when there's a mispredict, and if:
  private val t1_entryNeedWrite = t1_mispredictInfo.valid && (
    // 1. not hit, always write a new entry, use mbtb replacer's victim way.
    !t1_hit ||
      // 2. hit, do write only if:
      //   a. it's an OtherIndirect-type branch (to update target and play the role of Ittage's base table).
      t1_mispredictInfo.bits.attribute.needIttage ||
      //   b. it's a Direct-type branch (to update Page/RegionBtb because pointer is stale).
      t1_mispredictInfo.bits.attribute.isDirect ||
      //   b. attribute changed, probably indicating a software self-modification.
      !(t1_mispredictInfo.bits.attribute === Mux(
        t1_longSlotHit,
        Mux1H(t1_longSlotHitOH, t1_bankMetaByWay.map(_.head.attribute)),
        Mux1H(t1_shortSlotHitOH, t1_bankMeta.map(_.attribute))
      ))
  )

  private val t1_updateEntry = Wire(new MonitorBtbEntry)
  t1_updateEntry.fusion := t1_updateIsFused
  t1_updateEntry.tag    := getTag(t1_bankStartPc)

  private val t1_updateLongSlot = Wire(new MonitorBtbLongSlot)
  t1_updateLongSlot.valid           := true.B
  t1_updateLongSlot.attribute       := t1_mispredictInfo.bits.attribute
  t1_updateLongSlot.position        := t1_mispredictInfo.bits.cfiPosition
  t1_updateLongSlot.targetCarry     := t1_targetCarry
  t1_updateLongSlot.targetLowerBits := getTargetLowerBits(t1_mispredictInfo.bits.target)
  t1_updateLongSlot.targetIndex     := genTargetIndex(t1_pageBtbWay, t1_pageBtbSetIdx)

  private val t1_updateShortSlot = Wire(new MonitorBtbShortSlot)
  t1_updateShortSlot.valid := true.B
  t1_updateShortSlot.attr.fromBranchAttribute(t1_mispredictInfo.bits.attribute)
  t1_updateShortSlot.position := t1_mispredictInfo.bits.cfiPosition
  t1_updateShortSlot.targetLowerBits := t1_mispredictInfo.bits.target(
    ShortTargetWidth + instOffsetBits - 1,
    instOffsetBits
  )

  private val t1_updatePageBtbEntry = Wire(new PageBtbEntry)
  t1_updatePageBtbEntry.valid    := true.B
  t1_updatePageBtbEntry.vpnLower := t1_vpnLower
  t1_updatePageBtbEntry.vpnIndex := t1_regionBtbWay

  private val t1_updateRegionBtbEntry = Wire(new RegionBtbEntry)
  t1_updateRegionBtbEntry.valid    := true.B
  t1_updateRegionBtbEntry.vpnUpper := t1_vpnUpper

  object UpdateActionType extends EnumUInt(6) {
    def None:               UInt = 0.U(width.W)
    def ReplaceSameTagSlot: UInt = 1.U(width.W)
    def BreakFusedWay:      UInt = 2.U(width.W)
    def RetagUnfusedWay:    UInt = 3.U(width.W)
    def ReplaceFusedWay:    UInt = 4.U(width.W)
    def ReplaceUnfusedPair: UInt = 5.U(width.W)
  }

  class UpdateAction extends Bundle {
    val actionType: UInt = UpdateActionType()
    val rrpv:       UInt = UInt(RrpvWidth.W)
    val way:        UInt = UInt(log2Ceil(NumWay).W)
    val slot:       UInt = UInt(log2Ceil(NumSlots).W)
  }

  // Calculate update action.
  // GEM5's UseInvalidWay / UseSameTagFreeSlot depend on per-slot valid bits, which are
  // not observable from training meta. The remaining actions compare by 2-bit RRPV only.
  // If multiple ways have the same victim score, use an LFSR-based random tie-break.
  private val t1_updateActionVec = Wire(Vec(NumWay, new UpdateAction))

  // Default to zero
  t1_updateActionVec.zipWithIndex.foreach { case (a, way) =>
    a.actionType := UpdateActionType.None
    a.rrpv       := MaxRrpv.U
    a.way        := way.U
    a.slot       := 0.U
  }

  Seq.tabulate(NumWay) { wayIdx =>
    val action      = t1_updateActionVec(wayIdx)
    val rrpvs       = t1_bankRrpvs(wayIdx)
    val meta        = t1_bankMetaByWay(wayIdx)
    val wayFused    = meta.head.fused
    val wayRawHit   = meta.map(_.rawHit).reduce(_ || _)
    val maxSlotRrpv = rrpvs.reduceLeft((a, b) => Mux(a > b, a, b))
    val minSlotRrpv = rrpvs.reduceLeft((a, b) => Mux(a < b, a, b))

    when(!t1_updateIsFused) {        // If update is unfused
      when(!wayFused && wayRawHit) { // If entry is unfused and only hit tag
        action.actionType := UpdateActionType.ReplaceSameTagSlot
        action.rrpv       := maxSlotRrpv
        action.slot       := PriorityEncoder(rrpvs.map(_ === maxSlotRrpv))
      }.elsewhen(!wayFused) { // If entry is unfused and not hit
        action.actionType := UpdateActionType.RetagUnfusedWay
        action.rrpv       := minSlotRrpv
        action.slot       := 0.U
      }.otherwise { // If current way is fused
        action.actionType := UpdateActionType.BreakFusedWay
        action.rrpv       := rrpvs(0) // fused entry has same rrpv for 2 slots
        action.slot       := 0.U
      }
    }.otherwise {      // If update is fused
      when(wayFused) { // If entry is fused
        action.actionType := UpdateActionType.ReplaceFusedWay
        action.rrpv       := rrpvs(0)
        action.slot       := 0.U
      }.otherwise { // If entry is unfused
        action.actionType := UpdateActionType.ReplaceUnfusedPair
        action.rrpv       := minSlotRrpv
        action.slot       := 0.U
      }
    }
  }

  private val t1_updateActionRrpvs = VecInit(t1_updateActionVec.map(_.rrpv))
  private val t1_finalMatrix       = CompareMatrix(t1_updateActionRrpvs, order = (a, b) => a > b)
  private val t1_finalRrpvOH       = t1_finalMatrix.getLeastElementOH(VecInit.fill(NumWay)(true.B))
  private val t1_finalAction       = Mux1H(t1_finalRrpvOH, t1_updateActionVec)

  // Miss Path
  private val t1_missEntry      = t1_updateEntry
  private val t1_missLongSlot   = t1_updateLongSlot
  private val t1_missShortSlots = Wire(Vec(NumSlots, new MonitorBtbShortSlot))
  private val t1_missWayMask    = UIntToOH(t1_finalAction.way, NumWay)
  private val t1_missSlotMask   = WireInit(0.U(NumSlots.W))
  private val t1_missSlots      = Mux(t1_missEntry.fusion, t1_missLongSlot.toShortSlots, t1_missShortSlots)
  private val t1_missRrpvs      = WireInit(t1_bankRrpvs)

  private val t1_bankVictimMatrix = CompareMatrix(VecInit(t1_bankRrpvs.flatten), order = (a, b) => a > b)
  private val t1_bankAgedRrpvs    = rrpvAging(VecInit(t1_bankRrpvs.flatten), t1_bankVictimMatrix)

  t1_missShortSlots.zipWithIndex.foreach { case (s, i) =>
    s := Mux(t1_finalAction.slot === i.U, t1_updateShortSlot, 0.U.asTypeOf(new MonitorBtbShortSlot))
  }
  switch(t1_finalAction.actionType) {
    is(UpdateActionType.ReplaceSameTagSlot) {
      t1_missSlotMask := UIntToOH(t1_finalAction.slot, NumSlots)
      t1_missRrpvs.zipWithIndex.foreach { case (rrpvs, way) =>
        rrpvs.zipWithIndex.foreach { case (r, slot) =>
          r := Mux(
            t1_finalAction.way === way.U && t1_finalAction.slot === slot.U,
            (MaxRrpv - 1).U,
            t1_bankAgedRrpvs(way * NumSlots + slot)
          )
        }
      }
    }
    is(UpdateActionType.BreakFusedWay, UpdateActionType.RetagUnfusedWay) {
      t1_missSlotMask := Fill(NumSlots, true.B)
      t1_missRrpvs.zipWithIndex.foreach { case (rrpvs, way) =>
        rrpvs.zipWithIndex.foreach { case (r, slot) =>
          if (slot == 0)
            r := Mux(t1_finalAction.way === way.U, (MaxRrpv - 1).U, t1_bankAgedRrpvs(way * NumSlots + slot))
          else
            r := Mux(t1_finalAction.way === way.U, MaxRrpv.U, t1_bankAgedRrpvs(way * NumSlots + slot))
        }
      }
    }
    is(UpdateActionType.ReplaceFusedWay, UpdateActionType.ReplaceUnfusedPair) {
      t1_missSlotMask := Fill(NumSlots, true.B)
      t1_missRrpvs.zipWithIndex.foreach { case (rrpvs, way) =>
        rrpvs.zipWithIndex.foreach { case (r, slot) =>
          r := Mux(t1_finalAction.way === way.U, (MaxRrpv - 1).U, t1_bankAgedRrpvs(way * NumSlots + slot))
        }
      }
    }
  }

  // Hit Path
  private val t1_hitEntry      = t1_updateEntry
  private val t1_hitLongSlot   = t1_updateLongSlot
  private val t1_hitShortSlots = Wire(Vec(NumSlots, new MonitorBtbShortSlot))
  private val t1_hitSlotMask   = Wire(UInt(NumSlots.W))
  private val t1_hitWayMask    = Wire(UInt(NumWay.W))
  private val t1_hitRrpvs      = Wire(Vec(NumWay, Vec(NumSlots, UInt(RrpvWidth.W))))
  private val t1_hitSlots      = Mux(t1_hitEntry.fusion, t1_hitLongSlot.toShortSlots, t1_hitShortSlots)

  private val t1_shortSlotHitWayOH = VecInit.tabulate(NumWay) { way =>
    Seq.tabulate(NumSlots)(s => t1_shortSlotHitOH(way * NumSlots + s)).reduce(_ || _)
  }
  private val t1_shortSlotHitSlotOH = VecInit.tabulate(NumSlots) { slot =>
    Seq.tabulate(NumWay)(w => t1_shortSlotHitOH(w * NumSlots + slot)).reduce(_ || _)
  }

  t1_hitWayMask  := Mux(t1_longSlotHit, t1_longSlotHitOH.asUInt, t1_shortSlotHitWayOH.asUInt)
  t1_hitSlotMask := Mux(t1_longSlotHit, Fill(NumSlots, true.B), t1_shortSlotHitSlotOH.asUInt)
  t1_hitShortSlots.zipWithIndex.foreach { case (s, i) =>
    s := Mux(t1_hitSlotMask(i), t1_updateShortSlot, 0.U.asTypeOf(new MonitorBtbShortSlot))
  }
  t1_hitRrpvs.zipWithIndex.foreach { case (rrpvs, way) =>
    when(t1_longSlotHit) {
      rrpvs.zipWithIndex.foreach { case (r, slot) =>
        r := Mux(t1_longSlotHitOH(way), 0.U, t1_bankAgedRrpvs(way * NumSlots + slot))
      }
    }.otherwise {
      rrpvs.zipWithIndex.foreach { case (r, slot) =>
        r := Mux(t1_shortSlotHitOH(way * NumSlots + slot), 0.U, t1_bankAgedRrpvs(way * NumSlots + slot))
      }
    }
  }

  private val t1_writeEntry    = t1_updateEntry
  private val t1_writeSlotMask = Mux(t1_hit, t1_hitSlotMask, t1_missSlotMask)
  private val t1_writeWayMask  = Mux(t1_hit, t1_hitWayMask, t1_missWayMask)
  private val t1_writeRrpvs    = Mux(t1_hit, t1_hitRrpvs, t1_missRrpvs)
  private val t1_writeSlots    = Mux(t1_hit, t1_hitSlots, t1_missSlots)

  // Update Counters
  private val t1_newCounters    = Wire(Vec(NumAlignBanks, Vec(NumWay * NumSlots, TakenCounter())))
  private val t1_counterWayMask = Wire(Vec(NumAlignBanks, Vec(NumWay * NumSlots, Bool())))

  t1_meta.entries.zipWithIndex.foreach { case (alignMeta, alignIdx) =>
    alignMeta.zipWithIndex.foreach { case (meta, idx) =>
      val way  = idx / NumSlots
      val slot = idx % NumSlots
      val hitMask =
        t1_branches.map(branch => branch.valid && branch.bits.attribute.isConditional && meta.hit(branch.bits))
      val actualTaken    = Mux1H(hitMask, t1_branches.map(_.bits.taken))
      val slotOverridden = t1_entryNeedWrite && t1_writeWayMask(way) && t1_writeSlotMask(slot)

      t1_counterWayMask(alignIdx)(idx) := slotOverridden || hitMask.reduce(_ || _)
      t1_newCounters(alignIdx)(idx) :=
        Mux(slotOverridden, TakenCounter.WeakPositive, meta.counter.getUpdate(actualTaken))
    }
  }

  pageBtb.io.write.req.valid        := t1_fire && t1_entryNeedWrite && t1_targetCarry.isInvalid && !t1_pageBtbHit
  pageBtb.io.write.req.bits.setIdx  := t1_pageBtbSetIdx
  pageBtb.io.write.req.bits.wayMask := UIntToOH(t1_pageBtbWay, NumPageBtbWays)
  pageBtb.io.write.req.bits.entry   := t1_updatePageBtbEntry

  when(t1_fire && t1_entryNeedWrite && t1_targetCarry.isInvalid) {
    pageBtbRrpvs(t1_pageBtbSetIdx)     := t1_pageBtbWriteRrpvs
    regionBtbRrpvs(t1_regionBtbSetIdx) := t1_regionBtbWriteRrpvs
    when(!t1_regionBtbHit) {
      regionBtb(t1_regionBtbSetIdx)(t1_regionBtbWay) := t1_updateRegionBtbEntry
    }
  }

  alignBanks.zipWithIndex.foreach { case (alignBank, alignIdx) =>
    alignBank.zipWithIndex.foreach { case (internalBank, internalIdx) =>
      // Write Entry
      internalBank.io.writeEntry.req.valid :=
        t1_fire && t1_entryNeedWrite && t1_writeAlignBankMask(alignIdx) &&
          t1_internalBankMaskVec(alignIdx)(internalIdx)
      internalBank.io.writeEntry.req.bits.setIdx   := t1_setIdxVec(alignIdx)
      internalBank.io.writeEntry.req.bits.wayMask  := t1_writeWayMask
      internalBank.io.writeEntry.req.bits.entry    := t1_writeEntry
      internalBank.io.writeEntry.req.bits.slotMask := t1_writeSlotMask
      internalBank.io.writeEntry.req.bits.slots    := t1_writeSlots

      // Write Counters
      internalBank.io.writeCounter.req.valid :=
        t1_fire && t1_counterWayMask(alignIdx).reduce(_ || _) &&
          t1_internalBankMaskVec(alignIdx)(internalIdx)
      internalBank.io.writeCounter.req.bits.setIdx   := t1_setIdxVec(alignIdx)
      internalBank.io.writeCounter.req.bits.wayMask  := t1_counterWayMask(alignIdx).asUInt
      internalBank.io.writeCounter.req.bits.counters := t1_newCounters(alignIdx)
    }
    when(t1_fire && t1_entryNeedWrite && t1_writeAlignBankMask(alignIdx)) {
      alignBankRrpvs(alignIdx)(getReplacerSetIndex(t1_startPcVec(alignIdx))) := t1_writeRrpvs
    }
  }

  // Multi-Hit
  private val s2_multiHitMaskVec = Seq.tabulate(NumAlignBanks) { alignIdx =>
    detectMultiHit(s2_hitMaskVec(alignIdx), s2_positionsVec(alignIdx))
  }
  private val s2_multiHitWayMaskVec = Seq.tabulate(NumAlignBanks) { alignIdx =>
    VecInit.tabulate(NumWay) { wayIdx =>
      VecInit.tabulate(NumSlots)(slotIdx => s2_multiHitMaskVec(alignIdx)(wayIdx * NumSlots + slotIdx)).reduce(_ || _)
    }.asUInt
  }

  alignBanks.zipWithIndex.foreach { case (alignBank, alignIdx) =>
    alignBank.zipWithIndex.foreach { case (internalBank, internalIdx) =>
      internalBank.io.flush.req.valid :=
        s2_fire && s2_multiHitMaskVec(alignIdx).orR &&
          s2_internalBankMaskVec(alignIdx)(internalIdx)
      internalBank.io.flush.req.bits.setIdx  := s2_setIdxVec(alignIdx)
      internalBank.io.flush.req.bits.wayMask := s2_multiHitWayMaskVec(alignIdx)
    }
  }

  /* *** MBTB Trace *** */
  private val mbtbTrace = Wire(new MBTBTrace)
  mbtbTrace.startPc             := t1_train.startPc
  mbtbTrace.trainBranchMask     := VecInit(t1_branches.map(_.valid)).asUInt
  mbtbTrace.trainCondMask       := VecInit(t1_branches.map(b => b.valid && b.bits.attribute.isConditional)).asUInt
  mbtbTrace.trainTakenMask      := VecInit(t1_branches.map(b => b.valid && b.bits.taken)).asUInt
  mbtbTrace.trainMispredictMask := VecInit(t1_branches.map(b => b.valid && b.bits.mispredict)).asUInt

  mbtbTrace.mispredictValid       := t1_mispredictInfo.valid
  mbtbTrace.mispredictCfiPosition := t1_mispredictInfo.bits.cfiPosition
  mbtbTrace.mispredictTaken       := t1_mispredictInfo.bits.taken
  mbtbTrace.mispredictTarget      := t1_mispredictInfo.bits.target
  mbtbTrace.mispredictAttribute   := t1_mispredictInfo.bits.attribute

  mbtbTrace.bankStartPc        := t1_bankStartPc
  mbtbTrace.writeAlignBankMask := t1_writeAlignBankMask.asUInt
  mbtbTrace.writeAlignBankIdx  := OHToUInt(t1_writeAlignBankMask)
  mbtbTrace.writeSetIdx        := Mux1H(t1_writeAlignBankMask, t1_setIdxVec)
  mbtbTrace.writeInternalIdx   := Mux1H(t1_writeAlignBankMask, t1_internalBankIdxVec)
  mbtbTrace.canUseShortSlot    := t1_canUseShortSlot
  mbtbTrace.updateIsFused      := t1_updateIsFused
  mbtbTrace.targetCarry        := t1_targetCarry

  mbtbTrace.hit            := t1_hit
  mbtbTrace.longSlotHit    := t1_longSlotHit
  mbtbTrace.longSlotHitOH  := t1_longSlotHitOH.asUInt
  mbtbTrace.shortSlotHit   := t1_shortSlotHit
  mbtbTrace.shortSlotHitOH := t1_shortSlotHitOH.asUInt

  mbtbTrace.entryNeedWrite   := t1_entryNeedWrite
  mbtbTrace.updateAction     := t1_finalAction.actionType
  mbtbTrace.updateActionWay  := t1_finalAction.way
  mbtbTrace.updateActionSlot := t1_finalAction.slot
  mbtbTrace.updateActionRrpv := t1_finalAction.rrpv
  mbtbTrace.writeWayMask     := t1_writeWayMask
  mbtbTrace.writeSlotMask    := t1_writeSlotMask
  mbtbTrace.writeEntry       := t1_writeEntry
  mbtbTrace.writeSlots       := t1_writeSlots
  mbtbTrace.writeRrpvs       := VecInit(t1_writeRrpvs.flatten)

  mbtbTrace.counterWriteValidMask := VecInit(t1_counterWayMask.map(_.reduce(_ || _))).asUInt
  mbtbTrace.counterWriteMask      := VecInit(t1_counterWayMask.flatten).asUInt
  mbtbTrace.counterValues         := VecInit(t1_newCounters.flatten)

  mbtbTrace.pageSetIdx  := t1_pageBtbSetIdx
  mbtbTrace.pageHit     := t1_pageBtbHit
  mbtbTrace.pageWay     := t1_pageBtbWay
  mbtbTrace.pageWrite   := t1_entryNeedWrite && t1_targetCarry.isInvalid && !t1_pageBtbHit
  mbtbTrace.pageEntry   := t1_updatePageBtbEntry
  mbtbTrace.regionHit   := t1_regionBtbHit
  mbtbTrace.regionWay   := t1_regionBtbWay
  mbtbTrace.regionWrite := t1_entryNeedWrite && t1_targetCarry.isInvalid && !t1_regionBtbHit
  mbtbTrace.regionEntry := t1_updateRegionBtbEntry

  private val mbtbTraceDBTable = ChiselDB.createTable("MBTBTrace", new MBTBTrace, EnableMainbtbTrace)
  mbtbTraceDBTable.log(
    data = mbtbTrace,
    en = t1_fire,
    clock = clock,
    reset = reset
  )

  /* *** statistics *** */
  private val perf_s2HitMask          = VecInit(s2_hitMaskVec.flatten)
  private val perf_s2HasHit           = perf_s2HitMask.asUInt.orR
  private val perf_s2HasMultiHit      = VecInit(s2_multiHitMaskVec.map(_.orR)).asUInt.orR
  private val perf_s2HasLongJump      = s2_firstLongJumpOH.asUInt.orR
  private val perf_t1CounterWriteMask = VecInit(t1_counterWayMask.flatten)
  private val perf_t1CounterWrite     = perf_t1CounterWriteMask.asUInt.orR
  private val perf_t1PageWrite        = t1_entryNeedWrite && t1_targetCarry.isInvalid && !t1_pageBtbHit
  private val perf_t1RegionWrite      = t1_entryNeedWrite && t1_targetCarry.isInvalid && !t1_regionBtbHit

  XSPerfAccumulate("predict_total", s2_fire)
  XSPerfAccumulate("predict_hit", s2_fire && perf_s2HasHit)
  XSPerfAccumulate("predict_miss", s2_fire && !perf_s2HasHit)
  XSPerfAccumulate("predict_multi_hit", s2_fire && perf_s2HasMultiHit)
  XSPerfAccumulate("predict_long_jump", s2_fire && perf_s2HasLongJump)
  XSPerfAccumulate("predict_long_jump_taken", s3_fire && s3_firstLongJumpTaken)
  XSPerfHistogram("predict_hit_count", PopCount(perf_s2HitMask), s2_fire, 0, NumBtbResultEntries + 1)

  XSPerfAccumulate("train_total", t1_fire)
  XSPerfAccumulate("train_has_mispredict", t1_fire && t1_mispredictInfo.valid)
  XSPerfAccumulate("train_hit", t1_fire && t1_mispredictInfo.valid && t1_hit)
  XSPerfAccumulate("train_miss", t1_fire && t1_mispredictInfo.valid && !t1_hit)
  XSPerfAccumulate("train_entry_write", t1_fire && t1_entryNeedWrite)
  XSPerfAccumulate("train_counter_write", t1_fire && perf_t1CounterWrite)
  XSPerfAccumulate("train_page_write", t1_fire && perf_t1PageWrite)
  XSPerfAccumulate("train_region_write", t1_fire && perf_t1RegionWrite)
  XSPerfAccumulate("train_short_target", t1_fire && t1_mispredictInfo.valid && t1_canUseShortSlot)
  XSPerfAccumulate("train_long_target", t1_fire && t1_mispredictInfo.valid && !t1_canUseShortSlot)
  XSPerfAccumulate(
    "train_replace_same_tag_slot",
    t1_fire && t1_entryNeedWrite && t1_finalAction.actionType === UpdateActionType.ReplaceSameTagSlot
  )
  XSPerfAccumulate(
    "train_break_fused_way",
    t1_fire && t1_entryNeedWrite && t1_finalAction.actionType === UpdateActionType.BreakFusedWay
  )
  XSPerfAccumulate(
    "train_retag_unfused_way",
    t1_fire && t1_entryNeedWrite && t1_finalAction.actionType === UpdateActionType.RetagUnfusedWay
  )
  XSPerfAccumulate(
    "train_replace_fused_way",
    t1_fire && t1_entryNeedWrite && t1_finalAction.actionType === UpdateActionType.ReplaceFusedWay
  )
  XSPerfAccumulate(
    "train_replace_unfused_pair",
    t1_fire && t1_entryNeedWrite && t1_finalAction.actionType === UpdateActionType.ReplaceUnfusedPair
  )
  XSPerfHistogram("train_branch_count", PopCount(t1_branches.map(_.valid)), t1_fire, 0, ResolveEntryBranchNumber + 1)
  XSPerfHistogram("train_counter_write_count", PopCount(perf_t1CounterWriteMask), t1_fire, 0, NumBtbResultEntries + 1)
  XSPerfHistogram("train_write_way_count", PopCount(t1_writeWayMask), t1_fire && t1_entryNeedWrite, 0, NumWay + 1)
  XSPerfHistogram("train_write_slot_count", PopCount(t1_writeSlotMask), t1_fire && t1_entryNeedWrite, 0, NumSlots + 1)

}
