#!/usr/bin/env bash
# Query skills-db API for DevOps-relevant skills
# Usage: skills-db-query.sh "search term" [limit]
SKILLS_DB="http://100.115.152.102:8410"
QUERY="${1:?Usage: $0 <search term> [limit]}"
LIMIT="${2:-5}"

echo "=== Keyword Search: $QUERY ==="
curl -sf "${SKILLS_DB}/keyword_search?q=$(echo "$QUERY" | sed 's/ /+/g')&limit=${LIMIT}" \
    | jq -r '.results[] | "  [\(.id)] \(.name) (\(.installs // 0) installs) — \(.description // "" | .[0:100])"' \
    2>/dev/null || echo "  (skills-db unreachable)"

echo ""
echo "=== Semantic Search: $QUERY ==="
curl -sf "${SKILLS_DB}/semantic_search?q=$(echo "$QUERY" | sed 's/ /+/g')&limit=${LIMIT}" \
    | jq -r '.results[] | "  [\(.id)] \(.name) (\(.installs // 0) installs) — \(.description // "" | .[0:100])"' \
    2>/dev/null || echo "  (skills-db unreachable or semantic search unavailable)"
