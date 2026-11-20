/* Project navigation uses the URL so two tabs can open different projects. */
const Projects = (() => {
  const id = new URLSearchParams(location.search).get("project");
  const get = (name) => document.getElementById(`project-${name}`);
  const diagnostic = document.getElementById("ouroboros-link");
  if (id) diagnostic.href = `/static/ouroboros.html?project=${encodeURIComponent(id)}`;
  diagnostic.addEventListener("click", (event) => {
    if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    navigate(() => { location.assign(diagnostic.href); });
  });
  const path = id ? `/api/projects/${encodeURIComponent(id)}` : null;
  const draftKey = `czar.project.draft.v1.${id}`;
  let manifest = null;
  let pending = null;
  let saving = null;
  let timer = null;
  let mediaRequests = 0;
  let switching = false;
  let conflict = false;

  function mediaUrl(url) {
    if (!id || !url.startsWith("/api/") || url.startsWith("/api/projects")) return url;
    const scoped = new URL(url, location.origin);
    scoped.searchParams.set("project", id);
    return scoped.pathname + scoped.search;
  }

  async function fetchMedia(url, options) {
    const mutates = id && options?.method === "POST";
    if (mutates) mediaRequests++;
    try { return await fetch(mediaUrl(url), options); }
    finally { if (mutates) mediaRequests--; }
  }

  async function request(url, method = "GET", payload) {
    const response = await fetch(url, {
      method,
      ...(payload && { headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }),
    });
    const data = await response.json();
    if (!response.ok) {
      const message = typeof data.detail === "string" ? data.detail : "Não foi possível validar os dados do projeto.";
      throw Object.assign(new Error(message), { status: response.status });
    }
    return data;
  }

  function show(message) { get("status").textContent = message; }
  function showUpdated() {
    get("updated").textContent = `Última alteração · ${new Date(manifest.updatedAt).toLocaleString("pt-BR")}`;
    const option = [...get("list").options].find((item) => item.value === id);
    if (option) option.textContent = manifest.name;
  }

  function cacheDraft() {
    try {
      if (pending) sessionStorage.setItem(draftKey, JSON.stringify(pending));
      else sessionStorage.removeItem(draftKey);
    } catch { /* Saving to the server can continue without browser storage. */ }
  }

  function changed() {
    if (!manifest) return;
    pending = { name: get("name").value, revision: pending?.revision ?? manifest.revision,
                state: captureEditorState() };
    cacheDraft();
    if (conflict) return;
    show("Alterações a salvar…");
    clearTimeout(timer);
    timer = setTimeout(() => save(), 400);
  }

  async function save() {
    clearTimeout(timer);
    if (saving) await saving;
    if (!pending) return true;
    if (conflict) return false;
    const payload = pending;
    show("Salvando…");
    saving = request(path, "PUT", payload).then((result) => {
      manifest = result;
      if (pending === payload) pending = null;
      else if (pending) pending.revision = result.revision;
      cacheDraft();
      showUpdated();
      show(pending ? "Alterações a salvar…" : "Salvo");
      return true;
    }).catch((err) => {
      conflict = err.status === 409;
      show(`Alterações mantidas nesta aba. ${err.message}`);
      return false;
    }).finally(() => { saving = null; });
    return saving;
  }

  function nameDialog(value = "") {
    const dialog = get("dialog");
    const input = get("dialog-name");
    input.value = value;
    dialog.returnValue = "";
    dialog.showModal();
    return new Promise((resolve) => {
      dialog.addEventListener("close", () => {
        resolve(dialog.returnValue === "confirm" ? input.value.trim() : "");
      }, { once: true });
    });
  }

  async function navigate(action) {
    if (switching) return;
    if (mediaRequests || anim.busy || bgm.busy) {
      show("Aguarde o processamento da mídia antes de trocar de projeto.");
      return;
    }
    switching = true;
    const editor = document.querySelector("main");
    const wasInert = editor.inert;
    editor.inert = true;
    get("name").disabled = true;
    try {
      while (pending || saving) {
        if (!await save()) return;
      }
      const target = await action();
      if (target !== undefined) {
        location.href = target ? `?project=${encodeURIComponent(target)}` : "/";
      }
    } catch (err) { show(err.message); }
    finally {
      switching = false;
      editor.inert = wasInert;
      get("name").disabled = !manifest;
    }
  }

  async function boot() {
    const editor = document.querySelector("main");
    if (id) editor.inert = true;
    try {
      const list = await request("/api/projects");
      const select = get("list");
      select.replaceChildren(new Option("Selecione um projeto", ""));
      for (const item of list) {
        select.add(new Option(`${item.name} · ${new Date(item.updatedAt).toLocaleString("pt-BR")}`, item.id));
      }
      select.value = id || "";
      get("open").disabled = !list.length;
      get("open").onclick = () => navigate(() => select.value || undefined);
      get("new").disabled = false;
      get("new").onclick = () => navigate(async () => {
        const name = await nameDialog();
        if (name) return (await request("/api/projects", "POST", { name })).id;
      });
      if (!id) {
        show("Sessão avulsa. Crie um projeto para salvar novos trabalhos.");
        return;
      }
      get("close").disabled = false;
      get("close").onclick = () => navigate(() => null);
      const loaded = await request(path);
      let cached;
      try { cached = JSON.parse(sessionStorage.getItem(draftKey)); }
      catch { /* The last confirmed manifest remains available. */ }
      const missing = await restoreEditorState(cached?.state || loaded.state, true);
      if (missing) throw new Error("Não foi possível restaurar todas as mídias. Recarregue para tentar novamente.");
      manifest = loaded;
      get("name").value = cached?.name ?? manifest.name;
      get("name").disabled = false;
      get("name").oninput = changed;
      get("save").disabled = false;
      get("save").onclick = () => { changed(); save(); };
      get("copy").disabled = false;
      get("copy").onclick = () => navigate(async () => {
        const name = await nameDialog(`${manifest.name.slice(0, 90)} (cópia)`);
        if (name) return (await request(`${path}/duplicate`, "POST", { name })).id;
      });
      editor.inert = false;
      showUpdated();
      if (cached) {
        pending = cached;
        if (cached.revision < manifest.revision - 1) {
          conflict = true;
          show("Rascunho recuperado. Outra aba tem uma revisão mais recente. Abra uma nova aba para consultar a versão salva.");
        } else changed();
      } else {
        show(manifest.state ? "Salvo" : "Projeto criado. As alterações serão salvas automaticamente.");
      }
    } catch (err) {
      show(err.message);
    }
  }

  window.addEventListener("beforeunload", (event) => {
    if (!pending && !mediaRequests) return;
    event.preventDefault();
    event.returnValue = "";
  });

  async function flush() {
    if (!id || !manifest) return true;
    changed();
    while (pending || saving) {
      if (!await save()) return false;
    }
    return true;
  }

  return { id, boot, changed, flush, mediaUrl, fetchMedia };
})();
