# Avaliação — Memória (Odysseus × Alfred)

Data: 03/11/2026 · Branch: `claude/avaliacao-memoria-309d9c` · Odysseus lido no commit `c21980f` (clone raso, só leitura)

Pergunta: quanto melhoraria o Alfred se ele adotasse a memória do Odysseus (extração automática a cada
conversa, auditoria periódica e índice vetorial), ou algo parecido?

## 1. O que o Odysseus faz

- **Extração automática** (`services/memory/memory_extractor.py`, `extract_and_store`):
  - Roda em segundo plano depois da resposta do chat, e só a cada 4 pares de mensagens
    (`routes/chat_helpers.py`, `_should_extract`). As chamadas entram numa fila sequencial para não
    disputar o backend com o chat.
  - Pega as 6 últimas mensagens e as achata numa transcrição única, porque mandar os papéis
    alternados fazia o modelo "continuar a conversa" e não extrair nada.
  - O prompt de sistema é em inglês e pede só fatos duráveis **sobre o usuário**: no máximo 2 por
    conversa, frases curtas e JSON com `text`, `category` e `evidence`.
  - **Filtro de evidência** (`_has_user_fact_evidence`): o fato só entra se `evidence` for uma citação
    literal de uma afirmação do usuário. Uma regex em inglês exige "I'm / I am / my X is…", e perguntas
    são recusadas. Com isso, a saída do assistente ou de ferramentas nunca vira fato.
  - **Candidatos por regex** (`_fallback_memory_candidates`): "my name is", "call me", "I live in",
    "I prefer/like/hate", "I want to travel to". Funcionam sem LLM e só em inglês.
- **Deduplicação na entrada**, sem LLM. Na ordem:
  1. vetor parecido com cosseno ≥ 0,72 (`find_similar`): o novo é descartado;
  2. "enriquecimento de nome": um nome mais completo substitui o mais curto;
  3. texto idêntico;
  4. Jaccard ≥ 0,6 sobre tokens de conteúdo, ou contenção (`_is_text_duplicate`).
- **Auditoria** (`audit_memories`):
  - Dispara a cada 5 fatos novos (contador em memória do processo) ou por botão
    (`routes/memory/memory_routes.py`).
  - Manda **todas** as memórias do dono numa chamada só, com um prompt "conservador": funde apenas
    duplicatas verdadeiras, apaga só o inútil e mantém o texto original. Usa `max_tokens` 16384 e
    timeout de 120 s.
  - Recusa um resultado que corte mais de 50% com 8 ou mais entradas.
  - Uma impressão digital sha256 num sidecar (`memory_tidy_state.json`) pula a auditoria quando nada mudou.
  - No fim, reconstrói o índice vetorial.
- **Armazenamento e busca**:
  - Os fatos ficam em `memory.json` (`src/memory.py`, `MemoryManager`, gravação atômica), sem sujeito
    e sem validade.
  - O índice vetorial **hoje é ChromaDB**, não FAISS (`src/memory_vector.py`, coleção com
    `hnsw:space=cosine` em `src/embedding_lanes.py`). O FAISS ficou para trás:
    `scripts/migrate_faiss_to_chroma.py` migra `data/memory_vectors/` (FAISS) para o Chroma.
  - O embedding padrão é `all-MiniLM-L6-v2` (`src/embeddings.py`, 384 dimensões, treinado em inglês).
    É para ele que o limiar de 0,72 foi ajustado.
  - Até 5 fatos de identidade ou contato são "fixados" e entram em todo prompt.

## 2. O que o Alfred faz hoje

- **Biblioteca já portada:** `services/_shared/lib_memoria.py`, com prompts em
  `services/_shared/prompts/memoria_{extrair,deduplicar,auditar}.md` e configuração em
  `services/_shared/perfis.yml`, seção `memoria:`.
- **`extrair(texto, fonte, sujeito)`**:
  - Um portão heurístico barato (`_vale_extrair`) evita chamar o LLM à toa.
  - O prompt é em português e o LLM responde com **JSON Schema** (o `format` do Ollama).
  - Cada fato vem como `{fato, sujeito, tipo, validade}`: guarda fatos de Pedro **e dos contatos**,
    com data de validade.
- **`guardar()`**:
  - Calcula o vetor `bge-m3` de "sujeito: fato" na CPU.
  - Compara só com fatos do **mesmo sujeito**: texto igual é descartado sem LLM; cosseno > 0,85 vai
    para o LLM, que decide `manter_novo`, `manter_antigo` ou `fundir`.
- **`auditar()`**:
  - Apaga os vencidos pela validade, sem LLM.
  - Manda ao LLM lotes de 60 fatos **por sujeito**.
  - Guarda a impressão digital na tabela `estado` e recusa corte de mais de 50%.
- **`contexto()`**:
  - Funde por RRF o vetor (cosseno ≥ 0,45, **calculado em Python puro** sobre os blobs) e o FTS5 (bm25).
  - Lê **todas** as linhas com o vetor a cada consulta.
- **Armazenamento:** SQLite com FTS5. Espelho legível, um `.md` por sujeito, no cofre.
- **Onde está ligada hoje:**
  - `services/inbox/conversa.py:393` chama `contexto()` em **todo turno** do chat.
  - As ferramentas `lembrar`, `buscar_memoria` e `listar_memoria` ficam em
    `services/_shared/lib_ferramentas.py:1117`.
- **O que falta em relação ao Odysseus:**
  - A extração só acontece quando o modelo chama `lembrar`. **Não há extração automática** depois de
    cada conversa, pendência ou e-mail.
  - **Não há agendamento de `auditar()`**: nenhuma unit ou timer chama a auditoria.
  - A memória de produção está vazia.

## 3. Método
