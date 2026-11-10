from queue import Queue
import socket
import threading

from .http_common import *
from xlog import LogThrottle
import simple_http_client
import utils


def pack_headers(headers):
    out_list = []
    for k, v in headers.items():
        if isinstance(v, int):
            out_list.append(b'%s: %d\r\n' % (utils.to_bytes(k), v))
        else:
            out_list.append(b'%s: %s\r\n' % (utils.to_bytes(k), utils.to_bytes(v)))

    return b''.join(out_list)


class Http1Worker(HttpWorker):
    # (G 阶段 2026-10-28) HEAD 探活失败按前端聚合，跨 worker 共享账本。
    head_fail_throttle = LogThrottle(60)

    def __init__(self, logger, ip_manager, config, ssl_sock, close_cb, retry_task_cb, idle_cb, log_debug_data):
        super(Http1Worker, self).__init__(logger, ip_manager, config, ssl_sock,
                                          close_cb, retry_task_cb, idle_cb, log_debug_data)

        self.version = "1.1"
        self.task = None
        self.transfered_size = 0
        self.trace_time = []
        self.trace_time.append([ssl_sock.create_time, "connect"])
        self.record_active("init")

        self.task_queue = Queue()
        threading.Thread(target=self.work_loop, name="%s_http1_work_loop" % self.logger.name).start()
        self.idle_cb()

        if self.config.http1_first_ping_wait or self.config.http1_ping_interval:
            threading.Thread(target=self.keep_alive_thread, name="%s_http1_keep_alive" % self.logger.name).start()

    def record_active(self, active=""):
        self.trace_time.append([time.time(), active])
        if len(self.trace_time) > self.config.http1_trace_size:
            self.trace_time.pop(0)
        # self.logger.debug("%s stat:%s", self.ip, active)

    def get_trace(self):
        out_list = []
        last_time = self.trace_time[0][0]
        for t, stat in self.trace_time:
            time_diff = int((t - last_time) * 1000)
            last_time = t
            out_list.append(" %d:%s" % (time_diff, stat))
        out_list.append(":%d" % ((time.time() - last_time) * 1000))
        out_list.append(" processed:%d" % self.processed_tasks)
        out_list.append(" transfered:%d" % self.transfered_size)
        out_list.append(" sni:%s" % self.ssl_sock.sni)
        return ",".join(out_list)

    def request(self, task):
        with self._lock:
            if not self.keep_running or not self.accept_task:
                return False
            attempt = task.begin_attempt()
            self.accept_task = False
            self.task = task
            self.task_attempt = attempt
            self.task_queue.put(task)
            return True

    def keep_alive_thread(self):
        while time.time() - self.ssl_sock.create_time < self.config.http1_first_ping_wait:
            if not self.keep_running:
                self.close("exit")
                return

            time.sleep(3)

        if self.config.http1_first_ping_wait and self.processed_tasks == 0:
            self.task_queue.put("ping")

        if self.config.http1_ping_interval:
            while self.keep_running:
                time_to_ping = max(self.config.http1_ping_interval - (time.time() - self.last_recv_time), 3)
                time.sleep(time_to_ping)

                if not self.request_onway and \
                        time.time() - self.last_recv_time > self.config.http1_ping_interval - 3:
                    self.task_queue.put("ping")
                    time.sleep(3)

        elif self.config.http1_idle_time:
            while self.keep_running:
                time_to_sleep = max(self.config.http1_idle_time - (time.time() - self.last_recv_time), 3)
                time.sleep(time_to_sleep)

                if not self.request_onway and time.time() - self.last_recv_time > self.config.http1_idle_time:
                    self.close("idle timeout")
                    return

    def work_loop(self):
        while self.keep_running:
            try:
                task = self.task_queue.get(block=True)
            except:
                task = None

            if not task:
                # None task means exit
                self.accept_task = False
                self.keep_running = False
                return

            if task == "ping":
                if not self.head_request():
                    self.ip_manager.recheck_ip(self.ssl_sock.ip_str)
                    self.close("keep alive")
                    return

                continue

            # self.logger.debug("http1 get task")
            time_now = time.time()
            if time_now - self.last_recv_time > self.config.http1_idle_time:
                self.logger.warn("get task but inactive time:%d", time_now - self.last_recv_time)
                self.task = task
                self.close("inactive timeout %d" % (time_now - self.last_recv_time))
                return

            self.request_task(task)
            self.request_onway = False
            self.last_send_time = time_now

            life_end_reason = self.is_life_end()
            if life_end_reason:
                self.close("life_end:" + life_end_reason)
                return

    def _send_data(self, task, data, expected_attempt=None):
        sent = 0
        while sent < len(data):
            time_left = task.deadline - time.monotonic()
            if time_left <= 0:
                raise socket.timeout("h1 send deadline")

            self.ssl_sock.settimeout(time_left)
            with task._state_lock:
                if expected_attempt is not None and expected_attempt != task.attempt_id:
                    raise socket.error("h1 send superseded")
                sent_now = self.ssl_sock.send(data[sent:sent + 65535])
            if not sent_now:
                raise socket.error("h1 send returned no progress")
            sent += sent_now

    def request_task(self, task):
        attempt = getattr(self, "task_attempt", task.attempt_id)
        timeout = task.timeout
        self.request_onway = True
        start_time = time.time()

        self.record_active("request")
        task.set_state("h1_req")

        task.headers[b'Host'] = self.get_host(task.host)

        request_len = len(task.body)
        task.headers[b"Content-Length"] = request_len
        request_data = b'%s %s HTTP/1.1\r\n' % (task.method, task.path)
        request_data += pack_headers(task.headers)
        request_data += b'\r\n'

        try:
            self._send_data(task, request_data, expected_attempt=attempt)
            self._send_data(task, task.body, expected_attempt=attempt)
            task.set_state("h1_req_sent")
        except Exception as e:
            self.logger.warn("%s %s h1_request send:%r inactive_time:%d task.timeout:%d",
                             self.ip_str, self.ssl_sock.getsockname(),
                             e, time.time() - self.last_recv_time, task.timeout)
            self.logger.warn('%s trace:%s', self.ip_str, self.get_trace())

            self.retry_task_cb(task, expected_attempt=attempt)
            self.task = None
            self.close("send fail")
            return

        try:
            response = simple_http_client.Response(self.ssl_sock)
            while True:
                time_left = max(0, task.deadline - time.monotonic())
                response.begin(timeout=time_left)
                if 100 <= response.status < 200 and response.status != 101:
                    continue
                break
            task.set_state("response_begin")
            self.last_recv_time = time.time()
        except Exception as e:
            self.logger.warn("%s h1_request recv:%r inactive_time:%d task.timeout:%d",
                             self.ip_str, e, time.time() - self.last_recv_time, task.timeout)
            self.logger.warn('%s trace:%s', self.ip_str, self.get_trace())

            self.retry_task_cb(task, expected_attempt=attempt)
            self.task = None
            self.close("recv fail")
            return

        if response.status == 101:
            self.retry_task_cb(task, expected_attempt=attempt)
            self.task = None
            self.close("upgrade unsupported")
            return

        task.set_state("h1_get_head")

        has_body = simple_http_client.response_has_body(task.method, response.status)
        if not has_body:
            response.content_length = 0

        response.ssl_sock = self.ssl_sock
        response.task = task
        response.worker = self
        task.content_length = response.content_length
        if not task.mark_responsed(expected_attempt=attempt):
            self.task = None
            self.close("response superseded")
            return
        if task.queue:
            task.queue.put(response)

        if self.config.http2_show_debug:
                self.logger.debug("got res for %s status:%d", self.ip_str, response.status)

        read_target = None
        if has_body and not response.chunked and response.content_length not in (None, b""):
            try:
                read_target = int(response.content_length)
                if read_target < 0:
                    raise ValueError("negative content length")
            except (TypeError, ValueError):
                task.finish(expected_attempt=attempt)
                self.close("down fail")
                return

        data_len = 0
        while has_body and read_target != 0:
            if read_target is not None:
                remaining_body = read_target - data_len
                if remaining_body <= 0:
                    break
            else:
                remaining_body = None

            time_left = max(0, task.deadline - time.monotonic())
            try:
                data = response.read(read_len=remaining_body, timeout=time_left)
            except Exception as e:
                self.logger.warn("read fail, ip:%s, chunk:%d url:%s task.timeout:%d e:%r",
                                 self.ip_str, response.chunked, task.url, task.timeout, e)
                self.logger.warn('%s trace:%s', self.ip_str, self.get_trace())
                task.finish(expected_attempt=attempt)
                self.close("read fail")
                return

            if not data:
                if response.chunked:
                    complete = response.chunked_complete
                else:
                    complete = read_target is None and response.eof
                if complete:
                    break

                self.logger.warn("read fail, ip:%s, chunk:%d url:%s task.timeout:%d ",
                                 self.ip_str, response.chunked, task.url, task.timeout)
                self.ip_manager.recheck_ip(self.ssl_sock.ip_str)
                task.finish(expected_attempt=attempt)
                self.close("down fail")
                return

            if remaining_body is not None and len(data) > remaining_body:
                task.finish(expected_attempt=attempt)
                self.close("down fail")
                return

            if not task.put_data(data, expected_attempt=attempt):
                self.task = None
                self.close("response superseded")
                return
            data_len += len(data)

        if not task.finish(complete=True, expected_attempt=attempt):
            self.task = None
            self.close("response superseded")
            return

        if read_target is None and not response.chunked and response.eof:
            # EOF 定界的连接之后不能继续复用。
            self.task = None
            self.close("eof close")
            return

        self.ssl_sock.received_size += data_len

        time_now = time.time()
        time_cost = (time_now - start_time)
        xcost = float(response.headers.get(b"X-Cost", -1))
        if isinstance(xcost, list):
            xcost = float(xcost[0])

        road_time = time_cost - xcost
        if xcost != -1 and road_time > 0:
            self.update_speed(road_time, request_len, data_len)

        task.set_state("h1_finish[RTT:%d]" % (road_time * 1000))

        self.transfered_size += len(request_data) + data_len
        self.task = None
        self.accept_task = True
        self.idle_cb()
        self.processed_tasks += 1
        self.last_recv_time = time.time()
        self.record_active("Res")

    def head_request(self):
        if not self.ssl_sock.host:
            # self.logger.warn("try head but no host set")
            return True

        # for keep alive, not work now.
        self.request_onway = True
        self.record_active("head")
        # start_time = time.time()
        # self.logger.debug("head request %s", self.ip)
        request_data = b'GET / HTTP/1.1\r\nHost: %s\r\n\r\n' % utils.to_bytes(self.ssl_sock.host)

        try:
            data = request_data
            ret = self.ssl_sock.send(data)
            if ret != len(data):
                allowed, suppressed = self.head_fail_throttle.allow(self.ssl_sock.ip_str)
                if allowed:
                    self.logger.warn("h1 head send len:%r %d %s", ret, len(data), self.ip_str)
                    self.logger.warn('%s trace:%s%s', self.ip_str, self.get_trace(),
                                     " (suppressed %d)" % suppressed if suppressed else "")
                return False
            response = simple_http_client.Response(self.ssl_sock)
            response.begin(timeout=5)

            status = response.status
            if status not in [200, 404]:
                allowed, suppressed = self.head_fail_throttle.allow(self.ssl_sock.ip_str)
                if allowed:
                    self.logger.warn("%s host:%s head fail status:%d", self.ip_str, self.ssl_sock.host, status)
                    self.logger.warn('%s trace:%s%s', self.ip_str, self.get_trace(),
                                     " (suppressed %d)" % suppressed if suppressed else "")
                return False

            content = response.readall(timeout=5)
            self.record_active("head end")
            self.last_recv_time = time.time()
            return True
        except Exception as e:
            allowed, suppressed = self.head_fail_throttle.allow(self.ssl_sock.ip_str)
            if allowed:
                self.logger.warn("h1 %s HEAD keep alive request fail:%r", self.ssl_sock.ip_str, e)
                self.logger.warn('%s trace:%s%s', self.ip_str, self.get_trace(),
                                 " (suppressed %d)" % suppressed if suppressed else "")
            return False
        finally:
            self.request_onway = False

    def close(self, reason=""):
        # 先在同一把锁内拒绝新 request，再发布退出哨兵并捕获当前 attempt。
        with self._lock:
            self.accept_task = False
            task = self.task
            attempt = getattr(self, "task_attempt", None)
            self.task = None
            self.task_queue.put(None)

        if task is not None:
            if task.responsed:
                task.finish(expected_attempt=attempt)
            else:
                self.retry_task_cb(task, expected_attempt=attempt)

        super(Http1Worker, self).close(reason)
