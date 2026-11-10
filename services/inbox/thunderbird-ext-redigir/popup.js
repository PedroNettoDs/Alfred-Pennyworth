"use strict";
const $ = (id) => document.getElementById(id);
const TEXTOS = {
  redigir: { titulo: "Redigir e-mail com o Alfred", dica: "O que você quer dizer? Pode escrever do seu jeito.", botao: "Redigir" },
  reformular: { titulo: "Reformular com o Alfred", dica: "O que mudar? Vazio = deixar mais claro e natural.", botao: "Reformular" },
};

function avisar(texto) {
  $("aviso").textContent = texto;
  $("aviso").hidden = false;
}

async function iniciar() {
  const [tab] = await messenger.tabs.query({ active: true, currentWindow: true });
  const info = await messenger.runtime.sendMessage({ tipo: "abrir", tabId: tab.id });
  if (info.semToken) {
    avisar("Configure o token nas opções da extensão.");
    await messenger.runtime.openOptionsPage();
    return;
  }
  if (info.ocupado) {
    avisar("O Alfred ainda está trabalhando neste e-mail.");
    return;
  }
  const t = TEXTOS[info.modo];
  $("titulo").textContent = t.titulo;
  $("instrucao").placeholder = t.dica;
  $("enviar").textContent = t.botao;
  if (info.modo === "reformular") {
    $("trecho").textContent = info.trecho;
    $("trecho").hidden = false;
    $("instrucao").rows = 3;
  }
  $("formulario").hidden = false;
  $("instrucao").focus();

  const enviar = async () => {
    await messenger.runtime.sendMessage({ tipo: "executar", tabId: tab.id, modo: info.modo, instrucao: $("instrucao").value.trim() });
    window.close();
  };
  $("enviar").addEventListener("click", enviar);
  $("instrucao").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && e.ctrlKey) enviar();
  });
}

iniciar().catch((e) => avisar(String((e && e.message) || e)));
