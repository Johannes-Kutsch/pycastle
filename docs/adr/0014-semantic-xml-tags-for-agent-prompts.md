# Semantic XML tags for agent prompts

Adopt a fixed four-tag vocabulary for top-level structure in all agent prompts: `<task>`, `<context>`, `<workflow>`, `<output>`. These are the only top-level structural peers. Data sub-sections with existing inner tags (`<issue>`, `<comments>`, `<all-open-issues-json>`) collapse inside `<context>` and keep their inner tags unchanged. `##` sub-headings stay markdown. Standards fragments loaded via `PromptRenderer._STANDARDS_FILES` self-wrap inside the file using three named tags: `<design-standards>`, `<implementation-standards>`, `<output-rules>`; the fragment's `#` heading is removed.

## Considered Options

- **Generic `<Block>` wrapper for every section.** Rejected: loses per-section semantic signal.
- **Per-section sub-tags inside `<context>`.** Rejected: unbounded vocabulary growth; `##` headings already serve sub-structure.
- **Separate top-level tag per standards fragment.** Rejected: defeats fixed-vocabulary goal.
- **No XML, rely on markdown alone.** Rejected: status quo being replaced; Anthropic's guidance recommends XML.

## Consequences

- Fixed four-tag vocabulary; new sections that don't fit signal need to merge or revise via new ADR.
- Data sub-sections keep existing inner tags inside `<context>` — no renaming.
- Markdown sub-headings remain markdown.
- Standards fragments self-wrap; renderer injection mechanism unchanged.
- Standards fragments read less naturally in isolation — accepted; they are prompt fragments, not standalone docs.
