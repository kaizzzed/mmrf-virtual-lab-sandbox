# ORION agent: the run controller on AgentCore Runtime (one arm64 container, RFA rev 25 A1, A4)
# and the `orion-agent` service of mode L (orion/compose.orion.yaml). Serves POST /invocations,
# GET /ping and /ws on port 8080, as a non-root user.
#
# Self-contained build, from the sandbox repo root (what AgentCore runs and Compose builds):
#   docker buildx build --platform linux/arm64 --load -t orion-agent:<tag> \
#     --build-context golden=orion/golden --build-context sandbox-config=config orion/agent
# The two named contexts bake the vocabulary (/config/cohort-fields.json) and the golden request
# bodies (/golden) into the image. Without them (`docker build orion/agent`) the image still
# builds, and /config and /golden must be mounted at run time, as the agent test command does.

# Named build contexts. Each defaults to an empty stage, so a plain build needs neither.
FROM scratch AS golden
FROM scratch AS sandbox-config

FROM python:3.12.16-slim-trixie AS deps
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
COPY requirements.txt /tmp/requirements.txt
RUN /opt/venv/bin/pip install -r /tmp/requirements.txt

FROM python:3.12.16-slim-trixie AS runtime
# A fixed numeric id, so Kubernetes runAsNonRoot and volume ownership can check it.
RUN groupadd --system --gid 10001 orion \
 && useradd --system --uid 10001 --gid orion --home-dir /app --no-create-home --shell /usr/sbin/nologin orion \
 && install -d -o orion -g orion -m 0755 /data/runs
COPY --from=deps /opt/venv /opt/venv
WORKDIR /app
COPY orion_agent ./orion_agent
RUN --mount=type=bind,from=golden,target=/mnt/golden \
    --mount=type=bind,from=sandbox-config,target=/mnt/config \
    set -eu; \
    if [ -n "$(ls -A /mnt/golden)" ]; then \
      test -f /mnt/golden/proteinpaint/termdb-DE-wilcoxon.json; \
      cp -R /mnt/golden /golden; chmod -R a+rX,go-w /golden; \
    fi; \
    if [ -n "$(ls -A /mnt/config)" ]; then \
      install -D -m 0644 /mnt/config/cohort-fields.json /config/cohort-fields.json; \
    fi
# AGUIApp binds 0.0.0.0 only when it detects a container; AgentCore's microVM may lack /.dockerenv.
# ORION_GOLDEN_DIR names where the golden bodies are baked; a missing directory means not baked.
ENV PATH=/opt/venv/bin:$PATH \
    ORION_RUN_DIR=/data/runs \
    ORION_GOLDEN_DIR=/golden \
    DOCKER_CONTAINER=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
EXPOSE 8080
# 200 from /ping means Healthy or HealthyBusy (a run in flight); both are healthy.
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/ping', timeout=2)"]
CMD ["python", "-m", "orion_agent.app"]
