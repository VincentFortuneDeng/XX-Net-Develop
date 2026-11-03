# 性能与资源基线

## 范围

`tests/performance_baseline.py` 提供离线合成负载和指标守恒自验。默认不连接代理、不访问生产服务，也不注入生产故障。它用于固定观测口径和后续回归比较，不代表生产疗效或真实网络吞吐。

场景包括正常吞吐、慢消费者、多域名、front 故障恢复和限时 soak。输出包含 DNS、建连、front、roundtrip、日志等待的 p50/p95/p99，重传率、队列字节高水位，以及线程、FD、进程最大 RSS。

## 使用方法

```bash
python3 -I tests/performance_baseline.py --self-test
```

```bash
python3 -I tests/performance_baseline.py --soak-seconds 60 --output performance.json
```

指标采用线程安全账本：每次成功或失败都计入 attempts，retry 单独计数；reset 只清空当前队列字节，不清除累计次数或历史高水位。分位数使用 nearest-rank。macOS 的 `ru_maxrss` 直接按字节记录，其他平台按 KiB 转换为字节。

## 2026-11-03 基线

环境：macOS 27.2 arm64，Python 3.15.2；单进程离线运行，soak 0.1 秒。自验 3/3 通过。

| 场景 | 操作数 | roundtrip p50/p95/p99（毫秒） | 队列高水位 | 重传率 |
| --- | ---: | --- | ---: | ---: |
| normal | 500 | 0.000958 / 0.001333 / 0.002750 | 1023 B | 0 |
| slow_consumer | 500 | 0.000959 / 0.001125 / 0.001417 | 6096 B | 0 |
| multi_domain | 500 | 与固定种子的合成场景同量级 | 1023 B | 0 |
| front_recovery | 500 | 0.000916 / 0.001042 / 0.001250 | 1023 B | 10% |
| soak | 95758 | 0.000833 / 0.000958 / 0.001208 | 1023 B | 0 |

本次资源高水位：1 个线程、4 个 FD、最大 RSS 47,398,912 字节。roundtrip 只测量本地合成账本操作，不能解释为代理网络延迟。

## 后续比较门槛

1. 同一机器、Python 版本、种子和场景下比较完整分布，不以单次 p50 判定优化。
2. p95/p99 或资源高水位明显回退时先保留原始 JSON并定位原因；计数守恒或 reset 语义失败直接阻断提交。
3. 当前合成基线不构成新增零复制、时间轮、asyncio、窗口或 worker 调参的证据；这些优化需要真实只读 profiling 和独立验收。
4. 未覆盖生产部署、真实网络路径、浏览器负载和长时间生产 soak；生产数据只能在用户另行授权后按只读方式采集。
