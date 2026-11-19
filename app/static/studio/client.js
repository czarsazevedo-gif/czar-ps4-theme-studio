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
export function upload(path, file, progress) {
  const xhr = new XMLHttpRequest();
  const promise = new Promise((resolve, reject) => {
    xhr.open("POST", base + path);
    xhr.setRequestHeader("x-czar-token", token);
    xhr.setRequestHeader("x-filename", encodeURIComponent(file.name));
    xhr.setRequestHeader("content-type", "application/octet-stream");
    xhr.responseType = "json";
    xhr.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable) progress(event.loaded / event.total);
    });
    xhr.onload = () => xhr.status >= 200 && xhr.status < 300 ? resolve(xhr.response) :
      reject(new Error(xhr.response?.message || "Falha ao importar arquivo."));
    xhr.onerror = () => reject(new Error("Conexão interrompida durante a importação."));
    xhr.onabort = () => reject(new Error("Importação cancelada."));
    xhr.send(file);
  });
  return { promise, cancel: () => xhr.abort() };
}
