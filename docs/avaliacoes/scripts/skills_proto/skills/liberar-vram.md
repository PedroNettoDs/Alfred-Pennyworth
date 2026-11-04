---
nome: liberar-vram
descricao: Descarregar um modelo do Ollama para liberar a GPU.
gatilhos: ["libera a gpu", "descarrega o modelo", "tira o qwen da vram"]
ferramentas: [executar_comando]
comandos:
  - ollama ps
  - curl -s -X POST http://localhost:11434/api/generate -d '{"model": "<modelo>", "keep_alive": 0}'
perigosa: true
---
## Quando usar
Pedro quer liberar VRAM tirando um modelo carregado.

## Passos
1. `ollama ps`: qual modelo está carregado.
2. `curl -s -X POST http://localhost:11434/api/generate -d '{"model": "<modelo>", "keep_alive": 0}'`.
3. `ollama ps` de novo para confirmar.

## Cuidados
- Escreve no Ollama de produção: só com aprovação.

## Verificação
- O modelo sumiu do `ollama ps`.
