import os
import sys
import json
import platform

current_path = os.path.dirname(os.path.abspath(__file__))
python_path = os.path.abspath(os.path.join(current_path, os.pardir, os.pardir))

noarch_lib = os.path.abspath(os.path.join(python_path, 'lib', 'noarch'))
sys.path.append(noarch_lib)

root_path = os.path.abspath(os.path.join(current_path, os.pardir, os.pardir))
sys.path.append(root_path)

import env_info
data_path = env_info.data_path
data_xtunnel_path = os.path.join(data_path, 'x_tunnel')

lib_path = os.path.abspath(os.path.join(current_path, os.pardir, 'common'))
sys.path.append(lib_path)

ready = False
# don't remove, launcher web_control need it.


def create_data_path():
    if not os.path.isdir(data_path):
        os.mkdir(data_path)

    if not os.path.isdir(data_xtunnel_path):
        os.mkdir(data_xtunnel_path)


create_data_path()

from xlog import getLogger
xlog = getLogger("x_tunnel", log_path=data_xtunnel_path, save_start_log=1500, save_warning_log=True)

import os_platform
from x_tunnel.local.proxy_handler import Socks5Server
from x_tunnel.local import global_var as g
from x_tunnel.local import proxy_session
import simple_http_server
from x_tunnel.local import front_dispatcher
from x_tunnel.local import config

from x_tunnel.local import web_control
# don't remove, launcher web_control need it.


def xxnet_version():
    version_file = os.path.join(root_path, "version.txt")
    try:
        with open(version_file, "r") as fd:
            version = fd.read()
        return version
    except Exception as e:
        xlog.exception("get version fail")
    return "get_version_fail"


def get_launcher_uuid():
    launcher_config_fn = os.path.join(data_path, "launcher", "config.json")
    try:
        with open(launcher_config_fn, "r", encoding='utf-8') as fd:
            info = json.load(fd)
            return info.get("update_uuid")
    except Exception as e:
        xlog.exception("get_launcher_uuid except:%r", e)
        return ""


def start(args):
    global ready

    ready = False
    g.socks5_server = None
    g.session = None
    g.http_client = None

    try:
        g.xxnet_version = xxnet_version()
        g.client_uuid = get_launcher_uuid()
        g.system = os_platform.platform + "|" + platform.version() + "|" + str(platform.architecture()) + "|" + sys.version

        g.config = config.load_config()
        g.running = True
        front_dispatcher.init()
        g.data_path = data_path

        xlog.info("version:%s", g.xxnet_version)

        if not g.server_host or not g.server_port:
            if g.config.server_host and g.config.server_port == 443:
                xlog.info("Session Server:%s:%d", g.config.server_host, g.config.server_port)
                g.server_host = g.config.server_host
                g.server_port = g.config.server_port
                g.balance = 99999999
            elif g.config.api_server:
                pass
            else:
                xlog.debug("please check x-tunnel server in config")

        g.http_client = front_dispatcher

        g.session = proxy_session.ProxySession()

        allow_remote = args.get("allow_remote", 0)

        listen_ips = g.config.socks_host
        if isinstance(listen_ips, str):
            listen_ips = [listen_ips]
        else:
            listen_ips = list(listen_ips)

        if allow_remote and ("0.0.0.0" not in listen_ips or "::" not in listen_ips):
            listen_ips = [("0.0.0.0"),]

        for port in range(g.config.socks_port, g.config.socks_port + 2000):
            addresses = [(listen_ip, port) for listen_ip in listen_ips]
            try:
                server = simple_http_server.HTTPServer(addresses, Socks5Server, logger=xlog)
                server.init_socket()
                g.socks5_server = server
                g.bind_port = port
            except Exception:
                g.socks5_server = None
                continue
            xlog.info("Socks5 server listen:%s:%d.", g.config.socks_host, port, persist=True)
            break

        if g.socks5_server is None:
            raise OSError("no available Socks5 listen port")

        ready = True
        g.socks5_server.serve_forever()
    except Exception:
        ready = False
        stop()
        raise


def stop():
    global ready
    g.running = False
    if g.http_client:
        g.http_client.stop()
    else:
        front_dispatcher.stop()
    g.http_client = None

    if g.socks5_server:
        xlog.info("Close Socks5 server ", persist=True)
        g.socks5_server.shutdown()
        g.socks5_server = None

    # 余额/API 提交与 Session 换代共用同一边界，防止已通过检查的旧响应写入新代。
    with proxy_session.balance_lock:
        if g.session:
            xlog.info("Stopping session", persist=True)
            g.session.stop()
            g.session = None
    ready = False


if __name__ == '__main__':
    try:
        start({})
    except KeyboardInterrupt:
        stop()
        import sys

        sys.exit()
