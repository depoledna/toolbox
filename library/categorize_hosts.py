def categorize_hosts(hosts: list[dict]) -> dict[str, list[dict]]:
    """Categorize network hosts by device type based on MAC vendor and services.

    Input: list of host dicts (from parse_nmap output), each with:
      - ip, mac, vendor, os, ports (list of {port, service, version})

    Returns dict with category keys:
      - infrastructure, iot, cameras, servers, workstations, printers, unknown

    Usage:
      nmap_data = library.parse_nmap(scan_output)
      categories = library.categorize_hosts(nmap_data['hosts'])
      for cat, devices in categories.items():
          print(f"{cat}: {len(devices)} devices")
    """
    categories = {
        "infrastructure": [],
        "iot": [],
        "cameras": [],
        "servers": [],
        "workstations": [],
        "printers": [],
        "unknown": [],
    }

    for host in hosts:
        category = _classify(host)
        categories[category].append(host)

    return categories


# ── Vendor keyword mappings ──

_INFRA_VENDORS = [
    "ubiquiti", "cisco", "mikrotik", "routerboard", "netgear", "aruba",
    "fortinet", "juniper", "tp-link", "d-link", "zyxel", "linksys",
    "draytek", "peplink", "meraki", "sonicwall", "watchguard", "pfsense",
]

_IOT_VENDORS = [
    "espressif", "tuya", "meross", "shelly", "sonoff", "aqara", "lumi",
    "xiaomi", "broadlink", "wemo", "belkin", "ecobee", "nest", "ring",
    "wyze", "smartthings", "zigbee", "ewelink",
]

_CAMERA_VENDORS = [
    "dahua", "hikvision", "reolink", "axis", "amcrest", "foscam",
    "vivotek", "hanwha", "uniview", "geovision", "lorex",
]

_PRINTER_VENDORS = [
    "hp", "hewlett", "canon", "brother", "epson", "lexmark", "xerox",
    "ricoh", "kyocera", "samsung print", "konica",
]

_WORKSTATION_VENDORS = [
    "apple", "dell", "lenovo", "asus", "acer", "microsoft",
    "intel corporate", "realtek",
]

# Ports that strongly indicate a device category
_CAMERA_PORTS = {554, 37777, 34567, 8554}
_PRINTER_PORTS = {631, 9100, 515}
_INFRA_PORTS = {8291, 8728, 161}
_IOT_PORTS = {1883, 8883}


def _classify(host: dict) -> str:
    vendor = (host.get("vendor") or "").lower()
    os_info = (host.get("os") or "").lower()
    open_ports = {
        p["port"]
        for p in host.get("ports", [])
        if p.get("state") == "open"
    }
    services = " ".join(
        f"{p.get('service', '')} {p.get('version', '')}"
        for p in host.get("ports", [])
    ).lower()

    # Vendor-based classification (highest confidence)
    if _match_any(vendor, _CAMERA_VENDORS):
        return "cameras"
    if _match_any(vendor, _IOT_VENDORS):
        return "iot"
    if _match_any(vendor, _INFRA_VENDORS):
        return "infrastructure"
    if _match_any(vendor, _PRINTER_VENDORS):
        return "printers"

    # Port-based classification
    if open_ports & _CAMERA_PORTS:
        return "cameras"
    if open_ports & _PRINTER_PORTS:
        return "printers"
    if open_ports & _INFRA_PORTS:
        return "infrastructure"
    if open_ports & _IOT_PORTS:
        return "iot"

    # Service/OS-based classification
    if any(kw in services for kw in ["rtsp", "onvif"]):
        return "cameras"
    if any(kw in services for kw in ["cups", "ipp", "printer"]):
        return "printers"
    if any(kw in services for kw in ["routeros", "cisco ios", "ubnt"]):
        return "infrastructure"
    if any(kw in services for kw in ["mqtt", "coap"]):
        return "iot"

    # OS-based fallback
    if any(kw in os_info for kw in ["linux", "freebsd", "ubuntu", "debian", "raspbian"]):
        return "servers"
    if any(kw in os_info for kw in ["windows"]):
        return "workstations"
    if _match_any(vendor, _WORKSTATION_VENDORS):
        return "workstations"

    # Server heuristics: SSH + other services
    if 22 in open_ports and len(open_ports) > 1:
        return "servers"

    return "unknown"


def _match_any(text: str, keywords: list[str]) -> bool:
    return any(kw in text for kw in keywords)
