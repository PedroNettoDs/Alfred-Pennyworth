/*
 * Alfred — blog: lógica pura do envio (testável com node, sem Thunderbird).
 *
 *   - o destino (BLOGGER_EMAIL) vem do Alfred a cada ciclo: o endereço secreto do blog não fica na extensão;
 *   - com `enviarDeVerdade` = false (padrão) nada é enviado: o ciclo só lista o que iria para o blog;
 *   - post enviado entra em `enviados` até o Alfred confirmar: se o POST do resultado falhar, o próximo
 *     ciclo repete só o POST, não o envio (o Blogger publicaria o mesmo post duas vezes).
 */
"use strict";

async function enviarPost(messenger, baixar, item, destino, cfg) {
  const detalhes = { to: [destino], subject: item.assunto, body: item.html, isPlainText: false };
  if (cfg.identidade) detalhes.identityId = cfg.identidade;
  const tab = await messenger.compose.beginNew(detalhes);
  try {
    if (cfg.anexarAudio && item.audio) {
      const blob = await baixar(item.audio.rota);
      await messenger.compose.addAttachment(tab.id, { file: new File([blob], item.audio.nome, { type: "audio/mpeg" }) });
    }
    await messenger.compose.sendMessage(tab.id, { mode: "sendNow" });
  } catch (e) {
    await messenger.tabs.remove(tab.id).catch(() => {});
    throw e;
  }
}

/**
 * Busca a fila do Alfred, envia cada post e confirma. `http(metodo, rota, corpo)` → JSON; `baixar(rota)` → Blob;
 * `enviados` = {id: iso} persistido por `salvar(enviados)`. Retorna [{id, status, ...}] com status
 * simulado | postado | enviado (Alfred não confirmou) | erro.
 */
async function processarFila(messenger, http, baixar, cfg, enviados, salvar) {
  const { destino, itens = [], aviso } = await http("GET", "/blog/fila");
  if (!destino) throw new Error(aviso || "Alfred sem BLOGGER_EMAIL");
  const relatorio = [];
  for (const item of itens) {
    if (!enviados[item.id]) {
      if (!cfg.enviarDeVerdade) {
        relatorio.push({ id: item.id, status: "simulado", assunto: item.assunto, audio: !!item.audio });
        continue;
      }
      try {
        await enviarPost(messenger, baixar, item, destino, cfg);
      } catch (e) {
        const erro = String((e && e.message) || e);
        await http("POST", item.resultado, { ok: false, erro }).catch(() => {});
        relatorio.push({ id: item.id, status: "erro", erro });
        continue;
      }
      enviados[item.id] = new Date().toISOString();
      await salvar(enviados);
    }
    try {
      await http("POST", item.resultado, { ok: true });
      delete enviados[item.id];
      await salvar(enviados);
      relatorio.push({ id: item.id, status: "postado" });
    } catch (e) {
      relatorio.push({ id: item.id, status: "enviado", erro: `Alfred não confirmou: ${(e && e.message) || e}` });
    }
  }
  return relatorio;
}

const AlfredBlog = { enviarPost, processarFila };
if (typeof module !== "undefined") module.exports = AlfredBlog;
else globalThis.AlfredBlog = AlfredBlog;
