# Waveform Control & Fork Backup Integration

Capturing `.fst` waveforms is essential to view deep RTL signals but generates high overhead. XSPdb provides precise runtime toggles.

## 1. Basic On-Demand Waveform Capture
Start writing debug curves into a highly compressed FST file.

```text
# Turn on the waveform dumper (Defaults to wave_YYYYMMDD_HHMMSS.fst in the current directory)
(XiangShan) xwave_on

# 1000 clock cycles are thoroughly captured
(XiangShan) xstep 1000

# Force standard I/O to flush data directly into the .fst file (crucial before interacting with GTKWave)
(XiangShan) xwave_flush

# Suspend waveform recording to recover simulation performance speed
(XiangShan) xwave_off
```

## 2. Custom Output File Scoping
```text
# Begin recording into an explicitly designated FST file
(XiangShan) xwave_on /abs/path/to/my_bug_trace.fst
(XiangShan) xstep 200
(XiangShan) xwave_off
```

## 3. Fork Backup Continuation (`xwave_continue`)
Usually, generating waveforms constantly slows down the simulation. A common strategy is to generate them only when an error is detected via `xfork_backup_on`. When you identify a crash in the child process, you can surgically graft the backward-looking trace onto your primary file.

```text
# 1. Activate waveform in the primary debugger file
(XiangShan) xwave_on /abs/path/to/master_trace.fst

# 2. Absorb and fuse the background history generated from the fork engine
(XiangShan) xwave_continue /abs/path/to/fork_wave.fst

# 3. Carefully advance the simulation state and save
(XiangShan) xstep 100
(XiangShan) xwave_flush
```
