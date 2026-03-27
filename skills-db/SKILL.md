---
name: skills-db
description: >-
  Search 77K+ Claude Code skills database with keyword and semantic search.
  USE FOR: find skill, search skills, is there a skill for, existing skills, ready-made, reusable skill,
  how do I do X, find a tool for, check if exists
  DO NOT USE FOR: creating new skills from scratch (use skill-creator plugin)
argument-hint: [search query]
---

# Skills DB Search

Search the database of 77K+ Claude Code skills via REST API (A-RAG pattern).

API base: `http://100.115.152.102:8410`

## Search Strategy

### Step 1: Keyword search (fast, exact terms)
```bash
curl -s "http://100.115.152.102:8410/keyword_search?q=$ARGUMENTS&limit=20"
```

### Step 2: Semantic search (meaning-based, catches synonyms)
```bash
curl -s "http://100.115.152.102:8410/semantic_search?q=$ARGUMENTS&limit=20"
```

### Step 3: Read full skill details for top candidates
```bash
curl -s "http://100.115.152.102:8410/skill/{id}"
```

## How to use

1. Run **keyword search** first — fast, good for exact tech names
2. Run **semantic search** — finds related skills that keyword misses
3. Merge and deduplicate results from both searches
4. Read `skill_md_content` via `/skill/{id}` for the top 3-5 candidates to verify quality
5. Present results to user with name, description, repository URL, installs count

## Response format

For each recommended skill show:
- **Name** and brief description
- **Repository URL** for installation
- **Installs** count as popularity signal
- **Category** if available
- **Why it matches** — brief commentary

## Filter by category
Available categories: framework, language, devops, database, ai-ml, cloud, mcp, security, testing, design, api, mobile, productivity, docs, other

## Health check
```bash
curl -s "http://100.115.152.102:8410/health"
```

## Installation
```
claude skill install <repository_url>
```
