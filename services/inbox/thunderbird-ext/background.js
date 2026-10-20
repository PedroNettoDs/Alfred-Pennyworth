/* Alfred — limpeza da caixa: busca lotes aprovados no Alfred a cada N minutos e executa. */
"use strict";

const PADRAO = {
  urlAlfred: "http://127.0.0.1:7072",
  token: "",
  contasPermitidas: [],
  modoTeste: true,
  pastaTeste: "Alfred-Teste",
  executarDeVerdade: false,
  intervaloMin: 10,
};

async function carregarCfg() {
  return { ...PADRAO, ...(await messenger.storage.local.get(Object.keys(PADRAO))) };
}

function clienteHttp(cfg) {
  return async (metodo, rota, corpo) => {
    const r = await fetch(cfg.urlAlfred.replace(/\/$/, "") + rota, {
      method: metodo,
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${cfg.token}` },
      body: corpo ? JSON.stringify(corpo) : undefined,
    });
    if (!r.ok) throw new Error(`Alfred respondeu ${r.status} em ${rota}`);
    return r.json();
  };
}

async function ciclo() {
  const cfg = await carregarCfg();
  if (!cfg.token || !cfg.contasPermitidas.length) return { ok: false, erro: "configure token e contas nas opções" };
  try {
    const relatorio = await AlfredLimpeza.processarFila(messenger, clienteHttp(cfg), cfg);
    await messenger.storage.local.set({ ultimoCiclo: { em: new Date().toISOString(), relatorio } });
    return { ok: true, relatorio };
  } catch (e) {
    const erro = String(e && e.message || e);
    await messenger.storage.local.set({ ultimoCiclo: { em: new Date().toISOString(), erro } });
    return { ok: false, erro };
  }
}

async function agendar() {
  const { intervaloMin } = await carregarCfg();
  await messenger.alarms.clear("alfred-limpeza");
  messenger.alarms.create("alfred-limpeza", { periodInMinutes: Math.max(1, Number(intervaloMin) || 10) });
}

messenger.alarms.onAlarm.addListener((a) => { if (a.name === "alfred-limpeza") ciclo(); });
messenger.runtime.onInstalled.addListener(agendar);
messenger.runtime.onStartup.addListener(agendar);
messenger.runtime.onMessage.addListener((msg) => {
  if (msg && msg.tipo === "verificar-agora") return ciclo();
  if (msg && msg.tipo === "reagendar") return agendar();
  return undefined;
});
