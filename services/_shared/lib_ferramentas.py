"""
Alfred — ferramentas para tool calling nativo do Ollama (`/api/chat` com `tools`).

Substitui as tools do Open WebUI (tools/*.py, classe Tools + Valves). Cada ferramenta é uma função comum
registrada com @ferramenta(nome, descricao, perigosa=False); o JSON Schema dos parâmetros sai da assinatura
(str, int, float, bool, Optional, Literal e valores padrão) e das linhas `:param x:` da docstring.

    esquemas()                  → lista `tools` no formato do Ollama ({"type": "function", "function": {...}})
    executar(nome, argumentos)  → texto para a mensagem role "tool". Nunca levanta: argumento inválido,
                                  exceção e timeout viram texto curto começando com "[erro]"
    perigosa(nome)              → True para o que age fora do chat ou é difícil de desfazer (comando shell,
                                  responder pendência / limpeza, gravar no cofre, completar tarefa). A aprovação
                                  NÃO é feita aqui: quem conversa pergunta "Executar? [Sim] [Não]" antes de
                                  chamar executar()

Código do repositório é chamado direto (services/inbox: core, whatsapp, limpeza; _shared: lib_db, lib_memoria).
HTTP só para os serviços separados — Shell Executor (PORT_SHELL_EXECUTOR, 7071) e Researcher (PORT_RESEARCHER,
7072) —, sempre em 127.0.0.1 com Bearer SHELL_EXECUTOR_TOKEN.

Sobreposições das tools antigas, juntas aqui:
  - alfred_memory (chave-valor em memory.yml) → lib_memoria: lembrar / buscar_memoria / listar_memoria
  - alfred_tasks → o mesmo VAULT_ALFRED/tasks.yml que o "vou" das pendências e a agenda gravam (core._add_task)
  - search_vault do vault_reader e do vault_writer → buscar_cofre, que passa por _buscar()
"""

import copy
import importlib
import inspect
import json
import os
import re
import subprocess
import sys
import threading
import time
import types
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal, Optional, Union, get_args, get_origin

import httpx
import yaml

from _shared import lib_db, lib_memoria
from _shared.lib_alfred import PROJECT_ROOT, gravar_atomico, log

INBOX_DIR = Path(__file__).resolve().parent.parent / "inbox"
TIMEOUT_PADRAO = 60       # s — teto de uma ferramenta que não declara o seu
MAX_SAIDA = 12000         # caracteres devolvidos ao modelo; o resto é cortado (nota longa, jornal, briefing)
MAX_ERRO = 300            # caracteres de uma mensagem de erro

_REGISTRO: dict[str, dict] = {}
_TIPOS = {str: "string", int: "integer", float: "number", bool: "boolean"}


# ══════════════════════════════════════════════════════════════
#  Registro
# ══════════════════════════════════════════════════════════════

def _base(anotacao):
    """Optional[X] / X | None → (X, True); qualquer outra anotação → (anotacao, False)."""
    if get_origin(anotacao) in (Union, types.UnionType):
        resto = [a for a in get_args(anotacao) if a is not type(None)]
        if len(resto) == 1:
            return resto[0], True
    return anotacao, False


def _descricoes(doc: Optional[str]) -> dict[str, str]:
    """Linhas `:param x: texto` da docstring → {x: texto}. Linha seguinte sem ":" no começo continua o texto."""
    saida, atual = {}, None
    for linha in inspect.cleandoc(doc or "").splitlines():
        linha = linha.strip()
        m = re.match(r":param\s+(\w+):\s*(.*)", linha)
        if m:
            atual = m[1]
            saida[atual] = m[2]
        elif not linha or linha.startswith(":"):
            atual = None
        elif atual:
            saida[atual] += " " + linha
    return saida


def _esquema_parametros(funcao) -> dict:
    """JSON Schema (type object) dos parâmetros. Valor padrão vai no fim da descrição, não em "default"."""
    descricoes = _descricoes(funcao.__doc__)
    props, obrigatorios = {}, []
    for p in inspect.signature(funcao).parameters.values():
        tipo, _ = _base(p.annotation)
        if get_origin(tipo) is Literal:
            prop = {"type": "string", "enum": [str(v) for v in get_args(tipo)]}
        elif tipo in _TIPOS:
            prop = {"type": _TIPOS[tipo]}
        else:
            raise TypeError(f"{funcao.__name__}: parâmetro {p.name!r} com tipo não suportado: {p.annotation!r}")
        texto = descricoes.get(p.name, "")
        if p.default is p.empty:
            obrigatorios.append(p.name)
        elif p.default not in (None, ""):
            texto = f"{texto} (padrão: {json.dumps(p.default, ensure_ascii=False)})".strip()
        if texto:
            prop["description"] = texto
        props[p.name] = prop
    return {"type": "object", "properties": props, "required": obrigatorios}


def ferramenta(nome: str, descricao: str, perigosa: bool = False, timeout: float = TIMEOUT_PADRAO):
    """Registra a função como ferramenta `nome`. Tipo de parâmetro fora de str/int/float/bool/Optional/Literal
    ou nome repetido levantam já na importação (erro de programação, não do modelo)."""
    def registrar(funcao):
        if nome in _REGISTRO:
            raise ValueError(f"ferramenta repetida: {nome}")
        _REGISTRO[nome] = {"funcao": funcao, "descricao": descricao, "perigosa": bool(perigosa),
                           "timeout": float(timeout), "parametros": _esquema_parametros(funcao)}
        return funcao
    return registrar


def esquemas() -> list[dict]:
    """Todas as ferramentas no formato `tools` do /api/chat do Ollama."""
    return [{"type": "function",
             "function": {"name": nome, "description": f["descricao"], "parameters": copy.deepcopy(f["parametros"])}}
            for nome, f in _REGISTRO.items()]


def perigosa(nome: str) -> bool:
    """True se a ferramenta precisa de "Executar? [Sim] [Não]" antes de executar(). Nome desconhecido → False
    (executar() não faz nada com ele)."""
    return bool((_REGISTRO.get(nome) or {}).get("perigosa"))


def _curto(e) -> str:
    """Exceção ou texto → uma linha de até MAX_ERRO caracteres (KeyError sem as aspas do repr)."""
    texto = str(e.args[0]) if isinstance(e, KeyError) and e.args else (str(e) or type(e).__name__)
    texto = " ".join(texto.split())
    return texto if len(texto) <= MAX_ERRO else texto[:MAX_ERRO - 1] + "…"


def _converter(nome: str, valor, anotacao):
    """Valor que veio do modelo → tipo da assinatura. "5" vira 5, "true" vira True; o resto → ValueError."""
    tipo, _ = _base(anotacao)
    if get_origin(tipo) is Literal:
        opcoes = [str(v) for v in get_args(tipo)]
        if str(valor).strip() not in opcoes:
            raise ValueError(f"'{nome}' deve ser um destes: {', '.join(opcoes)} (veio {valor!r})")
        return str(valor).strip()
    if tipo is str:
        return valor if isinstance(valor, str) else json.dumps(valor, ensure_ascii=False)
    if tipo is bool:
        if isinstance(valor, bool):
            return valor
        txt = str(valor).strip().lower()
        if txt in ("true", "1", "sim", "yes"):
            return True
        if txt in ("false", "0", "nao", "não", "no"):
            return False
    elif tipo is int and not isinstance(valor, bool):
        if isinstance(valor, float) and valor.is_integer():
            return int(valor)
        if re.fullmatch(r"\s*-?\d+\s*", str(valor)):
            return int(str(valor))
    elif tipo is float and not isinstance(valor, bool):
        try:
            return float(valor)
        except (TypeError, ValueError):
            pass
    raise ValueError(f"'{nome}' deveria ser {_TIPOS[tipo]} (veio {valor!r})")


def _argumentos(f: dict, argumentos) -> dict:
    """dict (ou texto JSON) do modelo → kwargs. Ausente ou null usa o padrão; chave desconhecida é ignorada."""
    if isinstance(argumentos, str):
        argumentos = json.loads(argumentos) if argumentos.strip() else {}
    if argumentos is None:
        argumentos = {}
    if not isinstance(argumentos, dict):
        raise ValueError("os argumentos devem ser um objeto JSON")
    kwargs = {}
    for p in inspect.signature(f["funcao"]).parameters.values():
        valor = argumentos.get(p.name)
        if valor is None:
            if p.default is p.empty:
                raise ValueError(f"falta o argumento obrigatório '{p.name}'")
            continue
        kwargs[p.name] = _converter(p.name, valor, p.annotation)
    return kwargs


def executar(nome: str, argumentos=None) -> str:
    """
    Roda a ferramenta numa thread com o timeout do registro e devolve o texto para o modelo. Nunca levanta.
    Timeout: a thread é abandonada (não dá para matá-la) — numa ferramenta perigosa o texto avisa que a ação
    pode ter terminado depois.
    """
    f = _REGISTRO.get(nome)
    if not f:
        return f"[erro] ferramenta desconhecida: {nome!r}"
    try:
        kwargs = _argumentos(f, argumentos)
    except ValueError as e:
        return f"[erro] argumentos inválidos para {nome}: {_curto(e)}"
    resultado: dict = {}

    def rodar():
        try:
            resultado["saida"] = f["funcao"](**kwargs)
        except BaseException as e:   # thread: nada escapa sem virar texto
            resultado["erro"] = e

    try:
        t = threading.Thread(target=rodar, name=f"ferramenta-{nome}", daemon=True)
        t.start()
        t.join(f["timeout"])
    except Exception as e:
        return f"[erro] {nome} não começou: {_curto(e)}"
    if t.is_alive():
        log(f"[ferramentas] {nome} passou de {f['timeout']:g} s — abandonada")
        aviso = " Pode ter terminado depois: confira antes de repetir." if f["perigosa"] else ""
        return f"[erro] {nome} não respondeu em {f['timeout']:g} s.{aviso}"
    if "erro" in resultado:
        log(f"[ferramentas] {nome} falhou: {type(resultado['erro']).__name__}: {_curto(resultado['erro'])}")
        return f"[erro] {nome}: {_curto(resultado['erro'])}"
    saida = "" if resultado.get("saida") is None else str(resultado["saida"])
    if len(saida) > MAX_SAIDA:
        saida = saida[:MAX_SAIDA] + f"\n… (cortado: mais {len(saida) - MAX_SAIDA} caracteres)"
    return saida


# ══════════════════════════════════════════════════════════════
#  Apoio: cofres, módulos do inbox, serviços HTTP
# ══════════════════════════════════════════════════════════════

def _cofre(var: str = "VAULT_ALFRED") -> Path:
    v = os.getenv(var, "").strip()
    if not v:
        raise RuntimeError(f"{var} não definido no .env")
    return Path(v)


def _dentro(base: Path, relativo: Optional[str]) -> Path:
    """`relativo` resolvido dentro de `base`; caminho absoluto, ".." ou symlink para fora → ValueError."""
    raiz = base.resolve()
    alvo = (raiz / (relativo or "")).resolve()
    if not alvo.is_relative_to(raiz):
        raise ValueError(f"caminho fora do cofre: {relativo!r} — use um caminho relativo ao cofre")
    return alvo


def _sem_frontmatter(texto: str) -> str:
    if texto.startswith("---"):
        fim = texto.find("\n---", 3)
        if fim > 0:
            return texto[fim + 4:].lstrip("\n")
    return texto


def _titulo_md(md: Path) -> str:
    """`title:` do frontmatter, ou o nome do arquivo."""
    try:
        texto = md.read_text(encoding="utf-8", errors="ignore")
        if texto.startswith("---"):
            fim = texto.find("\n---", 3)
            for linha in texto[3:fim if fim > 0 else 0].splitlines():
                m = re.match(r"""^title:\s*["']?(.+?)["']?\s*$""", linha)
                if m:
                    return m[1].strip()
    except OSError:
        pass
    return md.stem


def _inbox(modulo: str):
    """Módulo de services/inbox (core, whatsapp, limpeza), importado só quando uma ferramenta precisa dele:
    importar lib_ferramentas para esquemas() não carrega o pipeline do inbox."""
    if str(INBOX_DIR) not in sys.path:
        sys.path.insert(0, str(INBOX_DIR))
    return importlib.import_module(modulo)


def _servico(var: str, padrao: str) -> str:
    return f"http://127.0.0.1:{os.getenv(var, '').strip() or padrao}"


def _pedir(servico: str, metodo: str, url: str, **kw) -> httpx.Response:
    """httpx com o Bearer SHELL_EXECUTOR_TOKEN; serviço fora do ar ou lento → RuntimeError curto."""
    try:
        return httpx.request(metodo, url, headers={"Authorization": f"Bearer {os.getenv('SHELL_EXECUTOR_TOKEN', '')}"},
                             **kw)
    except httpx.ConnectError:
        raise RuntimeError(f"{servico} fora do ar ({url.split('/')[2]})") from None
    except httpx.TimeoutException:
        raise RuntimeError(f"{servico} não respondeu em {kw.get('timeout')} s") from None


def _detalhe(r: httpx.Response) -> str:
    try:
        d = r.json()
    except ValueError:
        return r.text[:200]
    return str(d.get("detail") if isinstance(d, dict) else d)[:200]


# ══════════════════════════════════════════════════════════════
#  Sistema (Shell Executor)
# ══════════════════════════════════════════════════════════════

def _executor(comando: str, timeout: int) -> dict:
    """POST /execute no Shell Executor → {stdout, stderr, returncode}. Recusa ou falha → RuntimeError."""
    r = _pedir("Shell Executor", "POST", _servico("PORT_SHELL_EXECUTOR", "7071") + "/execute",
               json={"command": comando, "timeout": timeout}, timeout=timeout + 5)
    if r.status_code == 200:
        return r.json()
    if r.status_code == 403:
        raise RuntimeError(f"comando fora da whitelist do executor: {comando!r}")
    if r.status_code == 401:
        raise RuntimeError("o executor recusou o token (SHELL_EXECUTOR_TOKEN)")
    raise RuntimeError(f"executor respondeu {r.status_code}: {_detalhe(r)}")


def _saida(comando: str, timeout: int = 10) -> str:
    """stdout (ou stderr) pelo executor; falha vira "[erro] ..." para não derrubar o relatório inteiro."""
    try:
        d = _executor(comando, timeout)
    except RuntimeError as e:
        return f"[erro] {e}"
    return (d.get("stdout") or "").strip() or (d.get("stderr") or "").strip()


@ferramenta("executar_comando",
            "Executa um comando shell real na máquina do Pedro (Shell Executor, com whitelist) e devolve a saída. "
            "Use para docker, disco, memória, GPU, rede, processos e logs. As units do Alfred são de usuário: "
            "sempre systemctl --user e journalctl --user. Nunca invente a saída: execute e mostre o resultado real.",
            perigosa=True, timeout=1810)
def executar_comando(comando: str, timeout: int = 15) -> str:
    """
    :param comando: Comando, ex.: "df -h", "nvidia-smi", "docker ps", "journalctl --user -u alfred-inbox -n 30
        --no-pager". Só pipes simples ("a | b"); ; && || $( ) crase < > são recusados
    :param timeout: Limite em segundos, de 1 a 1800
    """
    timeout = min(max(timeout, 1), 1800)
    d = _executor(comando, timeout)
    stdout, stderr = (d.get("stdout") or "").strip(), (d.get("stderr") or "").strip()
    rc = d.get("returncode", 0)
    if stdout:
        return stdout
    if stderr:
        return f"[stderr rc={rc}] {stderr}"
    return f"[comando executado sem saída, rc={rc}]"


@ferramenta("relatorio_sistema",
            "Relatório da máquina numa chamada só: CPU e carga, RAM, disco, GPU e containers Docker. Use quando "
            "Pedro perguntar do estado geral, desempenho ou uso de recursos.",
            timeout=90)
def relatorio_sistema() -> str:
    secoes = [
        ("CPU", "lscpu | grep -E 'Model name|CPU\\(s\\)|MHz' | head -5", "uptime"),
        ("RAM", "free -h"),
        ("Disco", "df -h / /home"),
        ("GPU", "nvidia-smi --query-gpu=name,memory.used,memory.total,utilization.gpu,temperature.gpu "
                "--format=csv,noheader"),
        ("Docker", "docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' 2>/dev/null"),
    ]
    return "\n\n".join(f"## {titulo}\n" + "\n".join(_saida(c) for c in comandos)
                       for titulo, *comandos in secoes)


@ferramenta("status_disco", "Uso de disco (df -h). Use quando a pergunta for só sobre espaço em disco.", timeout=20)
def status_disco(caminho: str = "/ /home") -> str:
    """
    :param caminho: Caminho(s) a verificar, separados por espaço
    """
    if not re.fullmatch(r"[\w./ -]+", caminho):
        return "Caminho inválido: use só letras, números, espaço e / . _ -"
    return _saida(f"df -h {caminho}")


@ferramenta("status_gpu", "Estado detalhado da GPU (nvidia-smi): VRAM, temperatura, uso e processos.", timeout=20)
def status_gpu() -> str:
    return _saida("nvidia-smi")


# ══════════════════════════════════════════════════════════════
#  Pesquisa (Researcher)
# ══════════════════════════════════════════════════════════════

@ferramenta("pesquisar",
            "Pesquisa um tema na web, sintetiza com IA e salva as notas no cofre do Alfred (research/<slug>/). Use "
            "quando Pedro pedir para pesquisar, estudar ou explorar um assunto. A rápida leva até 5 minutos; a "
            "profunda roda em segundo plano e devolve um job_id para andamento_pesquisa.",
            timeout=320)
def pesquisar(tema: str, profundo: bool = False) -> str:
    """
    :param tema: O tema, ex.: "Kubernetes operators", "RAG com SQLite"
    :param profundo: true para a pesquisa profunda (subperguntas, leitura das páginas, relatório com citações)
    """
    r = _pedir("Researcher", "POST", _servico("PORT_RESEARCHER", "7072") + "/research",
               json={"topic": tema, "num_queries": 4, "results_per_query": 8, "profundo": profundo}, timeout=300)
    if r.status_code == 202:
        d = r.json()
        return (f"Pesquisa profunda de \"{d.get('topic', tema)}\" começou em segundo plano (job {d.get('job_id')}). "
                f"Acompanhe com andamento_pesquisa(\"{d.get('job_id')}\").")
    if r.status_code != 200:
        raise RuntimeError(f"researcher respondeu {r.status_code}: {_detalhe(r)}")
    d = r.json()
    slug = d.get("matched_slug") or d.get("slug", "")
    if d.get("cached"):
        # acerto de cache (exato ou semântico): a resposta não traz sources_found nem queries_used
        if d.get("cache_type") == "semantic":
            origem = (f"do cache (semântico: tema parecido \"{d.get('matched_topic', '?')}\", "
                      f"similaridade {d.get('similarity', '?')})")
        else:
            origem = "do cache (exato, pesquisa dos últimos 7 dias)"
        return (f"Pesquisa {origem}.\n- Tema: {d.get('topic', tema)}\n- Cofre: {d.get('vault_path', '?')}\n\n"
                f"{d.get('synthesis', '')}")
    return (f"Pesquisa concluída.\n- Tema: {d.get('topic', tema)}\n- Fontes: {d.get('sources_found', '?')}\n"
            f"- Cofre: {d.get('vault_path', '?')}\n- Consultas: {'; '.join(d.get('queries_used') or [])}\n\n"
            f"Leia a síntese com ler_nota(\"research/{slug}/synthesis.md\", \"alfred\").")


@ferramenta("andamento_pesquisa",
            "Progresso ou resultado de uma pesquisa profunda iniciada por pesquisar(profundo=true).", timeout=20)
def andamento_pesquisa(job_id: str) -> str:
    """
    :param job_id: O job_id devolvido por pesquisar, ex.: "3f9c0a1b2d4e"
    """
    r = _pedir("Researcher", "GET", _servico("PORT_RESEARCHER", "7072") + f"/research/{job_id.strip()}",
               timeout=15)
    if r.status_code == 404:
        return f"Pesquisa não encontrada: {job_id}"
    if r.status_code != 200:
        raise RuntimeError(f"researcher respondeu {r.status_code}: {_detalhe(r)}")
    d = r.json()
    st = d.get("status")
    if st == "done":
        res = d.get("resultado") or {}
        return (f"Pesquisa profunda concluída: {d.get('topic')}\n- Cofre: {res.get('vault_path', '?')}\n"
                f"- Fontes: {res.get('sources_found', '?')}\n\n{res.get('synthesis', '')}")
    if st == "error":
        return f"A pesquisa profunda falhou: {d.get('erro')}"
    if st == "interrompido":
        return "A pesquisa profunda foi interrompida (o researcher reiniciou no meio). Peça de novo."
    c = d.get("contagem") or {}
    return (f"Em andamento — etapa {d.get('etapa')}: {d.get('mensagem', '')}\n"
            f"{len(d.get('subperguntas') or [])} subpergunta(s), {c.get('lidas', 0)} de {c.get('tentadas', 0)} "
            f"página(s) lidas, {len(d.get('notas') or [])} nota(s).")


# ══════════════════════════════════════════════════════════════
#  Briefing e resumo do dia
# ══════════════════════════════════════════════════════════════

@ferramenta("gerar_briefing",
            "Gera agora um briefing de notícias de tecnologia (SearXNG + RSS, síntese com IA e áudio). Leva de 2 a "
            "4 minutos. Use quando Pedro pedir notícias, briefing, novidades de tech ou \"me atualiza\".",
            timeout=620)
def gerar_briefing() -> str:
    inicio = time.monotonic()
    try:
        r = subprocess.run([str(PROJECT_ROOT / "services" / "briefing" / "run.sh")],
                           capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return "Falha: o briefing passou de 10 minutos e foi interrompido. Nenhum briefing novo foi criado."
    saida = (r.stdout or "") + (r.stderr or "")
    duracao = time.monotonic() - inicio
    if r.returncode != 0:
        # falhou (Ollama fora, sem notícias...): nunca mostrar um briefing antigo como novo
        fim = "\n".join(saida.strip().splitlines()[-25:])
        return (f"**Falha ao gerar o briefing** (código de saída {r.returncode}, {duracao:.0f} s). "
                f"Nenhum briefing novo foi criado.\n\nFim do log:\n```\n{fim}\n```")
    arquivo = next((l.split("=", 1)[1].strip() for l in saida.splitlines() if l.startswith("BRIEFING_FILE=")), "")
    md = Path(arquivo) if arquivo else None
    if not md or not md.is_file():
        # modo jogo (alfred-switch.sh off) sai com 0 e só imprime o aviso
        return f"O briefing rodou ({duracao:.0f} s), mas não gerou arquivo.\n\nSaída:\n```\n{saida[-2000:]}\n```"
    return f"**Tech Briefing gerado em {duracao:.0f} s** ({md.name})\n\n" + \
        _sem_frontmatter(md.read_text(encoding="utf-8"))


@ferramenta("ver_briefings", "Lista os briefings já gerados (não gera um novo), do mais recente para o mais antigo.")
def ver_briefings(quantidade: int = 5) -> str:
    """
    :param quantidade: Quantos briefings listar
    """
    pasta = _cofre() / "briefings"
    mds = sorted(pasta.glob("*.md"), key=lambda f: f.stat().st_mtime, reverse=True)[:max(1, quantidade)] \
        if pasta.is_dir() else []
    if not mds:
        return "Nenhum briefing encontrado. Use gerar_briefing para criar um."
    linhas = []
    for f in mds:
        mp3 = f.with_suffix(".mp3")
        audio = " (com áudio)" if mp3.exists() and mp3.stat().st_size > 0 else ""
        linhas.append(f"- **{_titulo_md(f)}**{audio} — arquivo `{f.name}`")
    return f"{len(mds)} briefing(s) recente(s):\n" + "\n".join(linhas)


@ferramenta("ler_briefing", "Lê o conteúdo completo de um briefing listado por ver_briefings.")
def ler_briefing(arquivo: str) -> str:
    """
    :param arquivo: Nome do arquivo, ex.: "2026-04-06_0700_ia-revoluciona-saude.md"
    """
    pasta = _cofre() / "briefings"
    f = _dentro(pasta, arquivo)
    if not f.is_file():
        f = _dentro(pasta, f"{arquivo}.md")
    if not f.is_file():
        return f"Briefing não encontrado: {arquivo}. Use ver_briefings para listar."
    return _sem_frontmatter(f.read_text(encoding="utf-8"))


@ferramenta("resumo_do_dia",
            "Resumo do período, sem IA: briefings gerados, pesquisas salvas, decisões e logs, tarefas concluídas, "
            "vencidas e a vencer, e memórias que vencem em 7 dias. Use para \"o que aconteceu hoje?\", \"resumo do "
            "dia\", \"o que você fez?\", \"resumo da semana\".")
def resumo_do_dia(dias: int = 1) -> str:
    """
    :param dias: Quantos dias considerar (1 = hoje, 7 = semana)
    """
    dias = max(1, dias)
    desde = datetime.now() - timedelta(days=dias)
    periodo = "hoje" if dias <= 1 else f"nos últimos {dias} dias"
    vault = _cofre()
    secoes = []

    def recentes(pasta: str) -> list[Path]:
        d = vault / pasta
        return sorted((f for f in d.glob("*.md") if datetime.fromtimestamp(f.stat().st_mtime) >= desde),
                      key=lambda f: f.stat().st_mtime, reverse=True) if d.is_dir() else []

    briefings = recentes("briefings")
    if briefings:
        secoes.append(f"**Briefings gerados {periodo} ({len(briefings)}):**\n"
                      + "\n".join(f"  • {_titulo_md(f)}" for f in briefings[:5]))
    pesquisas = []
    if (vault / "research").is_dir():
        for d in sorted((vault / "research").iterdir(), key=lambda d: d.stat().st_mtime, reverse=True):
            s = d / "synthesis.md"
            if s.is_file() and datetime.fromtimestamp(s.stat().st_mtime) >= desde:
                pesquisas.append(f"  • {_titulo_md(s)}")
    if pesquisas:
        secoes.append(f"**Pesquisas {periodo} ({len(pesquisas)}):**\n" + "\n".join(pesquisas[:5]))
    for pasta in ("decisions", "logs"):
        notas = recentes(pasta)
        if notas:
            secoes.append(f"**{pasta.capitalize()} ({len(notas)}):**\n"
                          + "\n".join(f"  • {_titulo_md(f)}" for f in notas[:3]))

    try:
        tarefas = _ler_tasks()["tasks"]
    except RuntimeError as e:
        tarefas = []
        secoes.append(f"**Tarefas:** {e}")
    hoje = date.today().isoformat()
    em7 = (date.today() + timedelta(days=7)).isoformat()
    concluidas = [t for t in tarefas if t.get("status") == "done"
                  and str(t.get("completed_at") or "")[:10] >= desde.date().isoformat()]
    if concluidas:
        secoes.append(f"**Tarefas concluídas {periodo} ({len(concluidas)}):**\n"
                      + "\n".join(f"  ✓ #{t.get('id')} {t.get('title')}" for t in concluidas[:5]))
    abertas = [(t, _data_ou_none(t.get("due_date"))) for t in tarefas if t.get("status") == "open"]
    vencidas = [(t, d) for t, d in abertas if d and d < hoje]
    proximas = [(t, d) for t, d in abertas if d and hoje <= d <= em7]
    invalidas = [t for t, d in abertas if t.get("due_date") and not d]
    if vencidas:
        secoes.append(f"**Tarefas vencidas ({len(vencidas)}):**\n"
                      + "\n".join(f"  ⚠️ #{t.get('id')} {t.get('title')} (venceu {d})" for t, d in vencidas[:3]))
    if invalidas:
        secoes.append(f"**Tarefas com data inválida ({len(invalidas)}):**\n"
                      + "\n".join(f"  ? #{t.get('id')} {t.get('title')} ({t.get('due_date')})" for t in invalidas[:3]))
    if proximas:
        secoes.append("**Próximos vencimentos (7 dias):**\n"
                      + "\n".join(f"  → #{t.get('id')} {t.get('title')} (vence {d})" for t, d in proximas[:3]))

    vencendo = [f for f in lib_memoria.listar()
                if f.get("validade") != "permanente" and hoje <= str(f.get("validade"))[:10] <= em7]
    if vencendo:
        secoes.append("**Memórias que vencem em 7 dias:**\n"
                      + "\n".join(f"  • [{f['sujeito']}] {f['fato']} (até {f['validade']})" for f in vencendo[:3]))

    if not secoes:
        return f"Nada registrado {periodo}. Cofre sem atividade recente."
    return f"## Resumo — {date.today():%d/%m/%Y}\n" + "\n\n".join(secoes)


# ══════════════════════════════════════════════════════════════
#  Pendências, jornal e limpeza (services/inbox)
# ══════════════════════════════════════════════════════════════

@ferramenta("listar_pendencias",
            "Lista o que aguarda a decisão de Pedro: convites para eventos e contatos profissionais por e-mail "
            "(\"vou / não vou\") e mensagens de WhatsApp (\"o que você quer responder?\"). Use para \"tenho algum "
            "convite?\", \"o que está pendente?\", \"tem algo esperando resposta?\", \"quem me mandou WhatsApp?\".")
def listar_pendencias(status: Literal["aguardando", "todas", "vou", "nao_vou", "respondida", "ignorada",
                                      "expirada"] = "aguardando") -> str:
    """
    :param status: "aguardando" = só o que ainda espera decisão de Pedro (o normal para "o que está pendente?");
        "todas" inclui as já resolvidas; os outros filtram por um status
    """
    pend = _inbox("core").listar_pendencias(status)
    if not pend:
        return "Nenhuma pendência" + ("" if status == "todas" else f" com status '{status}'") + "."
    linhas = []
    for p in pend:
        ev = p.get("evento") or {}
        if p.get("tipo") == "whatsapp":
            wa = p.get("whatsapp") or {}
            linhas.append(
                f"• [{p.get('id')}] (whatsapp, {p.get('status')}) **{wa.get('contato') or p.get('titulo')}** "
                f"· {wa.get('tipo_msg')} · {wa.get('categoria')} · urgência {wa.get('urgencia')}"
                + (f"\n  ❝ {(wa.get('texto') or '')[:300]} ❞" if wa.get("texto") else "")
                + (f"\n  {ev.get('resumo')}" if ev.get("resumo") else "")
                + (f"\n  resposta enviada: {p.get('resposta_texto')}" if p.get("resposta_texto") else ""))
            continue
        local = ev.get("local") or ("online" if ev.get("online") else "")
        linhas.append(
            f"• [{p.get('id')}] ({p.get('tipo')}, {p.get('status')}) **{p.get('titulo')}** — "
            f"{ev.get('data_inicio') or 'data não informada'}" + (f" · {local}" if local else "")
            + (f"\n  {ev.get('resumo')}" if ev.get("resumo") else ""))
    return (f"{len(pend)} pendência(s):\n" + "\n".join(linhas)
            + "\n\nPara decidir: responder_pendencia(id, \"vou\" | \"nao_vou\" | \"ignorar\"); "
              "WhatsApp: responder_pendencia(id, \"<texto da resposta>\").")


@ferramenta("ver_pendencia",
            "Mostra o texto completo que Alfred escreveu sobre uma pendência: o que é, por que interessa e o que "
            "precisa decidir.")
def ver_pendencia(pendencia_id: str) -> str:
    """
    :param pendencia_id: ID da pendência, ex.: "20261019-meetup-hostgator"
    """
    core = _inbox("core")
    p = core.obter_pendencia(pendencia_id)
    if not p:
        return f"Pendência não encontrada: {pendencia_id}"
    md_rel = (p.get("arquivos") or {}).get("md")
    md = core.vault() / md_rel if md_rel else None
    if md and md.is_file():
        return _sem_frontmatter(md.read_text(encoding="utf-8"))
    return yaml.dump(p, allow_unicode=True, sort_keys=False)


@ferramenta("responder_pendencia",
            "Registra a decisão de Pedro sobre uma pendência. E-mail (convite/contato): resposta \"vou\" (cria "
            "tarefa), \"nao_vou\" ou \"ignorar\". WhatsApp: resposta é o TEXTO que Alfred envia ao contato "
            "(exatamente o que Pedro ditou) ou \"ignorar\". Use quando Pedro disser \"vou\", \"não vou\", \"ignora "
            "esse\", \"responde pro Carlos que ...\".",
            perigosa=True, timeout=90)
def responder_pendencia(pendencia_id: str, resposta: str, nota: str = "") -> str:
    """
    :param pendencia_id: ID da pendência
    :param resposta: "vou" | "nao_vou" | "ignorar" — ou, para WhatsApp, o texto da resposta
    :param nota: Observação de Pedro, ex.: "só a palestra da tarde"
    """
    core = _inbox("core")
    p = core.obter_pendencia(pendencia_id)
    if not p:
        return f"Pendência não encontrada: {pendencia_id}"
    try:
        if p.get("tipo") == "whatsapp":
            entry = _inbox("whatsapp").responder_texto(pendencia_id, resposta, True, "via chat", nota)
        else:
            entry = core.responder(pendencia_id, resposta, nota, origem="via chat", so_se_aguardando=True)
    except core.PendenciaJaResolvida as e:
        return f"{e} (resolvida por {e.caminho}). Nada mudou."
    if p.get("tipo") != "whatsapp":
        rotulo = {"vou": "VOU", "nao_vou": "NÃO VOU", "ignorada": "IGNORADA"}[entry["status"]]
        return (f"Anotado: {rotulo} para \"{entry.get('titulo')}\"."
                + (f" Tarefa #{entry['task_id']} criada em tasks.yml." if entry.get("_task_criada") else ""))
    wa = entry.get("whatsapp") or {}
    contato = wa.get("contato") or entry.get("titulo")
    envio = wa.get("envio") or {}
    if entry.get("status") == "ignorada":
        return f"Anotado: não respondo a {contato}. Nada foi enviado."
    if entry.get("status") == "aguardando" or not envio.get("ok"):
        return (f"O envio para {contato} falhou: {envio.get('erro') or 'a Evolution API não confirmou'}. "
                "O texto ficou guardado; peça de novo para reenviar.")
    if envio.get("simulado"):
        return f"Envio SIMULADO (EVOLUTION_URL vazio ou WHATSAPP_DRY_RUN): nada saiu da máquina. Texto para {contato}:\n“{resposta}”"
    return f"Enviado a {contato} pelo WhatsApp:\n“{resposta}”"


@ferramenta("jornal_de_hoje",
            "Última edição do jornal diário montado a partir das newsletters do e-mail. Use para \"jornal de "
            "hoje\", \"edição do dia\", \"o que chegou nas newsletters\".")
def jornal_de_hoje() -> str:
    md = _inbox("core").ultimo_jornal_md()
    if not md:
        return "Nenhuma edição do jornal foi gerada ainda."
    texto = _sem_frontmatter(md.read_text(encoding="utf-8"))
    if md.with_suffix(".mp3").exists():
        texto += f"\n\n*(Áudio disponível: {md.with_suffix('.mp3').name})*"
    return texto


@ferramenta("listar_limpezas",
            "Pedidos de limpeza da caixa de e-mail (\"posso apagar estes N e-mails?\") que aguardam Pedro, com os "
            "e-mails de cada lote e o motivo. Use para \"o que você quer apagar?\", \"tem limpeza pendente?\".")
def listar_limpezas() -> str:
    lotes = _inbox("limpeza").listar_lotes("aguardando")
    if not lotes:
        return "Nenhum pedido de limpeza aguardando."
    partes = []
    for l in lotes:
        itens = l.get("itens") or []
        amostra = "\n".join(f"  • {i['date'][:10]} {i['from']} — {(i.get('subject') or '')[:70]} ({i['motivo']})"
                            for i in itens[:15])
        mais = f"\n  … e mais {len(itens) - 15}" if len(itens) > 15 else ""
        partes.append(f"[{l['id']}] {l.get('total', len(itens))} e-mail(s) → Lixeira\n{amostra}{mais}")
    return "\n\n".join(partes) + \
        "\n\nSó aprove se Pedro disser claramente que sim: responder_limpeza(id, \"aprovar\" | \"recusar\")."


@ferramenta("responder_limpeza",
            "Registra a decisão de Pedro sobre um pedido de limpeza. \"aprovar\" libera a extensão do Thunderbird "
            "para MOVER os e-mails do lote para a Lixeira. Só aprove quando Pedro confirmar esse lote nesta "
            "conversa (\"pode apagar\", \"aprovo a limpeza X\"); na dúvida, pergunte.",
            perigosa=True)
def responder_limpeza(lote_id: str, decisao: Literal["aprovar", "recusar"], nota: str = "") -> str:
    """
    :param lote_id: ID do lote, ex.: "limpeza-20261019-2210"
    :param decisao: "aprovar" ou "recusar"
    :param nota: Observação de Pedro
    """
    limpeza = _inbox("limpeza")
    if decisao == "recusar":
        limpeza.recusar(lote_id, nota or "via chat")
        return f"Limpeza {lote_id} recusada — nada será apagado."
    lote = limpeza.aprovar(lote_id, nota or "via chat")
    fora = len(lote.get("removidos_na_aprovacao") or [])
    return (f"Limpeza {lote_id} aprovada: {lote.get('total')} e-mail(s) vão para a Lixeira na próxima passada da "
            "extensão do Thunderbird." + (f" {fora} saíram do lote na revalidação." if fora else ""))


# ══════════════════════════════════════════════════════════════
#  Tarefas (VAULT_ALFRED/tasks.yml — o mesmo do "vou" e da agenda)
# ══════════════════════════════════════════════════════════════

def _data_iso(valor) -> Optional[str]:
    """Vencimento → AAAA-MM-DD. Aceita AAAA-MM-DD (mês/dia de um dígito também), DD/MM/AAAA e date do YAML
    (data sem aspas editada à mão). None/"" → None. Inválida → ValueError."""
    if valor in (None, ""):
        return None
    if isinstance(valor, datetime):
        return valor.date().isoformat()
    if isinstance(valor, date):
        return valor.isoformat()
    txt = str(valor).strip()
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", txt)
    if m:
        return date(int(m[1]), int(m[2]), int(m[3])).isoformat()
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", txt)
    if m:
        return date(int(m[3]), int(m[2]), int(m[1])).isoformat()
    raise ValueError(f"data inválida: {txt!r}")


def _data_ou_none(valor) -> Optional[str]:
    try:
        return _data_iso(valor)
    except ValueError:
        return None


def _tasks_file() -> Path:
    return _cofre() / "tasks.yml"


def _ler_tasks() -> dict:
    """tasks.yml → {"tasks": [...]}. Ilegível ou fora do formato → RuntimeError: nada é gravado por cima
    (core._load_tasks, usado para criar, guarda uma cópia e segue vazio — aqui só se lê ou se marca)."""
    f = _tasks_file()
    if not f.exists():
        return {"tasks": []}
    try:
        data = yaml.safe_load(f.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise RuntimeError(f"tasks.yml com erro de YAML — corrija o arquivo; nada foi alterado ({_curto(e)})")
    if data is None or (isinstance(data, dict) and data.get("tasks") is None):
        return {**(data or {}), "tasks": []}
    if not isinstance(data, dict) or not isinstance(data["tasks"], list) \
            or not all(isinstance(t, dict) for t in data["tasks"]):
        raise RuntimeError("tasks.yml fora do formato (tasks: lista de tarefas) — corrija o arquivo; nada foi alterado")
    return data


@ferramenta("adicionar_tarefa",
            "Cria uma tarefa no tasks.yml do Alfred. Use quando Pedro mencionar algo que precisa fazer, um prazo ou "
            "compromisso, ex.: \"preciso estudar para a prova\", \"tenho que entregar o projeto até sexta\".")
def adicionar_tarefa(titulo: str, vencimento: Optional[str] = None, tags: str = "",
                     prioridade: Literal["low", "normal", "high"] = "normal") -> str:
    """
    :param titulo: Descrição clara da tarefa
    :param vencimento: Data de vencimento AAAA-MM-DD (aceita DD/MM/AAAA)
    :param tags: Tags separadas por vírgula, ex.: "fatec,estudo"
    :param prioridade: Prioridade da tarefa
    """
    try:
        venc = _data_iso(vencimento)
    except ValueError:
        return f"Data de vencimento inválida: {vencimento!r}. Use AAAA-MM-DD ou DD/MM/AAAA. Tarefa não criada."
    tid =_inbox("core")._add_task(titulo, venc, [t.strip() for t in tags.split(",") if t.strip()], prioridade)
    return f"Tarefa #{tid} criada: \"{titulo}\"" + (f" (vence em {venc})" if venc else "")


@ferramenta("listar_tarefas",
            "Lista as tarefas (por padrão, as abertas). Use quando Pedro perguntar \"o que tenho pra fazer\" ou ao "
            "falar do dia ou da semana.")
def listar_tarefas(filtro: Literal["abertas", "concluidas", "canceladas", "todas"] = "abertas") -> str:
    """
    :param filtro: Quais tarefas mostrar
    """
    status = {"abertas": "open", "concluidas": "done", "canceladas": "cancelled"}.get(filtro)
    tarefas = [t for t in _ler_tasks()["tasks"] if status is None or t.get("status") == status]
    if not tarefas:
        return "Nenhuma tarefa encontrada." if filtro == "todas" else f"Nenhuma tarefa {filtro[:-1]}."
    hoje = date.today().isoformat()
    linhas = []
    for t in sorted(tarefas, key=lambda x: (_data_ou_none(x.get("due_date")) or "9999", x.get("id", 0))):
        icone = {"done": "✓", "cancelled": "✗"}.get(t.get("status"), "○")
        prio = {"high": "!", "low": "↓"}.get(t.get("priority", ""), " ")
        venc = _data_ou_none(t.get("due_date"))
        if venc:
            txt_venc = f" · vence {venc}" + (" ⚠️ VENCIDA" if venc < hoje and t.get("status") == "open" else "")
        else:
            txt_venc = f" · vence {t['due_date']} (data inválida)" if t.get("due_date") else ""
        txt_tags = f" [{', '.join(map(str, t['tags']))}]" if t.get("tags") else ""
        linhas.append(f"{icone} [{prio}] #{t.get('id')} {t.get('title')}{txt_venc}{txt_tags}")
    titulo = {"abertas": "Tarefas abertas", "concluidas": "Tarefas concluídas", "canceladas": "Tarefas canceladas",
              "todas": "Todas as tarefas"}[filtro]
    return f"**{titulo} ({len(linhas)}):**\n" + "\n".join(linhas)


@ferramenta("completar_tarefa",
            "Marca uma tarefa como concluída. Use quando Pedro disser que terminou algo ou pedir para marcar como "
            "feita.",
            perigosa=True)
def completar_tarefa(tarefa_id: int) -> str:
    """
    :param tarefa_id: ID numérico da tarefa (de listar_tarefas)
    """
    core = _inbox("core")
    f = _tasks_file()
    with core.trava_arquivo(f):   # a mesma trava de core._add_task: ninguém grava no meio
        data = _ler_tasks()
        t = next((t for t in data["tasks"] if t.get("id") == tarefa_id), None)
        if not t:
            return f"Tarefa #{tarefa_id} não encontrada."
        if t.get("status") == "done":
            return f"Tarefa #{tarefa_id} já estava concluída."
        t["status"] = "done"
        t["completed_at"] = datetime.now().isoformat(timespec="minutes")
        gravar_atomico(f, yaml.dump(data, allow_unicode=True, sort_keys=False))
    return f"Tarefa #{tarefa_id} concluída: \"{t.get('title')}\""


@ferramenta("tarefas_vencidas",
            "Tarefas abertas com vencimento antes de hoje. Use ao começar uma conversa sobre agenda, dia ou semana; "
            "se houver alguma, avise Pedro antes de qualquer outra coisa.")
def tarefas_vencidas() -> str:
    hoje = date.today()
    abertas = [t for t in _ler_tasks()["tasks"] if t.get("status") == "open" and t.get("due_date")]
    datas = [(_data_ou_none(t["due_date"]), t) for t in abertas]
    vencidas = sorted(((d, t) for d, t in datas if d and d < hoje.isoformat()), key=lambda x: x[0])
    invalidas = [t for d, t in datas if d is None]
    linhas = [f"⚠️ #{t.get('id')} \"{t.get('title')}\" — venceu em {d} "
              f"({(hoje - date.fromisoformat(d)).days} dia(s) atrás)" for d, t in vencidas]
    linhas += [f"? #{t.get('id')} \"{t.get('title')}\" — vencimento inválido: {t['due_date']!r}" for t in invalidas]
    if not vencidas:
        return "\n".join(["Nenhuma tarefa vencida."] + linhas)
    return f"**{len(vencidas)} tarefa(s) vencida(s):**\n" + "\n".join(linhas)


# ══════════════════════════════════════════════════════════════
#  Memória (lib_memoria)
# ══════════════════════════════════════════════════════════════

@ferramenta("lembrar",
            "Guarda na memória do Alfred os fatos duráveis de um texto: preferências, projetos, trabalho, pessoas, "
            "rotina, prazos. Use quando Pedro pedir para lembrar algo ou contar algo que vale para as próximas "
            "conversas. Fatos repetidos ou parecidos são unidos sozinhos.",
            timeout=180)
def lembrar(texto: str, sujeito: Optional[str] = None) -> str:
    """
    :param texto: O que guardar, em frases completas, ex.: "Pedro prefere reuniões depois das 14h"
    :param sujeito: De quem são os fatos, ex.: "Carlos Silva" (sem ele, deduzido do texto; na dúvida, Pedro)
    """
    fatos = lib_memoria.extrair(texto, "chat", sujeito)
    if not fatos:
        return "Nada guardado: não achei fato durável nesse texto (ou a memória está desligada ou o Ollama fora do ar)."
    acoes = {"novo": "novo", "fundido": "unido a um parecido", "substituiu": "trocou um antigo",
             "descartado": "já sabia"}
    return "Memória:\n" + "\n".join(f"• [{f['sujeito']}] {f['fato']} ({acoes.get(f['acao'], f['acao'])})"
                                    for f in fatos)


@ferramenta("buscar_memoria",
            "Fatos que Alfred já sabe sobre um assunto ou pessoa (memória automática). Use antes de responder sobre "
            "projetos, preferências ou contatos do Pedro.")
def buscar_memoria(consulta: str, quantidade: int = 8) -> str:
    """
    :param consulta: Assunto ou pessoa, ex.: "Carlos Fatec orçamento"
    :param quantidade: Máximo de fatos
    """
    return lib_memoria.contexto(consulta, max(1, quantidade)) or f"Nada na memória sobre {consulta!r}."


@ferramenta("listar_memoria", "Lista os fatos guardados na memória do Alfred, de todos ou de um sujeito.")
def listar_memoria(sujeito: Optional[str] = None) -> str:
    """
    :param sujeito: Só os fatos desta pessoa, ex.: "Pedro"
    """
    fatos = lib_memoria.listar(sujeito)
    if not fatos:
        return f"Nada guardado sobre {sujeito}." if sujeito else "Memória vazia."
    return f"{len(fatos)} fato(s):\n" + "\n".join(
        f"• #{f['id']} [{f['sujeito']}] {f['fato']} ({f['tipo']}"
        + ("" if f.get("validade") == "permanente" else f", até {f.get('validade')}") + ")" for f in fatos)


# ══════════════════════════════════════════════════════════════
#  Cofres do Obsidian
# ══════════════════════════════════════════════════════════════

def _buscar(query: str, cofre: Optional[str], limite: int) -> list[dict]:
    """
    Busca nos cofres → [{cofre, caminho, titulo, trecho, score}], do mais relevante para o menos. Ponto único
    de troca: hoje FTS5 (lib_db.buscar_notas, índice de `inbox-assistant.py indexar`); a busca híbrida de
    _shared/lib_rag.py entra aqui, com a mesma saída. cofre None = todos.
    """
    return lib_db.buscar_notas(query, cofre=cofre, limite=limite)


@ferramenta("buscar_cofre",
            "Busca notas nos cofres do Obsidian por palavras-chave: \"pedro\" (anotações, projetos e decisões do "
            "Pedro) e \"alfred\" (pesquisas, briefings, logs e decisões que Alfred salvou). Use quando Pedro "
            "perguntar das próprias notas e antes de pesquisar na web, para não repetir o que já existe.")
def buscar_cofre(consulta: str, cofre: Literal["todos", "pedro", "alfred"] = "todos", limite: int = 5) -> str:
    """
    :param consulta: Termos a buscar
    :param cofre: Em qual cofre buscar
    :param limite: Máximo de resultados (até 20)
    """
    achados = _buscar(consulta, None if cofre == "todos" else cofre, min(max(limite, 1), 20))
    if not achados:
        return f"Nenhuma nota encontrada para {consulta!r}."
    return f"{len(achados)} resultado(s) para {consulta!r} — leia com ler_nota(caminho, cofre):\n\n" + "\n\n".join(
        f"### [{a['cofre']}] {a['caminho']} (score {a['score']})\n{a['trecho']}" for a in achados)


@ferramenta("ler_nota", "Lê o conteúdo completo de uma nota de um dos cofres (caminho de buscar_cofre ou listar_cofre).")
def ler_nota(caminho: str, cofre: Literal["pedro", "alfred"] = "pedro") -> str:
    """
    :param caminho: Caminho relativo ao cofre, ex.: "projetos/meu-projeto.md"
    :param cofre: De qual cofre
    """
    alvo = _dentro(_cofre("VAULT_PEDRO" if cofre == "pedro" else "VAULT_ALFRED"), caminho)
    if not alvo.is_file():
        return f"Nota não encontrada no cofre {cofre}: {caminho}"
    return alvo.read_text(encoding="utf-8", errors="ignore")


@ferramenta("listar_cofre", "Lista as notas (.md) do cofre do Alfred, inteiro ou de uma pasta.")
def listar_cofre(pasta: str = "") -> str:
    """
    :param pasta: Subpasta, ex.: "research", "decisions" (vazio = cofre inteiro)
    """
    raiz = _cofre().resolve()
    alvo = _dentro(raiz, pasta)
    if not alvo.is_dir():
        return f"Pasta não encontrada no cofre do Alfred: {pasta!r}"
    notas = sorted(alvo.rglob("*.md"))
    if not notas:
        return "Nenhuma nota nessa pasta."
    return f"{len(notas)} nota(s):\n" + "\n".join(f"- {n.relative_to(raiz)}" for n in notas)


@ferramenta("salvar_nota",
            "Salva uma nota no cofre do Alfred: resultado de pesquisa, decisão técnica, resumo de conversa, erro "
            "resolvido, aprendizado. Nunca salve placeholder ou conteúdo vazio.",
            perigosa=True)
def salvar_nota(titulo: str, conteudo: str, pasta: str = "logs") -> str:
    """
    :param titulo: Título da nota (vira o nome do arquivo)
    :param conteudo: Conteúdo em markdown
    :param pasta: Subpasta, ex.: "logs", "research", "decisions"
    """
    if not conteudo.strip():
        return "Conteúdo vazio: nada salvo."
    raiz = _cofre().resolve()
    destino = _dentro(raiz, pasta)
    destino.mkdir(parents=True, exist_ok=True)
    agora = datetime.now()
    nome = " ".join("".join(c if c.isalnum() or c in " -_" else "" for c in titulo).split()) \
        or agora.strftime("nota-%H%M%S")   # título só de símbolos
    arquivo, n = destino / f"{agora:%Y-%m-%d} {nome}.md", 2
    while arquivo.exists():   # mesmo título no mesmo dia: não sobrescreve
        arquivo, n = destino / f"{agora:%Y-%m-%d} {nome} ({n}).md", n + 1
    # yaml.safe_dump: título com ":" ("Decisão: usar QUIC") daria frontmatter inválido
    meta = {"title": titulo, "date": f"{agora:%Y-%m-%d}", "time": f"{agora:%H:%M}", "folder": pasta}
    with open(arquivo, "x", encoding="utf-8") as fh:
        fh.write("---\n" + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False) + "---\n\n" + conteudo)
    return f"Nota salva em: {arquivo.relative_to(raiz)}"
