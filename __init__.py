"""pixel-office plugin — a pixel-art virtual office for Hermes agents.

Every Hermes agent (main sessions AND delegate_task subagents) shows up as
an animated pixel character sitting at a desk in a tiny office, rendered in
your browser at http://127.0.0.1:8113 (port configurable).

Design:

* Hooks are pure observers — they never block, veto, or transform anything.
  Each hook appends one JSON line to ``~/.hermes/pixel-office/events.jsonl``.
  Appends are O(1) and wrapped in try/except, so the agent loop never pays
  more than a few microseconds.

* A daemon HTTP server thread is started lazily on the first event. It
  serves the office page and ``/state``, which folds the event log into a
  current-agents snapshot. Because state is derived from the shared event
  file (not process memory), agents from OTHER Hermes processes (gateway +
  CLI at once, cron sessions) appear in the same office. If the port is
  already bound, another Hermes process is serving — we just keep appending
  events and skip serving.

* The event log is trimmed when it exceeds ~512 KB (keeps the newest half),
  so it never grows unbounded.

Leaderboard, currency & P2P:

* Every tool call earns *Ops* (the office currency), weighted by how long the
  call really takes (see ``TOOL_OPS``). Cumulative Ops, tool-call count,
  sessions run and time run are folded from the event log into a persistent,
  append-only, signed hash-chained ledger (``ledger.jsonl``). Each block
  carries an Ed25519 signature from the instance that produced it, so every
  instance can verify every other instance's contribution — a p2p
  blockchain-like scoreboard with no central server. The leaderboard is the
  union of all signed snapshots.

* GitHub identity: connect a GitHub username in the office; the plugin
  resolves it through the public GitHub API so the correct display name,
  avatar and profile link appear on every profile card. ``DrGekoz`` gets a
  DEV badge.

* A chatbox posts signed messages into a shared append-only chat log so all
  instances in the office can talk to each other.

Configuration (all optional, config.yaml):

    plugins:
      entries:
        pixel-office:
          port: 8113        # HTTP port for the office page
          enabled: true

Nothing here touches the conversation, the prompt cache, or tool results.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import socket
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8113
_MAX_LOG_BYTES = 512 * 1024
_MAX_LEDGER_BYTES = 1024 * 1024
# An agent with no events for this long is swept from the office.
_STALE_SECONDS = 30 * 60
# How many tool calls a "session start" event must precede before we count a
# new session run for an agent (avoids counting every tool burst).
_SESSION_GAP_SECONDS = 60

_lock = threading.Lock()
_server_started = False
_port: int = DEFAULT_PORT
# Approval hooks don't carry session_id (only a gateway session_key), so we
# attribute them to the most recent session that fired a tool in this
# process — approvals always happen inside a tool dispatch.
_last_session_id: str = ""

# ---------------------------------------------------------------------------
# Currency: Ops earned per tool call (rebalanced to real tool-call cost)
# ---------------------------------------------------------------------------
# Cheap, instant reads.
TOOL_OPS = {
    "read_file": 1, "search_files": 1, "skill_view": 1, "skills_list": 1,
    "session_search": 1, "fact_store": 1, "fact_feedback": 1, "todo": 1,
    "memory": 1, "clarify": 1,
    # Cheap writes.
    "write_file": 2, "patch": 2, "skill_manage": 2,
    # Medium — terminal / process work.
    "process": 2, "terminal": 3,
    # Heavier compute / scripting.
    "execute_code": 4,
    # Web work.
    "web_search": 3, "web_extract": 3,
    # Expensive / slow.
    "text_to_speech": 5, "vision_analyze": 6, "image_generate": 10,
    "delegate_task": 8, "cronjob": 4,
}
_DEFAULT_OPS = 2


def _ops_for(tool: str) -> int:
    return TOOL_OPS.get(str(tool or ""), _DEFAULT_OPS)


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _office_dir() -> Path:
    d = get_hermes_home() / "pixel-office"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _events_path() -> Path:
    return _office_dir() / "events.jsonl"


def _ledger_path() -> Path:
    return _office_dir() / "ledger.jsonl"


def _chat_path() -> Path:
    return _office_dir() / "chat.jsonl"


def _identity_path() -> Path:
    return _office_dir() / "identity.json"


def _key_path() -> Path:
    return _office_dir() / "instance.key"


# Hermes' own session store (the same state.db the session_search tool and
# Hermes Desktop read). We report this real count instead of only the sessions
# observed in this office's event log, which only sees sessions that fired a
# hook while the plugin was running.
_SESSION_COUNT_CACHE = {"ts": 0.0, "n": None}


def _hermes_session_count() -> Optional[int]:
    """Return the number of sessions in Hermes' session DB (cached ~20s)."""
    global _SESSION_COUNT_CACHE
    now = time.time()
    if now - _SESSION_COUNT_CACHE["ts"] < 20:
        return _SESSION_COUNT_CACHE["n"]
    n = None
    try:
        import sqlite3
        db = get_hermes_home() / "state.db"
        if db.exists():
            conn = sqlite3.connect(str(db))
            try:
                n = int(conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])
            finally:
                conn.close()
    except Exception:
        n = None
    _SESSION_COUNT_CACHE = {"ts": now, "n": n}
    return n


# ---------------------------------------------------------------------------
# Instance Ed25519 key (for signing ledger blocks)
# ---------------------------------------------------------------------------

_KEY = None  # cache
_KEYID = None


def _load_or_create_key():
    """Return (ed25519 private_key, b64 public_key, keyid)."""
    global _KEY, _KEYID
    if _KEY is not None:
        return _KEY, _KEYID, _KEYID
    try:
        from cryptography.hazmat.primitives.asymmetric import ed25519
        from cryptography.hazmat.primitives import serialization

        key_path = _key_path()
        if key_path.exists():
            raw = key_path.read_bytes()
            priv = ed25519.Ed25519PrivateKey.from_private_bytes(raw)
        else:
            priv = ed25519.Ed25519PrivateKey.generate()
            key_path.write_bytes(
                priv.private_bytes(
                    serialization.Encoding.Raw,
                    serialization.PrivateFormat.Raw,
                    serialization.NoEncryption(),
                )
            )
        pub = priv.public_key()
        pub_b64 = base64.urlsafe_b64encode(
            pub.public_bytes(
                serialization.Encoding.Raw,
                serialization.PublicFormat.Raw,
            )
        ).decode("ascii").rstrip("=")
        kid = "i" + hashlib.sha256(pub_b64.encode()).hexdigest()[:12]
        _KEY = priv
        _KEYID = pub_b64
        return priv, pub_b64, kid
    except Exception as exc:
        logger.debug("pixel-office key init failed: %s", exc)
        return None, "", ""


def _sign(payload_str: str) -> str:
    """Return b64 signature of payload_str, or '' if crypto unavailable."""
    priv, _, _ = _load_or_create_key()
    if priv is None:
        return ""
    try:
        return base64.urlsafe_b64encode(priv.sign(payload_str.encode())).decode("ascii")
    except Exception as exc:
        logger.debug("pixel-office sign failed: %s", exc)
        return ""


def _verify(payload_str: str, sig_b64: str, pub_b64: str) -> bool:
    """Verify an Ed25519 signature against an embedded public key."""
    if not sig_b64 or not pub_b64:
        return False
    try:
        from cryptography.hazmat.primitives.asymmetric import ed25519
        from cryptography.hazmat.primitives.asymmetric import utils

        pub_bytes = base64.urlsafe_b64decode(pub_b64 + "=" * (-len(pub_b64) % 4))
        pub = ed25519.Ed25519PublicKey.from_public_bytes(pub_bytes)
        sig = base64.urlsafe_b64decode(sig_b64 + "=" * (-len(sig_b64) % 4))
        pub.verify(sig, payload_str.encode())
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# GitHub token + gist ledger broker
#
# The leaderboard ledger is a signed, append-only chain in ``ledger.jsonl``.
# It holds three block types:
#   identity  — who an instance is (signed)
#   score     — a cumulative per-user score snapshot (ops/tools/sessions/time)
#   chat      — a chat message (signed)
#
# Scores are read from the LATEST signed score block per user, so they survive
# any event-log trimming (they are NOT re-derived from the trimmable event log).
#
# The GitHub gist acts as the shared broker across machines (replaces a central
# DB — works over CGNAT since it's plain HTTPS). The sync loop pulls the shared
# ledger gist, merges it with the local ledger (latest-signed-score per user,
# de-dupe by hash), and pushes the merged superset back. The first instance
# online seeds the gist; everyone else pulls it when they come online.
# ---------------------------------------------------------------------------

def _token_path() -> Path:
    return _office_dir() / "github_token.txt"


def _load_token() -> str:
    """Load the GitHub token (gist scope) for login + signaling.

    Candidates, in order:
      1. the office dir github_token.txt,
      2. the legacy ~/.hermes/pixel-office/github_token.txt,
      3. `gh auth token` (the gh CLI's stored token).
    """
    try:
        p = _office_dir() / "github_token.txt"
        if p.exists():
            t = p.read_text(encoding="utf-8").strip()
            if t:
                return t
        legacy = Path.home() / ".hermes" / "pixel-office" / "github_token.txt"
        if legacy.exists():
            t = legacy.read_text(encoding="utf-8").strip()
            if t:
                return t
    except Exception:
        pass
    try:
        import subprocess
        out = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, timeout=8
        )
        t = (out.stdout or "").strip()
        if t:
            return t
    except Exception:
        pass
    return ""


def _gh_headers() -> Dict[str, str]:
    t = _load_token()
    return {
        "Authorization": f"Bearer {t}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "hermes-pixel-office",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _gh_get(url: str, timeout: float = 12):
    """JSON GET against the GitHub API (returns None on failure)."""
    import urllib.request
    try:
        req = urllib.request.Request(url, headers=_gh_headers())
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return None


def _gh_json(url: str, body: Dict[str, Any], method: str = "PATCH",
             timeout: float = 15):
    """JSON request against the GitHub API (returns decoded body or None).

    Gists: creation = POST /gists, update = PATCH /gists/{id}.
    """
    import urllib.request
    try:
        req = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"),
            headers=_gh_headers(), method=method,
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return None


def github_login() -> Optional[Dict[str, Any]]:
    """Resolve the authenticated GitHub user via the stored token (real login).

    Returns the GitHub profile dict (login, name, avatar_url, html_url, id)
    or None if no token / the token is invalid.
    """
    if not _load_token():
        return None
    return _gh_get("https://api.github.com/user")


def _gist_id() -> str:
    """Signaling gist id — from config or the local office file."""
    try:
        from hermes_cli.config import cfg_get, load_config

        g = cfg_get(load_config(), "plugins", "entries", "pixel-office", "ledger_gist")
        if g:
            return str(g)
    except Exception:
        pass
    try:
        f = _office_dir() / "gist_id.txt"
        if f.exists():
            g = f.read_text(encoding="utf-8").strip()
            if g:
                return g
    except Exception:
        pass
    return ""


def _set_gist_id(gid: str) -> None:
    # Primary persistence: a local file (robust regardless of config write).
    try:
        _office_dir().mkdir(parents=True, exist_ok=True)
        (_office_dir() / "gist_id.txt").write_text(gid, encoding="utf-8")
    except Exception:
        pass
    try:
        from hermes_cli.config import cfg_get, cfg_set, load_config
        cfg_set(load_config(), "plugins", "entries", "pixel-office", "ledger_gist", str(gid))
    except Exception:
        pass


def _ensure_gist() -> str:
    """Return the signaling gist id, creating it if needed. '' on failure."""
    gid = _gist_id()
    if gid:
        return gid
    if not _load_token():
        return ""
    body = _gh_json(
        "https://api.github.com/gists",
        {
            "description": "Hermes Pixel Office — WebRTC signaling (handshake only)",
            "public": False,
            "files": {"signaling.json": {"content": "{}"}},
        },
        method="POST",
    )
    if body and body.get("id"):
        _set_gist_id(body["id"])
        return str(body["id"])
    return ""


def _gist_pull() -> List[Dict[str, Any]]:
    """Fetch the shared ledger gist's ledger.jsonl lines. [] on failure."""
    gid = _gist_id()
    if not gid:
        return []
    data = _gh_get(f"https://api.github.com/gists/{gid}")
    if not data:
        return []
    files = data.get("files") or {}
    entry = files.get("ledger.jsonl") or {}
    content = entry.get("content") or ""
    out = []
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def _append_signed_block(data: Dict[str, Any]) -> None:
    """Append a signed, hash-chained block to ledger.jsonl (best-effort)."""
    try:
        ledger_path = _ledger_path()
        prev = "GENESIS"
        if ledger_path.exists():
            try:
                lines = ledger_path.read_text(encoding="utf-8", errors="replace").splitlines()
                if lines:
                    try:
                        prev = json.loads(lines[-1]).get("hash", "GENESIS")
                    except Exception:
                        prev = "GENESIS"
            except Exception:
                pass
        payload_str = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        block_hash = hashlib.sha256((prev + payload_str).encode()).hexdigest()
        block = {
            "hash": block_hash, "prev": prev,
            "payload": payload_str, "data": data,
            "pid": os.getpid(),
            "ts": time.time(),
        }
        priv, pub, _ = _load_or_create_key()
        block["pub"] = pub
        block["sig"] = _sign(payload_str)
        line = json.dumps(block, ensure_ascii=False, default=str)
        with _lock:
            with open(ledger_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            _maybe_trim_ledger(ledger_path)
    except Exception as exc:
        logger.debug("pixel-office ledger append failed: %s", exc)


# ---------------------------------------------------------------------------
# P2P signaling (GitHub gist, handshake only)
#
# Scores are NOT persisted and chat is NOT relayed through GitHub — both flow
# purely over a WebRTC data-channel mesh between browsers (exactly IdleViber's
# p2p.js pattern). GitHub's ONLY roles here:
#   * the login/identity layer (so a username gets set), and
#   * the WebRTC signaling handshake (presence + SDP offer/answer exchange)
#     so peers can find each other and open the mesh. No score/chat message
#     ever touches GitHub once the data channels are open.
# Each leaderboard ranks whatever scores it has received. No central DB.
# ---------------------------------------------------------------------------

def _signaling_path() -> Path:
    return _office_dir() / "signaling_cache.json"


def _peer_id() -> str:
    """Stable id for THIS office identity (GitHub username or 'local')."""
    ident = _load_identity() or {}
    return str(ident.get("github") or ident.get("name") or "local")


def _signaling_doc() -> Dict[str, Any]:
    """Read the shared signaling doc (peers + SDP mailboxes) from the gist.

    Falls back to a local cache when the gist is unreachable so the office
    still shows cached peer state offline.
    """
    gid = _gist_id()
    if gid:
        data = _gh_get(f"https://api.github.com/gists/{gid}")
        if data:
            entry = (data.get("files") or {}).get("signaling.json") or {}
            content = entry.get("content") or "{}"
            try:
                doc = json.loads(content)
                # refresh local cache
                try:
                    _signaling_path().write_text(
                        json.dumps(doc, ensure_ascii=False), encoding="utf-8")
                except Exception:
                    pass
                return doc if isinstance(doc, dict) else {}
            except Exception:
                pass
    try:
        if _signaling_path().exists():
            return json.loads(_signaling_path().read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _signaling_write(doc: Dict[str, Any]) -> bool:
    gid = _ensure_gist()
    if not gid:
        return False
    body = _gh_json(
        f"https://api.github.com/gists/{gid}",
        {"files": {"signaling.json": {
            "content": json.dumps(doc, ensure_ascii=False)}}},
    )
    ok = bool(body and "files" in body)
    if ok:
        try:
            _signaling_path().write_text(
                json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
    return ok


def _mutate_signaling(fn) -> bool:
    """Read the signaling doc, apply fn(doc) in place, write it back."""
    try:
        doc = _signaling_doc()
        fn(doc)
        return _signaling_write(doc)
    except Exception:
        return False


def signaling_register(online: bool) -> Dict[str, Any]:
    """Register/unregister this instance's presence in the shared signaling doc."""
    me = _peer_id()
    ident = _load_identity() or {}

    def _reg(doc):
        peers = doc.setdefault("peers", {})
        if online:
            peers[me] = {
                "github": ident.get("github", ""),
                "name": ident.get("name") or ident.get("github") or "you",
                "avatar": ident.get("avatar", ""),
                "url": ident.get("url", ""),
                "online": True,
                "last_seen": time.time(),
                "pid": os.getpid(),
                "offers": (peers.get(me) or {}).get("offers", {}),
                "answers": (peers.get(me) or {}).get("answers", {}),
            }
        else:
            peers.pop(me, None)

    _mutate_signaling(_reg)
    return signaling_state()


def signaling_state() -> Dict[str, Any]:
    """Return the signaling doc with me flagged (for the frontend)."""
    me = _peer_id()
    doc = _signaling_doc()
    peers = doc.get("peers", {})
    self_entry = peers.pop(me, None) or {}
    for p in peers.values():
        p.pop("offers", None)
        p.pop("answers", None)
    return {
        "me": me,
        "peers": peers,
        "offers": self_entry.get("offers", {}),
        "answers": self_entry.get("answers", {}),
        "ts": time.time(),
    }


def signaling_send_offer(to: str, sdp: Any) -> bool:
    """Write a WebRTC offer from me to peer `to`."""
    me = _peer_id()

    def _reg(doc):
        peers = doc.setdefault("peers", {})
        peer = peers.setdefault(to, {"online": False})
        peer.setdefault("offers", {})[me] = {"sdp": sdp, "ts": time.time()}
    return _mutate_signaling(_reg)


def signaling_send_answer(to: str, sdp: Any) -> bool:
    """Write a WebRTC answer from me to peer `to`."""
    me = _peer_id()

    def _reg(doc):
        peers = doc.setdefault("peers", {})
        peer = peers.setdefault(to, {"online": False})
        peer.setdefault("answers", {})[me] = {"sdp": sdp, "ts": time.time()}
    return _mutate_signaling(_reg)


def signaling_clear_inbox() -> None:
    """Clear my offer/answer mailboxes (after consuming them)."""
    me = _peer_id()

    def _reg(doc):
        p = doc.get("peers", {}).get(me)
        if p:
            p["offers"] = {}
            p["answers"] = {}
    _mutate_signaling(_reg)


# ---------------------------------------------------------------------------
# Event publishing (hook side — must be cheap and never raise)
# ---------------------------------------------------------------------------

def _publish(event: Dict[str, Any]) -> None:
    try:
        event.setdefault("ts", time.time())
        event.setdefault("pid", os.getpid())
        line = json.dumps(event, ensure_ascii=False, default=str)
        path = _events_path()
        with _lock:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            _maybe_trim(path)
        _ensure_server()
    except Exception as exc:  # observers must never break the loop — but say so
        logger.warning("pixel-office: failed to record event (%s: %s)",
                       type(exc).__name__, exc)


def _maybe_trim(path: Path) -> None:
    try:
        if path.stat().st_size <= _MAX_LOG_BYTES:
            return
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        keep = lines[len(lines) // 2:]
        tmp = path.with_suffix(".jsonl.tmp")
        tmp.write_text("\n".join(keep) + "\n", encoding="utf-8")
        tmp.replace(path)
    except Exception:
        logger.debug("pixel-office trim failed", exc_info=True)


# ---------------------------------------------------------------------------
# State folding (server side)
# ---------------------------------------------------------------------------

def _read_events() -> List[Dict[str, Any]]:
    path = _events_path()
    if not path.exists():
        return []
    out: List[Dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        logger.debug("pixel-office read failed", exc_info=True)
    return out


def _read_ledger() -> List[Dict[str, Any]]:
    path = _ledger_path()
    if not path.exists():
        return []
    out = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        pass
    return out


def _agent_key(ev: Dict[str, Any]) -> Optional[str]:
    sid = ev.get("session_id") or ev.get("child_session_id")
    if sid:
        return str(sid)
    # Fall back to pid so events without a session id still get a character.
    pid = ev.get("pid")
    return f"pid-{pid}" if pid else None


def _short(text: Any, n: int = 60) -> str:
    s = str(text or "").strip().replace("\n", " ")
    return s[: n - 1] + "…" if len(s) > n else s


def _load_tiers() -> List[Dict[str, Any]]:
    """Read (and cache) the tier ladder from web/tiers.js."""
    tiers = getattr(_load_tiers, "_cache", None)
    if tiers is None:
        tiers = []
        try:
            tj = Path(__file__).resolve().parent / "web" / "tiers.js"
            txt = tj.read_text(encoding="utf-8")
            idx = txt.index("[")
            end = txt.rindex("]")
            tiers = json.loads(txt[idx:end + 1])
        except Exception:
            tiers = [{"t": 1, "ops": 0, "name": "Herald Plank",
                      "icon": "001_Wood_Heraldic_Shield_orig462"}]
        _load_tiers._cache = tiers
    return tiers


def _tier_for_user(ops: float, calls: int, sessions: int) -> Dict[str, Any]:
    """Return the highest tier whose Ops + calls + sessions thresholds are all met.

    Reads web/tiers.js (window.OFFICE_TIERS) if present; else a tiny fallback.
    """
    tiers = _load_tiers()
    cur = tiers[0] if tiers else {"t": 1, "name": "—", "icon": ""}
    for t in tiers:
        if (float(t.get("ops", 0)) <= float(ops or 0)
                and float(t.get("calls", 0)) <= float(calls or 0)
                and float(t.get("sessions", 0)) <= float(sessions or 0)):
            cur = t
        else:
            break
    return cur


def _tier_for_ops(ops: float) -> Dict[str, Any]:
    """Back-compat: tier by Ops alone (warm the ladder cache / ops-only checks)."""
    return _tier_for_user(ops, 0, 0)



# Per-agent cumulative stats folded from the event log.
def _fold_agent_stats(events: List[Dict[str, Any]]):
    """Return {agent_key: {ops, tools, sessions, time_ms, label, kind, first, last}}."""
    stats: Dict[str, Dict[str, Any]] = {}
    last_tool_ts: Dict[str, float] = {}
    session_open: Dict[str, float] = {}
    session_count: Dict[str, int] = {}

    for ev in events:
        kind = ev.get("event")
        key = _agent_key(ev)
        if not key:
            continue
        s = stats.setdefault(key, {
            "ops": 0.0, "tools": 0, "sessions": 0, "time_ms": 0.0,
            "label": f"agent {key[-6:]}", "kind": "main",
            "pid": str(ev.get("pid") or ""),
            "first": ev.get("ts", time.time()), "last": ev.get("ts", time.time()),
        })
        if ev.get("pid"):
            s["pid"] = str(ev["pid"])
        ts = float(ev.get("ts") or 0)
        s["last"] = max(s["last"], ts)
        if kind == "session_start":
            s["label"] = f"{ev.get('platform') or 'hermes'} {key[-6:]}"
            session_open[key] = ts
            session_count[key] = session_count.get(key, 0) + 1
        elif kind == "session_end":
            if key in session_open:
                s["time_ms"] += max(0.0, ts - session_open[key]) * 1000.0
                session_open.pop(key, None)
        elif kind == "subagent_start":
            s["kind"] = "subagent"
            s["label"] = _short(ev.get("child_goal"), 26) or f"sub {key[-6:]}"
        elif kind == "tool_start":
            # Count a new session run if there's been a long quiet gap.
            gap = ts - last_tool_ts.get(key, 0)
            if gap > _SESSION_GAP_SECONDS and session_count.get(key, 0) == 0:
                session_count[key] = session_count.get(key, 0) + 1
            last_tool_ts[key] = ts
        elif kind == "tool_end":
            s["tools"] += 1
            s["ops"] += _ops_for(ev.get("tool_name"))
            # tool_end has no session_id on some paths; still attribute by key.
            gap = ts - last_tool_ts.get(key, 0)
            if gap > _SESSION_GAP_SECONDS and session_count.get(key, 0) == 0:
                session_count[key] = session_count.get(key, 0) + 1
            last_tool_ts[key] = ts

    # Any sessions still open count toward time.
    for key, st in session_open.items():
        if key in stats:
            s = stats[key]
            s["time_ms"] += max(0.0, time.time() - st) * 1000.0
    for key, s in stats.items():
        s["sessions"] = session_count.get(key, 1)
    return stats


def _fold_leaderboard(events: List[Dict[str, Any]], registry: Dict[str, Dict[str, Any]]):
    """Aggregate per-agent stats into a per-user leaderboard.

    registry: pid -> {name, avatar, url, github} from signed identity blocks.
    Every agent this machine produces belongs to the person running the office,
    so when a GitHub identity is connected ALL local agents fold into that one
    user row (the "User who controls all Hermes instances on this PC").
    Returns (leaderboard, stats_global) where leaderboard is sorted rows.
    """
    agent_stats = _fold_agent_stats(events)
    office_ident = _load_identity() or {}
    users: Dict[str, Dict[str, Any]] = {}
    for key, a in agent_stats.items():
        pid = str(a.get("pid", "")) if a.get("pid") else ""
        ident = registry.get(pid) or registry.get(key)
        if ident is None and office_ident.get("github"):
            # Fall back to the connected office identity for any un-mapped agent.
            ident = office_ident
        if not (ident and ident.get("github")):
            # No GitHub identity for this agent -> never show it on the
            # leaderboard. The board ranks PEOPLE (GitHub usernames) only,
            # never agents (cli / telegram / cron / subagent / etc). If the
            # person running this office isn't connected, their agents simply
            # don't appear until they connect a GitHub username.
            continue
        uid = ident["github"]
        u = users.setdefault(uid, {
            "id": uid, "name": ident.get("name") or uid,
            "github": uid,
            "avatar": ident.get("avatar", ""),
            "url": ident.get("url", ""),
            "ops": 0.0, "tools": 0, "sessions": 0, "time_ms": 0.0,
            "agents": [], "kind": "main", "first": time.time(), "last": 0.0,
        })
        u["ops"] += a["ops"]
        u["tools"] += a["tools"]
        u["sessions"] += a["sessions"]
        u["time_ms"] += a["time_ms"]
        u["agents"].append({"id": key, "label": a["label"], "kind": a["kind"]})
        u["first"] = min(u["first"], a["first"])
        u["last"] = max(u["last"], a["last"])


    rows = []
    for u in users.values():
        u["tier"] = _tier_for_user(u["ops"], u["tools"], u["sessions"])
        u["ops"] = round(u["ops"], 1)
        u["time_s"] = u["time_ms"] / 1000.0
        u["is_dev"] = u["github"].lower() == "drgekoz"
        rows.append(u)
    rows.sort(key=lambda r: (r["ops"], r["tools"]), reverse=True)
    for rank, r in enumerate(rows, 1):
        r["rank"] = rank

    # Attach each user's profile (bio, links, chosen tier icon, themes) so the
    # frontend + P2P score packets can render full profiles.
    profiles = _load_profiles()
    for r in rows:
        p = profiles.get(str(r.get("github") or "").lower())
        if p:
            r["bio"] = p.get("bio", "")
            r["links"] = p.get("links", {})
            r["tierIcon"] = p.get("tierIcon", "")
            r["profileTheme"] = p.get("profileTheme", {})
            r["interfaceTheme"] = p.get("interfaceTheme", {})

    # Global totals (whole office).
    g_ops = sum(r["ops"] for r in rows)
    g_tools = sum(r["tools"] for r in rows)
    g_sessions = sum(r["sessions"] for r in rows)
    g_time_ms = sum(r["time_ms"] for r in rows)
    global_stats = {
        "ops": round(g_ops, 1), "tools": g_tools, "sessions": g_sessions,
        "time_ms": g_time_ms, "users": len(rows),
    }
    return rows, global_stats


def _build_registry(ledger: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """pid -> identity from signed 'identity' blocks in the ledger."""
    reg: Dict[str, Dict[str, Any]] = {}
    for b in ledger:
        if not isinstance(b, dict):
            continue
        data = b.get("data") or {}
        if data.get("type") != "identity":
            continue
        payload_str = b.get("payload", "")
        if payload_str and not _verify(payload_str, b.get("sig", ""), b.get("pub", "")):
            continue  # reject tampered identity blocks
        reg[str(data.get("pid") or data.get("agent") or "")] = {
            "name": data.get("name"), "avatar": data.get("avatar", ""),
            "url": data.get("url", ""), "github": data.get("github", ""),
        }
    return reg


def build_state() -> Dict[str, Any]:
    """Fold the event log + signed ledger into the full office state."""
    now = time.time()
    events = _read_events()
    ledger = _read_ledger()
    registry = _build_registry(ledger)
    rows, global_stats = _fold_leaderboard(events, registry)
    identity = _load_identity()
    chat = _read_chat()

    # Report Hermes' real session count (the same store Hermes Desktop sees)
    # instead of only the sessions observed in this office's event log. All
    # local agents fold into the connected office identity, so its session
    # tally should equal Hermes' actual session count.
    real_sessions = _hermes_session_count()
    ident_gh = (identity or {}).get("github")
    if real_sessions is not None and ident_gh:
        for r in rows:
            if str(r.get("github") or "").lower() == str(ident_gh).lower():
                r["sessions"] = real_sessions
                r["tier"] = _tier_for_user(r["ops"], r["tools"], r["sessions"])
        global_stats["sessions"] = sum(r["sessions"] for r in rows)

    agents: Dict[str, Dict[str, Any]] = {}

    def ensure(key: str, ev: Dict[str, Any]) -> Dict[str, Any]:
        a = agents.get(key)
        if a is None:
            a = {
                "id": key, "label": f"agent {key[-6:]}", "kind": "main",
                "status": "idle", "tool": "", "activity": "", "detail": "",
                "platform": ev.get("platform") or "",
                "first_seen": ev.get("ts", now),
                "updated_at": ev.get("ts", now),
            }
            agents[key] = a
        a["updated_at"] = ev.get("ts", a["updated_at"])
        return a

    for ev in events:
        kind = ev.get("event")
        key = _agent_key(ev)
        if not key:
            continue
        if kind == "session_start":
            a = ensure(key, ev)
            a["status"] = "idle"
            plat = ev.get("platform") or ""
            a["label"] = f"{plat or 'hermes'} {key[-6:]}"
            a["detail"] = "session started"
        elif kind == "session_end":
            if key in agents:
                agents[key]["status"] = "gone"
                agents[key]["updated_at"] = ev.get("ts", now)
        elif kind == "subagent_start":
            child = ev.get("child_session_id")
            if child:
                ck = str(child)
                ev2 = dict(ev)
                ev2["session_id"] = ck
                a = ensure(ck, ev2)
                a["kind"] = "subagent"
                a["label"] = _short(ev.get("child_goal"), 26) or f"sub {ck[-6:]}"
                a["status"] = "working"
                a["detail"] = _short(ev.get("child_goal"))
                a["parent"] = str(ev.get("parent_session_id") or "")
        elif kind == "subagent_stop":
            child = ev.get("child_session_id")
            if child and str(child) in agents:
                agents[str(child)]["status"] = "done"
                agents[str(child)]["updated_at"] = ev.get("ts", now)
        elif kind == "tool_start":
            a = ensure(key, ev)
            a["status"] = "working"
            a["tool"] = str(ev.get("tool_name") or "")
            a["activity"] = str(ev.get("activity") or "working")
            a["detail"] = _short(ev.get("preview"))
        elif kind == "tool_end":
            a = ensure(key, ev)
            a["status"] = "thinking"
            a["tool"] = ""
            if ev.get("status") == "error":
                a["detail"] = f"⚠ {_short(ev.get('error_message'), 40)}"
            else:
                a["detail"] = ""
        elif kind == "approval_request":
            a = ensure(key, ev)
            a["status"] = "waiting"
            a["tool"] = ""
            a["detail"] = _short(ev.get("command"), 40) or "needs approval"
        elif kind == "approval_response":
            a = ensure(key, ev)
            choice = str(ev.get("choice") or "")
            if choice in ("deny", "timeout"):
                a["status"] = "thinking"
                a["detail"] = f"approval: {choice}"
            else:
                a["status"] = "working"
                a["detail"] = ""

    # Sweep stale + long-gone agents.
    visible = []
    for a in agents.values():
        age = now - float(a.get("updated_at") or 0)
        if a["status"] == "gone" and age > 20:
            continue
        if a["status"] == "done" and age > 120:
            continue
        if age > _STALE_SECONDS:
            continue
        if a["status"] in ("working", "thinking") and age > 300:
            a["status"] = "idle"
        visible.append(a)

    visible.sort(key=lambda a: (a["kind"] != "main", a.get("first_seen", 0)))

    # Attach live agent status to each user's agent roster so profile popups
    # show a brief, real-time list (also rides the P2P score packets).
    scene = {a["id"]: a for a in agents.values()}
    for r in rows:
        for ag in r.get("agents", []):
            a = scene.get(ag.get("id"))
            if a:
                ag["status"] = a.get("status", "")
                ag["tool"] = a.get("tool", "")
                ag["activity"] = a.get("activity", "")
                ag["detail"] = a.get("detail", "")

    # Warm + expose the tier ladder to the frontend.
    _tier_for_ops(0)
    tiers = getattr(_tier_for_ops, "_cache", None) or []

    return {
        "agents": visible,
        "leaderboard": rows,
        "stats": global_stats,
        "identity": identity,
        "chat": chat,
        "tiers": tiers,
        "ts": now,
    }


# ---------------------------------------------------------------------------
# Identity (GitHub connect)
# ---------------------------------------------------------------------------

def _load_identity() -> Optional[Dict[str, Any]]:
    p = _identity_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_identity(d: Dict[str, Any]) -> None:
    try:
        _identity_path().write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def set_identity(github: str) -> Dict[str, Any]:
    """Resolve a GitHub username via the public API and store identity.

    The display ``name`` is the GitHub USERNAME (login, e.g. ``DrGekoz``) —
    that's what shows on the leaderboard. The full name is kept in
    ``full_name`` for reference.
    """
    github = (github or "").strip().lstrip("@")
    ident = {"github": github, "name": github, "avatar": "", "url": ""}
    if github:
        try:
            import urllib.request
            req = urllib.request.Request(
                f"https://api.github.com/users/{urllib.parse.quote(github)}",
                headers={"User-Agent": "hermes-pixel-office", "Accept": "application/vnd.github+json"},
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                d = json.loads(resp.read().decode("utf-8", "replace"))
            # Display name = the GitHub username (login); keep the full name aside.
            ident["name"] = d.get("login") or d.get("name") or github
            ident["full_name"] = d.get("name") or d.get("login") or github
            ident["avatar"] = d.get("avatar_url", "")
            ident["url"] = d.get("html_url", f"https://github.com/{github}")
        except Exception as exc:
            logger.debug("pixel-office github lookup failed: %s", exc)
            ident["url"] = f"https://github.com/{github}"
    _save_identity(ident)

    # Publish a signed identity block into the ledger so every instance knows.
    payload = {
        "type": "identity", "pid": os.getpid(), "github": ident["github"],
        "name": ident["name"], "avatar": ident["avatar"], "url": ident["url"],
        "ts": time.time(),
    }
    _append_signed_block(payload)
    return ident


# ---------------------------------------------------------------------------
# User profiles (bio, links, chosen tier icon, profile/interface themes).
# Persisted per GitHub username in profiles.json.
# ---------------------------------------------------------------------------
def _profiles_path() -> Path:
    return _office_dir() / "profiles.json"


def _load_profiles() -> Dict[str, Dict[str, Any]]:
    try:
        return json.loads(_profiles_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_profiles(profiles: Dict[str, Dict[str, Any]]) -> None:
    try:
        _profiles_path().write_text(
            json.dumps(profiles, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def get_profile(user: str) -> Dict[str, Any]:
    if not user:
        return {}
    return _load_profiles().get(str(user).strip().lstrip("@").lower(), {})


def set_profile(user: str, data: Dict[str, Any]) -> Dict[str, Any]:
    user = (user or "").strip().lstrip("@")
    if not user:
        return {}
    key = user.lower()
    profiles = _load_profiles()
    cur = dict(profiles.get(key, {}))
    cur["user"] = user
    for f in ("bio", "links", "tierIcon", "profileTheme", "interfaceTheme"):
        if f in data:
            cur[f] = data[f]
    profiles[key] = cur
    _save_profiles(profiles)
    return cur


def _profile_for_row(github: str) -> Dict[str, Any]:
    p = get_profile(github)
    if not p:
        return {}
    return {
        "bio": p.get("bio", ""),
        "links": p.get("links", {}),
        "tierIcon": p.get("tierIcon", ""),
        "profileTheme": p.get("profileTheme", {}),
        "interfaceTheme": p.get("interfaceTheme", {}),
    }


def _maybe_trim_ledger(path: Path) -> None:
    """Trim the ledger, keeping only chat blocks eligible for removal.

    Identity + score blocks are persistent state (a user's score MUST never be
    dropped); only chat history is trimmed to bound the file size.
    """
    try:
        if path.stat().st_size <= _MAX_LEDGER_BYTES:
            return
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        keep = []
        chat = []
        for ln in lines:
            ln = ln.strip()
            if not ln:
                continue
            try:
                b = json.loads(ln)
            except Exception:
                continue
            if (b.get("data") or {}).get("type") == "chat":
                chat.append(ln)
            else:
                keep.append(ln)
        # Drop the oldest half of the chat blocks, keep everything else.
        drop = len(chat) // 2
        keep = keep + chat[drop:]
        tmp = path.with_suffix(".jsonl.tmp")
        tmp.write_text("\n".join(keep) + "\n", encoding="utf-8")
        tmp.replace(path)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------

def _read_chat(limit: int = 60) -> List[Dict[str, Any]]:
    p = _chat_path()
    if not p.exists():
        return []
    out = []
    try:
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    except Exception:
        return []
    return out[-limit:]


def post_chat(text: str) -> Optional[Dict[str, Any]]:
    text = (text or "").strip()
    if not text:
        return None
    ident = _load_identity() or {}
    msg = {
        "user": ident.get("name") or ident.get("github") or "guest",
        "github": ident.get("github") or "",
        "avatar": ident.get("avatar", ""),
        "url": ident.get("url", ""),
        "text": _short(text, 200),
        "ts": time.time(),
        "pid": os.getpid(),
    }
    try:
        with _lock:
            with open(_chat_path(), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(msg, ensure_ascii=False) + "\n")
    except Exception:
        return None
    _append_signed_block({"type": "chat", **msg})
    return msg


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

def _resolve_port() -> int:
    try:
        from hermes_cli.config import cfg_get, load_config

        p = cfg_get(load_config(), "plugins", "entries", "pixel-office", "port")
        if p:
            return int(p)
    except Exception:
        pass
    return DEFAULT_PORT


def _ensure_server() -> None:
    global _server_started
    if _server_started:
        return
    with _lock:
        if _server_started:
            return
        _server_started = True
    t = threading.Thread(target=_serve, name="pixel-office-http", daemon=True)
    t.start()


def _serve() -> None:
    global _port
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    _port = _resolve_port()
    web_root = Path(__file__).resolve().parent / "web"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:  # silence stdout
            pass

        def _send_bytes(self, body: bytes, ctype: str, code: int = 200) -> None:
            try:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            except Exception:
                pass

        def do_GET(self) -> None:
            try:
                path = self.path.split("?")[0]
                if path in ("/", "/index.html"):
                    body = (web_root / "index.html").read_bytes()
                    self._send_bytes(body, "text/html; charset=utf-8")
                elif path == "/state":
                    self._send_bytes(json.dumps(build_state()).encode("utf-8"),
                                     "application/json")
                elif path == "/tiers.js":
                    body = (web_root / "tiers.js").read_bytes()
                    self._send_bytes(body, "text/javascript; charset=utf-8")
                elif path == "/p2p.js":
                    body = (web_root / "p2p.js").read_bytes()
                    self._send_bytes(body, "text/javascript; charset=utf-8")
                elif path == "/signaling/state":
                    self._send_bytes(json.dumps(signaling_state()).encode("utf-8"),
                                     "application/json")
                elif path == "/auth/status":
                    user = github_login()
                    self._send_bytes(json.dumps({
                        "logged_in": bool(user and user.get("login")),
                        "token_present": bool(_load_token()),
                        "user": user,
                    }).encode("utf-8"), "application/json")
                elif path == "/profile":
                    from urllib.parse import parse_qs, urlparse
                    q = parse_qs(urlparse(self.path).query)
                    user = (q.get("user") or [""])[0].strip().lstrip("@")
                    ident = _load_identity() or {}
                    target = user or ident.get("github") or ""
                    prof = _profile_for_row(target) if target else {}
                    self._send_bytes(
                        json.dumps({"user": target, **prof}).encode("utf-8"),
                        "application/json")
                elif path.startswith("/icons/"):
                    rel = path[len("/icons/"):]
                    # Allow one safe subdirectory (e.g. "large/...") but never
                    # traversal or absolute paths.
                    if not rel or rel.startswith("/") or "\\" in rel or ".." in rel:
                        self._send_bytes(b"not found", "text/plain", 404)
                    else:
                        fp = (web_root / "icons" / rel).resolve()
                        base = (web_root / "icons").resolve()
                        if str(fp).startswith(str(base)) and fp.is_file():
                            self._send_bytes(fp.read_bytes(), "image/webp")
                        else:
                            self._send_bytes(b"not found", "text/plain", 404)
                else:
                    self._send_bytes(b"not found", "text/plain", 404)
            except Exception:
                logger.debug("pixel-office request failed", exc_info=True)

        def do_POST(self) -> None:
            try:
                path = self.path.split("?")[0]
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    body = json.loads(raw.decode("utf-8", "replace") or "{}")
                except Exception:
                    body = {}
                if path == "/identify":
                    gh = body.get("github") or ""
                    ident = set_identity(gh)
                    self._send_bytes(json.dumps(ident).encode("utf-8"), "application/json")
                elif path == "/auth/login":
                    # Real GitHub login — resolve the authenticated user from the
                    # stored token and store their identity.
                    user = github_login()
                    if user and user.get("login"):
                        ident = set_identity(str(user["login"]))
                        ident["github_login"] = user.get("login")
                        ident["logged_in"] = True
                    else:
                        ident = {"logged_in": False,
                                 "error": "no github token / login failed"}
                    self._send_bytes(json.dumps(ident).encode("utf-8"), "application/json")
                elif path == "/profile":
                    ident = _load_identity() or {}
                    github = (ident.get("github") or "").strip().lstrip("@")
                    if not github:
                        self._send_bytes(
                            json.dumps({"ok": False, "error": "connect a GitHub identity first"}).encode("utf-8"),
                            "application/json")
                    else:
                        saved = set_profile(github, body)
                        saved["user"] = github
                        self._send_bytes(json.dumps({"ok": True, **saved}).encode("utf-8"),
                                         "application/json")
                elif path == "/chat":
                    msg = post_chat(body.get("text") or "")
                    self._send_bytes(json.dumps(msg or {"ok": False}).encode("utf-8"),
                                     "application/json")
                elif path == "/signaling/register":
                    st = signaling_register(bool(body.get("online", True)))
                    self._send_bytes(json.dumps(st).encode("utf-8"), "application/json")
                elif path == "/signaling/offer":
                    ok = signaling_send_offer(str(body.get("to") or ""), body.get("sdp"))
                    self._send_bytes(json.dumps({"ok": ok}).encode("utf-8"),
                                     "application/json")
                elif path == "/signaling/answer":
                    ok = signaling_send_answer(str(body.get("to") or ""), body.get("sdp"))
                    self._send_bytes(json.dumps({"ok": ok}).encode("utf-8"),
                                     "application/json")
                elif path == "/signaling/clear":
                    signaling_clear_inbox()
                    self._send_bytes(b'{"ok":true}', "application/json")
                else:
                    self._send_bytes(b"not found", "text/plain", 404)
            except Exception:
                logger.debug("pixel-office POST failed", exc_info=True)

    try:
        srv = ThreadingHTTPServer(("127.0.0.1", _port), Handler)
    except OSError as exc:
        verdict = _probe_port(_port)
        if verdict == "office":
            logger.info(
                "pixel-office: port %s already serving a healthy office "
                "(another Hermes process) — this process will feed events only",
                _port,
            )
        else:
            logger.warning(
                "pixel-office: could NOT bind 127.0.0.1:%s (%s) and the "
                "current listener does not answer like a pixel-office "
                "(probe: %s). Another app or a stale VS Code port-forward is "
                "squatting the port.",
                _port, exc, verdict,
            )
        return
    logger.info("pixel-office serving at http://127.0.0.1:%s", _port)
    try:
        srv.serve_forever()
    except Exception:
        logger.debug("pixel-office server exited", exc_info=True)


def _probe_port(port: int) -> str:
    try:
        import urllib.request

        req = urllib.request.Request(f"http://127.0.0.1:{port}/state")
        with urllib.request.urlopen(req, timeout=2) as resp:
            body = resp.read(4096).decode("utf-8", errors="replace")
        return "office" if '"agents"' in body else "foreign"
    except Exception as exc:
        return f"dead/{type(exc).__name__}"


# ---------------------------------------------------------------------------
# Hook callbacks — all **kwargs so core payload changes never break us
# ---------------------------------------------------------------------------

_ACTIVITY = {
    "write_file": "typing", "patch": "typing", "skill_manage": "typing",
    "read_file": "reading", "search_files": "reading", "skill_view": "reading",
    "web_search": "browsing", "web_extract": "browsing",
    "browser_navigate": "browsing", "browser_click": "browsing",
    "browser_snapshot": "browsing", "browser_vision": "browsing",
    "terminal": "running", "execute_code": "running", "process": "running",
    "delegate_task": "delegating",
}


def _activity_for(tool: str) -> str:
    return _ACTIVITY.get(str(tool or ""), "working")


def _on_session_start(**kw: Any) -> None:
    _publish({
        "event": "session_start",
        "session_id": kw.get("session_id"),
        "platform": kw.get("platform"),
    })


def _on_session_end(**kw: Any) -> None:
    _publish({"event": "session_end", "session_id": kw.get("session_id")})


def _pre_tool_call(**kw: Any) -> None:
    global _last_session_id
    sid = kw.get("session_id") or ""
    if sid:
        _last_session_id = str(sid)
    args = kw.get("args") or {}
    preview = ""
    if isinstance(args, dict):
        for k in ("command", "path", "query", "url", "goal", "pattern", "prompt"):
            if args.get(k):
                preview = str(args[k])
                break
    tool = kw.get("tool_name")
    _publish({
        "event": "tool_start",
        "session_id": sid,
        "tool_name": tool,
        "activity": _activity_for(tool),
        "preview": _short(preview),
    })
    return None  # observer — never blocks


def _post_tool_call(**kw: Any) -> None:
    _publish({
        "event": "tool_end",
        "session_id": kw.get("session_id"),
        "tool_name": kw.get("tool_name"),
        "status": kw.get("status") or "ok",
        "error_message": kw.get("error_message"),
        "duration_ms": kw.get("duration_ms"),
    })


def _subagent_start(**kw: Any) -> None:
    _publish({
        "event": "subagent_start",
        "parent_session_id": kw.get("parent_session_id"),
        "child_session_id": kw.get("child_session_id"),
        "child_role": kw.get("child_role"),
        "child_goal": kw.get("child_goal"),
    })


def _subagent_stop(**kw: Any) -> None:
    _publish({
        "event": "subagent_stop",
        "child_session_id": kw.get("child_session_id"),
    })


def _pre_approval_request(**kw: Any) -> None:
    _publish({
        "event": "approval_request",
        "session_id": _last_session_id,
        "command": kw.get("command") or kw.get("description"),
        "surface": kw.get("surface"),
    })


def _post_approval_response(**kw: Any) -> None:
    _publish({
        "event": "approval_response",
        "session_id": _last_session_id,
        "choice": kw.get("choice"),
    })


def register(ctx: Any) -> None:
    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("on_session_end", _on_session_end)
    ctx.register_hook("pre_tool_call", _pre_tool_call)
    ctx.register_hook("post_tool_call", _post_tool_call)
    ctx.register_hook("subagent_start", _subagent_start)
    ctx.register_hook("subagent_stop", _subagent_stop)
    ctx.register_hook("pre_approval_request", _pre_approval_request)
    ctx.register_hook("post_approval_response", _post_approval_response)
    logger.info(
        "pixel-office registered — office at http://127.0.0.1:%s once events flow",
        _resolve_port(),
    )
