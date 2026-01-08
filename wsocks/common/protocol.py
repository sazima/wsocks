import struct
import xxhash
from typing import Dict, Any, Optional

# 消息类型
MSG_TYPE_CONNECT = 1         # 连接请求
MSG_TYPE_DATA = 2            # 数据传输
MSG_TYPE_CLOSE = 3           # 关闭连接
MSG_TYPE_HEARTBEAT = 4       # 心跳
MSG_TYPE_CONNECT_SUCCESS = 5 # 连接成功响应
MSG_TYPE_CONNECT_FAILED = 6  # 连接失败响应
MSG_TYPE_UDP_ASSOCIATE = 7   # UDP Associate 请求
MSG_TYPE_UDP_DATA = 8        # UDP 数据包

# 加密方法
CRYPTO_METHOD_NONE = 0           # 无加密
CRYPTO_METHOD_CHACHA20_POLY1305 = 1  # ChaCha20-Poly1305

# 加密方法字符串映射
CRYPTO_METHOD_NAMES = {
    'none': CRYPTO_METHOD_NONE,
    'chacha20': CRYPTO_METHOD_CHACHA20_POLY1305,
    'chacha20-poly1305': CRYPTO_METHOD_CHACHA20_POLY1305,
}

def parse_crypto_method(method_str: Optional[str]) -> int:
    """
    解析加密方法字符串为常量
    
    Args:
        method_str: 加密方法字符串，如 "chacha20", "none" 等
        
    Returns:
        加密方法常量
    """
    if not method_str:
        return CRYPTO_METHOD_NONE
    
    method_str = method_str.lower().strip()
    return CRYPTO_METHOD_NAMES.get(method_str, CRYPTO_METHOD_NONE)

class Protocol:
    """
    消息格式:
    | version(1) | crypto_method(1) | type(1) | length(4) | conn_id(4) | signature(8) | data(n) |
    """

    VERSION = 1
    HEADER_SIZE = 19  # 增加了crypto_method字段（1字节）：18 + 1 = 19

    @staticmethod
    def pack(msg_type: int, conn_id: bytes, data: bytes, password: str, crypto_manager=None) -> bytes:
        """
        打包消息

        Args:
            msg_type: 消息类型
            conn_id: 连接ID
            data: 消息数据
            password: 密码（用于签名）
            crypto_manager: 可选的加密管理器（CryptoManager实例）

        Returns:
            打包后的消息: header + (encrypted_data 或 data)
        """
        version = Protocol.VERSION
        
        # 1. 确定加密方法和加密数据
        crypto_method = CRYPTO_METHOD_NONE
        if crypto_manager and crypto_manager.is_enabled():
            crypto_method = crypto_manager.crypto_method
            data = crypto_manager.encrypt(data)

        length = len(data)

        # 2. 计算签名（使用加密后的数据）
        signature_data = struct.pack('!BBBII', version, crypto_method, msg_type, length,
                                     int.from_bytes(conn_id, 'big'))
        signature_data += data[:32] if len(data) > 32 else data
        signature_data += password.encode()
        signature = xxhash.xxh64(signature_data).digest()

        # 3. 打包头部和数据
        header = struct.pack('!BBBII8s', version, crypto_method, msg_type, length,
                            int.from_bytes(conn_id, 'big'), signature)
        return header + data

    @staticmethod
    def unpack(raw_data: bytes, password: str, crypto_manager=None) -> Dict[str, Any]:
        """
        解包消息

        Args:
            raw_data: 原始消息数据
            password: 密码（用于签名验证）
            crypto_manager: 可选的加密管理器（CryptoManager实例，用于解密）

        Returns:
            包含 type、conn_id、data、crypto_method 的字典
        """
        if len(raw_data) < Protocol.HEADER_SIZE:
            raise ValueError("Data too short")

        # 1. 解析头部
        version, crypto_method, msg_type, length, conn_id_int, signature = struct.unpack(
            '!BBBII8s', raw_data[:Protocol.HEADER_SIZE])

        if version != Protocol.VERSION:
            raise ValueError(f"Unsupported version: {version}")

        # 2. 提取数据（此时可能是加密的）
        data = raw_data[Protocol.HEADER_SIZE:Protocol.HEADER_SIZE + length]
        if len(data) != length:
            raise ValueError("Data length mismatch")

        conn_id = conn_id_int.to_bytes(4, 'big')

        # 3. 验证签名（使用加密后的数据）
        signature_data = struct.pack('!BBBII', version, crypto_method, msg_type, length, conn_id_int)
        signature_data += data[:32] if len(data) > 32 else data
        signature_data += password.encode()
        expected_signature = xxhash.xxh64(signature_data).digest()

        if signature != expected_signature:
            raise ValueError("Invalid signature")

        # 4. 根据加密方法解密数据
        if crypto_method == CRYPTO_METHOD_CHACHA20_POLY1305:
            if not crypto_manager:
                raise ValueError("Encryption required but crypto_manager not available")
            if not crypto_manager.is_enabled():
                raise ValueError("Encryption required but crypto_manager not enabled")
            try:
                data = crypto_manager.decrypt(data)
            except Exception as e:
                raise ValueError(f"Decryption failed: {e}")
        elif crypto_method == CRYPTO_METHOD_NONE:
            # 无加密，直接使用
            pass
        else:
            raise ValueError(f"Unsupported crypto method: {crypto_method}")

        return {
            'type': msg_type,
            'conn_id': conn_id,
            'data': data,
            'crypto_method': crypto_method
        }
