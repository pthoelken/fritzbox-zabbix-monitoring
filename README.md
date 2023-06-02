# fritzbox-zabbix-monitoring
First of all, a thanks to https://github.com/suptimal/zabbix-fritz.box-collector for this impress and start of a new way to check the AVM Fritz!Box devices. In this kind of fork I create a image and docker-compose file with env variables.

## Normal Usage Setup
1. Copy the content from the docker-compose.yml file to your local docker-compose.yml file and fillout the environments
2. Download the Zabbix template file from [template_fritz.box.xml](https://github.com/pthoelken/fritzbox-zabbix-monitoring/blob/master/templates/template_fritz.box.xml) and import it to your Zabbix Monitoring system
3. Create a host in zabbix with the same hostname from docker-compose.yml (```FRITZBOX_HOSTNAME```)
4. Start your docker-compose file with ```docker-compose up -d```
5. You can check the container with ```docker-compose logs``` into the same directory

## Build
1. ```git clone XX```
2. ```cd container```
3. ```docker build --no-cache -t fritzbox-zabbix-monitoring```
4. ```docker image ls```

## Known Errors
fritzbox-zabbix-monitoring-fritzbox-zabbix-monitoring-1  | zabbix_sender [8]: ERROR: [line 1] 'Hostname' required
fritzbox-zabbix-monitoring-fritzbox-zabbix-monitoring-1  | Sending failed.

## Roadmap
[] Alerts
[] ...
