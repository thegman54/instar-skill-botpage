"""
image_create — make a picture, and publish it so the page can show it.

The chat surface refuses outside URLs, so a bot with no image source can only ever
describe things. `botpage_image` solved half of that — it can publish bytes — and left
the obvious question of where bytes come from.

They come from our own GPU. The map expert already ships an on-demand ComfyUI stack with
SDXL on it (`map-imagen`), spun up by StackManager and idled back down. Nothing about it
is map-specific; it was simply the only thing that had ever asked for it.

This generates, then PUBLISHES, and returns the asset name. The publish is not a
convenience: a tool result is capped at 24KB and a 1024px PNG is a megabyte, so handing
the image back through the conversation is not an option. The image has to land
somewhere the page can reach, and the name is what crosses the wire.

    image_create(prompt="a red bicycle against a white wall", name="bicycle")
    ui_emit(ops=[{"op":"upsert_block","region":"stream",
                  "block":{"type":"image","id":"i1","asset":"bicycle",
                           "alt":"A red bicycle leaning on a white wall"}}])
"""

import asyncio
import base64
import json
import os
import re
import time
import uuid

import httpx
import structlog

from ..base import BaseTool, ToolResult
from ..registry import register_tool
from .botpage_publish import relay_origin

log = structlog.get_logger()

STACK = "map-imagen"
COMFY = "http://map-imagen:8188"
EXECUTOR = os.environ.get("TOOL_EXECUTOR_URL", "http://localhost:8081")
CHECKPOINT = "sd_xl_base_1.0.safetensors"
NAME_OK = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# SDXL is trained at ~1 megapixel. Going smaller does not just crop, it degrades badly,
# so these are the three shapes rather than a free width/height the bot can get wrong.
SIZES = {
    "square":    (1024, 1024),
    "landscape": (1216, 832),
    "portrait":  (832, 1216),
}

# Everything the stack has to do before it can draw: container up, ComfyUI boot, then a
# 6.5GB checkpoint off disk into VRAM on the first generation. Cold is minutes, warm is
# seconds, and the difference is entirely the first call.
STACK_WAIT_S = 180
GEN_WAIT_S = 300


def _graph(prompt: str, negative: str, w: int, h: int, steps: int, seed: int) -> dict:
    """A plain SDXL txt2img graph in ComfyUI's API format.

    Written out rather than loaded from a workflow file on purpose: a workflow file
    lives in the map-imagen DEPLOY TARGET, which the installer overwrites. A graph this
    small belongs in the code that depends on it.
    """
    return {
        "1": {"class_type": "CheckpointLoaderSimple",
              "inputs": {"ckpt_name": CHECKPOINT}},
        "2": {"class_type": "CLIPTextEncode",
              "inputs": {"text": prompt, "clip": ["1", 1]}},
        "3": {"class_type": "CLIPTextEncode",
              "inputs": {"text": negative, "clip": ["1", 1]}},
        "4": {"class_type": "EmptyLatentImage",
              "inputs": {"width": w, "height": h, "batch_size": 1}},
        "5": {"class_type": "KSampler",
              "inputs": {"seed": seed, "steps": steps, "cfg": 7.0,
                         "sampler_name": "dpmpp_2m", "scheduler": "karras",
                         "denoise": 1.0, "model": ["1", 0],
                         "positive": ["2", 0], "negative": ["3", 0],
                         "latent_image": ["4", 0]}},
        "6": {"class_type": "VAEDecode",
              "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        "7": {"class_type": "SaveImage",
              "inputs": {"filename_prefix": "instar", "images": ["6", 0]}},
    }


@register_tool
class ImageCreateTool(BaseTool):
    """Generate an image on our own GPU and publish it to this bot's page."""

    @property
    def name(self) -> str:
        return "image_create"

    @property
    def description(self) -> str:
        return (
            "Make an image from a description, on our own GPU, and publish it so your "
            "page can show it. Returns the asset name — then render it with "
            "{type:'image', asset:'<name>', alt:'...'}.\n\n"
            "This is how you show someone a picture of something that does not already "
            "exist as a file. You cannot fetch one from the open web; the page refuses "
            "outside URLs because a foreign host would be handed the visitor's IP.\n\n"
            "Describe what should be IN the image, not what you want it to feel like. "
            "'A red bicycle leaning against a white wall, morning light' works. "
            "'Something cool about cycling' does not.\n\n"
            "The first call after an idle period is slow — a GPU stack has to start and "
            "load a 6.5GB model. Say you are making it before you call this."
        )

    @property
    def short_description(self) -> str:
        return "Generate an image and publish it"

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "What is in the image. Concrete subject, setting, "
                                   "lighting. Not a mood.",
                },
                "name": {
                    "type": "string",
                    "description": "Short asset name to publish under: lowercase "
                                   "letters, digits, - or _. Name it for what it SHOWS.",
                },
                "negative": {
                    "type": "string",
                    "description": "What to keep out (optional). Defaults cover the "
                                   "usual artefacts.",
                },
                "size": {
                    "type": "string", "enum": ["square", "landscape", "portrait"],
                    "description": "Default square.",
                },
                "steps": {
                    "type": "integer",
                    "description": "20 is fine and fast. 30+ is slower for little gain.",
                },
            },
            "required": ["prompt", "name"],
        }

    def _binding_key(self) -> str:
        slug = (self._profile_slug or "").strip().upper().replace("-", "_")
        return f"BINDING_REGISTERABOT_{slug}_API_KEY" if slug else "REGISTERABOT_API_KEY"

    def credential_keys(self) -> list[str]:
        return [self._binding_key(), "REGISTERABOT_RELAY_URL"]

    async def _ensure_stack(self, client: httpx.AsyncClient) -> str | None:
        """Bring the GPU stack up if it is down. Returns an error string, or None."""
        try:
            r = await client.get(f"{COMFY}/system_stats", timeout=5.0)
            if r.status_code == 200:
                return None                       # already warm
        except Exception:
            pass

        try:
            await client.post(f"{EXECUTOR}/stacks/{STACK}/start", timeout=120.0)
        except Exception as e:
            return f"could not ask the executor to start {STACK}: {e}"

        deadline = time.time() + STACK_WAIT_S
        while time.time() < deadline:
            try:
                r = await client.get(f"{COMFY}/system_stats", timeout=5.0)
                if r.status_code == 200:
                    return None
            except Exception:
                pass
            await asyncio.sleep(3)
        return (f"{STACK} did not come up within {STACK_WAIT_S}s. It is an optional GPU "
                f"stack — if the GPU or disk is unavailable the preflight blocks it.")

    async def execute(self, prompt: str = "", name: str = "", negative: str = "",
                      size: str = "square", steps: int = 20, **kwargs) -> ToolResult:
        slug = self._profile_slug
        if not slug:
            return ToolResult.fail("No profile slug on this session.")
        if not str(prompt).strip():
            return ToolResult.fail("prompt is required — describe what is in the image.")
        if not NAME_OK.match(name or ""):
            return ToolResult.fail(
                f"{name!r} is not a usable asset name — lowercase letters, digits, - or _. "
                f"It becomes part of a URL.")
        w, h = SIZES.get(size, SIZES["square"])
        steps = max(8, min(int(steps or 20), 40))
        neg = negative or "blurry, low quality, watermark, text, signature, deformed"

        api_key = self.get_credential(self._binding_key())
        if not api_key:
            return ToolResult.fail(f"{self._binding_key()} is not set.")
        relay = relay_origin(self.get_credential("REGISTERABOT_RELAY_URL"))

        async with httpx.AsyncClient(timeout=60.0) as client:
            err = await self._ensure_stack(client)
            if err:
                return ToolResult.fail(err)

            seed = uuid.uuid4().int % (2 ** 31)
            try:
                r = await client.post(f"{COMFY}/prompt", json={
                    "prompt": _graph(prompt, neg, w, h, steps, seed),
                    "client_id": f"instar-{slug}",
                })
            except Exception as e:
                return ToolResult.fail(f"Could not reach the image stack: {e}")
            if r.status_code != 200:
                return ToolResult.fail(
                    f"The image stack refused the job ({r.status_code}): {r.text[:300]}")
            pid = (r.json() or {}).get("prompt_id")
            if not pid:
                return ToolResult.fail("The image stack accepted the job but gave no id.")

            # Poll. ComfyUI reports completion by the job appearing in /history with
            # outputs attached; there is no push.
            deadline = time.time() + GEN_WAIT_S
            out = None
            while time.time() < deadline:
                await asyncio.sleep(2)
                try:
                    h2 = await client.get(f"{COMFY}/history/{pid}", timeout=10.0)
                    if h2.status_code != 200:
                        continue
                    entry = (h2.json() or {}).get(pid)
                    if not entry:
                        continue
                    for node in (entry.get("outputs") or {}).values():
                        imgs = node.get("images") or []
                        if imgs:
                            out = imgs[0]
                            break
                    if out:
                        break
                except Exception:
                    continue
            if not out:
                return ToolResult.fail(
                    f"The image did not finish within {GEN_WAIT_S}s. The stack is up, so "
                    f"this is generation being slow rather than missing — try fewer steps.")

            try:
                img = await client.get(f"{COMFY}/view", params={
                    "filename": out.get("filename"),
                    "subfolder": out.get("subfolder", ""),
                    "type": out.get("type", "output"),
                }, timeout=60.0)
                data = img.content
            except Exception as e:
                return ToolResult.fail(f"Generated it but could not read it back: {e}")
            if not data or not data.startswith(b"\x89PNG"):
                return ToolResult.fail("The stack returned something that is not a PNG.")

            # Publish. A 1MB image cannot ride back through a 24KB tool result, so the
            # only useful thing to return is a name the page can resolve.
            b64 = base64.b64encode(data).decode()
            try:
                p = await client.put(
                    f"{relay}/bots/{slug}/assets",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={"assets": {name: b64}}, timeout=120.0)
            except Exception as e:
                return ToolResult.fail(f"Made the image but could not publish it: {e}")
            if p.status_code != 200:
                return ToolResult.fail(
                    f"Made the image but the relay refused it ({p.status_code}): "
                    f"{p.text[:200]}")
            body = p.json()
            if not (body.get("stored") or []):
                rej = (body.get("rejected") or [{}])[0].get("why", "no reason given")
                return ToolResult.fail(f"Made the image but it was not stored: {rej}")

        log.info("image_create", slug=slug, name=name, bytes=len(data),
                 size=size, steps=steps)
        return ToolResult.ok({
            "published": name,
            "bytes": len(data),
            "dimensions": f"{w}x{h}",
            "show_it_with": {"type": "image", "asset": name, "alt": "<describe it>"},
            "note": "Write real alt text. This page is voice-first.",
        })
