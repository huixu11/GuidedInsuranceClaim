FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    HOST=0.0.0.0 \
    PORT=8000

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY insurance_claims ./insurance_claims

# /data holds the SQLite session store when one is configured (README, "Scaling and limits").
RUN useradd --create-home --uid 10001 appuser \
    && mkdir /data && chown appuser:appuser /data
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT','8000'))"

# The model API key is supplied at run time:  docker run -e ANTHROPIC_API_KEY=... -p 8000:8000 sop-agent
# Defaults: one worker, conversations in memory. Several workers, or several containers serving
# the same conversations, share a SQLite file on a volume:
#   docker run -v sop-sessions:/data -e SOP_SESSION_STORE=/data/sessions.db -e SOP_WORKERS=4 ...
CMD ["python", "-m", "insurance_claims"]
