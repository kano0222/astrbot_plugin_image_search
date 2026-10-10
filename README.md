# astrbot_plugin_image_search

An AstrBot plugin for finding the origin of images, illustrations, and anime
screenshots.

## Status

The first runnable AstrBot vertical slice is implemented:

- `/搜图`: SauceNAO-first search with optional low-similarity follow-up and backup search.
- `/搜图 google` (or saucenao/ascii2d): only the selected enabled source provider;
  no fallback or anime follow-up. Multiple channels can be separated by spaces
  or commas. Invalid/disabled channels are reported and ignored; valid selections
  still run. No search runs when all selected channels are unavailable.
- `/搜番`: trace.moe anime title, episode, and timestamp search.
- Automatic anime follow-up: a reliable SauceNAO anime-database match triggers
  trace.moe in the same `/搜图` request.
- Directly attached and quoted HTTP(S) images are supported.
- Commands without an image wait for a later image from the same sender and
  conversation.
- Local/base64 images use AstrBot's configured callback file service.
- SerpAPI keys use round-robin selection and quota/authentication failover.
- The unified `search_image(mode=auto/source/anime, channels="")` LLM tool reuses the same
  orchestration and sends the complete grouped result itself.

Ascii2d color and BOVW searches are implemented with partial failure isolation.
QQ merged-forward result nodes and thumbnail delivery are implemented.
SoutuBot is not implemented.

Ascii2d uses curl_cffi for browser TLS impersonation. Optional session and
Cloudflare cookies can be configured; successful access still depends on the
website accepting the session. Install requirements.txt before loading the plugin.

## 搜索失败与超时

Google Lens 多 Key 认证/额度切换共用一次搜索的超时预算，不按 Key 数叠加。
超时和服务端错误不额外轮换 Key，取消会正常向上传递。
SauceNAO 与 Lens 的超时、认证、额度、限频、HTTP 错误有独立的安全诊断，
不把 API Key、图片 URL 或上游原始错误正文放进用户提示或插件日志。
非有限超时配置回退默认值，避免无限等待。
Lens 搜索类型仅提供 all、visual_matches、exact_matches；
[SerpAPI 已停止支持 products](https://serpapi.com/google-lens-api)。
旧配置若手动填写 products，应改为有效类型，不静默改变搜索意图。

## Google Lens safety filtering

safe_search supports default (omit safe and use server defaults), off (send
safe=off explicitly), and active (send safe=active). The initial value is off.
This request setting does not guarantee a particular result or service availability.

## Channel aliases

Configure aliases inside each source provider panel as a list of names.
Defaults: Google Lens has g and 谷歌; SauceNAO has s; Ascii2d has a.
For example, add 酱 to SauceNAO aliases and use 搜图 酱.
Aliases are case-insensitive, cannot contain command separators, and cannot
replace builtin names. Invalid/conflicting entries are ignored with a warning.
Aliases do not enable disabled providers. Use 搜图 谷歌 to select a channel.

## LLM tool contract

search_image describes only registered, enabled services and constrains its mode
schema accordingly. Source services allow auto/source; anime services allow anime.
Omitting mode uses auto when source search is enabled, otherwise anime.
Unavailable modes are rejected before resolving or publishing an image.
Descriptions and schemas are copied per request, leaving shared tools unchanged.
用户明确指定来源渠道时，模型须填写 channels 字符串，例如 google 或 g s；支持与命令相同的别名、大小写和分隔符。只搜索所选可用渠道，不追加备用渠道或自动搜番。混合有效/无效渠道时提示并继续有效渠道；全部不可用时不解析或发布图片、不搜索。channels 仅适用于 auto/source；未指定渠道时模型应填写空字符串，使用默认策略。对当前消息整句明确选择渠道的请求（如“搜一下谷歌”“搜一下谷歌和 SauceNAO”），工具会绑定准确渠道，避免模型漏填或填错后执行默认搜索；不读取引用正文、不匹配否定或混合意图、不做拼写模糊匹配。该绑定不改变自然语言直达范围。工具续答依据实际调用及返回结果的渠道，不能将结果归因于未命中的渠道。
Enabled services can still fail (for example HTTP 403); the tool does not claim
that all enabled services are called in every conditional search.

真正调用 LLM 搜图工具时，插件先发送完整结果或无结果说明，再让当前会话模型以既有人格语气补一句已使用搜图工具的反馈，不重复结果、链接或错误详情。复用当前模型会话的后续回复，会产生一次工具完成后的模型续答；不另行调用人格生成模型。命令与简单自然语言直达不走这条续答路径。单句和人格语气由模型遵循指令，不能保证每次严格一致。

## Search intents

- `auto`: run general source search and add anime follow-up search only when a
  provider returns a structured anime signal.
- `source`: search for image, illustration, author, and page sources only.
- `anime`: search specifically for anime title, episode, and timestamp matches.

Providers return structured results. They do not format or send platform
messages. Command handlers and LLM tools share the same search service.

## Provider plan

- Source search prefers SauceNAO. Low-similarity outcomes use only the selected
  follow-up service (default off). Empty or failed requests retain Ascii2d/Lens
  backup search. Disabled/unconfigured services are skipped. Explicit channel
  requests search only selected services and never add automatic follow-up.
- SauceNAO returns one preferred candidate: Pixiv receives a 1.03 score preference;
  nearby Pixiv candidates (less than 5 percentage points apart) prefer the lower
  artwork ID. This is an earlier-post heuristic, not proof of original authorship.
- Ascii2d returns the first valid result from each mode. Lens retains its own
  ranking and configured result limit; scores are not mixed across providers.
- Anime search: trace.moe, with SauceNAO anime matches used as follow-up signals.
- Later evaluation: SoutuBot.

OCR, image-generation features, general bot utilities, and standalone OneBot
administration are intentionally out of scope.

## 结果归并

命令与 LLM 工具共用展示层。相同来源链接只展示一条，保留所有命中渠道；
保留来源页和不同的转载链接，SauceNAO 按下述顺序展示。
归并不改变搜索计划或原始结果。
仅统一主机大小写、默认端口、已知追踪参数及现代 Pixiv 作品链接形式；
仅当转载链接对应唯一明确来源时，将单独的转载页结果并入该来源；
不同明确来源即使共用转载链接也不合并。不按标题或相似度推断同一作品，
不混算渠道分数。
其他查询参数、通用片段、路径大小写和尾部斜杠保持区分。

SauceNAO 使用 渠道、标题/作者、缩略图及链接布局：渠道与相似度、`「标题」/「作者」`、缩略图、
结果链接、可选 `Author:` 主页和来源链接。省略缺失标题及数据库索引名；
Google Lens 同样按渠道、标题、缩略图和链接展示，省略重复站点名；
未提供的作者、相似度不推测或补造。归并命中仍保留全部渠道及各自相似度。
不添加“图片来源”标题。

## 结果发送

“结果展示”面板提供合并推送触发条数（默认 2，范围 0–10）、缩略图开关及图片数量上限（默认 6）。
触发条数按归并后的来源及动画结果计算，不包括提示或错误：0 不使用合并转发，1 始终合并；2–10 超过设定条数才合并。
例如默认 2 时，两条结果普通发送，三条结果合并转发。
QQ/aiocqhttp 达到触发条件时使用原生合并转发，每条归并结果一个节点；命令与 LLM 工具共用。
其他平台使用普通图文消息。无结果时保留原文字提示。
图片仅使用渠道提供的缩略图，不下载来源原图；动画成人结果不附预览图。
单张下载最多 5 秒、3 MiB，并发最多 3，总等待最多 10 秒；
仅允许公网 HTTP(S) 地址与 80/443 端口，重定向逐次检查。
图片失败不丢文字；带图转发失败尝试文字转发，再失败回退普通文字。
缩略图保存在本次请求的内存中，不产生持久图片文件或磁盘缓存。

## Ascii2d 使用限制

Ascii2d 仅使用 HTTP 请求；插件不安装浏览器、虚拟显示或操作系统组件。
网站仍可能返回 403/Cloudflare 验证拦截，填写 Cookie 不保证可用。
默认搜图会保留其他渠道的结果并按既有规则继续搜索；显式指定 Ascii2d 时不自动调用未指定渠道。

## 批量回归验证

使用 AstrBot 自带的 Python 环境运行，不为测试向宿主系统安装额外依赖：
```sh
docker exec -w /AstrBot astrbot python /AstrBot/data/plugins/astrbot_plugin_image_search/checks/run_checks.py
```
统一执行基础测试、AstrBot 接口测试和结果发送测试；任一失败返回非零退出码。
QQ 群聊/私聊消息经过实际适配器序列化，但发送动作使用模拟对象，不向 QQ 发消息。
自动测试验证逻辑与协议，最终客户端外观仍需一轮人工验收。

### 转载页来源补全

默认启用“结果展示 → 补全转载页的原始出处”。对没有来源 URL 的 Danbooru、Gelbooru、Konachan、Yande.re 作品页，读取明确的来源字段；成功后展示原出处，并使用原有来源归并规则。不会覆盖已有来源，也不会根据标题或任意页面链接猜测出处。

每次最多解析 6 个不同作品页，同一页只请求一次，额外等待最多 6 秒。403、超时、无来源字段或字段冲突时保留原转载页，不计作搜索渠道失败。关闭此项可取消额外页面请求。仅支持上述站点的已知字段，不保证每个作品都能找到原出处。

只请求作品页 HTML（单页最多 1 MiB），不下载来源原图、不携带网站 Cookie、不递归追踪来源；沿用网络代理配置，拒绝非公网地址及跨作品/跨站跳转。无需安装额外依赖或浏览器。

### 简单自然语言直达搜图

默认启用“命令交互 → 简单自然语言直达搜图”。对已通过会话唤醒和群聊门控、进入模型请求流程的消息，在主对话模型调用前接管整句明确的搜图请求；不经过模型判断工具调用。

支持“帮我找下出处”“找来源”“找原图”“请帮我搜图”等，可在句尾指定渠道，例如“帮我找下出处 g s 123”。渠道别名、无效数字项提示及仅搜索所选渠道的行为与搜图命令一致。无图明确请求复用同一发送者的图片等待；“搜一下”“搜下”等省略对象的请求，只有可解析图片时才接管。

只读取当前消息的文本，不把引用消息正文当作请求；图片解析仍支持引用图片，且当前附图优先。“不要搜图”“你会搜图吗”“帮我找出处并分析画风”等不接管，保留原处理流程。复杂请求可继续使用 LLM 工具。

该开关独立于 LLM 工具开关；关闭后恢复原自然语言处理，不影响搜图命令。它不会绕过群聊门控或自动唤醒本来不回复的会话，也不修改或清空模型图片上下文。

### 低相似度候选与补搜

SauceNAO 的“最低相似度百分比”（默认 60）统一控制展示与补搜判断：低于门槛的候选不展示，也不触发自动搜番；达到门槛才展示。

“SauceNAO 相似度过低时自动使用”默认 off，另可选 Ascii2d 或 google。低于最低相似度时，仅补搜所选已启用渠道一次，失败或无结果也不追加其他渠道；未启用时提示检查配置。不发送低相似度候选或“仅供参考”提示。无可展示结果时，分别说明候选低于门槛未展示、渠道未找到匹配或接口失败；补搜成功时不附加低相似度说明。

明确指定渠道的命令不追加补搜；默认搜图、简单自然语言直达及 LLM 的 auto/source 模式共享上述策略。完全无候选、接口失败或 SauceNAO 未启用时，仍使用既有备用搜索策略；这与低相似度补搜开关不同。自定义渠道顺序配置已移除。

### 搜索与发送日志

搜索日志记录关联 ID、计划候选与实际调用渠道、结果数量、失败渠道及耗时。结果直接通过 `event.send()` 发送图文或合并转发时，Core 不一定打印 `Prepare to send`；插件记录发送开始、类型、节点数/图片数和直接发送成功。普通文字交给管线时只记录交付，不声称已成功发出。日志不包含图片 URL、标题、API Key 或上游错误原文。
