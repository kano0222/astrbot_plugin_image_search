# Repository Guidance

This repository contains one AstrBot plugin for finding the origin of an image.

- Keep command handling, LLM tools, search orchestration, providers, and presentation separate.
- Commands must invoke the search application directly and must not depend on an LLM.
- Providers return structured results and never send AstrBot messages themselves.
- `auto`, `source`, and `anime` are user intents, not provider names.
- Do not add unrelated bot features such as OCR, media parsing, reminders, or administration.
- Do not add compatibility migrations before the first public release.
- Do not copy GPL or AGPL implementation code without an explicit licensing decision.
- Add focused tests for search-plan behavior and provider failure isolation.

## Collaboration and product decisions

- Distinguish questions/evaluation from execution. An explicit outcome authorizes its reasonable implementation, integration, tests, and deployment steps, not unrelated features.
- Before adding a feature or setting, assess the concrete benefit, existing capabilities, user burden, and maintenance cost. Reuse or replace overlapping capabilities; do not add parallel controls for one decision.
- Challenge incorrect premises or low-value technical proposals with evidence and a practical alternative. Preserve user decision authority; only ask when an alternative materially changes the requested outcome or risk.
- Correct obvious typos and omissions in project conversation from context. This does not authorize runtime fuzzy matching, new configuration, or other product behavior.
- Inspect upstream hooks and downstream calls before claiming a workflow avoids a model call or external side effect; a local module is not the whole execution path.
- User-facing labels and instructions describe concrete behavior, not project nicknames or collaboration history. Keep display names separate from internal identifiers.
- Complete related changes and automated validation as one batch. Preserve prior acceptance, avoid repeated small manual checks, and distinguish file deployment from live-instance reload and verification.
