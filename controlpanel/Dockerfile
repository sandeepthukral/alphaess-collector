FROM python:3.12-slim

WORKDIR /app

# docker CLI + compose plugin -- every Docker-touching action in this image is a
# subprocess.run([...]) call to one of these, never the Python docker SDK. Keeps every
# action a single, auditable argv list instead of a library call that could grow scope
# later without anyone noticing in review.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian bookworm stable" \
        > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce-cli docker-compose-plugin \
    && apt-get purge -y --auto-remove curl gnupg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY docker_actions.py .
COPY backfill_actions.py .
COPY reliability_view.py .
COPY audit.py .
COPY templates/ templates/
COPY static/ static/

# Runs as root, deliberately: it needs to read/write the mounted Docker socket, and that
# socket is already root-equivalent access to the host regardless of the container's own
# UID -- matching a docker-group GID here would add a step without reducing the actual
# blast radius. See docs on why every action reaching it is still a hardcoded allowlist.
#
# gunicorn, not `flask run` / app.run(): the dev server is single-threaded, so one
# multi-minute backfill (docker_actions.py / backfill_actions.py time out at up to 1800s)
# would hang every other page -- including the dashboard's own status poll -- for as long
# as it ran. --timeout matches the longest allowed backfill so gunicorn does not recycle
# the worker mid-command.
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--threads", "4", \
     "--timeout", "1800", "app:app"]
