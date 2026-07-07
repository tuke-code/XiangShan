// Copyright (c) 2024-2026 Beijing Institute of Open Source Chip (BOSC)
// Copyright (c) 2020-2026 Institute of Computing Technology, Chinese Academy of Sciences
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

package xiangshan.frontend.bpu

import chisel3._
import chisel3.simulator.EphemeralSimulator._ // scalastyle:ignore
import chisel3.util._
import org.scalatest.flatspec.AnyFlatSpec

class CompareMatrixTest extends AnyFlatSpec {
  private class CurrentApiTestModule(n: Int, width: Int) extends Module {
    val io = IO(new Bundle {
      val values = Input(Vec(n, UInt(width.W)))
      val valid = Input(Vec(n, Bool()))
      val leastOH = Output(Vec(n, Bool()))
      val greatestOH = Output(Vec(n, Bool()))
      val lowerMask = Output(Vec(n, Bool()))
      val leaseIdx = Output(UInt(log2Ceil(n).W))
    })

    private val matrix = CompareMatrix(io.values)
    io.leastOH := matrix.getLeastElementOH(io.valid)
    io.greatestOH := matrix.getGreatestElementOH(io.valid)
    io.lowerMask := matrix.getLowerElementMask(io.valid)
    io.leaseIdx := matrix.getLeaseElementIdx(io.valid)
  }

  private class ClearApiTestModule(n: Int, width: Int) extends Module {
    val io = IO(new Bundle {
      val values = Input(Vec(n, UInt(width.W)))
      val valid = Input(Vec(n, Bool()))
      val leaseIdx = Output(UInt(log2Ceil(n).W))
      val leastIdx = Output(UInt(log2Ceil(n).W))
    })

    private val matrix = CompareMatrix(io.values)
    io.leaseIdx := matrix.getLeaseElementIdx(io.valid)
    io.leastIdx := matrix.getLeastElementIdx(io.valid)
  }

  private def pokeInputs(dut: CurrentApiTestModule, values: Seq[Int], valid: Seq[Boolean]): Unit = {
    values.zipWithIndex.foreach { case (value, index) => dut.io.values(index).poke(value.U) }
    valid.zipWithIndex.foreach { case (isValid, index) => dut.io.valid(index).poke(isValid.B) }
  }

  private def expectBoolVec(signal: Vec[Bool], expected: Seq[Boolean]): Unit = {
    expected.zipWithIndex.foreach { case (value, index) => signal(index).expect(value.B) }
  }

  behavior of "CompareMatrix"

  it should "select the least and greatest valid elements and expose lower elements below the valid minimum" in {
    simulate(new CurrentApiTestModule(n = 4, width = 3)) { dut =>
      pokeInputs(
        dut,
        values = Seq(3, 1, 2, 4),
        valid = Seq(true, false, true, false)
      )

      expectBoolVec(dut.io.leastOH, Seq(false, false, true, false))
      expectBoolVec(dut.io.greatestOH, Seq(true, false, false, false))
      expectBoolVec(dut.io.lowerMask, Seq(false, true, true, false))
      dut.io.leaseIdx.expect(2.U)
    }
  }

  it should "make the current equal-value tie behavior explicit" in {
    simulate(new CurrentApiTestModule(n = 4, width = 3)) { dut =>
      pokeInputs(
        dut,
        values = Seq(5, 5, 2, 7),
        valid = Seq(true, true, false, false)
      )

      expectBoolVec(dut.io.leastOH, Seq(false, true, false, false))
      expectBoolVec(dut.io.greatestOH, Seq(true, false, false, false))
      expectBoolVec(dut.io.lowerMask, Seq(false, true, true, false))
      dut.io.leaseIdx.expect(1.U)
    }
  }

  it should "provide a correctly spelled least-element index API matching the existing index API" in {
    simulate(new ClearApiTestModule(n = 4, width = 3)) { dut =>
      dut.io.values(0).poke(6.U)
      dut.io.values(1).poke(1.U)
      dut.io.values(2).poke(4.U)
      dut.io.values(3).poke(3.U)
      dut.io.valid(0).poke(true.B)
      dut.io.valid(1).poke(false.B)
      dut.io.valid(2).poke(true.B)
      dut.io.valid(3).poke(true.B)

      dut.io.leaseIdx.expect(3.U)
      dut.io.leastIdx.expect(3.U)
    }
  }
}
