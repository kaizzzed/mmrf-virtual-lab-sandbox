FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY orion_agent ./orion_agent
# AGUIApp binds 0.0.0.0 only when it detects a container; AgentCore's microVM may lack /.dockerenv.
ENV ORION_RUN_DIR=/data/runs \
    DOCKER_CONTAINER=1 \
    PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["python", "-m", "orion_agent.app"]
