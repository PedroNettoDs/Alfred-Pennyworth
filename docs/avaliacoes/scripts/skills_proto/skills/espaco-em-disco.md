---
nome: espaco-em-disco
descricao: Achar o que ocupa espaço quando o disco está enchendo.
gatilhos: ["o disco está cheio", "quanto espaço livre eu tenho", "o que está ocupando o ssd"]
ferramentas: [status_disco, executar_comando]
comandos:
  - df -h / /home
  - du -sh <pasta>
  - docker system df
perigosa: false
---
## Quando usar
Pedro pergunta de espaço livre, disco cheio ou o que está ocupando o SSD.

## Passos
1. `status_disco` (df -h / /home): qual partição está acima de 85%.
2. `docker system df`: imagens, volumes e cache do Docker.
3. `du -sh` nas pastas grandes conhecidas: ~/.ollama/models, ~/dev, ~/Downloads.
4. Liste os 3 maiores e o que pode ser limpo. Não apague nada: só recomende.

## Cuidados
- `du` sem `-s` numa pasta grande demora: use `-sh`.

## Verificação
- Os tamanhos citados vêm das saídas desta chamada.
