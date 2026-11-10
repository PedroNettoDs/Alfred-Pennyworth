/* Separa o que Pedro escreveu do que deve ficar na janela (citação e assinatura) e insere o texto do Alfred. */
"use strict";

const PRESERVAR = "blockquote[type=cite], .moz-cite-prefix, .moz-signature, .moz-forward-container";
const CITACAO = "blockquote[type=cite], .moz-forward-container";

function textoDe(no) {
  const copia = no.cloneNode(true);
  copia.querySelectorAll("br").forEach((b) => b.replaceWith("\n"));
  copia.querySelectorAll("p, div, li").forEach((b) => b.append("\n"));
  return copia.textContent.replace(/\n{3,}/g, "\n\n").trim();
}

function escreveu(no) {          // nó de topo que não é (nem contém) citação ou assinatura
  return !(no.nodeType === 1 && (no.matches(PRESERVAR) || no.querySelector(PRESERVAR)));
}

function linhasPlano(texto) {
  const linhas = (texto || "").split(/\r?\n/);
  const assinatura = linhas.indexOf("-- ");
  const corpo = assinatura >= 0 ? linhas.slice(0, assinatura) : linhas;
  const cita = (l, i) => l.startsWith(">") || (/:\s*$/.test(l) && (corpo[i + 1] || "").startsWith(">"));
  return {
    notas: corpo.filter((l, i) => !cita(l, i)).join("\n").trim(),
    citado: corpo.filter((l) => l.startsWith(">")).map((l) => l.replace(/^>+ ?/, "")).join("\n").trim(),
    resto: [...corpo.filter(cita), ...(assinatura >= 0 ? linhas.slice(assinatura) : [])].join("\n"),
    temAssinatura: assinatura >= 0,
  };
}

function separar(det) {
  if (det.isPlainText) {
    const p = linhasPlano(det.plainTextBody);
    return { notas: p.notas, original: p.citado, temAssinatura: p.temAssinatura };
  }
  const doc = new DOMParser().parseFromString(det.body || "", "text/html");
  const original = [...doc.querySelectorAll(CITACAO)].map(textoDe).join("\n\n");
  const temAssinatura = !!doc.querySelector(".moz-signature");
  [...doc.querySelectorAll(PRESERVAR)].forEach((n) => n.remove());
  return { notas: textoDe(doc.body), original, temAssinatura };
}

/* Campos para setComposeDetails. Se Pedro mudou as notas depois do clique, mantém o texto dele abaixo do rascunho. */
function inserir(det, texto, notasDoPedido) {
  const manter = separar(det).notas !== notasDoPedido;
  if (det.isPlainText) {
    if (manter) return { plainTextBody: `${texto}\n\n${det.plainTextBody}` };
    const resto = linhasPlano(det.plainTextBody).resto;
    return { plainTextBody: resto ? `${texto}\n\n${resto}` : texto };
  }
  const doc = new DOMParser().parseFromString(det.body || "", "text/html");
  if (!manter) [...doc.body.childNodes].filter(escreveu).forEach((n) => n.remove());
  const blocos = texto.split(/\n{2,}/).map((par) => {
    const p = doc.createElement("p");
    par.split("\n").forEach((linha, i) => {
      if (i) p.append(doc.createElement("br"));
      p.append(linha);
    });
    return p;
  });
  doc.body.prepend(...blocos);
  return { body: doc.documentElement.outerHTML };
}

if (typeof module !== "undefined") module.exports = { separar, inserir };
