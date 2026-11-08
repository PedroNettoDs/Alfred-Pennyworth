/*
 * Aba Conversa (conversa.html): envia a mensagem, ouve o turno por SSE e desenha a resposta em stream, os chips
 * de ferramenta e o cartão de aprovação. Tudo que vem do modelo entra como texto (textContent); o único HTML é o
 * da resposta final, já escapado no servidor por painel._markdown_seguro. Apagar fica com o painel.js (data-acao).
 */
(function () {
  "use strict";

  var ROTULOS = {
    rodando: "executando…", ok: "feito", erro: "erro", negada: "negada", expirada: "expirada", cancelada: "cancelada"
  };

  var raiz = document.getElementById("chat");
  if (!raiz) return;

  var conversa = raiz.getAttribute("data-conversa") || null;
  var turno = null;     // id do turno em andamento
  var fonte = null;     // EventSource
  var bloco = null;     // .resposta do turno em andamento (bloco.ferramentas, bloco.corpo)
  var bruto = "";       // texto da resposta como chegou

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

  function irParaLogin() {
    location.assign("/painel/login?proximo=" + encodeURIComponent(location.pathname + location.search));
  }

  function responder(r) {
    if (r.status === 401) {
      irParaLogin();
      throw new Error("Sessão expirada.");
    }
    return r.json().then(function (d) {
      if (!r.ok) throw new Error(d.erro || "O Alfred recusou o pedido.");
      return d;
    }, function () {
      throw new Error(r.ok ? "Resposta inesperada do Alfred." : "O Alfred recusou o pedido (HTTP " + r.status + ").");
    });
  }

  function postar(url, corpo) {
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf() },
      body: JSON.stringify(corpo || {})
    }).then(responder, function () { throw new Error("Não foi possível falar com a API do Alfred."); });
  }

  function rolar() {
    var m = $("mensagens");
    m.scrollTop = m.scrollHeight;
  }

  // ── estado da tela ─────────────────────────────────────────────────────

  function ocupado(sim) {
    $("enviar").disabled = sim;
    $("parar").hidden = !sim;
  }

  function encerrar() {
    if (fonte) fonte.close();
    fonte = null;
    turno = null;
    bloco = null;
    ocupado(false);
    $("texto").focus();
  }

  function mostrarErro(texto) {
    $("mensagens").appendChild(novo("p", "chat__erro", texto));
    rolar();
  }

  // Primeira mensagem de uma conversa nova: a URL, o título, a lista e o botão Apagar passam a ser dela.
  function adotarConversa(r) {
    conversa = r.conversa;
    raiz.setAttribute("data-conversa", r.conversa);
    if (window.history && history.replaceState) history.replaceState(null, "", r.url);
    document.title = r.titulo + " · Alfred";
    var sub = document.querySelector(".tela-subtitulo");
    if (sub) sub.textContent = r.titulo;
    var titulo = document.querySelector("[data-titulo-conversa]");
    if (titulo) titulo.textContent = r.titulo;
    var apagar = $("apagar");
    if (apagar) {
      apagar.setAttribute("data-acao", "/painel/api/conversas/" + encodeURIComponent(r.conversa) + "/apagar");
      apagar.hidden = false;
    }
    var lista = $("conversas");
    var vazio = lista.querySelector(".conversas__vazio");
    if (vazio) vazio.remove();
    var li = novo("li", "conversas__item");
    li.setAttribute("aria-current", "page");
    var a = novo("a", "conversas__link");
    a.href = r.url;
    a.appendChild(novo("span", "conversas__titulo", r.titulo));
    a.appendChild(novo("time", "conversas__quando", "agora"));
    li.appendChild(a);
    lista.insertBefore(li, lista.firstChild);
  }

  // ── um turno ───────────────────────────────────────────────────────────

  function adicionarPergunta(texto) {
    var boas = raiz.querySelector(".chat__boas-vindas");
    if (boas) boas.remove();
    var artigo = novo("article", "turno");
    var p = novo("div", "pergunta");
    p.appendChild(novo("p", "pergunta__texto", texto));
    artigo.appendChild(p);
    $("mensagens").appendChild(artigo);
    return artigo;
  }

  function ligarBloco(el) {
    bruto = "";
    bloco = el;
    bloco.ferramentas = el.querySelector(".resposta__ferramentas");
    bloco.corpo = el.querySelector(".resposta__corpo");
  }

  function abrirBloco(artigo) {
    var el = novo("div", "resposta");
    el.appendChild(novo("div", "resposta__ferramentas"));
    el.appendChild(novo("div", "resposta__corpo resposta__corpo--stream", ""));
    artigo.appendChild(el);
    ligarBloco(el);
  }

  function secao(pai, rotulo, texto) {
    pai.appendChild(novo("p", "ferramenta__rotulo", rotulo));
    pai.appendChild(novo("pre", "ferramenta__codigo", texto));
  }

  function mostrarFerramenta(ev) {
    var chip = null;
    bloco.ferramentas.querySelectorAll('[data-estado="rodando"]').forEach(function (c) {
      if (c.getAttribute("data-nome") === ev.nome) chip = c;
    });
    if (!chip) {
      chip = novo("details", "ferramenta");
      chip.setAttribute("data-nome", ev.nome);
      chip.appendChild(novo("summary", "ferramenta__titulo"));
      secao(chip, "Argumentos", JSON.stringify(ev.argumentos || {}, null, 2));
      bloco.ferramentas.appendChild(chip);
    }
    chip.setAttribute("data-estado", ev.estado);
    chip.querySelector("summary").textContent = ev.nome + " · " + (ROTULOS[ev.estado] || ev.estado);
    if (ev.estado !== "rodando") secao(chip, "Resultado", ev.resultado || "");
    rolar();
  }

  function mostrarAprovacao(ev) {
    var cartao = novo("div", "aprovacao");
    cartao.appendChild(novo("p", "aprovacao__titulo", "Alfred quer executar " + ev.nome));
    cartao.appendChild(novo("p", "aprovacao__rotulo", "Argumentos"));
    cartao.appendChild(novo("pre", "aprovacao__texto", JSON.stringify(ev.argumentos || {}, null, 2)));
    var acoes = novo("div", "aprovacao__acoes");
    var sim = novo("button", "botao botao--primario botao--pequeno", "Executar");
    var nao = novo("button", "botao botao--pequeno", "Não");
    sim.type = nao.type = "button";
    function decidir(decisao, rotulo) {
      sim.disabled = nao.disabled = true;
      postar("/painel/api/aprovacoes/" + encodeURIComponent(ev.id) + "/" + decisao)
        .then(function () { acoes.replaceChildren(novo("span", "aprovacao__decisao", rotulo)); })
        .catch(function (e) { acoes.replaceChildren(novo("span", "chat__erro", e.message)); });
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
      if (ev.recarregar) {   // os eventos já tinham sido consumidos por outra página: o banco é a verdade
        location.reload();
        return;
      }
      bloco.corpo.classList.remove("resposta__corpo--stream");
      bloco.corpo.innerHTML = ev.html || "";
      if (ev.parado) bloco.appendChild(novo("p", "resposta__nota", "Interrompido."));
      encerrar();
      rolar();
    } else if (ev.tipo === "erro") {
      bloco.corpo.classList.remove("resposta__corpo--stream");
      mostrarErro(ev.erro);
      encerrar();
    }
  }

  function ouvir(id) {
    turno = id;
    ocupado(true);
    fonte = new EventSource("/painel/api/turnos/" + encodeURIComponent(id) + "/eventos");
    fonte.onmessage = function (m) { tratar(JSON.parse(m.data)); };
    fonte.onerror = function () {
      if (fonte && fonte.readyState === EventSource.CLOSED) {
        mostrarErro("A conexão com o Alfred caiu. Recarregue a página para ver a resposta gravada.");
        encerrar();
      }
    };
  }

  function enviar(ev) {
    ev.preventDefault();
    var campo = $("texto");
    var texto = campo.value.trim();
    if (!texto || turno) return;
    ocupado(true);
    var url = conversa ? "/painel/api/conversas/" + encodeURIComponent(conversa) + "/mensagens" : "/painel/api/conversas";
    postar(url, { texto: texto })
      .then(function (r) {
        campo.value = "";
        if (!conversa) adotarConversa(r);
        abrirBloco(adicionarPergunta(texto));
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
    if (turno) postar("/painel/api/turnos/" + encodeURIComponent(turno) + "/parar").catch(function () {});
  });

  // Página recarregada com um turno em andamento: religa o SSE no bloco que o servidor deixou vazio.
  var ativo = raiz.getAttribute("data-turno-ativo");
  var vivo = raiz.querySelector("[data-vivo]");
  if (ativo && vivo) {
    ligarBloco(vivo);
    ouvir(ativo);
  }
  rolar();
})();
