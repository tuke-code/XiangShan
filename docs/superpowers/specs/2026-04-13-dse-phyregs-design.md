# DSE Dynamic Physical Register Configuration Design

## Goal

Port the dynamic `intphyregs` and `fpphyregs` support from the reference OpenLinkNan implementation into the current XiangShan tree, matching the behavior introduced by commits `b5a14649a074233ae4b1f7d993dd8218faf5f175` and `f5fe11c895e7488f06e18d73453fa9a9c60c662a`.

The migration must be functionally complete:

- software can program two banks for `intphyregs` and `fpphyregs`
- the active bank only takes effect during the DSE reset/apply window
- rename freelists consume the runtime logical size rather than elaboration-time constants
- default behavior remains unchanged when DSE does not override either parameter

## Scope

This work only covers the DSE-controlled physical register sizing path for integer and floating-point/vector rename resources.

Included:

- `DSECtrlUnit` banked register storage and bore-out for `intphyregs` and `fpphyregs`
- reset-time application semantics via `appliedCtrlSel`
- runtime logical-size support in rename freelists
- rename-level sink wiring from DSE control outputs into the active freelists

Excluded:

- unrelated DSE knobs
- driver-side software changes
- broader refactors in rename or DSE infrastructure not required for this feature

## Current State

In the current XiangShan tree:

- `DSECtrlUnit` still has `intPhyRegs` and `fpPhyRegs` support commented out
- `Rename` does not sink `DSE_INTFLSIZE` or `DSE_FPFLSIZE`
- freelists still use static `CircularQueuePtr` behavior, so logical size cannot change at runtime

The tree already contains `ResizeCircularQueuePtr` utility support, so the missing part is integrating it into the rename freelists and wiring the DSE control path end to end.

## Target Behavior

### DSE Control Path

`DSECtrlUnit` will expose banked runtime configuration registers for:

- `intPhyRegs0` and `intPhyRegs1` at `0x1C0` and `0x1C8`
- `fpPhyRegs0` and `fpPhyRegs1` at `0x1D0` and `0x1D8`

`ctrlSel` remains the software-visible selected bank. A new `appliedCtrlSel` register determines which bank is active in hardware. `appliedCtrlSel` updates only during the DSE reset/apply window, so a bank switch does not immediately shrink an executing core's freelist state.

`DSE_INTFLSIZE` and `DSE_FPFLSIZE` will be bored out from the active bank selected by `appliedCtrlSel`.

### Rename Runtime Sizing

Rename freelists will treat the elaboration-time size as a physical maximum and accept a runtime logical size through `io.psize`.

Required behavior:

- pointer arithmetic, wraparound, and free-count computation use the runtime logical size
- a logical-size change resets freelist state to a clean initial state
- redirect and resize are handled through the same recovery path where appropriate
- debug checks do not falsely fire while resize-driven state reset is still propagating

### Integer and FP/Vec Mapping

`intphyregs` maps to the integer freelist logical size.

`fpphyregs` maps to the shared FP/Vec freelist logical size, following the reference implementation. In this tree that corresponds to the freelist instantiated as `vecFreeList`, whose capacity represents the shared FP+Vec dynamic register pool after subtracting architectural FP and vector logical registers.

No separate floating-point-only freelist will be introduced.

## Design Changes

### 1. DSECtrlUnit

File: `src/main/scala/xiangshan/DSECtrlUnit.scala`

Changes:

- add `appliedCtrlSel`
- initialize `intPhyRegs0/1` with `IntPhyRegs`
- initialize `fpPhyRegs0/1` with the current maximum shared FP/Vec freelist size
- expose the four regmap entries for the two parameters
- select active `intPhyRegs` and `fpPhyRegs` from `appliedCtrlSel`
- add `BoringUtils.addSource` for `DSE_INTFLSIZE` and `DSE_FPFLSIZE`
- add assertions that runtime values do not exceed supported maxima

The existing DSE reset sequencing remains responsible for detecting `ctrlSel` changes and driving the reset/apply window.

### 2. BaseFreeList

File: `src/main/scala/xiangshan/backend/rename/freelist/BaseFreeList.scala`

Changes:

- add `io.psize`
- replace `CircularQueuePtr` with `ResizeCircularQueuePtr`
- add helpers to attach the current logical size to pointers before arithmetic or comparison
- detect `psize` changes
- treat resize similarly to redirect for snapshot/recovery purposes

This file provides the common mechanism used by both `MEFreeList` and `StdFreeList`.

### 3. MEFreeList

File: `src/main/scala/xiangshan/backend/rename/freelist/MEFreeList.scala`

Changes:

- perform allocation and free pointer arithmetic with the runtime-sized pointer helper
- compute free counts against the runtime logical size
- reset head, tail, arch head, and freelist contents when `psize` changes
- suppress delayed debug invariant checks while the resize reset is still flushing through the delayed debug shadows

### 4. StdFreeList

File: `src/main/scala/xiangshan/backend/rename/freelist/StdFreeList.scala`

Changes:

- same runtime-sized pointer treatment as `MEFreeList`
- reset head/tail/free-list state on logical-size change
- ensure `canAllocate` and free-count bookkeeping behave conservatively during resize transitions

### 5. Rename Wiring

File: `src/main/scala/xiangshan/backend/rename/Rename.scala`

Changes:

- sink `DSE_INTFLSIZE` into a runtime integer freelist size wire
- sink `DSE_FPFLSIZE` into a runtime shared FP/Vec freelist size wire
- validate minimum supported runtime sizes
- connect `io.psize` for the integer freelist and shared FP/Vec freelist
- keep `v0` and `vl` freelists on fixed maximum sizes

## Constraints

- Runtime logical size must never be zero.
- Runtime logical size must never exceed the elaboration-time maximum.
- Integer runtime logical size must not shrink below 32.
- Shared FP/Vec runtime logical size must not shrink below 32.
- Default values must match the current static configuration so the feature is a no-op until programmed by DSE.

## Verification Plan

Minimum verification for this task:

- elaborate or compile the modified XiangShan sources far enough to type-check the DSE and rename changes
- run a targeted build/check covering the modified rename freelist files and `DSECtrlUnit`
- confirm the default path still elaborates with runtime sizes left at their reset values

If existing targeted tests are available for rename or DSE, they should be preferred. If not, successful compilation/elaboration of the modified sources is the acceptance baseline for this migration.

## Risks

- The current XiangShan tree may differ from the reference rename implementation in surrounding signal names or debug behavior, so the migration must preserve local interfaces where possible.
- The shared FP/Vec freelist mapping is easy to misread as pure FP state. The implementation must keep the reference semantic rather than introducing a separate FP-only pool.
- Resize-triggered freelist reset must not race with delayed debug shadow state, or false invariant failures can appear.

## Acceptance Criteria

- `DSECtrlUnit` exposes and bores out runtime `intphyregs` and `fpphyregs` settings.
- The active DSE parameter bank is applied only during the reset/apply window.
- Integer and shared FP/Vec freelists consume runtime logical sizes.
- A logical-size change resets freelist runtime state safely.
- The tree compiles or elaborates successfully after the migration.
