run:
	pytest -v tests \
	       -s \
	       -k test_api_MemBlock_back_to_back_cacheable_load_saturation

test:
	pytest -v tests -n 16

report:
	PYTHONPATH=/nfs/home/majiuyue/ucagent/toffee-test pytest -v tests -n 32 \
	       --toffee-report \
	       --report-dir ./data/toffee_report_full \
	       --report-name memblock_full_regression_cov.html \
	       --report-dump-json
