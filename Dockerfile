# ═══════════════════════════════════════════════════════════
# Creative Studio — Production Dockerfile for Coolify/VPS
# ═══════════════════════════════════════════════════════════
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# Install curl for HEALTHCHECK (not guaranteed in slim base)
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

# Non-root user for security
RUN useradd -m -s /bin/bash appuser

WORKDIR /app

# Copy dependency files
COPY pyproject.toml uv.lock ./
COPY scripts/ ./scripts/
COPY templates/ ./templates/
COPY static/ ./static/
COPY launch.sh refine.sh ./
COPY recipes/ ./recipes/

# Fix figma_utils import path (file in scripts/ imported directly)
RUN ln -s /app/scripts/figma_utils.py /app/figma_utils.py 2>/dev/null || true

# Install dependencies as root first (uv needs write), then fix ownership
RUN uv sync --frozen --no-dev
RUN chown -R appuser:appuser /app

# Create writable directories for output
RUN mkdir -p /app/data/sessions /app/outputs /app/data/uploads

# Environment
ENV PYTHONUNBUFFERED=1
ENV FLASK_APP=scripts.creative-studio-web
ENV PORT=5173
ENV CREATIVE_OUTPUT_DIR=/app/outputs
ENV CREATIVE_DATA_DIR=/app/data

# The volume for persistent outputs + data
VOLUME ["/app/outputs", "/app/data"]

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:$PORT/api/costs > /dev/null 2>&1 || exit 1

EXPOSE 5173

# Run via gunicorn (production WSGI server)
CMD ["uv", "run", "gunicorn", "-w", "1", "-b", "0.0.0.0:5173", "--timeout", "300", "--access-logfile", "-", "--error-logfile", "-", "scripts.creative-studio-web:app"]
