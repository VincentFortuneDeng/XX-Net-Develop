#!/usr/bin/env python
# coding:utf-8

# public DNS servers https://en.wikipedia.org/wiki/Public_recursive_name_server

import errno
import json
import os
import sys
import threading
import socket
import time
import re
import ssl
import random
import struct
import subprocess

current_path = os.path.dirname(os.path.abspath(__file__))
root_path = os.path.abspath(os.path.join(current_path, os.pardir, os.pardir))

python_path = root_path
noarch_lib = os.path.join(python_path, 'lib', 'noarch')
sys.path.append(noarch_lib)

import env_info
data_path = os.path.join(env_info.data_path, 'smart_router')

from queue import Queue, Empty
import lru_cache
import utils
import simple_http_client
from dnslib import DNSRecord, DNSHeader, A, AAAA, RR, DNSQuestion, QTYPE

from . import global_var as g
from . import metrics

from xlog import getLogger
xlog = getLogger("smart_router")


def _normalize_dns_name(name):
    return utils.to_str(name).rstrip(".").lower()


def _match_dns_response(response, query_id, domain, dns_type):
    if not response.header.qr or response.header.id != query_id:
        return False
    if len(response.questions) != 1:
        return False

    question = response.questions[0]
    return (_normalize_dns_name(question.qname) == _normalize_dns_name(domain) and
            question.qtype == dns_type)


class _DnsAlias:
    def __init__(self, target):
        self.target = target


def _parse_response_ips(response, domain, dns_type):
    qname = _normalize_dns_name(domain)
    valid_owners = {qname}
    cname_by_owner = {}
    for record in response.rr:
        if record.rtype == QTYPE.CNAME:
            cname_by_owner[_normalize_dns_name(record.rname)] = utils.to_bytes(str(record.rdata))

    alias = None
    current = qname
    seen = set()
    while current in cname_by_owner and current not in seen:
        seen.add(current)
        alias = cname_by_owner[current]
        current = _normalize_dns_name(alias)
        valid_owners.add(current)
    if current in seen:
        alias = None

    ips = []
    for record in response.rr:
        if record.rtype != dns_type or _normalize_dns_name(record.rname) not in valid_owners:
            continue
        value = utils.to_bytes(str(record.rdata))
        if dns_type in (QTYPE.A, QTYPE.AAAA, QTYPE.NS) or utils.check_ip_valid(value):
            ips.append(value)

    return ips, alias


def _extract_response_ips(response, domain, dns_type, recursive_query):
    ips, alias = _parse_response_ips(response, domain, dns_type)
    if ips or alias is None:
        return ips

    # TCP/DoH 在各自调用线程跟随别名；UDP 接收线程只交付 _DnsAlias。
    return recursive_query(alias, dns_type)

def get_local_ips():
    def get_ip_address(NICname):
        import fcntl
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        return socket.inet_ntoa(fcntl.ioctl(
            s.fileno(),
            0x8915,  # SIOCGIFADDR
            struct.pack('256s', NICname[:15].encode("UTF-8"))
        )[20:24])

    if sys.platform.startswith("linux"):

        if os.path.isfile("/system/bin/dalvikvm") or os.path.isfile("/system/bin/dalvikvm64") or \
                "android.googlesource.com" in sys.version:
            ips = [b'127.0.0.1']
        else:
            try:
                proc = subprocess.Popen(["ip addr show | egrep inet | awk '{{print $2}}' | awk -F'/' '{{print $1}}'"],
                                        stdout=subprocess.PIPE, shell=True)
                x = proc.communicate()[0]
                ips = x.strip().split(b"\n")
            except Exception as e:
                xlog.warn("get ip address e:%r", e)
                ips = [b'127.0.0.1']
    elif sys.platform == "darwin":
        try:
            proc = subprocess.Popen(["ifconfig | egrep inet | awk '{{print $2}}' | awk -F'/' '{{print $1}}'"],
                                    stdout=subprocess.PIPE, shell=True)
            x = proc.communicate()[0]
            ips = x.strip().split(b"\n")
        except Exception as e:
            xlog.warn("get ip address e:%r", e)
            ips = [b'127.0.0.1']
    elif sys.platform == "ios":
        ips = [b'127.0.0.1']
    elif sys.platform == "win32":
        try:
            ips = [ip for ip in socket.gethostbyname_ex(socket.gethostname())[2]]
            ips = utils.to_bytes(ips)
            if b"127.0.0.1" not in ips:
                ips.append(b"127.0.0.1")
        except Exception as e:
            xlog.warn("get local ip fail:%r", e)
            ips = [b"127.0.0.1"]
    else:
        ips = []
        try:
            for ix in socket.if_nameindex():
                name = ix[1]
                ip = get_ip_address(name)
                ips.append(ip)
        except Exception as e:
            xlog.warn("get ip address e:%r", e)
            ips = [b'127.0.0.1']

    xlog.debug("local ips: %s", ips)
    return ips


def query_dns_from_xxnet(domain, dns_type=None):
    if not g.x_tunnel:
        return []

    t0 = time.time()
    content, status, response = g.x_tunnel.front_dispatcher.request(
        "GET", "dns.xx-net.org", path="/query?domain=%s" % (utils.to_str(domain)), timeout=5)
    t1 = time.time()

    if status != 200:
        xlog.warn("query_dns_from_xxnet fail status:%d, cost=%f", status, t1 - t0)
        return []

    if isinstance(content, memoryview):
        content = content.tobytes()

    content = utils.to_str(content)

    try:
        rs = json.loads(content)
        ips = rs["ip"]
        xlog.debug("query_dns_from_xxnet %s cost:%f return:%s", domain, t1 - t0, ips)
        #if dns_type == 1:
        #    ips = [ip for ip in ips if "." in ip]
        ips_out = []
        for ip_cn in ips:
            ip, cn = ip_cn.split("|")
            if dns_type == QTYPE.A and not utils.check_ip_valid4(ip):
                continue
            if dns_type == QTYPE.AAAA and not utils.check_ip_valid6(ip):
                continue
            ips_out.append(ip)
        return ips_out
    except Exception as e:
        xlog.warn("query_dns_from_xxnet %s json:%s parse fail:%s", domain, content, e)
        return []


class LocalDnsQuery():
    def __init__(self, timeout=3):
        self.timeout = timeout
        self.waiters = {}
        self.waiters_lock = threading.Lock()
        self.dns_server = self.get_local_dns_server()
        self.ns_backoff = {}
        self.ns_backoff_lock = threading.Lock()

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock6 = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        self.sock.settimeout(1)
        self.sock6.settimeout(1)

        self.running = True
        self.th = threading.Thread(target=self.dns_recv_worker, args=(self.sock,), name="dns_ipv4_receiver")
        self.th.start()

        self.th6 = threading.Thread(target=self.dns_recv_worker, args=(self.sock6,), name="dns_ipv6_receiver")
        self.th6.start()

    @staticmethod
    def _is_usable_nameserver(ip):
        # upstreams we can sendto() directly: plain IPv4 (except loopback), or
        # global-unicast IPv6 (2000::/3). Link-local (fe80::/10), ULA
        # (fc00::/7), ::1 and multicast IPv6 have no route via a plain UDP
        # sendto and only produce "No route to host" (errno 65) noise, e.g.
        # macOS resolv.conf "nameserver fe80::...%en0".
        if isinstance(ip, str):
            ip = ip.encode("ascii")
        if b":" in ip:
            try:
                packed = socket.inet_pton(socket.AF_INET6, ip.decode("ascii"))
                return packed[0] & 0xE0 == 0x20
            except (OSError, UnicodeError):
                return False
        try:
            packed = socket.inet_pton(socket.AF_INET, ip.decode("ascii"))
        except (OSError, UnicodeError):
            return False
        return (ip != b"127.0.0.1" and packed != b"\x00" * 4 and
                packed[:2] != b"\xa9\xfe" and packed[0] < 224)

    def get_local_dns_server(self):
        iplist = []
        if os.name == 'nt':
            import ctypes, ctypes.wintypes, struct, socket
            DNS_CONFIG_DNS_SERVER_LIST = 6
            buf = ctypes.create_string_buffer(2048)
            ctypes.windll.dnsapi.DnsQueryConfig(DNS_CONFIG_DNS_SERVER_LIST, 0, None, None, ctypes.byref(buf),
                                                ctypes.byref(ctypes.wintypes.DWORD(len(buf))))
            ipcount = struct.unpack('I', buf[0:4])[0]

            iplist = []
            for i in range(4, ipcount * 4 + 4, 4):
                ip = socket.inet_ntoa(buf[i:i + 4])
                iplist.append(ip)

        elif os.path.isfile('/etc/resolv.conf'):
            try:
                with open('/etc/resolv.conf', 'rb') as fp:
                    iplist = re.findall(br'(?m)^nameserver\s+(\S+)', fp.read())

                xlog.debug("DNS resolve servers:%s", iplist)

                local_ips = g.local_ips
                for ip in local_ips:
                    if ip in iplist:
                        xlog.warn("remove local DNS server %s from upstream", ip)
                        iplist.remove(ip)
            except Exception as e:
                xlog.warn("load /etc/resolv.conf fail:%r", e)

        out_list = []
        for ip in iplist:
            if self._is_usable_nameserver(ip):
                out_list.append(ip)
                xlog.info("Local DNS server:%s", ip)
            else:
                xlog.info("skip unusable DNS server:%s", ip)

        if not out_list:
            # fail-safe: nothing usable from the system config (e.g. only a
            # link-local IPv6 nameserver), fall back to public DNS.
            if g.config.country_code == "CN":
                out_list = [
                    b"114.114.114.114",
                    b"114.114.115.115",
                    b"119.29.29.29",
                    b"182.254.118.118",
                    b"223.5.5.5",
                    b"223.6.6.6",
                    b"180.76.76.76"
                ]
            else:
                out_list = [
                    b"1.1.1.1",
                    b"8.8.8.8",
                    b"9.9.9.9",
                    b"208.67.222.222",
                    b"168.126.63.2"
                ]

        return out_list

    def stop(self):
        self.running = False
        self.sock.close()

    def dns_recv_worker(self, sock):
        while self.running:
            try:
                try:
                    response, server = sock.recvfrom(8192)
                    server, port = server[:2]
                except Exception as e:
                    # xlog.exception("sock.recvfrom except:%r", e)
                    continue

                if not response:
                    continue
                if port != 53 or utils.to_bytes(server) not in [utils.to_bytes(ip) for ip in self.dns_server]:
                    xlog.warn("ignore DNS response from unconfigured source:%s port:%s", server, port)
                    continue

                try:
                    p = DNSRecord.parse(response)
                except Exception as e:
                    xlog.exception("dns client parse response fail:%r", e)
                    continue

                id = p.header.id
                with self.waiters_lock:
                    que = self.waiters.get(id)
                if que is None:
                    continue

                org_domain = que.domain
                dns_type = que.dns_type
                if not _match_dns_response(p, id, org_domain, dns_type):
                    xlog.warn("received unmatched DNS response from:%s", server)
                    continue

                domain = str(p.questions[0].qname)
                xlog.debug("DNS local query received %s from:%s domain:%s org:%s", len(p.rr), server, domain, org_domain)
                ips, alias = _parse_response_ips(p, org_domain, dns_type)
                if ips:
                    result = ips
                elif alias is not None:
                    result = _DnsAlias(alias)
                else:
                    continue

                # 只有仍持有该 waiter 的接收线程才能交付并消费这次查询。
                with self.waiters_lock:
                    if self.waiters.get(id) is not que:
                        continue
                    del self.waiters[id]
                que.put(result)
            except Exception as e:
                xlog.exception("dns recv_worker except:%r", e)

        xlog.info("DNS Client recv worker exit.")
        sock.close()

    def send_request(self, id, server_ip, domain, dns_type):
        try:
            d = DNSRecord(DNSHeader(id))
            d.add_question(DNSQuestion(domain, dns_type))
            req4_pack = d.pack()

            if utils.check_ip_valid4(server_ip):
                self.sock.sendto(req4_pack, (server_ip, 53))
            else:
                self.sock6.sendto(req4_pack, (server_ip, 53))
            with self.ns_backoff_lock:
                self.ns_backoff.pop(server_ip, None)
        except OSError as e:
            if e.errno not in (errno.EHOSTUNREACH, errno.ENETUNREACH):
                xlog.warn("send_request except:%r", e)
                return

            now = time.time()
            with self.ns_backoff_lock:
                cooldown_until = self.ns_backoff.get(server_ip, 0)
                if cooldown_until > now:
                    in_cooldown = True
                else:
                    in_cooldown = False
                    self.ns_backoff[server_ip] = now + 60
            if in_cooldown:
                xlog.debug("send_request %s except:%r during cooldown", server_ip, e)
            else:
                xlog.warn("send_request %s except:%r, cooldown for 60s", server_ip, e)
        except Exception as e:
            xlog.warn("send_request except:%r", e)

    def query_by_system(self, domain, dns_type):
        ips = []
        try:
            t0 = time.time()
            family = socket.AF_INET6 if dns_type == QTYPE.AAAA else socket.AF_INET
            addresses = socket.getaddrinfo(domain, None, family, socket.SOCK_STREAM)
            check_ip = utils.check_ip_valid6 if family == socket.AF_INET6 else utils.check_ip_valid4
            ips = list(dict.fromkeys(address[4][0] for address in addresses
                                     if check_ip(address[4][0])))
            t1 = time.time()

            xlog.debug("query_by_system, %s %d cost:%f, return:%s", domain, dns_type, t1 - t0, ips)
        except Exception as e:
            xlog.warn("query_by_system %s %d e:%r", domain, dns_type, e)

        return ips

    def query(self, domain, dns_type=1, timeout=3, history=None):
        normalized_domain = _normalize_dns_name(domain)
        if history is None:
            history = set()
        if normalized_domain in history or len(history) >= 16:
            return []
        history.add(normalized_domain)

        if sys.platform == "ios":
            return self.query_by_system(domain, dns_type)

        t0 = time.monotonic()
        end_time = t0 + timeout
        que = Queue()
        que.domain = domain
        que.dns_type = dns_type

        # 16 位 ID 的占用检查和登记必须原子完成。
        with self.waiters_lock:
            while True:
                id = random.randint(0, 65535)
                if id not in self.waiters:
                    self.waiters[id] = que
                    break

        now = time.time()
        candidates = []
        cooled_servers = []
        with self.ns_backoff_lock:
            for server_ip in self.dns_server:
                cooldown_until = self.ns_backoff.get(server_ip)
                if cooldown_until is not None and cooldown_until > now:
                    cooled_servers.append((server_ip, cooldown_until))
                else:
                    self.ns_backoff.pop(server_ip, None)
                    candidates.append(server_ip)
        if not candidates and cooled_servers:
            # 全部冷却时仅探测最早到期的一台，避免 DNS 完全中断或集中恢复。
            candidates = [min(cooled_servers, key=lambda item: item[1])[0]]

        for server_ip in candidates:
            new_time = time.monotonic()
            if new_time > end_time:
                break

            self.send_request(id, server_ip, domain, dns_type)

        try:
            ips = que.get(timeout=max(0, end_time - time.monotonic()))
        except Empty:
            metrics.mark_timeout()
            ips = []
        except:
            ips = []
        finally:
            with self.waiters_lock:
                if self.waiters.get(id) is que:
                    del self.waiters[id]

        if isinstance(ips, _DnsAlias):
            remaining = max(0, end_time - time.monotonic())
            if remaining <= 0:
                ips = []
            else:
                # 接收线程只交付别名事件，递归查询由原调用线程使用剩余预算执行。
                ips = self.query(ips.target, dns_type, remaining, history=history)

        if ips:
            ips = list(set(ips))

        t1 = time.monotonic()
        xlog.debug("query by udp, %s cost:%f, return:%s", domain, t1-t0, ips)

        return ips


class DnsOverTcpQuery():
    def __init__(self, server_list=[b"114.114.114.114"], port=53):
        self.protocol = "Tcp"
        self.timeout = 3
        self.connection_timeout = 60
        self.public_list = server_list
        self.port = port
        self.connections = []
        self.max_connections = 4
        self.running = True
        self.connections_lock = threading.Lock()

    def get_server(self):
        return random.choice(self.public_list)

    def direct_connect(self, host, port):
        connect_timeout = 30

        if b':' in host:
            info = [(socket.AF_INET6, socket.SOCK_STREAM, 0, "", (host, port, 0, 0))]
        elif utils.check_ip_valid4(host):
            info = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (host, port))]
        else:
            try:
                info = socket.getaddrinfo(host, port, socket.AF_UNSPEC,
                                          socket.SOCK_STREAM)
            except socket.gaierror:
                info = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (host, port))]

        for res in info:
            af, socktype, proto, canonname, sa = res
            s = None
            try:
                s = socket.socket(af, socktype, proto)

                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 32 * 1024)
                s.setsockopt(socket.SOL_TCP, socket.TCP_NODELAY, True)
                s.settimeout(connect_timeout)
                s.connect((host, port))
                return s
            except socket.error:
                if s:
                    try:
                        s.close()
                    except Exception:
                        pass
            except Exception as e:
                # direct path: socket() succeeded but a non-socket.error escaped
                # before connect; close s to avoid fd leak (same defense as Bug 4).
                if s:
                    try:
                        s.close()
                    except Exception:
                        pass
                xlog.warn("Connect to DNS server %s:%d fail:%r", host, port, e)

        return None

    def connect(self, host, port):
        if not g.config.PROXY_ENABLE:
            sock = self.direct_connect(host, self.port)
        else:
            connect_timeout = 5

            import socks

            sock = socks.socksocket(socket.AF_INET)
            sock.set_proxy(proxy_type=g.config.PROXY_TYPE,
                           addr=g.config.PROXY_HOST,
                           port=g.config.PROXY_PORT, rdns=True,
                           username=g.config.PROXY_USER,
                           password=g.config.PROXY_PASSWD)

            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 32*1024)
            sock.setsockopt(socket.SOL_TCP, socket.TCP_NODELAY, True)
            sock.settimeout(connect_timeout)

            try:
                sock.connect((host, self.port))
            except Exception:
                # proxy path: connect failed -> close sock then re-raise so the
                # caller-visible exception semantics stay unchanged, no fd leak.
                try:
                    sock.close()
                except Exception:
                    pass
                raise

        return sock

    def get_connection(self):
        while True:
            with self.connections_lock:
                if not self.running:
                    return None
                if not self.connections:
                    break
                sock, last_query_time = self.connections.pop()

            if time.time() - last_query_time < self.connection_timeout:
                return sock
            try:
                sock.close()
            except Exception:
                pass

        server_ip = self.get_server()
        if not server_ip:
            return None

        sock = self.connect(server_ip, self.port)
        if sock is not None:
            with self.connections_lock:
                if not self.running:
                    sock.close()
                    return None
        return sock

    def return_connection(self, sock):
        with self.connections_lock:
            if self.running and len(self.connections) < self.max_connections:
                self.connections.append([sock, time.time()])
                return
        try:
            sock.close()
        except Exception:
            pass

    def stop(self):
        with self.connections_lock:
            self.running = False
            connections = self.connections
            self.connections = []
        for sock, last_query_time in connections:
            try:
                sock.close()
            except Exception:
                pass

    def query(self, domain, dns_type=1, history=None):
        if history is None:
            history = set()
        elif not isinstance(history, set):
            history = set(_normalize_dns_name(name) for name in history)
        normalized_domain = _normalize_dns_name(domain)
        if normalized_domain in history or len(history) >= 16:
            return []
        history.add(normalized_domain)

        t0 = time.time()
        sock = None
        return_connection = False
        try:
            sock = self.get_connection()
            if not sock:
                xlog.warn("query_over_tcp %s type:%s connect fail.", domain, dns_type)
                return []

            d = DNSRecord(DNSHeader())
            d.add_question(DNSQuestion(domain, dns_type))

            data = d.pack()
            data = struct.pack("!H", len(data)) + data
            sock.sendall(data)

            response = b""
            while len(response) < 2:
                chunk = sock.recv(2 - len(response))
                if not chunk:
                    return []
                response += chunk

            length = struct.unpack("!H", response[:2])[0]
            while len(response) - 2 < length:
                chunk = sock.recv(length + 2 - len(response))
                if not chunk:
                    return []
                response += chunk

            t2 = time.time()

            p = DNSRecord.parse(response[2:length + 2])
            if not _match_dns_response(p, d.header.id, domain, dns_type):
                raise ValueError("unmatched DNS TCP response")
            if len(p.rr) == 0:
                xlog.warn("query_over_tcp for %s type:%d return none, cost:%f", domain, dns_type, t2-t0)

            unused_ips, alias = _parse_response_ips(p, domain, dns_type)
            ips = _extract_response_ips(
                p, domain, dns_type,
                lambda alias_domain, query_type: self.query(
                    alias_domain, query_type, history=history))

            xlog.debug("DNS %s %s return %s t:%f", self.protocol, domain, ips, t2-t0)
            if ips or alias is None:
                return_connection = True
                self.return_connection(sock)
            return ips
        except socket.timeout:
            xlog.warn("query_over_tcp %s type:%s timeout", domain, dns_type)
            return []
        except Exception as e:
            xlog.exception("query_over_tcp %s type:%s except:%r", domain, dns_type, e)
            return []
        finally:
            if sock is not None and not return_connection:
                try:
                    sock.close()
                except Exception:
                    pass


class DnsOverTlsQuery(DnsOverTcpQuery):
    def __init__(self, server_list=None):
        if not server_list:
            server_list = [
                {
                    "domain": "one.one.one.one",
                    "ipv4s": [b"1.1.1.1", b"1.0.0.1"],
                },
                {
                    "domain": "dns.quad9.net",
                    "ipv4s": [b"9.9.9.9", b"149.112.112.112"],
                }
            ]

        DnsOverTcpQuery.__init__(self, server_list=server_list, port=853)
        self.protocol = "DoT"
        self.ssl_context = ssl.create_default_context()
        self.ssl_context.check_hostname = False
        self.ssl_context.verify_mode = ssl.CERT_REQUIRED

    def connect(self, host, port):
        domain = host["domain"]
        ipv4 =  random.choice(host["ipv4s"])
        s = None
        try:
            s = super(DnsOverTlsQuery, self).connect(ipv4, port)
            if not s:
                # base connect failed and returned None (it logs its own warn).
                return None
            sock = self.ssl_context.wrap_socket(s, server_hostname=domain)
            return sock
        except Exception as e:
            xlog.warn("DnsOverTlsQuery connect %s %s:%d fail:%r", ipv4, domain, port, e)
            # A1 probe (2026-10-26): when wrap_socket fails mid-handshake the ssl
            # layer has already closed/detached the fd (fileno()==-1, _closed=True),
            # so this close is an idempotent no-op in that path; kept as defense
            # for failures where wrap never ran (e.g. base connect raised).
            if s is not None:
                try:
                    s.close()
                except Exception:
                    pass
            return None


class DnsOverHttpsQuery(object):
    def __init__(self, timeout=6):
        self.protocol = "DoH"
        self.timeout = timeout
        self.cn_servers = ["https://1.12.12.12/dns-query", "https://223.5.5.5/dns-query"]
        self.other_servers = [
            "https://1.1.1.1/dns-query",
            "https://dns10.quad9.net/dns-query",
            "https://dns.aa.net.uk/dns-query",
        ]
        self.connection_timeout = 60
        self.connections = []
        self.max_connections = 4
        self.running = True
        self.connections_lock = threading.Lock()

    def get_connection(self):
        while True:
            with self.connections_lock:
                if not self.running:
                    return None
                if not self.connections:
                    break
                client, last_query_time = self.connections.pop()

            if time.time() - last_query_time < self.connection_timeout:
                return client
            try:
                client.close()
            except Exception:
                pass

        if g.config.PROXY_ENABLE == 1:
            client = simple_http_client.Client(proxy={
                "type": g.config.PROXY_TYPE,
                "host": g.config.PROXY_HOST,
                "port": g.config.PROXY_PORT,
                "user": g.config.PROXY_USER,
                "pass": g.config.PROXY_PASSWD,
            }, timeout=self.timeout)
        else:
            client = simple_http_client.Client(timeout=self.timeout)

        with self.connections_lock:
            if not self.running:
                try:
                    client.close()
                except Exception:
                    pass
                return None
        return client

    def return_connection(self, client):
        with self.connections_lock:
            if self.running and len(self.connections) < self.max_connections:
                self.connections.append([client, time.time()])
                return
        try:
            client.close()
        except Exception:
            pass

    def stop(self):
        with self.connections_lock:
            self.running = False
            connections = self.connections
            self.connections = []
        for client, last_query_time in connections:
            try:
                client.close()
            except Exception:
                pass

    @property
    def server(self):
        return random.choice(self.other_servers)

    def query_json(self, domain, dns_type=1):
        client = None
        return_connection = False
        try:
            t0 = time.time()
            client = self.get_connection()

            url = self.server + "?name=" + domain + "&type=A" # type need to map to Text.
            r = client.request("GET", url, headers={"accept": "application/dns-json"})
            t2 = time.time()
            ips = []
            if not r:
                xlog.warn("DNS server:%s domain:%s fail t:%f", self.server, domain,  t2 - t0)
                return ips

            t = utils.to_str(r.text)

            data = json.loads(t)
            for answer in data["Answer"]:
                ips.append(answer["data"])

            return_connection = True
            self.return_connection(client)

            xlog.debug("DNS server:%s query:%s return %s t:%f", self.server, domain, ips, t2 - t0)
            return ips
        except Exception as e:
            xlog.warn("DNSOverHttpsQuery query fail:%r", e)
            return []
        finally:
            if client is not None and not return_connection:
                try:
                    client.close()
                except Exception:
                    pass

    def query(self, domain, dns_type=1, url=None, history=None):
        if history is None:
            history = set()
        elif not isinstance(history, set):
            history = set(_normalize_dns_name(name) for name in history)
        normalized_domain = _normalize_dns_name(domain)
        if normalized_domain in history or len(history) >= 16:
            return []
        history.add(normalized_domain)

        t0 = time.time()
        client = None
        return_connection = False
        try:
            client = self.get_connection()

            if not url:
                url = self.server
            # xlog.debug("DoH use %s", url)

            d = DNSRecord(DNSHeader())
            d.add_question(DNSQuestion(domain, dns_type))
            data = d.pack()

            r = client.request("POST", url, headers={"accept": "application/dns-message",
                                                     "content-type": "application/dns-message"}, body=data)

            t2 = time.time()
            ips = []
            if not r:
                xlog.warn("DNS s:%s query:%s fail t:%f", self.server, domain,  t2 - t0)
                return ips

            p = DNSRecord.parse(r.text)
            if not _match_dns_response(p, d.header.id, domain, dns_type):
                raise ValueError("unmatched DNS HTTPS response")

            unused_ips, alias = _parse_response_ips(p, domain, dns_type)
            ips = _extract_response_ips(
                p, domain, dns_type,
                lambda alias_domain, query_type: self.query(
                    alias_domain, query_type, history=history))

            if ips or alias is None:
                return_connection = True
                self.return_connection(client)

            xlog.debug("DNS %s %s return %s t:%f", self.protocol, domain, ips, t2 - t0)
            return ips
        except Exception as e:
            t1 = time.time()
            t = t1 - t0
            xlog.warn("DnsOverHttpsQuery query %s cost:%f fail:%r", domain, t, e)
            return []
        finally:
            if client is not None and not return_connection:
                try:
                    client.close()
                except Exception:
                    pass


class ParallelQuery():
    def __init__(self):
        self.generation_lock = threading.Lock()
        self.generations = {}

    def query_worker(self, task, function):
        try:
            ips = function(task.domain, task.dns_type)
            if len(ips):
                task.put(ips)
            rejected = None
            with self.generation_lock:
                current = self.generations.get((task.domain, task.dns_type)) == task.generation
                same_cache = g.domain_cache is task.domain_cache
                if current and len(ips) and same_cache:
                    task.domain_cache.set_ips(
                        task.domain, ips, task.dns_type, expected_epoch=task.cache_epoch)
                elif len(ips):
                    rejected = "dns.generation_drops_total" if not current else \
                        "dns.cache_commit_rejected_total"
            if rejected:
                metrics.increment(rejected)
        finally:
            with self.generation_lock:
                task.pending -= 1
                key = (task.domain, task.dns_type)
                if task.pending == 0 and self.generations.get(key) == task.generation:
                    self.generations.pop(key, None)

    def query(self, domain, dns_type, funcs):
        task = Queue()
        task.domain = domain
        task.dns_type = dns_type
        task.pending = len(funcs)
        key = (domain, dns_type)
        with self.generation_lock:
            task.generation = self.generations.get(key, 0) + 1
            task.domain_cache = g.domain_cache
            task.cache_epoch = task.domain_cache.get_epoch()
            self.generations[key] = task.generation

        for func in funcs:
            threading.Thread(target=self.query_worker, args=(task, func), name="ParalleQuery_%s" % domain).start()

        try:
            ips = task.get(timeout=5)
        except Empty:
            metrics.mark_timeout()
            ips = []
        except:
            ips = []

        return ips


class CombineDnsQuery():
    def __init__(self):
        self.domain_allowed_pattern = re.compile(br"(?!-)[A-Z\d-]{1,63}(?<!-)$")
        self.local_dns_resolve = LocalDnsQuery()

        self.tcp_query = DnsOverTcpQuery()
        self.tls_query = DnsOverTlsQuery()
        self.https_query = DnsOverHttpsQuery()

        self.parallel_query = ParallelQuery()

    def is_valid_hostname(self, hostname):
        hostname = hostname.upper()
        if len(hostname) > 255:
            return False
        if hostname.endswith(b"."):
            hostname = hostname[:-1]

        return all(self.domain_allowed_pattern.match(x) for x in hostname.split(b"."))

    def query_blocked_domain(self, domain, dns_type):
        return self.parallel_query.query(domain, dns_type, [
            self.https_query.query,
            self.tls_query.query,
            query_dns_from_xxnet,
        ])

    def query_unknown_domain(self, domain, dns_type):
        res = self.local_dns_resolve.query(domain, dns_type)
        if res:
            return res

        return self.parallel_query.query(domain, dns_type, [
            self.https_query.query,
            self.tls_query.query,
            self.tcp_query.query,
            query_dns_from_xxnet
        ])

    def query(self, domain, dns_type=1, history=None):
        token = metrics.begin_query()
        started = time.monotonic()
        ips = []
        try:
            ips = self._query(domain, dns_type, history)
            return ips
        finally:
            metrics.finish_query(token, ips, time.monotonic() - started)

    def _query(self, domain, dns_type=1, history=None):
        if history is None:
            history = []

        domain = utils.to_bytes(domain)
        if utils.check_ip_valid(domain):
            return [domain]

        if not self.is_valid_hostname(domain):
            xlog.warn("DNS query:%s not valid, type:%d", domain, dns_type)
            return []

        ips = g.domain_cache.get_ips(domain, dns_type)
        if ips:
            return ips

        rule = g.user_rules.check_host(domain, 0)
        if rule == "black":
            # user define black list like advertisement or malware server.
            ips = ["127.0.0.1"]
            xlog.debug("DNS query:%s in black", domain)
            return ips

        elif b"." not in domain or g.gfwlist.in_white_list(domain) or rule in ["direct"] or g.config.pac_policy == "all_Direct":
            domain_cache = g.domain_cache
            cache_epoch = domain_cache.get_epoch()
            ips = self.local_dns_resolve.query(domain, dns_type, timeout=1)
            if g.domain_cache is domain_cache:
                domain_cache.set_ips(domain, ips, dns_type, expected_epoch=cache_epoch)
            return ips

        elif g.gfwlist.in_block_list(domain) or rule in ["gae", "socks"] or g.config.pac_policy == "all_X-Tunnel":
            ips = self.query_blocked_domain(domain, dns_type)
        elif g.gfwlist.in_white_list(domain):
            ips = self.local_dns_resolve.query(domain, dns_type, timeout=1)
        else:
            ips = self.query_unknown_domain(domain, dns_type)

        if not ips:
            ips = self.local_dns_resolve.query(domain, dns_type, timeout=1)

        out_ips = []
        for ip in ips:
            if not utils.check_ip_valid(ip):
                if ip == domain:
                    continue

                if ip in history:
                    continue

                history.append(ip)
                ip_ips = self.query(ip, dns_type, history)
                for ip in ip_ips:
                    out_ips.append(ip)

            elif ip not in out_ips:
                out_ips.append(ip)

        return out_ips

    def query_recursively(self, domain, dns_type=None):
        if not dns_type:
            dns_types = [1, 28]
        else:
            dns_types = [dns_type]

        ips_out = []
        for dns_type in dns_types:
            ips = self.query(domain, dns_type)
            for ip in ips:
                if dns_type == 2 or utils.check_ip_valid(ip):
                    ips_out.append(ip)
                else:
                    ips_s = self.query_recursively(ip, dns_type)
                    ips_out += ips_s

        return ips_out

    def stop(self):
        self.local_dns_resolve.stop()
        self.tcp_query.stop()
        self.tls_query.stop()
        self.https_query.stop()