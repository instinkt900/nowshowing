# Stdlib-only app: no pip install. tzdata lets zoneinfo resolve TZ.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 PORT=8077

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY app.py style.css ./

USER nobody
EXPOSE 8077
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8077/healthz').status==200 else 1)"]
CMD ["python", "app.py"]
