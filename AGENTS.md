# Contributing to AstrBot Image Search

This repository contains an AstrBot plugin for finding image sources and identifying anime scenes. Start with [README.md](README.md) for usage and [ADVANCED.md](ADVANCED.md) for configuration and troubleshooting.

## Scope and architecture

Improve search reliability, result quality, and usability. New providers and settings should solve a concrete need and reuse existing capabilities. OCR, media parsing, reminders, and bot administration are outside this plugin's scope.

| Location | Responsibility |
| --- | --- |
| `main.py` | AstrBot integration, command and LLM hooks, configuration, request lifecycle |
| `command_aliases.py`, `simple_requests.py`, `image_input.py` | Channel parsing, direct text routing, image selection and identity |
| `tool_contract.py` | LLM tool descriptions and completion instructions |
| `core/models.py`, `core/provider.py`, `core/service.py` | Structured contracts, search plans and provider failure isolation |
| `core/providers/` | Search service adapters and response parsing |
| `core/search_state.py`, `core/image_wait.py`, `core/search_session.py` | Cache and quotas, pending images, continuous search sessions |
| `runtime/` | HTTP transport, uploads, preview downloads and source-page access |
| `presentation.py`, `result_delivery.py` | Result grouping, text and cards, rich delivery and fallback |
| `_conf_schema.json`, `metadata.yaml`, `requirements.txt` | Public configuration, plugin metadata and runtime dependencies |
| `tests/`, `checks/` | Unit tests and AstrBot integration checks |

Keep parsing, orchestration, network access, and message delivery separate. Providers return structured results; they do not send messages or depend on AstrBot handlers. Commands invoke the search service directly and do not require an LLM.

## Behavior contracts

- `auto`, `source`, and `anime` express user intent, not provider names. Adding a provider must not automatically expand the default search plan.
- Explicit channel selection searches only the selected available providers, without fallback or anime follow-up. Tool channels come from the current user message, not quoted text or model-supplied additions.
- Preserve current-image priority over quoted images, sender and chat isolation for pending images and sessions, and the meaning of `-purge`.
- Keep provider failures isolated. Preserve successful results and meaningful diagnostics; distinguish empty results, filtered candidates, and failed requests.
- Preserve cache identity, expiry, quota accounting, and persisted data. Changes to defaults must not silently overwrite explicit user settings.
- Keep raw result URLs and identities separate from display formatting. Reuse existing grouping and delivery paths for new providers.

## Making changes

- Check `git status` and read the affected implementation, callers, and tests. Preserve unrelated changes.
- Fix problems at the owning layer. Add regression coverage for observable behavior rather than reproducing the implementation in a test.
- Keep changes focused. Discuss changes to public behavior, configuration, persisted data, or dependencies before expanding an agreed task. Avoid speculative abstractions and duplicate controls.
- Validate external responses and configuration at their boundaries. Do not turn unexpected errors into successful empty results or invent missing metadata or links.
- Use bounded timeouts and downloads, release resources on cancellation, and retain configured proxy behavior. Logs must not contain credentials, private image URLs, or raw upstream payloads.
- Keep user-facing text concise and concrete. Put essential usage in the README and details in the advanced guide; update configuration labels and documentation together.
- Check licensing before reusing external code. Do not copy GPL or AGPL implementations without an explicit licensing decision. See [LICENSE](LICENSE) and the README's asset licensing note.

## Development and validation

Use Python 3.10 or later and an AstrBot 4.x environment compatible with `metadata.yaml`. Install [requirements.txt](requirements.txt) into the same Python environment used by AstrBot. The integration checks also require AstrBot and its dependencies to be installed.

Run commands from the repository root:

```sh
python -m unittest discover -s tests
python -m unittest discover -s tests -p test_trace_moe.py
python checks/run_checks.py
git diff --check
```

The second command is an example of focused provider validation. `checks/run_checks.py` runs the unit, AstrBot integration, and result-delivery suites. When changing the shared curl client, also run Cookie checks from the directory containing the plugin package:

```sh
python -m astrbot_plugin_image_search.checks.curl_cookie_smoke
```

- During iteration, run focused tests. Before handing off behavior changes, run the full regression batch in the AstrBot environment and check changed Python files with Ruff when available.
- Cover routing, thresholds, provider failure isolation, parsing, and resource cleanup where affected. For presentation, cover grouping and plain-message fallback as well as rich output.
- For documentation-only changes, verify wording, links, paths, commands, and `git diff --check`; runtime tests do not validate prose.
- Report failed, skipped, or unavailable checks. Missing dependencies and skipped integration tests are validation gaps.
- Tests and successful API responses do not prove live QQ delivery or client rendering. Distinguish source edits, installed files, runtime reload, and live verification. Use isolated copies for automated checks; deployment, reload, and external-service testing require authorization for that activity.

## Review and handoff

Review the final diff for correctness, regressions, omissions, and unnecessary complexity. Explain the behavior changed, validation results, and remaining gaps. Keep public PR descriptions focused on the change; exclude private logs and machine-specific details.

For complex or high-risk changes, provide the goal, constraints, scoped diff, test evidence, and specific review questions. Keep reviewed code stable and validate findings before applying them.

Keep this guide aligned with module ownership, behavior contracts, and validation commands. Use repository-relative links; keep local paths and conversation identifiers out of contributor documentation.
