"""
chat_link_create — mint a private chat link to this bot, for one named person.

The bot is already talking to someone in an authenticated interface (Slack, Zoom).
It mints a link there, for that person, and shares it. When they open it the page
already knows who they are, because the link remembers.

Two tiers, which is the whole point:

  * This returns a LINK token — durable, revocable, and it cannot talk to the bot.
    Its only power is exchanging itself for a short-lived visitor token.
  * The visitor token is minted by the relay when the page loads, lives ~15 minutes,
    and is the thing the socket actually accepts.

That split is why a link can sit in a Slack thread for a week and still work, while
the credential that reaches the bot expires in minutes.

**A link is a bearer credential.** Whoever opens it is treated as the subject. Send
it in a DM, not a channel, or everyone in that channel is greeted as Ross. Use
`bind_on_first_use` for anything sensitive so a forwarded copy stops working.

The link personalises; it does not authorise. Tool access still comes from `hints`
(passphrases the gatekeeper strips and turns into grants) and from the profile's
tool belt — the subject's name grants nothing on its own.
"""

from typing import Any, Optional

import httpx
import structlog

from ..base import BaseTool, ToolResult
from ..registry import register_tool
from .botpage_publish import relay_origin

log = structlog.get_logger()


@register_tool
class ChatLinkCreateTool(BaseTool):
    """Mint a personalised chat link to this bot."""

    @property
    def name(self) -> str:
        return "chat_link_create"

    @property
    def description(self) -> str:
        return (
            "Create a private chat link to yourself for one specific person, and get a "
            "URL you can share with them. The link remembers who it was made for, so "
            "when they open it you already know their name and whatever context you "
            "record now. Use it when you want to continue a conversation somewhere "
            "richer than the current interface, or hand someone a way to reach you. "
            "Send the link directly to that person — anyone who opens it is treated as "
            "them. Set bind_on_first_use for anything sensitive."
        )

    @property
    def short_description(self) -> str:
        return "Mint a personalised chat link to this bot"

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "subject_label": {
                    "type": "string",
                    "description": "Who this link is for, as you'd greet them: 'Ross'.",
                },
                "subject_ref": {
                    "type": "string",
                    "description": (
                        "Stable id for this person if you have one (slack user id, email) "
                        "so you can thread memory across their visits."
                    ),
                },
                "context": {
                    "type": "object",
                    "description": (
                        "What you want to remember about them when they arrive — role, "
                        "what you were just discussing, tone to use. You read this on "
                        "arrival; it is never shown to them directly."
                    ),
                },
                "hints": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Passphrases to unlock tools for that conversation. These grant "
                        "real capability — include only what the visit needs."
                    ),
                },
                "label": {
                    "type": "string",
                    "description": "Your own reference name for this link, for later review.",
                },
                "ttl_hours": {
                    "type": "integer",
                    "description": (
                        "How long the link stays valid. Default 168 (a week). Use a short "
                        "value for a one-off. 0 means never expires — avoid that."
                    ),
                    "default": 168,
                },
                "bind_on_first_use": {
                    "type": "boolean",
                    "description": (
                        "Pin the link to the first device that opens it. A forwarded copy "
                        "then stops working. Use for anything sensitive."
                    ),
                    "default": False,
                },
            },
            "required": ["subject_label"],
        }

    def _binding_key(self) -> str:
        slug = (self._profile_slug or "").strip().upper().replace("-", "_")
        return f"BINDING_REGISTERABOT_{slug}_API_KEY" if slug else "REGISTERABOT_API_KEY"

    def credential_keys(self) -> list[str]:
        return [self._binding_key(), "REGISTERABOT_RELAY_URL"]

    async def execute(self, subject_label: str, **kwargs) -> ToolResult:
        slug = self._profile_slug
        if not slug:
            return ToolResult.fail("No profile slug on this session — cannot tell which bot to link to.")

        key_name = self._binding_key()
        api_key = self.get_credential(key_name)
        if not api_key:
            return ToolResult.fail(f"{key_name} is not set; this bot cannot mint links to itself.")

        relay = relay_origin(self.get_credential("REGISTERABOT_RELAY_URL"))

        hints = kwargs.get("hints")
        payload: dict[str, Any] = {
            "subject_label": subject_label,
            "bind_on_first_use": bool(kwargs.get("bind_on_first_use", False)),
        }
        for field in ("subject_ref", "label"):
            v = kwargs.get(field)
            if isinstance(v, str) and v.strip():
                payload[field] = v.strip()
        if isinstance(kwargs.get("context"), dict):
            payload["context"] = kwargs["context"]
        if isinstance(hints, list) and hints:
            payload["hints"] = [h for h in hints if isinstance(h, str)][:8]
        ttl = kwargs.get("ttl_hours")
        if isinstance(ttl, int):
            payload["ttl_hours"] = ttl

        url = f"{relay}/bots/{slug}/chat-links"
        log.info("chat_link_create", slug=slug, subject=subject_label,
                 bind=payload["bind_on_first_use"], hints=len(payload.get("hints", [])))

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    url, json=payload,
                    headers={"Authorization": f"Bearer {api_key}",
                             "Content-Type": "application/json"},
                )
        except Exception as e:
            return ToolResult.fail(f"Could not reach the relay at {relay}: {e}")

        if resp.status_code == 401:
            return ToolResult.fail(
                f"The relay rejected the key (401). {key_name} is not a valid key for '{slug}'."
            )
        if resp.status_code != 200:
            return ToolResult.fail(f"Relay returned {resp.status_code}: {resp.text[:300]}")

        data = resp.json()
        result = {
            "url": data.get("url"),
            "for": subject_label,
            "expires_at": data.get("expires_at") or "never",
            "bind_on_first_use": data.get("bind_on_first_use"),
            "reminder": (
                "Send this to that person directly. Anyone who opens it is treated as "
                f"{subject_label}."
            ),
        }
        if payload.get("hints"):
            result["unlocks_tools"] = "yes — this link carries passphrases"
        return ToolResult.ok(result)
