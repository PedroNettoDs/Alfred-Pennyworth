/* Roda dentro do editor do e-mail: conta ao background o que está selecionado e troca o trecho quando a reformulação volta. */
"use strict";

let ultimo = null;      // { range, texto, marca }: a última seleção não vazia
let avisado = "";
let congelado = null;   // a seleção no momento do pedido; continua valendo se Pedro clicar em outro lugar enquanto o Alfred escreve
let timer;

function observar() {
  const s = window.getSelection();
  const texto = s.isCollapsed ? "" : s.toString().trim();
  if (texto) {
    const range = s.getRangeAt(0).cloneRange();
    ultimo = { range, texto, marca: range.toString() };
  }
  if (texto !== avisado) {
    avisado = texto;
    browser.runtime.sendMessage({ tipo: "selecao", texto }).catch(() => {});
  }
}

function substituir(texto) {
  if (!congelado || congelado.range.toString() !== congelado.marca) return { ok: false };
  const s = window.getSelection();
  s.removeAllRanges();
  s.addRange(congelado.range);
  document.body.focus();
  let feito = false;
  try {
    feito = document.execCommand("insertText", false, texto);   // preserva o Ctrl+Z
  } catch (e) {
    feito = false;
  }
  if (!feito) {
    congelado.range.deleteContents();
    congelado.range.insertNode(document.createTextNode(texto));
  }
  congelado = null;
  return { ok: true };
}

document.addEventListener("selectionchange", () => {
  clearTimeout(timer);
  timer = setTimeout(observar, 150);
});

browser.runtime.onMessage.addListener((msg) => {
  if (msg.tipo === "congelar") {
    congelado = ultimo;   // só é chamado em modo reformular; não depende de a seleção seguir visível com o diálogo aberto
    return Promise.resolve({ texto: congelado ? congelado.texto : "" });
  }
  if (msg.tipo === "substituir") return Promise.resolve(substituir(msg.texto));
  return undefined;
});
