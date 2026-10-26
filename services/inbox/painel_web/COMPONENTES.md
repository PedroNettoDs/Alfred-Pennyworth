# Painel do Alfred — guia de componentes (contrato para os templates)

Guia objetivo para quem escreve `painel.py` e os templates de cada tela. A linguagem visual está em
`static/painel.css`, o comportamento do cliente em `static/painel.js` e o markup padronizado em
`templates/_macros.html`. As telas **só compõem** o que está aqui: nenhuma cor, fonte ou script novo.

## 1. Regras (o que os templates NÃO fazem)

- **Nada de JS inline**: sem `<script>` no template, sem `onclick=`/`onsubmit=`, sem `javascript:`.
  A CSP é `script-src 'self'`; todo comportamento vem de atributos `data-*` (seção 4).
- **Nada de estilo inline**: sem `style=` e sem `<style>` (`style-src 'self'`). Faltou um ajuste?
  Acrescente uma classe em `painel.css`, usando os tokens de `:root`.
- **Sem cores, fontes, imagens ou CDNs externos.** Ícones só pela macro `m.icone()`.
- **Sem `|safe` em dado vindo dos YAML** (contato, texto, resumo, assunto…). O autoescape cuida;
  o JS também só usa `textContent`. A exceção é o corpo do `.md` do e-mail depois de passar pelo
  conversor de markdown mínimo e seguro do `painel.py`.
- **Cor nunca sozinha**: verde/âmbar/vermelho sempre via `m.selo`, `m.aviso` ou `.mensagem` (têm ícone + texto).
- **Dourado só na ação principal** de cada bloco (`botao--primario`). "Não enviar", "Não vou",
  "Recusar" e "Desfazer" são secundários.
- **`<dialog>` fora dos contêineres de polling** (o polling troca o `innerHTML` do contêiner).
- O token nunca vai para template, URL ou JS. A página recebe só o `csrf`.

## 2. Arquivos e o `base.html`

| Arquivo | Servido em |
|---|---|
| `static/painel.css` | `/painel/static/painel.css` |
| `static/painel.js` | `/painel/static/painel.js` (com `defer`) |

`base.html` (todas as telas, menos o login, que o estende e troca o bloco `pagina`):

| Variável | Tipo | Uso |
|---|---|---|
| `csrf` | str | `<meta name="csrf-token">`, que o JS manda em `X-CSRF-Token` em todo POST |
| `ativo` | `"inicio"`, `"pendencias"`, `"agenda"`, `"limpeza"` ou `"sistema"` | `aria-current="page"` no item da navegação |
| `contadores` | dict (opcional) | mesmo JSON de `/painel/api/contadores`; valor inicial dos contadores da navegação (`aguardando`, `limpeza_aguardando`) |
| `simulado` | bool (opcional) | mostra o selo "Modo simulado" no rodapé da navegação |

Blocos: `titulo` (texto do `<title>`, que vira "X · Alfred"), `conteudo` (dentro de `<main id="conteudo">`),
`pagina` (o `<body>` inteiro, que só o login troca).
Já vêm prontos no base: "Pular para o conteúdo", navegação com contador, botão **Sair**, `<noscript>`
e a área global `#avisos` (`aria-live="polite"`), onde o JS põe o resultado de ações feitas em listas
que se atualizam sozinhas e o aviso de "sem contato com a API".

**Cada template importa as macros por conta própria**: `{% import "_macros.html" as m %}` (o import
do base não chega aos blocos dos filhos).

`login.html`: variáveis `erro` (ex.: `"Token inválido"`, `"SHELL_EXECUTOR_TOKEN não definido no .env"`)
e `proximo` (caminho `/painel…`, vai num `<input type="hidden" name="proximo">`; o servidor **valida**:
começa com `/painel` e não com `//`). O campo `token` é `type=password` e nunca recebe `value`.

## 3. Macros (`_macros.html`)

| Macro | Gera |
|---|---|
| `icone(nome, rotulo=none)` | SVG decorativo (`aria-hidden`). Nomes: `sucesso erro aguardando info alerta simulado ponto whatsapp email inicio pendencias agenda limpeza sistema sair externo voltar audio audio-off busca pessoa marca` |
| `selo(tom, texto, nome_icone=none)` | `.selo.selo--{sucesso,aguardando,simulado,erro,neutro}` com ícone + texto |
| `selo_status(status)` | selo de status de pendência, item de agenda ou lote (`aguardando`, `respondida`, `respondida_manual`, `vou`, `nao_vou`, `ignorada`, `expirada`, `pendente`, `no_calendario`, `em_execucao`, `desfazer`, `desfeito`, `cancelado`, `erro`, `aprovado`, `recusado`, `expirado`, `concluido`, `simulado`, `parcial`, `falhou`) |
| `selo_urgencia(u)` | `alta` → vermelho "Urgência alta"; `media`/`baixa` → neutro; vazio → nada |
| `selo_simulado()` | "Modo simulado: nada sairá da máquina" |
| `tipo(tipo)` | `span.linha__tipo` com ícone + "WhatsApp" ou "E-mail" (`evento`/`contato`) |
| `tempo(iso, texto, absoluto=none, classe="")` | `<time datetime title>`; recebe strings prontas (filtros `data`/`relativo` do painel.py) |
| `cabecalho(titulo, subtitulo=none, voltar=none)` | `header.tela-cabecalho` com `h1`; `voltar=(url, texto)`; `{% call %}` põe botões à direita |
| `vazio(texto, nome_icone="info", compacto=false)` | estado vazio |
| `botao(texto, variante="secundario", tipo="submit", attrs={}, nome_icone=none, pequeno=false, href=none)` | `.botao` (`primario`, `secundario`, `perigo`, `discreto`); `attrs` é um dict de atributos (`data-*`); atributo booleano: repita o nome como valor |
| `campo(nome, rotulo, valor, tipo, id, ajuda, erro, obrigatorio, opcional, attrs)` | `label` + `input.entrada` + ajuda/erro ligados por `aria-describedby`. **id padrão = `campo-<nome>`**: passe `id` quando houver mais de um campo com o mesmo nome na página |
| `area_texto(nome, rotulo, valor, id, ajuda, grande, linhas, obrigatorio, opcional, attrs)` | `textarea`; `grande=true` é a caixa da resposta proposta |
| `selecao(nome, rotulo, opcoes, valor, id, enviar_ao_mudar=false)` | `select`; `opcoes=[(valor, rótulo)]`; `enviar_ao_mudar` envia o form GET de filtros quando a escolha é feita com o mouse ou com Enter (setas não recarregam) |
| `aviso(tom, texto)` | banner `.aviso` (`role=alert` se erro, senão `status`) |
| `banner_resolvida(texto="", visivel=false)` | banner âmbar com `role=alert` "Esta pendência já foi resolvida por …" que o JS revela quando outro caminho resolve a pendência durante a edição. Página que **já abre** resolvida usa `aviso("neutro", …)` (`role=status`) |
| `resultado(id=none)` | `<div data-resultado aria-live>` vazio, onde o JS escreve sucesso/erro do form |
| `dialogo(id, titulo, texto_confirmar, variante="primario")` + `{% call %}` | `<dialog>` com Cancelar (`data-fechar-dialogo`) e confirmar (`data-confirmar-ok`) |
| `chave_valor(pares)` | `<dl class="chave-valor">`; pula valores vazios |
| `audio(url, estado)` | `estado`: `disponivel` (player), `expirado` ("Áudio expirado"), `sem` ("Sem áudio") |
| `comparacao(proposta, resposta)` | "O Alfred proporia" × "Você respondeu", lado a lado (empilha < 600 px) |
| `historico(eventos)` | linha do tempo; evento = `{texto, quando, iso?, tom?, detalhe?}`, `tom` ∈ `sucesso`/`aguardando`/`erro` |
| `item_sistema(nome, estado, detalhe=none, rotulo=none)` | `li.sistema-item`; `estado` ∈ `ok`, `aviso`, `simulado`, `indisponivel`, `erro`, `neutro`; `rotulo` troca o texto do selo (ex.: "Conectado") |

## 4. Contrato do `painel.js` (atributos `data-*`)

### 4.1 Ações (POST JSON)

| Atributo | Onde | Efeito |
|---|---|---|
| `data-acao="<url>"` | `<form>` | O JS intercepta o submit e faz `POST <url>` com JSON, `X-CSRF-Token` e `credentials: same-origin`. **Obrigatório no form** (pode ser `""` quando a URL vem do botão) |
| `data-acao="<url>"` | botão submit | Sobrepõe a URL do form (ex.: "Aprovar" e "Recusar" no mesmo form da nota) |
| `name` + `value` | botão submit | Entram no corpo (ex.: `name="resposta" value="vou"`) |
| `data-sem-campos` | botão submit | Corpo `{}` (ignora os campos do form) |
| `data-confirmar="<id do dialog>"` | form ou botão | Abre o `<dialog>` antes; só executa no `[data-confirmar-ok]` |
| `data-dialogo-<nome>="texto"` | form ou botão | Texto que vai para `[data-preencher="<nome>"]` do diálogo |
| `data-preencher-campo="<name>"` | dentro do dialog | Recebe o valor **atual** do campo `<name>` do form (texto final) |
| `data-apos-sucesso` | form ou botão | `manter` (padrão: form continua usável), `encerrar` (desabilita o escopo e mantém o texto), `recarregar`, `ir:/caminho` |
| `data-texto-ocupado="Enviando…"` | botão | Texto do botão durante a requisição |
| `data-acoes` | contêiner | **Escopo de trava**: enquanto um form dentro dele roda, todos os botões dele ficam desabilitados (duplo clique, "Autorizar" + "Não enviar" juntos). Sem ele, o escopo é o próprio form |
| `data-resultado` | dentro do form (ou `data-resultado="<id>"` no form) | Onde aparece a mensagem (macro `m.resultado()`). Sem ele, o JS cria um no fim do form |

Fluxo: submit → (diálogo) → escopo travado (`data-ocupado`, botões `disabled`, campos `readonly`,
botão `aria-busy="true"`) → resposta:

- **200** com `{ok: true, mensagem, google_url?}`: mostra `mensagem` (texto do servidor) e, se houver,
  um link "Abrir no Google Agenda" (`target=_blank rel="noopener noreferrer"`, aceito só quando começa com
  `https://calendar.google.com/`). Dentro de um contêiner de polling, a mensagem vai para `#avisos` e o
  contêiner é redesenhado na hora. Atualiza os contadores.
- **409** `{erro}`: nada foi feito. No detalhe, revela o `banner_resolvida` com o `erro`; fora dele, mostra
  o `erro`. Desabilita o escopo **sem apagar o texto**.
- **401** (ou redirecionamento para o login): tratado como as outras falhas (texto mantido, mensagem de
  erro, "Tentar de novo") e `#avisos` mostra o aviso fixo de sessão expirada (ver 4.2). Nunca navega
  sozinho com a ação em andamento.
- **Outros** (`0` = rede, `403`, `5xx`, `502`…): `.mensagem--erro` com `role=alert` e o texto de `erro`.
  O texto digitado fica e há **"Tentar de novo"**, que reabre a confirmação quando havia uma.
  `400` e `404` não ganham o botão (repetir igual não adianta), a menos que venha `pode_tentar_de_novo: true`.
  `{campo: "<name>"}` marca o campo com `aria-invalid` e põe o foco nele.

O sucesso só aparece depois da resposta 200. O JS nunca supõe envio.

**Respostas que o servidor deve dar**: JSON sempre (`Content-Type: application/json`), com
`erro` em português na voz do Alfred. `POST /painel/sair` responde `200 {ok: true}` (um 303 para o
login também funciona).

### 4.2 Polling (a cada 15 s; pausa com a aba oculta e roda na hora quando ela volta)

| Atributo | Efeito |
|---|---|
| `data-poll-url="/painel/parcial/<tela>?<mesmos filtros>"` | O contêiner tem o `innerHTML` trocado pelo fragmento HTML (200, `text/html`) dessa URL. O fragmento é **o miolo**, sem o próprio contêiner, e usa o mesmo template parcial que a página inclui |
| `data-poll-pausado` | Pausa aquele contêiner (o servidor ou o template podem marcar) |
| `<details id="…">` dentro do contêiner | Volta aberto depois da troca se estava aberto (**precisa de `id` estável**, ex.: `lote-<lid>-emails`) |
| `data-contador="<chave>"` | Recebe o número de `GET /painel/api/contadores` (`aguardando`, `whatsapp`, `email`, `limpeza_aguardando`) |
| `data-contador-ocultar-zero` | Ancestral do contador, escondido quando o valor é 0 |
| `data-estado-url="/painel/api/pendencias/<id>/estado"` | (detalhe) Consulta `{status, resolvida, caminho, mensagem}`. Se deixou de aguardar, revela o `[data-banner-resolvida]` com `mensagem` (ou "Esta pendência já foi resolvida por <caminho>.") e desabilita todos os `[data-acoes]` dentro dele, mantendo o texto |

Uma rodada **pula** um contêiner quando o foco está nele, quando ele tem um form com `data-estado`
(`sujo` = Pedro digitou, `confirmando`, `ocupado`, `erro`), quando há um `<dialog>` aberto ou quando
está marcado com `data-poll-pausado`. O polling nunca toca nada fora dos contêineres. Enquanto uma ação
do próprio painel está em andamento, a consulta de estado do detalhe espera, para não acusar como
"já resolvida" o envio que Pedro acabou de autorizar. Se a API não responde, `#avisos` mostra um aviso
único, que some quando ela volta.

Rotas do polling: `401` JSON → login (`/painel/login?proximo=<página atual>`), **salvo** quando há edição
em andamento — form `[data-acao]` com `data-estado`, campo com texto diferente do original ou foco num campo
de um form de ação. Nesse caso a página fica onde está e `#avisos` mostra um aviso fixo (`data-aviso-sessao`):
"Sua sessão expirou. Copie o texto e entre de novo." com link para o login em nova aba. Quando uma consulta
volta a dar 200 (Pedro entrou de novo na outra aba), o aviso some e "Tentar de novo" funciona:
`GET /painel/api/contadores` devolve o cabeçalho `X-CSRF-Token` da sessão atual e o JS atualiza o
`<meta name="csrf-token">` com ele.
Outro erro → mantém o conteúdo e mostra o aviso de conexão.

Fechar o diálogo de confirmação sem confirmar (Cancelar, Esc) devolve o form ao `data-estado` que ele tinha
antes de abrir (sem atributo, se não havia): só a digitação de verdade pausa o contêiner.

### 4.3 Outros

- `select[data-enviar-ao-mudar]` (macro `selecao(..., enviar_ao_mudar=true)`) envia o form de filtros (GET comum,
  sem `data-acao`) quando a opção é escolhida com o mouse, ou com Enter no campo. Pelo teclado, as setas só mudam
  o valor (sem recarregar a página: WCAG 3.2.2); Enter ou o botão "Buscar" aplicam.
- `[data-fechar-dialogo]` fecha o diálogo (Esc também). O foco inicial do diálogo vai para **Cancelar**,
  salvo um elemento com `autofocus`.
- Evento `painel:atualizado` (borbulha) é disparado no contêiner após cada troca.

## 5. Markup por tela

As URLs abaixo são as do PLANO; `{{ … }}` são exemplos de variáveis.

### Início (`/painel`, `ativo="inicio"`)

```jinja
{{ m.cabecalho("Início", data_de_hoje) }}
<div data-poll-url="/painel/parcial/inicio">{% include "_inicio.html" %}</div>
```
`_inicio.html`: `div.grade-cartoes` com blocos `a.cartao.cartao--link` (cada um leva à tela):
`p.cartao__rotulo` + conteúdo. Contagem: `div.metricas > div.metrica > span.metrica__valor + span.metrica__rotulo`.
Alfred: `m.selo("sucesso","Ligado")` ou `m.selo("erro","Desligado")` + `p.pequeno.suave` "desde …".
WhatsApp: `m.selo` (`sucesso` Conectado, `erro` Desconectado, `aguardando` Conectando, `simulado` Modo simulado,
`neutro` + ícone `alerta` Indisponível). Vários selos lado a lado ficam em `div.selos`.
As **5 mais recentes** não vão dentro de um `<a>` (seriam links aninhados): use `section.cartao` com
`div.cartao__cabecalho` (`h2.cartao__titulo` + `a` "Ver todas") e `ul.lista.lista--solta` com as linhas da lista.

### Pendências — lista (`/painel/pendencias`, `ativo="pendencias"`)

```jinja
{% set subtitulo %}<span data-contador="aguardando">3</span> aguardando{% endset %}   {# o número segue o polling #}
{{ m.cabecalho("Pendências", subtitulo) }}
<nav class="abas" aria-label="Abas de pendências">
  <a class="aba" href="?aba=aguardando" aria-current="page">Aguardando</a>
  <a class="aba" href="?aba=resolvidas">Resolvidas</a>
</nav>
<form class="filtros" method="get" action="/painel/pendencias" role="search">
  <input type="hidden" name="aba" value="{{ aba }}">
  {{ m.selecao("tipo", "Tipo", [("", "Todos"), ("whatsapp", "WhatsApp"), ("email", "E-mail")], tipo, enviar_ao_mudar=true) }}
  <div class="filtros__busca">{{ m.campo("q", "Buscar por contato ou texto", q, tipo="search") }}</div>
  {{ m.botao("Buscar", nome_icone="busca") }}
</form>
<div data-poll-url="/painel/parcial/pendencias?aba={{ aba }}&tipo={{ tipo }}&q={{ q|urlencode }}">
  {% include "_pendencias_lista.html" %}
</div>
```
Linha (dentro de `ul.lista`):
```jinja
<li class="linha">
  <a class="linha__link" href="/painel/pendencias/{{ p.id }}">
    {{ m.tipo(p.tipo) }}
    <span class="linha__contato">{{ contato }}</span>
    <span class="linha__resumo">{{ resumo }}</span>          {# 1 linha, reticências no CSS #}
    <span class="linha__urgencia">{{ m.selo_urgencia(urg) }}</span>   {# Resolvidas: m.selo_status(p.status) #}
    {{ m.tempo(iso, "há 12 min", "25/10/2026 14:03", "linha__tempo") }}  {# Resolvidas: data da resolução #}
  </a>
  {% if p.status == "respondida_manual" %}
  <div class="linha__extra">{{ m.comparacao(rascunho, p.resposta_texto) }}</div>
  {% endif %}
</li>
```
Os cinco filhos do link são sempre emitidos, mesmo vazios (`<span class="linha__urgencia"></span>`), porque
a grade depende da posição. Lista vazia: `m.vazio("Nenhuma pendência aguardando, senhor.", "pendencias")`
(Resolvidas: "Nenhuma pendência resolvida ainda."; busca sem resultado: "Nada encontrado para essa busca.").

### Detalhe — WhatsApp (`/painel/pendencias/{id}`)

```jinja
<div class="detalhe" data-estado-url="/painel/api/pendencias/{{ p.id }}/estado">  {# só se aguardando #}
  {{ m.cabecalho(contato, "Pendência de WhatsApp · recebida " ~ relativo, voltar=("/painel/pendencias", "Pendências")) }}
  {{ m.banner_resolvida() }}             {# já resolvida: m.aviso("neutro", "Esta pendência já foi resolvida por …") e sem forms #}
  <div class="detalhe__grade">
    <div class="detalhe__principal">
      <section class="cartao" aria-labelledby="c-msg">
        <div class="cartao__cabecalho"><h2 class="cartao__titulo" id="c-msg">Mensagem</h2>{{ m.selo_urgencia(urg) }}</div>
        <div class="detalhe__contato pequeno">{{ m.icone("whatsapp") }}<strong>{{ contato }}</strong><span class="detalhe__numero">{{ numero }}</span></div>
        <p class="cartao__rotulo">Transcrição do áudio</p>      {# ou "Mensagem" #}
        <p class="texto-original">{{ texto }}</p>
      </section>
      <section class="cartao">… h2 "Resumo do Alfred", <p>, m.chave_valor([("Categoria", …), ("Confiança", …), ("Urgência", …)])</section>
      <section class="cartao">… h2 "Áudio", m.audio("/pendencias/" ~ p.id ~ "/audio", estado)</section>
      <section class="cartao">… h2 "Histórico", m.historico(eventos)</section>
    </div>
    <div class="detalhe__lateral" data-acoes>
      <section class="cartao" aria-labelledby="c-resp">
        <h2 class="cartao__titulo" id="c-resp">Resposta proposta</h2>
        <form id="form-resposta" data-acao="/painel/api/pendencias/{{ p.id }}/responder-texto"
              data-confirmar="dialogo-envio" data-apos-sucesso="encerrar">
          {{ m.area_texto("texto", "Texto que será enviado a " ~ contato, rascunho, id="texto-resposta",
                          grande=true, obrigatorio=true, ajuda="Edite à vontade. Nada sai sem a sua confirmação.") }}
          <div class="acoes">{{ m.botao("Autorizar envio", "primario", attrs={"data-texto-ocupado": "Enviando…"}) }}</div>
          {{ m.resultado() }}
        </form>
        <form class="acoes--separadas" data-acao="/painel/api/pendencias/{{ p.id }}/ignorar" data-apos-sucesso="encerrar">
          {{ m.botao("Não enviar", attrs={"data-texto-ocupado": "Fechando…"}) }}
          {{ m.resultado() }}
        </form>
      </section>
      <section class="cartao" aria-labelledby="c-ag">
        <h2 class="cartao__titulo" id="c-ag">Agendar</h2>
        <form data-acao="/painel/api/pendencias/{{ p.id }}/agendar">       {# padrão "manter": continua aguardando #}
          {{ m.campo("quando", "Quando?", sugestao_quando, obrigatorio=true,
                     ajuda="Ex.: 20/10 14:00 · 20/10/2026 · amanhã 10h · hoje 18:00 · terça 15h · 16:00") }}
          {{ m.botao("Agendar") }}
          {{ m.resultado() }}
        </form>
      </section>
    </div>
  </div>
</div>
{% call m.dialogo("dialogo-envio", "Confirmar envio", "Confirmar envio") %}
  <p>Para: <strong>{{ contato }}</strong> <span class="detalhe__numero">{{ numero }}</span></p>
  <pre class="dialogo__texto" data-preencher-campo="texto"></pre>
  {% if simulado %}<p>{{ m.selo_simulado() }}</p>{% endif %}
{% endcall %}
```
O servidor responde `mensagem` = "Enviado para <contato>." ou "Simulado: nada saiu da máquina.".
Para "Não enviar", uma frase curta, por exemplo "Pendência fechada sem resposta.". Para "Agendar", a mensagem
mais `google_url`.

### Detalhe — e-mail

Mesma estrutura. Em `detalhe__principal`: cartão "Explicação" com `div.prosa` (markdown seguro), link do
evento (`a` normal), `m.audio(...)` e o histórico. Em `detalhe__lateral data-acoes`:
```jinja
<form data-acao="/painel/api/pendencias/{{ p.id }}/responder" data-apos-sucesso="encerrar">
  {{ m.area_texto("nota", "Nota", opcional=true, linhas=2) }}
  <div class="acoes">
    {{ m.botao("Vou", "primario", attrs={"name": "resposta", "value": "vou"}) }}
    {{ m.botao("Não vou", attrs={"name": "resposta", "value": "nao_vou"}) }}
  </div>
  {{ m.resultado() }}
</form>
```
O corpo enviado é `{nota, resposta}`.

### Agenda (`/painel/agenda`, `ativo="agenda"`)

```jinja
<div data-poll-url="/painel/parcial/agenda">{% include "_agenda_lista.html" %}</div>
```
Dentro do parcial: `h2.secao-titulo` "Próximos" / "Desfeitos e cancelados", cada grupo num `ul.lista`:
```jinja
<li class="agenda-item{% if inativo %} agenda-item--inativo{% endif %}">
  {{ m.tempo(iso, "26/10/2026 10:00", none, "agenda-item__quando") }}
  <div><p class="agenda-item__titulo">{{ i.titulo }}</p><span class="agenda-item__origem">WhatsApp · Ana</span></div>
  {{ m.selo_status(i.status) }}
  {% if pode_desfazer %}
  <form data-acao="/painel/api/agenda/{{ i.id }}/desfazer" data-apos-sucesso="encerrar">{{ m.botao("Desfazer", pequeno=true) }}</form>
  {% else %}<span></span>{% endif %}
</li>
```
Vazia: `m.vazio("A agenda está vazia.", "agenda")`.

### Limpeza (`/painel/limpeza`, `ativo="limpeza"`)

```jinja
<div class="pilha" data-poll-url="/painel/parcial/limpeza">{% include "_limpeza_lista.html" %}</div>
{% call m.dialogo("dialogo-limpeza", "Aprovar a limpeza?", "Mover para a Lixeira", "perigo") %}
  <p data-preencher="mensagem"></p>
{% endcall %}
```
Lote:
```jinja
<article class="cartao lote" aria-labelledby="lote-{{ l.id }}-titulo">
  <div class="lote__cabecalho"><h2 class="lote__titulo" id="lote-{{ l.id }}-titulo">{{ l.total }} e-mails · {{ l.inbox }}</h2>{{ m.selo_status(l.status) }}</div>
  <p class="lote__meta">Proposto em … · expira em … · Motivo: …</p>
  <details class="recolhivel" id="lote-{{ l.id }}-emails"><summary>Ver os {{ l.total }} e-mails</summary>
    <div class="recolhivel__corpo"><table class="tabela">
      <thead><tr><th scope="col">Remetente</th><th scope="col">Assunto</th><th scope="col">Data</th></tr></thead>
      <tbody>… <td class="tabela__data">20/10/2026 09:12</td> …</tbody></table></div>
  </details>
  {% if l.status == "aguardando" %}
  <form class="lote__form" data-acao="" data-acoes>
    {{ m.area_texto("nota", "Nota", id="nota-" ~ l.id, opcional=true, linhas=2) }}
    <div class="acoes">
      {{ m.botao("Aprovar", "primario", attrs={"data-acao": "/painel/api/limpeza/" ~ l.id ~ "/aprovar",
          "data-confirmar": "dialogo-limpeza", "data-apos-sucesso": "encerrar",
          "data-dialogo-mensagem": "Os " ~ l.total ~ " e-mails irão para a Lixeira da conta. Nada é apagado de forma permanente."}) }}
      {{ m.botao("Recusar", attrs={"data-acao": "/painel/api/limpeza/" ~ l.id ~ "/recusar", "data-apos-sucesso": "encerrar"}) }}
    </div>
    {{ m.resultado() }}
  </form>
  {% endif %}
</article>
```
Vazia: `m.vazio("Nenhum lote de limpeza, senhor.", "limpeza")`.

### Sistema (`/painel/sistema`, `ativo="sistema"`, só leitura)

```jinja
<div data-poll-url="/painel/parcial/sistema">
  <section class="cartao sistema-grupo" aria-labelledby="s-apis">
    <h2 class="cartao__titulo" id="s-apis">APIs locais</h2>
    <ul class="sistema-lista">
      {{ m.item_sistema("Inbox (7072)", "ok", "No ar") }}
      {{ m.item_sistema("Executor (7070)", "indisponivel", "Sem resposta em 2 s") }}
    </ul>
  </section>
  … grupos "Alfred", "WhatsApp" (rotulo="Conectado"/"Desconectado"/"Conectando"), "Ollama", "Containers", "Timers"
</div>
```
Item que falhou na consulta: `estado="indisponivel"` (selo neutro com ícone de alerta, texto "Indisponível").

## 6. Tokens e contraste (WCAG AA)

Tokens em `:root` de `painel.css`. Razões calculadas com a fórmula de luminância relativa do WCAG 2.x.
O mínimo é 4.5:1 para texto normal e 3:1 para texto grande, ícones, contornos de componentes e foco.

| Texto / elemento | Fundo | Razão | Mínimo |
|---|---|---|---|
| `--texto` #ece9e2 | `--fundo` #16181b | 14.67:1 | 4.5 |
| `--texto` | `--superficie` #1f2226 | 13.17:1 | 4.5 |
| `--texto` | `--superficie-2` #292d32 | 11.43:1 | 4.5 |
| `--texto` | `--neutro-fundo` #2c3036 | 10.94:1 | 4.5 |
| `--texto` (aviso/mensagem) | `--verde-fundo` #173322 | 11.30:1 | 4.5 |
| `--texto` (aviso/mensagem) | `--ambar-fundo` #3a2a12 | 11.40:1 | 4.5 |
| `--texto` (aviso/mensagem) | `--vermelho-fundo` #3d1b19 | 12.65:1 | 4.5 |
| `--texto-suave` #b1b5bb (rótulos, placeholder) | `--fundo` | 8.64:1 | 4.5 |
| `--texto-suave` | `--superficie` | 7.75:1 | 4.5 |
| `--texto-suave` (botão desabilitado) | `--superficie-2` | 6.73:1 | 4.5 |
| `--texto-suave` | `--neutro-fundo` | 6.44:1 | 4.5 |
| `--sobre-dourado` #16181b (botão primário) | `--dourado` #cfae67 | 8.39:1 | 4.5 |
| `--sobre-dourado` | `--dourado-hover` #dcbf7f | 10.00:1 | 4.5 |
| `--sobre-perigo` #ffffff (botão perigo) | `--perigo` #b8372d | 5.80:1 | 4.5 |
| `--sobre-perigo` | `--perigo-hover` #9e2d25 | 7.35:1 | 4.5 |
| `--verde` #7fd69a (selo sucesso) | `--verde-fundo` | 7.82:1 | 4.5 |
| `--verde` | `--superficie` / `--superficie-2` / `--fundo` | 9.12 / 7.91 / 10.15:1 | 4.5 |
| `--ambar` #f0a54a (selo aguardando/simulado) | `--ambar-fundo` | 6.70:1 | 4.5 |
| `--ambar` | `--superficie` / `--superficie-2` / `--fundo` | 7.75 / 6.72 / 8.63:1 | 4.5 |
| `--vermelho` #ff8f84 (selo erro, texto de erro) | `--vermelho-fundo` | 6.95:1 | 4.5 |
| `--vermelho` | `--superficie` / `--superficie-2` / `--fundo` / `--neutro-fundo` | 7.23 / 6.28 / 8.06 / 6.01:1 | 4.5 |
| `--neutro` #c9ccd1 (selo neutro) | `--neutro-fundo` | 8.24:1 | 4.5 |
| `--neutro` | `--superficie` | 9.92:1 | 4.5 |
| `--dourado` (foco, marcador ativo) | `--fundo` / `--superficie` / `--superficie-2` | 8.39 / 7.53 / 6.53:1 | 3 |
| `--dourado` (foco) | `--neutro-fundo` / `--verde-fundo` / `--ambar-fundo` / `--vermelho-fundo` | 6.26 / 6.46 / 6.52 / 7.23:1 | 3 |
| `--borda-forte` #7a818c (contorno de campo/botão) | `--fundo` / `--superficie` / `--superficie-2` / `--neutro-fundo` | 4.53 / 4.07 / 3.53 / 3.38:1 | 3 |

`--borda` (#4a5058) é só divisória decorativa. Nenhum componente depende dela para ser percebido.
Âmbar (aguardando/simulado) e dourado (ação) têm matizes próximos, mas nunca se confundem: o selo
traz ícone e texto, e o selo "simulado" tem borda tracejada.

Outros cuidados do CSS: foco `outline: 3px` dourado em todo elemento focável; `prefers-reduced-motion`
desliga transições e o giro do indicador de ocupado (o botão também troca o texto); `forced-colors` mantém
os contornos; sem rolagem horizontal da página (no menu superior < 900 px, os itens rolam dentro dele;
tabelas rolam dentro de `.recolhivel__corpo`).

## 7. Layout e responsividade

- ≥ 900 px: `div.estrutura` em grade, com a barra lateral fixa (`header.lateral`, sticky, 15 rem) e o conteúdo à
  direita (máx. 76 rem). ≥ 1280 px: mais respiro.
- < 1100 px: o detalhe vira uma coluna (mensagem primeiro, resposta depois).
- < 900 px: a barra lateral vira menu superior (marca + Sair na primeira linha, seções roláveis na
  segunda) e o conteúdo ocupa a largura toda. As linhas da lista quebram em duas: contato e tempo em cima;
  tipo, resumo e urgência embaixo.
- < 600 px: a comparação e a grade chave-valor empilham, e os botões do diálogo se esticam.

Classes utilitárias: `.pilha` / `.pilha--curta` (espaço vertical entre filhos), `.acoes` (linha de botões),
`.acoes--separadas`, `.selos`, `.secao-titulo`, `.suave`, `.pequeno`, `.numero`, `.uma-linha`, `.sr-only`.

## 8. Voz do Alfred (textos fixos)

Curtos, formais e sem humor em erro. Estados vazios: "Nenhuma pendência aguardando, senhor.",
"A agenda está vazia.", "Nenhum lote de limpeza, senhor.". Erros do JS (quando o servidor não manda `erro`):
"Não foi possível falar com a API do Alfred. Verifique se ela está no ar e tente de novo.",
"O servidor recusou a ação por segurança. Recarregue a página e tente de novo.",
"Item não encontrado. Ele pode ter sido removido.", "A ação não foi concluída (HTTP n)."

## 9. Telas implementadas (B2)

| Template | Rota | Observação |
|---|---|---|
| `inicio.html` + `_inicio.html` | `/painel`, `/painel/parcial/inicio` | cartões (pendências, Alfred, WhatsApp, limpeza, agenda) + 5 mais recentes |
| `pendencias.html` + `_pendencias_lista.html` | `/painel/pendencias`, `/painel/parcial/pendencias` | linha em `_linha.html` (macro `linha`); aba Resolvidas usa `ul.lista--resolvidas` (coluna de data mais larga) |
| `pendencia.html` | `/painel/pendencias/{id}` | WhatsApp e e-mail no mesmo template |
| `agenda.html` + `_agenda_lista.html` | `/painel/agenda`, `/painel/parcial/agenda` | |
| `limpeza.html` + `_limpeza_lista.html` | `/painel/limpeza`, `/painel/parcial/limpeza` | |
| `sistema.html` + `_sistema.html` | `/painel/sistema`, `/painel/parcial/sistema` | |
| `nao_encontrada.html` | detalhe com id inexistente (404) | |

Os dados chegam prontos de `painel.py` (datas já formatadas, `iso`/`relativo`/`absoluto`); os templates não
calculam nada além de escolher selos e textos.
