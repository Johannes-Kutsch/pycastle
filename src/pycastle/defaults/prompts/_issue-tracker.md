# GitHub Issue Tracker — REST Recipes

All recipes authenticate with `Authorization: Bearer $GH_TOKEN`. `OWNER/REPO` is derived once per session:

```bash
REPO_URL=$(git remote get-url origin)
OWNER_REPO=$(echo "$REPO_URL" | sed 's|.*github\.com[:/]\(.*\)\.git|\1|;s|.*github\.com[:/]\(.*\)|\1|')
```

---

## Create issue (body from file)

```bash
curl -sS -X POST \
  -H "Authorization: Bearer $GH_TOKEN" \
  -H "Content-Type: application/json" \
  "https://api.github.com/repos/$OWNER_REPO/issues" \
  -d "{\"title\": \"$TITLE\", \"body\": $(jq -Rs . < body.md)}" \
  | jq '{number: .number, url: .html_url}'
```

Exits non-zero and prints the error message on non-2xx responses when `curl -sS --fail` is used.

---

## View issue with comments

```bash
# Fetch issue
curl -sS --fail \
  -H "Authorization: Bearer $GH_TOKEN" \
  "https://api.github.com/repos/$OWNER_REPO/issues/$ISSUE_NUMBER" \
  | jq '{number, title, body, state, labels: [.labels[].name]}'

# Fetch comments
curl -sS --fail \
  -H "Authorization: Bearer $GH_TOKEN" \
  "https://api.github.com/repos/$OWNER_REPO/issues/$ISSUE_NUMBER/comments" \
  | jq '[.[] | {author: .user.login, created_at, body}]'
```

---

## List issues by title search

```bash
curl -sS --fail \
  -H "Authorization: Bearer $GH_TOKEN" \
  "https://api.github.com/search/issues?q=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" "$QUERY")+repo:$OWNER_REPO+is:issue" \
  | jq '[.items[] | {number, title, state, url: .html_url}]'
```

---

## Add label

```bash
curl -sS --fail -X POST \
  -H "Authorization: Bearer $GH_TOKEN" \
  -H "Content-Type: application/json" \
  "https://api.github.com/repos/$OWNER_REPO/issues/$ISSUE_NUMBER/labels" \
  -d "{\"labels\": [\"$LABEL\"]}" \
  | jq '[.[].name]'
```

---

## Remove label

```bash
curl -sS --fail -X DELETE \
  -H "Authorization: Bearer $GH_TOKEN" \
  "https://api.github.com/repos/$OWNER_REPO/issues/$ISSUE_NUMBER/labels/$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" "$LABEL")"
```

Returns 200 with remaining labels on success, 404 if the label was not present.

---

## Add comment

```bash
curl -sS --fail -X POST \
  -H "Authorization: Bearer $GH_TOKEN" \
  -H "Content-Type: application/json" \
  "https://api.github.com/repos/$OWNER_REPO/issues/$ISSUE_NUMBER/comments" \
  -d "{\"body\": $(jq -Rs . <<< \"$COMMENT_BODY\")}" \
  | jq '{id, url: .html_url}'
```

---

## Close issue

```bash
curl -sS --fail -X PATCH \
  -H "Authorization: Bearer $GH_TOKEN" \
  -H "Content-Type: application/json" \
  "https://api.github.com/repos/$OWNER_REPO/issues/$ISSUE_NUMBER" \
  -d '{"state": "closed"}' \
  | jq '{number, state}'
```

---

## Link as sub-issue

```bash
# Extract numeric id of the child issue first
CHILD_ISSUE_ID=$(curl -sS --fail \
  -H "Authorization: Bearer $GH_TOKEN" \
  "https://api.github.com/repos/$OWNER_REPO/issues/$CHILD_ISSUE_NUMBER" \
  | jq '.id')

# Attach child to parent via the sub_issues endpoint
curl -sS --fail -X POST \
  -H "Authorization: Bearer $GH_TOKEN" \
  -H "Content-Type: application/json" \
  "https://api.github.com/repos/$OWNER_REPO/issues/$PARENT_ISSUE_NUMBER/sub_issues" \
  -d "{\"sub_issue_id\": $CHILD_ISSUE_ID}" \
  | jq '{sub_issue_id: .id, url: .html_url}'
```
