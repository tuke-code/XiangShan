test:
	pytest -v tests -n 16

report:
	pytest -v tests \
	       --toffee-report \
	       --report-dir ./data/toffee_report_full \
	       --report-name memblock_full_regression_cov.html \
	       --report-dump-json
