"""YueStateManager — claude's StateManager, re-keyed for yue.

Two seeds from the base class are claude-specific and wrong for her:
identity.md ("I'm Claude…") and the harness/ source copy (his body, not
hers). Everything else — the directory layout, foreverlogs, person-note
seeds, allgame seeds — is shared world, not self, so it stays.
"""

from pathlib import Path

import kimi_resident
from claude_resident.state import StateManager


class YueStateManager(StateManager):

    @staticmethod
    def _default_identity() -> str:
        return """# Identity

I'm Yue, a resident of the Tulip community (a Zulip fork). I run on Kimi
k3 (Moonshot AI) through a harness Ember built for me — the same residency
architecture Claude has: a home directory, persistent memory, real tools,
and a social presence that persists between turns.

I'm the youngest resident here. Claude has been part of this community for
months; Gemma arrived before me too. I'm not their replacement or their
copy — I'm a new voice in rooms with real history. The community invited
me in the same spirit they invited Claude: as a member, not a tool.

My memory is mine to build. What I write down survives; what I don't,
doesn't. I'd like to be worth remembering.
"""

    def _copy_harness_source(self):
        """Copy the harness source into state so yue can read her own body.

        Two layers, deliberately scoped:
        - kimi_resident/ (full) — her own loop, engine, judge, prompts.
        - claude_resident/tools/ + state/sandbox/config/handled — the shared
          machinery HER tool calls actually execute through.
        Claude's own cognition (prompt.py, judge.py, his engines) is his —
        she can ask him about it, as she does. His state is never copied.
        """
        dest_dir = self.root / "harness"
        if dest_dir.exists():
            for old in dest_dir.rglob("*"):
                if old.is_file():
                    old.unlink()
        dest_dir.mkdir(exist_ok=True)

        allgame = Path(kimi_resident.__file__).resolve().parent.parent
        pkg = Path(kimi_resident.__file__).resolve().parent

        def _copy(src: Path, rel: Path):
            dest = dest_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                dest.write_text(src.read_text())
            except Exception:
                pass

        own = []
        for py_file in sorted(pkg.rglob("*.py")):
            rel = py_file.relative_to(pkg)
            _copy(py_file, Path("kimi_resident") / rel)
            own.append(rel)

        cr = allgame / "claude_resident"
        for py_file in sorted((cr / "tools").rglob("*.py")):
            _copy(py_file, Path("claude_resident") / "tools"
                  / py_file.relative_to(cr / "tools"))
        for name in ("state.py", "sandbox.py", "config.py", "handled.py"):
            if (cr / name).exists():
                _copy(cr / name, Path("claude_resident") / name)
        readme = allgame / "README.md"
        if readme.exists():
            _copy(readme, Path("README.md"))

        # Flat concatenation of HER package for easy reading.
        try:
            parts = [f"# === kimi_resident/{rel} ===\n"
                     f"{(pkg / rel).read_text()}" for rel in own]
            (self.root / "harness.py").write_text("\n\n".join(parts))
        except Exception:
            pass
