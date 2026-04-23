# ═══════════════════════════════════════════════════════════
# Creative Studio — Production Dockerfile for Coolify/VPS
# ═══════════════════════════════════════════════════════════
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# Non-root user for security
RUN useradd -m -s /bin/bash appuser

WORKDIR /app

# Copy dependency files
COPY pyproject.toml uv.lock ./
COPY scripts/ ./scripts/
COPY launch.sh refine.sh ./
COPY recipes/ ./recipes/

# Install dependencies as root first (uv needs write), then fix ownership
RUN uv sync --frozen --no-dev
RUN chown -R appuser:appuser /app

# Production WSGI server
RUN uv pip install gunicorn

# Create writable directories for output
RUN mkdir -p /app/data/sessions /app/outputs /app/data/uploads && chown -R appuser:appuser /app/data /app/outputs

# Switch to app user
USER appuser

# Environment
ENV PYTHONUNBUFFERED=1
ENV FLASK_APP=scripts.creative-studio-web
ENV PORT=5173

# The volume for persistent outputs
VOLUME ["/app/outputs"]

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:$PORT/api/costs > /dev/null 2>&1 || exit 1

EXPOSE 5173

# Run via gunicorn (production WSGI server)
CMD ["uv", "run", "gunicorn", "-w", "2", "-b", "0.0.0.0:5173", "--access-logfile", "-", "--error-logfile", "-", "scripts.creative-studio-web:app"]
