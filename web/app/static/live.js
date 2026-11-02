// Live updates over a WebSocket. The server only says "something changed"; we then re-fetch the
// (permission-checked) HTML for this page. If the socket is down we poll slowly until it is back.
(() => {
  const box = document.getElementById("live-box");
  if (!box || !box.dataset.liveWs) return;
  const { liveUrl, liveWs, liveOrder, liveSelect, liveSound } = box.dataset;
  const connEl = document.getElementById("conn");
  const baseTitle = document.title;
  const target = () => (liveSelect ? box.querySelector(liveSelect) : box.firstElementChild);
  let known = null, connected = false, pending = false, audio = null;
  let soundOn = false;
  try { soundOn = localStorage.getItem("ahead-sound") === "1"; } catch (e) {}

  const toggle = document.getElementById("sound-toggle");
  function showSound() { if (toggle) toggle.textContent = "Sound: " + (soundOn ? "on" : "off"); }
  if (toggle) {
    showSound();
    toggle.addEventListener("click", () => {
      soundOn = !soundOn;
      try { localStorage.setItem("ahead-sound", soundOn ? "1" : "0"); } catch (e) {}
      if (soundOn) beep(); // the click also unlocks audio in the browser
      showSound();
    });
  }

  function beep() {
    try {
      audio = audio || new (window.AudioContext || window.webkitAudioContext)();
      const osc = audio.createOscillator(), gain = audio.createGain();
      osc.connect(gain); gain.connect(audio.destination);
      osc.frequency.value = 880; gain.gain.value = 0.08;
      osc.start(); osc.stop(audio.currentTime + 0.25);
    } catch (e) {}
  }

  function setState(ok) {
    connected = ok;
    if (!connEl) return;
    connEl.dataset.state = ok ? "live" : "down";
    connEl.textContent = ok ? "Live" : "Reconnecting…";
  }

  async function refresh() {
    const el = target();
    if (!el) return;
    const a = document.activeElement;
    if (el.contains(a) && a.matches("input, textarea, select")) { pending = true; return; } // don't wipe what is being typed
    pending = false;
    try {
      const res = await fetch(liveUrl, { cache: "no-store" });
      if (res.redirected) { location.reload(); return; } // session expired
      if (!res.ok) return;
      const text = await res.text();
      let html = text;
      if (liveSelect) {
        const src = new DOMParser().parseFromString(text, "text/html").querySelector(liveSelect);
        if (src) html = src.innerHTML;
      }
      el.innerHTML = html;
      const marker = el.querySelector("[data-new]");
      if (marker) {
        const n = Number(marker.dataset.new);
        if (liveSound && known !== null && n > known && soundOn) beep();
        known = n;
        document.title = (n ? "(" + n + ") " : "") + baseTitle;
      }
    } catch (e) {}
  }
  box.addEventListener("focusout", () => { if (pending) setTimeout(refresh, 300); });

  let delay = 1000, hadConnection = false;
  function open() {
    const ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + liveWs);
    let beat;
    ws.onopen = () => {
      setState(true); delay = 1000;
      if (hadConnection) refresh(); // catch up on anything we missed while offline
      hadConnection = true;
      beat = setInterval(() => ws.readyState === 1 && ws.send("ping"), 25000);
    };
    ws.onmessage = (e) => {
      if (e.data === "pong") return;
      try {
        const msg = JSON.parse(e.data);
        if (liveOrder && String(msg.order_id) !== liveOrder) return;
        refresh();
      } catch (err) {}
    };
    ws.onclose = () => {
      clearInterval(beat); setState(false);
      setTimeout(open, delay); delay = Math.min(delay * 2, 15000);
    };
  }
  const marker = box.querySelector("[data-new]");
  if (marker) known = Number(marker.dataset.new);
  open();
  setInterval(() => { if (!connected) refresh(); }, 20000); // fallback only while the socket is down
})();
