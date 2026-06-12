# Creative Studio — local dev + build helpers.
#
# Deploy is intentionally NOT in this Makefile. The deploy path is
# documented in RUNBOOK.md (source-tarball + on-server `docker build` +
# `docker run` on port 32778 behind a Caddy reverse proxy). Reasons:
#   1. The previous `make deploy-prod` / `make rollback` targets used
#      Coolify's Traefik-on-host labels, which are dead since 2026-06-11.
#   2. RUNBOOK is the canonical deploy procedure; a stale Makefile target
#      that disagrees with the runbook is worse than no target.
#   3. Forces a deploy-test pass through the runbook on every deploy —
#      which is the only deploy frequency that matters.
#
# Local dev / test / build still work below.

# ── Local dev ──────────────────────────────────────────────────
.PHONY: dev
dev:
	@echo "Local dev: open scripts/creative-studio-web.py in your editor."
	@echo "  python3 scripts/creative-studio-web.py"
	@echo "  (requires GEMINI_API_KEY in env)"

.PHONY: install
install:
	@if [ ! -d .venv ]; then uv venv .venv --python 3.12; fi
	@uv pip install --python .venv/bin/python -e .
	@uv pip install --python .venv/bin/python pytest

# ── Tests ──────────────────────────────────────────────────────
.PHONY: test
test:
	@.venv/bin/python -m pytest tests/ -v

# test-js extracts the inline <script> block from the Flask HTML_TEMPLATE
# (legacy compat: kept here so CI's `make lint` still works on old branches
# that ship JS inline in the template). New code lives in static/app.js and
# is checked directly with `node --check`.
.PHONY: test-js
test-js:
	@node --check static/app.js && echo "static/app.js OK"
	@python3 -c "import re; src=open('scripts/creative-studio-web.py').read(); start=src.find('HTML_TEMPLATE = r\\\\\\\"\\\\\\\"\\\\\\\"') + len('HTML_TEMPLATE = r\\\\\\\"\\\\\\\"\\\\\\\"'); end=src.find('\\\\\\\"\\\\\\\"\\\\\\\"', start); js=src[start:end][src[start:end].find('<script>')+8:src[start:end].rfind('</script>')]; open('/tmp/cs.js','w').write(js)" 2>/dev/null || echo "(no inline JS in template — skipping legacy check)"
	@if [ -s /tmp/cs.js ]; then node --check /tmp/cs.js && echo "inline JS OK"; rm -f /tmp/cs.js; fi

.PHONY: lint
lint: test test-js
	@echo "All checks passed."

# ── Build ──────────────────────────────────────────────────────
.PHONY: build
build:
	docker build -t creative-studio:$(shell git rev-parse --short HEAD) -t creative-studio:latest .

# ── Help ───────────────────────────────────────────────────────
.PHONY: help
help:
	@echo "Targets:"
	@echo "  make dev          - local dev instructions"
	@echo "  make install      - create .venv and install deps (uv + pip -e .)"
	@echo "  make test         - run pytest"
	@echo "  make test-js      - validate static/app.js (and legacy inline JS if present)"
	@echo "  make lint         - run all tests"
	@echo "  make build        - build Docker image locally"
	@echo ""
	@echo "Deploy is in RUNBOOK.md, not in this Makefile. See the runbook for:"
	@echo "  - source-tarball + on-server 'docker build' procedure"
	@echo "  - 'docker run' flags (port 32778, no Traefik labels, no --network coolify)"
	@echo "  - Caddy reverse-proxy config for photogen.ashbi.ca"
	@echo "  - rollback to a previous image tag"
