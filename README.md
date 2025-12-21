# SOCKS5/HTTP WebSocket 代理

基于 WebSocket 的高性能代理工具，支持 SOCKS5 和 HTTP CONNECT 协议，用于网络穿透和流量转发。

## 原理

```
本地应用 <--SOCKS5/HTTP--> 客户端 <--WebSocket--> 服务端 <--TCP--> 目标服务器
```

- **客户端**：在本地启动代理服务器（支持 SOCKS5 和 HTTP CONNECT），将流量通过 WebSocket 转发到远程服务端
- **服务端**：接收 WebSocket 连接，代理访问目标服务器
- 单端口自动识别协议类型（SOCKS5 / HTTP CONNECT）
- 使用 WebSocket 连接池，提升并发性能
- 通过密码和消息签名保证连接安全

## 与v2ray性能对比

测试环境：同一服务器节点（跨洋线路（RTT ~260ms））

### 延迟对比

| 测试目标 | wsocks | v2ray | 结果 |
|---------|--------|-------|------|
| Google | 654 ms | 661 ms | 快 7 ms |
| YouTube | 995 ms | 1101 ms | 快 106 ms |
| **平均延迟** | **825 ms** | **881 ms** | **快 56 ms** |

### 下载速度对比

| 指标 | wsocks | v2ray |
|------|--------|-------|
| 平均速度 | 0.47 MB/s | 0.43 MB/s |
| 峰值速度 | 0.76 MB/s | 0.49 MB/s |

测试脚本和测试数据参考 speed_test 目录

## 安装

```bash
pip install wsocks
```
如安装过程中提示 uvloop 安装失败，可以跳过 uvloop：

```bash
pip install 'wsocks[no-uvloop]'
```

## 使用方法

### 1. 服务端配置

编辑 `config_server.json`：

```json
{
  "server": {
    "host": "0.0.0.0",
    "port": 8888,
    "path": "/ws",
    "password": "your-password-here"
  },
  "log_level": "INFO"
}
```

### 2. 启动服务端

在有公网 IP 的服务器上运行：

```bash
wsocks_server -c config_server.json
```

### 3. 客户端配置

编辑 `config_client.json`：

```json
{
  "server": {
    "url": "ws://your-server.com:8888/ws",
    "password": "your-password-here",
    "compression": false,
    "ws_pool_size": 8,
    "heartbeat_enabled": true,
    "heartbeat_min": 20,
    "heartbeat_max": 50,
    "use_fingerprint": false,
    "impersonate": "chrome124"
  },
  "local": {
    "host": "127.0.0.1",
    "port": 1080
  },
  "udp": {
    "enabled": false,
    "timeout": 60
  },
  "log_level": "INFO"
}
```

**TLS 指纹伪装说明**:
- `use_fingerprint`: 设置为 `true` 启用 TLS 指纹伪装（需要 Python 3.10+ 和 `curl_cffi==0.14.0`， `pip install curl_cffi==0.14.0`）
- `impersonate`: 指定浏览器指纹，支持 Chrome、Safari、Firefox、Edge, [支持的浏览器指纹列表](#支持的浏览器指纹列表)
- 开发阶段，可能不稳定，如不需要此功能，保持 `use_fingerprint: false` 即可，无需安装额外依赖

正式使用务必自行配置使用wss

### 4. 启动客户端

```bash
wsocks_client -c config_client.json
```

### 5. 配置代理

支持两种代理方式（同一端口自动识别）：

**SOCKS5 代理**：
- 服务器：`127.0.0.1`
- 端口：`1080`

**HTTP 代理**：
- 服务器：`http://127.0.0.1:1080`

## 配置参数说明

### 服务端参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| host | 监听地址 | 0.0.0.0 |
| port | 监听端口 | 8888 |
| password | 连接密码 | - |
| timeout | 连接超时（秒） | 30 |
| max_connections | 最大并发连接数 | 1000 |

### 客户端参数

| 参数 | 说明 | 默认值   |
|------|------|-------|
| server.url | 服务端地址 | -     |
| server.password | 连接密码 | -     |
| server.compression | 启用数据压缩 | false |
| server.ws_pool_size | WebSocket 连接池大小 | 8     |
| server.heartbeat_enabled | 启用应用层随机心跳 | true  |
| server.heartbeat_min | 心跳最小间隔（秒） | 20    |
| server.heartbeat_max | 心跳最大间隔（秒） | 50    |
| server.use_fingerprint | 启用 TLS 指纹伪装 | false |
| server.impersonate | 浏览器指纹（chrome99-136/safari153-260/firefox133,135） | chrome124 |
| local.port | 本地代理端口（支持 SOCKS5 / HTTP） | 1080  |
| udp.enabled | 启用 UDP 转发（SOCKS5 UDP Associate） | false |
| udp.timeout | UDP 会话超时时间（秒） | 60    |



## 支持的浏览器指纹列表

| Browser         | Open Source                                                                                                                                                                         | Pro version                                                                |
|-----------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|----------------------------------------------------------------------------|
| Chrome          | chrome99, chrome100, chrome101, chrome104, chrome107, chrome110, chrome116[1], chrome119[1], chrome120[1], chrome123[3], chrome124[3], chrome131[4], chrome133a[5][6], chrome136[6] | chrome132, chrome134, chrome135                                            |
| Chrome Android  | chrome99_android, chrome131_android [4]                                                                                                                                             | chrome132_android, chrome133_android, chrome134_android, chrome135_android |
| Chrome iOS      | N/A                                                                                                                                                                                 | coming soon                                                                |
| Safari [7]      | safari153 [2], safari155 [2], safari170 [1], safari180 [4], safari184 [6], safari260 [8]                                                                                            | coming soon                                                                |
| Safari iOS [7]  | safari172_ios[1], safari180_ios[4], safari184_ios [6], safari260_ios [8]                                                                                                            | coming soon                                                                |
| Firefox         | firefox133[5], firefox135[7]                                                                                                                                                        | coming soon                                                                |
| Firefox Android | N/A                                                                                                                                                                                 | firefox135_android                                                         |
| Tor             | tor145 [7]                                                                                                                                                                          | coming soon                                                                |
| Edge            | edge99, edge101                                                                                                                                                                     | edge133, edge135                                                           |
| Opera           | N/A                                                                                                                                                                                 | coming soon                                                                |
| Brave           | N/A                                                                                                                                                                                 | coming soon                                                                |


## 常见问题

**无法连接服务端**
- 检查服务端是否运行
- 确认防火墙端口已开放
- 验证密码是否一致

**上传速度慢**
- 增加 `ws_pool_size`（建议 8-32）

**连接频繁断开**
- 调整 `ping_interval` 和 `ping_timeout`
- 增加服务端 `timeout` 值
-

## 安全建议

- 修改默认密码，使用强密码
- 务必使用 WSS（WebSocket over TLS）
- 配置防火墙限制访问 IP

## 许可证

MIT License

