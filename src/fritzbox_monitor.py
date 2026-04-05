#!/usr/bin/env python3
"""
FritzBox Zabbix Monitoring v2.2
Collects metrics from AVM Fritz!Box via three interfaces:
  1. TR-064 (SOAP/UPnP) - Standard router metrics
  2. LUA Web Interface (data.lua/query.lua) - CPU, RAM, temperature, traffic stats
  3. Callmonitor (TCP 1012) - Phone call tracking

Sends metrics and device log events to Zabbix via zabbix_sender.
"""

import os
import re
import sys
import json
import time
import socket
import hashlib
import logging
import subprocess
import tempfile
import threading
import xml.etree.ElementTree as ET
from collections import deque
from datetime import datetime

try:
    import requests
except ImportError:
    print("ERROR: requests not installed. Run: pip install requests")
    sys.exit(1)

try:
    from fritzconnection import FritzConnection
    from fritzconnection.lib.fritzhosts import FritzHosts
except ImportError:
    print("ERROR: fritzconnection not installed. Run: pip install fritzconnection")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
FRITZBOX_IP = os.environ.get("FRITZBOX_IP", "192.168.178.1")
FRITZBOX_USER = os.environ.get("FRITZBOX_USER", "")
FRITZBOX_PASSWD = os.environ.get("FRITZBOX_PASSWD", "")
FRITZBOX_PORT = int(os.environ.get("FRITZBOX_PORT", "49000"))
FRITZBOX_USE_TLS = os.environ.get("FRITZBOX_USE_TLS", "false").lower() == "true"
FRITZBOX_HOSTNAME = os.environ.get("FRITZBOX_HOSTNAME", "fritz.box")

ZABBIX_SERVER = os.environ.get("ZABBIX_SERVER", "")
ZABBIX_SERVER_PORT = os.environ.get("ZABBIX_SERVER_PORT", "10051")
TLS_PSK_IDENTITY = os.environ.get("TLS_PSK_IDENTITY", "")
TLS_PSK_FILE = os.environ.get("TLS_PSK_FILE", "")

INTERVAL = os.environ.get("INTERVAL", "60s")
DEBUG = os.environ.get("ZABBIX_SENDER_DEBUG", "false").lower() in ("true", "1", "yes")

# Feature toggles
ENABLE_LUA = os.environ.get("ENABLE_LUA", "true").lower() in ("true", "1", "yes")
ENABLE_CALLMONITOR = os.environ.get("ENABLE_CALLMONITOR", "false").lower() in (
    "true",
    "1",
    "yes",
)
CALLMONITOR_PORT = int(os.environ.get("CALLMONITOR_PORT", "1012"))
DEVICE_LOG_ITEM_KEY = os.environ.get("DEVICE_LOG_ITEM_KEY", "fritzbox.device.log")
try:
    DEVICE_LOG_HISTORY_SIZE = max(
        100, int(os.environ.get("DEVICE_LOG_HISTORY_SIZE", "5000"))
    )
except ValueError:
    DEVICE_LOG_HISTORY_SIZE = 5000

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
log_level = logging.DEBUG if DEBUG else logging.INFO
logging.basicConfig(
    level=log_level,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("fritzbox-monitor")

# Suppress noisy urllib3 warnings for self-signed certs
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# Detect UPnP-disabled errors from fritzconnection and add a helpful hint
class _UpnpHintFilter(logging.Filter):
    def filter(self, record):
        if "igddesc.xml" in record.getMessage():
            logging.getLogger("fritzbox-monitor").warning(
                "TR-064: Cannot retrieve igddesc.xml — UPnP may be disabled on the Fritz!Box. "
                "Enable 'Allow access for applications' (Zugriff für Anwendungen erlauben) "
                "under Home Network > Network > General."
            )
        return True


logging.getLogger("fritzconnection").addFilter(_UpnpHintFilter())


def parse_interval(s):
    s = s.strip().lower()
    if s.endswith("s"):
        return int(s[:-1])
    if s.endswith("m"):
        return int(s[:-1]) * 60
    if s.endswith("h"):
        return int(s[:-1]) * 3600
    return int(s)


# State for delta-based current BPS calculation (fallback for LTE devices)
_wan_bps_prev = {}  # {"bytes_recv": int, "bytes_sent": int, "timestamp": float}

# WLAN signal strength from TR-064, populated in collect_wlan_info, used in collect_lua_netdev
_wlan_signal_by_mac = {}  # {mac_lower: signal_strength_percent}

# Device log deduplication state (event stream for Zabbix log item)
_device_log_bootstrapped = False
_device_log_seen = set()
_device_log_seen_order = deque()


# ===================================================================
# TR-064 Interface
# ===================================================================
def safe_call(fc, service, action, arguments=None):
    if service not in fc.services:
        log.debug("Service %s not supported", service)
        return None
    if action not in fc.services[service].actions:
        log.debug("Action %s in service %s not supported", action, service)
        return None
    try:
        return (
            fc.call_action(service, action, arguments=arguments)
            if arguments
            else fc.call_action(service, action)
        )
    except Exception as e:
        log.debug("TR-064 %s/%s failed: %s", service, action, e)
        return None


def _normalize_sender_value(v):
    return str(v).replace("\n", " | ").replace("\r", "").replace("\t", " ").strip()


def _quote_sender_field(v):
    return (
        '"'
        + str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        + '"'
    )


def _xml_localname(tag):
    return tag.rsplit("}", 1)[-1].lower() if "}" in tag else str(tag).lower()


def _parse_fritz_log_clock(date_text="", time_text="", timestamp_text=""):
    if date_text and time_text:
        dt_text = f"{date_text} {time_text}"
        for fmt in ("%d.%m.%y %H:%M:%S", "%d.%m.%Y %H:%M:%S"):
            try:
                return int(datetime.strptime(dt_text, fmt).timestamp())
            except ValueError:
                pass
    if timestamp_text:
        ts = str(timestamp_text).strip()
        if ts.isdigit():
            return int(ts)
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S%z"):
            try:
                return int(datetime.strptime(ts, fmt).timestamp())
            except ValueError:
                pass
    return int(time.time())


def _register_device_log_fingerprint(fingerprint):
    if fingerprint in _device_log_seen:
        return
    _device_log_seen.add(fingerprint)
    _device_log_seen_order.append(fingerprint)
    while len(_device_log_seen_order) > DEVICE_LOG_HISTORY_SIZE:
        old = _device_log_seen_order.popleft()
        _device_log_seen.discard(old)


def _extract_new_device_log_events(events):
    global _device_log_bootstrapped
    new_events = []
    if not _device_log_bootstrapped:
        for event in events:
            _register_device_log_fingerprint(event["fingerprint"])
        _device_log_bootstrapped = True
        if events:
            log.info(
                "Device log: baseline initialized with %d existing entries", len(events)
            )
        return new_events

    for event in events:
        fingerprint = event["fingerprint"]
        if fingerprint in _device_log_seen:
            continue
        _register_device_log_fingerprint(fingerprint)
        new_events.append(event)
    return new_events


def _parse_device_log_lines(raw_text):
    events = []
    for raw_line in str(raw_text).splitlines():
        line = _normalize_sender_value(raw_line)
        if not line:
            continue
        m = re.match(r"^(\d{2}\.\d{2}\.\d{2,4})\s+(\d{2}:\d{2}:\d{2})\s+(.*)$", line)
        if m:
            date_text, time_text, message = m.group(1), m.group(2), m.group(3).strip()
            clock = _parse_fritz_log_clock(date_text, time_text)
            fingerprint_source = f"text|{date_text}|{time_text}|{message}"
        else:
            message = line
            clock = int(time.time())
            fingerprint_source = f"text|raw|{message}"
        if not message:
            continue
        events.append(
            {
                "clock": clock,
                "message": message,
                "fingerprint": hashlib.sha1(
                    fingerprint_source.encode("utf-8")
                ).hexdigest(),
            }
        )
    events.sort(key=lambda x: (x["clock"], x["fingerprint"]))
    return events


def _parse_device_log_xml(xml_text):
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    events = {}
    for node in root.iter():
        children = list(node)
        if not children:
            continue
        fields = {}
        for child in children:
            key = _xml_localname(child.tag)
            value = _normalize_sender_value(child.text or "")
            if value:
                fields[key] = value

        message = fields.get("msg") or fields.get("message") or fields.get("text")
        if not message:
            continue
        clock = _parse_fritz_log_clock(
            fields.get("date", ""), fields.get("time", ""), fields.get("timestamp", "")
        )
        event_id = fields.get("id", "")
        group = fields.get("group", "")
        fingerprint_source = f"xml|{event_id}|{fields.get('date', '')}|{fields.get('time', '')}|{group}|{message}"
        fingerprint = hashlib.sha1(fingerprint_source.encode("utf-8")).hexdigest()
        events[fingerprint] = {
            "clock": clock,
            "message": message,
            "fingerprint": fingerprint,
            "id": event_id,
        }

    parsed = list(events.values())
    parsed.sort(key=lambda x: (x["clock"], x.get("id", ""), x["fingerprint"]))
    return parsed


def collect_device_log_events(fc):
    # FritzOS >= 8 typically provides a richer log stream via GetDeviceLogPath.
    r = safe_call(fc, "DeviceInfo1", "X_AVM-DE_GetDeviceLogPath")
    if r:
        device_log_path = str(r.get("NewDeviceLogPath", "")).strip()
        if device_log_path:
            if device_log_path.startswith(("http://", "https://")):
                url = device_log_path
            else:
                if not device_log_path.startswith("/"):
                    device_log_path = "/" + device_log_path
                url = f"{fc.address}:{fc.port}{device_log_path}"
            try:
                resp = fc.session.get(url, timeout=10)
                if resp.status_code == 200:
                    events = _parse_device_log_xml(resp.text)
                    if not events:
                        events = _parse_device_log_lines(resp.text)
                    log.debug(
                        "Device log via X_AVM-DE_GetDeviceLogPath: %d entries",
                        len(events),
                    )
                    return events
                log.debug("Device log path request returned HTTP %d", resp.status_code)
            except Exception as e:
                log.debug("Device log path request failed: %s", e)

    # Fallback for older firmware or if the log-path request fails.
    r = safe_call(fc, "DeviceInfo1", "GetDeviceLog")
    if r:
        log_text = r.get("NewDeviceLog", "")
        events = _parse_device_log_lines(log_text)
        log.debug("Device log via GetDeviceLog: %d entries", len(events))
        return events
    return []


def collect_device_info(fc):
    m = {}
    r = safe_call(fc, "DeviceInfo1", "GetInfo")
    if r:
        m["fritzbox.device.model"] = r.get("NewModelName", "")
        m["fritzbox.device.firmware"] = r.get("NewSoftwareVersion", "")
        m["fritzbox.device.serial"] = r.get("NewSerialNumber", "")
        m["fritzbox.device.uptime"] = r.get("NewUpTime", 0)
    r = safe_call(fc, "DeviceInfo1", "GetSecurityPort")
    if r:
        m["fritzbox.device.security_port"] = r.get("NewSecurityPort", 0)
    all_log_events = collect_device_log_events(fc)
    new_log_events = _extract_new_device_log_events(all_log_events)
    if new_log_events:
        log.info("Device log: %d new event(s)", len(new_log_events))
    return m, new_log_events


def collect_wan_info(fc):
    global _wan_bps_prev
    m = {}
    r = safe_call(fc, "WANCommonInterfaceConfig1", "GetCommonLinkProperties")
    if r:
        m["fritzbox.wan.access_type"] = r.get("NewWANAccessType", "")
        m["fritzbox.wan.layer1_upstream_max"] = r.get("NewLayer1UpstreamMaxBitRate", 0)
        m["fritzbox.wan.layer1_downstream_max"] = r.get(
            "NewLayer1DownstreamMaxBitRate", 0
        )
        m["fritzbox.wan.physical_link_status"] = r.get("NewPhysicalLinkStatus", "")

    r = safe_call(fc, "WANCommonInterfaceConfig1", "GetTotalBytesReceived")
    if r:
        m["fritzbox.wan.bytes_received"] = r.get("NewTotalBytesReceived", 0)
    r = safe_call(fc, "WANCommonInterfaceConfig1", "GetTotalBytesSent")
    if r:
        m["fritzbox.wan.bytes_sent"] = r.get("NewTotalBytesSent", 0)
    r = safe_call(fc, "WANCommonInterfaceConfig1", "GetTotalPacketsReceived")
    if r:
        m["fritzbox.wan.packets_received"] = r.get("NewTotalPacketsReceived", 0)
    r = safe_call(fc, "WANCommonInterfaceConfig1", "GetTotalPacketsSent")
    if r:
        m["fritzbox.wan.packets_sent"] = r.get("NewTotalPacketsSent", 0)

    r = safe_call(
        fc,
        "WANCommonInterfaceConfig1",
        "X_AVM-DE_GetOnlineMonitor",
        arguments={"NewSyncGroupIndex": 0},
    )
    if r:
        try:
            ds = str(r.get("Newds_current_bps", "0")).split(",")[0]
            us = str(r.get("Newus_current_bps", "0")).split(",")[0]
            m["fritzbox.wan.downstream_current_bps"] = int(ds)
            m["fritzbox.wan.upstream_current_bps"] = int(us)
            m["fritzbox.wan.downstream_max_bps"] = r.get("Newmax_ds", 0)
            m["fritzbox.wan.upstream_max_bps"] = r.get("Newmax_us", 0)
        except (ValueError, TypeError):
            pass

    # Fallback for LTE devices: X_AVM-DE_GetOnlineMonitor always returns 0 for
    # current_bps on LTE interfaces. Calculate from byte counter deltas instead.
    now = time.time()
    bytes_recv = m.get("fritzbox.wan.bytes_received", 0)
    bytes_sent = m.get("fritzbox.wan.bytes_sent", 0)
    if _wan_bps_prev and bytes_recv and bytes_sent:
        elapsed = now - _wan_bps_prev.get("timestamp", now)
        if elapsed > 0:
            if m.get("fritzbox.wan.downstream_current_bps", 0) == 0:
                delta_recv = max(
                    0, bytes_recv - _wan_bps_prev.get("bytes_recv", bytes_recv)
                )
                bps = int(delta_recv * 8 / elapsed)
                m["fritzbox.wan.downstream_current_bps"] = bps
                log.debug(
                    "WAN: downstream_current_bps from delta: %d bps (%.1fs elapsed)",
                    bps,
                    elapsed,
                )
            if m.get("fritzbox.wan.upstream_current_bps", 0) == 0:
                delta_sent = max(
                    0, bytes_sent - _wan_bps_prev.get("bytes_sent", bytes_sent)
                )
                bps = int(delta_sent * 8 / elapsed)
                m["fritzbox.wan.upstream_current_bps"] = bps
                log.debug(
                    "WAN: upstream_current_bps from delta: %d bps (%.1fs elapsed)",
                    bps,
                    elapsed,
                )
    _wan_bps_prev = {
        "bytes_recv": bytes_recv,
        "bytes_sent": bytes_sent,
        "timestamp": now,
    }

    wan_connected = False
    for svc in ["WANPPPConnection1", "WANIPConnection1"]:
        r = safe_call(fc, svc, "GetInfo")
        if r:
            status = r.get("NewConnectionStatus", "")
            m["fritzbox.wan.connection_status"] = status
            m["fritzbox.wan.connection_type"] = r.get("NewConnectionType", "")
            m["fritzbox.wan.connection_uptime"] = r.get("NewUptime", 0)
            m["fritzbox.wan.last_error"] = r.get("NewLastConnectionError", "")
            wan_connected = status == "Connected"
            break

    for svc in ["WANPPPConnection1", "WANIPConnection1"]:
        r = safe_call(fc, svc, "GetExternalIPAddress")
        if r:
            m["fritzbox.wan.external_ip"] = r.get("NewExternalIPAddress", "")
            break

    for svc in ["WANIPConnection1", "WANPPPConnection1"]:
        r = safe_call(fc, svc, "X_AVM_DE_GetExternalIPv6Address")
        if r:
            ipv6 = r.get("NewExternalIPv6Address", "")
            prefix = r.get("NewPrefixLength", "")
            if ipv6:
                m["fritzbox.wan.external_ipv6"] = ipv6
                m["fritzbox.wan.external_ipv6_prefix"] = prefix
                break

    for svc in ["WANPPPConnection1", "WANIPConnection1"]:
        r = safe_call(fc, svc, "GetDNSServers") or safe_call(fc, svc, "X_GetDNSServers")
        if r:
            m["fritzbox.wan.dns_servers"] = r.get(
                "NewDNSServers", r.get("NewIPv4DNSServer1", "")
            )
            break

    m["fritzbox.wan.is_connected"] = 1 if wan_connected else 0
    return m


def collect_dsl_info(fc):
    m = {}
    r = safe_call(fc, "WANDSLInterfaceConfig1", "GetInfo")
    if r:
        for key, zkey in [
            ("NewStatus", "status"),
            ("NewUpstreamMaxRate", "upstream_max_rate"),
            ("NewDownstreamMaxRate", "downstream_max_rate"),
            ("NewUpstreamCurrRate", "upstream_curr_rate"),
            ("NewDownstreamCurrRate", "downstream_curr_rate"),
            ("NewUpstreamNoiseMargin", "upstream_noise_margin"),
            ("NewDownstreamNoiseMargin", "downstream_noise_margin"),
            ("NewUpstreamAttenuation", "upstream_attenuation"),
            ("NewDownstreamAttenuation", "downstream_attenuation"),
            ("NewUpstreamPower", "upstream_power"),
            ("NewDownstreamPower", "downstream_power"),
        ]:
            if key in r:
                m[f"fritzbox.dsl.{zkey}"] = r[key]

    r = safe_call(fc, "WANDSLInterfaceConfig1", "GetStatisticsTotal")
    if r:
        for key, zkey in [
            ("NewFECErrors", "fec_errors"),
            ("NewCRCErrors", "crc_errors"),
            ("NewHECErrors", "hec_errors"),
            ("NewATUCFECErrors", "atuc_fec_errors"),
            ("NewATUCCRCErrors", "atuc_crc_errors"),
            ("NewATUCHECErrors", "atuc_hec_errors"),
        ]:
            if key in r:
                m[f"fritzbox.dsl.{zkey}"] = r[key]

    r = safe_call(fc, "WANDSLLinkConfig1", "GetInfo")
    if r:
        m["fritzbox.dsl.link_status"] = r.get("NewLinkStatus", "")
        m["fritzbox.dsl.link_type"] = r.get("NewLinkType", "")
    return m


def collect_wlan_info(fc):
    global _wlan_signal_by_mac
    m = {}
    _wlan_signal_by_mac = {}
    for idx, band in {1: "2g", 2: "5g", 3: "guest"}.items():
        svc = f"WLANConfiguration{idx}"
        r = safe_call(fc, svc, "GetInfo")
        if r:
            m[f"fritzbox.wlan.{band}.enabled"] = 1 if r.get("NewEnable", False) else 0
            m[f"fritzbox.wlan.{band}.status"] = r.get("NewStatus", "")
            m[f"fritzbox.wlan.{band}.channel"] = r.get("NewChannel", 0)
            m[f"fritzbox.wlan.{band}.ssid"] = r.get("NewSSID", "")
            m[f"fritzbox.wlan.{band}.standard"] = r.get("NewStandard", "")
            m[f"fritzbox.wlan.{band}.encryption"] = r.get("NewBeaconType", "")
        r = safe_call(fc, svc, "GetTotalAssociations")
        count = 0
        if r:
            count = r.get("NewTotalAssociations", 0)
            m[f"fritzbox.wlan.{band}.total_associations"] = count
        # Collect per-device signal strength into shared dict (used later by collect_lua_netdev)
        for i in range(count):
            ra = safe_call(
                fc,
                svc,
                "GetGenericAssociatedDeviceInfo",
                arguments={"NewAssociatedDeviceIndex": i},
            )
            if ra:
                mac = ra.get("NewAssociatedDeviceMACAddress", "").lower()
                signal = ra.get("NewX_AVM-DE_SignalStrength")
                if mac and signal is not None:
                    _wlan_signal_by_mac[mac] = signal
                    log.debug("WLAN assoc: %s band=%s signal=%s", mac, band, signal)
        r = safe_call(fc, svc, "GetPacketStatistics")
        if r:
            m[f"fritzbox.wlan.{band}.packets_sent"] = r.get("NewTotalPacketsSent", 0)
            m[f"fritzbox.wlan.{band}.packets_received"] = r.get(
                "NewTotalPacketsReceived", 0
            )
        r = safe_call(fc, svc, "X_AVM-DE_GetNightControl")
        if r:
            m[f"fritzbox.wlan.{band}.night_control"] = (
                1 if r.get("NewNightControl", "") == "ON" else 0
            )
    log.debug(
        "WLAN: collected signal for %d associated devices", len(_wlan_signal_by_mac)
    )
    return m


def collect_lan_info(fc):
    m = {}
    r = safe_call(fc, "Hosts1", "GetHostNumberOfEntries")
    if r:
        m["fritzbox.lan.host_count"] = r.get("NewHostNumberOfEntries", 0)
    try:
        fh = FritzHosts(fc=fc)
        hosts = fh.get_hosts_info()
        active = [h for h in hosts if h.get("status")]
        m["fritzbox.lan.active_hosts"] = len(active)
        m["fritzbox.lan.total_hosts"] = len(hosts)
        m["fritzbox.lan.active_wlan_hosts"] = sum(
            1 for h in active if "802.11" in str(h.get("interface_type", ""))
        )
        m["fritzbox.lan.active_lan_hosts"] = sum(
            1 for h in active if "Ethernet" in str(h.get("interface_type", ""))
        )
    except Exception as e:
        log.debug("Host details failed: %s", e)
    for idx in range(1, 5):
        r = safe_call(fc, f"LANEthernetInterfaceConfig{idx}", "GetStatistics")
        if r:
            m[f"fritzbox.lan.eth{idx}.bytes_sent"] = r.get("NewBytesSent", 0)
            m[f"fritzbox.lan.eth{idx}.bytes_received"] = r.get("NewBytesReceived", 0)
            m[f"fritzbox.lan.eth{idx}.packets_sent"] = r.get("NewPacketsSent", 0)
            m[f"fritzbox.lan.eth{idx}.packets_received"] = r.get(
                "NewPacketsReceived", 0
            )
    return m


def collect_homeauto_info(fc):
    m = {}
    r = safe_call(fc, "X_AVM-DE_Dect1", "GetNumberOfDectEntries")
    if r:
        m["fritzbox.dect.device_count"] = r.get("NewNumberOfEntries", 0)
    try:
        for idx in range(50):
            r = safe_call(
                fc,
                "X_AVM-DE_Homeauto1",
                "GetGenericDeviceInfos",
                arguments={"NewIndex": idx},
            )
            if not r:
                break
            ain = r.get("NewAIN", "").strip()
            if not ain:
                break
            name = r.get("NewDeviceName", f"device_{idx}")
            sn = name.replace(" ", "_").replace("/", "_").replace(".", "_").lower()
            m[f"fritzbox.smarthome.{sn}.present"] = (
                1 if r.get("NewPresent", "") == "CONNECTED" else 0
            )
            for src, dst, div in [
                ("NewTemperatureCelsius", "temperature", 10.0),
                ("NewMultimeterPower", "power_mw", 1),
                ("NewMultimeterEnergy", "energy_wh", 1),
            ]:
                v = r.get(src, "")
                if v and str(v) != "0":
                    try:
                        m[f"fritzbox.smarthome.{sn}.{dst}"] = int(v) / div
                    except:
                        pass
            sw = r.get("NewSwitchState", "")
            if sw:
                m[f"fritzbox.smarthome.{sn}.switch_state"] = sw
            bat = r.get("NewBatteryLow", "")
            if bat:
                m[f"fritzbox.smarthome.{sn}.battery_low"] = 1 if bat == "1" else 0
    except Exception as e:
        log.debug("Smarthome error: %s", e)
    return m


def collect_update_info(fc):
    m = {}
    r = safe_call(fc, "UserInterface1", "GetInfo")
    if r:
        m["fritzbox.update.available"] = (
            1 if r.get("NewUpgradeAvailable", "") == "1" else 0
        )
        # Send current firmware version when no update is available (field may be empty)
        latest = r.get("NewX_AVM-DE_Version", "").strip()
        m["fritzbox.update.latest_firmware"] = latest if latest else "-"
        m["fritzbox.update.lab_mode"] = (
            1 if r.get("NewX_AVM-DE_LaborVersion", "") else 0
        )
    return m


def collect_service_info(fc):
    m = {}
    r = safe_call(fc, "X_AVM-DE_MyFritz1", "GetInfo")
    if r:
        m["fritzbox.myfritz.enabled"] = 1 if r.get("NewEnabled", False) else 0
        m["fritzbox.myfritz.dyndns"] = r.get("NewDynDNSName", "")
    r = safe_call(fc, "X_AVM-DE_RemoteAccess1", "GetInfo")
    if r:
        m["fritzbox.vpn.remote_access_enabled"] = 1 if r.get("NewEnabled", False) else 0
    r = safe_call(fc, "X_AVM-DE_Storage1", "GetInfo")
    if r:
        m["fritzbox.usb.ftp_enabled"] = 1 if r.get("NewFTPEnable", False) else 0
        m["fritzbox.usb.smb_enabled"] = 1 if r.get("NewSMBEnable", False) else 0
    r = safe_call(fc, "X_AVM-DE_UPnP1", "GetInfo")
    if r:
        m["fritzbox.upnp.enabled"] = 1 if r.get("NewEnable", False) else 0
    return m


# ===================================================================
# LUA Web Interface (data.lua / query.lua)
# ===================================================================
class FritzBoxLUA:
    def __init__(self, host, user, password, use_tls=False):
        proto = "https" if use_tls else "http"
        self.base_url = f"{proto}://{host}"
        self.user = user
        self.password = password
        self.sid = None
        self.session = requests.Session()
        self.session.verify = False

    def _get_sid(self):
        try:
            url = f"{self.base_url}/login_sid.lua?version=2"
            resp = self.session.get(url, timeout=10)
            xml = ET.fromstring(resp.text)
            challenge = xml.findtext("Challenge", "")
            sid = xml.findtext("SID", "")

            if sid and sid != "0000000000000000":
                self.sid = sid
                return True
            if not challenge:
                return False

            if challenge.startswith("2$"):
                response = self._solve_pbkdf2(challenge)
            else:
                response = self._solve_md5(challenge)

            resp = self.session.post(
                f"{self.base_url}/login_sid.lua?version=2",
                data={"username": self.user, "response": response},
                timeout=10,
            )
            xml = ET.fromstring(resp.text)
            sid = xml.findtext("SID", "")
            if sid and sid != "0000000000000000":
                self.sid = sid
                log.info("LUA: Authenticated (SID=%s...)", sid[:8])
                return True
            log.warning(
                "LUA: Auth failed (BlockTime=%s)", xml.findtext("BlockTime", "0")
            )
            return False
        except Exception as e:
            log.warning("LUA: Auth error: %s", e)
            return False

    def _solve_pbkdf2(self, challenge):
        parts = challenge.split("$")
        iter1, salt1, iter2, salt2 = (
            int(parts[1]),
            bytes.fromhex(parts[2]),
            int(parts[3]),
            bytes.fromhex(parts[4]),
        )
        hash1 = hashlib.pbkdf2_hmac(
            "sha256", self.password.encode("utf-8"), salt1, iter1
        )
        hash2 = hashlib.pbkdf2_hmac("sha256", hash1, salt2, iter2)
        return f"{salt2.hex()}${hash2.hex()}"

    def _solve_md5(self, challenge):
        response_str = f"{challenge}-{self.password}"
        md5 = hashlib.md5(response_str.encode("utf-16-le")).hexdigest()
        return f"{challenge}-{md5}"

    def _ensure_session(self):
        if self.sid:
            try:
                resp = self.session.get(
                    f"{self.base_url}/login_sid.lua?version=2&sid={self.sid}", timeout=5
                )
                xml = ET.fromstring(resp.text)
                if xml.findtext("SID", "") not in ("", "0000000000000000"):
                    return True
            except:
                pass
            self.sid = None
        return self._get_sid()

    def data_lua(self, page, xhr_id="all"):
        if not self._ensure_session():
            return None
        try:
            resp = self.session.post(
                f"{self.base_url}/data.lua",
                data={
                    "xhr": 1,
                    "sid": self.sid,
                    "lang": "de",
                    "page": page,
                    "xhrId": xhr_id,
                    "no_sidrenew": "",
                },
                timeout=15,
            )
            if resp.status_code != 200:
                log.debug(
                    "LUA data.lua?page=%s xhrId=%s returned HTTP %d",
                    page,
                    xhr_id,
                    resp.status_code,
                )
                return None
            return resp.json()
        except Exception as e:
            log.debug("LUA data.lua?page=%s xhrId=%s error: %s", page, xhr_id, e)
            return None

    def query_lua(self, params):
        if not self._ensure_session():
            return None
        try:
            params["sid"] = self.sid
            resp = self.session.get(
                f"{self.base_url}/query.lua", params=params, timeout=15
            )
            return resp.json() if resp.status_code == 200 else None
        except:
            return None


def _safe_int(val, default=0):
    try:
        return int(val)
    except:
        return default


def collect_lua_system(lua):
    """CPU temp, CPU usage, RAM from data.lua?page=ecoStat"""
    m = {}
    data = lua.data_lua("ecoStat")
    if not data:
        return m
    d = data.get("data", data)
    try:
        for src, dst in [
            ("cputemp", "cpu_temperature"),
            ("cpuutil", "cpu_usage"),
            ("ramusage", "ram_usage_percent"),
        ]:
            val = d.get(src)
            if isinstance(val, dict):
                series = val.get("series", [])
                if series and isinstance(series[0], list) and series[0]:
                    m[f"fritzbox.system.{dst}"] = series[0][-1]
            elif val is not None:
                m[f"fritzbox.system.{dst}"] = _safe_int(val)
        ram = d.get("ramusage", {})
        if isinstance(ram, dict):
            # Flat keys (some firmware versions)
            for k in ("fixed", "free", "cached", "total"):
                v = ram.get(k)
                if v is not None:
                    m[f"fritzbox.system.ram_{k}"] = _safe_int(v)
            # Series/labels format — labels may be English or German depending on firmware
            _ram_label_map = {
                "fixed": "fixed",
                "free": "free",
                "cached": "cached",
                "total": "total",
                "Fest": "fixed",
                "Frei": "free",
                "Gecacht": "cached",
                "Gesamt": "total",
                "fest": "fixed",
                "frei": "free",
                "gecacht": "cached",
                "gesamt": "total",
            }
            labels = ram.get("labels", [])
            series = ram.get("series", [])
            if labels and series and len(series) > 0 and isinstance(series[0], list):
                # Check if labels are metric names (older firmware) or time values (FritzOS 8.x)
                labels_are_time = labels and not isinstance(labels[0], str)
                if labels_are_time:
                    # FritzOS 8.x: labels=hours, series=[fixed, free, cached] by position
                    _positional_map = ["fixed", "free", "cached"]
                    total = 0
                    for i, metric in enumerate(_positional_map):
                        if i < len(series) and series[i]:
                            val = _safe_int(series[i][-1])
                            m[f"fritzbox.system.ram_{metric}"] = val
                            total += val
                    if total > 0:
                        m["fritzbox.system.ram_total"] = total
                else:
                    # Older firmware: labels are metric name strings
                    for i, label in enumerate(labels):
                        metric = _ram_label_map.get(label)
                        if metric and i < len(series) and series[i]:
                            m[f"fritzbox.system.ram_{metric}"] = _safe_int(
                                series[i][-1]
                            )
            log.debug(
                "LUA ecoStat ramusage: labels=%s series_count=%d flat_keys=%s",
                labels,
                len(series),
                {k: ram.get(k) for k in ("fixed", "free", "cached", "total")},
            )
    except Exception as e:
        log.debug("LUA ecoStat parse: %s", e)
    return m


def _parse_traffic_block(block, zp, m):
    """Parse a single period traffic block into metrics dict. Returns number of values found."""
    if not isinstance(block, dict):
        return 0
    # Use next() with key presence check to avoid skipping legitimate 0 values
    sent = next(
        (
            block[k]
            for k in (
                "BytesSent",
                "bytesSent",
                "BytesSentHigh",
                "TotalBytesSent",
                "bytes_sent",
                "sent",
            )
            if k in block
        ),
        None,
    )
    recv = next(
        (
            block[k]
            for k in (
                "BytesReceived",
                "bytesReceived",
                "BytesReceivedHigh",
                "TotalBytesReceived",
                "bytes_received",
                "received",
            )
            if k in block
        ),
        None,
    )
    found = 0
    if sent is not None:
        m[f"fritzbox.traffic.{zp}.bytes_sent"] = _safe_int(sent)
        found += 1
    if recv is not None:
        m[f"fritzbox.traffic.{zp}.bytes_received"] = _safe_int(recv)
        found += 1
    return found


def collect_lua_traffic(lua):
    """Daily/weekly/monthly traffic volumes from data.lua?page=netCnt"""
    m = {}
    # Period keys FritzBox may use → Zabbix metric suffix
    period_map = [
        ("today", "today"),
        ("Today", "today"),
        ("yesterday", "yesterday"),
        ("Yesterday", "yesterday"),
        ("thisWeek", "week"),
        ("thisweek", "week"),
        ("thisMonth", "month"),
        ("thismonth", "month"),
        ("total", "total"),
        ("Total", "total"),
    ]

    # Try netCnt page with multiple xhrId values — different firmware versions require different values
    for xhr_id in ("all", "count", "start", "update"):
        data = lua.data_lua("netCnt", xhr_id=xhr_id)
        if not data:
            continue
        d = data.get("data", data)
        log.debug("LUA netCnt xhrId=%s raw: %s", xhr_id, d)
        found = 0
        try:
            # Some firmware versions nest the traffic data under a sub-key
            for container_key in (
                None,
                "netCnt",
                "counter",
                "cnt",
                "traffic",
                "trafficData",
            ):
                container = d if container_key is None else d.get(container_key, {})
                if not isinstance(container, dict):
                    continue
                for period_key, zp in period_map:
                    found += _parse_traffic_block(container.get(period_key), zp, m)
                if found > 0:
                    break
            if found > 0:
                log.debug(
                    "LUA netCnt: found %d traffic values with xhrId=%s", found, xhr_id
                )
                break
            log.debug(
                "LUA netCnt xhrId=%s: no traffic data. Top-level keys: %s",
                xhr_id,
                list(d.keys()) if isinstance(d, dict) else type(d),
            )
        except Exception as e:
            log.warning("LUA netCnt parse: %s", e)
    if not m:
        log.info("LUA netCnt: no traffic data found with any xhrId")
    if m:
        return m

    # Fallback: query.lua traffic counters (works on more firmware versions)
    log.info("LUA netCnt: trying query.lua fallback for traffic stats")
    try:
        qdata = lua.query_lua(
            {
                "logic:status/today/BytesSent": "",
                "logic:status/today/BytesReceived": "",
                "logic:status/yesterday/BytesSent": "",
                "logic:status/yesterday/BytesReceived": "",
                "logic:status/thisWeek/BytesSent": "",
                "logic:status/thisWeek/BytesReceived": "",
                "logic:status/thisMonth/BytesSent": "",
                "logic:status/thisMonth/BytesReceived": "",
                "logic:status/total/BytesSent": "",
                "logic:status/total/BytesReceived": "",
            }
        )
        if isinstance(qdata, dict):
            log.debug("LUA query.lua traffic raw: %s", qdata)
            period_keys = {"today", "yesterday", "thisWeek", "thisMonth", "total"}
            period_zp = {
                "today": "today",
                "yesterday": "yesterday",
                "thisWeek": "week",
                "thisMonth": "month",
                "total": "total",
            }
            for period in period_keys:
                zp = period_zp[period]
                sent = qdata.get(f"logic:status/{period}/BytesSent")
                recv = qdata.get(f"logic:status/{period}/BytesReceived")
                if sent is not None:
                    m[f"fritzbox.traffic.{zp}.bytes_sent"] = _safe_int(sent)
                if recv is not None:
                    m[f"fritzbox.traffic.{zp}.bytes_received"] = _safe_int(recv)
        if not m:
            log.info(
                "LUA query.lua traffic: no data either. Available keys: %s",
                list(qdata.keys())[:20] if isinstance(qdata, dict) else qdata,
            )
    except Exception as e:
        log.warning("LUA query.lua traffic fallback: %s", e)
    return m


def collect_lua_netdev(lua):
    """Per-device network info including WLAN signal from data.lua?page=netDev"""
    m = {}
    data = lua.data_lua("netDev")
    if not data:
        return m
    d = data.get("data", data)
    try:
        active = d.get("active", [])
        passive = d.get("passive", [])
        if isinstance(active, list):
            m["fritzbox.netdev.active_count"] = len(active)
        if isinstance(passive, list):
            m["fritzbox.netdev.passive_count"] = len(passive)

        lld_devices = []
        if isinstance(active, list):
            for dev in active:
                if not isinstance(dev, dict):
                    continue
                name = dev.get("name", "")
                if not name:
                    continue
                sn = (
                    name.replace(" ", "_")
                    .replace("/", "_")
                    .replace(".", "_")
                    .replace("-", "_")
                    .lower()
                )

                # IP: field can be a plain string, a dict, or a list of dicts
                # e.g. {'_node': 'entry0', 'addrtype': 'IPv4', ..., 'ip': '192.168.178.x'}
                def _extract_ip(val):
                    if isinstance(val, str):
                        return val
                    if isinstance(val, dict):
                        return val.get("ip", "")
                    if isinstance(val, list):
                        for entry in val:
                            if isinstance(entry, dict) and entry.get(
                                "addrtype", ""
                            ).startswith("IPv4"):
                                return entry.get("ip", "")
                        for entry in val:
                            if isinstance(entry, dict):
                                return entry.get("ip", "")
                    return ""

                ip = (
                    _extract_ip(dev.get("ip"))
                    or _extract_ip(dev.get("ipv4"))
                    or _extract_ip(dev.get("addr"))
                    or _extract_ip(dev.get("address"))
                    or ""
                )
                if not ip:
                    # Try any key that looks like it could contain an IP
                    for k, v in dev.items():
                        if k not in (
                            "name",
                            "type",
                            "properties",
                            "wlan",
                            "uid",
                            "mac",
                            "UID",
                            "MAC",
                        ) and isinstance(v, (str, dict, list)):
                            candidate = _extract_ip(v)
                            if candidate and "." in candidate:
                                ip = candidate
                                log.debug(
                                    "LUA netDev: found IP in field '%s' for %s", k, name
                                )
                                break
                lld_devices.append(
                    {
                        "{#DEVNAME}": sn,
                        "{#DEVTYPE}": dev.get("type", ""),
                        "{#DEVIP}": ip,
                    }
                )
                log.debug("LUA netDev raw device: %s", dev)

                dev_type = dev.get("type", "")
                if dev_type:
                    m[f"fritzbox.netdev[{sn},type]"] = dev_type
                if ip:
                    m[f"fritzbox.netdev[{sn},ip]"] = ip

                log.debug("LUA netDev device keys: %s → %s", name, list(dev.keys()))

                # Speed and RSSI: newer FritzOS firmware encodes WLAN stats in
                # properties[0]['txt'] as "2,4 GHz, 720 / 288 Mbit/s"
                # Try structured fields first, then parse the properties text.
                wlan_sub = dev.get("wlan") if isinstance(dev.get("wlan"), dict) else {}
                props_list = dev.get("properties", [])
                props_txt = ""
                if isinstance(props_list, list):
                    for p in props_list:
                        if isinstance(p, dict) and p.get("txt"):
                            props_txt = p["txt"]
                            break

                speed = None
                for candidate in (
                    wlan_sub.get("speed"),
                    wlan_sub.get("rate"),
                    dev.get("speed"),
                    dev.get("rate"),
                ):
                    if candidate not in (None, "", 0):
                        speed = candidate
                        break
                # Parse "X GHz, RX / TX Mbit/s" from properties text
                if speed is None and props_txt:
                    mo = re.search(
                        r"(\d+(?:[,\.]\d+)?)\s*GHz,\s*(\d+)\s*/\s*(\d+)\s*Mbit/s",
                        props_txt,
                    )
                    if mo:
                        speed = max(int(mo.group(2)), int(mo.group(3)))
                        m[f"fritzbox.netdev[{sn},band]"] = (
                            mo.group(1).replace(",", ".") + " GHz"
                        )
                if speed is not None:
                    m[f"fritzbox.netdev[{sn},speed]"] = speed

                # RSSI: try structured LUA fields, then TR-064 signal data by MAC
                rssi = None
                for candidate in (
                    wlan_sub.get("rssi"),
                    wlan_sub.get("signal"),
                    wlan_sub.get("signalStrength"),
                    dev.get("rssi"),
                    dev.get("signal"),
                    dev.get("signalStrength"),
                ):
                    if candidate is not None and candidate != "":
                        rssi = candidate
                        break
                # Parse RSSI from properties text, e.g. "-65 dBm"
                if rssi is None and props_txt:
                    mo_rssi = re.search(r"(-\d+)\s*dBm", props_txt)
                    if mo_rssi:
                        rssi = mo_rssi.group(1)
                # Fall back to TR-064 signal data collected during collect_wlan_info
                if rssi is None:
                    mac = dev.get("mac", "").lower()
                    if mac in _wlan_signal_by_mac:
                        rssi = _wlan_signal_by_mac[mac]
                if rssi is not None:
                    try:
                        m[f"fritzbox.netdev[{sn},rssi]"] = int(rssi)
                    except:
                        pass
        if lld_devices:
            m["fritzbox.netdev.discovery"] = json.dumps({"data": lld_devices})
    except Exception as e:
        log.debug("LUA netDev parse: %s", e)
    return m


def collect_lua_overview(lua):
    """Provider info, USB devices, DECT info from data.lua?page=overview"""
    m = {}
    data = lua.data_lua("overview")
    if not data:
        return m
    d = data.get("data", data)
    log.debug(
        "LUA overview raw keys: %s", list(d.keys()) if isinstance(d, dict) else type(d)
    )
    try:
        # Internet section: try multiple known key names across firmware versions
        inet = None
        for key in ("internet", "inetStat", "wan", "inet"):
            inet = d.get(key)
            if isinstance(inet, dict):
                break
        if isinstance(inet, dict):
            # txt: list of status strings; ip: external IP; provider: provider name
            txt_items = inet.get("txt", inet.get("text", []))
            if isinstance(txt_items, list):
                conn_parts = []
                prov_parts = []
                for item in txt_items:
                    if not isinstance(item, str) or len(item) < 2:
                        continue
                    low = item.lower()
                    if any(
                        w in low
                        for w in (
                            "verbunden",
                            "connected",
                            "online",
                            "seit",
                            "since",
                            "ip",
                        )
                    ):
                        conn_parts.append(item)
                    else:
                        prov_parts.append(item)
                if conn_parts:
                    m["fritzbox.overview.connection_info"] = " | ".join(conn_parts)
                if prov_parts:
                    m["fritzbox.overview.provider_info"] = " | ".join(prov_parts)
            # Some firmware returns provider/IP as direct fields
            if not m.get("fritzbox.overview.provider_info"):
                for key in ("provider", "isp", "providerName"):
                    v = inet.get(key)
                    if isinstance(v, str) and v:
                        m["fritzbox.overview.provider_info"] = v
                        break
            if not m.get("fritzbox.overview.connection_info"):
                for key in ("ip", "externalIP", "externalIPv4"):
                    v = inet.get(key)
                    if isinstance(v, str) and v:
                        m["fritzbox.overview.connection_info"] = v
                        break
        usb = d.get("usb", {})
        if isinstance(usb, dict):
            cnt = usb.get("count", usb.get("anzahl", 0))
            if cnt is not None:
                m["fritzbox.overview.usb_devices"] = _safe_int(cnt)
    except Exception as e:
        log.warning("LUA overview parse: %s", e)
    return m


def collect_lua_energy(lua):
    """Energy consumption data from data.lua?page=energy"""
    m = {}
    data = lua.data_lua("energy")
    if not data:
        return m
    d = data.get("data", data)
    try:
        drains = d.get("drain", [])
        if isinstance(drains, list):
            for dev in drains:
                if not isinstance(dev, dict):
                    continue
                name = dev.get("name", "")
                if not name:
                    continue
                sn = name.replace(" ", "_").replace("/", "_").lower()
                act = dev.get("actCycle", "")
                if act:
                    m[f"fritzbox.energy.{sn}.active_cycle"] = act
    except Exception as e:
        log.debug("LUA energy parse: %s", e)
    return m


def collect_lua_dsl_detail(lua):
    """Extended DSL stats from data.lua?page=dslStat not available via TR-064"""
    m = {}
    data = lua.data_lua("dslStat")
    if not data:
        log.debug("LUA dslStat: page returned no data")
        return m
    d = data.get("data", data)
    log.debug("LUA dslStat raw: %s", d)
    try:
        # Try multiple known container keys across firmware versions
        line = None
        for container_key in (
            "line",
            "atur",
            "aturStat",
            "dsl",
            "dslStats",
            "dsl_stats",
            "stat",
        ):
            candidate = d.get(container_key)
            if isinstance(candidate, dict):
                line = candidate
                break
        if line is None:
            line = d if isinstance(d, dict) else {}

        if not line:
            log.info(
                "LUA dslStat: unexpected structure — top-level keys: %s",
                list(d.keys()) if isinstance(d, dict) else d,
            )
            return m

        neg_vals_raw = line.get("negotiatedValues")
        err_ctrs_raw = line.get("errorCounters")

        # --- List-based structure (FritzOS 8.x) ---
        # negotiatedValues: [{'title': '...', 'unit': '...', 'val': [{'ds': '...', 'us': '...'}]}, ...]
        # errorCounters:    [{'title': '...', 'val': [{'ds': '...', 'us': '...'}]}, ...]
        if isinstance(neg_vals_raw, list) or isinstance(err_ctrs_raw, list):
            # Title → metric key mapping (German and English titles)
            NEG_TITLE_MAP = {
                "störabstandsmarge": "snr",
                "noise margin": "snr",
                "snr margin": "snr",
                "leitungsdämpfung": "attn",
                "line attenuation": "attn",
                "leitungskapazität": "capacity",
                "attainable rate": "capacity",
                "maximum rate": "capacity",
            }
            ERR_TITLE_MAP = {
                "fehlern (es)": "errSeconds",
                "error seconds": "errSeconds",
                "fehlern (ses)": "sevErrSeconds",
                "severely errored": "sevErrSeconds",
                "pro minute": "crcPerMin",
                "per minute": "crcPerMin",
                "crc per minute": "crcPerMin",
                "signalverlust": "lossOfSignal",
                "loss of signal": "lossOfSignal",
                "rahmenverlust": "lossOfFrame",
                "loss of frame": "lossOfFrame",
            }

            def _parse_list_metrics(items, title_map):
                """Extract ds/us values from list-based FritzOS metric containers."""
                results = {}  # metric_key → {'ds': ..., 'us': ...}
                if not isinstance(items, list):
                    return results
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    title = item.get("title", "").strip().lower()
                    val_list = item.get("val")
                    if not val_list or not isinstance(val_list, list):
                        continue
                    val = val_list[0] if val_list else {}
                    if not isinstance(val, dict):
                        continue
                    # Match title against known mappings (substring match for flexibility)
                    for title_key, metric_key in title_map.items():
                        if title_key in title and metric_key not in results:
                            ds_v = val.get("ds")
                            us_v = val.get("us")
                            if ds_v is not None or us_v is not None:
                                results[metric_key] = {"ds": ds_v, "us": us_v}
                            break
                return results

            neg_metrics = _parse_list_metrics(neg_vals_raw, NEG_TITLE_MAP)
            err_metrics = _parse_list_metrics(err_ctrs_raw, ERR_TITLE_MAP)
            all_metrics = {**neg_metrics, **err_metrics}

            for metric_key, dir_vals in all_metrics.items():
                for direction, val in dir_vals.items():
                    if val is not None:
                        m[f"fritzbox.dsl_detail.{direction}.{metric_key}"] = val

            if not m:
                neg_titles = (
                    [i.get("title") for i in neg_vals_raw if isinstance(i, dict)]
                    if isinstance(neg_vals_raw, list)
                    else []
                )
                err_titles = (
                    [i.get("title") for i in err_ctrs_raw if isinstance(i, dict)]
                    if isinstance(err_ctrs_raw, list)
                    else []
                )
                log.info(
                    "LUA dslStat: no DSL metrics from list structure. neg titles: %s err titles: %s",
                    neg_titles,
                    err_titles,
                )

        # --- Dict/flat structure (older firmware) ---
        else:
            neg_vals = neg_vals_raw if isinstance(neg_vals_raw, dict) else {}
            err_ctrs = err_ctrs_raw if isinstance(err_ctrs_raw, dict) else {}

            def _get_dir_sub(container, direction):
                for key in (
                    direction,
                    f"{direction}stream",
                    "downstream" if direction == "ds" else "upstream",
                ):
                    v = container.get(key)
                    if isinstance(v, dict):
                        return v
                return {}

            ds_sub = _get_dir_sub(neg_vals, "ds") or _get_dir_sub(line, "ds")
            us_sub = _get_dir_sub(neg_vals, "us") or _get_dir_sub(line, "us")
            ds_err = _get_dir_sub(err_ctrs, "ds")
            us_err = _get_dir_sub(err_ctrs, "us")

            perf_aliases = {
                "snr": ("snr", "SNR", "noiseMargin", "noise_margin", "snrMargin"),
                "attn": ("attn", "attenuation", "Attenuation", "latn"),
                "capacity": (
                    "capacity",
                    "Capacity",
                    "maxBitRate",
                    "maxRate",
                    "attainableRate",
                ),
            }
            err_aliases = {
                "crcPerMin": ("crcPerMin", "crc", "CRC", "crcErrors"),
                "errSeconds": ("errSeconds", "es", "ES", "errorSeconds"),
                "sevErrSeconds": (
                    "sevErrSeconds",
                    "ses",
                    "SES",
                    "severelyErroredSeconds",
                ),
                "lossOfSignal": ("lossOfSignal", "los", "LOS"),
                "lossOfFrame": ("lossOfFrame", "lof", "LOF"),
            }

            def _lookup(containers_and_aliases, direction):
                results = {}
                for container, aliases_dict in containers_and_aliases:
                    for k, aliases in aliases_dict.items():
                        if k in results:
                            continue
                        for alias in aliases:
                            v = container.get(
                                alias,
                                container.get(
                                    f"{direction}_{alias}",
                                    container.get(f"{direction}{alias}"),
                                ),
                            )
                            if v is not None:
                                results[k] = v
                                break
                return results

            for direction, perf_sub, err_sub in (
                ("ds", ds_sub, ds_err),
                ("us", us_sub, us_err),
            ):
                found = _lookup(
                    [
                        (perf_sub, perf_aliases),
                        (err_sub, err_aliases),
                        (line, {**perf_aliases, **err_aliases}),
                    ],
                    direction,
                )
                for k, v in found.items():
                    m[f"fritzbox.dsl_detail.{direction}.{k}"] = v

            if not m:
                log.info(
                    "LUA dslStat: no DSL detail metrics found. line keys: %s",
                    list(line.keys()),
                )

    except Exception as e:
        log.warning("LUA dslStat parse: %s", e)
    return m


# ===================================================================
# Callmonitor (TCP 1012)
# ===================================================================
class CallMonitor:
    """
    Monitors FritzBox call events on TCP port 1012.
    Activate by dialing #96*5* on a connected phone. Deactivate: #96*4*.
    Event format: DD.MM.YY HH:MM:SS;TYPE;ID;...
    TYPE: RING (incoming), CALL (outgoing), CONNECT (answered), DISCONNECT (ended)
    """

    def __init__(self, host, port=1012):
        self.host = host
        self.port = port
        self.sock = None
        self.running = False
        self.thread = None
        self.lock = threading.Lock()
        self.active_calls = {}
        self.stats = {
            "total_incoming": 0,
            "total_outgoing": 0,
            "total_missed": 0,
            "total_answered": 0,
            "current_active": 0,
            "last_caller": "",
            "last_called": "",
            "last_event_type": "",
            "last_event_time": "",
            "available": 0,
        }

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        log.info("Callmonitor: Listener started for %s:%d", self.host, self.port)

    def stop(self):
        self.running = False
        if self.sock:
            try:
                self.sock.close()
            except:
                pass

    def get_metrics(self):
        with self.lock:
            return {f"fritzbox.callmon.{k}": v for k, v in self.stats.items()}

    def _loop(self):
        while self.running:
            try:
                self._connect()
                buf = ""
                while self.running:
                    data = self.sock.recv(1024)
                    if not data:
                        log.warning("Callmonitor: Connection closed")
                        break
                    buf += data.decode("utf-8", errors="replace")
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        line = line.strip()
                        if line:
                            self._parse(line)
            except socket.timeout:
                continue
            except Exception as e:
                log.debug("Callmonitor: Error: %s", e)
                with self.lock:
                    self.stats["available"] = 0
            if self.running:
                log.info("Callmonitor: Reconnecting in 30s...")
                time.sleep(30)

    def _connect(self):
        if self.sock:
            try:
                self.sock.close()
            except:
                pass
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(120)
        self.sock.connect((self.host, self.port))
        log.info("Callmonitor: Connected to %s:%d", self.host, self.port)
        with self.lock:
            self.stats["available"] = 1

    def _parse(self, line):
        try:
            parts = line.split(";")
            if len(parts) < 4:
                return
            ts, etype, cid = parts[0], parts[1], parts[2]
            with self.lock:
                self.stats["last_event_type"] = etype
                self.stats["last_event_time"] = ts
                if etype == "RING":
                    caller = parts[3] if len(parts) > 3 else ""
                    called = parts[4] if len(parts) > 4 else ""
                    self.active_calls[cid] = {
                        "type": "in",
                        "caller": caller,
                        "connected": False,
                    }
                    self.stats["total_incoming"] += 1
                    self.stats["last_caller"] = caller
                    log.info("Callmonitor: RING from %s", caller)
                elif etype == "CALL":
                    called = parts[4] if len(parts) > 4 else ""
                    self.active_calls[cid] = {
                        "type": "out",
                        "called": called,
                        "connected": False,
                    }
                    self.stats["total_outgoing"] += 1
                    self.stats["last_called"] = called
                    log.info("Callmonitor: CALL to %s", called)
                elif etype == "CONNECT":
                    if cid in self.active_calls:
                        self.active_calls[cid]["connected"] = True
                    self.stats["total_answered"] += 1
                elif etype == "DISCONNECT":
                    info = self.active_calls.pop(cid, None)
                    if info and not info.get("connected") and info["type"] == "in":
                        self.stats["total_missed"] += 1
                self.stats["current_active"] = len(self.active_calls)
        except Exception as e:
            log.debug("Callmonitor: Parse error: %s", e)


# ===================================================================
# Zabbix Sender
# ===================================================================
def _build_zabbix_sender_cmd(input_file, with_timestamps=False):
    cmd = ["zabbix_sender", "-z", ZABBIX_SERVER, "-p", ZABBIX_SERVER_PORT]
    if with_timestamps:
        cmd.append("-T")
    cmd += ["-i", input_file]
    if TLS_PSK_IDENTITY and TLS_PSK_FILE:
        cmd += [
            "--tls-connect",
            "psk",
            "--tls-psk-identity",
            TLS_PSK_IDENTITY,
            "--tls-psk-file",
            TLS_PSK_FILE,
        ]
    if DEBUG:
        cmd.append("-vv")
    return cmd


def send_to_zabbix(metrics):
    if not metrics or not ZABBIX_SERVER:
        return False
    lines = []
    for k, v in metrics.items():
        if v is None:
            continue
        if isinstance(v, bool):
            v = 1 if v else 0
        v = _normalize_sender_value(v)
        if not v:
            continue
        lines.append(f"{FRITZBOX_HOSTNAME} {k} {v}")
    if not lines:
        return False
    log.info(
        "Sending %d metrics to Zabbix %s:%s",
        len(lines),
        ZABBIX_SERVER,
        ZABBIX_SERVER_PORT,
    )
    if DEBUG:
        for line in lines:
            log.debug("  >> %s", line)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write("\n".join(lines) + "\n")
        tmp = f.name
    try:
        cmd = _build_zabbix_sender_cmd(tmp)
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if DEBUG and (r.stdout.strip() or r.stderr.strip()):
            for line in (r.stdout + r.stderr).splitlines():
                log.debug("  zabbix_sender: %s", line)
        if r.returncode == 0:
            log.info("Zabbix sender: %s", r.stdout.strip())
            return True
        log.error(
            "Zabbix sender failed (rc=%d): %s %s",
            r.returncode,
            r.stdout.strip(),
            r.stderr.strip(),
        )
        return False
    except Exception as e:
        log.error("zabbix_sender error: %s", e)
        return False
    finally:
        try:
            os.unlink(tmp)
        except:
            pass


def send_log_events_to_zabbix(log_events):
    if not log_events or not ZABBIX_SERVER:
        return False

    lines = []
    for event in sorted(
        log_events, key=lambda x: (x.get("clock", 0), x.get("fingerprint", ""))
    ):
        message = _normalize_sender_value(event.get("message", ""))
        if not message:
            continue
        clock = int(event.get("clock", int(time.time())))
        lines.append(
            f"{_quote_sender_field(FRITZBOX_HOSTNAME)} "
            f"{_quote_sender_field(DEVICE_LOG_ITEM_KEY)} "
            f"{clock} "
            f"{_quote_sender_field(message)}"
        )

    if not lines:
        return False

    log.info(
        "Sending %d device log event(s) to Zabbix %s:%s",
        len(lines),
        ZABBIX_SERVER,
        ZABBIX_SERVER_PORT,
    )
    if DEBUG:
        for line in lines:
            log.debug("  >> %s", line)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write("\n".join(lines) + "\n")
        tmp = f.name
    try:
        cmd = _build_zabbix_sender_cmd(tmp, with_timestamps=True)
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if DEBUG and (r.stdout.strip() or r.stderr.strip()):
            for line in (r.stdout + r.stderr).splitlines():
                log.debug("  zabbix_sender(log): %s", line)
        if r.returncode == 0:
            log.info("Zabbix sender(log): %s", r.stdout.strip())
            return True
        log.error(
            "zabbix_sender(log) failed (rc=%d): %s %s",
            r.returncode,
            r.stdout.strip(),
            r.stderr.strip(),
        )
        return False
    except Exception as e:
        log.error("zabbix_sender(log) error: %s", e)
        return False
    finally:
        try:
            os.unlink(tmp)
        except:
            pass


# ===================================================================
# Main
# ===================================================================
def collect_all(fc, lua, cmon):
    m = {
        "fritzbox.collector.status": 1,
        "fritzbox.collector.timestamp": int(time.time()),
    }
    device_log_events = []
    try:
        r, device_log_events = collect_device_info(fc)
        m.update(r)
        log.debug("TR-064 device: %d items", len(r))
    except Exception as e:
        log.warning("TR-064 device: %s", e)

    for name, fn in [
        ("wan", collect_wan_info),
        ("dsl", collect_dsl_info),
        ("wlan", collect_wlan_info),
        ("lan", collect_lan_info),
        ("homeauto", collect_homeauto_info),
        ("update", collect_update_info),
        ("services", collect_service_info),
    ]:
        try:
            r = fn(fc)
            m.update(r)
            log.debug("TR-064 %s: %d items", name, len(r))
        except Exception as e:
            log.warning("TR-064 %s: %s", name, e)
    if lua:
        for name, fn in [
            ("system", collect_lua_system),
            ("traffic", collect_lua_traffic),
            ("netdev", collect_lua_netdev),
            ("overview", collect_lua_overview),
            ("energy", collect_lua_energy),
            ("dsl_detail", collect_lua_dsl_detail),
        ]:
            try:
                r = fn(lua)
                m.update(r)
                log.debug("LUA %s: %d items", name, len(r))
            except Exception as e:
                log.warning("LUA %s: %s", name, e)
    if cmon:
        try:
            m.update(cmon.get_metrics())
        except:
            pass
    return m, device_log_events


def main():
    interval = parse_interval(INTERVAL)
    log.info("FritzBox Zabbix Monitor v2.2 (interval=%ds)", interval)
    log.info(
        "Target: %s@%s:%d | Zabbix: %s:%s | Host: %s",
        FRITZBOX_USER,
        FRITZBOX_IP,
        FRITZBOX_PORT,
        ZABBIX_SERVER,
        ZABBIX_SERVER_PORT,
        FRITZBOX_HOSTNAME,
    )
    log.info(
        "Features: TR-064=yes LUA=%s Callmonitor=%s", ENABLE_LUA, ENABLE_CALLMONITOR
    )

    if not FRITZBOX_USER or not FRITZBOX_PASSWD:
        log.error("FRITZBOX_USER and FRITZBOX_PASSWD required")
        sys.exit(1)
    if not ZABBIX_SERVER:
        log.error("ZABBIX_SERVER required")
        sys.exit(1)

    fc = None
    while fc is None:
        try:
            fc = FritzConnection(
                address=FRITZBOX_IP,
                user=FRITZBOX_USER,
                password=FRITZBOX_PASSWD,
                port=FRITZBOX_PORT,
                use_tls=FRITZBOX_USE_TLS,
                timeout=10,
            )
            log.info(
                "TR-064: Connected to %s (FritzOS %s)", fc.modelname, fc.system_version
            )
        except Exception as e:
            log.error("TR-064 connection failed: %s — retrying in 30 seconds", e)
            time.sleep(30)

    lua = None
    if ENABLE_LUA:
        try:
            lua = FritzBoxLUA(
                FRITZBOX_IP, FRITZBOX_USER, FRITZBOX_PASSWD, FRITZBOX_USE_TLS
            )
            log.info("LUA: Client initialized")
        except Exception as e:
            log.warning("LUA: Init failed: %s", e)

    cmon = None
    if ENABLE_CALLMONITOR:
        try:
            cmon = CallMonitor(FRITZBOX_IP, CALLMONITOR_PORT)
            cmon.start()
        except Exception as e:
            log.warning("Callmonitor: Init failed: %s", e)

    while True:
        t0 = time.time()
        try:
            metrics, device_log_events = collect_all(fc, lua, cmon)
            log.info("Collected %d metrics", len(metrics))
            if DEBUG:
                for k, v in sorted(metrics.items()):
                    log.debug("  %s = %s", k, v)
            send_to_zabbix(metrics)
            send_log_events_to_zabbix(device_log_events)
        except Exception as e:
            log.error("Main loop: %s", e)
            try:
                fc = FritzConnection(
                    address=FRITZBOX_IP,
                    user=FRITZBOX_USER,
                    password=FRITZBOX_PASSWD,
                    port=FRITZBOX_PORT,
                    use_tls=FRITZBOX_USE_TLS,
                    timeout=10,
                )
            except:
                pass
        time.sleep(max(1, interval - (time.time() - t0)))


if __name__ == "__main__":
    main()
