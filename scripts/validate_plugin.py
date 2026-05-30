#!/usr/bin/env python3
"""Validate the codebase-search plugin's manifest, MCP config, and skills.

Run locally with:  python3 scripts/validate_plugin.py

Exits non-zero (printing each problem) if anything is malformed. Uses only the
standard library so it runs anywhere without extra dependencies.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
errors: list[str] = []


def load_json(rel: str, required_keys: tuple[str, ...]):
    path = ROOT / rel
    if not path.is_file():
        errors.append(f"{rel}: missing")
        return None
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        errors.append(f"{rel}: invalid JSON ({exc})")
        return None
    for key in required_keys:
        if key not in data:
            errors.append(f"{rel}: missing required key '{key}'")
    return data


# Plugin manifest
load_json(".claude-plugin/plugin.json", ("name", "version", "description"))

# MCP server config
mcp = load_json(".mcp.json", ("mcpServers",))
if mcp and isinstance(mcp.get("mcpServers"), dict):
    for name, cfg in mcp["mcpServers"].items():
        if not isinstance(cfg, dict) or "command" not in cfg:
            errors.append(f".mcp.json: server '{name}' missing 'command'")

# Skills: every skills/*/SKILL.md needs YAML frontmatter with name + description
skill_files = sorted((ROOT / "skills").glob("*/SKILL.md"))
if not skill_files:
    errors.append("skills/: no SKILL.md files found")
for skill in skill_files:
    rel = skill.relative_to(ROOT)
    text = skill.read_text()
    if not text.startswith("---"):
        errors.append(f"{rel}: missing YAML frontmatter (no leading '---')")
        continue
    end = text.find("\n---", 3)
    if end == -1:
        errors.append(f"{rel}: frontmatter not terminated with '---'")
        continue
    frontmatter = text[3:end]
    for key in ("name", "description"):
        if not any(line.strip().startswith(f"{key}:") for line in frontmatter.splitlines()):
            errors.append(f"{rel}: frontmatter missing '{key}'")

if errors:
    print("Plugin validation FAILED:")
    for err in errors:
        print(f"  - {err}")
    sys.exit(1)

print(f"Plugin validation passed ({len(skill_files)} skill(s) checked).")
