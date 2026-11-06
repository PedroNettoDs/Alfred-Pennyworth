---
nome: containers-docker
descricao: Verificar os containers do Alfred (Evolution/WhatsApp, ChromaDB, SearXNG) quando algo deles falha.
gatilhos: ["o whatsapp parou", "o chromadb caiu", "o searxng não busca", "algum container reiniciando"]
ferramentas: [executar_comando]
comandos:
  - docker ps -a
  - docker logs --tail 50 <container>
  - docker stats --no-stream
perigosa: false
---
## Quando usar
Falha em WhatsApp (alfred-evolution), busca vetorial (alfred-chromadb) ou busca web (alfred-searxng).

## Passos
1. `docker ps -a`: status e há quanto tempo cada container está de pé; procure "Restarting" ou "Exited".
2. `docker logs --tail 50 <container>` do container suspeito.
3. Se parecer falta de memória: `docker stats --no-stream`.
4. Responda com o container, o erro e a sugestão (o executor não reinicia container).

## Cuidados
- Só leitura: nada de `docker restart`, `rm`, `compose up/down`.

## Verificação
- O estado citado vem do `docker ps -a` desta chamada.
