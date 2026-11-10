"use strict";
const PADRAO = { urlAlfred: "http://127.0.0.1:7073", token: "" };

async function carregar() {
  const cfg = { ...PADRAO, ...(await messenger.storage.local.get(Object.keys(PADRAO))) };
  for (const c of Object.keys(PADRAO)) document.getElementById(c).value = cfg[c];
}

document.getElementById("salvar").addEventListener("click", async () => {
  const cfg = {};
  for (const c of Object.keys(PADRAO)) cfg[c] = document.getElementById(c).value.trim();
  await messenger.storage.local.set(cfg);
  document.getElementById("saida").textContent = "Salvo.";
});
carregar();
