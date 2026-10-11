"""sign 算法测试。

运行方式（无需 pytest，也兼容 pytest）::

    python3 tests/test_sign.py

前两条是**真实抓包向量**，来自港华燃气（host=weixin.towngasvcc.com）
的实际请求，sign 值与抓包完全一致。任何改动若破坏这两条，说明算法被改错了。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components" / "towngas_ha"))

from sign import SIGN_SALT, compute_sign  # noqa: E402


# ----------------------------------------------------------------------
#  真实抓包向量（不要修改期望值）
# ----------------------------------------------------------------------

def test_real_capture_get_login_user_info() -> None:
    """GET /nv1/vcc-cbs/usersubs/getLoginUserInfo：只有 timestamp 一个参数。"""
    assert compute_sign({"timestamp": "1791728307821"}) == "941CAF075D4A2E4332143D0DABEE4474"


def test_real_capture_query_bind_list() -> None:
    """GET /nv1/vcc-cbs/usersubs/queryBindList：三个参数按 key 排序后拼接。"""
    assert compute_sign({
        "isPay": "N",
        "orgId": "2c90d8896048412a01604e047df30100",
        "timestamp": "1791728804211",
    }) == "6B7ED3BABFDD5D010EAC221225CA7083"


# ----------------------------------------------------------------------
#  算法细节
# ----------------------------------------------------------------------

def test_sign_key_is_ignored() -> None:
    """已存在的 sign 键必须被跳过，不影响结果。"""
    base = {"isPay": "N", "orgId": "abc", "timestamp": "123"}
    with_sign = {**base, "sign": "SHOULD_BE_IGNORED"}
    assert compute_sign(base) == compute_sign(with_sign)


def test_empty_and_none_values_are_skipped() -> None:
    """值为空串 / None 的键不参与拼接（对应前端的 null/undefined/'' 判断）。"""
    base = {"orgId": "abc", "timestamp": "123"}
    assert compute_sign(base) == compute_sign({**base, "extra": ""})
    assert compute_sign(base) == compute_sign({**base, "extra": None})


def test_key_order_does_not_matter() -> None:
    """排序由函数内部完成，入参顺序不影响结果。"""
    a = {"isPay": "N", "orgId": "abc", "timestamp": "123"}
    b = {"timestamp": "123", "orgId": "abc", "isPay": "N"}
    assert compute_sign(a) == compute_sign(b)


def test_output_format() -> None:
    """输出必须是 32 位大写十六进制。"""
    out = compute_sign({"timestamp": "1"})
    assert len(out) == 32
    assert out == out.upper()
    assert all(c in "0123456789ABCDEF" for c in out)


def test_salt_value() -> None:
    """盐值必须与前端一致，否则所有请求都会失败。"""
    assert SIGN_SALT == "hbasesoft.com-prod"


def test_empty_params() -> None:
    """无参数时不应抛异常（前端此时不生成 sign，这里返回盐值的散列）。"""
    assert compute_sign({}) == compute_sign({"sign": "x"})


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL  {fn.__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} 通过")
    sys.exit(1 if failed else 0)
