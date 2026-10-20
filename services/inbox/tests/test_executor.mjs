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
