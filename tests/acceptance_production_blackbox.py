#!/usr/bin/env python3
"""独立于单元测试的生产黑盒验收；只读，不重启、不部署。"""
import concurrent.futures
import hashlib
import http.client
import json
import os
from pathlib import Path
import subprocess
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
PROD = Path('/Applications/XX-Net')
BASE = 'http://127.0.0.1:8085'
results = []


def record(item, ok, evidence):
    results.append({'item': item, 'status': 'PASS' if ok else 'FAIL', 'evidence': evidence})


def fetch(url, timeout=10):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


# A01/A02：真实二进制、启动脚本和资源，而不是 mock import。
so = PROD / 'code/default/lib/darwin/boringssl.abi3.so'
file_info = subprocess.check_output(['file', str(so)], text=True).strip()
record('A01-binary', 'arm64' in file_info, file_info)
script = ROOT / 'Coding/XX-Net-5.16.7.1/start'
subprocess.run(['bash', '-n', str(script)], check=True)
script_text = script.read_text()
record('A02-start-script', 'sudo pip3' not in script_text and 'import boringssl' in script_text,
       'bash -n PASS；无 sudo pip3；按真实 import 判定')

# A23：O 部署前备份必须能够还原到固定基线。
manifest = ROOT / 'backups/pre-rsync-20261103-o/SHA256SUMS'
backup_ok = True
for line in manifest.read_text().splitlines():
    digest, relative = line.split('  ', 1)
    backup = manifest.parent / relative
    original = subprocess.check_output(['git', 'show', 'ca93de4:Coding/XX-Net-5.16.7.1/' + relative[:-5]], cwd=ROOT)
    backup_ok &= backup.is_file() and hashlib.sha256(backup.read_bytes()).hexdigest() == digest
    backup_ok &= backup.read_bytes() == original
record('A23-restore-proof', backup_ok, '13 份 .orig 同时匹配 SHA 清单和 ca93de4 基线')

# 两轮已授权部署哈希仍与生产一致，独立验收部署覆盖同路径的 O 旧哈希。
expected_deployed = {}
for deployed_manifest in (
        ROOT / 'backups/pre-rsync-20261103-o/DEPLOYED-SHA256SUMS',
        ROOT / 'backups/pre-rsync-20261103-independent-acceptance/DEPLOYED-SHA256SUMS'):
    for line in deployed_manifest.read_text().splitlines():
        digest, relative = line.split('  ', 1)
        expected_deployed[relative] = digest
deployed_ok = all(
    hashlib.sha256((PROD / relative).read_bytes()).hexdigest() == digest
    for relative, digest in expected_deployed.items())
record('A22-deployed-provenance', deployed_ok,
       f'{len(expected_deployed)} 个生产文件与最新已验收部署哈希一致')

status, config_raw = fetch(BASE + '/config?cmd=get_config')
config = json.loads(config_raw)
record('A04-config-contract', status == 200, f'config status={status}')
controls = [('launcher', BASE + '/log')]
if config.get('smart_router_enable'):
    controls.append(('smart_router', BASE + '/module/smart_router/control/log'))
if config.get('gae_proxy_enable'):
    controls.append(('gae_proxy', BASE + '/module/gae_proxy/control/log'))
if config.get('x_tunnel_enable'):
    controls.extend([
        ('x_tunnel', BASE + '/module/x_tunnel/control/log'),
        ('cloudflare_front', BASE + '/module/x_tunnel/control/cloudflare_front/log'),
        ('seley_front', BASE + '/module/x_tunnel/control/seley_front/log'),
        ('tls_relay', BASE + '/module/x_tunnel/control/tls_relay_front/log'),
    ])
record('A04-disabled-module-skip', not config.get('gae_proxy_enable') and
       all(name != 'gae_proxy' for name, _ in controls), 'gae_proxy 禁用时黑盒请求集合为零')

# A03/A22：并发真实 Web 请求、旧 JSON、新视图、非法参数和空视图。
debug_statuses = []
def debug_call(_):
    try:
        status, _ = fetch(BASE + '/debug', timeout=15)
        return status
    except Exception as error:
        return repr(error)
with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
    debug_statuses = list(pool.map(debug_call, range(24)))
record('legacy-http-keepalive', debug_statuses == [200] * 24,
       f'默认 HTTP/1.1 的 24 个并发 /debug 响应={sorted(set(map(str, debug_statuses)))}')


def debug_close_call(_):
    connection = http.client.HTTPConnection('127.0.0.1', 8085, timeout=15)
    try:
        connection.request('GET', '/debug', headers={'Connection': 'close'})
        response = connection.getresponse()
        response.read()
        return response.status
    except Exception as error:
        return repr(error)
    finally:
        connection.close()


with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
    close_statuses = list(pool.map(debug_close_call, range(24)))
record('A03-concurrent-debug', close_statuses == [200] * 24,
       f'显式 Connection: close 的 24 个并发 /debug 响应={sorted(set(map(str, close_statuses)))}')

view_ok = True
view_evidence = []
for name, url in controls:
    status, raw = fetch(url + '?cmd=get_last&max_line=4')
    try:
        parsed = json.loads(raw)
        memory_ok = status == 200 and isinstance(parsed, dict)
    except Exception:
        memory_ok = False
    view_ok &= memory_ok
    for view, limit in (('startup', 262144), ('warning', 524288)):
        status, raw = fetch(url + f'?cmd=export&view={view}&max_bytes={limit}')
        valid = status == 200 and len(raw) <= limit and (not raw or raw.endswith(b'\n'))
        try:
            raw.decode('utf-8')
        except UnicodeDecodeError:
            valid = False
        view_ok &= valid
        view_evidence.append(f'{name}:{view}={len(raw)}')
    status, raw = fetch(url + '?cmd=export&view=warning&max_bytes=not-a-number')
    view_ok &= status == 200 and raw == b''
record('A22-http-contract', view_ok, '；'.join(view_evidence) + '；非法预算为空')

# 正常代理请求不依赖日志测试替身。
proxy = urllib.request.ProxyHandler({'http': 'http://127.0.0.1:8086'})
opener = urllib.request.build_opener(proxy)
try:
    with opener.open('http://www.gstatic.com/generate_204', timeout=25) as response:
        proxy_status = response.status
except Exception as error:
    proxy_status = repr(error)
record('production-proxy', proxy_status == 204, f'generate_204={proxy_status}')

print(json.dumps(results, ensure_ascii=False, indent=2))
failed = [item for item in results if item['status'] == 'FAIL']
print(f'BLACKBOX SUMMARY: {len(results)-len(failed)}/{len(results)} PASS')
raise SystemExit(1 if failed else 0)
