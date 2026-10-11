"""港华燃气 API 客户端 — 异步封装，仅依赖 aiohttp。

API 说明：
- refreshToken 接口：用 refreshToken 刷新 access_token，无需 sign
- 业务接口（getLoginUserInfo 等）：URL 中拼接 sign 参数，Header 带 Bearer token
- sign 默认按 H5 前端的 requestSign 算法自动计算（见 sign.py），
  也可在 ConfigFlow 中手动指定固定值（兼容旧配置）
- 所有接口正常调用时 HTTP 响应码均为 200，成功与否只能通过响应体 resultCode="0" 判断
- 认证失败 resultCode="20001"，resultMsg="access token 过期"
- refreshToken 失效 resultCode="90143"，resultMsg="refreshToken已失效"
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
import time
from typing import Any
from urllib.parse import urlencode

import aiohttp

from .const import (
    API_PATH,
    OAUTH_PATH,
    TOKEN_EXPIRY_BUFFER_SECS,
    TOKEN_EXPIRES_IN,
    USER_AGENT,
)
from .crypto import decrypt_response
from .sign import compute_sign

_LOGGER = logging.getLogger(__name__)

# 跳过 SSL 证书验证（与参考项目一致，部分燃气公司证书可能不被信任）
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

# 日志响应体最大显示长度，超长截断防止刷屏
_LOG_RESP_MAX_LEN = 2000


def _truncate(data: Any, max_len: int = _LOG_RESP_MAX_LEN) -> str:
    """将响应体转为 JSON 字符串并截断，防止日志过长。"""
    try:
        text = json.dumps(data, ensure_ascii=False)
    except Exception:
        text = str(data)
    return text if len(text) <= max_len else text[:max_len] + "...(truncated)"


class AuthError(Exception):
    """认证失败异常（refreshToken 失效等不可恢复错误）。"""


class towngashaApi:
    """港华燃气 API 异步客户端。"""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        access_token: str | None = None,
        refresh_token: str | None = None,
        sign: str | None = None,
        token_create_time: float = 0,
        token_expires_in: int = TOKEN_EXPIRES_IN,
    ) -> None:
        self._session = session
        self._host = self._normalize_host(host)  # 规范化 host，确保带协议前缀
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.sign = sign or ""
        self.token_create_time = token_create_time
        self.token_expires_in = token_expires_in
        self._refresh_lock = asyncio.Lock()  # 防止并发刷新 token
        # 诊断日志：确认初始化参数
        # _LOGGER.debug(
        #     "[API Init] host=%s access_token=%s refresh_token=%s sign=%s token_create_time=%s",
        #     self._host, bool(self.access_token), bool(self.refresh_token), bool(self.sign), self.token_create_time,
        # )

    @staticmethod
    def _normalize_host(host: str) -> str:
        """规范化 host，确保带 https:// 前缀且无尾部斜杠。

        orglist.json 中部分 host 带 https:// 前缀，部分不带，需统一处理。
        """
        host = (host or "").strip()
        if not host:
            return ""
        if not host.startswith(("http://", "https://")):
            host = f"https://{host}"
        return host.rstrip("/")

    # ------------------------------------------------------------------
    #  Token 有效性检查
    # ------------------------------------------------------------------

    @property
    def bearer_valid(self) -> bool:
        """access_token 是否仍然有效（提前 buffer 秒视为过期）。"""
        if not self.access_token or not self.token_create_time:
            # _LOGGER.debug("[bearer_valid] 返回False: access_token=%s token_create_time=%s", bool(self.access_token), self.token_create_time)
            return False
        valid = (
            self.token_create_time + self.token_expires_in - TOKEN_EXPIRY_BUFFER_SECS
            > time.time()
        )
        # _LOGGER.debug("[bearer_valid] valid=%s create_time=%s expires_in=%s buffer=%s now=%s", valid, self.token_create_time, self.token_expires_in, TOKEN_EXPIRY_BUFFER_SECS, time.time())
        return valid

    @property
    def bearer_remain(self) -> int:
        """access_token 剩余有效时间（秒）。"""
        if not self.bearer_valid:
            return 0
        return int(
            self.token_create_time + self.token_expires_in - time.time()
        )

    # ------------------------------------------------------------------
    #  Token 刷新
    # ------------------------------------------------------------------

    async def refresh_access_token(self) -> bool:
        """调用 refreshToken 接口刷新 access_token。

        使用 asyncio.Lock 防止多个并发请求同时刷新 token。
        请求 Header 需要带当前（可能已过期的）access_token。
        成功后更新 access_token / refresh_token / token_create_time。
        """
        if not self.refresh_token:
            _LOGGER.warning("无 refresh_token，无法刷新")
            return False

        # 加锁：防止多个并发请求同时刷新 token
        async with self._refresh_lock:
            ts = int(time.time() * 1000)  # 13位毫秒时间戳
            url = (
                f"{self._host}{OAUTH_PATH}/refreshToken"
                f"?timestamp={ts}"
                f"&refreshToken={self.refresh_token}"
            )
            # refreshToken 接口需要带当前 access_token（即使可能已过期）
            headers = {
                "User-Agent": USER_AGENT,
                "Authorization": f"Bearer {self.access_token or ''}",
            }

            # 打印请求日志
            _LOGGER.debug("[refreshToken] 请求: method=POST url=%s headers=%s",url, headers,)

            try:
                async with self._session.post(
                    url, headers=headers, ssl=_SSL_CTX,
                ) as resp:
                    # 所有接口正常调用 HTTP 响应码都是 200，但仍记录实际状态码
                    body = await resp.text()
                    _LOGGER.debug("[refreshToken] 响应: http_status=%s body=%s",resp.status, _truncate(body),)
                    try:
                        data = json.loads(body) if body else {}
                    except json.JSONDecodeError:
                        _LOGGER.warning("[refreshToken] 响应体非 JSON: %s", _truncate(body))
                        return False

                # refreshToken 接口正常响应没有 resultCode，直接包含 access_token 等字段
                # 只有失效时才返回 resultCode=90143
                if isinstance(data, dict) and "access_token" in data:
                    # 刷新成功，更新 token 信息
                    self.access_token = data.get("access_token", self.access_token)
                    self.refresh_token = data.get("refresh_token", self.refresh_token)
                    self.token_expires_in = data.get("expires_in", TOKEN_EXPIRES_IN)
                    self.token_create_time = time.time()
                    _LOGGER.debug("[refreshToken] 刷新成功, expires_in=%ss remain=%ss",self.token_expires_in, self.bearer_remain,)
                    return True

                # resultCode=90143 表示 refreshToken 已失效，不可恢复
                result_code = data.get("resultCode") if isinstance(data, dict) else None
                if result_code == "90143":
                    _LOGGER.warning("[refreshToken] 认证失败: resultCode=%s msg=%s",result_code, data.get("resultMsg", ""),)
                    raise AuthError(
                        f"refreshToken 已失效: resultCode={result_code} "
                        f"msg={data.get('resultMsg', '')}"
                    )

                # 其他异常情况
                _LOGGER.warning("[refreshToken] 返回异常: %s",_truncate(data) if data else "empty",)
                return False

            except AuthError:
                raise
            except Exception:
                _LOGGER.exception("[refreshToken] 请求异常")
                return False

    async def ensure_token(self) -> bool:
        """确保 access_token 可用。

        不再依赖 bearer_valid 提前刷新——直接用当前 access_token 发请求，
        由 _cbs_get 在收到 resultCode=20001 时按需刷新并重试。
        这样可以避免 refreshToken 接口返回 10001（系统错误）时阻塞整个流程。
        """
        if not self.access_token:
            _LOGGER.warning("[ensureToken] 无 access_token，无法继续")
            return False
        return True

    # ------------------------------------------------------------------
    #  微信授权码换 token
    # ------------------------------------------------------------------

    async def access_token_by_auth_code(self, auth_code: str) -> dict[str, Any]:
        """用微信授权码（authCode）换取 access_token / refresh_token。

        接口：``POST {host}/vcc-oauth/oauth/authorize2/accessToekn``

        与 refreshToken 接口的区别与注意事项：
        - 路径中的 ``accessToekn`` 是**服务端自身的拼写错误**（正确拼写应为
          ``accessToken``），必须照原样使用，改成正确拼写会 404。
        - 请求头**不带** Authorization —— 此时还没有任何 token。
        - sign 按 ``authCode`` + ``timestamp`` 计算（与其他业务接口同一算法）。
        - 成功时响应体直接是 ``{access_token, refresh_token, expires_in, scope,
          openId}``，**没有 resultCode 字段**。

        换取成功后同步更新本实例的 token 状态，调用方可直接复用本实例
        继续调用 getLoginUserInfo 等业务接口。

        :param auth_code: 微信 OAuth 授权码（H5 地址栏 ``?code=`` 或
                          accessToekn 请求参数 ``authCode`` 的值）
        :return: 接口原始响应字典；失败时返回空字典或错误响应体
        """
        ts = int(time.time() * 1000)  # 13位毫秒时间戳
        params: dict[str, Any] = {"authCode": auth_code, "timestamp": ts}
        # sign 与「参数 + 时间戳」强绑定，按同一算法计算（见 sign.py）
        params["sign"] = self.sign or compute_sign(params)
        url = f"{self._host}{OAUTH_PATH}/accessToekn?{urlencode(params)}"
        # 换取 token 前没有 access_token，因此不带 Authorization 头
        headers = {"User-Agent": USER_AGENT}

        _LOGGER.debug("[accessToekn] 请求: POST url=%s", url)

        try:
            async with self._session.post(
                url, headers=headers, ssl=_SSL_CTX,
            ) as resp:
                body = await resp.text()
                _LOGGER.debug(
                    "[accessToekn] 响应: http_status=%s body=%s",
                    resp.status, _truncate(body),
                )
                try:
                    data = json.loads(body) if body else {}
                except json.JSONDecodeError:
                    _LOGGER.warning("[accessToekn] 响应体非 JSON: %s", _truncate(body))
                    return {}
        except Exception:
            _LOGGER.exception("[accessToekn] 请求异常")
            return {}

        if isinstance(data, dict) and data.get("access_token"):
            # 换取成功：更新本实例 token 状态
            self.access_token = data["access_token"]
            self.refresh_token = data.get("refresh_token", self.refresh_token)
            self.token_expires_in = data.get("expires_in", TOKEN_EXPIRES_IN)
            self.token_create_time = time.time()
            _LOGGER.debug(
                "[accessToekn] 换取成功, expires_in=%ss", self.token_expires_in,
            )
        else:
            _LOGGER.warning("[accessToekn] 换取失败: %s", _truncate(data))

        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------------
    #  业务 API 通用请求方法
    # ------------------------------------------------------------------

    async def _cbs_get(
        self, path: str, params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """签名 GET 请求 /nv1/vcc-cbs/* 接口。

        自动拼接 timestamp 和 sign 参数，带 Bearer token。
        所有接口正常调用 HTTP 响应码均为 200，通过 resultCode 判断成功。
        遇到 resultCode "20001"（token 过期）时自动刷新并重试一次。
        """
        if not await self.ensure_token():
            raise AuthError("无可用 access_token，且 refresh 失败")

        def _build() -> tuple[str, dict[str, str]]:
            """构建带 sign 的请求 URL 和 Header。"""
            ts = int(time.time() * 1000)  # 13位毫秒时间戳
            all_params: dict[str, Any] = {**(params or {}), "timestamp": ts}
            # sign 与「本次请求的全部参数 + 时间戳」强绑定，每次请求都不同。
            # 默认按 H5 前端的 requestSign 算法自动计算（见 sign.py）；
            # 若配置中手动指定了 sign，则优先使用，以兼容按抓包固定 sign 使用的旧方式。
            all_params["sign"] = self.sign or compute_sign(all_params)
            built_url = (
                f"{self._host}{API_PATH}{path}?{urlencode(all_params)}"
            )
            built_headers = {
                "User-Agent": USER_AGENT,
                "Authorization": f"Bearer {self.access_token}",
            }
            return built_url, built_headers

        url, headers = _build()
        # 记录本次请求实际发送的 access_token —— 响应解密用的密钥由它推导，
        # 若之后发生了 token 刷新，密文也必须用「当次」的 token 来解。
        request_token = self.access_token

        # _LOGGER.debug("[GET] 请求: method=GET path=%s url=%s headers=%s", path, url, headers)

        async with self._session.get(url, headers=headers, ssl=_SSL_CTX) as resp:
            # 所有接口正常调用 HTTP 响应码都是 200，但仍记录实际状态码
            body = await resp.text()

        data = self._decode_body(body, request_token, path)
        result_code = data.get("resultCode")

        # token 过期：刷新后重试一次
        if result_code == "20001":
            _LOGGER.warning("[GET] 接口返回 token 过期(resultCode=20001)，刷新后重试: %s", path,)
            ok = await self.refresh_access_token()
            if not ok:
                raise AuthError("access_token 过期且 refresh 失败")
            # 用新 token 重新构建 URL 并请求
            retry_url, retry_headers = _build()
            retry_token = self.access_token  # 重试用的是新 token，解密也要跟着换
            async with self._session.get(
                retry_url, headers=retry_headers, ssl=_SSL_CTX,
            ) as retry:
                retry_body = await retry.text()
            return self._decode_body(retry_body, retry_token, path)

        return data

    def _decode_body(
        self, body: str, access_token: str | None, path: str,
    ) -> dict[str, Any]:
        """解析响应体，必要时解密。

        本平台部分区域的**成功响应是加密的**，只返回
        ``{"encryptData": "<base64>"}``；而**错误响应是明文**
        （例如 ``{"resultCode":"20001", ...}``）。

        因此必须先解密再判断 resultCode —— 否则解密后的成功响应会被
        当成「没有 resultCode」而误判为认证失败。

        解密密钥由 ``access_token`` 推导（见 crypto.py），所以必须传入
        **该次请求实际发送的** token。
        """
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            _LOGGER.warning("[GET] 响应体非 JSON: path=%s body=%s", path, _truncate(body))
            return {}

        if isinstance(data, dict) and data.get("encryptData"):
            try:
                decrypted = decrypt_response(data, access_token or "")
            except Exception:
                _LOGGER.exception(
                    "[GET] 响应解密失败: path=%s。密钥由本次请求的 access_token 推导，"
                    "token 不匹配、或服务端加密方式变更时会触发。",
                    path,
                )
                return {}
            if isinstance(decrypted, dict):
                _LOGGER.debug("[GET] 响应已解密: path=%s keys=%s", path, list(decrypted))
                return decrypted
            _LOGGER.warning("[GET] 解密结果不是 JSON 对象: path=%s type=%s", path, type(decrypted).__name__)
            return {}

        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------------
    #  业务接口
    # ------------------------------------------------------------------

    async def get_login_user_info(self) -> dict[str, Any]:
        """获取登录用户信息（mobile、userId 等）。

        用于 ConfigFlow 中获取 mobile（token 文件命名）和 userId。
        """
        return await self._cbs_get("/usersubs/getLoginUserInfo")

    async def query_bind_list(self, org_id: str) -> dict[str, Any]:
        """查询绑定户号列表。

        返回 datas 数组，包含用户下所有户号信息（subsId、displayAddr 等）。
        """
        return await self._cbs_get(
            "/usersubs/queryBindList",
            {"isPay": "N", "orgId": org_id},
        )

    async def get_gas_fee_baseinfo(
        self, org_id: str, subs_id: str,
    ) -> dict[str, Any]:
        """获取费用信息（feePayable、availableBalance、lastMeterReadingDate）。"""
        return await self._cbs_get(
            "/charge/gasFeeBaseinfo",
            {"orgId": org_id, "subsId": subs_id},
        )

    async def get_gas_consumption_data(
        self, subs_id: str,
    ) -> dict[str, Any]:
        """获取用气记录数据（gasConsumptionTrendInfo、gasConsumptionInfo）。"""
        return await self._cbs_get(
            "/carelessWorkorder/gasConsumptionDataQuery",
            {"subsId": subs_id},
        )

    async def get_gas_step_fee(
        self, org_id: str, subs_id: str,
    ) -> dict[str, Any]:
        """查询阶梯气价（buyamount 本期费用、stepList 阶梯列表）。"""
        return await self._cbs_get(
            "/charge/gasStepFee",
            {"orgId": org_id, "subsId": subs_id},
        )

    async def charge_precheck(self, org_id: str, subs_id: str,) -> dict[str, Any]:
        """缴费预检查，输出结果的datas.readingRptList[].currReading为当前表读数。"""
        return await self._cbs_get(
            "/charge/preCheck",
            {"orgId": org_id, "subsId": subs_id},
        )