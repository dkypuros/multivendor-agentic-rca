# One image for every component; each Deployment picks its entrypoint with `command`.
# Everything is Python stdlib except PyYAML (the MCP gateway's policy file).
FROM registry.access.redhat.com/ubi9/python-312:latest

USER 0
WORKDIR /app
COPY src/ /app/
RUN pip install --no-cache-dir pyyaml==6.0.2 && \
    chgrp -R 0 /app && chmod -R g=u /app

# OpenShift runs the container with an arbitrary UID in group 0.
USER 1001
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
ENTRYPOINT []
CMD ["python3", "services/ran/sandbox_controller.py"]
