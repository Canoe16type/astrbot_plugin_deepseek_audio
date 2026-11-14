# astrbot_plugin_deepseek_audio

> 给 AstrBot 接上 **DeepSeek 官方语音能力**：语音合成（TTS）+ 语音识别（ASR）。
> 零第三方依赖，不修改 AstrBot 本体。

[![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D4.24.0-blue)](https://astrbot.app)
[![License](https://img.shields.io/badge/license-MIT-green)](./LICENSE)

> 🤖 **AI 辅助说明**：本项目代码与文档由 AI 辅助生成，并经真人审核与实测后发布。

---

## 为什么需要它

AstrBot 内置的 `openai_tts_api` / `openai_whisper_api` 请求的是 OpenAI 标准路径 `/v1/audio/speech`、`/v1/audio/transcriptions`，
而 DeepSeek 的语音接口是 `/v1/tools/tts`、`/v1/tools/asr`，**参数体也不同**（`voice_id`、需要 `format` 等）。
把 base_url 指过去只会拿到 404，所以本插件单独注册了两个 Provider 类型做适配。

顺带修掉一个老坑：内置 STT 的 `test()` 会去读 `samples/stt_health_check.wav`，该文件在多数安装里并不存在，
导致「测试」必失败。本插件用生成的静音 WAV 做自检。

## 特性

- ✅ 两个 Provider：`deepseek_tts_api`（TEXT_TO_SPEECH）、`deepseek_asr_api`（SPEECH_TO_TEXT）
- ✅ 可直接在「语音设置」里选用，走 AstrBot 原生语音链路（自动回复语音、语音转文字）
- ✅ 中文/英文音色切换：`stella`（女）/ `tide`（男），也自动把 `onyx`、`male` 等映射过来
- ✅ 配置改完即生效，无需重启（Provider 每次调用重新读配置）
- ✅ 音频格式自动转换（`MediaResolver` 统一转 16bit WAV），兼容 QQ 语音的 silk 之外的常见格式
- ✅ 私有网络部署友好：支持自定义 `api_base` 与 `proxy`
- ✅ 零第三方依赖，只用 AstrBot 自带的 `httpx`

## 安装

1. 下载本仓库，放进 `data/plugins/astrbot_plugin_deepseek_audio/`（或用 WebUI 的「安装插件 → 从 URL 安装」填仓库地址）；
2. 重启 AstrBot；
3. 打开 **插件管理 → DeepSeek 语音 → 配置**，填写 `api_key`。

> ⚠️ **插件白名单**：如果你在「配置 → 插件」里维护了启用列表（`plugin_set`），记得把本插件勾上，
> 否则插件会加载成功但**指令不响应**（事件处理器被白名单过滤）。

## 配置项

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `api_key` | string | 空 | DeepSeek 的 `sk-` 密钥，TTS / ASR 共用一个 |
| `api_base` | string | `https://api.deepseek.com` | 带不带 `/v1` 都可以，插件会自动归一化 |
| `voice` | string | `stella` | `stella` 女声 / `tide` 男声 |
| `timeout` | int | `120` | 单次请求超时（秒） |
| `proxy` | string | 空 | 形如 `http://127.0.0.1:7890`，不需要就留空 |

## 指令

| 指令 | 别名 | 作用 |
|---|---|---|
| `/tts <文本>` | `/语音`、`/dstts` | 合成语音并发送 |
| `/asr` | `/识音`、`/听写`、`/dsasr` | 发送或引用一条语音后转文字 |
| `/voice` | `/音色`、`/dsvoice` | 查看配置（音色 / Key / Base / 超时 / 代理） |
| `/voice tide` | — | 切换音色 |

> 中文指令**必须留空格**：`/语音 你好` ✅　`/语音你好` ❌（AstrBot 按「指令名 + 空格」前缀匹配）。
> 带文本参数建议直接用 `/tts`。

## 接入 AstrBot 自动语音链路

如果你想「机器人回复时自动发语音」或「用户发语音自动转文字」：

1. **服务提供商 → 新增** → 类型选 `DeepSeek TTS API` 或 `DeepSeek ASR API`；
   > 表单里**没有字段是正常的**：AstrBot 核心只会给内置已知类型渲染表单，第三方新增类型的字段渲染不出来，直接保存即可。
2. **语音设置** → 把 TTS / STT 提供商分别指向上面两个，打开开关。

所有真实参数都来自本插件的配置页，Provider 条目只是个「开关 + 占位」。

## DeepSeek 侧限制

**TTS**
- 音色仅 `stella` / `tide`；不支持自定义语速
- 单次 ≤ 10000 计费字符 / 64 KiB，音频时长 ≤ 30 分钟

**ASR**
- 仅支持 16bit PCM WAV 或 Ogg Opus，采样率 8000–48000 Hz
- 单文件 ≤ 8 MiB、时长 ≤ 120 秒；不支持流式

## 目录结构

```
astrbot_plugin_deepseek_audio/
├── main.py            # 插件入口：Provider 注册 + 指令
├── metadata.yaml      # 插件元数据（市场规范字段）
├── _conf_schema.json  # WebUI 配置表单
├── README.md
├── LICENSE
└── requirements.txt   # 无额外依赖，仅声明说明
```

## 常见问题

**Q：指令没反应，但插件显示已加载？**
A：① 检查上面的「插件白名单」是否勾选；② 中文指令是否漏了空格。

**Q：服务提供商表单是空的，填不了 Key？**
A：设计如此。Key 在插件配置页填，Provider 条目不用填任何东西。

**Q：`/asr` 提示「没找到语音」？**
A：要先发一条语音，或**引用**一条语音再发 `/asr`（QQ 官方机器人下，引用语音消息可正常取到 URL）。

## 许可证

[MIT](./LICENSE)
