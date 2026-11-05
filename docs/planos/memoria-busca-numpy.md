# Plano — busca vetorial da memória com numpy em cache

Origem: `docs/avaliacoes/memoria.md`, seção 4.3. **Ainda não implementado.**

## Problema

`lib_memoria.contexto()` roda em todo turno do chat (`services/inbox/conversa.py:393`). A cada chamada ele
faz duas coisas caras:

1. lê **todas** as linhas da tabela `fatos`, com o vetor, do SQLite;
2. calcula o cosseno de cada uma em Python puro (`_vetor` + `_cos`).

Custo medido de ponta a ponta:

| fatos | `contexto()` |
|---|---|
| 1 mil | 56 ms |
| 1,7 mil | ~100 ms |
| 10 mil | 0,6 a 0,9 s |
| 100 mil | 5,9 s |

Com numpy e a matriz em memória, a busca vetorial em si cai para 0,4 ms (1 mil), 1,8 ms (10 mil) e 19 ms
(100 mil).

## Por que numpy e não ChromaDB nem FAISS

Medido em 05/11/2026 (`docs/avaliacoes/scripts/memoria_chroma.py`, só leitura na coleção `cofres-bge-m3`,
que tem 1.749 vetores de 1024 dimensões):

| jeito de buscar (k=8, ~1,7 a 2 mil vetores) | mediana |
|---|---|
| `contexto()` de hoje (SELECT de tudo + Python) | ~100 a 120 ms |
| ChromaDB do jeito do `lib_chromadb` (cliente HTTP novo a cada consulta) | 11,7 a 16,8 ms |
| ChromaDB com conexão HTTP reaproveitada | 3,6 a 6,1 ms |
| numpy com a matriz em cache | 0,4 a 0,7 ms |

- **O Chroma é 7 a 10 vezes mais rápido que hoje, mas 5 a 25 vezes mais lento que o numpy em cache.** Quem
  domina o tempo dele é o HTTP: serializar 1024 floats em JSON e fazer a ida e volta. A busca HNSW em si
  custa ~1 ms.
- **O Chroma vira uma segunda cópia para manter em sincronia.** A verdade da memória é o SQLite: texto,
  FTS5, sujeito e validade. Cada `guardar()` (inserir, fundir, `manter_novo` apaga), cada `auditar()`
  (reescreve, apaga, vencidos) e o `_completar_vetores()` teriam que espelhar no Chroma. Qualquer falha
  deixa os dois divergentes, e é por isso que o Odysseus reconstrói o índice inteiro depois de cada
  auditoria.
- **O Chroma acrescenta um ponto de falha.** Com o contêiner fora do ar, o `contexto()` perde a metade
  vetorial. O numpy só depende do próprio banco.
- **O Chroma só empata com o numpy por volta de 100 mil fatos.** É o mesmo ponto do FAISS HNSW, e a
  memória cresce ~4 mil fatos por ano.
- **O FAISS flat empata com o numpy** e traria uma dependência nova. O numpy já está no venv do inbox
  (`services/inbox/requirements.txt`), o único processo que importa a `lib_memoria` (`conversa.py` e
  `lib_ferramentas.py`).

O Chroma continua sendo o lugar certo para o RAG dos cofres, porque lá os documentos não têm outra casa.
Se um dia fizer sentido uma busca única "memória + cofres", a fusão pode ser feita em Python, juntando o
`contexto()` com o `lib_rag`, sem mudar onde a memória mora.

## Mudança

Arquivo principal: `services/_shared/lib_memoria.py`. O `guardar()`/`_parecido()` continua com `_cos` em
Python, porque compara só com os fatos do mesmo sujeito (dezenas).

### 1. Versão dos vetores no banco (gatilhos SQLite)

Em `_SCHEMA`, três gatilhos novos sobem um contador na tabela `estado` sempre que um vetor entra, sai ou
muda:

```sql
CREATE TRIGGER IF NOT EXISTS fatos_versao_ai AFTER INSERT ON fatos BEGIN
    INSERT INTO estado VALUES ('versao_vetores', 1)
    ON CONFLICT(chave) DO UPDATE SET valor = valor + 1;
END;
-- idem AFTER DELETE ON fatos, e AFTER UPDATE OF vetor, modelo_vetor, validade ON fatos
```

- **Gatilhos, não `PRAGMA data_version`:** o `contexto()` abre uma conexão nova a cada chamada, e o
  `data_version` só avisa de mudanças feitas por outras conexões durante a vida de uma mesma conexão.
- **Gatilhos pegam escrita de qualquer processo:** o CLI, o futuro timer de `auditar` e as ferramentas
  do chat.
- **Bancos já existentes ganham os gatilhos sozinhos:** o `_conectar()` já roda `executescript(_SCHEMA)`
  com `IF NOT EXISTS`.

### 2. Cache da matriz no processo

```python
_cache: dict[str, tuple] = {}   # caminho do banco → (versão, modelo, ids int64[n], matriz float32[n, d], expira int32[n])
_cache_lock = threading.Lock()  # o inbox atende requisições em várias threads
```

`_matriz(con, cfg)`:

- Lê `versao_vetores` do `estado`, uma linha só.
- Se a versão e o `modelo_embed` são os do cache, devolve o cache.
- Senão, recarrega, sob o lock:
  - `SELECT id, validade, vetor FROM fatos WHERE vetor IS NOT NULL AND modelo_vetor = ?`;
  - monta a matriz com `np.frombuffer(b"".join(blobs), np.float32).reshape(n, d)`, pulando blob de
    tamanho diferente;
  - monta `expira` como o ordinal da data de validade, com `permanente` valendo um ordinal máximo.
- O vencimento sai com uma máscara no momento da consulta, `expira >= hoje`. Assim, virar o dia não
  exige recarregar.
- Memória: 4 KB por fato (41 MB com 10 mil).

### 3. `contexto()` sem o SELECT de tudo

1. Checagem barata de banco vazio:
   `SELECT EXISTS(SELECT 1 FROM fatos WHERE validade = 'permanente' OR validade >= ?)`. Mantém o
   comportamento "banco vazio não chama o embedding".
2. Embedding da consulta, como hoje.
3. Ranking vetorial:
   - `sims = matriz @ q`;
   - fica o que está dentro da validade e com `sims >= limiar_contexto`;
   - ordena por (sim desc, id desc), o mesmo desempate do `sorted(..., reverse=True)` de hoje, com
     `np.lexsort`;
   - aplica o mesmo RRF `1/(60+pos)`.
4. FTS5 com a validade no próprio SQL, no lugar do filtro `r[0] in fatos`:
   ```sql
   SELECT f.id FROM fatos_fts JOIN fatos f ON f.id = fatos_fts.rowid
   WHERE fatos_fts MATCH ? AND (f.validade = 'permanente' OR f.validade >= ?)
   ORDER BY bm25(fatos_fts, 1.0, 2.0) LIMIT 50
   ```
5. Os top-k saem por `SELECT id, fato, sujeito FROM fatos WHERE id IN (...)`. O formato do bloco
   devolvido não muda.

Sem Ollama, o caminho continua o mesmo: só a FTS5.

### 4. Contexto por ação: filtro por sujeito

`contexto(consulta, k=8, sujeitos=None)`. Com `sujeitos`, a busca considera só os fatos dessas pessoas,
comparando pela `chave`, o slug do sujeito.

- **No cache:** guardar também `chaves`, um array com a chave de cada linha. O filtro vira mais uma
  máscara (`np.isin(chaves, alvo)`).
- **Na FTS5:** `AND f.chave IN (...)`.
- **Quem chama:**
  - O chat (`conversa.py`) continua sem filtro, porque pode perguntar sobre qualquer um.
  - O rascunho de resposta a um contato passa `sujeitos=[remetente, "Pedro"]`, assim como o briefing
    sobre uma pessoa.
  - Cada ação diz de quem precisa saber, e um fato parecido de outra pessoa não entra no prompt.
- **Teste:** com `sujeitos=["Carlos"]`, um fato de Ana mais parecido com a consulta fica de fora.

## Testes (`services/inbox/tests/test_memoria.py`)

- **Regressão:** a classe de testes do `contexto()` inteira passa sem mudança. Ela cobre ranking, `k`,
  nome do sujeito, nada relevante, banco vazio, vencido e "sem embedding, só FTS".
- **Novos:**
  - a segunda chamada reaproveita o cache: o SELECT dos vetores não roda de novo;
  - o cache cai depois de um `guardar()`, de um `auditar()` que apaga e de um UPDATE feito por outra
    conexão `sqlite3`, que simula o CLI ou o timer;
  - o fato que vence entre duas chamadas sai pela máscara, sem recarregar;
  - um vetor de outro `modelo_embed` fica de fora;
  - um banco criado com o schema antigo, sem os gatilhos, passa a ter os gatilhos depois do `_conectar()`.
- **Suíte inteira:** `cd services/inbox && venv/bin/python -m unittest discover -s tests -t .`

## Medida antes e depois

Acrescentar ao `docs/avaliacoes/scripts/memoria_busca.py` a coluna `contexto_cache_ms`, que é o
`contexto()` da segunda chamada em diante, e rodar com 1 mil, 10 mil e 100 mil fatos.

| fatos | meta |
|---|---|
| 1 mil | < 2 ms |
| 10 mil | < 5 ms |
| 100 mil | < 30 ms |

A FTS5 e o embedding da consulta (~0,04 s na CPU) ficam de fora da meta. O embedding já existe hoje.

## Documentação

- `services/_shared/about.md`, seção `lib_memoria.py`: trocar "cosseno em Python puro" por "numpy com a
  matriz em cache, invalidada por gatilhos (`estado.versao_vetores`)".
- A docstring do topo de `lib_memoria.py`: o trecho sobre armazenamento.

## Ordem e esforço (~2,5 h)

1. Gatilhos + teste do schema antigo.
2. `_matriz()` + cache + testes de invalidação.
3. `contexto()` novo + regressão + filtro `sujeitos` (seção 4).
4. Medida com `memoria_busca.py`.
5. Docs.
6. Commit e reinício do `alfred-inbox-api`. O reinício só com o OK de Pedro, porque é produção.

**Fora do escopo:** ChromaDB e FAISS para a memória; mudar o `guardar()`/`_parecido()`; a extração
automática, que é outro plano.
