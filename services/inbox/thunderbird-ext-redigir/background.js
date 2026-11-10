/* Alfred — redigir: botão ao lado de "Anexar". Abre o diálogo, fala com o Alfred e põe o texto na janela; nunca envia. */
"use strict";

const PADRAO = { urlAlfred: "http://127.0.0.1:7073", token: "" };
const ROTULO = { redigir: "Redigir e-mail com o Alfred", reformular: "Reformular com o Alfred" };
const DIAS_AGENDA = 90;
const ESPERA_MS = 3000;
const emAndamento = new Set();
const selecoes = new Map();    // tabId → texto selecionado

const carregarCfg = async () => ({ ...PADRAO, ...(await messenger.storage.local.get(Object.keys(PADRAO))) });

async function http(cfg, metodo, rota, corpo) {
  const r = await fetch(cfg.urlAlfred.replace(/\/$/, "") + rota, {
    method: metodo,
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${cfg.token}` },
    body: corpo ? JSON.stringify(corpo) : undefined,
  });
  if (!r.ok) {
    const detalhe = await r.json().then((j) => j.detail, () => "");
    throw new Error(detalhe || `Alfred respondeu ${r.status} em ${rota}`);
  }
  return r.json();
}

const modoDa = (tabId) => (selecoes.get(tabId) ? "reformular" : "redigir");

async function atualizarBotao(tabId) {
  const rotulo = ROTULO[modoDa(tabId)];
  await messenger.composeAction.setLabel({ tabId, label: rotulo });
  if (!emAndamento.has(tabId)) await messenger.composeAction.setTitle({ tabId, title: rotulo });
}

async function estado(tabId, badge, titulo) {
  await messenger.composeAction.setBadgeText({ tabId, text: badge });
  await messenger.composeAction.setTitle({ tabId, title: titulo });
}

function avisar(mensagem) {
  return messenger.notifications.create({ type: "basic", title: "Alfred", message: mensagem });
}

// null = não deu para ler: o Alfred avisa no painel que só viu o que ele mesmo anotou.
async function eventosDoCalendario() {
  const agora = Date.now();
  try {
    return await messenger.alfredAgendaLeitura.listarEventos(
      new Date(agora).toISOString(), new Date(agora + DIAS_AGENDA * 864e5).toISOString());
  } catch (e) {
    console.error("[alfred-redigir] calendário:", e);
    return null;
  }
}

async function aguardarPainel(cfg, tabId, id) {
  for (;;) {
    await new Promise((ok) => setTimeout(ok, ESPERA_MS));
    await messenger.tabs.get(tabId);     // janela fechada: rejeita e encerra a espera
    const r = await http(cfg, "GET", `/redigir/${encodeURIComponent(id)}`);
    if (r.status !== "aguardando") return r;
  }
}

async function redigir(cfg, tabId, instrucao) {
  await estado(tabId, "…", "Alfred está redigindo…");
  const det = await messenger.compose.getComposeDetails(tabId);
  const { notas, original, temAssinatura } = separar(det);
  let r = await http(cfg, "POST", "/redigir", {
    de: String(det.from || ""),
    para: [].concat(det.to || []).map(String),
    assunto: det.subject || "",
    notas: [notas, instrucao].filter(Boolean).join("\n\n"),
    original,
    tem_assinatura: temAssinatura,
    eventos: await eventosDoCalendario(),
  });
  if (r.status === "aguardando") {
    await estado(tabId, "!", "Aguardando você no Painel do Alfred");
    r = await aguardarPainel(cfg, tabId, r.id);
  }
  if (!r.texto) {
    await avisar("Rascunho recusado no painel. Nada mudou no e-mail.");
    return;
  }
  const atual = await messenger.compose.getComposeDetails(tabId);
  await messenger.compose.setComposeDetails(tabId, inserir(atual, r.texto, notas));
}

async function reformular(cfg, tabId, instrucao) {
  await estado(tabId, "…", "Alfred está reformulando…");
  const { texto: trecho } = await messenger.tabs.sendMessage(tabId, { tipo: "congelar" });
  if (!trecho) throw new Error("A seleção se perdeu. Selecione o trecho de novo.");
  const det = await messenger.compose.getComposeDetails(tabId);
  const { texto } = await http(cfg, "POST", "/reformular", {
    trecho, instrucao, assunto: det.subject || "", corpo: separar(det).notas,
  });
  const r = await messenger.tabs.sendMessage(tabId, { tipo: "substituir", texto });
  if (!r.ok) throw new Error("O trecho mudou enquanto o Alfred escrevia. Nada foi trocado.");
}

async function executar(tabId, modo, instrucao) {
  if (emAndamento.has(tabId)) return;
  emAndamento.add(tabId);
  try {
    const cfg = await carregarCfg();
    await (modo === "reformular" ? reformular : redigir)(cfg, tabId, instrucao);
  } catch (e) {
    const aberta = await messenger.tabs.get(tabId).then(() => true, () => false);
    if (aberta) await avisar(String((e && e.message) || e));
  } finally {
    emAndamento.delete(tabId);
    await estado(tabId, "", ROTULO[modoDa(tabId)]).catch(() => {});
  }
}

messenger.runtime.onMessage.addListener((msg, sender) => {
  if (msg.tipo === "selecao" && sender.tab) {
    selecoes.set(sender.tab.id, msg.texto);
    atualizarBotao(sender.tab.id).catch(() => {});
    return undefined;
  }
  if (msg.tipo === "abrir") {
    return carregarCfg().then((cfg) => ({
      semToken: !cfg.token,
      ocupado: emAndamento.has(msg.tabId),
      modo: modoDa(msg.tabId),
      trecho: selecoes.get(msg.tabId) || "",
    }));
  }
  if (msg.tipo === "executar") {
    executar(msg.tabId, msg.modo, msg.instrucao);   // sem await: o diálogo fecha e o badge mostra o andamento
    return Promise.resolve();
  }
  return undefined;
});

messenger.tabs.onRemoved.addListener((tabId) => selecoes.delete(tabId));
