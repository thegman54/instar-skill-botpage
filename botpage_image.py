"""
botpage_image — publish pictures the page is allowed to show.

The chat surface refuses outside URLs. That is deliberate: a foreign host handed an
<img> src learns the visitor's IP and user-agent every time the page repaints, and a
bot should not be able to arrange that by accident. The consequence is that a bot with
no published images can only ever describe things.

This is the way in. Publish bytes here, get a name back, then point an image block at
it:

    botpage_image(action="put", name="mars", image_base64="iVBOR...")
    ui_emit(ops=[{"op":"upsert_block","region":"stream",
                  "block":{"type":"image","id":"i_mars","asset":"mars",
                           "alt":"Mars, rust-red, south polar cap visible"}}])

Publishing is separate from `botpage_publish` on purpose. Publishing a page replaces
the whole document; adding one picture mid-conversation must not take the page with it.
"""

import base64
import binascii
import re

import httpx
import structlog

from ..base import BaseTool, ToolResult
from ..registry import register_tool
from .botpage_publish import relay_origin

log = structlog.get_logger()

# Mirrors ASSET_NAME in relay/src/bot-profile.ts. A name is a path segment under
# /b/{slug}/a/, so it is validated at both ends.
NAME_OK = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
MAX_BYTES = 3 * 1024 * 1024
# PNG, JPEG, GIF, WebP — the four the relay sniffs for. Checked here too so the bot is
# told "that is not an image" instead of watching it silently not appear.
MAGIC = (
    (b"\x89PNG", "png"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"GIF8", "gif"),
    (b"RIFF", "webp"),
)


@register_tool
class BotPageImageTool(BaseTool):
    """Publish, list and remove the images this bot can show."""

    @property
    def name(self) -> str:
        return "botpage_image"

    @property
    def description(self) -> str:
        return (
            "Publish an image so you can show it on your page, list what you have "
            "already published, or delete one.\n\n"
            "You cannot show a picture from the open internet — the page refuses "
            "outside URLs, because a foreign host would be handed the visitor's IP "
            "every time the page repaints. Anything you want to show has to be "
            "published here first.\n\n"
            "put: give a short name and the image as base64. The name is how you "
            "refer to it afterwards: {type:'image', asset:'<name>'} in ui_emit.\n"
            "list: what you have published, and how close you are to the cap.\n"
            "delete: remove one you no longer need.\n\n"
            "PNG, JPEG, GIF or WebP, up to 3MB, 64 images in total."
        )

    @property
    def short_description(self) -> str:
        return "Publish images the page may show"

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["put", "list", "delete"],
                    "description": "put an image, list what exists, or delete one",
                },
                "name": {
                    "type": "string",
                    "description": (
                        "Short name you will refer to it by: lowercase letters, digits, "
                        "- or _. Name it for what it SHOWS ('mars', 'floorplan_2f'), "
                        "not when you made it — you have to recognise it later."
                    ),
                },
                "image_base64": {
                    "type": "string",
                    "description": "The image bytes, base64. A data: URL prefix is fine.",
                },
            },
            "required": ["action"],
        }

    def _binding_key(self) -> str:
        slug = (self._profile_slug or "").strip().upper().replace("-", "_")
        return f"BINDING_REGISTERABOT_{slug}_API_KEY" if slug else "REGISTERABOT_API_KEY"

    def credential_keys(self) -> list[str]:
        return [self._binding_key(), "REGISTERABOT_RELAY_URL"]

    async def execute(self, action: str = "list", name: str = "",
                      image_base64: str = "", **kwargs) -> ToolResult:
        slug = self._profile_slug
        if not slug:
            return ToolResult.fail("No profile slug on this session.")
        if action not in ("put", "list", "delete"):
            return ToolResult.fail("action must be put, list or delete")

        key_name = self._binding_key()
        api_key = self.get_credential(key_name)
        if not api_key:
            return ToolResult.fail(f"{key_name} is not set.")
        relay = relay_origin(self.get_credential("REGISTERABOT_RELAY_URL"))
        base = f"{relay}/bots/{slug}/assets"
        headers = {"Authorization": f"Bearer {api_key}"}

        if action in ("put", "delete"):
            if not name:
                return ToolResult.fail("name is required for %s" % action)
            if not NAME_OK.match(name):
                return ToolResult.fail(
                    f"{name!r} is not a usable name — lowercase letters, digits, - or _, "
                    f"up to 64 characters. It becomes part of a URL.")

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                if action == "list":
                    r = await client.get(base, headers=headers)
                elif action == "delete":
                    r = await client.delete(f"{base}/{name}", headers=headers)
                else:
                    raw = image_base64 or ""
                    if "," in raw[:64] and raw.lstrip().startswith("data:"):
                        raw = raw[raw.index(",") + 1:]
                    if not raw.strip():
                        return ToolResult.fail("image_base64 is empty — nothing to publish.")
                    try:
                        data = base64.b64decode(raw, validate=False)
                    except (binascii.Error, ValueError) as e:
                        return ToolResult.fail(f"image_base64 is not decodable: {e}")
                    if not data:
                        return ToolResult.fail("image_base64 decoded to zero bytes.")
                    if len(data) > MAX_BYTES:
                        return ToolResult.fail(
                            f"{len(data)} bytes; the cap is {MAX_BYTES} (3MB). Resize it.")
                    if not any(data.startswith(m) for m, _ in MAGIC):
                        return ToolResult.fail(
                            "Those bytes are not a PNG, JPEG, GIF or WebP. The relay "
                            "checks the bytes, not the name, so a mislabelled file is "
                            "refused rather than stored.")
                    r = await client.put(base, headers=headers, json={"assets": {name: raw}})
        except Exception as e:
            return ToolResult.fail(f"Could not reach the relay: {e}")

        if r.status_code != 200:
            return ToolResult.fail(f"Relay returned {r.status_code}: {r.text[:250]}")
        try:
            body = r.json()
        except Exception:
            return ToolResult.fail(f"Relay returned unreadable output: {r.text[:200]}")

        if action == "list":
            names = body.get("assets") or []
            return ToolResult.ok({
                "images": names, "count": len(names), "max": body.get("max"),
                "how_to_show": ("{type:'image', asset:'<name>', alt:'...'} in ui_emit"
                                if names else "nothing published yet"),
            })
        if action == "delete":
            return ToolResult.ok({"deleted": body.get("deleted", name)})

        stored = body.get("stored") or []
        if not stored:
            rej = body.get("rejected") or []
            why = rej[0].get("why") if rej else "the relay stored nothing and said no more"
            return ToolResult.fail(f"{name} was not stored: {why}")
        log.info("botpage_image_put", slug=slug, name=name, bytes=len(data))
        return ToolResult.ok({
            "published": stored[0],
            "show_it_with": {"type": "image", "asset": stored[0], "alt": "<describe it>"},
            "note": "Give alt text when you show it. The page is voice-first.",
        })
