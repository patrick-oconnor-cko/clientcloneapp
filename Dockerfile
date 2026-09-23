# CKO AI Sandbox (Arrakis) image — "Option 2" of the packaging guide: the app is pure
# Python standard library, so there is no requirements.txt for the platform to build from.
# Base image via the internal ECR pull-through cache (no public internet at build time).
FROM 891377407345.dkr.ecr.eu-west-1.amazonaws.com/cko-pull-through/docker-hub/library/python:3.12-slim

WORKDIR /srv
# Only the app. Never dev-creds.local.json (operator keys), clone-runs/ (real client data)
# or cat-api/ — see .dockerignore; the upload zip excludes them too.
COPY app/ app/

# Platform rules: listen on 3000; GET / must return 200 (it serves the page); log to stdout;
# durable state under /data. PUBLIC_URL (the hosted URL, for the Okta redirect) is set in
# the platform's environment, not here.
ENV HOST=0.0.0.0 \
    CLONE_PORT=3000 \
    CLONE_RUNS_DIR=/data/clone-runs \
    PYTHONUNBUFFERED=1
EXPOSE 3000
CMD ["python3", "app/server.py"]
