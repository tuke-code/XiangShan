# DSE Cache Sets Dynamic Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Re-enable DSE-controlled `L2SETS` and `L3SETS` and make coupled L2 plus HuanCun L3 honor runtime set reduction without false hits.

**Architecture:** The implementation restores DSE cache-set control registers, propagates runtime set counts through `L2Top` and `Top`, and teaches coupledL2 and HuanCun storage structures to index by masked runtime sets while folding truncated high set bits into stored tags. The work is split into DSE/top-level plumbing, coupledL2 storage semantics, and HuanCun storage semantics so each stage remains independently verifiable.

**Tech Stack:** Scala 2.13, Chisel 6, Mill, Rocket Chip diplomacy, coupledL2, HuanCun, ScalaTest, `make verilog`

---

## File Structure

**Files:**
- Create: `src/test/scala/xiangshan/CacheDynSetsMathTest.scala`
- Modify: `src/main/scala/xiangshan/DSECtrlUnit.scala`
- Modify: `src/main/scala/xiangshan/L2Top.scala`
- Modify: `src/main/scala/top/Top.scala`
- Modify: `coupledL2/src/main/scala/coupledL2/BaseSlice.scala`
- Modify: `coupledL2/src/main/scala/coupledL2/CoupledL2.scala`
- Modify: `coupledL2/src/main/scala/coupledL2/Directory.scala`
- Modify: `coupledL2/src/main/scala/coupledL2/DataStorage.scala`
- Modify: `coupledL2/src/main/scala/coupledL2/tl2tl/Slice.scala`
- Modify: `coupledL2/src/main/scala/coupledL2/tl2chi/Slice.scala`
- Modify: `huancun/src/main/scala/huancun/HuanCun.scala`
- Modify: `huancun/src/main/scala/huancun/Slice.scala`
- Modify: `huancun/src/main/scala/huancun/BaseDirectory.scala`
- Modify: `huancun/src/main/scala/huancun/DataStorage.scala`
- Modify: `huancun/src/main/scala/huancun/inclusive/Directory.scala`
- Modify: `huancun/src/main/scala/huancun/noninclusive/Directory.scala`

**Responsibilities:**
- `CacheDynSetsMathTest.scala`: lock down the runtime set math and DSE legality rules before RTL changes.
- `DSECtrlUnit.scala`: restore runtime `L2SETS/L3SETS` control registers, muxing, and assertions.
- `L2Top.scala` and `Top.scala`: bridge DSE runtime set values into cache hierarchies with `BoringUtils`.
- `coupledL2/*`: add runtime `sets` plumbing and correct masked-index plus extended-tag behavior in directory/data storage.
- `huancun/*`: add runtime `sets` plumbing and correct masked-index plus extended-tag behavior for self directory and data storage while leaving client directory semantics unchanged.

### Task 1: Lock Down Dynamic Set Math and Legality Rules

**Files:**
- Create: `src/test/scala/xiangshan/CacheDynSetsMathTest.scala`
- Modify: `src/main/scala/xiangshan/DSECtrlUnit.scala`
- Test: `src/test/scala/xiangshan/CacheDynSetsMathTest.scala`

- [ ] **Step 1: Write the failing test**

Create `src/test/scala/xiangshan/CacheDynSetsMathTest.scala` with tests that intentionally reference helper entry points that do not exist yet. Keep the test focused on three rules: power-of-two validation, HuanCun runtime-set legality for already-per-bank `sets` values, and extended-tag preservation of truncated set bits.

```scala
package xiangshan

import org.scalatest.flatspec.AnyFlatSpec
import org.scalatest.matchers.should.Matchers

class CacheDynSetsMathTest extends AnyFlatSpec with Matchers {
  "CoupledL2 dynamic-set helpers" should "preserve high set bits in extended tags" in {
    val staticSetBits = 10
    val runtimeSets = 512
    val tag = BigInt("155", 16)
    val set = BigInt("200", 16)

    coupledL2.DynamicSetMath.maskedSet(set, runtimeSets) shouldBe BigInt(0)
    coupledL2.DynamicSetMath.extendedTag(tag, set, runtimeSets, staticSetBits) shouldBe
      ((tag << (staticSetBits - coupledL2.DynamicSetMath.dynSetBits(runtimeSets))) | BigInt(1))
  }

  "HuanCun dynamic-set helpers" should "accept per-bank runtime sets only when positive, bounded, and power-of-two" in {
    huancun.DynamicSetMath.isValidRuntimeSets(runtimeSets = 1024, staticSets = 2048) shouldBe true
    huancun.DynamicSetMath.isValidRuntimeSets(runtimeSets = 768, staticSets = 2048) shouldBe false
    huancun.DynamicSetMath.isValidRuntimeSets(runtimeSets = 2, staticSets = 2048) shouldBe true
  }

  "DSE cache-set legality helpers" should "reject non-power-of-two set counts" in {
    coupledL2.DynamicSetMath.isPow2(1024) shouldBe true
    coupledL2.DynamicSetMath.isPow2(768) shouldBe false
  }
}
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
mill -i xiangshan.test.testOnly xiangshan.CacheDynSetsMathTest
```

Expected: FAIL at compile time with unresolved symbols such as `coupledL2.DynamicSetMath` and `huancun.DynamicSetMath`.

- [ ] **Step 3: Write minimal implementation**

Add small pure-Scala companion helpers at the bottom of the existing package files so the tests compile and so later RTL code can mirror the same rules without inventing different semantics in each subsystem.

In `coupledL2/src/main/scala/coupledL2/CoupledL2.scala`, add:

```scala
object DynamicSetMath {
  def isPow2(value: Int): Boolean = value > 0 && (value & (value - 1)) == 0

  def dynSetBits(runtimeSets: Int): Int = {
    require(isPow2(runtimeSets), s"runtimeSets must be power-of-two, got $runtimeSets")
    log2Ceil(runtimeSets)
  }

  def maskedSet(set: BigInt, runtimeSets: Int): BigInt = {
    val mask = (BigInt(1) << dynSetBits(runtimeSets)) - 1
    set & mask
  }

  def extendedTag(tag: BigInt, set: BigInt, runtimeSets: Int, staticSetBits: Int): BigInt = {
    val shift = dynSetBits(runtimeSets)
    val highSet = set >> shift
    (tag << (staticSetBits - shift)) | highSet
  }
}
```

In `huancun/src/main/scala/huancun/HuanCun.scala`, add:

```scala
object DynamicSetMath {
  def isPow2(value: Int): Boolean = value > 0 && (value & (value - 1)) == 0

  def dynSetBits(runtimeSets: Int): Int = {
    require(isPow2(runtimeSets), s"runtimeSets must be power-of-two, got $runtimeSets")
    log2Ceil(runtimeSets)
  }

  def isValidRuntimeSets(runtimeSets: Int, staticSets: Int): Boolean = {
    runtimeSets > 0 &&
    runtimeSets <= staticSets &&
    isPow2(runtimeSets)
  }
}
```

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
mill -i xiangshan.test.testOnly xiangshan.CacheDynSetsMathTest
```

Expected: PASS with 3 tests completed.

- [ ] **Step 5: Commit**

```bash
git add src/test/scala/xiangshan/CacheDynSetsMathTest.scala coupledL2/src/main/scala/coupledL2/CoupledL2.scala huancun/src/main/scala/huancun/HuanCun.scala
git commit -m "test: lock down dynamic cache set math"
```

### Task 2: Restore DSE Cache-Set Controls and Top-Level Wiring

**Files:**
- Modify: `src/main/scala/xiangshan/DSECtrlUnit.scala`
- Modify: `src/main/scala/xiangshan/L2Top.scala`
- Modify: `src/main/scala/top/Top.scala`
- Test: `src/test/scala/xiangshan/CacheDynSetsMathTest.scala`

- [ ] **Step 1: Write the failing test**

Extend `src/test/scala/xiangshan/CacheDynSetsMathTest.scala` with explicit legality checks that mirror the DSE behavior expected after restoring `L2SETS` and `L3SETS`.

```scala
  it should "match the intended DSE legality rules for L3 runtime sets" in {
    huancun.DynamicSetMath.isValidRuntimeSets(runtimeSets = 2048, staticSets = 2048) shouldBe true
    huancun.DynamicSetMath.isValidRuntimeSets(runtimeSets = 1024, staticSets = 2048) shouldBe true
    huancun.DynamicSetMath.isValidRuntimeSets(runtimeSets = 1536, staticSets = 2048) shouldBe false
    huancun.DynamicSetMath.isValidRuntimeSets(runtimeSets = 4096, staticSets = 2048) shouldBe false
  }
```

The test will compile and pass already, but the codebase still lacks the DSE wiring. The regression signal for this task is top-level elaboration.

- [ ] **Step 2: Run test to verify the current build still fails at the integration boundary**

Run:

```bash
make verilog CONFIG=WithL3DebugConfig NUM_CORES=1
```

Expected: PASS before this task only if no new integration is attempted. After adding the DSE-side `sets` hookups in the next step but before adding cache IOs, the build should FAIL with missing members such as `value sets is not a member of ...io`.

- [ ] **Step 3: Write minimal implementation**

In `src/main/scala/xiangshan/DSECtrlUnit.scala`, restore the runtime cache-set controls and legality assertions.

```scala
import coupledL2.L2ParamKey

val staticL2Sets = p(L2ParamKey).sets
val staticL3Sets = p(SoCParamsKey).L3CacheParamsOpt.get.sets

val l2Sets0 = RegInit(staticL2Sets.U(64.W))
val l2Sets1 = RegInit(staticL2Sets.U(64.W))
val l2Sets = Wire(UInt(64.W))

val l3Sets0 = RegInit(staticL3Sets.U(64.W))
val l3Sets1 = RegInit(staticL3Sets.U(64.W))
val l3Sets = Wire(UInt(64.W))

l2Sets := Mux(appliedCtrlSel.orR, l2Sets1, l2Sets0)
l3Sets := Mux(appliedCtrlSel.orR, l3Sets1, l3Sets0)

BoringUtils.addSource(l2Sets, "DSE_L2SETS")
BoringUtils.addSource(l3Sets, "DSE_L3SETS")

assert(l2Sets <= staticL2Sets.U, "DSE parameter must not exceed L2Sets")
assert(PopCount(l2Sets) === 1.U, "DSE L2Sets must be power-of-two")
assert(l3Sets <= staticL3Sets.U, "DSE parameter must not exceed L3Sets")
assert(PopCount(l3Sets) === 1.U, "DSE L3Sets must be power-of-two")
```

Restore the regmap entries:

```scala
0x1A0 -> Seq(RegField(64, l2Sets0)),
0x1A8 -> Seq(RegField(64, l2Sets1)),
0x1B0 -> Seq(RegField(64, l3Sets0)),
0x1B8 -> Seq(RegField(64, l3Sets1)),
```

In `src/main/scala/xiangshan/L2Top.scala`, add the runtime sink and hook it to coupledL2:

```scala
import chisel3.util.experimental.BoringUtils

val pL2Sets = WireInit(0.U(64.W))
BoringUtils.addSink(pL2Sets, "DSE_L2SETS")

if (l2cache.isDefined) {
  val l2 = l2cache.get.module
  l2.io.sets := pL2Sets
  // existing hookups stay unchanged
}
```

In `src/main/scala/top/Top.scala`, add the runtime sink and hook it to HuanCun:

```scala
import chisel3.util.experimental.BoringUtils

val pL3Sets = WireInit(0.U(64.W))
BoringUtils.addSink(pL3Sets, "DSE_L3SETS")
l3cacheOpt.foreach(_.module.io.sets := pL3Sets)
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
mill -i xiangshan.test.testOnly xiangshan.CacheDynSetsMathTest
make verilog CONFIG=WithL3DebugConfig NUM_CORES=1
```

Expected:
- ScalaTest PASS
- `make verilog` FAIL now only because coupledL2 and HuanCun do not yet expose `io.sets`

- [ ] **Step 5: Commit**

```bash
git add src/main/scala/xiangshan/DSECtrlUnit.scala src/main/scala/xiangshan/L2Top.scala src/main/scala/top/Top.scala src/test/scala/xiangshan/CacheDynSetsMathTest.scala
git commit -m "feat(dse): restore runtime cache set controls"
```

### Task 3: Implement Runtime L2 Set Selection in coupledL2

**Files:**
- Modify: `coupledL2/src/main/scala/coupledL2/BaseSlice.scala`
- Modify: `coupledL2/src/main/scala/coupledL2/CoupledL2.scala`
- Modify: `coupledL2/src/main/scala/coupledL2/Directory.scala`
- Modify: `coupledL2/src/main/scala/coupledL2/DataStorage.scala`
- Modify: `coupledL2/src/main/scala/coupledL2/tl2tl/Slice.scala`
- Modify: `coupledL2/src/main/scala/coupledL2/tl2chi/Slice.scala`
- Test: `src/test/scala/xiangshan/CacheDynSetsMathTest.scala`

- [ ] **Step 1: Write the failing test**

Extend `src/test/scala/xiangshan/CacheDynSetsMathTest.scala` with one more coupledL2-focused invariant covering the alias case that motivated the change.

```scala
  it should "distinguish lines that alias after masking by carrying high set bits into the stored tag" in {
    val staticSetBits = 10
    val runtimeSets = 256
    val tag = BigInt("2a", 16)
    val setA = BigInt(0x000)
    val setB = BigInt(0x300)

    coupledL2.DynamicSetMath.maskedSet(setA, runtimeSets) shouldBe coupledL2.DynamicSetMath.maskedSet(setB, runtimeSets)
    coupledL2.DynamicSetMath.extendedTag(tag, setA, runtimeSets, staticSetBits) should not be
      coupledL2.DynamicSetMath.extendedTag(tag, setB, runtimeSets, staticSetBits)
  }
```

- [ ] **Step 2: Run test to verify it fails correctly after removing the temporary pure-Scala shortcut**

Temporarily comment out the `extendedTag` implementation body or replace it with the wrong behavior to prove the test is meaningful, then run:

```bash
mill -i xiangshan.test.testOnly xiangshan.CacheDynSetsMathTest
```

Expected: FAIL on the new alias-preservation test because the two extended tags collapse together.

- [ ] **Step 3: Write minimal implementation**

In `coupledL2/src/main/scala/coupledL2/BaseSlice.scala`, add the runtime set input:

```scala
val dynSets = Input(UInt(64.W))
```

In `coupledL2/src/main/scala/coupledL2/CoupledL2.scala`, add top-level IO and hardware helpers:

```scala
def extTagBits = tagBits + setBits

def dynSetMask(set: UInt, dynSetBits: UInt): UInt = {
  set & ((1.U << dynSetBits) - 1.U)
}

def extendTag(tag: UInt, set: UInt, dynSetBits: UInt): UInt = {
  Cat(tag, set >> dynSetBits)
}

val io = IO(new Bundle {
  // existing fields...
  val sets = Input(UInt(64.W))
})

slice.io.dynSets := io.sets
```

In `coupledL2/src/main/scala/coupledL2/Directory.scala`, update tag storage and masked indexing:

```scala
val dynSetBits = Log2(io.dynSets)
val dynReadSet = dynSetMask(io.read.bits.set, dynSetBits)
val extTagWriteData = extendTag(io.tagWReq.bits.wtag, io.tagWReq.bits.set, dynSetBits)
val extReqTag_s3 = extendTag(req_s3.tag, req_s3.set, dynSetBits)

val tagArray = Module(new SRAMTemplate(
  gen = UInt(extTagBits.W),
  set = sets,
  way = ways,
  singlePort = true,
  hasMbist = mbist,
  suffix = "_l2c_tag"
))

tagArray.io.w(
  tagWen,
  extTagWriteData,
  dynSetMask(io.tagWReq.bits.set, dynSetBits),
  UIntToOH(io.tagWReq.bits.way)
)

val tagRead = tagArray.io.r(io.read.fire, dynReadSet).resp.data
val tagMatchVec = tagAll_s3.map(_ === extReqTag_s3)
io.resp.bits.tag := tag_s3(extTagBits - 1, setBits)
```

In `coupledL2/src/main/scala/coupledL2/DataStorage.scala`, align data SRAM addressing:

```scala
val dynSetBits = Log2(io.dynSets)
val arrayIdx = Cat(io.req.bits.way, dynSetMask(io.req.bits.set, dynSetBits)(setBits - 1, 0))
```

In `coupledL2/src/main/scala/coupledL2/tl2tl/Slice.scala` and `coupledL2/src/main/scala/coupledL2/tl2chi/Slice.scala`, forward `dynSets` to directory and data storage:

```scala
directory.io.dynSets := io.dynSets
dataStorage.io.dynSets := io.dynSets
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
mill -i xiangshan.test.testOnly xiangshan.CacheDynSetsMathTest
make verilog CONFIG=MinimalConfig NUM_CORES=1
```

Expected:
- ScalaTest PASS
- `make verilog CONFIG=MinimalConfig` PASS, confirming L2-only elaboration is healthy

- [ ] **Step 5: Commit**

```bash
git add coupledL2/src/main/scala/coupledL2/BaseSlice.scala coupledL2/src/main/scala/coupledL2/CoupledL2.scala coupledL2/src/main/scala/coupledL2/Directory.scala coupledL2/src/main/scala/coupledL2/DataStorage.scala coupledL2/src/main/scala/coupledL2/tl2tl/Slice.scala coupledL2/src/main/scala/coupledL2/tl2chi/Slice.scala src/test/scala/xiangshan/CacheDynSetsMathTest.scala
git commit -m "feat(l2): support dynamic runtime set selection"
```

### Task 4: Implement Runtime L3 Set Selection in HuanCun

**Files:**
- Modify: `huancun/src/main/scala/huancun/HuanCun.scala`
- Modify: `huancun/src/main/scala/huancun/Slice.scala`
- Modify: `huancun/src/main/scala/huancun/BaseDirectory.scala`
- Modify: `huancun/src/main/scala/huancun/DataStorage.scala`
- Modify: `huancun/src/main/scala/huancun/inclusive/Directory.scala`
- Modify: `huancun/src/main/scala/huancun/noninclusive/Directory.scala`
- Test: `src/test/scala/xiangshan/CacheDynSetsMathTest.scala`

- [ ] **Step 1: Write the failing test**

Extend `src/test/scala/xiangshan/CacheDynSetsMathTest.scala` with HuanCun-specific expectations for the repository's per-bank `sets` semantics.

```scala
  "HuanCun dynamic-set helpers" should "preserve high per-bank set bits in the extended tag" in {
    val staticSetBits = 9
    val runtimeSets = 256
    val tag = BigInt("31", 16)
    val setA = BigInt(0x00)
    val setB = BigInt(0x100)

    huancun.DynamicSetMath.maskedSet(setA, runtimeSets) shouldBe huancun.DynamicSetMath.maskedSet(setB, runtimeSets)
    huancun.DynamicSetMath.extendedTag(tag, setA, runtimeSets, staticSetBits) should not be
      huancun.DynamicSetMath.extendedTag(tag, setB, runtimeSets, staticSetBits)
  }
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
mill -i xiangshan.test.testOnly xiangshan.CacheDynSetsMathTest
```

Expected: FAIL at compile time because `huancun.DynamicSetMath.extendedTag` does not exist yet.

- [ ] **Step 3: Write minimal implementation**

First, extend the pure helper in `huancun/src/main/scala/huancun/HuanCun.scala`:

```scala
def maskedSet(set: BigInt, runtimeSets: Int): BigInt = {
  val mask = (BigInt(1) << dynSetBits(runtimeSets)) - 1
  set & mask
}

def extendedTag(tag: BigInt, set: BigInt, runtimeSets: Int, staticSetBits: Int): BigInt = {
  val shift = dynSetBits(runtimeSets)
  val highSet = set >> shift
  (tag << (staticSetBits - shift)) | highSet
}
```

Then add hardware plumbing in `huancun/src/main/scala/huancun/HuanCun.scala`:

```scala
val io = IO(new Bundle {
  // existing fields...
  val sets = Input(UInt(64.W))
})

slice.io.dynSets := io.sets
```

In `huancun/src/main/scala/huancun/Slice.scala`, add:

```scala
val dynSets = Input(UInt(64.W))

directory.io.dynSets := io.dynSets
dataStorage.io.dynSets := io.dynSets
```

In `huancun/src/main/scala/huancun/BaseDirectory.scala`, implement masked per-bank indexing and extended tag storage:

```scala
val dynSetBits = Log2(io.dynSets)

def dynSetMask(set: UInt): UInt = {
  set & ((1.U << dynSetBits) - 1.U)
}

def extendTag(tag: UInt, set: UInt): UInt = {
  Cat(tag, set >> dynSetBits)
}

val storedTagBits = tagBits + setBits
val tagRead = Wire(Vec(ways, UInt(storedTagBits.W)))
val tagArray = Module(new SRAMTemplate(UInt(storedTagBits.W), sets, ways, singlePort = true, input_clk_div_by_2 = clk_div_by_2))

tagArray.io.w(
  io.tag_w.fire,
  extendTag(io.tag_w.bits.tag, io.tag_w.bits.set),
  dynSetMask(io.tag_w.bits.set),
  UIntToOH(io.tag_w.bits.way)
)
tagRead := tagArray.io.r(io.read.fire, dynSetMask(io.read.bits.set)).resp.data
val tagMatchVec = tagRead.map(_ === extendTag(reqReg.tag, reqReg.set))
io.resp.bits.tag := tag_s2(storedTagBits - 1, setBits)
```

Update replacer and meta accesses in the same file to use `dynSetMask(...)`.

In `huancun/src/main/scala/huancun/DataStorage.scala`, mask the internal SRAM row address:

```scala
val dynSetBits = Log2(io.dynSets)
val maskedSet = addr.bits.set & ((1.U << dynSetBits) - 1.U)
val innerAddr = Cat(addr.bits.way, maskedSet, addr.bits.beat)
```

In `huancun/src/main/scala/huancun/inclusive/Directory.scala`, forward `dynSets` to the shared subdirectory logic. In `huancun/src/main/scala/huancun/noninclusive/Directory.scala`, forward `dynSets` only to `selfDir`, leaving `clientDir` unchanged.

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
mill -i xiangshan.test.testOnly xiangshan.CacheDynSetsMathTest
make verilog CONFIG=WithL3DebugConfig NUM_CORES=1
```

Expected:
- ScalaTest PASS
- `make verilog CONFIG=WithL3DebugConfig` PASS, confirming the L2+HuanCun path elaborates with the new runtime `sets` IOs and storage semantics

- [ ] **Step 5: Commit**

```bash
git add huancun/src/main/scala/huancun/HuanCun.scala huancun/src/main/scala/huancun/Slice.scala huancun/src/main/scala/huancun/BaseDirectory.scala huancun/src/main/scala/huancun/DataStorage.scala huancun/src/main/scala/huancun/inclusive/Directory.scala huancun/src/main/scala/huancun/noninclusive/Directory.scala src/test/scala/xiangshan/CacheDynSetsMathTest.scala
git commit -m "feat(l3): support dynamic HuanCun set selection"
```

### Task 5: Full Verification and Cleanup

**Files:**
- Modify: `src/test/scala/xiangshan/CacheDynSetsMathTest.scala`
- Test: `src/test/scala/xiangshan/CacheDynSetsMathTest.scala`

- [ ] **Step 1: Write the failing test**

Add the final regression expectations to `src/test/scala/xiangshan/CacheDynSetsMathTest.scala` so the test suite documents the constraints that must remain stable after refactors.

```scala
  "Dynamic set legality" should "reject zero and oversized values" in {
    coupledL2.DynamicSetMath.isPow2(0) shouldBe false
    huancun.DynamicSetMath.isValidRuntimeSets(runtimeSets = 0, staticSets = 2048) shouldBe false
    huancun.DynamicSetMath.isValidRuntimeSets(runtimeSets = 4096, staticSets = 2048) shouldBe false
  }
```

If any helper or assertion logic drifted during implementation, this final test is the guardrail.

- [ ] **Step 2: Run test to verify it fails when helper behavior is wrong**

Temporarily break one of the legality helpers, then run:

```bash
mill -i xiangshan.test.testOnly xiangshan.CacheDynSetsMathTest
```

Expected: FAIL on the zero or oversized runtime-set test.

- [ ] **Step 3: Write minimal implementation**

Restore the correct helper logic and keep the final suite minimal. No new RTL is required here beyond any last cleanup needed to match the assertions already implemented in `DSECtrlUnit.scala`.

```scala
def isValidRuntimeSets(runtimeSets: Int, staticSets: Int): Boolean = {
  runtimeSets > 0 &&
  runtimeSets <= staticSets &&
  isPow2(runtimeSets)
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
mill -i xiangshan.test.testOnly xiangshan.CacheDynSetsMathTest
mill -i xiangshan.compile
make verilog CONFIG=MinimalConfig NUM_CORES=1
make verilog CONFIG=WithL3DebugConfig NUM_CORES=1
```

Expected:
- ScalaTest PASS
- `mill -i xiangshan.compile` PASS
- `make verilog CONFIG=MinimalConfig` PASS
- `make verilog CONFIG=WithL3DebugConfig` PASS

- [ ] **Step 5: Commit**

```bash
git add src/test/scala/xiangshan/CacheDynSetsMathTest.scala
git commit -m "test: verify dynamic cache set integration"
```
