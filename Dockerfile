# sptrader image: runs the CLI (ingest/optimize/report) and the web dashboard.
FROM python:3.11-slim

# psycopg2-binary ships its own libpq, so no build deps are needed.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install deps first for better layer caching.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# App code.
COPY sptrader ./sptrader
COPY tests ./tests
COPY RESEARCH.md ./

EXPOSE 8000

# Default to the dashboard; override with `docker compose run app <cmd>`.
ENTRYPOINT ["python", "-m", "sptrader"]
CMD ["web", "--host", "0.0.0.0", "--port", "8000"]
