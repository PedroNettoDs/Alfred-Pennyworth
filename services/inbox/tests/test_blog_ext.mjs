// node --test tests/test_blog_ext.mjs — executor da extensão "Alfred — blog" com um `messenger` simulado.
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
const { processarFila } = require("../thunderbird-ext-blog/executor.js");

const DESTINO = "fulano.segredo@blogger.com";
const ITENS = [
  { id: "briefings/a.md", assunto: "Post A", html: "<p>A</p>\n<p>#end</p>",
    audio: { rota: "/blog/briefings/a.md/audio", nome: "a.mp3" }, resultado: "/blog/briefings/a.md/resultado" },
  { id: "jornal/b.md", assunto: "Post B", html: "<p>B</p>", audio: null, resultado: "/blog/jornal/b.md/resultado" },
];

function cenario({ falharEnvio = null, falharPost = false, destino = DESTINO } = {}) {
  const ch = { novos: [], anexos: [], envios: [], removidos: [], posts: [], salvos: 0 };
  let tabs = 0;
  const messenger = {
    compose: {
      beginNew: async (d) => { ch.novos.push(d); return { id: ++tabs, assunto: d.subject }; },
      addAttachment: async (tab, a) => { ch.anexos.push({ tab, nome: a.file.name, tipo: a.file.type }); },
      sendMessage: async (tab, o) => {
        if (falharEnvio && ch.novos[tab - 1].subject === falharEnvio) throw new Error("SMTP recusou");
        ch.envios.push({ tab, mode: o.mode });
      },
    },
    tabs: { remove: async (id) => { ch.removidos.push(id); } },
  };
  const http = async (metodo, rota, corpo) => {
    if (metodo === "GET") return { destino, itens: ITENS };
    ch.posts.push({ rota, corpo });
    if (falharPost) throw new Error("Alfred respondeu 500");
    return { alterado: true };
  };
  const baixar = async () => new Blob(["mp3"]);
  return { messenger, http, baixar, ch, salvar: async () => { ch.salvos++; } };
}

test("padrão: só simula — nada é composto nem enviado", async () => {
  const c = cenario();
  const r = await processarFila(c.messenger, c.http, c.baixar, { anexarAudio: true }, {}, c.salvar);
  assert.deepEqual(r.map((x) => x.status), ["simulado", "simulado"]);
  assert.equal(c.ch.novos.length, 0);
  assert.equal(c.ch.posts.length, 0);
});

test("de verdade: compõe em HTML para o destino do Alfred, anexa o .mp3, envia e confirma", async () => {
  const c = cenario();
  const enviados = {};
  const cfg = { enviarDeVerdade: true, anexarAudio: true, identidade: "id3" };
  const r = await processarFila(c.messenger, c.http, c.baixar, cfg, enviados, c.salvar);
  assert.deepEqual(r.map((x) => x.status), ["postado", "postado"]);
  assert.deepEqual(c.ch.novos[0], { to: [DESTINO], subject: "Post A", body: ITENS[0].html, isPlainText: false, identityId: "id3" });
  assert.deepEqual(c.ch.anexos, [{ tab: 1, nome: "a.mp3", tipo: "audio/mpeg" }]);   // B não tem áudio
  assert.deepEqual(c.ch.envios.map((e) => e.mode), ["sendNow", "sendNow"]);
  assert.deepEqual(c.ch.posts.map((p) => [p.rota, p.corpo.ok]), [[ITENS[0].resultado, true], [ITENS[1].resultado, true]]);
  assert.deepEqual(enviados, {});
});

test("anexarAudio desligado não anexa", async () => {
  const c = cenario();
  await processarFila(c.messenger, c.http, c.baixar, { enviarDeVerdade: true, anexarAudio: false }, {}, c.salvar);
  assert.equal(c.ch.anexos.length, 0);
  assert.equal(c.ch.envios.length, 2);
});

test("falha no envio: fecha a janela, relata o erro ao Alfred e segue para o próximo", async () => {
  const c = cenario({ falharEnvio: "Post A" });
  const enviados = {};
  const r = await processarFila(c.messenger, c.http, c.baixar, { enviarDeVerdade: true }, enviados, c.salvar);
  assert.deepEqual(r.map((x) => x.status), ["erro", "postado"]);
  assert.deepEqual(c.ch.removidos, [1]);
  assert.deepEqual(c.ch.posts[0], { rota: ITENS[0].resultado, corpo: { ok: false, erro: "SMTP recusou" } });
  assert.equal(enviados["briefings/a.md"], undefined);
});

test("Alfred não confirmou: o próximo ciclo repete só o POST, sem enviar de novo", async () => {
  const enviados = {};
  const c1 = cenario({ falharPost: true });
  const r1 = await processarFila(c1.messenger, c1.http, c1.baixar, { enviarDeVerdade: true }, enviados, c1.salvar);
  assert.deepEqual(r1.map((x) => x.status), ["enviado", "enviado"]);
  assert.deepEqual(Object.keys(enviados).sort(), ["briefings/a.md", "jornal/b.md"]);

  const c2 = cenario();
  const r2 = await processarFila(c2.messenger, c2.http, c2.baixar, { enviarDeVerdade: true }, enviados, c2.salvar);
  assert.deepEqual(r2.map((x) => x.status), ["postado", "postado"]);
  assert.equal(c2.ch.novos.length, 0);
  assert.deepEqual(enviados, {});
});

test("Alfred sem BLOGGER_EMAIL: o ciclo falha sem compor nada", async () => {
  const c = cenario({ destino: "" });
  await assert.rejects(processarFila(c.messenger, c.http, c.baixar, { enviarDeVerdade: true }, {}, c.salvar), /BLOGGER_EMAIL/);
  assert.equal(c.ch.novos.length, 0);
});
