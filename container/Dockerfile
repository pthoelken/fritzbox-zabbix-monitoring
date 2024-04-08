FROM php:8.2-cli-alpine3.19 as buildimage

RUN apk --update --no-cache add \
    zabbix-agent \
    zabbix-utils \
    libxml2-dev 

RUN apk upgrade --no-cache
RUN rm -rf /var/cache/apk/*

FROM scratch
COPY --from=buildimage / /

WORKDIR /app

ENV ZABBIX_SERVER \
    FRITZBOX_HOSTNAME \
    FRITZBOX_IP \
    INTERVAL \
    FRITZBOX_USER \
    FRITZBOX_PASSWD \
    ZABBIX_SENDER_DEBUG

RUN docker-php-ext-install soap

COPY bin/* /app/
RUN chmod +x -R /app

CMD [ "ash", "-c", "/app/send" ]