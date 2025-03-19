FROM php:8.4.5-alpine3.21

WORKDIR /app

ENV ZABBIX_SERVER \
    FRITZBOX_HOSTNAME \
    FRITZBOX_IP \
    INTERVAL \
    FRITZBOX_USER \
    FRITZBOX_PASSWD \
    ZABBIX_SENDER_DEBUG

RUN apk upgrade --no-cache && apk --update --no-cache add zabbix-utils

RUN apk add --no-cache --virtual .build-deps libxml2-dev && \
    docker-php-ext-install soap && \
    apk del .build-deps && \
    docker-php-source delete

RUN rm -rf /var/cache/apk/* /tmp/* /var/tmp/* \
           /usr/share/man/* /usr/share/doc/* /usr/share/info/* /usr/share/locale/* \
           /usr/include/* /usr/lib/debug /usr/lib/pkgconfig \
           /usr/lib/libpython* /usr/lib/perl5 /usr/src/* \
           /root/.cache /root/.local /home/*

COPY bin/* /app/
RUN chmod +x -R /app

CMD [ "ash", "-c", "/app/send" ]