# GitHub Issue Tracker — gh Recipes

All recipes authenticate via `$GH_TOKEN`. `OWNER/REPO` is resolved automatically from the cwd git remote by `gh`.

---

## Create issue (body from file)

```bash
GH_TOKEN=$GH_TOKEN gh issue create --title "$TITLE" --body-file body.md
```

---

## View issue with comments

```bash
GH_TOKEN=$GH_TOKEN gh issue view "$ISSUE_NUMBER" --comments
```

---

## List issues by title search

```bash
GH_TOKEN=$GH_TOKEN gh issue list --search "$QUERY"
```

---

## Add label

```bash
GH_TOKEN=$GH_TOKEN gh issue edit "$ISSUE_NUMBER" --add-label "$LABEL"
```

---

## Remove label

```bash
GH_TOKEN=$GH_TOKEN gh issue edit "$ISSUE_NUMBER" --remove-label "$LABEL"
```

---

## Add comment

```bash
GH_TOKEN=$GH_TOKEN gh issue comment "$ISSUE_NUMBER" --body "$COMMENT_BODY"
```

---

## Close issue

```bash
GH_TOKEN=$GH_TOKEN gh issue close "$ISSUE_NUMBER"
```

---

## Link as sub-issue

No native `gh` verb exists for sub-issues; use `gh api` instead. Replace `OWNER/REPO`, `N` (parent), and `M` (child) with the actual values:

```bash
GH_TOKEN=$GH_TOKEN gh api repos/OWNER/REPO/issues/N/sub_issues \
  --method POST \
  --field sub_issue_id=M
```
