import os
import sys
import socket
import operator
import time
import threading
import struct

import utils

if __name__ == '__main__':
    current_path = os.path.dirname(os.path.abspath(__file__))
    root_path = os.path.abspath(os.path.join(current_path, os.pardir))
    python_path = root_path
    noarch_lib = os.path.join(python_path, 'lib', 'noarch')
    sys.path.append(noarch_lib)


from .socket_wrap import SocketWrap
from queue import Queue
import socks
from . import global_var as g
from xlog import getLogger
xlog = getLogger("smart_router")


def load_proxy_config():
    global default_socket
    if g.config.PROXY_ENABLE:

        if g.config.PROXY_TYPE == "HTTP":
            proxy_type = socks.HTTP
        elif g.config.PROXY_TYPE == "SOCKS4":
            proxy_type = socks.SOCKS4
        elif g.config.PROXY_TYPE == "SOCKS5":
            proxy_type = socks.SOCKS5
        else:
            xlog.error("proxy type %s unknown, disable proxy", g.config.PROXY_TYPE)
            raise Exception()

        socks.set_default_proxy(proxy_type, g.config.PROXY_HOST, g.config.PROXY_PORT,
                                g.config.PROXY_USER, g.config.PROXY_PASSWD)


class ConnectManager(object):
    def __init__(self, connection_timeout=15, connect_threads=3, connect_timeout=5):
        self.lock = threading.Lock()
        self.cache = {}
        # host => [ { "conn":.., "create_time" }
        #    ... ]
        self.connection_timeout = connection_timeout
        self.connect_timeout = connect_timeout
        self.connect_threads = connect_threads
        self.connect_slots = threading.BoundedSemaphore(connect_threads)
        self.inflight = set()
        self.max_cache_per_host = max(1, connect_threads)
        self.max_cache_total = max(16, connect_threads * 4)

        self.running = True
        threading.Thread(target=self.connection_check_worker, name="smart_router_conn_checker").start()

    def stop(self):
        with self.lock:
            self.running = False
            connections = [cc["conn"] for cache in self.cache.values() for cc in cache]
            connections.extend(self.inflight)
            self.inflight = set()
            self.cache = {}

        for conn in connections:
            try:
                conn.close()
            except Exception:
                pass

    def connection_check_worker(self):
        while self.running:
            time_now = time.time()
            with self.lock:
                for host_port in list(self.cache.keys()):
                    try:
                        cache = self.cache[host_port]
                        for cc in list(cache):
                            if time_now - cc["create_time"] > self.connection_timeout:
                                cache.remove(cc)
                                cc["conn"].close()
                        if not cache:
                            self.cache.pop(host_port, None)
                    except:
                        pass

            time.sleep(10)

    def add_sock(self, host_port, sock):
        with self.lock:
            cache = self.cache.setdefault(host_port, [])
            total = sum(len(connections) for connections in self.cache.values())
            if not self.running or len(cache) >= self.max_cache_per_host or total >= self.max_cache_total:
                if not cache:
                    self.cache.pop(host_port, None)
            else:
                cache.append({"create_time": time.time(), "conn": sock})
                return

        try:
            sock.close()
        except Exception:
            pass

    def get_sock_from_cache(self, host_port):
        time_now = time.time()
        conn = None
        with self.lock:
            cache = self.cache.get(host_port)
            if cache is not None:
                while cache:
                    try:
                        cc = cache.pop(0)
                        if time_now - cc["create_time"] > self.connection_timeout:
                            cc["conn"].close()
                            continue

                        conn = cc["conn"]
                        break
                    except Exception as e:
                        xlog.exception("get_conn:%r", e)
                        break
                if not cache:
                    self.cache.pop(host_port, None)
        return conn

    def create_connect(self, queue, host, ip, port, timeout=5):
        ip = utils.to_bytes(ip)
        sock = None
        try:
            if int(g.config.PROXY_ENABLE):
                sock = socks.socksocket(socket.AF_INET if b':' not in ip else socket.AF_INET6)
            else:
                sock = socket.socket(socket.AF_INET if b':' not in ip else socket.AF_INET6)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            # set struct linger{l_onoff=1,l_linger=0} to avoid 10048 socket error
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
            # resize socket recv buffer 8K->32K to improve browser releated application performance
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 512 * 1024)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 512 * 1024)

            with self.lock:
                if not self.running:
                    raise OSError("connect manager stopped")
                self.inflight.add(sock)

            sock.setsockopt(socket.SOL_TCP, socket.TCP_NODELAY, True)
            sock.settimeout(timeout)

            start_time = time.time()
            sock.connect((ip, port))
            time_cost = (time.time() - start_time) * 1000
            # xlog.debug("connect %s %s:%d time:%d", host, ip, port, time_cost)
            g.ip_cache.update_connect_time(ip, port, time_cost)
            s = SocketWrap(sock, ip, port, host)
            host_port = "%s:%d" % (host, port)
            self.add_sock(host_port, s)
            queue.put(True)
        except Exception as e:
            # xlog.debug("connect %s %s:%d fail:%r", host, ip, port, e)
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
            g.ip_cache.report_connect_fail(ip, port)
            queue.put(False)
        finally:
            with self.lock:
                self.inflight.discard(sock)

    def _create_connect_worker(self, queue, host, ip, port):
        try:
            self.create_connect(queue, host, ip, port)
        finally:
            self.connect_slots.release()

    def get_conn(self, host, ips, port, timeout=5):
        # xlog.debug("connect to %s:%d %r", host, port, ips)
        end_time = time.time() + timeout
        host_port = "%s:%d" % (host, port)
        sock = self.get_sock_from_cache(host_port)
        if sock:
            return sock

        ip_rate = {}
        for ip in ips:
            connect_time = g.ip_cache.get_connect_time(ip, port)
            if connect_time >= 8000:
                continue

            ip_rate[ip] = connect_time

        if not ip_rate:
            return None

        ip_time = sorted(list(ip_rate.items()), key=operator.itemgetter(1))
        ordered_ips = [ip for ip, rate in ip_time]

        wait_queue = Queue()
        next_ip = 0
        active = 0
        while next_ip < len(ordered_ips) or active:
            while next_ip < len(ordered_ips) and active < self.connect_threads and time.time() < end_time:
                time_left = end_time - time.time()
                if time_left <= 0 or not self.connect_slots.acquire(timeout=time_left):
                    break
                ip = ordered_ips[next_ip]
                next_ip += 1
                active += 1
                try:
                    threading.Thread(target=self._create_connect_worker, args=(wait_queue, host, ip, port),
                                     name="smart_router_create_conn_%s" % ip).start()
                except Exception:
                    active -= 1
                    self.connect_slots.release()
                    break

            time_left = end_time - time.time()
            if time_left <= 0:
                break

            try:
                wait_queue.get(timeout=time_left)
                active -= 1
                sock = self.get_sock_from_cache(host_port)
                if sock:
                    return sock
            except Exception:
                break

        return self.get_sock_from_cache(host_port)