---
nome: resumo-da-manha
descricao: Resumo do dia para Pedro: agenda, pendências que esperam resposta e tarefas vencidas.
gatilhos: ["bom dia", "o que tenho hoje", "resumo do dia", "como está meu dia"]
ferramentas: [resumo_do_dia, ver_agenda, listar_pendencias, tarefas_vencidas]
perigosa: false
---
## Quando usar
Pedro pede o panorama do dia ou diz bom dia esperando um resumo.

## Passos
1. `ver_agenda` de hoje e amanhã.
2. `listar_pendencias` (padrão: aguardando).
3. `tarefas_vencidas`.
4. Se Pedro pedir o resumo pronto do Alfred, `resumo_do_dia`.
5. Responda em até 8 linhas: compromissos com hora, pendências mais antigas primeiro, tarefas vencidas.

## Cuidados
- Não responda pendência nem complete tarefa: só liste.

## Verificação
- Cada item citado veio de uma das ferramentas.
