/* Alfred — agenda: a cada N minutos busca no Alfred os compromissos detectados e grava no calendário. */
"use strict";

const PADRAO = {
  urlAlfred: "http://127.0.0.1:7073",
  token: "",
  calendario: "Agenda",      // nome do calendário no Thunderbird (o Google via CalDAV se chama "Agenda")
  intervaloMin: 2,
};

async function carregarCfg() {
  return { ...PADRAO, ...(await messenger.storage.local.get(Object.keys(PADRAO))) };
}

async function http(cfg, metodo, rota, corpo) {
  const r = await fetch(cfg.urlAlfred.replace(/\/$/, "") + rota, {
    method: metodo,
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${cfg.token}` },
    body: corpo ? JSON.stringify(corpo) : undefined,
  });
  if (!r.ok) throw new Error(`Alfred respondeu ${r.status} em ${rota}`);
  return r.json();
}

async function acharCalendario(nome) {
  const cals = await messenger.alfredCalendar.listCalendars();
  const graváveis = cals.filter((c) => !c.readOnly && !c.disabled);
  const alvo = graváveis.find((c) => c.name === nome) || graváveis.find((c) => c.name.toLowerCase() === String(nome).toLowerCase());
  if (!alvo) throw new Error(`calendário "${nome}" não encontrado (graváveis: ${graváveis.map((c) => c.name).join(", ")})`);
  return alvo;
}

async function ciclo() {
  const cfg = await carregarCfg();
  if (!cfg.token) return { ok: false, erro: "configure o token nas opções" };
  const relatorio = [];
  try {
    const { itens } = await http(cfg, "GET", "/agenda/fila");
    for (const it of itens) {
      let calendario;
      try {
        calendario = await acharCalendario(it.calendario || cfg.calendario);
        if (it.acao === "remover") {
          await messenger.alfredCalendar.deleteEvent(calendario.id, it.uid);
        } else {
          await messenger.alfredCalendar.createEvent(calendario.id, it.ics);
        }
        await http(cfg, "POST", `/agenda/${encodeURIComponent(it.id)}/resultado`,
                   { acao: it.acao, ok: true, calendario: calendario.name });
        relatorio.push({ id: it.id, acao: it.acao, ok: true });
      } catch (e) {
        const erro = String((e && e.message) || e);
        await http(cfg, "POST", `/agenda/${encodeURIComponent(it.id)}/resultado`,
                   { acao: it.acao, ok: false, calendario: calendario ? calendario.name : "", erro }).catch(() => {});
        relatorio.push({ id: it.id, acao: it.acao, ok: false, erro });
      }
    }
    await messenger.storage.local.set({ ultimoCiclo: { em: new Date().toISOString(), relatorio } });
    return { ok: true, relatorio };
  } catch (e) {
    const erro = String((e && e.message) || e);
    await messenger.storage.local.set({ ultimoCiclo: { em: new Date().toISOString(), erro } });
    return { ok: false, erro };
  }
}

async function agendar() {
  const { intervaloMin } = await carregarCfg();
  await messenger.alarms.clear("alfred-agenda");
  messenger.alarms.create("alfred-agenda", { periodInMinutes: Math.max(1, Number(intervaloMin) || 2) });
}

messenger.alarms.onAlarm.addListener((a) => { if (a.name === "alfred-agenda") ciclo(); });
messenger.runtime.onInstalled.addListener(agendar);
messenger.runtime.onStartup.addListener(agendar);
messenger.runtime.onMessage.addListener((msg) => {
  if (msg && msg.tipo === "verificar-agora") return ciclo();
  if (msg && msg.tipo === "reagendar") return agendar();
  if (msg && msg.tipo === "calendarios") return messenger.alfredCalendar.listCalendars();
  return undefined;
});
agendar();
