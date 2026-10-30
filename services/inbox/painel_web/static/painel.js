/*
 * Painel do Alfred — comportamento do cliente (sem dependências, sem build).
 *
 * Roda sob CSP "script-src 'self'": nada de JS inline nos templates. Todo o comportamento é ligado
 * por atributos data-* com delegação de eventos no document (o contrato está em
 * painel_web/COMPONENTES.md). O servidor é sempre a fonte da verdade: o JS só mostra sucesso depois
 * da resposta 200 e nunca decide sozinho que algo foi enviado.
 *
 * Partes:
 *   1. utilitários (CSRF, ícones, mensagens)
 *   2. cliente de ações  — form[data-acao] → POST JSON (+ <dialog> de confirmação opcional)
 *   3. polling (15 s)    — [data-poll-url], [data-contador], [data-estado-url]
 *   4. pequenos extras   — filtros que se enviam sozinhos (mouse/Enter), marcação de "texto sendo editado"
 */
(function () {
  "use strict";

  var INTERVALO_MS = 15000;
  var URL_LOGIN = "/painel/login";
  var URL_CONTADORES = "/painel/api/contadores";

  var MSG_REDE = "Não foi possível falar com a API do Alfred. Verifique se ela está no ar e tente de novo.";
  var MSG_SEM_CONEXAO = "Sem contato com a API do Alfred. As listas voltam a se atualizar sozinhas quando ela responder.";
  var MSG_SESSAO_EXPIRADA = "Sua sessão expirou. Copie o texto e entre de novo.";

  // ══════════════════════════════════════════════════════════════
  //  1. Utilitários
  // ══════════════════════════════════════════════════════════════

  function csrf() {
    var m = document.querySelector('meta[name="csrf-token"]');
    return m ? m.getAttribute("content") || "" : "";
  }

  // /painel/api/contadores devolve o X-CSRF-Token da sessão atual (muda quando Pedro entra de novo).
  function renovarCsrf(valor) {
    var m = document.querySelector('meta[name="csrf-token"]');
    if (m && typeof valor === "string" && /^[0-9a-f]{64}$/.test(valor)) m.setAttribute("content", valor);
  }

  // Login lembrando onde Pedro estava (só caminhos do próprio painel).
  function urlLogin() {
    var aqui = location.pathname + location.search;
    var destino = URL_LOGIN;
    if (aqui.indexOf("/painel") === 0 && aqui.indexOf(URL_LOGIN) !== 0) {
      destino += "?proximo=" + encodeURIComponent(aqui);
    }
    return destino;
  }

  // Há edição em andamento? Form de ação com data-estado (sujo, confirmando, ocupado, erro), campo com
  // texto diferente do original ou foco num campo de um form de ação.
  function temEdicao() {
    if (document.querySelector("form[data-acao][data-estado]")) return true;
    var ativo = document.activeElement;
    if (ativo && ativo.form && ativo.form.hasAttribute("data-acao") && /^(INPUT|TEXTAREA)$/.test(ativo.tagName)) return true;
    var alterado = false;
    document.querySelectorAll("form[data-acao] textarea, form[data-acao] input:not([type=hidden])").forEach(function (c) {
      if (typeof c.defaultValue === "string" && c.value !== c.defaultValue) alterado = true;
    });
    return alterado;
  }

  /*
   * Sessão expirada (401). Sem edição: volta ao login. Com edição: NÃO sai da página (o texto se perderia);
   * mostra um aviso fixo com link para o login em nova aba. Quando uma consulta volta a dar certo (Pedro
   * entrou de novo na outra aba), o aviso some sozinho e "Tentar de novo" funciona.
   */
  var indoParaLogin = false;
  function irParaLogin() {
    if (indoParaLogin) return;
    if (temEdicao()) {
      avisoSessao(true);
      return;
    }
    indoParaLogin = true;
    location.assign(urlLogin());
  }

  function avisoSessao(expirada) {
    var area = document.getElementById("avisos");
    if (!area) return;
    var atual = area.querySelector("[data-aviso-sessao]");
    if (expirada && !atual) {
      var m = criarMensagem("erro", MSG_SESSAO_EXPIRADA, {
        link: { url: urlLogin(), texto: "Entrar de novo", interno: true }
      });
      m.setAttribute("data-aviso-sessao", "");
      area.insertBefore(m, area.firstChild);
    } else if (!expirada && atual) {
      atual.remove();
    }
  }

  function foiParaLogin(resp) {
    return resp.status === 401 || (resp.redirected && resp.url.indexOf(URL_LOGIN) !== -1);
  }

  // Mesmos desenhos da macro icone() de _macros.html (só os que o JS cria).
  var ICONES = {
    sucesso:    ["M20 6 9 17l-5-5"],
    erro:       ["M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0z", "m15 9-6 6", "m9 9 6 6"],
    aguardando: ["M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0z", "M12 6v6l4 2"],
    info:       ["M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0z", "M12 16v-4", "M12 8h.01"],
    externo:    ["M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6", "M15 3h6v6", "M10 14 21 3"]
  };

  function icone(nome) {
    var ns = "http://www.w3.org/2000/svg";
    var svg = document.createElementNS(ns, "svg");
    svg.setAttribute("class", "icone");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    (ICONES[nome] || ICONES.info).forEach(function (d) {
      var p = document.createElementNS(ns, "path");
      p.setAttribute("d", d);
      svg.appendChild(p);
    });
    return svg;
  }

  function botaoSimples(texto, classe) {
    var b = document.createElement("button");
    b.type = "button";
    b.className = "botao " + (classe || "botao--pequeno");
    b.textContent = texto;
    return b;
  }

  /*
   * Monta uma mensagem inline (.mensagem--sucesso | --erro | --aguardando).
   * Sempre texto (textContent: nada vindo do servidor vira HTML) + ícone; erro ganha role=alert.
   * extras: {link: {url, texto}, tentar: fn, dispensar: bool}
   */
  function criarMensagem(tom, texto, extras) {
    extras = extras || {};
    var div = document.createElement("div");
    div.className = "mensagem mensagem--" + tom;
    div.setAttribute("role", tom === "erro" ? "alert" : "status");
    div.appendChild(icone(tom === "erro" ? "erro" : tom === "sucesso" ? "sucesso" : "aguardando"));

    var p = document.createElement("p");
    p.className = "mensagem__texto";
    p.textContent = texto;
    if (extras.link) {
      p.appendChild(document.createTextNode(" "));
      var a = document.createElement("a");
      a.className = "link-externo";
      a.href = extras.link.url;
      a.target = "_blank";
      // interno (login do próprio painel): sem noreferrer, como qualquer link do painel
      a.rel = extras.link.interno ? "noopener" : "noopener noreferrer";
      a.textContent = extras.link.texto;
      a.appendChild(icone("externo"));
      var aviso = document.createElement("span");
      aviso.className = "sr-only";
      aviso.textContent = " (abre em nova aba)";
      a.appendChild(aviso);
      p.appendChild(a);
    }
    div.appendChild(p);

    if (extras.tentar || extras.dispensar) {
      var acoes = document.createElement("div");
      acoes.className = "acoes";
      if (extras.tentar) {
        var t = botaoSimples("Tentar de novo", "botao--pequeno");
        t.setAttribute("data-tentar-de-novo", "");
        t.addEventListener("click", extras.tentar);
        acoes.appendChild(t);
      }
      if (extras.dispensar) {
        var d = botaoSimples("Dispensar", "botao--pequeno botao--discreto");
        d.addEventListener("click", function () {
          if (typeof extras.dispensar === "function") extras.dispensar();
          div.remove();
        });
        acoes.appendChild(d);
      }
      div.appendChild(acoes);
    }
    return div;
  }

  // Área global de avisos (#avisos, aria-live no base.html). Guarda no máximo 3 mensagens.
  function avisoGlobal(tom, texto, extras) {
    var area = document.getElementById("avisos");
    if (!area) return null;
    extras = extras || {};
    if (extras.dispensar === undefined) extras.dispensar = true;
    var m = criarMensagem(tom, texto, extras);
    area.appendChild(m);
    while (area.querySelectorAll(".mensagem:not([data-aviso-conexao])").length > 3) {
      area.querySelector(".mensagem:not([data-aviso-conexao])").remove();
    }
    return m;
  }

  // Aviso único de "sem conexão" do polling (some sozinho quando a API volta a responder).
  function avisoConexao(semConexao) {
    var area = document.getElementById("avisos");
    if (!area) return;
    var atual = area.querySelector("[data-aviso-conexao]");
    if (semConexao && !atual) {
      var m = criarMensagem("aguardando", MSG_SEM_CONEXAO);
      m.setAttribute("data-aviso-conexao", "");
      area.insertBefore(m, area.firstChild);
    } else if (!semConexao && atual) {
      atual.remove();
    }
  }

  // Link do Google Agenda só se for mesmo do Google Agenda (o resto vira texto).
  function linkAgenda(url) {
    if (typeof url === "string" && url.indexOf("https://calendar.google.com/") === 0) {
      return { url: url, texto: "Abrir no Google Agenda" };
    }
    return null;
  }

  // ══════════════════════════════════════════════════════════════
  //  2. Cliente de ações
  // ══════════════════════════════════════════════════════════════

  /*
   * POST JSON com X-CSRF-Token. Nunca lança: devolve {ok, status, dados, rede}.
   * 401 (ou redirecionado ao login) → falha comum (texto mantido, "Tentar de novo") + aviso de sessão
   * expirada; a página não navega sozinha no meio de uma ação.
   */
  function postJSON(url, corpo) {
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
      headers: {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-CSRF-Token": csrf()
      },
      body: JSON.stringify(corpo || {})
    }).then(function (resp) {
      if (foiParaLogin(resp)) {
        avisoSessao(true);
        return { ok: false, status: 401, dados: {}, sessao: true };
      }
      avisoSessao(false);
      var tipo = resp.headers.get("content-type") || "";
      var leitura = tipo.indexOf("application/json") !== -1
        ? resp.json().catch(function () { return {}; })
        : Promise.resolve({});
      return leitura.then(function (dados) {
        return { ok: resp.ok, status: resp.status, dados: dados || {} };
      });
    }, function () {
      return { ok: false, status: 0, dados: {}, rede: true };
    });
  }

  function textoDeErro(r) {
    if (r.rede) return MSG_REDE;
    if (r.status === 401) return "Sessão expirada: nada foi feito. Entre de novo e tente outra vez.";
    if (r.dados && typeof r.dados.erro === "string" && r.dados.erro) return r.dados.erro;
    if (r.status === 403) return "O servidor recusou a ação por segurança. Recarregue a página e tente de novo.";
    if (r.status === 404) return "Item não encontrado. Ele pode ter sido removido.";
    return "A ação não foi concluída (HTTP " + r.status + ").";
  }

  // Escopo de travamento: [data-acoes] agrupa formulários que não podem rodar ao mesmo tempo
  // (ex.: "Autorizar envio", "Não enviar" e "Agendar" da mesma pendência). Sem ele, o próprio form.
  function escopoDe(form) {
    return form.closest("[data-acoes]") || form;
  }

  function campos(raiz) {
    return raiz.querySelectorAll("input:not([type=hidden]), textarea, select");
  }

  // Durante a requisição: botões desabilitados, campos só leitura (o texto continua lá e
  // continua sendo enviado — campo disabled sairia do FormData), botão clicado com aria-busy.
  function ocupar(escopo, botao, sim) {
    if (sim) {
      escopo.setAttribute("data-ocupado", "");
      escopo.setAttribute("aria-busy", "true");
    } else {
      escopo.removeAttribute("data-ocupado");
      escopo.removeAttribute("aria-busy");
    }
    escopo.querySelectorAll("button").forEach(function (b) {
      if (sim) {
        if (b.disabled) b.setAttribute("data-ja-desabilitado", "");
        b.disabled = true;
      } else if (!escopo.hasAttribute("data-encerrado")) {
        if (b.hasAttribute("data-ja-desabilitado")) b.removeAttribute("data-ja-desabilitado");
        else b.disabled = false;
      }
    });
    campos(escopo).forEach(function (c) {
      if (c.tagName === "SELECT") return;
      if (sim) {
        if (c.readOnly) c.setAttribute("data-ja-somente-leitura", "");
        c.readOnly = true;
      } else if (!escopo.hasAttribute("data-encerrado")) {
        if (c.hasAttribute("data-ja-somente-leitura")) c.removeAttribute("data-ja-somente-leitura");
        else c.readOnly = false;
      }
    });
    if (botao) {
      if (sim) {
        botao.setAttribute("aria-busy", "true");
        var ocupado = botao.getAttribute("data-texto-ocupado");
        if (ocupado) {
          botao.setAttribute("data-texto-original", botao.textContent);
          botao.textContent = ocupado;
        }
      } else {
        botao.removeAttribute("aria-busy");
        if (botao.hasAttribute("data-texto-original")) {
          botao.textContent = botao.getAttribute("data-texto-original");
          botao.removeAttribute("data-texto-original");
        }
      }
    }
  }

  // Ação concluída ou impossível (pendência resolvida): desabilita tudo, sem apagar o texto.
  function encerrar(escopo) {
    escopo.setAttribute("data-encerrado", "");
    escopo.querySelectorAll("button").forEach(function (b) { b.disabled = true; });
    campos(escopo).forEach(function (c) {
      if (c.tagName === "SELECT") c.disabled = true;
      else c.readOnly = true;
    });
    escopo.querySelectorAll("form[data-acao]").forEach(function (f) { f.removeAttribute("data-estado"); });
    if (escopo.matches("form[data-acao]")) escopo.removeAttribute("data-estado");
  }

  // Onde mostrar o resultado de um formulário: data-resultado="<id>" → [data-resultado] dentro → cria um.
  function areaResultado(form) {
    var id = form.getAttribute("data-resultado");
    var area = id ? document.getElementById(id) : null;
    if (!area) area = form.querySelector("[data-resultado]");
    if (!area) {
      area = document.createElement("div");
      area.setAttribute("data-resultado", "");
      area.setAttribute("aria-live", "polite");
      form.appendChild(area);
    }
    return area;
  }

  function mostrarResultado(form, elemento) {
    var area = areaResultado(form);
    area.textContent = "";
    if (elemento) area.appendChild(elemento);
  }

  // Corpo JSON: campos com name do formulário + name/value do botão clicado (ex.: resposta=vou).
  function corpoDe(form, botao) {
    var dados = {};
    if (!(botao && botao.hasAttribute("data-sem-campos"))) {
      new FormData(form).forEach(function (valor, nome) {
        if (typeof valor === "string") dados[nome] = valor;
      });
    }
    if (botao && botao.name) dados[botao.name] = botao.value;
    return dados;
  }

  function atributo(form, botao, nome) {
    return (botao && botao.getAttribute(nome)) || form.getAttribute(nome) || "";
  }

  // Executa a ação de verdade (já confirmada, quando exigia confirmação).
  function executar(form, botao) {
    var escopo = escopoDe(form);
    if (escopo.hasAttribute("data-ocupado") || escopo.hasAttribute("data-encerrado")) {
      return Promise.resolve();
    }
    var url = atributo(form, botao, "data-acao");
    if (!url) return Promise.resolve();

    // Trava ANTES de qualquer await: o segundo clique encontra o escopo ocupado e não faz nada.
    ocupar(escopo, botao, true);
    form.setAttribute("data-estado", "ocupado");
    mostrarResultado(form, null);

    return postJSON(url, corpoDe(form, botao)).then(function (r) {
      ocupar(escopo, botao, false);
      if (r.ok && r.dados.ok !== false) sucesso(form, botao, r.dados);
      else falha(form, botao, r);
    });
  }

  function sucesso(form, botao, dados) {
    var apos = atributo(form, botao, "data-apos-sucesso") || "manter";
    if (apos.indexOf("ir:") === 0) {
      location.assign(apos.slice(3));
      return;
    }
    if (apos === "recarregar") {
      location.reload();
      return;
    }

    var texto = (dados && typeof dados.mensagem === "string" && dados.mensagem) || "Feito.";
    var extras = { link: linkAgenda(dados && dados.google_url) };
    var escopo = escopoDe(form);
    form.removeAttribute("data-estado");
    limparInvalidos(form);

    if (apos === "encerrar") {
      encerrar(escopo);
      // Esta página resolveu a pendência: o aviso "já resolvida" do polling não deve aparecer.
      var det = form.closest("[data-estado-url]");
      if (det) det.setAttribute("data-estado-parado", "");
    }

    var cont = form.closest("[data-poll-url]");
    if (cont) {
      // A lista vai ser redesenhada pelo servidor: o resultado vai para a área global.
      avisoGlobal("sucesso", texto, extras);
      atualizarConteiner(cont, true);
    } else {
      mostrarResultado(form, criarMensagem("sucesso", texto, extras));
    }
    atualizarContadores();
  }

  function falha(form, botao, r) {
    var texto = textoDeErro(r);
    var escopo = escopoDe(form);
    var cont = form.closest("[data-poll-url]");

    // 409: resolvida por outro caminho (ou lote/item já resolvido). Nada foi feito; ações saem do ar.
    if (r.status === 409) {
      form.removeAttribute("data-estado");
      encerrar(escopo);
      var det = form.closest("[data-estado-url]");
      if (det && mostrarResolvida(det, texto)) {
        mostrarResultado(form, null);
      } else if (cont) {
        avisoGlobal("aguardando", texto);
        atualizarConteiner(cont, true);
      } else {
        mostrarResultado(form, criarMensagem("aguardando", texto));
      }
      atualizarContadores();
      return;
    }

    // Demais erros: o texto digitado fica, o formulário continua "em erro" (o polling não o
    // redesenha) e há "Tentar de novo". 400/404 não ganham o botão: repetir igual não adianta.
    form.setAttribute("data-estado", "erro");
    marcarInvalido(form, r.dados && r.dados.campo);
    var podeRepetir = !(r.status === 400 || r.status === 404) || (r.dados && r.dados.pode_tentar_de_novo);
    var extras = {};
    if (podeRepetir) {
      extras.tentar = function () { iniciar(form, botao); };
    }
    if (cont) {
      // Dispensar devolve o contêiner ao polling.
      extras.dispensar = function () { form.removeAttribute("data-estado"); };
    }
    mostrarResultado(form, criarMensagem("erro", texto, extras));
  }

  function marcarInvalido(form, nome) {
    if (!nome || !form.elements[nome]) return;
    var c = form.elements[nome];
    if (c.setAttribute) {
      c.setAttribute("aria-invalid", "true");
      c.focus();
    }
  }

  function limparInvalidos(form) {
    form.querySelectorAll('[aria-invalid="true"]').forEach(function (c) { c.removeAttribute("aria-invalid"); });
  }

  // ── Confirmação em <dialog> ──────────────────────────────────

  var pendente = null;   // {form, botao, dialogo} enquanto o diálogo está aberto

  // Ponto de entrada de uma ação: confirma antes quando o form/botão pede (data-confirmar="<id>").
  function iniciar(form, botao) {
    var escopo = escopoDe(form);
    if (escopo.hasAttribute("data-ocupado") || escopo.hasAttribute("data-encerrado")) return;
    var id = atributo(form, botao, "data-confirmar");
    if (id) abrirConfirmacao(form, botao, id);
    else executar(form, botao);
  }

  function abrirConfirmacao(form, botao, id) {
    var dlg = document.getElementById(id);
    if (!dlg || typeof dlg.showModal !== "function") {
      // Sem <dialog> (navegador antigo): ainda assim exige o segundo passo.
      var msg = atributo(form, botao, "data-dialogo-mensagem") || "Confirma a ação?";
      if (window.confirm(msg)) executar(form, botao);
      return;
    }
    // Preenche o diálogo com o estado ATUAL do formulário (texto final, quantidade etc.).
    dlg.querySelectorAll("[data-preencher-campo]").forEach(function (el) {
      var c = form.elements[el.getAttribute("data-preencher-campo")];
      el.textContent = c && typeof c.value === "string" ? c.value : "";
    });
    dlg.querySelectorAll("[data-preencher]").forEach(function (el) {
      el.textContent = atributo(form, botao, "data-dialogo-" + el.getAttribute("data-preencher"));
    });
    dlg.querySelectorAll("[data-confirmar-ok]").forEach(function (b) { b.disabled = false; });

    // estado anterior (null, "sujo", "erro"): Cancelar/Esc devolve exatamente a ele
    pendente = { form: form, botao: botao, dialogo: dlg, estadoAnterior: form.getAttribute("data-estado") };
    form.setAttribute("data-estado", "confirmando");
    dlg.showModal();
    // Foco inicial no "Cancelar" (seguro por padrão), salvo se o diálogo marcar outro com autofocus.
    var alvo = dlg.querySelector("[autofocus]") || dlg.querySelector("[data-fechar-dialogo]");
    if (alvo) alvo.focus();
  }

  function confirmar(botaoOk) {
    var dlg = botaoOk.closest("dialog");
    if (!pendente || pendente.dialogo !== dlg) return;
    var p = pendente;
    pendente = null;              // segundo clique em "Confirmar" não acha nada pendente
    botaoOk.disabled = true;
    dlg.close();
    p.form.removeAttribute("data-estado");
    executar(p.form, p.botao);
  }

  // Fechou sem confirmar (Cancelar, Esc): volta ao estado anterior, nada é enviado. Sem estado antes
  // (ex.: "Aprovar" da Limpeza sem nota), o form sai sem data-estado e o contêiner volta ao polling.
  function aoFecharDialogo(dlg) {
    if (!pendente || pendente.dialogo !== dlg) return;
    var form = pendente.form;
    var anterior = pendente.estadoAnterior;
    pendente = null;
    if (form.getAttribute("data-estado") !== "confirmando") return;
    if (anterior) form.setAttribute("data-estado", anterior);
    else form.removeAttribute("data-estado");
  }

  // ══════════════════════════════════════════════════════════════
  //  3. Polling
  // ══════════════════════════════════════════════════════════════

  // Um contêiner fica como está quando: marcado como pausado, há formulário dentro sendo
  // editado/enviado/em erro, há diálogo aberto ou (fora de atualização forçada) o foco está nele.
  function pausado(cont, ignorarFoco) {
    if (cont.hasAttribute("data-poll-pausado")) return true;
    if (cont.querySelector("form[data-estado]")) return true;
    if (document.querySelector("dialog[open]")) return true;
    if (!ignorarFoco) {
      var ativo = document.activeElement;
      if (ativo && ativo !== document.body && ativo !== cont && cont.contains(ativo)) return true;
    }
    return false;
  }

  // Guarda o estado de <details> abertos (precisam de id) para reabrir após trocar o HTML.
  function detalhesAbertos(cont) {
    var ids = [];
    cont.querySelectorAll("details[id][open]").forEach(function (d) { ids.push(d.id); });
    return ids;
  }

  function atualizarConteiner(cont, forcar) {
    if (pausado(cont, !!forcar) || cont.hasAttribute("data-poll-carregando")) return Promise.resolve(true);
    var url = cont.getAttribute("data-poll-url");
    if (!url) return Promise.resolve(true);
    cont.setAttribute("data-poll-carregando", "");

    return fetch(url, {
      credentials: "same-origin",
      cache: "no-store",
      headers: { "Accept": "text/html" }
    }).then(function (resp) {
      if (foiParaLogin(resp)) { irParaLogin(); return true; }
      if (!resp.ok) return false;
      avisoSessao(false);
      return resp.text().then(function (html) {
        // O estado pode ter mudado enquanto baixava (Pedro começou a digitar): confere de novo.
        if (pausado(cont, !!forcar)) return true;
        var abertos = detalhesAbertos(cont);
        var tinhaFoco = cont.contains(document.activeElement) && document.activeElement !== cont;
        var rolagem = window.scrollY;
        cont.innerHTML = html;
        abertos.forEach(function (id) {
          var d = document.getElementById(id);
          if (d && d.tagName === "DETAILS" && cont.contains(d)) d.open = true;
        });
        if (tinhaFoco) {
          // O elemento focado sumiu com a troca: o foco fica no contêiner, não volta ao topo da página.
          if (!cont.hasAttribute("tabindex")) cont.setAttribute("tabindex", "-1");
          cont.focus({ preventScroll: true });
        }
        window.scrollTo(window.scrollX, rolagem);
        cont.dispatchEvent(new CustomEvent("painel:atualizado", { bubbles: true }));
        return true;
      });
    }, function () {
      return false;
    }).then(function (ok) {
      cont.removeAttribute("data-poll-carregando");
      return ok;
    });
  }

  function getJSON(url) {
    return fetch(url, {
      credentials: "same-origin",
      cache: "no-store",
      headers: { "Accept": "application/json" }
    }).then(function (resp) {
      if (foiParaLogin(resp)) { irParaLogin(); return null; }
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      avisoSessao(false);
      renovarCsrf(resp.headers.get("X-CSRF-Token"));
      return resp.json();
    });
  }

  // Contadores: [data-contador="<chave>"] recebe o número; o ancestral [data-contador-ocultar-zero]
  // (se houver) some quando o valor é 0.
  function atualizarContadores() {
    var els = document.querySelectorAll("[data-contador]");
    if (!els.length) return Promise.resolve(true);
    return getJSON(URL_CONTADORES).then(function (dados) {
      if (!dados) return true;
      els.forEach(function (el) {
        var v = dados[el.getAttribute("data-contador")];
        if (typeof v !== "number") return;
        el.textContent = String(v);
        var caixa = el.closest("[data-contador-ocultar-zero]");
        if (caixa) caixa.hidden = v === 0;
      });
      return true;
    }, function () { return false; });
  }

  // Banner "Esta pendência já foi resolvida por <caminho>." + ações desabilitadas (texto mantido).
  function mostrarResolvida(det, texto) {
    var banner = det.querySelector("[data-banner-resolvida]");
    if (!banner) return false;
    var alvo = banner.querySelector("[data-banner-texto]") || banner;
    alvo.textContent = texto;
    banner.hidden = false;
    det.setAttribute("data-estado-parado", "");
    det.querySelectorAll("[data-acoes]").forEach(encerrar);
    // Se a confirmação de envio estava aberta, fecha: não há mais o que confirmar.
    if (pendente && det.contains(pendente.form)) {
      var dlg = pendente.dialogo;
      pendente = null;
      if (dlg.open) dlg.close();
    }
    return true;
  }

  function consultarEstado() {
    var det = document.querySelector("[data-estado-url]");
    if (!det) return Promise.resolve(true);
    // Com uma ação do próprio painel em andamento, o servidor já marca a pendência antes de enviar:
    // consultar agora mostraria "já resolvida" para o envio que o próprio Pedro acabou de autorizar.
    var ocupada = function () { return det.hasAttribute("data-estado-parado") || !!det.querySelector("[data-ocupado]"); };
    if (ocupada()) return Promise.resolve(true);
    return getJSON(det.getAttribute("data-estado-url")).then(function (dados) {
      if (!dados || ocupada()) return true;
      var resolvida = dados.resolvida === true || (typeof dados.status === "string" && dados.status !== "aguardando");
      if (resolvida) {
        var texto = (typeof dados.mensagem === "string" && dados.mensagem)
          || "Esta pendência já foi resolvida por " + (dados.caminho || "outro caminho") + ".";
        mostrarResolvida(det, texto);
      }
      return true;
    }, function () { return false; });
  }

  var rodando = false;
  function rodada() {
    if (document.hidden || rodando || indoParaLogin) return;
    rodando = true;
    var tarefas = [atualizarContadores(), consultarEstado()];
    document.querySelectorAll("[data-poll-url]").forEach(function (c) {
      tarefas.push(atualizarConteiner(c, false));
    });
    Promise.all(tarefas).then(function (res) {
      avisoConexao(res.indexOf(false) !== -1);
    }).then(function () { rodando = false; }, function () { rodando = false; });
  }

  function temAlgoParaAtualizar() {
    return !!document.querySelector("[data-poll-url], [data-contador], [data-estado-url]");
  }

  // ══════════════════════════════════════════════════════════════
  //  4. Eventos (delegação)
  // ══════════════════════════════════════════════════════════════

  // Navegadores sem SubmitEvent.submitter: lembra o último botão clicado.
  var ultimoBotao = null;

  document.addEventListener("click", function (ev) {
    var alvo = ev.target instanceof Element ? ev.target : null;
    if (!alvo) return;

    var envio = alvo.closest("button[type=submit], button:not([type]), input[type=submit]");
    if (envio && envio.form) ultimoBotao = envio;

    var ok = alvo.closest("[data-confirmar-ok]");
    if (ok) {
      ev.preventDefault();
      confirmar(ok);
      return;
    }
    var fechar = alvo.closest("[data-fechar-dialogo]");
    if (fechar) {
      ev.preventDefault();
      var dlg = fechar.closest("dialog");
      if (dlg && dlg.open) dlg.close();
    }
  });

  document.addEventListener("submit", function (ev) {
    var form = ev.target;
    if (!(form instanceof HTMLFormElement) || !form.hasAttribute("data-acao")) return;
    ev.preventDefault();   // nunca deixa o navegador postar o form (não teria o X-CSRF-Token)
    var botao = ev.submitter || (ultimoBotao && ultimoBotao.form === form ? ultimoBotao : null);
    ultimoBotao = null;
    if (botao && botao.disabled) return;
    iniciar(form, botao);
  });

  // "close" não borbulha: escuta na captura.
  document.addEventListener("close", function (ev) {
    if (ev.target instanceof HTMLElement && ev.target.tagName === "DIALOG") aoFecharDialogo(ev.target);
  }, true);

  // Texto sendo editado num form de ação: marca "sujo" para o polling não redesenhar o contêiner.
  document.addEventListener("input", function (ev) {
    var form = ev.target && ev.target.form;
    if (form && form.hasAttribute("data-acao") && !form.hasAttribute("data-estado")) {
      form.setAttribute("data-estado", "sujo");
    }
    if (ev.target && ev.target.getAttribute && ev.target.getAttribute("aria-invalid") === "true") {
      ev.target.removeAttribute("aria-invalid");
    }
  });

  /*
   * Filtros GET: <select data-enviar-ao-mudar> envia o próprio formulário quando a opção é escolhida com o
   * mouse (ou toque). Pelo teclado as setas só mudam o valor — recarregar a cada seta tiraria o foco do
   * campo (WCAG 3.2.2) — e Enter (ou o botão "Buscar") aplica.
   */
  function enviarFiltro(form) {
    if (typeof form.requestSubmit === "function") form.requestSubmit();
    else form.submit();
  }

  function eFiltroAutomatico(el) {
    return el instanceof HTMLSelectElement && el.hasAttribute("data-enviar-ao-mudar") && !!el.form;
  }

  document.addEventListener("pointerdown", function (ev) {
    if (eFiltroAutomatico(ev.target)) ev.target.setAttribute("data-via-ponteiro", "");
  }, true);

  document.addEventListener("keydown", function (ev) {
    var el = ev.target;
    if (!eFiltroAutomatico(el)) return;
    el.removeAttribute("data-via-ponteiro");
    if (ev.key === "Enter") {
      // depois do evento: com a lista aberta, o Enter primeiro confirma a opção escolhida
      var form = el.form;
      window.setTimeout(function () { enviarFiltro(form); }, 0);
    }
  }, true);

  document.addEventListener("change", function (ev) {
    var el = ev.target;
    if (eFiltroAutomatico(el) && el.hasAttribute("data-via-ponteiro")) {
      el.removeAttribute("data-via-ponteiro");
      enviarFiltro(el.form);
    }
  });

  // Aba oculta: nada roda (o intervalo checa document.hidden). Voltou: atualiza na hora.
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden && temAlgoParaAtualizar()) rodada();
  });

  function iniciarPolling() {
    if (temAlgoParaAtualizar()) window.setInterval(rodada, INTERVALO_MS);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", iniciarPolling);
  else iniciarPolling();
})();
