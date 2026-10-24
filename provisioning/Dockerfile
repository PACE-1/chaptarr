# chaptarr-provisioner: the provisioner plus the docker CLI and compose plugin.
# It drives the host's Docker daemon through the mounted socket, so it runs as root.
FROM python:3.12-slim

ARG DOCKER_VERSION=27.5.1
ARG COMPOSE_VERSION=2.32.4

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl; \
    arch="$(uname -m)"; \
    case "$arch" in x86_64) darch=x86_64; carch=x86_64 ;; aarch64) darch=aarch64; carch=aarch64 ;; \
      *) echo "unsupported arch $arch"; exit 1 ;; esac; \
    curl -fsSL "https://download.docker.com/linux/static/stable/${darch}/docker-${DOCKER_VERSION}.tgz" \
      | tar -xz -C /usr/local/bin --strip-components=1 docker/docker; \
    mkdir -p /usr/local/lib/docker/cli-plugins; \
    curl -fsSL -o /usr/local/lib/docker/cli-plugins/docker-compose \
      "https://github.com/docker/compose/releases/download/v${COMPOSE_VERSION}/docker-compose-linux-${carch}"; \
    chmod +x /usr/local/lib/docker/cli-plugins/docker-compose; \
    docker --version; docker compose version; \
    apt-get purge -y curl; apt-get autoremove -y; rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY chaptarr_provision.py /app/chaptarr_provision.py
COPY templates /app/templates
RUN ln -s /app/chaptarr_provision.py /usr/local/bin/chaptarr-provision \
    && chmod +x /app/chaptarr_provision.py \
    && python -m py_compile /app/chaptarr_provision.py

ENV CHAPTARR_PROVISION_CONFIG=/etc/chaptarr-provision/config.toml \
    PYTHONUNBUFFERED=1
EXPOSE 8790
ENTRYPOINT ["chaptarr-provision"]
CMD ["serve"]
