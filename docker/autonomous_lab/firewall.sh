#!/bin/sh
# Fixed-code trusted controller helper. No worker code, mounts, sockets or API.
set -eu
iptables -P OUTPUT DROP
iptables -P INPUT DROP
iptables -P FORWARD DROP
ip6tables -P OUTPUT DROP
ip6tables -P INPUT DROP
ip6tables -P FORWARD DROP
iptables -F OUTPUT
iptables -F INPUT
iptables -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
# Deny private/special destinations before any provider allow rule.
for cidr in 0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8 169.254.0.0/16 172.16.0.0/12 192.168.0.0/16 198.18.0.0/15 224.0.0.0/4 240.0.0.0/4; do
    iptables -A OUTPUT -d "$cidr" -j REJECT
done
for ip in "$@"; do
    # Controller additionally validates global IPv4 provenance. No shell evaluation.
    case "$ip" in *[!0-9.]*|'') exit 2;; esac
    iptables -A OUTPUT -d "$ip/32" -p tcp --dport 443 -j ACCEPT
done
# DNS is intentionally denied: provider resolutions are controller-frozen /etc/hosts.
iptables-save
ip6tables-save
printf '\nLK_LAB_FIREWALL_READY\n' > /run/ready
exec sleep infinity
