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
  const SESSION_STORAGE = 'sopDemo.session'; // { id, scenario } of the conversation shown in this tab
  const VIEW_STORAGE = 'sopDemo.stateView'; // the SOP state tab last chosen in this browser tab
  const DEFAULT_TIMEOUT_MS = 30000;
  const MESSAGE_TIMEOUT_MS = 180000;
  const ROW_ALTERNATES = 3; // suggestions after the marked next step; the rest are under More

  const DEFAULT_PHASES = [
    { id: 'VERIFY_ID', label: 'Verify identity', freedom: 'strict', description: '' },
    { id: 'RESOLVE_INTENT', label: 'Resolve intent', freedom: 'guided', description: '' },
    { id: 'PROCESS_CASE', label: 'Process case', freedom: 'open', description: '' },
    { id: 'POST_PROCESS', label: 'Post-process', freedom: 'guided', description: '' },
  ];

  // Stepper labels on narrow screens; the full label stays as the accessible text.
  const SHORT_PHASE_LABELS = {
    VERIFY_ID: 'Verify',
    RESOLVE_INTENT: 'Intent',
    PROCESS_CASE: 'Case',
    POST_PROCESS: 'Follow-up',
  };

  // How much freedom the agent has in each phase. {n}: the identifiers that must match.
  const FREEDOM_TEXT = {
    VERIFY_ID: 'Strict. No claim details and no moving on until {n} identity fields match.',
    RESOLVE_INTENT: 'Guided. The agent works out which claim and what the caller needs, using what it already remembers.',
    PROCESS_CASE: 'Open. The agent answers freely, but only from the claim record.',
    POST_PROCESS: 'Guided. The agent offers an email summary; the caller chooses to send or skip.',
  };

  const DEFAULT_FIELDS = [
    ['full_name', 'Full name'],
    ['dob', 'Date of birth'],
    ['phone', 'Phone'],
    ['email', 'Email'],
    ['id_last4', 'SSN / ID last 4'],
  ];

  // Identity field status -> [class, icon, words].
  const FIELD_STATES = {
    matched: ['matched', 'check', 'matched'],
    mismatched: ['mismatched', 'x', 'did not match'],
    not_provided: ['missing', 'dash', 'not provided'],
  };

  const STEP_WORDS = {
    done: 'done',
    current: 'current phase',
    halted: 'stopped here, handed to a human',
    upcoming: 'not reached yet',
  };

  // Trace event kinds that get a colour of their own; everything else is neutral.
  const KIND_TONE = {
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
    conversation_busy: 'Still answering',
    server_busy: 'Server busy',
    payload_too_large: 'Message too large',
    conversation_closed: 'Conversation closed',
    internal_error: 'Server error',
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
    'server_busy', 'payload_too_large', 'rate_limited',
  ]);
  // "Busy, come back shortly" answers (409 conversation_busy, 503 server_busy, any 429): the
  // message is sent again by itself a few times, honouring Retry-After, while the typing
  // indicator stays up; only then does the error row with its Retry button appear.
  const AUTO_RETRY_CODES = new Set(['conversation_busy', 'server_busy']);
  const AUTO_RETRIES = 3;
  const AUTO_RETRY_MAX_WAIT_S = 10; // a longer Retry-After goes straight to the error row

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

  // Points inside a phase that have suggestions of their own (the `when` tags of
  // SAMPLE_PROMPTS in server.py; see suggestionMoment()), as headed in the examples dialog.
  const MOMENT_TEXT = {
    consent: 'While a representative waits for the policyholder’s consent',
    email_offer: 'When the agent offers the email summary',
    wrapped_up: 'After the conversation is wrapped up',
  };

  // The tabs of the SOP state pane, in order.
  const VIEWS = ['overview', 'trace', 'json'];

  // 16x16 stroke icons (path data only; rendered with createElementNS).
  const ICONS = {
    check: ['M3.5 8.5 6.5 11.5 12.5 4.5'],
    x: ['M4.5 4.5 11.5 11.5', 'M11.5 4.5 4.5 11.5'],
    dash: ['M5 8h6'],
    lock: ['M4 7.5h8v6H4z', 'M5.75 7.5V5.25a2.25 2.25 0 0 1 4.5 0V7.5'],
    gear: [
      'M6.74 3.47L6.85 1.81A6.3 6.3 0 0 1 9.15 1.81L9.26 3.47A4.7 4.7 0 0 1 10.31 3.91L11.57 2.81A6.3 6.3 0 0 1 '
        + '13.19 4.43L12.09 5.69A4.7 4.7 0 0 1 12.53 6.74L14.19 6.85A6.3 6.3 0 0 1 14.19 9.15L12.53 9.26A4.7 4.7 0 0 1 '
        + '12.09 10.31L13.19 11.57A6.3 6.3 0 0 1 11.57 13.19L10.31 12.09A4.7 4.7 0 0 1 9.26 12.53L9.15 14.19A6.3 6.3 0 0 1 '
        + '6.85 14.19L6.74 12.53A4.7 4.7 0 0 1 5.69 12.09L4.43 13.19A6.3 6.3 0 0 1 2.81 11.57L3.91 10.31A4.7 4.7 0 0 1 '
        + '3.47 9.26L1.81 9.15A6.3 6.3 0 0 1 1.81 6.85L3.47 6.74A4.7 4.7 0 0 1 3.91 5.69L2.81 4.43A6.3 6.3 0 0 1 '
        + '4.43 2.81L5.69 3.91A4.7 4.7 0 0 1 6.74 3.47z',
      'M8 6.1a1.9 1.9 0 1 0 0 3.8a1.9 1.9 0 1 0 0-3.8z',
    ],
    database: [
      'M3 4c0-1.1 2.24-2 5-2s5 .9 5 2-2.24 2-5 2-5-.9-5-2z',
      'M3 4v8c0 1.1 2.24 2 5 2s5-.9 5-2V4',
      'M3 8c0 1.1 2.24 2 5 2s5-.9 5-2',
    ],
    plus: ['M8 3.5v9', 'M3.5 8h9'],
    send: ['M8 12.75V3.5', 'M4 7.5 8 3.5l4 4'],
    copy: ['M5.5 5.5h7v8h-7z', 'M3.5 10.5v-8h7'],
    chevron: ['M6 4l4 4-4 4'],
    mail: ['M2.5 4h11v8h-11z', 'M2.5 4.5 8 9l5.5-4.5'],
    alert: ['M8 2.5 14 13H2z', 'M8 6.5v3', 'M8 11.25v.01'],
    info: ['M8 2.5a5.5 5.5 0 1 0 0 11a5.5 5.5 0 1 0 0-11z', 'M8 7.25V11', 'M8 5v.01'],
    retry: ['M13 8a5 5 0 1 1-1.46-3.54', 'M13 2.5V5h-2.5'],
    user: ['M8 2.5a2.5 2.5 0 1 0 0 5a2.5 2.5 0 1 0 0-5z', 'M3 13.5c.6-2.6 2.6-4 5-4s4.4 1.4 5 4'],
    pause: ['M6 4.5v7', 'M10 4.5v7'],
    arrow: ['M3 8h9.5', 'M9 4.5 12.5 8 9 11.5'],
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
    open: new Set(), // expanded text toggles in the Overview (email, handoff summary)
    pending: false,
    starting: false,
    sendController: null,
    sessionController: null,
    failed: null, // { text, row, retryBtn, userRow } for the latest failed message
    sessionLost: false,
    keyError: '', // code of the last API-key error, until a key is saved or a turn succeeds
    activeTab: 'chat', // narrow screens: chat | inspector
    view: 'overview', // SOP state tab: overview | trace | json
    memKey: '',
    typingTimer: 0,
    firstSession: true,
    userTurns: [], // { turn, text } of every caller message in this conversation
    suggestKey: null, // what the suggestion row shows, to skip needless re-renders
    emailSeq: 0,
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

  function numberWord(n) {
    const words = ['zero', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine', 'ten'];
    return Number.isInteger(n) && n >= 0 && n < words.length ? words[n] : String(n);
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

  function muted(text) {
    return el('span', { class: 'muted' }, text);
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
    constructor(code, message, status, retryAfter) {
      super(message);
      this.name = 'ApiError';
      this.code = code;
      this.status = status || 0;
      this.retryAfter = retryAfter || 0; // seconds, from a Retry-After header
    }
  }

  function canAutoRetry(err) {
    return (AUTO_RETRY_CODES.has(err.code) || err.status === 429)
      && (err.retryAfter || 1) <= AUTO_RETRY_MAX_WAIT_S;
  }

  // Resolves after `ms`, or at once when the request is cancelled.
  function pause(ms, signal) {
    return new Promise((resolve) => {
      if (signal.aborted) {
        resolve();
        return;
      }
      const timer = window.setTimeout(resolve, ms);
      signal.addEventListener('abort', () => {
        window.clearTimeout(timer);
        resolve();
      }, { once: true });
    });
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
      const retryAfter = Number.parseInt(res.headers.get('Retry-After') || '', 10);
      throw new ApiError(code, message, res.status, Number.isFinite(retryAfter) && retryAfter > 0 ? retryAfter : 0);
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

  /* -------------------------------------- which conversation this tab shows */

  // Kept in sessionStorage so a reload reopens the same conversation. Without storage
  // (private mode, blocked site data) a reload simply starts a new one.
  function isSessionId(v) {
    return typeof v === 'string' && /^[A-Za-z0-9_-]{1,128}$/.test(v);
  }

  function storedSession() {
    try {
      const v = JSON.parse(window.sessionStorage.getItem(SESSION_STORAGE) || 'null');
      if (isObj(v) && isSessionId(v.id)) return { id: v.id, scenario: str(v.scenario) };
    } catch (_) {
      /* unavailable or unreadable: nothing stored */
    }
    return null;
  }

  function storeSession(id, scenario) {
    try {
      window.sessionStorage.setItem(SESSION_STORAGE, JSON.stringify({ id, scenario: scenario || '' }));
    } catch (_) {
      /* not persisted; a reload starts a new conversation */
    }
  }

  function forgetStoredSession() {
    try {
      window.sessionStorage.removeItem(SESSION_STORAGE);
    } catch (_) {
      /* nothing to forget */
    }
  }

  // ?session=<id> opens that conversation (and wins over the stored one).
  function urlSessionParam() {
    try {
      return new URLSearchParams(window.location.search).get('session');
    } catch (_) {
      return null;
    }
  }

  // Drop ?session= once the tab shows another conversation, so a reload keeps that one.
  function clearUrlSession() {
    try {
      const url = new URL(window.location.href);
      if (!url.searchParams.has('session')) return;
      url.searchParams.delete('session');
      window.history.replaceState(window.history.state, '', `${url.pathname}${url.search}${url.hash}`);
    } catch (_) {
      /* leave the address as it is */
    }
  }

  // The SOP state tab chosen last in this browser tab.
  function storedView() {
    try {
      const v = window.sessionStorage.getItem(VIEW_STORAGE);
      if (VIEWS.indexOf(v) >= 0) return v;
    } catch (_) {
      /* unavailable: start on the overview */
    }
    return VIEWS[0];
  }

  function storeView(view) {
    try {
      window.sessionStorage.setItem(VIEW_STORAGE, view);
    } catch (_) {
      /* not persisted */
    }
  }

  /* ===================================================================== init */

  function cacheDom() {
    const ids = {
      app: 'app',
      appName: 'app-name',
      appNameShort: 'app-name-short',
      appNameRest: 'app-name-rest',
      appSub: 'app-sub',
      btnFixtures: 'btn-fixtures',
      btnSettings: 'btn-settings',
      btnSettingsText: 'btn-settings-text',
      settingsDot: 'settings-dot',
      btnNew: 'btn-new',
      tabChat: 'tab-chat',
      tabInspector: 'tab-inspector',
      panelChat: 'panel-chat',
      panelInspector: 'panel-inspector',
      workflow: 'workflow',
      stepper: 'stepper',
      messages: 'messages',
      chatItems: 'chat-items',
      chatTyping: 'chat-typing',
      chatStatus: 'chat-status',
      samples: 'samples',
      samplesList: 'samples-list',
      btnExamples: 'btn-examples',
      composer: 'composer',
      input: 'composer-input',
      btnSend: 'btn-send',
      modelNote: 'model-note',
      modelNoteText: 'model-note-text',
      modelNoteBtn: 'model-note-btn',
      inspector: 'inspector',
      viewOverview: 'ipanel-overview',
      viewTrace: 'ipanel-trace',
      viewJson: 'ipanel-json',
      srStatus: 'sr-status',
      settingsDialog: 'settings-dialog',
      keyForm: 'key-form',
      keyError: 'key-dialog-error',
      keyMode: 'key-dialog-mode',
      keySaved: 'key-dialog-saved',
      keyInput: 'key-input',
      keyInputError: 'key-input-error',
      keyClear: 'key-clear',
      settingsDone: 'settings-done',
      consentSelect: 'consent-select',
      consentHelp: 'consent-help',
      clockSection: 'clock-section',
      settingsClock: 'settings-clock',
      drawer: 'fixtures-drawer',
      fixturesBody: 'fixtures-body',
      examplesDialog: 'examples-dialog',
      examplesBody: 'examples-body',
    };
    Object.keys(ids).forEach((k) => {
      dom[k] = document.getElementById(ids[k]);
    });
    dom.viewTabs = Array.from(document.querySelectorAll('.itab[data-view]'));
  }

  function bindUi() {
    dom.btnNew.addEventListener('click', () => {
      startSession();
    });
    dom.btnSettings.addEventListener('click', () => openSettings());
    dom.modelNoteBtn.addEventListener('click', () => openSettings());
    dom.btnFixtures.addEventListener('click', openFixtures);
    dom.consentSelect.addEventListener('change', () => {
      announce(`Consent scenario ${dom.consentSelect.value} will be used for the next new conversation.`);
    });

    dom.btnExamples.addEventListener('click', openExamples);
    bindScrollRow(dom.samplesList);
    dom.messages.addEventListener('scroll', updateWorkflowEdge, { passive: true });

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
    dom.viewTabs.forEach((tab) => {
      tab.addEventListener('click', () => selectView(tab.getAttribute('data-view')));
      tab.addEventListener('keydown', onViewKeydown);
    });

    dom.inspector.addEventListener('click', onInspectorClick);

    dom.keyForm.addEventListener('submit', onKeySave);
    dom.keyClear.addEventListener('click', onKeyClear);
    dom.settingsDone.addEventListener('click', () => closeDialog(dom.settingsDialog));
    dom.keyInput.addEventListener('input', () => {
      dom.keyInputError.hidden = true;
    });
    dom.settingsDialog.addEventListener('close', onDialogClosed);
    dom.drawer.addEventListener('close', onDialogClosed);
    dom.drawer.addEventListener('click', onBackdropClick);
    dom.fixturesBody.addEventListener('click', onFixturesClick);
    dom.examplesDialog.addEventListener('close', onDialogClosed);
    dom.examplesDialog.addEventListener('click', onBackdropClick);
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
    selectView(storedView());
    updateSettingsButton();
    renderStepper();
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
      if (KEY_ERRORS.has(err.code)) {
        noteKeyError(err);
        openSettings({ error: err.message });
      }
      renderInspector();
      updateSettingsButton();
      return;
    }
    app.config = cfg;
    app.configLoaded = true;
    dom.chatItems.replaceChildren();
    applyConfig();
    if (cfg.fixtures_exposed) loadFixtures();
    else dom.btnFixtures.hidden = true;
    if (cfg.llm_mode === 'anthropic' && !cfg.has_server_key && !getApiKey()) openSettings();
    openInitialConversation();
  }

  // After a reload, reopen the conversation this tab was showing (or the one named by
  // ?session=); otherwise start a new one.
  function openInitialConversation() {
    const param = urlSessionParam();
    const fromUrl = isSessionId(param) ? param : '';
    if (param !== null && !fromUrl) clearUrlSession();
    const stored = storedSession();
    const id = fromUrl || (stored ? stored.id : '');
    if (id) restoreSession(id, stored && stored.id === id ? stored.scenario : '');
    else startSession();
  }

  function applyConfig() {
    const c = app.config;
    const name = str(c.app_name) || 'Claims Support — SOP Agent Demo';
    // The name up to " — " is shown; the rest ("SOP Agent Demo") is what the subtitle says,
    // so it is kept for screen readers only.
    const short = name.split(/\s+[—–]\s+/)[0].trim() || name;
    dom.appNameShort.textContent = short;
    dom.appNameRest.textContent = name.slice(name.indexOf(short) + short.length);
    dom.appName.title = name;
    document.title = name;

    updateModelLine();

    const today = str(c.demo_today);
    dom.settingsClock.textContent = today ? `The agent reasons as if today were ${today}.` : '';
    dom.clockSection.hidden = !today;

    const scenarios = arr(c.consent_scenarios).map(str).filter(Boolean);
    if (!scenarios.length) scenarios.push('default');
    const previous = dom.consentSelect.value;
    dom.consentSelect.replaceChildren(...scenarios.map((s) => el('option', { value: s }, s)));
    if (scenarios.indexOf(previous) >= 0) dom.consentSelect.value = previous;
    else dom.consentSelect.value = scenarios.indexOf('default') >= 0 ? 'default' : scenarios[0];

    dom.messages.setAttribute('aria-label', `Conversation with ${agentName()}`);
    app.suggestKey = null;
    renderSuggestions();
    updateSettingsButton();
    renderStepper();
  }

  /* ------------------------------------------------------------- suggestions */

  // The suggested caller messages (SAMPLE_PROMPTS in server.py), read defensively.
  function samplePrompts() {
    return arr(app.config.sample_prompts).filter((p) => isObj(p) && str(p.text)).map((p) => {
      const text = str(p.text);
      return {
        label: str(p.label) || (text.length > 40 ? `${text.slice(0, 40)}…` : text),
        text,
        phases: arr(p.phases).map(str).filter(Boolean), // none: any phase
        when: (Array.isArray(p.when) ? p.when : [p.when]).map(str).filter(Boolean),
        happy: p.happy_path === true,
        then: str(p.then),
      };
    });
  }

  // The point the conversation has reached inside its phase, for the suggestions'
  // `when` tags: what the agent is waiting for, or '' for an ordinary turn.
  function suggestionMoment() {
    if (!app.state) return '';
    if (statusOf() === 'completed') return 'wrapped_up';
    const phase = str(stateOf('phase'));
    if (phase === 'POST_PROCESS') {
      const em = obj(stateOf('email'));
      if (em.offered === true && !em.decision) return 'email_offer';
    }
    if (phase === 'VERIFY_ID' && str(obj(obj(stateOf('verification')).consent).status) === 'pending') return 'consent';
    return '';
  }

  function normText(v) {
    return str(v).replace(/\s+/g, ' ').trim().toLowerCase();
  }

  // What the caller has said since the conversation (last) entered this phase.
  function sentInPhase(phase) {
    let entered = 0;
    arr(stateOf('phase_history')).forEach((h) => {
      if (isObj(h) && h.to === phase && isNum(h.turn)) entered = Math.max(entered, h.turn);
    });
    return new Set(app.userTurns.filter((m) => m.turn > entered).map((m) => normText(m.text)));
  }

  // What to offer now: the suggestions tagged for this point of the phase, else the phase's
  // general ones, with the happy-path step first and flagged as the suggested next step.
  function currentSuggestions() {
    if (!app.state || !canSend()) return [];
    const phase = str(stateOf('phase'));
    const moment = suggestionMoment();
    const inPhase = samplePrompts().filter((p) => !p.phases.length || p.phases.indexOf(phase) >= 0);
    const specific = moment ? inPhase.filter((p) => p.when.indexOf(moment) >= 0) : [];
    const list = specific.length ? specific : inPhase.filter((p) => !p.when.length);
    // A happy-path step already sent in this phase hands the mark on to its `then`.
    const said = sentInPhase(phase);
    let next = list.find((p) => p.happy) || null;
    const passed = new Set();
    while (next && next.then && said.has(normText(next.text)) && !passed.has(next)) {
      passed.add(next);
      const target = next.then;
      next = list.find((p) => p.label === target) || next;
    }
    const ordered = next ? [next].concat(list.filter((p) => p !== next)) : list;
    return ordered.map((p) => Object.assign({}, p, { next: p === next }));
  }

  // The one-line suggestion row: the marked next step, then the first few others in list
  // order. A happy-path step that has handed the mark on (it was just sent) is left to More.
  function rowSuggestions() {
    const items = currentSuggestions();
    const others = items.filter((p) => !p.next && !p.happy);
    return items.filter((p) => p.next).concat(others.slice(0, ROW_ALTERNATES));
  }

  function renderSuggestions() {
    const visible = Boolean(app.state) && canSend(); // nothing to suggest once escalated
    const items = visible ? rowSuggestions() : [];
    const key = [visible ? 'on' : 'off']
      .concat(items.map((p) => `${p.next ? '>' : ''}${p.label}\u0000${p.text}`))
      .join('\n');
    if (key === app.suggestKey) return;
    app.suggestKey = key;
    dom.samplesList.replaceChildren(...items.map(suggestionChip));
    dom.samples.hidden = !visible;
    dom.samplesList.scrollLeft = 0;
    updateScrollFade(dom.samplesList);
  }

  function suggestionChip(p) {
    return el('button', {
      type: 'button',
      class: p.next ? 'sample sample-next' : 'sample',
      title: p.text,
      onclick: () => useSample(p.text),
    },
    p.next ? icon('arrow') : null,
    p.next ? el('span', { class: 'visually-hidden' }, 'Suggested next step: ') : null,
    p.label);
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

  /* ------------------------------------------------------------ all examples */

  function openExamples() {
    renderExamples();
    openDialog(dom.examplesDialog);
    // Start at the examples for where the conversation is (as in the suggestion row).
    const first = dom.examplesBody.querySelector('.ex-list-current .ex-item')
      || dom.examplesBody.querySelector('.ex-group-current .ex-item')
      || dom.examplesBody.querySelector('.ex-item');
    if (first) first.focus();
  }

  function renderExamples() {
    const prompts = samplePrompts();
    const current = app.state ? str(stateOf('phase')) : '';
    const moment = suggestionMoment();
    const groups = getPhases().map((ph) => {
      const items = prompts.filter((p) => p.phases.indexOf(ph.id) >= 0);
      if (!items.length) return null;
      return exampleGroup(`ex-${ph.id}`, ph.label, items, ph.id === current ? moment : null);
    }).filter(Boolean);
    const loose = prompts.filter((p) => !p.phases.length);
    if (loose.length) groups.push(exampleGroup('ex-any', 'Any phase', loose, null));
    dom.examplesBody.replaceChildren(...(groups.length
      ? groups
      : [el('p', { class: 'empty' }, 'No examples are configured on this server.')]));
  }

  // `moment`: null unless this is the conversation's phase; then the point reached in it ('' for
  // an ordinary turn), whose list is marked as the current one.
  function exampleGroup(titleId, title, items, moment) {
    const isCurrent = moment !== null;
    const moments = [];
    items.forEach((p) => p.when.forEach((m) => {
      if (moments.indexOf(m) < 0) moments.push(m);
    }));
    const atMoment = isCurrent && moments.indexOf(moment) >= 0;
    const parts = [];
    const general = items.filter((p) => !p.when.length);
    if (general.length) parts.push(exampleList(general, isCurrent && !atMoment));
    moments.forEach((m) => {
      parts.push(el('h4', { class: 'ex-moment' }, MOMENT_TEXT[m] || capitalize(humanize(m))));
      parts.push(exampleList(items.filter((p) => p.when.indexOf(m) >= 0), atMoment && m === moment));
    });
    return el('section', { class: isCurrent ? 'ex-group ex-group-current' : 'ex-group', 'aria-labelledby': titleId },
      el('h3', { class: 'ex-phase', id: titleId },
        el('span', null, title),
        isCurrent ? el('span', { class: 'ex-current' }, 'current phase') : null),
      parts);
  }

  function exampleList(items, isCurrent) {
    return el('ul', { class: isCurrent ? 'ex-list ex-list-current' : 'ex-list' }, items.map((p) => el('li', null,
      el('button', { type: 'button', class: 'ex-item', onclick: () => pickExample(p.text) },
        el('span', { class: 'ex-label' },
          el('span', null, p.label),
          p.happy ? el('span', { class: 'ex-next' }, icon('arrow'), 'next step') : null),
        el('span', { class: 'ex-text' }, p.text)))));
  }

  function pickExample(text) {
    closeDialog(dom.examplesDialog);
    useSample(text);
  }

  /* ================================================================ settings */

  // Which model answers this tab's next message: 'offline' or 'live'. In "auto" mode the server
  // uses the live model when a key is available (this tab's or its own), else the offline mock.
  function modelInUse() {
    const c = app.config;
    if (c.llm_mode === 'mock') return 'offline';
    if (c.llm_mode === 'auto' && !c.has_server_key && !getApiKey()) return 'offline';
    return 'live';
  }

  // The header's subtitle says which model is answering.
  function updateModelLine() {
    const c = app.config;
    const models = obj(c.models);
    const respond = str(models.respond);
    const understand = str(models.understand);
    if (modelInUse() === 'offline') {
      dom.appSub.textContent = 'SOP agent demo · offline mock model';
      dom.appSub.title = c.llm_mode === 'auto'
        ? 'No API key in this tab, so the offline mock model answers. Add a key in Settings to use the live model.'
        : 'The offline mock model is a deterministic test double. No model calls are made.';
    } else {
      dom.appSub.textContent = `SOP agent demo · live model${respond ? ` · ${respond}` : ''}`;
      dom.appSub.title = `Replies: ${respond || 'not set'} · structured calls: ${understand || 'not set'}`;
    }

    // Under the message box, where it is seen on a phone too (the subtitle is hidden there).
    const byKey = app.configLoaded && c.llm_mode === 'auto' && !c.has_server_key;
    dom.modelNote.hidden = !byKey;
    if (byKey && modelInUse() === 'offline') {
      dom.modelNoteText.textContent = 'Offline mock model, fixed replies.';
      dom.modelNoteBtn.textContent = 'Add your API key for the live model';
    } else if (byKey) {
      dom.modelNoteText.textContent = `Live model${respond ? ` (${respond})` : ''} with your API key.`;
      dom.modelNoteBtn.textContent = 'Change or clear it';
    }
  }

  // Saving or clearing the key in "auto" mode switches the model mid-conversation: say so.
  function noteModelSwitch(before) {
    const now = modelInUse();
    updateModelLine();
    if (now === before || !app.sessionId) return;
    appendDivider(now === 'live' ? 'Live model from here on' : 'Offline mock model from here on', false);
    scrollChatToBottom();
  }

  // Whether the API key needs the user: 'missing' (a live model with no key anywhere),
  // 'rejected' (the key sent was refused) or ''.
  function keyAttention() {
    const c = app.config;
    if (c.llm_mode === 'mock') return '';
    if (app.keyError === 'invalid_api_key') return 'rejected';
    if (c.llm_mode === 'auto') return ''; // no key is fine there: the offline mock answers
    if (app.keyError === 'missing_api_key') return 'missing';
    if (app.configLoaded && !c.has_server_key && !getApiKey()) return 'missing';
    return '';
  }

  // The header's Settings button turns into "Add API key" (with a dot) when a key is needed.
  function updateSettingsButton() {
    const attention = keyAttention();
    // In "auto" mode a key is an invitation, not a requirement: same label, no warning dot.
    const optional = !attention && app.configLoaded && app.config.llm_mode === 'auto'
      && modelInUse() === 'offline';
    let label = 'Settings';
    if (attention === 'rejected' && getApiKey()) label = 'Check API key';
    else if (attention || optional) label = 'Add API key';
    dom.btnSettingsText.textContent = label;
    dom.btnSettings.setAttribute('aria-label', label);
    if (attention) dom.btnSettings.title = 'The model API key is needed: open the settings to add it';
    else if (optional) dom.btnSettings.title = 'Add your API key to switch this tab from the offline mock model to the live model';
    else dom.btnSettings.title = 'Model API key, consent scenario and demo clock';
    dom.btnSettings.classList.toggle('needs-key', Boolean(attention));
    dom.settingsDot.hidden = !attention;
  }

  // The settings dialog: API key, consent scenario for the next conversation, demo clock.
  // `error`: why it opened (a key the server refused or did not have).
  function openSettings(opts) {
    const o = opts || {};
    if (o.error) {
      dom.keyError.replaceChildren(icon('alert'), el('span', null, o.error));
      dom.keyError.hidden = false;
    } else {
      dom.keyError.replaceChildren();
      dom.keyError.hidden = true;
    }
    dom.keyMode.textContent = keyModeNote();
    dom.keyInput.value = '';
    dom.keyInputError.hidden = true;
    refreshSavedKeyNote();
    refreshConsentHelp();
    openDialog(dom.settingsDialog);
    if (app.config.llm_mode === 'mock' && !o.error) dom.consentSelect.focus();
    else dom.keyInput.focus();
  }

  function keyModeNote() {
    const c = app.config;
    if (!app.configLoaded) return 'The server configuration has not loaded yet.';
    if (c.llm_mode === 'mock') return 'This server runs the offline mock model, so no key is needed.';
    const model = str(obj(c.models).respond);
    const live = `This server runs the live model${model ? ` (${model})` : ''}`;
    if (c.llm_mode === 'auto' && !c.has_server_key) {
      const named = `the live model${model ? ` (${model})` : ''}`;
      return getApiKey()
        ? `This tab uses ${named} with the key saved below. Clear the key to go back to the offline mock model.`
        : `Without a key this server answers with the offline mock model. Save your Anthropic API key below and this tab uses ${named} instead.`;
    }
    if (c.has_server_key) {
      return `${live} and already has a key, so yours is optional. A key saved here is used instead of the server's for your requests.`;
    }
    return `${live} and has no key of its own. Add yours so the agent can answer.`;
  }

  function refreshConsentHelp() {
    const base = 'How the simulated policyholder-consent service answers when a representative calls. It applies to the next new conversation';
    dom.consentHelp.textContent = app.sessionId && app.sessionScenario
      ? `${base}; this one uses “${app.sessionScenario}”.`
      : `${base}.`;
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
    const before = modelInUse();
    setApiKey(value);
    app.keyError = '';
    dom.keyInput.value = '';
    updateSettingsButton();
    closeDialog(dom.settingsDialog);
    noteModelSwitch(before);
    announce(modelInUse() !== before
      ? 'API key saved. This tab now uses the live model.'
      : 'API key saved for this browser tab.');
    if (app.failed && app.failed.retryBtn && app.failed.retryBtn.isConnected) {
      app.failed.retryBtn.focus({ preventScroll: true });
    }
  }

  function onKeyClear() {
    const before = modelInUse();
    setApiKey('');
    app.keyError = '';
    dom.keyInput.value = '';
    updateSettingsButton();
    refreshSavedKeyNote();
    dom.keyMode.textContent = keyModeNote();
    noteModelSwitch(before);
    announce(modelInUse() !== before
      ? 'Browser API key cleared. This tab is back on the offline mock model.'
      : 'Browser API key cleared.');
    dom.keyInput.focus();
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
        p.party_id ? el('span', { class: 'fx-party' }, `Party ${str(p.party_id)}`) : null),
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
        el('td', null, c.status ? capitalize(humanize(c.status)) : '—')));
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
      if (active && active !== document.body) return;
      restoreComposerFocus(true);
    }, 0);
  }

  function onBackdropClick(e) {
    // A click on the ::backdrop targets the dialog element itself, outside its box.
    const d = e.currentTarget;
    if (e.target !== d) return;
    const r = d.getBoundingClientRect();
    const inside = e.clientX >= r.left && e.clientX <= r.right && e.clientY >= r.top && e.clientY <= r.bottom;
    if (!inside) closeDialog(d);
  }

  /* ============================================================ layout & tabs */

  function applyLayoutMode() {
    const mobile = isMobile();
    const panels = [
      [dom.panelChat, 'tab-chat', 'Conversation'],
      [dom.panelInspector, 'tab-inspector', 'SOP state'],
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

  // Narrow screens: the chat and the SOP state are two tabs.
  function selectTab(name, focusTab) {
    app.activeTab = name === 'inspector' ? 'inspector' : 'chat';
    dom.app.setAttribute('data-tab', app.activeTab);
    let unreadChat = false;
    [['chat', dom.tabChat], ['inspector', dom.tabInspector]].forEach(([key, tab]) => {
      const selected = key === app.activeTab;
      tab.setAttribute('aria-selected', selected ? 'true' : 'false');
      tab.tabIndex = selected ? 0 : -1;
      if (selected && key === 'chat') unreadChat = isUnread('chat');
      if (selected) clearUnread(key);
    });
    if (focusTab) (app.activeTab === 'chat' ? dom.tabChat : dom.tabInspector).focus();
    if (app.activeTab === 'chat') {
      autoGrow();
      if (unreadChat) scrollChatToBottom(); // a reply came while the chat was hidden
    }
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

  function tabDot(tab) {
    return (tab === 'chat' ? dom.tabChat : dom.tabInspector).querySelector('.tab-dot');
  }

  function markUnread(tab) {
    const dot = tabDot(tab);
    if (dot) dot.hidden = false;
  }

  function clearUnread(tab) {
    const dot = tabDot(tab);
    if (dot) dot.hidden = true;
  }

  function isUnread(tab) {
    const dot = tabDot(tab);
    return Boolean(dot && !dot.hidden);
  }

  // The SOP state pane: Overview | Trace | JSON. The choice is kept for this browser tab.
  function selectView(name, focusTab) {
    app.view = VIEWS.indexOf(name) >= 0 ? name : VIEWS[0];
    storeView(app.view);
    dom.viewTabs.forEach((tab) => {
      const selected = tab.getAttribute('data-view') === app.view;
      tab.setAttribute('aria-selected', selected ? 'true' : 'false');
      tab.tabIndex = selected ? 0 : -1;
      if (selected && focusTab) tab.focus();
    });
    [['overview', dom.viewOverview], ['trace', dom.viewTrace], ['json', dom.viewJson]].forEach(([view, panel]) => {
      panel.hidden = view !== app.view;
    });
  }

  function onViewKeydown(e) {
    const i = VIEWS.indexOf(app.view);
    let next = null;
    if (e.key === 'ArrowRight') next = VIEWS[(i + 1) % VIEWS.length];
    else if (e.key === 'ArrowLeft') next = VIEWS[(i - 1 + VIEWS.length) % VIEWS.length];
    else if (e.key === 'Home') next = VIEWS[0];
    else if (e.key === 'End') next = VIEWS[VIEWS.length - 1];
    if (!next) return;
    e.preventDefault();
    selectView(next, true);
  }

  // A one-line row that scrolls sideways when it does not fit: the clipped edge fades out,
  // and a mouse wheel scrolls it.
  function bindScrollRow(row) {
    row.addEventListener('scroll', () => updateScrollFade(row), { passive: true });
    row.addEventListener('wheel', (e) => {
      const max = row.scrollWidth - row.clientWidth;
      if (e.ctrlKey || max < 2 || Math.abs(e.deltaY) <= Math.abs(e.deltaX)) return;
      const delta = e.deltaMode === 1 ? e.deltaY * 16 : e.deltaY;
      const next = clamp(row.scrollLeft + delta, 0, max);
      if (Math.abs(next - row.scrollLeft) < 1) return; // at the end: the panel scrolls instead
      row.scrollLeft = next;
      e.preventDefault();
    }, { passive: false });
    if (typeof window.ResizeObserver === 'function') {
      new window.ResizeObserver(() => updateScrollFade(row)).observe(row);
    }
  }

  function updateScrollFade(row) {
    const max = row.scrollWidth - row.clientWidth;
    row.classList.toggle('fade-start', max > 1 && row.scrollLeft > 1);
    row.classList.toggle('fade-end', max > 1 && row.scrollLeft < max - 1);
  }

  // A hairline under the stepper only once messages scroll beneath it.
  function updateWorkflowEdge() {
    dom.workflow.classList.toggle('is-scrolled', dom.messages.scrollTop > 2);
  }

  /* ================================================================= stepper */

  // done / current / halted (where a handoff stopped it) / upcoming, per phase.
  function stepStates() {
    const phases = getPhases();
    const status = statusOf();
    const current = app.state ? str(stateOf('phase')) : '';
    const curIdx = phases.findIndex((p) => p.id === current);
    return phases.map((p, i) => {
      let st = 'upcoming';
      if (!app.state) st = 'upcoming';
      else if (status === 'completed') st = 'done';
      else if (curIdx >= 0 && i < curIdx) st = 'done';
      else if (i === curIdx) st = status === 'escalated' ? 'halted' : 'current';
      return { phase: p, st };
    });
  }

  function renderStepper() {
    const steps = stepStates();
    dom.stepper.style.setProperty('--steps', String(Math.max(steps.length, 1)));
    dom.stepper.replaceChildren(...steps.map(({ phase, st }) => {
      const short = SHORT_PHASE_LABELS[phase.id] || '';
      let marker = null;
      if (st === 'done') marker = icon('check');
      else if (st === 'halted') marker = icon('pause');
      return el('li', {
        class: `step step-${st}`,
        'aria-current': st === 'current' ? 'step' : null,
        title: phase.description || null,
      },
      el('span', { class: 'step-dot', 'aria-hidden': 'true' }, marker),
      el('span', { class: 'step-label' },
        el('span', { class: short ? 'step-full' : null }, phase.label),
        short ? el('span', { class: 'step-short', 'aria-hidden': 'true' }, short) : null),
      el('span', { class: 'visually-hidden' }, ` (${STEP_WORDS[st]})`));
    }));
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

  function lastMessageRow() {
    for (let n = dom.chatItems.lastElementChild; n; n = n.previousElementSibling) {
      if (n.classList.contains('msg')) return n;
    }
    return null;
  }

  // The agent's name goes above its message when the message before it was not the agent's.
  function agentNameLabel() {
    const last = lastMessageRow();
    if (last && last.classList.contains('msg-assistant')) return null;
    return el('p', { class: 'msg-name', 'aria-hidden': 'true' }, agentName());
  }

  // `turn` (when known) lets the trace's "Show in chat" find the message.
  function appendUserMessage(text, turn) {
    const row = el('div', { class: 'msg msg-user', 'data-turn': isNum(turn) ? String(turn) : null },
      el('div', { class: 'bubble' }, el('span', { class: 'visually-hidden' }, 'You said: '), text));
    dom.chatItems.appendChild(row);
    scrollChatToBottom();
    return row;
  }

  // `emails`: outbox entries this turn "sent"; each is shown under the reply.
  function appendAssistantMessage(text, turn, emails) {
    const row = el('div', { class: 'msg msg-assistant', 'data-turn': isNum(turn) ? String(turn) : null },
      agentNameLabel(),
      el('div', { class: text ? 'bubble' : 'bubble bubble-empty' },
        el('span', { class: 'visually-hidden' }, `${agentName()} said: `),
        text || '(empty reply)'),
      arr(emails).filter(isObj).map(chatEmailCard));
    dom.chatItems.appendChild(row);
    row.querySelectorAll('.chat-email').forEach(fitChatEmail);
    return row;
  }

  // The outbox entries sent during a turn (each records the turn it was sent in).
  function emailsForTurn(turn) {
    if (!isNum(turn)) return [];
    return arr(obj(stateOf('email')).outbox).filter((m) => isObj(m) && m.sent_turn === turn);
  }

  // The summary email, as the caller would get it: collapsed to a few lines until expanded.
  function chatEmailCard(m) {
    app.emailSeq += 1;
    const bodyId = `chat-email-body-${app.emailSeq}`;
    const toggleText = el('span', null, 'Show full email');
    const toggle = el('button', {
      type: 'button',
      class: 'link-btn email-toggle',
      'aria-expanded': 'false',
      'aria-controls': bodyId,
    }, toggleText, icon('chevron', 'chev'));
    const node = el('div', { class: 'chat-email', role: 'group', 'aria-label': 'Email summary sent' },
      el('div', { class: 'email-head' },
        el('span', { class: 'email-icon' }, icon('mail')),
        el('div', { class: 'email-head-main' },
          el('p', { class: 'email-caption' }, `Email summary sent to ${str(m.to) || 'the address on file'}`),
          el('p', { class: 'email-subject' }, str(m.subject) || '(no subject)'))),
      el('div', { class: 'email-body', id: bodyId }, str(m.body) || '(empty body)'),
      toggle);
    toggle.addEventListener('click', () => {
      const open = toggle.getAttribute('aria-expanded') !== 'true';
      toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
      node.classList.toggle('is-open', open);
      toggleText.textContent = open ? 'Show less' : 'Show full email';
    });
    return node;
  }

  // An email that fits in the collapsed height needs no toggle. Measured once it is in the
  // page; while the chat is hidden (other tab on a phone) the toggle simply stays.
  function fitChatEmail(card) {
    const body = card.querySelector('.email-body');
    if (!body || !body.clientHeight) return;
    if (body.scrollHeight <= body.clientHeight + 1) card.classList.add('is-short');
  }

  function appendSystemMessage(text) {
    const row = el('div', { class: 'msg msg-system' }, el('div', { class: 'bubble' }, text || '(empty message)'));
    dom.chatItems.appendChild(row);
    return row;
  }

  // `phase`: a phase change, read out with its prefix.
  function appendDivider(text, phase) {
    const node = el('div', { class: phase ? 'chat-divider chat-divider-phase' : 'chat-divider' },
      el('span', { class: 'chat-divider-text' },
        phase ? el('span', { class: 'visually-hidden' }, 'Phase change: ') : null,
        text));
    dom.chatItems.appendChild(node);
    return node;
  }

  function appendPhaseDivider(before, after) {
    return appendDivider(`${phaseLabel(before)} → ${phaseLabel(after)}`, true);
  }

  function renderHistoryMessage(m) {
    if (!isObj(m)) return;
    const text = str(m.text);
    const turn = isNum(m.turn) ? m.turn : null;
    if (m.role === 'user') {
      appendUserMessage(text, turn);
      return;
    }
    if (m.role === 'assistant') {
      const trace = turn !== null ? findTrace(String(turn)) : null;
      const before = trace ? str(trace.phase_before) : '';
      const after = trace ? str(trace.phase_after) : '';
      if (before && after && before !== after) appendPhaseDivider(before, after);
      appendAssistantMessage(text, turn, emailsForTurn(turn));
      return;
    }
    appendSystemMessage(text);
  }

  function scrollChatToBottom() {
    dom.messages.scrollTop = dom.messages.scrollHeight;
  }

  // Where a node sits in the scrolling chat.
  function offsetInChat(node) {
    const box = dom.messages;
    return node.getBoundingClientRect().top - box.getBoundingClientRect().top + box.scrollTop;
  }

  // Show the start of a long reply rather than jumping to its end.
  function scrollChatTo(node) {
    const box = dom.messages;
    if (!node || !node.isConnected || !box.clientHeight) {
      scrollChatToBottom();
      return;
    }
    const top = Math.max(0, offsetInChat(node) - 12);
    if (box.scrollHeight - top > box.clientHeight) box.scrollTop = top;
    else scrollChatToBottom();
  }

  // From the trace: bring a turn's messages (the caller's and the reply) into view.
  function jumpToTurn(turn) {
    const rows = Array.from(dom.chatItems.querySelectorAll(`.msg[data-turn="${cssEscape(String(turn))}"]`));
    if (!rows.length) {
      announce(`Turn ${turn} is not in the chat.`);
      return;
    }
    if (isMobile()) selectTab('chat');
    const box = dom.messages;
    const top = Math.max(0, offsetInChat(rows[0]) - 16);
    box.scrollTo({ top, behavior: mqReduced.matches ? 'auto' : 'smooth' });
    rows.forEach((r) => flash(r.querySelector('.bubble') || r));
    const target = rows[0].querySelector('.bubble');
    if (target) {
      target.tabIndex = -1;
      target.focus({ preventScroll: true });
    }
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
    const elapsed = el('span', { class: 'typing-time', 'aria-hidden': 'true' });
    dom.chatTyping.replaceChildren(
      el('div', { class: 'msg msg-assistant msg-typing' },
        agentNameLabel(),
        el('div', { class: 'typing-line' },
          el('div', { class: 'bubble typing-bubble' },
            el('span', { class: 'typing-dots', 'aria-hidden': 'true' }, el('span'), el('span'), el('span')),
            el('span', { class: 'visually-hidden' }, `${agentName()} is typing…`)),
          elapsed)));
    dom.chatTyping.hidden = false;
    app.typingTimer = window.setInterval(() => {
      const s = Math.round((Date.now() - started) / 1000);
      elapsed.textContent = s >= 2 ? `${s}s` : '';
    }, 1000);
    scrollChatToBottom();
  }

  function updateStatusUi() {
    const status = statusOf();
    let node = null;
    if (app.sessionLost) {
      node = statusNote('bad', 'alert',
        'This conversation no longer exists on the server (it expired, made room for newer ones, or the server restarted).', true);
    } else if (app.state && status === 'escalated') {
      node = statusNote('warn', 'user', 'Transferred to a human representative. This virtual session has ended.', true);
    } else if (app.state && status === 'completed') {
      node = statusNote('ok', 'check', 'Conversation completed. You can still ask a follow-up, or start a new one.', false);
    }
    dom.chatStatus.replaceChildren(...(node ? [node] : []));
    updateComposerState();
    renderSuggestions();
    renderStepper();
  }

  function statusNote(tone, iconName, text, primary) {
    return el('div', { class: `status-note status-${tone}` },
      el('p', { class: 'status-text' }, icon(iconName), el('span', null, text)),
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
    dom.samples.querySelectorAll('button').forEach((b) => {
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
    let userRow = null;
    if (isRetry) {
      userRow = app.failed ? app.failed.userRow : null;
      removeFailedRow();
    } else {
      retireFailedRow();
      userRow = appendUserMessage(text, null);
    }
    const controller = new AbortController();
    app.sendController = controller;
    app.pending = true;
    showTyping(true);
    updateComposerState();

    let data = null;
    let error = null;
    for (let attempt = 0; ; attempt += 1) {
      try {
        data = await api('POST', `/api/sessions/${encodeURIComponent(sessionId)}/messages`, { text }, {
          controller,
          timeoutMs: MESSAGE_TIMEOUT_MS,
        });
        error = null;
      } catch (err) {
        error = toApiError(err);
      }
      if (!error || attempt >= AUTO_RETRIES || !canAutoRetry(error) || controller.signal.aborted) break;
      await pause((error.retryAfter || 1) * 1000, controller.signal);
      if (controller.signal.aborted) {
        error = new ApiError('aborted', 'The request was cancelled.');
        break;
      }
    }
    if (gen !== app.gen) return; // a new conversation started meanwhile
    app.sendController = null;
    app.pending = false;
    showTyping(false);
    updateComposerState();

    if (error) {
      handleSendError(error, text, userRow);
    } else {
      try {
        handleReply(data, text, userRow);
      } catch (err) {
        // A rendering bug must not wedge the conversation; surface it instead.
        window.console.error(err);
        showFailedRow(text, new ApiError('render_error', `The reply arrived but could not be displayed: ${str(err && err.message)}`), false, userRow);
      }
    }
    restoreComposerFocus(false);
  }

  function handleReply(data, text, userRow) {
    const trace = isObj(data.trace) ? normalizeTrace(data.trace) : null;
    const sentBefore = arr(obj(stateOf('email')).outbox).length;
    if (isObj(data.state)) app.state = data.state;
    if (trace) upsertTrace(trace);
    if (app.keyError) {
      app.keyError = ''; // the key worked
      updateSettingsButton();
    }

    let first = null;
    const before = trace ? str(trace.phase_before) : '';
    const after = trace ? str(trace.phase_after) : '';
    if (before && after && before !== after) first = appendPhaseDivider(before, after);

    let turn = trace && isNum(trace.turn) ? trace.turn : null;
    if (turn === null && isNum(stateOf('turn'))) turn = stateOf('turn');
    if (userRow && isNum(turn)) userRow.setAttribute('data-turn', String(turn));
    if (text) app.userTurns.push({ turn: isNum(turn) ? turn : 0, text });
    let emails = emailsForTurn(turn);
    if (!emails.length) emails = arr(obj(stateOf('email')).outbox).slice(sentBefore); // entries without a turn
    const row = appendAssistantMessage(str(data.reply), turn, emails);

    renderInspector();
    updateStatusUi();
    scrollChatTo(first || row);
    if (isMobile() && app.activeTab !== 'chat') markUnread('chat');
  }

  function handleSendError(err, text, userRow) {
    if (err.code === 'aborted') return;
    if (err.code === 'session_not_found') {
      app.sessionLost = true;
      showFailedRow(text, err, false, userRow);
      updateStatusUi();
      scrollChatToBottom(); // the note was added after the error row
      return;
    }
    if (err.code === 'conversation_closed') {
      // The message limit was reached (perhaps in another tab): nothing to retry; show the handoff.
      showFailedRow(text, err, false, userRow);
      resyncState();
      return;
    }
    showFailedRow(text, err, true, userRow);
    if (KEY_ERRORS.has(err.code)) {
      noteKeyError(err);
      openSettings({ error: err.message });
    } else if (!NO_RESYNC.has(err.code)) {
      resyncState();
    }
  }

  // A missing-key error without a browser key proves the server has no usable key,
  // whatever the config said at load time; keep the Settings button and dialog honest.
  function noteKeyError(err) {
    if (!KEY_ERRORS.has(err.code)) return;
    app.keyError = err.code;
    if (err.code === 'missing_api_key' && !getApiKey() && app.config.has_server_key) {
      app.config.has_server_key = false;
    }
    updateSettingsButton();
  }

  function showFailedRow(text, err, canRetry, userRow) {
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
    app.failed = canRetry ? { text, row, retryBtn, userRow: userRow || null } : null;
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
    if (Array.isArray(data.messages)) app.userTurns = userTurnsOf(data.messages);
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
    app.open.clear();
    app.failed = null;
    app.sessionLost = false;
    app.pending = false;
    app.userTurns = [];
    app.suggestKey = null;
    showTyping(false);
    dom.chatItems.replaceChildren();
    dom.chatStatus.replaceChildren();
    clearUnread('chat');
    clearUnread('inspector');
  }

  function userTurnsOf(messages) {
    return arr(messages)
      .filter((m) => isObj(m) && m.role === 'user')
      .map((m) => ({ turn: isNum(m.turn) ? m.turn : 0, text: str(m.text) }));
  }

  // "New conversation · consent scenario “timeout” · 14:05" (the default scenario goes unsaid).
  function sessionDividerText(lead, scenario, time) {
    return [lead, scenario && scenario !== 'default' ? `consent scenario “${scenario}”` : '', time || '']
      .filter(Boolean)
      .join(' · ');
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
          openSettings({ error: error.message });
        }
      }
      renderInspector();
      updateStatusUi();
      return;
    }

    presentConversation(data, scenario, sessionDividerText('New conversation', scenario, clockTime()));
    if (app.sessionId) clearUrlSession(); // the address no longer names the conversation shown
  }

  // Reopen a conversation the server still has (after a reload, or from ?session=<id>).
  async function restoreSession(id, scenario) {
    abortInFlight();
    app.gen += 1;
    const gen = app.gen;
    resetConversation();
    app.starting = true;
    renderInspector();
    updateStatusUi();

    const loading = loadingRow('Reopening the conversation…');
    dom.chatItems.appendChild(loading);
    const controller = new AbortController();
    app.sessionController = controller;

    let data = null;
    let error = null;
    try {
      data = await api('GET', `/api/sessions/${encodeURIComponent(id)}`, undefined, { controller });
    } catch (err) {
      error = toApiError(err);
    }
    if (gen !== app.gen) return;
    app.sessionController = null;
    app.starting = false;
    loading.remove();

    if (error && (error.code === 'session_not_found' || error.status === 404)) {
      // Gone (expired, or the server restarted): quietly start over.
      forgetStoredSession();
      clearUrlSession();
      startSession();
      return;
    }
    if (error) {
      if (error.code !== 'aborted') {
        dom.chatItems.appendChild(errorBlock('Could not reopen the conversation', error, () => {
          restoreSession(id, scenario);
        }));
      }
      renderInspector();
      updateStatusUi();
      return;
    }
    if (!data.session_id) data.session_id = id;
    // A reopened conversation simply reappears; only a non-default consent scenario is worth a line.
    const special = scenario && scenario !== 'default';
    presentConversation(data, scenario, special ? sessionDividerText('Conversation reopened', scenario, '') : '');
  }

  // Show a conversation as the server returned it: transcript (with phase dividers and sent
  // emails), state and traces.
  function presentConversation(data, scenario, dividerText) {
    const sid = data.session_id;
    app.sessionId = sid !== null && sid !== undefined && sid !== '' ? String(sid) : null;
    app.sessionScenario = scenario || null;
    app.state = isObj(data.state) ? data.state : null;
    if (Array.isArray(data.traces)) data.traces.filter(isObj).forEach((t) => upsertTrace(normalizeTrace(t)));
    app.userTurns = userTurnsOf(data.messages);

    if (dividerText) appendDivider(dividerText, false);
    arr(data.messages).forEach(renderHistoryMessage);
    if (app.sessionId) {
      storeSession(app.sessionId, app.sessionScenario);
    } else {
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

  // A claim's type and status as the latest tool results in the trace give them
  // (get_claim_details and list_claims return both; other tools name the claim without them).
  function claimFacts(caseId) {
    const facts = {};
    sortedTraces().forEach((t) => {
      arr(t.events).slice().reverse().forEach((ev) => {
        if (!isObj(ev) || ev.kind !== 'tool') return;
        const d = ev.detail;
        const hit = Array.isArray(d)
          ? d.find((x) => isObj(x) && str(x.case_id) === caseId)
          : (isObj(d) && str(d.case_id) === caseId ? d : null);
        if (!hit) return;
        ['case_type', 'status'].forEach((k) => {
          if (facts[k] === undefined && str(hit[k])) facts[k] = hit[k];
        });
      });
    });
    return facts;
  }

  function onInspectorClick(e) {
    const target = e.target;
    const jump = target.closest('[data-jump-turn]');
    if (jump && dom.inspector.contains(jump)) {
      jumpToTurn(Number(jump.getAttribute('data-jump-turn')));
      return;
    }
    const toggle = target.closest('[data-toggle]');
    if (toggle && dom.inspector.contains(toggle)) {
      flipToggle(toggle);
      return;
    }
    const copy = target.closest('[data-copy-state]');
    if (copy && dom.inspector.contains(copy)) {
      copyState(copy);
      return;
    }
    const summary = target.closest('summary');
    if (!summary || !dom.inspector.contains(summary)) return;
    const details = summary.parentElement;
    if (!details || details.tagName !== 'DETAILS') return;
    const key = details.getAttribute('data-open-key');
    if (!key) return;
    const willOpen = !details.open; // click fires before the native toggle
    if (key.slice(0, 2) === 't:') app.traceOpen.set(key.slice(2), willOpen);
    else if (willOpen) app.detailOpen.add(key);
    else app.detailOpen.delete(key);
  }

  /* ================================================================ SOP state */

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

  // Values rendered in the SOP state are registered here; one that changed since the last
  // render briefly flashes.
  function makeCtx() {
    const sigs = new Map();
    const nodes = new Map();
    return {
      sigs,
      nodes,
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
    dom.viewOverview.replaceChildren(...overviewView(ctx));
    dom.viewTrace.replaceChildren(...traceView(ctx));
    dom.viewJson.replaceChildren(...jsonView());

    let touched = false; // a value changed in the latest turn
    if (app.prevSigs && app.state) {
      ctx.sigs.forEach((sig, key) => {
        if (app.prevSigs.get(key) === sig) return;
        touched = true;
        (ctx.nodes.get(key) || []).forEach((n) => flash(n));
      });
    }
    app.prevSigs = app.state ? ctx.sigs : null;
    if (touched && isMobile() && app.activeTab !== 'inspector') markUnread('inspector');
    restoreFocusKey(focusKey);
  }

  function placeholder() {
    let text;
    if (app.configFailed) text = 'The demo configuration could not be loaded.';
    else if (!app.configLoaded) text = 'Loading…';
    else if (app.starting) text = 'Starting a conversation…';
    else text = 'No conversation yet. Start one to see the SOP state.';
    return el('p', { class: 'sec-empty sec-placeholder' }, text);
  }

  // A titled section of the SOP state: small label, optional status on the right, content.
  function section(key, title, status, body) {
    const titleId = `sec-${key}`;
    return el('section', { class: `sec sec-${key}`, 'aria-labelledby': titleId },
      el('div', { class: 'sec-head' }, el('h3', { class: 'sec-label', id: titleId }, title), status),
      body);
  }

  function statusText(text, tone) {
    return el('span', { class: tone ? `sec-status tone-${tone}` : 'sec-status' }, text);
  }

  function rows(list) {
    const items = list.filter(Boolean);
    return items.length ? el('dl', { class: 'rows' }, items) : null;
  }

  function row(label, value) {
    return el('div', { class: 'row' }, el('dt', null, label), el('dd', null, value));
  }

  // A text button that shows or hides content in place; stays open across re-renders.
  function toggleBlock(key, labels, content) {
    const open = app.open.has(key);
    const id = `toggle-${key}`;
    const btn = el('button', {
      type: 'button',
      class: 'link-btn toggle-btn',
      'aria-expanded': open ? 'true' : 'false',
      'aria-controls': id,
      'data-toggle': key,
      'data-labels': labels.join('|'),
      'data-fkey': `toggle:${key}`,
    }, el('span', null, open ? labels[1] : labels[0]), icon('chevron', 'chev'));
    return [btn, el('div', { class: 'toggle-body', id, hidden: !open }, content)];
  }

  function flipToggle(btn) {
    const key = btn.getAttribute('data-toggle');
    const open = btn.getAttribute('aria-expanded') !== 'true';
    if (open) app.open.add(key);
    else app.open.delete(key);
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    const labels = (btn.getAttribute('data-labels') || '').split('|');
    const label = btn.querySelector('span');
    if (label && labels.length === 2) label.textContent = open ? labels[1] : labels[0];
    const body = document.getElementById(btn.getAttribute('aria-controls'));
    if (body) body.hidden = !open;
  }

  /* ---------------------------------------------------------------- overview */

  function overviewView(ctx) {
    if (!app.state) return [placeholder()];
    return [
      sectionPhase(ctx),
      sectionIdentity(ctx),
      sectionMemory(ctx),
      sectionClaim(ctx),
      sectionSafeguards(ctx),
      sectionEmail(ctx),
    ];
  }

  function requiredMatches() {
    const v = obj(stateOf('verification'));
    return Math.max(0, num(v.required_matches, num(obj(app.config.limits).required_id_matches, 3)));
  }

  function freedomText(phase) {
    const text = FREEDOM_TEXT[phase];
    if (text) return text.replace('{n}', numberWord(requiredMatches()));
    const p = getPhases().find((x) => x.id === phase);
    if (!p) return '';
    return [p.freedom ? `${capitalize(p.freedom)}.` : '', p.description].filter(Boolean).join(' ');
  }

  function sectionPhase(ctx) {
    const phase = str(stateOf('phase'));
    const status = statusOf();
    const name = ctx.track(el('p', { class: 'sec-title' }, phaseLabel(phase) || 'Unknown phase'), 'p.phase', [phase, status]);
    if (status === 'escalated') {
      const e = stateOf('escalation');
      const esc = obj(e);
      const summary = str(esc.handoff_summary);
      let reason = muted('Not recorded');
      if (esc.reason) reason = humanizeCode(esc.reason);
      else if (e && !isObj(e)) reason = str(e);
      return section('phase', 'Current phase', statusText('Handed to a human', 'warn'), [
        name,
        el('p', { class: 'sec-text' },
          `The conversation stopped here and was transferred to a human representative${isNum(esc.turn) ? ` at turn ${esc.turn}` : ''}.`),
        rows([ctx.track(row('Reason', reason), 'p.reason', esc.reason === undefined ? null : esc.reason)]),
        summary
          ? toggleBlock('handoff', ['Show handoff summary', 'Hide handoff summary'], el('p', { class: 'sec-pre' }, summary))
          : null,
      ]);
    }
    if (status === 'completed') {
      return section('phase', 'Current phase', statusText('Completed'), [
        name,
        el('p', { class: 'sec-text' }, 'The conversation is complete. A follow-up question from the caller reopens it.'),
      ]);
    }
    return section('phase', 'Current phase', null, [name, el('p', { class: 'sec-text' }, freedomText(phase))]);
  }

  function roleText(role) {
    const r = str(role);
    if (r === 'policyholder') return 'Policyholder';
    if (r === 'representative') return 'Representative';
    return r && r !== 'unknown' ? capitalize(humanize(r)) : '';
  }

  function consentValue(consent) {
    const s = str(consent.status);
    const polls = Math.max(0, num(consent.polls, 0));
    const checks = polls ? ` · ${plural(polls, 'check')}` : '';
    if (s === 'approved') return el('span', { class: 'tone-ok' }, `Approved${checks}`);
    if (s === 'pending') return `Waiting for the policyholder${checks}`;
    if (s === 'timed_out') return el('span', { class: 'tone-bad' }, `Timed out${checks}`);
    if (s === 'denied') return el('span', { class: 'tone-bad' }, `Denied${checks}`);
    if (s === 'not_requested') return 'Not requested yet';
    return s ? `${capitalize(humanize(s))}${checks}` : 'Unknown';
  }

  function sectionIdentity(ctx) {
    const v = obj(stateOf('verification'));
    const limits = obj(app.config.limits);
    const verified = v.verified === true;
    const fields = obj(v.fields);
    const keys = Object.keys(fields).length ? Object.keys(fields) : DEFAULT_FIELDS.map((f) => f[0]);
    const required = requiredMatches();
    const counted = keys.filter((k) => obj(fields[k]).status === 'matched').length;
    const matched = Math.max(0, num(v.matched_count, counted));

    const status = ctx.track(verified ? statusText('Verified', 'ok') : statusText(`${matched} of ${required}`),
      'v.status', [verified, matched, required]);

    const meter = el('div', { class: 'meter', 'aria-hidden': 'true' });
    const segments = clamp(required || 1, 1, 12);
    for (let i = 0; i < segments; i += 1) {
      meter.appendChild(el('span', { class: verified || i < matched ? 'meter-seg on' : 'meter-seg' }));
    }

    const checks = el('ul', { class: 'checks', 'aria-label': 'Identity fields' }, keys.map((k) => {
      const f = obj(fields[k]);
      const label = str(f.label) || defaultFieldLabel(k);
      const st = FIELD_STATES[str(f.status)] || ['missing', 'dash', f.status ? humanize(f.status) : 'unknown'];
      return ctx.track(el('li', { class: `check check-${st[0]}`, title: `${label}: ${st[2]}` },
        icon(st[1]),
        el('span', null, label),
        el('span', { class: 'visually-hidden' }, `: ${st[2]}`)), `v.f.${k}`, f.status === undefined ? null : f.status);
    }));

    const policy = str(obj(v.policy_number).status);
    const toward = `toward the ${numberWord(required)}`;
    let policyNote = '';
    if (policy === 'matched') policyNote = `The policy number located the account. It does not count ${toward}.`;
    else if (policy === 'mismatched') policyNote = `The policy number did not match. It only locates the account and does not count ${toward}.`;
    else if (policy && policy !== 'not_provided') policyNote = `Policy number: ${humanize(policy)}. It does not count ${toward}.`;

    const failed = Math.max(0, num(v.failed_attempts, 0));
    const maxFailed = num(v.max_failed_attempts, num(limits.max_failed_attempts, 3));
    const role = roleText(v.caller_role);
    const rep = isObj(v.representative) ? v.representative : null;
    const consent = obj(v.consent);
    const consentStatus = str(consent.status);
    const showConsent = consent.required === true
      || ['pending', 'approved', 'denied', 'timed_out'].indexOf(consentStatus) >= 0;
    let onFile = muted('Unknown');
    if (rep && rep.on_file === true) onFile = 'Yes';
    else if (rep && rep.on_file === false) onFile = el('span', { class: 'tone-bad' }, 'No');

    const details = rows([
      failed > 0
        ? ctx.track(row('Failed attempts', el('span', { class: failed >= maxFailed ? 'tone-bad' : 'tone-warn' },
          `${failed} of ${maxFailed}`)), 'v.failed', [failed, maxFailed])
        : null,
      role ? ctx.track(row('Caller', role), 'v.role', role) : null,
      rep ? ctx.track(row('Representative', str(rep.name) || muted('Not named')), 'v.rep', rep.name || null) : null,
      rep && rep.relationship ? row('Relationship', capitalize(str(rep.relationship))) : null,
      rep ? ctx.track(row('Listed on file', onFile), 'v.rep.file', rep.on_file === undefined ? null : rep.on_file) : null,
      showConsent ? ctx.track(row('Consent', consentValue(consent)), 'v.consent', [consentStatus, consent.polls || 0]) : null,
      showConsent && app.sessionScenario ? row('Consent scenario', app.sessionScenario) : null,
    ]);

    return section('identity', 'Identity', status, [
      meter,
      checks,
      policyNote ? ctx.track(el('p', { class: 'sec-note' }, policyNote), 'v.policy', policy) : null,
      details,
    ]);
  }

  // A remembered item: label, value, and the turn (and phase) it was captured in.
  function memRow(label, value, src) {
    const s = obj(src);
    const turn = isNum(s.captured_turn) ? s.captured_turn : null;
    const phase = str(s.captured_phase);
    const where = [phase ? `in ${phaseLabel(phase)}` : '', turn !== null ? `turn ${turn}` : ''].filter(Boolean).join(', ');
    return el('div', { class: 'row', title: where ? `Captured ${where}` : null },
      el('dt', null, label),
      el('dd', null,
        el('span', null, value),
        turn !== null ? el('span', { class: 'row-meta', 'aria-hidden': 'true' }, `turn ${turn}`) : null,
        where ? el('span', { class: 'visually-hidden' }, ` (captured ${where})`) : null));
  }

  function sectionMemory(ctx) {
    const m = obj(stateOf('memory'));
    const items = [];

    const intent = m.intent;
    if (isObj(intent) && intent.value !== null && intent.value !== undefined && intent.value !== '') {
      items.push(ctx.track(memRow('Intent', humanizeCode(intent.value), intent), 'm.intent', intent));
    } else if (typeof intent === 'string' && intent) {
      items.push(ctx.track(memRow('Intent', humanizeCode(intent), null), 'm.intent', intent));
    }

    arr(m.claim_hints).forEach((h, i) => {
      if (!isObj(h)) return;
      const label = str(h.label) || (h.key ? capitalize(humanize(h.key)) : 'Claim hint');
      items.push(ctx.track(memRow(label, capitalize(str(h.value)) || '—', h), `m.hint.${h.key ? str(h.key) : i}`, h));
    });

    arr(m.notes).forEach((n, i) => {
      if (isObj(n)) items.push(ctx.track(memRow('Note', str(n.text) || '—', n), `m.note.${i}`, n));
      else if (typeof n === 'string' && n) items.push(ctx.track(memRow('Note', n, null), `m.note.${i}`, n));
    });

    const pref = m.email_summary_preference;
    if (isObj(pref) && pref.value !== null && pref.value !== undefined && pref.value !== '') {
      const v = str(pref.value);
      let text = humanizeCode(v);
      if (v === 'send') text = 'Send it';
      else if (v === 'skip') text = 'Skip it';
      items.push(ctx.track(memRow('Email summary', text, pref), 'm.emailpref', pref));
    }

    return section('memory', 'Remembered', null, items.length
      ? el('dl', { class: 'rows' }, items)
      : el('p', { class: 'sec-empty' }, 'Nothing yet. Details the caller mentions early are kept for later phases.'));
  }

  function sectionClaim(ctx) {
    const c = obj(stateOf('case'));
    if (obj(stateOf('verification')).verified !== true) {
      return section('claim', 'Claim', null,
        ctx.track(el('p', { class: 'sec-locked' }, icon('lock'), 'Locked until identity is verified.'), 'c.locked', true));
    }
    const selected = str(c.selected_case_id);
    const facts = selected ? claimFacts(selected) : {};
    const candidates = arr(c.candidates).map(str).filter(Boolean);
    const discussed = arr(c.cases_discussed).map(str).filter(Boolean);
    const onFile = c.claims_on_file;
    return section('claim', 'Claim', null, rows([
      ctx.track(row('Selected claim', selected ? el('strong', null, selected) : muted('Not chosen yet')), 'c.selected', selected),
      facts.case_type ? ctx.track(row('Type', capitalize(humanize(facts.case_type))), 'c.type', facts.case_type) : null,
      facts.status ? ctx.track(row('Status', capitalize(humanize(facts.status))), 'c.status', facts.status) : null,
      ctx.track(row('Workflow path', c.intent ? humanizeCode(c.intent) : muted('Not determined yet')),
        'c.intent', c.intent === undefined ? null : c.intent),
      ctx.track(row('Claims on file', isNum(onFile) || onFile ? str(onFile) : muted('—')),
        'c.claims', onFile === undefined ? null : onFile),
      candidates.length && (!selected || candidates.length > 1)
        ? ctx.track(row('Candidates', candidates.join(', ')), 'c.candidates', candidates)
        : null,
      discussed.length > 1 ? ctx.track(row('Cases discussed', discussed.join(', ')), 'c.discussed', discussed) : null,
    ]));
  }

  function sectionSafeguards(ctx) {
    const g = obj(stateOf('guardrails'));
    const limits = obj(app.config.limits);

    const oos = obj(g.out_of_scope);
    const oosLimit = Math.max(0, num(oos.limit, num(limits.out_of_scope_limit, 3)));
    const oosCount = Math.max(0, num(oos.consecutive, 0));
    const oosTotal = Math.max(0, num(oos.total, 0));
    const dots = el('span', { class: 'dots', 'aria-hidden': 'true' });
    for (let i = 0; i < clamp(oosLimit || 1, 1, 10); i += 1) {
      let cls = 'dot';
      if (i < oosCount) cls += oosCount >= oosLimit ? ' on full' : ' on';
      dots.appendChild(el('span', { class: cls }));
    }
    const oosValue = el('span', { class: 'with-dots', title: `${oosCount} in a row, ${oosTotal} in total` },
      dots, `${oosCount} of ${oosLimit}`, el('span', { class: 'visually-hidden' }, ` in a row (${oosTotal} in total)`));

    const ref = obj(g.refusals);
    const refCount = Math.max(0, num(ref.count, 0));
    const refMax = num(ref.max_persuasion_attempts, num(limits.max_persuasion_attempts, 2));
    let refTone = null;
    if (refCount > 0) refTone = refCount >= refMax ? 'tone-bad' : 'tone-warn';

    const emo = obj(g.emotion);
    const intensity = clamp(Math.round(num(emo.intensity, 0)), 0, 3);
    const streak = Math.max(0, num(emo.elevated_streak, 0));
    const mood = emo.label ? capitalize(humanize(emo.label)) : 'Unknown';
    const moodValue = el('span', { title: `Intensity ${intensity} of 3; elevated ${plural(streak, 'turn')} running` },
      intensity > 0 ? `${mood} · ${intensity} of 3` : mood);

    let handoff = muted('Not needed');
    if (statusOf() === 'escalated') handoff = el('span', { class: 'tone-warn' }, 'Transferred');
    else if (g.human_offer_pending === true) handoff = el('span', { class: 'tone-warn' }, 'Offered');
    const blocks = Math.max(0, num(g.guard_blocks, 0));

    return section('safeguards', 'Safeguards', null, rows([
      ctx.track(row('Out of scope', oosValue), 'g.oos', [oosCount, oosLimit]),
      ctx.track(row('Refusals', el('span', { class: refTone }, `${refCount} of ${refMax}`)), 'g.refusals', [refCount, refMax]),
      ctx.track(row('Caller mood', moodValue), 'g.mood', [emo.label || null, intensity]),
      ctx.track(row('Human handoff', handoff), 'g.handoff', [statusOf() === 'escalated', g.human_offer_pending === true]),
      blocks > 0 ? ctx.track(row('Blocked replies', el('span', { class: 'tone-warn' }, String(blocks))), 'g.blocks', blocks) : null,
    ]));
  }

  function sectionEmail(ctx) {
    const em = obj(stateOf('email'));
    const outbox = arr(em.outbox).filter(isObj);
    if (outbox.length) {
      return section('email', 'Summary email', null, outbox.map((m, i) => el('div', { class: 'sent-email' },
        ctx.track(el('p', { class: 'sec-text' },
          `Sent to ${str(m.to) || 'the address on file'}`,
          isNum(m.sent_turn) ? el('span', { class: 'row-meta' }, `turn ${m.sent_turn}`) : null), `em.sent.${i}`, m),
        toggleBlock(`email-${i}`, ['Show email', 'Hide email'], [
          el('p', { class: 'email-subject' }, str(m.subject) || '(no subject)'),
          el('p', { class: 'sec-pre' }, str(m.body) || '(empty body)'),
        ]))));
    }
    let text = 'Not offered yet';
    if (em.decision === 'skip') text = 'Skipped';
    else if (em.decision === 'send') text = 'The caller chose to receive it';
    else if (em.decision) text = humanizeCode(em.decision);
    else if (em.offered === true) text = 'Offered, waiting for the caller';
    return section('email', 'Summary email', null,
      ctx.track(el('p', { class: 'sec-text' }, text), 'em.state', [em.offered === true, em.decision || null]));
  }

  /* ------------------------------------------------------------------- trace */

  // "Turn 3" in the phase history: jumps to that turn in the chat.
  function turnLink(turn, fkey) {
    if (!isNum(turn)) return el('span', { class: 'history-turn' }, 'Turn ?');
    return el('button', {
      type: 'button',
      class: 'link-btn history-turn',
      'data-jump-turn': String(turn),
      'data-fkey': fkey,
      title: 'Show this turn in the chat',
    }, `Turn ${turn}`);
  }

  function traceView(ctx) {
    if (!app.state) return [placeholder()];
    const hist = arr(stateOf('phase_history')).filter(isObj);
    const history = hist.length
      ? el('ol', { class: 'history' }, hist.map((h, i) => ctx.track(el('li', { class: 'history-item' },
        turnLink(h.turn, `jump-h:${i}`),
        el('span', { class: 'history-hop' }, `${h.from ? phaseLabel(h.from) : 'Start'} → ${h.to ? phaseLabel(h.to) : '?'}`),
        h.reason ? el('span', { class: 'history-reason' }, humanize(h.reason)) : null), `h.${i}`, h)))
      : el('p', { class: 'sec-empty' }, 'No phase changes recorded yet.');

    const traces = sortedTraces();
    const newest = traces.length ? traces[0]._key : null;
    return [
      section('history', 'Phase history', null, history),
      section('turns', 'Turns', traces.length ? statusText(plural(traces.length, 'turn')) : null, traces.length
        ? el('div', { class: 'trace-list' }, traces.map((t) => traceGroup(ctx, t, t._key === newest)))
        : el('p', { class: 'sec-empty' }, 'No turns yet. Each reply adds that turn’s harness decisions here.')),
    ];
  }

  function traceTitle(t) {
    const parts = [isNum(t.turn) ? `Turn ${t.turn}` : 'Turn ?'];
    const before = str(t.phase_before);
    const after = str(t.phase_after);
    if (before && after && before !== after) parts.push(`${phaseLabel(before)} → ${phaseLabel(after)}`);
    else if (after || before) parts.push(phaseLabel(after || before));
    return parts.join(' · ');
  }

  function traceGroup(ctx, t, isNewest) {
    const events = arr(t.events);
    const open = app.traceOpen.has(t._key) ? app.traceOpen.get(t._key) : isNewest;
    const latency = fmtLatency(t.latency_ms);
    const summary = el('summary', { class: 'trace-summary', 'data-fkey': `trace:${t._key}` },
      icon('chevron', 'chev'),
      el('span', { class: 'trace-title' }, traceTitle(t)),
      el('span', { class: 'trace-count' }, plural(events.length, 'event') + (latency ? ` · ${latency}` : '')));
    const body = el('div', { class: 'trace-body' },
      isNum(t.turn)
        ? el('button', {
          type: 'button',
          class: 'link-btn trace-jump',
          'data-jump-turn': String(t.turn),
          'data-fkey': `jump-t:${t._key}`,
        }, 'Show in chat', icon('arrow'))
        : null,
      events.length
        ? el('ol', { class: 'events' }, events.map((ev, i) => renderEvent(ev, t._key, i)))
        : el('p', { class: 'sec-empty' }, 'No events recorded for this turn.'));
    const group = el('details', { class: 'trace-group', 'data-trace-key': t._key, 'data-open-key': `t:${t._key}` },
      summary, body);
    if (open) group.open = true;
    ctx.track(summary, `t.${t._key}`, [events.length, t.latency_ms === undefined ? null : t.latency_ms]);
    return group;
  }

  function renderEvent(ev, traceKey, index) {
    const e = isObj(ev) ? ev : { kind: 'event', label: str(ev), detail: null };
    const kind = str(e.kind) || 'event';
    const tone = Object.prototype.hasOwnProperty.call(KIND_TONE, kind) ? KIND_TONE[kind] : '';
    const main = [el('div', { class: 'event-label' }, str(e.label) || '(no label)')];
    if (e.detail !== null && e.detail !== undefined) {
      const openKey = `d:${traceKey}:${index}`;
      const details = el('details', { class: 'event-detail', 'data-open-key': openKey },
        el('summary', { class: 'detail-summary', 'data-fkey': openKey }, icon('chevron', 'chev'), 'Detail'),
        el('pre', { class: 'json' }, prettyJson(e.detail)));
      if (app.detailOpen.has(openKey)) details.open = true;
      main.push(details);
    }
    return el('li', { class: tone ? `event event-${tone}` : 'event', 'data-kind': kind },
      el('span', { class: 'event-kind', title: kind }, kind),
      el('div', { class: 'event-main' }, main));
  }

  /* -------------------------------------------------------------------- JSON */

  function jsonView() {
    if (!app.state) return [placeholder()];
    const meta = [];
    if (app.sessionId) meta.push(`Session ${app.sessionId}`);
    if (isNum(stateOf('turn'))) meta.push(`turn ${stateOf('turn')}`);
    return [
      el('div', { class: 'json-head' },
        el('p', { class: 'json-meta' }, meta.join(' · ')),
        el('button', {
          type: 'button',
          class: 'btn btn-quiet btn-sm',
          'data-copy-state': true,
          'data-fkey': 'copy-state',
        }, icon('copy'), el('span', null, 'Copy'))),
      el('pre', { class: 'json raw-json', 'aria-label': 'Session state as JSON' }, prettyJson(app.state)),
    ];
  }

  async function copyState(btn) {
    const ok = await copyText(prettyJson(app.state));
    const label = btn.querySelector('span');
    btn.classList.toggle('copied', ok);
    if (label) label.textContent = ok ? 'Copied' : 'Copy failed';
    announce(ok ? 'State JSON copied to the clipboard.' : 'Copy failed. Select the text manually.');
    window.clearTimeout(btn.resetTimer);
    btn.resetTimer = window.setTimeout(() => {
      btn.classList.remove('copied');
      if (label) label.textContent = 'Copy';
    }, 1400);
  }

  /* ===================================================================== boot */

  init();
})();
