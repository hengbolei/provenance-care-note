# Hugging Face Space for the public demo: builds the app from the GitHub repository and runs it
# as a shared public demo on the in-memory store, with the agent replaying recorded drafts.
# No API key, database or secret is needed. See deploy/huggingface/DEPLOY.md.
FROM python:3.12-slim

# The GitHub ref to build. A Factory rebuild of the Space pulls it again.
ARG REPO=https://github.com/hengbolei/provenance-care-note.git
ARG REF=main

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    CARENOTE_ENVIRONMENT=development \
    CARENOTE_PUBLIC_DEMO=1 \
    CARENOTE_DEMO_RESET_MINUTES=30 \
    CARENOTE_AGENT_GATEWAY=recorded

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# Spaces run the container as UID 1000.
RUN useradd --create-home --uid 1000 user
WORKDIR /app
RUN git clone --depth 1 --branch "$REF" "$REPO" . \
    && pip install -e . \
    && chown -R user /app

USER user
EXPOSE 8000
CMD ["uvicorn", "carenote.main:app", "--host", "0.0.0.0", "--port", "8000"]
