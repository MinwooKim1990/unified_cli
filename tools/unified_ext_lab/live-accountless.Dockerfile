FROM node:22-bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        python3 \
        python3-pip \
        python3-venv \
        unzip \
        xz-utils \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 lab \
    && useradd --uid 10001 --gid 10001 --create-home --shell /bin/sh lab \
    && install -d -o lab -g lab -m 0700 \
        /home/lab/.cache \
        /home/lab/.config \
        /home/lab/.local \
        /home/lab/.npm \
        /home/lab/provider-home \
        /home/lab/venv \
        /workspace \
    && printf '%s\n' '[core]' 'repositoryformatversion = 0' > /workspace/.gitconfig-marker \
    && chown -R lab:lab /workspace

USER 10001:10001
WORKDIR /workspace

ENV HOME=/home/lab \
    PATH=/home/lab/venv/bin:/home/lab/.local/bin:/home/lab/.npm/bin:/usr/local/bin:/usr/bin:/bin \
    NPM_CONFIG_PREFIX=/home/lab/.npm \
    NPM_CONFIG_CACHE=/home/lab/.cache/npm \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    XDG_CACHE_HOME=/home/lab/.cache \
    XDG_CONFIG_HOME=/home/lab/.config \
    XDG_DATA_HOME=/home/lab/.local/share

CMD ["sleep", "infinity"]
