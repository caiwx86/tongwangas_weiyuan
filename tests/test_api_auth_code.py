"""access_token_by_auth_code 的请求构造测试。

本测试**不联网**，只验证两件事：

1. 请求 URL 拼得对不对 —— 尤其是路径里那个**服务端拼写错误** ``accessToekn``
   必须原样保留（改成 ``accessToken`` 会 404）；
2. 响应解析与内部 token 状态更新是否正确。

api.py 依赖 aiohttp（HA 环境自带，本地测试环境未必有），这里用最小桩替代。
api.py 只通过 ``import aiohttp`` 引用它，且类型注解因
``from __future__ import annotations`` 被字符串化，因此桩只需要能让 import 通过。

运行方式（无需 pytest，也兼容 pytest）::

    python3 tests/test_api_auth_code.py
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path
from urllib.parse import parse_qs, urlparse

INTEGRATION_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "towngas_ha"

# ----------------------------------------------------------------------
#  1. aiohttp 最小桩（必须在导入 api 之前装好）
# ----------------------------------------------------------------------

if "aiohttp" not in sys.modules:
    _aiohttp_stub = types.ModuleType("aiohttp")

    class _ClientSession:  # pragma: no cover - 仅用于让类型注解可导入
        """占位类，测试中不会真正使用。"""

    _aiohttp_stub.ClientSession = _ClientSession
    sys.modules["aiohttp"] = _aiohttp_stub

# ----------------------------------------------------------------------
#  2. 以「合成包」方式导入 api / const / sign
#     集成目录的 __init__.py 会 import homeassistant，不能直接当包导入，
#     因此这里手工构造一个包命名空间，绕开 __init__.py。
# ----------------------------------------------------------------------

_PKG = "_tws_under_test"
if _PKG not in sys.modules:
    _pkg = types.ModuleType(_PKG)
    _pkg.__path__ = [str(INTEGRATION_DIR)]  # type: ignore[attr-defined]
    sys.modules[_PKG] = _pkg
    # 顺序很重要：api 依赖 const 与 sign，需先注册进 sys.modules
    for _name in ("const", "sign", "api"):
        _spec = importlib.util.spec_from_file_location(
            f"{_PKG}.{_name}", INTEGRATION_DIR / f"{_name}.py",
        )
        _mod = importlib.util.module_from_spec(_spec)
        sys.modules[f"{_PKG}.{_name}"] = _mod
        _spec.loader.exec_module(_mod)

from _tws_under_test.api import towngashaApi  # noqa: E402
from _tws_under_test.sign import compute_sign  # noqa: E402


# ----------------------------------------------------------------------
#  3. 假的 aiohttp session，用来捕获请求
# ----------------------------------------------------------------------

OK_BODY = json.dumps({
    "access_token": "AT-123",
    "refresh_token": "RT-456",
    "expires_in": 7200,
    "scope": "",
    "openId": "",
})


class _FakeResponse:
    def __init__(self, body: str, status: int = 200) -> None:
        self._body = body
        self.status = status

    async def text(self) -> str:
        return self._body

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeSession:
    def __init__(self, body: str = OK_BODY) -> None:
        self.body = body
        self.calls: list[tuple[str, str, dict]] = []

    def post(self, url: str, **kwargs: object) -> _FakeResponse:
        self.calls.append(("POST", url, kwargs))
        return _FakeResponse(self.body)

    def get(self, url: str, **kwargs: object) -> _FakeResponse:
        self.calls.append(("GET", url, kwargs))
        return _FakeResponse(self.body)


def _call(auth_code: str = "CODE-abc", body: str = OK_BODY, **api_kwargs):
    """发一次 access_token_by_auth_code，返回 (session, url, headers, 响应)。"""
    session = _FakeSession(body)
    api = towngashaApi(session=session, host="weixin.towngasvcc.com", **api_kwargs)
    data = asyncio.run(api.access_token_by_auth_code(auth_code))
    method, url, kwargs = session.calls[0]
    assert method == "POST", f"必须是 POST，实际是 {method}"
    return api, url, kwargs.get("headers", {}), data


# ----------------------------------------------------------------------
#  4. 测试
# ----------------------------------------------------------------------

def test_path_keeps_server_typo() -> None:
    """路径必须保留服务端的拼写错误 accessToekn，改成 accessToken 会 404。"""
    _, url, _, _ = _call()
    assert "/vcc-oauth/oauth/authorize2/accessToekn?" in url, url
    assert "accessToken?" not in url, "不要「纠正」服务端的拼写错误"


def test_no_authorization_header() -> None:
    """换取 token 前还没有 token，请求头不应带 Authorization。"""
    _, _, headers, _ = _call()
    assert "Authorization" not in headers, headers
    assert "User-Agent" in headers, "需要带 User-Agent"


def test_query_params() -> None:
    """authCode / timestamp / sign 三个参数都要在 query 里。"""
    _, url, _, _ = _call("CODE-abc")
    q = parse_qs(urlparse(url).query)
    assert q["authCode"] == ["CODE-abc"]
    assert q["timestamp"][0].isdigit() and len(q["timestamp"][0]) == 13, q["timestamp"]
    assert len(q["sign"][0]) == 32


def test_sign_is_computed_over_authcode_and_timestamp() -> None:
    """sign 必须等于对 {authCode, timestamp} 计算的结果。"""
    _, url, _, _ = _call("CODE-abc")
    q = parse_qs(urlparse(url).query)
    params = {k: v[0] for k, v in q.items() if k != "sign"}
    assert q["sign"][0] == compute_sign(params), "sign 与服务端算法不一致"


def test_manual_sign_overrides_auto() -> None:
    """配置了手动 sign 时应优先生效（兜底路径）。"""
    _, url, _, _ = _call(sign="MANUALSIGN")
    assert "sign=MANUALSIGN" in url, url


def test_tokens_are_stored_on_success() -> None:
    """成功后本实例的 token 状态应被更新，供后续业务接口复用。"""
    api, _, _, data = _call()
    assert data["access_token"] == "AT-123"
    assert api.access_token == "AT-123"
    assert api.refresh_token == "RT-456"
    assert api.token_expires_in == 7200
    assert api.token_create_time > 0


def test_failure_does_not_overwrite_tokens() -> None:
    """响应里没有 access_token 时，不能污染已有 token 状态。"""
    session = _FakeSession(json.dumps({"resultCode": "90143", "resultMsg": "已失效"}))
    api = towngashaApi(
        session=session, host="weixin.towngasvcc.com",
        access_token="OLD-AT", refresh_token="OLD-RT",
    )
    data = asyncio.run(api.access_token_by_auth_code("BAD"))
    assert "access_token" not in data
    assert api.access_token == "OLD-AT", "失败时不应覆盖原 access_token"
    assert api.refresh_token == "OLD-RT", "失败时不应覆盖原 refresh_token"


def test_non_json_body_is_handled() -> None:
    """响应体不是 JSON 时应返回空字典而不是抛异常。"""
    _, _, _, data = _call(body="<html>502 Bad Gateway</html>")
    assert data == {}


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
