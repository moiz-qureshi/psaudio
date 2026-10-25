"""crcmz MCP for psaudio-buddy: read-only squad knowledge for the voice AI.

A minimal Streamable-HTTP MCP client (JSON-RPC over POST) for app.crcmz.me/mcp,
authenticated with your personal crcmz token (Settings -> MCP). Only *read*
tools are ever offered to the model: the voice can be triggered by anyone in
party chat saying "Hey Hasaan", so it must never be able to post as you. Write
tools are blocked by name and by pattern, even if someone ticks them.
"""
import asyncio
import json
import logging
import re
import time

import httpx

log = logging.getLogger("buddy")

# Side-effect tools in crcmz (assistant._WRITE_TOOLS), plus a pattern for new ones.
WRITE_TOOLS = {
    "app_event_record", "clawbot_build", "coach_review_record", "ig_post_record", "ig_reel_share",
    "memory_reindex", "montage_record", "ring_squad", "send_mattermost_channel_message",
    "send_mattermost_dm", "send_psn_group_message", "send_whatsapp_dm", "send_whatsapp_group_message",
    "slap_add_song", "task_claim", "task_complete", "task_release", "task_submit",
    "video_post_record", "video_post_skip",
}
WRITE_PATTERN = re.compile(
    r"^(send|ring|post|create|delete|update|set|add|remove|claim|submit|share|build|invite)_"
    r"|_(record|claim|complete|submit|release|build|share|skip|reindex|send|post|delete)$"
    r"|_add_|_send_|_post_")

# What a gaming buddy needs to talk and roast; editable on the AI page.
DEFAULT_TOOLS = [
    "squad_roster", "person_profile", "squad_members", "squad_leaderboard", "squad_facts",
    "psn_squad_status", "steam_squad_status", "games_played", "game_sessions",
    "whatsapp_awards", "whatsapp_stats", "whatsapp_search", "whatsapp_words", "memory_search",
    "recent_clips", "tourney_leaderboard", "tourney_player_stats", "coach_player_profile", "slap_stats",
]
MAX_RESULT = 4000          # characters of a tool result handed to the model (cost + latency)


def is_write(name):
    return name in WRITE_TOOLS or bool(WRITE_PATTERN.search(name))


class MCPError(Exception):
    pass


class CrcmzMCP:
    def __init__(self, url, token):
        self.url, self.token = url, token
        self.session_id = None
        self.ready = False
        self.server_instructions = ""
        self._tools, self._tools_t = None, 0.0
        self._id = 0
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(12, connect=4))

    async def close(self):
        await self.client.aclose()

    async def _post(self, msg):
        h = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json",
             "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18"}
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        r = await self.client.post(self.url, json=msg, headers=h)
        if r.status_code in (401, 403):
            raise MCPError("crcmz refused the token (expired or revoked?)")
        if r.status_code == 202 or "id" not in msg:
            return None
        r.raise_for_status()
        self.session_id = r.headers.get("mcp-session-id") or self.session_id
        if r.headers.get("content-type", "").startswith("text/event-stream"):
            data = [l[5:].strip() for l in r.text.splitlines() if l.startswith("data:")]
            body = json.loads(data[-1]) if data else {}
        else:
            body = r.json()
        if "error" in body:
            raise MCPError(body["error"].get("message", "MCP error"))
        return body.get("result", {})

    async def rpc(self, method, params=None):
        self._id += 1
        return await self._post({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}})

    async def start(self):
        res = await self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                            "clientInfo": {"name": "psaudio-buddy", "version": "1"}})
        self.server_instructions = res.get("instructions", "")
        await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.ready = True

    async def tools(self, force=False):
        """All tools the token can see, write tools removed (cached 10 min)."""
        if force or self._tools is None or time.monotonic() - self._tools_t > 600:
            if not self.ready:
                await self.start()
            res = await self.rpc("tools/list")
            self._tools = [t for t in res.get("tools", []) if not is_write(t.get("name", ""))]
            self._tools_t = time.monotonic()
        return self._tools

    async def call(self, name, args):
        if is_write(name):
            return "refused: this voice assistant is read-only"
        t = time.monotonic()
        try:
            res = await self.rpc("tools/call", {"name": name, "arguments": args or {}})
        except (httpx.HTTPError, MCPError) as e:
            return f"error: {type(e).__name__}: {str(e)[:160]}"
        text = "\n".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
        log.info("mcp %s(%s) %.0f ms, %d chars", name, json.dumps(args)[:80],
                 (time.monotonic() - t) * 1000, len(text))
        if len(text) > MAX_RESULT:
            text = text[:MAX_RESULT] + " ...[truncated]"
        return ("error: " if res.get("isError") else "") + text


def clean_schema(schema):
    """MCP JSON Schema -> what Gemini function declarations / OpenAI tools accept."""
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}
    out = {}
    for k, v in schema.items():
        if k in ("$schema", "additionalProperties", "title", "examples", "$id", "default"):
            continue
        if k == "properties" and isinstance(v, dict):
            out[k] = {pk: clean_schema(pv) for pk, pv in v.items()}
        elif k == "items":
            out[k] = clean_schema(v)
        else:
            out[k] = v
    if out.get("type") == "object":
        out.setdefault("properties", {})
    return out


def selected(all_tools, chosen):
    names = set(chosen or DEFAULT_TOOLS)
    return [t for t in all_tools if t["name"] in names and not is_write(t["name"])]


def gemini_declarations(tools):
    from google.genai import types
    return [types.FunctionDeclaration(name=t["name"], description=(t.get("description") or "")[:900],
                                      parameters_json_schema=clean_schema(t.get("inputSchema")))
            for t in tools]


def openai_tools(tools):
    return [{"type": "function", "function": {"name": t["name"],
                                              "description": (t.get("description") or "")[:900],
                                              "parameters": clean_schema(t.get("inputSchema"))}}
            for t in tools]


TOOL_HINT = (" You can look things up in the squad's CRCMZ app (members, profiles, stats, "
             "leaderboards, PSN/Steam status, WhatsApp history, clips, tournaments, memories) "
             "with your tools. Use them whenever someone asks about a squad member or squad "
             "history, and use what you find to roast them properly. Call squad_roster first "
             "if you need to work out who a name refers to. Never read out phone numbers, "
             "addresses or private messages word for word; summarise and roast instead.")
