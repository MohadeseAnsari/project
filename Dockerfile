FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN groupadd --system --gid 1000 appgroup \
 && useradd  --system --uid 1000 --gid appgroup \
             --create-home --home-dir /home/appuser \
             --shell /usr/sbin/nologin appuser

WORKDIR /app

COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --index-url https://package-mirror.liara.ir/repository/pypi/simple \
    -r requirements.txt

COPY --chown=appuser:appgroup app/         app/
COPY --chown=appuser:appgroup migrations/  migrations/
COPY --chown=appuser:appgroup scripts/     scripts/

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; \
      sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
