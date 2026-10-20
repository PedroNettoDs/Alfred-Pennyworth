/*
 * Alfred — limpeza da caixa: lógica pura do executor (testável com node, sem Thunderbird).
 *
 * Regras de segurança aplicadas AQUI, independentemente do que o Alfred mandar:
 *   - a única operação é messages.move(..., <pasta Lixeira da mesma conta>); nunca messages.delete
 *     e nunca skipTrash — se a conta não tiver Lixeira identificável, o item é recusado;
 *   - só contas marcadas pelo usuário na página de opções (vazio = nada é executado);
 *   - modo teste: só mensagens em pastas cujo caminho contém `pastaTeste` (ex.: "Alfred-Teste");
 *   - a mensagem encontrada precisa bater Message-ID E remetente E assunto com o item do lote;
 *   - mensagens com estrela (flagged) ou já na Lixeira são recusadas;
 *   - com `executarDeVerdade` = false (padrão) nada é movido: o resultado volta como "simulado".
 */
"use strict";

function normalizar(s) {
  return String(s || "").normalize("NFKC").replace(/\s+/g, " ").trim().toLowerCase();
}

function semColchetes(mid) {
  return String(mid || "").trim().replace(/^</, "").replace(/>$/, "");
}

function ehLixeira(folder) {
  if (!folder) return false;
  if (Array.isArray(folder.specialUse) && folder.specialUse.includes("trash")) return true;
  return folder.type === "trash";
}

async function listarMensagens(messenger, lista) {
  // MV3: messages.query devolve {id, messages}; páginas seguintes via messages.continueList(id)
  let out = [...(lista.messages || [])];
  let id = lista.id;
  while (id) {
    const prox = await messenger.messages.continueList(id);
    out = out.concat(prox.messages || []);
    id = prox.id;
  }
  return out;
}

async function acharLixeira(messenger, accountId, cache) {
  if (cache.has(accountId)) return cache.get(accountId);
  let lixeira = null;
  try {
    const achadas = await messenger.folders.query({ accountId, specialUse: ["trash"] });
    lixeira = (achadas || [])[0] || null;
  } catch (e) {
    lixeira = null;
  }
  cache.set(accountId, lixeira);
  return lixeira;
}

/**
 * Executa um lote. Retorna [{message_id, status, detalhe}] com status
 * movido | simulado | nao_encontrado | recusado | erro.
 */
async function executarLote(messenger, lote, cfg) {
  const contas = new Set(cfg.contasPermitidas || []);
  const resultados = [];
  const cacheLixeira = new Map();
  if (lote.acao !== "mover_para_lixeira") {
    return (lote.itens || []).map((i) => ({ message_id: i.message_id, status: "recusado", detalhe: `ação desconhecida: ${lote.acao}` }));
  }
  const porLixeira = new Map(); // folderId da lixeira → {pasta, ids:[], mids:[]}

  for (const item of lote.itens || []) {
    const mid = item.message_id;
    try {
      if (!contas.size) {
        resultados.push({ message_id: mid, status: "recusado", detalhe: "nenhuma conta permitida nas opções da extensão" });
        continue;
      }
      const lista = await messenger.messages.query({ headerMessageId: semColchetes(mid) });
      const achadas = (await listarMensagens(messenger, lista)).filter((m) => contas.has(m.folder && m.folder.accountId));
      const candidatas = achadas.filter((m) => !ehLixeira(m.folder) &&
        (!cfg.modoTeste || String(m.folder.path || "").includes(cfg.pastaTeste || "Alfred-Teste")));
      if (!candidatas.length) {
        const motivo = achadas.length ? "só existe na Lixeira ou fora da pasta de teste" : "não encontrada nas contas permitidas";
        resultados.push({ message_id: mid, status: "nao_encontrado", detalhe: motivo });
        continue;
      }
      if (candidatas.length > 1) {
        resultados.push({ message_id: mid, status: "recusado", detalhe: `Message-ID ambíguo (${candidatas.length} cópias)` });
        continue;
      }
      const m = candidatas[0];
      if (!normalizar(m.author).includes(normalizar(item.from))) {
        resultados.push({ message_id: mid, status: "recusado", detalhe: `remetente não confere (${m.author})` });
        continue;
      }
      if (normalizar(m.subject) !== normalizar(item.subject)) {
        resultados.push({ message_id: mid, status: "recusado", detalhe: "assunto não confere" });
        continue;
      }
      if (m.flagged) {
        resultados.push({ message_id: mid, status: "recusado", detalhe: "mensagem com estrela" });
        continue;
      }
      const lixeira = await acharLixeira(messenger, m.folder.accountId, cacheLixeira);
      if (!lixeira || !ehLixeira(lixeira)) {
        resultados.push({ message_id: mid, status: "recusado", detalhe: "conta sem pasta Lixeira identificável" });
        continue;
      }
      const chave = lixeira.id || `${lixeira.accountId}:${lixeira.path}`;
      if (!porLixeira.has(chave)) porLixeira.set(chave, { pasta: lixeira, ids: [], mids: [] });
      porLixeira.get(chave).ids.push(m.id);
      porLixeira.get(chave).mids.push(mid);
    } catch (e) {
      resultados.push({ message_id: mid, status: "erro", detalhe: String(e && e.message || e) });
    }
  }

  for (const { pasta, ids, mids } of porLixeira.values()) {
    if (!cfg.executarDeVerdade) {
      mids.forEach((mid) => resultados.push({ message_id: mid, status: "simulado", detalhe: `iria para ${pasta.path}` }));
      continue;
    }
    try {
      // MV3 aceita o id da pasta (string); versões antigas aceitam o MailFolder
      await messenger.messages.move(ids, pasta.id || pasta);
      mids.forEach((mid) => resultados.push({ message_id: mid, status: "movido", detalhe: `→ ${pasta.path}` }));
    } catch (e) {
      mids.forEach((mid) => resultados.push({ message_id: mid, status: "erro", detalhe: String(e && e.message || e) }));
    }
  }
  return resultados;
}

/** Busca a fila do Alfred, executa e devolve o resultado de cada lote. `http(metodo, rota, corpo)` → JSON. */
async function processarFila(messenger, http, cfg) {
  const resp = await http("GET", "/limpeza/fila");
  const relatorio = [];
  for (const lote of resp.lotes || []) {
    const itens = await executarLote(messenger, lote, cfg);
    await http("POST", `/limpeza/${encodeURIComponent(lote.id)}/resultado`, { itens, executor: "thunderbird-ext" });
    relatorio.push({ id: lote.id, itens });
  }
  return relatorio;
}

const AlfredLimpeza = { executarLote, processarFila, ehLixeira, normalizar };
if (typeof module !== "undefined") module.exports = AlfredLimpeza;
else globalThis.AlfredLimpeza = AlfredLimpeza;
