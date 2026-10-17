# Node.js v20.20.2 (npm 10.8.2), pinned by digest; python3 runs the verifier.
FROM node:20.20.2-bookworm-slim@sha256:2cf067cfed83d5ea958367df9f966191a942351a2df77d6f0193e162b5febfc0

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 --shell /bin/bash agent \
    && mkdir -p /work/candidate /logs/verifier \
    && chown agent:agent /work/candidate /logs/verifier

USER agent
WORKDIR /work/candidate
CMD ["sleep", "infinity"]
