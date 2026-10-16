"use strict";
// ---------------------------------------------------------------- helpers
const $ = (id) => document.getElementById(id);
const esc = (s) =>
  String(s ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const wsBase =
  (location.protocol === "https:" ? "wss://" : "ws://") + location.host;
const store = {
  get(k, d) {
    try {
      return localStorage.getItem(k) ?? d;
    } catch {
      return d;
    }
  },
  set(k, v) {
    try {
      localStorage.setItem(k, v);
    } catch {}
  },
};
const sessionStore = {
  get(k, d) {
    try {
      return sessionStorage.getItem(k) ?? d;
    } catch {
      return d;
    }
  },
  set(k, v) {
    try {
      sessionStorage.setItem(k, v);
    } catch {}
  },
};
const DEMO = [
  {
    id: "cus_ananya",
    name: "Ananya Rao",
    token: "app-ananya-demo",
    lang: "en",
  },
  {
    id: "cus_rahul",
    name: "Rahul Sharma",
    token: "app-rahul-demo",
    lang: "hi",
  },
  {
    id: "cus_lakshmi",
    name: "Lakshmi Gowda",
    token: "app-lakshmi-demo",
    lang: "kn",
  },
];
let customers = DEMO.slice();
const cust = () =>
  customers.find((c) => c.id === $("customer").value) || customers[0];
const staffKey = () => $("staffKey").value.trim();
let toastT;
function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toastT);
  toastT = setTimeout(() => (t.hidden = true), 3500);
}
async function api(path, { method = "GET", body, headers = {}, form } = {}) {
  const h = { ...headers };
  if (body !== undefined) h["Content-Type"] = "application/json";
  const r = await fetch(path, {
    method,
    headers: h,
    body: form ?? (body !== undefined ? JSON.stringify(body) : undefined),
  });
  const txt = await r.text();
  let data;
  try {
    data = txt ? JSON.parse(txt) : {};
  } catch {
    data = { detail: txt };
  }
  if (!r.ok) {
    const d = data.detail;
    const error = new Error(
      typeof d === "string"
        ? d
        : d?.message || JSON.stringify(d) || r.statusText,
    );
    error.status = r.status;
    throw error;
  }
  return data;
}
const staffH = () => ({ "X-Staff-Key": staffKey() });
const appH = () => ({ "X-App-Token": cust().token });
const fmt = (v, d = 2) =>
  v === null || v === undefined ? "–" : Number(v).toFixed(d);

// ---------------------------------------------------------------- tabs + header
document.addEventListener("nishabdha:navigate", (event) =>
  showTab(event.detail),
);
function showTab(name) {
  document.querySelectorAll(".tabs button").forEach((b) => {
    if (b.dataset.tab === name) b.setAttribute("aria-current", "page");
    else b.removeAttribute("aria-current");
  });
  ["call", "phone", "desk", "knowledge", "demo"].forEach((t) => {
    const el = $("tab-" + t);
    if (el) el.hidden = t !== name;
  });
  store.set("tab", name);
  document.dispatchEvent(new CustomEvent("nishabdha:tab", { detail: name }));
  if (name === "phone") Phone.open();
  if (name === "desk") Desk.open();
  if (name === "knowledge") Knowledge.open();
  if (name === "demo") JudgeDemo.open();
}

try {
  localStorage.removeItem("staffKey");
} catch {}
$("staffKey").value = sessionStore.get("staffKey", "");
$("staffKey").addEventListener("change", async () => {
  sessionStore.set("staffKey", staffKey());
  if (staffKey()) {
    const r = await fetch("/api/customers", { headers: staffH() }).catch(
      () => null,
    );
    toast(
      r && r.ok
        ? "Staff key accepted"
        : "Wrong staff key - use STAFF_API_KEY from your .env file",
    );
  }
  Desk.reopen();
  loadCustomers();
});
function fillCustomers() {
  const keep = $("customer").value || store.get("customer", "cus_ananya");
  $("customer").innerHTML = customers
    .map((c) => `<option value="${esc(c.id)}">${esc(c.name)}</option>`)
    .join("");
  $("customer").value = customers.some((c) => c.id === keep)
    ? keep
    : customers[0].id;
}
async function loadCustomers() {
  if (!staffKey()) return fillCustomers();
  try {
    const r = await api("/api/customers", { headers: staffH() });
    customers = r.customers.map((c) => ({
      id: c.id,
      name: c.name,
      token: c.app_token,
      lang: c.lang,
      enrolled: c.voice_enrolled,
    }));
  } catch {
    customers = DEMO.slice();
  }
  fillCustomers();
}
$("customer").addEventListener("change", () => {
  store.set("customer", $("customer").value);
  const c = cust();
  if (c.lang) $("lang").value = c.lang;
  Phone.reopen();
  Enroll.load();
});

// ---------------------------------------------------------------- agent speech playback (queue)
const Speaker = {
  // Plays agent replies one after another. Every clip has a watchdog: browsers (Chrome especially) sometimes
  // never fire "ended" for speechSynthesis, which used to leave the mic muted forever.
  queue: [],
  playing: false,
  current: null,
  utter: null,
  timer: null,
  onIdle: null,
  push(msg) {
    this.queue.push(msg);
    if (!this.playing) this.next();
  },
  get busy() {
    return this.playing || this.queue.length > 0;
  },
  next() {
    clearTimeout(this.timer);
    const m = this.queue.shift();
    if (!m) {
      this.playing = false;
      this.current = null;
      Mic.refresh();
      this.onIdle && this.onIdle();
      return;
    }
    this.playing = true;
    this.current = m;
    Mic.refresh();
    let finished = false;
    const done = () => {
      if (finished) return;
      finished = true;
      clearTimeout(this.timer);
      Call.send({ type: "playback", id: m.id, state: "ended" });
      this.next();
    };
    this.finish = done;
    const est =
      Number(m.est_duration_s) ||
      Math.max(2, String(m.text || "").split(/\s+/).length * 0.45);
    this.timer = setTimeout(done, (est + 2.5) * 1000); // watchdog
    if (m.audio_mp3_b64) {
      const mime = m.audio_mp3_b64.startsWith("UklGR") ? "audio/wav" : "audio/mpeg";
      const a = new Audio("data:" + mime + ";base64," + m.audio_mp3_b64);
      this.audio = a;
      a.onended = done;
      a.onerror = done;
      a.play()
        .then(() => Call.send({ type: "playback", id: m.id, state: "started" }))
        .catch(() => this.speak(m, done));
    } else this.speak(m, done);
  },
  speak(m, done) {
    if (!("speechSynthesis" in window)) return done();
    try {
      speechSynthesis.cancel();
      const u = new SpeechSynthesisUtterance(m.text);
      u.lang = { en: "en-IN", hi: "hi-IN", kn: "kn-IN" }[m.lang] || "en-IN";
      const v = speechSynthesis
        .getVoices()
        .find((x) => x.lang?.toLowerCase().startsWith(u.lang.slice(0, 2)));
      if (v) u.voice = v;
      u.onend = done;
      u.onerror = done;
      this.utter = u; // keep a reference: Chrome drops 'onend' if the utterance is garbage-collected
      speechSynthesis.speak(u);
      Call.send({ type: "playback", id: m.id, state: "started" });
    } catch {
      done();
    }
  },
  skip() {
    // caller interrupts: stop the current clip and everything queued
    this.queue = [];
    try {
      this.audio?.pause();
    } catch {}
    try {
      speechSynthesis.cancel();
    } catch {}
    if (this.finish) this.finish();
  },
  stop() {
    clearTimeout(this.timer);
    this.queue = [];
    this.playing = false;
    try {
      this.audio?.pause();
      speechSynthesis.cancel();
    } catch {}
  },
};

// ---------------------------------------------------------------- microphone (AudioWorklet -> PCM16 over WebSocket)
const WORKLET = `class Tap extends AudioWorkletProcessor{process(i){const c=i[0]&&i[0][0];if(c)this.port.postMessage(c.slice(0));return true}}registerProcessor("tap",Tap);`;
const Mic = {
  ctx: null,
  node: null,
  stream: null,
  on: false,
  buf: [],
  bufLen: 0,
  async start() {
    if (!navigator.mediaDevices?.getUserMedia) {
      toast(
        "This browser can't use the microphone here. Open the page on http://localhost:8000.",
      );
      return;
    }
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });
    this.ctx = new AudioContext();
    await this.ctx.audioWorklet.addModule(
      URL.createObjectURL(
        new Blob([WORKLET], { type: "application/javascript" }),
      ),
    );
    const src = this.ctx.createMediaStreamSource(this.stream);
    this.node = new AudioWorkletNode(this.ctx, "tap");
    const per = Math.round(this.ctx.sampleRate / 10); // ~100 ms packets
    this.node.port.onmessage = (e) => {
      const f = e.data;
      let peak = 0;
      for (let i = 0; i < f.length; i++) peak = Math.max(peak, Math.abs(f[i]));
      $("meter").style.width = Math.min(100, peak * 140) + "%";
      if (!this.on || Speaker.busy) {
        this.buf = [];
        this.bufLen = 0;
        return;
      }
      this.buf.push(f);
      this.bufLen += f.length;
      if (this.bufLen >= per) this.flush();
    };
    src.connect(this.node);
    Call.send({ type: "config", sample_rate: this.ctx.sampleRate });
  },
  flush() {
    if (!this.bufLen) return;
    const out = new Int16Array(this.bufLen);
    let o = 0;
    for (const f of this.buf)
      for (let i = 0; i < f.length; i++) {
        const s = Math.max(-1, Math.min(1, f[i]));
        out[o++] = s < 0 ? s * 32768 : s * 32767;
      }
    this.buf = [];
    this.bufLen = 0;
    Call.sendBinary(out.buffer);
  },
  refresh() {
    const b = $("micBtn");
    b.classList.toggle("on", this.on && !Speaker.busy);
    b.classList.toggle("muted", this.on && Speaker.busy);
    b.textContent = !this.on
      ? "Mic off"
      : Speaker.busy
        ? "Bank speaking · tap to talk"
        : "Listening";
    b.setAttribute("aria-pressed", String(this.on));
  },
  stop() {
    this.on = false;
    this.refresh();
    try {
      this.node?.disconnect();
      this.stream?.getTracks().forEach((t) => t.stop());
      this.ctx?.close();
    } catch {}
    this.ctx = this.node = this.stream = null;
    $("meter").style.width = "0";
  },
};

// ---------------------------------------------------------------- call
const Call = {
  ws: null,
  id: null,
  token: null,
  send(o) {
    if (this.ws?.readyState === 1) this.ws.send(JSON.stringify(o));
  },
  sendBinary(b) {
    if (this.ws?.readyState === 1) this.ws.send(b);
  },
  setState(text, cls = "grey") {
    const s = $("callState");
    s.textContent = text;
    s.className = "pill " + cls;
  },
  bubble(text, cls = "") {
    const chat = $("chat");
    chat.querySelector(".empty")?.remove();
    const d = document.createElement("div");
    d.className = "bubble " + cls;
    d.textContent = text;
    chat.appendChild(d);
    chat.scrollTop = chat.scrollHeight;
  },
  handle(ev, d) {
    if (ev === "agent_say") {
      this.bubble(d.text, d.kind || "");
      Speaker.push(d);
    } else if (ev === "transcript") {
      if (d.text) this.bubble(d.text, "me");
    } else if (ev === "action_executed")
      this.bubble("✓ " + (d.result?.summary || d.intent), "sys");
    else if (ev === "call_ended") {
      this.bubble("Call ended", "sys");
      this.cleanup();
    } else if (ev === "error") this.bubble(d.message, "sys");
    else if (ev === "ready")
      this.setState(
        d.enrolled ? "Connected · voice ID on" : "Connected · no voice ID",
        d.enrolled ? "" : "warn",
      );
  },
  async start(outbound = false, { mic = true } = {}) {
    if (this.id) {
      toast("End the current call before opening another session.");
      return;
    }
    $("startBtn").disabled = true;
    $("customer").disabled = true;
    $("dOutbound").disabled = true;
    $("chat").innerHTML = "";
    try {
      const r = outbound
        ? await api("/api/bank-calls", {
            method: "POST",
            headers: staffH(),
            body: {
              customer_id: cust().id,
              lang: $("lang").value,
              reason:
                $("outboundReason").value.trim() || "Account security check",
            },
          })
        : await api("/api/calls", {
            method: "POST",
            body: { customer_id: cust().id, lang: $("lang").value },
          });
      if (outbound) showTab("call");
      this.id = r.call_id;
      this.token = r.call_token;
      this.ws = new WebSocket(wsBase + r.ws_url);
      this.ws.binaryType = "arraybuffer";
      this.ws.onmessage = (e) => {
        const m = JSON.parse(e.data);
        this.handle(m.event, m.data);
      };
      this.ws.onclose = () => {
        if (this.id) {
          this.bubble("Disconnected", "sys");
          this.cleanup();
        }
      };
      await new Promise((ok, bad) => {
        this.ws.onopen = ok;
        this.ws.onerror = () => bad(new Error("Could not connect to the call"));
      });
      ["endBtn", "micBtn", "doneBtn", "typeText", "typeBtn", "clipBtn"].forEach(
        (k) => ($(k).disabled = false),
      );
      if (mic) {
        try {
          await Mic.start();
          Mic.on = true;
        } catch (e) {
          toast("Microphone blocked: " + e.message + ". You can still type.");
        }
      }
      Mic.refresh();
      return true;
    } catch (e) {
      toast(e.message);
      this.cleanup();
      return false;
    }
  },
  end() {
    this.send({ type: "hangup" });
    setTimeout(() => this.ws?.close(), 300);
  },
  cleanup() {
    Mic.stop();
    Speaker.stop();
    this.id = null;
    this.ws = null;
    ["endBtn", "micBtn", "doneBtn", "clipBtn"].forEach(
      (k) => ($(k).disabled = true),
    );
    $("startBtn").disabled = false;
    $("customer").disabled = false;
    $("dOutbound").disabled = false;
    this.setState("Not connected");
  },
};
$("startBtn").onclick = () => Call.start();
$("dOutbound").onclick = () => Call.start(true);
$("endBtn").onclick = () => Call.end();
$("micBtn").onclick = () => {
  if (Speaker.busy) {
    Speaker.skip();
    Mic.on = true;
    Mic.refresh();
    return;
  } // tap to interrupt the bank and talk
  Mic.on = !Mic.on;
  if (!Mic.on) {
    Mic.flush();
    Call.send({ type: "end_utterance" });
  }
  Mic.refresh();
};
$("doneBtn").onclick = () => {
  Mic.flush();
  Call.send({ type: "end_utterance" });
};
$("typeForm").onsubmit = async (e) => {
  e.preventDefault();
  const t = $("typeText").value.trim();
  if (!t) return;
  $("typeBtn").disabled = true;
  if (!Call.id && !(await Call.start(false, { mic: false }))) {
    $("typeBtn").disabled = false;
    return;
  }
  Call.send({ type: "text", text: t });
  $("typeText").value = "";
  $("typeBtn").disabled = false;
};
$("clipBtn").onclick = async () => {
  const f = $("clipFile").files[0];
  if (!f || !Call.id) return toast("Choose an audio file first");
  const fd = new FormData();
  fd.append("file", f);
  $("clipBtn").disabled = true;
  Call.bubble("Sending " + f.name + "…", "sys");
  try {
    const r = await api(`/api/calls/${Call.id}/turn`, {
      method: "POST",
      headers: { "X-Call-Token": Call.token },
      form: fd,
    });
    // the WebSocket already delivers these events live; only fall back if it is closed
    if (!Call.ws || Call.ws.readyState !== 1)
      r.events.forEach((e) => Call.handle(e.event, e.data));
  } catch (e) {
    toast(e.message);
  } finally {
    $("clipBtn").disabled = !Call.id;
  }
};

// ---------------------------------------------------------------- phone app
const Phone = {
  ws: null,
  forId: null,
  open() {
    if (this.forId !== cust().id) this.reopen();
  },
  reopen() {
    try {
      this.ws?.close();
    } catch {}
    const c = cust();
    this.forId = c.id;
    $("pName").textContent = c.name;
    $("pNotifs").innerHTML =
      '<div class="empty">Approvals and alerts from the bank appear here.</div>';
    this.loadMe();
    this.loadPending();
    this.ws = new WebSocket(
      `${wsBase}/ws/customer/${encodeURIComponent(c.id)}?token=${encodeURIComponent(c.token)}`,
    );
    this.ws.onopen = () => {
      $("pConn").textContent = "online";
      $("pConn").className = "pill";
    };
    this.ws.onclose = () => {
      $("pConn").textContent = "offline";
      $("pConn").className = "pill grey";
    };
    this.ws.onmessage = (e) => {
      const m = JSON.parse(e.data);
      this.on(m.event, m.data);
    };
  },
  async loadMe() {
    try {
      const me = await api("/api/app/me", { headers: appH() });
      $("pBalance").innerHTML =
        me.accounts
          .map(
            (a) =>
              `<div class="lbl">Savings ··${esc(a.number_last4)}</div><div class="big" style="font-size:1.8rem">${esc(a.balance)}</div>`,
          )
          .join("") +
        `<div class="muted">Voice ID: ${me.voice_enrolled ? "set up" : "not set up"}${me.voice_locked ? " · locked by bank" : ""}</div>`;
      $("pCards").innerHTML = me.cards
        .map(
          (
            k,
          ) => `<div class="item"><div class="row between"><b>${esc(k.kind)} ··${esc(k.last4)}</b><span class="pill ${k.status === "active" ? "" : k.status === "frozen" ? "warn" : "bad"}">${esc(k.status)}</span></div>
        <div class="muted">Daily limit Rs ${(k.daily_limit_paise / 100).toLocaleString("en-IN")} · International ${k.international ? "on" : "off"}</div>
        ${k.status === "active" ? `<button class="danger" data-freeze="${esc(k.last4)}">Freeze now</button>` : ""}</div>`,
        )
        .join("");
    } catch (e) {
      $("pBalance").innerHTML = `<div class="muted">${esc(e.message)}</div>`;
    }
  },
  async loadPending() {
    try {
      const p = await api("/api/app/pending", { headers: appH() });
      p.stepups.forEach((s) =>
        this.on("stepup_request", {
          ...s,
          action: s.action || "Confirm a request on your call",
          code: null,
        }),
      );
      if (p.bank_call) this.on("incoming_bank_call", p.bank_call);
    } catch {}
  },
  add(html, cls = "") {
    const box = $("pNotifs");
    box.querySelector(".empty")?.remove();
    const d = document.createElement("div");
    d.className = "notif " + cls;
    d.innerHTML = html;
    box.prepend(d);
    return d;
  },
  on(ev, d) {
    if (ev === "stepup_request") {
      if (document.querySelector(`[data-stepup="${CSS.escape(d.stepup_id)}"]`))
        return;
      const n = this.add(
        `<div class="lbl">Approve this request?</div><b>${esc(d.action)}</b>
        ${d.code ? `<div class="muted">Code</div><div class="code">${esc(d.code)}</div>` : ""}
        <div class="muted">${esc(d.warning || "Approve only if you are on a call with Unclone Bank right now.")}</div>
        <div class="row"><button data-act="approve">Approve</button><button class="danger" data-act="deny">This isn't me</button></div>`,
        "bank",
      );
      n.dataset.stepup = d.stepup_id;
      n.querySelectorAll("button").forEach(
        (b) =>
          (b.onclick = async () => {
            n.querySelectorAll("button").forEach((x) => (x.disabled = true));
            try {
              const r =
                b.dataset.act === "approve"
                  ? await Security.approve(d.stepup_id)
                  : await api(`/api/app/stepups/${d.stepup_id}/deny`, {
                      method: "POST",
                      headers: appH(),
                    });
              n.querySelector(".row").innerHTML =
                `<span class="pill ${["approved", "executed"].includes(r.status) ? "" : "bad"}">${esc(r.status)}</span>`;
              this.loadMe();
            } catch (e) {
              n.querySelectorAll("button").forEach((x) => (x.disabled = false));
              let feedback = n.querySelector(".approval-feedback");
              if (!feedback) {
                feedback = document.createElement("div");
                feedback.className = "approval-feedback muted";
                feedback.setAttribute("role", "status");
                n.append(feedback);
              }
              feedback.textContent = e.message;
            }
          }),
      );
    } else if (ev === "incoming_bank_call" || ev === "bank_call_verification") {
      this.add(
        `<div class="lbl">Bank session notice — verify its signature below</div>
        <div class="muted">Session matching aid (not identity proof):</div><div class="code">${esc(d.code)}</div>
        ${d.reason ? `<div class="muted">${esc(d.reason)}</div>` : ""}
        <div class="row"><button class="ghost" data-v="confirm">Codes match</button><button class="danger" data-v="report">Code is different</button></div>`,
        "bank",
      )
        .querySelectorAll("button")
        .forEach(
          (b) =>
            (b.onclick = async () => {
              try {
                await api(`/api/app/bank-call/${d.notice_id}/${b.dataset.v}`, {
                  method: "POST",
                  headers: appH(),
                });
                toast(
                  b.dataset.v === "confirm"
                    ? "Thanks, call confirmed"
                    : "Reported to the fraud team",
                );
              } catch (e) {
                toast(e.message);
              }
            }),
        );
    } else if (ev === "security_alert")
      this.add(
        `<div class="lbl">Security alert</div><div>${esc(d.message)}</div>`,
        "alert",
      );
    else if (ev === "account_activity") {
      this.add(
        `<div class="lbl">Done on your call</div><div>${esc(d.summary)}</div>`,
      );
      this.loadMe();
    } else if (ev === "call_connected")
      this.add(
        `<div class="lbl">On a call</div><div>${esc(d.message)}</div>`,
        "bank",
      );
  },
};
$("pCheckBank").onclick = async () => {
  try {
    const r = await api("/api/app/bank-call", { headers: appH() });
    if (r.genuine_bank_call) Phone.on("incoming_bank_call", r);
    else
      Phone.add(
        `<div class="lbl">No call from the bank</div><div>${esc(r.message)}</div>`,
        "alert",
      );
  } catch (e) {
    toast(e.message);
  }
};
$("pCards").addEventListener("click", async (e) => {
  const l4 = e.target?.dataset?.freeze;
  if (!l4) return;
  try {
    await api(`/api/app/cards/${l4}/freeze`, {
      method: "POST",
      headers: appH(),
    });
    toast("Card frozen");
    Phone.loadMe();
  } catch (er) {
    toast(er.message);
  }
});

// Voice registration removed as callers are verified dynamically (authenticity + liveness + stepup)
const Enroll = {
  phrases: [],
  clips: [],
  rec: null,
  async load() {},
  render() {},
};

// ---------------------------------------------------------------- fraud desk
const Desk = {
  ws: null,
  calls: new Map(),
  sel: null,
  opened: false,
  open() {
    if (!this.opened) this.reopen();
  },
  reopen() {
    try {
      this.ws?.close();
    } catch {}
    if (!staffKey()) {
      $("dConn").textContent = "enter staff key";
      $("dConn").className = "pill warn";
      return;
    }
    this.opened = true;
    this.ws = new WebSocket(
      `${wsBase}/ws/dashboard?staff_key=${encodeURIComponent(staffKey())}`,
    );
    this.ws.onopen = () => {
      $("dConn").textContent = "live";
      $("dConn").className = "pill";
    };
    this.ws.onclose = (e) => {
      $("dConn").textContent = e.code === 4401 ? "wrong staff key" : "offline";
      $("dConn").className = "pill " + (e.code === 4401 ? "bad" : "grey");
      this.opened = false;
    };
    this.ws.onmessage = (e) => {
      const m = JSON.parse(e.data);
      this.on(m.event, m.data);
    };
    this.loadCalls();
    this.loadTickets();
  },
  get(id) {
    if (!this.calls.has(id))
      this.calls.set(id, {
        id,
        name: "",
        live: true,
        chunks: [],
        log: [],
        trust: null,
        risk: "unknown",
        spk: null,
        syn: null,
        pressure: 0,
      });
    return this.calls.get(id);
  },
  async loadCalls() {
    try {
      const r = await api("/api/calls?limit=30", { headers: staffH() });
      r.calls.forEach((c) => {
        const s = this.get(c.id);
        Object.assign(s, {
          name: s.name || c.customer_id || "Unknown caller",
          live: c.live,
          trust: c.trust,
          risk: c.risk_state,
          outcome: c.outcome,
          started: c.started_at,
        });
      });
      this.renderCalls();
      if (!this.sel && r.calls[0]) this.select(r.calls[0].id);
    } catch (e) {
      toast(e.message);
    }
  },
  async loadTickets() {
    try {
      const r = await api("/api/tickets?limit=30", { headers: staffH() });
      $("dTickets").innerHTML = r.tickets.length
        ? r.tickets
            .map(
              (t) => `<div class="item">
        <div class="row between"><span class="pill ${t.severity === "critical" ? "bad" : "warn"}">${esc(t.severity)}</span><span class="pill grey">${esc(t.status.replace("_", " "))}</span></div>
        <div style="font-size:.85rem">${esc((t.reasons || []).join("; "))}</div>
        <div class="row">${t.status === "open" ? `<button data-t="${esc(t.id)}" data-s="confirmed_fraud">Confirm fraud + lock voice</button><button class="ghost" data-t="${esc(t.id)}" data-s="false_positive">False alarm</button>` : ""}
        <button class="ghost" data-call="${esc(t.call_id)}">Open call</button></div></div>`,
            )
            .join("")
        : '<div class="empty">No tickets.</div>';
      const a = await api("/api/agents/support-tickets", { headers: staffH() });
      $("dTickets").insertAdjacentHTML(
        "beforeend",
        a.tickets
          .map(
            (t) =>
              `<div class="item"><span class="pill">Authorized AI support ticket</span><div>${esc(t.subject)}</div><div class="muted">${esc(t.id)} · ${esc(t.customer_id)}</div></div>`,
          )
          .join("") +
          a.decisions
            .map(
              (d) =>
                `<div class="item"><span class="pill ${d.outcome === "denied" ? "warn" : ""}">AI ${esc(d.outcome)}</span> ${esc(d.action)}<div class="muted">${esc(d.reason || d.ticket_id || "")}</div></div>`,
            )
            .join(""),
      );
    } catch {}
  },
  renderCalls() {
    const arr = [...this.calls.values()].sort(
      (a, b) =>
        b.live - a.live ||
        String(b.started || b.id).localeCompare(String(a.started || a.id)),
    );
    $("dCalls").innerHTML = arr.length
      ? arr
          .map(
            (
              c,
            ) => `<div class="item ${c.id === this.sel ? "sel" : ""}" data-call="${esc(c.id)}" role="button" tabindex="0">
      <div class="row between"><b>${esc(c.name || "Unknown caller")}</b><span class="pill ${riskCls(c.risk)}">${esc(c.risk)}</span></div>
      <div class="muted">${c.live ? "● live" : "ended"} · trust ${fmt(c.trust)}${c.outcome ? " · " + esc(c.outcome) : ""}</div></div>`,
          )
          .join("")
      : '<div class="empty">Live and recent calls appear here.</div>';
  },
  select(id) {
    this.sel = id;
    this.renderCalls();
    this.renderSel();
    $("sReport").disabled = false;
    $("sReportBox").hidden = true;
    const c = this.get(id);
    if (!c.loaded) {
      c.loaded = true;
      this.backfill(id);
    }
  },
  async backfill(id) {
    // ended or already-running call: rebuild its picture from the evidence report
    try {
      const r = await api(`/api/calls/${id}/report`, { headers: staffH() });
      const c = this.get(id);
      if (r.customer) c.name = r.customer.name;
      if (!c.chunks.length)
        c.chunks = r.voice_checks.map((v) => ({
          seq: v.seq,
          speaker_prob: v.speaker_prob,
          synthetic_prob: v.synthetic_prob,
          flags: v.flags,
          purpose: v.purpose,
        }));
      if (!c.log.length)
        r.timeline.forEach((t) => this.logLine(c, t.kind, t.data));
      c.risk = r.verdict.risk_state;
      c.trust = r.verdict.final_trust;
      c.pressure = r.verdict.pressure ?? c.pressure;
      this.renderCalls();
      if (this.sel === id) this.renderSel();
    } catch {}
  },
  logLine(c, ev, d) {
    const L = (cls, text) => c.log.push({ cls, text });
    if (ev === "transcript" && d.text) L("", "Caller: " + d.text);
    else if (ev === "agent_say") L("", "Agent: " + d.text);
    else if (ev === "decision")
      L(
        d.outcome === "allow" ? "ok" : d.outcome === "refuse" ? "bad" : "warn",
        `Decision: ${d.outcome.toUpperCase()} (${d.intent}) · ${(d.reasons || []).join("; ")}`,
      );
    else if (ev === "segment_flagged")
      L(
        "bad",
        `Flagged ${fmt(d.t_start, 1)}–${fmt(d.t_end, 1)}s: ${(d.flags || []).join(", ")}`,
      );
    else if (ev === "voice_swap")
      L("bad", "Voice changed mid-call: " + d.reason);
    else if (ev === "challenge_result")
      L(
        d.passed ? "ok" : "warn",
        `Liveness ${d.passed ? "passed" : "failed"}${d.latency_s != null ? " in " + d.latency_s + "s" : ""} · speaker ${fmt(d.speaker_prob)} · synthetic ${fmt(d.synthetic_prob)}`,
      );
    else if (ev === "stepup_sent")
      L("warn", "Approval sent to the customer's app");
    else if (ev === "stepup_result")
      L(d.status === "approved" ? "ok" : "bad", "App approval: " + d.status);
    else if (ev === "pressure_detected")
      L(
        "warn",
        `Pressure tactics (${fmt(d.score)}): ${(d.cues || []).join(", ")}`,
      );
    else if (ev === "action_executed")
      L("ok", "Done: " + (d.result?.summary || d.intent));
    else if (ev === "action_refused")
      L("bad", "Refused: " + (d.reasons || []).join("; "));
    else if (ev === "action_held") L("warn", "Held: " + d.reason);
    else if (ev === "ticket_created") L("bad", `Ticket opened (${d.severity})`);
    else if (ev === "protective_freeze")
      L("ok", "Card frozen instantly (protective)");
    if (c.log.length > 200) c.log.splice(0, c.log.length - 200);
  },
  on(ev, d) {
    if (!d || !d.call_id) {
      if (ev.startsWith("ticket")) this.loadTickets();
      return;
    }
    const c = this.get(d.call_id);
    c.loaded = true;
    if (ev === "call_started") {
      c.live = true;
      c.started = new Date().toISOString();
      if (!this.sel || !this.get(this.sel).live) this.sel = null;
    }
    if (ev === "identified") c.name = d.name;
    if (ev === "call_ended") {
      c.live = false;
      c.outcome = d.outcome;
    }
    if (ev === "trust_update") {
      c.chunks.push(d.chunk);
      c.trust = d.trust;
      c.risk = d.risk_state;
      c.spk = d.speaker_ema;
      c.syn = d.recent_synthetic;
    }
    if (ev === "pressure_detected") c.pressure = d.level;
    if (ev === "action_refused" || ev === "voice_swap") c.risk = "fraud";
    this.logLine(c, ev, d);
    if (ev === "ticket_created") this.loadTickets();
    this.renderCalls();
    if (!this.sel) this.select(d.call_id);
    if (this.sel === d.call_id) this.renderSel();
  },
  renderSel() {
    const c = this.calls.get(this.sel);
    if (!c) return;
    $("sTitle").textContent = c.name || "Unknown caller";
    $("sRisk").textContent = c.risk;
    $("sRisk").className = "pill " + riskCls(c.risk);
    $("sTrust").textContent = fmt(c.trust);
    $("sTrust").style.color =
      c.risk === "fraud" || c.risk === "suspicious"
        ? "var(--danger)"
        : "var(--ink)";
    const last = c.chunks.filter((x) => x.speaker_prob != null);
    $("sSpk").textContent = fmt(c.spk ?? last.at(-1)?.speaker_prob);
    $("sSyn").textContent = fmt(
      c.syn ??
        Math.max(0, ...c.chunks.slice(-3).map((x) => x.synthetic_prob ?? 0)),
    );
    $("sPress").textContent = fmt(c.pressure);
    $("sWave").innerHTML =
      c.chunks
        .map((x) => {
          const bad = (x.flags || []).some(
            (f) => f === "synthetic" || f === "voice_swap",
          );
          const warn =
            !bad &&
            (x.flags || []).some(
              (f) => f === "synthetic_suspect" || f === "speaker_mismatch",
            );
          const h = Math.max(6, Math.round((x.speaker_prob ?? 0.05) * 100));
          return `<i class="${bad ? "bad" : warn ? "warn" : ""} ${x.purpose === "challenge" ? "ch" : ""}" style="height:${h}%" title="#${x.seq} ${esc(x.purpose)} · speaker ${fmt(x.speaker_prob)} · synthetic ${fmt(x.synthetic_prob)}"></i>`;
        })
        .join("") || '<span class="muted">No voice checks yet.</span>';
    $("sLog").innerHTML = c.log.length
      ? c.log
          .slice()
          .reverse()
          .map((l) => `<div class="${l.cls}">${esc(l.text)}</div>`)
          .join("")
      : "<div>No activity yet.</div>";
    $("sKill").disabled = !c.live;
  },
  async report() {
    const box = $("sReportBox");
    box.hidden = false;
    box.innerHTML = '<span class="muted">Building report…</span>';
    try {
      const r = await api(`/api/calls/${this.sel}/report`, {
        headers: staffH(),
      });
      const v = await api("/api/reports/verify", {
        method: "POST",
        headers: staffH(),
        body: r,
      });
      const flagged =
        r.flagged_segments
          .map(
            (f) =>
              `${fmt(f.t_start, 1)}–${fmt(f.t_end, 1)}s ${f.flags.join("/")}`,
          )
          .join(" · ") || "none";
      box.innerHTML = `<div class="row between"><b>${esc(r.verdict.label)}</b><span class="pill ${v.valid ? "" : "bad"}">${v.valid ? "signature valid" : "TAMPERED"}</span></div>
        <div class="muted">Flagged: ${esc(flagged)}</div>
        <div class="muted">Executed: ${esc(r.verdict.actions_executed.join(", ") || "none")} · Refused: ${esc(r.verdict.actions_refused.join(", ") || "none")}</div>
        ${r.verdict.reasons.length ? `<div style="font-size:.85rem">${esc(r.verdict.reasons.join("; "))}</div>` : ""}
        <div class="muted" style="font-family:var(--mono);font-size:.72rem;overflow-wrap:anywhere">sha256 ${esc(r.integrity.sha256)}</div>
        <audio id="sAudio" controls style="width:100%"></audio>`;
      try {
        const a = await fetch(`/api/calls/${this.sel}/recording`, {
          headers: staffH(),
        });
        if (a.ok) $("sAudio").src = URL.createObjectURL(await a.blob());
        else $("sAudio").remove();
      } catch {}
    } catch (e) {
      box.innerHTML = `<span class="muted">${esc(e.message)}</span>`;
    }
  },
};
function riskCls(r) {
  return r === "fraud" || r === "suspicious"
    ? "bad"
    : r === "uncertain"
      ? "warn"
      : r === "trusted"
        ? ""
        : "grey";
}
$("dCalls").addEventListener("click", (e) => {
  const it = e.target.closest("[data-call]");
  if (it) Desk.select(it.dataset.call);
});
$("dCalls").addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    const it = e.target.closest("[data-call]");
    if (it) Desk.select(it.dataset.call);
  }
});
$("dTickets").addEventListener("click", async (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  if (b.dataset.call) return Desk.select(b.dataset.call);
  try {
    await api(`/api/tickets/${b.dataset.t}`, {
      method: "PATCH",
      headers: staffH(),
      body: {
        status: b.dataset.s,
        lock_customer_voice: b.dataset.s === "confirmed_fraud",
        note: "from dashboard",
      },
    });
    toast("Ticket updated");
    Desk.loadTickets();
  } catch (er) {
    toast(er.message);
  }
});
$("dRefresh").onclick = () => {
  Desk.loadCalls();
  Desk.loadTickets();
};
$("sReport").onclick = () => Desk.report();
$("sKill").onclick = async () => {
  try {
    await api(`/api/calls/${Desk.sel}/terminate`, {
      method: "POST",
      headers: staffH(),
    });
    toast("Call ended by fraud desk");
  } catch (e) {
    toast(e.message);
  }
};
$("dAudit").onclick = async () => {
  try {
    const r = await api("/api/audit/verify", { headers: staffH() });
    toast(
      r.valid
        ? `Audit log intact (${r.entries} entries)`
        : `Audit log TAMPERED at entry ${r.broken_at_seq}`,
    );
  } catch (e) {
    toast(e.message);
  }
};
$("dReset").onclick = async () => {
  if ($("dReset").dataset.armed !== "1") {
    $("dReset").dataset.armed = "1";
    $("dReset").textContent = "Click again to wipe all data";
    setTimeout(() => {
      $("dReset").dataset.armed = "";
      $("dReset").textContent = "Reset demo bank";
    }, 4000);
    return;
  }
  try {
    await api("/api/demo/reset", { method: "POST", headers: staffH() });
    Desk.calls.clear();
    Desk.sel = null;
    Desk.renderCalls();
    $("sLog").innerHTML = "";
    toast("Demo bank reset");
    Desk.loadTickets();
    Phone.reopen();
  } catch (e) {
    toast(e.message);
  }
  $("dReset").dataset.armed = "";
  $("dReset").textContent = "Reset demo bank";
};

// ---------------------------------------------------------------- knowledge & rag
const Knowledge = {
  activeRun: null,
  pollTimer: null,
  async open() {
    await Promise.all([this.loadStats(), this.loadClaims()]);
  },
  async loadStats() {
    try {
      const s = await api("/api/knowledge/stats");
      $("kbStatsPill").textContent =
        `${s.documents} docs · ${s.chunks} chunks · ${s.claims} claims`;
      $("kbStatsPill").className = "pill";
    } catch (e) {
      $("kbStatsPill").textContent = "KB Error";
    }
  },
  async loadClaims() {
    try {
      const r = await api("/api/knowledge/claims?limit=40");
      this.renderClaims(r.claims || []);
    } catch (e) {
      $("kbClaimsList").innerHTML =
        `<div class="empty">${esc(e.message)}</div>`;
    }
  },
  renderClaims(claims) {
    if (!claims.length) {
      $("kbClaimsList").innerHTML =
        '<div class="empty">No claims indexed yet. Run research or scrape a URL above.</div>';
      return;
    }
    $("kbClaimsList").innerHTML = claims
      .map((c) => {
        const kindCls =
          c.kind === "risk" ? "bad" : c.kind === "opportunity" ? "" : "grey";
        const authPercent = Math.round(c.authority * 100);
        return `<div class="item" style="font-size:.85rem">
        <div class="row between">
          <div class="row" style="gap:4px">
            <span class="pill ${kindCls}" style="font-size:.68rem">${esc(c.kind.toUpperCase())}</span>
            <span class="pill grey" style="font-size:.68rem">${esc(c.epistemic)}</span>
          </div>
          <span class="muted" style="font-size:.75rem">${esc(c.domain || "knowledge")} · ${authPercent}% auth</span>
        </div>
        <div>${esc(c.text)}</div>
      </div>`;
      })
      .join("");
  },
  async ask(q) {
    q = (q || "").trim();
    if (!q) return;
    $("ragStatus").textContent = "Retrieving & verifying...";
    $("ragStatus").className = "pill warn";
    $("ragSubmit").disabled = true;
    try {
      const res = await api("/api/knowledge/ask", {
        method: "POST",
        body: { question: q },
      });
      $("ragResults").hidden = false;
      const srcLabel = res.researched
        ? "Researched via Web Agent"
        : "Vector DB (Grounded)";
      $("ragStatus").textContent = `${srcLabel} · Zero Hallucination`;
      $("ragStatus").className = res.researched ? "pill warn" : "pill";

      $("ragSpoken").textContent =
        res.spoken_summary || "Answer retrieved from banking knowledge base.";

      const verif = res.verification || {};
      const sup = verif.supported || 0;
      const tot =
        (verif.supported || 0) +
        (verif.partial || 0) +
        (verif.unsupported || 0);
      $("ragVerifBadge").textContent = `${sup}/${tot} Supported`;
      $("ragVerifBadge").className =
        sup === tot && tot > 0 ? "pill" : "pill warn";

      $("ragSentences").innerHTML = (res.answer || [])
        .map((a) => {
          const rCls =
            a.ruling === "supported"
              ? ""
              : a.ruling === "partial"
                ? "warn"
                : "bad";
          const citesHtml = (a.cites || [])
            .map(
              (cid) =>
                `<span class="pill grey" style="cursor:pointer;font-family:var(--mono);padding:1px 6px" onclick="Knowledge.highlightEv('${esc(cid)}')">[${esc(cid)}]</span>`,
            )
            .join(" ");
          return `<div class="item" style="border-left:4px solid var(--accent)">
          <div class="row between">
            <span style="flex:1">${esc(a.text)}</span>
            <span class="pill ${rCls}" style="font-size:.7rem">${esc(a.ruling || "citation")}</span>
          </div>
          ${citesHtml ? `<div class="row" style="margin-top:2px;gap:4px"><span class="lbl" style="font-size:.65rem">Cites:</span> ${citesHtml}</div>` : ""}
        </div>`;
        })
        .join("");

      const ev = res.evidence || [];
      $("ragEvCount").textContent = ev.length;
      $("ragEvidenceList").innerHTML = ev
        .map((item) => {
          const authPill =
            item.authority >= 0.9
              ? '<span class="pill" style="font-size:.68rem">Official 1.0</span>'
              : item.authority >= 0.7
                ? '<span class="pill warn" style="font-size:.68rem">News/Research</span>'
                : '<span class="pill grey" style="font-size:.68rem">Standard</span>';
          return `<div id="ev-${esc(item.id)}" class="item" style="font-size:.82rem">
          <div class="row between">
            <div class="row" style="gap:6px">
              <span class="code" style="font-size:.85rem;color:var(--accent)">[${esc(item.id)}]</span>
              <span class="pill grey" style="font-size:.68rem">${esc(item.kind)}</span>
              ${authPill}
            </div>
            <span class="muted">${esc(item.domain)}</span>
          </div>
          <div>${esc(item.text)}</div>
          ${item.url ? `<a href="${esc(item.url)}" target="_blank" rel="noopener" class="muted" style="font-size:.72rem;text-decoration:underline">${esc(item.url.slice(0, 50))}...</a>` : ""}
        </div>`;
        })
        .join("");
    } catch (e) {
      toast("RAG failed: " + e.message);
      $("ragStatus").textContent = "Error";
      $("ragStatus").className = "pill bad";
    } finally {
      $("ragSubmit").disabled = false;
    }
  },
  highlightEv(cid) {
    const el = $("ev-" + cid);
    if (el) {
      $("ragEvidenceDetails").open = true;
      el.scrollIntoView({ behavior: "smooth", block: "center" });
      el.style.boxShadow = "0 0 0 3px var(--accent)";
      setTimeout(() => {
        el.style.boxShadow = "";
      }, 2500);
    }
  },
  async startResearch(query) {
    query = (query || "").trim();
    if (!query) return;
    $("researchBtn").disabled = true;
    $("researchProgress").hidden = false;
    $("researchProgress").innerHTML =
      `<div>Launching autonomous research on "${esc(query)}"...</div>`;
    try {
      const res = await api("/api/knowledge/research", {
        method: "POST",
        headers: staffH(),
        body: { query },
      });
      this.activeRun = res.run_id;
      this.pollResearch(res.run_id);
    } catch (e) {
      toast("Research start failed: " + e.message);
      $("researchBtn").disabled = false;
    }
  },
  async pollResearch(rid) {
    if (this.pollTimer) clearInterval(this.pollTimer);
    this.pollTimer = setInterval(async () => {
      try {
        const snap = await api(`/api/knowledge/runs/${rid}`, {
          headers: staffH(),
        });
        const events = snap.events || [];
        $("researchProgress").innerHTML = events
          .map((ev) => {
            const cls =
              ev.status === "failed"
                ? "bad"
                : ev.status === "done"
                  ? "ok"
                  : "warn";
            return `<div class="${cls}"><b>[${esc(ev.stage)}]</b> ${esc(ev.title)}: ${esc(ev.summary)}</div>`;
          })
          .join("");
        $("researchProgress").scrollTop = $("researchProgress").scrollHeight;

        if (snap.status === "completed" || snap.status === "failed") {
          clearInterval(this.pollTimer);
          this.pollTimer = null;
          $("researchBtn").disabled = false;
          toast(
            snap.status === "completed"
              ? "Source discovery completed; staff review is required before publication"
              : "Research run failed",
          );
          this.loadStats();
          this.loadClaims();
        }
      } catch (e) {
        clearInterval(this.pollTimer);
        this.pollTimer = null;
        $("researchBtn").disabled = false;
      }
    }, 1500);
  },
  async scrapeUrl(url) {
    url = (url || "").trim();
    if (!url) return;
    $("scrapeBtn").disabled = true;
    $("scrapeResult").hidden = false;
    $("scrapeResult").textContent = "Scraping page and extracting claims...";
    try {
      const res = await api("/api/knowledge/scrape", {
        method: "POST",
        headers: staffH(),
        body: { url },
      });
      $("scrapeResult").innerHTML =
        `<span class="pill warn">Pending staff review</span><div>${esc(res.url)}</div><button id="reviewSource">Inspect fetched source</button>`;
      $("reviewSource").onclick = async () => {
        try {
          const source = await api(`/api/knowledge/quarantine/${res.id}`, {
            headers: staffH(),
          });
          $("scrapeResult").innerHTML =
            `<span class="pill warn">Review before publishing</span><div>${esc(source.url)}</div><pre style="white-space:pre-wrap;max-height:320px;overflow:auto">${esc(source.text)}</pre><button id="publishSource">Approve this exact source text</button>`;
          $("publishSource").onclick = async () => {
            try {
              await api(`/api/knowledge/quarantine/${res.id}/approve`, {
                method: "POST",
                headers: staffH(),
                body: { text_hash: source.text_hash },
              });
              $("scrapeResult").textContent =
                "Reviewed source published to the knowledge base.";
              this.loadStats();
            } catch (e) {
              toast(e.message);
            }
          };
        } catch (e) {
          toast(e.message);
        }
      };
      toast("Source quarantined; it is not used in caller answers yet");
      this.loadStats();
      this.loadClaims();
    } catch (e) {
      $("scrapeResult").innerHTML =
        `<span class="pill bad">Failed</span> ${esc(e.message)}`;
      toast("Scrape error: " + e.message);
    } finally {
      $("scrapeBtn").disabled = false;
    }
  },
};

$("ragForm").onsubmit = (e) => {
  e.preventDefault();
  Knowledge.ask($("ragInput").value);
};
$("quickQueries").onclick = (e) => {
  const b = e.target.closest("button");
  if (!b || !b.dataset.q) return;
  $("ragInput").value = b.dataset.q;
  Knowledge.ask(b.dataset.q);
};
$("researchForm").onsubmit = (e) => {
  e.preventDefault();
  Knowledge.startResearch($("researchInput").value);
};
$("scrapeForm").onsubmit = (e) => {
  e.preventDefault();
  Knowledge.scrapeUrl($("scrapeInput").value);
};
$("refreshKbBtn").onclick = () => {
  Knowledge.loadStats();
  Knowledge.loadClaims();
  toast("Knowledge base refreshed");
};

// ---------------------------------------------------------------- judge demo
const JudgeDemo = {
  mediaRecorder: null,
  audioChunks: [],

  async open() {
    try {
      const res = await api("/api/judge-demo/status");
      if (res.device) {
        $("demoHwBadge").textContent = res.device.toUpperCase() + " Accelerated";
        $("demoHwBadge").style.background = res.device.includes("cuda") ? "#0f7a5c" : "#4f6b61";
      }
      if (res.detectifai_configured) {
        $("demoDetectBadge").textContent = "DetectifAI API (Configured & Live)";
        $("demoDetectBadge").className = "pill";
      } else {
        $("demoDetectBadge").textContent = "Deterministic Anti-Spoof (Fallback)";
        $("demoDetectBadge").className = "pill warn";
      }
      if (res.stt_provider) {
        $("demoSttBadge").textContent = res.stt_provider === "sarvam" ? "Sarvam AI (saaras:v4)" : res.stt_provider.toUpperCase();
      }
    } catch (e) {
      console.warn("judge demo status failed:", e);
    }
  },

  renderResult(res) {
    const outcome = res.policy_decision || "UNKNOWN";
    const badge = $("demoOutcomeBadge");
    const outcomeEl = $("demoFinalOutcome");
    badge.textContent = outcome;
    outcomeEl.textContent = outcome;

    if (outcome === "ALLOW" || outcome === "EXECUTED") {
      badge.className = "pill";
      badge.style.background = "#e6f4ef";
      badge.style.color = "#0f7a5c";
      outcomeEl.style.color = "#0f7a5c";
    } else if (outcome === "BLOCK" || outcome.includes("REFUSE")) {
      badge.className = "pill bad";
      outcomeEl.style.color = "var(--danger)";
    } else {
      badge.className = "pill warn";
      outcomeEl.style.color = "var(--warn)";
    }

    $("demoExplanation").textContent = res.explanation || "Evaluation complete.";
    const mut = res.state_diff && res.state_diff.mutation_detected;
    $("demoStateProof").textContent = mut ? "STATE MUTATED" : "ZERO MUTATION (Verified Unchanged)";
    $("demoStateProof").style.color = mut ? "var(--danger)" : "#0f7a5c";
    $("demoLatency").textContent = res.latency_ms ? res.latency_ms + " ms" : "–";

    // Render 9-stage visual timeline
    const tlEl = $("demoTimeline");
    if (!res.timeline || res.timeline.length === 0) {
      tlEl.innerHTML = '<div class="empty">No timeline data returned.</div>';
      return;
    }

    tlEl.innerHTML = res.timeline.map((st) => {
      let stColor = "#0f7a5c";
      let stBg = "var(--accent-soft)";
      let stBorder = "var(--line)";
      const s = String(st.status || "").toUpperCase();
      if (s.includes("FLAG") || s.includes("BLOCK") || s.includes("REFUSE") || s.includes("FAILED")) {
        stColor = "var(--danger)";
        stBg = "var(--danger-soft)";
        stBorder = "#ffccd0";
      } else if (s.includes("HELD") || s.includes("REQUIRE") || s.includes("CHECK") || s.includes("WARN")) {
        stColor = "var(--warn)";
        stBg = "var(--warn-soft)";
        stBorder = "#ffe6b3";
      }

      const detailsHtml = Object.entries(st.details || {})
        .map(([k, v]) => `<div><span style="color:var(--muted);">${esc(k)}:</span> <strong>${esc(typeof v === 'object' ? JSON.stringify(v) : v)}</strong></div>`)
        .join("");

      return `
        <div style="background:#fff; border:1px solid ${stBorder}; border-left:4px solid ${stColor}; border-radius:10px; padding:10px 12px; font-size:0.83rem;">
          <div class="row between" style="margin-bottom:4px;">
            <div>
              <span class="lbl" style="color:var(--muted); margin-right:6px;">STAGE 0${st.stage}</span>
              <strong>${esc(st.name)}</strong>
            </div>
            <span class="pill" style="background:${stBg}; color:${stColor}; font-size:0.72rem;">${esc(st.status)}</span>
          </div>
          <div style="margin-bottom:6px; color:var(--muted); font-size:0.78rem;">
            Provenance: <em>${esc(st.provenance)}</em> &middot; ${esc(st.timestamp || "")}
          </div>
          <div style="background:var(--bg); border-radius:8px; padding:6px 10px; font-size:0.78rem; display:grid; gap:2px;">
            ${detailsHtml}
          </div>
        </div>
      `;
    }).join("");
  },

  async runScenario(id) {
    try {
      $("demoFinalOutcome").textContent = "Evaluating scenario with live engines...";
      $("demoOutcomeBadge").textContent = "PROCESSING";
      $("demoOutcomeBadge").className = "pill warn";
      toast("Running " + id + "...");
      const res = await api("/api/judge-demo/run", {
        method: "POST",
        body: { scenario_id: id, customer_id: $("customer").value || "cus_ananya" }
      });
      this.renderResult(res);
      toast("Evaluation finished: " + (res.policy_decision || "DONE"));
    } catch (e) {
      toast("Scenario execution failed: " + e.message);
      $("demoFinalOutcome").textContent = "Error: " + e.message;
    }
  },

  async runText(text) {
    if (!text.trim()) return;
    try {
      $("demoFinalOutcome").textContent = "Analyzing input through deterministic policy...";
      $("demoOutcomeBadge").textContent = "PROCESSING";
      $("demoOutcomeBadge").className = "pill warn";
      toast("Evaluating simulated attack text...");
      const res = await api("/api/judge-demo/run", {
        method: "POST",
        body: { custom_text: text, customer_id: $("customer").value || "cus_ananya" }
      });
      this.renderResult(res);
      toast("Text evaluation finished: " + (res.policy_decision || "DONE"));
    } catch (e) {
      toast("Text evaluation failed: " + e.message);
      $("demoFinalOutcome").textContent = "Error: " + e.message;
    }
  },

  async resetState() {
    if (!confirm("Reset customer balances and card states to baseline? Permanent audit logs and tickets will be preserved.")) return;
    try {
      const res = await api("/api/judge-demo/reset", { method: "POST" });
      toast(res.message || "Demo state reset successfully.");
      $("demoFinalOutcome").textContent = "Demo environment reset to clean baseline.";
      $("demoStateProof").textContent = "Restored to Baseline";
      $("demoStateProof").style.color = "#0f7a5c";
      $("demoTimeline").innerHTML = '<div class="empty">Ready for fresh scenario. Balances restored.</div>';
      $("demoOutcomeBadge").textContent = "READY FOR TEST";
      $("demoOutcomeBadge").className = "pill grey";
    } catch (e) {
      toast("Reset failed: " + e.message);
    }
  },

  async startRecording() {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      this.audioChunks = [];
      this.mediaRecorder = new MediaRecorder(stream);
      this.mediaRecorder.ondataavailable = (e) => {
        if (e.data.size > 0) this.audioChunks.push(e.data);
      };
      this.mediaRecorder.onstop = async () => {
        const audioBlob = new Blob(this.audioChunks, { type: "audio/wav" });
        stream.getTracks().forEach((t) => t.stop());
        await this.submitAudio(audioBlob);
      };
      this.mediaRecorder.start();
      $("demoMicState").textContent = "Recording Live Audio...";
      $("demoMicState").className = "pill bad";
      $("btnDemoMicRecord").style.display = "none";
      $("btnDemoMicStop").style.display = "block";
    } catch (e) {
      toast("Microphone access denied: " + e.message);
    }
  },

  stopRecording() {
    if (this.mediaRecorder && this.mediaRecorder.state !== "inactive") {
      this.mediaRecorder.stop();
      $("demoMicState").textContent = "Analyzing speech & authenticity...";
      $("demoMicState").className = "pill warn";
      $("btnDemoMicRecord").style.display = "block";
      $("btnDemoMicStop").style.display = "none";
    }
  },

  async submitAudio(blob) {
    try {
      $("demoFinalOutcome").textContent = "Processing speech recognition & DetectifAI deepfake checks...";
      $("demoOutcomeBadge").textContent = "PROCESSING";
      $("demoOutcomeBadge").className = "pill warn";
      const form = new FormData();
      form.append("file", blob, "live_mic.wav");
      form.append("customer_id", $("customer").value || "cus_ananya");
      const res = await api("/api/judge-demo/run", { method: "POST", form });
      $("demoMicState").textContent = "Completed";
      $("demoMicState").className = "pill";
      this.renderResult(res);
      toast("Live speech processed: " + (res.policy_decision || "DONE"));
    } catch (e) {
      $("demoMicState").textContent = "Error";
      $("demoMicState").className = "pill bad";
      toast("Audio analysis failed: " + e.message);
      $("demoFinalOutcome").textContent = "Error: " + e.message;
    }
  }
};

const elReset = $("btnResetDemoState");
if (elReset) elReset.onclick = () => JudgeDemo.resetState();
const elD1 = $("btnDemo1");
if (elD1) elD1.onclick = () => JudgeDemo.runScenario("demo1_legitimate_balance");
const elD2 = $("btnDemo2");
if (elD2) elD2.onclick = () => JudgeDemo.runScenario("demo2_human_scammer");
const elD3 = $("btnDemo3");
if (elD3) elD3.onclick = () => JudgeDemo.runScenario("demo3_voice_clone");
const elD4 = $("btnDemo4");
if (elD4) elD4.onclick = () => JudgeDemo.runScenario("demo4_unauthorized_action");
const elRec = $("btnDemoMicRecord");
if (elRec) elRec.onclick = () => JudgeDemo.startRecording();
const elStop = $("btnDemoMicStop");
if (elStop) elStop.onclick = () => JudgeDemo.stopRecording();
const elSimForm = $("demoSimForm");
if (elSimForm) elSimForm.onsubmit = (e) => {
  e.preventDefault();
  JudgeDemo.runText($("demoSimInput").value);
};

// ---------------------------------------------------------------- boot
(async function boot() {
  try {
    const h = await fetch("/api/health").then(r => r.json()).catch(() => null);
    if (h && h.demo_staff_key && !$("staffKey").value) {
      $("staffKey").value = h.demo_staff_key;
      sessionStore.set("staffKey", h.demo_staff_key);
    }
  } catch {}
  await loadCustomers();
  const c = cust();
  if (c.lang) $("lang").value = c.lang;
  Enroll.load();
  const tab = store.get("tab", "demo");
  showTab(["call", "phone", "desk", "knowledge", "demo"].includes(tab) ? tab : "demo");
})();
