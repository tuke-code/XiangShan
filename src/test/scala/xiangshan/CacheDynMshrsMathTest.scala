package xiangshan

import chisel3._
import chiseltest._
import org.scalatest.flatspec.AnyFlatSpec
import org.scalatest.matchers.should.Matchers

class CoupledL2DynMshrHardwareHarness extends Module {
  val io = IO(new Bundle {
    val dynMshrs = Input(UInt(64.W))
    val idle = Input(Vec(4, Bool()))
    val pipeReqCount = Input(UInt(3.W))
    val mshrCount = Input(UInt(3.W))
    val eligibleIdle = Output(UInt(4.W))
    val full = Output(Bool())
    val aFull = Output(Bool())
  })

  io.eligibleIdle := VecInit(coupledL2.DynamicMshrHardware.limitIdle(io.idle, io.dynMshrs)).asUInt
  io.full := coupledL2.DynamicMshrHardware.isFull(io.pipeReqCount, io.mshrCount, io.dynMshrs)
  io.aFull := coupledL2.DynamicMshrHardware.isAFull(io.pipeReqCount, io.mshrCount, io.dynMshrs)
}

class HuanCunDynMshrHardwareHarness extends Module {
  val io = IO(new Bundle {
    val dynMshrs = Input(UInt(64.W))
    val idle = Input(Vec(4, Bool()))
    val eligibleIdle = Output(UInt(4.W))
    val hasFree = Output(Bool())
  })

  io.eligibleIdle := VecInit(huancun.DynamicMshrHardware.limitAbcIdle(io.idle, io.dynMshrs)).asUInt
  io.hasFree := huancun.DynamicMshrHardware.hasFreeAbc(io.idle, io.dynMshrs)
}

class CacheDynMshrsMathTest extends AnyFlatSpec with Matchers with ChiselScalatestTester {
  "CoupledL2 dynamic-mshr helpers" should "validate bounded positive runtime limits" in {
    coupledL2.DynamicMshrMath.isValidRuntimeMshrs(runtimeMshrs = 6, staticMshrs = 16) shouldBe true
    coupledL2.DynamicMshrMath.isValidRuntimeMshrs(runtimeMshrs = 0, staticMshrs = 16) shouldBe false
    coupledL2.DynamicMshrMath.isValidRuntimeMshrs(runtimeMshrs = 17, staticMshrs = 16) shouldBe false
  }

  it should "mask out idle entries above the runtime allocation window" in {
    coupledL2.DynamicMshrMath.eligibleIdleMask(
      idle = Seq(true, false, true, true),
      runtimeMshrs = 2
    ) shouldBe Seq(true, false, false, false)

    coupledL2.DynamicMshrMath.eligibleIdleMask(
      idle = Seq(true, false, true, true),
      runtimeMshrs = 3
    ) shouldBe Seq(true, false, true, false)
  }

  it should "keep one entry reserved for channel A requests" in {
    coupledL2.DynamicMshrMath.isFull(pipeReqCount = 0, mshrCount = 2, runtimeMshrs = 3) shouldBe false
    coupledL2.DynamicMshrMath.isAFull(pipeReqCount = 0, mshrCount = 2, runtimeMshrs = 3) shouldBe true
    coupledL2.DynamicMshrMath.isFull(pipeReqCount = 1, mshrCount = 2, runtimeMshrs = 3) shouldBe true
  }

  it should "provide hardware gating signals that match the allocation rules" in {
    test(new CoupledL2DynMshrHardwareHarness) { dut =>
      dut.io.dynMshrs.poke(2.U)
      dut.io.idle(0).poke(true.B)
      dut.io.idle(1).poke(false.B)
      dut.io.idle(2).poke(true.B)
      dut.io.idle(3).poke(true.B)
      dut.io.pipeReqCount.poke(0.U)
      dut.io.mshrCount.poke(1.U)
      dut.clock.step()

      dut.io.eligibleIdle.expect("b0001".U)
      dut.io.full.expect(false.B)
      dut.io.aFull.expect(true.B)

      dut.io.pipeReqCount.poke(1.U)
      dut.clock.step()

      dut.io.full.expect(true.B)
    }
  }

  "HuanCun dynamic-mshr helpers" should "validate bounded positive ABC runtime limits" in {
    huancun.DynamicMshrMath.isValidRuntimeMshrs(runtimeMshrs = 10, staticMshrs = 14) shouldBe true
    huancun.DynamicMshrMath.isValidRuntimeMshrs(runtimeMshrs = 0, staticMshrs = 14) shouldBe false
    huancun.DynamicMshrMath.isValidRuntimeMshrs(runtimeMshrs = 15, staticMshrs = 14) shouldBe false
  }

  it should "only treat the first runtime-configured ABC entries as allocatable" in {
    huancun.DynamicMshrMath.eligibleAbcIdleMask(
      idle = Seq(false, false, true, true),
      runtimeMshrs = 2
    ) shouldBe Seq(false, false, false, false)

    huancun.DynamicMshrMath.eligibleAbcIdleMask(
      idle = Seq(false, true, true, true),
      runtimeMshrs = 2
    ) shouldBe Seq(false, true, false, false)
  }

  it should "provide hardware free-slot detection for the ABC MSHR pool only" in {
    test(new HuanCunDynMshrHardwareHarness) { dut =>
      dut.io.dynMshrs.poke(2.U)
      dut.io.idle(0).poke(false.B)
      dut.io.idle(1).poke(false.B)
      dut.io.idle(2).poke(true.B)
      dut.io.idle(3).poke(true.B)
      dut.clock.step()

      dut.io.eligibleIdle.expect(0.U)
      dut.io.hasFree.expect(false.B)

      dut.io.idle(1).poke(true.B)
      dut.clock.step()

      dut.io.eligibleIdle.expect("b0010".U)
      dut.io.hasFree.expect(true.B)
    }
  }
}
