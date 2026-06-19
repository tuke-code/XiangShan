# Integer ER Rename Old-Destination Audit

This document records the task7 audit result for the int-only early register
release work. It is intentionally limited to the old-destination read strategy
needed before the later Rename, MEFreeList, RAB, ROB, and Difftest integration
tasks.

## Scope

Audited files:

- `src/main/scala/xiangshan/backend/rename/RenameTable.scala`
- `src/main/scala/xiangshan/backend/rename/Rename.scala`
- `src/main/scala/xiangshan/backend/rename/freelist/MEFreeList.scala`
- `src/main/scala/xiangshan/backend/rename/freelist/BaseFreeList.scala`
- `src/main/scala/xiangshan/backend/rob/Rab.scala`
- `src/main/scala/xiangshan/Bundle.scala`
- `src/main/scala/xiangshan/backend/Bundles.scala`
- `src/main/scala/xiangshan/backend/CtrlBlock.scala`
- `src/main/scala/xiangshan/backend/decode/DecodeStage.scala`
- `src/main/scala/xiangshan/backend/IntEarlyReleaseBundles.scala`
- `src/main/scala/xiangshan/backend/IntSparseUCA.scala`

Source requirements checked:

- `mydocs/new-er/spec/int-er-sparse-uca-spec.md`, especially old-dest read,
  commit suppress, RAB metadata, and risk sections.
- `mydocs/new-er/plan/plan.md`, task7 through task11.

BitLesson selection for this task returned placeholder text rather than a real
lesson. The effective selected lesson set is `NONE`.

## Conclusion

Functional ER must add a real rename-time integer old-destination read. It must
not reuse `psrcIntForMove`, `lsrc(0)`, commit-time `int_old_pdest`, or a separate
simplified RAT mirror.

The implementation strategy for task8 is:

1. Add an ER-only old-destination read port vector that follows the existing
   decode-to-rename RAT read path:
   `DecodeStageIO` -> `CtrlBlock` -> `Rename` IO -> `RenameTableWrapper`.
2. Drive each lane in `DecodeStage`, not inside `Rename`, with the same output
   uop and hold boundary that current integer source RAT reads use:
   - `addr := io.out(i).bits.ldest(log2Ceil(IntLogicRegs) - 1, 0)`
   - `hold := !io.out(i).ready`
3. Connect those ports as the third integer RAT read port per rename lane when
   `EnableIntEarlyRegRelease` is true. Existing `intReadPorts` remain the two
   source ports.
4. Consume the returned data in `Rename` as the base mapping for the aligned
   `Rename.io.in` uop before same-rename-group writes.
5. Apply a same-group bypass over all older lanes that really write the integer
   RAT in the current rename group. The bypass data is the older lane's final
   post-bypass `pdest`.
6. Generate UCA redef probes only for eligible, firing, non-move integer
   destination uops. Older move-eliminated writes still participate in the
   same-group old-dest bypass because they update the architectural integer RAT
   mapping, but a move lane itself is not an ER producer or ER redefiner.
7. Keep functional early-free disabled or observe-only if the old-dest read port
   cannot be proven cycle-aligned with the current RAT read and same-group
   bypass behavior.

This strategy matches the current XiangShan ownership split: Rename/RAT owns the
speculative mapping, MEFreeList is the only integer free-list writer, RAB carries
commit metadata, and ROB remains the later owner for precise readDone validation.

## Current Timing Facts

`RenameTable` uses a registered read address and a registered write bypass:

- `t1_raddr = RegEnable(p.addr, !p.hold)` records each read address.
- `t1_rdata_use_t1_raddr = spec_table(t1_raddr)` reads the speculative table.
- `t1_wSpec = RegNext(Mux(io.redirect, 0.U, io.specWritePorts))` delays
  speculative writes.
- The read result bypasses delayed writes with a registered `t1_bypass`.
- On redirect, the bypass vector is cleared and the table is restored through
  the existing snapshot or arch-table path.

Current integer source reads are driven through `DecodeStage`/`Rename` with:

- `addr := io.out(i).bits.lsrc(0/1)`
- `hold := !io.out(i).ready`

`CtrlBlock` wires `decode.io.intRat <> rename.io.intReadPorts`, and separately
pipelines `decode.io.out` into `rename.io.in` through `PipelineConnect`. The RAT
address is therefore presented while the uop is at `DecodeStage.io.out`, then
the registered RAT data is consumed when the same uop reaches `Rename.io.in`.
Current fusion replacement is applied after the decode-to-rename pipe and can
change selected control/source fields, but it does not replace ordinary integer
`ldest`; if a later fusion change can rewrite `ldest`, the old-dest read
placement must be revisited.

The ER old-dest read must mirror that path. Driving the old-dest address first
from `Rename.io.in(i).bits.ldest` would register the address in the rename cycle
and return the mapping in the following cycle, one cycle after the renamed uop
needs it.

The current commit old-dest path is not a rename-time source:

- `RenameTable.old_pdest` is produced from `arch_table` for commit or walk.
- `Rename.scala` frees integer old destinations with
  `intFreeList.io.freeReq(i) := int_need_free(i)` and
  `intFreeList.io.freePhyReg(i) := RegNext(int_old_pdest(i))`.
- That path is delayed for conventional free-list release and cannot tell UCA at
  rename time whether the old logical destination is currently tracked.

## Exact Old-Dest Dataflow

Task8 should add a decode-timed old-dest port chain:

```scala
// DecodeStageIO
val intOldDestRat =
  Option.when(EnableIntEarlyRegRelease)(
    Vec(RenameWidth, Flipped(new RatReadPort(log2Ceil(IntLogicRegs))))
  )

// Rename IO
val intOldDestReadPorts =
  Option.when(EnableIntEarlyRegRelease)(
    Vec(RenameWidth, new RatReadPort(log2Ceil(IntLogicRegs)))
  )

// RenameTableWrapper IO
val intOldDestReadPorts =
  Option.when(EnableIntEarlyRegRelease)(
    Vec(RenameWidth, new RatReadPort(log2Ceil(IntLogicRegs)))
  )
```

When enabled, `RenameTable(Reg_I, ...)` needs one extra read port per rename
lane. A minimal implementation can make the integer read-port count equal to
`backendParams.numIntRegSrc + 1` when ER is enabled, and connect:

```text
intRat.io.readPorts :=
  io.intReadPorts.flatten ++ io.intOldDestReadPorts.get
```

The top-level path must be wired in `CtrlBlock` next to the existing source RAT
ports:

```scala
decode.io.intOldDestRat.get <> rename.io.intOldDestReadPorts.get
```

`Rename.scala` then passes its top-level port through to the wrapper, matching
the existing source RAT pattern:

```scala
val intOldDestReadPorts = rat.io.intOldDestReadPorts.get
io.intOldDestReadPorts.get <> intOldDestReadPorts
```

The old-dest ports are driven in `DecodeStage`:

```scala
for (i <- 0 until DecodeWidth) {
  io.intOldDestRat.get(i).addr :=
    io.out(i).bits.ldest(log2Ceil(IntLogicRegs) - 1, 0)
  io.intOldDestRat.get(i).hold := !io.out(i).ready
}
```

`Rename` consumes the returned base value from its wrapper-connected port:

```scala
val intOldDestBase =
  VecInit(rat.io.intOldDestReadPorts.get.map(_.data))
```

This data is aligned with `Rename.io.in`, just like `intReadPortsData` for
`src0/src1`. Do not drive the old-dest read address from `Rename.io.in`.

The same-group bypass must use the same mapping order as integer RAT writes:

```scala
val intOldDestForER = Wire(Vec(RenameWidth, UInt(PhyRegIdxWidth.W)))
for (i <- 0 until RenameWidth) {
  intOldDestForER(i) := (0 until i).foldLeft(intOldDestBase(i)) { (old, j) =>
    val sameLdest =
      intRenamePorts(j).addr === io.in(i).bits.ldest(log2Ceil(IntLogicRegs) - 1, 0)
    val writesRat = intRenamePorts(j).wen
    Mux(writesRat && sameLdest, intRenamePorts(j).data, old)
  }
}
```

The increasing-lane `foldLeft` intentionally lets the youngest older matching
lane replace earlier matches, matching the current psrc bypass style. A `MuxCase`
implementation must reverse the older-lane sequence or otherwise provide the
same youngest-older priority.

The bypass must include older move-eliminated integer writes because XiangShan
updates the integer RAT for moves with the move source physical register. It
must not turn those move lanes into ER redefiners. The distinction is:

- Same-group old-dest bypass follows architectural RAT update semantics.
- UCA producer/redef eligibility follows ER safety policy and excludes move
  lanes.

## Rename-To-UCA Gating

UCA source probes must be built after final same-group source bypass, move
elimination, LUI fixes, and fused LUI-load fixes:

```text
probe.psrc := io.out(i).bits.psrc(s)
probe.srcIdx := s.U
probe.valid := io.out(i).fire && source is an integer register source
```

For this first int-only implementation, only logical `src0` and `src1` can be
integer RAT sources. The existing parameter guard `IntERIntSourceTopologyOk`
already requires `backendParams.numIntRegSrc == 2` when ER is enabled.

UCA producer allocation is valid only when the lane really allocates a new
integer physical destination:

```text
io.out(i).fire
&& io.out(i).bits.rfWen
&& !io.out(i).bits.isMove
&& io.out(i).bits.ldest =/= 0
&& single-uop eligibility holds
&& no unsupported exception/flush/single-step policy rejects the uop
```

UCA redef is valid only for eligible non-move integer destination uops:

```text
io.out(i).fire
&& io.out(i).bits.rfWen
&& !io.out(i).bits.isMove
&& io.out(i).bits.ldest =/= 0
&& single-uop eligibility holds
```

The redef probe payload is:

```text
oldPdest := intOldDestForER(i)
robIdx := io.out(i).bits.robIdx
```

If a move source matches a tracked entry, the matched entry must be forced to
fallback. This avoids releasing a physical register that has become part of a
move-eliminated architectural mapping. A move lane must produce:

```text
dest.valid = false
redef.valid = false
```

The first integration should also reject compressed or split ROB entries for ER
tracking unless task17 introduces a separate precise metadata store. The
eligibility predicate should include:

```text
firstUop && lastUop && numUops === 1
```

and later ROB/RAB code should assert that tracked ER metadata never appears in a
multi-uop compressed entry.

## Redirect, Snapshot, Hold, And Walk Rules

The old-dest read must not create a new recovery model:

- The base old-dest value comes from the existing integer `RenameTable`, so it
  follows the same snapshot and redirect repair as current integer source reads.
- `RenameTable` clears the registered write bypass on redirect. Do not add a
  second old-dest table that has to be repaired separately.
- During decode-to-rename backpressure, `hold := !io.out(i).ready` in
  `DecodeStage` keeps the old-dest address stable exactly like the existing
  source RAT read ports.
- During RAB walk, `Rename.io.out.valid` is false and `intSpecWen` is false.
  UCA source, alloc, and redef events must be gated by `io.out(i).fire`, so a
  walk cannot consume stale old-dest data.
- During redirect, `intSpecWen` is false and `RenameTable` clears speculative
  bypass. UCA rename events must additionally be gated by the same redirect kill
  policy used by `IntSparseUCA.redirectKill`.

If a later timing experiment changes where the old-dest port is physically
declared, the architectural contract is unchanged: the address is issued from
the decode output side, the hold condition is the decode-to-rename ready, and
the data is consumed only in the aligned rename cycle.

## Commit Suppress Alignment For Task10

Conventional integer free has a two-cycle lane relation from RAB commit metadata
to the final MEFreeList free lane:

```text
T:     RAB commit lane updates arch RAT and computes old_pdest for that lane.
T+1:   Rename sees raw int_old_pdest for that commit.
T+2:   Rename drives MEFreeList with
       freeReqBase = int_need_free
       freePdest   = RegNext(int_old_pdest)
```

Task10 must align ER suppress with that final free lane, not with the raw RAB
commit lane. The implementation should define the final free-lane signals first:

```scala
val intFreePdest = RegNext(int_old_pdest)
val intFreeReqBase = int_need_free
```

Then delay `RabCommitInfo.intERRedef` and the redefiner ROB index metadata to the
same lane and cycle as `intFreeReqBase/intFreePdest`. Suppress is legal only when
all identity fields match:

```text
intFreeReqBase
&& commitRedefDelayed.valid
&& commitRedefDelayed.oldPdest === intFreePdest
&& commitRedefDelayed.trackId/trackGen match the released UCA entry
&& commitRedefDelayed.redefinerRobIdx matches the released UCA entry
```

The final MEFreeList request is:

```scala
intFreeList.io.freeReq(i) := intFreeReqBase(i) && !erSuppressAligned(i)
intFreeList.io.freePhyReg(i) := intFreePdest(i)
```

Do not suppress by physical register alone. That would lose ABA safety when an
early-freed physical register is reallocated before the old redefiner commits.

## Implementation Hooks For Later Tasks

Task8 hooks:

- Add optional ER old-dest RAT read ports to `DecodeStageIO`, `Rename` IO, and
  `RenameTableWrapper`.
- Wire `decode.io.intOldDestRat.get <> rename.io.intOldDestReadPorts.get` in
  `CtrlBlock`.
- Increase the enabled integer RAT read-port count by one per rename lane.
- Drive the port in `DecodeStage` from `io.out(i).bits.ldest` with
  `hold := !io.out(i).ready`.
- Consume the returned base old-dest data in `Rename`, aligned with
  `Rename.io.in`.
- Build `intOldDestForER` with same-group older-lane bypass from final
  `intRenamePorts(j).data`.
- Instantiate/connect `IntSparseUCA` in `Rename.scala`.
- Drive UCA source probes from final `io.out(i).bits.psrc`.
- Drive UCA alloc from final non-move `io.out(i).bits.pdest`.
- Drive UCA redef from `intOldDestForER`.
- Attach `IntERUopMeta` fields to the renamed uop only after UCA outputs are
  computed.

Task9 hooks:

- Add an integer-only early-free input to `MEFreeList`.
- Merge early-free and conventional-free lanes inside the integer free-list
  owner.
- Keep FP, vector, v0, and vl free lists untouched.
- Replace or qualify the current integer debug invariant, because early release
  means a physical register can leave the arch-RAT set before the conventional
  commit free point.

Task10 hooks:

- Extend `RabCommitInfo` with optional `intERRedef`.
- Delay RAB ER redef metadata to the final integer free lane.
- Feed UCA commit suppress with aligned `intFreeReqBase` and `intFreePdest`.
- Assert suppress identity with `trackId`, `trackGen`, `oldPdest`, and
  `redefinerRobIdx`.

Task11 directed tests:

- Old-dest is not `lsrc(0)`: use an instruction with `ldest != lsrc0`, seed RAT
  mappings so stale `lsrc(0)` would produce a different oldPdest, and require
  the redef probe to use the `ldest` mapping.
- Same-group ordinary redef: lane 0 writes `x5 -> pA`; lane 1 writes `x5 -> pB`.
  Lane 1 oldPdest must be `pA`, not the stale RAT mapping.
- Same-group move then ordinary redef: lane 0 move writes `x5 -> pSrc`; lane 1
  writes `x5 -> pB`. Lane 1 oldPdest must be `pSrc`; lane 0 itself must not
  produce ER dest/redef metadata.
- Hold/stall: stall rename for at least one cycle and verify the held old-dest
  address/data pair is used only for the held uop.
- Cycle alignment: hold a decoded uop before rename, then change a following
  lane or next-cycle `ldest`; the consumed old-dest mapping must belong to the
  held `DecodeStage.io.out` uop, not to a later `Rename.io.in` address.
- Redirect/walk: assert no UCA source, alloc, or redef fires during redirect or
  RAB walk even if old-dest read data changes.
- Move fallback: a move source matching a tracked entry marks that entry
  fallback and does not allocate or redefine an ER entry.
- Duplicate free: conventional free and early-free must never enqueue the same
  physical register into `MEFreeList` in the same final free lane.
- Suppress alignment: after early-free, the redefiner commit suppresses the
  exact conventional free lane aligned with `RegNext(int_old_pdest)`.
- ABA reuse: early-free `p1`, reallocate `p1` to a younger producer, then commit
  the older redefiner. Suppress must match only the old `trackId/trackGen` and
  `redefinerRobIdx`, not the younger reuse.

## Risks To Track

- Adding one integer RAT read port per rename lane increases integer RAT read
  fanout and bypass muxing. The first functional mode must stay observe-only or
  disabled if timing/alignment cannot be proven.
- The old-dest port must be addressed at `DecodeStage.io.out`, not
  `Rename.io.in`; otherwise the synchronous RAT read returns one cycle late.
- Same-group old-dest bypass depends on final `intRenamePorts(j).data`, which for
  moves depends on final source bypass. This is already the rename critical path.
- ER source matching also depends on final post-bypass `psrc`, so task8 must place
  UCA probing after the existing bypass block rather than near the initial RAT
  read assignment.
- Commit suppress needs delayed RAB metadata. Using the raw RAB commit lane would
  be cycle-misaligned with the current integer free lane.
- ROB compression remains a correctness risk until task17/task20. The first
  integrated version should use single-uop eligibility and assertions.
- Difftest remains a separate blocking integration area before functional system
  validation. The A/B/C physical reuse case can still produce false mismatches
  until task21 through task23 land.
