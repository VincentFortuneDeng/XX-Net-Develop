#!/usr/bin/env python3
"""A1 实证：CPython wrap_socket 握手失败时，对底层 fd 的处置语义。

场景对应 XX-Net DnsOverTlsQuery.connect 的异常路径：
    s = socket.create_connection(...)
    ssock = ssl_ctx.wrap_socket(s, server_hostname=...)  # 握手必败
    -> except: s.close()  # 现在的问题：这一句是 no-op、双关闭、还是必要？

判定标准（打印结论）：
  case A: ssl 层已 detach（wrap 后的 SSLObject 持有 fd，raw s.fileno()==-1）
  case B: ssl 层已关闭 fd 且 raw s 也标记 closed（close 是无害 no-op）
  case C: ssl 层不动 fd，raw s 仍持有有效 fd（不 close 会泄漏 -> 必须保留 close）
"""
import os
import socket
import ssl
import sys
import threading

RESULT = {"case": None, "detail": {}}


def run_probe(server_port: int) -> None:
    s = socket.create_connection(("127.0.0.1", server_port), timeout=5)
    fd_before = s.fileno()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    # 对方是明文 server：TLS 握手读到非 TLS 数据必败
    try:
        ssock = ctx.wrap_socket(s, server_hostname="localhost")
    except Exception as e:
        exc = e
    else:
        print("UNEXPECTED: handshake succeeded (probe invalid)")
        ssock.close()
        sys.exit(2)

    fileno = s.fileno()
    try:
        os.fstat(fd_before)
        fstat = "ok"
    except OSError as fe:
        fstat = f"OSError errno={fe.errno}({os.strerror(fe.errno) if fe.errno else '?'})"
    closed_attr = getattr(s, "_closed", "n/a")

    RESULT["detail"] = {
        "exc_type": type(exc).__name__,
        "exc": repr(exc),
        "fd_before": fd_before,
        "fileno_after": fileno,
        "fstat_fd_before": fstat,
        "s._closed": closed_attr,
    }

    if fileno == -1:
        RESULT["case"] = "A(detached)"
    elif fstat.startswith("OSError") and "errno=9" in fstat:
        RESULT["case"] = "B(fd-closed-by-ssl)"
    else:
        RESULT["case"] = "C(fd-still-open)"

    # 兜底清场：无论哪种 case，把可能残留的连接收掉
    try:
        s.close()
        RESULT["detail"]["final_close"] = "ok"
    except OSError as e:
        RESULT["detail"]["final_close"] = repr(e)


def main() -> int:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def accept_once():
        conn, _ = srv.accept()
        try:
            conn.recv(1024)  # 读到 ClientHello 片段即可，随后直接关闭（触发对端握手失败）
        finally:
            conn.close()

    t = threading.Thread(target=accept_once, daemon=True)
    t.start()
    try:
        run_probe(port)
    finally:
        srv.close()
        t.join(timeout=2)

    d = RESULT["detail"]
    print("=== wrap_socket 失败 fd 语义实证 ===")
    print(f"异常: {d['exc_type']}: {d['exc'][:160]}")
    print(f"raw socket fileno: before={d['fd_before']} after={d['fileno_after']}")
    print(f"os.fstat(原 fd): {d['fstat_fd_before']}")
    print(f"s._closed = {d['s._closed']}")
    print(f"事后兜底 s.close(): {d['final_close']}")
    print(f"结论: {RESULT['case']}")
    # case C -> 现有 except 里 s.close() 必须保留；case A/B -> 保留亦无害（幂等）
    return 0


if __name__ == "__main__":
    sys.exit(main())
