/*
 * Alfred — limpeza da caixa: busca lotes aprovados no Alfred a cada N minutos e executa.
 * A cada minuto avisa o Alfred que está viva (POST /limpeza/extensao/ping, com o último ciclo e o erro dele);
 * se Pedro clicou "Verificar agora" no Painel, a resposta traz verificar=true e o ciclo roda na hora.
 */
"use strict";

const PADRAO = {
  urlAlfred: "http://127.0.0.1:7073",
  token: "",
  contasPermitidas: [],
  modoTeste: true,
  pastaTeste: "Alfred-Teste",
  executarDeVerdade: false,
  intervaloMin: 10,
};

async function carregarCfg() {
  const cfg = { ...PADRAO, ...(await messenger.storage.local.get(Object.keys(PADRAO))) };
  // portas +1 (31/10/2026): o inbox saiu da 7072 (hoje é o researcher) — URL salva antes disso vira a nova
  if (/^http:\/\/(127\.0\.0\.1|localhost):7072\/?$/.test(cfg.urlAlfred)) {
    cfg.urlAlfred = PADRAO.urlAlfred;
    await messenger.storage.local.set({ urlAlfred: cfg.urlAlfred });
  }
  return cfg;
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
  if (!cfg.token || !cfg.contasPermitidas.length) {
    // gravado para o ping levar ao Painel (com token e sem contas, Pedro vê o erro lá)
    const erro = "configure token e contas nas opções";
    await messenger.storage.local.set({ ultimoCiclo: { em: new Date().toISOString(), erro } });
    return { ok: false, erro };
  }
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

async function ping() {
  const cfg = await carregarCfg();
  if (!cfg.token) return;
  const { ultimoCiclo } = await messenger.storage.local.get("ultimoCiclo");
  try {
    const r = await clienteHttp(cfg)("POST", "/limpeza/extensao/ping", {
      versao: messenger.runtime.getManifest().version,
      ultimo_ciclo: ultimoCiclo ? { em: ultimoCiclo.em, erro: ultimoCiclo.erro || null } : null,
    });
    if (r && r.verificar) await ciclo();
  } catch (e) {
    // Alfred fora do ar ou token errado: o Painel mostra "sem contato" pelo tempo desde o último ping
  }
}

async function agendar() {
  const { intervaloMin } = await carregarCfg();
  await messenger.alarms.clear("alfred-limpeza");
  messenger.alarms.create("alfred-limpeza", { periodInMinutes: Math.max(1, Number(intervaloMin) || 10) });
  await messenger.alarms.clear("alfred-ping");
  messenger.alarms.create("alfred-ping", { periodInMinutes: 1 });
  ping();
}

messenger.alarms.onAlarm.addListener((a) => {
  if (a.name === "alfred-limpeza") ciclo();
  if (a.name === "alfred-ping") ping();
});
messenger.runtime.onInstalled.addListener(agendar);
messenger.runtime.onStartup.addListener(agendar);
messenger.runtime.onMessage.addListener((msg) => {
  if (msg && msg.tipo === "verificar-agora") return ciclo();
  if (msg && msg.tipo === "reagendar") return agendar();
  return undefined;
});
