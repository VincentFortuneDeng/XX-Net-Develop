import socket
import time
import json

import utils
from front_base.ip_manager import IpManagerBase


# probe 2001:4860:4860::8888 (Google DNS) over UDP: if connect succeeds and
# the local address is a global unicast one, this host has usable IPv6.
IPV6_PROBE_ADDR = ("2001:4860:4860::8888", 53, 0, 0)
IPV6_PROBE_INTERVAL = 300  # seconds between re-probes


def ipv6_available(logger=None):
    now = time.time()
    if ipv6_available.cached_until > now:
        return ipv6_available.cached_result

    result = False
    try:
        sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        try:
            sock.connect(IPV6_PROBE_ADDR)
            local = sock.getsockname()[0]
            # link-local (fe80::), loopback (::1), ULA (fc00::/7) are not
            # routable to the internet: only global unicast counts. An
            # unspecified source "::" means the OS picked no real local
            # address -> treat as unavailable, same as non-global unicast.
            # No lock around double probes: two threads probing concurrently
            # just do two UDP connects and write the same cache twice.
            result = local.startswith(("2", "3"))
        finally:
            sock.close()
    except Exception as e:
        if logger:
            logger.debug("ipv6_available probe fail:%r", e)
        result = False

    ipv6_available.cached_result = result
    ipv6_available.cached_until = now + IPV6_PROBE_INTERVAL
    return result

ipv6_available.cached_result = False
ipv6_available.cached_until = 0.0


class IpManager(IpManagerBase):
    def __init__(self, config, host_manager, logger, speed_fn):
        super(IpManager, self).__init__(config, None, logger, speed_fn)
        self.host_manager = host_manager
        self.ip_dict = {}

    def _ipv6_enabled(self):
        # honor front config: force_ipv6 keeps trying IPv6 even if the probe
        # says no (user override); force_ipv4/auto skip it when unavailable.
        use_ipv6 = getattr(self.config, "use_ipv6", "auto")
        if use_ipv6 == "force_ipv6":
            return True
        if use_ipv6 == "force_ipv4":
            return False
        return ipv6_available(self.logger)

    def __str__(self):
        o = ""
        o += " ip_dict: \r\n%s\r\n" % json.dumps(self.ip_dict, indent=2)
        return o

    def _get_ip_info(self, ip):
        self.ip_dict.setdefault(ip, {
            "ip": ip,
            "fail_times": 0,
            "links": 0,
            "rtt": 1000,
            "last_try": 0.0
        })
        return self.ip_dict[ip]

    def get_ip_sni_host(self):
        now = time.time()

        best_info = None
        best_speed = 0
        ipv6_enabled = self._ipv6_enabled()
        skipped_ipv6 = 0

        for ip, ip_info in self.host_manager.info.items():
            if "sni" not in ip_info:
                continue

            if ":" in ip and not ipv6_enabled:
                # no usable IPv6 on this host: skip IPv6 fronts instead of
                # burning connect threads on "No route to host".
                skipped_ipv6 += 1
                continue

            port = int(ip_info.get("port", 443))
            ip_str = utils.get_ip_str(ip, port)

            info = self._get_ip_info(ip)
            if info["links"] < 0:
                # self.logger.error("ip %s link:%d", ip, info["links"])
                info["links"] = 0

            if info["links"] >= self.config.max_links_per_ip:
                continue

            if info["fail_times"] and now - info["last_try"] < 60:
                continue

            speed, rtt = self.get_speed(ip_str)
            if speed > best_speed:
                best_speed = speed
                best_info = info

        if not best_info:
            return None

        if skipped_ipv6:
            # make the silent IPv6 filtering observable, throttled to one line
            # per probe cycle (cache TTL is 300s) so it stays low-noise.
            now_cycle = int(now // 300)
            if getattr(self, "_last_skip_log_cycle", None) != now_cycle:
                self._last_skip_log_cycle = now_cycle
                self.logger.info("get_ip_sni_host skipped %d IPv6 fronts (no usable local IPv6)", skipped_ipv6)

        best_info["links"] += 1
        best_info["last_try"] = now
        # self.logger.debug("get ip:%s", ip)

        ip = best_info["ip"]
        port = int(self.host_manager.info[ip].get("port", 443))
        ip_str = utils.get_ip_str(ip, port)
        return {
            "ip_str": ip_str,
            "sni": None,
            "host": None,
        }

    def update_ip(self, ip_str, sni, handshake_time):
        ip, _ = utils.get_ip_port(ip_str)
        ip = utils.to_str(ip)
        info = self._get_ip_info(ip)
        info["fail_times"] = 0
        info["rtt"] = handshake_time
        info["last_try"] = 0.0
        # self.logger.debug("ip %s connect success", ip)

    def report_connect_fail(self, ip_str, sni=None, reason="", force_remove=False):
        ip, _ = utils.get_ip_port(ip_str)
        ip = utils.to_str(ip)
        info = self._get_ip_info(ip)
        info["fail_times"] += 1
        if info["links"] <= 0:
            self.logger.error("report_connect_fail %s sni:%s links:%d reason:%s", ip, sni, info["links"], reason)
        else:
            info["links"] -= 1
        self.logger.debug("ip %s connect fail:%s", ip, reason)

    def report_connect_closed(self, ip_str, sni=None, reason=""):
        try:
            ip, _ = utils.get_ip_port(ip_str)
            ip = utils.to_str(ip)
            info = self._get_ip_info(ip)
            if info["links"] <= 0:
                self.logger.error("report_connect_closed %s sni:%s links:%d reason:%s", ip, sni, info["links"], reason)
            else:
                info["links"] -= 1
            self.logger.debug("ip %s sni:%s connect closed reason %s", ip, sni, reason)
        except Exception as e:
            self.logger.warn("report_connect_closed %s sni:%s reason:%s except:%r", ip_str, sni, reason, e)
