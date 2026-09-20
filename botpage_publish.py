"""
botpage_publish — the bot publishes its own public page.

The page at relay.registerabot.com/b/{slug} is rendered from a document the bot
PUTs. Nobody writes marketing copy for it: the bot pushes what it actually is, so
the page cannot drift from the bot.

The document is STRUCTURED — the bot picks a layout and theme tokens, it never
sends markup. That is enforced on the relay for a specific reason recorded there:
MySpace allowed raw CSS/HTML on the main origin and produced the Samy worm. This
tool validates the same constraints locally so a bad document fails with a useful
message instead of a 413 or a silently dropped field.

See project-instar docs/BOT_DRIVEN_UI.md (§0b of _INDEX.md for how this relates to
the live `ui` frames that mutate the page mid-conversation).
"""

import json
from typing import Any, Optional

import httpx
import structlog

from ..base import BaseTool, ToolResult
from ..registry import register_tool

log = structlog.get_logger()

DEFAULT_RELAY = "https://relay.registerabot.com"

# Mirrors relay/src/bot-profile.ts. Kept in sync by hand; the relay is authoritative
# and will reject anything these miss.
LAYOUTS = {"bubbles", "poster", "comic"}
FONTS = {"sans", "serif", "mono"}
MODES = {"light", "dark"}
BACKDROPS = {"nodes", "photo"}
MAX_DOC = 64 * 1024          # relay rejects above this
MAX_PROMPTS = 12             # relay slices to 12; trim here so the bot is told
MAX_SKILLS = 12


@register_tool
class BotPagePublishTool(BaseTool):
    """Publish (or refresh) this bot's public page."""

    @property
    def name(self) -> str:
        return "botpage_publish"

    @property
    def description(self) -> str:
        return (
            "Publish your own public page at relay.registerabot.com/b/{your-slug}. "
            "You describe yourself in a structured document — name, tagline, blurb, "
            "call to action, example prompts, skills, stats, footer — and pick a "
            "layout and theme. You never send HTML or CSS; you choose from the "
            "available layouts (bubbles, poster, comic), fonts (sans, serif, mono) "
            "and colours. Publish when your identity or capabilities change, so the "
            "page reflects what you actually are rather than drifting from it."
        )

    @property
    def short_description(self) -> str:
        return "Publish this bot's own public page"

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Display name"},
                "tagline": {"type": "string", "description": "One short line under the name"},
                "blurb": {"type": "string", "description": "A paragraph on what you are and do"},
                "cta": {"type": "string", "description": "Call to action, e.g. 'Ask me something'"},
                "prompts": {
                    "type": "array",
                    "description": "Example prompts a visitor can click. Max 12.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string"},
                            "tint": {"type": "string", "description": "Optional hex colour"},
                        },
                        "required": ["text"],
                    },
                },
                "skills": {
                    "type": "array",
                    "description": "What you can actually do. Max 12.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "blurb": {"type": "string"},
                            "color": {"type": "string"},
                            "count": {"type": "integer"},
                            "icon": {"type": "string"},
                        },
                        "required": ["name"],
                    },
                },
                "stats": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"label": {"type": "string"}},
                        "required": ["label"],
                    },
                },
                "footer": {"type": "string"},
                "theme": {
                    "type": "object",
                    "properties": {
                        "mode": {"type": "string", "enum": sorted(MODES)},
                        "accent": {"type": "string", "description": "Hex colour"},
                        "accent2": {"type": "string", "description": "Hex colour"},
                        "backdrop": {"type": "string", "enum": sorted(BACKDROPS)},
                        "layout": {
                            "type": "string",
                            "enum": sorted(LAYOUTS),
                            "description": (
                                "bubbles — soft light scene, wants a cut-out portrait with "
                                "real alpha. poster — ink on paper, wants flat/illustrated "
                                "art. comic — a newspaper strip, one panel per skill with "
                                "clickable speech balloons."
                            ),
                        },
                        "font": {"type": "string", "enum": sorted(FONTS)},
                    },
                },
                "public": {
                    "type": "boolean",
                    "description": "If false the page renders 'keeps a private profile'. Default true.",
                    "default": True,
                },
            },
            "required": ["name"],
        }

    def credential_keys(self) -> list[str]:
        return ["REGISTERABOT_API_KEY", "REGISTERABOT_RELAY_URL"]

    # --- validation -------------------------------------------------------

    def _clean_theme(self, theme: Optional[dict], warnings: list[str]) -> dict:
        if not isinstance(theme, dict):
            return {}
        out: dict[str, Any] = {}
        for key, allowed in (("layout", LAYOUTS), ("font", FONTS),
                             ("mode", MODES), ("backdrop", BACKDROPS)):
            v = theme.get(key)
            if v is None:
                continue
            if v in allowed:
                out[key] = v
            else:
                warnings.append(f"theme.{key}={v!r} is not one of {sorted(allowed)} — dropped")
        for key in ("accent", "accent2"):
            v = theme.get(key)
            if v is None:
                continue
            # The relay's colour() re-validates; this is to tell the bot, not to trust it.
            if isinstance(v, str) and v.startswith("#") and 4 <= len(v) <= 9:
                out[key] = v
            else:
                warnings.append(f"theme.{key}={v!r} is not a hex colour — dropped")
        return out

    async def execute(self, name: str, **kwargs) -> ToolResult:
        slug = self._profile_slug
        if not slug:
            return ToolResult.fail(
                "No profile slug on this session — the tool cannot tell which bot it is, "
                "and a page must be published for a specific bot."
            )

        api_key = self.get_credential("REGISTERABOT_API_KEY")
        if not api_key:
            return ToolResult.fail(
                "REGISTERABOT_API_KEY is not set for this bot. The relay scopes a key to "
                "its own slug, so each bot needs its own key to publish its own page."
            )
        relay = (self.get_credential("REGISTERABOT_RELAY_URL") or DEFAULT_RELAY).rstrip("/")

        warnings: list[str] = []
        profile: dict[str, Any] = {"name": name}

        for field in ("tagline", "blurb", "cta", "footer"):
            v = kwargs.get(field)
            if isinstance(v, str) and v.strip():
                profile[field] = v.strip()

        prompts = kwargs.get("prompts")
        if isinstance(prompts, list) and prompts:
            if len(prompts) > MAX_PROMPTS:
                warnings.append(f"{len(prompts)} prompts given; the page shows {MAX_PROMPTS}")
            profile["prompts"] = prompts[:MAX_PROMPTS]

        skills = kwargs.get("skills")
        if isinstance(skills, list) and skills:
            if len(skills) > MAX_SKILLS:
                warnings.append(f"{len(skills)} skills given; trimmed to {MAX_SKILLS}")
            profile["skills"] = skills[:MAX_SKILLS]

        stats = kwargs.get("stats")
        if isinstance(stats, list) and stats:
            profile["stats"] = stats

        theme = self._clean_theme(kwargs.get("theme"), warnings)
        if theme:
            profile["theme"] = theme

        profile["public"] = bool(kwargs.get("public", True))

        body = {"profile": profile}
        size = len(json.dumps(body))
        if size > MAX_DOC:
            return ToolResult.fail(
                f"Document is {size} bytes; the relay caps it at {MAX_DOC}. "
                "Shorten the blurb or drop some prompts."
            )

        url = f"{relay}/bots/{slug}/profile"
        log.info("botpage_publish", slug=slug, bytes=size, layout=theme.get("layout"))

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.put(
                    url,
                    json=body,
                    headers={"Authorization": f"Bearer {api_key}",
                             "Content-Type": "application/json"},
                )
        except Exception as e:
            log.error("botpage_publish_failed", slug=slug, error=str(e))
            return ToolResult.fail(f"Could not reach the relay at {relay}: {e}")

        if resp.status_code == 401:
            return ToolResult.fail(
                "The relay rejected the key (401). A bot's key only authorises its own "
                f"slug — check the key set for '{slug}' is that bot's own key."
            )
        if resp.status_code != 200:
            return ToolResult.fail(
                f"Relay returned {resp.status_code}: {resp.text[:300]}"
            )

        try:
            data = resp.json()
        except Exception:
            data = {}

        page_url = data.get("url") or f"{relay}/b/{slug}"
        result = {
            "url": page_url,
            "slug": slug,
            "bytes": size,
            "public": profile["public"],
            "layout": theme.get("layout", "bubbles (default)"),
        }
        if warnings:
            result["warnings"] = warnings
        # The page renders from this document immediately; no cache purge needed for
        # the HTML, but Cloudflare may hold a previous 404 at the edge for a short
        # while if the page was fetched before it existed.
        result["note"] = (
            "Live now. If you fetched this URL before publishing, the edge may serve a "
            "cached 404 briefly — add a query string to bypass it."
        )
        return ToolResult.ok(result)
