# SOCKS5-WebSocket 代理工具

一个基于 WebSocket 的 SOCKS5 代理工具，用于网络穿透和流量转发。

## 项目结构

```
socks5-ws-proxy/
├── client/
│   ├── __init__.py
│   ├── socks5_server.py      # SOCKS5 服务器
│   └── ws_client.py           # WebSocket 客户端
├── server/
│   ├── __init__.py
│   ├── ws_handler.py          # WebSocket 处理器
│   └── tcp_client.py          # 目标网站连接
├── common/
│   ├── __init__.py
│   ├── protocol.py            # 消息协议
│   ├── crypto.py              # 加密
│   └── logger.py              # 日志
├── config_client.json         # 客户端配置
├── config_server.json         # 服务端配置
├── run_client.py             # 客户端启动
└── run_server.py             # 服务端启动
```

## 功能特性

- 完整的 SOCKS5 协议支持
- WebSocket 长连接传输
- 密码认证
- 消息签名验证
- 支持 TCP 连接
- 异步 IO，高性能

## 安装依赖

```bash
pip install tornado websockets
```

## 使用方法

### 1. 配置服务端

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

参数说明：
- `host`: 监听地址（0.0.0.0 表示监听所有网卡）
- `port`: 监听端口
- `path`: WebSocket 路径
- `password`: 连接密码（客户端和服务端需一致）

### 2. 启动服务端

在有公网 IP 的服务器上运行：

```bash
python run_server.py
```

### 3. 配置客户端

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

参数说明：
- `server.url`: 服务端 WebSocket 地址
- `server.password`: 连接密码（与服务端一致）
- `local.host`: 本地 SOCKS5 监听地址
- `local.port`: 本地 SOCKS5 监听端口

### 4. 启动客户端

```bash
python run_client.py
```

### 5. 配置浏览器或应用

设置 SOCKS5 代理：
- 服务器：127.0.0.1
- 端口：1080（与客户端配置一致）

## 工作原理

1. **客户端**：
   - 启动本地 SOCKS5 服务器（默认 127.0.0.1:1080）
   - 通过 WebSocket 连接到远程服务端
   - 接收来自本地应用的 SOCKS5 请求

2. **服务端**：
   - 监听 WebSocket 连接
   - 接收客户端的连接请求
   - 代理连接到目标服务器

3. **数据流转**：
   ```
   本地应用 <--SOCKS5--> 客户端 <--WebSocket--> 服务端 <--TCP--> 目标服务器
   ```

## 协议说明

### 消息格式

```
| version(1) | type(1) | length(4) | conn_id(4) | signature(16) | data(n) |
```

- `version`: 协议版本（当前为 1）
- `type`: 消息类型
  - 1: 连接请求
  - 2: 数据传输
  - 3: 关闭连接
  - 4: 心跳（预留）
- `length`: 数据长度
- `conn_id`: 连接 ID
- `signature`: MD5 签名
- `data`: 实际数据

### 签名算法

```python
signature_data = version + type + length + conn_id + data[:32] + password
signature = md5(signature_data)
```

## 安全建议

1. **修改默认密码**：务必在配置文件中设置强密码
2. **使用 WSS**：生产环境建议使用 WSS（WebSocket over TLS）
3. **防火墙设置**：限制服务端只允许必要的 IP 访问
4. **日志监控**：定期检查日志文件，发现异常连接

## 性能优化建议

1. **调整超时时间**：根据实际网络情况调整 timeout 参数
2. **增加缓冲区**：修改 `sock_recv` 的缓冲区大小
3. **启用压缩**：在 WebSocket 连接中启用压缩（需修改代码）
4. **多进程部署**：使用 Nginx 反向代理多个服务端实例

## 故障排查

### 客户端无法连接服务端

1. 检查服务端是否正常运行
2. 检查网络连接和防火墙设置
3. 验证 WebSocket URL 是否正确
4. 确认密码是否匹配

### 连接建立后无法传输数据

1. 查看日志中的错误信息
2. 检查目标服务器是否可达
3. 验证 SOCKS5 客户端配置是否正确

### 性能问题

1. 检查网络带宽和延迟
2. 查看服务器资源使用情况
3. 考虑使用更高性能的服务器

## 许可证

MIT License

## 注意事项

本工具仅供学习和合法用途使用。使用者应遵守当地法律法规，开发者不对任何滥用行为负责。

## 贡献

欢迎提交 Issue 和 Pull Request。

## 参考项目

- [ProxyNT](https://github.com/sazima/proxynt) - 内网穿透工具
- Tornado Web 框架
- WebSockets 库
