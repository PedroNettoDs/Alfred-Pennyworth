/* Alfred — blog: a cada N minutos busca no Alfred os posts com Blogspot: none e envia ao Blogger. */
"use strict";

const PADRAO = {
  urlAlfred: "http://127.0.0.1:7073",
  token: "",
  identidade: "",            // vazio = identidade padrão do Thunderbird
  anexarAudio: true,
  enviarDeVerdade: false,
  intervaloMin: 10,
};

async function carregarCfg() {
  return { ...PADRAO, ...(await messenger.storage.local.get(Object.keys(PADRAO))) };
}

function clientes(cfg) {
  const base = cfg.urlAlfred.replace(/\/$/, "");
  const headers = { Authorization: `Bearer ${cfg.token}` };
  const pedir = async (metodo, rota, corpo) => {
    const r = await fetch(base + rota, {
      method: metodo,
      headers: corpo ? { ...headers, "Content-Type": "application/json" } : headers,
      body: corpo ? JSON.stringify(corpo) : undefined,
    });
    if (!r.ok) throw new Error(`Alfred respondeu ${r.status} em ${rota}`);
    return r;
  };
  return {
    http: async (metodo, rota, corpo) => (await pedir(metodo, rota, corpo)).json(),
    baixar: async (rota) => (await pedir("GET", rota)).blob(),
  };
}

let emAndamento = null;   // alarme e "Verificar agora" juntos não podem enviar o mesmo post duas vezes

async function ciclo() {
  const cfg = await carregarCfg();
  if (!cfg.token) return { ok: false, erro: "configure o token nas opções" };
  const { enviados = {} } = await messenger.storage.local.get("enviados");
  const { http, baixar } = clientes(cfg);
  try {
    const relatorio = await AlfredBlog.processarFila(messenger, http, baixar, cfg, enviados,
      (e) => messenger.storage.local.set({ enviados: e }));
    await messenger.storage.local.set({ ultimoCiclo: { em: new Date().toISOString(), relatorio } });
    return { ok: true, relatorio };
  } catch (e) {
    const erro = String((e && e.message) || e);
    await messenger.storage.local.set({ ultimoCiclo: { em: new Date().toISOString(), erro } });
    return { ok: false, erro };
  }
}

function cicloUnico() {
  if (!emAndamento) emAndamento = ciclo().finally(() => { emAndamento = null; });
  return emAndamento;
}

async function agendar() {
  const { intervaloMin } = await carregarCfg();
  await messenger.alarms.clear("alfred-blog");
  messenger.alarms.create("alfred-blog", { periodInMinutes: Math.max(1, Number(intervaloMin) || 10) });
}

messenger.alarms.onAlarm.addListener((a) => { if (a.name === "alfred-blog") cicloUnico(); });
messenger.runtime.onInstalled.addListener(agendar);
messenger.runtime.onStartup.addListener(agendar);
messenger.runtime.onMessage.addListener((msg) => {
  if (msg && msg.tipo === "verificar-agora") return cicloUnico();
  if (msg && msg.tipo === "reagendar") return agendar();
  return undefined;
});
