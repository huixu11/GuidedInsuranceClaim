/* SOP-guided insurance claims support agent — browser test UI.
 *
 * Vanilla JS, no build step, no external resources.
 * Every server- or model-derived string is inserted with text nodes /
 * textContent (never innerHTML), and every state field is read defensively.
 */
(function () {
  'use strict';

  /* ================================================================ constants */

  const SVG_NS = 'http://www.w3.org/2000/svg';
  const KEY_STORAGE = 'sopDemo.apiKey';
  const DEFAULT_TIMEOUT_MS = 30000;
  const MESSAGE_TIMEOUT_MS = 180000;

  const DEFAULT_PHASES = [
    { id: 'VERIFY_ID', label: 'Verify identity', freedom: 'strict', description: '' },
    { id: 'RESOLVE_INTENT', label: 'Resolve intent', freedom: 'guided', description: '' },
    { id: 'PROCESS_CASE', label: 'Process case', freedom: 'open', description: '' },
    { id: 'POST_PROCESS', label: 'Post-process', freedom: 'guided', description: '' },
  ];

  const DEFAULT_FIELDS = [
    ['full_name', 'Full name'],
    ['dob', 'Date of birth'],
    ['phone', 'Phone'],
    ['email', 'Email'],
    ['id_last4', 'SSN / ID last 4'],
  ];

  const STEP_WORDS = {
    done: 'done',
    current: 'current phase',
    halted: 'stopped here, escalated',
    upcoming: 'upcoming',
  };

  // Trace event kinds grouped into colour families; unknown kinds fall back to "other".
  const KIND_FAMILY = {
    understand: 'model',
    llm: 'model',
    memory: 'memory',
    gate: 'control',
    guard: 'control',
    tool: 'tool',
    decision: 'decision',
    directive: 'decision',
    transition: 'flow',
    escalation: 'alert',
    error: 'alert',
  };

  const ERROR_TITLES = {
    missing_api_key: 'API key required',
    invalid_api_key: 'API key rejected',
    rate_limited: 'Rate limited',
    llm_error: 'Model error',
    bad_request: 'Request rejected',
    session_not_found: 'Conversation not found',
    network_error: 'Network error',
    timeout: 'No response',
    bad_response: 'Unreadable response',
    client_error: 'Unexpected error',
    render_error: 'Display error',
  };

  const KEY_ERRORS = new Set(['missing_api_key', 'invalid_api_key']);
  // After these errors the server state cannot have moved, so skip the resync GET.
  const NO_RESYNC = new Set([
    'aborted', 'session_not_found', 'network_error', 'missing_api_key', 'invalid_api_key', 'bad_request', 'render_error',
  ]);

  const PH_FIELDS = [
    ['name', 'Full name'],
    ['policy_number', 'Policy number'],
    ['dob', 'Date of birth'],
    ['id_type', 'ID type'],
    ['id_last4', 'ID last 4'],
    ['phone', 'Phone'],
    ['email', 'Email'],
  ];

  const ID_TYPES = {
    ssn_last4: 'SSN (last 4)',
    national_id_last4: 'National ID (last 4)',
  };

  // 16x16 stroke icons (path data only; rendered with createElementNS).
  const ICONS = {
    check: ['M3.5 8.5 6.5 11.5 12.5 4.5'],
    x: ['M4.5 4.5 11.5 11.5', 'M11.5 4.5 4.5 11.5'],
    dash: ['M4.5 8h7'],
    lock: ['M4 7.5h8v6H4z', 'M5.75 7.5V5.25a2.25 2.25 0 0 1 4.5 0V7.5'],
    clock: ['M8 2.5a5.5 5.5 0 1 0 0 11a5.5 5.5 0 1 0 0-11z', 'M8 5v3.25l2 1.25'],
    cpu: ['M5 5h6v6H5z', 'M6.5 2.5V5M9.5 2.5V5M6.5 11v2.5M9.5 11v2.5M2.5 6.5H5M2.5 9.5H5M11 6.5h2.5M11 9.5h2.5'],
    key: ['M8 10.5a2.5 2.5 0 1 1-5 0a2.5 2.5 0 1 1 5 0z', 'M7.3 8.7 13 3', 'M11 5l1.5 1.5', 'M9.6 6.4l1.2 1.2'],
    database: [
      'M3 4c0-1.1 2.24-2 5-2s5 .9 5 2-2.24 2-5 2-5-.9-5-2z',
      'M3 4v8c0 1.1 2.24 2 5 2s5-.9 5-2V4',
      'M3 8c0 1.1 2.24 2 5 2s5-.9 5-2',
    ],
    plus: ['M8 3.5v9', 'M3.5 8h9'],
    send: ['M13.5 2.5 7 9', 'M13.5 2.5 9.5 13.5 7 9 2.5 6.5z'],
    copy: ['M5.5 5.5h7v8h-7z', 'M3.5 10.5v-8h7'],
    chevron: ['M6 4l4 4-4 4'],
    mail: ['M2.5 4h11v8h-11z', 'M2.5 4.5 8 9l5.5-4.5'],
    alert: ['M8 2.5 14 13H2z', 'M8 6.5v3', 'M8 11.25v.01'],
    info: ['M8 2.5a5.5 5.5 0 1 0 0 11a5.5 5.5 0 1 0 0-11z', 'M8 7.25V11', 'M8 5v.01'],
    retry: ['M13 8a5 5 0 1 1-1.46-3.54', 'M13 2.5V5h-2.5'],
    user: ['M8 2.5a2.5 2.5 0 1 0 0 5a2.5 2.5 0 1 0 0-5z', 'M3 13.5c.6-2.6 2.6-4 5-4s4.4 1.4 5 4'],
    pause: ['M6 4.5v7', 'M10 4.5v7'],
    history: ['M3 8a5 5 0 1 0 1.46-3.54', 'M3 2.5V5h2.5', 'M8 5.5V8l1.75 1.25'],
  };

  /* ==================================================================== state */

  const app = {
    config: {},
    configLoaded: false,
    configFailed: false,
    fixtures: null,
    sessionId: null,
    sessionScenario: null,
    gen: 0, // bumps on every new conversation; stale async results compare against it
    state: null,
    traces: [],
    traceSeq: 0,
    prevSigs: null,
    traceOpen: new Map(), // trace key -> explicitly toggled open/closed
    detailOpen: new Set(), // "d:<trace>:<index>" keys of opened event details
    rawOpen: false,
    pending: false,
    starting: false,
    sendController: null,
    sessionController: null,
    failed: null, // { text, row, retryBtn } for the latest failed message
    sessionLost: false,
    activeTab: 'chat',
    memKey: '',
    typingTimer: 0,
    firstSession: true,
  };

  const dom = {};
  const mqMobile = window.matchMedia('(max-width: 899px)');
  const mqReduced = window.matchMedia('(prefers-reduced-motion: reduce)');
  const mqCoarse = window.matchMedia('(pointer: coarse)');

  /* ================================================================ utilities */

  function isObj(v) {
    return v !== null && typeof v === 'object' && !Array.isArray(v);
  }

  function arr(v) {
    return Array.isArray(v) ? v : [];
  }

  function obj(v) {
    return isObj(v) ? v : {};
  }

  function isNum(v) {
    return typeof v === 'number' && Number.isFinite(v);
  }

  function num(v, fallback) {
    return isNum(v) ? v : fallback;
  }

  function str(v) {
    if (v === null || v === undefined) return '';
    if (typeof v === 'string') return v;
    if (typeof v === 'number' || typeof v === 'boolean') return String(v);
    try {
      return JSON.stringify(v);
    } catch (_) {
      return String(v);
    }
  }

  function humanize(v) {
    return str(v).replace(/_/g, ' ').replace(/\s+/g, ' ').trim();
  }

  function capitalize(v) {
    const s = str(v);
    return s ? s.charAt(0).toUpperCase() + s.slice(1) : s;
  }

  // "denial_question" -> "Denial question"; free text is left untouched.
  function humanizeCode(v) {
    const s = str(v);
    return /^[a-z][a-z0-9_]*$/.test(s) ? capitalize(humanize(s)) : s;
  }

  function clamp(n, lo, hi) {
    return Math.min(hi, Math.max(lo, n));
  }

  function plural(n, word) {
    return `${n} ${word}${n === 1 ? '' : 's'}`;
  }

  function cssEscape(s) {
    if (window.CSS && typeof window.CSS.escape === 'function') return window.CSS.escape(String(s));
    return String(s).replace(/["\\]/g, '\\$&');
  }

  function prettyJson(v) {
    if (typeof v === 'string') return v;
    try {
      const s = JSON.stringify(v, null, 2);
      return s === undefined ? String(v) : s;
    } catch (_) {
      return String(v);
    }
  }

  function fmtLatency(ms) {
    if (!isNum(ms) || ms < 0) return '';
    if (ms < 1000) return `${Math.round(ms)} ms`;
    return `${(ms / 1000).toFixed(1)}s`;
  }

  function clockTime() {
    try {
      return new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    } catch (_) {
      return '';
    }
  }

  function stateOf(key) {
    return app.state ? app.state[key] : undefined;
  }

  function statusOf() {
    return str(stateOf('status')) || 'active';
  }

  function agentName() {
    return str(app.config.agent_name) || 'Agent';
  }

  function isMobile() {
    return mqMobile.matches;
  }

  function onMediaChange(mq, fn) {
    if (typeof mq.addEventListener === 'function') mq.addEventListener('change', fn);
    else if (typeof mq.addListener === 'function') mq.addListener(fn);
  }

  /* ---------------------------------------------------------- DOM builders */

  function el(tag, props, ...children) {
    const node = document.createElement(tag);
    if (props) {
      Object.keys(props).forEach((key) => {
        const value = props[key];
        if (value === null || value === undefined || value === false) return;
        if (key === 'class') node.className = value;
        else if (key.slice(0, 2) === 'on' && typeof value === 'function') node.addEventListener(key.slice(2), value);
        else if (value === true) node.setAttribute(key, '');
        else node.setAttribute(key, String(value));
      });
    }
    append(node, children);
    return node;
  }

  function append(node, children) {
    children.forEach((child) => {
      if (child === null || child === undefined || child === false) return;
      if (Array.isArray(child)) {
        append(node, child);
        return;
      }
      node.appendChild(child instanceof Node ? child : document.createTextNode(String(child)));
    });
    return node;
  }

  function icon(name, extraClass) {
    const svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('viewBox', '0 0 16 16');
    svg.setAttribute('width', '16');
    svg.setAttribute('height', '16');
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('focusable', 'false');
    svg.setAttribute('class', extraClass ? `icon ${extraClass}` : 'icon');
    (ICONS[name] || []).forEach((d) => {
      const path = document.createElementNS(SVG_NS, 'path');
      path.setAttribute('d', d);
      svg.appendChild(path);
    });
    return svg;
  }

  function hydrateIcons(root) {
    root.querySelectorAll('[data-icon]').forEach((slot) => {
      slot.replaceWith(icon(slot.getAttribute('data-icon')));
    });
  }

  function badge(tone, text, iconName) {
    return el('span', { class: `badge badge-${tone}` }, iconName ? icon(iconName) : null, text);
  }

  function avatar() {
    const initial = agentName().trim().charAt(0).toUpperCase() || 'A';
    return el('span', { class: 'avatar', 'aria-hidden': 'true' }, initial);
  }

  function announce(message) {
    const region = document.querySelector('dialog[open] [data-live]') || dom.srStatus;
    if (!region) return;
    region.textContent = '';
    window.setTimeout(() => {
      region.textContent = message;
    }, 60);
  }

  /* ====================================================================== API */

  class ApiError extends Error {
    constructor(code, message, status) {
      super(message);
      this.name = 'ApiError';
      this.code = code;
      this.status = status || 0;
    }
  }

  function toApiError(err) {
    if (err instanceof ApiError) return err;
    return new ApiError('client_error', str(err && err.message ? err.message : err) || 'Unexpected error.');
  }

  async function api(method, path, body, options) {
    const opts = options || {};
    const controller = opts.controller || new AbortController();
    const timeoutMs = opts.timeoutMs === undefined ? DEFAULT_TIMEOUT_MS : opts.timeoutMs;
    const headers = { Accept: 'application/json' };
    const init = { method, headers, cache: 'no-store', signal: controller.signal };
    if (body !== undefined) {
      headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
    const key = getApiKey();
    if (key) headers['X-API-Key'] = key;

    let timedOut = false;
    const timer = timeoutMs > 0
      ? window.setTimeout(() => {
        timedOut = true;
        controller.abort();
      }, timeoutMs)
      : 0;

    let res;
    let text = '';
    try {
      res = await fetch(path, init);
      text = await res.text();
    } catch (_) {
      if (timedOut) {
        throw new ApiError('timeout', `No response from the server after ${Math.round(timeoutMs / 1000)} seconds.`);
      }
      if (controller.signal.aborted) throw new ApiError('aborted', 'The request was cancelled.');
      throw new ApiError('network_error', 'Could not reach the demo server. Check that it is still running.');
    } finally {
      window.clearTimeout(timer);
    }

    let data = null;
    if (text) {
      try {
        data = JSON.parse(text);
      } catch (_) {
        data = null;
      }
    }
    if (!res.ok) {
      const e = data && isObj(data.error) ? data.error : null;
      const code = e && typeof e.code === 'string' && e.code ? e.code : `http_${res.status}`;
      const message = e && typeof e.message === 'string' && e.message
        ? e.message
        : `The server responded with HTTP ${res.status}${res.statusText ? ` ${res.statusText}` : ''}.`;
      throw new ApiError(code, message, res.status);
    }
    if (!isObj(data)) {
      throw new ApiError('bad_response', 'The server sent a response this page could not read.', res.status);
    }
    return data;
  }

  function getApiKey() {
    try {
      const v = window.sessionStorage.getItem(KEY_STORAGE);
      if (v) return v;
    } catch (_) {
      /* sessionStorage unavailable: fall back to memory */
    }
    return app.memKey || '';
  }

  function setApiKey(value) {
    app.memKey = '';
    try {
      if (value) window.sessionStorage.setItem(KEY_STORAGE, value);
      else window.sessionStorage.removeItem(KEY_STORAGE);
    } catch (_) {
      app.memKey = value || '';
    }
  }

  /* ===================================================================== init */

  function cacheDom() {
    const ids = {
      app: 'app',
      appName: 'app-name',
      chipClock: 'chip-clock',
      chipClockValue: 'chip-clock-value',
      chipModel: 'chip-model',
      chipModelLabel: 'chip-model-label',
      chipModelValue: 'chip-model-value',
      chipKey: 'chip-key',
      chipKeyValue: 'chip-key-value',
      consentSelect: 'consent-select',
      btnKey: 'btn-key',
      btnFixtures: 'btn-fixtures',
      btnNew: 'btn-new',
      tabChat: 'tab-chat',
      tabInspector: 'tab-inspector',
      panelChat: 'panel-chat',
      panelInspector: 'panel-inspector',
      chatSub: 'chat-sub',
      chatPhase: 'chat-phase',
      messages: 'messages',
      chatItems: 'chat-items',
      chatTyping: 'chat-typing',
      chatStatus: 'chat-status',
      samples: 'samples',
      samplesList: 'samples-list',
      composer: 'composer',
      input: 'composer-input',
      btnSend: 'btn-send',
      inspector: 'inspector',
      inspectorSub: 'inspector-sub',
      srStatus: 'sr-status',
      keyDialog: 'key-dialog',
      keyForm: 'key-form',
      keyError: 'key-dialog-error',
      keyMode: 'key-dialog-mode',
      keySaved: 'key-dialog-saved',
      keyInput: 'key-input',
      keyInputError: 'key-input-error',
      keyClear: 'key-clear',
      keyClose: 'key-close',
      drawer: 'fixtures-drawer',
      fixturesBody: 'fixtures-body',
    };
    Object.keys(ids).forEach((k) => {
      dom[k] = document.getElementById(ids[k]);
    });
  }

  function bindUi() {
    dom.btnNew.addEventListener('click', () => {
      startSession();
    });
    dom.btnKey.addEventListener('click', () => {
      openKeyDialog();
    });
    dom.btnFixtures.addEventListener('click', openFixtures);
    dom.consentSelect.addEventListener('change', () => {
      announce(`Consent scenario ${dom.consentSelect.value} will be used for the next new conversation.`);
    });

    dom.composer.addEventListener('submit', (e) => {
      e.preventDefault();
      submitComposer();
    });
    dom.input.addEventListener('keydown', onComposerKeydown);
    dom.input.addEventListener('input', () => {
      autoGrow();
      updateComposerState();
    });

    dom.tabChat.addEventListener('click', () => selectTab('chat'));
    dom.tabInspector.addEventListener('click', () => selectTab('inspector'));
    dom.tabChat.addEventListener('keydown', onTabKeydown);
    dom.tabInspector.addEventListener('keydown', onTabKeydown);

    dom.inspector.addEventListener('click', onInspectorClick);

    dom.keyForm.addEventListener('submit', onKeySave);
    dom.keyClear.addEventListener('click', onKeyClear);
    dom.keyClose.addEventListener('click', () => closeDialog(dom.keyDialog));
    dom.keyInput.addEventListener('input', () => {
      dom.keyInputError.hidden = true;
    });
    dom.keyDialog.addEventListener('close', onDialogClosed);
    dom.drawer.addEventListener('close', onDialogClosed);
    dom.drawer.addEventListener('click', onDrawerClick);
    dom.fixturesBody.addEventListener('click', onFixturesClick);
    document.querySelectorAll('dialog [data-close]').forEach((btn) => {
      btn.addEventListener('click', () => closeDialog(btn.closest('dialog')));
    });

    onMediaChange(mqMobile, applyLayoutMode);
  }

  function init() {
    cacheDom();
    hydrateIcons(document);
    bindUi();
    applyLayoutMode();
    updateKeyChip();
    renderInspector();
    loadConfig();
  }

  async function loadConfig() {
    app.configFailed = false;
    dom.chatItems.replaceChildren(loadingRow('Loading the demo configuration…'));
    renderInspector();
    let cfg;
    try {
      cfg = await api('GET', '/api/config');
    } catch (err) {
      app.configFailed = true;
      dom.chatItems.replaceChildren(errorBlock('Could not load the demo configuration', toApiError(err), () => {
        loadConfig();
      }));
      if (KEY_ERRORS.has(err.code)) openKeyDialog({ error: err.message });
      renderInspector();
      updateKeyChip();
      return;
    }
    app.config = cfg;
    app.configLoaded = true;
    dom.chatItems.replaceChildren();
    applyConfig();
    if (cfg.fixtures_exposed) loadFixtures();
    else dom.btnFixtures.hidden = true;
    if (cfg.llm_mode === 'anthropic' && !cfg.has_server_key && !getApiKey()) openKeyDialog();
    startSession();
  }

  function applyConfig() {
    const c = app.config;
    const name = str(c.app_name) || 'Claims Support — SOP Agent Demo';
    dom.appName.textContent = name;
    dom.appName.title = name;
    document.title = name;

    const today = str(c.demo_today);
    dom.chipClockValue.textContent = today;
    dom.chipClock.hidden = !today;

    const models = obj(c.models);
    const respond = str(models.respond);
    const understand = str(models.understand);
    if (c.llm_mode === 'mock') {
      dom.chipModel.setAttribute('data-mode', 'mock');
      dom.chipModelLabel.hidden = true;
      dom.chipModelValue.textContent = 'Mock LLM';
      dom.chipModel.title = 'Mock LLM: an offline, deterministic test double. No model calls are made.';
      dom.chipModel.hidden = false;
    } else {
      dom.chipModel.removeAttribute('data-mode');
      dom.chipModelLabel.hidden = false;
      const value = respond && understand && respond !== understand
        ? `${respond} · ${understand}`
        : respond || understand || str(c.llm_mode);
      dom.chipModelValue.textContent = value;
      dom.chipModel.title = `respond: ${respond || 'not set'} · understand: ${understand || 'not set'}`
        + (c.llm_mode ? ` · mode: ${str(c.llm_mode)}` : '');
      dom.chipModel.hidden = !value;
    }

    const scenarios = arr(c.consent_scenarios).map(str).filter(Boolean);
    if (!scenarios.length) scenarios.push('default');
    const previous = dom.consentSelect.value;
    dom.consentSelect.replaceChildren(...scenarios.map((s) => el('option', { value: s }, s)));
    if (scenarios.indexOf(previous) >= 0) dom.consentSelect.value = previous;
    else dom.consentSelect.value = scenarios.indexOf('default') >= 0 ? 'default' : scenarios[0];

    renderSamples();
    updateKeyChip();
    updateHeads();
  }

  function renderSamples() {
    const prompts = arr(app.config.sample_prompts).filter((p) => isObj(p) && str(p.text));
    dom.samplesList.replaceChildren(...prompts.map((p) => {
      const text = str(p.text);
      const label = str(p.label) || (text.length > 40 ? `${text.slice(0, 40)}…` : text);
      return el('button', {
        type: 'button',
        class: 'sample',
        title: text,
        onclick: () => useSample(text),
      }, label);
    }));
    dom.samples.hidden = prompts.length === 0;
    updateComposerState();
  }

  function useSample(text) {
    const ta = dom.input;
    if (ta.disabled) return;
    ta.value = text;
    autoGrow();
    updateComposerState();
    ta.focus();
    ta.setSelectionRange(text.length, text.length);
  }

  function updateKeyChip() {
    const c = app.config;
    let state;
    let text;
    let title;
    if (c.llm_mode === 'mock') {
      // The mock never calls a model, so any saved or server key is irrelevant here.
      state = 'mock';
      text = 'No key needed';
      title = getApiKey()
        ? 'Mock LLM mode: no API key is needed. The key saved in this tab is kept but not used.'
        : 'Mock LLM mode: no API key is needed.';
    } else if (getApiKey()) {
      state = 'browser';
      text = 'Browser key';
      title = c.has_server_key
        ? 'Using the API key saved in this tab; it overrides the server key.'
        : 'Using the API key saved in this tab.';
    } else if (c.has_server_key) {
      state = 'server';
      text = 'Server key';
      title = 'The demo server has a model API key configured.';
    } else if (!app.configLoaded) {
      state = 'unknown';
      text = 'Key status unknown';
      title = 'The server configuration has not loaded.';
    } else {
      state = 'none';
      text = 'No key';
      title = 'No API key is configured. Add one with the API key button.';
    }
    dom.chipKey.setAttribute('data-state', state);
    dom.chipKeyValue.textContent = text;
    dom.chipKey.title = title;
  }

  /* ================================================================ fixtures */

  async function loadFixtures() {
    try {
      app.fixtures = await api('GET', '/api/fixtures');
      dom.btnFixtures.hidden = false;
    } catch (_) {
      app.fixtures = null;
      dom.btnFixtures.hidden = true;
    }
  }

  function openFixtures() {
    renderFixtures();
    openDialog(dom.drawer);
  }

  function renderFixtures() {
    const fx = obj(app.fixtures);
    const holders = arr(fx.policyholders).filter(isObj);
    const reps = arr(fx.representatives).filter(isObj);
    const out = [el('h3', { class: 'fx-section-title' }, `Policyholders (${holders.length})`)];
    if (holders.length) holders.forEach((p) => out.push(renderPolicyholder(p)));
    else out.push(el('p', { class: 'empty' }, 'No policyholders in the fixtures.'));
    out.push(el('h3', { class: 'fx-section-title' }, `Representatives (${reps.length})`));
    if (reps.length) out.push(el('ul', { class: 'fx-reps' }, reps.map(renderRep)));
    else out.push(el('p', { class: 'empty' }, 'No representatives in the fixtures.'));
    dom.fixturesBody.replaceChildren(...out);
  }

  function renderPolicyholder(p) {
    const rows = [];
    const known = new Set(['party_id', 'claims']);
    PH_FIELDS.forEach(([key, label]) => {
      known.add(key);
      const v = p[key];
      if (v === null || v === undefined || v === '') return;
      if (key === 'id_type') rows.push(fxRow(label, ID_TYPES[str(v)] || humanize(v), false));
      else rows.push(fxRow(label, str(v), true));
    });
    // Any other scalar (or list-of-scalar) fields, e.g. name_aliases.
    Object.keys(p).forEach((key) => {
      if (known.has(key)) return;
      const v = p[key];
      const label = capitalize(humanize(key).replace(/aliases$/, 'alias'));
      if (Array.isArray(v)) {
        v.forEach((item) => {
          if (item !== null && item !== undefined && item !== '' && typeof item !== 'object') {
            rows.push(fxRow(label, str(item), true));
          }
        });
      } else if (v !== null && v !== undefined && v !== '' && typeof v !== 'object') {
        rows.push(fxRow(label, str(v), true));
      }
    });
    const claims = arr(p.claims).filter(isObj);
    return el('section', { class: 'fx-block' },
      el('div', { class: 'fx-head' },
        el('h4', { class: 'fx-name' }, str(p.name) || 'Unnamed policyholder'),
        p.party_id ? el('span', { class: 'tag' }, `Party ${str(p.party_id)}`) : null),
      el('dl', { class: 'fx-fields' }, rows),
      claims.length ? claimsTable(claims) : el('p', { class: 'empty fx-noclaims' }, 'No claims on file.'));
  }

  function fxRow(label, value, copyable) {
    return el('div', { class: 'fx-row' },
      el('dt', null, label),
      el('dd', null, el('span', { class: 'fx-value' }, value), copyable ? copyButton(value, label) : null));
  }

  function copyButton(value, label) {
    const btn = el('button', { type: 'button', class: 'copy-btn', 'aria-label': `Copy ${label}`, title: `Copy ${label}` },
      icon('copy'));
    btn.dataset.copy = value;
    return btn;
  }

  function claimStatusBadge(status) {
    const s = str(status).toLowerCase();
    if (!s) return el('span', { class: 'muted' }, '—');
    let tone = 'neutral';
    if (s === 'denied' || s === 'rejected') tone = 'bad';
    else if (s === 'open' || s === 'pending' || s === 'in_progress' || s === 'under_review') tone = 'warn';
    else if (s === 'closed' || s === 'settled' || s === 'paid' || s === 'approved') tone = 'ok';
    return badge(tone, humanize(s));
  }

  function claimsTable(claims) {
    const tbody = el('tbody');
    const skip = new Set(['case_id', 'case_type', 'created_at', 'status', 'summary', 'party_id']);
    claims.forEach((c) => {
      const id = str(c.case_id);
      tbody.appendChild(el('tr', { class: 'fx-claim-main' },
        el('td', null, el('span', { class: 'fx-case' },
          el('span', { class: 'fx-value' }, id || '—'),
          id ? copyButton(id, `case ID ${id}`) : null)),
        el('td', null, str(c.case_type) || '—'),
        el('td', null, str(c.created_at) || '—'),
        el('td', null, claimStatusBadge(c.status))));
      const extras = Object.keys(c)
        .filter((k) => !skip.has(k) && c[k] !== null && c[k] !== undefined && c[k] !== '')
        .map((k) => el('div', { class: 'fx-extra-row' },
          el('dt', null, capitalize(humanize(k))),
          el('dd', null, Array.isArray(c[k]) ? c[k].map(str).join(', ') : str(c[k]))));
      if (c.summary || extras.length) {
        tbody.appendChild(el('tr', { class: 'fx-claim-sub' },
          el('td', { colspan: '4' },
            c.summary ? el('p', { class: 'fx-summary' }, str(c.summary)) : null,
            extras.length
              ? el('details', { class: 'fx-more' }, el('summary', null, 'More fields'), el('dl', { class: 'fx-extra' }, extras))
              : null)));
      }
    });
    return el('table', { class: 'fx-claims' },
      el('caption', null, `Claims (${claims.length})`),
      el('thead', null, el('tr', null,
        el('th', { scope: 'col' }, 'Case'),
        el('th', { scope: 'col' }, 'Type'),
        el('th', { scope: 'col' }, 'Created'),
        el('th', { scope: 'col' }, 'Status'))),
      tbody);
  }

  function renderRep(r) {
    const name = str(r.rep_name) || 'Unnamed representative';
    const meta = [
      r.relationship ? capitalize(str(r.relationship)) : '',
      r.buyer_name ? `of ${str(r.buyer_name)}` : '',
      r.buyer_party_id ? `(party ${str(r.buyer_party_id)})` : '',
    ].filter(Boolean).join(' ');
    return el('li', { class: 'fx-rep' },
      el('div', { class: 'fx-rep-main' },
        el('span', { class: 'fx-value' }, name),
        r.rep_name ? copyButton(name, `representative name ${name}`) : null),
      meta ? el('div', { class: 'fx-rep-meta' }, meta) : null);
  }

  function onFixturesClick(e) {
    const btn = e.target.closest('.copy-btn');
    if (btn && dom.fixturesBody.contains(btn)) doCopy(btn);
  }

  async function doCopy(btn) {
    const ok = await copyText(btn.dataset.copy || '');
    btn.classList.toggle('copied', ok);
    btn.classList.toggle('copy-failed', !ok);
    btn.replaceChildren(icon(ok ? 'check' : 'x'));
    announce(ok ? 'Copied to the clipboard.' : 'Copy failed. Select the text manually.');
    window.clearTimeout(btn.resetTimer);
    btn.resetTimer = window.setTimeout(() => {
      btn.classList.remove('copied', 'copy-failed');
      btn.replaceChildren(icon('copy'));
    }, 1400);
  }

  async function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      try {
        await navigator.clipboard.writeText(text);
        return true;
      } catch (_) {
        /* fall back to execCommand below */
      }
    }
    // Plain-http deployments (e.g. a container on a LAN IP) have no async clipboard API.
    // The proxy must live inside the open modal dialog, or it would be inert.
    const host = document.querySelector('dialog[open]') || document.body;
    const previous = document.activeElement;
    const proxy = el('textarea', { class: 'clipboard-proxy', readonly: true, tabindex: '-1', 'aria-hidden': 'true' });
    proxy.value = text;
    host.appendChild(proxy);
    proxy.select();
    let ok = false;
    try {
      ok = document.execCommand('copy');
    } catch (_) {
      ok = false;
    }
    proxy.remove();
    if (previous && typeof previous.focus === 'function') previous.focus({ preventScroll: true });
    return ok;
  }

  /* ================================================================== dialogs */

  function openDialog(d) {
    if (!d || d.open) return;
    if (typeof d.showModal === 'function') {
      try {
        d.showModal();
        return;
      } catch (_) {
        /* fall through to the attribute */
      }
    }
    d.setAttribute('open', '');
  }

  function closeDialog(d) {
    if (!d || !d.open) return;
    if (typeof d.close === 'function') d.close();
    else d.removeAttribute('open');
  }

  function onDialogClosed() {
    window.setTimeout(() => {
      if (document.querySelector('dialog[open]')) return;
      const active = document.activeElement;
      if (!active || active === document.body) restoreComposerFocus(true);
    }, 0);
  }

  function onDrawerClick(e) {
    // A click on the ::backdrop targets the dialog element itself, outside its box.
    if (e.target !== dom.drawer) return;
    const r = dom.drawer.getBoundingClientRect();
    const inside = e.clientX >= r.left && e.clientX <= r.right && e.clientY >= r.top && e.clientY <= r.bottom;
    if (!inside) closeDialog(dom.drawer);
  }

  function openKeyDialog(opts) {
    const o = opts || {};
    const c = app.config;
    if (o.error) {
      dom.keyError.replaceChildren(icon('alert'), el('span', null, o.error));
      dom.keyError.hidden = false;
    } else {
      dom.keyError.replaceChildren();
      dom.keyError.hidden = true;
    }
    let note;
    if (!app.configLoaded) note = 'The server configuration has not loaded yet.';
    else if (c.llm_mode === 'mock') {
      note = 'This server runs the mock LLM (an offline, deterministic test double), so no key is needed.';
    } else if (c.has_server_key) {
      note = 'A server key is configured, so a browser key is optional. If you save one here, it overrides the server key for your requests.';
    } else {
      note = 'No server key is configured. Add your own Anthropic API key so the agent can call the model.';
    }
    dom.keyMode.textContent = note;
    dom.keyInput.value = '';
    dom.keyInputError.hidden = true;
    refreshSavedKeyNote();
    openDialog(dom.keyDialog);
    dom.keyInput.focus();
  }

  function refreshSavedKeyNote() {
    const k = getApiKey();
    dom.keySaved.textContent = k
      ? `A browser key is saved in this tab (ending in ${k.slice(-4)}). Saving a new one replaces it.`
      : 'No browser key is saved in this tab.';
    dom.keyClear.disabled = !k;
  }

  function showKeyInputError(message) {
    dom.keyInputError.textContent = message;
    dom.keyInputError.hidden = false;
    dom.keyInput.focus();
  }

  function onKeySave(e) {
    e.preventDefault();
    const value = dom.keyInput.value.trim();
    if (!value) {
      showKeyInputError('Enter a key first, or use Clear to remove the saved one.');
      return;
    }
    if (!/^[\x21-\x7e]+$/.test(value)) {
      showKeyInputError('An API key is a single line of plain ASCII characters without spaces.');
      return;
    }
    setApiKey(value);
    dom.keyInput.value = '';
    updateKeyChip();
    closeDialog(dom.keyDialog);
    announce('API key saved for this browser tab.');
    if (app.failed && app.failed.retryBtn && app.failed.retryBtn.isConnected) {
      app.failed.retryBtn.focus({ preventScroll: true });
    }
  }

  function onKeyClear() {
    setApiKey('');
    dom.keyInput.value = '';
    updateKeyChip();
    refreshSavedKeyNote();
    announce('Browser API key cleared.');
    dom.keyInput.focus();
  }

  /* ============================================================ layout & tabs */

  function applyLayoutMode() {
    const mobile = isMobile();
    const panels = [
      [dom.panelChat, 'tab-chat', 'Conversation'],
      [dom.panelInspector, 'tab-inspector', 'SOP inspector'],
    ];
    panels.forEach(([panel, tabId, label]) => {
      if (mobile) {
        panel.setAttribute('role', 'tabpanel');
        panel.setAttribute('aria-labelledby', tabId);
        panel.removeAttribute('aria-label');
      } else {
        panel.setAttribute('role', 'region');
        panel.removeAttribute('aria-labelledby');
        panel.setAttribute('aria-label', label);
      }
    });
    selectTab(app.activeTab);
  }

  function selectTab(name, focusTab) {
    app.activeTab = name === 'inspector' ? 'inspector' : 'chat';
    dom.app.setAttribute('data-tab', app.activeTab);
    [['chat', dom.tabChat], ['inspector', dom.tabInspector]].forEach(([key, tab]) => {
      const selected = key === app.activeTab;
      tab.setAttribute('aria-selected', selected ? 'true' : 'false');
      tab.tabIndex = selected ? 0 : -1;
      if (selected) clearUnread(key);
    });
    if (focusTab) (app.activeTab === 'chat' ? dom.tabChat : dom.tabInspector).focus();
    if (app.activeTab === 'chat') autoGrow();
  }

  function onTabKeydown(e) {
    let next = null;
    if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') next = app.activeTab === 'chat' ? 'inspector' : 'chat';
    else if (e.key === 'Home') next = 'chat';
    else if (e.key === 'End') next = 'inspector';
    if (!next) return;
    e.preventDefault();
    selectTab(next, true);
  }

  function markUnread(tab) {
    const dot = (tab === 'chat' ? dom.tabChat : dom.tabInspector).querySelector('.tab-dot');
    if (dot) dot.hidden = false;
  }

  function clearUnread(tab) {
    const dot = (tab === 'chat' ? dom.tabChat : dom.tabInspector).querySelector('.tab-dot');
    if (dot) dot.hidden = true;
  }

  /* ===================================================================== chat */

  function loadingRow(text) {
    return el('div', { class: 'chat-loading' }, el('span', { class: 'spinner', 'aria-hidden': 'true' }), text);
  }

  function errorBlock(title, err, retry) {
    return el('div', { class: 'chat-error chat-error-block', role: 'group', 'aria-label': title },
      icon('alert'),
      el('div', { class: 'chat-error-body' },
        el('p', { class: 'chat-error-title' }, title),
        el('p', { class: 'chat-error-msg' }, err.message || 'Unknown error.'),
        err.code ? el('p', { class: 'chat-error-code' }, `Error code: ${err.code}`) : null),
      retry ? el('button', { type: 'button', class: 'btn btn-sm', onclick: retry }, icon('retry'), 'Try again') : null);
  }

  function appendUserMessage(text) {
    const row = el('div', { class: 'msg msg-user' },
      el('div', { class: 'msg-body' },
        el('span', { class: 'visually-hidden' }, 'You said: '),
        el('div', { class: 'bubble' }, text)));
    dom.chatItems.appendChild(row);
    scrollChatToBottom();
    return row;
  }

  function appendAssistantMessage(text, turn, phase) {
    const key = isNum(turn) ? String(turn) : null;
    const label = metaLabel(turn, phase);
    let meta = null;
    if (label) {
      meta = key !== null && findTrace(key)
        ? el('button', {
          type: 'button',
          class: 'meta meta-link',
          title: 'Show this turn in the SOP inspector',
          onclick: () => revealTrace(key),
        }, label)
        : el('span', { class: 'meta' }, label);
    }
    const row = el('div', { class: 'msg msg-assistant' },
      avatar(),
      el('div', { class: 'msg-body' },
        el('span', { class: 'visually-hidden' }, `${agentName()} said: `),
        el('div', { class: text ? 'bubble' : 'bubble bubble-empty' }, text || '(empty reply)'),
        meta));
    dom.chatItems.appendChild(row);
    return row;
  }

  function appendSystemMessage(text) {
    const row = el('div', { class: 'msg msg-system' },
      el('div', { class: 'msg-body' }, el('div', { class: 'bubble' }, text || '(empty message)')));
    dom.chatItems.appendChild(row);
    return row;
  }

  function appendDivider(text, isPhase) {
    const node = el('div', { class: isPhase ? 'chat-divider chat-divider-phase' : 'chat-divider' },
      el('span', { class: 'chat-divider-text' }, text));
    dom.chatItems.appendChild(node);
    return node;
  }

  function appendPhaseDivider(before, after) {
    return appendDivider(`Phase: ${phaseLabel(before)} → ${phaseLabel(after)}`, true);
  }

  function metaLabel(turn, phase) {
    const parts = [];
    if (isNum(turn)) parts.push(`Turn ${turn}`);
    if (phase) parts.push(phaseLabel(phase));
    return parts.join(' · ');
  }

  function renderHistoryMessage(m) {
    if (!isObj(m)) return;
    const text = str(m.text);
    if (m.role === 'user') {
      appendUserMessage(text);
      return;
    }
    if (m.role === 'assistant') {
      const turn = isNum(m.turn) ? m.turn : null;
      const trace = turn !== null ? findTrace(String(turn)) : null;
      const before = trace ? str(trace.phase_before) : '';
      const after = trace ? str(trace.phase_after) : '';
      if (before && after && before !== after) appendPhaseDivider(before, after);
      const phase = after || (turn !== null ? phaseAtTurn(turn) : '') || str(stateOf('phase'));
      appendAssistantMessage(text, turn, phase);
      return;
    }
    appendSystemMessage(text);
  }

  function phaseAtTurn(turn) {
    let phase = '';
    arr(stateOf('phase_history')).forEach((h) => {
      if (isObj(h) && isNum(h.turn) && h.turn <= turn && h.to) phase = str(h.to);
    });
    return phase;
  }

  function scrollChatToBottom() {
    dom.messages.scrollTop = dom.messages.scrollHeight;
  }

  // Show the start of a long reply rather than jumping to its end.
  function scrollChatTo(node) {
    const box = dom.messages;
    if (!node || !node.isConnected) {
      scrollChatToBottom();
      return;
    }
    const top = Math.max(0, node.offsetTop - 12);
    if (box.scrollHeight - top > box.clientHeight) box.scrollTop = top;
    else scrollChatToBottom();
  }

  function showTyping(on) {
    window.clearInterval(app.typingTimer);
    app.typingTimer = 0;
    if (!on) {
      dom.chatTyping.hidden = true;
      dom.chatTyping.replaceChildren();
      return;
    }
    const started = Date.now();
    const elapsed = el('span', { 'aria-hidden': 'true' });
    dom.chatTyping.replaceChildren(
      el('div', { class: 'msg msg-assistant' },
        avatar(),
        el('div', { class: 'msg-body' },
          el('div', { class: 'bubble typing-bubble' },
            el('span', { class: 'typing-dots', 'aria-hidden': 'true' }, el('span'), el('span'), el('span'))),
          el('span', { class: 'meta' }, `${agentName()} is typing…`, elapsed))));
    dom.chatTyping.hidden = false;
    app.typingTimer = window.setInterval(() => {
      const s = Math.round((Date.now() - started) / 1000);
      elapsed.textContent = s >= 2 ? ` ${s}s` : '';
    }, 1000);
    scrollChatToBottom();
  }

  function updateStatusUi() {
    const status = statusOf();
    let node = null;
    if (app.sessionLost) {
      node = statusBanner('bad', 'alert',
        'This conversation no longer exists on the server (it was probably restarted).', true);
    } else if (app.state && status === 'escalated') {
      node = statusBanner('warn', 'user', 'Transferred to a human representative — this virtual session has ended', true);
    } else if (app.state && status === 'completed') {
      node = statusBanner('ok', 'check', 'Conversation completed — you can still ask a follow-up or start a new one', false);
    }
    dom.chatStatus.replaceChildren(...(node ? [node] : []));
    updateComposerState();
    updateHeads();
  }

  function statusBanner(tone, iconName, text, primary) {
    return el('div', { class: `status-banner status-banner-${tone}` },
      icon(iconName),
      el('p', { class: 'status-banner-text' }, text),
      el('button', {
        type: 'button',
        class: primary ? 'btn btn-sm btn-primary' : 'btn btn-sm',
        onclick: () => {
          startSession();
        },
      }, 'Start new conversation'));
  }

  function canSend() {
    return Boolean(app.sessionId) && !app.starting && !app.sessionLost && statusOf() !== 'escalated';
  }

  function updateComposerState() {
    const ended = app.sessionLost || (app.state && statusOf() === 'escalated');
    const ready = canSend();
    dom.input.disabled = !ready;
    dom.btnSend.disabled = !ready || app.pending || !dom.input.value.trim();
    let placeholder;
    if (app.sessionLost) placeholder = 'Start a new conversation to continue';
    else if (ended) placeholder = 'This virtual session has ended. Start a new conversation.';
    else if (!app.sessionId) placeholder = 'Waiting for the conversation to start…';
    else placeholder = `Message ${agentName()}…`;
    dom.input.placeholder = placeholder;
    dom.samplesList.querySelectorAll('button').forEach((b) => {
      b.disabled = !ready;
    });
  }

  function restoreComposerFocus(force) {
    if (document.querySelector('dialog[open]')) return;
    if (isMobile() && app.activeTab !== 'chat') return;
    const active = document.activeElement;
    const free = !active || active === document.body || dom.composer.contains(active) || dom.messages.contains(active);
    if (!force && !free) return;
    if (!dom.input.disabled) {
      dom.input.focus({ preventScroll: true });
      return;
    }
    const btn = dom.chatStatus.querySelector('button');
    if (btn) btn.focus({ preventScroll: true });
  }

  function onComposerKeydown(e) {
    if (e.key !== 'Enter' || e.shiftKey || e.isComposing || e.keyCode === 229) return;
    e.preventDefault();
    submitComposer();
  }

  function submitComposer() {
    const text = dom.input.value.trim();
    if (!text || app.pending || !canSend()) return;
    dom.input.value = '';
    autoGrow();
    sendMessage(text, false);
  }

  function autoGrow() {
    const ta = dom.input;
    if (!ta.offsetParent) return; // hidden (other tab); recomputed when shown
    ta.style.height = 'auto';
    const frame = ta.offsetHeight - ta.clientHeight;
    const wanted = ta.scrollHeight + frame;
    const max = 180;
    ta.style.height = `${Math.min(wanted, max)}px`;
    ta.style.overflowY = wanted > max ? 'auto' : 'hidden';
  }

  /* ----------------------------------------------------------- send / reply */

  async function sendMessage(text, isRetry) {
    if (app.pending || !canSend()) return;
    const gen = app.gen;
    const sessionId = app.sessionId;
    if (isRetry) removeFailedRow();
    else {
      retireFailedRow();
      appendUserMessage(text);
    }
    const controller = new AbortController();
    app.sendController = controller;
    app.pending = true;
    showTyping(true);
    updateComposerState();

    let data = null;
    let error = null;
    try {
      data = await api('POST', `/api/sessions/${encodeURIComponent(sessionId)}/messages`, { text }, {
        controller,
        timeoutMs: MESSAGE_TIMEOUT_MS,
      });
    } catch (err) {
      error = toApiError(err);
    }
    if (gen !== app.gen) return; // a new conversation started meanwhile
    app.sendController = null;
    app.pending = false;
    showTyping(false);
    updateComposerState();

    if (error) {
      handleSendError(error, text);
    } else {
      try {
        handleReply(data);
      } catch (err) {
        // A rendering bug must not wedge the conversation; surface it instead.
        window.console.error(err);
        showFailedRow(text, new ApiError('render_error', `The reply arrived but could not be displayed: ${str(err && err.message)}`), false);
      }
    }
    restoreComposerFocus(false);
  }

  function handleReply(data) {
    const trace = isObj(data.trace) ? normalizeTrace(data.trace) : null;
    if (isObj(data.state)) app.state = data.state;
    if (trace) upsertTrace(trace);

    let first = null;
    const before = trace ? str(trace.phase_before) : '';
    const after = trace ? str(trace.phase_after) : '';
    if (before && after && before !== after) first = appendPhaseDivider(before, after);

    let turn = trace && isNum(trace.turn) ? trace.turn : null;
    if (turn === null && isNum(stateOf('turn'))) turn = stateOf('turn');
    const phase = after || str(stateOf('phase'));
    const row = appendAssistantMessage(str(data.reply), turn, phase);

    renderInspector();
    updateStatusUi();
    scrollChatTo(first || row);
    if (isMobile() && app.activeTab !== 'chat') markUnread('chat');
  }

  function handleSendError(err, text) {
    if (err.code === 'aborted') return;
    if (err.code === 'session_not_found') {
      app.sessionLost = true;
      showFailedRow(text, err, false);
      updateStatusUi();
      scrollChatToBottom(); // the banner was added after the error row
      return;
    }
    showFailedRow(text, err, true);
    if (KEY_ERRORS.has(err.code)) {
      noteKeyError(err);
      openKeyDialog({ error: err.message });
    } else if (!NO_RESYNC.has(err.code)) {
      resyncState();
    }
  }

  // A missing-key error without a browser key proves the server has no usable key,
  // whatever the config said at load time; keep the chip and dialog honest.
  function noteKeyError(err) {
    if (err.code === 'missing_api_key' && !getApiKey() && app.config.has_server_key) {
      app.config.has_server_key = false;
      updateKeyChip();
    }
  }

  function showFailedRow(text, err, canRetry) {
    removeFailedRow();
    const title = ERROR_TITLES[err.code] || (err.status ? `Server error ${err.status}` : 'Something went wrong');
    const retryBtn = canRetry
      ? el('button', {
        type: 'button',
        class: 'btn btn-sm',
        onclick: () => {
          sendMessage(text, true);
        },
      }, icon('retry'), 'Retry')
      : null;
    const row = el('div', { class: 'chat-error', role: 'group', 'aria-label': 'Message not answered' },
      icon('alert'),
      el('div', { class: 'chat-error-body' },
        el('p', { class: 'chat-error-title' }, `${title} — ${canRetry ? 'your message was not answered' : 'not sent'}`),
        err.message ? el('p', { class: 'chat-error-msg' }, err.message) : null,
        err.code ? el('p', { class: 'chat-error-code' }, `Error code: ${err.code}`) : null),
      retryBtn);
    dom.chatItems.appendChild(row);
    app.failed = canRetry ? { text, row, retryBtn } : null;
    scrollChatToBottom();
  }

  function removeFailedRow() {
    if (app.failed && app.failed.row) app.failed.row.remove();
    app.failed = null;
  }

  // The user sent something new instead of retrying: keep the record, drop the Retry.
  function retireFailedRow() {
    const f = app.failed;
    app.failed = null;
    if (!f || !f.row) return;
    if (f.retryBtn) f.retryBtn.remove();
    f.row.classList.add('chat-error-retired');
    const title = f.row.querySelector('.chat-error-title');
    if (title) title.textContent = 'Not answered — a newer message was sent instead of retrying';
  }

  // After a failed turn, show whatever the server recorded (state + traces).
  async function resyncState() {
    const gen = app.gen;
    const id = app.sessionId;
    if (!id) return;
    let data;
    try {
      data = await api('GET', `/api/sessions/${encodeURIComponent(id)}`, undefined, { timeoutMs: 15000 });
    } catch (_) {
      return;
    }
    if (gen !== app.gen || app.pending) return;
    if (isObj(data.state)) app.state = data.state;
    if (Array.isArray(data.traces)) {
      app.traces = [];
      data.traces.filter(isObj).forEach((t) => upsertTrace(normalizeTrace(t)));
    }
    renderInspector();
    updateStatusUi();
  }

  /* ---------------------------------------------------------------- session */

  function abortInFlight() {
    if (app.sendController) {
      app.sendController.abort();
      app.sendController = null;
    }
    if (app.sessionController) {
      app.sessionController.abort();
      app.sessionController = null;
    }
  }

  function resetConversation() {
    app.sessionId = null;
    app.sessionScenario = null;
    app.state = null;
    app.traces = [];
    app.prevSigs = null;
    app.traceOpen.clear();
    app.detailOpen.clear();
    app.failed = null;
    app.sessionLost = false;
    app.pending = false;
    showTyping(false);
    dom.chatItems.replaceChildren();
    dom.chatStatus.replaceChildren();
    clearUnread('chat');
    clearUnread('inspector');
  }

  async function startSession() {
    if (!app.configLoaded) {
      loadConfig();
      return;
    }
    abortInFlight();
    app.gen += 1;
    const gen = app.gen;
    resetConversation();
    app.starting = true;
    renderInspector();
    updateStatusUi();

    const scenario = dom.consentSelect.value || 'default';
    const loading = loadingRow('Starting a new conversation…');
    dom.chatItems.appendChild(loading);
    const controller = new AbortController();
    app.sessionController = controller;

    let data = null;
    let error = null;
    try {
      data = await api('POST', '/api/sessions', { consent_scenario: scenario }, { controller });
    } catch (err) {
      error = toApiError(err);
    }
    if (gen !== app.gen) return;
    app.sessionController = null;
    app.starting = false;
    loading.remove();

    if (error) {
      if (error.code !== 'aborted') {
        dom.chatItems.appendChild(errorBlock('Could not start a conversation', error, () => {
          startSession();
        }));
        if (KEY_ERRORS.has(error.code)) {
          noteKeyError(error);
          openKeyDialog({ error: error.message });
        }
      }
      renderInspector();
      updateStatusUi();
      return;
    }

    const sid = data.session_id;
    app.sessionId = sid !== null && sid !== undefined && sid !== '' ? String(sid) : null;
    app.sessionScenario = scenario;
    app.state = isObj(data.state) ? data.state : null;
    if (Array.isArray(data.traces)) data.traces.filter(isObj).forEach((t) => upsertTrace(normalizeTrace(t)));

    const time = clockTime();
    appendDivider(`New conversation · consent scenario “${scenario}”${time ? ` · ${time}` : ''}`, false);
    arr(data.messages).forEach(renderHistoryMessage);
    if (!app.sessionId) {
      dom.chatItems.appendChild(errorBlock('The server did not return a session id',
        new ApiError('bad_response', 'Messages cannot be sent without a session id.'), () => {
          startSession();
        }));
    }
    renderInspector();
    updateStatusUi();
    scrollChatToBottom();
    const skipFocus = app.firstSession && mqCoarse.matches; // no surprise keyboard on phones
    app.firstSession = false;
    if (!skipFocus) restoreComposerFocus(true);
  }

  /* ------------------------------------------------------------------ traces */

  function normalizeTrace(t) {
    const copy = Object.assign({}, t);
    app.traceSeq += 1;
    copy._seq = app.traceSeq;
    copy._key = isNum(t.turn) ? String(t.turn) : `n${app.traceSeq}`;
    return copy;
  }

  function upsertTrace(t) {
    const i = app.traces.findIndex((x) => x._key === t._key);
    if (i >= 0) app.traces[i] = t;
    else app.traces.push(t);
  }

  function findTrace(key) {
    return app.traces.find((t) => t._key === key) || null;
  }

  function sortedTraces() {
    return app.traces.slice().sort((a, b) => {
      const ta = isNum(a.turn) ? a.turn : Infinity;
      const tb = isNum(b.turn) ? b.turn : Infinity;
      if (ta !== tb) return tb > ta ? 1 : -1;
      return b._seq - a._seq;
    });
  }

  function revealTrace(key) {
    app.traceOpen.set(key, true);
    if (isMobile()) selectTab('inspector');
    const group = dom.inspector.querySelector(`details[data-trace-key="${cssEscape(key)}"]`);
    if (!group) return;
    group.open = true;
    group.scrollIntoView({ block: 'start', behavior: mqReduced.matches ? 'auto' : 'smooth' });
    const summary = group.querySelector('summary');
    if (summary) {
      summary.focus({ preventScroll: true });
      flash(summary);
    }
  }

  function onInspectorClick(e) {
    const summary = e.target.closest('summary');
    if (!summary || !dom.inspector.contains(summary)) return;
    const details = summary.parentElement;
    if (!details || details.tagName !== 'DETAILS') return;
    const key = details.getAttribute('data-open-key');
    if (!key) return;
    const willOpen = !details.open; // click fires before the native toggle
    if (key === 'raw') app.rawOpen = willOpen;
    else if (key.slice(0, 2) === 't:') app.traceOpen.set(key.slice(2), willOpen);
    else if (willOpen) app.detailOpen.add(key);
    else app.detailOpen.delete(key);
  }

  /* ================================================================ inspector */

  function getPhases() {
    const list = arr(app.config.phases).filter((p) => isObj(p) && str(p.id));
    const source = list.length ? list : DEFAULT_PHASES;
    return source.map((p) => ({
      id: str(p.id),
      label: str(p.label) || defaultPhaseLabel(str(p.id)),
      freedom: str(p.freedom),
      description: str(p.description),
    }));
  }

  function defaultPhaseLabel(id) {
    const d = DEFAULT_PHASES.find((x) => x.id === id);
    return d ? d.label : capitalize(humanize(String(id).toLowerCase()));
  }

  function phaseLabel(id) {
    const key = str(id);
    if (!key) return '';
    const p = getPhases().find((x) => x.id === key);
    return p ? p.label : defaultPhaseLabel(key);
  }

  function defaultFieldLabel(key) {
    const d = DEFAULT_FIELDS.find((f) => f[0] === key);
    return d ? d[1] : capitalize(humanize(key));
  }

  function makeCtx() {
    const sigs = new Map();
    const nodes = new Map();
    return {
      sigs,
      nodes,
      // Register a rendered node whose value should flash when it changes between turns.
      track(node, key, value) {
        let sig;
        try {
          sig = JSON.stringify(value === undefined ? null : value);
        } catch (_) {
          sig = String(value);
        }
        sigs.set(key, sig);
        if (!nodes.has(key)) nodes.set(key, []);
        nodes.get(key).push(node);
        return node;
      },
    };
  }

  function flash(node) {
    if (!node || !node.classList) return;
    if (node.classList.contains('changed')) {
      node.classList.remove('changed');
      void node.offsetWidth; // restart the animation
    }
    node.classList.add('changed');
    const done = (e) => {
      if (e.target !== node || e.animationName !== 'flash') return;
      node.classList.remove('changed');
      node.removeEventListener('animationend', done);
    };
    node.addEventListener('animationend', done);
  }

  function activeFocusKey() {
    const active = document.activeElement;
    if (!active || !dom.inspector.contains(active)) return null;
    return active.getAttribute('data-fkey');
  }

  function restoreFocusKey(key) {
    if (!key) return;
    const node = dom.inspector.querySelector(`[data-fkey="${cssEscape(key)}"]`);
    if (node) node.focus({ preventScroll: true });
  }

  function renderInspector() {
    const focusKey = activeFocusKey();
    const ctx = makeCtx();
    let cards;
    if (!app.state) {
      cards = [placeholderCard()];
    } else {
      cards = [
        cardWorkflow(ctx),
        cardVerification(ctx),
        cardMemory(ctx),
        cardCase(ctx),
        cardGuardrails(ctx),
        cardEscalation(ctx),
        cardEmail(ctx),
        cardTrace(ctx),
        cardRaw(),
      ].filter(Boolean);
    }
    dom.inspector.replaceChildren(...cards);

    let changed = 0;
    if (app.prevSigs && app.state) {
      ctx.sigs.forEach((sig, key) => {
        if (app.prevSigs.get(key) === sig) return;
        changed += 1;
        (ctx.nodes.get(key) || []).forEach((n) => flash(n));
      });
    }
    app.prevSigs = app.state ? ctx.sigs : null;
    if (changed && isMobile() && app.activeTab !== 'inspector') markUnread('inspector');
    restoreFocusKey(focusKey);
    updateHeads();
  }

  function updateHeads() {
    dom.chatSub.textContent = app.configLoaded ? `with ${agentName()}` : '';

    const children = [];
    if (app.state) {
      const phase = str(stateOf('phase'));
      const status = statusOf();
      if (phase) {
        const p = getPhases().find((x) => x.id === phase);
        children.push(el('span', null, 'Phase'), el('strong', null, phaseLabel(phase)));
        if (p && p.freedom) children.push(freedomTag(p.freedom));
      }
      if (status === 'completed') children.push(badge('ok', 'Completed', 'check'));
      else if (status === 'escalated') children.push(badge('warn', 'Escalated', 'user'));
    }
    dom.chatPhase.replaceChildren(...children);
    dom.chatPhase.hidden = children.length === 0;

    const parts = [];
    if (app.sessionId) parts.push(`Session ${app.sessionId}`);
    if (app.state && isNum(app.state.turn)) parts.push(`turn ${app.state.turn}`);
    dom.inspectorSub.textContent = parts.join(' · ');
    if (app.sessionId) dom.inspectorSub.title = `Session ID: ${app.sessionId}`;
    else dom.inspectorSub.removeAttribute('title');
  }

  function placeholderCard() {
    let text;
    if (app.configFailed) text = 'The demo configuration could not be loaded.';
    else if (!app.configLoaded) text = 'Loading…';
    else if (app.starting) text = 'Starting a conversation…';
    else text = 'No conversation yet. Start one to see the SOP state.';
    return el('section', { class: 'card', 'aria-label': 'SOP state' },
      el('div', { class: 'card-body' }, el('p', { class: 'empty' }, text)));
  }

  function card(key, title, headExtra, body) {
    const titleId = `card-${key}-title`;
    return el('section', { class: `card card-${key}`, 'aria-labelledby': titleId },
      el('div', { class: 'card-head' }, el('h3', { class: 'card-title', id: titleId }, title), headExtra),
      el('div', { class: 'card-body' }, body));
  }

  function kvRow(label, value) {
    return el('div', { class: 'kv-row' }, el('dt', null, label), el('dd', null, value));
  }

  function kvList(rows) {
    return el('dl', { class: 'kv' }, rows);
  }

  function freedomTag(freedom) {
    const f = str(freedom).toLowerCase();
    const cls = f === 'strict' || f === 'guided' || f === 'open' ? f : 'other';
    return el('span', { class: `tag tag-freedom tag-${cls}`, title: `${capitalize(f)} freedom` }, f);
  }

  function fieldStatus(status) {
    const s = str(status);
    const map = {
      matched: ['ok', 'check', 'Matched'],
      mismatched: ['bad', 'x', 'Mismatched'],
      not_provided: ['muted', 'dash', 'Not provided'],
    };
    const entry = map[s] || ['muted', 'dash', s ? capitalize(humanize(s)) : 'Unknown'];
    return el('span', { class: `status tone-${entry[0]}` }, icon(entry[1]), entry[2]);
  }

  function consentBadge(status) {
    const s = str(status);
    if (s === 'approved') return badge('ok', 'Approved', 'check');
    if (s === 'pending') return badge('warn', 'Pending', 'clock');
    if (s === 'timed_out') return badge('bad', 'Timed out', 'x');
    if (s === 'not_requested') return badge('neutral', 'Not requested');
    if (s === 'not_required') return badge('neutral', 'Not required');
    return badge('neutral', s ? capitalize(humanize(s)) : 'Unknown');
  }

  function roleText(role) {
    const r = str(role);
    if (r === 'policyholder') return 'Policyholder';
    if (r === 'representative') return 'Representative';
    if (r && r !== 'unknown') return capitalize(humanize(r));
    return el('span', { class: 'muted' }, 'Unknown');
  }

  function provenanceTag(src) {
    if (!isObj(src)) return null;
    const phase = str(src.captured_phase);
    const turn = isNum(src.captured_turn) ? src.captured_turn : null;
    if (!phase && turn === null) return null;
    const text = [phase ? `captured in ${phaseLabel(phase)}` : 'captured', turn !== null ? `turn ${turn}` : '']
      .filter(Boolean)
      .join(' · ');
    const current = str(stateOf('phase'));
    const carried = Boolean(phase && current && phase !== current);
    return el('span', {
      class: carried ? 'prov prov-carried' : 'prov',
      title: carried ? `Captured during ${phaseLabel(phase)} and still remembered in ${phaseLabel(current)}` : null,
    }, icon('history'), text);
  }

  function memItem(label, value, source) {
    return el('li', { class: 'mem-item' },
      el('div', { class: 'mem-main' },
        el('span', { class: 'mem-label' }, label),
        el('span', { class: 'mem-value' }, value)),
      provenanceTag(source));
  }

  function idChips(list, selected) {
    const ids = arr(list).map(str).filter(Boolean);
    if (!ids.length) return el('span', { class: 'muted' }, 'None');
    const sel = str(selected);
    return el('span', { class: 'id-chips' }, ids.map((id) => el('span', {
      class: sel && id === sel ? 'id-chip id-chip-selected' : 'id-chip',
      title: sel && id === sel ? 'Selected claim' : null,
    }, id)));
  }

  /* ---------------------------------------------------------------- cards */

  function cardWorkflow(ctx) {
    const s = app.state;
    const phases = getPhases();
    const status = statusOf();
    const current = str(s.phase);
    const curIdx = phases.findIndex((p) => p.id === current);

    const stepper = el('ol', { class: 'stepper', 'aria-label': 'SOP phases' });
    stepper.style.setProperty('--steps', String(Math.max(phases.length, 1)));
    phases.forEach((p, i) => {
      let st = 'upcoming';
      if (status === 'completed') st = 'done';
      else if (curIdx >= 0 && i < curIdx) st = 'done';
      else if (i === curIdx) st = status === 'escalated' ? 'halted' : 'current';
      let marker = null;
      if (st === 'done') marker = icon('check');
      else if (st === 'halted') marker = icon('pause');
      const li = el('li', {
        class: `step step-${st}`,
        title: p.description || null,
        'aria-current': st === 'current' ? 'step' : null,
      },
      el('span', { class: 'step-marker', 'aria-hidden': 'true' }, marker),
      el('span', { class: 'step-label' }, p.label),
      p.freedom ? freedomTag(p.freedom) : null,
      el('span', { class: 'visually-hidden' }, ` (${STEP_WORDS[st]})`));
      stepper.appendChild(ctx.track(li, `wf.step.${p.id}`, st));
    });

    const body = [stepper];
    if (current && curIdx < 0) body.push(el('p', { class: 'note' }, `Current phase: ${current}`));

    body.push(el('h4', { class: 'sub-title' }, 'Phase history'));
    const hist = arr(s.phase_history).filter(isObj);
    if (hist.length) {
      body.push(el('ol', { class: 'history' }, hist.map((h, i) => ctx.track(
        el('li', { class: 'history-item' },
          el('span', { class: 'history-turn' }, `turn ${isNum(h.turn) ? h.turn : '?'}:`),
          el('span', { class: 'history-hop' }, `${h.from ? str(h.from) : 'start'} → ${h.to ? str(h.to) : '?'}`),
          h.reason ? el('span', { class: 'history-reason' }, `— ${humanize(h.reason)}`) : null),
        `wf.hist.${i}`, h))));
    } else {
      body.push(el('p', { class: 'empty' }, 'No phase changes recorded yet'));
    }

    let head;
    if (status === 'completed') head = badge('ok', 'Completed', 'check');
    else if (status === 'escalated') head = badge('warn', 'Escalated', 'user');
    else head = badge('neutral', status === 'active' ? 'Active' : capitalize(humanize(status)));
    ctx.track(head, 'wf.status', status);
    return card('workflow', 'Workflow', head, body);
  }

  function cardVerification(ctx) {
    const v = obj(app.state.verification);
    const limits = obj(app.config.limits);
    const verified = v.verified === true;
    const fields = obj(v.fields);
    const keys = Object.keys(fields).length ? Object.keys(fields) : DEFAULT_FIELDS.map((f) => f[0]);
    const required = Math.max(0, num(v.required_matches, num(limits.required_id_matches, 3)));
    const counted = keys.filter((k) => obj(fields[k]).status === 'matched').length;
    const matched = Math.max(0, num(v.matched_count, counted));

    const head = ctx.track(
      verified ? badge('ok', 'Verified', 'check') : badge('neutral', 'Not verified', 'lock'),
      'v.verified', verified);

    const meter = el('div', { class: 'meter', 'aria-hidden': 'true' });
    const segments = clamp(required || 1, 1, 12);
    for (let i = 0; i < segments; i += 1) {
      meter.appendChild(el('span', { class: i < matched ? 'meter-seg on' : 'meter-seg' }));
    }
    const progress = ctx.track(el('div', { class: 'progress' },
      el('div', null, el('strong', null, `${matched} of ${required}`), ' required matches'),
      meter), 'v.progress', [matched, required]);

    const fieldList = el('ul', { class: 'fields', 'aria-label': 'Identity fields' }, keys.map((k) => {
      const f = obj(fields[k]);
      return ctx.track(el('li', { class: 'field-row' },
        el('span', { class: 'field-label' }, str(f.label) || defaultFieldLabel(k)),
        fieldStatus(f.status)), `v.f.${k}`, f.status === undefined ? null : f.status);
    }));

    const pn = obj(v.policy_number);
    const locator = ctx.track(el('div', { class: 'locator' },
      el('div', { class: 'field-row' },
        el('span', { class: 'field-label' }, str(pn.label) || 'Policy number'),
        fieldStatus(pn.status)),
      el('p', { class: 'locator-caption' }, 'locator only — does not count')),
    'v.policy', pn.status === undefined ? null : pn.status);

    const failed = Math.max(0, num(v.failed_attempts, 0));
    const maxFailed = num(v.max_failed_attempts, num(limits.max_failed_attempts, 3));
    let failTone = null;
    if (failed > 0) failTone = failed >= maxFailed ? 'tone-bad' : 'tone-warn';
    const consent = obj(v.consent);
    const rows = [
      ctx.track(kvRow('Failed attempts', el('span', { class: failTone }, `${failed} / ${maxFailed}`)),
        'v.failed', [failed, maxFailed]),
      ctx.track(kvRow('Caller role', roleText(v.caller_role)), 'v.role',
        v.caller_role === undefined ? null : v.caller_role),
    ];
    if (consent.required !== true) {
      rows.push(ctx.track(kvRow('Consent', el('span', { class: 'muted' }, 'Not required')), 'v.consent', consent));
    }
    const body = [progress, fieldList, locator, kvList(rows)];

    if (isObj(v.representative)) {
      const rep = v.representative;
      let onFile;
      if (rep.on_file === true) onFile = badge('ok', 'Yes', 'check');
      else if (rep.on_file === false) onFile = badge('bad', 'No', 'x');
      else onFile = badge('neutral', 'Unknown');
      body.push(ctx.track(el('div', { class: 'subblock' },
        el('h4', { class: 'sub-title' }, 'Representative'),
        kvList([
          kvRow('Name', str(rep.name) || '—'),
          kvRow('Relationship', rep.relationship ? capitalize(str(rep.relationship)) : '—'),
          kvRow('On file', onFile),
        ])), 'v.rep', rep));
    }

    if (consent.required === true) {
      body.push(ctx.track(el('div', { class: 'subblock' },
        el('h4', { class: 'sub-title' }, 'Policyholder consent'),
        kvList([
          kvRow('Status', consentBadge(consent.status)),
          kvRow('Polls', String(Math.max(0, num(consent.polls, 0)))),
          app.sessionScenario ? kvRow('Scenario', app.sessionScenario) : null,
        ])), 'v.consent', consent));
    }

    return card('verification', 'Identity verification', head, body);
  }

  function cardMemory(ctx) {
    const m = obj(app.state.memory);
    const items = [];

    const intent = m.intent;
    if (isObj(intent) && intent.value !== null && intent.value !== undefined && intent.value !== '') {
      items.push(ctx.track(memItem('Intent', humanizeCode(intent.value), intent), 'm.intent', intent));
    } else if (typeof intent === 'string' && intent) {
      items.push(ctx.track(memItem('Intent', humanizeCode(intent), null), 'm.intent', intent));
    }

    arr(m.claim_hints).forEach((h, i) => {
      if (!isObj(h)) return;
      const label = str(h.label) || (h.key ? capitalize(humanize(h.key)) : 'Claim hint');
      const value = str(h.value);
      items.push(ctx.track(memItem(label, value || '—', h), `m.hint.${h.key ? str(h.key) : i}`, h));
    });

    arr(m.notes).forEach((n, i) => {
      if (isObj(n)) items.push(ctx.track(memItem('Note', str(n.text) || '—', n), `m.note.${i}`, n));
      else if (typeof n === 'string' && n) items.push(ctx.track(memItem('Note', n, null), `m.note.${i}`, n));
    });

    const pref = m.email_summary_preference;
    if (isObj(pref) && pref.value !== null && pref.value !== undefined && pref.value !== '') {
      const v = str(pref.value);
      let text = humanizeCode(v);
      if (v === 'send') text = 'Send the email summary';
      else if (v === 'skip') text = 'Skip the email summary';
      items.push(ctx.track(memItem('Email summary preference', text, pref), 'm.emailpref', pref));
    }

    const head = items.length ? el('span', { class: 'count' }, plural(items.length, 'item')) : null;
    const body = items.length
      ? el('ul', { class: 'mem-list' }, items)
      : el('p', { class: 'empty' }, 'Nothing captured yet');
    return card('memory', 'Memory', head, body);
  }

  function cardCase(ctx) {
    const c = obj(app.state.case);
    const verified = obj(app.state.verification).verified === true;
    if (!verified) {
      return card('case', 'Case', ctx.track(badge('neutral', 'Locked', 'lock'), 'c.locked', true),
        el('p', { class: 'locked' }, icon('lock'), 'Locked until identity is verified'));
    }
    const selected = str(c.selected_case_id);
    const rows = [
      ctx.track(kvRow('Claims on file', isNum(c.claims_on_file) || c.claims_on_file
        ? str(c.claims_on_file)
        : el('span', { class: 'muted' }, '—')), 'c.claims', c.claims_on_file === undefined ? null : c.claims_on_file),
      ctx.track(kvRow('Selected claim', selected
        ? el('span', { class: 'case-id' }, selected)
        : el('span', { class: 'muted' }, 'None selected')), 'c.selected', selected),
      ctx.track(kvRow('Workflow path', c.intent
        ? humanizeCode(c.intent)
        : el('span', { class: 'muted' }, 'Not determined')), 'c.intent', c.intent === undefined ? null : c.intent),
      ctx.track(kvRow('Candidates', idChips(c.candidates, selected)), 'c.candidates', arr(c.candidates)),
      ctx.track(kvRow('Cases discussed', idChips(c.cases_discussed)), 'c.discussed', arr(c.cases_discussed)),
    ];
    return card('case', 'Case', null, kvList(rows));
  }

  function cardGuardrails(ctx) {
    const g = obj(app.state.guardrails);
    const limits = obj(app.config.limits);

    const oos = obj(g.out_of_scope);
    const oosLimit = Math.max(0, num(oos.limit, num(limits.out_of_scope_limit, 3)));
    const oosCount = Math.max(0, num(oos.consecutive, 0));
    const oosTotal = Math.max(0, num(oos.total, 0));
    const dots = el('span', { class: 'dots', 'aria-hidden': 'true' });
    const dotCount = clamp(oosLimit || 1, 1, 10);
    for (let i = 0; i < dotCount; i += 1) {
      let cls = 'dot';
      if (i < oosCount) cls += oosCount >= oosLimit ? ' on bad' : ' on';
      dots.appendChild(el('span', { class: cls }));
    }
    const oosValue = el('span', { class: 'gr-value' },
      dots,
      el('span', null, `${oosCount} of ${oosLimit} in a row`),
      el('span', { class: 'muted' }, `${oosTotal} total`));

    const emo = obj(g.emotion);
    const intensity = clamp(Math.round(num(emo.intensity, 0)), 0, 3);
    const bars = el('span', { class: 'bars', 'aria-hidden': 'true' });
    for (let i = 1; i <= 3; i += 1) bars.appendChild(el('span', { class: i <= intensity ? `bar on lvl-${intensity}` : 'bar' }));
    const emoValue = el('span', { class: 'gr-value' },
      el('span', null, emo.label ? capitalize(humanize(emo.label)) : 'Unknown'),
      bars,
      el('span', { class: 'muted' }, `intensity ${intensity}/3`),
      el('span', { class: 'muted' }, `elevated streak ${Math.max(0, num(emo.elevated_streak, 0))}`));

    const ref = obj(g.refusals);
    const refCount = Math.max(0, num(ref.count, 0));
    const refMax = num(ref.max_persuasion_attempts, num(limits.max_persuasion_attempts, 2));
    let refTone = null;
    if (refCount > 0) refTone = refCount >= refMax ? 'tone-bad' : 'tone-warn';
    const refValue = el('span', { class: 'gr-value' },
      el('span', { class: refTone }, `${refCount} of ${refMax}`),
      el('span', { class: 'muted' }, 'max persuasion attempts'));

    const offer = g.human_offer_pending === true;
    const blocks = Math.max(0, num(g.guard_blocks, 0));

    const rows = [
      ctx.track(kvRow('Out-of-scope strikes', oosValue), 'g.oos', oos),
      ctx.track(kvRow('Emotion', emoValue), 'g.emotion', emo),
      ctx.track(kvRow('Refusals', refValue), 'g.refusals', ref),
      ctx.track(kvRow('Human offer pending', offer ? badge('warn', 'Yes') : el('span', { class: 'muted' }, 'No')),
        'g.offer', offer),
      ctx.track(kvRow('Guard blocks', el('span', { class: blocks > 0 ? 'tone-warn' : null }, String(blocks))),
        'g.blocks', blocks),
    ];
    return card('guardrails', 'Guardrails', null, kvList(rows));
  }

  function cardEscalation(ctx) {
    const e = stateOf('escalation');
    if (e === null || e === undefined) return null;
    const rows = [];
    let summary = '';
    if (isObj(e)) {
      rows.push(kvRow('Reason', e.reason ? humanizeCode(e.reason) : el('span', { class: 'muted' }, '—')));
      if (isNum(e.turn)) rows.push(kvRow('Turn', String(e.turn)));
      summary = str(e.handoff_summary);
    } else {
      rows.push(kvRow('Detail', str(e)));
    }
    const node = card('escalation', 'Escalation', badge('warn', 'Handed to a human', 'user'), [
      kvList(rows),
      el('h4', { class: 'sub-title' }, 'Handoff summary'),
      summary ? el('div', { class: 'handoff prewrap' }, summary) : el('p', { class: 'empty' }, 'No handoff summary provided'),
    ]);
    return ctx.track(node, 'esc', e);
  }

  function cardEmail(ctx) {
    const em = obj(app.state.email);
    const outbox = arr(em.outbox).filter(isObj);
    let decision = el('span', { class: 'muted' }, 'None yet');
    if (em.decision === 'send') decision = 'Send';
    else if (em.decision === 'skip') decision = 'Skip';
    else if (em.decision) decision = humanizeCode(em.decision);
    const rows = [
      ctx.track(kvRow('Summary offered', em.offered === true ? 'Yes' : el('span', { class: 'muted' }, 'No')),
        'em.offered', em.offered === true),
      ctx.track(kvRow('Decision', decision), 'em.decision', em.decision === undefined ? null : em.decision),
    ];
    const list = outbox.length
      ? el('ul', { class: 'outbox' }, outbox.map((m, i) => ctx.track(emailCard(m), `em.out.${i}`, m)))
      : el('p', { class: 'empty' }, 'No email sent');
    const head = outbox.length ? el('span', { class: 'count' }, `${outbox.length} sent`) : null;
    return card('email', 'Email', head, [kvList(rows), el('h4', { class: 'sub-title' }, 'Outbox'), list]);
  }

  function emailCard(m) {
    return el('li', { class: 'email' },
      el('div', { class: 'email-head' },
        icon('mail'),
        el('dl', { class: 'email-meta' },
          el('div', null, el('dt', null, 'To'), el('dd', null, str(m.to) || '—')),
          el('div', null, el('dt', null, 'Subject'), el('dd', null, str(m.subject) || '(no subject)'))),
        isNum(m.sent_turn) ? el('span', { class: 'email-turn' }, `turn ${m.sent_turn}`) : null),
      el('div', { class: 'email-body' }, str(m.body) || '(empty body)'));
  }

  function traceTitle(t) {
    const parts = [isNum(t.turn) ? `Turn ${t.turn}` : 'Turn ?'];
    const before = str(t.phase_before);
    const after = str(t.phase_after);
    if (before && after && before !== after) parts.push(`${before} → ${after}`);
    else if (after || before) parts.push(after || before);
    const latency = fmtLatency(t.latency_ms);
    if (latency) parts.push(latency);
    return parts.join(' · ');
  }

  function renderEvent(ev, traceKey, index) {
    const e = isObj(ev) ? ev : { kind: 'event', label: str(ev), detail: null };
    const kind = str(e.kind) || 'event';
    const family = Object.prototype.hasOwnProperty.call(KIND_FAMILY, kind) ? KIND_FAMILY[kind] : 'other';
    const main = [el('div', { class: 'event-label' }, str(e.label) || '(no label)')];
    if (e.detail !== null && e.detail !== undefined) {
      const openKey = `d:${traceKey}:${index}`;
      const details = el('details', { class: 'event-detail', 'data-open-key': openKey },
        el('summary', { class: 'detail-summary', 'data-fkey': openKey }, icon('chevron', 'chev'), 'Detail'),
        el('pre', { class: 'json' }, prettyJson(e.detail)));
      if (app.detailOpen.has(openKey)) details.open = true;
      main.push(details);
    }
    return el('li', { class: 'event', 'data-kind': kind },
      el('span', { class: `kind-badge kind-${family}`, title: kind }, kind),
      el('div', { class: 'event-main' }, main));
  }

  function cardTrace(ctx) {
    const traces = sortedTraces();
    if (!traces.length) {
      return card('trace', 'Turn trace', null,
        el('p', { class: 'empty' }, 'No turns yet. Each reply adds that turn’s harness decisions here.'));
    }
    const newest = traces[0]._key;
    const groups = traces.map((t) => {
      const events = arr(t.events);
      const open = app.traceOpen.has(t._key) ? app.traceOpen.get(t._key) : t._key === newest;
      const summary = el('summary', { class: 'trace-summary', 'data-fkey': `trace:${t._key}` },
        icon('chevron', 'chev'),
        el('span', { class: 'trace-title' }, traceTitle(t)),
        el('span', { class: 'trace-count' }, plural(events.length, 'event')));
      const body = events.length
        ? el('ol', { class: 'events' }, events.map((ev, i) => renderEvent(ev, t._key, i)))
        : el('p', { class: 'empty events' }, 'No events recorded for this turn');
      const group = el('details', { class: 'trace-group', 'data-trace-key': t._key, 'data-open-key': `t:${t._key}` },
        summary, body);
      if (open) group.open = true;
      ctx.track(summary, `t.${t._key}`, [events.length, t.latency_ms === undefined ? null : t.latency_ms]);
      return group;
    });
    return card('trace', 'Turn trace', el('span', { class: 'count' }, plural(traces.length, 'turn')),
      el('div', { class: 'trace-list' }, groups));
  }

  function cardRaw() {
    const details = el('details', { class: 'raw', 'data-open-key': 'raw' },
      el('summary', { class: 'raw-summary', 'data-fkey': 'raw' }, icon('chevron', 'chev'), 'Raw state JSON'),
      el('pre', { class: 'json raw-json' }, prettyJson(app.state)));
    if (app.rawOpen) details.open = true;
    return el('section', { class: 'card card-raw', 'aria-label': 'Raw state' }, el('div', { class: 'card-body' }, details));
  }

  /* ===================================================================== boot */

  init();
})();
