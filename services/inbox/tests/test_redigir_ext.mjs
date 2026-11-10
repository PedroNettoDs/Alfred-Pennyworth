// node --test tests/test_redigir_ext.mjs — texto simples da extensão "Alfred — redigir" (o HTML precisa de DOMParser).
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
const { separar, inserir } = require("../thunderbird-ext-redigir/corpo.js");

const CORPO = "quero confirmar\n\nEm 01/11, Ana escreveu:\n> Pode na terça?\n\n-- \nPedro";

test("separa notas, citação e assinatura", () => {
  const r = separar({ isPlainText: true, plainTextBody: CORPO });
  assert.equal(r.notas, "quero confirmar");
  assert.equal(r.original, "Pode na terça?");
  assert.equal(r.temAssinatura, true);
});

test("troca as notas pelo texto e mantém citação e assinatura", () => {
  const r = inserir({ isPlainText: true, plainTextBody: CORPO }, "Olá, Ana.", "quero confirmar");
  assert.equal(r.plainTextBody, "Olá, Ana.\n\nEm 01/11, Ana escreveu:\n> Pode na terça?\n-- \nPedro");
});

test("mantém o que Pedro digitou depois do clique", () => {
  const r = inserir({ isPlainText: true, plainTextBody: "mudei" }, "Olá.", "antes");
  assert.equal(r.plainTextBody, "Olá.\n\nmudei");
});
