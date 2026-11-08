/*
 * Janela do Alfred ao lado do WordCraft (escrita.py): mostra a seleção atual e conversa em stream.
 * Tudo que vem do documento ou do modelo entra como texto (textContent); o único HTML é o da resposta
 * final, já escapado no servidor por painel._markdown_seguro.
 */
(function () {
  "use strict";

  var INTERVALO_MS = 1200;
  var URL_LOGIN = "/painel/login?proximo=" + encodeURIComponent("/painel/escrita");
  var ROTULOS = {
    rodando: "executando…", ok: "feito", erro: "erro", negada: "negada", expirada: "expirada", cancelada: "cancelada"
  };

  var conversa = null;
  var turno = null;
  var fonte = null;
  var bloco = null;
  var bruto = "";

  function $(id) { return document.getElementById(id); }

  function novo(tag, classe, texto) {
    var el = document.createElement(tag);
    if (classe) el.className = classe;
    if (texto !== undefined) el.textContent = texto;
    return el;
  }

  function csrf() {
    var m = document.querySelector('meta[name="csrf-token"]');
    return m ? m.getAttribute("content") || "" : "";
  }

  function responder(r) {
    if (r.status === 401) {
      location.assign(URL_LOGIN);
      throw new Error("Sessão expirada.");
    }
    return r.json().then(function (d) {
      if (!r.ok) throw new Error(d.erro || "O Alfred recusou o pedido.");
      return d;
    });
  }

  function postar(url, corpo) {
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf() },
      body: JSON.stringify(corpo || {})
    }).then(responder);
  }

  function rolar() {
    var m = $("mensagens");
    m.scrollTop = m.scrollHeight;
  }

  function alcance() {
    return document.querySelector('input[name="alcance"]:checked').value;
  }

  // ── seleção atual no WordCraft ─────────────────────────────────────────

  function mostrarEstado(e) {
    var doc = $("documento");
    doc.textContent = e.conectado ? (e.titulo || "Documento sem nome") + (e.alterado ? " · não salvo" : "") : e.erro;
    doc.classList.toggle("escrita__documento--erro", !e.conectado);
    var inteiro = alcance() === "documento";
    $("previa").textContent = !e.conectado ? "" : inteiro ? "Alfred vai ler o documento inteiro."
      : e.caracteres ? "“" + e.previa + "”" : "Nada selecionado no documento.";
    $("contagem").textContent = e.conectado && !inteiro && e.caracteres ? e.caracteres + " caracteres selecionados" : "";
  }

  function atualizarEstado() {
    if (document.hidden) return;
    fetch("/painel/api/escrita/estado", { credentials: "same-origin" })
      .then(responder)
      .then(mostrarEstado)
      .catch(function () { mostrarEstado({ conectado: false, erro: "Sem contato com a API do Alfred." }); });
  }

  // ── um turno da conversa ───────────────────────────────────────────────

  function ocupado(sim) {
    $("enviar").disabled = sim;
    $("nova").disabled = sim;
    $("parar").hidden = !sim;
  }

  function encerrar() {
    if (fonte) fonte.close();
    fonte = null;
    turno = null;
    ocupado(false);
    $("texto").focus();
  }

  function mostrarErro(texto) {
    $("mensagens").appendChild(novo("p", "escrita__erro", texto));
    rolar();
  }

  function adicionarPergunta(texto, r) {
    var el = novo("div", "pergunta");
    if (r.alcance === "documento") el.appendChild(novo("p", "pergunta__citacao", "Documento inteiro"));
    else if (r.previa) el.appendChild(novo("p", "pergunta__citacao", "“" + r.previa + "”"));
    el.appendChild(novo("p", "pergunta__texto", texto));
    $("mensagens").appendChild(el);
  }

  function abrirBloco() {
    bruto = "";
    bloco = novo("div", "resposta");
    bloco.ferramentas = novo("div", "resposta__ferramentas");
    bloco.corpo = novo("div", "resposta__corpo resposta__corpo--stream", "");
    bloco.appendChild(bloco.corpo);
    bloco.appendChild(bloco.ferramentas);
    $("mensagens").appendChild(bloco);
  }

  function mostrarFerramenta(ev) {
    var chips = bloco.ferramentas.querySelectorAll('[data-rodando="1"]');
    var chip = null;
    chips.forEach(function (c) { if (c.dataset.nome === ev.nome) chip = c; });
    if (!chip) {
      chip = novo("details", "ferramenta");
      chip.dataset.nome = ev.nome;
      chip.appendChild(novo("summary", "ferramenta__titulo"));
      bloco.ferramentas.appendChild(chip);
    }
    chip.dataset.rodando = ev.estado === "rodando" ? "1" : "0";
    chip.querySelector("summary").textContent = ev.nome + " · " + (ROTULOS[ev.estado] || ev.estado);
    if (ev.resultado) chip.appendChild(novo("pre", "ferramenta__resultado", ev.resultado));
    rolar();
  }

  function comparacao(cartao, rotulo, texto) {
    cartao.appendChild(novo("p", "aprovacao__rotulo", rotulo));
    cartao.appendChild(novo("pre", "aprovacao__texto", texto));
  }

  function mostrarAprovacao(ev) {
    var cartao = novo("div", "aprovacao");
    cartao.appendChild(novo("p", "aprovacao__titulo", "Alfred quer executar " + ev.nome));
    if (ev.nome === "documento_substituir") {
      comparacao(cartao, "Trecho atual", ev.argumentos.trecho_original || "");
      comparacao(cartao, "Texto novo (entra como alteração rastreada)", ev.argumentos.novo_texto || "");
    } else {
      comparacao(cartao, "Argumentos", JSON.stringify(ev.argumentos, null, 2));
    }
    var acoes = novo("div", "aprovacao__acoes");
    var sim = novo("button", "botao botao--primario botao--pequeno", "Executar");
    var nao = novo("button", "botao botao--discreto botao--pequeno", "Não");
    sim.type = nao.type = "button";
    function decidir(decisao, rotulo) {
      sim.disabled = nao.disabled = true;
      postar("/painel/api/escrita/aprovacoes/" + encodeURIComponent(ev.id) + "/" + decisao)
        .then(function () { acoes.replaceChildren(novo("span", "aprovacao__decisao", rotulo)); })
        .catch(function (e) { acoes.replaceChildren(novo("span", "escrita__erro", e.message)); });
    }
    sim.addEventListener("click", function () { decidir("executar", "Autorizado"); });
    nao.addEventListener("click", function () { decidir("negar", "Negado"); });
    acoes.appendChild(sim);
    acoes.appendChild(nao);
    cartao.appendChild(acoes);
    bloco.ferramentas.appendChild(cartao);
    rolar();
  }

  function tratar(ev) {
    if (ev.tipo === "texto") {
      bruto += ev.texto;
      bloco.corpo.textContent = bruto;
      rolar();
    } else if (ev.tipo === "ferramenta") {
      mostrarFerramenta(ev);
    } else if (ev.tipo === "aprovar") {
      mostrarAprovacao(ev);
    } else if (ev.tipo === "fim") {
      bloco.corpo.classList.remove("resposta__corpo--stream");
      bloco.corpo.innerHTML = ev.html || "";
      if (ev.parado) bloco.appendChild(novo("p", "resposta__nota", "Interrompido."));
      encerrar();
      rolar();
    } else if (ev.tipo === "erro") {
      mostrarErro(ev.erro);
      encerrar();
    }
  }

  function ouvir(id) {
    abrirBloco();
    fonte = new EventSource("/painel/api/escrita/turnos/" + encodeURIComponent(id) + "/eventos");
    fonte.onmessage = function (m) { tratar(JSON.parse(m.data)); };
    fonte.onerror = function () {
      if (fonte && fonte.readyState === EventSource.CLOSED) {
        mostrarErro("A conexão com o Alfred caiu.");
        encerrar();
      }
    };
  }

  function enviar(ev) {
    ev.preventDefault();
    var texto = $("texto").value.trim();
    if (!texto || turno) return;
    ocupado(true);
    postar("/painel/api/escrita/mensagens", { texto: texto, alcance: alcance(), conversa: conversa })
      .then(function (r) {
        conversa = r.conversa;
        turno = r.turno;
        $("texto").value = "";
        adicionarPergunta(texto, r);
        ouvir(r.turno);
      })
      .catch(function (e) {
        mostrarErro(e.message);
        ocupado(false);
      });
  }

  // ── ligações ───────────────────────────────────────────────────────────

  $("formulario").addEventListener("submit", enviar);
  $("texto").addEventListener("keydown", function (ev) {
    if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing) enviar(ev);
  });
  $("parar").addEventListener("click", function () {
    if (turno) postar("/painel/api/escrita/turnos/" + encodeURIComponent(turno) + "/parar").catch(function () {});
  });
  $("nova").addEventListener("click", function () {
    conversa = null;
    $("mensagens").replaceChildren();
    $("texto").focus();
  });
  document.querySelectorAll('input[name="alcance"]').forEach(function (r) {
    r.addEventListener("change", atualizarEstado);
  });

  atualizarEstado();
  window.setInterval(atualizarEstado, INTERVALO_MS);
})();
