"""Run as the chatting user: tools are opened once, as that user, by a backend that enforces
permissions. Anything missing or forbidden refuses the run before the model is called."""

import pytest

from agent_kit import runner
from agent_kit.agents.notes_curator.tools import NotesBackend, build_tools
from agent_kit.confirm import ConfirmGate
from agent_kit.consent import RuleConsentJudge
from agent_kit.fakes import FakeBackendOpener, ScriptedLLM
from agent_kit.handles import HandleStore
from agent_kit.opener import OpenRefused
from agent_kit.registry import BUILTIN_AGENTS, discover
from agent_kit.write_guard import WriteGuard

from .conftest import DONE, calls, tool_observations

ALL = {"list_notes", "save_note", "archive_notes"}


class World:
    def __init__(self, grants=None):
        self.backends = {"alice": NotesBackend(30), "bob": NotesBackend(5)}
        self.backends["alice"].notes[0]["title"] = "ALICE-SECRET"
        self.opener = FakeBackendOpener(
            lambda user: build_tools(self.backends[user]),
            grants or {"alice": ALL, "bob": ALL},
        )
        self.card = discover(BUILTIN_AGENTS, build_tools(NotesBackend())).agents["notes_curator"].card
        self.gate = ConfirmGate()
        self.guard = WriteGuard(build_tools(NotesBackend()), self._exists, self.card.budget)

    async def _exists(self, tool, key):
        return any(n["title"] == key for b in self.backends.values() for n in b.notes)

    async def run(self, llm, user, task_id="t1"):
        return await runner.run(self.card, runner.Task(task_id, "go"), llm, None, HandleStore(),
                                self.guard, self.gate, opener=self.opener, user=user)


async def test_user_b_cannot_see_user_a_notes():
    w = World()
    llm = ScriptedLLM([calls(("list_notes", {})), DONE])
    assert (await w.run(llm, "bob")).status == "completed"
    seen = tool_observations(llm)[0]
    assert "Note 1" in seen and "ALICE-SECRET" not in seen and "Note 30" not in seen  # bob has 5


async def test_guarded_save_lands_in_the_chatting_users_data_only():
    w = World()
    await w.run(ScriptedLLM([calls(("save_note", {"title": "mine", "body": "b"})), DONE]), "bob")
    assert [n["title"] for n in w.backends["bob"].notes[-1:]] == ["mine"]
    assert all(n["title"] != "mine" for n in w.backends["alice"].notes)


async def test_tools_are_opened_once_per_run_as_that_user():
    w = World()
    await w.run(ScriptedLLM([calls(("list_notes", {})), calls(("list_notes", {})), DONE]), "bob")
    assert w.opener.opened == ["bob"]


async def test_forbidden_tool_refuses_to_start():
    w = World(grants={"alice": ALL, "bob": {"list_notes", "save_note"}})  # bob may not archive
    llm = ScriptedLLM([DONE])
    result = await w.run(llm, "bob")
    assert result.status == "failed" and "refusing to start" in result.message
    assert "archive_notes" in result.message and llm.seen == []  # the model was never called


async def test_missing_tool_and_unknown_user_refuse_to_start():
    w = World()
    w.opener._build = lambda user: build_tools(w.backends[user]).subset(["list_notes", "save_note"])
    assert "missing" in (await w.run(ScriptedLLM([DONE]), "alice")).message
    assert "unknown user" in (await w.run(ScriptedLLM([DONE]), "mallory")).message


async def test_opener_without_user_or_without_anything_fails_not_raises():
    w = World()
    result = await runner.run(w.card, runner.Task("t", "x"), ScriptedLLM([DONE]), None, HandleStore(),
                              w.guard, w.gate, opener=w.opener)
    assert result.status == "failed"
    result = await runner.run(w.card, runner.Task("t", "x"), ScriptedLLM([DONE]), None, HandleStore(),
                              w.guard, w.gate)
    assert result.status == "failed"


async def test_opened_registry_holds_only_the_cards_tools():
    w = World()

    def build(user):
        reg = build_tools(w.backends[user])

        async def extra():
            ...

        reg.register("not_on_card", extra)
        return reg

    w.opener._build = build
    opened = await w.opener.open(w.card, "alice")
    assert "not_on_card" not in opened and set(w.card.tools) == {t for t in ALL if t in opened}


async def test_confirmed_action_runs_as_the_same_user():
    w = World()
    first = await w.run(ScriptedLLM([calls(("archive_notes", {"ids": [1]})), DONE]), "bob")
    assert first.status == "needs_input"
    done = await runner.resume(w.card, first.pending_action.id, "yes", RuleConsentJudge(), None,
                               w.gate, w.guard, "t1", current_version="", opener=w.opener, user="bob")
    assert done.status == "completed"
    assert w.backends["bob"].notes[0]["archived"] and not w.backends["alice"].notes[0]["archived"]
    assert w.opener.opened == ["bob", "bob"]


async def test_resume_refuses_when_the_user_lost_permission():
    w = World()
    first = await w.run(ScriptedLLM([calls(("archive_notes", {"ids": [1]})), DONE]), "bob")
    w.opener._grants["bob"] = {"list_notes", "save_note"}  # permission removed between turns
    done = await runner.resume(w.card, first.pending_action.id, "yes", RuleConsentJudge(), None,
                               w.gate, w.guard, "t1", current_version="", opener=w.opener, user="bob")
    assert done.status == "failed" and not w.backends["bob"].notes[0]["archived"]


def test_open_refused_is_an_exception_type():
    assert issubclass(OpenRefused, Exception)
    with pytest.raises(OpenRefused):
        raise OpenRefused("x")
