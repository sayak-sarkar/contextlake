"""Steering-layer generation: turn the knowledge graph into the per-tool context
and guardrail files that local AI dev tools read natively (AGENTS.md, CLAUDE.md,
.windsurfrules, .kiro/steering, .mcp.json).

The templates here are generic; the generated output is specific to the user's
workspace (its repos, languages, dependencies) and lands in their local tree.
"""
