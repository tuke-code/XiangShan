// Copyright (c) 2024-2025 Beijing Institute of Open Source Chip (BOSC)
// Copyright (c) 2020-2025 Institute of Computing Technology, Chinese Academy of Sciences
// Copyright (c) 2020-2021 Peng Cheng Laboratory

// XiangShan is licensed under Mulan PSL v2.
// You can use this software according to the terms and conditions of the Mulan PSL v2.
// You may obtain a copy of Mulan PSL v2 at:
//          https://license.coscl.org.cn/MulanPSL2

// THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
// EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
// MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.

// See the Mulan PSL v2 for more details.

package xiangshan.mem.mdp.NewMdp

import chisel3._
import chisel3.util._
import xiangshan._
import utils._
import utility._
import org.chipsalliance.cde.config.Parameters
import utility.XSPerfAccumulate
import utility.sram.SRAMTemplate
import xiangshan.frontend.bpu.SaturateCounter
import xiangshan.frontend.bpu.SaturateCounterInit
import xiangshan.frontend.bpu.SaturateCounterFactory
import xiangshan.frontend.PrunedAddr
import xiangshan.frontend.bpu.tage.TableWriteReq
import xiangshan.frontend.bpu.WriteBuffer
import xiangshan.frontend.bpu.WriteReqBundle


object TakenCounter extends SaturateCounterFactory {
  def width(implicit p: Parameters): Int =
    p(XSCoreParamsKey).frontendParameters.bpuParameters.mdpTageTableParameters.TakenCtrWidth
}

object UsefulCounter extends SaturateCounterFactory {
  def width(implicit p: Parameters): Int =
    p(XSCoreParamsKey).frontendParameters.bpuParameters.mdpTageTableParameters.UsefulCtrWidth

  def Init(implicit p: Parameters): SaturateCounter =
    SaturateCounterInit(width, 0)
  def InitWeak(implicit p: Parameters): SaturateCounter =
    SaturateCounterInit(width, 1) //TODO:更改为宏
  def InitStrong(implicit p: Parameters): SaturateCounter =
    SaturateCounterInit(width, 6)
}

object BypassCounter extends SaturateCounterFactory {
  def width(implicit p: Parameters): Int =
    p(XSCoreParamsKey).frontendParameters.bpuParameters.mdpTageTableParameters.BypassCtrWidth

  def Init(implicit p: Parameters): SaturateCounter =
    SaturateCounterInit(width, 0)
  def InitWeak(implicit p: Parameters): SaturateCounter =
    SaturateCounterInit(width, 1)
}

class TageEntry(implicit p: Parameters) extends XSBundle with HasMdpTageTableParameters{
  val valid:    Bool            = Bool()
  val tag:      UInt            = UInt(TagWidth.W)
  val distance: UInt            = UInt(RobDistance.W)
  val bypassCtr: SaturateCounter = BypassCounter()
}

class MdpTableReadReq(implicit p: Parameters, info: MdpTageTableInfo) extends XSBundle with HasMdpTageTableParameters{
  val setIdx:   UInt = UInt(SetIdxWidth.W)
  val bankMask: UInt = UInt(NumBanks.W)
}

class MdpTableReadResp(implicit p: Parameters, info: MdpTageTableInfo) extends XSBundle with HasMdpTageTableParameters{
  val entries:    Vec[TageEntry]       = Vec(NumWays, new TageEntry)
  val usefulCtrs: Vec[SaturateCounter] = Vec(NumWays, UsefulCounter())
}

class MdpTableWriteReq(implicit p: Parameters, info: MdpTageTableInfo) extends XSBundle with HasMdpTageTableParameters{ 
  val setIdx:          UInt                 = UInt(SetIdxWidth.W)
  val bankMask:        UInt                 = UInt(NumBanks.W)
  val wayMask:         UInt                 = UInt(NumWays.W)
  val entries:         Vec[TageEntry]       = Vec(NumWays, new TageEntry)
  val usefulCtrs:      Vec[SaturateCounter] = Vec(NumWays, UsefulCounter())
}

class MdpAllWayWeakReq(implicit p: Parameters, info: MdpTageTableInfo) extends XSBundle with HasMdpTageTableParameters{ 
  val setIdx:          UInt                 = UInt(SetIdxWidth.W)
  val bankIdx:         UInt                 = UInt(BankIdxWidth.W)
  val usefulCtrs:      Vec[SaturateCounter] = Vec(NumWays, UsefulCounter())
}

class MdpTageFoldedHist(implicit p: Parameters, info: MdpTageTableInfo) extends XSBundle with HasMdpTageTableParameters{
  val forIdx: UInt = UInt(MaxSetIdxWidth.W) //TODO:
  val forTag: UInt = UInt(TagWidth.W)
}

class MdpEntrySramWriteReq(implicit p: Parameters, info: MdpTageTableInfo) extends WriteReqBundle
    with HasMdpTageTableParameters {
  val setIdx:       UInt                    = UInt(SetIdxWidth.W)
  val entry:        TageEntry               = new TageEntry
  val usefulCtr:    SaturateCounter         = UsefulCounter()
  override def tag: Option[UInt]            = Some(entry.tag)
}

class MdpTageTable(
  tableIdx:          Int,
  implicit val info: MdpTageTableInfo // declare info as implicit val to pass it to Bundles / methods like TableReadReq
)(implicit p: Parameters) extends XSModule with TableHelper{
  val io = IO(new Bundle {
    val predictReadReq  = Flipped(Valid(new MdpTableReadReq))
    val trainReadReq    = Flipped(Valid(new MdpTableReadReq))
    val predictReadResp = Output(new MdpTableReadResp)
    val trainReadResp   = Output(new MdpTableReadResp)
    val writeReq        = Flipped(Valid(new MdpTableWriteReq))
    val allWayWeakReq   = Flipped(Valid(new MdpAllWayWeakReq))
    val resetUseful:     Bool                 = Input(Bool())
    val resetDone:       Bool                 = Output(Bool())
  })    
  /* submodules */

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
        suffix = Option("mdp_tage")
      )).suggestName(s"tage_entry_sram_bank${bankIdx}_way${wayIdx}")
    }

  // TODO: use SRAM to implement it
  private val usefulCtrs = RegInit(
    VecInit.fill(NumBanks)(
      VecInit.fill(NumWays)(
        VecInit.fill(NumSets)(
          UsefulCounter.Zero
        )
      )
    )
  )

  // use a write buffer to store a entrySram write request
  // TODO: add writeBuffer multi port simultaneous writing
  private val entryWriteBuffers =
    Seq.tabulate(NumBanks) { bankIdx =>
      Module(new WriteBuffer(
        new MdpEntrySramWriteReq,
        WriteBufferSize,
        numPorts = NumWays,
        hasCnt = false,
        nameSuffix = s"mdpTageTable${tableIdx}_${bankIdx}"
      )).suggestName(s"tage_entry_write_buffer_bank${bankIdx}")
    }

  // read sram
  entrySram.zipWithIndex.foreach { case (bank, bankIdx) =>
    val predictReadValid = io.predictReadReq.valid && io.predictReadReq.bits.bankMask(bankIdx)
    val trainReadValid   = io.trainReadReq.valid && io.trainReadReq.bits.bankMask(bankIdx)
    bank.foreach { way =>
      way.io.r.req.valid       := predictReadValid || trainReadValid
      way.io.r.req.bits.setIdx := Mux(predictReadValid, io.predictReadReq.bits.setIdx, io.trainReadReq.bits.setIdx)
    }
    assert(!(predictReadValid && trainReadValid), s"read conflict in tage_table_${tableIdx}_bank_${bankIdx}")
  }

  // delay one cycle for better timing
  private val writeReqValid = RegNext(io.writeReq.valid, false.B)
  private val writeReq      = RegEnable(io.writeReq.bits, io.writeReq.valid)

  // write to write buffer
  entryWriteBuffers.zipWithIndex.foreach { case (buffer, bankIdx) =>
    buffer.io.write.zipWithIndex.foreach { case (writePort, wayIdx) =>
      writePort.valid          := writeReqValid && writeReq.bankMask(bankIdx) && writeReq.wayMask(wayIdx)
      writePort.bits.setIdx    := writeReq.setIdx
      writePort.bits.entry     := writeReq.entries(wayIdx)
      writePort.bits.usefulCtr := writeReq.usefulCtrs(wayIdx)
    }
  }

  // write to sram from write buffer
  entrySram.zip(usefulCtrs).zip(entryWriteBuffers) foreach { case ((bank, ctrsPerBank), buffer) =>
    bank.zip(ctrsPerBank).zip(buffer.io.read).foreach { case ((way, ctrsPerWay), readPort) =>
      val valid  = readPort.valid && !way.io.r.req.valid
      val setIdx = readPort.bits.setIdx
      val entry  = readPort.bits.entry
      way.io.w.apply(valid, entry, setIdx, 1.U(1.W))
      readPort.ready := way.io.w.req.ready && !way.io.r.req.valid && !io.allWayWeakReq.valid

      when(io.resetUseful) {
        ctrsPerWay.foreach(_.resetZero())
      }.elsewhen(readPort.fire) {
        ctrsPerWay(setIdx) := readPort.bits.usefulCtr
      }
    }
  }

  when(io.allWayWeakReq.valid){
    for(i <- 0 until NumWays){
      usefulCtrs(io.allWayWeakReq.bits.bankIdx)(i)(io.allWayWeakReq.bits.setIdx) := io.allWayWeakReq.bits.usefulCtrs(i)
    }
  }

  private val predictReadSetIdxNext   = RegEnable(io.predictReadReq.bits.setIdx, io.predictReadReq.valid)
  private val predictReadBankMaskNext = RegEnable(io.predictReadReq.bits.bankMask, io.predictReadReq.valid)
  io.predictReadResp.entries := Mux1H(
    predictReadBankMaskNext,
    entrySram.map(bank => VecInit(bank.map(way => way.io.r.resp.data.head)))
  )
  io.predictReadResp.usefulCtrs := Mux1H(
    predictReadBankMaskNext,
    usefulCtrs.map(ctrsPerBank =>
      VecInit(ctrsPerBank.map(ctrsPerWay => ctrsPerWay(predictReadSetIdxNext)))
    )
  )

  private val trainReadSetIdxNext   = RegEnable(io.trainReadReq.bits.setIdx, io.trainReadReq.valid)
  private val trainReadBankMaskNext = RegEnable(io.trainReadReq.bits.bankMask, io.trainReadReq.valid)
  io.trainReadResp.entries := Mux1H(
    trainReadBankMaskNext,
    entrySram.map(bank => VecInit(bank.map(way => way.io.r.resp.data.head)))
  )
  io.trainReadResp.usefulCtrs := Mux1H(
    trainReadBankMaskNext,
    usefulCtrs.map(ctrsPerBank =>
      VecInit(ctrsPerBank.map(ctrsPerWay => ctrsPerWay(trainReadSetIdxNext)))
    )
  )

  io.resetDone := entrySram.flatten.map(_.io.r.req.ready).reduce(_ && _)

}
