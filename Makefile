.PHONY: requirements upgrade-requirements docs

requirements: export CUSTOM_COMPILE_COMMAND=`make requirements` or `make upgrade-requirements`
requirements:
	pip-compile -o requirements/requirements.txt requirements/requirements.in
	pip-compile -o requirements/prod-requirements.txt requirements/prod-requirements.in --allow-unsafe
	pip-compile -o requirements/test-requirements.txt requirements/test-requirements.in
	pip-compile -o requirements/dev-requirements.txt requirements/dev-requirements.in
	scripts/pip-post-compile.sh requirements/*requirements.txt

	cp requirements/*requirements.txt requirements-python3/
	grep -v '^futures==' requirements-python3/requirements.txt > temp && mv temp requirements-python3/requirements.txt
	grep -v '^futures==' requirements-python3/prod-requirements.txt > temp && mv temp requirements-python3/prod-requirements.txt
	grep -v '^futures==' requirements-python3/test-requirements.txt > temp && mv temp requirements-python3/test-requirements.txt
	grep -v '^futures==' requirements-python3/dev-requirements.txt > temp && mv temp requirements-python3/dev-requirements.txt

upgrade-requirements: export CUSTOM_COMPILE_COMMAND=`make requirements` or `make upgrade-requirements`
upgrade-requirements:
	pip-compile --upgrade -o requirements/requirements.txt requirements/requirements.in
	pip-compile --upgrade -o requirements/prod-requirements.txt requirements/prod-requirements.in --allow-unsafe
	pip-compile --upgrade -o requirements/test-requirements.txt requirements/test-requirements.in
	pip-compile --upgrade -o requirements/dev-requirements.txt requirements/dev-requirements.in
	scripts/pip-post-compile.sh requirements/*requirements.txt

	cp requirements/*requirements.txt requirements-python3/
	grep -v '^futures==' requirements-python3/requirements.txt > temp && mv temp requirements-python3/requirements.txt
	grep -v '^futures==' requirements-python3/prod-requirements.txt > temp && mv temp requirements-python3/prod-requirements.txt
	grep -v '^futures==' requirements-python3/test-requirements.txt > temp && mv temp requirements-python3/test-requirements.txt
	grep -v '^futures==' requirements-python3/dev-requirements.txt > temp && mv temp requirements-python3/dev-requirements.txt

docs:
	cd docs && $(MAKE) html
