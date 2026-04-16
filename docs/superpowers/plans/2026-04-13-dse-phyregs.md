# DSE Dynamic Physical Register Configuration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement complete runtime `intphyregs` and `fpphyregs` support in XiangShan so DSE can program both knobs safely and they only take effect during the DSE apply/reset window.

**Architecture:** Extend `DSECtrlUnit` with banked runtime physical-register controls selected by `appliedCtrlSel`, then migrate rename freelists from static circular pointers to runtime-sized pointers driven by DSE-sourced logical sizes. Keep default elaboration-time capacities as the physical maxima and reset freelist state whenever the runtime logical size changes.

**Tech Stack:** Scala, Chisel3, Rocket Chip regmap, BoringUtils, XiangShan rename freelist infrastructure

---

## File Map

- Modify: `src/main/scala/xiangshan/DSECtrlUnit.scala`
  Responsibility: expose banked `intphyregs/fpphyregs`, add applied-on-reset selection, bore out runtime control signals.
- Modify: `src/main/scala/xiangshan/backend/rename/Rename.scala`
  Responsibility: sink DSE runtime sizes and wire them into the active freelists.
- Modify: `src/main/scala/xiangshan/backend/rename/freelist/BaseFreeList.scala`
  Responsibility: add runtime logical-size input and common resize-aware pointer helpers.
- Modify: `src/main/scala/xiangshan/backend/rename/freelist/MEFreeList.scala`
  Responsibility: apply runtime logical size to the integer freelist implementation.
- Modify: `src/main/scala/xiangshan/backend/rename/freelist/StdFreeList.scala`
  Responsibility: apply runtime logical size to the shared FP/Vec freelist implementation.
- Verify: elaboration/build commands that cover the modified XiangShan tree.

### Task 1: Add DSE runtime physical-register control support

**Files:**
- Modify: `src/main/scala/xiangshan/DSECtrlUnit.scala`

- [ ] **Step 1: Write the failing check**

Use the existing source as the failing baseline: `intPhyRegs` and `fpPhyRegs` are commented out, there is no `appliedCtrlSel`, and no `DSE_INTFLSIZE` / `DSE_FPFLSIZE` source exists.

Expected failing indicators before implementation:

```text
rg -n "appliedCtrlSel|DSE_INTFLSIZE|DSE_FPFLSIZE|0x1C0|0x1D0" src/main/scala/xiangshan/DSECtrlUnit.scala
```

Expected: no active `appliedCtrlSel`, and physical-register DSE source lines are commented out.

- [ ] **Step 2: Run the failing check**

Run:

```bash
rg -n "appliedCtrlSel|DSE_INTFLSIZE|DSE_FPFLSIZE|0x1C0|0x1D0" src/main/scala/xiangshan/DSECtrlUnit.scala
```

Expected: matches only commented lines or no `appliedCtrlSel`, proving the feature is absent.

- [ ] **Step 3: Write the minimal implementation**

Apply the following edits in `src/main/scala/xiangshan/DSECtrlUnit.scala`:

```scala
val appliedCtrlSel = RegInit(0.U(8.W))

val intPhyRegs0 = RegInit(IntPhyRegs.U(64.W))
val intPhyRegs1 = RegInit(IntPhyRegs.U(64.W))
val intPhyRegs = Wire(UInt(64.W))

val fpPhyRegs0 = RegInit((VfPhyRegs - VecLogicRegs - FpLogicRegs).U(64.W))
val fpPhyRegs1 = RegInit((VfPhyRegs - VecLogicRegs - FpLogicRegs).U(64.W))
val fpPhyRegs = Wire(UInt(64.W))

when (coreResetReg) {
  appliedCtrlSel := ctrlSel
}

0x1C0 -> Seq(RegField(64, intPhyRegs0)),
0x1C8 -> Seq(RegField(64, intPhyRegs1)),
0x1D0 -> Seq(RegField(64, fpPhyRegs0)),
0x1D8 -> Seq(RegField(64, fpPhyRegs1)),

robSize := Mux(appliedCtrlSel.orR, robSize1, robSize0)
lqSize := Mux(appliedCtrlSel.orR, lqSize1, lqSize0)
sqSize := Mux(appliedCtrlSel.orR, sqSize1, sqSize0)
ftqSize := Mux(appliedCtrlSel.orR, ftqSize1, ftqSize0)
ibufSize := Mux(appliedCtrlSel.orR, ibufSize1, ibufSize0)
intPhyRegs := Mux(appliedCtrlSel.orR, intPhyRegs1, intPhyRegs0)
fpPhyRegs := Mux(appliedCtrlSel.orR, fpPhyRegs1, fpPhyRegs0)

BoringUtils.addSource(intPhyRegs, "DSE_INTFLSIZE")
BoringUtils.addSource(fpPhyRegs, "DSE_FPFLSIZE")

assert(intPhyRegs <= IntPhyRegs.U, "DSE parameter must not exceed IntPhyRegs")
assert(fpPhyRegs <= (VfPhyRegs - VecLogicRegs - FpLogicRegs).U, "DSE parameter must not exceed FpFreeListSize")
```

Keep the existing `ctrlSel` visible to software and let `coreResetReg` remain the existing apply window for this tree.

- [ ] **Step 4: Run the targeted check**

Run:

```bash
rg -n "appliedCtrlSel|DSE_INTFLSIZE|DSE_FPFLSIZE|0x1C0|0x1D0" src/main/scala/xiangshan/DSECtrlUnit.scala
```

Expected: all feature lines are now active and no longer commented out.

- [ ] **Step 5: Commit**

```bash
git add src/main/scala/xiangshan/DSECtrlUnit.scala
git commit -m "feat: add dse phyreg control support"
```

### Task 2: Make freelist base logic resize-aware

**Files:**
- Modify: `src/main/scala/xiangshan/backend/rename/freelist/BaseFreeList.scala`

- [ ] **Step 1: Write the failing check**

Use the current file as the failing baseline: it still extends `HasCircularQueuePtrHelper`, has no `io.psize`, and defines `FreeListPtr` with `CircularQueuePtr`.

```text
rg -n "HasCircularQueuePtrHelper|val psize|ResizeCircularQueuePtr|redirectOrResize" src/main/scala/xiangshan/backend/rename/freelist/BaseFreeList.scala
```

Expected: `HasCircularQueuePtrHelper` exists, while `psize`, `ResizeCircularQueuePtr`, and `redirectOrResize` do not.

- [ ] **Step 2: Run the failing check**

Run:

```bash
rg -n "HasCircularQueuePtrHelper|val psize|ResizeCircularQueuePtr|redirectOrResize" src/main/scala/xiangshan/backend/rename/freelist/BaseFreeList.scala
```

Expected: confirms the base freelist is still static-size.

- [ ] **Step 3: Write the minimal implementation**

Update `src/main/scala/xiangshan/backend/rename/freelist/BaseFreeList.scala` to introduce runtime logical-size support:

```scala
abstract class BaseFreeList(size: Int, numLogicRegs:Int = 32)(implicit p: Parameters)
  extends XSModule with HasResizeCircularQueuePtrHelper {
  val io = IO(new Bundle {
    val redirect = Input(Bool())
    val walk = Input(Bool())
    val psize = Input(UInt(log2Up(size + 1).W))
    ...
  })

  class FreeListPtr extends ResizeCircularQueuePtr[FreeListPtr](size)

  object FreeListPtr {
    def apply(f: Boolean, v: Int): FreeListPtr = {
      val ptr = Wire(new FreeListPtr)
      ptr.flag := f.B
      ptr.value := v.U
      ptr.psize.valid := false.B
      ptr.psize.bits := size.U
      ptr
    }
  }

  protected def withPSize(ptr: FreeListPtr): FreeListPtr = {
    val sizedPtr = WireInit(ptr)
    sizedPtr.psize.valid := true.B
    sizedPtr.psize.bits := io.psize
    sizedPtr
  }

  protected def ptrToOH(ptr: FreeListPtr): UInt = withPSize(ptr).toOH

  protected def resizedTailPtr(flag: Boolean): FreeListPtr = {
    val ptr = Wire(new FreeListPtr)
    ptr.flag := flag.B
    ptr.value := Mux(io.psize === 0.U, 0.U, io.psize - 1.U)
    ptr.psize.valid := true.B
    ptr.psize.bits := io.psize
    ptr
  }

  val lastPSize = RegNext(io.psize, size.U(log2Up(size + 1).W))
  val psizeChanged = lastPSize =/= io.psize
  val redirectOrResize = io.redirect || psizeChanged
```

Also update snapshot and pointer consistency checks to use `redirectOrResize` and `ptrToOH`.

- [ ] **Step 4: Run the targeted check**

Run:

```bash
rg -n "HasResizeCircularQueuePtrHelper|val psize|ResizeCircularQueuePtr|redirectOrResize|withPSize" src/main/scala/xiangshan/backend/rename/freelist/BaseFreeList.scala
```

Expected: all resize-aware helpers are present.

- [ ] **Step 5: Commit**

```bash
git add src/main/scala/xiangshan/backend/rename/freelist/BaseFreeList.scala
git commit -m "refactor: make rename freelist base resize-aware"
```

### Task 3: Convert the integer freelist to runtime logical sizing

**Files:**
- Modify: `src/main/scala/xiangshan/backend/rename/freelist/MEFreeList.scala`

- [ ] **Step 1: Write the failing check**

Use the current file as the failing baseline: pointer arithmetic still uses static `headPtr`/`tailPtr`, and there is no `psizeChanged` reset path.

```text
rg -n "withPSize|psizeChanged|redirectOrResize|freeListInit|resizeInFlight" src/main/scala/xiangshan/backend/rename/freelist/MEFreeList.scala
```

Expected: no matches or only unrelated lines.

- [ ] **Step 2: Run the failing check**

Run:

```bash
rg -n "withPSize|psizeChanged|redirectOrResize|freeListInit|resizeInFlight" src/main/scala/xiangshan/backend/rename/freelist/MEFreeList.scala
```

Expected: proves the integer freelist is not yet resize-aware.

- [ ] **Step 3: Write the minimal implementation**

Update `src/main/scala/xiangshan/backend/rename/freelist/MEFreeList.scala` with:

```scala
private val freeListInit = Seq.tabulate(size - 1)(i => (i + 1).U(PhyRegIdxWidth.W)) :+ 0.U(PhyRegIdxWidth.W)
val freeList = RegInit(VecInit(freeListInit))

val doWalkRename = io.walk && io.doAllocate && !redirectOrResize
val doNormalRename = io.canAllocate && io.doAllocate && !redirectOrResize

val phyRegCandidates = VecInit.tabulate(RenameWidth + 1)(i => freeList((withPSize(headPtr) + i.U).value))
val archHeadPtrNew  = withPSize(archHeadPtr) + numArchAllocate
val headPtrNew      = Mux(lastCycleRedirect, redirectedHeadPtr, withPSize(headPtr) + numAllocate)
val headPtrOHNew    = Mux(lastCycleRedirect, redirectedHeadPtrOH, ptrToOH(headPtrNew))
val freePtr         = withPSize(tailPtr) + PopCount(io.freeReq.take(i))
val tailPtrNext     = withPSize(tailPtr) + PopCount(io.freeReq)

val freeRegCnt = Mux(
  doWalkRename && !lastCycleRedirect,
  distanceBetween(withPSize(tailPtrNext), withPSize(headPtr)) - PopCount(io.walkReq),
  Mux(
    doNormalRename,
    distanceBetween(withPSize(tailPtrNext), withPSize(headPtr)) - PopCount(io.allocateReq),
    distanceBetween(withPSize(tailPtrNext), withPSize(headPtr))
  )
)

when (psizeChanged) {
  headPtr := FreeListPtr(false, 0)
  headPtrOH := 1.U(size.W)
  archHeadPtr := FreeListPtr(false, 0)
  tailPtr := resizedTailPtr(false)
  freeList.zip(freeListInit).foreach { case (entry, init) => entry := init }
}.otherwise {
  archHeadPtr := archHeadPtrNext
  headPtr := headPtrNext
  headPtrOH := headPtrOHNext
  tailPtr := tailPtrNext
}
```

Add the delayed debug reset suppression logic around the invariant check so resize-triggered state reset does not falsely trip the debug assertion.

- [ ] **Step 4: Run the targeted check**

Run:

```bash
rg -n "withPSize|psizeChanged|redirectOrResize|freeListInit|resizeInFlight" src/main/scala/xiangshan/backend/rename/freelist/MEFreeList.scala
```

Expected: resize-aware allocation, reset, and debug handling are present.

- [ ] **Step 5: Commit**

```bash
git add src/main/scala/xiangshan/backend/rename/freelist/MEFreeList.scala
git commit -m "feat: support runtime integer freelist sizing"
```

### Task 4: Convert the shared FP/Vec freelist to runtime logical sizing

**Files:**
- Modify: `src/main/scala/xiangshan/backend/rename/freelist/StdFreeList.scala`

- [ ] **Step 1: Write the failing check**

Use the current file as the failing baseline: it still computes `tailPtr`, `headPtr`, and `freeRegCnt` with static pointer semantics.

```text
rg -n "withPSize|psizeChanged|redirectOrResize|freeListInit|realHeadPtr" src/main/scala/xiangshan/backend/rename/freelist/StdFreeList.scala
```

Expected: no matches or no active resize-aware logic.

- [ ] **Step 2: Run the failing check**

Run:

```bash
rg -n "withPSize|psizeChanged|redirectOrResize|freeListInit|realHeadPtr" src/main/scala/xiangshan/backend/rename/freelist/StdFreeList.scala
```

Expected: confirms the shared freelist is still static-size.

- [ ] **Step 3: Write the minimal implementation**

Update `src/main/scala/xiangshan/backend/rename/freelist/StdFreeList.scala` with:

```scala
private val freeListInit = Seq.tabulate(freeListSize)(i => (i + numLogicRegs).U(PhyRegIdxWidth.W))
val freeList = RegInit(VecInit(freeListInit))

val enqPtr = withPSize(lastTailPtr) + offset
tailPtr := withPSize(lastTailPtr) + PopCount(freeReqReg)

val phyRegCandidates = VecInit.tabulate(RenameWidth + 1)(i => freeList((withPSize(headPtr) + i.U).value))
val archHeadPtrNew = withPSize(archHeadPtr) + numArchAllocate

val isWalkAlloc = io.walk && io.doAllocate && !redirectOrResize
val isNormalAlloc = io.canAllocate && io.doAllocate && !redirectOrResize
val headPtrAllocate = Mux(lastCycleRedirect, redirectedHeadPtr, withPSize(headPtr) + numAllocate)
val headPtrOHAllocate = Mux(lastCycleRedirect, redirectedHeadPtrOH, ptrToOH(headPtrAllocate))

freeRegCnt := Mux(
  isWalkAlloc && !lastCycleRedirect,
  distanceBetween(withPSize(tailPtr), withPSize(headPtr)) - PopCount(io.walkReq),
  Mux(
    isNormalAlloc,
    distanceBetween(withPSize(tailPtr), withPSize(headPtr)) - PopCount(io.allocateReq),
    distanceBetween(withPSize(tailPtr), withPSize(headPtr))
  )
)

when (psizeChanged) {
  headPtr := FreeListPtr(false, 0)
  headPtrOH := 1.U(freeListSize.W)
  archHeadPtr := FreeListPtr(false, 0)
  lastTailPtr := FreeListPtr(true, 0)
  freeList.zip(freeListInit).foreach { case (entry, init) => entry := init }
}.otherwise {
  archHeadPtr := archHeadPtrNext
  headPtr := realHeadPtr
  headPtrOH := realHeadPtrOH
  lastTailPtr := tailPtr
}
```

Also gate `freeRegCntReg` during resize so `canAllocate` stays conservative while the freelist resets.

- [ ] **Step 4: Run the targeted check**

Run:

```bash
rg -n "withPSize|psizeChanged|redirectOrResize|freeListInit|realHeadPtr" src/main/scala/xiangshan/backend/rename/freelist/StdFreeList.scala
```

Expected: resize-aware shared freelist logic is active.

- [ ] **Step 5: Commit**

```bash
git add src/main/scala/xiangshan/backend/rename/freelist/StdFreeList.scala
git commit -m "feat: support runtime shared fp-vec freelist sizing"
```

### Task 5: Wire DSE runtime sizes into Rename

**Files:**
- Modify: `src/main/scala/xiangshan/backend/rename/Rename.scala`

- [ ] **Step 1: Write the failing check**

Use the current file as the failing baseline: there are no `DSE_INTFLSIZE` or `DSE_FPFLSIZE` sinks, and freelists are instantiated without `psize`.

```text
rg -n "DSE_INTFLSIZE|DSE_FPFLSIZE|pIntFreeListSize|pFpFreeListSize|io.psize" src/main/scala/xiangshan/backend/rename/Rename.scala
```

Expected: no matches, proving Rename does not yet consume the DSE runtime controls.

- [ ] **Step 2: Run the failing check**

Run:

```bash
rg -n "DSE_INTFLSIZE|DSE_FPFLSIZE|pIntFreeListSize|pFpFreeListSize|io.psize" src/main/scala/xiangshan/backend/rename/Rename.scala
```

Expected: confirms the missing sink wiring.

- [ ] **Step 3: Write the minimal implementation**

Update `src/main/scala/xiangshan/backend/rename/Rename.scala`:

```scala
import chisel3.util.experimental.BoringUtils

val pIntFreeListSize = WireInit(IntPhyRegs.U(log2Up(IntPhyRegs + 1).W))
BoringUtils.addSink(pIntFreeListSize, "DSE_INTFLSIZE")
XSError(pIntFreeListSize < 32.U, "IntFreeListSize should be at least 32\n")

val pFpFreeListSize = WireInit((VfPhyRegs - VecLogicRegs - FpLogicRegs).U(log2Up(VfPhyRegs - VecLogicRegs - FpLogicRegs + 1).W))
BoringUtils.addSink(pFpFreeListSize, "DSE_FPFLSIZE")
XSError(pFpFreeListSize < 32.U, "FpFreeListSize should be at least 32\n")

val intFreeList = Module(new MEFreeList(IntPhyRegs))
val fpFreeList = Module(new StdFreeList(FpPhyRegs - FpLogicRegs, FpLogicRegs, Reg_F))
val vecFreeList = Module(new StdFreeList(VfPhyRegs - VecLogicRegs, VecLogicRegs, Reg_V, 31))

intFreeList.io.psize := pIntFreeListSize
fpFreeList.io.psize := pFpFreeListSize
vecFreeList.io.psize := pFpFreeListSize
v0FreeList.io.psize := (V0PhyRegs - V0LogicRegs).U
vlFreeList.io.psize := (VlPhyRegs - VlLogicRegs).U
```

Adapt the exact freelist wiring to the current tree’s structure. If the current XiangShan tree keeps FP and Vec as separate freelists, preserve that structure while still letting `fpphyregs` drive the intended floating-point-side logical size.

- [ ] **Step 4: Run the targeted check**

Run:

```bash
rg -n "DSE_INTFLSIZE|DSE_FPFLSIZE|pIntFreeListSize|pFpFreeListSize|io.psize" src/main/scala/xiangshan/backend/rename/Rename.scala
```

Expected: Rename now sinks and forwards both runtime sizes.

- [ ] **Step 5: Commit**

```bash
git add src/main/scala/xiangshan/backend/rename/Rename.scala
git commit -m "feat: wire dse runtime phyreg sizes into rename"
```

### Task 6: Run focused verification

**Files:**
- Verify only

- [ ] **Step 1: Run the structural checks**

Run:

```bash
rg -n "DSE_INTFLSIZE|DSE_FPFLSIZE|appliedCtrlSel|io.psize|ResizeCircularQueuePtr" \
  src/main/scala/xiangshan/DSECtrlUnit.scala \
  src/main/scala/xiangshan/backend/rename/Rename.scala \
  src/main/scala/xiangshan/backend/rename/freelist/BaseFreeList.scala \
  src/main/scala/xiangshan/backend/rename/freelist/MEFreeList.scala \
  src/main/scala/xiangshan/backend/rename/freelist/StdFreeList.scala
```

Expected: all runtime-size integration points are present.

- [ ] **Step 2: Run a targeted compile/elaboration command**

Run the narrowest project command available in this tree that type-checks the modified XiangShan sources. Prefer an existing target such as:

```bash
make verilog CONFIG=MinimalConfig
```

If that is too heavy for the environment, run the repository’s narrower Chisel elaboration or compile entry point that still compiles the modified files.

Expected: exit code `0`.

- [ ] **Step 3: Inspect the output for the actual result**

Record:

- command used
- exit code
- whether any warnings or unrelated failures occurred

If the command fails, capture the first relevant error and loop back to the owning task before claiming success.

- [ ] **Step 4: Commit**

```bash
git add src/main/scala/xiangshan/DSECtrlUnit.scala \
  src/main/scala/xiangshan/backend/rename/Rename.scala \
  src/main/scala/xiangshan/backend/rename/freelist/BaseFreeList.scala \
  src/main/scala/xiangshan/backend/rename/freelist/MEFreeList.scala \
  src/main/scala/xiangshan/backend/rename/freelist/StdFreeList.scala
git commit -m "feat: support dse runtime physical register sizing"
```

## Self-Review

- Spec coverage: the plan covers DSE control-path exposure, applied-on-reset semantics, runtime-sized freelists, rename sink wiring, and verification.
- Placeholder scan: the only flexible item is the final compile command because the exact fastest repository command depends on what this tree already supports; the implementer must still run a concrete command and record it.
- Type consistency: all runtime logical-size plumbing uses `io.psize`, `withPSize`, `DSE_INTFLSIZE`, and `DSE_FPFLSIZE` consistently across tasks.
