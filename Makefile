# Creative Studio — local dev + deploy helpers
# Most of the time, you just need: make deploy-prod or make rollback
# Requires: docker, ssh access to the Coolify server (key in ~/.ssh/coolify_new),
#           gh CLI authenticated as camster91

SERVER := coolify
CONTAINER := photogen
STAGE_CONTAINER := photogen-stage
DATA_DIR := /root/photogen-data
OUTPUTS_DIR := /root/photogen-outputs
STAGE_DATA_DIR := /root/photogen-stage-data
STAGE_OUTPUTS_DIR := /root/photogen-stage-outputs
ENV_FILE := /root/.env.photogen
STAGE_ENV_FILE := /root/.env.photogen-stage
IMAGE := ghcr.io/camster91/creative-studio

# ── Local dev ────────────────────────────────────────────────
.PHONY: dev
dev:
	@echo "Local dev: open scripts/creative-studio-web.py in your editor."
	@echo "  python3 scripts/creative-studio-web.py"
	@echo "  (requires GEMINI_API_KEY in env)"

.PHONY: install
install:
	@if [ ! -d .venv ]; then uv venv .venv --python 3.12; fi
	@.venv/bin/pip install -e .

.PHONY: test
test:
	@.venv/bin/python -m pytest tests/ -v

.PHONY: test-js
test-js:
	@python3 -c "import re; src=open('scripts/creative-studio-web.py').read(); start=src.find('HTML_TEMPLATE = r\\\"\\\"\\\"') + len('HTML_TEMPLATE = r\\\"\\\"\\\"'); end=src.find('\\\"\\\"\\\"', start); js=src[start:end][src[start:end].find('<script>')+8:src[start:end].rfind('</script>')]; open('/tmp/cs.js','w').write(js)"
	@node --check /tmp/cs.js && echo "JS syntax OK"

.PHONY: lint
lint: test test-js
	@echo "All checks passed."

# ── Build ─────────────────────────────────────────────────────
.PHONY: build
build:
	docker build -t creative-studio:$(shell git rev-parse --short HEAD) -t creative-studio:latest .

.PHONY: build-push
build-push:
	@echo "Logging in to ghcr.io..."
	@echo "$$GITHUB_TOKEN" | docker login ghcr.io -u camster91 --password-stdin
	docker buildx build --platform linux/amd64 \
		-t $(IMAGE):$(shell git rev-parse --short HEAD) \
		-t $(IMAGE):$(shell git rev-parse HEAD) \
		--push .

# ── Deploy to staging (manual, bypasses CI for quick testing) ──
.PHONY: deploy-stage
deploy-stage: build
	ssh $(SERVER) "docker tag creative-studio:$(shell git rev-parse --short HEAD) $(IMAGE):local-build && \
		docker pull $(IMAGE):$(shell git rev-parse --short HEAD) 2>/dev/null || docker pull $(IMAGE):local-build 2>/dev/null || true"
	ssh $(SERVER) 'docker stop $(STAGE_CONTAINER) 2>/dev/null; docker rm $(STAGE_CONTAINER) 2>/dev/null; \
		docker run -d --name $(STAGE_CONTAINER) --network coolify --restart unless-stopped -p 5174:5173 \
			-v $(STAGE_DATA_DIR):/app/data -v $(STAGE_OUTPUTS_DIR):/app/outputs \
			--env-file $(STAGE_ENV_FILE) -e CREATIVE_OUTPUT_DIR=/app/outputs -e CREATIVE_DATA_DIR=/app/data -e PORT=5173 \
			$(IMAGE):$(shell git rev-parse --short HEAD) || \
		docker run -d --name $(STAGE_CONTAINER) --network coolify --restart unless-stopped -p 5174:5173 \
			-v $(STAGE_DATA_DIR):/app/data -v $(STAGE_OUTPUTS_DIR):/app/outputs \
			--env-file $(STAGE_ENV_FILE) -e CREATIVE_OUTPUT_DIR=/app/outputs -e CREATIVE_DATA_DIR=/app/data -e PORT=5173 \
			creative-studio:latest'
	sleep 5
	@echo "Staging health:"
	@curl -sf http://$(SERVER):5174/api/whoami | python3 -m json.tool

# ── Deploy to production (manual, bypasses CI) ──
.PHONY: deploy-prod
deploy-prod: build
	ssh $(SERVER) "docker tag creative-studio:$(shell git rev-parse --short HEAD) $(IMAGE):local-build"
	ssh $(SERVER) 'docker stop $(CONTAINER) 2>/dev/null; docker rm $(CONTAINER) 2>/dev/null; \
		docker run -d --name $(CONTAINER) --network coolify --restart unless-stopped -p 5173:5173 \
			-v $(DATA_DIR):/app/data -v $(OUTPUTS_DIR):/app/outputs \
			--env-file $(ENV_FILE) -e CREATIVE_OUTPUT_DIR=/app/outputs -e CREATIVE_DATA_DIR=/app/data -e PORT=5173 \
			-l traefik.enable=true \
			-l "traefik.http.routers.https-0-$(CONTAINER).rule=Host(\`photogen.ashbi.ca\`)" \
			-l traefik.http.routers.https-0-$(CONTAINER).tls=true \
			-l traefik.http.routers.https-0-$(CONTAINER).tls.certresolver=letsencrypt \
			-l traefik.http.routers.https-0-$(CONTAINER).entrypoints=https \
			-l traefik.http.services.https-0-$(CONTAINER).loadbalancer.server.port=5173 \
			-l coolify.managed=true \
			$(IMAGE):$(shell git rev-parse --short HEAD)'
	sleep 8
	@echo "Prod health:"
	@curl -sf http://$(SERVER):5173/api/whoami | python3 -m json.tool

# ── Rollback to a specific git tag or SHA ──
.PHONY: rollback
rollback:
	@if [ -z "$(TAG)" ]; then echo "Usage: make rollback TAG=v4.5.1-prod"; exit 1; fi
	ssh $(SERVER) "docker pull $(IMAGE):$(TAG) || docker pull $(IMAGE):$(shell git rev-parse --short $(TAG))"
	ssh $(SERVER) 'docker stop $(CONTAINER) 2>/dev/null; docker rm $(CONTAINER) 2>/dev/null; \
		docker run -d --name $(CONTAINER) --network coolify --restart unless-stopped -p 5173:5173 \
			-v $(DATA_DIR):/app/data -v $(OUTPUTS_DIR):/app/outputs \
			--env-file $(ENV_FILE) -e CREATIVE_OUTPUT_DIR=/app/outputs -e CREATIVE_DATA_DIR=/app/data -e PORT=5173 \
			-l traefik.enable=true \
			-l "traefik.http.routers.https-0-$(CONTAINER).rule=Host(\`photogen.ashbi.ca\`)" \
			-l traefik.http.routers.https-0-$(CONTAINER).tls=true \
			-l traefik.http.routers.https-0-$(CONTAINER).tls.certresolver=letsencrypt \
			-l traefik.http.routers.https-0-$(CONTAINER).entrypoints=https \
			-l traefik.http.services.https-0-$(CONTAINER).loadbalancer.server.port=5173 \
			-l coolify.managed=true \
			$(IMAGE):$(TAG)'
	@curl -sf http://$(SERVER):5173/api/whoami | python3 -m json.tool

# ── Logs ──────────────────────────────────────────────────────
.PHONY: logs
logs:
	ssh $(SERVER) "docker logs -f --tail 100 $(CONTAINER)"

.PHONY: logs-stage
logs-stage:
	ssh $(SERVER) "docker logs -f --tail 100 $(STAGE_CONTAINER)"

# ── Status checks ─────────────────────────────────────────────
.PHONY: status
status:
	@echo "=== Prod ==="
	@curl -s https://photogen.ashbi.ca/api/whoami | python3 -m json.tool
	@echo ""
	@echo "=== Staging ==="
	@curl -sf http://$(SERVER):5174/api/whoami 2>/dev/null | python3 -m json.tool || echo "Staging not deployed"
	@echo ""
	@echo "=== Containers ==="
	@ssh $(SERVER) "docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}' | grep -E 'photogen|NAMES'"

.PHONY: costs
costs:
	@curl -s https://photogen.ashbi.ca/api/costs | python3 -m json.tool

# ── Backup ───────────────────────────────────────────────────
.PHONY: backup
backup:
	@DATE=$$(date +%F); \
	ssh $(SERVER) "mkdir -p /root/backups/creative-studio/$$DATE/{data,outputs,env} && \
		tar czf /root/backups/creative-studio/$$DATE/data/sessions-and-costs.tgz -C $(DATA_DIR) . && \
		tar czf /root/backups/creative-studio/$$DATE/outputs/outputs.tgz -C $(OUTPUTS_DIR) . && \
		cp $(ENV_FILE) /root/backups/creative-studio/$$DATE/env/env.photogen.$$DATE && \
		chmod 600 /root/backups/creative-studio/$$DATE/env/env.photogen.$$DATE"
	@echo "Backup complete: /root/backups/creative-studio/$$DATE"

# ── Help ─────────────────────────────────────────────────────
.PHONY: help
help:
	@echo "Targets:"
	@echo "  make dev          - local dev instructions"
	@echo "  make test         - run pytest"
	@echo "  make test-js      - validate the JS in the HTML template parses"
	@echo "  make lint         - run all tests"
	@echo "  make build        - build Docker image locally"
	@echo "  make deploy-stage - build + push + deploy to staging (manual)"
	@echo "  make deploy-prod  - build + push + deploy to production (manual)"
	@echo "  make rollback TAG=v4.5.1-prod - rollback prod to a specific tag"
	@echo "  make logs         - tail prod container logs"
	@echo "  make status       - show prod + staging + container status"
	@echo "  make costs        - show today's spend + by-model breakdown"
	@echo "  make backup       - snapshot data + outputs + env to /root/backups/"
