FROM php:8.2-cli-alpine3.19 as builder

WORKDIR /app

ENV ZABBIX_SERVER \
    FRITZBOX_HOSTNAME \
    FRITZBOX_IP \
    INTERVAL \
    FRITZBOX_USER \
    FRITZBOX_PASSWD \
    ZABBIX_SENDER_DEBUG

RUN apk upgrade --no-cache && apk --update --no-cache add \
    zabbix-agent \
    zabbix-utils \
    libxml2-dev 

RUN docker-php-ext-install soap

RUN rm -rf /var/cache/apk/*

COPY bin/* /app/
RUN chmod +x -R /app

FROM scratch
COPY --from=builder /app/ /app/
CMD [ "ash", "-c", "/app/send" ]