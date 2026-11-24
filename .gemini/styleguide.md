# Wardrobe PR Review Style Guide

Review pull requests only for high-severity correctness, packaging, security, or specification defects in changed lines. Never post style nits, summary banners, or speculative suggestions on untouched lines.

## 1. Repository Invariants

* **Markdown Link Integrity**: All relative Markdown links and intra-document `#heading` anchors in changed `.md` files must resolve on disk. Generated `.html` companions under `.docs/**` are derived from the same source, so a broken link there also blocks the source.
* **GitHub Flavored Markdown Tables**: Never use multiline `:` continuation rows in Markdown tables. Every table row must stay on one line so GitHub renders it properly.
* **GitHub Actions Security (`zizmor`)**: Keep top-level workflow `permissions` minimal (`{}` or `contents: read`). Declare write scopes only on the specific job that needs them, set `persist-credentials: false` on `actions/checkout`, and pin every action to a full commit SHA with an inline justification comment.
* **Silent Failure and Comment Accuracy**: Flag swallowed exceptions, unlogged error fallbacks, and modified comments or docstrings that contradict the implementation.
* **Issue Tracker Conventions**: Issues and specs live as markdown under `.scratch/<slug>/` (`spec.md`, `issues/NN-<slug>.md`), with triage state recorded as a `Status:` line. Triage role strings are `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`.
* **Domain Documentation**: Single-context repo. Domain vocabulary lives in `GLOSSARY.md` and decisions in `docs/adr/`; flag edits that invent terms or contradict an ADR without noting it.

## 2. Negative-Rule Ledger (Do Not Flag)

Do not flag any of the following intentional repository conventions:

* **Vendored EGM reviewer**: `.github/scripts/review_pr.py` and `.gemini/styleguide.md` are vendored from `google/elephant-goldfish-model` (Apache-2.0) and are intentionally kept byte-identical apart from the documented `--enforce-template` patch. Do not flag its naming, style, or the EGM-specific prompt wording.
* **Generated Diagram Artifacts**: `.docs/solo-mode/` ships generated `.html`, `.svg`, `.json`, `.pdf` and `img/*.png` artifacts next to their markdown sources. Do not flag generated output for style, size, or duplicated text unless a generator source in the same PR was changed.
* **Hardcoded SHA Pins in `.github/workflows/`**: Repository security rules require Action SHA pins and inline `zizmor: ignore[...]` justifications.
* **Local `.scratch/` Issue Tracker**: `.scratch/` is gitignored in local worktrees. Do not flag a path there as missing from the repository, and do not ask contributors to publish it.
* **`agents/*.md` snake_case**: Repo agent prompt files use snake_case filenames without an `egm-` prefix and YAML frontmatter `name` prefixed with `egm-`. Never ask to rename them to `agents/egm-*.md`.

## 3. Comment Formatting Rules

* **Direct Opening**: Start every comment with `[High]: <1-sentence defect summary>. <1-sentence fix>`. Never print meta-labels such as `BLUF:`, `Bottom line:`, `Summary:`, or `Issue:`.
* **Committable Suggestions**: Include a 1-5 line GitHub `suggestion` block only when committing the block fixes the defect completely without follow-up edits.
* **Concise Technical Prose**: Keep sentences at or under 25 words in active voice and present tense. Avoid em dashes, semicolons, filler praise, and counted preambles.
* **Respect Author Pushback**: Do not re-open or argue a thread after a contributor or maintainer replies `intended`, `by design`, or `won't fix`.
