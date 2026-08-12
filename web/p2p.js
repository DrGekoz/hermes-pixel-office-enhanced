// ============================================================
// Hermes Pixel Office — P2P mesh (IdleViber pattern)
// Scores + chat flow ONLY over WebRTC data channels between
// browsers. GitHub is used purely for login (username) and the
// signaling handshake (presence + SDP offer/answer exchange via
// the local plugin server), so peers can find each other.
// No score/chat message ever touches GitHub once channels open.
// ============================================================

window.PixelOfficeP2P = (function () {
  "use strict";

  var ICE = {
    iceServers: [
      { urls: "stun:stun.l.google.com:19302" },
      { urls: "stun:stun1.l.google.com:19302" },
    ],
  };
  var POLL_MS = 4000;      // signaling poll (handshake)
  var REBROADCAST_MS = 5000; // keep peers' scores fresh
  var CONNECT_TIMEOUT = 8000;

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

  // ---- mesh primitives ------------------------------------------------
  function broadcastMsg(msg) {
    var s = JSON.stringify(msg);
    peers.forEach(function (p) {
      if (p.channel && p.channel.readyState === "open") {
        try { p.channel.send(s); } catch (_) {}
      }
    });
  }

  function handleMessage(peerId, msg) {
    try { msg = JSON.parse(msg); } catch (_) { return; }
    if (msg.type === "score" && msg.entry && msg.entry.id) {
      // keep the freshest score per peer (higher counter wins)
      var prev = entries.get(msg.entry.id);
      if (!prev || (msg.entry.counter || 0) >= (prev.counter || 0)) {
        entries.set(msg.entry.id, msg.entry);
        scoreHandlers.forEach(function (cb) { try { cb(); } catch (_) {} });
      }
    } else if (msg.type === "chat") {
      msg.ts = msg.ts || Date.now();
      msg.peer = peerId;
      peerChat.push(msg);
      if (peerChat.length > 200) peerChat = peerChat.slice(-200);
      chatHandlers.forEach(function (cb) { try { cb(msg); } catch (_) {} });
    }
  }

  function attachChannel(peerId, pc, channel) {
    var peer = { pc: pc, channel: channel, connected: false };
    peers.set(peerId, peer);

    channel.onopen = function () {
      peer.connected = true;
      connecting.delete(peerId);
      // On connect, hand the peer our current score immediately.
      if (lastEntry) {
        try { channel.send(JSON.stringify({ type: "score", entry: lastEntry })); } catch (_) {}
      }
      scoreHandlers.forEach(function (cb) { try { cb(); } catch (_) {} });
    };
    channel.onmessage = function (ev) { handleMessage(peerId, ev.data); };
    channel.onclose = function () { teardown(peerId); };
    pc.oniceconnectionstatechange = function () {
      if (pc.iceConnectionState === "failed" || pc.iceConnectionState === "disconnected") {
        teardown(peerId);
      }
    };
    if (peer.timer) clearTimeout(peer.timer);
    peer.timer = setTimeout(function () {
      if (!peer.connected) teardown(peerId);
    }, CONNECT_TIMEOUT);
  }

  function teardown(peerId) {
    var p = peers.get(peerId);
    if (p) {
      if (p.channel) { try { p.channel.close(); } catch (_) {} }
      if (p.pc) { try { p.pc.close(); } catch (_) {} }
      if (p.timer) clearTimeout(p.timer);
    }
    peers.delete(peerId);
    connecting.delete(peerId);
    // keep last known score in entries; a fresh broadcast replaces it.
    scoreHandlers.forEach(function (cb) { try { cb(); } catch (_) {} });
  }

  function initiate(peerId) {
    connecting.add(peerId);
    var pc;
    try {
      pc = new RTCPeerConnection(ICE);
    } catch (e) { connecting.delete(peerId); return; }
    var channel = pc.createDataChannel("office");
    attachChannel(peerId, pc, channel);
    pc.createOffer().then(function (offer) {
      return pc.setLocalDescription(offer);
    }).then(function () {
      return post("signaling/offer", { to: peerId, sdp: pc.localDescription });
    }).catch(function () { teardown(peerId); });
  }

  function answer(peerId, sdp) {
    connecting.add(peerId);
    var pc;
    try {
      pc = new RTCPeerConnection(ICE);
    } catch (e) { connecting.delete(peerId); return; }
    pc.ondatachannel = function (ev) { attachChannel(peerId, pc, ev.channel); };
    pc.setRemoteDescription(new RTCSessionDescription(sdp)).then(function () {
      return pc.createAnswer();
    }).then(function (answer) {
      return pc.setLocalDescription(answer);
    }).then(function () {
      return post("signaling/answer", { to: peerId, sdp: pc.localDescription });
    }).catch(function () { teardown(peerId); });
  }

  // ---- signaling poll (presence + inbox) ------------------------------
  function pollSignaling() {
    if (!started || !me) return;
    get("signaling/state").then(function (st) {
      var pl = st.peers || {};
      Object.keys(pl).forEach(function (id) {
        if (id === me.id) return;
        if (peers.has(id) || connecting.has(id)) return;
        if (pl[id].online) initiate(id);
      });
      var offers = st.offers || {};
      var any = false;
      Object.keys(offers).forEach(function (fromId) {
        if (fromId === me.id) return;
        if (peers.has(fromId) || connecting.has(fromId)) return;
        any = true;
        answer(fromId, offers[fromId].sdp);
      });
      if (any) post("signaling/clear", {}); // consume handled offers

      // We are the initiator: apply answers our peers sent for our offers.
      var answers = st.answers || {};
      Object.keys(answers).forEach(function (fromId) {
        var p = peers.get(fromId);
        if (!p || p.connected) return;
        if (p.pc && p.pc.signalingState === "have-local-offer") {
          try {
            p.pc.setRemoteDescription(new RTCSessionDescription(answers[fromId].sdp));
          } catch (_) {}
          post("signaling/clear", {});
        }
      });
    }).catch(function () {});
  }

  // ---- public API -----------------------------------------------------
  function start(identity) {
    setMe(identity);
    if (!me) return;
    started = true;
    post("signaling/register", { online: true });
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(pollSignaling, POLL_MS);
    if (rebroadcastTimer) clearInterval(rebroadcastTimer);
    rebroadcastTimer = setInterval(function () {
      if (lastEntry) broadcastMsg({ type: "score", entry: lastEntry });
    }, REBROADCAST_MS);
  }

  function stop() {
    started = false;
    if (pollTimer) clearInterval(pollTimer);
    if (rebroadcastTimer) clearInterval(rebroadcastTimer);
    peers.forEach(function (_p, id) { teardown(id); });
    post("signaling/register", { online: false });
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
        time_s: e.time_s || 0, tier: e.tier || null, agents: [],
        bio: e.bio || "", links: e.links || {}, tierIcon: e.tierIcon || "",
        profileTheme: e.profileTheme || {}, interfaceTheme: e.interfaceTheme || {},
      });
    });
    rows.sort(function (a, b) { return (b.ops - a.ops) || (b.tools - a.tools); });
    rows.forEach(function (r, i) { r.rank = i + 1; });
    return rows;
  }

  function sendChat(text) {
    if (!text.trim()) return;
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
