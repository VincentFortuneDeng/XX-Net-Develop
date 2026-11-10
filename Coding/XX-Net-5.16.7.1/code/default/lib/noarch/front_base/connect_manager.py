#!/usr/bin/env python
# coding:utf-8


"""
This file manage the ssl connection pool.
For faster access the target host,

ssl link will save to pool after use.
and need keep alive every 60 seconds.

We create multi-thread to try-connect google cloud ip.

we also keep host connect for direct connect.
every ssl connect can't change host after request.
"""

import time
import threading
import operator
import socket
import random
from queue import Queue

from .openssl_wrap import SSLConnection
from xlog import LogThrottle

class NoRescourceException(Exception):
    pass


class ConnectPool():
    def __init__(self):
        self.pool_lock = threading.Lock()
        self.not_empty = threading.Condition(self.pool_lock)
        self.pool = {}
        self.closed = False

    def qsize(self):
        return len(self.pool)

    def put(self, item):
        handshake_time, sock = item
        self.not_empty.acquire()
        try:
            if self.closed:
                sock.close()
                return False
            self.pool[sock] = handshake_time
            self.not_empty.notify()
            return True
        finally:
            self.not_empty.release()

    def get(self, block=True, timeout=None):
        self.not_empty.acquire()
        try:
            if self.closed:
                return None
            if not block:
                if self.qsize() == 0:
                    return None
            elif timeout is None:
                while self.qsize() == 0 and not self.closed:
                    self.not_empty.wait()
            elif timeout < 0:
                raise ValueError("'timeout' must be a positive number")
            else:
                end_time = time.time() + timeout
                while not self.qsize() and not self.closed:
                    remaining = end_time - time.time()
                    if remaining <= 0.0:
                        return None
                    self.not_empty.wait(remaining)

            if self.closed or not self.qsize():
                return None
            item = self._get()
            return item
        finally:
            self.not_empty.release()

    def get_nowait(self):
        return self.get(block=False)

    def _get(self):
        fastest_time = 9999
        fastest_sock = None
        for sock in self.pool:
            hs_time = self.pool[sock]
            if hs_time < fastest_time or not fastest_sock:
                fastest_time = hs_time
                fastest_sock = sock

        self.pool.pop(fastest_sock)
        return fastest_time, fastest_sock

    def get_slowest(self):
        self.not_empty.acquire()
        try:
            if not self.qsize():
                raise ValueError("no item")

            slowest_handshake_time = 0
            slowest_sock = None
            for sock in self.pool:
                handshake_time = self.pool[sock]
                if handshake_time > slowest_handshake_time:
                    slowest_handshake_time = handshake_time
                    slowest_sock = sock

            self.pool.pop(slowest_sock)
            return slowest_handshake_time, slowest_sock
        finally:
            self.not_empty.release()

    def get_need_keep_alive(self, maxtime=200):
        return_list = []
        self.pool_lock.acquire()
        try:
            pool = tuple(self.pool)
            for sock in pool:
                inactive_time = time.time() - sock.last_use_time
                # self.logger.debug("inactive_time:%d", inactive_time * 1000)
                if inactive_time >= maxtime:
                    return_list.append(sock)

                    del self.pool[sock]

            return return_list
        finally:
            self.pool_lock.release()

    def clear(self):
        self.pool_lock.acquire()
        try:
            for sock in self.pool:
                sock.close()

            self.pool = {}
        finally:
            self.pool_lock.release()

    def close(self):
        self.pool_lock.acquire()
        try:
            self.closed = True
            socks = list(self.pool)
            self.pool = {}
            self.not_empty.notify_all()
        finally:
            self.pool_lock.release()

        for sock in socks:
            sock.close()

    def to_string(self):
        out_str = ''
        self.pool_lock.acquire()
        try:
            pool = sorted(list(self.pool.items()), key=operator.itemgetter(1))
            i = 0
            for item in pool:
                sock, t = item
                out_str += "%d \t %s handshake:%d not_active_time:%d \r\n" % (i, sock.ip_str, t, time.time() - sock.last_use_time)
                i += 1
        finally:
            self.pool_lock.release()

        return out_str


class ConnectManager(object):
    def __init__(self, logger, config, connect_creator, ip_manager, check_local_network):
        self.class_name = "ConnectManager"
        self.logger = logger
        self.config = config
        self.connect_creator = connect_creator
        self.ip_manager = ip_manager
        self.check_local_network = check_local_network

        self.thread_num_lock = threading.Lock()
        self.timeout = 4
        self.start_connect_time = 0
        self.thread_num = 0
        self.running = True

        self._waiting_num_lock = threading.Lock()
        self._connection_waiting_num = 0
        self.no_ip_lock = threading.Lock()
        self.no_ip_time = 0
        # (H 阶段 2026-10-29) total 统计本轮全部旱态事件（放行+抑制）。
        self.last_no_ip_warn = 0
        self.suppressed_no_ip_warn = 0
        self.total_no_ip_events = 0
        self.connect_ssl_log_throttle = LogThrottle(60)

        # after new created ssl_sock timeout(50 seconds)
        # call the callback.
        # This callback will put ssl to worker
        self.ssl_timeout_cb = None
        
        self.new_conn_pool = ConnectPool()

        self.connecting_more_thread = None

        self.keep_alive_th = threading.Thread(target=self.keep_alive_thread,
                                              name="%s_conn_manager_keep_alive" % self.logger.name)
        self.keep_alive_th.daemon = True
        self.keep_alive_th.start()

        if self.config.connection_pool_min:
            self.keep_conn_th = threading.Thread(target=self.keep_connection_daemon,
                                                 name="%s_conn_manager_keep_conn" % self.logger.name)
            self.keep_conn_th.daemon = True
            self.keep_conn_th.start()
        else:
            self.keep_conn_th = None

    def stop(self):
        self.running = False
        self.new_conn_pool.close()
        suppressed, key_count = self.connect_ssl_log_throttle.flush()
        if suppressed:
            self.logger.warn("connect_ssl final suppressed:%d keys:%d", suppressed, key_count)

    def set_ssl_created_cb(self, cb):
        self.ssl_timeout_cb = cb

    def keep_alive_thread(self):
        while self.running:
            to_keep_live_list = self.new_conn_pool.get_need_keep_alive(maxtime=self.config.https_keep_alive-6)

            for ssl_sock in to_keep_live_list:
                inactive_time = time.time() - ssl_sock.last_use_time
                if inactive_time > self.config.https_keep_alive or not self.ssl_timeout_cb:
                    self.ip_manager.report_connect_closed(ssl_sock.ip_str, ssl_sock.sni, "alive_timeout")
                    ssl_sock.close()
                else:
                    # put ssl to worker
                    try:
                        self.ssl_timeout_cb(ssl_sock)
                    except Exception as e:
                        self.logger.exception("ssl_timeout_cb except:%r", e)
                        # no appid avaiable
                        pass

            time.sleep(5)

    def keep_connection_daemon(self):
        while self.running:
            if self.new_conn_pool.qsize() >= self.config.https_connection_pool_min:
                time.sleep(5)
                continue

            if self.config.show_state_debug:
                self.logger.debug("call _connect_process from keep_connection_daemon")
            self._connect_process()

    def _need_more_ip(self):
        if self._connection_waiting_num:
            return True
        else:
            return False

    def _create_more_connection(self):
        if self.config.show_state_debug:
            self.logger.debug("_create_more_connection")
        if not self.connecting_more_thread:
            with self.thread_num_lock:
                # 锁外判断可能已过期，创建前必须在同一所有权边界内复核。
                if self.connecting_more_thread:
                    return
                self.connecting_more_thread = threading.Thread(target=self._create_more_connection_worker,
                                                               name="%s_conn_manager__create_more_conn" % self.logger.name)
                self.connecting_more_thread.start()

    def _create_more_connection_worker(self):
        if self.config.show_state_debug:
            self.logger.debug("_create_more_connection_worker")
        if self.start_connect_time and self.start_connect_time + 30 < time.time():
            self.start_connect_time = 0
            self.config.https_max_connect_thread += 1
            self.logger.warning("Connect creating process blocked, max connect thread increase to %d",
                                self.config.https_max_connect_thread)

        for i in range(self.thread_num, self.config.https_max_connect_thread):
            self.thread_num_lock.acquire()
            self.thread_num += 1
            self.thread_num_lock.release()

            p = threading.Thread(target=self._connect_thread, name="%s_conn_manager__connect_th" % self.logger.name)
            p.start()
            if self.config.connect_create_interval > 0.1:
                time.sleep(self.config.connect_create_interval)

            if not self._need_more_ip():
                break

        with self.thread_num_lock:
            self.connecting_more_thread = None

    def _connect_thread(self, sleep_time=0):
        if self.config.show_state_debug:
            self.logger.debug("_connect_thread")

        if sleep_time > 0.1:
            self.logger.debug("_connect_thread sleep %f", sleep_time)
            time.sleep(sleep_time)

        try:
            while self.running and self._need_more_ip() and time.time() - self.no_ip_time > 10:
                if self.new_conn_pool.qsize() > self.config.https_connection_pool_max:
                    break

                if self.config.show_state_debug:
                    self.logger.debug("call _connect_process from _connect_thread")
                self.start_connect_time = time.time()
                self._connect_process()
                self.start_connect_time = 0
        finally:
            self.thread_num_lock.acquire()
            self.thread_num -= 1
            self.thread_num_lock.release()

    def _connect_process(self):
        if self.config.show_state_debug:
            self.logger.debug("_connect_process")
        try:
            host_info = self.ip_manager.get_ip_sni_host()
            if not host_info:
                now = time.time()
                with self.no_ip_lock:
                    self.no_ip_time = now
                    self.total_no_ip_events += 1
                    # 每次放行结算上一窗口；总事件数保留到恢复通告。
                    if now - self.last_no_ip_warn >= 60:
                        self.logger.warning("not enough ip (suppressed %d last window, suppressing for 60s after this line)",
                                            self.suppressed_no_ip_warn)
                        self.last_no_ip_warn = now
                        self.suppressed_no_ip_warn = 0
                    else:
                        self.suppressed_no_ip_warn += 1
                    time.sleep(1)
                return None

            # 与旱态计账共用同一把锁，确保恢复通告只结算、清零一次。
            with self.no_ip_lock:
                if self.last_no_ip_warn:
                    self.logger.warning("not enough ip recovered (suppressed %d, total %d)",
                                        self.suppressed_no_ip_warn,
                                        self.total_no_ip_events)
                    self.last_no_ip_warn = 0
                    self.suppressed_no_ip_warn = 0
                    self.total_no_ip_events = 0

            # self.logger.debug("create ssl conn %s", ip_str)
            ssl_sock = self._create_ssl_connection(host_info)
            if not ssl_sock:
                return None

            if not self.running:
                ssl_sock.close()
                return None

            if not self.new_conn_pool.put((ssl_sock.handshake_time, ssl_sock)):
                return None

            if self.config.connect_create_interval > 0.1:
                sleep = random.uniform(self.config.connect_create_interval, self.config.connect_create_interval*2)
                time.sleep(sleep)

            return ssl_sock
        except Exception as e:
            self.logger.exception("connect_process except:%r", e)

    def _connect_ssl(self, ip_str, sni, host, close_cb, queue, state=None):
        if self.config.show_state_debug:
            self.logger.debug("_connect_ssl")

        try:
            ssl_sock = self.connect_creator.connect_ssl(ip_str, sni, host, close_cb=close_cb)
            if state is None:
                queue.put(ssl_sock)
            else:
                with state["lock"]:
                    if state["timed_out"]:
                        if ssl_sock and hasattr(ssl_sock, "close"):
                            ssl_sock.close()
                        return
                    queue.put(ssl_sock)
        except Exception as e:
            if state is not None:
                with state["lock"]:
                    if state["timed_out"]:
                        return
                    queue.put(e)
            else:
                queue.put(e)
            allowed, suppressed = self.connect_ssl_log_throttle.allow((ip_str, type(e).__name__))
            if allowed:
                suffix = " (suppressed %d)" % suppressed if suppressed else ""
                self.logger.warn("_connect_ssl %s sni:%s host:%s fail:%r%s",
                                 ip_str, sni, host, e, suffix)

    def _create_ssl_connection(self, host_info):
        if self.config.show_state_debug:
            self.logger.debug("_create_ssl_connection")

        ip_str = host_info["ip_str"]
        sni = host_info["sni"]
        host = host_info["host"]

        try:
            q = Queue()
            state = {"lock": threading.Lock(), "timed_out": False}
            fn_args = (ip_str, sni, host, self.ip_manager.ssl_closed, q, state)
            t = threading.Thread(target=self._connect_ssl, args=fn_args, name="connect_ssl_%s" % ip_str)
            t.start()
            try:
                ssl_sock = q.get(timeout=30)
            except:
                with state["lock"]:
                    state["timed_out"] = True
                    try:
                        late_result = q.get(timeout=0)
                    except Exception:
                        late_result = None
                    if late_result and hasattr(late_result, "close"):
                        late_result.close()
                self.logger.warn("connect_ssl_timeout %s", ip_str)
                raise socket.error("timeout")

            if not ssl_sock or isinstance(ssl_sock, ValueError) or isinstance(ssl_sock, OSError) or not hasattr(ssl_sock, "handshake_time"):
                raise socket.error("timeout")

            self.ip_manager.update_ip(ip_str, sni, ssl_sock.handshake_time)
            self.logger.debug("create_ssl update ip:%s time:%d h2:%d sni:%s, host:%s",
                              ip_str, ssl_sock.handshake_time, ssl_sock.h2, ssl_sock.sni, ssl_sock.host)
            ssl_sock.host_info = host_info

            return ssl_sock
        except socket.error as e:
            if str(e) in ["no host", "timeout"]:
                pass
            elif not self.check_local_network.is_ok(ip_str):
                self.logger.debug("connect %s network fail, %r", ip_str, e)
                time.sleep(1)
            else:
                self.logger.debug("connect %s fail:%r", ip_str, e)
            self.ip_manager.report_connect_fail(ip_str, sni, str(e))
        except NoRescourceException as e:
            self.logger.warning("create ssl for %s except:%r", ip_str, e)
            self.ip_manager.report_connect_fail(ip_str, sni, str(e))
        except Exception as e:
            self.logger.exception("connect except:%r", e)
            self.ip_manager.report_connect_fail(ip_str, sni, str(e))
            if not self.check_local_network.is_ok(ip_str):
                self.logger.debug("connect %s network fail, %r", ip_str, e)
                time.sleep(10)
            else:
                self.logger.exception("connect %s fail:%r", ip_str, e)
                time.sleep(1)

    def get_ssl_connection(self, timeout=30):
        with self._waiting_num_lock:
            self._connection_waiting_num += 1

        end_time = time.time() + timeout
        try:
            while self.running:
                ret = self.new_conn_pool.get(block=False)
                if not ret:
                    self._create_more_connection()
                    ret = self.new_conn_pool.get(block=True, timeout=1)

                if ret:
                    handshake_time, ssl_sock = ret
                    if time.time() - ssl_sock.last_use_time < self.config.https_keep_alive - 1:
                        if self.config.show_state_debug:
                            self.logger.debug("new_conn_pool.get:%s handshake:%d", ssl_sock.ip, handshake_time)
                        return ssl_sock
                    else:
                        self.logger.warn("new_conn_pool.get:%s handshake:%d timeout.", ssl_sock.ip, handshake_time)
                        self.ip_manager.report_connect_closed(ssl_sock.ip_str, ssl_sock.sni, "get_ssl_timeout")
                        ssl_sock.close()
                else:
                    if time.time() > end_time:
                        self.logger.debug("get_ssl_connection timeout")
                        return None

                self._create_more_connection()
        finally:
            with self._waiting_num_lock:
                self._connection_waiting_num -= 1
