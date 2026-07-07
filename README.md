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

如需 TLS 指纹伪装功能（`--fingerprint` / `--impersonate`），额外安装可选依赖（需要 Python 3.10+）：

```bash
pip install 'wsocks[fingerprint]'
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

**WebSocket 代理说明**:
- `proxy`: 客户端连接 WebSocket 服务端时使用的前置代理，适用于客户端本身需要通过代理访问外网的场景
- 支持格式：`http://host:port`、`http://user:pass@host:port`、`socks5://host:port`、`socks5://user:pass@host:port`
- 不配置则直连服务端

**TLS 指纹伪装说明**:
- `use_fingerprint`: 设置为 `true` 启用 TLS 指纹伪装（需要 Python 3.10+ 和 `curl_cffi==0.15.0`，安装：`pip install 'wsocks[fingerprint]'`）
- `impersonate`: 指定浏览器指纹，支持 Chrome、Safari、Firefox、Edge, [支持的浏览器指纹列表](#支持的浏览器指纹列表)
- 开发阶段，可能不稳定，如不需要此功能，保持 `use_fingerprint: false` 即可，无需安装额外依赖

正式使用务必自行配置使用wss

> **提示**：配置文件支持 `//` 行注释和 `/* */` 块注释。

### 4. 启动客户端

使用配置文件启动：

```bash
wsocks_client -c config_client.json
```

#### 无配置文件启动（命令行参数）

也可以完全不用配置文件，直接用命令行参数启动，最少只需服务器地址与密码：

```bash
# 最简启动
wsocks_client -s wss://your-server.com:8888/ws -k your-password

# 指定本地监听、加密方式，并启用「私有网段直连、其余走代理」
wsocks_client -s wss://your-server.com:8888/ws -k your-password \
  -l 127.0.0.1:1080 -m chacha20 --direct-lan

# 使用浏览器 TLS 指纹（指定 --impersonate 会自动启用指纹伪装）
wsocks_client -s wss://your-server.com:8888/ws -k your-password --impersonate safari180
```

命令行参数也可与 `-c` 混用，用于临时覆盖配置文件中的字段（例如临时切换服务器、调高日志级别）：

```bash
wsocks_client -c config_client.json -s wss://backup.com/ws --log-level DEBUG
```

**参数说明**：

| 参数 | 对应配置项 | 说明 |
|------|-----------|------|
| `-c, --config` | — | 配置文件路径（不指定且当前目录存在 `config_client.json` 时自动加载） |
| `-s, --server` | `server.url` | WebSocket 服务器地址（`ws://` 或 `wss://`），无配置文件时必填 |
| `-k, --password` | `server.password` | 连接密码，无配置文件时必填 |
| `-l, --listen` | `local.host:port` | 本地监听地址 `HOST:PORT`（默认 `127.0.0.1:28888`） |
| `-m, --crypto` | `server.crypto_method` | 加密方式（如 `chacha20`） |
| `--pool-size` | `server.ws_pool_size` | WebSocket 连接池大小（默认 8） |
| `--compression` / `--no-compression` | `server.compression` | 启用 / 禁用压缩 |
| `--proxy` | `server.proxy` | 连接服务端使用的前置代理 |
| `--fingerprint` | `server.use_fingerprint` | 启用 TLS 指纹伪装（需 Python 3.10+ 与 `curl_cffi`） |
| `--impersonate` | `server.impersonate` | 浏览器指纹（指定后自动启用指纹伪装），如 `chrome124`、`safari180` |
| `--udp` | `udp.enabled` | 启用 UDP 转发 |
| `--direct-lan` | `routing` | 内置路由：私有网段直连、其余走代理 |
| `--log-level` | `log_level` | 日志等级（如 `INFO`、`DEBUG`） |

> 更细致的分流规则请使用配置文件的 `routing` 字段，详见[分流规则配置](#分流规则配置)。

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
| server.proxy | WebSocket 连接使用的代理（见下方说明） | 无 |
| local.port | 本地代理端口（支持 SOCKS5 / HTTP） | 1080  |
| udp.enabled | 启用 UDP 转发（SOCKS5 UDP Associate） | false |
| udp.timeout | UDP 会话超时时间（秒） | 60    |



## 分流规则配置

在 `config_client.json` 中添加 `routing` 字段即可启用分流。未配置时所有流量走代理（与旧版行为一致）。

### 基本结构

```json
"routing": {
  "default": "proxy",
  "rules": [...]
}
```

| 字段 | 说明 | 可选值 |
|------|------|--------|
| `default` | 未命中任何规则时的默认动作 | `proxy` / `direct` / `block` |
| `rules` | 规则列表，按顺序匹配，首次命中即返回 | — |

### 规则类型

#### `domain_suffix` — 域名后缀匹配

匹配域名本身及所有子域名，如 `.cn` 同时匹配 `example.cn` 和 `sub.example.cn`。

```json
{
  "type": "domain_suffix",
  "value": [".cn", ".com.cn", ".edu.cn", ".gov.cn"],
  "action": "direct"
}
```

#### `domain` — 精确域名匹配

```json
{
  "type": "domain",
  "value": ["www.google.com", "api.github.com"],
  "action": "proxy"
}
```

#### `domain_keyword` — 域名关键词匹配

域名中包含关键词即命中。

```json
{
  "type": "domain_keyword",
  "value": ["baidu", "taobao", "alibaba"],
  "action": "direct"
}
```

#### `ip_cidr` — IP 段匹配

```json
{
  "type": "ip_cidr",
  "value": ["127.0.0.0/8", "192.168.0.0/16", "10.0.0.0/8", "172.16.0.0/12"],
  "action": "direct"
}
```

#### `ruleset` — 从文件加载规则

适合加载较大的规则集（如国内域名列表）。`path` 为规则文件路径（相对于工作目录或绝对路径）。

```json
{
  "type": "ruleset",
  "path": "direct.lst",
  "action": "direct"
}
```

规则文件支持两种格式，可混用：

```
# 注释以 # 开头

# 格式一：纯域名（视为 domain_suffix，匹配本身及子域名）
baidu.com
qq.com
taobao.com

# 格式二：Surge / Clash 风格
DOMAIN-SUFFIX,weibo.com
DOMAIN,www.jd.com
DOMAIN-KEYWORD,alipay
```

### 动作说明

| 动作 | 说明 |
|------|------|
| `proxy` | 通过 WebSocket 代理转发 |
| `direct` | 直接连接目标，不经过代理 |
| `block` | 拒绝连接 |

### 典型配置示例

#### 国内直连，境外走代理

```json
"routing": {
  "default": "proxy",
  "rules": [
    {
      "type": "ip_cidr",
      "value": ["127.0.0.0/8", "192.168.0.0/16", "10.0.0.0/8", "172.16.0.0/12"],
      "action": "direct"
    },
    {
      "type": "domain_suffix",
      "value": [".cn", ".com.cn", ".edu.cn", ".gov.cn", ".net.cn", ".org.cn"],
      "action": "direct"
    },
    {
      "type": "ruleset",
      "path": "direct.lst",
      "action": "direct"
    }
  ]
}
```

#### 仅代理被墙域名，其余直连（黑名单模式）

```json
"routing": {
  "default": "direct",
  "rules": [
    {
      "type": "ruleset",
      "path": "proxy.lst",
      "action": "proxy"
    }
  ]
}
```

> **说明**：`proxy.lst` 可由 GUI 工具将 GFWList 预处理后生成，wsocks-core 只读取标准格式文件。

### 规则匹配优先级

1. 规则按 **声明顺序** 依次匹配，首次命中即返回，后续规则不再检查
2. IP 地址请求优先走 `ip_cidr` 规则，域名请求走其余规则
3. 未命中任何规则时使用 `default` 动作

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

