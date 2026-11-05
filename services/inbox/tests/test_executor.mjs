// node --test tests/test_executor.mjs — executor da extensão com um `messenger` simulado.
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
const { executarLote, processarFila } = require("../thunderbird-ext/executor.js");

function caixa() {
  const trash = { id: "acc1://Deleted", accountId: "acc1", path: "/Deleted", specialUse: ["trash"] };
  const inbox = { id: "acc1://INBOX", accountId: "acc1", path: "/INBOX", specialUse: ["inbox"] };
  const teste = { id: "acc1://Alfred-Teste", accountId: "acc1", path: "/Alfred-Teste" };
  const outra = { id: "acc2://INBOX", accountId: "acc2", path: "/INBOX" };
  const msgs = [
    { id: 1, headerMessageId: "a@x", author: "Loja <ofertas@loja.com>", subject: "50% OFF", folder: teste, flagged: false },
    { id: 2, headerMessageId: "b@x", author: "ofertas@loja.com", subject: "Outro", folder: teste, flagged: true },
    { id: 3, headerMessageId: "c@x", author: "ofertas@loja.com", subject: "Real", folder: inbox, flagged: false },
    { id: 4, headerMessageId: "d@x", author: "ofertas@loja.com", subject: "Outra conta", folder: outra, flagged: false },
    { id: 5, headerMessageId: "e@x", author: "golpe@x.com", subject: "50% OFF", folder: teste, flagged: false },
  ];
  const chamadas = { move: [], delete: [] };
  const messenger = {
    messages: {
      query: async ({ headerMessageId }) => ({ id: null, messages: msgs.filter((m) => m.headerMessageId === headerMessageId) }),
      continueList: async () => ({ id: null, messages: [] }),
      move: async (ids, pasta) => { chamadas.move.push({ ids, pasta }); },
      delete: async (...a) => { chamadas.delete.push(a); throw new Error("delete nunca deve ser chamado"); },
    },
    folders: { query: async ({ accountId }) => (accountId === "acc1" ? [trash] : []) },
  };
  return { messenger, chamadas };
}

const lote = {
  id: "limpeza-1", acao: "mover_para_lixeira",
  itens: [
    { message_id: "a@x", from: "ofertas@loja.com", subject: "50% OFF" },
    { message_id: "b@x", from: "ofertas@loja.com", subject: "Outro" },
    { message_id: "c@x", from: "ofertas@loja.com", subject: "Real" },
    { message_id: "d@x", from: "ofertas@loja.com", subject: "Outra conta" },
    { message_id: "e@x", from: "ofertas@loja.com", subject: "50% OFF" },
  ],
};
const porId = (r) => Object.fromEntries(r.map((x) => [x.message_id, x.status]));

test("padrão: modo teste + simulação — nada é movido", async () => {
  const { messenger, chamadas } = caixa();
  const r = porId(await executarLote(messenger, lote, { contasPermitidas: ["acc1"], modoTeste: true, pastaTeste: "Alfred-Teste" }));
  assert.equal(r["a@x"], "simulado");
  assert.equal(r["b@x"], "recusado");        // estrela
  assert.equal(r["c@x"], "nao_encontrado");  // fora da pasta de teste
  assert.equal(r["d@x"], "nao_encontrado");  // conta não permitida
  assert.equal(r["e@x"], "recusado");        // remetente não confere
  assert.equal(chamadas.move.length, 0);
  assert.equal(chamadas.delete.length, 0);
});

test("execução real move só para a Lixeira, nunca delete", async () => {
  const { messenger, chamadas } = caixa();
  const r = porId(await executarLote(messenger, lote, { contasPermitidas: ["acc1"], modoTeste: true, executarDeVerdade: true }));
  assert.equal(r["a@x"], "movido");
  assert.deepEqual(chamadas.move, [{ ids: [1], pasta: "acc1://Deleted" }]);
  assert.equal(chamadas.delete.length, 0);
});

test("lote com executar=true (botão Limpar do Painel) move mesmo com executarDeVerdade desligado", async () => {
  const { messenger, chamadas } = caixa();
  const cfg = { contasPermitidas: ["acc1"], modoTeste: false, executarDeVerdade: false };
  const r = porId(await executarLote(messenger, { ...lote, executar: true }, cfg));
  assert.equal(r["a@x"], "movido");
  assert.equal(r["c@x"], "movido");
  assert.equal(r["b@x"], "recusado");   // estrela continua protegendo
  assert.deepEqual(chamadas.move, [{ ids: [1, 3], pasta: "acc1://Deleted" }]);
  assert.equal(chamadas.delete.length, 0);
  // só true liga a execução; o modo teste continua valendo para o lote do Limpar
  const r2 = porId(await executarLote(caixa().messenger, { ...lote, executar: "sim" }, cfg));
  assert.equal(r2["a@x"], "simulado");
  const r3 = porId(await executarLote(caixa().messenger, { ...lote, executar: true }, { ...cfg, modoTeste: true }));
  assert.equal(r3["a@x"], "movido");
  assert.equal(r3["c@x"], "nao_encontrado");
});

test("sem contas permitidas nada acontece; ação desconhecida é recusada", async () => {
  const { messenger, chamadas } = caixa();
  const r1 = await executarLote(messenger, lote, { contasPermitidas: [], executarDeVerdade: true });
  assert.ok(r1.every((x) => x.status === "recusado"));
  const r2 = await executarLote(messenger, { ...lote, acao: "apagar_permanente" }, { contasPermitidas: ["acc1"], executarDeVerdade: true });
  assert.ok(r2.every((x) => x.status === "recusado"));
  assert.equal(chamadas.move.length, 0);
});

test("conta sem Lixeira identificável é recusada", async () => {
  const { messenger, chamadas } = caixa();
  const r = porId(await executarLote(messenger, lote, { contasPermitidas: ["acc2"], modoTeste: false, executarDeVerdade: true }));
  assert.equal(r["d@x"], "recusado");
  assert.equal(chamadas.move.length, 0);
});

test("processarFila relata cada lote ao Alfred", async () => {
  const { messenger } = caixa();
  const posts = [];
  const http = async (metodo, rota, corpo) => {
    if (metodo === "GET") return { lotes: [lote] };
    posts.push({ rota, corpo });
    return {};
  };
  await processarFila(messenger, http, { contasPermitidas: ["acc1"], modoTeste: true });
  assert.equal(posts.length, 1);
  assert.equal(posts[0].rota, "/limpeza/limpeza-1/resultado");
  assert.equal(posts[0].corpo.itens.length, 5);
});

test("mensagem que só existe na Lixeira volta como ja_na_lixeira, sem mover", async () => {
  const { messenger, chamadas } = caixa();
  const lixeira = { id: "acc1://Deleted", accountId: "acc1", path: "/Deleted", specialUse: ["trash"] };
  const query = messenger.messages.query;
  messenger.messages.query = async ({ headerMessageId }) => (headerMessageId === "z@x"
    ? { id: null, messages: [{ id: 9, headerMessageId: "z@x", author: "ofertas@loja.com", subject: "Velha", folder: lixeira }] }
    : query({ headerMessageId }));
  const r = await executarLote(messenger, { id: "l", acao: "mover_para_lixeira", executar: true,
    itens: [{ message_id: "z@x", from: "ofertas@loja.com", subject: "Velha" }] }, { contasPermitidas: ["acc1"], modoTeste: false });
  assert.deepEqual(r, [{ message_id: "z@x", status: "ja_na_lixeira", detalhe: "já estava na Lixeira" }]);
  assert.equal(chamadas.move.length, 0);
});
