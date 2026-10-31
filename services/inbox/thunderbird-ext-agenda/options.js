"use strict";
const CAMPOS = ["urlAlfred", "token", "calendario", "intervaloMin"];
const PADRAO = { urlAlfred: "http://127.0.0.1:7073", token: "", calendario: "Agenda", intervaloMin: 2 };
const $ = (id) => document.getElementById(id);

async function carregar() {
  const cfg = { ...PADRAO, ...(await messenger.storage.local.get(CAMPOS)) };
  $("urlAlfred").value = cfg.urlAlfred;
  $("token").value = cfg.token;
  $("intervaloMin").value = cfg.intervaloMin;
  const cals = await messenger.runtime.sendMessage({ tipo: "calendarios" });
  const sel = $("calendario");
  sel.innerHTML = "";
  for (const c of cals.filter((c) => !c.readOnly && !c.disabled)) {
    const o = document.createElement("option");
    o.value = c.name; o.textContent = `${c.name} (${c.type})`;
    if (c.name === cfg.calendario) o.selected = true;
    sel.appendChild(o);
  }
  const { ultimoCiclo } = await messenger.storage.local.get("ultimoCiclo");
  $("saida").textContent = ultimoCiclo ? JSON.stringify(ultimoCiclo, null, 2) : "";
}

$("salvar").addEventListener("click", async () => {
  await messenger.storage.local.set({
    urlAlfred: $("urlAlfred").value.trim(),
    token: $("token").value.trim(),
    calendario: $("calendario").value,
    intervaloMin: Number($("intervaloMin").value) || 2,
  });
  await messenger.runtime.sendMessage({ tipo: "reagendar" });
  $("saida").textContent = "Salvo.";
});
$("agora").addEventListener("click", async () => {
  $("saida").textContent = "Verificando…";
  const r = await messenger.runtime.sendMessage({ tipo: "verificar-agora" });
  $("saida").textContent = JSON.stringify(r, null, 2);
});
carregar();
