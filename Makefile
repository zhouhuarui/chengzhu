BACKEND_PYTHON ?= $(if $(wildcard backend/.venv/bin/python),.venv/bin/python,python3)

.PHONY: competition-up competition-down competition-verify test

competition-up:
	./agentteams/scripts/competition-up.sh

competition-down:
	./agentteams/scripts/competition-down.sh

competition-verify:
	./agentteams/scripts/verify.sh
	./agentteams/scripts/competition-preflight.sh

test:
	cd backend && $(BACKEND_PYTHON) -m pytest -q
	cd frontend && pnpm test -- --run
