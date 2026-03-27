---
name: skill-creator
description: >-
  Create, improve, and manage Claude Code skills in daicon-it/skills repo.
  USE FOR: create agent, create skill, new agent, make a skill for, build agent,
  add skill, improve skill, update agent, I need an agent that, automate with agent
  DO NOT USE FOR: searching existing skills (use skills-db), running existing agents
install: global
argument-hint: [what the agent/skill should do]
---

# Skill Creator

Create and manage skills in `daicon-it/skills` repo. Skills auto-deploy to all machines via bootstrap.

## Workflow

When user asks to create an agent or skill, follow this exact sequence:

### Step 1: Check for duplicates (MANDATORY)

Before creating anything, check if a similar skill already exists:

```bash
# Check our own skills first
curl -s "http://100.115.152.102:8410/own_skills" | jq '.skills[] | {name, description}'

# Then check semantic similarity across ALL 77K+ skills
curl -s "http://100.115.152.102:8410/similar?q=DESCRIPTION_OF_WHAT_AGENT_SHOULD_DO&threshold=0.7&limit=10" \
  | jq '.results[] | {name, author, similarity, is_own, description}'
```

**Decision matrix:**

| Result | Action |
|--------|--------|
| Own skill with similarity > 0.8 | **IMPROVE** existing skill, don't create new |
| Own skill with similarity 0.6-0.8 | Ask user: improve existing or create new? |
| Community skill with similarity > 0.8 | Use as reference, adapt for our needs |
| Community skill with similarity 0.6-0.8 | Use as inspiration for structure/patterns |
| Nothing above 0.6 | Create from scratch |

### Step 2: Gather reference material

```bash
# Get full content of top community matches for reference
curl -s "http://100.115.152.102:8410/skill/{id}" | jq '.skill_md_content'

# Search by relevant keywords
curl -s "http://100.115.152.102:8410/keyword_search?q=RELEVANT_KEYWORDS&limit=10" \
  | jq '.results[] | {id, name, installs, description}'
```

### Step 3: Create or improve the skill

**If CREATING new skill:**

```bash
# Skills repo is at /root/skills/ on CT 101
# Create skill directory
mkdir -p /root/skills/SKILL_NAME

# Write SKILL.md following the format below
# Add references/ and scripts/ subdirectories if needed

# Commit and push
cd /root/skills
git add SKILL_NAME/
git commit -m "add SKILL_NAME skill"
git push
```

**If IMPROVING existing skill:**

```bash
# Edit existing SKILL.md
# Read current version first
cat /root/skills/EXISTING_SKILL/SKILL.md

# Edit with improvements
# Commit
cd /root/skills
git add EXISTING_SKILL/
git commit -m "improve EXISTING_SKILL: description of changes"
git push
```

### Step 4: Sync to database

```bash
# Trigger immediate ingest to skillsdb
ssh root@193.168.199.43 "cd /root/skills-db && python3 tools/ingest_own.py"
```

### Step 5: Deploy to current machine

```bash
# Install on current machine immediately (other machines pick up on next bootstrap)
curl -fsSL "https://raw.githubusercontent.com/daicon-it/infra/master/machine-bootstrap.sh" \
  | bash -s -- --skills-only --force
```

## Skill Classification

Every skill MUST have an `install:` field in frontmatter:

| Type | `install:` | Meaning | Watchdog monitors? |
|------|-----------|---------|-------------------|
| **global** | `install: global` | Installed on ALL machines via bootstrap | Yes — drift detection |
| **shared** | `install: shared` | In daicon-it/skills repo, installed manually per-project need | No |
| **project** | (no field, lives in repo `.claude/`) | Part of a specific project repo | No |
| **vendor** | (no field, comes from dependency) | Third-party (e.g. bmad-method) | No |

**When creating a new skill, ask:** "Does every machine need this?" → `global`. "Only machines with specific projects?" → `shared`.

## SKILL.md Format

```yaml
---
name: skill-name
description: >-
  What this skill does. One paragraph.
  USE FOR: comma-separated trigger phrases
  DO NOT USE FOR: what other skills handle (with cross-references)
install: global|shared
---

# Skill Title

Brief description of purpose.

## When to use
Trigger conditions in plain language.

## How it works
Step-by-step methodology.

## Commands / Examples
Concrete commands, code blocks, tables.

## References
Links to reference files if the skill has subdirectories.
```

## Rules

1. **Always check duplicates first** — never create a skill that overlaps with an existing one
2. **Skill names** — lowercase, hyphenated: `monitoring-agent`, `backup-manager`
3. **One skill = one responsibility** — don't combine unrelated concerns
4. **USE FOR / DO NOT USE FOR** — always include both, with cross-references to other skills
5. **Keep SKILL.md under 2000 words** — put details in `references/` subdirectory
6. **Include practical examples** — commands, not just theory
7. **Test before pushing** — verify the skill works in current session
