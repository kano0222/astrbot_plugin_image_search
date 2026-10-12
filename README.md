# astrbot_plugin_image_search

<a href="https://github.com/kano0222/astrbot_plugin_image_search/releases"><img alt="Version" src="https://img.shields.io/badge/version-1.0.0-blue?style=for-the-badge" /></a>
<a href="https://github.com/Soulter/AstrBot"><img alt="AstrBot" src="https://img.shields.io/badge/AstrBot-plugin-green?style=for-the-badge" /></a>
<a href="https://github.com/kano0222/astrbot_plugin_image_search"><img alt="Last Commit" src="https://img.shields.io/github/last-commit/kano0222/astrbot_plugin_image_search?style=for-the-badge" /></a>

<img src="https://count.getloli.com/@kano_astrbot_plugin_image_search?theme=booru-jaypee" alt="Moe Counter" />

用于查找图片出处的 AstrBot 插件。支持 SauceNAO、Google Lens、Ascii2d 和 trace.moe，可以通过命令和简单自然语言搜图，也支持 LLM 工具调用。

## 目录

- [快速开始](#快速开始)
- [使用](#使用)
- [搜索渠道与策略](#搜索渠道与策略)
- [搜索与展示设置](#搜索与展示设置)
- [网络与数据说明](#网络与数据说明)
- [常见问题](#常见问题)
- [鸣谢](#鸣谢)
- [进阶配置与排错](ADVANCED.md)

## 快速开始

需要 AstrBot **4.25 及以上的 4.x 版本**。通过 WebUI 安装时，AstrBot 会检查插件依赖并尝试安装缺失的包；如果依赖安装失败，按[手动安装说明](ADVANCED.md#手动安装与依赖)处理后再重载插件。

1. 在 AstrBot WebUI 插件页安装本插件：
   - 下载本仓库 zip 后选择「导入压缩包」
   - 或粘贴仓库地址：`https://github.com/kano0222/astrbot_plugin_image_search`
2. 在插件配置的“搜索服务”中启用需要的渠道，填写对应凭证，再加载或重载插件。
3. 在同一条消息中发送 `/搜图` 和图片，或引用一条带图消息发送 `/搜图`。发送 `/搜图帮助` 可确认当前可用渠道。

| 渠道 | 需要填写 |
| --- | --- |
| SauceNAO | [API Key](https://saucenao.com/user.php) |
| Google Lens | [SerpAPI API Key](https://serpapi.com/manage-api-key)，支持多个 Key |
| Ascii2d | 可选的 `_session_id`、`cf_clearance` Cookie |
| trace.moe | 可选 Token，留空使用匿名额度 |

## 使用

以下以 `/` 为命令前缀，实际前缀遵循 AstrBot 设置。发送 `/搜图帮助` 可查看当前配置下可用的渠道、别名和用法。

| 命令                           | 用途                                             |
| ------------------------------ | ------------------------------------------------ |
| `/搜图`                        | 按默认策略查找图片出处                           |
| `/搜图 g`                      | 只用 Google Lens                                 |
| `/搜图 g s`                    | 同时使用 Google Lens 和 SauceNAO，也可用逗号分隔 |
| `/搜番`                        | 查询动画名称、集数和时间点                       |
| `/搜图 -purge`、`/搜番 -purge` | 跳过缓存，重新搜索                               |
| `/搜图模式`                    | 开启连续搜图，可附渠道参数，如 `/搜图模式 g`     |
| `/退出搜图模式`                | 退出连续搜图                                     |

在命令消息中附图，或引用一条带图消息即可搜索。也可以先发命令，再在默认 **60 秒**内补图；等待只接收同一聊天中同一发送者的图片。当前消息和引用消息都有图片时，只搜索当前附图。

### 自然语言搜图

默认开启“自然语言直接搜图”。附图发送 `帮我找下出处`，或引用图片发送 `找原图 谷歌`，会直接搜索，不调用模型。`请帮我搜图` 会等待补图；`搜一下`、`搜下` 只有在找到图片时才直接处理。

请求末尾可加渠道，例如 `帮我找下出处 g s`。`怎么搜图`、`怎么搜番` 等使用问句返回帮助，不搜索、不计次。自然语言请求仍遵循 AstrBot 的会话唤醒设置。

`不要搜图`、`你会搜图吗`、`找出处并分析画风` 等交给原有对话处理。关闭自然语言直接搜图不影响命令或 LLM 工具。

### 连续搜图

发送 `/搜图模式` 后，只发图片即可连续搜索；模式仅对当前聊天中的你生效。`/搜图模式 g` 切换到 Google Lens，再次发送 `/搜图模式` 恢复默认策略。

默认空闲 60 秒退出，发图或切换渠道会续期。可用 `/退出搜图模式` 或单独发送 `退出` 提前关闭，不取消已开始的搜索。重载或重启后模式关闭；带文字或引用的消息不由模式自动搜索。

### LLM 搜图工具

工具默认开启，模型可搜索当前附图或引用图片，插件发出结果后由当前模型简短回复。单独发图不会向模型提供搜图工具，连续搜图和补图等待仍可处理。

工具是否被调用取决于模型，需要直接执行时用搜图命令。渠道按用户消息正文确定，无法准确解析时提示改用命令；运行中要换渠道，请重新发送明确命令。LLM 工具和自然语言直接搜图可分别开关。

## 搜索渠道与策略

默认优先使用 **SauceNAO**；不可用、请求失败或没有候选时，再依次尝试 **Ascii2d、Google Lens** 中可用的服务，找到结果后停止。

如果 SauceNAO 找到了候选，但相似度低于配置门槛，候选不展示，默认结束搜索。需要继续查找时，可在“SauceNAO 相似度过低时自动使用”中选择 Ascii2d 或 Google Lens；只补搜所选可用渠道一次，即使没有结果也不继续追加渠道。

指定渠道只搜所选可用服务，不追加备用渠道、低相似度补搜或自动搜番。多个渠道并行执行；部分无效时提示并继续有效渠道，全部不可用时不搜索。默认别名是 Google Lens 的 `g`、`谷歌`，SauceNAO 的 `s`，Ascii2d 的 `a`。

默认搜图命中可靠的 SauceNAO 动画库结果时，会追加已启用的 trace.moe 查询；低相似度候选不会触发。`/搜番` 直接使用 trace.moe，同样按配置的相似度门槛筛选结果。

## 搜索与展示设置

插件配置可以调整批量处理、每日搜索量、缓存和结果展示：

- **批量图片**：在“命令交互”中调整每次处理的图片数，超出部分会跳过并提示，单张失败不影响后续图片。
- **每日搜索量**：在“命令交互”中设置每名用户的每日次数，便于控制服务消耗；设为 `0` 不限，管理员免限。同一平台连接下，每名用户的群聊、私聊及搜图、搜番共用次数，按 AstrBot 时区每日重置。
- **结果缓存**：在“搜索结果缓存”中调整开关和有效期。命中缓存会标记 `[缓存]`，可减少重复查询；需要新结果时使用 `/搜图 -purge` 或 `/搜番 -purge`，Google Lens 同时跳过 SerpAPI 缓存。
- **合并转发**：在“结果展示”中调整合并门槛，减少 QQ/aiocqhttp 的消息条数。同图两条及以上结果始终合并，门槛设为 `0` 也适用；其他平台按图发送普通消息。
- **缩略图与出处**：在“结果展示”中调整缩略图开关和数量，或开关转载页出处补全。出处补全失败时保留原链接。

缓存命中不计搜索次数；有候选但因相似度不足被隐藏仍计次。其他计次例外、缓存保存条件及合并门槛见[进阶文档](ADVANCED.md#缓存次数和展示设置)。

## 网络与数据说明

实际使用的第三方搜索服务需要读取图片，第三方处理和留存以其政策为准。本地或 Base64 等没有公网地址的图片需要 AstrBot 文件服务：在 **AstrBot 系统配置**中设置 `callback_api_base`，让外部服务能访问生成的链接。

不要公开 API Key、Token、Cookie 或带访问 Token 的下载链接，分享配置和日志前请检查内容。代理、文件服务和数据保留见[进阶文档](ADVANCED.md#网络排错与数据保留)。

## 常见问题

- **渠道不可用**：检查启用状态及凭据，用 `/搜图帮助` 确认可用渠道。
- **图片无法搜索**：检查附图或引用是否可解析，以及文件服务链接能否被外部访问。
- **Ascii2d 验证拦截**：Cookie 可能过期，填写后也不保证可用，可明确指定其他渠道。
- **响应慢或超时**：检查网络和服务额度。请求超时按单次服务请求计算，整批多图可能耗时更久。
- **模型没有调用工具**：确认工具开关，或直接用搜图命令。

## 鸣谢

本插件结合了 [cq-picsearcher-bot](https://github.com/Tsuk1ko/cq-picsearcher-bot) 和 [astrbot_plugin_imgexploration](https://github.com/iona-s/astrbot_plugin_imgexploration) 的搜图思路与交互方式。
