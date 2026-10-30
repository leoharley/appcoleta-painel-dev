// Atualiza partes da página quando há novidade, sem recarregar (não perde o texto sendo digitado).
// liveRefresh(urlDoEstado, ["id1", "id2"], {onUpdate, canReplace(id, elementoAtual)})
function liveRefresh(stateUrl, ids, opts) {
  opts = opts || {};
  let version = null, busy = false;
  async function tick() {
    if (busy || document.hidden) return;
    busy = true;
    try {
      const r = await fetch(stateUrl, {cache: "no-store", headers: {"Accept": "application/json"}});
      if (!r.ok) return;
      const v = (await r.json()).v;
      if (version !== null && v !== version) {
        const page = await fetch(location.href, {cache: "no-store"});
        const doc = new DOMParser().parseFromString(await page.text(), "text/html");
        for (const id of ids) {
          const cur = document.getElementById(id), fresh = doc.getElementById(id);
          if (cur && fresh && (!opts.canReplace || opts.canReplace(id, cur))) cur.innerHTML = fresh.innerHTML;
        }
        if (opts.onUpdate) opts.onUpdate();
      }
      version = v;
    } catch (e) {
      // sessão expirada ou rede fora: tenta de novo no próximo ciclo
    } finally {
      busy = false;
    }
  }
  tick();
  setInterval(tick, 5000);
  document.addEventListener("visibilitychange", tick);
  // Página restaurada pelo botão Voltar (cache do navegador): pode estar desatualizada.
  window.addEventListener("pageshow", (e) => { if (e.persisted) { version = version === null ? "" : version + "*"; tick(); } });
}
