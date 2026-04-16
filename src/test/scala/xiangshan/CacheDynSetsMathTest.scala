package xiangshan

import chisel3._
import chisel3.util.log2Ceil
import chiseltest._
import org.scalatest.flatspec.AnyFlatSpec
import org.scalatest.matchers.should.Matchers

class CacheDynSetsHardwareHarness extends Module {
  val staticSetBits = 10
  val tagBits = 9

  val io = IO(new Bundle {
    val dynSets = Input(UInt(64.W))
    val set = Input(UInt(staticSetBits.W))
    val tag = Input(UInt(tagBits.W))
    val dynSetBits = Output(UInt(log2Ceil(staticSetBits + 1).W))
    val maskedSet = Output(UInt(staticSetBits.W))
    val extendedTag = Output(UInt((tagBits + staticSetBits).W))
    val reconstructedSet = Output(UInt(staticSetBits.W))
    val logicalTag = Output(UInt(tagBits.W))
  })

  io.dynSetBits := coupledL2.DynamicSetHardware.dynSetBits(io.dynSets)
  io.maskedSet := coupledL2.DynamicSetHardware.dynSetMask(io.set, io.dynSetBits)
  io.extendedTag := coupledL2.DynamicSetHardware.extendTag(io.tag, io.set, io.dynSetBits)
  io.reconstructedSet := coupledL2.DynamicSetHardware.reconstructSet(io.extendedTag, io.maskedSet, io.dynSetBits, staticSetBits)
  io.logicalTag := io.extendedTag(io.extendedTag.getWidth - 1, staticSetBits)
}

class HuanCunDynSetsHardwareHarness extends Module {
  val staticSetBits = 11
  val tagBits = 9

  val io = IO(new Bundle {
    val dynSets = Input(UInt(64.W))
    val set = Input(UInt(staticSetBits.W))
    val tag = Input(UInt(tagBits.W))
    val dynSetBits = Output(UInt(log2Ceil(staticSetBits + 1).W))
    val maskedSet = Output(UInt(staticSetBits.W))
    val extendedTag = Output(UInt((tagBits + staticSetBits).W))
    val reconstructedSet = Output(UInt(staticSetBits.W))
    val logicalTag = Output(UInt(tagBits.W))
  })

  io.dynSetBits := huancun.DynamicSetHardware.dynSetBits(io.dynSets)
  io.maskedSet := huancun.DynamicSetHardware.dynSetMask(io.set, io.dynSetBits)
  io.extendedTag := huancun.DynamicSetHardware.extendTag(io.tag, io.set, io.dynSetBits)
  io.reconstructedSet := huancun.DynamicSetHardware.reconstructSet(io.extendedTag, io.maskedSet, io.dynSetBits, staticSetBits)
  io.logicalTag := io.extendedTag(io.extendedTag.getWidth - 1, staticSetBits)
}

class HuanCunDynSetsConflictHarness extends Module {
  val staticSetBits = 11

  val io = IO(new Bundle {
    val dynSets = Input(UInt(64.W))
    val setA = Input(UInt(staticSetBits.W))
    val setB = Input(UInt(staticSetBits.W))
    val granularity = Input(UInt(log2Ceil(staticSetBits + 1).W))
    val conflict = Output(Bool())
  })

  val dynSetBits = huancun.DynamicSetHardware.dynSetBits(io.dynSets)
  io.conflict := huancun.DynamicSetHardware.physicalSetConflict(io.setA, io.setB, dynSetBits, io.granularity)
}

class HuanCunSourceDHazardHarness extends Module {
  val staticSetBits = 11
  val wayBits = 4

  val io = IO(new Bundle {
    val dynSets = Input(UInt(64.W))
    val hazardSet = Input(UInt(staticSetBits.W))
    val hazardWay = Input(UInt(wayBits.W))
    val set = Input(UInt(staticSetBits.W))
    val way = Input(UInt(wayBits.W))
    val conflict = Output(Bool())
  })

  io.conflict := huancun.DynamicSetHardware.dataHazardConflict(
    io.hazardSet,
    io.hazardWay,
    io.set,
    io.way,
    io.dynSets,
    staticSetBits
  )
}

class CacheDynSetsMathTest extends AnyFlatSpec with Matchers with ChiselScalatestTester {
  "CoupledL2 dynamic-set helpers" should "preserve high set bits in extended tags" in {
    val staticSetBits = 10
    val runtimeSets = 512
    val tag = BigInt("155", 16)
    val set = BigInt("200", 16)

    coupledL2.DynamicSetMath.maskedSet(set, runtimeSets) shouldBe BigInt(0)
    coupledL2.DynamicSetMath.extendedTag(tag, set, runtimeSets, staticSetBits) shouldBe
      ((tag << (staticSetBits - coupledL2.DynamicSetMath.dynSetBits(runtimeSets))) | BigInt(1))
    coupledL2.DynamicSetMath.dynSetBits(runtimeSets) shouldBe 9
  }

  it should "handle boundary runtime set counts with clear validation" in {
    val staticSetBits = 10
    val tag = BigInt("155", 16)
    val set = BigInt("200", 16)

    coupledL2.DynamicSetMath.maskedSet(set, runtimeSets = 1) shouldBe BigInt(0)
    coupledL2.DynamicSetMath.extendedTag(tag, set, runtimeSets = 1, staticSetBits) shouldBe
      ((tag << staticSetBits) | set)

    coupledL2.DynamicSetMath.maskedSet(set, runtimeSets = 1 << staticSetBits) shouldBe set
    coupledL2.DynamicSetMath.extendedTag(tag, set, runtimeSets = 1 << staticSetBits, staticSetBits) shouldBe tag

    val error = the [IllegalArgumentException] thrownBy {
      coupledL2.DynamicSetMath.extendedTag(tag, set, runtimeSets = 1 << (staticSetBits + 1), staticSetBits)
    }
    error.getMessage should include(s"staticSetBits ($staticSetBits) must cover runtimeSets (${1 << (staticSetBits + 1)})")
  }

  it should "reject non-power-of-two runtime sets directly" in {
    val error = the [IllegalArgumentException] thrownBy {
      coupledL2.DynamicSetMath.dynSetBits(768)
    }
    error.getMessage should include("runtimeSets must be power-of-two")
  }

  it should "provide matching hardware masking and extended-tag semantics" in {
    test(new CacheDynSetsHardwareHarness) { dut =>
      dut.io.dynSets.poke(512.U)
      dut.io.set.poke("h200".U)
      dut.io.tag.poke("h155".U)
      dut.clock.step()

      dut.io.dynSetBits.expect(9.U)
      dut.io.maskedSet.expect(0.U)
      dut.io.extendedTag.expect(((BigInt("155", 16) << 10) | BigInt(1)).U)
      dut.io.reconstructedSet.expect("h200".U)
      dut.io.logicalTag.expect("h155".U)

      dut.io.dynSets.poke(1.U)
      dut.io.set.poke("h200".U)
      dut.io.tag.poke("h155".U)
      dut.clock.step()

      dut.io.dynSetBits.expect(0.U)
      dut.io.maskedSet.expect(0.U)
      dut.io.extendedTag.expect(((BigInt("155", 16) << 10) | BigInt("200", 16)).U)
      dut.io.reconstructedSet.expect("h200".U)
      dut.io.logicalTag.expect("h155".U)
    }
  }

  it should "reconstruct aliased logical sets instead of returning masked physical sets" in {
    test(new CacheDynSetsHardwareHarness) { dut =>
      dut.io.dynSets.poke(512.U)
      dut.io.set.poke("h200".U)
      dut.io.tag.poke("h155".U)
      dut.clock.step()

      dut.io.maskedSet.expect(0.U)
      dut.io.reconstructedSet.expect("h200".U)
    }
  }

  "HuanCun dynamic-set helpers" should "accept per-bank runtime sets only when positive, bounded, and power-of-two" in {
    huancun.DynamicSetMath.isValidRuntimeSets(runtimeSets = 1024, staticSets = 2048) shouldBe true
    huancun.DynamicSetMath.isValidRuntimeSets(runtimeSets = 768, staticSets = 2048) shouldBe false
    huancun.DynamicSetMath.isValidRuntimeSets(runtimeSets = 2, staticSets = 2048) shouldBe true
    huancun.DynamicSetMath.isValidRuntimeSets(runtimeSets = 2048, staticSets = 2048) shouldBe true
    huancun.DynamicSetMath.isValidRuntimeSets(runtimeSets = 4096, staticSets = 2048) shouldBe false
  }

  it should "derive hardware set bits directly from per-bank runtime sets" in {
    test(new HuanCunDynSetsHardwareHarness) { dut =>
      dut.io.dynSets.poke(1024.U)
      dut.io.set.poke("h600".U)
      dut.io.tag.poke("h155".U)
      dut.clock.step()

      dut.io.dynSetBits.expect(10.U)
      dut.io.maskedSet.expect("h200".U)
      dut.io.extendedTag.expect(((BigInt("155", 16) << 11) | BigInt(1)).U)
      dut.io.reconstructedSet.expect("h600".U)
      dut.io.logicalTag.expect("h155".U)
    }
  }

  it should "treat aliased logical sets as conflicting under runtime-masked physical indexing" in {
    huancun.DynamicSetMath.physicalSetConflict(0x000, 0x400, 1024, 11) shouldBe true
    huancun.DynamicSetMath.physicalSetConflict(0x012, 0x412, 1024, 11) shouldBe true
    huancun.DynamicSetMath.physicalSetConflict(0x012, 0x013, 1024, 11) shouldBe false
  }

  it should "provide hardware physical-set conflict checks for alias-aware serialization" in {
    test(new HuanCunDynSetsConflictHarness) { dut =>
      dut.io.dynSets.poke(1024.U)
      dut.io.granularity.poke(11.U)
      dut.io.setA.poke("h000".U)
      dut.io.setB.poke("h400".U)
      dut.clock.step()
      dut.io.conflict.expect(true.B)

      dut.io.setA.poke("h012".U)
      dut.io.setB.poke("h013".U)
      dut.clock.step()
      dut.io.conflict.expect(false.B)
    }
  }

  it should "treat aliased sets with the same way as banked-store hazards" in {
    test(new HuanCunSourceDHazardHarness) { dut =>
      dut.io.dynSets.poke(1024.U)
      dut.io.hazardSet.poke("h000".U)
      dut.io.hazardWay.poke(3.U)
      dut.io.set.poke("h400".U)
      dut.io.way.poke(3.U)
      dut.clock.step()
      dut.io.conflict.expect(true.B)

      dut.io.set.poke("h400".U)
      dut.io.way.poke(2.U)
      dut.clock.step()
      dut.io.conflict.expect(false.B)
    }
  }

  "DSE cache-set legality helpers" should "reject non-power-of-two set counts" in {
    coupledL2.DynamicSetMath.isPow2(1024) shouldBe true
    coupledL2.DynamicSetMath.isPow2(768) shouldBe false
  }
}
