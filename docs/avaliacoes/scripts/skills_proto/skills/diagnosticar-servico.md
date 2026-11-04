---
nome: diagnosticar-servico
descricao: Descobrir por que um serviço do Alfred (inbox, executor, researcher, laya) parou, está lento ou dá erro.
gatilhos: ["o inbox caiu", "o painel não abre", "o researcher está dando erro", "por que o alfred parou de responder"]
ferramentas: [executar_comando]
comandos:
  - systemctl --user is-active alfred-<servico>
  - systemctl --user status alfred-<servico> --no-pager
  - journalctl --user -u alfred-<servico> -n 50 --no-pager
  - curl -s http://127.0.0.1:<porta>/health
perigosa: false
---
## Quando usar
Pedro diz que um serviço do Alfred não responde, caiu, está lento ou devolve erro. Portas: executor 7071,
researcher 7072, inbox e Painel 7073, laya 7074.

## Passos
1. `systemctl --user is-active alfred-<servico>`: está ativo?
2. Se não estiver ativo ou se reiniciou há pouco: `systemctl --user status alfred-<servico> --no-pager`.
3. `journalctl --user -u alfred-<servico> -n 50 --no-pager`: procure Traceback, "Error", "Killed", timeout.
4. Se estiver ativo: `curl -s http://127.0.0.1:<porta>/health` para ver se a API responde.
5. Responda com a causa provável e o próximo passo. Reiniciar é com Pedro: o executor não reinicia serviços.

## Cuidados
- Sempre `--user`: as units do Alfred são de usuário.
- Não invente a saída. Se o executor recusar o comando, diga isso.

## Verificação
- A causa citada aparece no log ou no status.
