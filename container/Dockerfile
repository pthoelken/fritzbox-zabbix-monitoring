FROM php:8.2-cli-alpine3.18

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

# HF for CVE-2023-44487⁠
RUN echo "http://dl-cdn.alpinelinux.org/alpine/v3.18/main" >> /etc/apk/repositories
RUN apk --update --no-cache add nghttp2=1.57.0-r0

# HF for CVE-2023-38545
RUN apk --update --no-cache add curl=8.4.0-r0

RUN docker-php-ext-install soap

RUN rm -rf /var/cache/apk/*

COPY bin/* /app/
RUN sed -i -e 's/\r$//' /app/send
RUN chmod +x -R /app

CMD [ "bash", "-c", "/app/send" ]