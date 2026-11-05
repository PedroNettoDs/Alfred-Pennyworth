#!/usr/bin/env bash
# Entrypoint da imagem única hipotética: sobe os 4 serviços HTTP pelos run.sh de sempre e cai se
# qualquer um cair (o restart fica com o Docker). Os timers do systemd (inbox, semanal, vigia,
# briefing, backup, rag) NÃO têm equivalente aqui — precisariam de um agendador no container.
cd /opt/alfred/services
shell-executor/run.sh & researcher/run.sh & laya/run.sh & inbox/run.sh serve &
wait -n
exit 1
