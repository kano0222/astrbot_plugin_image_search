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

- Distinguish discussion and evaluation from execution. Explicit implementation requests authorize reasonable implementation, integration, and testing, but do not establish that the proposed design is sound. Raise material objections with evidence and practical alternatives; otherwise proceed without unnecessary confirmation.
- Before adding a feature or setting, assess its concrete benefit, existing capabilities, user burden, and maintenance cost. Reuse or replace overlapping capabilities; do not add parallel controls for one decision.
- Prefer improving core search reliability, result quality, and usability over expanding the feature set. Do not introduce features, settings, or abstractions solely for hypothetical future needs.
- Challenge incorrect premises, unnecessary complexity, and low-value technical proposals. Do not treat technical feasibility as sufficient justification or invent hypothetical benefits to support a proposal.
- Preserve user decision authority. Ask only when unresolved ambiguity or an alternative materially changes the requested outcome or risk. Once the user has acknowledged a tradeoff and explicitly chosen a direction, respect that decision unless new material evidence or risks emerge.
- Correct obvious typos and omissions in project conversation from context. This does not authorize runtime fuzzy matching, new configuration, or other product behavior.
- Inspect relevant code, upstream hooks, and downstream calls before drawing conclusions about behavior or side effects. Do not infer the behavior of an entire workflow from one module.
- User-facing labels and instructions must describe concrete behavior, not project nicknames or collaboration history. Keep display names separate from internal identifiers.

## Execution and verification

- Complete authorized implementation, integration, and automated validation as one coherent batch. Avoid repeated confirmation and fragmented manual checks.
- For small, low-risk changes, apply proportionate judgment and execute directly. Do not turn routine modifications into lengthy design discussions.
- Prefer the smallest sufficient change, but do not sacrifice correctness or maintainability merely to minimize the diff.
- Verify the actual behavior affected by a change. Use focused tests and existing project tooling; do not introduce dependencies or infrastructure without a concrete need.
- Distinguish code changes, file deployment, live-instance reload, and runtime verification. Do not present one as proof of another.
- Do not assume authorization for externally visible, destructive, or disruptive actions.
- Never report intended or unverified work as completed. Clearly identify meaningful validation gaps.

## Communication and reporting

- Communicate in concise, natural engineering language. Prioritize conclusions, concrete facts, and information that affects decisions.
- Match explanation depth to complexity. Answer simple questions directly; explain reasoning, tradeoffs, and risks when they materially matter.
- Do not restate or repackage the user's requirements as a lengthy plan or summary. Explain what was discovered, decided, changed, or remains unresolved.
- Avoid boilerplate sections, ceremonial confirmations, repetitive progress narration, and obvious implementation details presented as important findings.
- Do not turn routine technical choices into formal design discussions. Explain a choice only when its rationale is non-obvious or affects behavior, maintainability, or risk.
- During execution, report meaningful findings, blockers, or deviations rather than narrating every step.
- After implementation, briefly state what actually changed, what was verified, and any remaining issues. Omit empty sections and unnecessary process descriptions.
- Distinguish facts, inferences, assumptions, and uncertainty. Do not exaggerate benefits, risks, confidence, or the significance of minor changes.
- Brevity must not hide important evidence, behavior changes, failures, or unresolved risks. Minimize unnecessary words, not necessary reasoning or verification.