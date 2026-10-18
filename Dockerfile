# Demo image for the synthetic-data prototype. Not a production build.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Editable install keeps the source layout, which the app uses to find migrations/ and the
# recorded agent cassettes in evals/cassettes/.
COPY pyproject.toml ./
COPY src ./src
COPY migrations ./migrations
COPY evals ./evals
RUN pip install -e . \
    && useradd --create-home --uid 10001 carenote \
    && chown -R carenote /app

USER carenote
EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=6 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=2)"

CMD ["uvicorn", "carenote.main:app", "--host", "0.0.0.0", "--port", "8000"]
