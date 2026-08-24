FROM python:3.12

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        unzip \
        ca-certificates \
        docker.io \
    && rm -rf /var/lib/apt/lists/*

# Pulumi CLI
RUN curl -fsSL https://get.pulumi.com | sh \
    && cp -a /root/.pulumi/bin/. /usr/local/bin/ \
    && rm -rf /root/.pulumi

# AWS CLI v2
RUN curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscliv2.zip \
    && unzip -q /tmp/awscliv2.zip -d /tmp \
    && /tmp/aws/install \
    && rm -rf /tmp/aws /tmp/awscliv2.zip

# uv
RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
    && mv /root/.local/bin/uv /usr/local/bin/uv \
    && rm -rf /root/.local

WORKDIR /workspace