#!/usr/bin/env python3
"""
FritzBox Zabbix Monitoring v2.1
Collects metrics from AVM Fritz!Box via three interfaces:
  1. TR-064 (SOAP/UPnP) - Standard router metrics
  2. LUA Web Interface (data.lua/query.lua) - CPU, RAM, temperature, traffic stats
  3. Callmonitor (TCP 1012) - Phone call tracking

Sends all metrics to Zabbix via zabbix_sender.
"""

import os
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
TLS_PSK = os.environ.get("TLS_PSK", "")

INTERVAL = os.environ.get("INTERVAL", "60s")
DEBUG = os.environ.get("ZABBIX_SENDER_DEBUG", "false").lower() in ("true", "1", "yes")

# Feature toggles
ENABLE_LUA = os.environ.get("ENABLE_LUA", "true").lower() in ("true", "1", "yes")
ENABLE_CALLMONITOR = os.environ.get("ENABLE_CALLMONITOR", "false").lower() in ("true", "1", "yes")
CALLMONITOR_PORT = int(os.environ.get("CALLMONITOR_PORT", "1012"))

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
log_level = logging.DEBUG if DEBUG else logging.INFO
logging.basicConfig(level=log_level, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("fritzbox-monitor")

# Suppress noisy urllib3 warnings for self-signed certs
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def parse_interval(s):
    s = s.strip().lower()
    if s.endswith("s"): return int(s[:-1])
    if s.endswith("m"): return int(s[:-1]) * 60
    if s.endswith("h"): return int(s[:-1]) * 3600
    return int(s)


# ===================================================================
# TR-064 Interface
# ===================================================================
def safe_call(fc, service, action, arguments=None):
    try:
        return fc.call_action(service, action, arguments=arguments) if arguments else fc.call_action(service, action)
    except Exception as e:
        log.debug("TR-064 %s/%s failed: %s", service, action, e)
        return None


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
    return m


def collect_wan_info(fc):
    m = {}
    r = safe_call(fc, "WANCommonInterfaceConfig1", "GetCommonLinkProperties")
    if r:
        m["fritzbox.wan.access_type"] = r.get("NewWANAccessType", "")
        m["fritzbox.wan.layer1_upstream_max"] = r.get("NewLayer1UpstreamMaxBitRate", 0)
        m["fritzbox.wan.layer1_downstream_max"] = r.get("NewLayer1DownstreamMaxBitRate", 0)
        m["fritzbox.wan.physical_link_status"] = r.get("NewPhysicalLinkStatus", "")

    r = safe_call(fc, "WANCommonInterfaceConfig1", "GetTotalBytesReceived")
    if r: m["fritzbox.wan.bytes_received"] = r.get("NewTotalBytesReceived", 0)
    r = safe_call(fc, "WANCommonInterfaceConfig1", "GetTotalBytesSent")
    if r: m["fritzbox.wan.bytes_sent"] = r.get("NewTotalBytesSent", 0)
    r = safe_call(fc, "WANCommonInterfaceConfig1", "GetTotalPacketsReceived")
    if r: m["fritzbox.wan.packets_received"] = r.get("NewTotalPacketsReceived", 0)
    r = safe_call(fc, "WANCommonInterfaceConfig1", "GetTotalPacketsSent")
    if r: m["fritzbox.wan.packets_sent"] = r.get("NewTotalPacketsSent", 0)

    r = safe_call(fc, "WANCommonInterfaceConfig1", "X_AVM-DE_GetOnlineMonitor", arguments={"NewSyncGroupIndex": 0})
    if r:
        try:
            ds = str(r.get("Newds_current_bps", "0")).split(",")[0]
            us = str(r.get("Newus_current_bps", "0")).split(",")[0]
            m["fritzbox.wan.downstream_current_bps"] = int(ds)
            m["fritzbox.wan.upstream_current_bps"] = int(us)
            m["fritzbox.wan.downstream_max_bps"] = r.get("Newmax_ds", 0)
            m["fritzbox.wan.upstream_max_bps"] = r.get("Newmax_us", 0)
        except (ValueError, TypeError): pass

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

    r = safe_call(fc, "WANIPConnection1", "X_AVM_DE_GetExternalIPv6Address")
    if r:
        m["fritzbox.wan.external_ipv6"] = r.get("NewExternalIPv6Address", "")
        m["fritzbox.wan.external_ipv6_prefix"] = r.get("NewPrefixLength", "")

    for svc in ["WANPPPConnection1", "WANIPConnection1"]:
        r = safe_call(fc, svc, "GetDNSServers") or safe_call(fc, svc, "X_GetDNSServers")
        if r:
            m["fritzbox.wan.dns_servers"] = r.get("NewDNSServers", r.get("NewIPv4DNSServer1", ""))
            break

    m["fritzbox.wan.is_connected"] = 1 if wan_connected else 0
    return m


def collect_dsl_info(fc):
    m = {}
    r = safe_call(fc, "WANDSLInterfaceConfig1", "GetInfo")
    if r:
        for key, zkey in [("NewStatus","status"),("NewUpstreamMaxRate","upstream_max_rate"),
            ("NewDownstreamMaxRate","downstream_max_rate"),("NewUpstreamCurrRate","upstream_curr_rate"),
            ("NewDownstreamCurrRate","downstream_curr_rate"),("NewUpstreamNoiseMargin","upstream_noise_margin"),
            ("NewDownstreamNoiseMargin","downstream_noise_margin"),("NewUpstreamAttenuation","upstream_attenuation"),
            ("NewDownstreamAttenuation","downstream_attenuation"),("NewUpstreamPower","upstream_power"),
            ("NewDownstreamPower","downstream_power")]:
            if key in r: m[f"fritzbox.dsl.{zkey}"] = r[key]

    r = safe_call(fc, "WANDSLInterfaceConfig1", "GetStatisticsTotal")
    if r:
        for key, zkey in [("NewFECErrors","fec_errors"),("NewCRCErrors","crc_errors"),
            ("NewHECErrors","hec_errors"),("NewATUCFECErrors","atuc_fec_errors"),
            ("NewATUCCRCErrors","atuc_crc_errors"),("NewATUCHECErrors","atuc_hec_errors")]:
            if key in r: m[f"fritzbox.dsl.{zkey}"] = r[key]

    r = safe_call(fc, "WANDSLLinkConfig1", "GetInfo")
    if r:
        m["fritzbox.dsl.link_status"] = r.get("NewLinkStatus", "")
        m["fritzbox.dsl.link_type"] = r.get("NewLinkType", "")
    return m


def collect_wlan_info(fc):
    m = {}
    for idx, band in {1:"2g", 2:"5g", 3:"guest"}.items():
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
        if r: m[f"fritzbox.wlan.{band}.total_associations"] = r.get("NewTotalAssociations", 0)
        r = safe_call(fc, svc, "GetPacketStatistics")
        if r:
            m[f"fritzbox.wlan.{band}.packets_sent"] = r.get("NewTotalPacketsSent", 0)
            m[f"fritzbox.wlan.{band}.packets_received"] = r.get("NewTotalPacketsReceived", 0)
        r = safe_call(fc, svc, "X_AVM-DE_GetNightControl")
        if r: m[f"fritzbox.wlan.{band}.night_control"] = 1 if r.get("NewNightControl","") == "ON" else 0
    return m


def collect_lan_info(fc):
    m = {}
    r = safe_call(fc, "Hosts1", "GetHostNumberOfEntries")
    if r: m["fritzbox.lan.host_count"] = r.get("NewHostNumberOfEntries", 0)
    try:
        fh = FritzHosts(address=FRITZBOX_IP, user=FRITZBOX_USER, password=FRITZBOX_PASSWD,
                        port=FRITZBOX_PORT, use_tls=FRITZBOX_USE_TLS)
        hosts = fh.get_hosts_info()
        active = [h for h in hosts if h.get("status")]
        m["fritzbox.lan.active_hosts"] = len(active)
        m["fritzbox.lan.total_hosts"] = len(hosts)
        m["fritzbox.lan.active_wlan_hosts"] = sum(1 for h in active if "802.11" in str(h.get("interface_type","")))
        m["fritzbox.lan.active_lan_hosts"] = sum(1 for h in active if "Ethernet" in str(h.get("interface_type","")))
    except Exception as e:
        log.debug("Host details failed: %s", e)
    for idx in range(1, 5):
        r = safe_call(fc, f"LANEthernetInterfaceConfig{idx}", "GetStatistics")
        if r:
            m[f"fritzbox.lan.eth{idx}.bytes_sent"] = r.get("NewBytesSent", 0)
            m[f"fritzbox.lan.eth{idx}.bytes_received"] = r.get("NewBytesReceived", 0)
            m[f"fritzbox.lan.eth{idx}.packets_sent"] = r.get("NewPacketsSent", 0)
            m[f"fritzbox.lan.eth{idx}.packets_received"] = r.get("NewPacketsReceived", 0)
    return m


def collect_homeauto_info(fc):
    m = {}
    r = safe_call(fc, "X_AVM-DE_Dect1", "GetNumberOfDectEntries")
    if r: m["fritzbox.dect.device_count"] = r.get("NewNumberOfEntries", 0)
    try:
        for idx in range(50):
            r = safe_call(fc, "X_AVM-DE_Homeauto1", "GetGenericDeviceInfos", arguments={"NewIndex": idx})
            if not r: break
            ain = r.get("NewAIN", "").strip()
            if not ain: break
            name = r.get("NewDeviceName", f"device_{idx}")
            sn = name.replace(" ","_").replace("/","_").replace(".","_").lower()
            m[f"fritzbox.smarthome.{sn}.present"] = 1 if r.get("NewPresent","") == "CONNECTED" else 0
            for src, dst, div in [("NewTemperatureCelsius","temperature",10.0),("NewMultimeterPower","power_mw",1),("NewMultimeterEnergy","energy_wh",1)]:
                v = r.get(src, "")
                if v and str(v) != "0":
                    try: m[f"fritzbox.smarthome.{sn}.{dst}"] = int(v) / div
                    except: pass
            sw = r.get("NewSwitchState", "")
            if sw: m[f"fritzbox.smarthome.{sn}.switch_state"] = sw
            bat = r.get("NewBatteryLow", "")
            if bat: m[f"fritzbox.smarthome.{sn}.battery_low"] = 1 if bat == "1" else 0
    except Exception as e:
        log.debug("Smarthome error: %s", e)
    return m


def collect_update_info(fc):
    m = {}
    r = safe_call(fc, "UserInterface1", "GetInfo")
    if r:
        m["fritzbox.update.available"] = 1 if r.get("NewUpgradeAvailable","") == "1" else 0
        m["fritzbox.update.latest_firmware"] = r.get("NewX_AVM-DE_Version", "")
        m["fritzbox.update.lab_mode"] = 1 if r.get("NewX_AVM-DE_LaborVersion", "") else 0
    return m


def collect_service_info(fc):
    m = {}
    r = safe_call(fc, "X_AVM-DE_MyFritz1", "GetInfo")
    if r:
        m["fritzbox.myfritz.enabled"] = 1 if r.get("NewEnabled", False) else 0
        m["fritzbox.myfritz.dyndns"] = r.get("NewDynDNSName", "")
    r = safe_call(fc, "X_AVM-DE_RemoteAccess1", "GetInfo")
    if r: m["fritzbox.vpn.remote_access_enabled"] = 1 if r.get("NewEnabled", False) else 0
    r = safe_call(fc, "X_AVM-DE_Storage1", "GetInfo")
    if r:
        m["fritzbox.usb.ftp_enabled"] = 1 if r.get("NewFTPEnable", False) else 0
        m["fritzbox.usb.smb_enabled"] = 1 if r.get("NewSMBEnable", False) else 0
    r = safe_call(fc, "X_AVM-DE_UPnP1", "GetInfo")
    if r: m["fritzbox.upnp.enabled"] = 1 if r.get("NewEnable", False) else 0
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

            resp = self.session.post(f"{self.base_url}/login_sid.lua?version=2",
                                     data={"username": self.user, "response": response}, timeout=10)
            xml = ET.fromstring(resp.text)
            sid = xml.findtext("SID", "")
            if sid and sid != "0000000000000000":
                self.sid = sid
                log.info("LUA: Authenticated (SID=%s...)", sid[:8])
                return True
            log.warning("LUA: Auth failed (BlockTime=%s)", xml.findtext("BlockTime","0"))
            return False
        except Exception as e:
            log.warning("LUA: Auth error: %s", e)
            return False

    def _solve_pbkdf2(self, challenge):
        parts = challenge.split("$")
        iter1, salt1, iter2, salt2 = int(parts[1]), bytes.fromhex(parts[2]), int(parts[3]), bytes.fromhex(parts[4])
        hash1 = hashlib.pbkdf2_hmac("sha256", self.password.encode("utf-8"), salt1, iter1)
        hash2 = hashlib.pbkdf2_hmac("sha256", hash1, salt2, iter2)
        return f"{salt2.hex()}${hash2.hex()}"

    def _solve_md5(self, challenge):
        response_str = f"{challenge}-{self.password}"
        md5 = hashlib.md5(response_str.encode("utf-16-le")).hexdigest()
        return f"{challenge}-{md5}"

    def _ensure_session(self):
        if self.sid:
            try:
                resp = self.session.get(f"{self.base_url}/login_sid.lua?version=2&sid={self.sid}", timeout=5)
                xml = ET.fromstring(resp.text)
                if xml.findtext("SID","") not in ("", "0000000000000000"):
                    return True
            except: pass
            self.sid = None
        return self._get_sid()

    def data_lua(self, page):
        if not self._ensure_session(): return None
        try:
            resp = self.session.post(f"{self.base_url}/data.lua",
                data={"xhr":1, "sid":self.sid, "lang":"de", "page":page, "xhrId":"all", "no_sidrenew":""},
                timeout=15)
            return resp.json() if resp.status_code == 200 else None
        except: return None

    def query_lua(self, params):
        if not self._ensure_session(): return None
        try:
            params["sid"] = self.sid
            resp = self.session.get(f"{self.base_url}/query.lua", params=params, timeout=15)
            return resp.json() if resp.status_code == 200 else None
        except: return None


def _safe_int(val, default=0):
    try: return int(val)
    except: return default


def collect_lua_system(lua):
    """CPU temp, CPU usage, RAM from data.lua?page=ecoStat"""
    m = {}
    data = lua.data_lua("ecoStat")
    if not data: return m
    d = data.get("data", data)
    try:
        for src, dst in [("cputemp","cpu_temperature"), ("cpuutil","cpu_usage"), ("ramusage","ram_usage_percent")]:
            val = d.get(src)
            if isinstance(val, dict):
                series = val.get("series", [])
                if series and isinstance(series[0], list) and series[0]:
                    m[f"fritzbox.system.{dst}"] = series[0][-1]
            elif val is not None:
                m[f"fritzbox.system.{dst}"] = _safe_int(val)
        ram = d.get("ramusage", {})
        if isinstance(ram, dict):
            for k in ("fixed","free","cached","total"):
                v = ram.get(k)
                if v is not None: m[f"fritzbox.system.ram_{k}"] = _safe_int(v)
    except Exception as e:
        log.debug("LUA ecoStat parse: %s", e)
    return m


def collect_lua_traffic(lua):
    """Daily/weekly/monthly traffic volumes from data.lua?page=netCnt"""
    m = {}
    data = lua.data_lua("netCnt")
    if not data: return m
    d = data.get("data", data)
    try:
        for period in ("today","yesterday","thisWeek","thisMonth","total"):
            block = d.get(period, {})
            if not isinstance(block, dict): continue
            zp = period.lower().replace("this","")
            sent = block.get("BytesSentHigh", block.get("TotalBytesSent", 0))
            recv = block.get("BytesReceivedHigh", block.get("TotalBytesReceived", 0))
            if sent: m[f"fritzbox.traffic.{zp}.bytes_sent"] = _safe_int(sent)
            if recv: m[f"fritzbox.traffic.{zp}.bytes_received"] = _safe_int(recv)
    except Exception as e:
        log.debug("LUA netCnt parse: %s", e)
    return m


def collect_lua_netdev(lua):
    """Per-device network info including WLAN signal from data.lua?page=netDev"""
    m = {}
    data = lua.data_lua("netDev")
    if not data: return m
    d = data.get("data", data)
    try:
        active = d.get("active", [])
        passive = d.get("passive", [])
        if isinstance(active, list): m["fritzbox.netdev.active_count"] = len(active)
        if isinstance(passive, list): m["fritzbox.netdev.passive_count"] = len(passive)

        if isinstance(active, list):
            for dev in active:
                if not isinstance(dev, dict): continue
                name = dev.get("name", "")
                if not name: continue
                sn = name.replace(" ","_").replace("/","_").replace(".","_").replace("-","_").lower()
                for k in ("speed","type","ip"):
                    v = dev.get(k, "")
                    if v: m[f"fritzbox.netdev.{sn}.{k}"] = v
                rssi = dev.get("rssi", "")
                if rssi:
                    try: m[f"fritzbox.netdev.{sn}.rssi"] = int(rssi)
                    except: pass
                if dev.get("guest"): m[f"fritzbox.netdev.{sn}.is_guest"] = 1
    except Exception as e:
        log.debug("LUA netDev parse: %s", e)
    return m


def collect_lua_overview(lua):
    """Provider info, USB devices, DECT info from data.lua?page=overview"""
    m = {}
    data = lua.data_lua("overview")
    if not data: return m
    d = data.get("data", data)
    try:
        inet = d.get("internet", d.get("inetStat", {}))
        if isinstance(inet, dict):
            for item in inet.get("txt", []):
                if isinstance(item, str) and len(item) > 3:
                    if "verbunden" in item.lower():
                        m["fritzbox.overview.connection_info"] = item
                    else:
                        m["fritzbox.overview.provider_info"] = item
        usb = d.get("usb", {})
        if isinstance(usb, dict):
            cnt = usb.get("count", 0)
            if cnt: m["fritzbox.overview.usb_devices"] = _safe_int(cnt)
    except Exception as e:
        log.debug("LUA overview parse: %s", e)
    return m


def collect_lua_energy(lua):
    """Energy consumption data from data.lua?page=energy"""
    m = {}
    data = lua.data_lua("energy")
    if not data: return m
    d = data.get("data", data)
    try:
        drains = d.get("drain", [])
        if isinstance(drains, list):
            for dev in drains:
                if not isinstance(dev, dict): continue
                name = dev.get("name", "")
                if not name: continue
                sn = name.replace(" ","_").replace("/","_").lower()
                act = dev.get("actCycle", "")
                if act: m[f"fritzbox.energy.{sn}.active_cycle"] = act
    except Exception as e:
        log.debug("LUA energy parse: %s", e)
    return m


def collect_lua_dsl_detail(lua):
    """Extended DSL stats from data.lua?page=dslStat not available via TR-064"""
    m = {}
    data = lua.data_lua("dslStat")
    if not data: return m
    d = data.get("data", data)
    try:
        line = d.get("line", d.get("atur", {}))
        if isinstance(line, dict):
            for k in ("snr","attn","capacity","crcPerMin","errSeconds","sevErrSeconds","lossOfSignal","lossOfFrame"):
                for direction in ("ds","us"):
                    v = line.get(f"{direction}_{k}", line.get(f"{direction}{k}"))
                    if v is not None:
                        m[f"fritzbox.dsl_detail.{direction}.{k}"] = v
    except Exception as e:
        log.debug("LUA dslStat parse: %s", e)
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
            "total_incoming": 0, "total_outgoing": 0, "total_missed": 0,
            "total_answered": 0, "current_active": 0,
            "last_caller": "", "last_called": "",
            "last_event_type": "", "last_event_time": "", "available": 0,
        }

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        log.info("Callmonitor: Listener started for %s:%d", self.host, self.port)

    def stop(self):
        self.running = False
        if self.sock:
            try: self.sock.close()
            except: pass

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
                        if line: self._parse(line)
            except socket.timeout:
                continue
            except Exception as e:
                log.debug("Callmonitor: Error: %s", e)
                with self.lock: self.stats["available"] = 0
            if self.running:
                log.info("Callmonitor: Reconnecting in 30s...")
                time.sleep(30)

    def _connect(self):
        if self.sock:
            try: self.sock.close()
            except: pass
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(120)
        self.sock.connect((self.host, self.port))
        log.info("Callmonitor: Connected to %s:%d", self.host, self.port)
        with self.lock: self.stats["available"] = 1

    def _parse(self, line):
        try:
            parts = line.split(";")
            if len(parts) < 4: return
            ts, etype, cid = parts[0], parts[1], parts[2]
            with self.lock:
                self.stats["last_event_type"] = etype
                self.stats["last_event_time"] = ts
                if etype == "RING":
                    caller = parts[3] if len(parts) > 3 else ""
                    called = parts[4] if len(parts) > 4 else ""
                    self.active_calls[cid] = {"type":"in","caller":caller,"connected":False}
                    self.stats["total_incoming"] += 1
                    self.stats["last_caller"] = caller
                    log.info("Callmonitor: RING from %s", caller)
                elif etype == "CALL":
                    called = parts[4] if len(parts) > 4 else ""
                    self.active_calls[cid] = {"type":"out","called":called,"connected":False}
                    self.stats["total_outgoing"] += 1
                    self.stats["last_called"] = called
                    log.info("Callmonitor: CALL to %s", called)
                elif etype == "CONNECT":
                    if cid in self.active_calls: self.active_calls[cid]["connected"] = True
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
def send_to_zabbix(metrics):
    if not metrics or not ZABBIX_SERVER: return False
    lines = []
    for k, v in metrics.items():
        if v is None: continue
        if isinstance(v, bool): v = 1 if v else 0
        v = str(v).replace("\n", " | ").replace("\r", "").replace("\t", " ").strip()
        if not v: continue
        lines.append(f"{FRITZBOX_HOSTNAME} {k} {v}")
    if not lines: return False
    log.info("Sending %d metrics to Zabbix %s:%s", len(lines), ZABBIX_SERVER, ZABBIX_SERVER_PORT)
    if DEBUG:
        for line in lines:
            log.debug("  >> %s", line)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write("\n".join(lines) + "\n")
        tmp = f.name
    try:
        cmd = ["zabbix_sender","-z",ZABBIX_SERVER,"-p",ZABBIX_SERVER_PORT,"-i",tmp]
        if TLS_PSK_IDENTITY and TLS_PSK:
            cmd += ["--tls-connect","psk","--tls-psk-identity",TLS_PSK_IDENTITY,"--tls-psk",TLS_PSK]
        if DEBUG: cmd.append("-vv")
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if DEBUG and (r.stdout.strip() or r.stderr.strip()):
            for line in (r.stdout + r.stderr).splitlines():
                log.debug("  zabbix_sender: %s", line)
        if r.returncode == 0:
            log.info("Zabbix sender: %s", r.stdout.strip())
            return True
        log.error("Zabbix sender failed (rc=%d): %s %s", r.returncode, r.stdout.strip(), r.stderr.strip())
        return False
    except Exception as e:
        log.error("zabbix_sender error: %s", e)
        return False
    finally:
        try: os.unlink(tmp)
        except: pass


# ===================================================================
# Main
# ===================================================================
def collect_all(fc, lua, cmon):
    m = {"fritzbox.collector.status": 1, "fritzbox.collector.timestamp": int(time.time())}
    for name, fn in [("device",collect_device_info),("wan",collect_wan_info),("dsl",collect_dsl_info),
                     ("wlan",collect_wlan_info),("lan",collect_lan_info),("homeauto",collect_homeauto_info),
                     ("update",collect_update_info),("services",collect_service_info)]:
        try:
            r = fn(fc); m.update(r)
            log.debug("TR-064 %s: %d items", name, len(r))
        except Exception as e:
            log.warning("TR-064 %s: %s", name, e)
    if lua:
        for name, fn in [("system",collect_lua_system),("traffic",collect_lua_traffic),
                         ("netdev",collect_lua_netdev),("overview",collect_lua_overview),
                         ("energy",collect_lua_energy),("dsl_detail",collect_lua_dsl_detail)]:
            try:
                r = fn(lua); m.update(r)
                log.debug("LUA %s: %d items", name, len(r))
            except Exception as e:
                log.warning("LUA %s: %s", name, e)
    if cmon:
        try: m.update(cmon.get_metrics())
        except: pass
    return m


def main():
    interval = parse_interval(INTERVAL)
    log.info("FritzBox Zabbix Monitor v2.1 (interval=%ds)", interval)
    log.info("Target: %s@%s:%d | Zabbix: %s:%s | Host: %s",
             FRITZBOX_USER, FRITZBOX_IP, FRITZBOX_PORT, ZABBIX_SERVER, ZABBIX_SERVER_PORT, FRITZBOX_HOSTNAME)
    log.info("Features: TR-064=yes LUA=%s Callmonitor=%s", ENABLE_LUA, ENABLE_CALLMONITOR)

    if not FRITZBOX_USER or not FRITZBOX_PASSWD:
        log.error("FRITZBOX_USER and FRITZBOX_PASSWD required"); sys.exit(1)
    if not ZABBIX_SERVER:
        log.error("ZABBIX_SERVER required"); sys.exit(1)

    try:
        fc = FritzConnection(address=FRITZBOX_IP, user=FRITZBOX_USER, password=FRITZBOX_PASSWD,
                             port=FRITZBOX_PORT, use_tls=FRITZBOX_USE_TLS, timeout=10)
        log.info("TR-064: Connected to %s (FritzOS %s)", fc.modelname, fc.system_version)
    except Exception as e:
        log.error("TR-064 connection failed: %s", e); sys.exit(1)

    lua = None
    if ENABLE_LUA:
        try:
            lua = FritzBoxLUA(FRITZBOX_IP, FRITZBOX_USER, FRITZBOX_PASSWD, FRITZBOX_USE_TLS)
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
            metrics = collect_all(fc, lua, cmon)
            log.info("Collected %d metrics", len(metrics))
            if DEBUG:
                for k, v in sorted(metrics.items()): log.debug("  %s = %s", k, v)
            send_to_zabbix(metrics)
        except Exception as e:
            log.error("Main loop: %s", e)
            try: fc = FritzConnection(address=FRITZBOX_IP, user=FRITZBOX_USER, password=FRITZBOX_PASSWD,
                                      port=FRITZBOX_PORT, use_tls=FRITZBOX_USE_TLS, timeout=10)
            except: pass
        time.sleep(max(1, interval - (time.time() - t0)))


if __name__ == "__main__":
    main()
