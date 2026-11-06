# syntax=docker/dockerfile:1
#
# trafficsim image: dashboard/API (`serve`), headless runs (`run`) and the mock collector.
#   docker build -t trafficsim .
#   docker run --rm --init -p 127.0.0.1:8000:8000 trafficsim            # dashboard
#   docker run --rm --init trafficsim run baseline_streaming --dry-run --max-wall 10
# See compose.yaml for the full local stack and docs/docker.md for details.

ARG PYTHON_VERSION=3.12

# --- build: resolve every dependency into wheels; no compilers reach the runtime image ---
FROM python:${PYTHON_VERSION}-slim AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY trafficsim ./trafficsim
COPY scenarios ./scenarios
COPY catalogs ./catalogs
COPY config/destinations.example.yaml ./config/
RUN pip wheel --wheel-dir /wheels .

# --- runtime ---
FROM python:${PYTHON_VERSION}-slim AS runtime

LABEL org.opencontainers.image.title="trafficsim" \
      org.opencontainers.image.description="Application traffic simulator that sends Segment events for streaming, e-commerce and gaming apps" \
      org.opencontainers.image.source="https://github.com/jaydeep-punjani/traffic-simulator" \
      org.opencontainers.image.documentation="https://github.com/jaydeep-punjani/traffic-simulator/tree/main/docs" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    TRAFFICSIM_HOME=/app \
    TRAFFICSIM_RUNS_DIR=/data/runs

RUN groupadd --system --gid 10001 trafficsim \
 && useradd --system --uid 10001 --gid trafficsim --home-dir /app --no-create-home --shell /usr/sbin/nologin trafficsim

RUN --mount=type=bind,from=build,source=/wheels,target=/wheels \
    pip install --no-index --find-links=/wheels trafficsim

# Editable copies: mount your own scenarios/, catalogs/ or config/destinations.yaml over these.
WORKDIR /app
COPY scenarios ./scenarios
COPY catalogs ./catalogs
COPY config/destinations.example.yaml ./config/

RUN mkdir -p /data/runs && chown trafficsim:trafficsim /data/runs
VOLUME ["/data/runs"]

USER trafficsim
EXPOSE 8000 8088

ENTRYPOINT ["trafficsim"]
CMD ["serve", "--bind", "0.0.0.0", "--port", "8000"]
