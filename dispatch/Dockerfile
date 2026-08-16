FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY registers.py .
COPY plan.py .
COPY translator.py .
COPY translate.py .
COPY slots.py .
COPY state.py .
COPY scheduler.py .
COPY entrypoint.sh .

# `corpus.py` and `test_mode1_negative.py` are deliberately NOT copied. The first reads
# archived household plan data and belongs to the test corpus; the second opens its own Modbus
# connection, which would steal the dispatcher's single connection if it were ever run inside
# this container by mistake (section 1). Neither has any business in the running image.

RUN useradd --create-home dispatch \
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
HEALTHCHECK --interval=120s --timeout=10s --start-period=90s \
    CMD python /app/scheduler.py --alive || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
