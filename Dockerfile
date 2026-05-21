FROM zabbix/zabbix-agent:alpine-7.4-latest AS zabbix-src

FROM python:3.14-alpine

LABEL maintainer="pthoelken"
LABEL description="FritzBox Zabbix Monitoring via TR-064, LUA, and Callmonitor"

RUN --mount=type=bind,from=zabbix-src,source=/,target=/zabbix-src \
    SENDER=$(find /zabbix-src/usr -name "zabbix_sender" -type f 2>/dev/null | head -1) && \
    [ -n "$SENDER" ] || (echo "ERROR: zabbix_sender not found in zabbix-src image" && exit 1) && \
    cp "$SENDER" /usr/local/bin/zabbix_sender && \
    chmod +x /usr/local/bin/zabbix_sender

RUN apk add --no-cache pcre2 && \
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
    CALLMONITOR_PORT=1012 \
    DEVICE_LOG_ITEM_KEY=fritzbox.device.log \
    DEVICE_LOG_HISTORY_SIZE=5000

CMD ["python3", "-u", "/opt/fritzbox_monitor.py"]
