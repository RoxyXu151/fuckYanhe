import os
from typing import Literal

from pydantic import Field, SecretStr, model_validator

from ..cloud_asr import safe_base_url
from ..schema import StrictModel


class ModelSettings(StrictModel):
    asr_deployment: Literal['cloud', 'self-hosted'] = 'cloud'
    event_deployment: Literal['cloud', 'self-hosted'] = 'cloud'
    asr_provider: Literal['openai-compatible', 'gemini', 'groq'] = 'openai-compatible'
    asr_base_url: str = ''
    asr_model: str = ''
    asr_api_key: SecretStr = SecretStr('')
    event_provider: Literal['deepseek', 'openai-compatible'] = 'deepseek'
    event_base_url: str = 'https://api.deepseek.com'
    event_model: str = 'deepseek-flash'
    event_thinking: bool = False
    event_api_key: SecretStr = SecretStr('')
    vision_base_url: str = ''
    vision_model: str = ''
    vision_api_key: SecretStr = SecretStr('')

    @model_validator(mode='after')
    def validate_urls(self):
        if self.asr_deployment == 'self-hosted' and self.asr_provider != 'openai-compatible':
            raise ValueError('自建转写服务请选择兼容接口')
        if self.event_deployment == 'self-hosted' and self.event_provider != 'openai-compatible':
            raise ValueError('自建分析服务请选择兼容接口')
        for value in (self.asr_base_url, self.event_base_url, self.vision_base_url):
            if value:
                safe_base_url(value)
        return self

    @classmethod
    def from_environment(cls):
        provider = os.environ.get('YANHE_ASR_PROVIDER') or ('openai-compatible' if os.environ.get('YANHE_ASR_API_KEY')
            else 'gemini' if os.environ.get('GEMINI_API_KEY') else 'groq' if os.environ.get('GROQ_API_KEY') else 'openai-compatible')
        deepseek = bool(os.environ.get('DEEPSEEK_API_KEY')) or not any(os.environ.get(key) for key in ('YANHE_API_KEY', 'YANHE_BASE_URL', 'YANHE_MODEL'))
        return cls(asr_provider=provider,
            asr_deployment=os.environ.get('YANHE_ASR_DEPLOYMENT', 'cloud'),
            event_deployment=os.environ.get('YANHE_EVENT_DEPLOYMENT', 'cloud'),
            asr_base_url=os.environ.get('YANHE_ASR_BASE_URL', ''), asr_model=os.environ.get('YANHE_ASR_MODEL', ''),
            asr_api_key=os.environ.get('YANHE_ASR_API_KEY') or os.environ.get('GEMINI_API_KEY' if provider == 'gemini' else 'GROQ_API_KEY' if provider == 'groq' else 'YANHE_ASR_API_KEY', ''),
            event_provider='deepseek' if deepseek else 'openai-compatible',
            event_base_url=(os.environ.get('DEEPSEEK_BASE_URL') or 'https://api.deepseek.com') if deepseek else os.environ.get('YANHE_BASE_URL', ''),
            event_model=(os.environ.get('DEEPSEEK_MODEL') or 'deepseek-flash') if deepseek else os.environ.get('YANHE_MODEL', ''),
            event_thinking=os.environ.get('YANHE_QWEN_THINKING') == '1',
            event_api_key=os.environ.get('DEEPSEEK_API_KEY' if deepseek else 'YANHE_API_KEY', ''),
            vision_base_url=os.environ.get('YANHE_VISION_BASE_URL', ''), vision_model=os.environ.get('YANHE_VISION_MODEL', ''),
            vision_api_key=os.environ.get('YANHE_VISION_API_KEY', ''))

    def public(self):
        values = self.model_dump(exclude={'asr_api_key', 'event_api_key', 'vision_api_key'})
        for kind in ('asr', 'event', 'vision'):
            values[kind + '_key_configured'] = bool(getattr(self, kind + '_api_key').get_secret_value())
        return values

    def ready(self, *, subtitles=False, vision=False):
        missing = []
        if not subtitles:
            if self.asr_deployment == 'cloud' and not self.asr_api_key.get_secret_value():
                missing.append('音频转写密钥')
            if self.asr_provider == 'openai-compatible' and (not self.asr_base_url or not self.asr_model):
                missing.append('音频转写地址与模型')
        if (self.event_deployment == 'cloud' and not self.event_api_key.get_secret_value()) or not self.event_base_url or not self.event_model:
            missing.append('事项分析地址、模型与密钥')
        if missing:
            raise ValueError('请先在模型设置中配置：' + '、'.join(missing))

    def environment(self):
        environment = os.environ.copy()
        tools = {key: environment[key] for key in ('YANHE_FFMPEG', 'YANHE_FFPROBE') if key in environment}
        for name in list(environment):
            if name.startswith(('YANHE_', 'DEEPSEEK_')) or name in ('GEMINI_API_KEY', 'GROQ_API_KEY'):
                environment.pop(name)
        environment.update(tools)
        environment['YANHE_ASR_ALLOW_NO_KEY'] = '1' if self.asr_deployment == 'self-hosted' else '0'
        environment['YANHE_ALLOW_NO_KEY'] = '1' if self.event_deployment == 'self-hosted' else '0'
        for name, value in {
            'YANHE_ASR_API_KEY': self.asr_api_key.get_secret_value(), 'YANHE_ASR_BASE_URL': self.asr_base_url,
            'YANHE_ASR_MODEL': self.asr_model, 'YANHE_VISION_API_KEY': self.vision_api_key.get_secret_value() or self.event_api_key.get_secret_value(),
            'YANHE_VISION_BASE_URL': self.vision_base_url or self.event_base_url,
            'YANHE_VISION_MODEL': self.vision_model or self.event_model,
        }.items():
            if value:
                environment[name] = value
        prefix = 'DEEPSEEK' if self.event_provider == 'deepseek' else 'YANHE'
        environment.update({prefix + '_API_KEY': self.event_api_key.get_secret_value(),
                            prefix + '_BASE_URL': self.event_base_url, prefix + '_MODEL': self.event_model,
                            'YANHE_QWEN_THINKING': '1' if self.event_thinking else '0',
                            'PYTHONIOENCODING': 'utf-8', 'PYTHONUNBUFFERED': '1'})
        return environment


class JobRequest(StrictModel):
    course_id: str | None = None
    session_id: str | None = None
    video_id: str | None = None
    upload_id: str | None = None
    signal: Literal['camera', 'screen'] = 'screen'
    audio_source: Literal['original', 'microphone', 'camera'] = 'original'
    audio_offset: float = 0
    vision: bool = False
    vision_interval: float = Field(default=30, ge=1, le=3600)
    vision_max_frames: int = Field(default=240, ge=2, le=1000)
    export_clips: bool = True
    lecture_date: str | None = None
    force: bool = False

    @model_validator(mode='after')
    def has_source(self):
        if self.lecture_date:
            from datetime import date
            date.fromisoformat(self.lecture_date)
        if not self.upload_id and not all((self.course_id, self.session_id, self.video_id)):
            raise ValueError('请选择课次或上传本地文件')
        if self.upload_id and any((self.course_id, self.session_id, self.video_id)):
            raise ValueError('不能同时选择课次和本地文件')
        return self


class LibraryUpdate(StrictModel):
    archived: bool | None = None
    starred: bool | None = None
    notes: str | None = Field(default=None, max_length=2000)


class BatchRequest(StrictModel):
    course_ids: list[str] = Field(min_length=1, max_length=30)
    skip_completed: bool = True
    signal: Literal['auto', 'screen', 'camera'] = 'auto'
    audio_source: Literal['original', 'microphone', 'camera'] = 'original'
    audio_offset: float = 0
    vision: bool = False
    export_clips: bool = True
