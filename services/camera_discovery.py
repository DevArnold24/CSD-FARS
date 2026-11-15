"""Bounded, read-only camera discovery on a current local IPv4 adapter.

Discovery is not a connection or credential test. Device responses are untrusted;
only correlated replies and a small metadata allowlist leave this module.
"""

import ipaddress
import re
import select
import socket
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from urllib.parse import unquote, urlsplit

import psutil


DISCOVERY_SECONDS = 3.0
MAX_PACKETS = 128
MAX_DEVICES = 64
MAX_PACKET_BYTES = 32768
MULTICAST_GROUP = "239.255.255.250"
_DISCOVERY_LOCK = threading.Lock()
_VIRTUAL_NAME = re.compile(
    r"virtual|vethernet|vmware|vbox|hyper-v|vpn|loopback|docker|wsl|"
    r"(?:^|[\s_-])(?:tap|tun|tailscale|zerotier|wireguard)(?:[\s_\d-]|$)",
    re.IGNORECASE,
)
_DISCOVERY_NAMESPACES = (
    "http://schemas.xmlsoap.org/ws/2005/04/discovery",
    "http://docs.oasis-open.org/ws-dd/ns/discovery/2009/01",
)
_ADDRESSING_NAMESPACES = (
    "http://schemas.xmlsoap.org/ws/2004/08/addressing",
    "http://www.w3.org/2005/08/addressing",
)


class DiscoveryUnavailable(RuntimeError):
    """The host could not perform the requested discovery operation."""


class DiscoveryBusy(RuntimeError):
    """Another guided discovery operation is already running."""


class InvalidDiscoveryInterface(ValueError):
    """The selected IPv4 address is not a currently usable local adapter."""


def _ipv4(value):
    try:
        address = ipaddress.IPv4Address(str(value))
    except (ValueError, TypeError):
        return None
    if address.is_unspecified or address.is_loopback or address.is_multicast:
        return None
    if int(address) == 0xFFFFFFFF:
        return None
    return address


def list_discovery_interfaces():
    """List active adapters without asking DNS or opening a network socket."""
    try:
        addresses = psutil.net_if_addrs()
        stats = psutil.net_if_stats()
    except (OSError, RuntimeError) as exc:
        raise DiscoveryUnavailable from exc
    interfaces = []
    seen = set()
    for name, assigned in addresses.items():
        if name not in stats or not stats[name].isup:
            continue
        for item in assigned:
            if item.family != socket.AF_INET:
                continue
            address = _ipv4(item.address)
            if address is None or str(address) in seen:
                continue
            try:
                network = ipaddress.IPv4Network(f"{address}/{item.netmask}", strict=False)
            except (ValueError, TypeError):
                continue
            if network.prefixlen < 31 and address in (network.network_address, network.broadcast_address):
                continue
            seen.add(str(address))
            interfaces.append({
                "id": str(address),
                "name": str(name)[:100],
                "address": str(address),
                "netmask": str(network.netmask),
                "likely_virtual": bool(_VIRTUAL_NAME.search(name)),
                "recommended": False,
            })
    # Names are a preference hint, not proof of hardware. A user can explicitly
    # choose any current adapter; a recognizable VPN/virtual adapter is never auto-selected.
    interfaces.sort(key=lambda item: (
        item["likely_virtual"],
        ipaddress.IPv4Address(item["address"]).is_link_local,
        0 if "ethernet" in item["name"].lower() else 1,
        item["name"].casefold(),
        int(ipaddress.IPv4Address(item["address"])),
    ))
    if interfaces and not interfaces[0]["likely_virtual"]:
        interfaces[0]["recommended"] = True
    return interfaces


def select_discovery_interface(interfaces, requested=None):
    if requested is not None:
        for item in interfaces:
            if item["id"] == requested:
                return item
        raise InvalidDiscoveryInterface
    return next((item for item in interfaces if item["recommended"]), None)


def _safe_text(value, limit=120):
    return " ".join("".join(c for c in str(value or "") if c.isprintable()).split())[:limit]


def _xml(data):
    if not data or len(data) > MAX_PACKET_BYTES:
        return None
    upper = data.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper or b"\x00" in data:
        return None
    try:
        return ET.fromstring(data)
    except (ET.ParseError, ValueError, LookupError):
        return None


def _matches_id(value, expected):
    try:
        return uuid.UUID(str(value).strip().lower().removeprefix("urn:uuid:")) == uuid.UUID(expected)
    except (ValueError, AttributeError):
        return False


def _port(value):
    try:
        port = int(value)
    except (ValueError, TypeError):
        return None
    return port if 1 <= port <= 65535 else None


def _device(ip, model, protocol, interface, netmask=None, http_port=None, activated=None):
    address = _ipv4(ip)
    if address is None:
        return None
    desktop_network = ipaddress.IPv4Network(
        f"{interface['address']}/{interface['netmask']}", strict=False,
    )
    on_same_network = address in desktop_network
    validated_mask = None
    if netmask:
        try:
            camera_network = ipaddress.IPv4Network(f"{address}/{netmask}", strict=False)
            validated_mask = str(camera_network.netmask)
            on_same_network = on_same_network and ipaddress.IPv4Address(interface["address"]) in camera_network
        except (ValueError, TypeError):
            pass
    model = _safe_text(model) or "Network camera"
    return {
        "ip": str(address),
        "model": model,
        "name": f"Camera ({model})",
        "protocols": [protocol],
        "netmask": validated_mask,
        "http_port": _port(http_port),
        "activated": activated,
        "network_status": "same_network" if on_same_network else "different_network",
    }


def parse_sadp_reply(data, probe_id, interface):
    root = _xml(data)
    if root is None or root.tag != "ProbeMatch" or not _matches_id(root.findtext("Uuid"), probe_id):
        return None
    activation = {"true": True, "false": False, "1": True, "0": False}.get(
        str(root.findtext("Activated") or "").strip().lower(),
    )
    return _device(
        root.findtext("IPv4Address"), root.findtext("DeviceDescription"), "Hikvision", interface,
        netmask=root.findtext("IPv4SubnetMask"), http_port=root.findtext("HttpPort"), activated=activation,
    )


def parse_onvif_reply(data, sender_ip, probe_id, interface):
    root = _xml(data)
    if root is None or root.tag not in (
        "{http://www.w3.org/2003/05/soap-envelope}Envelope",
        "{http://schemas.xmlsoap.org/soap/envelope/}Envelope",
    ):
        return None
    soap_namespace = root.tag.split("}", 1)[0] + "}"
    header = root.find(f"{soap_namespace}Header")
    body = root.find(f"{soap_namespace}Body")
    if header is None or body is None or not any(
        _matches_id(header.findtext(f"{{{namespace}}}RelatesTo"), probe_id)
        for namespace in _ADDRESSING_NAMESPACES
    ):
        return None
    for namespace in _DISCOVERY_NAMESPACES:
        matches = body.find(f"{{{namespace}}}ProbeMatches")
        if matches is None:
            continue
        match = matches.find(f"{{{namespace}}}ProbeMatch")
        if match is None:
            continue
        model = None
        for scope in str(match.findtext(f"{{{namespace}}}Scopes") or "").split():
            if scope.startswith("onvif://www.onvif.org/hardware/"):
                model = unquote(scope.split("/hardware/", 1)[1])
                break
        http_port = None
        for url in str(match.findtext(f"{{{namespace}}}XAddrs") or "").split():
            try:
                parsed = urlsplit(url)
                if parsed.scheme in ("http", "https") and parsed.hostname == sender_ip and not parsed.username:
                    http_port = parsed.port or (443 if parsed.scheme == "https" else 80)
                    break
            except ValueError:
                continue
        return _device(sender_ip, model, "ONVIF", interface, http_port=http_port)
    return None


def _probe(protocol, probe_id):
    if protocol == "Hikvision":
        return (
            '<?xml version="1.0" encoding="utf-8"?>'
            f"<Probe><Uuid>{probe_id.upper()}</Uuid><Types>inquiry</Types></Probe>"
        ).encode("utf-8")
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
        'xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
        'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery">'
        '<s:Header><a:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</a:Action>'
        f'<a:MessageID>urn:uuid:{probe_id}</a:MessageID>'
        '<a:ReplyTo><a:Address>http://schemas.xmlsoap.org/ws/2004/08/addressing/role/anonymous</a:Address></a:ReplyTo>'
        '<a:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</a:To></s:Header>'
        '<s:Body><d:Probe><d:Types xmlns:dp0="http://www.onvif.org/ver10/network/wsdl">'
        'dp0:NetworkVideoTransmitter</d:Types></d:Probe></s:Body></s:Envelope>'
    ).encode("utf-8")


def _open_probe_socket(protocol, interface, probe_id):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        address = interface["address"]
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(address))
        port = 37020 if protocol == "Hikvision" else 3702
        if protocol == "Hikvision":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.bind(("", port))
            sock.setsockopt(
                socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                socket.inet_aton(MULTICAST_GROUP) + socket.inet_aton(address),
            )
        else:
            sock.bind((address, 0))
        sock.setblocking(False)
        message = _probe(protocol, probe_id)
        sock.sendto(message, (MULTICAST_GROUP, port))
        if protocol == "Hikvision":
            network = ipaddress.IPv4Network(f"{address}/{interface['netmask']}", strict=False)
            if network.prefixlen < 31:
                # The broadcast is calculated only from the validated local adapter.
                sock.sendto(message, (str(network.broadcast_address), port))
        return sock
    except OSError:
        _close_socket(sock)
        raise


def _close_socket(sock):
    try:
        sock.close()
    except OSError:
        pass


def discover_cameras(requested_interface=None):
    """Send two discovery protocols, without scanning hosts or changing settings."""
    interfaces = list_discovery_interfaces()
    interface = select_discovery_interface(interfaces, requested_interface)
    if interface is None:
        return {
            "interface": None, "interfaces": interfaces, "devices": [],
            "warnings": ["no_local_network"], "network_changes_made": False,
        }
    if not _DISCOVERY_LOCK.acquire(blocking=False):
        raise DiscoveryBusy
    sockets = {}
    warnings = []
    devices = {}
    probe_id = str(uuid.uuid4())
    deadline = time.monotonic() + DISCOVERY_SECONDS
    try:
        for protocol in ("ONVIF", "Hikvision"):
            try:
                sockets[_open_probe_socket(protocol, interface, probe_id)] = protocol
            except OSError:
                warnings.append(f"{protocol.lower()}_unavailable")
        if not sockets:
            raise DiscoveryUnavailable
        packets = 0
        while sockets and packets < MAX_PACKETS and len(devices) < MAX_DEVICES:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                readable, _, _ = select.select(list(sockets), [], [], remaining)
            except OSError as exc:
                raise DiscoveryUnavailable from exc
            if not readable:
                break
            for sock in readable:
                if packets >= MAX_PACKETS or len(devices) >= MAX_DEVICES:
                    break
                protocol = sockets[sock]
                try:
                    data, sender = sock.recvfrom(MAX_PACKET_BYTES + 1)
                except BlockingIOError:
                    continue
                except OSError:
                    warnings.append(f"{protocol.lower()}_unavailable")
                    _close_socket(sock)
                    del sockets[sock]
                    continue
                packets += 1
                if not isinstance(sender, tuple) or not sender or _ipv4(sender[0]) is None:
                    continue
                device = (
                    parse_sadp_reply(data, probe_id, interface) if protocol == "Hikvision"
                    else parse_onvif_reply(data, str(sender[0]), probe_id, interface)
                )
                if device is None:
                    continue
                old = devices.get(device["ip"])
                if old:
                    combined = sorted(set(old["protocols"] + device["protocols"]))
                    if protocol == "Hikvision":
                        devices[device["ip"]] = device
                    devices[device["ip"]]["protocols"] = combined
                else:
                    devices[device["ip"]] = device
        if not sockets and not devices:
            raise DiscoveryUnavailable
        return {
            "interface": interface, "interfaces": interfaces,
            "devices": sorted(devices.values(), key=lambda item: int(ipaddress.IPv4Address(item["ip"]))),
            "warnings": sorted(set(warnings)), "network_changes_made": False,
        }
    finally:
        for sock in sockets:
            _close_socket(sock)
        _DISCOVERY_LOCK.release()
