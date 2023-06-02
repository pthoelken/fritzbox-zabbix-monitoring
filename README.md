# fritzbox-zabbix-monitoring
First of all, a thanks to https://github.com/suptimal/zabbix-fritz.box-collector for this impress and start of a new way to check the AVM Fritz!Box devices.

## Normal Usage Setup
1. Copy the content from the docker-compose.yml file to your local docker-compose.yml file and fillout the environments
2. Download the Zabbix template file from https://github.com/pthoelken/fritzbox-zabbix-monitoring/blob/master/templates/template_fritz.box.xml and import it to your Zabbix Monitoring system
3. Create a simple host into zabbix with the same Hostname with you are declared in the docker-compose.yml from ```FRITZBOX_HOSTNAME```
4. Start your docker-compose file with ```docker-compose up -d``` - you can check the container with ```docker-compose logs``` into the same directory

## Build
1. ```git clone XX```
2. ```cd container```
3. ```docker build --no-cache -t fritzbox-zabbix-monitoring```
4. ```docker image ls```

## Known Errors
fritzbox-zabbix-monitoring-fritzbox-zabbix-monitoring-1  | zabbix_sender [8]: ERROR: [line 1] 'Hostname' required
fritzbox-zabbix-monitoring-fritzbox-zabbix-monitoring-1  | Sending failed.

## Roadmap
* Alerts
* ...
