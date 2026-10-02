from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

from novel.store import ValidationError, trim, validate_memory


class ModelError(RuntimeError):
    pass


@dataclass(frozen=True)
class ModelConfig:
    endpoint: str = ''
    model: str = ''
    api_key: str = field(default='', repr=False)
    timeout: float = 120
    max_tokens: int = 8192

    @classmethod
    def from_env(cls):
        try:
            return cls(os.getenv('NOVEL_API_URL', '').strip(), os.getenv('NOVEL_MODEL', '').strip(),
                       os.getenv('NOVEL_API_KEY', '').strip(), float(os.getenv('NOVEL_API_TIMEOUT', '120')),
                       int(os.getenv('NOVEL_MAX_OUTPUT_TOKENS', '8192')))
        except ValueError:
            raise ValidationError('模型超时或最大输出 token 配置需要是数字。') from None

    def validate(self):
        if not self.endpoint or not self.model or not self.api_key:
            raise ValidationError('请在项目 .env 文件中填写模型接口地址、模型名称和 API 密钥，再重启应用。')
        url = urlparse(self.endpoint)
        if (url.scheme != 'https' and not (url.scheme == 'http' and url.hostname in ('localhost', '127.0.0.1', '::1'))):
            raise ValidationError('模型接口请使用 HTTPS；本机测试接口可使用 HTTP。')
        if not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValidationError('模型接口需要完整地址，不能包含账号、密码、查询参数或片段。')
        if not 5 <= self.timeout <= 600 or not 256 <= self.max_tokens <= 65536:
            raise ValidationError('模型超时范围为 5–600 秒，最大输出范围为 256–65536 token。')

    @property
    def ready(self):
        try:
            self.validate()
            return True
        except ValidationError:
            return False


@dataclass
class ModelResponse:
    text: str
    usage: dict
    finish_reason: str


class ModelClient:
    """Adapter for the chat-completions protocol; no default destination or model."""

    def __init__(self, config, transport=None):
        self.config = config
        self.transport = transport

    def generate(self, messages):
        self.config.validate()
        try:
            with httpx.Client(timeout=self.config.timeout, follow_redirects=False, transport=self.transport) as client:
                response = client.post(self.config.endpoint,
                                       headers={'Authorization': f'Bearer {self.config.api_key}'},
                                       json={'model': self.config.model, 'messages': messages,
                                             'stream': False, 'max_tokens': self.config.max_tokens})
        except httpx.TimeoutException:
            raise ModelError('模型请求超时；原稿已保留。服务端可能已产生用量，请核对后手动重试。') from None
        except httpx.HTTPError:
            raise ModelError('无法连接模型服务；请检查接口地址与网络。原稿已保留。') from None
        if response.status_code != 200:
            message = {401: '密钥无效或无权限', 403: '服务拒绝访问', 429: '额度不足或请求过于频繁',
                       400: '请求参数与所选接口不兼容', 404: '接口路径或模型不存在'}.get(
                response.status_code, '模型服务未成功返回')
            raise ModelError(f'{message}（HTTP {response.status_code}）。原稿已保留。')
        try:
            data = response.json()
            choice = data['choices'][0]
            text = choice['message']['content']
            if not isinstance(text, str) or not text.strip():
                raise ValueError('Empty response')
            raw_usage = data.get('usage') or {}
            usage = {key: val for key, val in raw_usage.items()
                     if key in ('prompt_tokens', 'completion_tokens', 'total_tokens') and isinstance(val, int)}
            return ModelResponse(trim(text, keep_indent=True), usage, str(choice.get('finish_reason', 'unknown')))
        except (KeyError, IndexError, TypeError, ValueError, AttributeError):
            raise ModelError('模型返回为空或不符合 Chat Completions 格式，请检查服务接口。') from None


def parse_memory(text):
    text = text.strip()
    if text.startswith('```'):
        text = re.sub(r'^```(?:json)?\s*|\s*```$', '', text, flags=re.IGNORECASE).strip()
    try:
        data = json.loads(text)
        if not isinstance(data, dict) or set(data) != {'summary', 'facts'}:
            raise ValueError('Expected memory')
        summary, facts = validate_memory(data['summary'], data['facts'])
        return {'summary': summary, 'facts': facts}
    except (TypeError, KeyError, ValueError) as error:
        raise ValidationError('记忆提取结果格式不完整。请手动填写本章摘要与事实，或重新提取。') from error
