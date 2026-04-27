# MemBlock Coverage Report Workflow

## 1. 目的

本文档记录在 `toffee-report` 未稳定产出 `code_coverage.json` 或 line 汇总时，如何直接基于 `merged.info` 恢复 MemBlock 的 line / branch 覆盖率数字。

核心规则：

- line coverage 以 `DA:` 记录为准
- branch coverage 以 `BRDA:` 明细为准

原因是当前 `verilator_coverage -write-info` 生成的 LCOV 文件可能没有 `LF:` / `LH:`，且 `BRH:` / `BRF:` 与 `genhtml` 最终显示的 branch 总数口径并不一致；但 `DA:` / `BRDA:` 与 `genhtml` 展示结果是一致的，因此应直接按明细统计。

## 2. 多线程回归

```bash
source /nfs/home/share/unitychip/env.bashrc >/dev/null 2>&1 && \
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -p xdist.plugin -p toffee_test.plugin -n 16 -q src/test/python/MemBlock/tests
```

回归结束后，会在 `src/test/python/MemBlock/data/toffee_tmp_*` 下留下各 worker 的 `*.dat`。

## 3. 合并 dat

```bash
mkdir -p src/test/python/MemBlock/data/toffee_report_manual_run/line_dat
find src/test/python/MemBlock/data/toffee_tmp_* -type f -name '*.dat' | sort > \
  src/test/python/MemBlock/data/toffee_report_manual_run/dat_list.txt
xargs -a src/test/python/MemBlock/data/toffee_report_manual_run/dat_list.txt \
  verilator_coverage -write-info \
  src/test/python/MemBlock/data/toffee_report_manual_run/line_dat/merged.info
```

## 4. 生成摘要

```bash
python3 src/test/python/MemBlock/scripts/lcov_summary.py \
  src/test/python/MemBlock/data/toffee_report_manual_run/line_dat/merged.info \
  --output-json src/test/python/MemBlock/data/toffee_report_manual_run/line_dat/code_coverage.json
```

如果需要 HTML：

```bash
python3 src/test/python/MemBlock/scripts/lcov_summary.py \
  src/test/python/MemBlock/data/toffee_report_manual_run/line_dat/merged.info \
  --output-json src/test/python/MemBlock/data/toffee_report_manual_run/line_dat/code_coverage.json \
  --genhtml-output src/test/python/MemBlock/data/toffee_report_manual_run/line_dat
```

## 5. 当前已验证的口径

对 `2026-04-16` 的全量回归手工合并结果，脚本可正确恢复：

- line：`195234 / 295581 = 66.0509%`
- branch：`933899 / 1573403 = 59.3573%`

这组结果说明此前的 `line = 0 / 0` 是统计口径错误，不是仿真没有产出 line 数据。
