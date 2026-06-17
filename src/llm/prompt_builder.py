"""Deliberately empty — the skill owns all prompt logic now.

Earlier this module assembled a system prompt and transcribed the package into a
messages array. That responsibility moved to the external
``pm-analysis-code-supplement`` skill: ``claude_client.generate_report`` now
uploads the package and invokes the skill by ``skill_id``, and the skill itself
loads the package, writes ``report.json``, verifies it, and renders the ``.docx``
inside its own container.

There is no prompt to build on this side, so this module intentionally exposes
nothing. It is kept only as a placeholder so the import path remains documented;
do not re-add prompt-assembly code here.
"""

from __future__ import annotations
