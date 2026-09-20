"""
botpage_status — is this bot's page published, and what does it currently say?

The relay has no "read my document back" endpoint; the document is only observable
through the rendered page. So this fetches the page and reports what it can tell
from the response, which is enough to answer the two questions that matter: does
the page exist, and is it public.
"""

import httpx
import structlog

from ..base import BaseTool, ToolResult
from ..registry import register_tool

log = structlog.get_logger()

DEFAULT_RELAY = "https://relay.registerabot.com"


@register_tool
class BotPageStatusTool(BaseTool):
    """Check whether this bot's public page is published."""

    @property
    def name(self) -> str:
        return "botpage_status"

    @property
    def description(self) -> str:
        return (
            "Check whether your public page is published, and get its URL. "
            "Use before publishing to see whether you already have a page, or "
            "after publishing to confirm it went live."
        )

    @property
    def short_description(self) -> str:
        return "Is this bot's public page live?"

    @property
    def input_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    def credential_keys(self) -> list[str]:
        return ["REGISTERABOT_RELAY_URL"]

    async def execute(self, **kwargs) -> ToolResult:
        slug = self._profile_slug
        if not slug:
            return ToolResult.fail("No profile slug on this session.")

        relay = (self.get_credential("REGISTERABOT_RELAY_URL") or DEFAULT_RELAY).rstrip("/")
        # Cache-bust: the edge can hold a 404 from before the page was published,
        # which would otherwise report "not published" for a page that is live.
        url = f"{relay}/b/{slug}"
        try:
            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
                resp = await client.get(url, params={"_cb": "status"})
        except Exception as e:
            return ToolResult.fail(f"Could not reach the relay at {relay}: {e}")

        body = resp.text or ""
        if resp.status_code == 200 and "keeps a private profile" in body:
            return ToolResult.ok({
                "published": True, "public": False, "url": url,
                "message": "Published, but marked private — visitors see a placeholder.",
            })
        if resp.status_code == 200:
            return ToolResult.ok({
                "published": True, "public": True, "url": url,
                "bytes": len(body),
                "message": "Published and public.",
            })
        if resp.status_code == 404:
            return ToolResult.ok({
                "published": False, "url": url,
                "message": "No page published yet — call botpage_publish to create one.",
            })
        return ToolResult.fail(f"Relay returned {resp.status_code} for {url}")
