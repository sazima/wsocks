# SOCKS5-WebSocket 代理

基于 WebSocket 的 SOCKS5 代理工具，用于网络穿透和流量转发。

## 原理

```
本地应用 <--SOCKS5--> 客户端 <--WebSocket--> 服务端 <--TCP--> 目标服务器
```

- **客户端**：在本地启动 SOCKS5 服务器，将流量通过 WebSocket 转发到远程服务端
- **服务端**：接收 WebSocket 连接，代理访问目标服务器
- 使用 WebSocket 连接池, 提升并发性能
- 通过密码和消息签名保证连接安全

## 安装

```bash
pip install wsocks
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
    "password": "your-password-here"
  },
  "local": {
    "host": "127.0.0.1",
    "port": 1080
  },
  "log_level": "INFO"
}
```

### 4. 启动客户端

```bash
wsocks_client -c config_client.json
```

### 5. 配置代理

在浏览器或应用中设置 SOCKS5 代理：
- 服务器：`127.0.0.1`
- 端口：`1080`

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

| 参数 | 说明 | 默认值 |
|------|------|--------|
| server.url | 服务端地址 | - |
| server.password | 连接密码 | - |
| server.ws_pool_size | WebSocket 连接池大小 | 8 |
| local.port | 本地 SOCKS5 端口 | 1080 |

## 性能测试

使用 `test_speed.py` 脚本对比 wsocks 和其他代理（如 v2ray）的性能：

```bash
# 安装测试依赖
pip install requests

# 运行测试（默认 wsocks:1088, v2ray:4086）
python3 test_speed.py

# 自定义端口
python3 test_speed.py --wsocks-port 1088 --v2ray-port 4086

# 只测试延迟
python3 test_speed.py --no-download

# 只测试下载速度
python3 test_speed.py --no-latency
```

测试内容：
- **延迟测试**：访问 Google、YouTube、GitHub 等网站，测试响应时间
- **下载速度**：下载测试文件，测量实际传输速度

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

## 安全建议

- 修改默认密码，使用强密码
- 生产环境使用 WSS（WebSocket over TLS）
- 配置防火墙限制访问 IP

## 许可证

MIT License

## 注意事项

本工具仅供学习和合法用途使用，请遵守当地法律法规。
