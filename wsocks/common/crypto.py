"""
加密模块 - 提供 ChaCha20-Poly1305 认证加密
"""
import hashlib
from typing import Optional

try:
    from Crypto.Cipher import ChaCha20_Poly1305
    from Crypto.Protocol.KDF import PBKDF2
    CRYPTO_AVAILABLE = True
except ImportError:
    CRYPTO_AVAILABLE = False

from wsocks.common.protocol import CRYPTO_METHOD_NONE, CRYPTO_METHOD_CHACHA20_POLY1305, parse_crypto_method


class CryptoManager:
    """加密管理器 - 支持可配置的加密方法"""

    def __init__(self, password: str, enabled: bool = True, crypto_method: Optional[str] = None):
        """
        初始化加密管理器

        Args:
            password: 密码字符串
            enabled: 是否启用加密
            crypto_method: 加密方法字符串，如 "chacha20", "none" 等。如果为None，enabled=True时默认使用chacha20
        """
        # 解析加密方法
        if enabled and crypto_method is None:
            crypto_method = 'chacha20'  # 默认使用chacha20
        
        self.crypto_method = parse_crypto_method(crypto_method)
        self.enabled = enabled and (self.crypto_method != CRYPTO_METHOD_NONE) and CRYPTO_AVAILABLE
        self.password = password

        if self.enabled:
            if self.crypto_method == CRYPTO_METHOD_CHACHA20_POLY1305:
                # 从密码派生32字节密钥 (ChaCha20需要256位密钥)
                # 使用 PBKDF2 增强密钥强度
                self.key = PBKDF2(
                    password.encode('utf-8'),
                    salt=b'wsocks-chacha20-salt-v1',  # 固定salt，确保相同密码生成相同密钥
                    dkLen=32,  # 32字节 = 256位
                    count=100000  # 迭代次数，平衡安全性和性能
                )
            else:
                raise ValueError(f"Unsupported crypto method: {self.crypto_method}")
        else:
            self.key = None
            if enabled and self.crypto_method != CRYPTO_METHOD_NONE and not CRYPTO_AVAILABLE:
                print("警告: pycryptodome 未安装，加密功能已禁用")

    def encrypt(self, data: bytes) -> bytes:
        """
        加密数据

        Args:
            data: 原始数据

        Returns:
            加密后的数据: nonce(12) + tag(16) + ciphertext(n)
            如果加密未启用，返回原始数据
        """
        if not self.enabled or not data:
            return data

        try:
            # 创建 ChaCha20-Poly1305 密码器（每次加密使用新的随机nonce）
            cipher = ChaCha20_Poly1305.new(key=self.key)

            # 加密并生成认证tag
            ciphertext, tag = cipher.encrypt_and_digest(data)

            # 返回格式: nonce(12字节) + tag(16字节) + ciphertext
            return cipher.nonce + tag + ciphertext
        except Exception as e:
            print(f"加密失败: {e}")
            return data

    def decrypt(self, data: bytes) -> bytes:
        """
        解密数据

        Args:
            data: 加密的数据 (nonce + tag + ciphertext)

        Returns:
            解密后的原始数据
            如果加密未启用，返回原始数据

        Raises:
            ValueError: 如果数据格式错误或认证失败
        """
        if not self.enabled or not data:
            return data

        # 最小长度: 12(nonce) + 16(tag) = 28字节
        if len(data) < 28:
            raise ValueError("加密数据格式错误: 长度不足")

        try:
            # 解析格式: nonce(12) + tag(16) + ciphertext
            nonce = data[:12]
            tag = data[12:28]
            ciphertext = data[28:]

            # 创建密码器并解密
            cipher = ChaCha20_Poly1305.new(key=self.key, nonce=nonce)
            plaintext = cipher.decrypt_and_verify(ciphertext, tag)

            return plaintext
        except ValueError as e:
            # 认证失败或数据被篡改
            raise ValueError(f"解密失败，数据可能被篡改: {e}")
        except Exception as e:
            raise ValueError(f"解密错误: {e}")

    def is_enabled(self) -> bool:
        """检查加密是否已启用"""
        return self.enabled


# 向后兼容的 XOR 加密（不安全，不推荐使用）
def xor_encrypt(data: bytes, key: str) -> bytes:
    """
    简单异或加密（已弃用，不安全）

    警告: XOR加密容易被破解，请使用 CryptoManager
    """
    key_bytes = key.encode()
    key_len = len(key_bytes)
    return bytes([data[i] ^ key_bytes[i % key_len] for i in range(len(data))])


def xor_decrypt(data: bytes, key: str) -> bytes:
    """
    简单异或解密（已弃用，不安全）

    警告: XOR加密容易被破解，请使用 CryptoManager
    """
    return xor_encrypt(data, key)


# 工具函数：检查加密库是否可用
def is_crypto_available() -> bool:
    """检查 pycryptodome 是否已安装"""
    return CRYPTO_AVAILABLE
