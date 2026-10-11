# astrbot_plugin_image_search

[简体中文](README.md) | English

An AstrBot plugin for finding image and illustration sources and identifying anime screenshots. This English edition is based on the original README; the main README is maintained in Chinese. Commands remain in Chinese in both editions.

## Features and commands

Install the dependencies in `requirements.txt` before loading the plugin, then configure the search providers you need. Command prefixes follow your AstrBot settings; examples below use `/`.

| Command | Purpose |
| --- | --- |
| `/搜图` | Search for an image source, starting with SauceNAO when available |
| `/搜图 g` | Search only Google Lens; separate multiple channels with spaces or commas |
| `/搜番` | Find an anime title, episode, and timestamp through trace.moe |
| `/搜图 -purge` | Bypass cached results and search again |
| `/搜图帮助` | Show currently available channels, aliases, and usage |
| `/搜图模式` | Start continuous image search; optional channel arguments are supported |
| `/退出搜图模式` | Leave continuous mode without cancelling an ongoing search |

Attach images, reply to an image, send multiple images, or send the command before sending an image. Direct attachments take priority over quoted images. Local/base64 images use AstrBot's callback file service, which must be configured correctly.

Explicit channel selection searches only the selected enabled providers, with no extra fallback or automatic anime follow-up. Invalid channels are reported and ignored when valid channels remain. If all selected channels are unavailable, no image is resolved or published and no search runs.

Implemented providers: SauceNAO, Google Lens via SerpAPI, Ascii2d, and trace.moe. QQ merged forwards and thumbnails are supported. SoutuBot is not implemented. Ascii2d uses `curl_cffi` for browser TLS impersonation; optional session and Cloudflare cookies do not guarantee access.

## Direct natural-language search

The **自然语言直接搜图** setting under command interaction is enabled by default. It handles explicit whole-message requests before the conversation model is called. It applies only after AstrBot's existing conversation wake-up and group-message checks; it does not wake otherwise inactive conversations.

| Message | Behavior |
| --- | --- |
| Image + `帮我找下出处` | Search directly without an LLM call |
| Reply to an image + `找原图 谷歌` | Search the quoted image through Google only |
| `请帮我搜图`, followed by an image | Wait for the same sender to provide an image in the same conversation |
| `搜一下` or `搜下` | Route directly only when an image can be resolved |
| `怎么搜图`, `如何搜图？`, `搜图怎么用`, `搜图帮助` | Return configuration-aware help without searching or consuming quota |
| `怎么搜番`, `如何搜番`, `搜番怎么用` | Return the same help; these questions do not start an anime search |
| `不要搜图`, `你会搜图吗`, or a mixed task such as `找出处并分析画风` | Leave the original processing flow unchanged |

Help questions accept the `请问` prefix and trailing punctuation. Source requests may include channel aliases at the end, for example `帮我找下出处 g s 123`; invalid numeric channel arguments use the same warning as commands. Quoted text is never treated as the current request, although quoted images remain usable. No fuzzy spelling matching is performed.

This setting is independent of the LLM tool switch. Disabling it leaves ordinary commands available and returns natural-language messages to their normal processing flow. It does not modify or clear the model's image context. Complex requests may still use the LLM search tool.

## Channel aliases

Configure aliases as a list in each source provider's panel. Defaults are `g` and `谷歌` for Google Lens, `s` for SauceNAO, and `a` for Ascii2d. `/搜图 谷歌` selects Google Lens.

Aliases are case-insensitive, must not contain command separators, and cannot override built-in names. Invalid or conflicting aliases are ignored with a warning. Adding an alias does not enable its provider.

## Search intents and provider strategy

- `auto`: search for image sources, adding anime search only after a structured anime signal.
- `source`: search for image, illustration, author, and page sources only.
- `anime`: search for anime titles, episodes, and timestamps.

Providers return structured results and never send platform messages. Commands and LLM tools share the same search service.

The default source plan prefers SauceNAO. A candidate below its minimum similarity is hidden and does not trigger automatic anime search. The default threshold is 60%. Low-similarity follow-up is disabled by default; it can be configured to use one enabled Ascii2d or Google provider. That follow-up runs once, with no additional provider appended if it fails or finds nothing. An unavailable configured follow-up produces a configuration notice.

Empty results, provider failure, or an unavailable SauceNAO provider use the existing Ascii2d/Google backup plan. This differs from low-similarity follow-up. Explicit channel selection never appends another provider or anime search. The configurable provider-order setting has been removed.

SauceNAO selects one preferred candidate. Pixiv gets a 1.03 score preference; Pixiv candidates less than five percentage points apart prefer the lower artwork ID. This is an earlier-post heuristic, not proof of authorship. Ascii2d keeps the first valid match from each color/BOVW search, isolating partial failures. Google retains its own ranking and configured result limit. Scores are not mixed across providers.

A reliable SauceNAO anime-database match can trigger trace.moe during default `/搜图`. It does not mean every image is classified as an anime screenshot. Low-confidence candidates are not sent with a separate “reference only” warning. When no result is displayed, messages distinguish threshold filtering, no matches, and provider failure. A successful follow-up does not add a redundant low-similarity notice.

OCR, image generation, general bot utilities, and standalone OneBot administration are outside this plugin's scope.

## Google Lens options

| Display name | Value | Behavior |
| --- | --- | --- |
| 默认搜索 (Default search) | `all` | Request general results; the plugin reads the visual-match list |
| 相似图片 (Similar images) | `visual_matches` | Look for visually similar images and pages |
| 完全匹配 (Exact matches) | `exact_matches` | Look for pages containing the same image |

[SerpAPI discontinued `products`](https://serpapi.com/google-lens-api). Manually configured obsolete values must be replaced with a supported type; the plugin does not silently change the search intent.

Safe search supports `default` (omit `safe`), `off` (`safe=off`), and `active` (`safe=active`). The initial value is `off`. This setting does not guarantee a particular result or service availability.

SerpAPI keys rotate, with another key tried for rejected authentication or quota. All attempts share a single request timeout budget. Timeouts and server errors do not trigger extra key rotation. Cancellation propagates normally. SauceNAO and Lens report safe timeout, authentication, quota, rate-limit, and HTTP failure diagnostics without exposing credentials, image URLs, or raw upstream errors. Non-finite timeout settings fall back to defaults.

## LLM search tool

`search_image(mode=auto/source/anime, channels="", refresh=false)` describes only registered, enabled providers. Source providers allow `auto` and `source`; anime providers allow `anime`. Omitting the mode uses `auto` when source search is enabled, otherwise `anime`. Unavailable modes are rejected before image resolution or publication. Tool descriptions and schemas are copied per request rather than mutating shared tools.

`channels` applies only to `auto` and `source`. For an explicit provider request the model must pass the selected channels, using the same aliases, case handling, and separators as commands. An empty string uses the default plan. Whole-message provider requests such as `搜一下谷歌` or `搜一下谷歌和 SauceNAO` bind the requested channels so omitted or incorrect model arguments cannot silently select the default plan. This does not expand the direct natural-language router. Negations, mixed intents, and quoted text are not bound.

The plugin sends complete results or a no-result explanation first. The existing conversation model then continues with a brief acknowledgment in its current persona, without repeating titles, links, or error details. This is a model continuation after tool use, not a separate persona-generation call. Commands and direct natural-language routing skip that continuation. Exact wording and persona adherence depend on the model. Feedback reports the actual providers and must not claim a source was found when none was returned.

## Result grouping and delivery

Commands and tools share presentation. Identical source links are grouped while retaining provider attribution and distinct source/mirror links. Grouping does not change search plans or original results. Normalization covers host case, default ports, known tracking parameters, and modern Pixiv artwork links. A standalone mirror is attached only when it has one unambiguous source. Different explicit sources sharing a mirror are kept separate. Titles and scores are not used to infer identity; other query parameters, generic fragments, path case, and trailing slashes remain distinct.

SauceNAO displays provider/similarity, title/author, thumbnail, result link, optional author homepage, and source links. Missing titles and database-index labels are omitted. Google uses provider, title, thumbnail, and links without repeating site names. Missing authors or scores are not invented. Merged results retain each provider and its independent score.

The merged-forward threshold defaults to 2 and accepts 0–10. It counts ordinary messages: all results and diagnostics for one input image form one message; an image-processing failure also occupies one message. Normally, 0 disables forwards, 1 always forwards, and 2–10 forwards at or above the threshold.

**Google exception:** two or more Google matches for one input image force a merged forward, even at threshold 0. One Google match follows the threshold. Cache hits follow the same rule. Each input image remains one forward node; results are not split into multiple forwards at ten matches.

Google results share a single heading, with numbered titles, thumbnails, and links separated by blank lines. A single match is not numbered. Cached Google results use `Google Lens[缓存]` once at the top. On QQ/aiocqhttp, each node corresponds to one ordinary message. Other platforms send one ordinary message per input image.

Only provider thumbnails are downloaded, never source originals. The default limit is six thumbnails **per input image**; extra results retain their text and links. Adult anime results omit previews. Downloads allow public HTTP(S) URLs on ports 80/443, checking each redirect. Limits are five seconds and 3 MiB per image, three concurrent downloads, and ten seconds total thumbnail waiting time. Thumbnails stay in request-local memory without persistent image files.

Image failures do not discard text. A failed image forward retries as a text forward, then falls back to ordinary text if forwarding still fails.

## Original-source enrichment and Ascii2d limitations

Original-source enrichment is enabled by default. For Danbooru, Gelbooru, Konachan, and Yande.re artwork pages without a source URL, the plugin reads explicit source fields. It never overwrites a known source or guesses from titles or arbitrary links. Successful enrichment uses the existing grouping rules.

Up to six distinct pages are inspected per search, once per page, with up to six seconds of extra waiting. HTTP 403, timeout, absent fields, or conflicting fields preserve the mirror page and do not count as provider failure. Disabling enrichment removes these requests. Source discovery is not guaranteed.

Only artwork-page HTML is fetched, up to 1 MiB per page, using the configured proxy. It does not download originals, send site cookies, or recursively follow links. Non-public addresses and cross-work/cross-site redirects are rejected. No browser or additional dependency is needed.

Ascii2d uses HTTP only: no browser, virtual display, or OS component is installed. Cloudflare/403 blocking can still occur despite cookies. Default search retains other provider results and follows its normal backup policy; explicitly selecting Ascii2d does not invoke unselected providers.

## Cache, refresh, quota, and batches

Commands, direct natural-language requests, and LLM tools share these rules:

- Valid local cache entries are used by default. `/搜图 -purge` and `/搜番 -purge` force a new search.
- `搜图 -purge g s`, `搜图 g -purge s`, and `搜图 g s -purge` are equivalent. Selected providers run concurrently; argument order does not determine priority.
- `-purge` applies to all images in that request and also bypasses SerpAPI's cache for Google. A new result satisfying the cache conditions replaces the old record. Failure or no reliable result leaves the old record intact but does not return it for that refresh request.
- The LLM tool uses `refresh=true` only for an explicit refresh request; its default is false. One call handles the batch. Model feedback includes image/cache counts and actual external providers.

Local caching is enabled by default for 172800 seconds (48 hours). It stores structured results only when there are matches, no failed providers, and no diagnostic notices. It does not store persona replies, sent messages, or image files. Cache hits show `[缓存]` without separate timestamps or refresh instructions. Current display settings are applied when rendering cached data.

Identity prefers a OneBot `file_unique`, valid MD5/file identifier, or recognized QQ image-path MD5. Unknown formats fall back to the full URL. Modes, channels, and relevant search settings separate cache entries. Ordinary filenames and unknown `fileid` formats are not trusted; arbitrary URL signatures are not removed. No input download is performed solely to hash images. A changed URL without a stable identifier can miss the cache, and recompressed/cropped images are not recognized by perceptual similarity.

The daily limit defaults to 30; 0 means unlimited. AstrBot administrators are exempt. Within one platform connection, each user's group and private searches share the count. One externally searched input image reserves one use, regardless of multiple channels or follow-ups. Cache hits, missing images, and wholly unavailable channels do not consume quota. Filtered low-confidence candidates count; all-provider failure or no candidates refunds the reservation. Source and anime search share the quota.

Quota resets on the natural day in AstrBot's `timezone`, falling back to the system timezone when invalid or empty. Cache and quota are stored together in `data/plugin_data/astrbot_plugin_image_search/search_state.sqlite3`, surviving plugin reload and bot restart. Uninstall cleanup follows AstrBot's data/config deletion options. Storage is capped at 1000 cache records without extra image or log directories.

Batch size defaults to five and is configurable as a positive integer. Current-message images take priority; quoted images are used only when there are no direct images. Images are searched sequentially, preserving order and isolating failures; results are delivered after the batch according to the delivery rules above. Excess images are skipped with a notice. Quota exhaustion stops the remaining batch, while cache lookup happens before quota checks. A later multi-image reply to a waiting command preserves its channels and refresh option.

If cumulative external-search time exceeds three seconds and work remains, one waiting message is sent per batch. Cache lookup and result-delivery time do not count toward this timer. Existing provider timeouts still apply; Ascii2d and trace.moe explicitly report timeouts. No separate overall search-timeout setting is added. In-flight duplicate suppression applies only to continuous mode, not ordinary commands or LLM requests.

## Continuous search mode

The separate mode settings panel is enabled by default. Disabling it leaves ordinary commands available. `/搜图模式` enables it for the current sender in the current platform conversation only. It automatically handles directly sent image-only messages, including batches. Messages with text, replies, or other component types follow their original processing flow.

- `/搜图模式 g` starts or switches the mode to Google.
- Repeating `/搜图模式` restores the default providers and renews the timeout.
- `/退出搜图模式` closes the mode without cancelling an ongoing search or single-image wait. While the mode is active, the same sender may also send the whole plain-text phrase `结束搜图`, `退出搜图`, `结束`, or `退出`. These shortcuts do not invoke the LLM and are not intercepted outside the mode.
- The default idle timeout is 60 seconds, configurable from 10 to 3600. Images, reopening, and channel switching renew it. Both group and private chats expire and receive an expiration notice.
- Cache, quota, batch limits, and delivery are shared with ordinary searches. Use a separate `/搜图 -purge` command to refresh; refresh is not persisted in the mode.
- An active single-image command wait has priority over mode capture. A new mode cannot be opened while that wait is pending. Mode state exists only in memory and is cleared on plugin reload or bot restart without a shutdown notice.

Behavior references CQPS's sender/conversation isolation, image-based renewal, and manual exit without copying its implementation. Differences include private-chat expiration, image-only capture, and provider selection instead of database selection.

Within the mode, repeated images in one message and images still being processed for the same sender/conversation/channels are skipped without another search, quota charge, or result. Stable identity is used when available, otherwise the full URL. Finished images can be requested again and use the cache. Explicit search commands and `-purge` are not restricted by this suppression. In-flight results are not shared across users or conversations.

## Logs and regression checks

Each request has a shared ID, with `/1`, `/2`, etc. for input images. Logs show cache status/age, URL and stable-identity fingerprints, condition fingerprints, provider plan, actual providers, grouped/raw result counts, filtering, failures, cache writes, quota handling, and elapsed time. A URL fingerprint change alone does not prove recompression. A generic cache miss may mean an absent, expired, or invalid record.

Delivery logs distinguish ordinary messages, forward thresholds, cards, thumbnails, and actual message counts. Diagnostics attached to results do not add another threshold count. “Handed to ordinary text delivery” does not claim platform delivery succeeded. Direct `event.send()` calls may not produce Core's `Prepare to send` line. Mode logs record entry, switching, renewal, duplicate skips, manual exit, and timeout using a hashed conversation/sender identifier. Logs omit raw credentials, image URLs, titles, and upstream error bodies.

Run the regression suite in AstrBot's Python environment without installing test dependencies on the host:

```sh
docker exec -w /AstrBot astrbot python /AstrBot/data/plugins/astrbot_plugin_image_search/checks/run_checks.py
```

This runs unit, AstrBot integration, and delivery checks, returning a nonzero status on failure. QQ group/private payloads use the actual adapter serializer but mock message sending. Tests verify logic and protocols; final client appearance still requires manual acceptance.
