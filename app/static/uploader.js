/* Streaming 2 MiB chunked uploader with transparent HTTP 413 reverse-proxy fallback.
 *
 * Works across all Studio upload paths (Animated Background, Media Library,
 * Compositor, Image/Icon Slots, BGM/SFX Audio, Forge Audio/PKG Inspect).
 * Never buffers an entire large file into memory in the browser or server.
 */
window.CzarUploader = (() => {
  const DEFAULT_CHUNK_BYTES = 2 * 1024 * 1024; // 2 MiB
  const MIN_CHUNK_BYTES = 256 * 1024;          // 256 KiB adaptive proxy floor
  const MAX_CHUNK_RETRIES = 4;
  const POLL_INTERVAL_MS = 500;

  let forceChunked = false;

  const isCodespacesProxy = () =>
    /(?:\.app\.github\.dev|\.githubpreview\.dev)$/i.test(location.hostname || "");

  function scopedUrl(url, purpose) {
    if (purpose === "library") return url;
    if (typeof Projects !== "undefined" && Projects && typeof Projects.mediaUrl === "function") {
      return Projects.mediaUrl(url);
    }
    return url;
  }

  function makeAbortError(msg = "Envio interrompido no navegador.") {
    const err = new Error(msg);
    err.name = "AbortError";
    err.aborted = true;
    return err;
  }

  function parseJsonSafe(text) {
    if (!text) return null;
    try { return JSON.parse(text); } catch { return null; }
  }

  function sleep(ms, signal) {
    return new Promise((resolve, reject) => {
      if (signal?.aborted) { reject(makeAbortError()); return; }
      const timer = setTimeout(() => {
        signal?.removeEventListener("abort", onAbort);
        resolve();
      }, ms);
      function onAbort() {
        clearTimeout(timer);
        reject(makeAbortError());
      }
      signal?.addEventListener("abort", onAbort, { once: true });
    });
  }

  function chunkSizeFor(opts) {
    const cfg = typeof sys !== "undefined" && (sys?.chunk_size || sys?.upload?.chunk_bytes);
    return Math.max(MIN_CHUNK_BYTES, Number(opts?.chunkBytes || cfg || DEFAULT_CHUNK_BYTES));
  }

  function shouldStartChunked(file, opts) {
    if (opts?.forceChunked || forceChunked) return true;
    if (!opts?.directUrl) return true;
    const threshold = chunkSizeFor(opts);
    if (file.size > threshold) return true;
    if (isCodespacesProxy() && file.size > 1024 * 1024) return true;
    return false;
  }

  function _directUpload(file, opts) {
    const {
      purpose,
      directUrl,
      directFields = {},
      directFieldName = "file",
      onProgress,
      onStage,
      signal,
      abortMessage,
    } = opts;

    return new Promise((resolve, reject) => {
      if (signal?.aborted) {
        reject(makeAbortError(abortMessage));
        return;
      }
      const xhr = new XMLHttpRequest();
      const url = scopedUrl(directUrl, purpose);
      xhr.open("POST", url);
      xhr.responseType = "text";

      const onAbort = () => xhr.abort();
      signal?.addEventListener("abort", onAbort, { once: true });
      const cleanup = () => signal?.removeEventListener("abort", onAbort);

      xhr.upload.onprogress = (ev) => {
        if (!ev.lengthComputable) return;
        const frac = ev.total > 0 ? ev.loaded / ev.total : 0;
        onProgress?.({ phase: "upload", loaded: ev.loaded, total: ev.total, ratio: frac, mode: "direct" });
      };
      xhr.upload.onload = () => {
        onStage?.("Processando no servidor…", null);
        onProgress?.({ phase: "processing", loaded: file.size, total: file.size, ratio: null, mode: "direct" });
      };
      xhr.onload = () => {
        cleanup();
        const data = parseJsonSafe(xhr.responseText);
        if (xhr.status >= 200 && xhr.status < 300) {
          resolve({ status: xhr.status, data, mode: "direct" });
          return;
        }
        const isAppLimit = xhr.getResponseHeader("X-Czar-Limit-Error") === "1";
        if (xhr.status === 413 && !isAppLimit) {
          resolve({ fallbackChunked: true, proxyStatus: 413 });
          return;
        }
        const detail = typeof data?.detail === "string"
          ? data.detail
          : `O servidor recusou o arquivo (HTTP ${xhr.status}).`;
        const err = new Error(detail);
        err.status = xhr.status;
        reject(err);
      };
      xhr.onabort = () => {
        cleanup();
        reject(makeAbortError(abortMessage));
      };
      xhr.onerror = () => {
        cleanup();
        reject(new Error("A conexão falhou durante o envio."));
      };

      const fd = new FormData();
      fd.append(directFieldName, file, file.name || "arquivo");
      for (const [k, v] of Object.entries(directFields)) {
        if (v !== undefined && v !== null) fd.append(k, String(v));
      }
      xhr.send(fd);
    });
  }

  function _sendChunkXhr(url, blob, offset, totalBytes, onChunkProgress, signal, abortMessage) {
    return new Promise((resolve, reject) => {
      if (signal?.aborted) { reject(makeAbortError(abortMessage)); return; }
      const xhr = new XMLHttpRequest();
      xhr.open("PUT", url);
      xhr.responseType = "text";
      xhr.setRequestHeader("Content-Type", "application/octet-stream");
      xhr.setRequestHeader("X-Upload-Offset", String(offset));
      if (blob.size > 0) {
        xhr.setRequestHeader("Content-Range", `bytes ${offset}-${offset + blob.size - 1}/${totalBytes}`);
      }

      const onAbort = () => xhr.abort();
      signal?.addEventListener("abort", onAbort, { once: true });
      const cleanup = () => signal?.removeEventListener("abort", onAbort);

      xhr.upload.onprogress = (ev) => {
        if (!ev.lengthComputable) return;
        onChunkProgress?.(ev.loaded);
      };
      xhr.onload = () => {
        cleanup();
        resolve({
          status: xhr.status,
          data: parseJsonSafe(xhr.responseText),
          isAppLimit: xhr.getResponseHeader("X-Czar-Limit-Error") === "1",
        });
      };
      xhr.onabort = () => {
        cleanup();
        reject(makeAbortError(abortMessage));
      };
      xhr.onerror = () => {
        cleanup();
        const err = new Error("Falha de rede ao enviar bloco.");
        err.transient = true;
        reject(err);
      };
      xhr.send(blob);
    });
  }

  async function _cancelRemote(uploadId, purpose) {
    if (!uploadId) return;
    try {
      await fetch(scopedUrl(`/api/uploads/${encodeURIComponent(uploadId)}/cancel`, purpose), {
        method: "POST",
        keepalive: true,
      });
    } catch { /* best-effort cleanup */ }
  }

  async function _fetchStatus(uploadId, purpose, signal) {
    const res = await fetch(scopedUrl(`/api/uploads/${encodeURIComponent(uploadId)}/status`, purpose), {
      cache: "no-store",
      signal,
    });
    const data = await res.json().catch(() => null);
    if (!res.ok) {
      const err = new Error(typeof data?.detail === "string" ? data.detail : `Erro ao consultar upload (HTTP ${res.status}).`);
      err.status = res.status;
      throw err;
    }
    return data;
  }

  async function _chunkedUpload(file, opts) {
    const {
      purpose,
      params = {},
      onProgress,
      onStage,
      signal,
      abortMessage,
    } = opts;

    if (signal?.aborted) throw makeAbortError(abortMessage);

    onStage?.("Iniciando envio em blocos (2 MiB)…", 0);
    const initRes = await fetch(scopedUrl("/api/uploads/init", purpose), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        purpose,
        filename: file.name || "arquivo",
        size: file.size,
        mime: file.type || "",
        params,
      }),
      signal,
    });
    const initData = await initRes.json().catch(() => null);
    if (!initRes.ok) {
      const err = new Error(
        typeof initData?.detail === "string"
          ? initData.detail
          : `O servidor recusou iniciar o envio (HTTP ${initRes.status}).`
      );
      err.status = initRes.status;
      throw err;
    }

    const uploadId = initData.upload_id;
    let chunkSize = Math.min(chunkSizeFor(opts), Number(initData.chunk_bytes || DEFAULT_CHUNK_BYTES));
    let offset = Number(initData.received_bytes || 0);
    let cancelled = false;

    const onSignalAbort = () => {
      if (cancelled) return;
      cancelled = true;
      _cancelRemote(uploadId, purpose);
    };
    signal?.addEventListener("abort", onSignalAbort, { once: true });

    try {
      const total = file.size;
      const chunkUrlBase = scopedUrl(`/api/uploads/${encodeURIComponent(uploadId)}/chunk`, purpose);

      while (offset < total) {
        if (signal?.aborted) throw makeAbortError(abortMessage);
        const end = Math.min(total, offset + chunkSize);
        const slice = file.slice(offset, end);
        const sep = chunkUrlBase.includes("?") ? "&" : "?";
        const chunkUrl = `${chunkUrlBase}${sep}offset=${offset}`;

        let attempt = 0;
        while (true) {
          if (signal?.aborted) throw makeAbortError(abortMessage);
          try {
            const resp = await _sendChunkXhr(
              chunkUrl,
              slice,
              offset,
              total,
              (loaded) => {
                const sent = Math.min(total, offset + loaded);
                const ratio = total > 0 ? sent / total : 1;
                onProgress?.({ phase: "upload", loaded: sent, total, ratio, mode: "chunked" });
              },
              signal,
              abortMessage,
            );

            if (resp.status >= 200 && resp.status < 300) {
              offset = Number(resp.data?.received_bytes ?? end);
              const ratio = total > 0 ? offset / total : 1;
              onProgress?.({ phase: "upload", loaded: offset, total, ratio, mode: "chunked" });
              break;
            }
            if (resp.status === 413 && !resp.isAppLimit && chunkSize > MIN_CHUNK_BYTES) {
              chunkSize = Math.max(MIN_CHUNK_BYTES, Math.floor(chunkSize / 2));
              onStage?.(`Proxy limitou o bloco — reduzindo para ${Math.round(chunkSize / 1024)} KiB…`, null);
              break; // re-slice with smaller chunkSize at same offset
            }
            if (resp.status === 409 && typeof resp.data?.expected_offset === "number") {
              offset = resp.data.expected_offset;
              break;
            }
            if ([408, 429, 502, 503, 504].includes(resp.status) && attempt < MAX_CHUNK_RETRIES) {
              attempt++;
              onStage?.(`Reconectando envio de bloco (${attempt}/${MAX_CHUNK_RETRIES})…`, null);
              await sleep(600 * Math.pow(2, attempt - 1), signal);
              try {
                const st = await _fetchStatus(uploadId, purpose, signal);
                if (typeof st?.received_bytes === "number") offset = st.received_bytes;
              } catch { /* retry with current offset */ }
              break;
            }
            const detail = typeof resp.data?.detail === "string"
              ? resp.data.detail
              : `Falha ao enviar bloco (HTTP ${resp.status}).`;
            const err = new Error(detail);
            err.status = resp.status;
            throw err;
          } catch (err) {
            if (err?.aborted || err?.name === "AbortError") throw err;
            if (err?.transient && attempt < MAX_CHUNK_RETRIES) {
              attempt++;
              onStage?.(`Conexão instável — retomando bloco (${attempt}/${MAX_CHUNK_RETRIES})…`, null);
              await sleep(600 * Math.pow(2, attempt - 1), signal);
              try {
                const st = await _fetchStatus(uploadId, purpose, signal);
                if (typeof st?.received_bytes === "number") {
                  offset = st.received_bytes;
                  break;
                }
              } catch { /* continue retry */ }
              continue;
            }
            throw err;
          }
        }
      }

      if (signal?.aborted) throw makeAbortError(abortMessage);

      // Finalize in background so long 4K video normalizations never hit proxy HTTP timeouts!
      onStage?.("Processando e validando no servidor…", null);
      onProgress?.({ phase: "processing", loaded: total, total, ratio: null, mode: "chunked" });

      const compBase = scopedUrl(`/api/uploads/${encodeURIComponent(uploadId)}/complete`, purpose);
      const compSep = compBase.includes("?") ? "&" : "?";
      const compRes = await fetch(`${compBase}${compSep}background=true`, {
        method: "POST",
        signal,
      });
      let state = await compRes.json().catch(() => null);
      if (!compRes.ok && compRes.status !== 202) {
        const err = new Error(
          typeof state?.detail === "string"
            ? state.detail
            : `Falha ao finalizar upload (HTTP ${compRes.status}).`
        );
        err.status = compRes.status;
        throw err;
      }

      let pollFailures = 0;
      while (state && (state.status === "processing" || state.status === "uploading")) {
        if (signal?.aborted) throw makeAbortError(abortMessage);
        if (state.stage) onStage?.(state.stage, state.percent ?? null);
        if (typeof state.percent === "number") {
          onProgress?.({
            phase: "processing",
            loaded: total,
            total,
            ratio: Math.min(0.99, Math.max(0, state.percent / 100)),
            percent: state.percent,
            stage: state.stage,
            mode: "chunked",
          });
        }
        await sleep(POLL_INTERVAL_MS, signal);
        try {
          state = await _fetchStatus(uploadId, purpose, signal);
          pollFailures = 0;
        } catch (err) {
          if (err?.name === "AbortError" || err?.aborted) throw err;
          pollFailures++;
          if (pollFailures > 12 || (err?.status && err.status >= 400 && err.status < 500)) {
            throw err;
          }
          onStage?.("Aguardando reconexão com o servidor…", null);
          await sleep(1200, signal);
        }
      }

      if (!state || state.status === "cancelled") {
        throw makeAbortError(abortMessage);
      }
      if (state.status === "failed") {
        const err = new Error(state.error || "Falha ao processar o arquivo enviado.");
        err.status = state.error_status || 422;
        throw err;
      }

      onProgress?.({ phase: "done", loaded: total, total, ratio: 1, mode: "chunked" });
      return {
        status: Number(state.http_status || 200),
        data: state.result,
        mode: "chunked",
        uploadId,
      };
    } catch (err) {
      if (err?.aborted || err?.name === "AbortError") {
        await _cancelRemote(uploadId, purpose);
      }
      throw err;
    } finally {
      signal?.removeEventListener("abort", onSignalAbort);
    }
  }

  async function upload(file, opts = {}) {
    if (!file) throw new Error("Nenhum arquivo selecionado.");
    if (!shouldStartChunked(file, opts)) {
      const direct = await _directUpload(file, opts);
      if (!direct.fallbackChunked) return direct;
      opts.onStage?.("Proxy HTTP 413 detectado — alternando automaticamente para envio em blocos de 2 MiB…", 0);
    }
    return await _chunkedUpload(file, opts);
  }

  async function uploadData(file, opts = {}) {
    const res = await upload(file, opts);
    return res.data;
  }

  return {
    DEFAULT_CHUNK_BYTES,
    get forceChunked() { return forceChunked; },
    set forceChunked(val) { forceChunked = Boolean(val); },
    isCodespacesProxy,
    upload,
    uploadData,
  };
})();
