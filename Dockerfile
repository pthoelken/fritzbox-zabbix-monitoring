FROM python:3.12-slim

LABEL maintainer="pthoelken"
LABEL description="FritzBox Zabbix Monitoring via TR-064, LUA, and Callmonitor"

RUN apt-get update && \
    apt-get install -y --no-install-recommends zabbix-sender curl && \
    rm -rf /var/lib/apt/lists/* && \
    pip install --no-cache-dir "fritzconnection>=1.13.0" "requests>=2.31.0"

COPY src/fritzbox_monitor.py /opt/fritzbox_monitor.py
RUN chmod +x /opt/fritzbox_monitor.py

HEALTHCHECK --interval=60s --timeout=10s --retries=3 \
    CMD pgrep -f fritzbox_monitor.py || exit 1

ENV FRITZBOX_IP=192.168.178.1 \
    FRITZBOX_PORT=49000 \
    FRITZBOX_USE_TLS=false \
    FRITZBOX_HOSTNAME=fritz.box \
    ZABBIX_SERVER="" \
    ZABBIX_SERVER_PORT=10051 \
    INTERVAL=60s \
    ZABBIX_SENDER_DEBUG=false \
    ENABLE_LUA=true \
    ENABLE_CALLMONITOR=false \
    CALLMONITOR_PORT=1012

CMD ["python3", "-u", "/opt/fritzbox_monitor.py"]
