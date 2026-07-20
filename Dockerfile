# JK Tyre BTP — B2C building+curing scheduler API
#
# Linux/amd64 image. On Windows this runs as a LINUX container via Docker
# Desktop + WSL2 — there is no separate Windows image and none is needed
# (gunicorn is Unix-only; a Windows base would break it).
#
# Build (from an Apple-Silicon Mac, cross-compile via QEMU):
#   docker buildx build --platform linux/amd64 -t <user>/jkt-btp-planning:v1-amd64 --load .
#
# Run (secrets injected at runtime — NEVER baked in):
#   docker run -d --name jkt-btp-planning -p 5001:5001 \
#     --env-file .env -v "$PWD/output:/app/output" <user>/jkt-btp-planning:v1-amd64
#   PowerShell:  -v "${PWD}\output:/app/output"   (backtick ` for line continuation)
FROM python:3.14-slim

# tzdata is REQUIRED: connection.now_ist() uses ZoneInfo("Asia/Kolkata").
# Without it ZoneInfo raises and timestamps silently fall back to UTC.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata curl \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PLAN_OUTPUT_DIR=/app/output

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Building/curing workbooks are written here and kept, so they can be
# downloaded. Mount a host volume at /app/output to see them outside.
RUN mkdir -p /app/output /app/data/output

EXPOSE 5001

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
  CMD curl -fsS http://localhost:5001/app/v1/jkt/planning-scheduling/health || exit 1

# --workers 1  : _RUN_LOCK and the per-run config injection in main.py are
#                PROCESS-local; >1 worker breaks the 409 concurrency guard.
# --threads 4  : keeps /health (and the healthcheck) responsive while a plan
#                run blocks the request for 1-4 minutes.
# --timeout    : a run is synchronous; must exceed the longest plan.
CMD ["gunicorn", "--workers", "1", "--threads", "4", "--timeout", "1800", \
     "--access-logfile", "-", "--error-logfile", "-", \
     "--bind", "0.0.0.0:5001", "app:app"]
