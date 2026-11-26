/*
 * SHYS Barcode Scanner – Seitenleisten-Panel für Home Assistant.
 * Eigenständige Web Component ohne Build-Schritt und ohne externe Abhängigkeiten.
 */
const DOMAIN = "shys_barcode_scanner";
const WATCHED = [
  `sensor.${DOMAIN}_last_scan`,
  `sensor.${DOMAIN}_known_products`,
  `sensor.${DOMAIN}_unknown_count`,
];
const SOURCE_LABELS = {
  openfoodfacts: "Open Food Facts",
  "ean-suche": "ean-suche.net",
  opengtindb: "OpengtinDB",
  manual: "Eigener Eintrag",
  import: "Import",
};
const STATUS_LABELS = {
  ok: "Auf der Liste",
  duplicate: "Stand schon auf der Liste",
  unknown: "Unbekannt – bitte anlernen",
  error: "Fehler",
};

// Mini-Icon (Strichcode + gelber Scan-Strahl) in den SmartHome-yourself-Farben
const LOGO_SVG = `<svg class="logo" viewBox="0 0 48 48" aria-hidden="true">
  <rect x="1.5" y="1.5" width="45" height="45" rx="10" fill="#002039" stroke="#2E6EB4" stroke-width="2"/>
  <path d="M10 15v-3a2 2 0 0 1 2-2h3M33 10h3a2 2 0 0 1 2 2v3M38 33v3a2 2 0 0 1-2 2h-3M15 38h-3a2 2 0 0 1-2-2v-3" fill="none" stroke="#00AEE7" stroke-width="2" stroke-linecap="round"/>
  <g fill="#fff"><rect x="14" y="15" width="2" height="18"/><rect x="18" y="15" width="1" height="18"/><rect x="25" y="15" width="1" height="18"/><rect x="28" y="15" width="3" height="18"/><rect x="33" y="15" width="1" height="18"/></g>
  <g fill="#00AEE7"><rect x="21" y="15" width="2.5" height="18"/></g>
  <rect x="10" y="23" width="28" height="2.4" rx="1.2" fill="#F9B013"/>
</svg>`;

function esc(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function fmtDate(iso) {
  if (!iso) return "–";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "–";
  return d.toLocaleString("de-DE", { dateStyle: "short", timeStyle: "short" });
}

class ShysBarcodePanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._narrow = false;
    this._data = { products: [], pending: [], last_result: null, todo_entity: "" };
    this._tab = "pending";
    this._search = "";
    this._editing = null;
    this._message = null;
    this._loaded = false;
    this._loading = false;
    this._stamp = "";
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    this.toggleAttribute("dark", !!(hass.themes && hass.themes.darkMode));
    if (this._menuButton) this._menuButton.hass = hass;
    const stamp = WATCHED.map((id) => hass.states[id]?.last_updated || "").join("|");
    if (first) {
      this._render();
      this._load();
    } else if (stamp !== this._stamp && this._loaded) {
      this._load();
    }
    this._stamp = stamp;
  }

  get hass() {
    return this._hass;
  }

  set narrow(value) {
    this._narrow = value;
    if (this._menuButton) this._menuButton.narrow = value;
  }

  set panel(_value) {}

  get _todoLists() {
    if (!this._hass) return [];
    return Object.keys(this._hass.states)
      .filter((id) => id.startsWith("todo."))
      .map((id) => ({ id, name: this._hass.states[id].attributes.friendly_name || id }))
      .sort((a, b) => a.name.localeCompare(b.name, "de"));
  }

  async _ws(type, payload = {}) {
    return this._hass.callWS({ type: `${DOMAIN}/${type}`, ...payload });
  }

  async _load() {
    if (!this._hass || this._loading) return;
    this._loading = true;
    try {
      this._data = await this._ws("get_state");
      this._loaded = true;
    } catch (err) {
      this._flash(`Daten konnten nicht geladen werden: ${err.message || err}`, true);
    } finally {
      this._loading = false;
    }
    this._renderContent();
  }

  _flash(text, error = false) {
    this._message = { text, error };
    this._renderContent();
    clearTimeout(this._flashTimer);
    this._flashTimer = setTimeout(() => {
      this._message = null;
      this._renderContent();
    }, 5000);
  }

  // ------------------------------------------------------------ Rendering
  _render() {
    this.shadowRoot.innerHTML = `
      <style>
        :host {
          --shys-gelb: #F9B013; --shys-hellblau: #00AEE7; --shys-blau: #2E6EB4; --shys-dunkelblau: #002039;
          --shys-accent: var(--shys-blau);
          display: block; min-height: 100vh; background: var(--primary-background-color); color: var(--primary-text-color);
          font-family: var(--paper-font-body1_-_font-family, Roboto, sans-serif);
        }
        :host([dark]) { --shys-accent: var(--shys-hellblau); }
        .toolbar { display: flex; align-items: center; min-height: var(--header-height, 56px); padding: 6px 12px; gap: 10px; box-sizing: border-box;
          background: linear-gradient(135deg, #062d52 0%, var(--shys-dunkelblau) 70%); color: #fff; border-bottom: 4px solid var(--shys-gelb);
          --app-header-text-color: #fff; --mdc-icon-button-ink-color: #fff; }
        .toolbar svg.logo { width: 38px; height: 38px; flex: none; display: block; }
        .titles { display: flex; flex-direction: column; line-height: 1.15; min-width: 0; }
        .title { font-family: "Roboto Condensed", var(--paper-font-body1_-_font-family, Roboto), sans-serif; font-weight: 700; font-size: 20px; letter-spacing: .3px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .title b { color: var(--shys-gelb); font-weight: 700; }
        .subtitle { font-size: 12px; color: var(--shys-hellblau); letter-spacing: .4px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .wrap { max-width: 980px; margin: 0 auto; padding: 16px; box-sizing: border-box; }
        .tabs { display: flex; gap: 8px; margin-bottom: 16px; flex-wrap: wrap; }
        .tab { border: 1px solid var(--divider-color); background: var(--card-background-color); color: var(--primary-text-color); border-radius: 18px; padding: 8px 16px; cursor: pointer; font-size: 14px; font-weight: 500; transition: border-color .15s, background .15s; }
        .tab:hover { border-color: var(--shys-hellblau); }
        .tab.active { background: var(--shys-blau); color: #fff; border-color: var(--shys-blau); box-shadow: inset 0 -3px 0 var(--shys-hellblau); }
        .badge { display: inline-block; min-width: 18px; padding: 0 6px; margin-left: 6px; border-radius: 9px; background: var(--shys-gelb); color: var(--shys-dunkelblau); font-size: 12px; font-weight: 700; text-align: center; }
        .card { background: var(--card-background-color); border-radius: var(--ha-card-border-radius, 12px); box-shadow: var(--ha-card-box-shadow, none); border: 1px solid var(--divider-color); border-top: 3px solid var(--shys-blau); padding: 16px; margin-bottom: 12px; }
        h2 { font-size: 18px; margin: 0 0 12px; font-weight: 500; display: flex; align-items: center; gap: 8px; }
        h2::before { content: ""; width: 4px; height: 18px; border-radius: 2px; background: var(--shys-gelb); flex: none; }
        .muted { color: var(--secondary-text-color); font-size: 13px; }
        .row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
        .grow { flex: 1 1 180px; min-width: 0; }
        input, select, textarea { font: inherit; padding: 8px 10px; border-radius: 6px; border: 1px solid var(--divider-color); background: var(--secondary-background-color, var(--card-background-color)); color: var(--primary-text-color); box-sizing: border-box; width: 100%; }
        input:focus, select:focus, textarea:focus { outline: none; border-color: var(--shys-hellblau); box-shadow: 0 0 0 2px rgba(0, 174, 231, .25); }
        textarea { min-height: 120px; font-family: monospace; font-size: 12px; }
        button.btn { font: inherit; font-size: 14px; font-weight: 500; border: none; border-radius: 6px; padding: 8px 14px; cursor: pointer; background: var(--shys-blau); color: #fff; white-space: nowrap; transition: background .15s; }
        button.btn:hover:not(:disabled) { background: #00A0D6; }
        button.btn.secondary { background: transparent; color: var(--shys-accent); border: 1px solid var(--shys-accent); }
        button.btn.secondary:hover:not(:disabled) { background: rgba(0, 174, 231, .12); }
        button.btn.danger { background: transparent; color: var(--error-color, #db4437); border: 1px solid var(--error-color, #db4437); }
        button.btn.danger:hover:not(:disabled) { background: rgba(219, 68, 55, .1); }
        button.btn:disabled { opacity: .5; cursor: default; }
        .code { font-family: monospace; font-size: 15px; font-weight: 600; word-break: break-all; }
        .src { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 12px; background: rgba(0, 174, 231, .14); color: var(--primary-text-color); border: 1px solid rgba(0, 174, 231, .35); }
        .src.hl { background: var(--shys-gelb); color: var(--shys-dunkelblau); border-color: var(--shys-gelb); font-weight: 700; }
        .product { display: grid; grid-template-columns: 1fr auto; gap: 8px; padding: 12px 0; border-bottom: 1px solid var(--divider-color); }
        .product:last-child { border-bottom: none; }
        .pname { font-weight: 500; }
        .msg { padding: 10px 14px; border-radius: 6px; margin-bottom: 12px; background: var(--success-color, #43a047); color: #fff; border-left: 5px solid var(--shys-gelb); }
        .msg.error { background: var(--error-color, #db4437); }
        .empty { text-align: center; padding: 24px; color: var(--secondary-text-color); }
        label.check { display: flex; gap: 6px; align-items: center; font-size: 14px; }
        label.check input { width: auto; accent-color: var(--shys-blau); }
        .actions { display: flex; gap: 6px; flex-wrap: wrap; justify-content: flex-end; align-items: flex-start; }
        @media (max-width: 600px) { .product { grid-template-columns: 1fr; } .actions { justify-content: flex-start; } .subtitle { display: none; } }
      </style>
      <div class="toolbar"><span id="menu"></span>${LOGO_SVG}
        <div class="titles"><span class="title"><b>SHYS</b> Barcode Scanner</span>
        <span class="subtitle">SmartHome yourself · Home Assistant Integration</span></div></div>
      <div class="wrap" id="content"></div>`;
    try {
      const btn = document.createElement("ha-menu-button");
      btn.hass = this._hass;
      btn.narrow = this._narrow;
      this.shadowRoot.getElementById("menu").appendChild(btn);
      this._menuButton = btn;
    } catch (_e) {
      /* außerhalb von HA egal */
    }
    const content = this.shadowRoot.getElementById("content");
    content.addEventListener("click", (ev) => this._onClick(ev));
    content.addEventListener("input", (ev) => this._onInput(ev));
    content.addEventListener("change", (ev) => this._onChange(ev));
    this._renderContent();
  }

  _todoOptions(selected, withDefault = true) {
    const opts = withDefault
      ? [`<option value="">Standard (${esc(this._data.todo_entity || "")})</option>`]
      : [];
    for (const t of this._todoLists) {
      opts.push(`<option value="${esc(t.id)}" ${t.id === selected ? "selected" : ""}>${esc(t.name)}</option>`);
    }
    return opts.join("");
  }

  _renderContent() {
    const content = this.shadowRoot && this.shadowRoot.getElementById("content");
    if (!content) return;
    // Fokus und Eingaben beim Neuzeichnen erhalten
    const active = this.shadowRoot.activeElement;
    const focusKey = active && active.dataset ? active.dataset.key : null;
    const saved = {};
    content.querySelectorAll("[data-key]").forEach((el) => {
      if ("value" in el && el.type !== "file") saved[el.dataset.key] = el.type === "checkbox" ? el.checked : el.value;
    });

    const pendingCount = (this._data.pending || []).length;
    const tabs = [
      ["pending", `Unbekannte Scans${pendingCount ? `<span class="badge">${pendingCount}</span>` : ""}`],
      ["products", `Produktdatenbank (${(this._data.products || []).length})`],
      ["tools", "Test &amp; Backup"],
    ];
    let html = "";
    if (this._message) html += `<div class="msg ${this._message.error ? "error" : ""}">${esc(this._message.text)}</div>`;
    html += `<div class="tabs">${tabs
      .map(([id, label]) => `<button class="tab ${this._tab === id ? "active" : ""}" data-action="tab" data-tab="${id}">${label}</button>`)
      .join("")}</div>`;
    if (!this._loaded) html += `<div class="card empty">Lade …</div>`;
    else if (this._tab === "pending") html += this._renderPending();
    else if (this._tab === "products") html += this._renderProducts();
    else html += this._renderTools();
    content.innerHTML = html;

    content.querySelectorAll("[data-key]").forEach((el) => {
      const key = el.dataset.key;
      if (key in saved && el.type !== "file") {
        if (el.type === "checkbox") el.checked = saved[key];
        else if (el.tagName !== "SELECT" || saved[key] !== "") el.value = saved[key];
      }
    });
    if (focusKey) {
      const el = content.querySelector(`[data-key="${CSS.escape(focusKey)}"]`);
      if (el) {
        el.focus();
        if (typeof el.setSelectionRange === "function" && el.type === "text") {
          const len = el.value.length;
          el.setSelectionRange(len, len);
        }
      }
    }
  }

  _renderLast() {
    const r = this._data.last_result;
    if (!r) return "";
    return `<div class="card"><div class="muted">Letzter Scan · ${esc(fmtDate(r.timestamp))}</div>
      <div class="row" style="margin-top:6px"><span class="code">${esc(r.ean)}</span>
      <span class="grow">${esc(r.name || "")}</span><span class="src ${r.status === "ok" ? "" : "hl"}">${esc(STATUS_LABELS[r.status] || r.status)}</span></div></div>`;
  }

  _renderPending() {
    const pending = this._data.pending || [];
    let html = this._renderLast();
    if (!pending.length) {
      return html + `<div class="card empty">Keine unbekannten Scans. Alles angelernt 🎉</div>`;
    }
    html += `<div class="card"><h2>Unbekannte Scans</h2><div class="muted">Einmal einen Namen vergeben – ab dem nächsten Scan kennt Home Assistant das Produkt.</div>`;
    for (const p of pending) {
      const k = esc(p.ean);
      html += `<div class="product">
        <div>
          <div class="code">${k}</div>
          <div class="muted">${p.scan_count}× gescannt · zuletzt ${esc(fmtDate(p.last_seen))}${p.device ? ` · ${esc(p.device)}` : ""}</div>
          <div class="row" style="margin-top:8px">
            <input type="text" class="grow" data-key="pname-${k}" placeholder="Name (z. B. Milch 1,5 %)" />
            <input type="text" class="grow" data-key="pbrand-${k}" placeholder="Marke (optional)" />
            <select class="grow" data-key="plist-${k}">${this._todoOptions("")}</select>
          </div>
        </div>
        <div class="actions">
          <button class="btn" data-action="learn-pending" data-ean="${k}">Speichern &amp; auf Liste</button>
          <button class="btn secondary" data-action="learn-pending-only" data-ean="${k}">Nur speichern</button>
          <button class="btn danger" data-action="dismiss" data-ean="${k}">Verwerfen</button>
        </div>
      </div>`;
    }
    return html + `</div>`;
  }

  _renderProducts() {
    const q = this._search.trim().toLowerCase();
    const products = (this._data.products || []).filter(
      (p) => !q || [p.ean, p.name, p.brand].some((v) => String(v || "").toLowerCase().includes(q)),
    );
    let html = `<div class="card"><h2>+ Eigenen Code anlegen</h2>
      <div class="muted">Für lose Ware, selbst gedruckte QR-/EAN-Sticker oder Produkte ohne Treffer.</div>
      <div class="row" style="margin-top:8px">
        <input type="text" class="grow" data-key="new-ean" placeholder="Code (EAN, QR-Inhalt …)" />
        <input type="text" class="grow" data-key="new-name" placeholder="Name" />
        <input type="text" class="grow" data-key="new-brand" placeholder="Marke (optional)" />
        <select class="grow" data-key="new-list">${this._todoOptions("")}</select>
        <button class="btn" data-action="create">Anlegen</button>
      </div></div>`;
    html += `<div class="card"><div class="row"><input type="text" class="grow" data-key="search" placeholder="Suchen (Name, Marke, Code)" value="${esc(this._search)}" /></div>`;
    if (!products.length) {
      html += `<div class="empty">${q ? "Nichts gefunden." : "Noch keine Produkte gespeichert. Einfach scannen!"}</div>`;
    }
    for (const p of products) {
      const k = esc(p.ean);
      if (this._editing === p.ean) {
        html += `<div class="product"><div>
          <div class="code">${k}</div>
          <div class="row" style="margin-top:8px">
            <input type="text" class="grow" data-key="ename-${k}" value="${esc(p.name)}" placeholder="Name" />
            <input type="text" class="grow" data-key="ebrand-${k}" value="${esc(p.brand || "")}" placeholder="Marke" />
            <select class="grow" data-key="elist-${k}">${this._todoOptions(p.target_list || "")}</select>
          </div></div>
          <div class="actions">
            <button class="btn" data-action="save-edit" data-ean="${k}">Speichern</button>
            <button class="btn secondary" data-action="cancel-edit">Abbrechen</button>
          </div></div>`;
      } else {
        html += `<div class="product"><div>
          <div class="pname">${esc(p.brand ? `${p.brand} · ${p.name}` : p.name)}</div>
          <div class="muted"><span class="code" style="font-size:13px">${k}</span> · ${p.scan_count || 0}× gescannt · <span class="src">${esc(SOURCE_LABELS[p.source] || p.source || "?")}</span>${p.target_list ? ` · → ${esc(p.target_list)}` : ""}</div>
          </div>
          <div class="actions">
            <button class="btn secondary" data-action="add" data-ean="${k}">Auf Liste</button>
            <button class="btn secondary" data-action="edit" data-ean="${k}">Bearbeiten</button>
            <button class="btn danger" data-action="delete" data-ean="${k}">Löschen</button>
          </div></div>`;
      }
    }
    return html + `</div>`;
  }

  _renderTools() {
    return `${this._renderLast()}
      <div class="card"><h2>Test-Scan</h2>
        <div class="muted">Verarbeitet einen Code genau wie ein echter Scan (lokale Datenbank, APIs, Liste).</div>
        <div class="row" style="margin-top:8px">
          <input type="text" class="grow" data-key="test-ean" placeholder="z. B. 4337256722261" />
          <button class="btn" data-action="test-scan">Scannen</button>
        </div></div>
      <div class="card"><h2>Backup exportieren</h2>
        <div class="muted">Die Datenbank liegt in Home Assistant unter .storage und ist im normalen HA-Backup enthalten. Hier zusätzlich als JSON-Datei.</div>
        <div class="row" style="margin-top:8px"><button class="btn" data-action="export">JSON herunterladen</button></div></div>
      <div class="card"><h2>Importieren</h2>
        <div class="row"><input type="file" accept=".json,application/json" data-key="import-file" class="grow" /></div>
        <div class="row" style="margin-top:8px"><textarea data-key="import-text" placeholder='oder JSON hier einfügen: {"products": {...}}'></textarea></div>
        <div class="row" style="margin-top:8px">
          <label class="check"><input type="checkbox" data-key="import-merge" checked /> Zusammenführen (aus = Datenbank ersetzen)</label>
          <span class="grow"></span>
          <button class="btn" data-action="import">Importieren</button>
        </div></div>`;
  }

  // -------------------------------------------------------------- Events
  _val(key) {
    const el = this.shadowRoot.querySelector(`[data-key="${CSS.escape(key)}"]`);
    if (!el) return "";
    return el.type === "checkbox" ? el.checked : (el.value || "").trim();
  }

  _onInput(ev) {
    if (ev.target.dataset.key === "search") {
      this._search = ev.target.value;
      this._renderContent();
    }
  }

  async _onChange(ev) {
    if (ev.target.dataset.key === "import-file" && ev.target.files && ev.target.files[0]) {
      const text = await ev.target.files[0].text();
      const area = this.shadowRoot.querySelector('[data-key="import-text"]');
      if (area) area.value = text;
    }
  }

  async _onClick(ev) {
    const btn = ev.target.closest("[data-action]");
    if (!btn) return;
    const action = btn.dataset.action;
    const ean = btn.dataset.ean;
    try {
      switch (action) {
        case "tab":
          this._tab = btn.dataset.tab;
          this._editing = null;
          this._renderContent();
          return;
        case "learn-pending":
        case "learn-pending-only": {
          const name = this._val(`pname-${ean}`);
          if (!name) return this._flash("Bitte einen Namen eingeben.", true);
          btn.disabled = true;
          await this._ws("learn", {
            ean,
            name,
            brand: this._val(`pbrand-${ean}`) || null,
            target_list: this._val(`plist-${ean}`) || null,
            add_to_list: action === "learn-pending",
          });
          this._flash(action === "learn-pending" ? `„${name}“ gespeichert und auf die Liste gesetzt.` : `„${name}“ gespeichert.`);
          break;
        }
        case "dismiss":
          await this._ws("dismiss_pending", { ean });
          this._flash(`${ean} verworfen.`);
          break;
        case "create": {
          const code = this._val("new-ean");
          const name = this._val("new-name");
          if (!code || !name) return this._flash("Code und Name sind Pflicht.", true);
          await this._ws("learn", {
            ean: code,
            name,
            brand: this._val("new-brand") || null,
            target_list: this._val("new-list") || null,
            add_to_list: false,
          });
          ["new-ean", "new-name", "new-brand"].forEach((k) => {
            const el = this.shadowRoot.querySelector(`[data-key="${k}"]`);
            if (el) el.value = "";
          });
          this._flash(`„${name}“ angelegt.`);
          break;
        }
        case "edit":
          this._editing = ean;
          this._renderContent();
          return;
        case "cancel-edit":
          this._editing = null;
          this._renderContent();
          return;
        case "save-edit": {
          const name = this._val(`ename-${ean}`);
          if (!name) return this._flash("Name darf nicht leer sein.", true);
          await this._ws("learn", {
            ean,
            name,
            brand: this._val(`ebrand-${ean}`),
            target_list: this._val(`elist-${ean}`) || null,
            add_to_list: false,
          });
          this._editing = null;
          this._flash(`„${name}“ gespeichert.`);
          break;
        }
        case "delete":
          if (!confirm(`Produkt ${ean} wirklich löschen?`)) return;
          await this._ws("delete", { ean });
          this._flash(`${ean} gelöscht.`);
          break;
        case "add": {
          const res = await this._ws("scan", { ean });
          this._flash(`${res.name || ean}: ${STATUS_LABELS[res.status] || res.status}`, res.status === "error");
          break;
        }
        case "test-scan": {
          const code = this._val("test-ean");
          if (!code) return this._flash("Bitte einen Code eingeben.", true);
          const res = await this._ws("scan", { ean: code });
          this._flash(`${res.name || code}: ${STATUS_LABELS[res.status] || res.status}`, res.status === "error");
          break;
        }
        case "export": {
          const data = await this._ws("export");
          const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
          const url = URL.createObjectURL(blob);
          const a = document.createElement("a");
          a.href = url;
          a.download = `barcode-scanner-backup-${new Date().toISOString().slice(0, 10)}.json`;
          document.body.appendChild(a);
          a.click();
          a.remove();
          setTimeout(() => URL.revokeObjectURL(url), 1000);
          this._flash(`${Object.keys(data.products || {}).length} Produkte exportiert.`);
          return;
        }
        case "import": {
          const text = this._val("import-text");
          if (!text) return this._flash("Bitte eine Datei wählen oder JSON einfügen.", true);
          let parsed;
          try {
            parsed = JSON.parse(text);
          } catch (e) {
            return this._flash(`Ungültiges JSON: ${e.message}`, true);
          }
          const merge = this._val("import-merge");
          if (!merge && !confirm("Die komplette Datenbank wird ersetzt. Fortfahren?")) return;
          const stats = await this._ws("import", { data: parsed, merge: !!merge });
          this._flash(`${stats.imported} Produkte importiert (${stats.skipped} übersprungen, gesamt ${stats.total}).`);
          break;
        }
        default:
          return;
      }
      await this._load();
    } catch (err) {
      btn.disabled = false;
      this._flash(`Fehler: ${err.message || err}`, true);
    }
  }
}

if (!customElements.get("shys-barcode-panel")) {
  customElements.define("shys-barcode-panel", ShysBarcodePanel);
}
