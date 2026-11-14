# -*- coding: utf-8 -*-
"""DeepSeek 语音(ASR/TTS)插件

能力：
1) 注册两种 Provider，供 AstrBot 的“语音设置”选用（自动语音链路用）：
     - deepseek_tts_api : POST {base}/v1/tools/tts  语音合成
     - deepseek_asr_api : POST {base}/v1/tools/asr  语音识别
2) 提供调试指令：
     /tts  <文本>      合成并发送语音（别名：语音 / dstts）
     /asr              发送或引用一条语音，转文字（别名：识音 / 听写）
     /voice [音色]     查看配置或切换音色（别名：音色 / voice）

配置在「插件管理 → DeepSeek 语音 → 配置」（_conf_schema.json）。
原因：AstrBot 的 Provider 表单字段由核心 schema 按“已知类型”条件渲染，
第三方新增类型渲染不出字段，因此参数放在插件自己的配置页。
"""

from __future__ import annotations

import io
import json
import os
import wave

import httpx

from astrbot import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Record
from astrbot.api.star import Context, Star
from astrbot.core.config import AstrBotConfig
from astrbot.core.provider.entities import ProviderType
from astrbot.core.provider.provider import STTProvider, TTSProvider
from astrbot.core.provider.register import provider_cls_map, register_provider_adapter
from astrbot.core.utils.astrbot_path import (
    get_astrbot_config_path,
    get_astrbot_temp_path,
)
from astrbot.core.utils.datetime_utils import generate_timestamp_id
from astrbot.core.utils.media_utils import MediaResolver

DEEPSEEK_BASE = "https://api.deepseek.com"
PLUGIN_DIR_NAME = "astrbot_plugin_deepseek_audio"

_VALID_VOICES = ("stella", "tide")

# 运行时持有的插件配置对象（由 Star.__init__ 注入）
_RUNTIME: dict = {"config": None}


# ------------------------- 通用工具 -------------------------
def _endpoint(base: str, path: str) -> str:
    """base 归一化后拼出 /v1{path}；base 带不带 /v1 都兼容。"""
    b = (base or DEEPSEEK_BASE).strip().rstrip("/")
    if b.endswith("/v1"):
        b = b[:-3].rstrip("/")
    return f"{b}/v1{path}"


def _auth_headers(api_key: str) -> dict:
    key = (api_key or "").strip()
    return {"Authorization": f"Bearer {key}"} if key else {}


def _map_voice(voice: str) -> str:
    v = (voice or "").strip().lower()
    if v in _VALID_VOICES:
        return v
    if v in ("onyx", "echo", "male", "man", "男", "男声"):
        return "tide"
    return "stella"


def _parse_timeout(value, default: float = 120.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _silent_wav(seconds: float = 0.3, rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


def _mask_key(key: str) -> str:
    key = key or ""
    if not key:
        return "（未配置）"
    if len(key) <= 12:
        return "****"
    return f"{key[:5]}{'*' * (len(key) - 9)}{key[-4:]}"


# ------------------------- 配置读取 -------------------------
def _cfg_file_path() -> str:
    return os.path.join(get_astrbot_config_path(), f"{PLUGIN_DIR_NAME}_config.json")


def _read_cfg_file() -> dict:
    path = _cfg_file_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[DeepSeek Audio] 读取插件配置失败: {e}")
        return {}


def get_cfg() -> dict:
    """合并默认值 + 内存配置 + 磁盘配置（磁盘优先，保证改完即生效）。"""
    cfg = {
        "api_key": "",
        "api_base": DEEPSEEK_BASE,
        "voice": "stella",
        "timeout": 120,
        "proxy": "",
    }
    conf = _RUNTIME.get("config")
    if conf is not None:
        try:
            for k in cfg:
                if conf.get(k) not in (None, ""):
                    cfg[k] = conf[k]
        except Exception:  # noqa: BLE001
            pass
    for k, v in _read_cfg_file().items():
        if k in cfg and v not in (None, ""):
            cfg[k] = v
    return cfg


async def _set_cfg(key: str, value) -> None:
    conf = _RUNTIME.get("config")
    if conf is not None:
        try:
            conf[key] = value
            await conf.save_config_async()
            return
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[DeepSeek Audio] 保存插件配置失败，回退写文件: {e}")
    data = _read_cfg_file()
    data[key] = value
    try:
        with open(_cfg_file_path(), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:  # noqa: BLE001
        logger.error(f"[DeepSeek Audio] 写入配置文件失败: {e}")


# ------------------------- 真正的调用逻辑 -------------------------
def _httpx_kwargs(cfg: dict) -> dict:
    kwargs = {"timeout": _parse_timeout(cfg.get("timeout", 120))}
    if (cfg.get("proxy") or "").strip():
        kwargs["proxy"] = cfg["proxy"].strip()
    return kwargs


async def tts_to_bytes(cfg: dict, text: str) -> bytes:
    if not (text or "").strip():
        raise ValueError("待合成文本为空")
    payload = {
        "text": text,
        "voice_id": _map_voice(cfg.get("voice")),
        "format": "wav",
        "filter_markdown": True,
    }
    async with httpx.AsyncClient(**_httpx_kwargs(cfg)) as client:
        resp = await client.post(
            _endpoint(cfg.get("api_base"), "/tools/tts"),
            json=payload,
            headers=_auth_headers(cfg.get("api_key")),
        )
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code} {resp.text[:400]}")
    return resp.content


async def asr_from_audio(cfg: dict, audio: str) -> str:
    async with MediaResolver(
        audio, media_type="audio", default_suffix=".wav"
    ).as_path(target_format="wav") as path:
        data = path.read_bytes()
    files = {"file": ("audio.wav", data, "audio/wav")}
    async with httpx.AsyncClient(**_httpx_kwargs(cfg)) as client:
        resp = await client.post(
            _endpoint(cfg.get("api_base"), "/tools/asr"),
            files=files,
            headers=_auth_headers(cfg.get("api_key")),
        )
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code} {resp.text[:400]}")
    try:
        return resp.json().get("text", "") or ""
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"返回解析失败: {e}") from e


# ------------------------- Provider 实现 -------------------------
class ProviderDeepSeekTTS(TTSProvider):
    def __init__(self, provider_config: dict, provider_settings: dict) -> None:
        super().__init__(provider_config, provider_settings)
        self.set_model(provider_config.get("model", "") or "deepseek-tts")

    async def get_audio(self, text: str) -> str:
        cfg = get_cfg()
        if not cfg.get("api_key"):
            raise RuntimeError(
                "未配置 DeepSeek API Key，请在 插件管理 → DeepSeek 语音 → 配置 中填写"
            )
        audio = await tts_to_bytes(cfg, text)
        path = os.path.join(
            get_astrbot_temp_path(), f"deepseek_tts_{generate_timestamp_id()}.wav"
        )
        with open(path, "wb") as f:
            f.write(audio)
        return path

    async def test(self) -> None:
        try:
            await self.get_audio("你好，这里是语音合成自检。")
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"DeepSeek TTS 自检失败: {e}") from e


class ProviderDeepSeekASR(STTProvider):
    def __init__(self, provider_config: dict, provider_settings: dict) -> None:
        super().__init__(provider_config, provider_settings)
        self.set_model(provider_config.get("model", "") or "deepseek-asr")

    async def get_text(self, audio_url: str) -> str:
        cfg = get_cfg()
        if not cfg.get("api_key"):
            raise RuntimeError(
                "未配置 DeepSeek API Key，请在 插件管理 → DeepSeek 语音 → 配置 中填写"
            )
        return await asr_from_audio(cfg, audio_url)

    async def test(self) -> None:
        # 重写默认 test()，避免读取不存在的 samples/stt_health_check.wav
        tmp = os.path.join(
            get_astrbot_temp_path(),
            f"deepseek_asr_selftest_{generate_timestamp_id()}.wav",
        )
        with open(tmp, "wb") as f:
            f.write(_silent_wav())
        try:
            await self.get_text(tmp)
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass


# 注册 Provider 类型（重载安全）
if "deepseek_tts_api" not in provider_cls_map:
    register_provider_adapter(
        "deepseek_tts_api",
        "DeepSeek TTS API",
        provider_type=ProviderType.TEXT_TO_SPEECH,
        default_config_tmpl={
            "id": "deepseek_tts",
            "type": "deepseek_tts_api",
            "provider": "deepseek",
            "provider_type": "text_to_speech",
            "enable": True,
            "api_key": "",
            "api_base": DEEPSEEK_BASE,
            "model": "deepseek-tts",
            "timeout": 120,
            "proxy": "",
        },
    )(ProviderDeepSeekTTS)

if "deepseek_asr_api" not in provider_cls_map:
    register_provider_adapter(
        "deepseek_asr_api",
        "DeepSeek ASR API",
        provider_type=ProviderType.SPEECH_TO_TEXT,
        default_config_tmpl={
            "id": "deepseek_asr",
            "type": "deepseek_asr_api",
            "provider": "deepseek",
            "provider_type": "speech_to_text",
            "enable": True,
            "api_key": "",
            "api_base": DEEPSEEK_BASE,
            "model": "deepseek-asr",
            "timeout": 120,
            "proxy": "",
        },
    )(ProviderDeepSeekASR)


# ------------------------- 指令 & 插件主体 -------------------------
_NAMES_TTS = ("tts", "语音", "dstts", "ds_tts")
_NAMES_ASR = ("asr", "识音", "听写", "dsasr", "ds_asr")
_NAMES_VOICE = ("voice", "音色", "dsvoice", "ds_voice")


def _arg_after(message_str: str, names: tuple[str, ...]) -> str:
    raw = (message_str or "").strip()
    parts = raw.split()
    if parts and parts[0].lstrip("/").lower() in names:
        parts = parts[1:]
    return " ".join(parts).strip()


def _find_audio_url(event: AstrMessageEvent) -> str | None:
    try:
        comps = event.get_messages()
    except Exception:  # noqa: BLE001
        comps = getattr(getattr(event, "message_obj", None), "message", []) or []
    for comp in comps:
        if isinstance(comp, Record):
            return comp.url or comp.file or comp.path
        chain = getattr(comp, "chain", None)
        if chain:
            for sub in chain:
                if isinstance(sub, Record):
                    return sub.url or sub.file or sub.path
    return None


class DeepSeekAudioPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context, config)
        _RUNTIME["config"] = config
        logger.info(
            "[DeepSeek Audio] 已注册 Provider: deepseek_tts_api / deepseek_asr_api；"
            "指令: /tts /asr /voice"
        )

    @filter.command("tts", alias={"语音", "dstts", "ds_tts"})
    async def cmd_tts(self, event: AstrMessageEvent):
        """合成语音：/tts 要说的文本"""
        text = _arg_after(event.message_str, _NAMES_TTS)
        if not text:
            yield event.plain_result("用法：/tts 要合成的文本")
            return
        cfg = get_cfg()
        if not cfg.get("api_key"):
            yield event.plain_result(
                "还没填 API Key：插件管理 → DeepSeek 语音 → 配置 → api_key"
            )
            return
        try:
            audio = await tts_to_bytes(cfg, text)
        except Exception as e:  # noqa: BLE001
            yield event.plain_result(f"合成失败：{e}")
            return
        path = os.path.join(
            get_astrbot_temp_path(), f"deepseek_tts_{generate_timestamp_id()}.wav"
        )
        with open(path, "wb") as f:
            f.write(audio)
        yield event.chain_result([Record.fromFileSystem(path)])

    @filter.command("asr", alias={"识音", "听写", "dsasr", "ds_asr"})
    async def cmd_asr(self, event: AstrMessageEvent):
        """识别语音：发送或引用一条语音后发 /asr"""
        audio = _find_audio_url(event)
        if not audio:
            yield event.plain_result("没找到语音：发一条语音或引用一条语音后再发 /asr")
            return
        cfg = get_cfg()
        if not cfg.get("api_key"):
            yield event.plain_result(
                "还没填 API Key：插件管理 → DeepSeek 语音 → 配置 → api_key"
            )
            return
        try:
            text = await asr_from_audio(cfg, audio)
        except Exception as e:  # noqa: BLE001
            yield event.plain_result(f"识别失败：{e}")
            return
        yield event.plain_result(f"识别结果：{text or '（空）'}")

    @filter.command("voice", alias={"音色", "dsvoice", "ds_voice"})
    async def cmd_voice(self, event: AstrMessageEvent):
        """查看配置/切换音色：/voice 或 /voice tide"""
        arg = _arg_after(event.message_str, _NAMES_VOICE).lower()
        if arg in _VALID_VOICES:
            await _set_cfg("voice", arg)
            yield event.plain_result(f"已切换音色：{arg}")
            return
        cfg = get_cfg()
        yield event.plain_result(
            "DeepSeek 语音\n"
            f"音色：{cfg.get('voice')}（stella=女 / tide=男）\n"
            f"Key：{_mask_key(cfg.get('api_key'))}\n"
            f"Base：{cfg.get('api_base')}\n"
            f"超时：{cfg.get('timeout')}s｜代理：{cfg.get('proxy') or '无'}\n"
            "切换音色：/voice stella 或 /voice tide"
        )
