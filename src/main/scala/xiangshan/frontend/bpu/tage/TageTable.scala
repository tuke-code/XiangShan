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

package xiangshan.frontend.bpu.tage

import chisel3._
import chisel3.util._
import org.chipsalliance.cde.config.Parameters
import utility.XSPerfAccumulate
import utility.sram.SRAMTemplate
import xiangshan.frontend.bpu.SaturateCounter
import xiangshan.frontend.bpu.TageTableInfo
import xiangshan.frontend.bpu.WriteBuffer

class TageTable(
    tableIdx:          Int,
    implicit val info: TageTableInfo // declare info as implicit val to pass it to Bundles / methods like TableReadReq
)(implicit p: Parameters) extends TageModule with TableHelper {
  class TageTableIO extends TageBundle {
    val predictReadReq:      Valid[TableReadReq]  = Flipped(Valid(new TableReadReq))
    val trainReadReq:        Valid[TableReadReq]  = Flipped(Valid(new TableReadReq))
    val predictReadResp:     TableReadResp        = Output(new TableReadResp)
    val trainReadResp:       TableReadResp        = Output(new TableReadResp)
    val writeReq:            Valid[TableWriteReq] = Flipped(Valid(new TableWriteReq))
    val usefulResetStart:    Bool                 = Input(Bool())
    val usefulResetInFlight: Bool                 = Output(Bool())
    val initDone:            Bool                 = Output(Bool())
  }

  val io: TageTableIO = IO(new TageTableIO)

  println(f"TageTable[$tableIdx]:")
  println(f"  Size(set, bank, way): $NumSets * $NumBanks * $NumWays = ${info.Size}")
  println(f"  History length: ${info.HistoryLength}")
  println(f"  Address fields:")
  addrFields.show(indent = 4)

  private val entrySram =
    Seq.tabulate(NumBanks, NumWays) { (bankIdx, wayIdx) =>
      Module(new SRAMTemplate(
        new TageEntry,
        set = NumSets,
        way = 1,
        singlePort = true,
        shouldReset = true,
        withClockGate = true,
        hasMbist = hasMbist,
        hasSramCtl = hasSramCtl,
        suffix = Option("bpu_tage_entry")
      )).suggestName(s"tage_entry_sram_bank${bankIdx}_way${wayIdx}")
    }

  // TODO: use folded sram
  private val usefulSram =
    Seq.tabulate(NumBanks, NumWays) { (bankIdx, wayIdx) =>
      Module(new SRAMTemplate(
        UsefulCounter(),
        set = NumSets,
        way = 1,
        singlePort = true,
        shouldReset = true,
        withClockGate = true,
        hasMbist = hasMbist,
        hasSramCtl = hasSramCtl,
        suffix = Option("bpu_tage_useful")
      )).suggestName(s"tage_useful_sram_bank${bankIdx}_way${wayIdx}")
    }

  private val usefulResetSetIdx       = RegInit(VecInit.fill(NumBanks)(0.U(SetIdxWidth.W)))
  private val usefulResetGrantedMask  = RegInit(VecInit.fill(NumBanks)(false.B))
  private val usefulResetInFlightMask = RegInit(VecInit.fill(NumBanks)(false.B))
  private val usefulResetInFlight     = usefulResetInFlightMask.reduce(_ || _)

  when(io.usefulResetStart && !usefulResetInFlight) {
    usefulResetInFlightMask := VecInit.fill(NumBanks)(false.B)
    usefulResetSetIdx.foreach(_ := 0.U)
  }
  io.usefulResetInFlight := usefulResetInFlight

  // use a write buffer to store a entrySram write request
  private val entryWriteBuffers =
    Seq.tabulate(NumBanks) { bankIdx =>
      Module(new WriteBuffer(
        new EntrySramWriteReq,
        WriteBufferSize,
        numPorts = NumWays,
        hasCnt = true,
        nameSuffix = s"tageTable${tableIdx}_${bankIdx}"
      )).suggestName(s"tage_entry_write_buffer_bank${bankIdx}")
    }

  // read sram
  private val predictReadMask = Wire(Vec(NumBanks, Bool()))
  private val trainReadMask   = Wire(Vec(NumBanks, Bool()))
  entrySram.zip(usefulSram).zipWithIndex.foreach { case ((entryBank, usefulBank), bankIdx) =>
    val predictReadValid = io.predictReadReq.valid && io.predictReadReq.bits.bankMask(bankIdx)
    val trainReadValid   = io.trainReadReq.valid && io.trainReadReq.bits.bankMask(bankIdx)
    predictReadMask(bankIdx) := predictReadValid
    trainReadMask(bankIdx)   := trainReadValid

    val readValid  = predictReadValid || trainReadValid
    val readSetIdx = Mux(predictReadValid, io.predictReadReq.bits.setIdx, io.trainReadReq.bits.setIdx)

    entryBank.foreach { way =>
      way.io.r.req.valid       := readValid
      way.io.r.req.bits.setIdx := readSetIdx
    }
    usefulBank.foreach { way =>
      way.io.r.req.valid       := readValid
      way.io.r.req.bits.setIdx := readSetIdx
    }
    assert(!(predictReadValid && trainReadValid), s"read conflict in tage_table_${tableIdx}_bank_${bankIdx}")
  }

  // delay one cycle for better timing
  private val writeReqValid = RegNext(io.writeReq.valid, init = false.B)
  private val writeReq      = RegEnable(io.writeReq.bits, io.writeReq.valid)

  // write to write buffer
  entryWriteBuffers.zipWithIndex.foreach { case (buffer, bankIdx) =>
    buffer.io.write.zipWithIndex.foreach { case (writePort, wayIdx) =>
      writePort.valid          := writeReqValid && writeReq.bankMask(bankIdx) && writeReq.wayMask(wayIdx)
      writePort.bits.setIdx    := writeReq.setIdx
      writePort.bits.entry     := writeReq.entries(wayIdx)
      writePort.bits.usefulCtr := writeReq.usefulCtrs(wayIdx)
    }
    buffer.io.takenMask.get := writeReq.actualTakenMask
  }

  // write to sram from write buffer
  entrySram.zip(usefulSram).zip(entryWriteBuffers).zipWithIndex foreach {
    case (((entryBank, usefulBank), buffer), bankIdx) =>
      val hasRead = predictReadMask(bankIdx) || trainReadMask(bankIdx)
      val writeWayMask = buffer.io.read.zip(entryBank).map { case (readPort, entryWay) =>
        readPort.valid && !entryWay.io.r.req.valid
      }
      val hasWrite           = writeWayMask.reduce(_ || _)
      val usefulResetGranted = usefulResetInFlightMask(bankIdx) && !hasRead && !hasWrite
      usefulResetGrantedMask(bankIdx) := usefulResetGranted

      entryBank.zip(usefulBank).zip(buffer.io.read).zipWithIndex.foreach {
        case (((entryWay, usefulWay), readPort), wayIdx) =>
          val writeValid  = writeWayMask(wayIdx)
          val writeSetIdx = readPort.bits.setIdx

          entryWay.io.w.apply(writeValid, readPort.bits.entry, writeSetIdx, 1.U(1.W))
          usefulWay.io.w.apply(
            writeValid || usefulResetGranted,
            Mux(writeValid, readPort.bits.usefulCtr, UsefulCounter.Zero),
            Mux(writeValid, writeSetIdx, usefulResetSetIdx(bankIdx)),
            1.U(1.W)
          )

          readPort.ready := writeValid && entryWay.io.w.req.ready && usefulWay.io.w.req.ready
      }
  }

  when(usefulResetInFlight) {
    (0 until NumBanks).foreach { bankIdx =>
      when(usefulResetGrantedMask(bankIdx)) {
        when(usefulResetSetIdx(bankIdx) === (NumSets - 1).U) {
          usefulResetInFlightMask(bankIdx) := false.B
        }.otherwise {
          usefulResetSetIdx(bankIdx) := usefulResetSetIdx(bankIdx) + 1.U
        }
      }
    }
  }

  private val predictReadBankMaskNext = RegEnable(io.predictReadReq.bits.bankMask, io.predictReadReq.valid)
  io.predictReadResp.entries := Mux1H(
    predictReadBankMaskNext,
    entrySram.map(bank => VecInit(bank.map(way => way.io.r.resp.data.head)))
  )
  io.predictReadResp.usefulCtrs := Mux1H(
    predictReadBankMaskNext,
    usefulSram.map(bank => VecInit(bank.map(way => way.io.r.resp.data.head)))
  )

  private val trainReadBankMaskNext = RegEnable(io.trainReadReq.bits.bankMask, io.trainReadReq.valid)
  io.trainReadResp.entries := Mux1H(
    trainReadBankMaskNext,
    entrySram.map(bank => VecInit(bank.map(way => way.io.r.resp.data.head)))
  )
  io.trainReadResp.usefulCtrs := Mux1H(
    trainReadBankMaskNext,
    usefulSram.map(bank => VecInit(bank.map(way => way.io.r.resp.data.head)))
  )

  io.initDone := (entrySram.flatten ++ usefulSram.flatten).map(_.io.r.req.ready).reduce(_ && _)

  XSPerfAccumulate("predict_read", io.predictReadReq.valid)
  XSPerfAccumulate("train_read", io.trainReadReq.valid)
  XSPerfAccumulate("write", io.writeReq.valid)
  XSPerfAccumulate(
    "drop_write",
    PopCount(entryWriteBuffers.flatMap(writePorts => writePorts.io.write.map(p => p.valid && !p.ready)))
  )
}
