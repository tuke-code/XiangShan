// Copyright (c) 2024 Beijing Institute of Open Source Chip (BOSC)
// Copyright (c) 2020-2024 Institute of Computing Technology, Chinese Academy of Sciences
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

package xiangshan.frontend.ifu

import chisel3._
import chisel3.util._
import org.chipsalliance.cde.config.Parameters
import utility.XSError

class InstrBoundary(implicit p: Parameters) extends IfuModule with PreDecodeHelper {
  class InstrBoundaryIO(implicit p: Parameters) extends IfuBundle {
    class Req(implicit p: Parameters) extends IfuBundle {
      val valid:               Bool            = Bool()
      val firstInstrIsHalfRvi: Bool            = Bool()
      val fetchBlock:          Vec[FetchBlock] = Vec(MaxFetchReqNum, new FetchBlock)
      val icacheData:          IfuData         = new IfuData
      val totalEndPos:         UInt            = UInt(FetchBlockInstOffsetWidth.W)
    }
    class Resp(implicit p: Parameters) extends IfuBundle {
      val rawInstrVec:  Vec[Instruction] = Vec(FetchBlockInstNum, new Instruction)
      val instrEndMask: Vec[Bool]        = Vec(FetchBlockInstNum, Bool())
      val endIsHalfRvi: Bool             = Bool()
    }
    val req:  Req  = Flipped(new Req)
    val resp: Resp = new Resp
  }
  val io: InstrBoundaryIO = IO(new InstrBoundaryIO)

  private val data     = io.req.icacheData.data
  private val range    = io.req.icacheData.range
  private val mayBeRvc = io.req.icacheData.maybeRvcMap
  private val blockSel = io.req.icacheData.blockSel

  private class BoundaryFunc extends Bundle {
    val in0: Bool = Bool()
    val in1: Bool = Bool()
  }

  private def mergeBoundaryFunc(left: BoundaryFunc, right: BoundaryFunc): BoundaryFunc = {
    val out = Wire(new BoundaryFunc)

    // The merged function means passing through left first and then right.
    out.in0 := Mux(left.in0, right.in1, right.in0)
    out.in1 := Mux(left.in1, right.in1, right.in0)

    out
  }

  private def genBoundaryPrefix(mayBeRvc: UInt, firstInstrIsHalfRvi: Bool): Vec[Bool] = {
    require(HasCExtension, "C Extension can not be disabled in XiangShan")

    val n = FetchBlockInstNum

    // funcs(i) summarizes the logic from a boundary input to boundary(i).
    // in0 is the output when the input is 0; in1 is the output when the input is 1.
    var funcs = Wire(Vec(n, new BoundaryFunc))

    // boundary(0) is the initial seed, so funcs(0) is an identity function.
    funcs(0).in0 := false.B
    funcs(0).in1 := true.B

    for (i <- 1 until n) {
      // boundary(i) = !boundary(i - 1) || mayBeRvc(i - 1)
      funcs(i).in0 := true.B
      funcs(i).in1 := mayBeRvc(i - 1)
    }

    var step = 1
    while (step < n) {
      val next = Wire(Vec(n, new BoundaryFunc))

      for (i <- 0 until n) {
        if (i >= step) {
          next(i) := mergeBoundaryFunc(funcs(i - step), funcs(i))
        } else {
          next(i) := funcs(i)
        }
      }

      funcs = next
      step = step << 1
    }

    val seed = !firstInstrIsHalfRvi
    VecInit((0 until n).map(i => Mux(seed, funcs(i).in1, funcs(i).in0)))
  }

  private def generateBoundary(
      boundary:            Vec[Bool],
      start:               Int,
      end:                 Int,
      firstInstrIsHalfRvi: Bool
  ): Unit = {
    require(HasCExtension, "C Extension can not be disabled in XiangShan")
    for (i <- start until end) {
      boundary(i) := {
        if (i == start) !firstInstrIsHalfRvi else !boundary(i - 1) || mayBeRvc(i - 1)
      }
    }
  }

  private val boundary = genBoundaryPrefix(mayBeRvc, io.req.firstInstrIsHalfRvi)

  io.resp.rawInstrVec := (0 until FetchBlockInstNum).map { i =>
    val instr = Wire(new Instruction)

    instr.valid := {
      if (i == 0)
        Mux(io.req.firstInstrIsHalfRvi, range(i), boundary(i) && range(i) && (mayBeRvc(i) || range(i + 1)))
      else if (i == FetchBlockInstNum - 1)
        boundary(i) && range(i) && mayBeRvc(i)
      else
        boundary(i) && range(i) && (mayBeRvc(i) || range(i + 1))
    }

    instr.data := {
      if (i == FetchBlockInstNum - 1)
        Cat(0.U(16.W), data(i))
      else
        Cat(data(i + 1), data(i))
    }

    instr.isRvc    := boundary(i) && mayBeRvc(i)
    instr.blockSel := blockSel(i)

    instr.isPredTaken      := false.B
    instr.invalidTaken     := false.B
    instr.isPrevEndHalfRvi := false.B

    val localOffset = Mux(
      blockSel(i),
      i.U(FetchBlockInstOffsetWidth.W) - io.req.fetchBlock(0).size,
      i.U(FetchBlockInstOffsetWidth.W)
    )
    instr.startOffset := localOffset
    instr.endOffset   := Mux(mayBeRvc(i), localOffset, localOffset + 1.U)

    instr
  }

  io.resp.instrEndMask := boundary.zip(mayBeRvc.asBools).zip(range.asBools).map { case ((boundary, isRvc), range) =>
    (!boundary || (boundary && isRvc)) && range
  }

  io.resp.endIsHalfRvi := range(io.req.totalEndPos) && boundary(io.req.totalEndPos) && !mayBeRvc(io.req.totalEndPos)

  // For differential test only. Will be optimized out in release
  private val boundDiff = WireInit(VecInit(Seq.fill(FetchBlockInstNum)(false.B)))
  generateBoundary(boundDiff, 0, FetchBlockInstNum, io.req.firstInstrIsHalfRvi)
  boundary.zip(boundDiff).foreach {
    case (a, b) => XSError(io.req.valid && (a =/= b), p"boundary different: $a vs $b\n")
  }
}
