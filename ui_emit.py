"""
ui_emit — the bot repaints the page it is talking on.

The visitor is looking at a live surface, not a transcript. This sends ops to it:
theme, text, blocks in regions. They land mid-sentence, while the bot is still
talking, on the same socket carrying its words.

Two things are deliberate and worth not "fixing" later:

**A motive is required.** Every call must say why this layout, for this answer.
Not decoration — the reason is what stops the bot reaching for the same shape
every turn. A bot that cannot say why it is rendering a list should be speaking
instead. The motive is recorded, never displayed.

**It is a vocabulary, not a canvas.** No HTML, no CSS, no positions. The bot picks
from ops and block types the page knows how to render; anything else is dropped at
the browser. That is what makes it safe to let a model drive a page at all.
"""

from typing import Any, Optional

import httpx
import structlog

from ..base import BaseTool, ToolResult
from ..registry import register_tool
from .botpage_publish import relay_origin

log = structlog.get_logger()

# Mirrors the applyOps whitelist in relay/src/chat-surface.ts. The browser is
# authoritative; these exist so a bad op fails here with an explanation.
OPS = {"set_theme", "transition", "say", "clear", "upsert_block"}
BLOCKS = {"text", "heading", "list", "json"}
REGIONS = {"stream", "rail"}
COLOR_TOKENS = {"bg", "ink", "soft", "accent", "surface"}
FONTS = {"sans", "serif", "mono"}

MOTIVES = [
    "data_is_the_answer",   # the content is a structure; speaking it would fail
    "parallel_points",      # 3+ items that are peers, not prose
    "reference_while_talking",  # they need it visible while the bot keeps going
    "state_change",         # mood/topic/severity shifted and the page should say so
    "working",              # a long operation; the work is the content
    "greeting",             # arrival — set the tone for this specific visitor
]


@register_tool
class UiEmitTool(BaseTool):
    """Change the page the visitor is looking at."""

    @property
    def name(self) -> str:
        return "ui_emit"

    @property
    def description(self) -> str:
        return (
            "Change the page the visitor is looking at, right now, mid-sentence. "
            "Set the theme, put text or a list or JSON into the main area or the side "
            "rail, clear a region. Only works when the visitor is on a chat surface "
            "(a /c/ link) — it does nothing in Slack or Zoom.\n\n"
            "You must give a motive: why THIS layout for THIS answer. If you cannot "
            "name one, say the thing out loud instead of rendering it.\n\n"
            "Do not render every turn. The default is to just talk. Show something "
            "when the shape of the answer is not a sentence: data, parallel points, "
            "something they need to keep looking at while you continue."
        )

    @property
    def short_description(self) -> str:
        return "Repaint the visitor's page mid-turn"

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "motive": {
                    "type": "string",
                    "enum": MOTIVES,
                    "description": (
                        "Why this layout, for this answer. "
                        "data_is_the_answer — the content is a structure and speaking it "
                        "would fail. parallel_points — three or more peer items. "
                        "reference_while_talking — they need it visible while you keep "
                        "going. state_change — topic, mood or severity shifted. "
                        "working — a long operation, the work is the content. "
                        "greeting — arrival, set the tone for this visitor."
                    ),
                },
                "why": {
                    "type": "string",
                    "description": (
                        "One sentence, in your own words, on why this shape beats saying "
                        "it. Recorded, never shown to the visitor."
                    ),
                },
                "ops": {
                    "type": "array",
                    "description": (
                        "Ops applied in order.\n"
                        "  {op:'set_theme', tokens:{color:{bg,ink,soft,accent,surface}, font:'sans|serif|mono'}}\n"
                        "  {op:'transition', duration_ms:120-1200}\n"
                        "  {op:'say', text:'...'} — a line in the main area\n"
                        "  {op:'clear', region:'stream'|'rail'}\n"
                        "  {op:'upsert_block', region:'stream'|'rail', block:{...}}\n"
                        "     block types: {type:'text',id,text} {type:'heading',id,text}\n"
                        "                  {type:'list',id,items:[...],ordered?} {type:'json',id,value}\n"
                        "Blocks carry a stable id you choose, so you can update the same "
                        "block later instead of adding another one."
                    ),
                    "items": {"type": "object"},
                },
            },
            "required": ["motive", "why", "ops"],
        }

    def _binding_key(self) -> str:
        slug = (self._profile_slug or "").strip().upper().replace("-", "_")
        return f"BINDING_REGISTERABOT_{slug}_API_KEY" if slug else "REGISTERABOT_API_KEY"

    def credential_keys(self) -> list[str]:
        return [self._binding_key(), "REGISTERABOT_RELAY_URL"]

    # --- validation (the browser is authoritative; this explains failures) ----

    def _check_ops(self, ops: Any) -> tuple[list[dict], list[str]]:
        problems: list[str] = []
        clean: list[dict] = []
        if not isinstance(ops, list) or not ops:
            return [], ["ops must be a non-empty array"]
        if len(ops) > 64:
            problems.append(f"{len(ops)} ops given; the relay caps it at 64 — trimmed")
            ops = ops[:64]

        for i, op in enumerate(ops):
            if not isinstance(op, dict):
                problems.append(f"op {i} is not an object — dropped")
                continue
            name = op.get("op")
            if name not in OPS:
                problems.append(f"op {i} '{name}' is not one of {sorted(OPS)} — dropped")
                continue
            if name == "upsert_block":
                block = op.get("block")
                if not isinstance(block, dict):
                    problems.append(f"op {i} upsert_block has no block — dropped")
                    continue
                if block.get("type") not in BLOCKS:
                    problems.append(
                        f"op {i} block type {block.get('type')!r} is not one of "
                        f"{sorted(BLOCKS)} — dropped")
                    continue
                if not block.get("id"):
                    problems.append(
                        f"op {i} block has no id — it can never be updated, only added to")
            if name in ("upsert_block", "clear"):
                region = op.get("region", "stream")
                if region not in REGIONS:
                    problems.append(f"op {i} region {region!r} is not one of {sorted(REGIONS)} — dropped")
                    continue
            if name == "set_theme":
                colors = (op.get("tokens") or {}).get("color") or {}
                unknown = [k for k in colors if k not in COLOR_TOKENS]
                if unknown:
                    problems.append(
                        f"op {i} unknown colour tokens {unknown} — the page ignores these. "
                        f"Known: {sorted(COLOR_TOKENS)}")
                font = (op.get("tokens") or {}).get("font")
                if font and font not in FONTS:
                    problems.append(f"op {i} font {font!r} is not one of {sorted(FONTS)}")
            clean.append(op)
        return clean, problems

    async def _relay_session_id(self) -> Optional[str]:
        """The visitor's relay session.

        The registerabot adapter sets the gatekeeper conversation_id to
        'registerabot:{relay_session}', so the relay session is recoverable from the
        session this tool is running in. A conversation that did not come from that
        adapter has no visitor page, and this returns None.
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
            log.warning("ui_emit_session_lookup_failed", error=str(e))
            return None
        prefix = "registerabot:"
        return conv[len(prefix):] if conv.startswith(prefix) else None

    async def execute(self, motive: str, why: str, ops: Any, **kwargs) -> ToolResult:
        slug = self._profile_slug
        if not slug:
            return ToolResult.fail("No profile slug on this session.")
        if motive not in MOTIVES:
            return ToolResult.fail(f"motive must be one of {MOTIVES}")
        if not isinstance(why, str) or len(why.strip()) < 8:
            return ToolResult.fail(
                "why must be a sentence explaining why this shape beats saying it.")

        clean, problems = self._check_ops(ops)
        if not clean:
            return ToolResult.fail("No usable ops. " + " ".join(problems))

        session_id = await self._relay_session_id()
        if not session_id:
            return ToolResult.fail(
                "This conversation is not on a chat surface, so there is no page to "
                "change. ui_emit only works when the visitor came through a /c/ link."
            )

        key_name = self._binding_key()
        api_key = self.get_credential(key_name)
        if not api_key:
            return ToolResult.fail(f"{key_name} is not set.")
        relay = relay_origin(self.get_credential("REGISTERABOT_RELAY_URL"))

        # The corpus. motive + why + the shape actually rendered is the training signal
        # for the presentation layer: what the bot chose, and why it thought so. The
        # executor already persists tool-call arguments, so every call is a labelled
        # example without a second store — this log line just makes it greppable live.
        log.info("ui_emit", slug=slug, motive=motive, why=why.strip(),
                 ops=len(clean), shapes=[o.get("op") for o in clean],
                 blocks=[(o.get("block") or {}).get("type") for o in clean
                         if o.get("op") == "upsert_block"],
                 session=session_id[:8])

        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(
                    f"{relay}/bots/{slug}/ui",
                    json={"session_id": session_id, "ops": clean},
                    headers={"Authorization": f"Bearer {api_key}",
                             "Content-Type": "application/json"},
                )
        except Exception as e:
            return ToolResult.fail(f"Could not reach the relay: {e}")

        if resp.status_code != 200:
            return ToolResult.fail(f"Relay returned {resp.status_code}: {resp.text[:250]}")

        result: dict[str, Any] = {
            "applied": len(clean),
            "motive": motive,
            # "accepted" not "displayed": the relay routes it, and reports nothing back
            # about whether a socket was actually listening.
            "status": "sent to the visitor's page",
        }
        if problems:
            result["problems"] = problems
        return ToolResult.ok(result)
