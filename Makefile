# FinOps Dashboard Agent — Google ADK & Agent Starter Pack Standard Recipes
.PHONY: install lint test coverage smoke ci playground demo deploy preview eval eval-generate eval-grade

PYTHON ?= $(shell if [ -x .venv/bin/python ]; then echo .venv/bin/python; else echo python3; fi)
OFFLINE_ENV = GOOGLE_APPLICATION_CREDENTIALS=/nonexistent/offline-test-credentials.json GOOGLE_CLOUD_PROJECT=test-project
AGENT_EVAL_URL ?= https://finops-dashboard-mcp-app-123456789012.us-central1.run.app
AGENT_EVAL_DATASET ?= tests/eval/datasets/basic-dataset.json
AGENT_EVAL_CONFIG ?= tests/eval/eval_config.yaml

install:
	$(PYTHON) -m pip install -r requirements.txt -c constraints.txt
	$(PYTHON) -m pip install "ruff>=0.5.0" "codespell>=2.2.0" "mypy>=1.11.0" "coverage[toml]>=7.5.0" "pytest>=8.0.0"

lint:
	$(PYTHON) -m compileall -q app finops_dashboard tests deploy/bigquery
	$(PYTHON) -m ruff check .
	$(PYTHON) -m codespell_lib
	$(PYTHON) -m mypy app finops_dashboard deploy/bigquery

test:
	$(OFFLINE_ENV) $(PYTHON) -m unittest discover -s tests -t . -v

coverage:
	$(OFFLINE_ENV) $(PYTHON) -m coverage run --source=finops_dashboard,app -m unittest discover -s tests -t .
	$(PYTHON) -m coverage report --fail-under=90 -m

smoke:
	$(OFFLINE_ENV) $(PYTHON) -c 'import finops_dashboard.server as s; ctrl = s._global_controller; [assert_eq := (ctrl.get_state_payload(window_hours=h)["ge_fleet"]["window_hours"] == h) for h in (1,6,24,168,720,2160,4320,8760)]; print("Smoke OK")'

ci: lint coverage smoke

eval-generate:
	@rm -f artifacts/traces/*.json
	@TOKEN=$$(CLOUDSDK_METRICS_ENVIRONMENT="$${CLOUDSDK_METRICS_ENVIRONMENT:+$$CLOUDSDK_METRICS_ENVIRONMENT }datacloud.antigravity" gcloud auth print-identity-token) && \
	agents-cli eval generate --dataset "$(AGENT_EVAL_DATASET)" --url "$(AGENT_EVAL_URL)" --app-name app --concurrency 2 -H "Authorization: Bearer $$TOKEN"

eval-grade:
	agents-cli eval grade --config "$(AGENT_EVAL_CONFIG)" --qps 2

eval: eval-generate eval-grade


playground:
	PORT=8080 $(PYTHON) -m finops_dashboard.server --port=8080

demo: playground

# Dashboard on fictional fixture data at http://127.0.0.1:8766 (no Google Cloud calls; bannered as fictional).
preview:
	$(PYTHON) scripts/preview_dashboard.py --port 8766

deploy:
	./deploy/deploy_cloud_run.sh

enable-agent-logging:
	./deploy/enable_agent_logging.sh

disable-agent-logging:
	./deploy/enable_agent_logging.sh --disable


