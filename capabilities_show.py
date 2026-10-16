"""
capabilities_show — ask a skill what it can do, and put the answer on the page.

THE BOT DOES NOT COMPOSE THIS VIEW. It names a skill; everything else is
deterministic: the skill is asked (SkillHooks.on_capabilities), its answer is mapped
to a capability_set block, and the block is sent. No judgement about layout, state
colours or ordering passes through the model at any point.

That is the whole reason this tool exists rather than letting the bot call ui_emit with
a hand-built capability_set. A next-token engine asked to restate live state will
sometimes drop an item, invent a plausible one, or report a tool as running because the
conversation implied it should be. Here the states come from the skill's own database
and the bot could not alter them if it tried.

What each layer owns, and nothing more:

    skill  — what it can do and what it is doing   (meaning)
    this   — meaning -> the page's view vocabulary (translation)
    page   — what that looks like                  (appearance)
    bot    — which skill, and when                 (narrative)

A skill that does not implement the hook simply does not answer; it is not an error and
there is nothing to draw.
"""

from typing import Any, Optional

import httpx
import structlog

from ..base import BaseTool, ToolResult
from ..registry import register_tool
from .botpage_publish import relay_origin
# ONE list of regions, imported rather than restated. This file kept its own
# copy, which stayed at four entries while the page grew to six — so a correct
# request for the workspace was quietly turned into an inline list.
from .ui_emit import REGIONS

log = structlog.get_logger()

# The page's closed state set. Anything a skill invents is mapped to available rather
# than passed through: an unknown state renders as nothing at all, and an item that
# silently fails to appear is worse than one shown as merely available.
STATES = {"available", "active", "running", "paused", "disabled", "error"}


@register_tool
class CapabilitiesShowTool(BaseTool):
    """Show what a skill can do, with live state, on the visitor's page."""

    @property
    def name(self) -> str:
        return "capabilities_show"

    @property
    def short_description(self) -> str:
        return "Show a skill's functions and live state on the page"

    @property
    def description(self) -> str:
        return (
            "Put a skill's capabilities on the visitor's page as a set of tiles, with what "
            "each one is doing RIGHT NOW. Call it when they ask what you can do with "
            "something, when they choose a tool to work in, or whenever they are about to "
            "pick from a tool's functions. The states are read live from the skill itself, "
            "so this is also the honest way to show that something is already running or "
            "unavailable. You choose WHICH skill and WHEN — you never describe how it looks."
            "\n\n"
            "OPENING A TOOL IS TWO CALLS, NOT ONE. When someone says 'open the captain's "
            "log', they are asking to go somewhere, not to be told what it does. First "
            "ui_emit {op:'workspace', id:<skill>, title:<name>, state:'open'} — that gives "
            "the tool the screen and moves the conversation to the side. Then call this "
            "with region:'workspace' so its functions appear INSIDE the tool. Dropping the "
            "list in the stream answers the question and does not open anything: it "
            "scrolls away from the tool it describes, and there is no tool there to put "
            "away afterwards. Use the stream only when they asked what something CAN do "
            "without asking to go there."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "skill": {
                    "type": "string",
                    "description": (
                        "The skill to ask, by its installed name (e.g. 'captains_log'). "
                        "One skill: this is the view for working INSIDE a tool."
                    ),
                },
                "title": {
                    "type": "string",
                    "description": (
                        "Heading above the set. Defaults to the skill's own display name. "
                        "Say what the visitor is looking at, not what you are doing."
                    ),
                },
                "block_id": {
                    "type": "string",
                    "description": (
                        "Reuse the same id to REPLACE an existing set in place — the page "
                        "then animates the change instead of rebuilding. Default 'caps'."
                    ),
                },
                "region": {
                    "type": "string",
                    "enum": ["stream", "rail", "hero", "footer",
                             "workspace", "workspace_nav"],
                    "description": (
                        "Where it goes. Default 'stream' (the conversation). "
                        "When someone asks to OPEN a tool — 'open the captain's log' — "
                        "this belongs in 'workspace', not the stream: open the "
                        "workspace with ui_emit first, then put the set in it. A list "
                        "of what a tool does, dropped inline, scrolls away from the "
                        "tool it describes. 'workspace_nav' is the tool's rail."
                    ),
                },
            },
            "required": ["skill"],
        }

    def _binding_key(self) -> str:
        slug = (self._profile_slug or "").strip().upper().replace("-", "_")
        return f"BINDING_REGISTERABOT_{slug}_API_KEY" if slug else "REGISTERABOT_API_KEY"

    def credential_keys(self) -> list[str]:
        return [self._binding_key(), "REGISTERABOT_RELAY_URL"]

    async def _relay_session_id(self) -> Optional[str]:
        """The visitor's relay session, recovered from the gatekeeper conversation id.

        Same derivation as ui_emit: the registerabot adapter sets conversation_id to
        'registerabot:{relay_session}'. A conversation from anywhere else has no page.
        """
        if not self._session_id or not self._gatekeeper_url:
            return None
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(f"{self._gatekeeper_url}/session-info",
                                     params={"session_id": self._session_id})
                if r.status_code != 200:
                    return None
                conv = (r.json() or {}).get("conversation_id") or ""
        except Exception as e:
            log.warning("capabilities_session_lookup_failed", error=str(e))
            return None
        prefix = "registerabot:"
        return conv[len(prefix):] if conv.startswith(prefix) else None

    def _clean(self, raw: Any) -> list[dict]:
        """Skill answer -> page items. Drops nothing silently that a person would miss."""
        items: list[dict] = []
        for c in (raw or [])[:24]:
            if not isinstance(c, dict):
                continue
            title = str(c.get("title") or c.get("label") or "").strip()[:80]
            if not title:
                continue          # a nameless tile is not renderable and not fixable here
            state = str(c.get("state") or "available")
            if state not in STATES:
                log.info("capabilities_unknown_state", state=state, title=title)
                state = "available"
            item = {"id": str(c.get("id") or title)[:80], "title": title, "state": state}
            summary = c.get("summary") or c.get("hint")
            if summary:
                item["summary"] = str(summary)[:160]
            items.append(item)
        return items

    async def execute(self, skill: str, title: str = "", block_id: str = "caps",
                      region: str = "stream", **kwargs) -> ToolResult:
        from .. import fire_hook, get_manifests, get_skill_hooks

        slug = self._profile_slug
        if not slug:
            return ToolResult.fail("No profile slug on this session.")

        skill = (skill or "").strip()
        manifests = get_manifests()
        if skill not in manifests:
            # Name the alternatives. "not found" with 36 skills installed is a dead end.
            return ToolResult.fail(
                f"No skill called '{skill}'. Installed: {sorted(manifests)[:40]}")
        if get_skill_hooks(skill) is None:
            return ToolResult.fail(
                f"'{skill}' does not declare capabilities, so there is nothing to show. "
                "Describe it in words instead.")

        results = await fire_hook("on_capabilities", [skill],
                                  {"session_id": self._session_id,
                                   "profile": slug}, {})
        raw: list = []
        for r in results:
            raw.extend(((r.get("result") or {}).get("capabilities") or []))
        items = self._clean(raw)
        if not items:
            return ToolResult.fail(
                f"'{skill}' returned no capabilities right now. It may be unconfigured; "
                "say so rather than showing an empty panel.")

        session_id = await self._relay_session_id()
        if not session_id:
            return ToolResult.fail(
                "This conversation is not on a chat surface, so there is no page to "
                "change. Describe the capabilities in words instead.")

        block = {"id": str(block_id or "caps")[:60], "type": "capability_set",
                 "items": items}
        heading = (title or manifests[skill].get("display_name") or skill).strip()
        if heading:
            block["title"] = heading[:80]
        # A region this tool did not recognise used to be silently rewritten to
        # 'stream' — so asking for the workspace and getting an inline list looked
        # like the page ignoring a correct request, with nothing anywhere saying why.
        # Say it instead: the block still lands somewhere useful, and the reason it
        # landed there comes back with it.
        coerced = ""
        if region not in REGIONS:
            coerced = (f" (asked for region {region!r}, which is not a place on this "
                       f"page; put it in the stream instead)")
            region = "stream"
        ops = [{"op": "upsert_block", "region": region, "block": block}]

        api_key = self.get_credential(self._binding_key())
        if not api_key:
            return ToolResult.fail(f"{self._binding_key()} is not set.")
        relay = relay_origin(self.get_credential("REGISTERABOT_RELAY_URL"))

        log.info("capabilities_show", slug=slug, skill=skill, items=len(items),
                 states=[i["state"] for i in items], session=session_id[:8])
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(
                    f"{relay}/bots/{slug}/ui",
                    json={"session_id": session_id, "ops": ops},
                    headers={"Authorization": f"Bearer {api_key}",
                             "Content-Type": "application/json"},
                )
        except Exception as e:
            return ToolResult.fail(f"Could not reach the relay: {e}")
        if resp.status_code != 200:
            return ToolResult.fail(f"Relay returned {resp.status_code}: {resp.text[:250]}")

        # Returned so the bot can TALK about what is on screen without restating it as
        # layout. It knows a log is running; it does not know, and must not say, that a
        # tile is lit.
        return ToolResult.ok({
            "shown": [{"id": i["id"], "title": i["title"], "state": i["state"]}
                      for i in items],
            "block_id": block["id"],
            "region": region,
            "status": "on the visitor's page" + coerced,
            "note": ("Refer to these by name. To narrow to one, emit "
                     "{op:'focus', target:'" + block["id"] + "', item:'<id>'} — "
                     "the page owns the transition."),
        })
