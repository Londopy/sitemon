# The hub as a container, for the internet mirror. Build from the repo root:
#   docker compose -f deploy/hub/docker-compose.yml up -d --build
FROM python:3.12-slim

RUN useradd --system --create-home --home-dir /var/lib/sitemon sitemon
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY sitemon ./sitemon
RUN pip install --no-cache-dir . && rm -rf /src

USER sitemon
WORKDIR /var/lib/sitemon
EXPOSE 8470
CMD ["sitemon", "hub", "--config", "/etc/sitemon/hub.toml"]
