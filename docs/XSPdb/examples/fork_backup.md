# Lagging Fork Waveform Backup

Enabling massive waveform recordings generally grinds simulations to a halt. Fork Backup keeps a low-overhead, suspended secondary background process. It acts as a time machine cache so you can replay a window of states seamlessly when an exception suddenly strikes.

## 1. Lifecycle Example Setup

Provide the length of the window (5.0s), the path to dump files (`./`), and the tracking log route.
```text
(XiangShan) xfork_backup_on 5.0 . ./fork_backup.log
```

## 2. Trigger the Main Thread
Now that the net is deployed, we configure a highly rigorous trigger sequence on the front process spanning millions of fast cycles.

```text
# Register a crash site anomaly signature
(XiangShan) xbreak SimTop_top.SimTop.cpu.l_soc.core_with_l2.core.backend.inner_ctrlBlock.rob.difftest_commit_pc == 0x80000000

# Execute extremely fast since we bypassed global .fst tracing.
(XiangShan) xstep 10000000
```

## 3. Collision and Recovery
Upon reaching the faulting site, the primary simulator wakes the fork child. The child will reproduce the precise past context right before the event while locally generating an `.fst` trace!

```text
# Verify the backup machinery generated the splice files effectively
(XiangShan) xfork_backup_status

# Clean up resources safely
(XiangShan) xfork_backup_off
```
> See `waveform.md` regarding `xwave_continue` to view how you stitch the fork output back into an analyzable main trace.
