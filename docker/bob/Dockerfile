FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y \
        git \
        curl \
        ca-certificates \
        bash \
        && \
    rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    openhands-sdk==1.49.6 \
    openhands-tools

RUN mkdir -p /bob-home && \
    chown 1000:1000 /bob-home

ENV HOME=/bob-home

WORKDIR /workspace

CMD ["bash"]
