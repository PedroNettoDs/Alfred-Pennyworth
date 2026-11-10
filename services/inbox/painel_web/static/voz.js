/*
 * Voz das telas de conversa (conversa.html e escrita.html). Rotas em painel_voz.py.
 *
 * Falar (#microfone): grava; ao parar, manda o áudio para POST /painel/api/voz, põe a transcrição (feita na
 * máquina) no campo #texto e envia a mensagem, como um áudio de WhatsApp. Se a transcrição falhar, nada é enviado.
 * Transcrever enquanto grava foi medido e não entrou (docs/planos/integracoes-pestinha.md, fase 8).
 *
 * Ativar áudio (#audio-alfred): o Alfred fala a resposta enquanto ela chega. conversa.js e escrita.js avisam cada
 * pedaço de texto ("alfred:texto") e o fim ("alfred:resposta"); aqui o texto é cortado em frases, cada frase vira um
 * áudio (POST /painel/api/voz/falar devolve a URL na hora; o .mp3 é gerado enquanto toca) e elas tocam em fila.
 * A escolha fica em localStorage. Enviar outra mensagem, gravar ou desligar o botão cala o Alfred.
 *
 * Sem microfone ou MediaRecorder, o botão Falar some.
 */
(function () {
  "use strict";

  var MAX_MS = 120000;          // gravação: o mesmo limite de painel_voz.MAX_SEGUNDOS
  var FALA_MAX = 1200;          // letras faladas por resposta (painel_voz.FALA_MAX); depois disso, RESTO
  var RESTO = "O restante está na tela.";
  var FRASE_MIN = 25;           // frase menor que isso espera a seguinte ("1.", "Sr.", "Ok.")
  var CHAVE_AUDIO = "alfred_audio";

  var campo = document.getElementById("texto");
  var botao = document.getElementById("microfone");
  var botaoAudio = document.getElementById("audio-alfred");
  var meta = document.querySelector('meta[name="csrf-token"]');
  var csrf = meta ? meta.content : "";

  function postar(url, corpo, tipo) {
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "X-CSRF-Token": csrf, "Content-Type": tipo },
      body: corpo
    }).then(function (resposta) {
      return resposta.json().then(function (dados) { return { ok: resposta.ok, dados: dados }; });
    });
  }

  // ── Ativar áudio: o Alfred fala a resposta, frase a frase ───────────────

  var fila = [];                // áudios das frases, na ordem; o primeiro é o que toca
  var pedidos = Promise.resolve();   // os pedidos de fala saem um por vez, para a fila ficar na ordem do texto
  var geracao = 0;              // muda quando o Alfred é calado: o que foi pedido antes é descartado
  var resto = "";               // texto da resposta atual que ainda não fechou uma frase
  var dito = 0;                 // letras já mandadas para a fala nesta resposta
  var chegouTexto = false;      // esta resposta veio aos pedaços (senão fala o texto do fim de uma vez)

  function audioLigado() {
    try { return localStorage.getItem(CHAVE_AUDIO) === "1"; } catch (e) { return false; }
  }

  function novaResposta() {
    resto = "";
    dito = 0;
    chegouTexto = false;
  }

  function calar() {
    geracao += 1;
    fila.forEach(function (audio) { audio.pause(); });
    fila = [];
    novaResposta();
  }

  function tocar() {
    if (fila.length) fila[0].play().catch(function () { /* o navegador bloqueou tocar sozinho: a resposta está na tela */ });
  }

  function proximo(audio) {
    if (fila[0] !== audio) return;
    fila.shift();
    tocar();
  }

  function falar(frase) {
    var minha = geracao;
    pedidos = pedidos.then(function () {
      if (minha !== geracao) return null;
      return postar("/painel/api/voz/falar", JSON.stringify({ texto: frase }), "application/json");
    }).then(function (r) {
      if (!r || minha !== geracao || !r.ok || !r.dados.url) return;      // frase sem nada falável: pula
      var audio = new Audio(r.dados.url);                                // já começa a baixar: fica pronto na sua vez
      audio.preload = "auto";
      audio.addEventListener("ended", function () { proximo(audio); });
      audio.addEventListener("error", function () { proximo(audio); });
      fila.push(audio);
      if (fila.length === 1) tocar();
    }).catch(function () { /* sem a API não há fala; a resposta está na tela */ });
  }

  function fatiar(final) {
    var fim = /[.!?…]+["')\]*_]*\s+|\n+/g, m;
    while (dito <= FALA_MAX && (m = fim.exec(resto))) {
      var ate = m.index + m[0].length;
      if (resto.slice(0, ate).trim().length < FRASE_MIN) continue;       // curta demais: junta com a próxima
      falarTrecho(resto.slice(0, ate));
      resto = resto.slice(ate);
      fim.lastIndex = 0;
    }
    if (final && dito <= FALA_MAX && resto.trim()) falarTrecho(resto);
    if (final) resto = "";
  }

  function falarTrecho(trecho) {
    dito += trecho.length;
    falar(trecho);
    if (dito > FALA_MAX) falar(RESTO);
  }

  function pintarAudio() {
    if (!botaoAudio) return;
    var ligado = audioLigado();
    botaoAudio.textContent = ligado ? "Desativar áudio" : "Ativar áudio";
    botaoAudio.setAttribute("aria-pressed", ligado ? "true" : "false");
  }

  if (botaoAudio) {
    pintarAudio();
    botaoAudio.addEventListener("click", function () {
      try { localStorage.setItem(CHAVE_AUDIO, audioLigado() ? "0" : "1"); } catch (e) { /* sem armazenamento: fica desligado */ }
      if (!audioLigado()) calar();
      pintarAudio();
    });
    document.addEventListener("alfred:texto", function (e) {
      if (!audioLigado()) return;
      chegouTexto = true;
      resto += (e.detail && e.detail.texto) || "";
      fatiar(false);
    });
    document.addEventListener("alfred:resposta", function (e) {
      if (!audioLigado()) return;
      if (e.detail && e.detail.parado) {
        calar();
        return;
      }
      if (!chegouTexto) resto = (e.detail && e.detail.texto) || "";      // resposta que não veio aos pedaços
      fatiar(true);
      novaResposta();                                                    // a fila continua tocando o que falta
    });
    if (campo && campo.form) campo.form.addEventListener("submit", calar, true);   // mensagem nova: ele para de falar
  }

  // ── Falar: microfone ────────────────────────────────────────────────────

  if (!botao || !campo) return;
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || !window.MediaRecorder) {
    botao.hidden = true;
    return;
  }

  var ROTULO = botao.textContent;
  var nota = document.createElement("p");        // erro do microfone, visível embaixo do campo
  nota.className = "mensagem mensagem--erro";
  nota.setAttribute("role", "alert");
  nota.hidden = true;
  (campo.form || botao.parentNode).appendChild(nota);

  var gravador = null, fluxo = null, pedacos = [], prazo = null;

  function mostrar(texto, gravando) {
    botao.textContent = texto;
    botao.setAttribute("aria-pressed", gravando ? "true" : "false");
    botao.classList.toggle("botao--perigo", !!gravando);
  }

  function avisar(texto) {
    nota.textContent = texto;
    nota.hidden = !texto;
  }

  function enviar() {
    if (!campo.form) return;
    if (campo.form.requestSubmit) campo.form.requestSubmit();
    else campo.form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  }

  function transcrever() {
    clearTimeout(prazo);
    fluxo.getTracks().forEach(function (faixa) { faixa.stop(); });
    var tipo = gravador.mimeType || "audio/webm";
    var entendeu = false;
    botao.disabled = true;
    mostrar("Transcrevendo…", false);
    postar("/painel/api/voz", new Blob(pedacos, { type: tipo }), tipo).then(function (r) {
      if (!r.ok || !r.dados.texto) {
        avisar(r.dados.erro || "Não entendi o áudio.");
        return;
      }
      campo.value = (campo.value ? campo.value.replace(/\s*$/, " ") : "") + r.dados.texto;
      campo.dispatchEvent(new Event("input", { bubbles: true }));
      avisar("");
      entendeu = true;
    }).catch(function () {
      avisar("Não foi possível falar com a API do Alfred.");
    }).then(function () {
      botao.disabled = false;
      mostrar(ROTULO, false);
      campo.focus();
      if (entendeu) enviar();                     // como um áudio: falou, enviou
    });
  }

  botao.addEventListener("click", function () {
    if (gravador && gravador.state === "recording") {
      gravador.stop();
      return;
    }
    calar();                                      // o Alfred para de falar quando você começa
    navigator.mediaDevices.getUserMedia({ audio: true }).then(function (entrada) {
      fluxo = entrada;
      pedacos = [];
      gravador = new MediaRecorder(entrada);
      gravador.addEventListener("dataavailable", function (e) { if (e.data && e.data.size) pedacos.push(e.data); });
      gravador.addEventListener("stop", transcrever);
      gravador.start();
      mostrar("Parar e enviar", true);
      avisar("");
      prazo = setTimeout(function () { if (gravador.state === "recording") gravador.stop(); }, MAX_MS);
    }).catch(function () {
      avisar("Sem acesso ao microfone. Libere o microfone para esta página no navegador.");
    });
  });
})();
