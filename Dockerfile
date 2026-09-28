# Dockerfile for VibeLift Analytics Platform on Google Cloud Run
FROM python:3.12-slim

# Prevent Python from writing .pyc files and enable unbuffered output
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8080

WORKDIR /app

# Install Python dependencies. requirements.txt pins the top-level packages; constraints.txt locks every
# transitive version to the set verified in production, so rebuilds are reproducible.
# (python:3.12-slim already ships ca-certificates; no other system packages are needed.)
COPY requirements.txt constraints.txt ./
RUN pip install --no-cache-dir -r requirements.txt -c constraints.txt

# Copy application source code (.dockerignore keeps tests, deploy tooling and local env files out)
COPY . .

# Create non-root user and set permissions
RUN useradd -m -u 1000 appuser && \
    chown -R appuser:appuser /app
USER appuser

EXPOSE 8080

# Run FastAPI / Uvicorn server respecting Cloud Run $PORT
CMD exec uvicorn app.fast_api_app:app --host 0.0.0.0 --port ${PORT:-8080}
