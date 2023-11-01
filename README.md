# Requirements and successfully tested
- Zabbix Server Version: 7
- Zabbix Sender Version: 6.4.7

This template is in development with Zabbix Server version 7. Zabbix Server Version 6 with the templates from here may not working properly.

## Setup
1. Copy the content from the docker-compose.yml file to your local docker-compose.yml file and fillout the environment variables with yours.
2. Download the Zabbix template file from [template_fritz.box.xml](https://github.com/pthoelken/fritzbox-zabbix-monitoring/blob/master/templates) and import it to your Zabbix Monitoring system (if you update, delete the old tempalte first)
3. Create a host in zabbix with the same hostname from docker-compose.yml (```FRITZBOX_HOSTNAME```)
4. Start your docker-compose file with ```docker-compose up -d```
5. You can check the container with ```docker-compose logs``` into the same directory

## For builders and developers only
1. ```git clone XX```
2. ```cd container```
3. ```docker build --no-cache -t fritzbox-zabbix-monitoring```

For more information checkout the build file in this repo.

## Known Errors
### 'Hostname' required
```
fritzbox-zabbix-monitoring-fritzbox-zabbix-monitoring-1  | zabbix_sender [8]: ERROR: [line 1] 'Hostname' required
fritzbox-zabbix-monitoring-fritzbox-zabbix-monitoring-1  | Sending failed.
```
- This error can be solved by removing the " chars in your environment section from docker-compose.yml

### Values are 0 in my Zabbix Server Host
- This error may comes up when you are using your AVM Device for a internal switch / access point instead for an normal internet router. I'm working on it to investigate this problem or create a fix. 

## DockerHub
https://hub.docker.com/r/pthoelken/fritzbox-zabbix-monitoring
