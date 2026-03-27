#!/usr/bin/env bash
# health-check.sh — verify machine is correctly bootstrapped
# Usage: bash health-check.sh
# Exit: 0 if all critical checks pass, 1 if any critical check fails

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

PASS=0
WARN=0
FAIL=0

ok()   { echo -e "${GREEN}[OK]${NC}   $1"; ((PASS++)); }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; ((WARN++)); }
fail() { echo -e "${RED}[FAIL]${NC} $1"; ((FAIL++)); }

echo "=== Machine Health Check ==="
echo ""

# Claude CLI
if CLAUDE_VERSION=$(~/.local/bin/claude --version 2>/dev/null | head -1); then
    ok "claude CLI: $CLAUDE_VERSION"
elif CLAUDE_VERSION=$(claude --version 2>/dev/null | head -1); then
    ok "claude CLI (system): $CLAUDE_VERSION"
else
    fail "claude CLI not found (tried ~/.local/bin/claude and PATH)"
fi

# Codex CLI
if CODEX_VERSION=$(codex --version 2>/dev/null | head -1); then
    ok "codex CLI: $CODEX_VERSION"
else
    fail "codex CLI not found"
fi

# Node.js
if NODE_VERSION=$(node --version 2>/dev/null); then
    ok "node: $NODE_VERSION"
else
    fail "node not found"
fi

# zsh
if ZSH_VERSION=$(zsh --version 2>/dev/null | head -1); then
    ok "zsh: $ZSH_VERSION"
else
    fail "zsh not found"
fi

# Default shell is zsh
CURRENT_SHELL=$(getent passwd root | cut -d: -f7 2>/dev/null || echo "$SHELL")
if [[ "$CURRENT_SHELL" == *zsh* ]]; then
    ok "default shell is zsh ($CURRENT_SHELL)"
else
    warn "default shell is not zsh (currently: $CURRENT_SHELL) — run: chsh -s \$(which zsh)"
fi

# ~/.claude/settings.json
if [[ -f ~/.claude/settings.json ]]; then
    ok "~/.claude/settings.json exists"
else
    fail "~/.claude/settings.json missing"
fi

# ~/.claude/statusline-command.sh
if [[ -f ~/.claude/statusline-command.sh && -x ~/.claude/statusline-command.sh ]]; then
    ok "~/.claude/statusline-command.sh exists and is executable"
elif [[ -f ~/.claude/statusline-command.sh ]]; then
    warn "~/.claude/statusline-command.sh exists but is not executable — run: chmod +x ~/.claude/statusline-command.sh"
else
    fail "~/.claude/statusline-command.sh missing"
fi

# skills-db skill
if [[ -f ~/.claude/skills/skills-db/SKILL.md ]]; then
    ok "~/.claude/skills/skills-db/SKILL.md exists"
else
    fail "~/.claude/skills/skills-db/SKILL.md missing"
fi

# devops-agent skill
if [[ -f ~/.claude/skills/devops-agent/SKILL.md ]]; then
    ok "~/.claude/skills/devops-agent/SKILL.md exists"
else
    fail "~/.claude/skills/devops-agent/SKILL.md missing"
fi

# Skills DB API reachability (non-critical)
echo ""
echo "--- Network checks (warn only) ---"
if curl -sf --max-time 5 "http://100.115.152.102:8410/health" >/dev/null 2>&1; then
    ok "skills-db API reachable at 100.115.152.102:8410"
else
    warn "skills-db API unreachable at 100.115.152.102:8410 (semantic search unavailable)"
fi

# Summary
echo ""
echo "=== Summary ==="
echo -e "  ${GREEN}Passed:${NC}  $PASS"
if [[ $WARN -gt 0 ]]; then
    echo -e "  ${YELLOW}Warnings:${NC} $WARN"
fi
if [[ $FAIL -gt 0 ]]; then
    echo -e "  ${RED}Failed:${NC}  $FAIL"
    echo ""
    echo "Critical checks failed — machine needs attention."
    exit 1
else
    echo ""
    echo "All critical checks passed."
    exit 0
fi
