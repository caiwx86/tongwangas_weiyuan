"""请求签名（sign）计算。

算法来源
--------
反编译自 H5 前端构建产物 ``/h5-gas/js/app.02138c03.js`` 中的 ``requestSign`` 函数：

.. code-block:: javascript

    requestSign = function (e) {
      if (Object.keys(e).length > 0) {
        var a = "";
        Object.keys(e)
          .sort(function (e, a) { return e.localeCompare(a); })
          .forEach(function (n) {
            "sign" !== n && void 0 !== e[n] && null !== e[n] && "" !== e[n]
              && (a += "".concat(n).concat(e[n]));
          });
        return js_md5("".concat(a, "hbasesoft.com-").concat("prod"))
          .toString().toUpperCase();
      }
    };

即::

    sign = MD5(按 key 排序拼接的 "key+value" 串 + "hbasesoft.com-prod").toUpperCase()

细节：
- 跳过 key 为 ``sign`` 的项；跳过值为 ``null`` / ``undefined`` / 空串的项
- 盐值 = ``"hbasesoft.com-"`` 拼接运行环境 ``"prod"``
- 前端用 JS 的 ``localeCompare`` 排序。本平台参数均为小写开头的 camelCase
  （isPay、orgId、subsId、timestamp、authCode、refreshToken 等），此时
  ``localeCompare`` 与 Python 的字典序一致，故直接使用 ``sorted()``。
  若将来出现大写开头的参数名，两种排序可能不同，需要改为大小写不敏感比较。

验证依据
--------
该算法已用真实抓包（港华燃气，host=weixin.towngasvcc.com）逐字节验证：

- ``getLoginUserInfo``，参数 ``timestamp=1791728307821``
  → ``941CAF075D4A2E4332143D0DABEE4474``（与抓包 sign 完全一致）
- ``queryBindList``，参数 ``isPay=N``、``orgId=2c90d8896048412a01604e047df30100``、
  ``timestamp=1791728804211``
  → ``6B7ED3BABFDD5D010EAC221225CA7083``（与抓包 sign 完全一致）

为什么必须动态计算
------------------
``sign`` 与「本次请求的全部参数 + 时间戳」强绑定，因此每次请求都不同。
把某一个抓包得到的 sign 当作固定值复用，在原理上无法对所有请求成立。
"""

from __future__ import annotations

import hashlib
from typing import Any

# 盐值：前端为 "hbasesoft.com-" + 环境名，生产环境为 "prod"
SIGN_SALT = "hbasesoft.com-prod"


def compute_sign(params: dict[str, Any]) -> str:
    """按 H5 前端 ``requestSign`` 的算法计算 sign。

    :param params: 本次请求的全部参数（**应包含 timestamp**，可包含 sign，
                   函数内部会自动跳过 sign）
    :return: 32 位大写十六进制 MD5 字符串
    """
    parts: list[str] = []
    for key in sorted(params.keys()):
        # sign 自身不参与计算
        if key == "sign":
            continue
        value = params[key]
        # 跳过 null / undefined / 空串，与前端判断保持一致
        if value is None or value == "":
            continue
        parts.append(f"{key}{value}")

    raw = "".join(parts) + SIGN_SALT
    return hashlib.md5(raw.encode("utf-8")).hexdigest().upper()
