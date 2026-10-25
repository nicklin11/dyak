# docs/ — index

| file | holds |
|---|---|
| [adr/](adr/README.md) | every decision made so far, each ADR dated and signed off against the measurements — read ADR-0001 first |
| [running.md](running.md) | full configuration reference: every `DYAK_*` variable, every flag, every subcommand |
| [artefacts.md](artefacts.md) | what the bundle contains, who writes each file, what `report.json` really verified |
| [quality.md](quality.md) | what each gate rejects, what exit 0 does not guarantee, the baseline comparison |
| [stages.md](stages.md) | the 12 pipeline stages, what exists, what is verified |
| [audit-baseline.md](audit-baseline.md) | the #20 regression check: the fixed-note audit, the 21-finding comparison, and the recall gap it measured |

The README carries only what a first run needs; this directory owns everything else. Where
no document holds a claim, an issue does — that is the single-copy rule for the zero-shot
classifier plan ([#85](https://github.com/nicklin11/dyak/issues/85)), which used to live in
both places.
