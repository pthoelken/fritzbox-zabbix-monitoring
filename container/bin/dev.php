<?php
$fritzbox_ip = $_ENV["FRITZBOX_IP"];
$hostname = $_ENV["FRITZBOX_HOSTNAME"];
$fritz_user = $_ENV["FRITZBOX_USER"];
$fritz_password = $_ENV["FRITZBOX_PASSWD"];

function firstLayerinLayer1Stream($completeLayer) {
    $firstLayer = explode(",", $completeLayer);
    return $firstLayer[0];
}

$client = new SoapClient(
    null,
    array(
        'location' => "http://".$fritzbox_ip.":49000/upnp/control/wancommonifconfig1",
        'uri' => "urn:dslforum-org:service:WANCommonInterfaceConfig:1",
        'noroot' => True,
        'login'      => $fritz_user,
	    'password'   => $fritz_password
    )
);
$commonLinkProperties = $client->GetCommonLinkProperties();
print($hostname . " layer1DownstreamCurrentUtilization " . $commonLinkProperties["NewX_AVM-DE_DownstreamCurrentUtilization"] . "\n");
# print($hostname . " layer1DownstreamCurrentUtilization " . firstLayerinLayer1Stream($commonLinkProperties["NewX_AVM-DE_DownstreamCurrentUtilization"]) . "\n");
print($hostname . " layer1DownstreamCurrentUtilization " . $commonLinkProperties["NewX_AVM-DE_UpstreamCurrentUtilization"] . "\n");
# print($hostname . " layer1UpstreamCurrentUtilization " . firstLayerinLayer1Stream($commonLinkProperties["NewX_AVM-DE_UpstreamCurrentUtilization"]) . "\n");

?>
