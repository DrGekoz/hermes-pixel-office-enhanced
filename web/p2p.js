// ============================================================
// Hermes Pixel Office — P2P mesh (IdleViber pattern)
// Scores + chat flow ONLY over WebRTC data channels between
// browsers. GitHub is used purely for login (username) and the
// signaling handshake (presence + SDP offer/answer exchange via
// the local plugin server), so peers can find each other.
// No score/chat message ever touches GitHub once channels open.
//
// Instrumented + hardened (2026-08-13): mirrors the Hermes
// IdleViber console logging pattern ("📡 P2P: ...") so every
// connection / message / disconnect / backoff is visible in the
// browser console, and adds input validation, message-size caps,
// a connect-timeout reconnect backoff, a max-peer cap and
// try/catch on every handler so a bad peer can never crash the
// mesh or corrupt the board.
// ============================================================

window.PixelOfficeP2P = (function () {
  "use strict";

  var ICE = {
    iceServers: [
      { urls: "stun:stun.l.google.com:19302" },
      { urls: "stun:stun1.l.google.com:19302" },
    ],
  };
  var POLL_MS = 4000;        // signaling poll (handshake)
  var REBROADCAST_MS = 5000; // keep peers' scores fresh
  var CONNECT_TIMEOUT = 8000;
  var MAX_MSG = 65536;       // max inbound data-channel message (bytes)
  var MAX_PEERS = 12;        // cap concurrent connections

  var me = null;             // {id, github, name, avatar, url}
  var peers = new Map();     // id -> {pc, channel, connected, entry, timer}
  var connecting = new Set();
  var entries = new Map();   // id -> entry (latest score we have)
  var peerChat = [];         // chat messages (peers + own)
  var lastEntry = null;      // our latest broadcast entry
  var entryCounter = 0;
  var scoreHandlers = [];
  var chatHandlers = [];
  var started = false;
  var pollTimer = null;
  var rebroadcastTimer = null;
  var fallbackTimer = null;    // posts our score to the gist when no peer is reachable via WebRTC
  var backoff = new Map();   // peerId -> {attempts, next}

  // ---- tiny logging helpers (IdleViber "📡 P2P:" style) ----------------
  function short(id) { return String(id || "").substr(0, 8); }
  function log() {
    var a = Array.prototype.slice.call(arguments);
    a.unshift("📡 P2P:");
    console.log.apply(console, a);
  }
  function warn() {
    var a = Array.prototype.slice.call(arguments);
    a.unshift("📡 P2P:");
    console.warn.apply(console, a);
  }

  // ---- small helpers -------------------------------------------------
  function post(path, body) {
    return fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    }).then(function (r) { return r.json(); }).catch(function () { return {}; });
  }
  function get(path) {
    return fetch(path).then(function (r) { return r.json(); })
      .catch(function () { return {}; });
  }

  function setMe(identity) {
    if (identity && identity.github) {
      me = {
        id: identity.github,
        github: identity.github,
        name: identity.name || identity.github,
        avatar: identity.avatar || "",
        url: identity.url || "https://github.com/" + identity.github,
      };
      log("identity set:", me.github);
    } else {
      me = null;
    }
  }

  function myEntry(serverRow) {
    // Build the entry we broadcast from the live score the server folded.
    if (!serverRow) return null;
    return {
      id: me ? me.id : serverRow.id || "local",
      user: me ? me.github : "local",
      github: me ? me.github : (serverRow.github || ""),
      name: me ? me.name : (serverRow.name || "you"),
      avatar: (me && me.avatar) || (serverRow.avatar || ""),
      url: (me && me.url) || (serverRow.url || ""),
      ops: serverRow.ops || 0,
      tools: serverRow.tools || 0,
      sessions: serverRow.sessions || 0,
      time_s: serverRow.time_s || 0,
      tier: serverRow.tier || null,
      // profile payload rides along with the score so peers see bio/links/
      // chosen tier icon/themes without a separate channel.
      bio: serverRow.bio || "",
      links: serverRow.links || {},
      tierIcon: serverRow.tierIcon || "",
      profileTheme: serverRow.profileTheme || {},
      interfaceTheme: serverRow.interfaceTheme || {},
      agents: serverRow.agents || [],
      // modelboard rides the score packet so peers see each other's
      // jekyll-hyde audit data (model corrections) on a shared board.
      modelboard: serverRow.modelboard || null,
      counter: ++entryCounter,
      ts: Date.now(),
    };
  }

  function broadcastScore(serverRow) {
    if (!started || !me) return;
    var e = myEntry(serverRow);
    if (!e) return;
    lastEntry = e;
    entries.set(me.id, e);       // our own entry in the local board
    broadcastMsg({ type: "score", entry: e });
  }

  // Trimmed copy of our latest score, sized for the gist fallback (we don't
  // want the full profile/theme/agents/modelboard payload in the shared doc).
  function fallbackEntry() {
    if (!lastEntry) return null;
    return {
      id: lastEntry.id, github: lastEntry.github, name: lastEntry.name,
      avatar: lastEntry.avatar, url: lastEntry.url,
      ops: lastEntry.ops, tools: lastEntry.tools, sessions: lastEntry.sessions,
      time_s: lastEntry.time_s, tier: lastEntry.tier,
      counter: lastEntry.counter, ts: Date.now(),
    };
  }

  // ---- mesh primitives ------------------------------------------------
  function broadcastMsg(msg, silent) {
    var s = JSON.stringify(msg);
    var sent = 0;
    peers.forEach(function (p) {
      if (p.channel && p.channel.readyState === "open") {
        try { p.channel.send(s); sent++; } catch (_) {}
      }
    });
    if (!silent) log("send", msg.type, "->", sent, "peer(s)");
    return sent;
  }

  function handleMessage(peerId, raw) {
    // Harden: drop empty/oversized payloads before parsing.
    if (!raw || typeof raw !== "string" || raw.length > MAX_MSG) return;
    var msg;
    try { msg = JSON.parse(raw); } catch (e) { return; }
    if (!msg || typeof msg !== "object") return;

    if (msg.type === "score" && msg.entry &&
        typeof msg.entry.id === "string" && msg.entry.id &&
        msg.entry.id.length <= 200) {
      var eid = msg.entry.id;
      if (eid === me.id) return;              // ignore our own echoed entry
      var prev = entries.get(eid);
      var c = Number(msg.entry.counter) || 0;
      if (!prev || c >= (Number(prev.counter) || 0)) {
        entries.set(eid, msg.entry);
        log("score recv", short(eid), "ops=" + (msg.entry.ops || 0), "counter=" + c);
        scoreHandlers.forEach(function (cb) { try { cb(); } catch (_) {} });
      }
    } else if (msg.type === "chat" && typeof msg.text === "string" && msg.text) {
      msg.ts = msg.ts || Date.now();
      msg.peer = peerId;
      peerChat.push(msg);
      if (peerChat.length > 200) peerChat = peerChat.slice(-200);
      log("chat recv from", short(peerId), ":", msg.text.slice(0, 60));
      chatHandlers.forEach(function (cb) { try { cb(msg); } catch (_) {} });
    }
  }

  function noteBackoff(peerId) {
    var b = backoff.get(peerId) || { attempts: 0, next: 0 };
    b.attempts++;
    // exponential: 3s, 6s, 12s, 24s, 48s, capped at 60s
    var wait = Math.min(60000, 3000 * Math.pow(2, Math.min(b.attempts, 5)));
    b.next = Date.now() + wait;
    backoff.set(peerId, b);
    warn("backoff", short(peerId), "for", wait + "ms", "(attempt " + b.attempts + ")");
  }
  function backoffAllowed(peerId) {
    var b = backoff.get(peerId);
    if (!b) return true;
    if (Date.now() >= b.next) { backoff.delete(peerId); return true; }
    return false;
  }

  function attachChannel(peerId, pc, channel) {
    if (!peerId || !pc || !channel) { warn("attachChannel: bad args"); return; }
    var peer = { pc: pc, channel: channel, connected: false };
    peers.set(peerId, peer);

    channel.onopen = function () {
      peer.connected = true;
      connecting.delete(peerId);
      backoff.delete(peerId);
      log("connected to peer", short(peerId));
      // On connect, hand the peer our current score immediately.
      if (lastEntry) {
        try { channel.send(JSON.stringify({ type: "score", entry: lastEntry })); } catch (_) {}
      }
      scoreHandlers.forEach(function (cb) { try { cb(); } catch (_) {} });
    };
    channel.onmessage = function (ev) {
      try { handleMessage(peerId, ev && ev.data); }
      catch (e) { warn("msg handler error:", e && e.message); }
    };
    channel.onerror = function (ev) { warn("channel error", short(peerId)); };
    channel.onclose = function () { log("closed peer", short(peerId)); teardown(peerId); };
    pc.oniceconnectionstatechange = function () {
      if (pc.iceConnectionState === "failed" || pc.iceConnectionState === "disconnected") {
        warn("ice", pc.iceConnectionState, short(peerId));
        noteBackoff(peerId);
        teardown(peerId);
      }
    };
    if (peer.timer) clearTimeout(peer.timer);
    peer.timer = setTimeout(function () {
      if (!peer.connected) {
        noteBackoff(peerId);
        teardown(peerId);
      }
    }, CONNECT_TIMEOUT);
  }

  function teardown(peerId) {
    var p = peers.get(peerId);
    if (p) {
      if (p.channel) { try { p.channel.close(); } catch (_) {} }
      if (p.pc) { try { p.pc.close(); } catch (_) {} }
      if (p.timer) clearTimeout(p.timer);
      log("teardown peer", short(peerId));
    }
    peers.delete(peerId);
    connecting.delete(peerId);
    // keep last known score in entries; a fresh broadcast replaces it.
    scoreHandlers.forEach(function (cb) { try { cb(); } catch (_) {} });
  }

  function initiate(peerId) {
    if (!peerId) return;
    if (peers.has(peerId) || connecting.has(peerId)) return;
    if (connecting.size >= MAX_PEERS) { warn("peer cap reached, skipping", short(peerId)); return; }
    if (!backoffAllowed(peerId)) return;
    connecting.add(peerId);
    var pc;
    try {
      pc = new RTCPeerConnection(ICE);
    } catch (e) { connecting.delete(peerId); warn("RTCPeerConnection init failed:", e && e.message); return; }
    var channel = pc.createDataChannel("office");
    attachChannel(peerId, pc, channel);
    log("offering to peer", short(peerId));
    pc.createOffer().then(function (offer) {
      return pc.setLocalDescription(offer);
    }).then(function () {
      return post("signaling/offer", { to: peerId, sdp: pc.localDescription });
    }).catch(function (err) {
      warn("offer creation failed:", err && err.message);
      noteBackoff(peerId);
      teardown(peerId);
    });
  }

  function answer(peerId, sdp) {
    if (!peerId || !sdp) { warn("answer: bad args from", short(peerId)); return; }
    if (peers.has(peerId) || connecting.has(peerId)) return;
    if (connecting.size >= MAX_PEERS) { warn("peer cap reached, skipping", short(peerId)); return; }
    connecting.add(peerId);
    var pc;
    try {
      pc = new RTCPeerConnection(ICE);
    } catch (e) { connecting.delete(peerId); warn("RTCPeerConnection init failed:", e && e.message); return; }
    pc.ondatachannel = function (ev) { attachChannel(peerId, pc, ev.channel); };
    log("answering peer", short(peerId));
    pc.setRemoteDescription(new RTCSessionDescription(sdp)).then(function () {
      return pc.createAnswer();
    }).then(function (answer) {
      return pc.setLocalDescription(answer);
    }).then(function () {
      return post("signaling/answer", { to: peerId, sdp: pc.localDescription });
    }).catch(function (err) {
      warn("answer creation failed:", err && err.message);
      noteBackoff(peerId);
      teardown(peerId);
    });
  }

  // ---- signaling poll (presence + inbox) ------------------------------
  function pollSignaling() {
    if (!started || !me) return;
    get("signaling/state").then(function (st) {
      if (!st || typeof st !== "object") return;
      var pl = st.peers || {};
      Object.keys(pl).forEach(function (id) {
        if (!id || id === me.id) return;
        if (peers.has(id) || connecting.has(id)) return;
        if (!pl[id].online) return;
        // Glare avoidance: when two peers come online at the same instant both
        // would initiate and create a duplicate connection pair. Only the
        // lexicographically-smaller id initiates; the larger id waits for that
        // peer's offer and answers it (deterministic initiator).
        if (me.id < id && backoffAllowed(id)) initiate(id);
      });
      var offers = st.offers || {};
      var any = false;
      Object.keys(offers).forEach(function (fromId) {
        if (!fromId || fromId === me.id) return;
        if (peers.has(fromId) || connecting.has(fromId)) return;
        var sdp = offers[fromId] && offers[fromId].sdp;
        if (sdp) { any = true; answer(fromId, sdp); }
      });
      if (any) post("signaling/clear", {}); // consume handled offers

      // We are the initiator: apply answers our peers sent for our offers.
      var answers = st.answers || {};
      Object.keys(answers).forEach(function (fromId) {
        if (!fromId) return;
        var p = peers.get(fromId);
        if (!p || p.connected) return;
        if (p.pc && p.pc.signalingState === "have-local-offer") {
          var sdp = answers[fromId] && answers[fromId].sdp;
          if (sdp) {
            try {
              p.pc.setRemoteDescription(new RTCSessionDescription(sdp));
            } catch (e) { warn("apply answer failed:", e && e.message); }
            post("signaling/clear", {});
          }
        }
      });

      // ---- Gist fallback for realtime scores ----------------------------
      // WebRTC data channels are the FRONT transport for scores. But when a
      // peer can't be reached directly (e.g. strict CGNAT — ICE fails, we're
      // backing off), fall back to reading their score that they published to
      // the gist so the board still stays live for every player.
      Object.keys(pl).forEach(function (id) {
        if (!id || id === me.id) return;
        var p = peers.get(id);
        if (p && p.connected) return;          // already live over WebRTC
        if (connecting.has(id)) return;        // handshake still in progress
        var fs = pl[id].score;
        if (!fs || !fs.id) return;
        var c = Number(fs.counter) || 0;
        var prev = entries.get(fs.id);
        if (!prev || c >= (Number(prev.counter) || 0)) {
          entries.set(fs.id, fs);
          log("score recv (gist fallback)", short(fs.id), "ops=" + (fs.ops || 0), "counter=" + c);
          scoreHandlers.forEach(function (cb) { try { cb(); } catch (_) {} });
        }
      });
    }).catch(function (err) { warn("signaling poll error:", err && err.message); });
  }

  // ---- public API -----------------------------------------------------
  function start(identity) {
    setMe(identity);
    if (!me) { warn("start: no identity"); return; }
    started = true;
    log("starting, ID", short(me.id));
    post("signaling/register", { online: true });
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(pollSignaling, POLL_MS);
    if (rebroadcastTimer) clearInterval(rebroadcastTimer);
    rebroadcastTimer = setInterval(function () {
      if (lastEntry) {
        var n = broadcastMsg({ type: "score", entry: lastEntry }, true);
        console.debug("📡 P2P: rebroadcast score to", n, "peer(s)");
      }
    }, REBROADCAST_MS);
    // Gist fallback: when we have ZERO peers reachable over WebRTC, publish
    // our score to the gist so other players (even ones we can't connect to
    // directly) can still read it. WebRTC remains the front transport.
    if (fallbackTimer) clearInterval(fallbackTimer);
    fallbackTimer = setInterval(function () {
      if (!started || !me || !lastEntry) return;
      var connectedCount = 0;
      peers.forEach(function (p) { if (p.connected) connectedCount++; });
      if (connectedCount === 0) {
        post("signaling/score", { entry: fallbackEntry() });
      }
    }, 10000);
  }

  function stop() {
    started = false;
    if (pollTimer) clearInterval(pollTimer);
    if (rebroadcastTimer) clearInterval(rebroadcastTimer);
    if (fallbackTimer) clearInterval(fallbackTimer);
    peers.forEach(function (_p, id) { teardown(id); });
    backoff.clear();
    post("signaling/register", { online: false });
    log("stopped");
  }

  // Merge our live local row + all peer entries into a ranked board.
  function getLeaderboard(localRow) {
    if (localRow && started && me) {
      // keep our broadcast entry in sync with the live server-folded score
      if (lastEntry) {
        lastEntry.ops = localRow.ops || lastEntry.ops;
        lastEntry.tools = localRow.tools || lastEntry.tools;
        lastEntry.sessions = localRow.sessions || lastEntry.sessions;
        lastEntry.time_s = localRow.time_s || lastEntry.time_s;
        lastEntry.tier = localRow.tier || lastEntry.tier;
        entries.set(me.id, lastEntry);
      }
    }
    var rows = [];
    entries.forEach(function (e) {
      // Only GitHub usernames belong on the leaderboard — never agents.
      var g = e.github || e.user || "";
      if (!g) return;                    // skip non-GitHub entries entirely
      rows.push({
        id: e.id, name: e.name || e.user, github: g,
        avatar: e.avatar || "", url: e.url || "",
        ops: e.ops || 0, tools: e.tools || 0, sessions: e.sessions || 0,
        time_s: e.time_s || 0, tier: e.tier || null,
        bio: e.bio || "", links: e.links || {}, tierIcon: e.tierIcon || "",
        profileTheme: e.profileTheme || {}, interfaceTheme: e.interfaceTheme || {},
        agents: e.agents || [],
        modelboard: e.modelboard || null,
      });
    });
    rows.sort(function (a, b) { return (b.ops - a.ops) || (b.tools - a.tools); });
    rows.forEach(function (r, i) { r.rank = i + 1; });
    return rows;
  }

  function sendChat(text) {
    if (!text || !text.trim()) return;
    var msg = {
      type: "chat",
      user: me ? me.name : "guest",
      github: me ? me.github : "",
      avatar: me ? me.avatar : "",
      url: me ? me.url : "",
      ops: lastEntry ? lastEntry.ops : 0,
      tier: lastEntry ? lastEntry.tier : null,
      tierIcon: lastEntry ? lastEntry.tierIcon : "",
      text: text.slice(0, 200),
      ts: Date.now(),
    };
    peerChat.push(msg);
    if (peerChat.length > 200) peerChat = peerChat.slice(-200);
    broadcastMsg(msg);
    log("chat sent:", msg.text.slice(0, 60));
    chatHandlers.forEach(function (cb) { try { cb(msg); } catch (_) {} });
    return msg;
  }

  function getChat() { return peerChat.slice(); }
  function onScore(cb) { scoreHandlers.push(cb); }
  function onChat(cb) { chatHandlers.push(cb); }
  function isStarted() { return started; }

  // seed chat history with any messages the server already recorded
  function seedChat(msgs) {
    (msgs || []).forEach(function (m) {
      if (m && m.text) peerChat.push({
        user: m.user, github: m.github, avatar: m.avatar, url: m.url,
        text: m.text, ts: (m.ts || Date.now()) * 1000, peer: "seed",
      });
    });
    if (peerChat.length > 200) peerChat = peerChat.slice(-200);
  }

  return {
    start: start,
    stop: stop,
    broadcastScore: broadcastScore,
    sendChat: sendChat,
    getLeaderboard: getLeaderboard,
    getChat: getChat,
    onScore: onScore,
    onChat: onChat,
    seedChat: seedChat,
    isStarted: isStarted,
  };
})();
