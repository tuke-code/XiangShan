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
import utility.ReplacementPolicy
import utility.XSError
import utility.XSPerfAccumulate
import utility.XSPerfHistogram

class MonitorBtbWriteBuffer(
    numEntries: Int = 1,
    numPorts:   Int = 1,
    nameSuffix: String = ""
)(implicit p: Parameters) extends MainBtbModule {
  private type T = MonitorBtbEntrySramWriteReq
  require(numEntries >= 0)
  require(numPorts >= 1)
  require(numPorts <= numEntries)

  class WriteBufferIO extends Bundle {
    val write: Vec[DecoupledIO[T]] = Vec(numPorts, Flipped(DecoupledIO(new T)))
    val read:  Vec[DecoupledIO[T]] = Vec(numPorts, DecoupledIO(new T))
  }
  val io: WriteBufferIO = IO(new WriteBufferIO)

  private val namePrefix = s"MonitorWriteBuffer_${nameSuffix}"

  private def mergeSameSlot(
      entry:    T,
      writeGen: T
  ): T = {
    val merged        = WireInit(entry)
    val fusionChanged = entry.entry.fusion =/= writeGen.entry.fusion
    when(fusionChanged) {
      merged          := writeGen
      merged.slotMask := Fill(NumSlots, true.B)
    }.otherwise {
      when(writeGen.entry.fusion) {
        merged := writeGen
      }.elsewhen(writeGen.retagged || entry.retagged) {
        Seq.tabulate(NumSlots) { s =>
          if (s == 0) merged.slots(s) := writeGen.effectiveShortSlot
          else merged.slots(s)        := 0.U.asTypeOf(new MonitorBtbShortSlot)
        }
        merged.slotMask := Fill(NumSlots, true.B)
      }.otherwise {
        Seq.tabulate(NumSlots) { s =>
          merged.slots(s) := Mux(entry.slotMask(s), writeGen.effectiveShortSlot, entry.slots(s))
        }
      }
    }
    merged.genEffectiveFields()
    merged
  }

  private def slotChanged(
      entry:    T,
      writeGen: T
  ): Bool =
    entry.entry.fusion =/= writeGen.entry.fusion ||
      entry.compareKey =/= writeGen.compareKey ||
      Mux(
        writeGen.entry.fusion,
        entry.getEffectiveLongSlot.asUInt =/= writeGen.getEffectiveLongSlot.asUInt,
        entry.effectiveShortSlot.asUInt =/= writeGen.effectiveShortSlot.asUInt
      )

  private val needWrite = RegInit(VecInit(Seq.fill(numPorts)(VecInit(Seq.fill(numEntries)(false.B)))))
  private val entries   = RegInit(VecInit(Seq.fill(numPorts)(VecInit(Seq.fill(numEntries)(0.U.asTypeOf(new T))))))
  private val valids    = RegInit(VecInit(Seq.fill(numPorts)(VecInit(Seq.fill(numEntries)(false.B)))))

  private val s1Valid        = RegInit(VecInit(Seq.fill(numPorts)(false.B)))
  private val s1Req          = RegInit(VecInit(Seq.fill(numPorts)(0.U.asTypeOf(new T))))
  private val s1Hit          = RegInit(VecInit(Seq.fill(numPorts)(false.B)))
  private val s1HitRowIdx    = RegInit(VecInit(Seq.fill(numPorts)(0.U(log2Ceil(numPorts).W))))
  private val s1HitEntryIdx  = RegInit(VecInit(Seq.fill(numPorts)(0.U(log2Ceil(numEntries).W))))
  private val s1VictimIdx    = RegInit(VecInit(Seq.fill(numPorts)(0.U(log2Ceil(numEntries).W))))
  private val s1OldEntry     = RegInit(VecInit(Seq.fill(numPorts)(0.U.asTypeOf(new T))))
  private val s1OldValid     = RegInit(VecInit(Seq.fill(numPorts)(false.B)))
  private val s1OldNeedWrite = RegInit(VecInit(Seq.fill(numPorts)(false.B)))

  private val stage1HitTouchVec =
    WireInit(VecInit.fill(numPorts)(VecInit.fill(numEntries)(0.U.asTypeOf(Valid(UInt(log2Ceil(numEntries).W))))))
  private val stage1WriteTouchVec =
    WireInit(VecInit.fill(numPorts)(0.U.asTypeOf(Valid(UInt(log2Ceil(numEntries).W)))))
  private val replacerWay = Wire(Vec(numPorts, UInt(log2Ceil(numEntries).W)))

  private val curEntries   = WireInit(entries)
  private val curValids    = WireInit(valids)
  private val curNeedWrite = WireInit(needWrite)

  private val stage1UpdateEntry = WireInit(VecInit(Seq.fill(numPorts)(0.U.asTypeOf(new T))))
  private val stage1ApplyUpdate = WireInit(VecInit(Seq.fill(numPorts)(false.B)))
  private val stage1EntryChange = WireInit(VecInit(Seq.fill(numPorts)(false.B)))
  private val stage1UpdateRow   = WireInit(VecInit(Seq.fill(numPorts)(0.U(log2Ceil(numPorts).W))))
  private val stage1UpdateIdx   = WireInit(VecInit(Seq.fill(numPorts)(0.U(log2Ceil(numEntries).W))))

  for (portIdx <- 0 until numPorts) {
    when(s1Valid(portIdx)) {
      when(s1Hit(portIdx)) {
        val merged = mergeSameSlot(s1OldEntry(portIdx), s1Req(portIdx))
        stage1UpdateEntry(portIdx) := merged
        stage1EntryChange(portIdx) := slotChanged(s1OldEntry(portIdx), merged)
        stage1ApplyUpdate(portIdx) := s1OldNeedWrite(portIdx) || stage1EntryChange(portIdx)
        stage1UpdateRow(portIdx)   := s1HitRowIdx(portIdx)
        stage1UpdateIdx(portIdx)   := s1HitEntryIdx(portIdx)
        stage1HitTouchVec(s1HitRowIdx(portIdx))(s1HitEntryIdx(portIdx)).valid := true.B
        stage1HitTouchVec(s1HitRowIdx(portIdx))(s1HitEntryIdx(portIdx)).bits  := s1HitEntryIdx(portIdx)
      }.otherwise {
        stage1UpdateEntry(portIdx) := s1Req(portIdx)
        stage1ApplyUpdate(portIdx) := true.B
        stage1UpdateRow(portIdx)   := portIdx.U
        stage1UpdateIdx(portIdx)   := s1VictimIdx(portIdx)
        stage1WriteTouchVec(portIdx).valid := true.B
        stage1WriteTouchVec(portIdx).bits  := s1VictimIdx(portIdx)
      }
    }
  }

  for (portIdx <- 0 until numPorts) {
    when(stage1ApplyUpdate(portIdx)) {
      curEntries(stage1UpdateRow(portIdx))(stage1UpdateIdx(portIdx)) := stage1UpdateEntry(portIdx)
      curValids(stage1UpdateRow(portIdx))(stage1UpdateIdx(portIdx))  := true.B
      when(s1Hit(portIdx)) {
        when(stage1EntryChange(portIdx)) {
          curNeedWrite(stage1UpdateRow(portIdx))(stage1UpdateIdx(portIdx)) := true.B
        }
      }.otherwise {
        curNeedWrite(stage1UpdateRow(portIdx))(stage1UpdateIdx(portIdx)) := true.B
      }
    }
  }

  private val readValidVec = Wire(Vec(numPorts, Vec(numEntries, Bool())))
  private val readReadyVec = Wire(Vec(numPorts, Bool()))
  private val emptyVec     = Wire(Vec(numPorts, Bool()))
  private val fullVec      = Wire(Vec(numPorts, Bool()))
  private val readIdxVec   = Wire(Vec(numPorts, UInt(log2Ceil(numEntries).W)))
  private val nextNeedWrite = WireInit(curNeedWrite)

  for (rowIdx <- 0 until numPorts) {
    val replacer = ReplacementPolicy.fromString("plru", numEntries)
    readReadyVec(rowIdx) := io.read(rowIdx).ready
    readValidVec(rowIdx) := curNeedWrite(rowIdx)
    emptyVec(rowIdx)     := !readValidVec(rowIdx).reduce(_ || _)
    fullVec(rowIdx)      := readValidVec(rowIdx).reduce(_ && _)
    readIdxVec(rowIdx)   := PriorityEncoder(readValidVec(rowIdx))

    io.write(rowIdx).ready := !fullVec(rowIdx)
    io.read(rowIdx).valid  := !emptyVec(rowIdx)
    io.read(rowIdx).bits   := curEntries(rowIdx)(readIdxVec(rowIdx))

    when(readReadyVec(rowIdx) && !emptyVec(rowIdx)) {
      nextNeedWrite(rowIdx)(readIdxVec(rowIdx)) := false.B
    }

    replacerWay(rowIdx) := replacer.way
    replacer.access(Seq(stage1WriteTouchVec(rowIdx)) ++ stage1HitTouchVec(rowIdx))

    XSPerfAccumulate(f"${namePrefix}_port${rowIdx}_is_full", io.write(rowIdx).valid && fullVec(rowIdx))
    XSPerfHistogram(
      f"${namePrefix}_port${rowIdx}_useful",
      PopCount(readValidVec(rowIdx)),
      readReadyVec(rowIdx),
      0,
      numEntries
    )
  }

  private val victimSameSetIdx = Wire(Vec(numPorts, Valid(UInt(log2Ceil(numEntries).W))))
  private val writeHit = Wire(Vec(numPorts, Bool()))
  private val writeHitRowIdx = Wire(Vec(numPorts, UInt(log2Ceil(numPorts).W)))
  private val writeHitEntryIdx = Wire(Vec(numPorts, UInt(log2Ceil(numEntries).W)))
  private val writeVictimIdx = Wire(Vec(numPorts, UInt(log2Ceil(numEntries).W)))
  private val writeOldEntry = Wire(Vec(numPorts, new T))
  private val writeOldValid = Wire(Vec(numPorts, Bool()))
  private val writeOldNeedWrite = Wire(Vec(numPorts, Bool()))

  for (portIdx <- 0 until numPorts) {
    val writeValid = io.write(portIdx).valid
    val writeBits  = io.write(portIdx).bits
    val setIdxHitVec = curEntries(portIdx).zip(curValids(portIdx)).map { case (entry, valid) =>
      writeValid && valid && writeBits.setIdx === entry.setIdx
    }
    XSError(
      writeValid && PopCount(setIdxHitVec) > 1.U,
      f"$nameSuffix port${portIdx}_hitMask should be no more than 1"
    )
    victimSameSetIdx(portIdx).valid := setIdxHitVec.reduce(_ || _)
    victimSameSetIdx(portIdx).bits  := OHToUInt(VecInit(setIdxHitVec))
  }

  for {
    i <- 0 until numPorts
    j <- i + 1 until numPorts
  } {
    val writeSameEntry =
      io.write(i).valid && io.write(j).valid && io.write(i).bits.setIdx === io.write(j).bits.setIdx &&
        io.write(i).bits.compareKey === io.write(j).bits.compareKey
    XSError(
      writeSameEntry,
      f"Cannot write same data simultaneously, $nameSuffix port$i and $j violated"
    )
  }

  for (portIdx <- 0 until numPorts) {
    val hitMask = Wire(Vec(numPorts, Vec(numEntries, Bool())))
    for (p <- 0 until numPorts; e <- 0 until numEntries) {
      hitMask(p)(e) := io.write(portIdx).valid && curValids(p)(e) &&
        io.write(portIdx).bits.setIdx === curEntries(p)(e).setIdx &&
        io.write(portIdx).bits.compareKey === curEntries(p)(e).compareKey
    }

    XSError(
      io.write(portIdx).valid && PopCount(hitMask.flatten) > 1.U,
      f"$nameSuffix port${portIdx}_hitMask should be no more than 1"
    )

    val hitRowsVec   = VecInit(hitMask.map(_.reduce(_ || _)))
    val hitRowIdxVec = VecInit(hitMask.map(OHToUInt(_)))
    val hit          = hitRowsVec.reduce(_ || _)
    val rowIdx       = OHToUInt(hitRowsVec)
    val hitIdx       = hitRowIdxVec(rowIdx)
    val notUsefulVec = curNeedWrite(portIdx).map(!_)
    val notUseful    = notUsefulVec.reduce(_ || _)
    val notUsefulIdx = PriorityEncoder(notUsefulVec)

    writeHit(portIdx)        := hit
    writeHitRowIdx(portIdx)  := rowIdx
    writeHitEntryIdx(portIdx) := hitIdx
    writeVictimIdx(portIdx) := Mux(
      victimSameSetIdx(portIdx).valid,
      victimSameSetIdx(portIdx).bits,
      Mux(notUseful, notUsefulIdx, replacerWay(portIdx))
    )
    writeOldEntry(portIdx)     := curEntries(rowIdx)(hitIdx)
    writeOldValid(portIdx)     := curValids(rowIdx)(hitIdx)
    writeOldNeedWrite(portIdx) := curNeedWrite(rowIdx)(hitIdx)

    XSPerfAccumulate(f"${namePrefix}_port${portIdx}_hit_not_written", io.write(portIdx).valid && hit && writeOldNeedWrite(portIdx))
    XSPerfAccumulate(f"${namePrefix}_port${portIdx}_hit_written", io.write(portIdx).valid && hit && !writeOldNeedWrite(portIdx))
    XSPerfAccumulate(f"${namePrefix}_port${portIdx}_hit", io.write(portIdx).valid && hit)
    XSPerfAccumulate(f"${namePrefix}_port${portIdx}_not_hit", io.write(portIdx).valid && !hit)
  }

  for (portIdx <- 0 until numPorts) {
    when(io.write(portIdx).fire) {
      s1Valid(portIdx)        := true.B
      s1Req(portIdx)          := io.write(portIdx).bits
      s1Hit(portIdx)          := writeHit(portIdx)
      s1HitRowIdx(portIdx)    := writeHitRowIdx(portIdx)
      s1HitEntryIdx(portIdx)  := writeHitEntryIdx(portIdx)
      s1VictimIdx(portIdx)    := writeVictimIdx(portIdx)
      s1OldEntry(portIdx)     := writeOldEntry(portIdx)
      s1OldValid(portIdx)     := writeOldValid(portIdx)
      s1OldNeedWrite(portIdx) := writeOldNeedWrite(portIdx)
    }.otherwise {
      s1Valid(portIdx) := false.B
    }
  }

  entries   := curEntries
  valids    := curValids
  needWrite := nextNeedWrite
}
