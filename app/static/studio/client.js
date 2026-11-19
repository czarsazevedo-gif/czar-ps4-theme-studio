const base = "/api/studio";
let token;
export async function request(path, options = {}) {
  const headers = { ...options.headers };
  if (token) headers["x-czar-token"] = token;
  let body;
  if (options.json) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(options.json);
  }
  const response = await fetch(base + path, { method: options.method, headers, body, signal: options.signal });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    const error = new Error(data.message || "Falha de comunicação com o Studio.");
    error.code = data.code;
    throw error;
  }
  if (response.status === 204) return null;
  return options.blob ? response.blob() : response.json();
}
export async function connect() {
  const settings = await request("/session");
  token = settings.token;
  return settings;
}
export const urlFor = (path) => base + path;

/** Chunked upload persists its session ID across refreshes.
 * Re-select the same File on the tablet to resume, with no recompression.
 */
export function upload(path, file, progress) {
  const controller = new AbortController();
  const pendingKey = "czar.upload." + path + ":" + file.name + ":" + file.size + ":" + file.lastModified;
  const chunkSize = 512 * 1024;
  let aborted = false;
  const sleep = (ms) => new Promise(resolve => setTimeout(resolve, ms));
  // A cryptographic digest prevents accidentally resuming a different file
  // with the same name, length and lastModified timestamp.
  const jsonRequest = async (endpoint, method, json) => {
    const response = await fetch(base + endpoint, {
      method, credentials: "same-origin", signal: controller.signal,
      headers: { "x-czar-token": token, ...(json ? { "content-type": "application/json" } : {}) },
      ...(json ? { body: JSON.stringify(json) } : {}),
    });
    if (!response.ok) {
      const details = await response.json().catch(() => ({}));
      const error = new Error(details.message || ("Servidor respondeu HTTP " + response.status));
      error.status = response.status;
      error.code = details.code;
      throw error;
    }
    return response.status === 204 ? null : response.json();
  };
  const retry = async operation => {
    for (let attempt = 0; ; attempt++) {
      if (aborted) throw new DOMException("Importação pausada.", "AbortError");
      try { return await operation(); }
      catch (error) {
        if (aborted || error.name === "AbortError") throw error;
        if ((error.status && ![408, 429, 500, 502, 503, 504].includes(error.status)) || attempt >= 4) throw error;
        await sleep(Math.min(500 * 2 ** attempt, 4000));
      }
    }
  };
  const promise = (async () => {
    let session = null;
    try {
      const checksum = await (async () => {
        const bytes = await file.arrayBuffer();
        const digest = await crypto.subtle.digest("SHA-256", bytes);
        return Array.from(new Uint8Array(digest), b => b.toString(16).padStart(2, "0")).join("");
      })();
      const saved = localStorage.getItem(pendingKey);
      if (saved) {
        try {
          session = await jsonRequest(path + "/uploads/" + saved, "GET");
          if (session.size !== file.size || session.name !== file.name || session.sha256 !== checksum) throw Error("Arquivo divergente");
        } catch (error) {
          if (error.name === "AbortError") throw error;
          if (error.status === 404 || error.message === "Arquivo divergente") localStorage.removeItem(pendingKey);
          else throw error;
          session = null;
        }
      }
      if (!session) {
        session = await retry(() => jsonRequest(path + "/uploads", "POST", { name: file.name, size: file.size, sha256: checksum }));
        localStorage.setItem(pendingKey, session.id);
      }
      let offset = session.offset;
      progress(offset / file.size);
      while (offset < file.size) {
        const intended = offset;
        const slice = file.slice(offset, Math.min(offset + chunkSize, file.size));
        try {
          const response = await retry(async () => {
            const result = await fetch(base + path + "/uploads/" + session.id, {
              method: "PUT", credentials: "same-origin", signal: controller.signal,
              headers: { "x-czar-token": token, "x-upload-offset": String(intended),
                         "content-type": "application/octet-stream" }, body: slice
            });
            if (!result.ok) {
              const details = await result.json().catch(() => ({}));
              const error = new Error(details.message || ("Falha HTTP " + result.status + " no bloco."));
              error.status = result.status;
              error.code = details.code;
              throw error;
            }
            return result.json();
          });
          offset = response.offset;
        } catch (error) {
          if (error.name === "AbortError") throw error;
          const status = await retry(() => jsonRequest(path + "/uploads/" + session.id, "GET"));
          if (status.offset <= intended) throw error;
          offset = status.offset;
        }
        progress(offset / file.size);
      }
      const finished = await retry(() => jsonRequest(path + "/uploads/" + session.id + "/finalize", "POST"));
      localStorage.removeItem(pendingKey);
      progress(1);
      return finished;
    } catch (error) {
      if (error.name === "AbortError") throw new Error("Envio pausado. Selecione o mesmo arquivo para retomar.");
      throw error;
    }
  })();
  return { promise, cancel: () => { aborted = true; controller.abort(); } };
}
