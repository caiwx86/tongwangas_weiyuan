"""通用小工具（不依赖 Home Assistant，便于独立测试）。"""

from __future__ import annotations

import re
from urllib.parse import unquote

# 从任意文本中提取 authCode。
#
# 兼容以下三种粘贴内容（用户从抓包工具里复制，形态不可控）：
# 1. accessToekn 请求的完整 URL 或整段请求原文
#    POST /vcc-oauth/oauth/authorize2/accessToekn?authCode=XXXX&sign=...&timestamp=...
# 2. 微信授权跳转后的页面 URL（在 Referer 头里）
#    https://.../h5-gas/pages/transitionPage/index?code=XXXX
# 3. 裸的 authCode 字符串
#
# 两种参数名都会匹配：authCode（业务接口用的名字）与 code（微信 OAuth 的名字）。
# 值中不允许出现 & 空白 引号 尖括号，避免把后续参数一起吃进来。
_AUTH_CODE_PATTERN = re.compile(
    r"(?:authCode=|[?&]code=)([^&\s\"'<>]+)",
    re.IGNORECASE,
)


def extract_auth_code(raw: str) -> str:
    """从用户粘贴的内容中提取 authCode。

    支持粘贴：完整请求 URL、整段 HTTP 请求原文、页面 URL、或裸的 authCode。
    若无法识别出参数形式，则把整串当作裸 authCode 原样返回。

    :param raw: 用户粘贴的原始文本
    :return: 提取到的 authCode；输入为空时返回空串
    """
    text = (raw or "").strip()
    if not text:
        return ""

    match = _AUTH_CODE_PATTERN.search(text)
    if match:
        # URL 中的值可能做过百分号编码，这里还原
        return unquote(match.group(1)).strip()

    # 没有参数形式，按裸 authCode 处理
    return text
