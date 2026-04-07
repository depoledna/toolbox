import re


def parse_nmap(raw: str) -> dict:
    """Parse nmap text output into structured data.

    Returns dict with:
      - hosts: list of {ip, mac, vendor, os, ports: [{port, state, service, version}]}
      - summary: {total, up, down}

    Usage:
      result = library.parse_nmap(nmap_output)
      for host in result['hosts']:
          print(host['ip'], host['vendor'], len(host['ports']), 'open ports')
    """
    hosts = []
    current_host = None

    for line in raw.splitlines():
        line = line.strip()

        # Host line: "Nmap scan report for hostname (ip)" or "Nmap scan report for ip"
        host_match = re.match(
            r"Nmap scan report for (?:(.+?) \((.+?)\)|(.+?))\s*$", line
        )
        if host_match:
            if current_host is not None:
                hosts.append(current_host)
            hostname = host_match.group(1) or ""
            ip = host_match.group(2) or host_match.group(3) or ""
            current_host = {
                "ip": ip,
                "hostname": hostname,
                "mac": "",
                "vendor": "",
                "os": "",
                "ports": [],
            }
            continue

        if current_host is None:
            continue

        # MAC line: "MAC Address: AA:BB:CC:DD:EE:FF (Vendor Name)"
        mac_match = re.match(
            r"MAC Address:\s+([0-9A-Fa-f:]+)(?:\s+\((.+?)\))?", line
        )
        if mac_match:
            current_host["mac"] = mac_match.group(1)
            current_host["vendor"] = mac_match.group(2) or ""
            continue

        # OS line: "OS details: ..." or "Running: ..."
        os_match = re.match(r"(?:OS details|Running):\s+(.+)", line)
        if os_match:
            current_host["os"] = os_match.group(1)
            continue

        # Port line: "80/tcp   open  http    Apache httpd 2.4.41"
        port_match = re.match(
            r"(\d+)/(tcp|udp)\s+(open|closed|filtered)\s+(\S+)(?:\s+(.+))?", line
        )
        if port_match:
            current_host["ports"].append(
                {
                    "port": int(port_match.group(1)),
                    "proto": port_match.group(2),
                    "state": port_match.group(3),
                    "service": port_match.group(4),
                    "version": (port_match.group(5) or "").strip(),
                }
            )
            continue

    if current_host is not None:
        hosts.append(current_host)

    # Parse summary line: "Nmap done: X IP addresses (Y hosts up)"
    total, up = 0, 0
    summary_match = re.search(
        r"(\d+)\s+IP address(?:es)?\s+\((\d+)\s+hosts?\s+up\)", raw
    )
    if summary_match:
        total = int(summary_match.group(1))
        up = int(summary_match.group(2))

    return {
        "hosts": hosts,
        "summary": {"total": total, "up": up, "down": total - up},
    }
