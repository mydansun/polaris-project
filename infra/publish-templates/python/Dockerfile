FROM python:3.12-slim AS runner
WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Install dependencies. Supports both requirements.txt and pyproject.toml
# (pip-based). For uv / poetry users: override the Dockerfile after scaffold.
COPY requirements.txt* pyproject.toml* ./
RUN set -eux; \
    if [ -f requirements.txt ]; then pip install -r requirements.txt; \
    elif [ -f pyproject.toml ]; then pip install .; \
    else echo "no requirements.txt or pyproject.toml"; exit 1; \
    fi

COPY . .
EXPOSE __POLARIS_PORT__
CMD __POLARIS_START_CMD_JSON__
