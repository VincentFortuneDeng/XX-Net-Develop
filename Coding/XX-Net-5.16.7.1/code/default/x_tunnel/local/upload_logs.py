import os
import time
import io
import json
import zipfile
import operator

import simple_http_client
import env_info
import utils
from xlog import getLogger, reset_log_files
xlog = getLogger("x_tunnel")

from x_tunnel.local import global_var as g

current_path = os.path.dirname(os.path.abspath(__file__))
root_path = os.path.abspath(os.path.join(current_path, os.pardir, os.pardir))
data_path = env_info.data_path
data_xtunnel_path = os.path.join(data_path, 'x_tunnel')


def sleep(t):
    end_time = time.time() + t
    while g.running:
        if time.time() > end_time:
            return

        sleep_time = min(10, end_time - time.time())
        if sleep_time < 0:
            break

        time.sleep(sleep_time)


def mask_x_tunnel_password(fp):
    # xlog.debug("mask_x_tunnel_password:%s", fp)
    with open(fp, "r") as fd:
        content = fd.read()
        try:
            dat = json.loads(content)
            del dat["login_password"]
            dat_str = json.dumps(dat)
            return dat_str
        except Exception as e:
            xlog.exception("mask_x_tunnel_password fail, %s, content:%s e:%r", fp, content, e)
            return content


def get_launcher_port():
    launcher_config_fn = os.path.join(data_path, "launcher", "config.json")
    try:
        with open(launcher_config_fn, "r", encoding='utf-8') as fd:
            info = json.load(fd)
            return info.get("control_port", 8085)
    except Exception as e:
        xlog.exception("get_launcher_port except:%r", e)
        return 8085


def get_module_enable_flags(port):
    # return {"gae_proxy": bool, "x_tunnel": bool, "smart_router": bool}, or None if unknown.
    # None means fetch/parse failed: caller must fail safe to the old behavior (poll all).
    url = f"http://127.0.0.1:{port}/config?cmd=get_config"
    try:
        res = simple_http_client.request("GET", url, timeout=1)
        dat = json.loads(res.text)
        flags = {}
        for module in ("gae_proxy", "x_tunnel", "smart_router"):
            key = module + "_enable"
            if key not in dat:
                return None
            flags[module] = bool(dat[key])
        return flags
    except Exception as e:
        xlog.warn("get module enable flags fail:%r", e)
        return None


def collect_debug_and_log():
    port = get_launcher_port()

    # collect debug info and save to folders
    debug_infos = {
        "system_info": f"http://127.0.0.1:{port}/debug",
        "gae_info": f"http://127.0.0.1:{port}/module/gae_proxy/control/debug",
        "gae_log": f"http://127.0.0.1:{port}/module/gae_proxy/control/log?cmd=get_new&last_no=1",
        "xtunnel_info": f"http://127.0.0.1:{port}/module/x_tunnel/control/debug",
        "xtunnel_status": f"http://127.0.0.1:{port}/module/x_tunnel/control/status",
        "cloudflare_info": f"http://127.0.0.1:{port}/module/x_tunnel/control/cloudflare_front/debug",
        "tls_info": f"http://127.0.0.1:{port}/module/x_tunnel/control/tls_relay_front/debug",
        "seley_info": f"http://127.0.0.1:{port}/module/x_tunnel/control/seley_front/debug",
        "cloudflare_log": f"http://127.0.0.1:{port}/module/x_tunnel/control/cloudflare_front/log?cmd=get_new&last_no=1",
        "tls_log": f"http://127.0.0.1:{port}/module/x_tunnel/control/tls_relay_front/log?cmd=get_new&last_no=1",
        "seley_log": f"http://127.0.0.1:{port}/module/x_tunnel/control/seley_front/log?cmd=get_new&last_no=1",
        "xtunnel_log": f"http://127.0.0.1:{port}/module/x_tunnel/control/log?cmd=get_new&last_no=1",
        "smartroute_log": f"http://127.0.0.1:{port}/module/smart_router/control/log?cmd=get_new&last_no=1",
        "launcher_log": f"http://127.0.0.1:{port}/log?cmd=get_new&last_no=1"
    }

    # only poll module endpoints that are enabled, so disabled modules
    # don't generate 404 "no module in path" noise in launcher logs.
    enable_flags = get_module_enable_flags(port)
    if enable_flags:
        if not enable_flags["gae_proxy"]:
            del debug_infos["gae_info"]
            del debug_infos["gae_log"]
        if not enable_flags["x_tunnel"]:
            for name in list(debug_infos):
                if name.startswith("xtunnel") or name in (
                        "cloudflare_info", "cloudflare_log",
                        "tls_info", "tls_log", "seley_info", "seley_log"):
                    del debug_infos[name]
        if not enable_flags["smart_router"]:
            del debug_infos["smartroute_log"]
    # launcher endpoints (system_info / launcher_log) always stay.

    download_path = os.path.join(env_info.data_path, "downloads")
    if not os.path.isdir(download_path):
        os.mkdir(download_path)

    for name, url in debug_infos.items():
        # xlog.debug("fetch %s %s", name, url)
        try:
            res = simple_http_client.request("GET", url, timeout=1)
            if name.endswith("log"):
                dat = json.loads(res.text)
                no_line = list(dat.items())
                no_line = [[int(line[0]), line[1]] for line in no_line]
                no_line = sorted(no_line, key=operator.itemgetter(0))
                lines = [line[1] for line in no_line]
                data = "".join(lines)
                data = utils.to_bytes(data)
            else:
                data = res.text

            fn = os.path.join(download_path, name + ".txt")
            with open(fn, "wb") as fd:
                fd.write(data)
        except Exception as e:
            xlog.warn("fetch info %s fail:%r", url, e)


def collect_log_views():
    port = get_launcher_port()
    enable_flags = get_module_enable_flags(port)
    controls = {
        "launcher": f"http://127.0.0.1:{port}/log",
        "smart_router": f"http://127.0.0.1:{port}/module/smart_router/control/log",
        "gae_proxy": f"http://127.0.0.1:{port}/module/gae_proxy/control/log",
        "x_tunnel": f"http://127.0.0.1:{port}/module/x_tunnel/control/log",
        "cloudflare_front": f"http://127.0.0.1:{port}/module/x_tunnel/control/cloudflare_front/log",
        "seley_front": f"http://127.0.0.1:{port}/module/x_tunnel/control/seley_front/log",
        "tls_relay": f"http://127.0.0.1:{port}/module/x_tunnel/control/tls_relay_front/log",
    }
    if enable_flags:
        for module in ("gae_proxy", "smart_router"):
            if not enable_flags[module]:
                del controls[module]
        if not enable_flags["x_tunnel"]:
            for module in ("x_tunnel", "cloudflare_front", "seley_front", "tls_relay"):
                del controls[module]

    views = {}
    for name, base_url in controls.items():
        for view, max_bytes in (("startup", 256 * 1024), ("warning", 512 * 1024)):
            url = "%s?cmd=export&view=%s&max_bytes=%d" % (base_url, view, max_bytes)
            try:
                res = simple_http_client.request("GET", url, timeout=1)
                views["views/%s_%s.log" % (name, view)] = utils.to_bytes(res.text)
            except Exception as e:
                xlog.warn("fetch log view %s fail:%r", url, e)
    return views


UNIFIED_LOG_NAMES = {
    "launcher.log", "smart_router.log", "gae_proxy.log", "x_tunnel.log",
    "cloudflare_front.log", "seley_front.log", "tls_relay.log",
}


def diagnostic_file_groups(include_legacy=False):
    groups = {"state": [], "memory": [], "main": [], "backup": [], "legacy": []}
    for root, subdirs, files in os.walk(data_path):
        for filename in files:
            src_file = os.path.join(root, filename)
            relate_path = os.path.relpath(src_file, data_path)
            extension = filename.split(".")[-1]
            if extension in ("json", "txt"):
                if relate_path.startswith("downloads" + os.path.sep) and filename.endswith("_log.txt"):
                    groups["memory"].append((relate_path, src_file))
                else:
                    groups["state"].append((relate_path, src_file))
            elif filename in UNIFIED_LOG_NAMES:
                groups["main"].append((relate_path, src_file))
            elif filename.endswith(".log.1") and filename[:-6] + ".log" in UNIFIED_LOG_NAMES:
                groups["backup"].append((relate_path, src_file))
            elif include_legacy and (filename.endswith(".log") or filename.endswith(".log.1")):
                groups["legacy"].append((relate_path, src_file))

    for group in groups.values():
        group.sort()
    for group_name in ("main", "backup"):
        groups[group_name].sort(key=lambda item: os.path.getmtime(item[1]), reverse=True)
    return groups


def pack_logs(max_size=10 * 1024 * 1024, include_legacy=False):
    content_size = 0

    try:
        collect_debug_and_log()
        views = collect_log_views()
        groups = diagnostic_file_groups(include_legacy)
        candidates = groups["state"] + groups["memory"]
        candidates += sorted((name, content) for name, content in views.items())
        candidates += groups["main"] + groups["backup"] + groups["legacy"]

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zfd:
            for relate_path, source in candidates:
                if isinstance(source, bytes):
                    content = source
                else:
                    if os.path.getsize(source) > max_size - content_size:
                        continue
                    if relate_path.endswith("client.json"):
                        content = utils.to_bytes(mask_x_tunnel_password(source))
                    else:
                        with open(source, "rb") as fd:
                            content = fd.read()
                if content_size + len(content) > max_size:
                    continue
                zfd.writestr(relate_path, content)
                content_size += len(content)

        compressed_data = zip_buffer.getvalue()
        xlog.debug("compress log size:%d to %d", content_size, len(compressed_data))
        return compressed_data
    except Exception as e:
        xlog.exception("packing logs except:%r", e)
        return None


def upload_logs_thread():
    sleep(g.config.delay_collect_log)
    while g.running:
        if not g.running or not g.server_host or not g.session or g.session.last_receive_time == 0:
            time.sleep(10)
        else:
            break

    sleep(g.config.delay_collect_log2)
    if not g.running:
        return

    session_id = utils.to_str(g.session.session_id)
    data = pack_logs()
    if data:
        upload(session_id, data)


def upload(session_id, data):
    try:
        content, status, response = g.http_client.request(method="POST", host=g.server_host,
                                                          path="/upload_logs?session_id=%s" % session_id,
                                                          data=data,
                                                          headers={"Content-Length": str(len(data))})

    except Exception as e:
        xlog.exception("upload logs:%r ", e)
        return

    if status != 200:
        xlog.warn("upload logs status:%r ", status)
        return

    # xlog.info("upload logs successful")
    reset_log_files()
