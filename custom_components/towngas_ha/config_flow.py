"""港华燃气集成配置流。

配置流程（3步）：
1. 选择燃气公司（从 orglist.json 读取，下拉框展示 orgName，value 为 orgId）
2. 输入 access_token、refresh_token、sign，调用 getLoginUserInfo 验证并获取 mobile
3. 调用 queryBindList 获取户号列表，多选要添加的户号（每个户号 = 一个设备）

Options 流：集成添加后可在集成页面重新配置 access_token、refresh_token、sign、刷新间隔。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from .api import AuthError, towngashaApi
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_AUTH_CODE,
    CONF_HOST,
    CONF_MOBILE,
    CONF_ORG_CODE,
    CONF_ORG_ID,
    CONF_ORG_NAME,
    CONF_REFRESH_TOKEN,
    CONF_SCAN_INTERVAL,
    CONF_SIGN,
    CONF_SUBS,
    CONF_SUBS_IDS,
    CONF_TOKEN_CREATE_TIME,
    CONF_TOKEN_REFRESH_INTERVAL,
    CONF_USER_ID,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TOKEN_REFRESH_INTERVAL,
    DOMAIN,
)
from .util import extract_auth_code

_LOGGER = logging.getLogger(__name__)


# ------------------------------------------------------------------
#  orglist.json 读取
# ------------------------------------------------------------------

def _load_orglist_sync() -> list[dict[str, Any]]:
    """同步读取集成根目录下的 orglist.json，返回 orgList 数组。

    orglist.json 中的键名为 camelCase（orgId、orgName、orgCode、host），
    这里统一转换为 CONF_* 常量（snake_case），方便后续代码一致使用。
    """
    orglist_path = Path(__file__).parent / "orglist.json"
    with orglist_path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    orgs = data.get("orgList", []) if isinstance(data, dict) else []
    # 只保留有 orgId、orgName 的有效条目，并统一键名
    result: list[dict[str, Any]] = []
    for org in orgs:
        if not isinstance(org, dict):
            continue
        org_id = org.get("orgId")
        org_name = org.get("orgName")
        if not org_id or not org_name:
            continue
        result.append({
            CONF_ORG_ID: org_id,
            CONF_ORG_NAME: org_name,
            CONF_ORG_CODE: org.get("orgCode", ""),
            CONF_HOST: org.get("host", ""),
        })
    return result


async def _async_load_orglist(hass: HomeAssistant) -> list[dict[str, Any]]:
    """在线程池中读取 orglist.json，避免阻塞事件循环。"""
    return await hass.async_add_executor_job(_load_orglist_sync)


# ------------------------------------------------------------------
#  Config Flow
# ------------------------------------------------------------------

class towngashaConfigFlow(ConfigFlow, domain=DOMAIN):
    """处理港华燃气的配置流。"""

    VERSION = 1

    def __init__(self) -> None:
        """初始化配置流。"""
        self._org_info: dict[str, Any] = {}       # 选中的燃气公司完整信息
        self._access_token: str = ""
        self._refresh_token: str = ""
        self._sign: str = ""
        self._mobile: str = ""                     # 从 getLoginUserInfo 获取
        self._user_id: str = ""
        self._all_subs: list[dict[str, Any]] = []  # queryBindList 返回的全部户号

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: Any) -> OptionsFlow:
        """返回 Options 流处理器。"""
        return towngashaOptionsFlow(config_entry)

    # ------------------------------------------------------------------
    #  Step 1: 选择燃气公司
    # ------------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """配置流第一步：从 orglist.json 生成下拉框选择燃气公司。"""
        if user_input is not None:
            org_id = user_input[CONF_ORG_ID]
            # 从已加载的列表中找到完整 org 信息
            orgs = await _async_load_orglist(self.hass)
            self._org_info = next(
                (org for org in orgs if org.get(CONF_ORG_ID) == org_id), {},
            )
            return await self.async_step_auth()

        # 加载 orglist.json 构建下拉选项
        try:
            orgs = await _async_load_orglist(self.hass)
        except Exception:
            _LOGGER.exception("读取 orglist.json 失败")
            return self.async_abort(reason="orglist_load_failed")

        # 下拉框：展示 orgName，value 为 orgId（可搜索）
        options = [
            {"value": org[CONF_ORG_ID], "label": org[CONF_ORG_NAME]}
            for org in orgs
        ]

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_ORG_ID): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=options,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
            }),
        )

    # ------------------------------------------------------------------
    #  Step 2: 选择认证方式（微信授权码 / 手动粘贴 token）
    # ------------------------------------------------------------------

    async def async_step_auth(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """配置流第二步：选择用「微信授权码」还是「手动粘贴 token」认证。"""
        return self.async_show_menu(
            step_id="auth",
            menu_options=["auth_code", "auth_token"],
        )

    # ---- 方式一：微信授权码换取 token（推荐）----

    async def async_step_auth_code(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """用微信授权码（authCode）直接换取 access_token / refresh_token。"""
        errors: dict[str, str] = {}

        if user_input is not None:
            # 允许直接粘贴整段抓包内容，这里自动提取出 authCode
            auth_code = extract_auth_code(user_input.get(CONF_AUTH_CODE, ""))
            self._sign = user_input.get(CONF_SIGN, "").strip()

            if not auth_code:
                errors["base"] = "auth_code_required"
            else:
                host = self._org_info.get(CONF_HOST, "")
                _LOGGER.debug(
                    "[ConfigFlow] Step auth_code — host=%s orgName=%s",
                    host, self._org_info.get(CONF_ORG_NAME, ""),
                )
                session = async_get_clientsession(self.hass, verify_ssl=False)
                api = towngashaApi(
                    session=session, host=host, sign=self._sign,
                )
                try:
                    token_data = await api.access_token_by_auth_code(auth_code)
                except Exception:
                    _LOGGER.exception("accessToekn 请求失败")
                    errors["base"] = "connect_error"
                else:
                    if token_data.get("access_token"):
                        # 换取成功，api 实例内部已更新 token，直接复用它取 mobile
                        result, err = await self._async_resolve_mobile(api)
                        if result is not None:
                            return result
                        errors["base"] = err or "auth_failed"
                    else:
                        errors["base"] = "auth_code_invalid"

        return self.async_show_form(
            step_id="auth_code",
            description_placeholders={
                "org_name": self._org_info.get(CONF_ORG_NAME, ""),
            },
            data_schema=vol.Schema({
                vol.Required(CONF_AUTH_CODE): str,
                vol.Optional(CONF_SIGN, default=""): str,
            }),
            errors=errors,
        )

    # ---- 方式二：手动粘贴 token ----

    async def async_step_auth_token(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """手动输入 access_token、refresh_token、sign 并验证。"""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._access_token = user_input[CONF_ACCESS_TOKEN].strip()
            # refresh_token 允许留空：仅凭 access_token 也能添加集成，
            # 但 access_token 只有 2 小时有效期，过期后需重新抓包配置。
            self._refresh_token = user_input.get(CONF_REFRESH_TOKEN, "").strip()
            self._sign = user_input.get(CONF_SIGN, "").strip()

            host = self._org_info.get(CONF_HOST, "")
            _LOGGER.debug(
                "[ConfigFlow] Step auth_token — host=%s orgName=%s",
                host, self._org_info.get(CONF_ORG_NAME, ""),
            )
            session = async_get_clientsession(self.hass, verify_ssl=False)
            # token_create_time 设为当前时间，避免刚输入的有效 token 被误判为过期而触发刷新
            api = towngashaApi(
                session=session,
                host=host,
                access_token=self._access_token,
                refresh_token=self._refresh_token,
                sign=self._sign,
                token_create_time=time.time(),
            )
            result, err = await self._async_resolve_mobile(api)
            if result is not None:
                return result
            errors["base"] = err or "auth_failed"

        return self.async_show_form(
            step_id="auth_token",
            description_placeholders={
                "org_name": self._org_info.get(CONF_ORG_NAME, ""),
            },
            data_schema=vol.Schema({
                vol.Required(CONF_ACCESS_TOKEN): str,
                vol.Optional(CONF_REFRESH_TOKEN, default=""): str,
                # sign 留空即按 H5 前端算法自动计算（推荐）
                vol.Optional(CONF_SIGN, default=""): str,
            }),
            errors=errors,
        )

    # ---- 两种方式共用的后续处理 ----

    async def _async_resolve_mobile(
        self, api: towngashaApi,
    ) -> tuple[ConfigFlowResult | None, str | None]:
        """用已有 token 调 getLoginUserInfo 取回 mobile / userId。

        成功时保存 token 到本地文件，并返回 (跳转「选择户号」的返回值, None)；
        失败时返回 (None, 错误码)。

        注意：这里刻意把失败原因拆成不同的错误码，并把服务端返回的
        resultCode / resultMsg 记入日志。早期版本统一返回 auth_failed，
        导致「token 过期且续期失败」「返回码非 0」等情况无法区分，
        用户只看到一句「认证失败」，无从排查。
        """
        try:
            user_info = await api.get_login_user_info()
        except AuthError as err:
            # access_token 已过期，且用 refresh_token 自动续期失败
            _LOGGER.error(
                "认证失败：access_token 已过期，且用 refresh_token 自动续期失败（%s）。"
                "请确认 refresh_token 填写正确、且未被新登录作废；"
                "也可改用「微信授权码」方式登录。",
                err,
            )
            return None, "token_refresh_failed"
        except Exception:
            _LOGGER.exception("getLoginUserInfo 请求失败")
            return None, "connect_error"

        if user_info.get("resultCode") == "0" and user_info.get("mobile"):
            self._mobile = user_info["mobile"]
            self._user_id = user_info.get("userId", "")
            # api 内部可能已更新过 token（如 accessToekn 换取），以它为准
            self._access_token = api.access_token or self._access_token
            self._refresh_token = api.refresh_token or self._refresh_token
            await _async_save_token_file(
                self.hass, self._mobile,
                self._access_token, self._refresh_token, time.time(),
            )
            return await self.async_step_select_subs(), None

        # 服务端已经给出了原因，必须记录下来，否则用户无从排查
        _LOGGER.error(
            "认证失败：getLoginUserInfo 返回 resultCode=%s resultMsg=%s，完整响应=%s",
            user_info.get("resultCode"),
            user_info.get("resultMsg"),
            user_info,
        )
        if user_info.get("resultCode") == "0":
            # 返回码正常却没有手机号，通常与 sign 或账号绑定状态有关
            return None, "no_mobile"
        return None, "auth_rejected"

    # ------------------------------------------------------------------
    #  Step 3: 查询户号列表，多选要添加的户号
    # ------------------------------------------------------------------

    async def async_step_select_subs(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """配置流第三步：调用 queryBindList 获取户号列表，多选添加。"""
        errors: dict[str, str] = {}

        if user_input is not None:
            selected_ids: list[str] = user_input.get(CONF_SUBS_IDS, [])
            if not selected_ids:
                errors["base"] = "no_subs_selected"
            else:
                # 构建选中的户号信息列表
                selected_subs = [
                    subs for subs in self._all_subs
                    if subs.get("subsId") in selected_ids
                ]
                # 以 mobile 为唯一标识，防止重复添加同一用户
                await self.async_set_unique_id(self._mobile)
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=f"港华燃气 ({self._org_info.get(CONF_ORG_NAME, '')})",
                    data={
                        CONF_ORG_ID: self._org_info.get(CONF_ORG_ID, ""),
                        CONF_ORG_NAME: self._org_info.get(CONF_ORG_NAME, ""),
                        CONF_ORG_CODE: self._org_info.get(CONF_ORG_CODE, ""),
                        CONF_HOST: self._org_info.get(CONF_HOST, ""),
                        CONF_ACCESS_TOKEN: self._access_token,
                        CONF_REFRESH_TOKEN: self._refresh_token,
                        CONF_SIGN: self._sign,
                        CONF_TOKEN_CREATE_TIME: time.time(),
                        CONF_MOBILE: self._mobile,
                        CONF_USER_ID: self._user_id,
                        CONF_SUBS: selected_subs,
                    },
                    options={
                        CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
                        CONF_TOKEN_REFRESH_INTERVAL: DEFAULT_TOKEN_REFRESH_INTERVAL,
                    },
                )

        # 首次进入：调用 queryBindList 获取户号列表
        if not self._all_subs:
            host = self._org_info.get(CONF_HOST, "")
            session = async_get_clientsession(self.hass, verify_ssl=False)
            api = towngashaApi(
                session=session,
                host=host,
                access_token=self._access_token,
                refresh_token=self._refresh_token,
                sign=self._sign,
                token_create_time=time.time(),
            )
            try:
                result = await api.query_bind_list(
                    self._org_info.get(CONF_ORG_ID, ""),
                )
            except AuthError:
                errors["base"] = "auth_failed"
            except Exception:
                _LOGGER.exception("queryBindList 请求失败")
                errors["base"] = "connect_error"
            else:
                if result.get("resultCode") == "0":
                    self._all_subs = result.get("datas", [])
                    if not self._all_subs:
                        errors["base"] = "no_subs"
                else:
                    errors["base"] = "connect_error"

        if errors:
            return self.async_show_form(
                step_id="select_subs", data_schema=vol.Schema({}), errors=errors,
            )

        # 多选框：展示 displayAddr 和 name，value 为 subsId
        options = [
            {
                "value": subs["subsId"],
                "label": f"{subs.get('displayAddr', subs['subsId'])}"
                         f" - {subs.get('name', '')}",
            }
            for subs in self._all_subs
        ]

        return self.async_show_form(
            step_id="select_subs",
            data_schema=vol.Schema({
                vol.Required(CONF_SUBS_IDS): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=options,
                        multiple=True,
                        mode=selector.SelectSelectorMode.LIST,
                    )
                ),
            }),
        )

    # ------------------------------------------------------------------
    #  Reauth Flow — token 失效时自动触发重新认证
    # ------------------------------------------------------------------

    async def async_step_reauth(
        self, entry_data: dict[str, Any],
    ) -> ConfigFlowResult:
        """token 失效时触发：选择用授权码还是手动粘贴 token 重新认证。"""
        return self.async_show_menu(
            step_id="reauth",
            menu_options=["reauth_code", "reauth_confirm"],
        )

    # ---- 方式一：微信授权码重新换取 token（推荐）----

    async def async_step_reauth_code(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """用微信授权码重新换取 token，成功后更新配置条目并重载。"""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        entry_data = entry.data

        if user_input is not None:
            # 允许直接粘贴整段抓包内容，这里自动提取出 authCode
            auth_code = extract_auth_code(user_input.get(CONF_AUTH_CODE, ""))
            sign = user_input.get(CONF_SIGN, entry_data.get(CONF_SIGN, "")).strip()

            if not auth_code:
                errors["base"] = "auth_code_required"
            else:
                host = entry_data.get(CONF_HOST, "")
                session = async_get_clientsession(self.hass, verify_ssl=False)
                api = towngashaApi(session=session, host=host, sign=sign)
                try:
                    token_data = await api.access_token_by_auth_code(auth_code)
                except Exception:
                    _LOGGER.exception("reauth: accessToekn 请求失败")
                    errors["base"] = "connect_error"
                else:
                    if token_data.get("access_token"):
                        mobile = entry_data.get(CONF_MOBILE, "")
                        new_data = {
                            **entry_data,
                            CONF_ACCESS_TOKEN: api.access_token,
                            CONF_REFRESH_TOKEN: api.refresh_token or "",
                            CONF_SIGN: sign,
                            CONF_TOKEN_CREATE_TIME: time.time(),
                        }
                        if mobile:
                            await _async_save_token_file(
                                self.hass, mobile,
                                api.access_token, api.refresh_token or "",
                                time.time(),
                            )
                        return self.async_update_reload_and_abort(
                            entry, data=new_data,
                        )
                    errors["base"] = "auth_code_invalid"

        return self.async_show_form(
            step_id="reauth_code",
            description_placeholders={
                "org_name": entry_data.get(CONF_ORG_NAME, ""),
            },
            data_schema=vol.Schema({
                vol.Required(CONF_AUTH_CODE): str,
                vol.Optional(
                    CONF_SIGN, default=entry_data.get(CONF_SIGN, ""),
                ): str,
            }),
            errors=errors,
        )

    # ---- 方式二：手动粘贴 token ----

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """重新输入 access_token、refresh_token、sign 并验证。"""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        entry_data = entry.data

        if user_input is not None:
            access_token = user_input[CONF_ACCESS_TOKEN].strip()
            refresh_token = user_input.get(CONF_REFRESH_TOKEN, "").strip()
            sign = user_input.get(CONF_SIGN, "").strip()

            host = entry_data.get(CONF_HOST, "")
            session = async_get_clientsession(self.hass, verify_ssl=False)
            api = towngashaApi(
                session=session, host=host,
                access_token=access_token,
                refresh_token=refresh_token,
                sign=sign,
                token_create_time=time.time(),
            )
            try:
                user_info = await api.get_login_user_info()
            except AuthError:
                errors["base"] = "auth_failed"
            except Exception:
                _LOGGER.exception("reauth: getLoginUserInfo 请求失败")
                errors["base"] = "connect_error"
            else:
                if user_info.get("resultCode") == "0":
                    # 验证成功，更新 config entry data
                    mobile = entry_data.get(CONF_MOBILE, "")
                    new_data = {
                        **entry_data,
                        CONF_ACCESS_TOKEN: access_token,
                        CONF_REFRESH_TOKEN: refresh_token,
                        CONF_SIGN: sign,
                        CONF_TOKEN_CREATE_TIME: time.time(),
                    }
                    # 保存 token 到本地文件
                    if mobile:
                        await _async_save_token_file(
                            self.hass, mobile,
                            access_token, refresh_token, time.time(),
                        )
                    return self.async_update_reload_and_abort(
                        entry, data=new_data,
                    )
                errors["base"] = "auth_failed"

        return self.async_show_form(
            step_id="reauth_confirm",
            description_placeholders={
                "org_name": entry_data.get(CONF_ORG_NAME, ""),
            },
            data_schema=vol.Schema({
                vol.Required(
                    CONF_ACCESS_TOKEN,
                    default=entry_data.get(CONF_ACCESS_TOKEN, ""),
                ): str,
                vol.Optional(
                    CONF_REFRESH_TOKEN,
                    default=entry_data.get(CONF_REFRESH_TOKEN, ""),
                ): str,
                vol.Optional(
                    CONF_SIGN, default=entry_data.get(CONF_SIGN, ""),
                ): str,
            }),
            errors=errors,
        )


# ------------------------------------------------------------------
#  Options Flow — 集成添加后重新配置 token / sign / 刷新间隔
# ------------------------------------------------------------------

class towngashaOptionsFlow(OptionsFlow):
    """Options 流：支持在集成页面重新配置 access_token、refresh_token、sign。"""

    def __init__(self, config_entry: Any) -> None:
        self._entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """展示配置表单，保存后更新 config entry 并重载。"""
        if user_input is not None:
            # Options 中保存 scan_interval 和 token_refresh_interval
            options_data = {
                CONF_SCAN_INTERVAL: user_input[CONF_SCAN_INTERVAL],
                CONF_TOKEN_REFRESH_INTERVAL: user_input[CONF_TOKEN_REFRESH_INTERVAL],
            }
            # token 和 sign 更新到 config entry 的 data 中
            entry_data = dict(self._entry.data)
            entry_data[CONF_ACCESS_TOKEN] = user_input[CONF_ACCESS_TOKEN].strip()
            entry_data[CONF_REFRESH_TOKEN] = user_input.get(CONF_REFRESH_TOKEN, "").strip()
            entry_data[CONF_SIGN] = user_input.get(CONF_SIGN, "").strip()
            entry_data[CONF_TOKEN_CREATE_TIME] = time.time()

            # 同时更新本地 token 文件
            mobile = entry_data.get(CONF_MOBILE, "")
            if mobile:
                await _async_save_token_file(
                    self.hass, mobile,
                    entry_data[CONF_ACCESS_TOKEN],
                    entry_data[CONF_REFRESH_TOKEN],
                    entry_data[CONF_TOKEN_CREATE_TIME],
                )

            # 更新 config entry 的 data 和 options
            self.hass.config_entries.async_update_entry(
                self._entry, data=entry_data, options=options_data,
            )
            await self.hass.config_entries.async_reload(self._entry.entry_id)
            return self.async_create_entry(title="", data=options_data)

        # 展示当前值作为默认值
        data = self._entry.data
        opts = self._entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required(
                    CONF_ACCESS_TOKEN, default=data.get(CONF_ACCESS_TOKEN, ""),
                ): str,
                vol.Optional(
                    CONF_REFRESH_TOKEN, default=data.get(CONF_REFRESH_TOKEN, ""),
                ): str,
                vol.Optional(
                    CONF_SIGN, default=data.get(CONF_SIGN, ""),
                ): str,
                vol.Optional(
                    CONF_SCAN_INTERVAL,
                    default=opts.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                ): vol.All(int, vol.Range(min=300, max=86400)),
                vol.Optional(
                    CONF_TOKEN_REFRESH_INTERVAL,
                    default=opts.get(
                        CONF_TOKEN_REFRESH_INTERVAL, DEFAULT_TOKEN_REFRESH_INTERVAL,
                    ),
                ): vol.All(int, vol.Range(min=300, max=7140)),
            }),
        )


# ------------------------------------------------------------------
#  Token 本地文件存储（.storage/towngas_ha/{mobile}.json）
# ------------------------------------------------------------------

async def _async_save_token_file(
    hass: HomeAssistant,
    mobile: str,
    access_token: str,
    refresh_token: str,
    token_create_time: float,
) -> None:
    """将 token 保存到 .storage/towngas_ha/{mobile}.json。

    使用 HA 标准 Store helper，数据持久化到 .storage 目录，重启不丢失。
    """
    store = Store(hass, 1, f"{DOMAIN}/{mobile}")
    await store.async_save({
        CONF_ACCESS_TOKEN: access_token,
        CONF_REFRESH_TOKEN: refresh_token,
        CONF_TOKEN_CREATE_TIME: token_create_time,
    })


async def _async_load_token_file(
    hass: HomeAssistant, mobile: str,
) -> dict[str, Any] | None:
    """从 .storage/towngas_ha/{mobile}.json 加载 token。"""
    store = Store(hass, 1, f"{DOMAIN}/{mobile}")
    return await store.async_load()
