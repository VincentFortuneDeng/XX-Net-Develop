import time

from six.moves import queue
import threading
import socket
import errno
import struct
from ssl import SSLError

from .http_common import *


from hyper.common.bufsocket import BufferedSocket
from hyper.common.exceptions import ConnectionResetError

from hyper.packages.hyperframe.frame import (
    FRAMES, DataFrame, HeadersFrame, PushPromiseFrame, RstStreamFrame,
    SettingsFrame, Frame, WindowUpdateFrame, GoAwayFrame, PingFrame,
    BlockedFrame, FRAME_MAX_ALLOWED_LEN, FRAME_MAX_LEN
)
from .http2_stream import Stream
from hyper.http20.window import BaseFlowControlManager

from hyper.packages.hpack import Encoder, Decoder

# this is defined in rfc7540
# default window size 64k
DEFAULT_WINDOW_SIZE = 65535

# default max frame is 16k, defined in rfc7540
DEFAULT_MAX_FRAME = FRAME_MAX_LEN


class FlowControlManager(BaseFlowControlManager):
    """
    ``hyper``'s default flow control manager.

    This implements hyper's flow control algorithms. This algorithm attempts to
    reduce the number of WINDOWUPDATE frames we send without blocking the remote
    endpoint behind the flow control window.

    This algorithm will become more complicated over time. In the current form,
    the algorithm is very simple:
        - When the flow control window gets less than 3/4 of the maximum size,
          increment back to the maximum.
        - Otherwise, if the flow control window gets to less than 1kB, increment
          back to the maximum.
    """
    def increase_window_size(self, frame_size):
        future_window_size = self.window_size - frame_size

        if ((future_window_size < (self.initial_window_size * 3 / 4)) or
            (future_window_size < 1000)):
            return self.initial_window_size - future_window_size

        return 0

    def blocked(self):
        return self.initial_window_size - self.window_size


class RawFrame(object):
    def __init__(self, dat):
        self.dat = dat

    def serialize(self):
        return self.dat

    def __repr__(self):
        out_str = "{type}".format(type=type(self).__name__)
        return out_str


class Http2Worker(HttpWorker):
    version = "2"

    def __init__(self, logger, ip_manager, config, ssl_sock, close_cb, retry_task_cb, idle_cb, log_debug_data,
                 stream_class=None):
        super(Http2Worker, self).__init__(
            logger, ip_manager, config, ssl_sock, close_cb, retry_task_cb, idle_cb, log_debug_data)
        self.version = "2"

        self.network_buffer_size = 65535
        if stream_class:
            self.stream_class = stream_class
        else:
            self.stream_class = Stream

        # Google http/2 time out is 4 mins.
        self.ssl_sock.settimeout(240)
        self._sock = BufferedSocket(ssl_sock, self.network_buffer_size)

        self.next_stream_id = 1
        self.streams = {}
        self.last_ping_time = time.time()
        self.continue_timeout = 0

        # count ping not ACK
        # increase when send ping
        # decrease when recv ping ack
        # if this in not 0, don't accept request.
        self.ping_on_way = 0
        self.accept_task = False

        # request/close 只在这里交换状态，网络回调在锁外执行。
        self._state_lock = threading.RLock()
        self._closing = False

        # request_lock
        self.request_lock = threading.Lock()

        # all send frame must put to this queue
        # then send by send_loop
        # every frame put to this queue must allowed by stream window and connection window
        # any data frame blocked by connection window should put to self.blocked_send_frames
        self.send_queue = queue.Queue()
        self._send_lock = threading.RLock()
        self._header_lock = threading.RLock()
        self.encoder = Encoder()
        self.decoder = Decoder()

        # keep blocked data frame in this buffer
        # which is allowed by stream window but blocked by connection window.
        # They will be sent when connection window open
        self.blocked_send_frames = []

        # Values for the settings used on an HTTP/2 connection.
        # will send to remote using Setting Frame
        self.local_settings = {
            SettingsFrame.INITIAL_WINDOW_SIZE: 16 * 1024 * 1024,
            SettingsFrame.SETTINGS_MAX_FRAME_SIZE: 256 * 1024
        }
        self.local_connection_initial_windows = 32 * 1024 * 1024
        self.local_window_manager = FlowControlManager(self.local_connection_initial_windows)

        # changed by server, with SettingFrame
        self.remote_settings = {
            SettingsFrame.INITIAL_WINDOW_SIZE: DEFAULT_WINDOW_SIZE,
            SettingsFrame.SETTINGS_MAX_FRAME_SIZE: DEFAULT_MAX_FRAME,
            SettingsFrame.MAX_CONCURRENT_STREAMS: 100
        }

        self.remote_window_size = DEFAULT_WINDOW_SIZE

        # send Setting frame before accept task.
        self._send_preamble()

        threading.Thread(target=self.h2_send_loop, name="h2_send_%s" % self.ip_str).start()
        threading.Thread(target=self.h2_recv_loop, name="h2_recv_%s" % self.ip_str).start()

    # export api
    def request(self, task):
        task.set_state("h2_req")
        if not self.request_task(task):
            # 未接受的 attempt 必须先交还 claim，才能重新排队。
            task.clear_retry()
            self.retry_task_cb(task)

    def encode_header(self, headers):
        return self.encoder.encode(headers)

    def request_task(self, task):
        with self.request_lock:
            with self._state_lock:
                if self._closing or not self.keep_running:
                    return False

                stream_id = self.next_stream_id
                self.next_stream_id += 2
                stream = self.stream_class(
                    self.logger, self.config, self, self.ip_str, stream_id, task,
                    self._send_cb, self._close_stream_cb, self.encode_header, self.decoder,
                    FlowControlManager(self.local_settings[SettingsFrame.INITIAL_WINDOW_SIZE]),
                    self.remote_settings[SettingsFrame.INITIAL_WINDOW_SIZE],
                    self.remote_settings[SettingsFrame.SETTINGS_MAX_FRAME_SIZE])

                # 构造期间可能发生重入 close，登记前必须再次确认。
                if self._closing or not self.keep_running:
                    return False
                # 新 attempt 在接受点取得单调身份，旧 stream 的迟到回调不得再操作 Task。
                stream.attempt_id = task.begin_attempt()
                self.streams[stream_id] = stream

            if len(self.streams) > self.config.http2_max_concurrent:
                self.accept_task = False

        try:
            stream.start_request()
        except Exception:
            stream.close("request fail")
        return True

    def get_streams(self):
        with self._state_lock:
            return list(self.streams.values())

    def _should_send_frame(self, frame):
        stream_id = getattr(frame, "stream_id", 0)
        if not stream_id:
            return True

        with self._state_lock:
            stream = self.streams.get(stream_id)
            if stream is None or getattr(stream, "_closed", False):
                return False
            task = stream.task
            attempt = getattr(stream, "attempt_id", None)
            if attempt is not None and attempt != task.attempt_id:
                return False
            return not task.finished and not task.retry_pending and time.monotonic() < task.deadline

    def h2_send_loop(self):
        while self.keep_running:
            frame = self.send_queue.get(True)
            if not frame:
                # None frame means exist
                break

            if not self._should_send_frame(frame):
                if frame.type == DataFrame.type:
                    # 只有 DATA 已扣除连接窗口，丢弃时必须返还。
                    with self._send_lock:
                        self.remote_window_size += len(frame.data)
                continue

            if self.config.http2_show_debug:
                self.logger.debug("%s Send:%s", self.ip_str, str(frame))
            data = frame.serialize()
            try:
                self._sock.send(data, flush=False)
                # don't flush for small package
                # reduce send api call

                if self.send_queue._qsize():
                    continue

                # wait for payload frame
                # time.sleep(0.01)
                # combine header and payload in one tcp package.
                if not self.send_queue._qsize():
                    self._sock.flush()

                self.last_send_time = time.time()
            except socket.error as e:
                if e.errno not in (errno.EPIPE, errno.ECONNRESET):
                    self.logger.warn("%s http2 send fail:%r", self.ip_str, e)
                else:
                    self.logger.exception("send error:%r", e)

                self.close("send fail:%r" % e)
            except Exception as e:
                self.logger.debug("http2 %s send error:%r", self.ip_str, e)
                self.close("send fail:%r" % e)

    def h2_recv_loop(self):
        while self.keep_running:
            try:
                self._consume_single_frame()
            except Exception as e:
                self.logger.exception("recv fail:%r", e)
                self.close("recv fail:%r" % e)

    def close(self, reason="conn close"):
        with self._state_lock:
            if self._closing:
                return
            self._closing = True
            self.accept_task = False
            streams = list(self.streams.values())

        # Notify loop to exit
        # This function may be call by out side http2
        # When gae_proxy found the appid or ip is wrong
        if reason.startswith("GoAway") or reason in ["life end"]:
            life_time = time.time() - self.ssl_sock.create_time
            self.logger.debug("%s close, reason: %s, life_time:%d trace:%s",
                              self.ip_str, reason, life_time, self.get_trace())
        else:
            self.logger.warn("%s close, reason: %s, trace:%s", self.ip_str, reason, self.get_trace())
        self.send_queue.put(None)

        for stream in streams:
            stream.close(reason=reason)
        super(Http2Worker, self).close(reason)

    def send_ping(self):
        p = PingFrame(0)
        p.opaque_data = struct.pack("!d", time.time())
        #self.send_queue.put(p)
        self._send_cb(p)
        self.last_ping_time = time.time()
        self.ping_on_way += 1

    def _send_preamble(self):
        self.send_queue.put(RawFrame(b'PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n'))

        f = SettingsFrame(0)
        f.settings[SettingsFrame.ENABLE_PUSH] = 0
        f.settings[SettingsFrame.INITIAL_WINDOW_SIZE] = self.local_settings[SettingsFrame.INITIAL_WINDOW_SIZE]
        f.settings[SettingsFrame.SETTINGS_MAX_FRAME_SIZE] = self.local_settings[SettingsFrame.SETTINGS_MAX_FRAME_SIZE]
        self._send_cb(f)

        # update local connection windows size
        f = WindowUpdateFrame(0)
        f.window_increment = self.local_connection_initial_windows - DEFAULT_WINDOW_SIZE
        self._send_cb(f)

    def increase_remote_window_size(self, inc_size):
        # 窗口扣减、阻塞帧搬运和恢复共用同一账本边界。
        with self._send_lock:
            self.remote_window_size += inc_size
            while self.blocked_send_frames:
                frame = self.blocked_send_frames[0]
                if len(frame.data) > self.remote_window_size:
                    break

                self.remote_window_size -= len(frame.data)
                self.blocked_send_frames.pop(0)
                self.send_queue.put(frame)

        if self.keep_running and \
                self.accept_task == False and \
                len(self.streams) < self.config.http2_max_concurrent and \
                self.remote_window_size > 10000:
            self.accept_task = True
            self.idle_cb()

    def _send_cb(self, frame):
        # HEADERS 不受 DATA 连接流控限制。
        if frame.type != DataFrame.type:
            self.send_queue.put(frame)
            return

        with self._send_lock:
            if len(frame.data) > self.remote_window_size:
                self.blocked_send_frames.append(frame)
                self.accept_task = False
                return

            self.remote_window_size -= len(frame.data)
            self.send_queue.put(frame)

    def _close_stream_cb(self, stream_id, reason):
        # 只有真实删除才计数；重复 close 不制造处理量。
        with self._state_lock:
            try:
                del self.streams[stream_id]
            except KeyError:
                return
            self.processed_tasks += 1

        with self._send_lock:
            self.blocked_send_frames = [
                frame for frame in self.blocked_send_frames
                if frame.stream_id != stream_id
            ]

        if len(self.streams) == 0 and self.is_life_end():
            self.close("life end")
            return

        if self.keep_running and \
                len(self.streams) < self.config.http2_max_concurrent and \
                self.remote_window_size > 10000:
            self.accept_task = True
            self.idle_cb()

    def _consume_single_frame(self):
        try:
            header = self._sock.recv(9)
        except SSLError as e:
            self.close("recv.ssl error:%r" % e)
            return
        except socket.timeout as e:
            self.logger.debug("%s _consume_single_frame:%r, inactive time:%d", self.ip_str, e,
                                  time.time() - self.last_recv_time)
            self.close("recv.timeout:%r" % e)
            return
        except ConnectionResetError as e:
            self.logger.debug("%s _consume_single_frame:%r, inactive time:%d", self.ip_str, e,
                                  time.time() - self.last_recv_time)
            self.close("ConnectionReset:%r" % e)
            return
        except Exception as e:
            if self.keep_running:
                self.logger.warn("%s _consume_single_frame:%r, inactive time:%d", self.ip_str, e, time.time() - self.last_recv_time)
            self.close("ConnectionReset:%r" % e)
            return
        self.last_recv_time = time.time()

        # Parse the header. We can use the returned memoryview directly here.
        frame, length = Frame.parse_frame_header(header)
        #self.logger.debug("h2 %s recv %s", self.ip_str, frame)

        if length > FRAME_MAX_ALLOWED_LEN:
            self.logger.error("%s Frame size exceeded on stream %d (received: %d, max: %d)",
                              self.ip_str, frame.stream_id, length, FRAME_MAX_LEN)
            # self._send_rst_frame(frame.stream_id, 6) # 6 = FRAME_SIZE_ERROR

        try:
            data = self._recv_payload(length)
        except Exception as e:
            self.close("ConnectionReset:%r" % e)
            return

        self._consume_frame_payload(frame, data)

    def _recv_payload(self, length):
        if not length:
            return memoryview(b'')

        buffer = bytearray(length)
        buffer_view = memoryview(buffer)
        index = 0
        data_length = -1

        # _sock.recv(length) might not read out all data if the given length
        # is very large. So it should be to retrieve from socket repeatedly.
        while length and data_length:
            data = self._sock.recv(length)
            self.last_recv_time = time.time()
            data_length = len(data)
            end = index + data_length
            buffer_view[index:end] = data[:]
            length -= data_length
            index = end

        return buffer_view[:end]

    def _consume_frame_payload(self, frame, data):
        frame.parse_body(data)

        if self.config.http2_show_debug:
            self.logger.debug("h2 %s Recv:%s", self.ip_str, str(frame))

        # Maintain our flow control window. We do this by delegating to the
        # chosen WindowManager.
        if frame.type == DataFrame.type:

            size = frame.flow_controlled_length
            increment = self.local_window_manager._handle_frame(size)

            if increment < 0:
                self.logger.warn("increment:%d", increment)
            elif increment:
                #self.logger.debug("%s frame size:%d increase win:%d", self.ip, size, increment)
                w = WindowUpdateFrame(0)
                w.window_increment = increment
                self._send_cb(w)

        elif frame.type == PushPromiseFrame.type:
            self.logger.error("%s receive push frame", self.ip_str, )

        # Work out to whom this frame should go.
        if frame.stream_id != 0:
            try:
                stream = self.streams[frame.stream_id]
                stream.receive_frame(frame)
            except KeyError as e:
                if frame.type not in [WindowUpdateFrame.type]:
                    self.logger.warn("%s Unexpected stream identifier %d, frame.type:%s e:%r",
                                          self.ip_str, frame.stream_id, frame, e)
        else:
            self.receive_frame(frame)

    def receive_frame(self, frame):
        #self.logger.debug("h2conn recv:%s", frame)
        if frame.type == WindowUpdateFrame.type:
            # self.logger.debug("WindowUpdateFrame %d", frame.window_increment)
            self.increase_remote_window_size(frame.window_increment)

        elif frame.type == PingFrame.type:
            if b'ACK' in frame.flags:
                ping_time = struct.unpack("!d", frame.opaque_data)[0]
                time_now = time.time()
                rtt = (time_now - ping_time) * 1000
                if rtt < 0:
                    self.logger.error("rtt:%f ping_time:%f now:%f", rtt, ping_time, time_now)
                self.ping_on_way -= 1
                #self.logger.debug("RTT:%d, on_way:%d", self.rtt, self.ping_on_way)
                if self.keep_running and self.ping_on_way == 0:
                    self.accept_task = True
            else:
                # The spec requires us to reply with PING+ACK and identical data.
                p = PingFrame(0)
                p.flags.add(b'ACK')
                p.opaque_data = frame.opaque_data
                self._send_cb(p)

        elif frame.type == SettingsFrame.type:
            if b'ACK' not in frame.flags:
                # send ACK as soon as possible
                f = SettingsFrame(0)
                f.flags.add(b'ACK')
                self._send_cb(f)

                # this may trigger send DataFrame blocked by remote window
                self._update_settings(frame)
            else:
                self.accept_task = True
                self.idle_cb()

        elif frame.type == GoAwayFrame.type:
            # If we get GoAway with error code zero, we are doing a graceful
            # shutdown and all is well. Otherwise, throw an exception.

            # If an error occured, try to read the error description from
            # code registry otherwise use the frame's additional data.
            error_string = frame._extra_info()
            time_cost = time.time() - self.last_recv_time
            # if frame.additional_data != b"session_timed_out":
            #     self.logger.warn("goaway:%s, t:%d", error_string, time_cost)

            self.close("GoAway:%s inactive time:%d" % (error_string, time_cost))

        elif frame.type == BlockedFrame.type:
            self.logger.warn("%s get BlockedFrame", self.ip_str)
        elif frame.type in FRAMES:
            # This frame isn't valid at this point.
            #raise ValueError("Unexpected frame %s." % frame)
            self.logger.error("%s Unexpected frame %s.", self.ip_str, frame)
        else:  # pragma: no cover
            # Unexpected frames belong to extensions. Just drop it on the
            # floor, but log so that users know that something happened.
            self.logger.error("%s Received unknown frame, type %d", self.ip_str, frame.type)

    def _update_settings(self, frame):
        if SettingsFrame.HEADER_TABLE_SIZE in frame.settings:
            new_size = frame.settings[SettingsFrame.HEADER_TABLE_SIZE]

            with self._header_lock:
                self.remote_settings[SettingsFrame.HEADER_TABLE_SIZE] = new_size
                self.encoder.header_table_size = new_size

        with self._state_lock:
            if SettingsFrame.INITIAL_WINDOW_SIZE in frame.settings:
                newsize = frame.settings[SettingsFrame.INITIAL_WINDOW_SIZE]
                oldsize = self.remote_settings[SettingsFrame.INITIAL_WINDOW_SIZE]
                delta = newsize - oldsize

                for stream in list(self.streams.values()):
                    with stream._send_lock:
                        stream.remote_window_size += delta

                self.remote_settings[SettingsFrame.INITIAL_WINDOW_SIZE] = newsize

            if SettingsFrame.SETTINGS_MAX_FRAME_SIZE in frame.settings:
                new_size = frame.settings[SettingsFrame.SETTINGS_MAX_FRAME_SIZE]
                if not (FRAME_MAX_LEN <= new_size <= FRAME_MAX_ALLOWED_LEN):
                    self.logger.error("%s Frame size %d is outside of allowed range", self.ip_str, new_size)

                    # Tear the connection down with error code PROTOCOL_ERROR
                    self.close("bad max frame size")
                    #error_string = ("Advertised frame size %d is outside of range" % (new_size))
                    #raise ConnectionError(error_string)
                    return

                self.remote_settings[SettingsFrame.SETTINGS_MAX_FRAME_SIZE] = new_size

                for stream in list(self.streams.values()):
                    with stream._send_lock:
                        stream.max_frame_size = new_size

    def get_trace(self):
        now = time.time()
        out_list = [
            " continue_timeout:%d" % self.continue_timeout, " processed:%d" % self.processed_tasks,
            " inactive:%d, %d" % (now - self.last_send_time, now - self.last_recv_time),
            " h2.stream_num:%d" % len(self.streams),
            " sni:%s, host:%s" % (self.ssl_sock.sni, self.ssl_sock.host)
        ]
        return ",".join(out_list)
