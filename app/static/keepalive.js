/* Continuous session keep-alive (>= 5h uninterrupted operation),
 * automatic reconnection when returning from tablet background/gallery,
 * connection status indicator, and Screen Wake Lock management.
 */
window.CzarKeepAlive = (() => {
  const DEFAULT_INTERVAL_MS = 25_000;
  const BACKOFF_MS = [1000, 2000, 4000, 8000, 15000];

  let intervalMs = DEFAULT_INTERVAL_MS;
  let timer = null;
  let reconnectTimer = null;
  let inFlight = false;
  let booted = false;
  let state = "connecting"; // "connected" | "reconnecting" | "offline"
  let consecutiveFailures = 0;
  let lastOkAt = 0;
  let serverTtlHours = 8;
  let wakeLock = null;
  let wakeLockSupported = false;
  let wakeLockActive = false;

  const pill = () => document.getElementById("conn-status");

  function endpoint() {
    const base = "/api/keepalive";
    if (typeof Projects !== "undefined" && Projects?.id && typeof Projects.mediaUrl === "function") {
      return Projects.mediaUrl(base);
    }
    return base;
  }

  function renderPill() {
    const el = pill();
    if (!el) return;
    el.dataset.state = state;
    el.dataset.wakelock = wakeLockActive ? "on" : (wakeLockSupported ? "ready" : "unsupported");

    let label = "Conectado";
    let cls = "pill conn-pill ok";
    if (state === "reconnecting") {
      label = "Reconectando…";
      cls = "pill conn-pill warn";
    } else if (state === "offline") {
      label = "Offline · reconectar";
      cls = "pill conn-pill err";
    } else if (state === "connecting") {
      label = "Conectando…";
      cls = "pill conn-pill off";
    }

    el.className = cls;
    el.textContent = label;
    const parts = [
      `Status da conexão: ${label}`,
      `Keep-alive ativo a cada ${Math.round(intervalMs / 1000)}s`,
      `Sessão protegida por ≥ ${serverTtlHours}h`,
      wakeLockActive
        ? "Screen Wake Lock ativo (tela mantida desperta)"
        : (wakeLockSupported ? "Toque para manter a tela ativa (Wake Lock)" : "Screen Wake Lock indisponível neste navegador"),
    ];
    if (lastOkAt) {
      parts.push(`Último sinal: ${new Date(lastOkAt).toLocaleTimeString("pt-BR")}`);
    }
    el.title = parts.join(" · ");
    el.setAttribute("aria-label", parts[0]);
  }

  async function requestWakeLock() {
    wakeLockSupported = Boolean(navigator?.wakeLock && typeof navigator.wakeLock.request === "function");
    if (!wakeLockSupported || document.visibilityState !== "visible") {
      renderPill();
      return false;
    }
    if (wakeLock && !wakeLock.released) {
      wakeLockActive = true;
      renderPill();
      return true;
    }
    try {
      const sentinel = await navigator.wakeLock.request("screen");
      wakeLock = sentinel;
      wakeLockActive = true;
      sentinel.addEventListener("release", () => {
        if (wakeLock === sentinel) {
          wakeLock = null;
          wakeLockActive = false;
          renderPill();
        }
      });
      renderPill();
      return true;
    } catch {
      wakeLockActive = false;
      renderPill();
      return false;
    }
  }

  function scheduleNext(ms = intervalMs) {
    clearTimeout(timer);
    timer = setTimeout(() => { ping("interval"); }, ms);
  }

  function scheduleReconnect() {
    clearTimeout(reconnectTimer);
    const delay = BACKOFF_MS[Math.min(Math.max(0, consecutiveFailures - 1), BACKOFF_MS.length - 1)];
    reconnectTimer = setTimeout(() => { ping("reconnect"); }, delay);
  }

  async function onRecovered() {
    // Re-sync live system status, pending project draft, and active PKG tasks after a drop.
    try {
      if (typeof refreshSystemHealth === "function") await refreshSystemHealth(true);
    } catch { /* non-fatal */ }
    try {
      if (typeof Projects !== "undefined" && Projects?.id && typeof Projects.flush === "function") {
        // Flush only if there are pending changes; save() inside flush is safe.
        await Projects.save?.();
      }
    } catch { /* non-fatal */ }
    try {
      if (typeof forgeLoadHistory === "function") forgeLoadHistory();
    } catch { /* non-fatal */ }
    requestWakeLock();
  }

  async function ping(reason = "manual") {
    if (inFlight) return state === "connected";
    if (navigator.onLine === false) {
      consecutiveFailures++;
      state = "offline";
      renderPill();
      scheduleReconnect();
      return false;
    }
    inFlight = true;
    const wasDown = state === "offline" || state === "reconnecting" || consecutiveFailures > 0;
    if (wasDown && state !== "reconnecting") {
      state = "reconnecting";
      renderPill();
    }
    const ctrl = new AbortController();
    const timeout = setTimeout(() => ctrl.abort(), 10_000);
    try {
      const res = await fetch(endpoint(), {
        method: "POST",
        cache: "no-store",
        keepalive: true,
        headers: { "X-Czar-Keepalive-Reason": reason },
        signal: ctrl.signal,
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json().catch(() => ({}));
      if (typeof data?.ttl_seconds === "number" && data.ttl_seconds > 0) {
        serverTtlHours = Math.max(1, Math.round(data.ttl_seconds / 3600));
      }
      consecutiveFailures = 0;
      lastOkAt = Date.now();
      state = "connected";
      clearTimeout(reconnectTimer);
      renderPill();
      scheduleNext(intervalMs);
      if (wasDown) {
        if (typeof toast === "function") toast("Conexão restabelecida com o servidor.", "ok");
        onRecovered();
      }
      return true;
    } catch {
      consecutiveFailures++;
      state = consecutiveFailures >= 2 ? "offline" : "reconnecting";
      renderPill();
      scheduleReconnect();
      return false;
    } finally {
      clearTimeout(timeout);
      inFlight = false;
    }
  }

  function setIntervalMs(ms) {
    const clamped = Math.max(250, Number(ms) || DEFAULT_INTERVAL_MS);
    intervalMs = clamped;
    if (booted) scheduleNext(intervalMs);
    renderPill();
  }

  function boot(opts = {}) {
    if (opts.intervalMs) intervalMs = Math.max(250, Number(opts.intervalMs));
    wakeLockSupported = Boolean(navigator?.wakeLock && typeof navigator.wakeLock.request === "function");
    renderPill();
    if (booted) {
      ping("reboot");
      return;
    }
    booted = true;

    const el = pill();
    if (el) {
      el.addEventListener("click", () => {
        requestWakeLock();
        ping("manual");
      });
    }

    // Reconnect immediately and re-acquire Screen Wake Lock when returning from
    // background or gallery app (critical on Android / Galaxy Tab S10 Ultra).
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") {
        requestWakeLock();
        ping("visibility");
      }
    });
    window.addEventListener("online", () => {
      state = "reconnecting";
      renderPill();
      ping("online");
    });
    window.addEventListener("offline", () => {
      consecutiveFailures = Math.max(consecutiveFailures, 2);
      state = "offline";
      renderPill();
    });
    window.addEventListener("focus", () => {
      if (Date.now() - lastOkAt > 5000 || state !== "connected") ping("focus");
    });
    window.addEventListener("pageshow", (ev) => {
      if (ev.persisted || state !== "connected") {
        requestWakeLock();
        ping("pageshow");
      }
    });

    // Acquire Screen Wake Lock on the first user gesture (required by some mobile browsers).
    const gestureAcquire = () => {
      requestWakeLock();
    };
    window.addEventListener("pointerdown", gestureAcquire, { once: true, passive: true });
    window.addEventListener("keydown", gestureAcquire, { once: true, passive: true });

    requestWakeLock();
    ping("boot");
  }

  return {
    boot,
    ping,
    requestWakeLock,
    setIntervalMs,
    getState: () => ({
      state,
      consecutiveFailures,
      lastOkAt,
      intervalMs,
      wakeLockSupported,
      wakeLockActive,
      serverTtlHours,
    }),
  };
})();
