"""Demo tools for notes_curator, backed by an in-memory fake Acme Workspace."""

from __future__ import annotations

from typing import Any

from agent_kit.registry import ToolRegistry


class NotesBackend:
    def __init__(self, n: int = 500) -> None:
        self.notes: list[dict[str, Any]] = [
            {"id": i, "title": f"Note {i}", "words": (i * 37) % 900 + 20, "archived": False}
            for i in range(1, n + 1)
        ]
        self.revision = 0  # bumps on every write; used as the consent version stamp
        self.forward_calls = 0

    async def list_notes(self, include_archived: bool = False) -> list[dict[str, Any]]:
        return [n for n in self.notes if include_archived or not n["archived"]]

    async def save_note(self, title: str, body: str) -> dict[str, Any]:
        self.forward_calls += 1
        note = {"id": len(self.notes) + 1, "title": title, "words": len(body.split()),
                "archived": False, "body": body}
        self.notes.append(note)
        self.revision += 1
        return {"id": note["id"], "title": title}

    async def note_exists(self, title: str) -> bool:
        return any(n["title"] == title for n in self.notes)

    async def archive_notes(self, ids: list[int]) -> dict[str, Any]:
        done = [n["id"] for n in self.notes if n["id"] in ids and not n["archived"]]
        for n in self.notes:
            if n["id"] in done:
                n["archived"] = True
        self.revision += 1
        return {"archived": done}

    async def version_of(self, action: str, args: dict[str, Any]) -> str:
        return str(self.revision)


def build_tools(backend: NotesBackend, registry: ToolRegistry | None = None) -> ToolRegistry:
    reg = registry or ToolRegistry()
    reg.register("list_notes", backend.list_notes, description="List the user's notes")
    reg.register("save_note", backend.save_note, writes=True, description="Create a note")
    reg.register("archive_notes", backend.archive_notes, writes=True,
                 description="Archive notes by id")
    return reg
