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

import re
from typing import Any, Optional

import httpx
import structlog

from ..base import BaseTool, ToolResult
from ..registry import register_tool
from .botpage_publish import relay_origin

log = structlog.get_logger()

# Mirrors the applyOps whitelist in relay/src/chat-surface.ts. The browser is
# authoritative; these exist so a bad op fails here with an explanation.
OPS = {"set_theme", "transition", "say", "clear", "upsert_block",
       "style", "animate", "move_block"}

# Mirrors STYLE_OK in relay/src/chat-surface.ts. The security boundary in CSS is values
# and selectors, not properties — a block only ever sets its own inline style, so no
# selector is reachable. Values carrying url(), expression(), javascript: or a second
# declaration are rejected at both ends.
STYLE_PROPS = {
    "color", "backgroundColor", "opacity", "filter", "transform", "transformOrigin",
    "borderColor", "borderWidth", "borderStyle", "borderRadius", "boxShadow", "outline",
    "padding", "paddingTop", "paddingRight", "paddingBottom", "paddingLeft",
    "margin", "marginTop", "marginRight", "marginBottom", "marginLeft", "gap",
    "width", "height", "maxWidth", "minWidth", "maxHeight", "minHeight",
    "fontSize", "fontWeight", "fontStyle", "lineHeight", "letterSpacing", "textAlign",
    "textTransform", "textDecoration", "whiteSpace", "fontFamily",
    "display", "flexDirection", "alignItems", "justifyContent", "flexWrap",
    "position", "top", "right", "bottom", "left", "zIndex", "overflow", "mixBlendMode",
    "backdropFilter", "background",
}
BAD_VALUE = re.compile(r"url\(|expression|javascript:|@import|</|\\|;\s*[a-z-]+\s*:", re.I)

# transform and opacity are composited off the main thread. Animating layout properties
# forces reflow, which visibly janks the text stream and the audio playing alongside it.
CHEAP_ANIM = {"transform", "opacity", "filter", "backdropFilter"}

# Named effects the page knows. Every one is built only from transform, opacity and
# filter, so the safe, smooth options are also the easy ones to reach for.
EFFECTS = [
    "fade_in", "fade_out", "dissolve", "dissolve_out",
    "slide_up", "slide_down", "slide_left", "slide_right",
    "zoom_in", "zoom_out", "pop", "bounce", "shake", "pulse", "flip", "drift_in",
]
BLOCKS = {"text", "heading", "list", "json", "image", "video"}

# Mirrors mediaSrc() in relay/src/chat-surface.ts. Media is the one block family that
# makes the page fetch something, and a fetch is a disclosure: whatever host serves the
# bytes learns the visitor's IP, user-agent and referrer every time the page repaints.
# So a source is never a free-form URL. It is one of:
#   asset='hero'       -> /b/{slug}/a/hero, published by the bot, already size-capped
#   src='/stream/abc'  -> a same-origin path this relay serves (e.g. a media proxy)
#   src='https://host' -> only if host is in MEDIA_ORIGINS
#
# MEDIA_ORIGINS is EMPTY on purpose, and the browser's copy is empty too. Adding a host
# here alone changes nothing — the browser is authoritative and will still refuse it.
# Both ends have to be edited together, which is the point: it makes "let the bot show
# me anything off the internet" a deliberate act rather than a default.
MEDIA_ORIGINS: set[str] = set()
ASSET_OK = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
REGIONS = {"stream", "rail", "hero", "footer", "layer"}
ANCHORS = {"top-left", "top-center", "top-right",
           "center-left", "center", "center-right",
           "bottom-left", "bottom-center", "bottom-right"}
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
            "Set the theme, put text, a list, JSON, an image or a video into the main "
            "area or the side rail, clear a region. Only works when the visitor is on a "
            "chat surface (a /c/ link) — it does nothing in Slack or Zoom.\n\n"
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
                "rationale": {
                    "type": "string",
                    "description": (
                        "One sentence, in your own words, on why this shape beats saying "
                        "it. Recorded, never shown to the visitor. (Named `rationale` "
                        "because `why` is reserved by the tool layer for every tool.)"
                    ),
                },
                "ops": {
                    "type": "array",
                    "description": (
                        "Ops applied in order.\n"
                        "  {op:'set_theme', tokens:{color:{bg,ink,soft,accent,surface}, font:'sans|serif|mono'}}\n"
                        "  {op:'transition', duration_ms:120-1200}\n"
                        "  {op:'say', text:'...'} — a line in the main area\n"
                        "  {op:'clear', region:<region>}\n"
                        "  {op:'upsert_block', region:<region>, block:{...}}\n"
                        "     regions: stream (centre), rail (right panel), hero (top),\n"
                        "              footer (bottom), layer (free placement, stacked)\n"
                        "     block types: {type:'text',id,text} {type:'heading',id,text}\n"
                        "                  {type:'list',id,items:[...],ordered?} {type:'json',id,value}\n"
                        "                  {type:'image',id,asset|src,alt,caption?,fit?}\n"
                        "                  {type:'video',id,asset|src,poster?,caption?,\n"
                        "                                controls?,autoplay?,muted?,loop?}\n"
                        "     MEDIA SOURCES are not free-form URLs. Either:\n"
                        "       asset:'hero'      — an asset you published for this bot\n"
                        "       src:'/path'       — a same-origin path this relay serves\n"
                        "     An outside https:// URL is REFUSED unless allowlisted: a\n"
                        "     foreign host would learn the visitor's IP every repaint.\n"
                        "     image: always give alt. This page is voice-first, so the\n"
                        "       visitor who cannot see it is the likeliest one here.\n"
                        "     video: controls are ON unless you turn them off. autoplay\n"
                        "       forces muted — browsers refuse autoplay with sound — so\n"
                        "       if the sound matters, let them press play instead.\n"
                        "       Re-sending the same video id with the same source keeps\n"
                        "       playing; it does not restart. Change the source to reset.\n"
                        "     any block may carry style:{...} — see below\n"
                        "  {op:'style', target:'<block id>', style:{...}}\n"
                        "     colour, spacing, radius, shadow, filter, type, layout.\n"
                        "  {op:'animate', target:'<block id>', effect:'<name>',\n"
                        "                 duration?, delay?, easing?, iterations?}\n"
                        "     effects: fade_in fade_out dissolve dissolve_out slide_up\n"
                        "              slide_down slide_left slide_right zoom_in zoom_out\n"
                        "              pop bounce shake pulse flip drift_in\n"
                        "     or pass your own keyframes:[{...},{...}] instead of effect\n"
                        "     keyframes are plain objects. Animate transform and opacity:\n"
                        "     they run off the main thread. Animating width/top/height\n"
                        "     forces layout every frame and stutters your own speech.\n"
                        "  {op:'move_block', target:'<block id>', region:<region>, place?:{...}}\n"
                        "     the browser morphs it from the old position to the new one.\n"
                        "  Free placement (region 'layer'): give the block a place object —\n"
                        "     {anchor:'top-right', x:'24px', y:'80px', w:'320px', z:5}\n"
                        "     anchors: top/center/bottom x left/center/right. z stacks 0-99.\n"
                        "     Layers float over everything; use them for things that should\n"
                        "     sit beside the conversation rather than inside it.\n"
                        "Blocks carry a stable id you choose, so you can update the same "
                        "block later instead of adding another one."
                    ),
                    "items": {"type": "object"},
                },
            },
            "required": ["motive", "rationale", "ops"],
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
                if block.get("type") in ("image", "video"):
                    problems.extend(self._media_problems(i, block))
            if name == "upsert_block":
                block = op.get("block") or {}
                place = block.get("place")
                if place is not None:
                    if not isinstance(place, dict):
                        problems.append(f"op {i} place must be an object")
                    else:
                        a = place.get("anchor")
                        if a is not None and a not in ANCHORS:
                            problems.append(
                                f"op {i} anchor {a!r} is not one of {sorted(ANCHORS)}")
                        if op.get("region") != "layer":
                            problems.append(
                                f"op {i} has a place but region is "
                                f"{op.get('region','stream')!r} — placement only applies "
                                f"in region 'layer'")
            if name in ("upsert_block", "clear", "move_block"):
                region = op.get("region", "stream")
                if region not in REGIONS:
                    problems.append(f"op {i} region {region!r} is not one of {sorted(REGIONS)} — dropped")
                    continue
            if name in ("style", "animate", "move_block"):
                if not op.get("target"):
                    problems.append(f"op {i} {name} needs a target block id — dropped")
                    continue
            if name == "style":
                bad = self._bad_style(op.get("style"))
                if bad:
                    problems.append(f"op {i} style: {bad}")
            if name == "animate":
                eff = op.get("effect")
                kf = op.get("keyframes")
                if eff is not None and eff not in EFFECTS:
                    problems.append(
                        f"op {i} effect {eff!r} is not one of {EFFECTS} — the page will "
                        f"ignore it")
                if eff in EFFECTS and not isinstance(kf, list):
                    clean.append(op)
                    continue
                if not isinstance(kf, list) or not kf:
                    problems.append(
                        f"op {i} animate needs either effect:'<name>' or a keyframes "
                        f"array — dropped")
                    continue
                heavy = set()
                for frame in kf:
                    if isinstance(frame, dict):
                        bad = self._bad_style({k: v for k, v in frame.items()
                                               if k not in ("offset", "easing")})
                        if bad:
                            problems.append(f"op {i} keyframe: {bad}")
                        heavy |= {k for k in frame
                                  if k in STYLE_PROPS and k not in CHEAP_ANIM}
                if heavy:
                    problems.append(
                        f"op {i} animates {sorted(heavy)}, which forces layout on every "
                        f"frame and will stutter the text and audio. Prefer transform "
                        f"and opacity.")
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

    def _media_problems(self, i: int, block: dict) -> list[str]:
        """Explain anything the page will refuse or quietly change about a media block.

        The browser enforces all of this independently; these messages exist so the bot
        learns WHY nothing appeared, instead of emitting the same rejected URL forever.
        """
        out: list[str] = []
        kind = block.get("type")
        asset, src = block.get("asset"), block.get("src")

        if asset is not None:
            if not isinstance(asset, str) or not ASSET_OK.match(asset) or ".." in asset:
                out.append(
                    f"op {i} asset {asset!r} is not a plain published asset name — refused")
        elif isinstance(src, str) and src:
            low = src.lower()
            if src.startswith("//"):
                out.append(
                    f"op {i} src {src[:40]!r} is protocol-relative, which resolves to a "
                    f"foreign host — refused. Use a path starting with a single '/'.")
            elif src.startswith("/"):
                pass                                     # same-origin, fine
            elif low.startswith("https://"):
                host = src[8:].split("/", 1)[0].lower()
                if host not in MEDIA_ORIGINS:
                    out.append(
                        f"op {i} src host {host!r} is not in the media allowlist — refused "
                        f"by the browser. Publish it as an asset, or serve it from this "
                        f"origin, rather than hotlinking it.")
            else:
                out.append(
                    f"op {i} src {src[:40]!r} is refused — only a same-origin path or an "
                    f"allowlisted https host. data:, blob: and http: are all rejected.")
        else:
            out.append(f"op {i} {kind} block has neither 'asset' nor 'src' — nothing to show")

        if kind == "image" and not str(block.get("alt") or "").strip():
            out.append(
                f"op {i} image has no alt text. This page is voice-first; a visitor who "
                f"cannot see it is the one most likely to be here. It still renders.")
        if kind == "video":
            if block.get("autoplay") and not block.get("muted"):
                out.append(
                    f"op {i} video sets autoplay without muted. Every browser refuses "
                    f"autoplay with sound, so the page mutes it to make it play at all. "
                    f"If the sound matters, drop autoplay and let them press play.")
            if block.get("controls") is False:
                out.append(
                    f"op {i} video sets controls:false, so it cannot be paused. Fine for "
                    f"an ambient loop; hostile for anything a visitor is meant to watch.")
        return out

    def _bad_style(self, style: Any) -> Optional[str]:
        """Explain why a style object would be rejected, or None if it is fine."""
        if style is None:
            return "no style object"
        if not isinstance(style, dict):
            return "style must be an object"
        unknown = [k for k in style if k not in STYLE_PROPS]
        for k, v in style.items():
            if isinstance(v, str) and BAD_VALUE.search(v):
                return f"value for {k} contains something the page refuses (url/expression/injection)"
            if isinstance(v, str) and len(v) > 200:
                return f"value for {k} is too long"
        if unknown:
            return f"unknown properties {sorted(unknown)} — dropped by the page"
        return None

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

    async def execute(self, motive: str, rationale: str, ops: Any, **kwargs) -> ToolResult:
        slug = self._profile_slug
        if not slug:
            return ToolResult.fail("No profile slug on this session.")
        if motive not in MOTIVES:
            return ToolResult.fail(f"motive must be one of {MOTIVES}")
        if not isinstance(rationale, str) or len(rationale.strip()) < 8:
            return ToolResult.fail(
                "rationale must be a sentence explaining why this shape beats saying it.")

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
        log.info("ui_emit", slug=slug, motive=motive, rationale=rationale.strip(),
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
