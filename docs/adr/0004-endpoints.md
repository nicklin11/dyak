# ADR 0004: Model endpoints are configuration; provenance is recorded

- **Status:** Accepted
- **Date:** 2026-10-03
- **Proposal:** [#15](https://github.com/nicklin11/dyak/issues/15)
- **Scope:** MVP. Endpoint configuration and artefact provenance.

> **`D<n>`** = decision *n* within *this* ADR. The number is local to this file and
> collides with numbers in ADR-0001 and ADR-0004; references from anywhere else must be
> written `ADR-000N Dn`. Reading order and index: [`docs/adr/README.md`](README.md).

## Context

Two model endpoints are needed: one with vision capability for frames (stage 6) and one for
text (stages 7–9). **They may be the same endpoint.** Per ADR-0001 D5 the audit must run in a
separate model **context** — that is a property of the request, not of the model, so the same
model id may legitimately serve both.

Requirements: fast configuration from the command line; no key ever reaches the repository or
the vault (ADR-0001 D9).

## Decision

### D1 — One configuration file, environment overrides on top

`$DYAK_SETTINGS` if set, else `$XDG_CONFIG_HOME/dyak/settings.toml` (the shipped name is
`settings.toml`, not `config.toml`). Precedence: **CLI flag > environment > file > built-in
default**. The flag layer was added by #52: the file is now a real third layer, resolved in one
place (`settings.resolve`), so a flag an operator passes always beats a value in the file.

The file holds URLs, model ids and non-secret settings, as **flat keys** named after the
environment variable lowercased (`llm_endpoint`, `vlm_model`). Credentials come from the
environment only, so the file is safe to keep in a dotfiles repository and the vault sync
cannot leak a credential.

An endpoint is a **plain location**, never a place to carry a credential: neither a
`user:password@` userinfo nor a `?key=` query authenticates anything — the auth channel is
the `api_key` variable, sent as `Authorization: Bearer <key>` (`DYAK_LLM_KEY` /
`DYAK_OPENAI_KEY`). `urllib` does nothing with `user:password@`, and a query key is part of
the location, so on the wire it is not redacted and can leak into an error string; keep
credentials out of the endpoint string entirely (#279). In the recorded form
`Config.redacted()` masks the userinfo and a credential-named query value so the bundle stays
safe (#273, #280).

### D2 — `dyak config set|get|list`, validating as it writes

**Not implemented.** Hand-editing the file is the supported path today. The subcommand is
deliberately deprioritised: #52's owner decision is "make config work and scope everything
doctor checks", and the resolver and the breadth come first. Validating as it writes remains
the shape if it is built.

### D3 — Vision and text are separate sections

`[vision]` and `[text]`, each with its own URL, model id and key variable. They may point at
the same endpoint. Keeping them separate means switching either one later costs one line
rather than a refactor.

**Shipped as separate flat keys, not TOML tables.** The roles are separate exactly as decided —
`vlm_endpoint` / `vlm_model` / `vlm_key` beside `llm_endpoint` / `llm_model` / `llm_key`, with
the vision role falling back to the text one when unset. TOML tables are out of scope (#52
owner decision 1), so the section *shape* changed while the separation this decision is about
did not.

### D4 — Provenance is recorded in the artefact, not in the run log

Every artefact a model produced carries the model id, the endpoint host, and a prompt version.
Without this, the regression check in #3 cannot distinguish "the model changed" from "the
prompt changed" from "the transcript changed" — **and a regression gate that cannot attribute
a difference is not a gate.**

This pairs with stage 6's cache key (frame hash **+** model id, #10): the same model id that
keys the cache is the one written into the artefact.

### D5 — `doctor` reports both endpoints, and prints no key

Reachability and the model id that would be used. **Never the key** — not truncated, not
hashed. A truncated key is still a credential prefix, and a hashed one tempts a reader to
wonder whether it is reversible.

## Alternatives rejected

- **One shared endpoint and key for both roles.** Rejected by measurement rather than taste:
  the default vision backend and the default auditor are different models with different
  failure modes, and merging them hides which one is unavailable when a run goes wrong.
- **Keys in the config file.** Rejected: the vault is a sync target (D9), and a config file
  living beside notes is the likeliest place for a secret to be committed by accident.
- **A TUI for configuration.** Rejected: a second binary to save two command-line arguments.
- **A credential/config library.** Rejected: it would become dyak's first and only runtime
  dependency, to read six keys from a TOML file. The `dependencies = []` property (ADR-0002 D2)
  is worth more than the library.

## Consequences

**Good.** Swapping a backend is configuration, not code. Artefacts stay reproducible.

**Bad.** Two endpoints means two failure modes and two places to look when a run is slow or
wrong.

**Not decided.** Whether the audit's default model equals the synth model. ADR-0001 open
question 1 stands, and remains the largest unmeasured thing in the pipeline.

## Acceptance

- `dyak config set vision.model_id X` round-trips through the file.
- An artefact written by stage 6 or stage 7 names the model id that produced it.
- `doctor` shows both endpoints and prints no key under any circumstance.
- Changing the model id changes the stage 6 cache key.

Implements ADR-0001 D5 and D8. Part of #3.