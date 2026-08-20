FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY registers.py .
COPY heartbeat.py .
COPY plan.py .
COPY translator.py .
COPY translate.py .
COPY slots.py .
COPY state.py .
COPY slot_publisher.py .
COPY scheduler.py .
COPY entrypoint.sh .

# `corpus.py` and `test_mode1_negative.py` are deliberately NOT copied. The first reads
# archived household plan data and belongs to the test corpus; the second opens its own Modbus
# connection, which would steal the dispatcher's single connection if it were ever run inside
# this container by mistake (section 1). Neither has any business in the running image.

# The UID is PINNED. Docker seeds a named volume's ownership from the image the first time it
# creates it, and never again -- so `alphaess-dispatch-data` is already chowned to whatever
# `useradd` picked on the day it was first deployed. Leaving the UID implicit means any future
# edit that adds a user above this line shifts `dispatch` to 1001 and the rebuilt container
# cannot write slots.json to its own volume, with the plan file simply going stale.
RUN useradd --uid 1000 --create-home dispatch \
    && mkdir -p /data \
    && chown dispatch /data /app \
    && chmod +x entrypoint.sh

# The slots file, the heartbeat file and the audit log all live on the volume mounted here, so
# a rebuild does not discard the schedule the battery is currently following. Docker seeds a
# fresh named volume from this directory's ownership, which is why the chown above matters.
VOLUME ["/data"]
ENV DISPATCH_HEARTBEAT=/data/dispatch_heartbeat.json
WORKDIR /data

USER dispatch

# `--alive` reads only the heartbeat file. It must never open a Modbus connection -- see
# section 6.2 on why the obvious TCP port monitor would break the system.
#
# The start period covers a cold boot, which is the slowest one: the entrypoint runs a
# synchronous translation first -- an Influx query over a 48 h window, with no client timeout --
# and only then does the scheduler connect over Modbus and write its first heartbeat. 90 s was
# tight for that on a NAS that has just rebooted, which is exactly when this container starts.
# Being generous costs nothing here: an actually-dead dispatcher is caught by monitor #5, not
# by Docker, which does not restart on unhealthy anyway.
HEALTHCHECK --interval=120s --timeout=10s --start-period=180s \
    CMD python /app/scheduler.py --alive || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
