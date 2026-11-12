"use strict";

const element = (id) => document.getElementById(id);
const networkFields = element("networks");
const statusLabel = element("status");
const checkButton = element("check");
const startButton = element("start");
const stopButton = element("stop");
const journal = element("journal-events");
const copyButton = element("copy");
const journalLevel = element("journal-level");
const journalSearch = element("journal-search");
const follow = element("autoscroll");
const tabs = Array.from(document.querySelectorAll("[data-tab]"));
const settingsForm = element("settings-form");
const settingsList = element("settings-networks");
const settingsRpc = element("settings-rpc");
const settingsName = element("settings-network-name");
const settingsChainId = element("settings-chain-id");
const settingsStartBlock = element("settings-start-block");
const settingsPermit2 = element("settings-permit2");

const fragment = new URLSearchParams(location.hash.slice(1)).get("t");
const token = fragment || sessionStorage.getItem("revoker-token");
if (fragment) {
  sessionStorage.setItem("revoker-token", fragment);
  history.replaceState(null, "", location.pathname);
}

const states = {
  idle: ["Готов к работе", "warn", "готов"],
  running: ["Программа работает", "info", "работает"],
  stopping: ["Остановка…", "info", "остановка"],
  stopped: ["Остановлено пользователем", "warn", "готов"],
  completed: ["Выполнение завершено", "ok", "готов"],
  error: ["Выполнение завершилось с ошибкой", "danger", "ошибка"],
};

let state = "idle";
let connected = false;
let pending = false;
let activeRun = null;
let after = 0;
let entries = [];
let actionVersion = 0;
let activeTab = "home";
let networkChoicesKey = "";
let lastNetworkError = "";
let settingsSnapshot = null;
let settingsLoading = false;
let settingsNetworkId = null;

function moveIndicator() {
  const indicator = element("tab-ind");
  const tab = element(`tab-${activeTab}`);
  const width = tab.offsetWidth;
  if (!width) return;
  const radius = Math.min(indicator.firstElementChild.offsetWidth, width / 2);
  indicator.style.setProperty("--it", `${tab.offsetTop}px`);
  indicator.style.setProperty("--ih", `${tab.offsetHeight}px`);
  indicator.style.setProperty("--il", `${tab.offsetLeft}px`);
  indicator.style.setProperty("--im", `${tab.offsetLeft + radius - 1}px`);
  indicator.style.setProperty("--is", String(Math.max(0, (width - 2 * radius + 2) / 10)));
  indicator.style.setProperty("--irx", `${tab.offsetLeft + width - radius}px`);
  indicator.classList.add("is-placed");
  requestAnimationFrame(() => indicator.classList.add("is-ready"));
}

function selectTab(name) {
  activeTab = name;
  sessionStorage.setItem("revoker-tab", name);
  for (const tab of tabs) {
    const selected = tab.dataset.tab === name;
    tab.setAttribute("aria-selected", String(selected));
    tab.tabIndex = selected ? 0 : -1;
    const panel = element(tab.getAttribute("aria-controls"));
    panel.hidden = !selected;
    panel.classList.toggle("is-entering", selected);
  }
  moveIndicator();
  if (name === "journal") renderJournal();
  if (name === "settings" && !settingsSnapshot) loadNetworkSettings();
  if (name !== "settings" && settingsRpc.type !== "password") setRpcVisibility(false);
}

tabs.forEach((tab, index) => {
  tab.addEventListener("click", () => selectTab(tab.dataset.tab));
  tab.addEventListener("keydown", (event) => {
    const target = {
      Home: 0,
      End: tabs.length - 1,
      ArrowLeft: (index + tabs.length - 1) % tabs.length,
      ArrowRight: (index + 1) % tabs.length,
    }[event.key];
    if (target === undefined) return;
    event.preventDefault();
    selectTab(tabs[target].dataset.tab);
    tabs[target].focus();
  });
});
window.addEventListener("resize", moveIndicator);
document.addEventListener("visibilitychange", () => {
  document.documentElement.classList.toggle("is-idle", document.hidden);
});

function selectedNetworks() {
  return Array.from(networkFields.querySelectorAll("input:checked"), (input) => input.value);
}

function runActive() {
  return state === "running" || state === "stopping";
}

function refreshControls() {
  const active = runActive();
  element("run-settings").disabled = active || pending || !connected;
  startButton.hidden = checkButton.hidden = active;
  startButton.disabled = checkButton.disabled = active || pending || !connected || selectedNetworks().length === 0;
  stopButton.hidden = !active;
  stopButton.disabled = state === "stopping" || pending || !connected;
  const [label, tone, brand] = connected ? states[state] : ["Нет подключения", "danger", "нет связи"];
  if (statusLabel.textContent !== label) statusLabel.textContent = label;
  element("status-card").className = `card status-card tone-${tone}${active ? " ui-running" : ""}`;
  element("status-indicator").className = active ? "spinner" : "status-orb";
  const badge = element("brand-state");
  badge.className = `brand-state tone-${tone}${active ? " ui-running" : ""}`;
  badge.textContent = brand;
  const dot = element("journal-dot");
  if (dot.hidden === active) {
    dot.hidden = !active;
    moveIndicator();
  }
  const settingsLocked = !connected || active || pending || settingsLoading || !settingsSnapshot;
  element("settings-edit-fields").disabled = settingsLocked;
  element("settings-add").disabled = settingsLocked;
  element("settings-reload").disabled = pending || settingsLoading;
  element("settings-locked").hidden = !active;
}

function showMessage(text = "") {
  element("message").textContent = text;
  element("message").hidden = !text;
}

async function api(path, body) {
  const headers = { Authorization: `Bearer ${token}` };
  const options = { headers, cache: "no-store" };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    options.method = "POST";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  const payload = await response.json();
  if (!response.ok) {
    const error = new Error(payload.error || "Ошибка локального сервера.");
    error.status = response.status;
    throw error;
  }
  return payload;
}

function updateNetworkChoices(networks) {
  const key = JSON.stringify(networks.map(({ id, name }) => [id, name]));
  if (key === networkChoicesKey) return;
  networkChoicesKey = key;
  const checked = new Map(Array.from(networkFields.querySelectorAll("input"), (input) => [input.value, input.checked]));
  const rows = document.createDocumentFragment();
  for (const network of networks) {
    const label = document.createElement("label");
    label.className = "row";
    const name = document.createElement("span");
    name.className = "row-main";
    name.textContent = network.name;
    const control = document.createElement("span");
    control.className = "switch";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.value = network.id;
    input.checked = checked.get(network.id) ?? true;
    control.append(input);
    label.append(name, control);
    rows.append(label);
  }
  networkFields.replaceChildren(rows);
}

function updateView(snapshot) {
  updateNetworkChoices(snapshot.networks);
  const networkError = snapshot.network_error || "";
  if (networkError !== lastNetworkError || (!connected && networkError)) {
    lastNetworkError = networkError;
    showMessage(networkError);
  }

  const newRun = activeRun !== snapshot.run_id;
  if (newRun) {
    activeRun = snapshot.run_id;
    after = 0;
    entries = [];
  }
  const newLines = snapshot.lines.filter((line) => line.id > after);
  if (newLines.length) {
    entries.push(...newLines);
    after = newLines.at(-1).id;
  }
  if (newRun || newLines.length) {
    entries = entries.slice(-5000);
    if (activeTab === "journal") renderJournal();
  }
  updateStatusMeta(snapshot);
  state = snapshot.status;
  connected = true;
  refreshControls();
}

function showSettingsMessage(text = "", tone = "danger") {
  const banner = element("settings-message");
  banner.className = `banner tone-${tone}`;
  banner.setAttribute("role", tone === "danger" ? "alert" : "status");
  banner.textContent = text;
  banner.hidden = !text;
}

function currentNetwork() {
  return settingsSnapshot?.networks.find((network) => network.id === settingsNetworkId);
}

function setRpcVisibility(visible) {
  const button = element("settings-reveal-rpc");
  settingsRpc.type = visible ? "text" : "password";
  button.setAttribute("aria-pressed", String(visible));
  button.textContent = visible ? "Скрыть" : "Показать";
}

function selectSettingsNetwork(id) {
  settingsNetworkId = id;
  const network = currentNetwork();
  const builtin = network?.builtin ?? false;
  for (const option of settingsList.querySelectorAll("[data-network-id]")) {
    option.setAttribute("aria-pressed", String(option.dataset.networkId === id));
  }
  element("settings-editor").hidden = false;
  element("settings-editor-title").textContent = network?.name || "Новая сеть";
  const chainMeta = element("settings-chain-meta");
  chainMeta.textContent = builtin ? `Chain ID ${network.chain_id}` : "";
  chainMeta.hidden = !builtin;
  const envNote = element("settings-env-note");
  envNote.hidden = !network?.env_override;
  envNote.textContent = network?.env_override ? `Сейчас используется RPC из REVOKER_RPC_${network.id.toUpperCase()}.` : "";
  element("settings-custom-fields").hidden = builtin;
  const advanced = element("settings-advanced");
  advanced.hidden = builtin;
  advanced.open = false;
  settingsName.disabled = settingsChainId.disabled = settingsStartBlock.disabled = settingsPermit2.disabled = builtin;
  settingsName.value = builtin ? "" : network?.name || "";
  settingsChainId.value = builtin ? "" : network?.chain_id ?? "";
  settingsStartBlock.value = builtin ? "" : network?.start_block ?? "0";
  settingsPermit2.value = builtin ? "" : network?.permit2 || "";
  settingsRpc.value = network?.rpc || "";
  setRpcVisibility(false);
  element("settings-reset").hidden = !builtin || !Object.hasOwn(settingsSnapshot.settings, id);
  element("settings-delete").hidden = !network || builtin;
}

function applySettingsSnapshot(snapshot, selectedId) {
  settingsSnapshot = snapshot;
  const options = document.createDocumentFragment();
  for (const network of snapshot.networks) {
    const option = document.createElement("button");
    option.type = "button";
    option.className = "settings-network-option";
    option.dataset.networkId = network.id;
    const name = document.createElement("span");
    name.textContent = network.name;
    const id = document.createElement("span");
    id.className = "row-sub";
    id.textContent = `ID ${network.chain_id}`;
    option.append(name, id);
    option.addEventListener("click", () => {
      selectSettingsNetwork(network.id);
      element("settings-editor").scrollIntoView({ block: "nearest" });
    });
    options.append(option);
  }
  settingsList.replaceChildren(options);
  const selected = snapshot.networks.find(({ id }) => id === selectedId) || snapshot.networks[0];
  selectSettingsNetwork(selected?.id ?? null);
  updateNetworkChoices(snapshot.networks);
}

async function loadNetworkSettings() {
  if (!token || settingsLoading || pending) return;
  settingsLoading = true;
  element("settings-loading").hidden = false;
  element("settings-reload").hidden = true;
  showSettingsMessage();
  refreshControls();
  try {
    applySettingsSnapshot(await api("/api/networks/settings"), settingsNetworkId);
  } catch (error) {
    showSettingsMessage(error.message);
    element("settings-reload").hidden = false;
  } finally {
    settingsLoading = false;
    element("settings-loading").hidden = true;
    refreshControls();
  }
}

async function saveNetworkSettings(settings, selectedId) {
  if (element("settings-edit-fields").disabled) return;
  actionVersion += 1;
  pending = true;
  showSettingsMessage();
  refreshControls();
  try {
    const snapshot = await api("/api/networks/settings", { settings, revision: settingsSnapshot.revision });
    applySettingsSnapshot(snapshot, selectedId);
    element("settings-reload").hidden = true;
    showSettingsMessage("Настройки сохранены.", "ok");
  } catch (error) {
    showSettingsMessage(error.message);
    if (error.status === 409) element("settings-reload").hidden = false;
  } finally {
    pending = false;
    refreshControls();
  }
}

function removeNetworkSetting() {
  if (!settingsNetworkId) return;
  const settings = { ...settingsSnapshot.settings };
  delete settings[settingsNetworkId];
  saveNetworkSettings(settings, settingsNetworkId);
}

function customNetworkId(chainId) {
  const ids = new Set(settingsSnapshot.networks.map((network) => network.id));
  let id = `chain_${chainId}`;
  for (let suffix = 2; ids.has(id); suffix += 1) id = `chain_${chainId}_${suffix}`;
  return id;
}

function submitNetworkSettings(event) {
  event.preventDefault();
  if (element("settings-edit-fields").disabled) return;
  const network = currentNetwork();
  const settings = { ...settingsSnapshot.settings };
  const rpc = settingsRpc.value.trim();
  if (network?.builtin) {
    if (rpc === network.default_rpc) delete settings[network.id];
    else settings[network.id] = { rpc };
    saveNetworkSettings(settings, network.id);
    return;
  }
  const name = settingsName.value.trim();
  const chain_id = Number(settingsChainId.value.trim());
  const start_block = Number(settingsStartBlock.value.trim() || "0");
  if (!name || !Number.isSafeInteger(chain_id) || chain_id < 1 || !Number.isSafeInteger(start_block) || start_block < 0) {
    showSettingsMessage("Укажите название, положительный Chain ID и начальный блок от 0.");
    return;
  }
  if (settingsSnapshot.networks.some((item) => item.chain_id === chain_id && item.id !== network?.id)) {
    showSettingsMessage("Сеть с таким Chain ID уже есть.");
    settingsChainId.focus();
    return;
  }
  const id = network?.id || customNetworkId(chain_id);
  settings[id] = { name, chain_id, rpc, start_block, permit2: settingsPermit2.value.trim() || null };
  saveNetworkSettings(settings, id);
}

function updateStatusMeta(snapshot) {
  const startedAt = snapshot.started_at && new Date(snapshot.started_at).toLocaleString("ru-RU", {
    day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
  const details = {
    "status-mode": typeof snapshot.dry_run === "boolean" ? (snapshot.dry_run ? "Проверка" : "Отзыв аппрувов") : "",
    "status-started": startedAt ? `Запущено: ${startedAt}` : "",
    "status-exit": Number.isInteger(snapshot.exit_code) ? `Код завершения: ${snapshot.exit_code}` : "",
  };
  for (const [id, text] of Object.entries(details)) {
    const field = element(id);
    field.hidden = !text;
    field.textContent = text;
  }
}

function filteredEntries() {
  const needle = journalSearch.value.trim().toLowerCase();
  return entries.filter((entry) => (journalLevel.value === "ALL" || entry.level === journalLevel.value)
    && entry.text.toLowerCase().includes(needle));
}

function renderJournal() {
  const position = journal.scrollTop;
  const filtered = filteredEntries();
  const fragment = document.createDocumentFragment();
  for (const entry of filtered) {
    const row = document.createElement("div");
    row.className = "event";
    const level = entry.level || "INFO";
    for (const [className, text] of [
      ["event-ts", entry.time || "—"],
      [`event-level lv-${level.toLowerCase()}`, level],
      ["event-msg", entry.text],
    ]) {
      const field = document.createElement("span");
      field.className = className;
      field.textContent = text;
      row.append(field);
    }
    fragment.append(row);
  }
  if (!filtered.length) {
    const empty = document.createElement("p");
    empty.className = "events-empty";
    empty.textContent = entries.length ? "Подходящих сообщений нет." : "Сообщений пока нет.";
    fragment.append(empty);
  }
  journal.replaceChildren(fragment);
  journal.scrollTop = follow.checked ? journal.scrollHeight : position;
  copyButton.disabled = !filtered.length;
}

async function poll() {
  const version = actionVersion;
  try {
    if (!pending) {
      const snapshot = await api(`/api/status?after=${after}`);
      if (!pending && version === actionVersion) {
        if (!connected) showMessage();
        updateView(snapshot);
      }
    }
  } catch {
    if (version === actionVersion) {
      connected = false;
      showMessage("Связь с программой потеряна. Проверьте, запущен ли ui.py.");
      refreshControls();
    }
  } finally {
    setTimeout(poll, 900);
  }
}

async function runAction(path, body) {
  actionVersion += 1;
  pending = true;
  showMessage();
  refreshControls();
  try {
    updateView(await api(path, body));
  } catch (error) {
    showMessage(error.message);
  } finally {
    pending = false;
    refreshControls();
    if (activeTab === "settings" && !settingsSnapshot) loadNetworkSettings();
  }
}

networkFields.addEventListener("change", refreshControls);
checkButton.addEventListener("click", () => runAction("/api/start", { networks: selectedNetworks(), dry_run: true }));
startButton.addEventListener("click", () => runAction("/api/start", { networks: selectedNetworks(), dry_run: false }));
stopButton.addEventListener("click", () => runAction("/api/stop", {}));
element("settings-reload").addEventListener("click", loadNetworkSettings);
element("settings-add").addEventListener("click", () => {
  showSettingsMessage();
  selectSettingsNetwork(null);
  settingsName.focus();
});
element("settings-reveal-rpc").addEventListener("click", () => setRpcVisibility(settingsRpc.type === "password"));
element("settings-reset").addEventListener("click", removeNetworkSetting);
element("settings-delete").addEventListener("click", removeNetworkSetting);
settingsForm.addEventListener("submit", submitNetworkSettings);
settingsForm.addEventListener("invalid", (event) => {
  if (event.target === settingsStartBlock || event.target === settingsPermit2) element("settings-advanced").open = true;
}, true);
journalLevel.addEventListener("change", renderJournal);
journalSearch.addEventListener("input", renderJournal);
follow.addEventListener("change", () => { if (follow.checked) journal.scrollTop = journal.scrollHeight; });
copyButton.addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(filteredEntries().map((entry) => entry.text).join("\n"));
    copyButton.textContent = "Скопировано";
    setTimeout(() => { copyButton.textContent = "Копировать"; }, 1300);
  } catch {
    showMessage("Не удалось скопировать журнал.");
  }
});

const savedTab = sessionStorage.getItem("revoker-tab");
selectTab(tabs.some((tab) => tab.dataset.tab === savedTab) ? savedTab : "home");
if (token) {
  poll();
} else {
  showMessage("Откройте интерфейс командой python ui.py.");
  refreshControls();
}
