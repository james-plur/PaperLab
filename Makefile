.PHONY: install-local install-publish test test-local test-publish boundaries

install-local:
	python3 -m venv paperlab_local/.venv
	paperlab_local/.venv/bin/pip install -e 'paperlab_local[sync]'

install-publish:
	python3 -m venv paperlab_publish/.venv
	paperlab_publish/.venv/bin/pip install -e 'paperlab_publish[backup]'

test: boundaries test-local test-publish

boundaries:
	python3 scripts/check_boundaries.py

test-local:
	python3 -m compileall -q paperlab_local/src
	PYTHONPATH=paperlab_local/src python3 -m unittest discover -s paperlab_local/tests

test-publish:
	python3 -m compileall -q paperlab_publish/src
	PYTHONPATH=paperlab_publish/src python3 -m unittest discover -s paperlab_publish/tests
