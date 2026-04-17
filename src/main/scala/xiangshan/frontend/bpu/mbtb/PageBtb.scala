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

class PageBtb(implicit p: Parameters) extends MainBtbModule {
  class PageBtbIO extends Bundle {
    class ReadWay extends Bundle {
      class Req extends Bundle {
        val setIdx:  UInt = UInt(log2Ceil(NumPageBtbSets).W)
        val wayMask: UInt = UInt(NumPageBtbWays.W)
      }
      class Resp extends Bundle {
        val entry: PageBtbEntry = new PageBtbEntry
      }

      val req:  Valid[Req] = Flipped(Valid(new Req))
      val resp: Resp       = Output(new Resp)
    }
    class ReadSet extends Bundle {
      class Req extends Bundle {
        val setIdx: UInt = UInt(log2Ceil(NumPageBtbSets).W)
      }
      class Resp extends Bundle {
        val entries: Vec[PageBtbEntry] = Vec(NumPageBtbWays, new PageBtbEntry)
      }

      val req:  Valid[Req] = Flipped(Valid(new Req))
      val resp: Resp       = Output(new Resp)
    }

    class Write extends Bundle {
      class Req extends Bundle {
        val setIdx:  UInt         = UInt(log2Ceil(NumPageBtbSets).W)
        val wayMask: UInt         = UInt(NumPageBtbWays.W)
        val entry:   PageBtbEntry = new PageBtbEntry
      }

      val req: Valid[Req] = Flipped(Valid(new Req))
    }

    val readWay: ReadWay = new ReadWay
    val readSet: ReadSet = new ReadSet
    val write:   Write   = new Write
  }

  val io: PageBtbIO = IO(new PageBtbIO)

  private val table = Seq.fill(NumPageBtbWays) {
    Reg(Vec(NumPageBtbSets, new PageBtbEntry))
  }

  when(reset.asBool)(table.foreach(_.foreach(_.valid := false.B)))

  /* *** read way *** */
  private val readWayRespEntryReg = RegInit(0.U.asTypeOf(new PageBtbEntry))
  private val readWayEntry = Mux1H(
    io.readWay.req.bits.wayMask,
    VecInit(table.map(_(io.readWay.req.bits.setIdx)))
  )

  // write bypass
  private val writeHitReadWay = io.write.req.valid && io.readWay.req.valid &&
    (io.write.req.bits.setIdx === io.readWay.req.bits.setIdx) &&
    (io.write.req.bits.wayMask === io.readWay.req.bits.wayMask)
  private val writeBypassEntry = io.write.req.bits.entry

  private val readWayRespEntry = MuxCase(
    readWayRespEntryReg,
    Seq(
      writeHitReadWay      -> writeBypassEntry,
      io.readWay.req.valid -> readWayEntry
    )
  )
  readWayRespEntryReg   := readWayRespEntry
  io.readWay.resp.entry := readWayRespEntryReg

  /* *** read set *** */
  private val readSetRespEntriesReg = RegInit(0.U.asTypeOf(Vec(NumPageBtbWays, new PageBtbEntry)))
  private val readSetEntries        = VecInit(table.map(_(io.readSet.req.bits.setIdx)))

  // write bypass
  private val writeHitReadSet = io.write.req.valid && io.readSet.req.valid &&
    io.write.req.bits.setIdx === io.readSet.req.bits.setIdx
  private val writeBypassEntries = VecInit((0 until NumPageBtbWays).map { i =>
    Mux(io.write.req.bits.wayMask(i), io.write.req.bits.entry, readSetEntries(i))
  })

  private val readSetRespEntries = MuxCase(
    readSetRespEntriesReg,
    Seq(
      writeHitReadSet      -> writeBypassEntries,
      io.readSet.req.valid -> readSetEntries
    )
  )

  readSetRespEntriesReg   := readSetRespEntries
  io.readSet.resp.entries := readSetRespEntriesReg

  /* *** write *** */
  when(io.write.req.valid) {
    for (i <- 0 until NumPageBtbWays) {
      when(io.write.req.bits.wayMask(i)) {
        table(i)(io.write.req.bits.setIdx) := io.write.req.bits.entry
      }
    }
  }
}
