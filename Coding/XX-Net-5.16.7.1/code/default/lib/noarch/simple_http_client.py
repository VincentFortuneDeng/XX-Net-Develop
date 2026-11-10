
import selectors2 as selectors
from xlog import getLogger
xlog = getLogger("simple_http_client")

try:
    # py3
    from urllib.parse import urlparse, urlsplit
    from http.client import IncompleteRead
except ImportError:
    # py2
    from urlparse import urlparse, urlsplit
    from httplib import IncompleteRead

import socket
import time
import os
import utils
import ssl


def response_has_body(method, status):
    method = utils.to_bytes(method).upper()
    return method != b"HEAD" and not (100 <= status < 200) and status not in (204, 304)


class Connection():
    def __init__(self, sock):
        self.sock = sock
        self.create_time = time.time()

    def close(self):
        self.sock.close()


class BaseResponse(object):
    def __init__(self, status=601, reason=b"", headers={}, body=b""):
        self.status = status
        self.reason = reason
        self.headers = {}
        for key in headers:
            if isinstance(key, tuple):
                key, value = key
            else:
                value = headers[key]
            key = key.title()
            self.headers[key] = value

        self.text = body

    def getheader(self, key, default_value=b""):
        key = key.title()
        if key in self.headers:
            return self.headers[key]
        else:
            return default_value


class TxtResponse(BaseResponse):
    def __init__(self, buf):
        BaseResponse.__init__(self)
        if isinstance(buf, memoryview):
            self.view = buf
            self.read_buffer = buf.tobytes()
        elif isinstance(buf, str):
            self.read_buffer = utils.to_bytes(buf)
            self.view = memoryview(self.read_buffer)
        elif isinstance(buf, bytes):
            self.read_buffer = buf
            self.view = memoryview(buf)
        else:
            raise Exception("TxtResponse error")

        self.buffer_start = 0
        self.version = None
        self.info = None
        self.body = None
        self.parse()

    def read_line(self):
        n1 = self.read_buffer.find(b"\r\n", self.buffer_start)
        if n1 == -1:
            raise Exception("read_line fail")

        line = self.read_buffer[self.buffer_start:n1]
        self.buffer_start = n1 + 2
        return line

    def read_headers(self):
        n1 = self.read_buffer.find(b"\r\n\r\n", self.buffer_start)
        if n1 == -1:
            raise Exception("read_headers fail")
        block = self.read_buffer[self.buffer_start:n1]
        self.buffer_start = n1 + 4
        return block

    def parse(self):
        requestline = self.read_line()
        words = requestline.split()
        if len(words) < 2:
            raise Exception("status line:%s" % requestline)

        self.version = words[0]
        self.status = int(words[1])
        self.info = b" ".join(words[2:])

        self.headers = {}
        header_block = self.read_headers()
        lines = header_block.split(b"\r\n")
        for line in lines:
            p = line.find(b":")
            key = line[0:p]
            value = line[p + 2:]
            key = str(key.title())
            self.headers[key] = value

        self.body = self.view[self.buffer_start:]
        self.read_buffer = b""
        self.buffer_start = 0


class Response(BaseResponse):

    def __init__(self, sock):
        BaseResponse.__init__(self)
        self.sock = sock
        self.sock.settimeout(1)
        self.sock.setblocking(0)
        self.read_buffer = b""
        self.buffer_start = 0
        self.chunked = False
        self.chunked_complete = False
        self.eof = False
        self.version = None
        self.content_length = None
        self.select2 = selectors.DefaultSelector()
        self.select2.register(sock, selectors.EVENT_READ)

    def __del__(self):
        try:
            self.select2.unregister(self.sock)
        except:
            pass

        try:
            self.select2.close()
        except:
            pass

    def recv(self, to_read=8192, timeout=30.0):
        if self.eof:
            return b""
        if timeout < 0:
            raise Exception("recv timeout")

        start_time = time.time()
        end_time = start_time + timeout
        while time.time() < end_time:
            try:
                data = self.sock.recv(to_read)
                if data == b"":
                    self.eof = True
                return data
            except (BlockingIOError, socket.error) as e:
                if e.errno in [2, 11, 35, 60, 10035]:
                    time_left = end_time - time.time()
                    if time_left < 0:
                        break

                    # select.select([self.sock], [], [self.sock], time_left)
                    self.select2.select(timeout=time_left)
                    continue
                else:
                    raise e

        raise Exception("recv timeout")

    def read_line(self, timeout=60.0):
        start_time = time.time()
        end_time = start_time + timeout
        while True:
            n1 = self.read_buffer.find(b"\r\n", self.buffer_start)
            if n1 > -1:
                line = self.read_buffer[self.buffer_start:n1]
                self.buffer_start = n1 + 2
                return line

            if time.time() > end_time:
                raise socket.timeout()

            time_left = end_time - time.time()
            data = self.recv(8192, time_left)

            if isinstance(data, int):
                continue
            if data and len(data):
                self.read_buffer += data
            else:
                if self.eof:
                    raise socket.error("response EOF before line complete")
                time_left = end_time - time.time()
                if time_left < 0:
                    raise socket.error

    def read_headers(self, timeout=60.0):
        start_time = time.time()
        lines = []
        while True:
            left_time = timeout - (time.time() - start_time)
            line = self.read_line(left_time)
            if len(line.strip()) == 0:
                return b"\r\n".join(lines)

            lines.append(line)

    def begin(self, timeout=60.0):
        start_time = time.time()
        line = self.read_line(timeout)

        requestline = line.rstrip(b'\r\n')
        words = requestline.split()
        if len(words) < 2:
            raise Exception("status line:%s" % requestline)

        self.version = words[0]
        self.status = int(words[1])
        self.reason = b" ".join(words[2:])

        self.headers = {}
        self.chunked = False
        self.chunked_complete = False
        timeout = max(0, timeout - (time.time() - start_time))
        header_block = self.read_headers(timeout)
        lines = header_block.split(b"\r\n")
        for line in lines:
            p = line.find(b":")
            key = line[0:p]
            value = line[p + 2:]
            key = key.title()
            self.headers[key] = value

        self.content_length = self.getheader(b"content-length", b"")
        if b"chunked" in self.getheader(b"Transfer-Encoding", b""):
            self.chunked = True

        if b"gzip" in self.getheader(b"Transfer-Encoding", b""):
            print("gzip not work")

    def _read_plain(self, read_len, timeout):
        if read_len == 0:
            return ""

        if read_len is not None and len(self.read_buffer) - self.buffer_start >= read_len:
            out_str = self.read_buffer[self.buffer_start:self.buffer_start + read_len]
            self.buffer_start += read_len
            if len(self.read_buffer) == self.buffer_start:
                self.read_buffer = b""
                self.buffer_start = 0
            return out_str

        start_time = time.time()
        end_time = start_time + timeout
        out_len = len(self.read_buffer) - self.buffer_start
        out_list = [self.read_buffer[self.buffer_start:]]

        self.read_buffer = b""
        self.buffer_start = 0

        while time.time() - start_time < timeout:
            if not read_len and out_len > 0:
                break

            if read_len and out_len >= read_len:
                break

            if read_len:
                to_read = read_len - out_len
                to_read = min(to_read, 65535)
            else:
                to_read = 65535

            time_left = end_time - time.time()
            data = self.recv(to_read, time_left)

            if data:
                out_list.append(data)
                out_len += len(data)
            elif self.eof:
                break
            else:
                time_left = start_time + timeout - time.time()
                if time_left < 0:
                    raise socket.error

                # r, w, e = select.select([self.sock], [], [self.sock], time_left)
                events = self.select2.select(timeout=time_left)
                for key, event in events:
                    if not event & selectors.EVENT_READ:
                        raise socket.error

        if read_len is not None and out_len < read_len:
            raise socket.timeout()

        return b"".join(out_list)

    def _read_size(self, read_len, timeout):
        if len(self.read_buffer) - self.buffer_start > read_len:
            buf = memoryview(self.read_buffer)
            out_str = buf[self.buffer_start:self.buffer_start + read_len]
            self.buffer_start += read_len
            if len(self.read_buffer) == self.buffer_start:
                self.read_buffer = b""
                self.buffer_start = 0
            return out_str

        start_time = time.time()
        out_len = len(self.read_buffer) - self.buffer_start
        out_bytes = bytearray(read_len)
        view = memoryview(out_bytes)
        view[0:out_len] = self.read_buffer[self.buffer_start:]

        self.read_buffer = b""
        self.buffer_start = 0

        while time.time() - start_time < timeout:
            if out_len >= read_len:
                break

            to_read = read_len - out_len
            to_read = min(to_read, 65535)

            try:
                nbytes = self.sock.recv_into(view[out_len:], to_read)
            except (BlockingIOError, socket.error) as e:
                if e.errno in [2, 11, 35, 60, 10035]:
                    time_left = start_time + timeout - time.time()
                    if time_left < 0:
                        raise socket.timeout

                    # select.select([self.sock], [], [self.sock], time_left)
                    self.select2.select(timeout=time_left)
                    continue
                else:
                    raise e

            out_len += nbytes
        if out_len < read_len:
            raise socket.timeout()

        return out_bytes

    def _read_chunked(self, timeout):
        start_time = time.time()
        line = self.read_line(timeout)
        chunk_size = int(line, 16)
        if chunk_size == 0:
            # 零块之后还有可选 trailer，必须读到终止空行。
            while True:
                time_left = max(0, timeout - (time.time() - start_time))
                if not self.read_line(time_left):
                    break
            self.chunked_complete = True
            return b""

        time_left = max(0, timeout - (time.time() - start_time))
        dat = self._read_plain(chunk_size + 2, time_left)
        return dat[:-2]

    def read(self, read_len=None, timeout=60):
        if not self.chunked:
            data = self._read_plain(read_len, timeout)
        else:
            data = self._read_chunked(timeout)
        return data

    def readall(self, timeout=60):
        start_time = time.time()
        if self.chunked:
            out_list = []
            while True:
                time_left = timeout - (time.time() - start_time)
                if time_left < 0:
                    raise socket.timeout()

                dat = self._read_chunked(time_left)
                if not dat:
                    break

                out_list.append(dat)

            return b"".join(out_list)
        else:
            return self._read_plain(int(self.content_length), timeout=timeout)


class Client(object):
    def __init__(self, proxy=None, timeout=60, cert=""):
        self.timeout = timeout
        self.cert = cert
        self.sock = None
        self.host = None
        self.port = None
        self.tls = None
        self.ssl_context = None

        if isinstance(proxy, str):
            proxy_sp = urlsplit(proxy)

            self.proxy = {
                "type": proxy_sp.scheme,
                "host": proxy_sp.hostname,
                "port": proxy_sp.port,
                "user": proxy_sp.username,
                "pass": proxy_sp.password
            }
        elif isinstance(proxy, dict):
            self.proxy = proxy
        else:
            self.proxy = None

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None

    @staticmethod
    def direct_connect(host, port, timeout=30):
        deadline = time.monotonic() + max(0, timeout)

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
            remaining = max(0, deadline - time.monotonic())
            if remaining <= 0:
                break
            af, socktype, proto, canonname, sa = res
            ip_port = (sa[0], sa[1])
            s = None
            try:
                s = socket.socket(af, socktype, proto)
                # See http://groups.google.com/group/cherrypy-users/
                #        browse_frm/thread/bbfe5eb39c904fe0

                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 32 * 1024)
                s.setsockopt(socket.SOL_TCP, socket.TCP_NODELAY, True)
                s.settimeout(remaining)
                s.connect(ip_port)
                s.settimeout(max(0, deadline - time.monotonic()))
                return s
            except socket.error as e:
                xlog.warn("direct connect %s except:%r", sa, e)
                if s:
                    s.close()

        return None

    def connect(self, host, port, tls, timeout=None):
        if self.sock and host == self.host and port == self.port and self.tls == tls:
            return self.sock

        connect_timeout = self.timeout if timeout is None else max(0, timeout)
        deadline = time.monotonic() + connect_timeout
        sock = None
        try:
            if not self.proxy:
                sock = self.direct_connect(host, port, max(0, deadline - time.monotonic()))
                if not sock:
                    return None
            else:
                import socks

                sock = socks.socksocket(socket.AF_INET)
                sock.set_proxy(proxy_type=self.proxy["type"],
                               addr=self.proxy["host"],
                               port=self.proxy["port"], rdns=True,
                               username=self.proxy["user"],
                               password=self.proxy["pass"])

                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 32 * 1024)
                sock.setsockopt(socket.SOL_TCP, socket.TCP_NODELAY, True)
                sock.settimeout(connect_timeout)
                sock.connect((host, port))

                # conn_time = time.time() - start_time
                # xlog.debug("proxy:%s tcp conn:%s time:%d", proxy["host"], host, conn_time * 1000)

            if tls:
                remaining = max(0, deadline - time.monotonic())
                if remaining <= 0:
                    sock.close()
                    return None
                sock.settimeout(remaining)
                if not self.ssl_context:
                    self.ssl_context = ssl.create_default_context()
                    self.ssl_context.check_hostname = False
                    self.ssl_context.verify_mode = ssl.CERT_REQUIRED

                if os.path.isfile(self.cert):
                    sock = self.ssl_context.wrap_socket(sock, server_hostname=host)
        except Exception:
            # 尚未写入 self.sock 的新连接只能由当前栈回收。
            if sock is not None:
                sock.close()
            raise

        old_sock = self.sock
        self.sock = sock
        self.host = host
        self.port = port
        self.tls = tls
        if old_sock is not None:
            # 只有新连接已成功，才替换并回收旧连接。
            old_sock.close()

        return sock

    def request(self, method, url, headers=None, body=b"", read_payload=True):
        deadline = time.monotonic() + self.timeout
        if headers is None:
            headers = {}
        method = utils.to_bytes(method)
        url = utils.to_bytes(url)

        upl = urlsplit(url)
        headers["Content-Length"] = str(len(body))
        headers["Host"] = upl.netloc
        port = upl.port
        if not port:
            if upl.scheme == b"http":
                port = 80
            elif upl.scheme == b"https":
                port = 443
            else:
                raise Exception("unknown method:%s" % upl.scheme)

        path = upl.path
        if not path:
            path = b"/"

        if upl.query:
            path += b"?" + upl.query

        try:
            sock = self.connect(upl.hostname, port, upl.scheme == b"https",
                                max(0, deadline - time.monotonic()))
        except Exception as e:
            xlog.warn("connect %s:%s fail:%r", upl.hostname, port, e)
            return None

        if not sock:
            return None

        request_data = b'%s %s HTTP/1.1\r\n' % (method, path)

        for k, v in headers.items():
            if isinstance(v, int):
                request_data += b'%s: %d\r\n' % (utils.to_bytes(k), v)
            else:
                request_data += b'%s: %s\r\n' % (utils.to_bytes(k), utils.to_bytes(v))

        request_data += b'\r\n'

        body = utils.to_bytes(body)

        try:
            if len(request_data) + len(body) < 1300:
                body = request_data + body
                request_data = b""

            for data in (request_data, body):
                payload_len = len(data)
                start = 0
                while start < payload_len:
                    sock.settimeout(max(0, deadline - time.monotonic()))
                    send_size = min(payload_len - start, 65535)
                    sended = sock.send(data[start:start + send_size])
                    if not sended:
                        raise socket.error("request send returned no progress")
                    start += sended

            response = Response(sock)
            while True:
                response.begin(timeout=max(0, deadline - time.monotonic()))
                if 100 <= response.status < 200 and response.status != 101:
                    continue
                break
        except Exception as e:
            self.close()
            return None

        try:
            if response.status == 101:
                self.close()
                return response

            if not read_payload:
                return response

            if not response_has_body(method, response.status):
                response.text = b""
                return response

            if b'Transfer-Encoding' in response.headers:
                data_buffer = []
                while True:
                    try:
                        data = response.read(8192, timeout=max(0, deadline - time.monotonic()))
                    except IncompleteRead as e:
                        data = e.partial

                    if not data:
                        break
                    else:
                        data_buffer.append(data)

                response.text = b"".join(data_buffer)
                return response

            content_length_header = response.getheader(b'Content-Length', None)
            if content_length_header is not None:
                content_length = int(content_length_header)
                if content_length:
                    response.text = response.read(
                        content_length, timeout=max(0, deadline - time.monotonic()))
                else:
                    response.text = b""
                return response

            data_buffer = []
            while True:
                data = response.read(timeout=max(0, deadline - time.monotonic()))
                if not data:
                    if response.eof:
                        break
                    raise socket.timeout("EOF-delimited response body read timeout")
                data_buffer.append(data)

            response.text = b"".join(data_buffer)
            self.close()
            return response
        except Exception:
            self.close()
            return None


def request(method="GET", url=None, headers=None, body=b"", proxy=None, timeout=60, read_payload=True):
    if headers is None:
        headers = {}
    if not url:
        raise Exception("no url")

    client = Client(proxy, timeout=timeout)
    return client.request(method, url, headers, body, read_payload)
