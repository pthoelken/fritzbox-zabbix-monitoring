FROM php:8.1.21RC1-cli-alpine3.16

WORKDIR /app

ENV ZABBIX_SERVER \
    FRITZBOX_HOSTNAME \
    FRITZBOX_IP \
    INTERVAL \
    FRITZBOX_USER \
    FRITZBOX_PASSWD

RUN apk --update --no-cache add \
    zabbix-agent \
    zabbix-utils \
    libxml2-dev \
    bash

RUN docker-php-ext-install soap

RUN rm -rf /var/cache/apk/*

COPY bin/* /app/
RUN chmod +x -R /app

CMD [ "bash", "-c", "/app/send" ]