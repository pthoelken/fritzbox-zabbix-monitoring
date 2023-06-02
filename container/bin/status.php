<?php
$fritzbox_ip = $_ENV["FRITZBOX_IP"];
$hostname = $_ENV["FRITZBOX_HOSTNAME"];
$fritz_user = $_ENV["FRITZBOX_USER"];
$fritz_password = $_ENV["FRITZBOX_PASSWD"];

## traffic stats wam
$client = new SoapClient(
    null,
    array(
        'location'   => "http://".$fritzbox_ip.":49000/igdupnp/control/WANCommonIFC1",
        'uri'        => "urn:schemas-upnp-org:service:WANCommonInterfaceConfig:1",
        'noroot'     => True
    )
);
$addonInfos = $client->GetAddonInfos();  
$commonLinkProperties = $client->GetCommonLinkProperties();
print($hostname . " totalBytesSent " . $addonInfos["NewTotalBytesSent"] . "\n");
print($hostname . " totalBytesReceived " . $addonInfos["NewTotalBytesReceived"] . "\n");
print($hostname . " layer1UpstreamMaxBitRate " . $commonLinkProperties["NewLayer1UpstreamMaxBitRate"] . "\n");
print($hostname . " layer1DownstreamMaxBitRate " . $commonLinkProperties["NewLayer1DownstreamMaxBitRate"] . "\n");
print($hostname . " physicalLinkStatus " . $commonLinkProperties["NewPhysicalLinkStatus"] . "\n");

## wan status
$client = new SoapClient(
    null,
    array(
        'location'   => "http://".$fritzbox_ip.":49000/igdupnp/control/WANIPConn1",
        'uri'        => "urn:schemas-upnp-org:service:WANIPConnection:1",
        'noroot'     => True
    )
);
$status = $client->GetStatusInfo();
$externalIPAddress = $client->GetExternalIPAddress();

print($hostname . " connectionStatus " . $status["NewConnectionStatus"] . "\n");
print($hostname . " uptime " . $status["NewUptime"] . "\n");
print($hostname . " externalIPAddress " . $externalIPAddress . "\n");

!$fritz_password  && exit(0);
## software version
$client = new SoapClient(
    null,
    array(
        'location'   => "http://".$fritzbox_ip.":49000/upnp/control/deviceinfo",
        'uri'        => "urn:dslforum-org:service:DeviceInfo:1",
        'noroot'     => True,
	'login'      => $fritz_user,
	'password'   => $fritz_password
    )
);
$info = $client->GetInfo();
print($hostname . " softwareVersion " . $info['NewSoftwareVersion'] . "\n");

## get some informations about connected wifi devices
$client = new SoapClient(
    null,
    array(
        'location'   => "http://".$fritzbox_ip.":49000/upnp/control/wlanconfig1",
        'uri'        => "urn:dslforum-org:service:WLANConfiguration:1",
        'noroot'     => True,
	'login'      => $fritz_user,
	'password'   => $fritz_password
    )
);

$wifi_device_count = $client->GetTotalAssociations();
$wifi_stats = "";
$macs = array();
for ($i=0;$i<$wifi_device_count;$i++){
    $d = $client->GetGenericAssociatedDeviceInfo(new SoapParam($i,'NewAssociatedDeviceIndex'));
    $mac = $d['NewAssociatedDeviceMACAddress'];

    $wifi_stats .= "$hostname associatedDevice[$mac,authState] " . $d['NewAssociatedDeviceAuthState'] . "\n";
    $wifi_stats .= "$hostname associatedDevice[$mac,signalStrength] " . $d['NewX_AVM-DE_SignalStrength'] . "\n";
    $wifi_stats .= "$hostname associatedDevice[$mac,speed] " . $d['NewX_AVM-DE_Speed'] . "\n";
    $wifi_stats .= "$hostname associatedDevice[$mac,ipAddress] " . $d['NewAssociatedDeviceIPAddress'] . "\n";

    array_push($macs, ["{#MAC}" => $mac]);
}
#send discover wifi devices
print($hostname . " associatedDeviceDiscovery " . json_encode(["data" => $macs]) . "\n");
#send wifi devices stats
print($wifi_stats);
?>
