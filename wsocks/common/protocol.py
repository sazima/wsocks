import struct
import hashlib
from typing import Dict, Any

# 消息类型
MSG_TYPE_CONNECT = 1         # 连接请求
MSG_TYPE_DATA = 2            # 数据传输
MSG_TYPE_CLOSE = 3           # 关闭连接
MSG_TYPE_HEARTBEAT = 4       # 心跳
MSG_TYPE_CONNECT_SUCCESS = 5 # 连接成功响应
MSG_TYPE_CONNECT_FAILED = 6  # 连接失败响应

class Protocol:
    """
    消息格式:
    | version(1) | type(1) | length(4) | conn_id(4) | signature(16) | data(n) |
    """

    VERSION = 1
    HEADER_SIZE = 26

    @staticmethod
    def pack(msg_type: int, conn_id: bytes, data: bytes, password: str) -> bytes:
        """打包消息"""
        version = Protocol.VERSION
        length = len(data)

        # 计算签名
        signature_data = struct.pack('!BBII', version, msg_type, length,
                                     int.from_bytes(conn_id, 'big'))
        signature_data += data[:32] if len(data) > 32 else data
        signature_data += password.encode()
        signature = hashlib.md5(signature_data).digest()

        # 打包
        header = struct.pack('!BBII16s', version, msg_type, length,
                            int.from_bytes(conn_id, 'big'), signature)
        return header + data

    @staticmethod
    def unpack(raw_data: bytes, password: str) -> Dict[str, Any]:
        """解包消息"""
        if len(raw_data) < Protocol.HEADER_SIZE:
            raise ValueError("Data too short")

        # 解析头部
        version, msg_type, length, conn_id_int, signature = struct.unpack(
            '!BBII16s', raw_data[:Protocol.HEADER_SIZE])

        if version != Protocol.VERSION:
            raise ValueError(f"Unsupported version: {version}")

        # 提取数据
        data = raw_data[Protocol.HEADER_SIZE:Protocol.HEADER_SIZE + length]
        if len(data) != length:
            raise ValueError("Data length mismatch")

        # 验证签名
        conn_id = conn_id_int.to_bytes(4, 'big')
        signature_data = struct.pack('!BBII', version, msg_type, length, conn_id_int)
        signature_data += data[:32] if len(data) > 32 else data
        signature_data += password.encode()
        expected_signature = hashlib.md5(signature_data).digest()

        if signature != expected_signature:
            raise ValueError("Invalid signature")

        return {
            'type': msg_type,
            'conn_id': conn_id,
            'data': data
        }
