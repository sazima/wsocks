def xor_encrypt(data: bytes, key: str) -> bytes:
    """简单异或加密"""
    key_bytes = key.encode()
    key_len = len(key_bytes)
    return bytes([data[i] ^ key_bytes[i % key_len] for i in range(len(data))])

def xor_decrypt(data: bytes, key: str) -> bytes:
    """简单异或解密"""
    return xor_encrypt(data, key)
