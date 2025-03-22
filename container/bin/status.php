<?php
$fritzbox_ip = $_ENV["FRITZBOX_IP"];
$hostname = $_ENV["FRITZBOX_HOSTNAME"];
$fritz_user = $_ENV["FRITZBOX_USER"];
$fritz_password = $_ENV["FRITZBOX_PASSWD"];

# ---------------------------------------------------------------------------------------------------------------------------------
# - Traffic Stats from WAN
# ---------------------------------------------------------------------------------------------------------------------------------
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
// print($hostname . " totalBytesSent " . $addonInfos["NewTotalBytesSent"] . "\n");
// print($hostname . " totalBytesReceived " . $addonInfos["NewTotalBytesReceived"] . "\n");
print($hostname . " totalBytesSent " . $addonInfos["NewX_AVM_DE_TotalBytesSent64"] . "\n");
print($hostname . " totalBytesReceived " . $addonInfos["NewX_AVM_DE_TotalBytesReceived64"] . "\n");
print($hostname . " layer1UpstreamMaxBitRate " . $commonLinkProperties["NewLayer1UpstreamMaxBitRate"] . "\n");
print($hostname . " layer1DownstreamMaxBitRate " . $commonLinkProperties["NewLayer1DownstreamMaxBitRate"] . "\n");
print($hostname . " physicalLinkStatus " . $commonLinkProperties["NewPhysicalLinkStatus"] . "\n");

# ---------------------------------------------------------------------------------------------------------------------------------
# - Routers Informations: Internal IP Address List
# ---------------------------------------------------------------------------------------------------------------------------------
$client = new SoapClient(
    null,
    array(
        'location' => "http://".$fritzbox_ip.":49000/upnp/control/lanhostconfigmgm",
        'uri' => "urn:dslforum-org:service:LANHostConfigManagement:1",
        'noroot' => True,
        'login'      => $fritz_user,
        'password'   => $fritz_password
    )
);
$RoutersIPList = $client->GetInfo();
print($hostname . " ipAddressFromRouter " . $RoutersIPList["NewIPRouters"] . "\n");

# ---------------------------------------------------------------------------------------------------------------------------------
# - WAN Status
# ---------------------------------------------------------------------------------------------------------------------------------
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
$externalIPV6Address = $client->X_AVM_DE_GetExternalIPv6Address();
$externalIPV6Prefix = $client->X_AVM_DE_GetIPv6Prefix();

function extractFirstValue($value) {
    if (is_array($value)) {
        $value = implode(", ", $value);
    }
    return trim(explode(",", $value)[0]);
}

$externalIPV6AddressOnly = extractFirstValue($externalIPV6Address);
$externalIPV6PrefixOnly = extractFirstValue($externalIPV6Prefix);

$externalIPAddress = !empty($externalIPAddress) ? $externalIPAddress : "not configured (check externalIPAddress value)";
$externalIPV6AddressOnly = !empty($externalIPV6AddressOnly) ? $externalIPV6AddressOnly : "not configured (check externalIPV6Address value)";
$externalIPV6PrefixOnly = !empty($externalIPV6PrefixOnly) ? $externalIPV6PrefixOnly : "not configured (check externalIPV6Prefix value)";

print($hostname . " connectionStatus " . $status["NewConnectionStatus"] . "\n");
print($hostname . " uptime " . $status["NewUptime"] . "\n");
print($hostname . " externalIPAddress " . $externalIPAddress . "\n");
print($hostname . " externalIPV6Address " . $externalIPV6AddressOnly . "\n");
print($hostname . " externalIPV6Prefix " . $externalIPV6PrefixOnly . "\n");

!$fritz_password && exit(0);

# ---------------------------------------------------------------------------------------------------------------------------------
# - Router Informations: Software Version
# ---------------------------------------------------------------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------------------------------------------------------------
# - Informations about connected WiFi Network Devices
# ---------------------------------------------------------------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------------------------------------------------------------
# - Discoverd WiFi Devices
# ---------------------------------------------------------------------------------------------------------------------------------
print($hostname . " associatedDeviceDiscovery " . json_encode(["data" => $macs]) . "\n");
print($wifi_stats);

?>
