Only work on the issue specified.

Work on branch {{BRANCH}}.

</task>

<context>

<issue>

{{ISSUE_BODY}}

</issue>

<comments>

{{ISSUE_COMMENTS}}

</comments>
{{INTERRUPTED_WORK}}
</context>

<workflow>

## Explore

Read the issue's **What to build** and acceptance criteria to scope the work.
Explore only the files allowed by this prompt's workflow. Do not survey the full repository.

Use the domain glossary in `CONTEXT.md` so that terminology matches the project's language.
Consult `docs/adr/README.md` if present, then read any ADRs that touch the area you're changing.
