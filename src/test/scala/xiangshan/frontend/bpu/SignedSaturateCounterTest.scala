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
import chisel3.util._
import chisel3.simulator.EphemeralSimulator._ // scalastyle:ignore
import org.scalatest.flatspec.AnyFlatSpec
import utils.EnumUInt
import scala.util.Random
import scala.math.pow

class SignedSaturateCounterTest extends AnyFlatSpec {
  object OP extends EnumUInt(3) {
    def Inc: UInt = 0.U(width.W)
    def Dec: UInt = 1.U(width.W)
    def Upd: UInt = 2.U(width.W)
  }

  class TestModule(width: Int) extends Module {
    class ControlIO extends Bundle {
      val en: Bool = Input(Bool())
      val op: UInt = Input(OP())
      val step: UInt = Input(UInt(width.W)) // for inc/dec
      val inc: Bool = Input(Bool()) // for upd
    }
    val ctrl: ControlIO = IO(new ControlIO)

    class QueryIO extends Bundle {
      class ShouldHold extends Bundle {
        val increase: Bool = Bool()
      }
      val shouldHold: ShouldHold = Input(new ShouldHold)
    }
    val query: QueryIO = IO(new QueryIO)

    class ResultIO extends Bundle {
      val value: SInt = Output(SInt(width.W))
      // direction
      val isPositive: Bool = Output(Bool())
      val isNegative: Bool = Output(Bool())
      // saturated
      val isSaturatePositive: Bool = Output(Bool())
      val isSaturateNegative: Bool = Output(Bool())
      val isSaturate:         Bool = Output(Bool())
      val shouldHold: Bool = Output(Bool())
      // weak
      val isWeakPositive: Option[Bool] = Option.when(width >= 2)(Output(Bool()))
      val isWeakNegative: Option[Bool] = Option.when(width >= 2)(Output(Bool()))
      val isWeak: Option[Bool] = Option.when(width >= 2)(Output(Bool()))
      // medium
      val isMid: Option[Bool] = Option.when(width >= 3)(Output(Bool()))
    }
    val res: ResultIO = IO(new ResultIO)

    private val cnt = RegInit(SignedSaturateCounter.Zero(width))

    when(ctrl.op === OP.Inc) {
      cnt.selfIncrease(step = ctrl.step, en = ctrl.en)
    }.elsewhen(ctrl.op === OP.Dec) {
      cnt.selfDecrease(step = ctrl.step, en = ctrl.en)
    }.elsewhen(ctrl.op === OP.Upd) {
      cnt.selfUpdate(ctrl.inc, en = ctrl.en)
    }

    res.value := cnt.value
    res.isPositive := cnt.isPositive
    res.isNegative := cnt.isNegative
    res.isSaturatePositive := cnt.isSaturatePositive
    res.isSaturateNegative := cnt.isSaturateNegative
    res.isSaturate := cnt.isSaturate
    res.shouldHold := cnt.shouldHold(query.shouldHold.increase)
    if (width >= 2) {
      res.isWeakPositive.get := cnt.isWeakPositive
      res.isWeakNegative.get := cnt.isWeakNegative
      res.isWeak.get := cnt.isWeak
    }
    if (width >= 3) {
      res.isMid.get := cnt.isMid
    }
  }

  class SignedCounterView(width: Int) extends Bundle {
    val value: SInt = SInt(width.W)
    val isPositive: Bool = Bool()
    val isNegative: Bool = Bool()
    val isSaturatePositive: Bool = Bool()
    val isSaturateNegative: Bool = Bool()
    val isWeakPositive: Bool = Bool()
    val isWeakNegative: Bool = Bool()
    val isWeak: Bool = Bool()
  }

  class ResetAndConstructorModule(width: Int) extends Module {
    val resetTarget: UInt = IO(Input(UInt(3.W)))
    val constructedZero: SignedCounterView = IO(Output(new SignedCounterView(width)))
    val constructedWeakPositive: SignedCounterView = IO(Output(new SignedCounterView(width)))
    val constructedWeakNegative: SignedCounterView = IO(Output(new SignedCounterView(width)))
    val resetCounter: SignedCounterView = IO(Output(new SignedCounterView(width)))

    private val cnt = RegInit(SignedSaturateCounter.Zero(width))

    when(resetTarget === 1.U) {
      cnt.resetWeakPositive()
    }.elsewhen(resetTarget === 2.U) {
      cnt.resetWeakNegative()
    }.elsewhen(resetTarget === 3.U) {
      cnt.resetSaturatePositive()
    }.elsewhen(resetTarget === 4.U) {
      cnt.resetSaturateNegative()
    }

    private def connectView(view: SignedCounterView, counter: SignedSaturateCounter): Unit = {
      view.value := counter.value
      view.isPositive := counter.isPositive
      view.isNegative := counter.isNegative
      view.isSaturatePositive := counter.isSaturatePositive
      view.isSaturateNegative := counter.isSaturateNegative
      view.isWeakPositive := counter.isWeakPositive
      view.isWeakNegative := counter.isWeakNegative
      view.isWeak := counter.isWeak
    }

    connectView(constructedZero, SignedSaturateCounter.Zero(width))
    connectView(constructedWeakPositive, SignedSaturateCounter.WeakPositive(width))
    connectView(constructedWeakNegative, SignedSaturateCounter.WeakNegative(width))
    connectView(resetCounter, cnt)
  }

  private def expectWeakPositive(view: SignedCounterView): Unit = {
    view.value.expect(0.S)
    view.isPositive.expect(true.B)
    view.isNegative.expect(false.B)
    view.isSaturatePositive.expect(false.B)
    view.isSaturateNegative.expect(false.B)
    view.isWeakPositive.expect(true.B)
    view.isWeakNegative.expect(false.B)
    view.isWeak.expect(true.B)
  }

  private def expectWeakNegative(view: SignedCounterView): Unit = {
    view.value.expect((-1).S)
    view.isPositive.expect(false.B)
    view.isNegative.expect(true.B)
    view.isSaturatePositive.expect(false.B)
    view.isSaturateNegative.expect(false.B)
    view.isWeakPositive.expect(false.B)
    view.isWeakNegative.expect(true.B)
    view.isWeak.expect(true.B)
  }

  private def expectSaturatePositive(view: SignedCounterView, width: Int): Unit = {
    view.value.expect(SignedSaturateCounter.Value.SaturatePositive(width).S)
    view.isPositive.expect(true.B)
    view.isNegative.expect(false.B)
    view.isSaturatePositive.expect(true.B)
    view.isSaturateNegative.expect(false.B)
    view.isWeakPositive.expect(false.B)
    view.isWeakNegative.expect(false.B)
    view.isWeak.expect(false.B)
  }

  private def expectSaturateNegative(view: SignedCounterView, width: Int): Unit = {
    view.value.expect(SignedSaturateCounter.Value.SaturateNegative(width).S)
    view.isPositive.expect(false.B)
    view.isNegative.expect(true.B)
    view.isSaturatePositive.expect(false.B)
    view.isSaturateNegative.expect(true.B)
    view.isWeakPositive.expect(false.B)
    view.isWeakNegative.expect(false.B)
    view.isWeak.expect(false.B)
  }

  private def expectCompanionConstants(width: Int): Unit = {
    assert(
      SignedSaturateCounter.Value.WeakPositive(width) == 0,
      s"weak positive for width $width must be the signed threshold value 0"
    )
    assert(
      SignedSaturateCounter.Value.WeakNegative(width) == -1,
      s"weak negative for width $width must be the signed threshold value -1"
    )
  }

  def maxValue(implicit width: Int): Int = pow(2, width - 1).toInt - 1
  def minValue(implicit width: Int): Int = -pow(2, width - 1).toInt
  def thres(implicit width: Int): Int = 0

  def isPositive(value: Int)(implicit width: Int): Boolean = value >= thres
  def isNegative(value: Int)(implicit width: Int): Boolean = value < thres
  def isSaturatePositive(value: Int)(implicit width: Int): Boolean = value == maxValue
  def isSaturateNegative(value: Int)(implicit width: Int): Boolean = value == minValue
  def isSaturate(value: Int)(implicit width: Int): Boolean = isSaturatePositive(value) || isSaturateNegative(value)

  def isWeakPositive(value: Int)(implicit width: Int): Boolean = value == thres
  def isWeakNegative(value: Int)(implicit width: Int): Boolean = value == thres - 1
  def isWeak(value: Int)(implicit width: Int): Boolean = isWeakPositive(value) || isWeakNegative(value)

  def isMid(value: Int)(implicit width: Int): Boolean = !isSaturate(value) && !isWeak(value)

  def forceInRange(value: Int)(implicit width: Int): Int = value.max(minValue).min(maxValue)

  private val opString = Seq("Inc", "Dec", "Upd")
  private def flagString(value: Int)(implicit width: Int): String = {
    val ret = new StringBuilder()
    if (isSaturate(value)) ret.append("S")
    else if (isWeak(value)) ret.append("W")
    else ret.append("M")
    if (isPositive(value)) ret.append("P")
    else ret.append("N")
    ret.toString()
  }

  behavior of "SignedSaturateCounter"
  it should "align signed weak-state constants with constructors, predicates, and reset methods" in {
    for (width <- Seq(2, 3, 4, 8)) {
      simulate(new ResetAndConstructorModule(width)) { dut =>
        dut.reset.poke(true.B)
        dut.resetTarget.poke(0.U)
        dut.clock.step(1)
        dut.reset.poke(false.B)

        expectWeakPositive(dut.constructedZero)
        expectWeakPositive(dut.constructedWeakPositive)
        expectWeakNegative(dut.constructedWeakNegative)
        expectWeakPositive(dut.resetCounter)

        dut.resetTarget.poke(1.U)
        dut.clock.step(1)
        expectWeakPositive(dut.resetCounter)

        dut.resetTarget.poke(2.U)
        dut.clock.step(1)
        expectWeakNegative(dut.resetCounter)

        dut.resetTarget.poke(3.U)
        dut.clock.step(1)
        expectSaturatePositive(dut.resetCounter, width)

        dut.resetTarget.poke(4.U)
        dut.clock.step(1)
        expectSaturateNegative(dut.resetCounter, width)
      }

      expectCompanionConstants(width)
    }
  }

  it should "work" in {
    def test(implicit width: Int): Unit = {
      print(f"===== test SignedSaturateCounter width=${width} =====\n")
      var value = 0
      simulate(new TestModule(width)) { dut =>
        dut.reset.poke(true.B)
        dut.clock.step(10)
        dut.reset.poke(false.B)
        dut.clock.step(10)
        for (_ <- 0 until 1000) {
          val op = Random.nextInt(3)
          val step = Random.nextInt(pow(2, width - 1).toInt - 1)
          val inc = Random.nextInt(2) == 1

          dut.ctrl.en.poke(true.B)
          dut.ctrl.op.poke(op.U)
          dut.ctrl.step.poke(step.U)
          dut.ctrl.inc.poke(inc.B)
          dut.clock.step(1)

          val old = value
          value = forceInRange(
            if (op == 0) value + step
            else if (op == 1) value - step
            else if (inc) value + 1 // op == 2
            else value - 1 // op == 2 && !inc
          )

          print(
            f"op=${opString(op)} " +(
              if (op == 0 || op == 1) f"step=${step}%03d           "
              else f"         inc=${inc}%5s ") +
              f"value=${old}%04d(${flagString(old)})->${value}%04d(${flagString(value)}) ... "
          )
          dut.res.value.expect(value.S)
          dut.res.isPositive.expect(isPositive(value).B)
          dut.res.isNegative.expect(isNegative(value).B)
          dut.res.isSaturatePositive.expect(isSaturatePositive(value).B)
          dut.res.isSaturateNegative.expect(isSaturateNegative(value).B)
          dut.res.isSaturate.expect((isSaturatePositive(value) || isSaturateNegative(value)).B)
          dut.res.isWeakPositive.foreach(_.expect(isWeakPositive(value).B))
          dut.res.isWeakNegative.foreach(_.expect(isWeakNegative(value).B))
          dut.res.isWeak.foreach(_.expect(isWeak(value).B))
          dut.res.isMid.foreach(_.expect(isMid(value).B))
          print("pass\n")
        }
      }
    }

    for (width <- 2 until 10) {
      test(width)
    }
  }
}
