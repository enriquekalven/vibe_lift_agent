# VibeLift Agent — Google ADK & Agent Starter Pack Standard Recipes
.PHONY: install lint test coverage smoke ci playground demo deploy

PYTHON ?= python3
OFFLINE_ENV = GOOGLE_APPLICATION_CREDENTIALS=/nonexistent/offline-test-credentials.json GOOGLE_CLOUD_PROJECT=test-project

install:
	$(PYTHON) -m pip install -r requirements.txt -c constraints.txt
	$(PYTHON) -m pip install "ruff>=0.5.0" "codespell>=2.2.0" "mypy>=1.11.0" "coverage[toml]>=7.5.0" "pytest>=8.0.0"

lint:
	$(PYTHON) -m compileall -q app vibelift tests deploy/bigquery
	ruff check .
	codespell
	mypy app vibelift deploy/bigquery

test:
	$(OFFLINE_ENV) $(PYTHON) -m unittest discover -s tests -t . -v

coverage:
	$(OFFLINE_ENV) $(PYTHON) -m coverage run --source=vibelift,app -m unittest discover -s tests -t .
	$(PYTHON) -m coverage report --fail-under=90 -m

smoke:
	$(OFFLINE_ENV) $(PYTHON) -c 'import vibelift.server as s; ctrl = s._global_controller; [assert_eq := (ctrl.get_state_payload(window_hours=h)["ge_fleet"]["window_hours"] == h) for h in (1,6,24,168,720,2160,4320,8760)]; print("Smoke OK")'

ci: lint coverage smoke

playground:
	PORT=8080 $(PYTHON) -m vibelift.server --port=8080

demo: playground

deploy:
	./deploy/deploy_cloud_run.sh
