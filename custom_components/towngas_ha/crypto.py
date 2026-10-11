"""接口响应解密（AES-256-ECB）。

为什么需要这个
--------------
部分区域（已确认港华燃气 host=weixin.towngasvcc.com）的**业务接口成功响应是加密的**，
只返回 ``{"encryptData": "<base64>"}``，而不是明文 JSON。

注意区分两种响应：
- **错误响应是明文**，例如 ``{"resultCode":"20001","resultMsg":"access token 过期"}``
- **成功响应是密文**，形如 ``{"encryptData":"..."}``

因此不能凭 ``resultCode`` 判断成功——那会导致「token 明明有效却报认证失败」。

算法来源
--------
反编译自 H5 前端构建产物 ``/h5-gas/js/app.02138c03.js``：

.. code-block:: javascript

    AESencrypt = function (e) {
      var a = md5("1234567890123456".concat(getAccessToken())).toUpperCase(),
          n = CryptoJS.enc.Utf8.parse(a);
      return CryptoJS.AES.encrypt(JSON.stringify(e), n, {
        mode: CryptoJS.mode.ECB,
        padding: CryptoJS.pad.Pkcs7,
      }).toString();
    };

    function AESdecrypt(e) {
      var a = md5("1234567890123456".concat(getAccessToken())).toUpperCase(),
          n = CryptoJS.enc.Utf8.parse(a),
          r = CryptoJS.AES.decrypt(e, n, {
            mode: CryptoJS.mode.ECB,
            padding: CryptoJS.pad.Pkcs7,
          }).toString(CryptoJS.enc.Utf8);
      return JSON.parse(r);
    }

即：

- **算法**：AES-256-ECB，PKCS#7 填充，密文为 Base64
- **密钥**：``MD5("1234567890123456" + access_token)`` 的**大写**十六进制字符串，
  共 32 个字符，直接按 UTF-8 当作 32 字节密钥使用（= AES-256）
- 密钥与 access_token 绑定，因此**解密必须使用该次请求实际发送的那个 access_token**。
  token 刷新后密文也必须用新 token 解。

实现说明
--------
这里用纯 Python 实现 AES，原因有两个：
1. 避免给集成增加依赖（HA 里虽然可能有 ``cryptography``，但版本不保证）；
2. 让 ``tools/diagnose_tokens.py`` 在用户机器上零依赖就能跑。

正确性由 ``tests/test_crypto.py`` 中的 **FIPS-197 官方测试向量**保证
（AES-128 / AES-192 / AES-256 三组加解密已知答案）。
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

# 密钥派生用的固定前缀（前端硬编码）
KEY_PREFIX = "1234567890123456"


# ----------------------------------------------------------------------
#  AES 基础表
# ----------------------------------------------------------------------

def _xtime(a: int) -> int:
    """GF(2^8) 上乘以 2（模 AES 多项式 0x11B）。"""
    a <<= 1
    if a & 0x100:
        a ^= 0x11B
    return a & 0xFF


def _rotl8(a: int, k: int) -> int:
    """8 位循环左移。"""
    return ((a << k) | (a >> (8 - k))) & 0xFF


def _build_sbox() -> tuple[list[int], list[int]]:
    """生成 AES S 盒与逆 S 盒。

    S 盒 = GF(2^8) 乘法逆元再做仿射变换（b ^ rotl(b,1..4) ^ 0x63）。
    """
    # 以 3 为生成元建 exp / log 表
    exp = [0] * 255
    log = [0] * 256
    x = 1
    for i in range(255):
        exp[i] = x
        log[x] = i
        x ^= _xtime(x)  # x *= 3
    inv = [0] * 256
    for i in range(1, 256):
        inv[i] = exp[(255 - log[i]) % 255]

    sbox = []
    for i in range(256):
        b = inv[i]
        s = b
        for k in range(1, 5):
            s ^= _rotl8(b, k)
        sbox.append(s ^ 0x63)

    inv_sbox = [0] * 256
    for i, v in enumerate(sbox):
        inv_sbox[v] = i
    return sbox, inv_sbox


SBOX, INV_SBOX = _build_sbox()

# 轮常量
RCON = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36]


def _gmul(a: int, b: int) -> int:
    """GF(2^8) 乘法。"""
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        a = _xtime(a)
        b >>= 1
    return p


def _expand_key(key: bytes) -> list[list[int]]:
    """AES 密钥扩展，支持 16 / 24 / 32 字节密钥。"""
    nk = len(key) // 4
    if nk not in (4, 6, 8):
        raise ValueError(f"不支持的 AES 密钥长度: {len(key)} 字节")
    total = 4 * (nk + 7)  # 4 * (Nr + 1)，Nr = nk + 6
    w = [list(key[4 * i:4 * i + 4]) for i in range(nk)]
    for i in range(nk, total):
        temp = list(w[i - 1])
        if i % nk == 0:
            temp = temp[1:] + temp[:1]          # RotWord
            temp = [SBOX[b] for b in temp]      # SubWord
            temp[0] ^= RCON[i // nk - 1]
        elif nk > 6 and i % nk == 4:
            temp = [SBOX[b] for b in temp]
        w.append([w[i - nk][j] ^ temp[j] for j in range(4)])
    return w


def _add_round_key(state: list[int], w: list[list[int]], rnd: int) -> None:
    for c in range(4):
        word = w[rnd * 4 + c]
        for r in range(4):
            state[4 * c + r] ^= word[r]


def _shift_rows(s: list[int]) -> None:
    """行左移：第 r 行循环左移 r 位。"""
    for r in range(1, 4):
        row = [s[4 * c + r] for c in range(4)]
        for c in range(4):
            s[4 * c + r] = row[(c + r) % 4]


def _inv_shift_rows(s: list[int]) -> None:
    """逆行移位：第 r 行循环右移 r 位。"""
    for r in range(1, 4):
        row = [s[4 * c + r] for c in range(4)]
        for c in range(4):
            s[4 * c + r] = row[(c - r) % 4]


def _mix_columns(s: list[int]) -> None:
    for c in range(4):
        a = s[4 * c:4 * c + 4]
        s[4 * c + 0] = _gmul(a[0], 2) ^ _gmul(a[1], 3) ^ a[2] ^ a[3]
        s[4 * c + 1] = a[0] ^ _gmul(a[1], 2) ^ _gmul(a[2], 3) ^ a[3]
        s[4 * c + 2] = a[0] ^ a[1] ^ _gmul(a[2], 2) ^ _gmul(a[3], 3)
        s[4 * c + 3] = _gmul(a[0], 3) ^ a[1] ^ a[2] ^ _gmul(a[3], 2)


def _inv_mix_columns(s: list[int]) -> None:
    for c in range(4):
        a = s[4 * c:4 * c + 4]
        s[4 * c + 0] = _gmul(a[0], 14) ^ _gmul(a[1], 11) ^ _gmul(a[2], 13) ^ _gmul(a[3], 9)
        s[4 * c + 1] = _gmul(a[0], 9) ^ _gmul(a[1], 14) ^ _gmul(a[2], 11) ^ _gmul(a[3], 13)
        s[4 * c + 2] = _gmul(a[0], 13) ^ _gmul(a[1], 9) ^ _gmul(a[2], 14) ^ _gmul(a[3], 11)
        s[4 * c + 3] = _gmul(a[0], 11) ^ _gmul(a[1], 13) ^ _gmul(a[2], 9) ^ _gmul(a[3], 14)


def aes_encrypt_block(block: bytes, w: list[list[int]]) -> bytes:
    """加密单个 16 字节分组。"""
    nr = len(w) // 4 - 1
    s = list(block)
    _add_round_key(s, w, 0)
    for rnd in range(1, nr):
        s = [SBOX[b] for b in s]
        _shift_rows(s)
        _mix_columns(s)
        _add_round_key(s, w, rnd)
    s = [SBOX[b] for b in s]
    _shift_rows(s)
    _add_round_key(s, w, nr)
    return bytes(s)


def aes_decrypt_block(block: bytes, w: list[list[int]]) -> bytes:
    """解密单个 16 字节分组。"""
    nr = len(w) // 4 - 1
    s = list(block)
    _add_round_key(s, w, nr)
    for rnd in range(nr - 1, 0, -1):
        _inv_shift_rows(s)
        s = [INV_SBOX[b] for b in s]
        _add_round_key(s, w, rnd)
        _inv_mix_columns(s)
    _inv_shift_rows(s)
    s = [INV_SBOX[b] for b in s]
    _add_round_key(s, w, 0)
    return bytes(s)


# ----------------------------------------------------------------------
#  ECB 模式 + PKCS#7
# ----------------------------------------------------------------------

def _pkcs7_pad(data: bytes, block: int = 16) -> bytes:
    n = block - (len(data) % block)
    return data + bytes([n]) * n


def _pkcs7_unpad(data: bytes, block: int = 16) -> bytes:
    if not data or len(data) % block:
        raise ValueError("密文长度不是分组的整数倍")
    n = data[-1]
    if not 1 <= n <= block or data[-n:] != bytes([n]) * n:
        raise ValueError("PKCS#7 填充不合法")
    return data[:-n]


def aes_ecb_encrypt(data: bytes, key: bytes) -> bytes:
    """AES-ECB 加密（含 PKCS#7 填充）。"""
    w = _expand_key(key)
    padded = _pkcs7_pad(data)
    return b"".join(
        aes_encrypt_block(padded[i:i + 16], w) for i in range(0, len(padded), 16)
    )


def aes_ecb_decrypt(data: bytes, key: bytes) -> bytes:
    """AES-ECB 解密（并去除 PKCS#7 填充）。"""
    w = _expand_key(key)
    if not data or len(data) % 16:
        raise ValueError("密文长度不是 16 的整数倍")
    plain = b"".join(
        aes_decrypt_block(data[i:i + 16], w) for i in range(0, len(data), 16)
    )
    return _pkcs7_unpad(plain)


# ----------------------------------------------------------------------
#  本平台专用的密钥派生与响应解密
# ----------------------------------------------------------------------

def derive_key(access_token: str) -> bytes:
    """由 access_token 推导 AES 密钥。

    ``MD5("1234567890123456" + access_token)`` 的大写十六进制字符串（32 字符），
    按 UTF-8 编码后作为 32 字节密钥（AES-256）。
    """
    digest = hashlib.md5(f"{KEY_PREFIX}{access_token}".encode("utf-8")).hexdigest()
    return digest.upper().encode("utf-8")


def decrypt_encrypt_data(encrypt_data: str, access_token: str) -> Any:
    """解密 ``encryptData`` 字段并解析为 JSON。

    :param encrypt_data: Base64 密文
    :param access_token: **该次请求实际发送的** access_token
    :return: 解密后的 JSON 对象
    """
    raw = aes_ecb_decrypt(base64.b64decode(encrypt_data), derive_key(access_token))
    return json.loads(raw.decode("utf-8"))


def decrypt_response(data: Any, access_token: str) -> Any:
    """若响应体是 ``{"encryptData": ...}`` 则解密，否则原样返回。

    解密失败时**原样返回**并抛出异常由调用方决定如何处理——
    这里选择抛出，避免把密文当成有效数据静默使用。
    """
    if (
        isinstance(data, dict)
        and data.get("encryptData")
        and access_token
    ):
        return decrypt_encrypt_data(data["encryptData"], access_token)
    return data
