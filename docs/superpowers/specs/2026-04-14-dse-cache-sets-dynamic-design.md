# DSE Cache Sets Dynamic Design

## Goal

Make `L2SETS` and `L3SETS` runtime-configurable through DSE in this repository, following the same semantic model used in the reference repository, while adapting the implementation to XiangShan's current cache architecture.

The scope of this design is limited to:

- XiangShan DSE control path
- Coupled L2 (`coupledL2`)
- HuanCun L3 (`huancun`)

Out of scope:

- `openLLC`
- dynamic MSHR changes
- unrelated cache parameter controls

## Current Architecture

### DSE control path

The DSE control unit already supports runtime selection for several parameters through a ping-pong register model in [src/main/scala/xiangshan/DSECtrlUnit.scala](/nfs/home/wujiabin/work/xs-env/XiangShan/src/main/scala/xiangshan/DSECtrlUnit.scala). The `L2SETS` and `L3SETS` controls are present only as commented-out code.

The runtime selection model is:

- software writes candidate values into `*_0` and `*_1`
- `ctrlSel` selects which bank should become active
- `appliedCtrlSel` updates only when a core reset is triggered
- downstream modules consume the selected values through `BoringUtils`

This means cache set changes are expected to take effect only after the DSE-controlled reset boundary, not immediately in the middle of live traffic.

### L2 path

L2 is instantiated in [src/main/scala/xiangshan/L2Top.scala](/nfs/home/wujiabin/work/xs-env/XiangShan/src/main/scala/xiangshan/L2Top.scala) through either:

- `TL2TLCoupledL2`
- `TL2CHICoupledL2`

Both are built from [coupledL2/src/main/scala/coupledL2/CoupledL2.scala](/nfs/home/wujiabin/work/xs-env/XiangShan/coupledL2/src/main/scala/coupledL2/CoupledL2.scala).

Within `coupledL2`, the actual set index is consumed by:

- directory SRAM indexing in [coupledL2/src/main/scala/coupledL2/Directory.scala](/nfs/home/wujiabin/work/xs-env/XiangShan/coupledL2/src/main/scala/coupledL2/Directory.scala)
- data SRAM indexing in [coupledL2/src/main/scala/coupledL2/DataStorage.scala](/nfs/home/wujiabin/work/xs-env/XiangShan/coupledL2/src/main/scala/coupledL2/DataStorage.scala)

### L3 path

Non-CHI L3 is instantiated in [src/main/scala/top/Top.scala](/nfs/home/wujiabin/work/xs-env/XiangShan/src/main/scala/top/Top.scala) as `HuanCun`.

The relevant HuanCun hierarchy is:

- top-level module: [huancun/src/main/scala/huancun/HuanCun.scala](/nfs/home/wujiabin/work/xs-env/XiangShan/huancun/src/main/scala/huancun/HuanCun.scala)
- slice: [huancun/src/main/scala/huancun/Slice.scala](/nfs/home/wujiabin/work/xs-env/XiangShan/huancun/src/main/scala/huancun/Slice.scala)
- shared directory primitive: [huancun/src/main/scala/huancun/BaseDirectory.scala](/nfs/home/wujiabin/work/xs-env/XiangShan/huancun/src/main/scala/huancun/BaseDirectory.scala)
- inclusive directory wrapper: [huancun/src/main/scala/huancun/inclusive/Directory.scala](/nfs/home/wujiabin/work/xs-env/XiangShan/huancun/src/main/scala/huancun/inclusive/Directory.scala)
- non-inclusive directory wrapper: [huancun/src/main/scala/huancun/noninclusive/Directory.scala](/nfs/home/wujiabin/work/xs-env/XiangShan/huancun/src/main/scala/huancun/noninclusive/Directory.scala)
- data SRAM path: [huancun/src/main/scala/huancun/DataStorage.scala](/nfs/home/wujiabin/work/xs-env/XiangShan/huancun/src/main/scala/huancun/DataStorage.scala)

HuanCun is banked, but `HCCacheParameters.sets` is already defined per bank in this repository. Therefore, runtime `L3SETS` must be interpreted as per-bank sets as well.

## Problem Statement

Masking the runtime set index without changing the stored tag format is incorrect.

Example:

- static L2 or L3 has 1024 sets
- runtime configuration selects 512 sets
- two different lines differ only in the highest set bit

If the implementation only masks the SRAM index to 9 bits, those lines alias to the same set. If the tag still stores only the original tag bits, the dropped set bit is lost and the directory can report false hits.

Therefore, correct runtime set reduction requires two changes:

1. SRAM indexing must use the masked runtime set.
2. The dropped high set bits must be folded into the stored tag and the tag compare path.

This is the same semantic model used by the reference repository.

## Design

### 1. Restore DSE control registers

In [src/main/scala/xiangshan/DSECtrlUnit.scala](/nfs/home/wujiabin/work/xs-env/XiangShan/src/main/scala/xiangshan/DSECtrlUnit.scala):

- restore `l2Sets0`, `l2Sets1`, and `l2Sets`
- restore `l3Sets0`, `l3Sets1`, and `l3Sets`
- re-enable their regmap entries
- re-enable `BoringUtils.addSource` for `DSE_L2SETS` and `DSE_L3SETS`
- select the active value using `appliedCtrlSel`, not `ctrlSel`

Initial values:

- `L2SETS` initializes from `p(L2ParamKey).sets`
- `L3SETS` initializes from `p(SoCParamsKey).L3CacheParamsOpt.get.sets`

### 2. Enforce runtime constraints in DSE

`L2SETS` constraints:

- `l2Sets <= staticL2Sets`
- `PopCount(l2Sets) === 1.U`

`L3SETS` constraints:

- `l3Sets <= staticL3Sets`
- `PopCount(l3Sets) === 1.U`

Rationale:

- the implementation derives effective set bits with `Log2(runtimeSets)`
- in this repository, HuanCun `sets` already means sets per bank

### 3. Propagate runtime L2 sets through L2Top

In [src/main/scala/xiangshan/L2Top.scala](/nfs/home/wujiabin/work/xs-env/XiangShan/src/main/scala/xiangshan/L2Top.scala):

- add a `BoringUtils` sink for `DSE_L2SETS`
- drive `l2cache.module.io.sets`

This keeps the runtime control localized at the integration boundary and avoids adding DSE-specific code inside unrelated core logic.

### 4. Extend coupledL2 with runtime set selection

#### 4.1 Top-level and slice plumbing

Add `sets: UInt(64.W)` at the coupled L2 module IO in [coupledL2/src/main/scala/coupledL2/CoupledL2.scala](/nfs/home/wujiabin/work/xs-env/XiangShan/coupledL2/src/main/scala/coupledL2/CoupledL2.scala), and add `dynSets` on slice IO in [coupledL2/src/main/scala/coupledL2/BaseSlice.scala](/nfs/home/wujiabin/work/xs-env/XiangShan/coupledL2/src/main/scala/coupledL2/BaseSlice.scala).

Wire `dynSets` through:

- [coupledL2/src/main/scala/coupledL2/tl2tl/Slice.scala](/nfs/home/wujiabin/work/xs-env/XiangShan/coupledL2/src/main/scala/coupledL2/tl2tl/Slice.scala)
- [coupledL2/src/main/scala/coupledL2/tl2chi/Slice.scala](/nfs/home/wujiabin/work/xs-env/XiangShan/coupledL2/src/main/scala/coupledL2/tl2chi/Slice.scala)

#### 4.2 Runtime set helpers

Add the following helper semantics in coupledL2 parameter utilities:

- `dynSetBits = Log2(io.dynSets)`
- `dynSetMask(set, dynSetBits) = set & ((1 << dynSetBits) - 1)`
- `extendTag(tag, set, dynSetBits) = Cat(tag, set >> dynSetBits)`

This preserves the identity of lines that differ only in the truncated high set bits.

#### 4.3 Directory changes

In [coupledL2/src/main/scala/coupledL2/Directory.scala](/nfs/home/wujiabin/work/xs-env/XiangShan/coupledL2/src/main/scala/coupledL2/Directory.scala):

- add `dynSets` input
- index tag/meta/replacer SRAMs with masked set
- widen stored tag from `tagBits` to `tagBits + setBits`
- when writing tag SRAM, store `extendTag(writeTag, writeSet, dynSetBits)`
- when comparing, compare stored extended tag with `extendTag(req.tag, req.set, dynSetBits)`
- when returning public tag fields, strip the appended high set fragment and return the logical tag width expected by the rest of coupledL2

#### 4.4 Data storage changes

In [coupledL2/src/main/scala/coupledL2/DataStorage.scala](/nfs/home/wujiabin/work/xs-env/XiangShan/coupledL2/src/main/scala/coupledL2/DataStorage.scala):

- add `dynSets` input
- replace the array index calculation `Cat(way, set)` with `Cat(way, dynSetMask(set, dynSetBits))`

This must match directory masking exactly.

### 5. Propagate runtime L3 sets through Top and HuanCun

In [src/main/scala/top/Top.scala](/nfs/home/wujiabin/work/xs-env/XiangShan/src/main/scala/top/Top.scala):

- add a `BoringUtils` sink for `DSE_L3SETS`
- drive `l3cacheOpt.get.module.io.sets`

In [huancun/src/main/scala/huancun/HuanCun.scala](/nfs/home/wujiabin/work/xs-env/XiangShan/huancun/src/main/scala/huancun/HuanCun.scala):

- add top-level `sets: UInt(64.W)` IO
- forward it to each slice as `dynSets`

### 6. Interpret runtime L3 sets as per-bank sets

In this repository, `HCCacheParameters.sets` is already a per-bank quantity. HuanCun slice-local `set` fields are therefore also per-bank. Runtime set reduction should use the DSE value directly.

Derived values:

- `dynSetBits = Log2(io.dynSets)`
- `dynSetMask(set) = set & ((1 << dynSetBits) - 1)`
- `extendTag(tag, set) = Cat(tag, set >> dynSetBits)`

This matches the existing HuanCun address decomposition and avoids introducing a second global-to-bank interpretation layer that does not exist in the current architecture.

### 7. Extend HuanCun slice, directory, and data storage

#### 7.1 Slice plumbing

In [huancun/src/main/scala/huancun/Slice.scala](/nfs/home/wujiabin/work/xs-env/XiangShan/huancun/src/main/scala/huancun/Slice.scala):

- add `dynSets` input
- pass it to directory and data storage

#### 7.2 Base directory primitive

In [huancun/src/main/scala/huancun/BaseDirectory.scala](/nfs/home/wujiabin/work/xs-env/XiangShan/huancun/src/main/scala/huancun/BaseDirectory.scala):

- add `dynSets` input to the primitive interface
- compute dynamic set bits from the per-bank runtime set count
- mask all SRAM set indices for tag/meta/replacer access
- widen stored tag from `tagBits` to `tagBits + setBits`
- write `extendTag(tag, set)` to tag SRAM
- compare requested `extendTag(req.tag, req.set)` against the stored value
- return the logical tag portion to existing consumers

This centralizes most of the HuanCun change for both inclusive and non-inclusive self directory flows.

#### 7.3 Inclusive directory wrapper

In [huancun/src/main/scala/huancun/inclusive/Directory.scala](/nfs/home/wujiabin/work/xs-env/XiangShan/huancun/src/main/scala/huancun/inclusive/Directory.scala):

- pass `dynSets` into the shared base directory logic
- do not change the public logical set/tag interface seen by the rest of the inclusive slice

#### 7.4 Non-inclusive directory wrapper

In [huancun/src/main/scala/huancun/noninclusive/Directory.scala](/nfs/home/wujiabin/work/xs-env/XiangShan/huancun/src/main/scala/huancun/noninclusive/Directory.scala):

- apply runtime set handling only to `selfDir`
- keep `clientDir` unchanged

Rationale:

- `selfDir` tracks L3-resident lines and therefore depends on L3 runtime set count
- `clientDir` tracks client cache structure and must retain the client-defined set geometry

#### 7.5 Data storage

In [huancun/src/main/scala/huancun/DataStorage.scala](/nfs/home/wujiabin/work/xs-env/XiangShan/huancun/src/main/scala/huancun/DataStorage.scala):

- add `dynSets` input
- in the internal address remap path, replace the raw `set` field with `dynSetMask(set)`
- keep the externally visible logical `set` fields unchanged for protocol and pipeline logic

This ensures physical SRAM indexing matches directory behavior while avoiding protocol-level semantic changes.

## Error Handling and Safety Rules

The implementation relies on strict runtime legality checks in DSE instead of trying to tolerate arbitrary set counts.

Rejected values:

- zero
- non-power-of-two values
- values larger than the elaborated cache configuration

No fallback or clamping behavior will be added. Illegal configurations should fail immediately through assertions.

## Testing Strategy

### Elaboration and compile coverage

Run at least:

- a build/elaboration path that includes `L2Top` with coupled L2 enabled
- a build/elaboration path that includes HuanCun L3 enabled

### Behavioral checks

At minimum, validate:

- default runtime values equal static values and produce no behavioral change
- reduced `L2SETS` compiles and elaborates cleanly
- reduced `L3SETS` compiles and elaborates cleanly
- illegal `L2SETS` and `L3SETS` values are rejected by assertions

### Regression focus

High-risk areas:

- coupledL2 directory replacement state when masked sets alias
- HuanCun non-inclusive self directory versus client directory split
- HuanCun data storage row indexing consistency with directory indexing

## Implementation Notes

- This design intentionally does not change `openLLC`.
- This design intentionally does not attempt live in-flight cache resizing; changes are applied across the existing DSE reset boundary.
- Runtime `sets` handling must be implemented in storage/indexing structures, not in high-level task generation. High-level tasks should continue to use logical addresses.

## Acceptance Criteria

- `DSECtrlUnit` exposes working `L2SETS` and `L3SETS` controls again.
- Coupled L2 honors runtime `L2SETS` without false hits caused by dropped high set bits.
- HuanCun honors runtime `L3SETS` with correct per-bank indexing and without corrupting client directory semantics.
- Default static configurations remain behaviorally unchanged.
- `openLLC` remains untouched.
