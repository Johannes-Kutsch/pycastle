When you have finalised your ranked candidates, emit a `<candidates>` block containing a JSON array, then close with `<promise>COMPLETE</promise>`.

Each entry must carry `rank` (integer, starting at 1), `title` (string), and an optional `summary` (string). Order entries by strength — rank 1 is the strongest. Return between one and the number you were asked for; do not pad the list to reach that number if you found fewer genuine candidates.

Example (two candidates):

```
<candidates>
[
  {"rank": 1, "title": "Deepen the output-protocol parse seam", "summary": "Extract and unit-test _parse_planner_body in isolation."},
  {"rank": 2, "title": "Consolidate fixture sprawl in test_improve.py"}
]
</candidates>
<promise>COMPLETE</promise>
```

If every candidate fails the AFK-safety filter, emit `<promise>NO-CANDIDATE</promise>` with no `<candidates>` block.
