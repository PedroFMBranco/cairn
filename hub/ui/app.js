import { h, render, Component } from 'preact';
import { useState, useEffect, useLayoutEffect, useRef, useCallback, useMemo } from 'preact/hooks';
import htm from 'htm';
import { marked } from 'marked';

const html = htm.bind(h);

/* ---------- API ---------- */

const PARAMS = new URLSearchParams(location.search);
// ?fixture= swaps in sample data, and only on a page served by `cairn serve --dev` (CAIRN_DEV=1), which marks it with
// <meta name="cairn-dev">: on the real hub a link to ?fixture= opens the real dashboard.
const FIXTURE = document.querySelector('meta[name="cairn-dev"]') ? PARAMS.get('fixture') : null;
let TOKEN = document.querySelector('meta[name="cairn-token"]')?.content || '';
const POLL_MS = 10000;

const qs = (o) => new URLSearchParams(o).toString();

// Every request needs the hub's sign-in cookie, which only the link `cairn open` prints hands out. A 401 from any
// request means this browser is not signed in (or its cookie predates a new ~/.cairn/hub-secret): the page says so
// until a request gets through again, for instance after `cairn open` signed the browser in from another tab.
const SIGNED_OUT = 'this browser is not signed in to the hub; run `cairn open` on the Mac';
const signInListeners = new Set();
let signedOut = false;
const setSignedOut = (v) => {
  if (signedOut === v) return;
  signedOut = v;
  for (const f of signInListeners) f(v);
};

// The POST token is derived from the hub's secret, so it changes only when ~/.cairn/hub-secret is replaced. A page
// opened before that, in a browser signed in again since, reads the new token from / once; requests refused together
// share one read.
let tokenRead = null;
const readToken = () => (tokenRead ??= fetch('/', { cache: 'no-store' })
  .then((r) => {
    if (r.status === 401) { setSignedOut(true); return ''; }
    return r.text().then((t) => new DOMParser().parseFromString(t, 'text/html').querySelector('meta[name="cairn-token"]')?.content || '');
  })
  .catch(() => '')
  .finally(() => { tokenRead = null; }));

// GETs that return a secret (the ntfy topic) carry the token too: pass auth.
async function http(method, path, body, auth = false, retried = false) {
  const sent = TOKEN;
  const opts = { method, headers: {} };
  if (auth) opts.headers['X-Cairn-Token'] = sent;
  if (method === 'POST') {
    opts.headers['Content-Type'] = 'application/json';
    opts.headers['X-Cairn-Token'] = sent;
    opts.body = JSON.stringify(body || {});
  }
  let res;
  try {
    res = await fetch(path, opts);
  } catch (e) {
    throw new Error('hub not reachable (' + e.message + ')');
  }
  let data = null;
  try { data = await res.json(); } catch { /* non-JSON error body */ }
  if (res.status === 401) {
    setSignedOut(true);
    throw Object.assign(new Error(SIGNED_OUT), { status: res.status, data });
  }
  setSignedOut(false);
  // A refused token means the hub did nothing, so the one retry with the new token cannot act twice.
  if (res.status === 403 && data?.error === 'bad token') {
    const fresh = retried ? '' : await readToken();
    if (fresh && fresh !== sent) {
      TOKEN = fresh;
      return http(method, path, body, auth, true);
    }
    throw Object.assign(new Error(signedOut ? SIGNED_OUT : 'the hub refused this page\'s token (its sign-in secret changed): reload the page'),
      { status: res.status, data });
  }
  if (!res.ok) throw Object.assign(new Error((data && data.error) || `HTTP ${res.status} ${res.statusText}`), { status: res.status, data });
  return data;
}

const hubApi = {
  state: () => http('GET', '/api/state'),
  file: (node, project, path) => http('GET', '/api/file?' + qs({ node, project, path })),
  pane: (node, project) => http('GET', '/api/pane?' + qs({ node, project })),
  sync: (node) => http('POST', '/api/sync', node ? { node } : undefined),
  start: (node, project) => http('POST', '/api/start', { node, project }),
  stop: (node, project) => http('POST', '/api/stop', { node, project }),
  sshHosts: () => http('GET', '/api/ssh-hosts'),
  addNode: (body) => http('POST', '/api/nodes', body),
  nodeTokens: (name, allowed_tokens) => http('POST', '/api/nodes/tokens', { name, allowed_tokens }),
  updateNode: (name) => http('POST', '/api/nodes/update', { name }),
  notify: () => http('GET', '/api/notify', null, true),
  notifySetup: (body) => http('POST', '/api/notify/setup', body),
  notifyTest: () => http('POST', '/api/notify/test'),
  restart: (node, project) => http('POST', '/api/restart', { node, project }),
  dialog: (node, project) => http('GET', '/api/dialog?' + qs({ node, project })),
  answerDialog: (node, project, fingerprint, n) => http('POST', '/api/dialog', { node, project, fingerprint, n }),
  run: (node, project) => http('POST', '/api/run', { node, project }),
  runStop: (node, project) => http('POST', '/api/run/stop', { node, project }),
  leadUpdate: (node, project) => http('POST', '/api/lead/update', { node, project }),
  usage: (node, project) => http('GET', '/api/usage?' + qs({ node, project })),
  removeNode: (name, force) => http('POST', '/api/nodes/remove', force ? { name, force: true } : { name }),
  sandboxes: () => http('GET', '/api/sandboxes'),
  createSandbox: (body) => http('POST', '/api/sandboxes', body),
  sandboxStart: (node) => http('POST', '/api/sandboxes/start', { node }),
  sandboxStop: (node, force) => http('POST', '/api/sandboxes/stop', force ? { node, force: true } : { node }),
  removeSandbox: (node, confirm, force) => http('POST', '/api/sandboxes/remove', force ? { node, confirm, force: true } : { node, confirm }),
  recreateSandbox: (node, force) => http('POST', '/api/sandboxes/recreate', force ? { node, force: true } : { node }),
  addToken: (body) => http('POST', '/api/tokens', body),
  updateToken: (body) => http('POST', '/api/tokens/update', body),
  removeToken: (label, force) => http('POST', '/api/tokens/remove', force ? { label, force: true } : { label }),
  clearLimit: (label, node) => http('POST', '/api/tokens/clear-limit', node ? { label, node } : { label }),
  createProject: (body) => http('POST', '/api/projects', body),
  projectToken: (node, project, token) => http('POST', '/api/projects/token', { node, project, token }),
  projectTokens: (node, project, tokens) => http('POST', '/api/projects/token', { node, project, tokens }),
  projectModels: (node, project, model, subagent_model) => http('POST', '/api/projects/models', { node, project, model, subagent_model }),
  copyProject: (node, project) => http('POST', '/api/projects/copy', { node, project }),
  ticket: (node, project, op, fields) => http('POST', '/api/tickets', { node, project, op, ...fields }),
  // fingerprint: the one the snapshot gave the question shown (none from a node before 0.4.3), so the node refuses an
  // answer to a question rewritten since.
  answer: (node, project, id, choice, note, fingerprint) => http('POST', '/api/answer', fingerprint ? { node, project, id, choice, note, fingerprint } : { node, project, id, choice, note }),
  chatStart: (node, project, resume) => http('POST', '/api/chat/start', { node, project, resume: !!resume }),
  chatSend: (node, project, text) => http('POST', '/api/chat/send', { node, project, text }),
  chatEnd: (node, project) => http('POST', '/api/chat/end', { node, project }),
  chat: (node, project, after) => http('GET', '/api/chat?' + qs({ node, project, after })),
  chatEventsUrl: (node, project, after) => '/api/chat/events?' + qs({ node, project, after }),
};

// ?fixture=1 serves fixtures/*.json and stubs every write endpoint against an in-memory copy; ?fixture=empty also answers every question,
// ?fixture=first starts with no nodes or tokens, and ?fixture=down loads once and then finds the hub gone. With &stuck=1 a new
// review chat never leaves "starting"; &dupe=1 gives two projects a q09.
// The review chat is simulated too: fixtures/chat.json seeds an earlier chat and the scripted "why" replies.
// fixtures/sandboxes.json adds two sandbox nodes and a create job stuck in "building"; &docker=down shows Docker not running, &docker=fail a failed read.
// A new sandbox named fail-* fails once at "waiting", so Retry can be tried.
// fixtures/dialogs.json holds the dialog shown by each lead that waits at one; &dialog=changed swaps in its "next" dialog on the first answer,
// and &dialog=stuck leaves the answered dialog on screen, as when Claude Code has not repainted yet.
function fixtureApi(mode) {
  let state = null;
  let files = {};
  let script = {};
  let usage = {};
  let sbx = null;
  let dialogs = {};
  const failedOnce = new Set();
  // Notifications as the hub keeps them; the topic leaves the fixture only through notify(), as only GET /api/notify carries it.
  // &notify=error shows a failed push, &notify=off a hub that was never set up.
  const notify = PARAMS.get('notify') === 'off'
    ? { configured: false, enabled: false, server: null, topic: null, last_error: null, last_sent_at: null }
    : { configured: true, enabled: true, server: 'https://ntfy.example.com', topic: 'cairn-fixture-not-a-real-topic',
      last_error: PARAMS.get('notify') === 'error' ? 'ntfy.example.com did not answer within 10 s (timed out)' : null, last_sent_at: null };
  let loads = 0;
  const chats = {};
  const copiedPaths = new Set();
  // The cairn-node version this hub carries; nodes below it get "Update cairn-node".
  const HUB_NODE_VERSION = '0.2.0';
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const nowIso = () => new Date().toISOString().replace(/\.\d+Z$/, 'Z');
  // status mirrors the hub's: 404 for a chat that is gone, 409 for a refusal by the node.
  const fail = (msg, status) => { throw Object.assign(new Error(msg), status ? { status } : {}); };
  // As the hub: a running sandbox whose node cannot be read now refuses a stop or delete, unless forced.
  const sbUnchecked = (n, x, force) => {
    if (n && uncheckable(n) && !n.stopped && x.state === 'running' && !force) {
      throw Object.assign(new Error(`node ${n.name} is unreachable or not synced yet, so live sessions cannot be checked; do it anyway with force`),
        { status: 409, data: { unchecked: [n.name] } });
    }
  };
  const load = async () => {
    if (state) return;
    // A hub run without its dev flag serves no fixtures.
    const get = (name) => fetch(`./fixtures/${name}`).then((r) => {
      if (!r.ok) throw new Error(`this server has no fixtures/${name} (HTTP ${r.status})`);
      return r.json();
    });
    const [s, f, c, u, x, d] = await Promise.all(['state.json', 'files.json', 'chat.json', 'usage.json', 'sandboxes.json', 'dialogs.json'].map(get));
    // Fixture times are moved so the newest sync is now.
    const shift = Date.now() - Date.parse(s.synced_at);
    const shifted = (o) => JSON.parse(JSON.stringify(o), (k, v) =>
      typeof v === 'string' && /^\d{4}-\d\d-\d\dT\d\d:\d\d(:\d\d)?Z$/.test(v)
        ? new Date(Date.parse(v) + shift).toISOString().replace('.000Z', 'Z') : v);
    state = shifted(s);
    files = f;
    script = shifted(c);
    usage = shifted(u);
    dialogs = d;
    const sx = shifted(x);
    for (const n of state.nodes) n.sandbox = false;
    state.nodes.push(...sx.nodes);
    Object.assign(state.projects, sx.projects);
    sbx = PARAMS.get('docker') === 'down'
      ? { docker: false, error: 'Cannot connect to the Docker daemon at unix:///Users/you/.docker/run/docker.sock. Is the docker daemon running?', images: {}, cpus_max: null, sandboxes: [], jobs: [] }
      : sx.view;
    // The resume_at variants stay true whatever the clock: today (tile-cache), tomorrow (tile-bench), already due (isbn-check).
    const at = (days, h, m) => { const d = new Date(); d.setDate(d.getDate() + days); d.setHours(h, m, 0, 0); return d.toISOString().replace(/\.\d+Z$/, 'Z'); };
    const att = (k) => state.projects[k]?.attention;
    if (att('box-1/tile-cache')) att('box-1/tile-cache').resume_at = new Date(Math.min(Date.now() + 100 * 60000, new Date().setHours(23, 58, 0, 0))).toISOString().replace(/\.\d+Z$/, 'Z');
    if (att('box-1/tile-bench')) att('box-1/tile-bench').resume_at = at(1, 14, 32);
    if (notify.configured) notify.last_sent_at = new Date(Date.now() - 47 * 60000).toISOString().replace(/\.\d+Z$/, 'Z');
    for (const x of script.chats || []) chats[`${x.node}/${x.project}`] = { ...x, timers: [], skipped: [], answers: [] };
    for (const v of Object.values(state.projects)) if (v.copied_to) copiedPaths.add(v.copied_to);
    // &dupe=1 renames isbn-check's q03 to q09, so two projects share a question id as real ones do.
    if (PARAMS.get('dupe')) {
      for (const n of state.nodes) for (const p of n.snapshot?.projects || []) {
        if (p.name !== 'isbn-check') continue;
        for (const q of p.questions) if (q.id === 'q03') Object.assign(q, { id: 'q09', path: 'work/inbox/q09.md' });
      }
    }
    // &unknown=<project> reports that project's lead "unknown", as a node whose tmux did not answer does.
    // &sbbroken=<node> makes a running sandbox's sync fail, as when its lead broke its own cairn-node.
    const sbBroken = state.nodes.find((n) => n.sandbox && n.name === PARAMS.get('sbbroken'));
    if (sbBroken) Object.assign(sbBroken, { ok: false, error: 'node returned invalid JSON' });
    for (const n of state.nodes) for (const p of n.snapshot?.projects || []) if (p.name === PARAMS.get('unknown')) Object.assign(p, { lead: 'unknown', lead_state: null });
    if (mode === 'first') { state.nodes = []; state.tokens = []; state.projects = {}; sbx.sandboxes = []; sbx.jobs = []; for (const k of Object.keys(chats)) delete chats[k]; }
    if (mode === 'empty') {
      for (const n of state.nodes) for (const p of n.snapshot?.projects || []) {
        p.questions = p.questions.filter((q) => q.answered);
        for (const t of p.tickets) if (t.state === 'waiting') t.state = t.assignee ? 'claimed' : 'frontier';
      }
    }
    // &broken=1 adds what a misbehaving lead could make its node send: questions null and a numeric sha, an option that is
    // no string and a slash in a name, a numeric status with tickets null, and a project that is no object. Every other
    // project must show as usual.
    if (PARAMS.get('broken')) {
      const base = { lead: 'none', status: 'idle', tickets: [], adrs: [], map: { destination: '', decisions: [] }, result: null, pane_last: null };
      state.nodes.find((n) => n.name === 'box-1').snapshot.projects.push(
        { ...base, name: 'no-questions', path: '/home/agent/work/no-questions', questions: null, commits: [{ sha: 1234567, subject: 'A numeric sha', date: nowIso() }] },
        { ...base, name: 'odd/name', path: '/home/agent/work/odd', lead: 'alive', commits: [],
          questions: [{ id: 'q01', context: 'An option that is no string', options: [42, null], default: 'A', gate: false, answered: false, asked_at: nowIso(), path: 'work/inbox/q01.md', ticket: null }] },
        { ...base, name: 'odd-fields', path: '/home/agent/work/odd-fields', status: 5, tickets: null, commits: [],
          questions: [{ id: 'q02', context: 'A project whose status and tickets are malformed', options: ['A. Yes', 'B. No'], default: 'A', gate: false, answered: false, asked_at: nowIso(), path: 'work/inbox/q02.md', ticket: null }] },
        1,
      );
    }
  };
  // A string hash stands in for the node's sha256; the stub only compares fingerprints.
  const fixtureDialog = (node, project) => {
    const [, p] = find(node, project);
    const d = dialogs[`${node}/${project}`];
    if (p.lead !== 'alive' || p.lead_state !== 'dialog' || !d) return null;
    const text = d.lines.join('\n');
    let h = 2166136261;
    for (let i = 0; i < text.length; i++) h = Math.imul(h ^ text.charCodeAt(i), 16777619) >>> 0;
    return { lines: d.lines, options: d.options, selected: d.selected, fingerprint: h.toString(16).padStart(8, '0').repeat(8) };
  };
  const nodeOf = (name) => state.nodes.find((x) => x.name === name) || fail(`no node named ${name}`);
  const find = (node, project) => {
    const n = nodeOf(node);
    if (!n.ok) fail(`node ${node} is unreachable`);
    const p = n.snapshot.projects.find((x) => x.name === project);
    if (!p) fail(`no project ${project} on ${node}`);
    return [n, p];
  };
  const tokenFor = (n, p) => state.projects[`${n.name}/${p.name}`]?.token || n.allowed_tokens[0];
  const snap = () => {
    for (const t of state.tokens) t.in_use = [];
    for (const n of state.nodes) for (const p of n.snapshot?.projects || []) {
      if (p.lead !== 'alive' && p.review !== 'alive') continue;
      state.tokens.find((t) => t.label === tokenFor(n, p))?.in_use.push(`${n.name}/${p.name}`);
    }
    state.chats = Object.values(chats).map((c) => ({ node: c.node, project: c.project, state: c.state, detail: c.detail }));
    state.notify = { configured: notify.configured, enabled: notify.enabled, last_error: notify.last_error };
    for (const v of Object.values(state.projects)) {
      v.attention ??= null; v.running_url ??= null;
      if (v.attention) { v.attention.resume_error ??= null; v.attention.resume_attempts ??= 0; }
    }
    for (const v of Object.values(state.projects)) for (const k of ['model', 'subagent_model', 'copied_sha', 'copied_to']) if (v[k] === undefined) v[k] = null;
    // As the hub computes it: done, a result, a newest commit, and copied_sha (full) not matching it by prefix.
    for (const n of state.nodes) for (const p of n.snapshot?.projects || []) {
      if (typeof p !== 'object') continue;
      const k = `${n.name}/${p.name}`;
      const v = (state.projects[k] = state.projects[k] || { token: null });
      const head = p.commits[0]?.sha;
      v.result_ready = p.status === 'done' && p.result != null && !!head && !(v.copied_sha && v.copied_sha.startsWith(head));
    }
    for (const n of state.nodes) { n.node_version ??= n.snapshot?.node_version ?? null; n.update_available = !!n.node_version && n.node_version !== HUB_NODE_VERSION; }
    return JSON.parse(JSON.stringify(state));
  };
  const commit = (p, subject) => {
    const sha = Math.random().toString(16).slice(2, 9).padEnd(7, '0');
    p.commits = [{ sha, date: nowIso(), subject }, ...p.commits].slice(0, 10);
    return sha;
  };
  // Ticket state per the contract: closed, waiting, claimed, blocked, else frontier.
  const restate = (p) => {
    const open = new Set(p.tickets.filter((t) => t.status === 'open').map((t) => t.id));
    const asked = new Set(p.questions.filter((q) => !q.answered && q.ticket).map((q) => q.ticket));
    for (const t of p.tickets) {
      t.state = t.status === 'closed' ? 'closed' : asked.has(t.id) ? 'waiting' : t.assignee ? 'claimed'
        : t.blocked_by.some((id) => open.has(id)) ? 'blocked' : 'frontier';
    }
  };
  const ticketFile = (t, body) => `---\nid: ${t.id}\ntitle: ${JSON.stringify(t.title)}\ntype: ${t.type}\nstatus: ${t.status}\nassignee: ${t.assignee || ''}\nblocked_by: [${t.blocked_by.join(', ')}]\n---\n${body}`;
  const homeOf = (n) => (n.ssh.includes('@') ? `/home/${n.ssh.split('@')[0]}` : '/root');

  // Like the hub: sync the node, export the mirror's sha to ~/Downloads/<project> (or a dated, then numbered, name), never overwriting.
  const copyNow = (node, project) => {
    const [, p] = find(node, project);
    const k = `${node}/${project}`;
    const sha = p.commits[0]?.sha;
    if (!sha || !state.projects[k]?.mirror?.sha) fail(`${project} has no mirrored commit yet; try again after the next sync`, 409);
    const day = nowIso().slice(0, 10);
    const base = `/Users/you/Downloads/${project}`;
    const names = [base, `${base}-${day}`, ...Array.from({ length: 20 }, (_, i) => `${base}-${day}-${i + 2}`)];
    const path = names.find((x) => !copiedPaths.has(x));
    copiedPaths.add(path);
    const full = (sha + '4f1c9e27b0a85d63e2a17c40d9b56f8a3e21c07d').slice(0, 40);
    // A committed link to an absolute path (a virtualenv's python) is left out of the copy, as the hub does.
    const skipped = project === 'attachment-move' ? ['venv/bin/python', 'venv/bin/python3'] : [];
    state.projects[k] = { ...state.projects[k], copied_sha: full, copied_to: path, mirror: { ...state.projects[k].mirror, sha, synced_at: nowIso(), error: null } };
    return { path, sha: full, skipped };
  };
  // Results whose copy has index.html at its root, so Run it can serve them.
  const RUNNABLE = new Set(['four-in-a-row']);

  // Same checks as cairn-node answer. A note starting with "conflict" simulates another session answering first.
  const answerQ = (node, p, id, choice, note = '') => {
    const q = p.questions.find((x) => x.id === id) || fail(`no question ${id}`, 409);
    if (q.answered || /^conflict\b/i.test(note)) fail(`${id} is already answered`, 409);
    const letters = q.options.map((o) => parseOption(o).letter).filter(Boolean);
    if (choice !== 'other' && !letters.includes(choice)) fail(`choice must be one of ${letters.join(', ')} or other`, 409);
    if (choice === 'other' && !note.trim()) fail('an "other" answer needs a note', 409);
    if (note.length > 4000) fail('the note is over 4000 characters', 409);
    Object.assign(q, { answered: true, choice, answered_at: nowIso(), applied: null });
    files[`${node}/${p.name}/${q.path.replace(/\.md$/, '.answer.md')}`] = `---\nid: ${id}\nchoice: ${choice}\nanswered_at: ${q.answered_at}\n---\n${note}\n`;
    restate(p);
    return { id, choice, answered_at: q.answered_at, path: q.path.replace(/\.md$/, '.answer.md') };
  };

  // Chat simulation: events, state changes and scripted turns, timed like the spike measured.
  const LIVE_CHAT = new Set(['starting', 'working', 'waiting']);
  const chatPub = (c, after = 0) => JSON.parse(JSON.stringify({ node: c.node, project: c.project, state: c.state, detail: c.detail,
    session_id: c.session_id, started_at: c.started_at, events: c.events.filter((e) => e.seq > after) }));
  const emit = (c, kind, fields) => {
    const e = { seq: (c.events.at(-1)?.seq || 0) + 1, at: nowIso(), kind, ...fields };
    c.events.push(e);
    return e;
  };
  const setChat = (c, s, detail = null) => { c.state = s; c.detail = detail; emit(c, 'state', { state: s, detail }); };
  const chatOfKey = (node, project) => chats[`${node}/${project}`] || fail(`no chat for ${node}/${project}`, 404);
  const queue = (c, p) => openQs(p).filter((q) => !c.skipped.includes(q.id)).sort(byFocus);
  const ask = (p, q) => {
    const t = p.tickets.find((x) => x.id === q.ticket);
    return `**${q.id}**${t ? `, on *${t.title.toLowerCase()}*` : ''}. ${q.context}\n\n${q.options.join('\n')}\n\n` +
      `Gate: ${q.gate ? 'yes' : 'no'}. If you skip it, the lead already applies ${q.default}.\nReply: a letter, y = take the recommendation, later = skip, or your own text.`;
  };
  // Like the skill, every question goes in its own message that starts with **qNN**; texts is one message or several.
  // A send has already set the chat working (as the hub does, before the user event); a start has not.
  const turn = (c, tools, texts, after) => {
    const t0 = Date.now();
    const steps = c.state === 'working' ? [[100, () => {}]] : [[300, () => setChat(c, 'working')]];
    for (const [name, summary] of tools) steps.push([450, () => emit(c, 'tool', { name, summary })]);
    [].concat(texts).forEach((text, i) => steps.push([i ? 500 : 800, () => emit(c, 'text', { text })]));
    steps.push([150, () => {
      emit(c, 'turn', { ok: true, seconds: Math.round((Date.now() - t0) / 100) / 10, cost_usd: 0.012 + tools.length * 0.004 });
      after();
    }]);
    let at = 0;
    for (const [ms, fn] of steps) { at += ms; c.timers.push(setTimeout(fn, at)); }
  };
  const summary = (c, p) => {
    const lines = c.answers.map((a) => {
      const q = p.questions.find((x) => x.id === a.id);
      const o = q?.options.map(parseOption).find((x) => x.letter === a.choice);
      return `- **${a.id}** ${a.choice}: ${o ? o.text : 'your note'}`;
    });
    const left = openQs(p).length;
    return `That was the last one. Answered ${c.answers.length}, skipped ${c.skipped.length}.\n\n${lines.join('\n')}\n\n` +
      (c.answers.length ? (p.lead === 'alive' ? 'The lead is working; it reads the answers before its next ticket.'
        : 'The hub restarts the lead within about 30 seconds of this session ending, and it reads the answers first.') : '') +
      (left ? `\n\n${left === 1 ? '1 question stays open with its default' : `${left} questions stay open with their defaults`}.` : '');
  };
  // Like the hub after each turn: waiting, then (after its sync) the answered rows, and ended "finished" when the skill's
  // summary closed with "Review finished." (the hub strips that line, so the fixture never shows it).
  const afterTurn = (c, answered, finished) => {
    setChat(c, 'waiting');
    c.timers.push(setTimeout(() => {
      if (answered) emit(c, 'answered', answered);
      if (finished) setChat(c, 'ended', 'finished');
    }, 600));
  };
  // Moves on after an answer or a skip: the next question, or the summary.
  const advance = (c, p, lead, answered) => {
    const next = queue(c, p)[0];
    c.cur = next?.id || null;
    turn(c, answered ? [['Write', `work/inbox/${answered.id}.answer.md`]] : [], [lead, next ? ask(p, next) : summary(c, p)],
      () => afterTurn(c, answered, !next));
  };
  const respond = (c, p, text) => {
    const q = p.questions.find((x) => x.id === c.cur && !x.answered);
    if (!q) { turn(c, [], 'No open question is left.', () => afterTurn(c, null, true)); return; }
    const opts = q.options.map(parseOption);
    const rec = opts.find((o) => o.recommended)?.letter || q.default;
    const m = /^([A-Za-z])(?:$|[\s,.:;)-]+([\s\S]*))/.exec(text);
    if (/^(y|yes)$/i.test(text)) {
      c.answers.push({ id: q.id, choice: rec });
      answerQ(c.node, p, q.id, rec);
      advance(c, p, `Wrote ${q.id}: ${rec}, the recommendation.`, { id: q.id, choice: rec });
    } else if (/^later$/i.test(text)) {
      c.skipped.push(q.id);
      advance(c, p, `Skipped ${q.id}; the lead keeps the default ${q.default}.`, null);
    } else if (m && opts.some((o) => o.letter === m[1].toUpperCase())) {
      const choice = m[1].toUpperCase();
      c.answers.push({ id: q.id, choice });
      answerQ(c.node, p, q.id, choice, (m[2] || '').trim());
      advance(c, p, `Wrote ${q.id}: ${choice}${m[2]?.trim() ? ', with your note' : ''}.`, { id: q.id, choice });
    } else if (/\?|^(why|how|what|which|show|explain|can|could|is|are|does|do|should|would)\b/i.test(text)) {
      const s = script.why?.[`${c.node}/${c.project}`]?.[q.id];
      const t = p.tickets.find((x) => x.id === q.ticket);
      const tools = s?.tools || [['Read', t?.path || q.path], ['Bash', `git log --oneline -5 -- ${t?.path || q.path}`]];
      const body = s?.text || `${q.context} The recommendation, ${rec}: ${opts.find((o) => o.letter === rec)?.reason || 'it is the default the lead already applies'}.`;
      turn(c, tools, `${body}\n\n${opts.map((o) => `${o.letter}. ${o.text}${o.recommended ? ' (recommended)' : ''}`).join('\n')}`, () => setChat(c, 'waiting'));
    } else {
      c.answers.push({ id: q.id, choice: 'other' });
      answerQ(c.node, p, q.id, 'other', text);
      advance(c, p, `Wrote ${q.id} as your own answer.`, { id: q.id, choice: 'other' });
    }
  };

  return {
    state: async () => {
      await load(); await wait(120);
      if (mode === 'down' && loads++) fail('hub not reachable (Failed to fetch)');
      return snap();
    },
    sync: async () => {
      await load(); await wait(600);
      state.synced_at = nowIso();
      for (const n of state.nodes) n.synced_at = state.synced_at;
      return snap();
    },
    file: async (node, project, path) => {
      await load(); await wait(150);
      const [, p] = find(node, project);
      let content = files[`${node}/${project}/${path}`];
      const t = p.tickets.find((x) => x.path === path);
      if (content == null && t) content = ticketFile(t, `## Question\n\n${t.title}.\n`);
      if (content == null) fail(`fixture has no ${path}`);
      return { path, content };
    },
    pane: async (node, project) => {
      await load(); await wait(200);
      const [, p] = find(node, project);
      if (p.lead !== 'alive') fail(`no tmux session ${project}`);
      return { lines: FIXTURE_PANE };
    },
    start: async (node, project) => {
      await load(); await wait(400);
      const [n, p] = find(node, project);
      if (p.lead === 'alive') fail(`session ${project} is already running`);
      if (!n.snapshot.deps.claude) fail(`claude not found on ${node}: install Claude Code, then sync`);
      const resumed = p.lead === 'dead';
      p.lead = 'alive';
      p.pane_last = '● Reading new answer files in work/inbox…';
      p.lead_state = 'working'; p.lead_note = null;
      const e = state.projects[`${node}/${project}`];
      if (e) e.attention = null;
      return { session: project, resumed, session_id: '00000000-0000-4000-8000-000000000000' };
    },
    stop: async (node, project) => {
      await load(); await wait(300);
      const [, p] = find(node, project);
      const stopped = p.lead === 'alive';
      if (stopped) { p.lead = 'dead'; p.pane_last = null; p.lead_state = null; p.lead_note = null; }
      return { stopped };
    },
    sshHosts: async () => {
      await wait(150);
      return { hosts: ['box-1', 'box-2', 'build-server', 'cloud-vps', 'gpu-vm', 'nas'] };
    },
    addNode: async (b) => {
      await load(); await wait(1500);
      if (state.nodes.some((n) => n.name === b.name)) fail(`node ${b.name} already exists`);
      if (/^(bad|nohost)/.test(b.ssh)) fail(`ssh: Could not resolve hostname ${b.ssh}: nodename nor servname provided, or not known`);
      const unknown = b.allowed_tokens.filter((l) => !state.tokens.some((t) => t.label === l));
      if (unknown.length) fail(`unknown tokens: ${unknown.join(', ')}`);
      const host = b.ssh.replace(/^.*@/, '');
      const deps = { tmux: '3.3a', git: '2.39.5', python3: '3.11.2', claude: /^gpu/.test(host) ? null : '2.1.296' };
      const check = { deps, host, free_ram_mb: 3920, free_disk_mb: 25610 };
      state.nodes.push({ name: b.name, ssh: b.ssh, port: b.port ?? null, allowed_tokens: [...b.allowed_tokens], ok: true, error: null,
        synced_at: nowIso(), snapshot: { node_version: '0.1.0', time: nowIso(), ...check, projects: [] } });
      return { node: b.name, check };
    },
    nodeTokens: async (name, labels) => {
      await load(); await wait(400);
      const n = nodeOf(name);
      const unknown = labels.filter((l) => !state.tokens.some((t) => t.label === l));
      if (unknown.length) fail(`unknown tokens: ${unknown.join(', ')}`);
      n.allowed_tokens = [...labels];
      return { node: name, allowed_tokens: n.allowed_tokens };
    },
    removeNode: async (name, force) => {
      await load(); await wait(500);
      const n = nodeOf(name);
      if (uncheckable(n) && !force) fail(`node ${name} is unreachable or not synced yet, so live sessions cannot be checked; remove it anyway with force`, 409);
      const live = n.ok ? (n.snapshot?.projects || []).filter((p) => p.lead === 'alive' || p.review === 'alive') : [];
      if (live.length) fail(`${name} has live sessions (${live.map((p) => p.name).join(', ')}); stop them first`);
      state.nodes = state.nodes.filter((x) => x !== n);
      for (const k of Object.keys(state.projects)) if (k.startsWith(name + '/')) delete state.projects[k];
      return { removed: name };
    },
    sandboxes: async () => {
      await load(); await wait(150);
      if (PARAMS.get('docker') === 'fail') fail('HTTP 500 Internal Server Error');
      return JSON.parse(JSON.stringify(sbx));
    },
    // Like the hub's job: building (only when the image is missing), starting, waiting, adding, done; the node appears at done.
    createSandbox: async (b) => {
      await load(); await wait(300);
      if (!sbx.docker) fail(sbx.error, 503);
      if (!NAME_RE.test(b.name || '')) fail('invalid node name', 400);
      if (!['slim', 'dev'].includes(b.image)) fail('image must be one of slim, dev', 400);
      if (b.memory_mb != null && !(Number.isInteger(b.memory_mb) && b.memory_mb >= 1024)) fail('memory_mb is a whole number of MB, at least 1024', 400);
      if (b.cpus != null && !(b.cpus >= 1 && b.cpus <= sbx.cpus_max)) fail(`cpus is a number from 1 to ${sbx.cpus_max}`, 400);
      const old = sbx.jobs.find((j) => j.node === b.name);
      if (old && SB_RUNNING.includes(old.phase)) fail(`a sandbox job for ${b.name} is already running`, 409);
      if (state.nodes.some((n) => n.name === b.name) && old?.phase !== 'failed') fail(`node ${b.name} already exists`, 409);
      const job = { node: b.name, phase: sbx.images[b.image] ? 'starting' : 'building', error: null, log_tail: [], started_at: nowIso(), finished_at: null };
      sbx.jobs = [...sbx.jobs.filter((j) => j.node !== b.name), job];
      const port = old?.port || Math.max(2222, ...sbx.sandboxes.map((x) => x.port)) + 1;
      job.port = port;
      const steps = [];
      if (job.phase === 'building') {
        const lines = [`#1 [internal] load build definition from Dockerfile`, `#5 [2/6] RUN apt-get update && apt-get install -y --no-install-recommends ...`,
          `#6 [3/6] RUN HOME=/opt/claude-home bash -c 'curl -fsSL https://claude.ai/install.sh | bash'`, `#9 exporting to image`, `#9 naming to docker.io/library/cairn-${b.image}:0.1 done`];
        for (const l of lines) steps.push([900, () => { job.log_tail = [...job.log_tail, l].slice(-20); }]);
        steps.push([300, () => { sbx.images[b.image] = true; sbx.images.slim = true; job.phase = 'starting'; }]);
      }
      steps.push([900, () => { job.phase = 'waiting'; }]);
      steps.push([1500, () => {
        if (/^fail/.test(b.name) && !failedOnce.has(b.name)) {
          failedOnce.add(b.name);
          Object.assign(job, { phase: 'failed', error: `ssh to agent@127.0.0.1 port ${port} did not answer within 60 s`, finished_at: nowIso() });
          if (!sbx.sandboxes.some((x) => x.node === b.name)) sbx.sandboxes.push({ node: b.name, container: `cairn-${b.name}`, image: b.image, port, memory_mb: b.memory_mb ?? null, cpus: b.cpus ?? null, adopted: false, state: 'running', cpu_pct: 0.4, mem_used_mb: 38, mem_limit_mb: b.memory_mb ?? null, created: nowIso() });
          return true;
        }
        job.phase = 'adding';
      }]);
      steps.push([1200, () => {
        const deps = { tmux: '3.3a', git: '2.39.5', python3: '3.11.2', claude: '2.1.296' };
        state.nodes.push({ name: b.name, ssh: 'agent@127.0.0.1', port, allowed_tokens: [...(b.allowed_tokens || [])], sandbox: true, ok: true, error: null,
          synced_at: nowIso(), snapshot: { node_version: HUB_NODE_VERSION, host: `cairn-${b.name}`, time: nowIso(), free_ram_mb: b.memory_mb || 7800, free_disk_mb: 51200, deps, projects: [] } });
        sbx.sandboxes = sbx.sandboxes.filter((x) => x.node !== b.name);
        sbx.sandboxes.push({ node: b.name, container: `cairn-${b.name}`, image: b.image, port, memory_mb: b.memory_mb ?? null, cpus: b.cpus ?? null, adopted: false,
          state: 'running', cpu_pct: 0.6, mem_used_mb: 41, mem_limit_mb: b.memory_mb ?? null, created: nowIso(),
          login_command: `ssh -t -a -o ForwardAgent=no -p ${port} -o UserKnownHostsFile=/Users/you/.cairn/known_hosts -o StrictHostKeyChecking=accept-new agent@127.0.0.1 'PATH="$PATH:$HOME/.local/bin" claude auth login'` });
        Object.assign(job, { phase: 'done', finished_at: nowIso() });
      }]);
      (async () => { for (const [ms, fn] of steps) { await wait(ms); if (fn()) return; } })();
      return { job: { ...job } };
    },
    sandboxStart: async (node) => {
      await load(); await wait(700);
      const x = sbx.sandboxes.find((v) => v.node === node) || fail(`${node} is not a sandbox this hub manages`, 409);
      Object.assign(x, { state: 'running', cpu_pct: 2.1, mem_used_mb: 64 });
      const n = state.nodes.find((v) => v.name === node);
      if (n) Object.assign(n, { ok: true, error: null, stopped: false, synced_at: nowIso() });
      return { node, state: 'running' };
    },
    sandboxStop: async (node, force) => {
      await load(); await wait(900);
      const x = sbx.sandboxes.find((v) => v.node === node) || fail(`${node} is not a sandbox this hub manages`, 409);
      const n = state.nodes.find((v) => v.name === node);
      const live = (n?.ok ? n.snapshot?.projects || [] : []).filter((p) => p.lead === 'alive' || p.review === 'alive');
      if (live.length) fail(`node ${node} has a live lead or review: ${live.map((p) => p.name).join(', ')}`, 409);
      sbUnchecked(n, x, force);
      Object.assign(x, { state: 'exited', cpu_pct: null, mem_used_mb: null });
      if (n) Object.assign(n, { ok: false, error: 'sandbox stopped', stopped: true, synced_at: nowIso() });
      return { node, state: 'exited' };
    },
    recreateSandbox: async (node, force) => {
      await load(); await wait(1200);
      const x = sbx.sandboxes.find((v) => v.node === node) || fail(`${node} is not a sandbox this hub manages`, 409);
      const n = state.nodes.find((v) => v.name === node);
      const live = (n?.ok ? n.snapshot?.projects || [] : []).filter((p) => p.lead === 'alive' || p.review === 'alive');
      if (live.length) fail(`node ${node} has a live lead or review: ${live.map((p) => p.name).join(', ')}`, 409);
      sbUnchecked(n, x, force);
      if (!sbx.images[x.image]) fail(`image cairn-${x.image}:0.1 is missing; build it first (a new sandbox of that image builds it)`, 409);
      Object.assign(x, { state: 'running', cpu_pct: 1.2, mem_used_mb: 40, created: nowIso() });
      if (n) Object.assign(n, { ok: true, error: null, stopped: false, synced_at: nowIso() });
      return { node, state: 'running' };
    },
    removeSandbox: async (node, confirm, force) => {
      await load(); await wait(900);
      if (confirm !== node) fail(`confirm must be the node name ${node}`, 400);
      const x = sbx.sandboxes.find((v) => v.node === node) || fail(`${node} is not a sandbox this hub manages`, 409);
      const n = state.nodes.find((v) => v.name === node);
      const live = (n?.ok ? n.snapshot?.projects || [] : []).filter((p) => p.lead === 'alive' || p.review === 'alive');
      if (live.length) fail(`node ${node} has a live lead or review: ${live.map((p) => p.name).join(', ')}`, 409);
      sbUnchecked(n, x, force);
      const mirrored = (n?.snapshot?.projects || []).length > 0;
      sbx.sandboxes = sbx.sandboxes.filter((v) => v !== x);
      sbx.jobs = sbx.jobs.filter((j) => j.node !== node);
      state.nodes = state.nodes.filter((v) => v !== n);
      for (const k of Object.keys(state.projects)) if (k.startsWith(node + '/')) delete state.projects[k];
      return { removed: node, mirror: mirrored ? `/Users/you/.cairn/mirror/${node}` : null };
    },
    addToken: async (b) => {
      await load(); await wait(900);
      if (!['oauth', 'node-login'].includes(b.kind)) fail(`unknown token kind ${b.kind}: use oauth or node-login`, 400);
      if (b.kind === 'oauth' && !/^sk-ant-oat/.test(b.secret || '')) fail('that is not a setup token: expected it to start with sk-ant-oat', 400);
      const old = state.tokens.find((t) => t.label === b.label);
      if (old && !b.replace) fail(`token ${b.label} already exists`, 409);
      if (b.replace) {
        // The label, its node allowances and its project mappings stay; only the kind and secret change.
        if (!old) fail(`no token ${b.label} to replace`, 404);
        Object.assign(old, { kind: 'oauth', last4: b.secret.slice(-4), account: b.account ?? old.account,
          expires: new Date(Date.now() + 365 * 864e5).toISOString().replace(/\.\d+Z$/, 'Z') });
        return { label: b.label };
      }
      const secretKind = b.kind !== 'node-login';
      state.tokens.push({ label: b.label, kind: b.kind, account: b.account || '', last4: secretKind ? b.secret.slice(-4) : '',
        expires: b.kind === 'oauth' ? new Date(Date.now() + 365 * 864e5).toISOString().replace(/\.\d+Z$/, 'Z') : null, in_use: [] });
      for (const n of state.nodes) if (b.nodes.includes(n.name) && !n.allowed_tokens.includes(b.label)) n.allowed_tokens.push(b.label);
      return { label: b.label };
    },
    updateToken: async (b) => {
      await load(); await wait(500);
      const t = state.tokens.find((x) => x.label === b.label) || fail(`no token ${b.label}`);
      if (b.new_label && b.new_label !== b.label) {
        if (state.tokens.some((x) => x.label === b.new_label)) fail(`token ${b.new_label} already exists`);
        for (const n of state.nodes) n.allowed_tokens = n.allowed_tokens.map((l) => (l === b.label ? b.new_label : l));
        for (const v of Object.values(state.projects)) if (v.token === b.label) v.token = b.new_label;
        t.label = b.new_label;
      }
      if (b.account != null) t.account = b.account;
      if (b.nodes) {
        for (const n of state.nodes) {
          const has = n.allowed_tokens.includes(t.label);
          if (b.nodes.includes(n.name) && !has) n.allowed_tokens.push(t.label);
          if (!b.nodes.includes(n.name) && has) n.allowed_tokens = n.allowed_tokens.filter((l) => l !== t.label);
        }
      }
      return { label: t.label };
    },
    clearLimit: async (label, node) => {
      await load(); await wait(300);
      const t = state.tokens.find((x) => x.label === label) || fail(`no such token ${label}`, 404);
      const before = (t.limits || []).length;
      t.limits = (t.limits || []).filter((l) => l.node !== (node || null));
      if (!node) for (const v of Object.values(state.projects)) delete v.limits?.[label];
      return { label, node: node || null, cleared: t.limits.length < before };
    },
    removeToken: async (label, force) => {
      await load(); await wait(500);
      const used = snap().tokens.find((t) => t.label === label)?.in_use || fail(`no token ${label}`);
      if (used.length) fail(`${label} is in use by ${used.join(', ')}`);
      // As the hub: a node that may run the token and cannot be read now refuses, unless forced.
      const dark = state.nodes.filter((n) => uncheckable(n) && !n.stopped && n.allowed_tokens.includes(label)).map((n) => n.name);
      if (dark.length && !force) {
        throw Object.assign(new Error(`cannot check ${dark.join(', ')} for a live lead on token ${label}; remove it anyway with force`),
          { status: 409, data: { unchecked: dark } });
      }
      state.tokens = state.tokens.filter((t) => t.label !== label);
      for (const n of state.nodes) n.allowed_tokens = n.allowed_tokens.filter((l) => l !== label);
      for (const v of Object.values(state.projects)) if (v.token === label) delete v.token;
      return { removed: label };
    },
    createProject: async (b) => {
      await load(); await wait(1400);
      const n = nodeOf(b.node);
      if (!n.ok) fail(`node ${n.name} is unreachable: ${n.error}`);
      if (!NAME_RE.test(b.project)) fail(`bad project name ${b.project}`);
      if (b.project.endsWith('-review')) fail('a project name may not end in -review: project x\'s review session is x-review', 400);
      if (n.snapshot.projects.some((p) => p.name === b.project)) fail(`project ${b.project} already exists on ${n.name}`);
      if (!n.allowed_tokens.includes(b.token)) fail(`token ${b.token} is not allowed on ${n.name}`);
      if (b.repo.kind === 'clone' && /nope|missing/.test(b.repo.url)) fail(`git clone: repository '${b.repo.url}' not found`);
      const path = b.path || `${homeOf(n)}/work/${b.project}`;
      const tickets = (b.tickets || []).map((t, i) => {
        const id = String(i + 1).padStart(2, '0');
        const slug = t.title.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 40);
        const tk = { id, title: t.title, type: t.type, status: 'open', assignee: '', blocked_by: [], state: 'frontier', path: `work/tickets/${id}-${slug}.md` };
        files[`${n.name}/${b.project}/${tk.path}`] = ticketFile(tk, `## Question\n\n${t.question || ''}\n`);
        return tk;
      });
      const p = { name: b.project, path, status: 'unknown', lead: 'none', review: 'none',
        map: { destination: b.destination, decisions: [] }, tickets, questions: [], adrs: [], commits: [] };
      commit(p, 'cairn: init map, LEAD.md, first tickets');
      n.snapshot.projects.push(p);
      for (const m of [b.model, b.subagent_model]) if (m != null && !MODEL_IDS.includes(m)) fail(`unknown model ${m}`);
      state.projects[`${n.name}/${b.project}`] = { token: b.token, model: b.model ?? null, subagent_model: b.subagent_model ?? null, mirror: { path: `~/.cairn/mirror/${n.name}/${b.project}.git`, sha: null, synced_at: null, error: null } };
      let started = false;
      let start_error = null;
      if (b.start) {
        const kind = state.tokens.find((t) => t.label === b.token)?.kind;
        if (!n.snapshot.deps.claude) start_error = `claude not found on ${n.name}: install Claude Code, then start the lead`;
        else if (kind === 'node-login') { p.lead = 'dead'; start_error = `the pane shows "Not logged in": ${n.name} has no login of its own; run claude login there or pick an OAuth token`; }
        else { p.lead = 'alive'; started = true; }
      }
      return { project: b.project, path, started, start_error };
    },
    projectToken: async (node, project, token) => {
      await load(); await wait(400);
      const [n] = find(node, project);
      if (!n.allowed_tokens.includes(token)) fail(`token ${token} is not allowed on ${node}`);
      const k = `${node}/${project}`;
      const pool = state.projects[k]?.tokens || [];
      state.projects[k] = { ...state.projects[k], token, tokens: pool.includes(token) ? pool : [token, ...pool] };
      return { token, tokens: state.projects[k].tokens };
    },
    projectTokens: async (node, project, tokens) => {
      await load(); await wait(400);
      const [n] = find(node, project);
      const bad = tokens.find((l) => !n.allowed_tokens.includes(l));
      if (bad) fail(`token ${bad} is not allowed on ${node}`);
      const k = `${node}/${project}`;
      const token = state.projects[k]?.token || n.allowed_tokens[0];
      state.projects[k] = { ...state.projects[k], token, tokens: tokens.includes(token) ? tokens : [token, ...tokens] };
      return { token, tokens: state.projects[k].tokens };
    },
    projectModels: async (node, project, model, subagent_model) => {
      await load(); await wait(300);
      find(node, project);
      for (const m of [model, subagent_model]) if (m != null && !MODEL_IDS.includes(m)) fail(`unknown model ${m}: use fable, opus, sonnet, haiku or null`, 400);
      const k = `${node}/${project}`;
      state.projects[k] = { ...state.projects[k], model, subagent_model };
      return { model, subagent_model };
    },
    // Like the hub: sync the node, export the mirror's sha to ~/Downloads/<project> (or a dated, then numbered, name), never overwriting.
    copyProject: async (node, project) => {
      await load(); await wait(900);
      return copyNow(node, project);
    },
    // Run it: copy first when the copy is missing or behind, then serve it on a free port; only folders with index.html.
    run: async (node, project) => {
      await load(); await wait(700);
      const [, p] = find(node, project);
      const k = `${node}/${project}`;
      const e = state.projects[k] || {};
      const head = p.commits[0]?.sha;
      if (!e.copied_to || !head || !String(e.copied_sha || '').startsWith(head)) copyNow(node, project);
      const path = state.projects[k].copied_to;
      if (!RUNNABLE.has(project)) fail(`${path}/index.html does not exist; Run it serves a copy that has index.html at its root`, 409);
      const url = state.projects[k].running_url || `http://127.0.0.1:${53000 + Math.floor(Math.random() * 900)}/`;
      state.projects[k].running_url = url;
      return { url, path };
    },
    runStop: async (node, project) => {
      await load(); await wait(250);
      find(node, project);
      const e = state.projects[`${node}/${project}`] || {};
      const stopped = !!e.running_url;
      e.running_url = null;
      return { stopped };
    },
    // Restart: stop when alive, then start resuming; refused while a review chat is open.
    restart: async (node, project) => {
      await load(); await wait(900);
      const [n, p] = find(node, project);
      if (LIVE_CHAT.has(chats[`${node}/${project}`]?.state)) fail(`a review chat is open for ${project}; end it first`, 409);
      if (!n.snapshot.deps.claude) fail(`claude not found on ${node}: install Claude Code, then sync`);
      p.lead = 'alive'; p.lead_state = 'working'; p.lead_note = null;
      p.pane_last = '● Reading new answer files in work/inbox…';
      if (p.status && !p.status.startsWith('working') && p.status !== 'done') p.status = 'idle';
      state.projects[`${node}/${project}`].attention = null;
      return { session: project, resumed: true, session_id: '00000000-0000-4000-8000-000000000002' };
    },
    dialog: async (node, project) => {
      await load(); await wait(300);
      return { dialog: fixtureDialog(node, project) };
    },
    answerDialog: async (node, project, fingerprint, n) => {
      await load(); await wait(900);
      const key = `${node}/${project}`;
      if (LIVE_CHAT.has(chats[key]?.state)) fail(`a review chat is open for ${project}; end it first`, 409);
      if (PARAMS.get('dialog') === 'changed' && dialogs[key]?.next && !failedOnce.has('dialog:' + key)) {
        failedOnce.add('dialog:' + key);
        dialogs[key] = dialogs[key].next;
      }
      const d = fixtureDialog(node, project);
      if (!d || d.fingerprint !== fingerprint) throw Object.assign(new Error('the dialog changed; read it again'), { status: 409, data: { dialog: d } });
      const o = d.options.find((x) => x.n === n) || fail(`option ${n} is not one of the dialog's options`, 409);
      if (PARAMS.get('dialog') === 'stuck') return { answered: n, label: o.label, dialog: { ...d, fingerprint: [...d.fingerprint].reverse().join('') } };
      const [, p] = find(node, project);
      delete dialogs[key];
      p.lead_state = 'working'; p.lead_note = null;
      p.pane_last = '● Carrying on after the dialog…';
      state.projects[key].attention = null;
      return { answered: n, label: o.label, dialog: null };
    },
    leadUpdate: async (node, project) => {
      await load(); await wait(800);
      const [n, p] = find(node, project);
      const version = n.snapshot.lead_template_version || 2;
      p.lead_version = version;
      return { version, commit: commit(p, `cairn: update LEAD.md to v${version}`), dropped: project === 'isbn-check' ? ['Scoring notes'] : [] };
    },
    usage: async (node, project) => {
      await load(); await wait(500);
      const [, p] = find(node, project);
      return usage[`${node}/${project}`] || { models: [], sessions: 0, first_at: null, last_at: null, active_minutes: 0, files: 0, path: p.path };
    },
    notify: async () => { await load(); await wait(150); return { ...notify }; },
    notifySetup: async (b) => {
      await load(); await wait(500);
      if (b.server != null) {
        if (!/^https?:\/\/\S+$/.test(b.server)) fail('the server is a URL such as https://ntfy.sh', 400);
        notify.server = b.server.replace(/\/$/, '');
      }
      if (b.enabled != null) notify.enabled = !!b.enabled;
      if (!notify.configured && !notify.server) fail('give the ntfy server first', 400);
      if (!notify.configured) { notify.configured = true; notify.topic = 'cairn-fixture-not-a-real-topic'; if (b.enabled == null) notify.enabled = true; }
      return { ...notify };
    },
    notifyTest: async () => {
      await load(); await wait(700);
      if (!notify.configured) fail('notifications are not set up', 409);
      if (PARAMS.get('notify') === 'error' && notify.last_error) fail(notify.last_error, 502);
      notify.last_error = null;
      notify.last_sent_at = nowIso();
      return { ok: true };
    },
    updateNode: async (name) => {
      await load(); await wait(1200);
      const n = nodeOf(name);
      if (!n.ok) fail(`${name} is unreachable: ${n.error}`, 502);
      n.node_version = HUB_NODE_VERSION;
      if (n.snapshot) n.snapshot.node_version = HUB_NODE_VERSION;
      return { node: name, node_version: HUB_NODE_VERSION };
    },
    ticket: async (node, project, op, f) => {
      await load(); await wait(600);
      const [, p] = find(node, project);
      const byId = (id) => p.tickets.find((t) => t.id === id);
      const keyOf = (t) => `${node}/${project}/${t.path}`;
      const unknown = (f.blocked_by || []).filter((id) => !byId(id));
      if (unknown.length) fail(`unknown ticket ids: ${unknown.join(', ')}`);
      let t;
      if (op === 'create') {
        if (!f.title?.trim()) fail('title is required');
        const id = String(Math.max(0, ...p.tickets.map((x) => +x.id)) + 1).padStart(2, '0');
        const slug = f.title.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 40);
        t = { id, title: f.title.trim(), type: f.type, status: 'open', assignee: '', blocked_by: f.blocked_by || [], state: 'frontier', path: `work/tickets/${id}-${slug}.md` };
        p.tickets.push(t);
        files[keyOf(t)] = ticketFile(t, `## Question\n\n${f.question || ''}\n`);
      } else {
        t = byId(f.id) || fail(`no ticket ${f.id}`);
        if (t.status !== 'open') fail(`ticket ${t.id} is ${t.status}`);
        if (t.assignee) fail(`ticket ${t.id} is claimed by ${t.assignee}`);
        const body = splitFrontmatter(files[keyOf(t)] || '').body || `## Question\n\n${t.title}.\n`;
        const link = `- [${t.title}](${t.path.replace(/^work\//, '')}) — `;
        if (op === 'close' || op === 'out-of-scope') {
          const text = op === 'close' ? f.resolution : `Out of scope: ${f.reason}`;
          if (!(op === 'close' ? f.resolution : f.reason)?.trim()) fail(op === 'close' ? 'resolution is required' : 'reason is required');
          t.status = 'closed';
          files[keyOf(t)] = ticketFile(t, `${body.trimEnd()}\n\n## Resolution\n\n${text}\n`);
          if (op === 'close') p.map.decisions.push(link + f.resolution);
        } else if (op === 'blockers') {
          if (f.blocked_by.includes(t.id)) fail(`ticket ${t.id} cannot block itself`);
          t.blocked_by = [...f.blocked_by];
          files[keyOf(t)] = ticketFile(t, body);
        } else fail(`unknown op ${op}`);
      }
      restate(p);
      return { id: t.id, path: t.path, commit: commit(p, `cairn: ${op} ticket ${t.id}`) };
    },
    answer: async (node, project, id, choice, note, fingerprint) => {
      await load(); await wait(400);
      const [, p] = find(node, project);
      // As the hub: the chat owns the questions while it is live.
      if (LIVE_CHAT.has(chats[`${node}/${project}`]?.state)) fail(`a review chat is open for ${project}; answer there`, 409);
      // &rewrite=q11 has the lead rewrite q11's options after the page read them, so the node refuses the answer.
      // A reversed fingerprint stands in for the node's new sha256; the stub only compares them.
      const q = p.questions.find((x) => x.id === id);
      if (q && PARAMS.get('rewrite') === id && !q.rewritten) {
        Object.assign(q, { options: [...q.options.slice(0, -1), `${q.options.at(-1)}, rewritten`], rewritten: true,
          fingerprint: q.fingerprint && [...q.fingerprint].reverse().join('') });
      }
      if (q && fingerprint && fingerprint !== q.fingerprint) {
        throw Object.assign(new Error(`${id} changed since it was shown; read it again`), { status: 409, data: { question: q } });
      }
      return answerQ(node, p, id, choice, note || '');
    },
    chatStart: async (node, project, resume) => {
      await load(); await wait(350);
      const [n, p] = find(node, project);
      if (!n.snapshot.deps.claude) fail(`claude not found on ${node}: install Claude Code, then sync`);
      const key = `${node}/${project}`;
      let c = chats[key];
      if (c && LIVE_CHAT.has(c.state)) return chatPub(c);
      const resumed = !!(resume && c?.session_id);
      if (!resumed) {
        c = chats[key] = { node, project, events: [], timers: [], skipped: [], answers: [],
          session_id: crypto.randomUUID?.() || '00000000-0000-4000-8000-000000000001', started_at: nowIso() };
      }
      setChat(c, 'starting');
      // ?stuck=1 leaves a fresh chat in "starting", like a first turn that never answers, to try End session on it.
      if (PARAMS.get('stuck') && !resumed) return chatPub(c);
      const list = queue(c, p);
      if (resumed) {
        // The hub sends nothing on a resume: the session just waits, still on the question it last asked.
        const asked = p.questions.find((q) => q.id === (c.cur || askedId(c.events)) && !q.answered);
        c.cur = asked?.id || list[0]?.id || null;
        c.timers.push(setTimeout(() => setChat(c, 'waiting'), 600));
        return chatPub(c);
      }
      c.cur = list[0]?.id || null;
      const gates = list.filter((q) => q.gate).length;
      turn(c, [['Bash', 'ls work/inbox'], ['Read', list[0]?.path || 'work/inbox']],
        list.length ? [`${plural(list.length, 'open question')}${gates ? `, ${gates} gated; gates first` : ''}.`, ask(p, list[0])]
          : 'No unanswered questions in work/inbox, so there is nothing to review.',
        () => afterTurn(c, null, !list.length));
      return chatPub(c);
    },
    chatSend: async (node, project, text) => {
      await load(); await wait(150);
      // Two test messages: "/hub-restart" forgets every chat, as a restarted hub does; "/drop" fails the chat as a dropped ssh link would.
      if (text.trim() === '/hub-restart') { for (const k of Object.keys(chats)) { chats[k].timers.forEach(clearTimeout); delete chats[k]; } }
      const c = chatOfKey(node, project);
      if (text.trim() === '/drop') { setChat(c, 'error', 'ssh: client_loop: send disconnect: Broken pipe'); return { seq: c.events.at(-1).seq }; }
      const [, p] = find(node, project);
      if (c.state !== 'waiting') fail(`the chat is ${c.state}, not waiting for you`, 409);
      if (!text.trim() || text.length > 8000) fail('a message is 1 to 8000 characters');
      setChat(c, 'working');
      const e = emit(c, 'user', { text });
      respond(c, p, text.trim());
      return { seq: e.seq };
    },
    chatEnd: async (node, project) => {
      await load(); await wait(300);
      const c = chatOfKey(node, project);
      if (LIVE_CHAT.has(c.state)) {
        c.timers.forEach(clearTimeout);
        c.timers = [];
        setChat(c, 'ended', 'closed');
      }
      return chatPub(c);
    },
    chat: async (node, project, after) => {
      await load();
      return chatPub(chatOfKey(node, project), after || 0);
    },
  };
}

const FIXTURE_PANE = [
  '● Reading work/inbox for new answers… none since q08.',
  '● Ticket 07: writer streams tiles from the sync query.',
  '',
  '  Bash(python3 -m tilecache.write --batch tiles_shared --zoom 14 --flush 500)',
  '  ⎿  zoom 14: 61,220 tiles in 49.8 s, peak RSS 286 MB',
  '     flushed 123 times, 0 retries',
  '',
  '● Peak RAM is under 300 MB with the q11 default (flush every 500). Carrying on with',
  '  zoom 15 and 16; I will note the q11 answer on ticket 07 when it lands.',
  '',
  '  Bash(python3 -m tilecache.write --batch tiles_shared --zoom 15)',
  '  ⎿  Running… (2m 14s)',
  '',
  '✻ Writing zoom 15… (esc to interrupt)',
];

const api = FIXTURE ? fixtureApi(FIXTURE) : hubApi;

/* ---------- helpers ---------- */

function dur(ms) {
  const m = Math.max(0, Math.floor(ms / 60000));
  if (m < 1) return 'under a minute';
  if (m < 60) return `${m} min`;
  const hrs = Math.floor(m / 60);
  if (hrs < 24) return m % 60 ? `${hrs} h ${m % 60} min` : `${hrs} h`;
  const d = Math.floor(hrs / 24);
  return hrs % 24 ? `${d} d ${hrs % 24} h` : `${d} d`;
}
const since = (iso, now) => (iso ? dur(now - Date.parse(iso)) : '');
function ago(iso, now) {
  if (!iso) return 'never';
  const s = Math.floor((now - Date.parse(iso)) / 1000);
  if (s < 10) return 'just now';
  if (s < 60) return `${s} s ago`;
  return `${dur(s * 1000)} ago`;
}
const clock = (iso) => (iso ? new Date(iso).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '');
// "today 09:12", "yesterday 18:40", else the date: for chat dividers.
function dayClock(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  const days = Math.round((new Date().setHours(0, 0, 0, 0) - new Date(iso).setHours(0, 0, 0, 0)) / 864e5);
  const hm = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  return days === 0 ? `today ${hm}` : days === 1 ? `yesterday ${hm}` : `${d.toLocaleDateString([], { month: 'short', day: 'numeric' })} ${hm}`;
}
const mb = (n) => (n == null ? 'n/a' : n >= 1024 ? `${(n / 1024).toFixed(n >= 10240 ? 0 : 1)} GB` : `${n} MB`);
// The node's snapshot is the lead's own output: the helpers every page sums over must survive a malformed project.
const openQs = (p) => (Array.isArray(p.questions) ? p.questions : []).filter((q) => q && !q.answered);
const projectHref = (node, project) => `#/p/${encodeURIComponent(node)}/${encodeURIComponent(project)}`;
const NAME_RE = /^[a-z0-9][a-z0-9-]{0,39}$/;
const KIND_LABEL = { oauth: 'OAuth', 'node-login': 'Node login' };
// tokens.json can still hold a kind the hub no longer supports; it is listed so it can be replaced.
const kindLabel = (k) => KIND_LABEL[k] || 'Unsupported';
const projectEntry = (state, node, project) => state.projects?.[`${node}/${project}`] || {};
const tokenOf = (state, node, p) => projectEntry(state, node.name, p.name).token || node.allowed_tokens?.[0] || null;
const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;
const missingDeps = (n) => Object.entries(n.snapshot?.deps || {}).filter(([, v]) => !v).map(([k]) => k);
const sshTarget = (n) => `${n.ssh}${n.port ? ` port ${n.port}` : ''}`;
// A sandbox stopped on purpose is off, not an outage: it never counts as down.
const isDown = (n) => !n.ok && !n.stopped;
const offWord = (n) => (n.stopped ? 'stopped' : 'unreachable');
const LIVE_STATES = ['starting', 'working', 'waiting'];
// Model aliases the node accepts; null is the account default.
const MODELS = [[null, 'Default'], ['fable', 'Fable'], ['opus', 'Opus'], ['sonnet', 'Sonnet'], ['haiku', 'Haiku']];
const MODEL_IDS = MODELS.map(([v]) => v).filter(Boolean);
// "Result ready" is the hub's call (done, a result, a newest commit, not yet copied); the UI never compares shas itself.
const resultReady = (state, node, p) => !!projectEntry(state, node.name, p.name).result_ready;
const resultHeadline = (r) => (r || '').split('\n').map((l) => l.replace(/^#+\s*/, '').trim()).find(Boolean) || '';
const chatOf = (state, node, project) => (state?.chats || []).find((c) => c.node === node && c.project === project) || null;
const isLive = (c) => !!c && LIVE_STATES.includes(c.state);
// Review resumes a chat the operator closed or left idle; a finished or failed one starts fresh.
const wantsResume = (c) => c?.state === 'ended' && (c.detail === 'idle' || c.detail === 'closed');
// Gates first, then oldest, as the review skill walks them.
const byFocus = (a, b) => Number(b.gate) - Number(a.gate) || Date.parse(a.asked_at) - Date.parse(b.asked_at);

function parseOption(s) {
  s = String(s);
  const m = /^([A-Za-z])[.)]\s*(.*)$/.exec(s.trim());
  const letter = m ? m[1].toUpperCase() : '';
  let text = m ? m[2] : s;
  let reason = null;
  const r = /\s*\(recommended(?::\s*([^)]*))?\)\s*$/i.exec(text);
  if (r) { reason = r[1] || ''; text = text.slice(0, r.index); }
  return { letter, text, recommended: !!r, reason };
}

function allProjects(state) {
  const out = [];
  for (const n of state?.nodes || []) {
    const ps = n.snapshot?.projects;
    for (const p of Array.isArray(ps) ? ps : []) if (p && typeof p === 'object') out.push({ node: n, p });
  }
  return out;
}
// "node/project" keys (data-project, the hub's in_use): node names never hold a slash, so the first one splits them.
const splitKey = (k) => { const i = k.indexOf('/'); return [k.slice(0, i), k.slice(i + 1)]; };

// Renders render(); when it or anything below it throws, fallback(error) takes its place, so one malformed project or
// page cannot blank the dashboard. A new `retry` value (the next state) tries render() again.
class Guard extends Component {
  constructor() { super(); this.state = { error: null }; }
  static getDerivedStateFromError(error) { return { error }; }
  componentDidCatch(error) { console.error(error); }
  componentDidUpdate(prev) { if (this.state.error && prev.retry !== this.props.retry) this.setState({ error: null }); }
  // render() runs in a child: a component's own render throwing is caught above it, not by itself.
  render({ render: show, fallback }, { error }) { return error ? fallback(error) : h(Show, { show }); }
}
const Show = ({ show }) => show();
const errText = (e) => String(e?.message || e);
const projectError = (node, p, e) => html`<div class="inline-error project-error" role="alert">
  <b>${String(p?.name)}</b> on ${node.name} sent a project the dashboard cannot read: ${errText(e)}</div>`;

function leadLabel(lead) {
  return { alive: 'Running', dead: 'Not running', none: 'Never started', unknown: 'Unknown' }[lead] || lead;
}

function statusLine(p) {
  const s = p.status || 'unknown';
  if (s.startsWith('working')) {
    const id = s.split(/\s+/)[1];
    const t = p.tickets.find((x) => x.id === id);
    return html`Working on <span class="mono">${id}</span>${t ? html` ${t.title}` : ''}`;
  }
  return { idle: 'Idle: only waiting items remain', done: 'Done: the map is worked', unknown: 'No status yet' }[s] || s;
}

/* ---------- markdown ---------- */

const escapeHtml = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
marked.use({ gfm: true, renderer: { html: (tok) => escapeHtml(tok.text ?? tok.raw ?? '') } });

const ALLOWED = new Set('a p br hr em strong del code pre blockquote ul ol li h1 h2 h3 h4 h5 h6 table thead tbody tr th td input span'.split(' '));
// Judged on the URL the browser would follow: a tab or an entity inside "javascript:" fools a pattern, not the URL parser.
const LINK_SCHEMES = ['http:', 'https:', 'mailto:'];
function sanitize(markup) {
  const t = document.createElement('template');
  t.innerHTML = markup;
  for (const el of [...t.content.querySelectorAll('*')]) {
    const tag = el.tagName.toLowerCase();
    if (!ALLOWED.has(tag)) {
      el.replaceWith(document.createTextNode(tag === 'img' ? `[image: ${el.getAttribute('alt') || ''}]` : el.textContent));
      continue;
    }
    for (const a of [...el.attributes]) {
      const keep = (a.name === 'href' && tag === 'a') || a.name === 'title' || a.name === 'align' ||
        (a.name === 'class' && tag === 'code') || (a.name === 'start' && tag === 'ol') ||
        (tag === 'input' && ['type', 'checked', 'disabled'].includes(a.name));
      if (!keep) el.removeAttribute(a.name);
    }
    if (tag === 'a') {
      // Only http(s) and mailto stay; a link off this origin opens in a new tab without an opener.
      const href = el.getAttribute('href');
      let url = null;
      try { url = href ? new URL(href, location.href) : null; } catch { /* unparseable: dropped */ }
      if (!url || !LINK_SCHEMES.includes(url.protocol)) el.removeAttribute('href');
      else if (url.protocol !== 'mailto:' && url.origin !== location.origin) { el.setAttribute('target', '_blank'); el.setAttribute('rel', 'noopener noreferrer'); }
    }
  }
  return t.innerHTML;
}
const md = (src) => sanitize(marked.parse(src || '', { async: false }));
const mdInline = (src) => sanitize(marked.parseInline(src || '', { async: false }));
// Chat text keeps single line breaks, so option lines A./B./C. stay on their own lines.
const mdChat = (src) => sanitize(marked.parse(src || '', { async: false, breaks: true }));
// The review skill starts every question message with **qNN**; the newest such message is the question being asked.
function askedId(events) {
  for (let i = events.length - 1; i >= 0; i--) {
    const m = events[i].kind === 'text' && /^\s*\*\*(q\d+)\*\*/.exec(events[i].text);
    if (m) return m[1];
  }
  return null;
}

function splitFrontmatter(src) {
  const m = /^---\r?\n([\s\S]*?)\r?\n---\r?\n?/.exec(src || '');
  if (!m) return { meta: null, body: src || '' };
  const meta = [];
  for (const line of m[1].split(/\r?\n/)) {
    const kv = /^([A-Za-z_][\w-]*):\s*(.*?)\s*(#.*)?$/.exec(line);
    if (kv) meta.push([kv[1], kv[2]]);
  }
  return { meta, body: src.slice(m[0].length) };
}

function resolvePath(base, href) {
  const parts = base.split('/').slice(0, -1);
  for (const seg of href.split('#')[0].split('/')) {
    if (seg === '..') parts.pop();
    else if (seg && seg !== '.') parts.push(seg);
  }
  return parts.join('/');
}

// Relative .md links inside rendered markdown open in the drawer. The browser follows only what the sanitiser marked to
// leave (target _blank) and mailto; nothing in lead-written markdown navigates the dashboard itself.
function mdLinkHandler(base, onOpen) {
  return (e) => {
    const a = e.target.closest('a');
    if (!a) return;
    const href = a.getAttribute('href') || '';
    if (!href || a.target === '_blank' || /^mailto:/i.test(href)) return;
    e.preventDefault();
    // An in-page anchor would change the route; lead-written markdown has no heading ids to jump to anyway.
    if (href.startsWith('#') || /^([a-z][a-z0-9+.-]*:|\/)/i.test(href)) return;
    if (/\.md(#.*)?$/.test(href)) onOpen(resolvePath(base, href));
  };
}

/* ---------- hooks ---------- */

function useNow(ms = 5000) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => { const t = setInterval(() => setNow(Date.now()), ms); return () => clearInterval(t); }, [ms]);
  return now;
}

function parseHash() {
  let parts;
  try { parts = location.hash.replace(/^#\/?/, '').split('/').map(decodeURIComponent); } catch { return { view: 'inbox' }; }
  if (parts[0] === 'p' && parts[1] && parts[2]) return { view: 'project', node: parts[1], project: parts[2] };
  if (parts[0] === 'new') return { view: 'new', node: parts[1] || null };
  if (['projects', 'nodes', 'tokens', 'settings'].includes(parts[0])) return { view: parts[0] };
  return { view: 'inbox' };
}
function useRoute() {
  const [route, setRoute] = useState(parseHash());
  useEffect(() => {
    const on = () => setRoute(parseHash());
    addEventListener('hashchange', on);
    return () => removeEventListener('hashchange', on);
  }, []);
  return route;
}

/* ---------- small components ---------- */

function Dot({ tone, label }) {
  return html`<span class=${'dot dot-' + tone} role="img" aria-label=${label} title=${label}></span>`;
}

function GateBadge() {
  return html`<span class="badge badge-gate" title="The lead holds the gated work until this is answered">Gate</span>`;
}

function LeadState({ p }) {
  const tone = p.lead === 'alive' ? 'ok' : p.lead === 'dead' ? 'idle' : 'off';
  return html`<span class="lead-state"><${Dot} tone=${tone} label=${'Lead ' + leadLabel(p.lead).toLowerCase()} />${leadLabel(p.lead)}</span>`;
}

function Options({ q }) {
  return html`<ol class="opts">
    ${q.options.map((raw) => {
      const o = parseOption(raw);
      const isDefault = o.letter && o.letter === String(q.default).toUpperCase();
      const isChoice = q.answered && o.letter === String(q.choice).toUpperCase();
      return html`<li class=${'opt' + (isDefault ? ' is-default' : '') + (isChoice ? ' is-choice' : '')}>
        <span class="opt-letter">${o.letter || '·'}</span>
        <span class="opt-body">
          <span class="opt-text">${o.text}</span>
          ${o.recommended && html`<span class="opt-tag tag-rec">Recommended</span>`}
          ${isDefault && !q.answered && html`<span class="opt-tag tag-default">Default</span>`}
          ${isChoice && html`<span class="opt-tag tag-choice">Chosen</span>`}
          ${o.reason && html`<span class="opt-reason">${o.reason}</span>`}
        </span>
      </li>`;
    })}
  </ol>`;
}

function QuestionAge({ q, now }) {
  if (q.answered) {
    return html`<span class="q-age">Answered ${q.choice ? html`<b>${q.choice}</b>` : ''} ${q.answered_at ? `after ${since(q.asked_at, Date.parse(q.answered_at))}` : ''}${q.applied ? html`<span class="q-applied" title=${'Applied ' + clock(q.applied)}> · applied by the lead ${ago(q.applied, now)}</span>` : ''}</span>`;
  }
  if (q.gate) return html`<span class="q-age q-age-gate" title=${'Asked ' + clock(q.asked_at)}>Work held for ${since(q.asked_at, now)}</span>`;
  return html`<span class="q-age" title=${'Asked ' + clock(q.asked_at)}>Default <b>${q.default}</b> for ${since(q.asked_at, now)}</span>`;
}

/* ---------- multiselect ---------- */

function MultiSelect({ label, options, value, onChange }) {
  const ref = useRef();
  useEffect(() => {
    const off = (e) => { if (ref.current?.open && !ref.current.contains(e.target)) ref.current.open = false; };
    document.addEventListener('click', off);
    return () => document.removeEventListener('click', off);
  }, []);
  const summary = value.length === 0 ? 'All' : value.length <= 2 ? value.join(', ') : `${value.length} selected`;
  const toggle = (v) => onChange(value.includes(v) ? value.filter((x) => x !== v) : [...value, v]);
  return html`<details class="ms" ref=${ref}>
    <summary class="btn"><span class="ms-label">${label}</span><span class="ms-value">${summary}</span><span class="caret" aria-hidden="true"></span></summary>
    <div class="ms-menu" role="group" aria-label=${label}>
      ${options.map(([v, count]) => html`<label class="ms-opt">
        <input type="checkbox" checked=${value.includes(v)} onChange=${() => toggle(v)} />
        <span>${v}</span><span class="ms-count">${count}</span>
      </label>`)}
      <button class="btn btn-quiet btn-sm ms-clear" disabled=${!value.length} onClick=${() => onChange([])}>Show all types</button>
    </div>
  </details>`;
}

/* ---------- forms ---------- */

// Open forms and the drawer register here; Esc closes the newest one. Layout effect, so an Esc right after opening still lands.
const escStack = [];
function useEscape(fn, ref) {
  const live = useRef(fn);
  live.current = fn;
  useLayoutEffect(() => {
    const entry = { fn: () => live.current(), ref };
    escStack.push(entry);
    return () => { const i = escStack.indexOf(entry); if (i >= 0) escStack.splice(i, 1); };
  }, []);
}

let uidSeq = 0;
const useUid = (prefix) => useMemo(() => `${prefix}${++uidSeq}`, []);

function Field({ id, label, hint, error, optional, children }) {
  return html`<div class=${'field' + (error ? ' has-error' : '')}>
    <label class="field-label" for=${id}>${label}${optional && html`<span class="field-opt">optional</span>`}</label>
    ${children}
    ${error ? html`<p class="field-error" id=${id + '-msg'}>${error}</p>` : hint ? html`<p class="field-hint" id=${id + '-msg'}>${hint}</p>` : null}
  </div>`;
}
const msgId = (id, error, hint) => (error || hint ? id + '-msg' : undefined);

// Searchable listbox. multiple: chips plus a filter input. Single: free text with suggestions.
function Combo({ id, options, value, onChange, multiple = false, placeholder = '', invalid = false, describedBy, emptyText = 'No matches' }) {
  const lid = useUid('lb');
  const [query, setQuery] = useState('');
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const wrap = useRef();
  const input = useRef();
  const text = multiple ? query : value;
  const f = (text || '').trim().toLowerCase();
  const shown = options.filter((o) => !f || `${o.value} ${o.label || ''} ${o.hint || ''}`.toLowerCase().includes(f));
  const labelOf = (v) => options.find((o) => o.value === v)?.chip || options.find((o) => o.value === v)?.label || v;

  useEffect(() => { setActive(0); }, [f, open]);
  useEffect(() => {
    if (!open) return;
    const off = (e) => { if (!wrap.current?.contains(e.target)) setOpen(false); };
    document.addEventListener('mousedown', off);
    return () => document.removeEventListener('mousedown', off);
  }, [open]);

  const pick = (o) => {
    if (!o || o.disabled) return;
    if (multiple) {
      onChange(value.includes(o.value) ? value.filter((v) => v !== o.value) : [...value, o.value]);
      setQuery('');
    } else {
      onChange(o.value);
      setOpen(false);
    }
    input.current?.focus();
  };
  const onKeyDown = (e) => {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      if (!open) { setOpen(true); return; }
      const n = Math.max(1, shown.length);
      setActive((a) => (a + (e.key === 'ArrowDown' ? 1 : -1) + n) % n);
    } else if (e.key === 'Enter' && open && shown[active]) {
      e.preventDefault();
      pick(shown[active]);
    } else if (e.key === 'Escape' && open) {
      e.stopPropagation();
      setOpen(false);
    } else if (e.key === 'Backspace' && multiple && !query && value.length) {
      onChange(value.slice(0, -1));
    } else if (e.key === 'Tab') {
      setOpen(false);
    }
  };

  return html`<div class=${'combo' + (open ? ' is-open' : '') + (invalid ? ' is-invalid' : '')} ref=${wrap}>
    <div class="combo-box" onMouseDown=${(e) => { if (e.target === e.currentTarget) { e.preventDefault(); input.current.focus(); setOpen(true); } }}>
      ${multiple && value.map((v) => html`<span class="chip" key=${v}>
        <span class="chip-text">${labelOf(v)}</span>
        <button type="button" class="chip-x" aria-label=${'Remove ' + labelOf(v)} onClick=${() => { onChange(value.filter((x) => x !== v)); input.current?.focus(); }}>×</button>
      </span>`)}
      <input id=${id} ref=${input} class="combo-input" role="combobox" aria-expanded=${open} aria-controls=${lid}
        aria-autocomplete="list" aria-activedescendant=${open && shown[active] ? `${lid}-${active}` : undefined}
        aria-invalid=${invalid} aria-describedby=${describedBy} autocomplete="off" spellcheck=${false}
        value=${text} placeholder=${multiple && value.length ? '' : placeholder}
        onInput=${(e) => { if (multiple) setQuery(e.target.value); else onChange(e.target.value); setOpen(true); }}
        onClick=${() => setOpen(true)} onKeyDown=${onKeyDown} />
    </div>
    ${open && html`<ul class="combo-list" id=${lid} role="listbox" aria-multiselectable=${multiple}>
      ${shown.length ? shown.map((o, i) => {
        const sel = multiple ? value.includes(o.value) : value === o.value;
        return html`<li id=${`${lid}-${i}`} key=${o.value} role="option" aria-selected=${sel} aria-disabled=${!!o.disabled}
          class=${'combo-opt' + (i === active ? ' is-active' : '') + (sel ? ' is-selected' : '') + (o.disabled ? ' is-disabled' : '')}
          onMouseDown=${(e) => e.preventDefault()} onClick=${() => pick(o)} onMouseMove=${() => active !== i && setActive(i)}>
          <span class="combo-check" aria-hidden="true"></span>
          <span class="combo-label">${o.label || o.value}</span>
          ${o.hint && html`<span class="combo-hint">${o.hint}</span>`}
        </li>`;
      }) : html`<li class="combo-empty">${emptyText}</li>`}
    </ul>`}
  </div>`;
}

// Inline confirmation for destructive actions. Cancel holds focus so Enter never deletes by accident.
function ConfirmBar({ message, confirmLabel, busyLabel, onConfirm, onCancel }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const ref = useRef();
  useEscape(onCancel, ref);
  useLayoutEffect(() => { ref.current?.querySelector('[data-cancel]')?.focus(); }, []);
  const go = async () => {
    setBusy(true); setError(null);
    try { await onConfirm(); } catch (e) { setError(e.message); setBusy(false); }
  };
  return html`<div class="confirm" ref=${ref} role="group" aria-label="Confirm">
    <p class="confirm-msg">${message}</p>
    ${error && html`<div class="inline-error" role="alert">${error}</div>`}
    <div class="form-actions">
      <button type="button" class="btn btn-danger" onClick=${go} disabled=${busy}>${busy ? busyLabel : confirmLabel}</button>
      <button type="button" class="btn btn-quiet" data-cancel onClick=${onCancel} disabled=${busy}>Cancel</button>
    </div>
  </div>`;
}

function Notice({ children, onClose }) {
  const ref = useRef();
  useEscape(onClose, ref);
  return html`<div class="notice" ref=${ref}>
    <p>${children}</p>
    <button type="button" class="btn btn-quiet btn-sm" onClick=${onClose}>OK</button>
  </div>`;
}

// Tab and Shift+Tab cycle inside a modal drawer instead of leaving it for the page under the scrim.
function trapTab(e) {
  if (e.key !== 'Tab') return;
  const box = e.currentTarget;
  const items = [...box.querySelectorAll('a[href], button, input, select, textarea, summary, [tabindex]')]
    .filter((el) => !el.disabled && el.tabIndex >= 0 && el.offsetParent !== null);
  if (!items.length) { e.preventDefault(); return; }
  const first = items[0];
  const last = items[items.length - 1];
  if (e.shiftKey && (document.activeElement === first || document.activeElement === box)) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
}

const firstInvalid = (root) => setTimeout(() => root?.querySelector('[aria-invalid="true"]')?.focus(), 0);

function tokenOptions(state) {
  return (state.tokens || []).map((t) => ({ value: t.label, hint: kindLabel(t.kind) + (t.last4 ? ` ··${t.last4}` : '') }));
}

/* ---------- sidebar ---------- */

// One Leads row per project: running shows its ticket, idle a ring, done a grey dot, a lead that died mid-work a red dot,
// and a lead the node could not check (tmux did not answer) a faint ring.
function leadView(p) {
  const working = (p.status || '').startsWith('working') ? p.status.split(/\s+/)[1] : null;
  if (p.lead === 'alive') return { dot: 'ok', meta: working || (p.status === 'done' ? 'done' : 'running'), ticket: !!working };
  if (p.lead === 'none') return { dot: 'none', meta: '—' };
  if (p.lead === 'unknown') return { dot: 'unknown', meta: 'unknown' };
  if (p.status === 'done') return { dot: 'done', meta: 'done', done: true };
  if (p.status === 'idle') return { dot: 'idle', meta: 'idle' };
  return { dot: 'bad', meta: 'dead', bad: true };
}

function Sidebar({ state, route }) {
  const projects = allProjects(state);
  const total = projects.reduce((n, x) => n + openQs(x.p).length, 0);
  const gated = projects.some((x) => openQs(x.p).some((q) => q.gate));
  const down = (state?.nodes || []).filter(isDown).length;
  const item = (view, href, label, extra) => html`<a class=${'nav-item' + (route.view === view ? ' is-active' : '')}
    href=${href} aria-current=${route.view === view ? 'page' : undefined}><span>${label}</span>${extra}</a>`;
  return html`<aside class="side"><div class="side-inner">
    <a class="brand" href="#/"><span class="brand-mark" aria-hidden="true"></span>Cairn</a>
    <nav class="side-nav" aria-label="Main">
      ${item('inbox', '#/', 'Inbox', total > 0 && html`<span class=${'nav-count' + (gated && route.view !== 'inbox' ? ' is-gate' : '')}>${total}</span>`)}
      ${item('projects', '#/projects', 'Projects')}
    </nav>
    <div class="side-label" id="leads-label">Leads</div>
    <nav class="leads" aria-labelledby="leads-label">
      ${projects.map(({ node, p }) => html`<${Guard} key=${`${node.name}/${p.name}`} retry=${state}
        fallback=${(e) => html`<span class="lead-row" title=${`${String(p.name)} on ${node.name}: ${errText(e)}`}>
          <span class="ldot ldot-bad" aria-hidden="true"></span><span class="lead-name">${String(p.name)}</span><span class="lead-meta is-bad">malformed</span>
        </span>`}
        render=${() => {
          const att = attentionOf(state, node, p);
          // A lead that needs attention says so in the list: limit, dialog, prompt or stopped.
          const v = att ? { ...leadView(p), meta: { limit: 'limit', dialog: 'dialog', prompt: 'prompt', crashed: 'stopped' }[att.kind] || att.kind, ticket: false, att: true } : leadView(p);
          const active = route.view === 'project' && route.node === node.name && route.project === p.name;
          return html`<a class=${'lead-row' + (active ? ' is-active' : '') + (v.done ? ' is-done' : '')} href=${projectHref(node.name, p.name)}
              title=${[`${p.name} on ${node.name}`, p.pane_last].filter(Boolean).join('\n')}>
            <span class=${'ldot ldot-' + v.dot} aria-hidden="true"></span>
            <span class="lead-name">${p.name}</span>
            <span class=${'lead-meta' + (v.ticket ? ' is-ticket' : '') + (v.att ? ' is-att' : v.bad ? ' is-bad' : '')}>${v.meta}</span>
          </a>`;
        }} />`)}
      ${state && !projects.length && html`<p class="leads-empty">No projects yet</p>`}
    </nav>
    <div class="side-spacer"></div>
    <nav class="side-foot" aria-label="Setup">
      ${item('nodes', '#/nodes', 'Nodes', down > 0 && html`<span class="nav-down">${down} down</span>`)}
      ${item('tokens', '#/tokens', 'Tokens')}
      ${item('settings', '#/settings', 'Settings', state?.notify?.last_error && html`<span class="warn-dot" role="img" aria-label="The last notification failed" title="The last notification failed"></span>`)}
      ${item('new', '#/new', 'New project')}
    </nav>
    <div class="keys" aria-hidden="true">
      <kbd>j k</kbd><span>move</span>
      <kbd>/</kbd><span>filter</span>
      <kbd>a–c</kbd><span>pick answer</span>
      <kbd>↵</kbd><span>send answer</span>
      <kbd>r</kbd><span>open review</span>
      <kbd>c</kbd><span>type in review</span>
      <kbd>esc</kbd><span>close</span>
    </div>
  </div></aside>`;
}

/* ---------- attention ---------- */

const ATTENTION_WORDS = { limit: 'Hit a usage limit', dialog: 'Waiting at a dialog', prompt: 'Stopped at its prompt', crashed: 'Stopped while working' };
const attentionOf = (state, node, p) => projectEntry(state, node.name, p.name).attention || null;
const hhmm = (iso) => new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
// The day before a time: nothing for today, else "tomorrow " or a date such as "Nov 17 ".
function dayOf(iso, now) {
  const at = new Date(iso);
  const days = Math.round((new Date(at).setHours(0, 0, 0, 0) - new Date(now).setHours(0, 0, 0, 0)) / 864e5);
  return days === 0 ? '' : days === 1 ? 'tomorrow ' : `${at.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })} `;
}
// When the hub restarts a limited lead; a time already passed waits for the next sync.
function resumeWhen(iso, now) {
  if (Date.parse(iso) <= now) return 'Restarting at the next sync';
  return `Restarts ${dayOf(iso, now)}at ${hhmm(iso)}`;
}

// Restart: stop when alive, then start resuming its conversation with the project's token and models.
function RestartButton({ node, p, toast, refresh, primary }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const run = async () => {
    setBusy(true); setError(null);
    try {
      await api.restart(node.name, p.name);
      toast(`Lead for ${p.name} restarted; it resumes its conversation`);
      await refresh();
    } catch (x) { setError(x.message); } finally { setBusy(false); }
  };
  return html`<button class=${'btn' + (primary ? ' btn-primary' : '')} onClick=${run} disabled=${busy || !node.ok}
      title=${node.ok ? 'Stop the lead if it runs, then start it again; it resumes its conversation' : `${node.name} is ${offWord(node)}`}>${busy ? 'Restarting…' : 'Restart'}</button>
    ${error && html`<span class="field-error att-err" role="alert">${error}</span>`}`;
}

function DialogButton({ node, p, openDrawer, primary }) {
  return html`<button class=${'btn' + (primary ? ' btn-primary' : '')} disabled=${!node.ok}
    onClick=${() => openDrawer({ kind: 'dialog', node: node.name, project: p.name })}
    title=${node.ok ? "Read the lead's dialog and answer it" : `${node.name} is ${offWord(node)}`}>Review dialog</button>`;
}

// What the attention is, how long, and when the hub restarts it by itself.
function AttentionText({ a, now }) {
  return html`<span class="att-kind">${ATTENTION_WORDS[a.kind] || a.kind}</span>
    ${a.note && html`<span class="att-note" title=${a.note}>${a.note}</span>`}
    <span class="att-since" title=${'Since ' + clock(a.since)}>for ${since(a.since, now)}</span>
    ${a.resume_error ? html`<span class="att-resume-err" title=${a.resume_attempts ? `${plural(a.resume_attempts, 'attempt')} so far` : null}>Automatic restart failed: ${a.resume_error}${a.resume_at ? `; next try ${dayOf(a.resume_at, now)}at ${hhmm(a.resume_at)}` : ''}</span>`
      : a.resume_at && html`<span class="att-resume" title=${'The hub restarts the lead once, 2 minutes after the reset: ' + clock(a.resume_at)}>${resumeWhen(a.resume_at, now)}${a.switch_to && !a.switched_to ? ` on ${a.switch_to}` : ''}</span>`}
    ${a.switched_to && html`<span class="att-resume" title=${`${a.token} hit its limit; the hub moved the lead to the next token in the project's pool`}>Switched to ${a.switched_to}</span>`}`;
}

function AttentionRow({ node, p, a, now, toast, refresh, openDrawer }) {
  return html`<div class="att-row" data-project=${`${node.name}/${p.name}`}>
    <a class="rr-name" href=${projectHref(node.name, p.name)}>${p.name}</a>
    <span class="grp-node">${node.name}</span>
    <span class="att-text"><${AttentionText} a=${a} now=${now} /></span>
    <span class="att-act">${a.kind === 'dialog' && html`<${DialogButton} node=${node} p=${p} openDrawer=${openDrawer} primary />`}<${RestartButton} node=${node} p=${p} toast=${toast} refresh=${refresh} /></span>
  </div>`;
}

/* ---------- models and results ---------- */

function ModelSelect({ id, value, onChange, disabled, small }) {
  return html`<select id=${id} class=${'input input-select' + (small ? ' input-sm' : '')} value=${value || ''} disabled=${disabled}
    onChange=${(e) => onChange(e.target.value || null)}>
    ${MODELS.map(([v, label]) => html`<option value=${v || ''}>${v ? label : 'Default (account default)'}</option>`)}
  </select>`;
}

// Get a copy: the hub exports the mirror's newest commit to ~/Downloads and opens it in Finder.
function useCopy(node, p, toast, refresh) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [done, setDone] = useState(null);
  const copy = async () => {
    setBusy(true); setError(null);
    try {
      const r = await api.copyProject(node.name, p.name);
      setDone(r);
      const left = (r.skipped || []).length;
      toast(`Copied ${p.name}`, 'ok', `${r.path} · opened in Finder · ${String(r.sha).slice(0, 7)}${left ? ` · ${plural(left, 'link')} left out` : ''}`);
      await refresh();
    } catch (x) { setError(x.message); } finally { setBusy(false); }
  };
  return { busy, error, done, copy };
}

// skipped: links that pointed outside the folder and were left out of the copy.
function CopyNote({ path, sha, skipped }) {
  const left = skipped || [];
  return html`<span class="copy-note">Copied to <span class="mono">${path}</span> · opened in Finder · <span class="mono">${String(sha).slice(0, 7)}</span>${left.length > 0
    && html` · <span class="copy-skipped" title=${`Left out, they point outside the folder:\n${left.join('\n')}`}>${plural(left.length, 'link')} left out</span>`}</span>`;
}

// The lead's work/result.md, above the project tabs.
function ResultCard({ state, node, p, toast, refresh, openDrawer }) {
  const [all, setAll] = useState(false);
  const c = useCopy(node, p, toast, refresh);
  const entry = projectEntry(state, node.name, p.name);
  const last = c.done || (entry.copied_sha ? { path: entry.copied_to, sha: entry.copied_sha } : null);
  // Copied and current unless the hub says the newest commit is still to copy.
  const current = !!last && !entry.result_ready;
  const onMd = mdLinkHandler('work/result.md', (path) => openDrawer({ node: node.name, project: p.name, path, kind: path.includes('/adr/') ? 'adr' : path.includes('/inbox/') ? 'question' : 'ticket' }));
  // Run it serves the copy (made first when missing or behind) on its own port and opens the browser; Stop ends that server.
  const [running, setRunning] = useState(null);
  const [runError, setRunError] = useState(null);
  const url = entry.running_url;
  const resultHtml = useMemo(() => md(p.result), [p.result]);
  const run = async (stop) => {
    setRunning(stop ? 'stop' : 'run'); setRunError(null);
    try {
      if (stop) await api.runStop(node.name, p.name);
      else {
        const r = await api.run(node.name, p.name);
        toast(`${p.name} runs at ${r.url}`, 'ok', `serving ${r.path} · opened in the browser`);
      }
      await refresh();
    } catch (x) { setRunError(x.message); } finally { setRunning(null); }
  };
  return html`<section class="result-card" aria-labelledby="result-h">
    <header class="result-head">
      <h2 id="result-h" class="label">Result</h2>
      ${p.status !== 'done' && html`<span class="dim small">from an earlier finish; the lead is still working</span>`}
      <span class="spacer"></span>
      ${url && html`<a class="run-url mono" href=${url} target="_blank" rel="noopener noreferrer" title="Served from the copy on this Mac">${url}</a>`}
      ${url && html`<button class="btn" onClick=${() => run(true)} disabled=${!!running}>${running === 'stop' ? 'Stopping…' : 'Stop'}</button>`}
      <button class=${'btn' + (current && !url ? ' btn-primary' : '')} onClick=${() => run(false)} disabled=${!!running || c.busy || (!node.ok && !current)}
        title="Serve the copy on its own 127.0.0.1 port and open it in the browser; copies first when needed. Needs index.html at the root.">${running === 'run' ? 'Starting…' : url ? 'Open again' : 'Run it'}</button>
      <button class=${'btn' + (current ? '' : ' btn-primary')} onClick=${c.copy} disabled=${c.busy || !node.ok}
        title=${node.ok ? 'Export the newest commit to ~/Downloads and open it in Finder' : `${node.name} is ${offWord(node)}`}>${c.busy ? 'Copying…' : 'Get a copy'}</button>
    </header>
    ${runError && html`<div class="inline-error" role="alert"><b>Could not run ${p.name}.</b> ${runError}</div>`}
    ${last && html`<p class="copy-line"><${CopyNote} path=${last.path} sha=${last.sha} skipped=${last.skipped} />${!current && entry.result_ready && html`<span class="dim small"> · newer commits since</span>`}</p>`}
    ${c.error && html`<div class="inline-error" role="alert"><b>Could not copy ${p.name}.</b> ${c.error}</div>`}
    <div class=${'result-body md' + (all ? ' is-all' : '')} onClick=${onMd} dangerouslySetInnerHTML=${{ __html: resultHtml }}></div>
    <button class="linkbtn result-more" aria-expanded=${all} onClick=${() => setAll((v) => !v)}>${all ? 'Show less' : 'Show the whole result'}</button>
  </section>`;
}

// One Inbox row per result waiting to be copied; it leaves once the newest commit is copied.
function ResultRow({ node, p, toast, refresh }) {
  const c = useCopy(node, p, toast, refresh);
  return html`<div class="rr" data-project=${`${node.name}/${p.name}`}>
    <a class="rr-name" href=${projectHref(node.name, p.name)}>${p.name}</a>
    <span class="grp-node">${node.name}</span>
    <span class="rr-text" title=${resultHeadline(p.result)}>${resultHeadline(p.result)}</span>
    <button class="btn" onClick=${c.copy} disabled=${c.busy || !node.ok}>${c.busy ? 'Copying…' : 'Get a copy'}</button>
    ${c.error && html`<p class="qr-err rr-err" role="alert">Could not copy: ${c.error}</p>`}
  </div>`;
}

/* ---------- inbox ---------- */

function leadPhrase(p) {
  const v = leadView(p);
  const text = p.lead === 'alive' ? `Running${v.ticket ? ` ${v.meta}` : ''}` : p.status === 'idle' ? 'Idle'
    : p.lead === 'unknown' ? 'Unknown' : p.status === 'done' ? 'Done' : p.lead === 'none' ? 'Never started' : 'Not running';
  const tone = { ok: 'ok', idle: 'idle', done: 'off', bad: 'bad', none: 'off', unknown: 'off' }[v.dot];
  return html`<span class="lead-state"><${Dot} tone=${tone} label=${'Lead: ' + text} />${text}</span>`;
}

// Review button, or the "In review" pill while the project's chat is live.
function ReviewButton({ state, node, p, openChat, primary, count, showKey, review }) {
  const c = chatOf(state, node.name, p.name);
  const target = { node: node.name, project: p.name, resume: wantsResume(c) };
  if (isLive(c) || (review?.target?.node === node.name && review.target.project === p.name && review.live)) {
    return html`<button class="in-review" onClick=${() => openChat(target)} title="The review chat is live; open the panel"><span class="sdot" aria-hidden="true"></span>In review</button>`;
  }
  return html`<button class=${'btn' + (primary ? ' btn-primary' : '')} disabled=${!node.ok} title=${node.ok ? null : `${node.name} is ${offWord(node)}`}
    onClick=${() => openChat(target)}>Review${count ? ` ${count}` : ''}${showKey && html`<span class="key">r</span>`}</button>`;
}

const ageOf = (q, now) => since(q.asked_at, now);

// One question row. Collapsed: id, context, default and age. Focused (and free to answer): the options, a note and Answer.
function QRow({ q, p, node, now, mode, focused, onFocusRow, oneClick, setOneClick, answer, openDrawer, discuss, why }) {
  const ref = useRef();
  const t = q.ticket ? p.tickets.find((x) => x.id === q.ticket) : null;
  const opts = q.options.map(parseOption).filter((o) => o.letter);
  const rec = opts.find((o) => o.recommended)?.letter || String(q.default).toUpperCase();
  const st = oneClick || {};
  const choice = st.choice || rec;
  const open = focused && mode === 'open' && !why;
  const pick = (l) => setOneClick({ ...st, choice: l, error: null });
  const submit = (e) => { e?.preventDefault(); if (!st.submitting) answer(q, choice, (st.note || '').trim()); };
  // One Tab stop for the options, on the chosen one; arrow keys move the choice, as in any radio group.
  const tabStop = opts.some((o) => o.letter === choice) ? choice : opts[0]?.letter;
  const onArrow = (e) => {
    const step = { ArrowDown: 1, ArrowRight: 1, ArrowUp: -1, ArrowLeft: -1 }[e.key];
    if (!step || !opts.length || st.submitting) return;
    e.preventDefault(); e.stopPropagation();
    const i = opts.findIndex((o) => o.letter === tabStop);
    const j = (i + step + opts.length) % opts.length;
    pick(opts[j].letter);
    e.currentTarget.querySelectorAll('.qr-opt')[j]?.focus();
  };
  // a–c pick and ↵ sends, on the row itself or its option buttons; the note field suspends them.
  const onLetter = (key) => {
    if (mode !== 'open' || why) return false;
    const l = key.length === 1 ? key.toUpperCase() : '';
    if (opts.some((o) => o.letter === l)) { pick(l); return true; }
    if (key === 'Enter') { submit(); return true; }
    return false;
  };
  const onKeyDown = (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.target !== ref.current && !(e.target.closest('.qr-opt') && e.key !== 'Enter')) return;
    if (onLetter(e.key)) { e.preventDefault(); e.stopPropagation(); }
  };
  const live = useRef(onLetter);
  live.current = onLetter;
  useEffect(() => {
    const on = (e) => live.current(e.detail);
    ref.current?.addEventListener('cairn-key', on);
    return () => ref.current?.removeEventListener('cairn-key', on);
  }, []);
  const cls = 'qr' + (q.gate && mode !== 'done' ? ' is-gate' : '') + (mode === 'done' ? ' is-done' : '') +
    (mode === 'current' ? ' is-current' : '') + (open ? ' is-open' : '');
  const right = mode === 'done' ? html`<span class="qr-check">✓ ${q.choice === 'other' ? 'note' : q.choice}${st.answeredAt ? ' · just now' : ''}</span>`
    : mode === 'current' ? html`<span class="qr-tag">discussing</span>`
    : mode === 'next' ? html`<span class="qr-next">next</span>`
    : q.gate ? html`<span class="qr-held" title=${'Asked ' + clock(q.asked_at)}>held ${ageOf(q, now)}</span>`
    : html`<span class="qr-meta" title=${'Asked ' + clock(q.asked_at)}><span class="qr-default">default </span><b>${q.default}</b> · ${ageOf(q, now)}</span>`;
  return html`<article class=${cls} ref=${ref} tabindex="0" data-nav data-project=${`${node.name}/${p.name}`} aria-label=${`${q.id}: ${q.context}`}
      aria-expanded=${mode === 'open' ? open : undefined} onFocusIn=${onFocusRow} onKeyDown=${onKeyDown}>
    <div class="qr-line">
      <button type="button" class="qr-id" tabindex="-1" title=${`Open ${q.id}`} onClick=${() => openDrawer({ node: node.name, project: p.name, path: q.path, kind: 'question', q })}>${q.id}</button>
      <span class="qr-text">${q.gate && mode !== 'done' && html`<b class="qr-gate">GATE</b>`}${q.context}${q.ticket && mode !== 'done' && html`<span class="mut"> · ${q.ticket}</span>`}</span>
      ${open ? html`<span class="qr-meta">${q.gate ? html`<span class="qr-held">held ${ageOf(q, now)}</span>` : html`default ${q.default} · ${ageOf(q, now)}`}</span>` : right}
    </div>
    ${focused && why && mode === 'open' && html`<div class="qr-body"><p class="qr-why">${why}</p></div>`}
    ${open && html`<div class="qr-body">
      ${t && html`<p class="qr-sub">Ticket ${t.id} · ${t.title}</p>`}
      <div class="qr-opts" role="radiogroup" aria-label=${`Answer for ${q.id}`} onKeyDown=${onArrow}>
        ${opts.map((o) => html`<button type="button" role="radio" aria-checked=${choice === o.letter} tabindex=${tabStop === o.letter ? 0 : -1} disabled=${st.submitting}
          class=${'qr-opt' + (choice === o.letter ? ' is-on' : '')} onClick=${() => pick(o.letter)}>
          <span class="qr-letter">${o.letter}</span>
          <span>${o.text}${o.recommended && html` <span class="rec">recommended</span>`}${o.reason && html` <span class="rec-why">${o.reason}</span>`}</span>
        </button>`)}
      </div>
      <form class="qr-act" onSubmit=${submit}>
        <input class="input qr-note" placeholder="Note for the lead (optional)" aria-label=${`Note for ${q.id}`} maxlength="4000"
          value=${st.note || ''} disabled=${st.submitting} onInput=${(e) => setOneClick({ ...st, choice, note: e.target.value })} />
        <button type="submit" class="btn btn-primary" disabled=${st.submitting}>${st.submitting ? 'Answering…' : html`Answer ${choice}<span class="key">↵</span>`}</button>
        <button type="button" class="linkbtn" onClick=${discuss}>Discuss in Review</button>
      </form>
      ${st.error && html`<p class="qr-err" role="alert">${st.error}</p>`}
    </div>`}
  </article>`;
}

function Inbox({ state, now, openDrawer, openChat, toast, refresh, review, panelOpen }) {
  const [filter, setFilter] = useState('');
  const [focusId, setFocusId] = useState(null);
  const [oneClick, setOneClickAll] = useState({});
  const f = filter.trim().toLowerCase();
  const projects = allProjects(state);
  const key = (node, p, q) => `${node.name}/${p.name}/${q.id}`;
  const setOneClick = (k) => (v) => setOneClickAll((m) => ({ ...m, [k]: v }));

  // A one-click answer: the row stays struck through until a minute after it was written.
  const answer = async (node, p, q, choice, note) => {
    const k = key(node, p, q);
    setOneClickAll((m) => ({ ...m, [k]: { ...m[k], choice, note, submitting: true, error: null } }));
    try {
      const r = await api.answer(node.name, p.name, q.id, choice, note, q.fingerprint);
      setOneClickAll((m) => ({ ...m, [k]: { ...m[k], submitting: false, answeredAt: Date.now() } }));
      toast(`${q.id} answered ${choice}`, 'ok', `wrote ${r?.path || q.path.replace(/\.md$/, '.answer.md')} · leaves the review queue`);
      await refresh();
    } catch (x) {
      const error = x.status === 409 && x.data?.question ? `${q.id} changed after you read it, so nothing was answered. Its new text is shown; read it again.`
        : x.status === 409 && /review (chat )?is open/i.test(x.message) ? `A review is open for ${p.name}; answer there.`
        : x.status === 409 ? `The node refused: ${x.message}` : `Could not answer: ${x.message}`;
      setOneClickAll((m) => ({ ...m, [k]: { ...m[k], submitting: false, error } }));
      if (x.status === 409) refresh();
    }
  };

  // Review mode (answered, discussing, next) lasts while the panel shows a live session for the project.
  const reviewing = (node, p) => panelOpen && review.live && review.target?.node === node.name && review.target.project === p.name;
  // A project whose snapshot cannot be read becomes a group that says so; the others are unaffected.
  const groups = projects.map(({ node, p }) => {
    try { return groupOf(node, p); } catch (error) { return { node, p, error, qs: [], open: 0, gate: false }; }
  });
  function groupOf(node, p) {
    const live = isLive(chatOf(state, node.name, p.name));
    const inPanel = reviewing(node, p);
    const answeredHere = new Map(inPanel ? review.events.filter((e) => e.kind === 'answered').map((e) => [e.id, e.choice]) : []);
    const recent = (q) => (oneClick[key(node, p, q)]?.answeredAt || 0) > now - 60000;
    let qs = p.questions.filter((q) => !q.answered || answeredHere.has(q.id) || recent(q));
    if (f) {
      qs = qs.filter((q) => {
        const t = p.tickets.find((x) => x.id === q.ticket);
        return [p.name, node.name, q.id, q.context, t?.title, ...q.options].join(' ').toLowerCase().includes(f);
      });
    }
    qs.sort(byFocus);
    const asked = inPanel ? askedId(review.events) : null;
    const pending = qs.filter((q) => !q.answered);
    const cur = pending.find((q) => q.id === asked) || null;
    const next = cur ? pending[pending.indexOf(cur) + 1] : null;
    const modeOf = (q) => (q.answered ? 'done' : q === cur ? 'current' : q === next ? 'next' : inPanel ? 'plain' : 'open');
    return { node, p, qs, live: live || inPanel, inPanel, modeOf, open: openQs(p).length, gate: openQs(p).some((q) => q.gate) };
  }
  const broken = groups.filter((g) => g.error);
  // Groups keep their place while their rows get struck through: gated first, then oldest, over the rows shown.
  const shown = groups.filter((g) => g.qs.length)
    .sort((a, b) => Number(b.qs.some((q) => q.gate)) - Number(a.qs.some((q) => q.gate)) || Date.parse(a.qs[0].asked_at) - Date.parse(b.qs[0].asked_at));
  const ready = projects.filter(({ node, p }) => resultReady(state, node, p));
  const isReady = (g) => ready.some((x) => x.node === g.node && x.p === g.p);
  const attention = projects.map(({ node, p }) => ({ node, p, a: attentionOf(state, node, p) })).filter((x) => x.a);
  const needs = (g) => attention.some((x) => x.node === g.node && x.p === g.p);
  const quiet = groups.filter((g) => !g.error && !g.open && !g.qs.length && !isReady(g) && !needs(g));
  const totalOpen = groups.reduce((n, g) => n + g.open, 0);
  const gates = groups.reduce((n, g) => n + openQs(g.p).filter((q) => q.gate).length, 0);
  const inPanel = groups.find((g) => g.inPanel);
  // Review mode hides the filter field, so a filter left over from before is cleared rather than silently applied.
  const reviewKey = inPanel ? `${inPanel.node.name}/${inPanel.p.name}` : '';
  useEffect(() => { if (reviewKey) setFilter(''); }, [reviewKey]);
  const focusGroup = shown.find((g) => g.qs.some((q) => key(g.node, g.p, q) === focusId)) || shown[0];

  const brokenNote = broken.map((g) => projectError(g.node, g.p, g.error));
  if (totalOpen === 0 && !shown.length && !ready.length && !attention.length) return html`<div class="page page-inbox"><${InboxEmpty} state=${state} />${brokenNote}</div>`;
  return html`<div class="page page-inbox">
    <header class="inbox-head">
      <div>
        <h1>${inPanel ? `${inPanel.open} left in review` : !totalOpen && attention.length ? `${plural(attention.length, 'lead')} ${attention.length === 1 ? 'needs' : 'need'} attention`
          : !totalOpen && ready.length ? `${plural(ready.length, 'result')} ready` : `${plural(totalOpen, 'question')} waiting`}${!inPanel && gates > 0 && html` <span class="gated">· ${gates} gated</span>`}${!inPanel && totalOpen > 0 && attention.length > 0 && html` <span class="att-count">· ${attention.length} need${attention.length === 1 ? 's' : ''} attention</span>`}</h1>
        <p class="lede">${totalOpen || inPanel || (!ready.length && !attention.length) ? 'Answer here, or open Review to talk it through. Both write the same answer file.'
          : attention.length ? 'No questions are waiting. A lead that stopped needs a Restart, or waits for its limit to reset.'
          : 'No questions are waiting. Get a copy puts a finished project on this Mac and opens it in Finder.'}</p>
      </div>
      ${!inPanel && totalOpen > 0 && html`<label class="filter-box">
        <input type="search" placeholder="Filter" aria-label="Filter questions" data-filter value=${filter} onInput=${(e) => setFilter(e.target.value)} />
        <kbd aria-hidden="true">/</kbd>
      </label>`}
    </header>
    ${brokenNote}
    ${shown.length === 0 && totalOpen > 0 && html`<div class="empty empty-tight"><p>No questions match “${filter}”.</p></div>`}
    ${shown.map((g) => html`<${Guard} key=${g.node.name + '/' + g.p.name} retry=${state} fallback=${(e) => projectError(g.node, g.p, e)} render=${() => {
      const { node, p } = g;
      const why = g.live && !g.inPanel ? 'The review chat for this project is open, so it owns the questions. r opens the panel.'
        : !node.ok ? `${node.name} is ${offWord(node)}, so answers can't be written now.` : null;
      return html`<section class="grp" aria-labelledby=${'g-' + node.name + '-' + p.name}>
        <header class="grp-head" data-project=${`${node.name}/${p.name}`}>
          <h2 id=${'g-' + node.name + '-' + p.name}><a href=${projectHref(node.name, p.name)}>${p.name}</a></h2>
          <span class="grp-node">${node.name}</span>
          ${leadPhrase(p)}
          <span class="spacer"></span>
          ${!g.inPanel && html`<span class="grp-count">${plural(g.open, 'question')}</span>`}
          <${ReviewButton} state=${state} node=${node} p=${p} openChat=${openChat} review=${review}
            primary=${g.gate} count=${g.open} showKey=${g === focusGroup} />
        </header>
        <div class="qrows">
          ${g.qs.map((q) => {
            const k = key(node, p, q);
            return html`<${QRow} key=${q.id} q=${q} p=${p} node=${node} now=${now} mode=${g.modeOf(q)} focused=${focusId === k}
              onFocusRow=${() => setFocusId(k)} oneClick=${oneClick[k]} setOneClick=${setOneClick(k)} why=${why}
              answer=${(qq, c, n) => answer(node, p, qq, c, n)} openDrawer=${openDrawer}
              discuss=${() => openChat({ node: node.name, project: p.name, resume: wantsResume(chatOf(state, node.name, p.name)) })} />`;
          })}
        </div>
      </section>`;
    }} />`)}
    ${attention.length > 0 && html`<section class="grp" aria-labelledby="att-h">
      <header class="grp-head"><h2 id="att-h">Needs attention</h2><span class="grp-count">${plural(attention.length, 'lead')} stopped or stuck</span></header>
      ${attention.map(({ node, p, a }) => html`<${Guard} key=${node.name + '/' + p.name} retry=${state} fallback=${(e) => projectError(node, p, e)}
        render=${() => html`<${AttentionRow} node=${node} p=${p} a=${a} now=${now} toast=${toast} refresh=${refresh} openDrawer=${openDrawer} />`} />`)}
    </section>`}
    ${ready.length > 0 && html`<section class="grp" aria-labelledby="ready-h">
      <header class="grp-head"><h2 id="ready-h">Result ready</h2><span class="grp-count">${plural(ready.length, 'project')} finished; copy the result to this Mac</span></header>
      ${ready.map(({ node, p }) => html`<${Guard} key=${node.name + '/' + p.name} retry=${state} fallback=${(e) => projectError(node, p, e)}
        render=${() => html`<${ResultRow} node=${node} p=${p} toast=${toast} refresh=${refresh} />`} />`)}
    </section>`}
    ${quiet.length > 0 && html`<p class="inbox-foot">${quiet.map((g) => g.p.name).join(', ')} ${quiet.length > 1 ? 'have' : 'has'} nothing waiting.</p>`}
  </div>`;
}

function InboxEmpty({ state }) {
  const projects = allProjects(state);
  return html`
    <header class="inbox-head"><div>
      <h1>Nothing waiting</h1>
      <p class="lede">Leads carry on with the default until you answer. New questions show up here on the next sync.</p>
    </div></header>
    <div class="empty">
      <h2>Nothing needs you right now</h2>
      <p>When a lead needs a decision it writes a question file with a default and keeps going. The question shows up here, with a one-click answer and a Review button that opens a chat with a review session on its node.</p>
      ${!state.nodes.length && html`<p style="margin-top: 10px"><b>No nodes yet.</b> A node is any machine the Mac reaches over ssh. <a href="#/nodes">Add one on the Nodes page</a>, then start a project from New project.</p>`}
      ${projects.length > 0 && html`<ul class="empty-projects">
        ${projects.map(({ node, p }) => html`<${Guard} key=${node.name + '/' + p.name} retry=${state} fallback=${(e) => html`<li>${projectError(node, p, e)}</li>`}
          render=${() => html`<li>
            <a href=${projectHref(node.name, p.name)}>${p.name}</a>
            <span class="dim">${node.name}</span>
            ${leadPhrase(p)}
            <span class="dim">${statusLine(p)}</span>
          </li>`} />`)}
      </ul>`}
    </div>`;
}

/* ---------- projects ---------- */

function MirrorState({ m, head, now, long = false }) {
  if (!m || (!m.sha && !m.error)) return html`<span class="dim">Not mirrored yet</span>`;
  const last = m.sha ? html`<span class="mono">${m.sha.slice(0, 7)}</span> <span class="dim" title=${clock(m.synced_at)}>${ago(m.synced_at, now)}</span>` : null;
  if (m.error) {
    return long
      ? html`<span class="mirror-bad">Fetch failed:</span> <span class="mono mirror-err">${m.error}</span>${last && html`<span class="dim"> · last good copy </span>${last}`}`
      : html`<span class="mirror-bad" title=${m.error}>Fetch failed</span>${last && html`<span class="dim small mirror-sub">${m.sha.slice(0, 7)} · ${ago(m.synced_at, now)}</span>`}`;
  }
  const behind = head && !String(head).startsWith(m.sha.slice(0, 7));
  return html`${long && html`<span class="mono">${m.path}</span><span class="dim"> at </span>`}${last}${behind && html` <span class="mirror-behind" title=${`Newest commit on the node is ${head}`}>behind</span>`}`;
}

function ProjectsPage({ state, now }) {
  const [filter, setFilter] = useState('');
  const f = filter.trim().toLowerCase();
  const all = allProjects(state);
  const rows = all.filter(({ node, p }) => !f || [p.name, node.name, p.status, tokenOf(state, node, p)].join(' ').toLowerCase().includes(f));
  const dark = state.nodes.filter((n) => isDown(n) && !n.snapshot);
  const open = (node, p) => { location.hash = projectHref(node.name, p.name); };
  return html`<div class="page">
    <header class="page-head">
      <div>
        <h1>Projects</h1>
        <p class="lede">${all.length ? `${plural(all.length, 'project')} on ${plural(new Set(all.map((x) => x.node.name)).size, 'node')}.` : 'No projects yet.'}</p>
      </div>
      <div class="tools">
        ${all.length > 0 && html`<input class="filter" type="search" placeholder="Filter projects" aria-label="Filter projects" data-filter value=${filter} onInput=${(e) => setFilter(e.target.value)} />`}
        <a class="btn btn-primary" href="#/new">New project</a>
      </div>
    </header>
    ${all.length === 0
      ? html`<div class="empty"><h2>Start the first project</h2><p>New project scaffolds <span class="mono">work/</span> and <span class="mono">LEAD.md</span> in a repo on a node, picks the Claude token its lead uses, and can start the lead right away.</p></div>`
      : html`<div class="table-wrap"><table class="grid">
        <thead><tr>
          <th>Project</th><th>Node</th><th>Lead</th>
          <th class="num" title="Open tickets, including those waiting on you">Open</th>
          <th class="num" title="Open tickets with an unanswered question">Waiting</th>
          <th class="num">Closed</th>
          <th>Token</th><th>Last commit</th><th>Mirror</th>
        </tr></thead>
        <tbody>
          ${rows.map(({ node, p }) => html`<${Guard} key=${node.name + '/' + p.name} retry=${state}
            fallback=${(e) => html`<tr><td colspan="9">${projectError(node, p, e)}</td></tr>`} render=${() => {
            const m = projectEntry(state, node.name, p.name).mirror;
            const count = (fn) => p.tickets.filter(fn).length;
            const waiting = count((t) => t.state === 'waiting');
            return html`<tr class="row-link" tabindex="0" data-nav data-project=${`${node.name}/${p.name}`}
                onClick=${(e) => { if (!e.target.closest('a')) open(node, p); }}
                onKeyDown=${(e) => { if (e.key === 'Enter') open(node, p); }}>
              <td><a class="cell-main" href=${projectHref(node.name, p.name)} tabindex="-1">${p.name}</a></td>
              <td class=${isDown(node) ? 'state-bad' : ''}>${node.name}${!node.ok && html`<div class=${'small' + (node.stopped ? ' dim' : '')}>${offWord(node)}</div>`}</td>
              <td><${LeadState} p=${p} /><div class="dim small cell-sub">${statusLine(p)}</div></td>
              <td class="num">${count((t) => t.status === 'open')}</td>
              <td class=${'num' + (waiting ? ' is-waiting' : '')}>${waiting || html`<span class="dim">0</span>`}</td>
              <td class="num">${count((t) => t.status === 'closed')}</td>
              <td class="mono small">${tokenOf(state, node, p) || html`<span class="dim">none</span>`}</td>
              <td class="nowrap" title=${clock(p.commits[0]?.date)}>${p.commits[0] ? ago(p.commits[0].date, now) : html`<span class="dim">none</span>`}</td>
              <td class="small"><${MirrorState} m=${m} head=${p.commits[0]?.sha} now=${now} /></td>
            </tr>`;
          }} />`)}
          ${rows.length === 0 && html`<tr><td colspan="9" class="dim">No projects match “${filter}”.</td></tr>`}
        </tbody>
      </table></div>`}
    ${dark.length > 0 && html`<p class="muted-note table-note">${dark.map((n) => n.name).join(', ')} ${dark.length > 1 ? 'are' : 'is'} unreachable, so ${dark.length > 1 ? 'their' : 'its'} projects are not listed. <a href="#/nodes">Nodes</a></p>`}
  </div>`;
}

/* ---------- new project ---------- */

const GIT_URL_RE = /^(https?:\/\/|ssh:\/\/|git:\/\/|file:\/\/|[\w.-]+@[\w.-]+:)\S+$/;

// The node's home, when the ssh target or an existing project reveals it; otherwise the node decides.
function homeOf(node) {
  if (!node) return null;
  const user = node.ssh.includes('@') ? node.ssh.split('@')[0] : null;
  if (user) return user === 'root' ? '/root' : `/home/${user}`;
  const m = /^(.*)\/work\/[^/]+$/.exec(node.snapshot?.projects?.[0]?.path || '');
  return m ? m[1] : null;
}

function NewProjectPage({ state, route, toast, refresh }) {
  const nodes = state.nodes;
  const initialNode = nodes.find((n) => n.name === route.node && n.ok) || nodes.find((n) => n.ok);
  const [f, setF] = useState({ name: '', node: initialNode?.name || '', repo: 'new', url: '', path: '', destination: '', notes: '', tickets: [], token: '', model: null, subagent_model: null, start: true });
  const [touched, setTouched] = useState({});
  const [tried, setTried] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [created, setCreated] = useState(null);
  const [nodeFilter, setNodeFilter] = useState('');
  const form = useRef();
  const set = (k) => (v) => setF((x) => ({ ...x, [k]: v }));
  const touch = (k) => () => setTouched((t) => ({ ...t, [k]: true }));

  const node = nodes.find((n) => n.name === f.node);
  const allowed = node?.allowed_tokens || [];
  const token = allowed.includes(f.token) ? f.token : allowed[0] || '';
  const missing = node ? missingDeps(node) : [];
  const blocking = missing.filter((d) => d === 'claude' || d === 'tmux');
  const canStart = !blocking.length;
  const home = homeOf(node);
  const pathPlaceholder = `${home || '$HOME'}/work/${f.name || '<name>'}`;
  const rows = f.tickets.map((t) => ({ ...t, title: t.title.trim(), question: t.question.trim() }));

  const errors = {
    name: !f.name ? 'Name the project.'
      : !NAME_RE.test(f.name) ? 'Use lowercase letters, digits and dashes, starting with a letter or digit, up to 40.'
      : f.name.endsWith('-review') ? 'A name may not end in -review: that is the name of a project\'s review session.'
      : node?.snapshot?.projects.some((p) => p.name === f.name) ? `${node.name} already has a project named ${f.name}.` : null,
    node: !node ? 'Pick a node.' : !node.ok ? `${node.name} is ${offWord(node)}.` : null,
    url: f.repo !== 'clone' ? null : !f.url.trim() ? 'Paste the git URL to clone.'
      : !GIT_URL_RE.test(f.url.trim()) ? 'Use an https://, ssh:// or user@host:path URL.' : null,
    path: f.repo === 'existing' && !f.path.trim() ? 'Give the path of the git repo on the node.'
      : f.path.trim() && !f.path.trim().startsWith('/') ? 'Use an absolute path, starting with /.' : null,
    destination: !f.destination.trim() ? 'Say where the project should end up, in one or two lines.' : null,
    token: node?.ok && !allowed.length ? `${node.name} allows no tokens yet. Allow one on the Nodes page first.` : null,
  };
  const rowErrors = rows.map((t) => (!t.title && t.question ? 'Give this ticket a title, or remove it.' : null));
  const shown = (k) => (tried || touched[k] || (k === 'name' && f.name) ? errors[k] : null);
  const problems = Object.values(errors).filter(Boolean).length + rowErrors.filter(Boolean).length;

  const setTicket = (i, k) => (e) => setF((x) => ({ ...x, tickets: x.tickets.map((t, j) => (j === i ? { ...t, [k]: e.target.value } : t)) }));
  const addTicket = () => {
    setF((x) => ({ ...x, tickets: [...x.tickets, { title: '', type: 'research', question: '' }] }));
    setTimeout(() => [...form.current.querySelectorAll('.tk-row input')].pop()?.focus(), 0);
  };
  const removeTicket = (i) => setF((x) => ({ ...x, tickets: x.tickets.filter((_, j) => j !== i) }));

  const submit = async (e) => {
    e.preventDefault();
    setTried(true);
    if (problems) { firstInvalid(form.current); return; }
    setBusy(true); setError(null);
    const body = {
      node: node.name, project: f.name,
      repo: f.repo === 'clone' ? { kind: 'clone', url: f.url.trim() } : { kind: f.repo },
      destination: f.destination.trim(), notes: f.notes.trim(),
      tickets: rows.filter((t) => t.title).map(({ title, type, question }) => ({ title, type, question })),
      token, model: f.model, subagent_model: f.subagent_model, start: f.start && canStart,
    };
    if (f.path.trim()) body.path = f.path.trim();
    try {
      const r = await api.createProject(body);
      await refresh();
      if (r.start_error) { setCreated(r); return; }
      toast(r.started ? `Created ${r.project}; the lead is starting` : `Created ${r.project}`);
      location.hash = projectHref(node.name, r.project);
    } catch (err) {
      setError(err.message);
    } finally { setBusy(false); }
  };

  const nf = nodeFilter.trim().toLowerCase();
  const pickable = nodes.filter((n) => !nf || `${n.name} ${n.ssh}`.toLowerCase().includes(nf));

  return html`<div class="page page-form">
    <header class="page-head">
      <div>
        <div class="crumbs"><a href="#/projects">Projects</a><span aria-hidden="true">/</span><span>New</span></div>
        <h1>New project</h1>
        <p class="lede">Scaffolds <span class="mono">work/</span> and <span class="mono">LEAD.md</span> in a repo on the node with <span class="mono">cairn-node init</span>.</p>
      </div>
    </header>

    <form class="wizard" ref=${form} onSubmit=${submit} novalidate>
      <section class="wz-sec">
        <div class="wz-head"><h2>Name</h2><p>Names the repo folder and the tmux session.</p></div>
        <div class="wz-body">
          <${Field} id="np-name" label="Project name" error=${shown('name')}
            hint=${f.name && !errors.name ? html`Session <span class="mono">${f.name}</span>, review session <span class="mono">${f.name}-review</span>` : 'Lowercase letters, digits and dashes.'}>
            <input id="np-name" class="input input-name" value=${f.name} autocomplete="off" spellcheck=${false} placeholder="my-research"
              aria-invalid=${!!shown('name')} aria-describedby="np-name-msg"
              onInput=${(e) => set('name')(e.target.value.trim())} onBlur=${touch('name')} />
          </${Field}>
        </div>
      </section>

      <section class="wz-sec">
        <div class="wz-head"><h2 id="np-node-label">Node</h2><p>Where the lead runs. Unreachable nodes can't take a project.</p></div>
        <div class="wz-body">
          ${nodes.length > 6 && html`<input class="filter filter-sm np-filter" type="search" placeholder="Filter nodes" aria-label="Filter nodes" value=${nodeFilter} onInput=${(e) => setNodeFilter(e.target.value)} />`}
          ${nodes.length === 0
            ? html`<div class="inline-error">There are no nodes yet. <a href="#/nodes">Add one on the Nodes page</a> first.</div>`
            : html`<div class="np-list" role="radiogroup" aria-labelledby="np-node-label">
              ${pickable.map((n) => {
                const miss = missingDeps(n);
                return html`<label class=${'np-opt' + (f.node === n.name ? ' is-checked' : '') + (n.ok ? '' : ' is-disabled')}>
                  <input type="radio" name="np-node" value=${n.name} checked=${f.node === n.name} disabled=${!n.ok}
                    aria-invalid=${!!shown('node') && f.node === n.name} onChange=${() => set('node')(n.name)} />
                  <span class="np-main"><span class="np-name">${n.name}</span><span class="mono dim small">${sshTarget(n)}</span></span>
                  <span class="np-facts">${n.ok
                    ? html`<span>${mb(n.snapshot?.free_ram_mb)} RAM</span><span>${mb(n.snapshot?.free_disk_mb)} disk</span>
                        <span>${plural(n.snapshot?.projects.length || 0, 'project')}</span>
                        ${miss.length > 0 && html`<span class="np-missing">${miss.join(', ')} missing</span>`}`
                    : n.stopped ? html`<span class="dim">Stopped</span>` : html`<span class="state-bad">Unreachable</span>`}</span>
                </label>`;
              })}
            </div>`}
          ${shown('node') && html`<p class="field-error">${errors.node}</p>`}
        </div>
      </section>

      <section class="wz-sec">
        <div class="wz-head"><h2 id="np-repo-label">Repository</h2><p>A fresh repo, a clone, or a repo already on the node. Init commits only the files it adds.</p></div>
        <div class="wz-body">
          <div class="seg" role="radiogroup" aria-labelledby="np-repo-label">
            ${[['new', 'New empty repo'], ['clone', 'Clone a git URL'], ['existing', 'Existing repo']].map(([k, l]) => html`<label class=${'seg-opt' + (f.repo === k ? ' is-checked' : '')}>
              <input type="radio" name="np-repo" value=${k} checked=${f.repo === k} onChange=${() => set('repo')(k)} />${l}
            </label>`)}
          </div>
          ${f.repo === 'clone' && html`<${Field} id="np-url" label="Git URL" error=${shown('url')} hint="https://, ssh:// or user@host:path. The node clones it with its own git credentials.">
            <input id="np-url" class="input mono-input" value=${f.url} autocomplete="off" spellcheck=${false} placeholder="git@github.com:you/repo.git"
              aria-invalid=${!!shown('url')} aria-describedby="np-url-msg" onInput=${(e) => set('url')(e.target.value)} onBlur=${touch('url')} />
          </${Field}>`}
          <${Field} id="np-path" label=${f.repo === 'existing' ? 'Path of the repo on the node' : 'Repo path on the node'} optional=${f.repo !== 'existing'} error=${shown('path')}
            hint=${f.repo === 'existing' ? 'Must already be a git repo without work/map.md.' : 'Leave empty for the default shown.'}>
            <input id="np-path" class="input mono-input" value=${f.path} autocomplete="off" spellcheck=${false}
              placeholder=${f.repo === 'existing' ? `${home || '$HOME'}/src/…` : pathPlaceholder}
              aria-invalid=${!!shown('path')} aria-describedby="np-path-msg" onInput=${(e) => set('path')(e.target.value)} onBlur=${touch('path')} />
          </${Field}>
        </div>
      </section>

      <section class="wz-sec">
        <div class="wz-head"><h2>Destination</h2><p>Where the project should end up. It heads <span class="mono">work/map.md</span>.</p></div>
        <div class="wz-body">
          <${Field} id="np-dest" label="Destination" error=${shown('destination')} hint="One or two lines.">
            <textarea id="np-dest" class="input" rows="3" value=${f.destination}
              placeholder="Find out which open-source tile cache serves a city map fastest on a laptop, with numbers."
              aria-invalid=${!!shown('destination')} aria-describedby="np-dest-msg" onInput=${(e) => set('destination')(e.target.value)} onBlur=${touch('destination')}></textarea>
          </${Field}>
          <${Field} id="np-notes" label="Notes and skills for the lead" optional hint="Conventions, skills to load, things to avoid. Goes into LEAD.md.">
            <textarea id="np-notes" class="input" rows="4" value=${f.notes} aria-describedby="np-notes-msg"
              placeholder="Cite sources for every claim. Never delete data outside work/." onInput=${(e) => set('notes')(e.target.value)}></textarea>
          </${Field}>
        </div>
      </section>

      <section class="wz-sec">
        <div class="wz-head"><h2>First tickets</h2><p>Optional. The lead works these first and adds its own. You can add more later.</p></div>
        <div class="wz-body">
          ${f.tickets.length > 0 && html`<ol class="tk-rows">
            ${f.tickets.map((t, i) => html`<li class=${'tk-row' + (tried && rowErrors[i] ? ' has-error' : '')} key=${i}>
              <span class="mono dim tk-num">${String(i + 1).padStart(2, '0')}</span>
              <input class="input" aria-label=${`Ticket ${i + 1} title`} placeholder="Title" value=${t.title} onInput=${setTicket(i, 'title')}
                aria-invalid=${!!(tried && rowErrors[i])} />
              <select class="input" aria-label=${`Ticket ${i + 1} type`} value=${t.type} onChange=${setTicket(i, 'type')}>
                ${TYPES.map((ty) => html`<option value=${ty}>${ty}</option>`)}
              </select>
              <button type="button" class="btn btn-quiet btn-sm" onClick=${() => removeTicket(i)} aria-label=${`Remove ticket ${i + 1}`}>Remove</button>
              <textarea class="input tk-q" rows="2" aria-label=${`Ticket ${i + 1} question`} placeholder="The question this ticket answers" value=${t.question} onInput=${setTicket(i, 'question')}></textarea>
              ${tried && rowErrors[i] && html`<p class="field-error tk-err">${rowErrors[i]}</p>`}
            </li>`)}
          </ol>`}
          <button type="button" class="btn btn-sm" onClick=${addTicket}><span class="plus" aria-hidden="true"></span>Add a ticket</button>
        </div>
      </section>

      <section class="wz-sec">
        <div class="wz-head"><h2>Token, models and start</h2><p>The Claude login for the lead and review sessions, and the models they run. Only tokens allowed on the node are listed.</p></div>
        <div class="wz-body">
          <${Field} id="np-token" label="Token" error=${node?.ok ? errors.token : null}
            hint=${node ? `Allowed on ${node.name}. Change it later on the project page.` : null}>
            <select id="np-token" class="input input-select" value=${token} disabled=${!allowed.length}
              aria-invalid=${!!(node?.ok && errors.token)} aria-describedby="np-token-msg" onChange=${(e) => set('token')(e.target.value)}>
              ${allowed.map((l) => {
                const t = state.tokens.find((x) => x.label === l);
                return html`<option value=${l}>${l}${t ? `  ·  ${kindLabel(t.kind)}${t.last4 ? ` ··${t.last4}` : ''}` : ''}</option>`;
              })}
            </select>
          </${Field}>
          <div class="form-grid form-grid-2 np-models">
            <${Field} id="np-model" label="Lead model" hint="Default is the account's default model.">
              <${ModelSelect} id="np-model" value=${f.model} onChange=${set('model')} />
            </${Field}>
            <${Field} id="np-submodel" label="Subagent model" hint="For the subagents the lead starts.">
              <${ModelSelect} id="np-submodel" value=${f.subagent_model} onChange=${set('subagent_model')} />
            </${Field}>
          </div>
          <div class="field">
            <label class=${'switch' + (canStart ? '' : ' is-disabled')}>
              <input type="checkbox" role="switch" checked=${f.start && canStart} disabled=${!canStart} onChange=${(e) => set('start')(e.target.checked)} aria-describedby="np-start-msg" />
              <span class="switch-track" aria-hidden="true"></span>
              <span>Start the lead now</span>
            </label>
            <p class=${canStart ? 'field-hint' : 'field-error'} id="np-start-msg">${canStart
              ? 'It reads LEAD.md and works the map. Otherwise start it from the project page.'
              : `${blocking.join(' and ')} ${blocking.length > 1 ? 'are' : 'is'} missing on ${node?.name}, so the lead can't start yet. The project can still be created.`}</p>
          </div>
        </div>
      </section>

      <div class="form-foot">
        <div class="form-foot-msg" aria-live="polite">
          ${busy ? html`<span class="progress"><span class="spinner" aria-hidden="true"></span>Running <span class="mono">cairn-node init</span> on ${node?.name}${f.start && canStart ? ', then starting the lead' : ''}…</span>`
            : error ? html`<div class="inline-error" role="alert"><b>Could not create the project.</b> ${error}</div>`
            : created ? html`<div class="inline-warn" role="alert"><b>Created ${created.project}</b> at <span class="mono">${created.path}</span>, but the lead did not start: ${created.start_error}
                <a class="btn btn-sm" href=${projectHref(node.name, created.project)}>Open project page</a></div>`
            : tried && problems ? html`<span class="field-error">${problems === 1 ? 'One field needs' : `${problems} fields need`} attention.</span>` : null}
        </div>
        <a class="btn btn-quiet" href="#/projects">Cancel</a>
        <button class="btn btn-primary" type="submit" disabled=${busy || !!created}>${busy ? 'Creating…' : 'Create project'}</button>
      </div>
    </form>
  </div>`;
}

/* ---------- nodes ---------- */

// `login` is the node's own Claude login (snapshot, cairn-node 0.4.6); "no" matters where a node-login token may run.
function Deps({ deps, login, needsLogin }) {
  if (!deps) return html`<span class="dim">unknown</span>`;
  return html`<ul class="deps">${Object.entries(deps).map(([k, v]) => html`<li class=${!v || (k === 'claude' && login === 'no' && needsLogin) ? 'is-missing' : ''}>
    ${k} ${v ? html`<span class="dim">${v}</span>` : html`<b>missing</b>`}${k === 'claude' && v && login === 'no' && html` <span class=${needsLogin ? '' : 'dim'}>· not signed in</span>`}
  </li>`)}</ul>`;
}

function Chips({ items, empty = 'none' }) {
  return items.length ? html`<span class="chips">${items.map((x) => html`<span class="chip chip-static">${x}</span>`)}</span>` : html`<span class="dim">${empty}</span>`;
}

function AddNodeForm({ state, onClose, toast, refresh }) {
  const [hosts, setHosts] = useState(null);
  const [ssh, setSsh] = useState('');
  const [port, setPort] = useState('');
  const [name, setName] = useState(null);
  const [tokens, setTokens] = useState([]);
  const [tried, setTried] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [check, setCheck] = useState(null);
  const ref = useRef();
  useEscape(onClose, ref);
  useEffect(() => {
    api.sshHosts().then((r) => setHosts(r.hosts || [])).catch(() => setHosts([]));
    ref.current?.querySelector('input')?.focus();
  }, []);

  const derived = ssh.trim().replace(/^.*@/, '').toLowerCase().replace(/[^a-z0-9-]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 40);
  const nm = name ?? derived;
  const errors = {
    ssh: !ssh.trim() ? 'Pick a host from ~/.ssh/config or type user@host.' : /\s/.test(ssh.trim()) ? 'An ssh target has no spaces.' : null,
    port: port && !(/^\d+$/.test(port) && +port > 0 && +port < 65536) ? 'A port is a number from 1 to 65535.' : null,
    name: !nm ? 'Name the node.' : !NAME_RE.test(nm) ? 'Lowercase letters, digits and dashes.'
      : state.nodes.some((n) => n.name === nm) ? `A node named ${nm} already exists.` : null,
  };
  const shown = (k) => (tried || (k === 'name' && nm) || (k === 'port' && port) ? errors[k] : null);

  const submit = async (e) => {
    e.preventDefault();
    setTried(true);
    if (Object.values(errors).some(Boolean)) { firstInvalid(ref.current); return; }
    setBusy(true); setError(null);
    try {
      const r = await api.addNode({ name: nm, ssh: ssh.trim(), port: port ? +port : null, allowed_tokens: tokens });
      setCheck({ ...r.check, name: nm });
      await refresh();
      toast(`Node ${nm} added`);
    } catch (err) { setError(err.message); } finally { setBusy(false); }
  };
  const again = () => { setCheck(null); setSsh(''); setPort(''); setName(null); setTokens([]); setTried(false); };

  if (check) {
    const miss = Object.entries(check.deps || {}).filter(([, v]) => !v).map(([k]) => k);
    return html`<section class="panel" ref=${ref} aria-label="Node added">
      <header class="panel-head"><h2>${check.name} is set up</h2>
        <button type="button" class="btn btn-quiet btn-sm" onClick=${onClose}>Close <kbd>Esc</kbd></button></header>
      <p class="panel-lede">Installed <span class="mono">cairn-node</span> and the <span class="mono">cairn-review</span> skill on <span class="mono">${check.host}</span>.</p>
      <dl class="check">
        <div><dt>Free RAM</dt><dd>${mb(check.free_ram_mb)}</dd></div>
        <div><dt>Free disk</dt><dd>${mb(check.free_disk_mb)}</dd></div>
        <div class="check-deps"><dt>Dependencies</dt><dd><${Deps} deps=${check.deps} /></dd></div>
      </dl>
      ${miss.length > 0 && html`<div class="inline-warn"><b>${miss.join(', ')} ${miss.length > 1 ? 'are' : 'is'} missing.</b> Install ${miss.length > 1 ? 'them' : 'it'} on the node, then sync: it happens within 30 s, or click “Synced …” in the top bar. Leads can't start until then.</div>`}
      <div class="form-actions"><button type="button" class="btn btn-primary" onClick=${onClose}>Done</button><button type="button" class="btn btn-quiet" onClick=${again}>Add another</button></div>
    </section>`;
  }

  return html`<form class="panel" ref=${ref} onSubmit=${submit} novalidate aria-label="Add node">
    <header class="panel-head"><h2>Add a node</h2>
      <button type="button" class="btn btn-quiet btn-sm" onClick=${onClose}>Close <kbd>Esc</kbd></button></header>
    <div class="form-grid form-grid-node">
      <${Field} id="an-ssh" label="SSH host" error=${shown('ssh')}
        hint=${hosts?.length ? `${plural(hosts.length, 'host')} from ~/.ssh/config, or type user@host.` : 'Type user@host or an alias.'}>
        <${Combo} id="an-ssh" options=${(hosts || []).map((h) => ({ value: h, hint: state.nodes.some((n) => n.ssh === h) ? 'already a node' : '' }))}
          value=${ssh} onChange=${setSsh} placeholder="alias or user@host" invalid=${!!shown('ssh')} describedBy="an-ssh-msg"
          emptyText=${hosts === null ? 'Reading ~/.ssh/config…' : 'No alias matches; the typed target is used as is'} />
      </${Field}>
      <${Field} id="an-port" label="Port" optional error=${shown('port')} hint="Empty means 22.">
        <input id="an-port" class="input" inputmode="numeric" value=${port} placeholder="22" autocomplete="off"
          aria-invalid=${!!shown('port')} aria-describedby="an-port-msg" onInput=${(e) => setPort(e.target.value.trim())} />
      </${Field}>
      <${Field} id="an-name" label="Name" error=${shown('name')} hint="Shown in the dashboard; defaults to the host.">
        <input id="an-name" class="input" value=${nm} autocomplete="off" spellcheck=${false}
          aria-invalid=${!!shown('name')} aria-describedby="an-name-msg" onInput=${(e) => setName(e.target.value.trim())} />
      </${Field}>
      <div class="span-all"><${Field} id="an-tokens" label="Allowed tokens" hint="Leads on this node may use these. The first is the default for new projects.">
        <${Combo} id="an-tokens" multiple options=${tokenOptions(state)} value=${tokens} onChange=${setTokens}
          placeholder="Search tokens" describedBy="an-tokens-msg" emptyText="No tokens match. Add one on the Tokens page." />
      </${Field}></div>
    </div>
    ${error && html`<div class="inline-error" role="alert"><b>Could not add the node.</b> ${error}</div>`}
    <div class="form-actions">
      <button class="btn btn-primary" type="submit" disabled=${busy}>${busy ? 'Installing…' : 'Add and install'}</button>
      <button type="button" class="btn btn-quiet" onClick=${onClose} disabled=${busy}>Cancel</button>
      ${busy && html`<span class="progress"><span class="spinner" aria-hidden="true"></span>Checking dependencies and installing <span class="mono">cairn-node</span> over ssh…</span>`}
    </div>
  </form>`;
}

function NodeTokensForm({ node, state, onDone, toast, refresh }) {
  const [value, setValue] = useState(node.allowed_tokens || []);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const ref = useRef();
  useEscape(onDone, ref);
  useEffect(() => { ref.current?.querySelector('input')?.focus(); }, []);
  const save = async (e) => {
    e.preventDefault();
    setBusy(true); setError(null);
    try {
      await api.nodeTokens(node.name, value);
      await refresh();
      toast(`Allowed tokens on ${node.name} saved`);
      onDone();
    } catch (err) { setError(err.message); setBusy(false); }
  };
  return html`<form class="inline-form" ref=${ref} onSubmit=${save}>
    <${Field} id=${'nt-' + node.name} label=${`Allowed tokens on ${node.name}`} hint="The first is the default for new projects. A project keeps its own token choice; changes apply at its next start.">
      <${Combo} id=${'nt-' + node.name} multiple options=${tokenOptions(state)} value=${value} onChange=${setValue} placeholder="Search tokens" describedBy=${'nt-' + node.name + '-msg'} />
    </${Field}>
    ${error && html`<div class="inline-error" role="alert">${error}</div>`}
    <div class="form-actions">
      <button class="btn btn-primary" type="submit" disabled=${busy}>${busy ? 'Saving…' : 'Save tokens'}</button>
      <button type="button" class="btn btn-quiet" onClick=${onDone}>Cancel</button>
    </div>
  </form>`;
}

// Update cairn-node reinstalls the hub's copy on the node (the same install as Add node).
function UpdateNodeButton({ n, toast, refresh }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const run = async () => {
    setBusy(true); setError(null);
    try {
      const r = await api.updateNode(n.name);
      await refresh();
      toast(`${n.name} runs cairn-node ${r.node_version}`);
    } catch (x) { setError(x.message); } finally { setBusy(false); }
  };
  return html`<button class="btn btn-sm" onClick=${run} disabled=${busy || !n.ok}>${busy ? 'Updating…' : 'Update cairn-node'}</button>
    ${error && html`<div class="field-error small">${error}</div>`}`;
}

/* ---------- sandboxes ---------- */

const SB_RUNNING = ['building', 'starting', 'waiting', 'adding'];
const SB_PHASE = {
  building: 'Building the image; this takes a few minutes',
  starting: 'Starting the container',
  waiting: 'Waiting for ssh to answer',
  adding: 'Installing cairn-node and adding the node',
};
const SB_IMAGES = [['slim', 'Slim', 'Tools only: tmux, git, python3, claude'], ['dev', 'Dev', 'sudo, Node 22, PostgreSQL + PostGIS']];
const liveOn = (n) => (n?.ok ? (n.snapshot?.projects || []).filter((p) => p.lead === 'alive' || p.review === 'alive') : []);
// As the hub: a node it cannot read, or one whose tmux did not answer for a lead, cannot be checked for live sessions.
const uncheckable = (n) => !n.ok || (n.snapshot?.projects || []).some((p) => p.lead === 'unknown');

// GET /api/sandboxes every 10 s while the page is open, every 2 s while a job runs. A job that reaches done added a node: refresh the state.
function useSandboxes(refresh) {
  const [sb, setSb] = useState(null);
  const [error, setError] = useState(null);
  const seen = useRef(null);
  const load = useCallback(async () => {
    let v;
    try { v = await api.sandboxes(); } catch (e) { setError(e.message); return; }
    setError(null);
    const done = v.jobs.filter((j) => j.phase === 'done').map((j) => j.node + j.started_at);
    if (seen.current && done.some((k) => !seen.current.has(k))) refresh();
    seen.current = new Set(done);
    setSb(v);
  }, []);
  const busy = !!sb?.jobs.some((j) => SB_RUNNING.includes(j.phase));
  useEffect(() => { load(); }, []);
  useEffect(() => { const t = setInterval(load, busy ? 2000 : POLL_MS); return () => clearInterval(t); }, [busy]);
  return [sb, load, error];
}

function JobProgress({ job }) {
  return html`${SB_RUNNING.includes(job.phase) && html`<p class="progress"><span class="spinner" aria-hidden="true"></span>${SB_PHASE[job.phase]}…</p>`}
    ${job.phase === 'failed' && html`<div class="inline-error" role="alert"><b>Could not create ${job.node}.</b> ${job.error}</div>`}
    ${job.log_tail?.length > 0 && html`<pre class="pane-dark sb-log" aria-label="Build output">${job.log_tail.join('\n')}</pre>`}`;
}

function NewSandboxForm({ state, sb, reload, onClose, onSent, preset }) {
  const [name, setName] = useState(preset || '');
  const [image, setImage] = useState('slim');
  const [memory, setMemory] = useState('');
  const [cpus, setCpus] = useState('');
  const [tokens, setTokens] = useState([]);
  const [tried, setTried] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [sent, setSent] = useState(null);
  const ref = useRef();
  useEscape(onClose, ref);
  useEffect(() => { ref.current?.querySelector('#ns-name')?.focus(); }, []);

  const jobOf = (nm) => sb?.jobs.find((j) => j.node === nm);
  const job = sent && jobOf(sent.name);
  const top = sb?.cpus_max || 1;
  const errors = {
    name: !name ? 'Name the sandbox.' : !NAME_RE.test(name) ? 'Lowercase letters, digits and dashes.'
      : state.nodes.some((n) => n.name === name) && jobOf(name)?.phase !== 'failed' ? `A node named ${name} already exists.` : null,
    memory: memory && !(/^\d+$/.test(memory) && +memory >= 1024) ? 'Whole MB, at least 1024.' : null,
    cpus: cpus && !(/^\d+(\.\d+)?$/.test(cpus) && +cpus >= 1 && +cpus <= top) ? `A number from 1 to ${top}.` : null,
  };
  const shown = (k) => (tried || (k === 'name' && name) || (k === 'memory' && memory) || (k === 'cpus' && cpus) ? errors[k] : null);

  const send = async (body) => {
    setBusy(true); setError(null);
    try {
      await api.createSandbox(body);
      setSent(body);
      onSent(body.name);
      await reload();
    } catch (err) { setError(err.message); } finally { setBusy(false); }
  };
  const submit = (e) => {
    e.preventDefault();
    setTried(true);
    if (Object.values(errors).some(Boolean)) { firstInvalid(ref.current); return; }
    send({ name, image, memory_mb: memory ? +memory : null, cpus: cpus ? +cpus : null, allowed_tokens: tokens });
  };

  if (sent && job) {
    return html`<section class="panel" ref=${ref} aria-label=${`Sandbox ${sent.name}`}>
      <header class="panel-head"><h2>${job.phase === 'done' ? `${sent.name} is set up` : `New sandbox ${sent.name}`}</h2>
        <button type="button" class="btn btn-quiet btn-sm" onClick=${onClose}>Close <kbd>Esc</kbd></button></header>
      <p class="panel-lede">${job.phase === 'done'
        ? html`Container <span class="mono">cairn-${sent.name}</span> runs and is a node: <span class="mono">agent@127.0.0.1</span>. Create a project on it from New project.`
        : html`Container <span class="mono">cairn-${sent.name}</span> from <span class="mono">cairn-${sent.image}:0.1</span>${sent.memory_mb ? `, ${mb(sent.memory_mb)}` : ''}${sent.cpus ? `, ${sent.cpus} CPU` : ''}. You can close this; the job keeps running and its row shows here.`}</p>
      <${JobProgress} job=${job} />
      <div class="form-actions">
        ${job.phase === 'failed' && html`<button type="button" class="btn btn-primary" onClick=${() => send(sent)} disabled=${busy}>${busy ? 'Retrying…' : 'Retry'}</button>`}
        <button type="button" class=${'btn' + (job.phase === 'done' ? ' btn-primary' : ' btn-quiet')} onClick=${onClose}>${job.phase === 'done' ? 'Done' : 'Close'}</button>
        ${error && html`<span class="field-error small">${error}</span>`}
      </div>
    </section>`;
  }

  return html`<form class="panel" ref=${ref} onSubmit=${submit} novalidate aria-label="New sandbox">
    <header class="panel-head"><h2>New sandbox</h2>
      <button type="button" class="btn btn-quiet btn-sm" onClick=${onClose}>Close <kbd>Esc</kbd></button></header>
    <p class="panel-lede">A Docker container on this Mac, added as a node on <span class="mono">agent@127.0.0.1</span> with a free port from 2222.</p>
    <div class="field">
      <span class="field-label" id="ns-image-label">Image</span>
      <div class="seg seg-wide seg-2" role="radiogroup" aria-labelledby="ns-image-label">
        ${SB_IMAGES.map(([k, l, d]) => html`<label class=${'seg-opt' + (image === k ? ' is-checked' : '')}>
          <input type="radio" name="ns-image" value=${k} checked=${image === k} onChange=${() => setImage(k)} />
          <span class="seg-title">${l}</span><span class="seg-desc">${d}</span>
        </label>`)}
      </div>
      ${sb && !sb.images[image] && html`<p class="field-hint">No <span class="mono">cairn-${image}:0.1</span> image yet: the hub builds it first${image === 'dev' && !sb.images.slim ? ', after slim' : ''}, a few minutes.</p>`}
    </div>
    <div class="form-grid form-grid-sandbox">
      <${Field} id="ns-name" label="Name" error=${shown('name')} hint=${`The node's name; the container is cairn-${name || '<name>'}.`}>
        <input id="ns-name" class="input" value=${name} autocomplete="off" spellcheck=${false} placeholder="sandbox-2"
          aria-invalid=${!!shown('name')} aria-describedby="ns-name-msg" onInput=${(e) => setName(e.target.value.trim())} />
      </${Field}>
      <${Field} id="ns-memory" label="Memory" optional error=${shown('memory')} hint="MB, at least 1024.">
        <input id="ns-memory" class="input" inputmode="numeric" value=${memory} placeholder="no cap" autocomplete="off"
          aria-invalid=${!!shown('memory')} aria-describedby="ns-memory-msg" onInput=${(e) => setMemory(e.target.value.trim())} />
      </${Field}>
      <${Field} id="ns-cpus" label="CPUs" optional error=${shown('cpus')} hint=${`1 to ${top}.`}>
        <input id="ns-cpus" class="input" inputmode="decimal" value=${cpus} placeholder="no cap" autocomplete="off"
          aria-invalid=${!!shown('cpus')} aria-describedby="ns-cpus-msg" onInput=${(e) => setCpus(e.target.value.trim())} />
      </${Field}>
      <div class="span-all"><${Field} id="ns-tokens" label="Allowed tokens" hint="Leads on this sandbox may use these. The first is the default for new projects.">
        <${Combo} id="ns-tokens" multiple options=${tokenOptions(state)} value=${tokens} onChange=${setTokens}
          placeholder="Search tokens" describedBy="ns-tokens-msg" emptyText="No tokens match. Add one on the Tokens page." />
      </${Field}></div>
    </div>
    ${error && html`<div class="inline-error" role="alert"><b>Could not create the sandbox.</b> ${error}</div>`}
    <div class="form-actions">
      <button class="btn btn-primary" type="submit" disabled=${busy}>${busy ? 'Starting…' : 'Create sandbox'}</button>
      <button type="button" class="btn btn-quiet" onClick=${onClose} disabled=${busy}>Cancel</button>
    </div>
  </form>`;
}

// Jobs the open form does not show: one started before a reload, or by another tab.
function SandboxJob({ job, onRetry }) {
  return html`<section class="panel sb-job" aria-label=${`Sandbox job ${job.node}`}>
    <header class="panel-head"><h2>New sandbox ${job.node}</h2>
      ${job.phase === 'failed' && html`<button type="button" class="btn btn-sm" onClick=${() => onRetry(job.node)}>Retry…</button>`}</header>
    <${JobProgress} job=${job} />
  </section>`;
}

// A stop the hub refused because the node cannot be checked offers "Stop anyway"; a live lead is never forced.
function SandboxPower({ s, live, reload, refresh, toast }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [unchecked, setUnchecked] = useState(false);
  const running = s.state === 'running';
  const go = async (force) => {
    setBusy(true); setError(null); setUnchecked(false);
    try {
      await (running ? api.sandboxStop : api.sandboxStart)(s.node, force === true);
      await Promise.all([reload(), refresh()]);
      toast(`${running ? 'Stopped' : 'Started'} ${s.node}`);
    } catch (x) { setError(x.message); setUnchecked(!!x.data?.unchecked); } finally { setBusy(false); }
  };
  if (s.state === 'missing') return null;
  return html`<button class="btn btn-quiet btn-sm" onClick=${() => go(false)} disabled=${busy || (running && live.length > 0)}
      title=${running && live.length ? `Lead alive on ${live.map((p) => p.name).join(', ')}` : undefined}>${busy ? (running ? 'Stopping…' : 'Starting…') : running ? 'Stop' : 'Start'}</button>
    ${error && html`<div class="field-error small">${unchecked ? `The hub cannot check ${s.node} for a running lead: ${error}. Stopping it stops any lead there.` : error}</div>`}
    ${unchecked && running && html`<button class="btn btn-danger btn-sm" onClick=${() => go(true)} disabled=${busy}>Stop anyway</button>`}`;
}

// The sign-in runs in the operator's own terminal: Claude Code shows a link there and reads the code back, so neither
// passes through the hub.
function SandboxSignIn({ s, login, onClose, toast, refresh }) {
  const ref = useRef();
  useEscape(onClose, ref);
  // While the panel is open and the node is not known to be signed in, ask the hub to read that node again every
  // few seconds, so the row shows the new login without waiting for the next full sync.
  const waiting = login !== 'yes' && !!refresh;   // no refresh: a sandbox the hub has no node for
  useEffect(() => {
    if (!waiting) return undefined;
    let busy = false;
    const t = setInterval(async () => {
      if (busy || document.visibilityState !== 'visible') return;
      busy = true;
      try { await api.sync(s.node); await refresh(); } catch { /* the next tick tries again */ } finally { busy = false; }
    }, 5000);
    return () => clearInterval(t);
  }, [waiting, s.node]);
  const copy = async () => {
    try { await navigator.clipboard.writeText(s.login_command); toast('Command copied'); }
    catch { toast('Copy failed: select the command and copy it by hand', 'error'); }
  };
  return html`<div class="confirm sb-login" ref=${ref} role="group" aria-label=${`Sign in ${s.node}`}>
    <p class="confirm-msg">${login === 'yes' ? html`<b>${s.node}</b> is signed in to Claude. To sign it in to another account, run` : login === 'no'
      ? html`<b>${s.node}</b> is not signed in to Claude, so a node-login token cannot run there. To sign it in, run`
      : html`To sign <b>${s.node}</b> in to its own Claude account, run`} this in a terminal on this Mac. Claude Code prints a link: open it, sign in, and paste the code the page shows back into the terminal. The login stays in the sandbox's home volume. Or run <span class="mono">cairn sandbox login ${s.node}</span>.</p>
    <pre class="sb-login-cmd mono">${s.login_command}</pre>
    <div class="form-actions">
      <button type="button" class="btn btn-primary btn-sm" onClick=${copy}>Copy command</button>
      <button type="button" class="btn btn-quiet btn-sm" onClick=${onClose}>Close</button>
      ${waiting && html`<span class="dim small">Checking the login every 5 s while this is open</span>`}
    </div>
  </div>`;
}

// Recreate keeps both volumes, so the home folder (the Claude login, every repo) and the host key survive.
function RecreateSandbox({ s, onCancel, reload, refresh, toast }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [unchecked, setUnchecked] = useState(false);
  const ref = useRef();
  useEscape(onCancel, ref);
  const go = async () => {
    setBusy(true); setError(null);
    try {
      await api.recreateSandbox(s.node, unchecked);
      onCancel();
      await Promise.all([reload(), refresh()]);
      toast(`Recreated ${s.node}; its home volume and Claude login stayed`);
    } catch (x) {
      setError(x.data?.unchecked ? `The hub cannot check ${s.node} for a running lead: ${x.message}. Recreating it ends any lead there.` : x.message);
      setUnchecked(!!x.data?.unchecked);
      setBusy(false);
    }
  };
  return html`<div class="confirm" ref=${ref} role="group" aria-label=${`Recreate ${s.node}`}>
    <p class="confirm-msg">Recreate sandbox <b>${s.node}</b>? The hub removes container <span class="mono">${s.container}</span> and runs a new one from <span class="mono">cairn-${s.image}:0.1</span> as it is now, on the same port. Both volumes stay: <span class="mono">${s.container}-home</span> keeps the Claude login and every repo, <span class="mono">${s.container}-hostkeys</span> keeps the host key. Anything else in the container goes.</p>
    ${error && html`<div class="inline-error" role="alert">${error}</div>`}
    <div class="form-actions">
      <button type="button" class="btn btn-danger" onClick=${go} disabled=${busy}>${busy ? 'Recreating…' : unchecked ? 'Recreate anyway' : 'Recreate sandbox'}</button>
      <button type="button" class="btn btn-quiet" onClick=${onCancel} disabled=${busy}>Cancel</button>
    </div>
  </div>`;
}

// Delete asks for the name: it removes the container's volumes, which hold everything the leads did there.
function DeleteSandbox({ s, onCancel, reload, refresh, toast }) {
  const [typed, setTyped] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  // Set once the hub refused because the node cannot be checked: the next submit deletes with force.
  const [unchecked, setUnchecked] = useState(false);
  const ref = useRef();
  useEscape(onCancel, ref);
  useLayoutEffect(() => { ref.current?.querySelector('input')?.focus(); }, []);
  const go = async (e) => {
    e.preventDefault();
    if (typed !== s.node) return;
    setBusy(true); setError(null);
    try {
      const r = await api.removeSandbox(s.node, typed, unchecked);
      onCancel();
      await Promise.all([reload(), refresh()]);
      toast(`Deleted ${s.node}${r.mirror ? `; its mirror stays at ${r.mirror}` : ''}`);
    } catch (x) {
      setError(x.data?.unchecked ? `The hub cannot check ${s.node} for a running lead: ${x.message}. Deleting it ends any lead there.` : x.message);
      setUnchecked(!!x.data?.unchecked);
      setBusy(false);
    }
  };
  return html`<form class="confirm" ref=${ref} onSubmit=${go} aria-label=${`Delete ${s.node}`}>
    <p class="confirm-msg">Delete sandbox <b>${s.node}</b>? This removes container <span class="mono">${s.container}</span>, its volumes <span class="mono">${s.container}-home</span> and <span class="mono">${s.container}-hostkeys</span> (everything in the container's home), and the node with its projects' settings. The repo mirrors on this Mac stay, so every commit the leads made is kept.</p>
    <div class="type-confirm">
      <label class="field-label" for=${'ds-' + s.node}>Type <span class="mono">${s.node}</span> to confirm</label>
      <input id=${'ds-' + s.node} class="input" value=${typed} autocomplete="off" spellcheck=${false} onInput=${(e) => setTyped(e.target.value.trim())} />
    </div>
    ${error && html`<div class="inline-error" role="alert">${error}</div>`}
    <div class="form-actions">
      <button type="submit" class="btn btn-danger" disabled=${busy || typed !== s.node}>${busy ? 'Deleting…' : unchecked ? 'Delete anyway' : 'Delete sandbox'}</button>
      <button type="button" class="btn btn-quiet" onClick=${onCancel} disabled=${busy}>Cancel</button>
    </div>
  </form>`;
}

function SandboxState({ s }) {
  if (!s) return null;
  const pct = s.cpu_pct == null ? null : `${s.cpu_pct.toFixed(s.cpu_pct < 10 ? 1 : 0)}%`;
  const mem = s.mem_used_mb == null ? null : `${mb(Math.round(s.mem_used_mb))} of ${s.mem_limit_mb ? mb(Math.round(s.mem_limit_mb)) : 'no cap'}`;
  return html`<div class="small sb-state">Container <b>${s.state}</b>${s.state === 'running' && pct != null ? html`<span class="dim"> · CPU ${pct} · ${mem}</span>` : ''}</div>`;
}

function NodesPage({ state, now, toast, refresh }) {
  const [adding, setAdding] = useState(false);
  const [creating, setCreating] = useState(null);
  const [sb, reloadSb, sbError] = useSandboxes(refresh);
  const [edit, setEdit] = useState(null);
  const [filter, setFilter] = useState('');
  const f = filter.trim().toLowerCase();
  const nodes = state.nodes.filter((n) => !f || [n.name, n.ssh, ...(n.allowed_tokens || []), ...(n.snapshot?.projects || []).map((p) => p.name)].join(' ').toLowerCase().includes(f));
  const toggle = (name, mode) => setEdit((e) => (e?.name === name && e.mode === mode ? null : { name, mode }));
  const close = () => setEdit(null);
  const sbOf = Object.fromEntries((sb?.sandboxes || []).map((s) => [s.node, s]));
  const orphans = (sb?.sandboxes || []).filter((s) => !state.nodes.some((n) => n.name === s.node));
  const jobs = (sb?.jobs || []).filter((j) => j.phase !== 'done' && j.node !== creating?.shown);
  const sbActions = (s, live, open, login) => html`
    <${SandboxPower} s=${s} live=${live} reload=${reloadSb} refresh=${refresh} toast=${toast} />
    <button class="btn btn-quiet btn-sm btn-danger-text" aria-expanded=${open === 'delete'} disabled=${live.length > 0}
      title=${live.length ? `Lead alive on ${live.map((p) => p.name).join(', ')}` : undefined} onClick=${() => toggle(s.node, 'delete')}>Delete</button>
    <div class="sb-more">
      ${s.login_command && html`<button class="btn btn-quiet btn-sm" aria-expanded=${open === 'login'} disabled=${s.state !== 'running'}
        title=${s.state !== 'running' ? 'Start the sandbox first' : undefined} onClick=${() => toggle(s.node, 'login')}>${login === 'yes' ? 'Signed in' : 'Sign in'}</button>`}
      ${s.image && html`<button class="btn btn-quiet btn-sm" aria-expanded=${open === 'recreate'} disabled=${live.length > 0}
        title=${live.length ? `Lead alive on ${live.map((p) => p.name).join(', ')}` : undefined} onClick=${() => toggle(s.node, 'recreate')}>Recreate</button>`}
    </div>
    ${live.length > 0 && html`<div class="dim small sb-why">Lead alive on ${live.map((p) => p.name).join(', ')}</div>`}`;
  return html`<div class="page">
    <header class="page-head">
      <div><h1>Nodes</h1><p class="lede">Machines the hub reaches over ssh, and the tokens their leads may use.</p>
        ${sb && !sb.docker && html`<p class="dim small sb-down">Docker is not running: ${sb.error}</p>`}
        ${sbError && html`<p class="field-error small sb-down" role="alert">Could not read the sandboxes: ${sbError}</p>`}</div>
      <div class="tools">
        <input class="filter" type="search" placeholder="Filter nodes" aria-label="Filter nodes" data-filter value=${filter} onInput=${(e) => setFilter(e.target.value)} />
        ${sb?.docker && html`<button class="btn" onClick=${() => setCreating({ preset: '' })} disabled=${!!creating}>New sandbox</button>`}
        <button class="btn btn-primary" onClick=${() => setAdding(true)} disabled=${adding}>Add node</button>
      </div>
    </header>
    ${adding && html`<${AddNodeForm} state=${state} onClose=${() => setAdding(false)} toast=${toast} refresh=${refresh} />`}
    ${creating && sb && html`<${NewSandboxForm} key=${creating.preset} state=${state} sb=${sb} reload=${reloadSb} preset=${creating.preset}
      onSent=${(nm) => setCreating((c) => c && { ...c, shown: nm })} onClose=${() => setCreating(null)} />`}
    ${jobs.map((j) => html`<${SandboxJob} key=${j.node} job=${j} onRetry=${(name) => setCreating({ preset: name })} />`)}
    ${state.nodes.length === 0 && !adding
      ? html`<div class="empty"><h2>No nodes yet</h2><p>A node is any machine the Mac can ssh into. Adding one installs <span class="mono">cairn-node</span> and the review skill and checks for tmux, git, python3 and claude.</p></div>`
      : html`<div class="table-wrap"><table class="grid grid-nodes">
        <thead><tr><th>Node</th><th>State</th><th class="num">Free RAM</th><th class="num">Free disk</th><th>Dependencies</th><th>Allowed tokens</th><th>Projects</th><th class="actions-col"><span class="sr">Actions</span></th></tr></thead>
        <tbody>
          ${nodes.map((n) => {
            const projects = n.snapshot?.projects || [];
            const live = liveOn(n);
            const open = edit?.name === n.name ? edit.mode : null;
            const s = sbOf[n.name];
            return html`
              <tr key=${n.name} class=${(isDown(n) ? 'is-bad' : '') + (open ? ' is-editing' : '')}>
                <td><div class="cell-main">${n.name}</div><div class="mono dim small nowrap">${sshTarget(n)}</div>
                  ${(s || n.sandbox) && html`<div class="sb-chip"><span class="chip chip-static">Sandbox${s?.image ? ` · ${s.image}` : ''}${s?.adopted ? ' · adopted' : ''}</span></div>`}
                  ${n.node_version && html`<div class=${'small nowrap' + (n.update_available ? ' node-old' : ' dim')}>cairn-node ${n.node_version}${n.update_available ? ', older than the hub' : ''}</div>`}
                  ${n.update_available && html`<div class="node-update"><${UpdateNodeButton} n=${n} toast=${toast} refresh=${refresh} /></div>`}</td>
                <td>${n.ok
                  ? html`<span class="state-ok"><${Dot} tone="ok" label="reachable" />Reachable</span>`
                  : n.stopped || (s && s.state !== 'running') ? html`<span class="state-off"><${Dot} tone="off" label="stopped" />Stopped</span>`
                  : html`<span class="state-bad"><${Dot} tone="bad" label="unreachable" />Unreachable</span><div class="node-error">${n.error || 'no error message'}</div>`}
                  <div class="dim small" title=${clock(n.synced_at)}>synced ${ago(n.synced_at, now)}</div>
                  <${SandboxState} s=${s} /></td>
                <td class="num">${n.ok ? mb(n.snapshot?.free_ram_mb) : html`<span class="dim">n/a</span>`}</td>
                <td class="num">${n.ok ? mb(n.snapshot?.free_disk_mb) : html`<span class="dim">n/a</span>`}</td>
                <td><${Deps} deps=${n.snapshot?.deps} login=${n.snapshot?.login}
                  needsLogin=${(n.allowed_tokens || []).some((l) => state.tokens.find((t) => t.label === l)?.kind === 'node-login')} /></td>
                <td><${Chips} items=${n.allowed_tokens || []} /></td>
                <td>${projects.length ? html`<ul class="plain">${projects.map((p) => html`<li><a href=${projectHref(n.name, p.name)}>${p.name}</a></li>`)}</ul>`
                  : html`<span class="dim">${n.ok ? 'none' : 'unknown'}</span>`}</td>
                <td class="actions">
                  <button class="btn btn-quiet btn-sm" aria-expanded=${open === 'tokens'} onClick=${() => toggle(n.name, 'tokens')}>Tokens</button>
                  ${s ? sbActions(s, live, open, n.snapshot?.login)
                    : n.sandbox ? sb && !sb.docker && html`<div class="dim small sb-why">Docker is not running</div>`
                    : html`<button class="btn btn-quiet btn-sm btn-danger-text" aria-expanded=${open === 'remove'} onClick=${() => toggle(n.name, 'remove')}>Remove</button>`}
                </td>
              </tr>
              ${open && html`<tr class="row-extra" key=${n.name + '-x'}><td colspan="8">
                ${open === 'tokens' && html`<${NodeTokensForm} node=${n} state=${state} onDone=${close} toast=${toast} refresh=${refresh} />`}
                ${open === 'delete' && s && html`<${DeleteSandbox} s=${s} onCancel=${close} reload=${reloadSb} refresh=${refresh} toast=${toast} />`}
                ${open === 'login' && s && html`<${SandboxSignIn} s=${s} login=${n.snapshot?.login} onClose=${close} toast=${toast} refresh=${refresh} />`}
                ${open === 'recreate' && s && html`<${RecreateSandbox} s=${s} onCancel=${close} reload=${reloadSb} refresh=${refresh} toast=${toast} />`}
                ${open === 'remove' && (live.length
                  ? html`<${Notice} onClose=${close}><b>${n.name} can't be removed while sessions run on it:</b> ${live.map((p) => `${p.name} (${[p.lead === 'alive' && 'lead', p.review === 'alive' && 'review'].filter(Boolean).join(' and ')})`).join(', ')}. Stop them from their project pages first.</${Notice}>`
                  : html`<${ConfirmBar} message=${!uncheckable(n)
                      ? html`Remove <b>${n.name}</b> from the hub? Only the hub's records change (<span class="mono">nodes.json</span> and its projects' token choices): the machine, its repos and the mirrors on this Mac stay as they are.`
                      : html`<b>${n.ok ? `tmux on ${n.name} did not answer` : `${n.name} is unreachable`}</b>, so the hub can't check for running leads or reviews. Remove it anyway? Only the hub's records change: anything running there keeps running, and the machine, its repos and the mirrors on this Mac stay as they are.`}
                      confirmLabel=${uncheckable(n) ? 'Remove anyway' : 'Remove node'} busyLabel="Removing…" onCancel=${close}
                      onConfirm=${async () => { await api.removeNode(n.name, uncheckable(n)); close(); await refresh(); toast(`Removed ${n.name} from the hub`); }} />`)}
              </td></tr>`}`;
          })}
          ${orphans.map((s) => html`
            <tr key=${'sb-' + s.node}>
              <td><div class="cell-main">${s.node}</div><div class="sb-chip"><span class="chip chip-static">Sandbox · ${s.image || '?'}</span></div></td>
              <td colspan="6"><${SandboxState} s=${s} /><div class="dim small">No node record for this sandbox. Create it again with the same name, or delete it.</div></td>
              <td class="actions">${sbActions(s, [], edit?.name === s.node ? edit.mode : null)}</td>
            </tr>
            ${edit?.name === s.node && html`<tr class="row-extra" key=${'sb-' + s.node + '-x'}><td colspan="8">
              ${edit.mode === 'delete' && html`<${DeleteSandbox} s=${s} onCancel=${close} reload=${reloadSb} refresh=${refresh} toast=${toast} />`}
              ${edit.mode === 'login' && html`<${SandboxSignIn} s=${s} onClose=${close} toast=${toast} />`}
              ${edit.mode === 'recreate' && html`<${RecreateSandbox} s=${s} onCancel=${close} reload=${reloadSb} refresh=${refresh} toast=${toast} />`}</td></tr>`}`)}
          ${nodes.length === 0 && html`<tr><td colspan="8" class="dim">No nodes match “${filter}”.</td></tr>`}
        </tbody>
      </table></div>`}
  </div>`;
}

/* ---------- tokens ---------- */

function Expiry({ iso, now }) {
  if (!iso) return html`<span class="dim">never</span>`;
  const left = Date.parse(iso) - now;
  const date = new Date(iso).toLocaleDateString([], { year: 'numeric', month: 'short', day: 'numeric' });
  if (left <= 0) return html`<span class="state-bad">expired ${date}</span>`;
  const days = Math.floor(left / 864e5);
  return html`<span class=${'nowrap' + (days < 30 ? ' expiring' : '')} title=${new Date(iso).toLocaleString()}>${date}<span class="dim small"> · ${days >= 2 ? `${days} d` : dur(left)} left</span></span>`;
}

const TOKEN_KINDS = [
  ['oauth', 'OAuth token', 'From claude setup-token; lasts a year'],
  ['node-login', 'Node login', 'The node signs in itself'],
];
const LABEL_ERR = (label, state, self) => (!label ? 'Give the token a label.' : /\s/.test(label) ? 'No spaces in a label.'
  : label.length > 40 ? 'Up to 40 characters.' : state.tokens.some((t) => t.label === label && t.label !== self) ? `A token named ${label} already exists.` : null);

// Add a token, or (replace = an existing token) put a new OAuth secret under the same label: its node allowances
// and project mappings stay. That is how an unsupported kind is replaced, and how an OAuth token is rotated.
function AddTokenForm({ state, onClose, toast, refresh, replace }) {
  const [kind, setKind] = useState('oauth');
  const [label, setLabel] = useState(replace?.label || '');
  const [account, setAccount] = useState(replace?.account || '');
  const [nodes, setNodes] = useState([]);
  const [tried, setTried] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [hasSecret, setHasSecret] = useState(false);
  const ref = useRef();
  const secret = useRef();
  useEscape(onClose, ref);
  useEffect(() => {
    ref.current?.scrollIntoView({ block: 'nearest' });
    ref.current?.querySelector(replace ? '#at-secret' : '#at-label')?.focus();
  }, []);

  const needsSecret = kind !== 'node-login';
  const errors = {
    label: replace ? null : LABEL_ERR(label, state),
    secret: needsSecret && !hasSecret ? 'Paste the setup token.' : null,
  };
  const shown = (k) => (tried || (k === 'label' && label) ? errors[k] : null);
  const allowedOn = replace ? state.nodes.filter((n) => n.allowed_tokens?.includes(replace.label)).map((n) => n.name) : [];

  const submit = async (e) => {
    e.preventDefault();
    setTried(true);
    if (Object.values(errors).some(Boolean)) { firstInvalid(ref.current); return; }
    const body = replace ? { label: replace.label, kind: 'oauth', nodes: [], replace: true } : { label, kind, nodes };
    if (account.trim()) body.account = account.trim();
    if (needsSecret) body.secret = secret.current.value.trim();
    // The secret leaves the field as soon as it is sent, whatever the outcome.
    if (secret.current) secret.current.value = '';
    setHasSecret(false);
    setBusy(true); setError(null);
    try {
      await api.addToken(body);
      await refresh();
      toast(replace ? `${replace.label} now holds the new OAuth token; its nodes and projects are unchanged`
        : `Token ${label} added${kind === 'node-login' ? '' : ' to the Keychain'}`);
      onClose();
    } catch (err) {
      setError(err.status === 409 && !replace ? `A token named ${label} already exists. Use Replace on its row to put a new secret under that label.` : err.message);
      setBusy(false);
    }
  };

  return html`<form class="panel" ref=${ref} onSubmit=${submit} novalidate aria-label=${replace ? `Replace ${replace.label}` : 'Add token'} autocomplete="off">
    <header class="panel-head"><h2>${replace ? html`Replace <span class="mono">${replace.label}</span> with an OAuth token` : 'Add a token'}</h2>
      <button type="button" class="btn btn-quiet btn-sm" onClick=${onClose}>Close <kbd>Esc</kbd></button></header>
    ${replace ? html`<p class="panel-lede">${`The label stays, so ${allowedOn.length ? `${allowedOn.join(', ')} keep${allowedOn.length === 1 ? 's' : ''} allowing it` : 'its node allowances stay'} and projects that use it keep it. Leads pick up the new token at their next start.`}</p>`
      : html`<div class="field">
      <span class="field-label" id="at-kind-label">Kind</span>
      <div class="seg seg-wide seg-2" role="radiogroup" aria-labelledby="at-kind-label">
        ${TOKEN_KINDS.map(([k, l, d]) => html`<label class=${'seg-opt' + (kind === k ? ' is-checked' : '')}>
          <input type="radio" name="at-kind" value=${k} checked=${kind === k} onChange=${() => {
            // A secret pasted for another kind never carries over, and the field may unmount.
            if (secret.current) secret.current.value = '';
            setHasSecret(false); setKind(k); setError(null);
          }} />
          <span class="seg-title">${l}</span><span class="seg-desc">${d}</span>
        </label>`)}
      </div>
    </div>`}
    <div class="form-grid form-grid-2">
      <${Field} id="at-label" label="Label" error=${shown('label')} hint=${replace ? 'Kept, so nodes and projects still find it.' : 'How nodes and projects refer to it.'}>
        <input id="at-label" class="input" value=${label} spellcheck=${false} placeholder=${kind === 'node-login' ? 'team-seat' : 'main-max'} readonly=${!!replace}
          aria-invalid=${!!shown('label')} aria-describedby="at-label-msg" onInput=${(e) => setLabel(e.target.value.trim())} />
      </${Field}>
      <${Field} id="at-account" label="Account" optional hint="Whose login this is, for your reference.">
        <input id="at-account" class="input" value=${account} placeholder=${kind === 'oauth' ? 'me (Max)' : ''} aria-describedby="at-account-msg" onInput=${(e) => setAccount(e.target.value)} />
      </${Field}>
      ${needsSecret && html`<div class="span-all"><${Field} id="at-secret" label="Setup token" error=${shown('secret')}
        hint="Run claude setup-token while logged into the account and paste the result. It goes to the Keychain; the hub shows only the last 4 characters.">
        <input id="at-secret" ref=${secret} type="password" class="input mono-input" autocomplete="new-password" spellcheck=${false}
          aria-invalid=${!!shown('secret')} aria-describedby="at-secret-msg" onInput=${(e) => setHasSecret(!!e.target.value.trim())} />
      </${Field}></div>`}
      ${kind === 'node-login' && html`<p class="span-all field-hint kind-note">Nothing secret is stored. Each node signs in on its own, with <span class="mono">claude login</span>, and the hub injects nothing.</p>`}
      ${!replace && html`<div class="span-all"><${Field} id="at-nodes" label="Allowed on nodes" optional hint="Adds the label to each node's allowed list. Change it later.">
        <${Combo} id="at-nodes" multiple options=${state.nodes.map((n) => ({ value: n.name, hint: n.ok ? n.ssh : offWord(n) }))}
          value=${nodes} onChange=${setNodes} placeholder="Search nodes" describedBy="at-nodes-msg" />
      </${Field}></div>`}
    </div>
    ${error && html`<div class="inline-error" role="alert"><b>${replace ? `Could not replace ${replace.label}.` : 'Could not add the token.'}</b> ${error}${needsSecret ? ' The secret field was cleared; paste it again.' : ''}</div>`}
    <div class="form-actions">
      <button class="btn btn-primary" type="submit" disabled=${busy}>${busy ? 'Saving…' : replace ? 'Replace token' : 'Add token'}</button>
      <button type="button" class="btn btn-quiet" onClick=${onClose} disabled=${busy}>Cancel</button>
    </div>
  </form>`;
}

function RenameTokenForm({ t, state, onDone, toast, refresh }) {
  const [label, setLabel] = useState(t.label);
  const [account, setAccount] = useState(t.account || '');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const ref = useRef();
  useEscape(onDone, ref);
  useEffect(() => { ref.current?.querySelector('input')?.select(); }, []);
  const err = LABEL_ERR(label, state, t.label);
  const save = async (e) => {
    e.preventDefault();
    if (err) return;
    setBusy(true); setError(null);
    try {
      const body = { label: t.label };
      if (label !== t.label) body.new_label = label;
      if (account !== (t.account || '')) body.account = account;
      await api.updateToken(body);
      await refresh();
      toast(label !== t.label ? `Renamed ${t.label} to ${label}` : `Saved ${label}`);
      onDone();
    } catch (x) { setError(x.message); setBusy(false); }
  };
  return html`<form class="inline-form" ref=${ref} onSubmit=${save} novalidate>
    <div class="form-grid">
      <${Field} id=${'rt-label-' + t.label} label="Label" error=${err} hint="Nodes and projects that use it are updated too.">
        <input id=${'rt-label-' + t.label} class="input" value=${label} spellcheck=${false} aria-invalid=${!!err}
          aria-describedby=${'rt-label-' + t.label + '-msg'} onInput=${(e) => setLabel(e.target.value.trim())} />
      </${Field}>
      <${Field} id=${'rt-acc-' + t.label} label="Account" optional>
        <input id=${'rt-acc-' + t.label} class="input" value=${account} onInput=${(e) => setAccount(e.target.value)} />
      </${Field}>
    </div>
    ${error && html`<div class="inline-error" role="alert">${error}</div>`}
    <div class="form-actions">
      <button class="btn btn-primary" type="submit" disabled=${busy || !!err}>${busy ? 'Saving…' : 'Save'}</button>
      <button type="button" class="btn btn-quiet" onClick=${onDone}>Cancel</button>
    </div>
  </form>`;
}

function TokenNodesForm({ t, state, onDone, toast, refresh }) {
  const [value, setValue] = useState(state.nodes.filter((n) => n.allowed_tokens?.includes(t.label)).map((n) => n.name));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const ref = useRef();
  useEscape(onDone, ref);
  useEffect(() => { ref.current?.querySelector('input')?.focus(); }, []);
  const save = async (e) => {
    e.preventDefault();
    setBusy(true); setError(null);
    try {
      await api.updateToken({ label: t.label, nodes: value });
      await refresh();
      toast(`${t.label} is allowed on ${value.length ? value.join(', ') : 'no nodes'}`);
      onDone();
    } catch (x) { setError(x.message); setBusy(false); }
  };
  return html`<form class="inline-form" ref=${ref} onSubmit=${save}>
    <${Field} id=${'tn-' + t.label} label=${`Nodes that allow ${t.label}`} hint="Projects already using it keep it until their next start.">
      <${Combo} id=${'tn-' + t.label} multiple options=${state.nodes.map((n) => ({ value: n.name, hint: n.ok ? n.ssh : offWord(n) }))}
        value=${value} onChange=${setValue} placeholder="Search nodes" describedBy=${'tn-' + t.label + '-msg'} />
    </${Field}>
    ${error && html`<div class="inline-error" role="alert">${error}</div>`}
    <div class="form-actions">
      <button class="btn btn-primary" type="submit" disabled=${busy}>${busy ? 'Saving…' : 'Save nodes'}</button>
      <button type="button" class="btn btn-quiet" onClick=${onDone}>Cancel</button>
    </div>
  </form>`;
}

// A token's recorded usage limits (a node-login token's, one per node), each with Clear, so rotation may pick it again.
function TokenLimits({ t, now, toast, refresh }) {
  const [busy, setBusy] = useState(null);
  const clear = async (node) => {
    setBusy(node || '-');
    try {
      const r = await api.clearLimit(t.label, node);
      await refresh();
      toast(r.cleared ? `Cleared the limit of ${t.label}${node ? ` on ${node}` : ''}` : `${t.label} had no recorded limit`);
    } catch (e) { toast(`Could not clear the limit: ${e.message}`, 'error'); } finally { setBusy(null); }
  };
  return (t.limits || []).map((l) => html`<div class="small dim" key=${l.node || '-'}>
    Limited until ${l.known ? '' : 'about '}${dayOf(l.until, now)}${hhmm(l.until)}${l.node ? ` on ${l.node}` : ''}
    <button type="button" class="linkbtn" onClick=${() => clear(l.node)} disabled=${!!busy}
      title="Forget this limit, so a lead may switch to this token again">${busy === (l.node || '-') ? 'Clearing…' : 'Clear'}</button></div>`);
}

function TokensPage({ state, now, toast, refresh }) {
  const [adding, setAdding] = useState(false);
  const [edit, setEdit] = useState(null);
  const [filter, setFilter] = useState('');
  const f = filter.trim().toLowerCase();
  const allowedOn = (label) => state.nodes.filter((n) => n.allowed_tokens?.includes(label)).map((n) => n.name);
  const tokens = (state.tokens || []).filter((t) => !f || [t.label, t.kind, t.account, ...allowedOn(t.label)].join(' ').toLowerCase().includes(f));
  // A delete the hub refused because a node that may run the token cannot be read now: {label, nodes}.
  const [unchecked, setUnchecked] = useState(null);
  const toggle = (label, mode) => { setUnchecked(null); setEdit((e) => (e?.label === label && e.mode === mode ? null : { label, mode })); };
  const close = () => { setEdit(null); setUnchecked(null); };
  const remove = async (label, force) => {
    try {
      await api.removeToken(label, force);
    } catch (e) {
      if (!force && e.data?.unchecked) { setUnchecked({ label, nodes: e.data.unchecked }); return; }
      throw e;
    }
    close(); await refresh(); toast(`Deleted ${label}`);
  };
  return html`<div class="page">
    <header class="page-head">
      <div><h1>Tokens</h1><p class="lede">Claude logins the nodes may use. Secrets live in the Keychain and reach a node only through ssh stdin.</p></div>
      <div class="tools">
        <input class="filter" type="search" placeholder="Filter tokens" aria-label="Filter tokens" data-filter value=${filter} onInput=${(e) => setFilter(e.target.value)} />
        <button class="btn btn-primary" onClick=${() => setAdding({})} disabled=${!!adding && !adding.replace}>Add token</button>
      </div>
    </header>
    ${adding && html`<${AddTokenForm} key=${adding.replace?.label || 'new'} state=${state} replace=${adding.replace} onClose=${() => setAdding(false)} toast=${toast} refresh=${refresh} />`}
    ${(state.tokens || []).length === 0 && !adding
      ? html`<div class="empty"><h2>No tokens yet</h2><p>Add an OAuth setup token or a node login, then allow it on the nodes whose leads should use it.</p></div>`
      : html`<div class="table-wrap"><table class="grid">
        <thead><tr><th>Label</th><th>Kind</th><th>Account</th><th>Last 4</th><th>Expires</th><th>Allowed on</th><th>In use by</th><th class="actions-col"><span class="sr">Actions</span></th></tr></thead>
        <tbody>
          ${tokens.map((t) => {
            const used = t.in_use || [];
            const open = edit?.label === t.label ? edit.mode : null;
            return html`
              <tr key=${t.label} class=${open ? 'is-editing' : ''}>
                <td class="nowrap"><span class="cell-main mono">${t.label}</span><${TokenLimits} t=${t} now=${now} toast=${toast} refresh=${refresh} /></td>
                <td>${KIND_LABEL[t.kind] ? KIND_LABEL[t.kind]
                  : html`<span class="dim kind-unsupported" title="This kind was removed; replace it with an OAuth token under the same label to keep its nodes and projects">Unsupported</span>`}</td>
                <td>${t.account || html`<span class="dim">none</span>`}</td>
                <td class="mono">${t.last4 ? html`<span class="dim">··</span>${t.last4}` : html`<span class="dim">none</span>`}</td>
                <td><${Expiry} iso=${t.expires} now=${now} /></td>
                <td><${Chips} items=${allowedOn(t.label)} /></td>
                <td>${used.length ? html`<ul class="plain">${used.map((k) => { const [n, p] = splitKey(k); return html`<li><a href=${projectHref(n, p)}>${p}</a> <span class="dim">on ${n}</span></li>`; })}</ul>`
                  : html`<span class="dim">nothing</span>`}</td>
                <td class="actions">
                  ${!KIND_LABEL[t.kind]
                    ? html`<button class="btn btn-sm" onClick=${() => setAdding({ replace: t })} title="Put an OAuth token under this label; its nodes and projects stay">Replace with OAuth</button>`
                    : t.kind === 'oauth' && html`<button class="btn btn-quiet btn-sm" onClick=${() => setAdding({ replace: t })} title="Rotate: a new OAuth token under the same label">Replace</button>`}
                  <button class="btn btn-quiet btn-sm" aria-expanded=${open === 'rename'} onClick=${() => toggle(t.label, 'rename')}>Rename</button>
                  <button class="btn btn-quiet btn-sm" aria-expanded=${open === 'nodes'} onClick=${() => toggle(t.label, 'nodes')}>Nodes</button>
                  <button class=${'btn btn-quiet btn-sm btn-danger-text' + (used.length ? ' is-blocked' : '')} aria-expanded=${open === 'remove'}
                    title=${used.length ? `In use by ${used.join(', ')}` : null} onClick=${() => toggle(t.label, 'remove')}>Delete</button>
                </td>
              </tr>
              ${open && html`<tr class="row-extra" key=${t.label + '-x'}><td colspan="8">
                ${open === 'rename' && html`<${RenameTokenForm} t=${t} state=${state} onDone=${close} toast=${toast} refresh=${refresh} />`}
                ${open === 'nodes' && html`<${TokenNodesForm} t=${t} state=${state} onDone=${close} toast=${toast} refresh=${refresh} />`}
                ${open === 'remove' && (used.length
                  ? html`<${Notice} onClose=${close}><b>${t.label} can't be deleted while it is in use</b> by ${used.join(', ')}. Stop those sessions, or switch the projects to another token, first.</${Notice}>`
                  : unchecked?.label === t.label
                  ? html`<${ConfirmBar} key="force" message=${html`<b>The hub cannot check ${unchecked.nodes.join(', ')}</b> for a lead running on ${t.label}: unreachable now, or its tmux did not answer. Delete <b>${t.label}</b> anyway? A lead already running on it there keeps running; it cannot start on it again.`}
                      confirmLabel="Delete anyway" busyLabel="Deleting…" onCancel=${close} onConfirm=${() => remove(t.label, true)} />`
                  : html`<${ConfirmBar} message=${html`Delete <b>${t.label}</b>? ${t.kind === 'node-login' ? '' : 'Its Keychain entry is removed and '}${allowedOn(t.label).length ? `it is dropped from ${allowedOn(t.label).join(', ')}.` : 'no node allows it.'} Projects that picked it fall back to their node's first token.`}
                      confirmLabel="Delete token" busyLabel="Deleting…" onCancel=${close} onConfirm=${() => remove(t.label, false)} />`)}
              </td></tr>`}`;
          })}
          ${tokens.length === 0 && html`<tr><td colspan="8" class="dim">No tokens match “${filter}”.</td></tr>`}
        </tbody>
      </table></div>`}
  </div>`;
}

/* ---------- settings ---------- */

// Settings: notifications through ntfy, and how to keep the hub running. The topic is a secret: it is shown here only,
// fetched from GET /api/notify, and never put in a toast, a title or the state the other pages read.
function SettingsPage({ state, now, toast, refresh }) {
  const [n, setN] = useState(null);
  const [loadError, setLoadError] = useState(null);
  const [server, setServer] = useState('');
  const [busy, setBusy] = useState(null);
  const [error, setError] = useState(null);
  const [testError, setTestError] = useState(null);
  // The server last read from the hub: a reload replaces the field only while it still shows that value.
  const savedServer = useRef('');
  const load = useCallback(async () => {
    try {
      const r = await api.notify();
      const prev = savedServer.current;
      savedServer.current = r.server || '';
      setN(r); setServer((s) => (s === prev ? r.server || '' : s)); setLoadError(null);
    } catch (e) { setLoadError(e.message); }
  }, []);
  useEffect(() => { load(); }, [load]);
  const save = async (body, what) => {
    setBusy(what); setError(null);
    try {
      const r = await api.notifySetup(body);
      // On/off sends no server, so a server being typed stays in the field; a save takes the hub's.
      const prev = savedServer.current;
      setN(r); setServer((s) => (what === 'toggle' && s !== prev ? s : r.server || s));
      savedServer.current = r.server || prev;
      toast(what === 'setup' ? 'Notifications are set up; subscribe on your phone with the server and topic shown here'
        : what === 'toggle' ? `Notifications ${r.enabled ? 'on' : 'off'}` : 'Server saved');
      refresh();
    } catch (e) { setError(e.message); } finally { setBusy(null); }
  };
  const test = async () => {
    setBusy('test'); setTestError(null);
    try {
      await api.notifyTest();
      toast('Test sent: "Cairn: test" should reach your phone');
      await load(); refresh();
    } catch (e) { setTestError(e.message); await load(); refresh(); } finally { setBusy(null); }
  };
  const copyTopic = async () => {
    try { await navigator.clipboard.writeText(n.topic); toast('Topic copied'); }
    catch { toast('Copy failed: select the topic and copy it by hand', 'error'); }
  };
  const serverChanged = n && server.trim() && server.trim().replace(/\/$/, '') !== n.server;
  const serverErr = server.trim() && !/^https?:\/\/\S+$/.test(server.trim()) ? 'Use a URL such as https://ntfy.sh.' : null;
  return html`<div class="page page-form settings">
    <header class="page-head"><div>
      <h1>Settings</h1>
      <p class="lede">Notifications on your phone, and keeping the hub running.</p>
    </div></header>
    <section class="wz-sec">
      <div class="wz-head"><h2>Notifications</h2><p>A push through ntfy for each new question, each result ready and each lead that needs attention, once each.</p></div>
      <div class="wz-body">
        ${loadError ? html`<div class="inline-error" role="alert"><b>Could not read the notification settings.</b> ${loadError}</div>`
          : !n ? html`<div class="skeleton" aria-label="Loading"><span></span><span></span></div>`
          : html`
          <${Field} id="st-server" label="ntfy server" error=${serverErr} hint="The ntfy server to push through (https://ntfy.sh or your own); your phone subscribes to the same one.">
            <div class="st-row">
              <input id="st-server" class="input mono-input st-server" value=${server} spellcheck=${false} placeholder="https://ntfy.sh"
                aria-invalid=${!!serverErr} aria-describedby="st-server-msg" onInput=${(e) => setServer(e.target.value)} />
              ${n.configured && html`<button class="btn" disabled=${!serverChanged || !!serverErr || !!busy} onClick=${() => save({ server: server.trim() }, 'server')}>${busy === 'server' ? 'Saving…' : 'Save server'}</button>`}
            </div>
          </${Field}>
          ${!n.configured ? html`<div class="st-setup">
              <p class="muted-note">Not set up yet. Give the server, then Set up creates a private topic for this hub and keeps it in the Keychain.</p>
              <button class="btn btn-primary" disabled=${!!busy || !!serverErr || !server.trim()} title=${server.trim() ? null : 'Give the ntfy server first'}
                onClick=${() => save({ server: server.trim(), enabled: true }, 'setup')}>${busy === 'setup' ? 'Setting up…' : 'Set up'}</button>
            </div>`
          : html`
            <div class="field">
              <label class="switch">
                <input type="checkbox" role="switch" checked=${n.enabled} disabled=${!!busy} onChange=${(e) => save({ enabled: e.target.checked }, 'toggle')} />
                <span class="switch-track" aria-hidden="true"></span>
                <span>Send notifications</span>
              </label>
              <p class="field-hint">${n.enabled ? 'On: new items are pushed on the next sync.' : 'Off: nothing is sent or recorded while it is off.'}</p>
            </div>
            <div class="st-subscribe">
              <p>Subscribe on your phone: server <span class="mono">${n.server}</span>, topic <span class="mono st-topic">${n.topic}</span></p>
              <button class="btn btn-sm" onClick=${copyTopic}>Copy topic</button>
            </div>
            <div class="st-status">
              <button class="btn" disabled=${!!busy} onClick=${test}>${busy === 'test' ? 'Sending…' : 'Send test'}</button>
              <span class="dim small">${n.last_sent_at ? `Last sent ${ago(n.last_sent_at, now)}` : 'Nothing sent yet'}</span>
            </div>
            ${(testError || n.last_error) && html`<div class="inline-error" role="alert"><b>The last notification failed.</b> ${testError || n.last_error}</div>`}`}
          ${error && html`<div class="inline-error" role="alert">${error}</div>`}`}
      </div>
    </section>
    <section class="wz-sec">
      <div class="wz-head"><h2>Service</h2><p>Keep the hub running after a reboot or a crash.</p></div>
      <div class="wz-body">
        <p class="dim">Run <span class="mono">cairn service install</span> once in a Terminal: a LaunchAgent starts the hub at login and restarts it if it stops (logs in <span class="mono">~/.cairn/serve.log</span>).</p>
      </div>
    </section>
  </div>`;
}

/* ---------- project page ---------- */

const TYPES = ['research', 'prototype', 'grilling', 'task', 'build'];

function ticketOptions(p, exclude) {
  return p.tickets.filter((t) => t.id !== exclude && t.status === 'open')
    .map((t) => ({ value: t.id, label: `${t.id}  ${t.title}`, chip: `${t.id} ${t.title}`, hint: t.state }));
}

function NewTicketForm({ node, p, onClose, toast, refresh }) {
  const [title, setTitle] = useState('');
  const [type, setType] = useState('task');
  const [question, setQuestion] = useState('');
  const [blockers, setBlockers] = useState([]);
  const [tried, setTried] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const ref = useRef();
  useEscape(onClose, ref);
  useEffect(() => { ref.current?.querySelector('input')?.focus(); }, []);
  const titleErr = !title.trim() ? 'Give the ticket a title.' : null;
  const submit = async (e) => {
    e.preventDefault();
    setTried(true);
    if (titleErr) { firstInvalid(ref.current); return; }
    setBusy(true); setError(null);
    try {
      const r = await api.ticket(node.name, p.name, 'create', { title: title.trim(), type, question: question.trim(), blocked_by: blockers });
      await refresh();
      toast(`Ticket ${r.id} created and committed`);
      onClose();
    } catch (x) { setError(x.message); setBusy(false); }
  };
  return html`<form class="panel" ref=${ref} onSubmit=${submit} novalidate aria-label="New ticket">
    <header class="panel-head"><h2>New ticket</h2>
      <button type="button" class="btn btn-quiet btn-sm" onClick=${onClose}>Close <kbd>Esc</kbd></button></header>
    <div class="form-grid form-grid-ticket">
      <div class="span-2"><${Field} id="nt-title" label="Title" error=${tried ? titleErr : null}>
        <input id="nt-title" class="input" value=${title} aria-invalid=${!!(tried && titleErr)} aria-describedby=${msgId('nt-title', tried && titleErr)}
          placeholder="Measure cold-cache pan latency" onInput=${(e) => setTitle(e.target.value)} />
      </${Field}></div>
      <${Field} id="nt-type" label="Type">
        <select id="nt-type" class="input input-select" value=${type} onChange=${(e) => setType(e.target.value)}>
          ${TYPES.map((ty) => html`<option value=${ty}>${ty}</option>`)}
        </select>
      </${Field}>
      <div class="span-all"><${Field} id="nt-q" label="Question" optional hint="What the ticket answers or delivers. Becomes its ## Question section.">
        <textarea id="nt-q" class="input" rows="3" value=${question} aria-describedby="nt-q-msg" onInput=${(e) => setQuestion(e.target.value)}></textarea>
      </${Field}></div>
      <div class="span-all"><${Field} id="nt-blockers" label="Blocked by" optional hint="Open tickets that must close first.">
        <${Combo} id="nt-blockers" multiple options=${ticketOptions(p)} value=${blockers} onChange=${setBlockers}
          placeholder="Search tickets by name or id" describedBy="nt-blockers-msg" emptyText="No open ticket matches" />
      </${Field}></div>
    </div>
    ${error && html`<div class="inline-error" role="alert"><b>Could not create the ticket.</b> ${error}</div>`}
    <div class="form-actions">
      <button class="btn btn-primary" type="submit" disabled=${busy}>${busy ? 'Creating…' : 'Create ticket'}</button>
      <button type="button" class="btn btn-quiet" onClick=${onClose} disabled=${busy}>Cancel</button>
      <span class="dim small">Commits one file on ${node.name}; the lead picks it up from the frontier.</span>
    </div>
  </form>`;
}

// The tokens the lead moves through when one hits a usage limit, in the order picked, each with its limit.
function TokenPool({ state, node, p, entry, now, toast, refresh }) {
  const pool = entry.tokens?.length ? entry.tokens : [tokenOf(state, node, p)].filter(Boolean);
  const limits = entry.limits || {};
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(pool);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const allowed = node.allowed_tokens || [];
  const save = async (e) => {
    e.preventDefault();
    setBusy(true); setError(null);
    try {
      await api.projectTokens(node.name, p.name, value);
      await refresh();
      toast(`Token pool of ${p.name} saved`);
      setEditing(false);
    } catch (x) { setError(x.message); } finally { setBusy(false); }
  };
  if (editing) return html`<form class="inline-form" onSubmit=${save}>
    <label class="sr" for="pf-pool">Token pool, in the order the lead moves through it</label>
    <${Combo} id="pf-pool" multiple options=${tokenOptions(state).filter((o) => allowed.includes(o.value))} value=${value} onChange=${setValue} placeholder="Search tokens" />
    ${error && html`<div class="inline-error" role="alert">${error}</div>`}
    <div class="form-actions">
      <button class="btn btn-primary btn-sm" type="submit" disabled=${busy}>${busy ? 'Saving…' : 'Save pool'}</button>
      <button type="button" class="btn btn-quiet btn-sm" onClick=${() => setEditing(false)}>Cancel</button>
    </div>
  </form>`;
  return html`<span class="chips">${pool.map((l) => html`<span class="chip chip-static">${l}${limits[l]
      ? html` <span class="dim" title=${limits[l].known ? 'The reset time Claude Code showed' : 'Claude Code showed no reset time; the hub waits 5 hours'}>· limited until ${limits[l].known ? '' : 'about '}${dayOf(limits[l].until, now)}${hhmm(limits[l].until)}</span>` : ''}</span>`)}</span>
    <button class="btn btn-sm btn-quiet" onClick=${() => { setValue(pool); setEditing(true); }} disabled=${!node.ok}>Edit</button>
    <span class="dim small details-full">On a usage limit the lead moves to the next token in this order that is not limited.</span>`;
}

// Details: the long paths and settings that used to crowd the header.
function ProjectDetails({ state, node, p, now, toast, refresh }) {
  const entry = projectEntry(state, node.name, p.name);
  const allowed = node.allowed_tokens || [];
  const current = tokenOf(state, node, p) || '';
  const opts = current && !allowed.includes(current) ? [current, ...allowed] : allowed;
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const change = async (e) => {
    const v = e.target.value;
    setBusy(true); setError(null);
    try {
      await api.projectToken(node.name, p.name, v);
      await refresh();
      toast(`${p.name} uses ${v} from its next start`);
    } catch (x) { setError(x.message); e.target.value = current; } finally { setBusy(false); }
  };
  const live = p.lead === 'alive' || p.review === 'alive';
  // Both models are sent together; the one not changed keeps its saved value.
  const [saving, setSaving] = useState(false);
  const [modelError, setModelError] = useState(null);
  const saveModels = async (change) => {
    const next = { model: entry.model ?? null, subagent_model: entry.subagent_model ?? null, ...change };
    setSaving(true); setModelError(null);
    try {
      await api.projectModels(node.name, p.name, next.model, next.subagent_model);
      await refresh();
      toast(`${p.name}: lead ${next.model || 'default'}, subagents ${next.subagent_model || 'default'}${p.lead === 'alive' ? ', from the next start' : ''}`);
    } catch (x) { setModelError(x.message); } finally { setSaving(false); }
  };
  const [leadBusy, setLeadBusy] = useState(false);
  const [leadError, setLeadError] = useState(null);
  const updateLead = async () => {
    setLeadBusy(true); setLeadError(null);
    try {
      const r = await api.leadUpdate(node.name, p.name);
      await refresh();
      const dropped = r.dropped || [];
      toast(`LEAD.md of ${p.name} is now v${r.version}`, 'ok', `committed ${String(r.commit).slice(0, 7)} · read at the next start${dropped.length ? ` · dropped sections: ${dropped.join(', ')} (still in git history)` : ''}`);
    } catch (x) { setLeadError(x.message); } finally { setLeadBusy(false); }
  };
  return html`<dl class="details" id="pf-details">
    <dt>Repo</dt><dd class="mono">${p.path}</dd>
    <dt>Node</dt><dd><span class="mono">${sshTarget(node)}</span></dd>
    <dt><label for="pf-token">Token</label></dt><dd>
      <select id="pf-token" class="input input-sm input-select" value=${current} onChange=${change} disabled=${busy || !opts.length || !node.ok}>
        ${!opts.length && html`<option value="">no tokens allowed on ${node.name}</option>`}
        ${opts.map((l) => {
          const t = state.tokens.find((x) => x.label === l);
          return html`<option value=${l}>${l}${t ? ` · ${kindLabel(t.kind)}` : ''}${allowed.includes(l) ? '' : ' (no longer allowed)'}</option>`;
        })}
      </select>
      ${busy ? html`<span class="dim small">Saving…</span>` : live ? html`<span class="dim small">A change applies at the next start.</span>` : null}
      ${error && html`<span class="field-error">${error}</span>`}
    </dd>
    <dt>Token pool</dt><dd><${TokenPool} state=${state} node=${node} p=${p} entry=${entry} now=${now} toast=${toast} refresh=${refresh} /></dd>
    <dt><label for="pf-model">Lead model</label></dt><dd>
      <${ModelSelect} id="pf-model" small value=${entry.model} disabled=${saving || !node.ok} onChange=${(v) => saveModels({ model: v })} />
      ${p.lead === 'alive' && html`<span class="dim small">Used at the next start</span>`}
    </dd>
    <dt><label for="pf-submodel">Subagent model</label></dt><dd>
      <${ModelSelect} id="pf-submodel" small value=${entry.subagent_model} disabled=${saving || !node.ok} onChange=${(v) => saveModels({ subagent_model: v })} />
      ${saving ? html`<span class="dim small">Saving…</span>` : p.lead === 'alive' ? html`<span class="dim small">Used at the next start</span>` : null}
      ${modelError && html`<span class="field-error">${modelError}</span>`}
      ${node.update_available && html`<span class="dim small details-full">This node runs an older cairn-node; update it on the <a href="#/nodes">Nodes page</a> before using models</span>`}
    </dd>
    ${(p.lead_version || 1) < (node.snapshot?.lead_template_version || 0) && html`<dt>LEAD.md</dt><dd>
      <span class="dim small">LEAD.md is from an older template (v${p.lead_version || 1}, now v${node.snapshot.lead_template_version}).</span>
      <button class="btn btn-sm" onClick=${updateLead} disabled=${leadBusy || !node.ok}>${leadBusy ? 'Updating…' : 'Update LEAD.md'}</button>
      ${leadError && html`<span class="field-error">${leadError}</span>`}
      <span class="dim small details-full">It keeps the destination and notes, commits only LEAD.md, and the lead reads it at its next start.</span>
    </dd>`}
    <dt>Mirror</dt><dd><${MirrorState} m=${entry.mirror} head=${p.commits[0]?.sha} now=${now} long /></dd>
  </dl>`;
}

// Ticket list grouped by what it needs: Waiting on you, Claimed, Frontier, Blocked, then Closed folded.
const TICKET_GROUPS = [['waiting', 'Waiting on you'], ['claimed', 'Claimed'], ['frontier', 'Frontier'], ['blocked', 'Blocked']];

function ticketMeta(t, p) {
  const qs = openQs(p).filter((x) => x.ticket === t.id);
  const gate = qs.find((x) => x.gate);
  if (t.state === 'waiting' && gate) return { text: `gate ${gate.id}`, gate: true };
  if (t.state === 'waiting') return { text: [qs.map((x) => x.id).join(', '), t.assignee].filter(Boolean).join(' · ') };
  if (t.state === 'claimed') return { text: t.assignee };
  if (t.state === 'blocked') return { text: `after ${t.blocked_by.join(', ')}` };
  return { text: '' };
}

function TicketRow({ t, p, onOpen }) {
  const m = ticketMeta(t, p);
  const titleOf = (id) => p.tickets.find((x) => x.id === id)?.title;
  return html`<li><button class="tr" data-nav onClick=${onOpen}
      title=${t.state === 'blocked' ? t.blocked_by.map((id) => `${id} ${titleOf(id) || ''}`).join('\n') : null}>
    <span class="tr-id">${t.id}</span><span class="tr-type">${t.type}</span><span class="tr-title">${t.title}</span>
    <span class=${'tr-meta' + (m.gate ? ' is-gate' : '')}>${m.text}</span><span class="tr-open">↵ open</span>
  </button></li>`;
}

function TicketList({ node, p, openDrawer, toast, refresh }) {
  const [types, setTypes] = useState([]);
  const [creating, setCreating] = useState(false);
  const [text, setText] = useState('');
  const [showClosed, setShowClosed] = useState(false);
  const f = text.trim().toLowerCase();
  const shown = p.tickets.filter((t) => (!types.length || types.includes(t.type)) &&
    (!f || `${t.id} ${t.title} ${t.assignee || ''}`.toLowerCase().includes(f)));
  const typeCounts = [...new Set([...TYPES, ...p.tickets.map((t) => t.type)])]
    .map((ty) => [ty, p.tickets.filter((t) => t.type === ty).length]);
  const open = (t) => () => openDrawer({ node: node.name, project: p.name, path: t.path, kind: 'ticket', ticket: t });
  const closed = shown.filter((t) => t.state === 'closed');
  return html`
    <div class="tl-toolbar">
      <label class="filter-box filter-box-sm">
        <input type="search" placeholder="Filter tickets" aria-label="Filter tickets" data-filter value=${text} onInput=${(e) => setText(e.target.value)} />
        <kbd aria-hidden="true">/</kbd>
      </label>
      <${MultiSelect} label="Type" options=${typeCounts} value=${types} onChange=${setTypes} />
      <span class="spacer"></span>
      <button class="linkbtn" onClick=${() => setCreating(true)} disabled=${creating || !node.ok}
        title=${node.ok ? null : `${node.name} is ${offWord(node)}`}>+ New ticket</button>
    </div>
    ${creating && html`<${NewTicketForm} node=${node} p=${p} onClose=${() => setCreating(false)} toast=${toast} refresh=${refresh} />`}
    ${TICKET_GROUPS.map(([key, label]) => {
      const items = shown.filter((t) => t.state === key);
      return items.length > 0 && html`<section class="tg" key=${key}>
        <h3 class=${'tg-head' + (key === 'waiting' ? ' is-wait' : '')}>${label} <span>${items.length}</span></h3>
        <ul class="tg-rows">${items.map((t) => html`<${TicketRow} key=${t.id} t=${t} p=${p} onOpen=${open(t)} />`)}</ul>
      </section>`;
    })}
    ${closed.length > 0 && html`<section class="tg">
      <button class="tg-closed" aria-expanded=${showClosed} onClick=${() => setShowClosed((v) => !v)}>Closed ${closed.length} ${showClosed ? '▾' : '▸'}</button>
      ${showClosed && html`<ul class="tg-rows" style="margin-top: 6px">${closed.map((t) => html`<${TicketRow} key=${t.id} t=${t} p=${p} onOpen=${open(t)} />`)}</ul>`}
    </section>`}
    ${shown.length === 0 && html`<p class="tl-empty">${p.tickets.length ? 'No tickets match the filter.' : 'No tickets yet.'}</p>`}`;
}

function QuestionHistory({ node, p, now, openDrawer }) {
  const list = [...p.questions].sort((a, b) => Number(a.answered) - Number(b.answered) ||
    Date.parse(b.asked_at) - Date.parse(a.asked_at));
  return html`<section class="block">
    <h2 class="label">Questions <span class="dim">${openQs(p).length} open, ${p.questions.length - openQs(p).length} answered</span></h2>
    ${list.length ? html`<ul class="rows">
      ${list.map((q) => html`<li>
        <button class="row" data-nav onClick=${() => openDrawer({ node: node.name, project: p.name, path: q.path, kind: 'question', q })}>
          <span class="mono row-id">${q.id}</span>
          <span class="row-main">
            <span class="row-title">${q.context}</span>
            <span class="row-sub">
              ${q.answered ? html`<span class="badge badge-done">Answered ${q.choice}</span>` : html`<span class="badge badge-open">Open</span>`}
              ${q.gate && html`<${GateBadge} />`}
              ${q.ticket && html`<span>ticket <span class="mono">${q.ticket}</span></span>`}
              <${QuestionAge} q=${q} now=${now} />
            </span>
          </span>
        </button>
      </li>`)}
    </ul>` : html`<p class="muted-note">No questions asked yet.</p>`}
  </section>`;
}

function AdrList({ node, p, openDrawer }) {
  return html`<section class="block">
    <h2 class="label">Decision records <span class="dim">${p.adrs.length}</span></h2>
    ${p.adrs.length ? html`<ul class="rows">
      ${p.adrs.map((a) => html`<li><button class="row" data-nav onClick=${() => openDrawer({ node: node.name, project: p.name, path: a.path, kind: 'adr' })}>
        <span class="mono row-id">${a.id}</span><span class="row-main"><span class="row-title">${a.title}</span></span>
      </button></li>`)}
    </ul>` : html`<p class="muted-note">No ADRs yet.</p>`}
  </section>`;
}

function Commits({ p, now }) {
  return html`<section class="block">
    <h2 class="label">Last commits</h2>
    ${p.commits.length ? html`<ul class="commits">
      ${p.commits.map((c) => html`<li>
        <span class="mono dim">${String(c.sha).slice(0, 7)}</span>
        <span class="commit-subject">${c.subject}</span>
        <span class="dim commit-date" title=${clock(c.date)}>${ago(c.date, now)}</span>
      </li>`)}
    </ul>` : html`<p class="muted-note">No commits yet.</p>`}
  </section>`;
}

// The lead's tmux pane, read when the lead runs (or on Refresh).
function usePane(node, p) {
  const [pane, setPane] = useState({ loading: false, lines: null, error: null, at: null });
  const load = useCallback(async () => {
    setPane((s) => ({ ...s, loading: true }));
    try {
      const r = await api.pane(node.name, p.name);
      setPane({ loading: false, lines: r.lines || [], error: null, at: Date.now() });
    } catch (e) {
      setPane({ loading: false, lines: null, error: e.message, at: Date.now() });
    }
  }, [node.name, p.name]);
  // Read again whenever the snapshot's last pane line moves, so the rail agrees with the lead bar.
  useEffect(() => { if (p.lead === 'alive') load(); else setPane({ loading: false, lines: null, error: null, at: null }); }, [load, p.lead, p.pane_last]);
  return { ...pane, load };
}
const paneTime = (at) => (at ? new Date(at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '');

function PaneBlock({ pane, p, tail }) {
  if (pane.error) return html`<div class="inline-error">Could not read the pane: ${pane.error}</div>`;
  if (!pane.lines) return html`<p class="muted-note">${p.lead === 'alive' ? 'Loading…' : 'The lead is not running, so there is no pane. Refresh reads it anyway.'}</p>`;
  const lines = tail ? pane.lines.slice(-tail) : pane.lines;
  return html`<pre class=${'pane-dark' + (tail ? '' : ' pane-full')}>${lines.length ? lines.join('\n') : '(empty pane)'}</pre>`;
}

function Rail({ node, p, pane, openDrawer }) {
  const onMd = mdLinkHandler('work/map.md', (path) => openDrawer({ node: node.name, project: p.name, path, kind: path.includes('/adr/') ? 'adr' : 'ticket' }));
  return html`<aside class="rail" aria-label="Map and lead pane">
    <section>
      <h2 class="label">Destination</h2>
      <p class="destination">${p.map?.destination || html`<span class="dim">work/map.md has no destination.</span>`}</p>
    </section>
    <section>
      <h2 class="label">Decisions so far</h2>
      ${p.map?.decisions?.length
        ? html`<ul class="decisions" onClick=${onMd}>${p.map.decisions.map((d) => html`<li dangerouslySetInnerHTML=${{ __html: mdInline(d.replace(/^\s*[-*]\s+/, '')) }}></li>`)}</ul>`
        : html`<p class="muted-note">None yet.</p>`}
    </section>
    <section class="rail-pane">
      <h2 class="label">Lead pane <span class="dim">${paneTime(pane.at)}</span></h2>
      <${PaneBlock} pane=${pane} p=${p} tail=${14} />
    </section>
  </aside>`;
}

// The lead bar: state, what it works on, the pane's latest line, and only the actions that apply.
function LeadBar({ state, node, p, toast, onChanged, openChat, openDrawer, now }) {
  const [busy, setBusy] = useState(false);
  const [startError, setStartError] = useState(null);
  const att = attentionOf(state, node, p);
  const [confirmStop, setConfirmStop] = useState(false);
  const working = (p.status || '').startsWith('working') ? p.status.split(/\s+/)[1] : null;
  const workingOn = working && p.tickets.find((t) => t.id === working);
  const alive = p.lead === 'alive';
  const v = leadView(p);
  const stateWord = alive ? 'Running' : p.lead === 'unknown' ? 'Unknown' : p.lead === 'none' ? 'Never started' : p.status === 'done' ? 'Done' : p.status === 'idle' ? 'Idle' : 'Not running';
  const stop = async () => {
    const r = await api.stop(node.name, p.name);
    setConfirmStop(false);
    toast(r.stopped ? `Lead for ${p.name} stopped` : `Lead for ${p.name} was not running`);
    onChanged();
  };
  const start = async () => {
    setBusy(true); setStartError(null);
    try {
      const r = await api.start(node.name, p.name);
      toast(r.resumed ? `Lead for ${p.name} resumed` : `Lead for ${p.name} started`);
      onChanged();
    } catch (e) {
      setStartError(e.message);
    } finally { setBusy(false); }
  };
  return html`<div class=${'leadbar' + (att ? ' is-attention' : alive ? ' is-live' : '')}>
    <${Dot} tone=${{ ok: 'ok', idle: 'idle', done: 'off', bad: 'bad', none: 'off', unknown: 'off' }[v.dot]} label=${'Lead ' + stateWord.toLowerCase()} />
    <span class="leadbar-state">${stateWord}</span>
    <span class="leadbar-what">${working ? html`ticket <span class="mono">${working}</span>${workingOn ? ` ${workingOn.title}` : ''}` : statusLine(p)}</span>
    ${!att && p.pane_last && html`<span class="leadbar-pane" title=${p.pane_last}>${p.pane_last}</span>`}
    ${att && html`<span class="leadbar-att"><${AttentionText} a=${att} now=${now || Date.now()} /></span>`}
    ${p.review === 'alive' && html`<span class="badge badge-open" title="A review session from the CLI runs in tmux">Review session open</span>`}
    <span class="leadbar-actions">
      ${att?.kind === 'dialog' && html`<${DialogButton} node=${node} p=${p} openDrawer=${openDrawer} primary />`}
      ${att && html`<${RestartButton} node=${node} p=${p} toast=${toast} refresh=${onChanged} primary=${att.kind !== 'dialog'} />`}
      ${alive
        ? html`<button class="btn" aria-expanded=${confirmStop} onClick=${() => setConfirmStop((x) => !x)}>Stop</button>`
        : html`<button class="btn" onClick=${start} disabled=${busy || !node.ok}>${busy ? 'Starting…' : 'Start'}</button>
          ${startError && html`<span class="field-error att-err" role="alert">Start failed: ${startError}</span>`}`}
      <${ReviewButton} state=${state} node=${node} p=${p} openChat=${openChat} primary=${!att} count=${openQs(p).length} showKey />
    </span>
    ${confirmStop && alive && html`<${ConfirmBar}
      message=${working
        ? html`Stop the lead while it works on <span class="mono">${working}</span>${workingOn ? html` ${workingOn.title}` : ''}? The next Start resumes its conversation, but the step in flight is lost.`
        : html`Stop the lead? Its tmux session is killed. The next Start resumes its conversation.`}
      confirmLabel="Stop lead" busyLabel="Stopping…" onCancel=${() => setConfirmStop(false)} onConfirm=${stop} />`}
  </div>`;
}

const PROJECT_TABS = [['board', 'Board'], ['map', 'Map'], ['questions', 'Questions'], ['commits', 'Commits'], ['pane', 'Pane'], ['usage', 'Usage']];

const num = (n) => (n == null ? '—' : Number(n).toLocaleString('en-US'));
const minutes = (m) => (m < 60 ? `${m} min` : `${Math.floor(m / 60)} h${m % 60 ? ` ${m % 60} min` : ''}`);

// Usage: token counts per model from the project's transcripts, read when the tab opens.
function UsageTab({ node, p, now }) {
  const [u, setU] = useState({ loading: true });
  // Only the answer for the project still open is kept; switching projects while it loads drops the old one.
  const want = useRef('');
  want.current = `${node.name}/${p.name}`;
  const load = useCallback(async () => {
    const key = `${node.name}/${p.name}`;
    setU((x) => ({ ...x, loading: true, error: null }));
    try {
      const data = await api.usage(node.name, p.name);
      if (want.current === key) setU({ data, at: Date.now() });
    } catch (e) { if (want.current === key) setU({ error: e.message }); }
  }, [node.name, p.name]);
  useEffect(() => { load(); }, [load]);
  const d = u.data;
  const cols = [['input', 'Input'], ['output', 'Output'], ['cache_read', 'Cache read'], ['cache_write', 'Cache write'], ['messages', 'Messages']];
  const total = (k) => (d?.models || []).reduce((a, m) => a + (m[k] || 0), 0);
  return html`<section class="block">
    <div class="block-head">
      <h2 class="label">Usage <span class="dim">tokens per model, lead, review chats and subagents</span></h2>
      <div class="tools">
        ${u.at && html`<span class="dim small">read ${ago(new Date(u.at).toISOString(), now)}</span>`}
        <button class="btn btn-sm" onClick=${load} disabled=${u.loading}>${u.loading ? 'Reading…' : 'Refresh'}</button>
      </div>
    </div>
    ${u.error ? html`<div class="inline-error" role="alert"><b>Could not read the usage.</b> ${u.error}</div>`
      : !d ? html`<div class="skeleton" aria-label="Loading"><span></span><span></span><span></span></div>`
      : !d.models.length ? html`<p class="muted-note">No transcripts yet for ${p.name}.</p>`
      : html`<div class="table-wrap"><table class="grid usage">
          <thead><tr><th>Model</th>${cols.map(([, l]) => html`<th class="num">${l}</th>`)}</tr></thead>
          <tbody>
            ${d.models.map((m) => html`<tr><td class="mono">${m.model}</td>${cols.map(([k]) => html`<td class="num">${num(m[k])}</td>`)}</tr>`)}
            <tr class="usage-total"><td>Total</td>${cols.map(([k]) => html`<td class="num">${num(total(k))}</td>`)}</tr>
          </tbody>
        </table></div>
        <dl class="usage-facts">
          <div><dt>Sessions</dt><dd>${num(d.sessions)}</dd></div>
          <div><dt>Active time</dt><dd>${minutes(d.active_minutes || 0)}</dd></div>
          <div><dt>First activity</dt><dd>${d.first_at ? clock(d.first_at) : '—'}</dd></div>
          <div><dt>Last activity</dt><dd>${d.last_at ? html`${clock(d.last_at)} <span class="dim">(${ago(d.last_at, now)})</span>` : '—'}</dd></div>
          <div><dt>Transcripts</dt><dd>${num(d.files)}</dd></div>
        </dl>
        <p class="muted-note">Tokens only: seats are subscriptions, so no cost is shown.</p>`}
  </section>`;
}

function ProjectPage({ state, route, now, openDrawer, openChat, toast, refresh }) {
  const [tab, setTab] = useState('board');
  const [details, setDetails] = useState(false);
  const node = state.nodes.find((n) => n.name === route.node);
  const p = node?.snapshot?.projects.find((x) => x.name === route.project);
  const pane = usePane(node || { name: route.node }, p || { name: route.project, lead: 'none' });
  if (!node) return html`<${NotFound} msg=${`No node named ${route.node}.`} />`;
  if (node.stopped && !node.snapshot) {
    return html`<div class="page"><div class="banner banner-quiet">
      <b>${node.name} is stopped.</b> Start it on the <a href="#/nodes">Nodes</a> page; the project page needs a snapshot from the node.
    </div></div>`;
  }
  if (!node.ok && !node.snapshot) {
    return html`<div class="page"><div class="banner banner-error">
      <b>${node.name} is unreachable.</b> ${node.error || ''} Last attempt ${ago(node.synced_at, now)}. The project page needs a snapshot from the node.
    </div></div>`;
  }
  if (!p) return html`<${NotFound} msg=${`No project ${route.project} on ${node.name}.`} />`;
  const open = openQs(p).length;
  const onMd = mdLinkHandler('work/map.md', (path) => openDrawer({ node: node.name, project: p.name, path, kind: path.includes('/adr/') ? 'adr' : 'ticket' }));
  // Arrow keys, Home and End move between the tabs and show the one reached.
  const onTabKey = (e) => {
    const i = PROJECT_TABS.findIndex(([k]) => k === tab);
    const n = PROJECT_TABS.length;
    const j = { ArrowRight: (i + 1) % n, ArrowLeft: (i - 1 + n) % n, Home: 0, End: n - 1 }[e.key];
    if (j == null) return;
    e.preventDefault();
    setTab(PROJECT_TABS[j][0]);
    document.getElementById('tab-' + PROJECT_TABS[j][0])?.focus();
  };
  return html`<div class="proj">
    ${node.stopped && html`<div class="banner banner-quiet" style="margin: 16px 28px 0"><b>${node.name} is stopped.</b> Showing the last snapshot. Start it on the <a href="#/nodes">Nodes</a> page.</div>`}
    ${isDown(node) && html`<div class="banner banner-error" style="margin: 16px 28px 0"><b>${node.name} is unreachable:</b> ${node.error}. Showing the last snapshot.</div>`}
    <header class="proj-head">
      <div class="proj-title">
        <h1>${p.name}</h1>
        <span class="mut">on ${node.name}</span>
        <span class="spacer"></span>
        <button class="linkbtn details-toggle" aria-expanded=${details} aria-controls="pf-details" onClick=${() => setDetails((d) => !d)}>Details ${details ? '▴' : '▾'}</button>
      </div>
      ${details && html`<${ProjectDetails} state=${state} node=${node} p=${p} now=${now} toast=${toast} refresh=${refresh} />`}
      <${LeadBar} state=${state} node=${node} p=${p} toast=${toast} onChanged=${refresh} openChat=${openChat} openDrawer=${openDrawer} now=${now} />
      ${p.result != null && html`<${ResultCard} key=${`${node.name}/${p.name}`} state=${state} node=${node} p=${p} toast=${toast} refresh=${refresh} openDrawer=${openDrawer} />`}
      <nav class="tabs" role="tablist" aria-label="Project" onKeyDown=${onTabKey}>
        ${PROJECT_TABS.map(([k, label]) => html`<button role="tab" id=${'tab-' + k} class=${'tab' + (tab === k ? ' is-active' : '')} aria-selected=${tab === k}
          aria-controls="proj-panel" tabindex=${tab === k ? 0 : -1} onClick=${() => setTab(k)}>
          ${label}${k === 'questions' && open > 0 && html`<span class="tab-count">${open}</span>`}
        </button>`)}
      </nav>
    </header>
    <div class=${'proj-body' + (tab === 'board' ? ' has-rail' : '')}>
      <div class="proj-main" role="tabpanel" id="proj-panel" aria-labelledby=${'tab-' + tab}>
        ${tab === 'board' && html`<${TicketList} node=${node} p=${p} openDrawer=${openDrawer} toast=${toast} refresh=${refresh} />`}
        ${tab === 'map' && html`<section class="block">
            <h2 class="label">Destination</h2>
            <p class="destination" style="font-size: 15px; max-width: 70ch">${p.map?.destination || html`<span class="dim">work/map.md has no destination.</span>`}</p>
          </section>
          <section class="block">
            <h2 class="label">Decisions so far</h2>
            ${p.map?.decisions?.length
              ? html`<ul class="decisions" onClick=${onMd}>${p.map.decisions.map((d) => html`<li dangerouslySetInnerHTML=${{ __html: mdInline(d.replace(/^\s*[-*]\s+/, '')) }}></li>`)}</ul>`
              : html`<p class="muted-note">None yet.</p>`}
          </section>
          <${AdrList} node=${node} p=${p} openDrawer=${openDrawer} />`}
        ${tab === 'questions' && html`<${QuestionHistory} node=${node} p=${p} now=${now} openDrawer=${openDrawer} />`}
        ${tab === 'commits' && html`<${Commits} p=${p} now=${now} />`}
        ${tab === 'usage' && html`<${UsageTab} node=${node} p=${p} now=${now} />`}
        ${tab === 'pane' && html`<section class="block">
          <div class="block-head">
            <h2 class="label">Lead pane <span class="dim">last 60 lines, read-only</span></h2>
            <div class="tools">
              ${pane.at && html`<span class="dim small">${paneTime(pane.at)}</span>`}
              <button class="btn btn-sm" onClick=${pane.load} disabled=${pane.loading}>${pane.loading ? 'Loading…' : 'Refresh'}</button>
            </div>
          </div>
          <${PaneBlock} pane=${pane} p=${p} />
        </section>`}
      </div>
      ${tab === 'board' && html`<${Rail} node=${node} p=${p} pane=${pane} openDrawer=${openDrawer} />`}
    </div>
  </div>`;
}

function NotFound({ msg }) {
  return html`<div class="page"><div class="empty"><h2>Not found</h2><p>${msg}</p><p><a href="#/">Back to the inbox</a></p></div></div>`;
}

/* ---------- drawer ---------- */

function Frontmatter({ meta, p, onOpen }) {
  const titleOf = (id) => p?.tickets.find((t) => t.id === id);
  const show = meta.filter(([k]) => k !== 'title');
  return html`<dl class="fm">
    ${show.map(([k, v]) => {
      let val = v || html`<span class="dim">none</span>`;
      if (k === 'blocked_by') {
        const ids = v.replace(/[\[\]\s]/g, '').split(',').filter(Boolean);
        val = ids.length ? ids.map((id) => {
          const t = titleOf(id);
          return html`<span class="fm-ref">${t ? html`<a href="#" onClick=${(e) => { e.preventDefault(); onOpen(t.path, 'ticket'); }}><span class="mono">${id}</span> ${t.title}</a>` : html`<span class="mono">${id}</span>`}${t ? html` <span class="dim">(${t.state})</span>` : ''}</span>`;
        }) : html`<span class="dim">none</span>`;
      } else if (k === 'gate') {
        val = /^(yes|true)$/i.test(v) ? html`<${GateBadge} />` : 'no';
      }
      return html`<div class="fm-row"><dt>${(k[0].toUpperCase() + k.slice(1)).replace(/_/g, ' ')}</dt><dd>${val}</dd></div>`;
    })}
  </dl>`;
}

const TICKET_OPS = {
  close: { button: 'Close ticket…', field: 'resolution', label: 'Resolution', submit: 'Close ticket', busy: 'Closing…',
    hint: 'Appended under ## Resolution, and listed under Decisions so far in the map.', done: (id) => `Ticket ${id} closed` },
  'out-of-scope': { button: 'Out of scope…', field: 'reason', label: 'Why it is out of scope', submit: 'Rule out of scope', busy: 'Saving…',
    hint: 'Closes the ticket with "Out of scope: <reason>" and lists it under Out of scope in the map.', done: (id) => `Ticket ${id} ruled out of scope` },
  blockers: { button: 'Edit blockers…', submit: 'Save blockers', busy: 'Saving…', done: (id) => `Blockers of ${id} saved` },
};

function TicketOpForm({ op, node, p, t, onDone, onCancel }) {
  const spec = TICKET_OPS[op];
  const [text, setText] = useState('');
  const [blockers, setBlockers] = useState(t.blocked_by || []);
  const [tried, setTried] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const ref = useRef();
  const id = `to-${op}`;
  useEscape(onCancel, ref);
  useEffect(() => { ref.current?.querySelector('textarea, input')?.focus(); }, []);
  const textErr = spec.field && !text.trim() ? `Write the ${spec.field}.` : null;
  const submit = async (e) => {
    e.preventDefault();
    setTried(true);
    if (textErr) { firstInvalid(ref.current); return; }
    setBusy(true); setError(null);
    try {
      await api.ticket(node.name, p.name, op, spec.field ? { id: t.id, [spec.field]: text.trim() } : { id: t.id, blocked_by: blockers });
      onDone(spec.done(t.id));
    } catch (x) { setError(x.message); setBusy(false); }
  };
  return html`<form class="op-form" ref=${ref} onSubmit=${submit} novalidate>
    ${spec.field
      ? html`<${Field} id=${id} label=${spec.label} error=${tried ? textErr : null} hint=${spec.hint}>
          <textarea id=${id} class="input" rows="3" value=${text} aria-invalid=${!!(tried && textErr)} aria-describedby=${id + '-msg'}
            onInput=${(e) => setText(e.target.value)}></textarea>
        </${Field}>`
      : html`<${Field} id=${id} label="Blocked by" hint="Open tickets that must close first. Leave empty to unblock.">
          <${Combo} id=${id} multiple options=${ticketOptions(p, t.id)} value=${blockers} onChange=${setBlockers}
            placeholder="Search tickets by name or id" describedBy=${id + '-msg'} emptyText="No other open ticket matches" />
        </${Field}>`}
    ${error && html`<div class="inline-error" role="alert">${error}</div>`}
    <div class="form-actions">
      <button class=${'btn ' + (op === 'blockers' ? 'btn-primary' : 'btn-danger')} type="submit" disabled=${busy}>${busy ? spec.busy : spec.submit}</button>
      <button type="button" class="btn btn-quiet" onClick=${onCancel} disabled=${busy}>Cancel</button>
    </div>
  </form>`;
}

function TicketActions({ node, p, t, toast, onChanged }) {
  const [op, setOp] = useState(null);
  const why = !node.ok ? `${node.name} is ${offWord(node)}, so the ticket can't be changed now.`
    : t.status !== 'open' ? `Ticket ${t.id} is closed. Closed tickets are history and stay as they are.`
    : t.assignee ? `${t.assignee === 'lead' ? 'The lead' : t.assignee} claimed this ticket, so only ${t.assignee === 'lead' ? 'the lead' : 'its claimant'} edits it. Close, Out of scope and Edit blockers apply to unclaimed tickets.`
    : null;
  const done = (msg) => { setOp(null); toast(msg); onChanged(); };
  return html`<div class="tk-actions">
    <div class="tk-actions-bar" role="group" aria-label="Ticket actions">
      ${Object.entries(TICKET_OPS).map(([k, s]) => html`<button type="button" class=${'btn btn-sm' + (op === k ? ' is-pressed' : '')}
        aria-pressed=${op === k} disabled=${!!why} onClick=${() => setOp((o) => (o === k ? null : k))}>${s.button}</button>`)}
    </div>
    ${why && html`<p class="tk-why">${why}</p>`}
    ${op && !why && html`<${TicketOpForm} key=${op} op=${op} node=${node} p=${p} t=${t} onDone=${done} onCancel=${() => setOp(null)} />`}
  </div>`;
}

function Drawer({ item, state, onClose, onOpen, now, toast, refresh }) {
  const [docRead, setDoc] = useState({});
  const [answerRead, setAnswer] = useState(null);
  const [rev, setRev] = useState(0);
  const panel = useRef();
  useEscape(onClose, panel);
  const node = state?.nodes.find((n) => n.name === item.node);
  const p = node?.snapshot?.projects.find((x) => x.name === item.project);
  const ticket = item.kind === 'ticket' ? (p?.tickets.find((t) => t.path === item.path) || item.ticket) : null;
  const q = item.kind === 'question' ? (p?.questions.find((x) => x.path === item.path) || item.q) : null;

  // Each read carries its path: another path shows Loading from its first render, while a reload of the same path (after an
  // action, or once the question is answered) keeps the old text meanwhile.
  const key = `${item.node}/${item.project}/${item.path}`;
  const doc = docRead.key === key ? docRead : { loading: true };
  const answer = answerRead?.key === key ? answerRead : null;
  useEffect(() => {
    let live = true;
    api.file(item.node, item.project, item.path)
      .then((r) => live && setDoc({ key, content: r.content }))
      .catch((e) => live && setDoc({ key, error: e.message }));
    if (q?.answered) {
      api.file(item.node, item.project, item.path.replace(/\.md$/, '.answer.md'))
        .then((r) => live && setAnswer({ key, content: r.content }))
        .catch((e) => live && setAnswer({ key, error: e.message }));
    }
    return () => { live = false; };
  }, [key, rev, q?.answered]);
  useEffect(() => { panel.current?.focus(); }, [key]);
  const changed = async () => { await refresh(); setRev((r) => r + 1); };

  const { meta, body } = splitFrontmatter(doc.content || '');
  const title = ticket?.title || meta?.find(([k]) => k === 'title')?.[1] ||
    (item.kind === 'question' && q ? `Question ${q.id}` : null) ||
    (/^#\s+(.*)$/m.exec(body)?.[1]) || item.path.split('/').pop();
  const kindLabel = { ticket: 'Ticket', adr: 'Decision record', question: 'Question' }[item.kind] || 'File';
  const related = ticket && p ? p.questions.filter((x) => x.ticket === ticket.id) : [];
  const shownBody = item.kind === 'adr' ? body.replace(/^#\s+.*\n?/, '') : body;
  const bodyHtml = useMemo(() => md(shownBody), [shownBody]);
  const onMd = mdLinkHandler(item.path, (path) => onOpen(path, path.includes('/adr/') ? 'adr' : path.includes('/inbox/') ? 'question' : 'ticket'));

  return html`<div class="drawer-wrap">
    <div class="scrim" onClick=${onClose}></div>
    <aside class="drawer" role="dialog" aria-modal="true" aria-label=${title} tabindex="-1" ref=${panel} onKeyDown=${trapTab}>
      <header class="drawer-head">
        <div>
          <div class="drawer-kind">${kindLabel}${ticket ? html` <span class="mono">${ticket.id}</span>` : ''} <span class="dim">· ${item.project}</span></div>
          <h2 class="drawer-title">${title}</h2>
          <div class="mono dim small">${item.path}</div>
        </div>
        <button class="btn btn-quiet btn-sm" onClick=${onClose} aria-label="Close">Close <kbd>Esc</kbd></button>
      </header>
      <div class="drawer-body">
        ${ticket && node && p && html`<${TicketActions} key=${ticket.path} node=${node} p=${p} t=${ticket} toast=${toast} onChanged=${changed} />`}
        ${q && html`<div class="drawer-q">
          <div class="q-head">${q.gate && html`<${GateBadge} />`}<${QuestionAge} q=${q} now=${now} /></div>
          <p class="q-context">${q.context}</p>
          <${Options} q=${q} />
        </div>`}
        ${doc.loading && html`<div class="skeleton" aria-label="Loading"><span></span><span></span><span></span></div>`}
        ${doc.error && html`<div class="inline-error">Could not read ${item.path}: ${doc.error}</div>`}
        ${doc.content != null && html`
          ${meta && item.kind !== 'question' && html`<${Frontmatter} meta=${meta} p=${p} onOpen=${onOpen} />`}
          ${item.kind !== 'question' && html`<div class="md" onClick=${onMd} dangerouslySetInnerHTML=${{ __html: bodyHtml }}></div>`}
        `}
        ${answer && html`<div class="drawer-section">
          <h3 class="label">Answer file</h3>
          ${answer.error ? html`<div class="inline-error">Could not read the answer: ${answer.error}</div>` : (() => {
            const a = splitFrontmatter(answer.content);
            return html`${a.meta && html`<${Frontmatter} meta=${a.meta} p=${p} onOpen=${onOpen} />`}
              ${a.body.trim() ? html`<div class="md" onClick=${onMd} dangerouslySetInnerHTML=${{ __html: md(a.body) }}></div>` : html`<p class="muted-note">No notes with the answer.</p>`}`;
          })()}
        </div>`}
        ${related.length > 0 && html`<div class="drawer-section">
          <h3 class="label">Questions on this ticket</h3>
          <ul class="rows">${related.map((x) => html`<li><button class="row" onClick=${() => onOpen(x.path, 'question')}>
            <span class="mono row-id">${x.id}</span>
            <span class="row-main"><span class="row-title">${x.context}</span>
              <span class="row-sub">${x.answered ? html`<span class="badge badge-done">Answered ${x.choice}</span>` : html`<span class="badge badge-open">Open</span>`}${x.gate && html`<${GateBadge} />`}<${QuestionAge} q=${x} now=${now} /></span></span>
          </button></li>`)}</ul>
        </div>`}
      </div>
    </aside>
  </div>`;
}

// An answer carries the fingerprint of the text shown, so the node refuses it once the pane shows anything else; the drawer
// redraws from the refusal.
function DialogDrawer({ item, onClose, toast, refresh }) {
  const [view, setView] = useState({ loading: true });
  const [sending, setSending] = useState(null);
  const panel = useRef();
  useEscape(onClose, panel);
  const read = async (notice = null) => {
    try {
      const r = await api.dialog(item.node, item.project);
      setView({ dialog: r.dialog, notice });
    } catch (e) { setView({ error: e.message }); }
  };
  useEffect(() => { read(); panel.current?.focus(); }, [item.node, item.project]);
  const pick = async (o) => {
    setSending(o.n);
    try {
      const r = await api.answerDialog(item.node, item.project, view.dialog.fingerprint, o.n);
      toast(`Answered ${o.label}`);
      await refresh();
      // The same text means Claude Code has not repainted yet: a second answer could land on the dialog after it.
      const unmarked = (x) => x.lines.join('\n').replaceAll('❯', ' ');
      if (!r.dialog) onClose();
      else if (unmarked(r.dialog) === unmarked(view.dialog)) setView({ dialog: r.dialog, pending: true, notice: 'Claude Code has not moved on from this dialog yet. Read it again before you choose.' });
      else setView({ dialog: r.dialog, notice: 'The lead is at another dialog now.' });
    } catch (e) {
      if (e.status === 409 && e.data && 'dialog' in e.data) {
        const changed = e.message.startsWith('the dialog changed');
        setView({ dialog: e.data.dialog, notice: !e.data.dialog ? null : changed ? 'The dialog changed since you opened it, so nothing was sent. This is what it shows now.'
          : `${e.message[0].toUpperCase()}${e.message.slice(1)}. This is what the pane shows now.` });
      } else setView((v) => ({ ...v, error: e.message }));
    } finally { setSending(null); }
  };
  const d = view.dialog;
  const question = d && [...d.lines].reverse().find((l) => l.trim().endsWith('?'))?.trim();
  const yes = d?.options.find((o) => /^yes\b/i.test(o.label));
  return html`<div class="drawer-wrap">
    <div class="scrim" onClick=${onClose}></div>
    <aside class="drawer" role="dialog" aria-modal="true" aria-label=${`Dialog for ${item.project}`} tabindex="-1" ref=${panel} onKeyDown=${trapTab}>
      <header class="drawer-head">
        <div>
          <div class="drawer-kind">Claude Code dialog <span class="dim">· ${item.project} · ${item.node}</span></div>
          <h2 class="drawer-title">${question || 'The lead is waiting at a dialog'}</h2>
        </div>
        <button class="btn btn-quiet btn-sm" onClick=${onClose} aria-label="Close">Close <kbd>Esc</kbd></button>
      </header>
      <div class="drawer-body">
        ${view.loading && html`<div class="skeleton" aria-label="Loading"><span></span><span></span><span></span></div>`}
        ${view.notice && html`<div class="inline-warn dialog-note" role="status">${view.notice}</div>`}
        ${view.error && html`<div class="inline-error" role="alert">${view.error}</div>`}
        ${!view.loading && !view.error && d === null && html`<p class="muted-note">The lead is no longer at a dialog.</p>`}
        ${d && html`<pre class="pane-dark dialog-pre">${d.lines.join('\n')}</pre>
          ${view.pending
            ? html`<div class="dialog-opts"><button class="btn" onClick=${() => { setView((v) => ({ ...v, loading: true, pending: false })); read(); }}>Read again</button></div>`
            : d.options.length
            ? html`<div class="dialog-opts" role="group" aria-label="Options">
                ${d.options.map((o) => html`<button key=${o.n} class=${'btn' + (o === yes ? ' btn-primary' : '')} disabled=${sending != null}
                  onClick=${() => pick(o)}>${sending === o.n ? 'Sending…' : `${o.n}. ${o.label}`}</button>`)}
              </div>
              <p class="dim small">Cairn moves the lead's cursor to your choice and presses Enter, only while its pane still shows exactly this text.</p>`
            : html`<p class="muted-note">Cairn could not read this dialog's options. Answer it in the lead's tmux session.</p>`}`}
      </div>
    </aside>
  </div>`;
}

// What a drawer or the review panel shows when it cannot render its project.
function PanelError({ error, onClose }) {
  const panel = useRef();
  useEscape(onClose, panel);
  return html`<div class="drawer-wrap">
    <div class="scrim" onClick=${onClose}></div>
    <aside class="drawer" role="dialog" aria-modal="true" aria-label="Could not show this" tabindex="-1" ref=${panel} onKeyDown=${trapTab}>
      <header class="drawer-head">
        <h2 class="drawer-title">This could not be shown</h2>
        <button class="btn btn-quiet btn-sm" onClick=${onClose} aria-label="Close">Close <kbd>Esc</kbd></button>
      </header>
      <div class="drawer-body"><div class="inline-error" role="alert">${errText(error)}</div></div>
    </aside>
  </div>`;
}

/* ---------- review chat ---------- */

const END_REASON = {
  finished: 'it went through every question',
  closed: 'you ended it',
  idle: '20 minutes passed without a message',
  exited: 'the review session exited',
};
const endReason = (state, detail) => (state === 'error' ? detail || 'the session stopped' : END_REASON[detail] || detail || 'the session closed');
const isAnswerPath = (s) => /work\/inbox\/q\d+\.answer\.md$/.test(s || '');
const TOOL_VERB = { Read: 'Reading', Bash: 'Running', Write: 'Writing', Edit: 'Editing', Grep: 'Searching', Glob: 'Listing' };

function fmtSecs(s) {
  const t = Math.max(0, Math.round(s));
  if (t < 60) return `${t} s`;
  const m = Math.floor(t / 60);
  return m < 60 ? `${m} min${t % 60 ? ` ${t % 60} s` : ''}` : dur(t * 1000);
}

// "Read 2 files, ran git log -5": the folded line for one turn's tool calls.
function toolGist(tools) {
  const by = (n) => tools.filter((t) => t.name === n);
  const files = (n) => (n === 1 ? 'a file' : `${n} files`);
  const parts = [];
  if (by('Read').length) parts.push(`read ${files(by('Read').length)}`);
  const bash = by('Bash');
  if (bash.length === 1) parts.push(`ran ${(bash[0].summary || '').split(/\s+/).slice(0, 3).join(' ')}`);
  else if (bash.length) parts.push(`ran ${bash.length} commands`);
  if (by('Edit').length) parts.push(`edited ${files(by('Edit').length)}`);
  const writes = by('Write');
  const answers = writes.filter((t) => isAnswerPath(t.summary)).length;
  if (answers) parts.push(answers === 1 ? 'wrote the answer' : `wrote ${answers} answers`);
  if (writes.length - answers) parts.push(`wrote ${files(writes.length - answers)}`);
  const other = tools.length - by('Read').length - bash.length - by('Edit').length - writes.length;
  if (other) parts.push(plural(other, 'other call'));
  const s = parts.join(', ');
  return s.charAt(0).toUpperCase() + s.slice(1);
}

// Folds events into rows. Before the last resume, earlier messages fold into one divider plus a summary of the answers.
function chatRows(events, showEarlier) {
  let cut = -1;
  for (let i = events.length - 1; i > 0; i--) if (events[i].kind === 'state' && events[i].state === 'starting') { cut = i; break; }
  const rows = [];
  // The current end or error is shown by the panel itself; earlier ones (before a resume) stay as dividers.
  const fold = (list, ends = true) => {
    let tools = null;
    for (const e of list) {
      if (e.kind === 'tool') {
        if (!tools) { tools = { type: 'tools', key: 't' + e.seq, tools: [] }; rows.push(tools); }
        tools.tools.push(e);
        continue;
      }
      if (e.kind === 'user' || e.kind === 'turn') tools = null;
      if (e.kind === 'turn') { rows.push({ type: e.ok ? 'turn' : 'failed', key: 'u' + e.seq, e }); continue; }
      if (e.kind === 'state') {
        if (ends && (e.state === 'ended' || e.state === 'error')) rows.push({ type: 'end', key: 'e' + e.seq, e });
        continue;
      }
      rows.push({ type: e.kind, key: 'm' + e.seq, e });
    }
  };
  if (cut < 0) { fold(events, false); return rows; }
  const earlier = events.slice(0, cut);
  const later = events.slice(cut);
  const shown = earlier.filter((e) => e.kind === 'text' || e.kind === 'user').length;
  rows.push({ type: 'earlier', key: 'earlier', count: shown, at: earlier[0]?.at, open: showEarlier });
  if (showEarlier) fold(earlier);
  else {
    const answered = earlier.filter((e) => e.kind === 'answered');
    if (answered.length) rows.push({ type: 'answered-sum', key: 'as', list: answered });
  }
  rows.push({ type: 'resumed', key: 'r' + events[cut].seq, e: events[cut] });
  // The hub sends nothing on a resume, so the question still being asked is shown again below the divider.
  const asked = askedId(earlier);
  if (!showEarlier && asked && askedId(later) == null) {
    const msg = [...earlier].reverse().find((e) => e.kind === 'text' && askedId([e]) === asked);
    if (msg) rows.push({ type: 'text', key: 'again' + msg.seq, e: msg });
  }
  fold(later, false);
  return rows;
}

// Assistant text: markdown, with the skill's "A. …" option lines as a list and its Gate:/Reply: lines as a quiet note.
// Parsed once per text: the panel re-renders every second while a turn runs.
function ChatText({ text, onMd }) {
  const blocks = useMemo(() => {
    const out = [];
    for (const line of String(text ?? '').split('\n')) {
      const kind = /^\s*[A-Z][.)]\s+\S/.test(line) ? 'opts' : /^(Gate:|Reply:)/.test(line.trim()) ? 'meta' : 'md';
      const last = out[out.length - 1];
      if (last && last.kind === kind) last.lines.push(line);
      else out.push({ kind, lines: [line] });
    }
    for (const b of out) {
      if (b.kind === 'opts') {
        b.opts = b.lines.map((l) => {
          const o = parseOption(l);
          return { ...o, html: mdInline(o.text) + (o.recommended ? ` <span class="rec">recommended</span>${o.reason ? ` <span class="rec-why">${escapeHtml(o.reason)}</span>` : ''}` : '') };
        });
      } else if (b.kind === 'meta') b.html = mdInline(b.lines.join(' '));
      else { const src = b.lines.join('\n').trim(); b.html = src && mdChat(src); }
    }
    return out;
  }, [text]);
  return html`<div class="msg msg-text" onClick=${onMd}>
    ${blocks.map((b, i) => {
      if (b.kind === 'opts') {
        return html`<div class="msg-opts" key=${i}>${b.opts.map((o) => html`<div class=${'msg-opt' + (o.recommended ? ' is-rec' : '')}><span>${o.letter}</span>
            <span dangerouslySetInnerHTML=${{ __html: o.html }}></span></div>`)}</div>`;
      }
      if (b.kind === 'meta') return html`<div class="msg-meta" key=${i} dangerouslySetInnerHTML=${{ __html: b.html }}></div>`;
      return b.html && html`<div class="md" key=${i} dangerouslySetInnerHTML=${{ __html: b.html }}></div>`;
    })}
  </div>`;
}

function ChatRow({ row, onMd, p, live, toggleEarlier }) {
  const { e } = row;
  const at = e ? clock(e.at) : '';
  if (row.type === 'user') return html`<div class="msg msg-user" title=${at}>${e.text}</div>`;
  if (row.type === 'text') return html`<${ChatText} text=${e.text} onMd=${onMd} />`;
  if (row.type === 'tools') {
    if (live) {
      const t = row.tools[row.tools.length - 1];
      return html`<div class="tools-line"><div class="tools-live">${TOOL_VERB[t.name] || t.name} <code>${t.summary}</code></div>
        ${row.tools.length > 1 && html`<ul>${row.tools.slice(0, -1).map((x) => html`<li key=${x.seq}><span>${x.name}</span><code>${x.summary}</code></li>`)}</ul>`}</div>`;
    }
    return html`<details class="tools-line">
      <summary>${toolGist(row.tools)}</summary>
      <ul>${row.tools.map((t) => html`<li key=${t.seq}><span>${t.name}</span><code>${t.summary}</code></li>`)}</ul>
    </details>`;
  }
  if (row.type === 'turn') return html`<div class="turn-cost">${fmtSecs(e.seconds)} · $${(Number(e.cost_usd) || 0).toFixed(2)}</div>`;
  if (row.type === 'failed') return html`<div class="turn-failed">The turn failed after ${fmtSecs(e.seconds)}.</div>`;
  if (row.type === 'answered') {
    const q = p?.questions.find((x) => x.id === e.id);
    const t = q?.ticket && q.gate ? p.tickets.find((x) => x.id === q.ticket) : null;
    return html`<div class="answered-row" title=${at}>
      <b>✓ ${e.id} answered ${e.choice === 'other' ? 'with a note' : e.choice}</b>
      <code>work/inbox/${e.id}.answer.md</code>
      ${t && html`<span class="then">ticket ${t.id} unblocks</span>`}
    </div>`;
  }
  if (row.type === 'answered-sum') {
    return html`<div class="answered-row"><b>${row.list.map((x) => `✓ ${x.id} answered ${x.choice === 'other' ? 'with a note' : x.choice}`).join(' · ')}</b></div>`;
  }
  if (row.type === 'earlier') {
    return html`<div class="rv-divider"><button type="button" aria-expanded=${row.open} onClick=${toggleEarlier}>
      ${row.open ? 'Hide earlier messages' : `${plural(row.count, 'earlier message')}${row.at ? ` · ${dayClock(row.at)}` : ''}`}</button></div>`;
  }
  if (row.type === 'resumed') return html`<div class="rv-divider"><span>Resumed ${dayClock(e.at)}</span></div>`;
  if (row.type === 'end') return html`<div class="rv-divider"><span>${e.state === 'error' ? 'Error' : 'Ended'}: ${endReason(e.state, e.detail)}</span></div>`;
  return null;
}

// The review session the dashboard follows: one at a time, kept while the panel is closed so the Inbox can show it.
// Events arrive over EventSource; after three failed connects in a row it polls GET /api/chat. Fixture mode always polls.
// A 404 means the hub no longer has the chat (it restarted): the feed stops and the session is marked gone.
function useReview(refresh, toast) {
  const [target, setTarget] = useState(null);
  const [events, setEvents] = useState([]);
  const [info, setInfo] = useState({ state: 'starting', detail: null });
  const [opening, setOpening] = useState(false);
  const [openError, setOpenError] = useState(null);
  const [gone, setGone] = useState(false);
  const [pending, setPending] = useState(null);
  const [sending, setSending] = useState(false);
  const [ending, setEnding] = useState(false);
  const [text, setText] = useState('');
  // Messages typed while a turn runs, oldest first; each goes out when the chat is waiting again.
  const [queued, setQueued] = useState([]);
  const seq = useRef(0);
  const gen = useRef(0);
  const feed = useRef(null);
  const tref = useRef(null);
  // The chat the events belong to; a different session_id or started_at means it was restarted elsewhere.
  const sess = useRef(null);
  // Set the moment a message leaves, before the next render, so a fast second key cannot send twice.
  const inFlight = useRef(false);
  const queuedRef = useRef([]);
  queuedRef.current = queued;

  // Nothing typed is lost: queued messages go back into the composer, ahead of any draft.
  const restoreQueued = () => {
    const q = queuedRef.current;
    if (!q.length) return;
    setText((d) => [...q, d].filter((x) => x && x.trim()).join('\n\n'));
    setQueued([]);
    queuedRef.current = [];
  };

  const stopFeed = () => { feed.current?.(); feed.current = null; };

  const ingest = (evs, chat, reset = false) => {
    // A reset renumbers the events, so a message still awaiting its echo would never see it.
    if (reset) { seq.current = 0; setPending(null); }
    if (chat) sess.current = { session_id: chat.session_id, started_at: chat.started_at };
    const fresh = evs.filter((e) => e.seq > seq.current);
    if (fresh.length) seq.current = fresh[fresh.length - 1].seq;
    if (reset || fresh.length) setEvents((prev) => [...(reset ? [] : prev), ...fresh]);
    const last = [...fresh].reverse().find((e) => e.kind === 'state');
    const now = chat ? { state: chat.state, detail: chat.detail } : last ? { state: last.state, detail: last.detail } : null;
    if (now) setInfo((i) => (i.state === now.state && i.detail === now.detail ? i : now));
    // A final chat needs no feed; Continue, Retry or a new start subscribes again.
    if (now && !LIVE_STATES.includes(now.state)) { stopFeed(); restoreQueued(); }
    // The question list and the Review labels come from /api/state: refresh when answers land or the chat stops.
    if (fresh.some((e) => e.kind === 'answered' || (e.kind === 'state' && !LIVE_STATES.includes(e.state)))) refresh();
  };
  const ingestRef = useRef(ingest);
  ingestRef.current = ingest;

  // One feed per tab: EventSource, or GET /api/chat every 2 s after three failed connects (fixture mode always polls).
  // A 404 means the hub no longer has the chat; a restarted chat (new session, or numbering from 1) replaces the list.
  const subscribe = (t, g) => {
    stopFeed();
    let done = false;
    let es = null;
    let timer = null;
    let fails = 0;
    let after = seq.current;
    const ok = () => !done && gen.current === g;
    const lost = () => { stopFeed(); setGone(true); restoreQueued(); };
    const restarted = (c) => c && sess.current && (c.session_id !== sess.current.session_id || c.started_at !== sess.current.started_at);
    const adopt = async () => {
      const c = await api.chat(t.node, t.project, 0);
      if (!ok()) return;
      ingestRef.current(c.events || [], c, true);
      after = seq.current;
    };
    const take = (evs, chat) => {
      if (!ok()) return;
      for (const e of evs) after = Math.max(after, e.seq);
      ingestRef.current(evs, chat);
    };
    const poll = async () => {
      try {
        const c = await api.chat(t.node, t.project, after);
        if (!ok()) return;
        if (restarted(c)) await adopt();
        else take(c.events || [], c);
      } catch (e) {
        if (e.status === 404 && ok()) { lost(); return; }
        if (e.status === 401 && ok() && feed.current) { timer = setTimeout(poll, POLL_MS); return; }
      }
      if (ok() && feed.current) timer = setTimeout(poll, FIXTURE ? 500 : 2000);
    };
    const connect = () => {
      es = new EventSource(api.chatEventsUrl(t.node, t.project, after));
      es.onopen = () => { fails = 0; };
      es.onmessage = async (m) => {
        let e;
        try { e = JSON.parse(m.data); } catch { return; /* skip a malformed event */ }
        // The hub restarts the stream at 0 for a chat started anew elsewhere: numbering below ours means a new chat.
        if (e.seq <= after) {
          if (e.seq === 1) { try { await adopt(); } catch { /* the next reconnect tries again */ } }
          return;
        }
        take([e]);
      };
      es.onerror = async () => {
        es.close();
        if (!ok()) return;
        // EventSource hides the status, so ask GET /api/chat whether the chat still exists (or was restarted) before reconnecting.
        try {
          const c = await api.chat(t.node, t.project, after);
          if (ok() && restarted(c)) await adopt();
        } catch (e) {
          if (e.status === 404 && ok()) { lost(); return; }
          // not signed in: the page says so; reconnect only once a request gets through
          if (e.status === 401 && ok() && feed.current) { timer = setTimeout(connect, POLL_MS); return; }
        }
        if (!ok() || !feed.current) return;
        if (++fails >= 3) poll();
        else timer = setTimeout(connect, 1000 * fails);
      };
    };
    feed.current = () => { done = true; es?.close(); clearTimeout(timer); };
    if (FIXTURE) poll(); else connect();
  };
  useEffect(() => () => stopFeed(), []);

  const start = async (t, resume) => {
    const g = ++gen.current;
    stopFeed();
    const same = tref.current && tref.current.node === t.node && tref.current.project === t.project;
    // A start can reset the event numbering, so nothing sent earlier is still awaited.
    setPending(null);
    if (same) restoreQueued();
    else { setEvents([]); seq.current = 0; sess.current = null; setText(''); setQueued([]); setInfo({ state: 'starting', detail: null }); }
    tref.current = t;
    setTarget(t);
    setOpening(true); setOpenError(null); setGone(false);
    try {
      const c = await api.chatStart(t.node, t.project, resume);
      // A later start (another project, or Retry) supersedes this one.
      if (gen.current !== g) return;
      ingest(c.events || [], c, true);
      if (LIVE_STATES.includes(c.state)) subscribe(t, g);
      refresh();
    } catch (e) { if (gen.current === g) setOpenError(e.message); }
    finally { if (gen.current === g) setOpening(false); }
  };

  const live = !gone && !openError && LIVE_STATES.includes(info.state);
  const awaitingEcho = !!pending && !events.some((e) => e.seq >= pending.seq);
  const canSend = info.state === 'waiting' && live && !opening && !sending && !awaitingEcho;

  const send = async (raw, from = 'chip') => {
    const t = raw.trim();
    if (!t || !tref.current || inFlight.current) return;
    if (t.length > 8000) { toast('A message is at most 8000 characters', 'error'); return; }
    if (!canSend) {
      // Typed while a turn runs: it joins the queue and goes out, in order, when the chat is waiting again.
      if (from === 'box' && live && !opening) { setQueued((q) => [...q, t]); setText(''); }
      return;
    }
    const g = gen.current;
    const where = tref.current;
    inFlight.current = true;
    setSending(true);
    // The box empties before the POST, so a second Enter has nothing to send again; a failure puts the text back.
    if (from === 'box') setText('');
    try {
      const r = await api.chatSend(where.node, where.project, t);
      // A reply for a project the panel no longer shows is dropped.
      if (gen.current !== g) return;
      setPending({ seq: r.seq, text: t });
      if (from === 'queue') setQueued((q) => q.slice(1));
    } catch (e) {
      if (gen.current !== g) return;
      if (from === 'box') setText((d) => [t, d].filter((x) => x && x.trim()).join('\n\n'));
      if (from === 'queue') restoreQueued();
      if (e.status === 404) setGone(true);
      else toast(`Send failed: ${e.message}`, 'error');
    } finally { inFlight.current = false; setSending(false); }
  };
  useEffect(() => { if (queued.length && canSend) send(queued[0], 'queue'); }, [queued, canSend]);

  const end = async () => {
    if (!tref.current) return;
    const g = gen.current;
    const where = tref.current;
    setEnding(true);
    try {
      const c = await api.chatEnd(where.node, where.project);
      if (gen.current === g) ingest(c.events || [], c);
      refresh();
    } catch (e) {
      if (gen.current !== g) return;
      if (e.status === 404) { setGone(true); stopFeed(); restoreQueued(); }
      else toast(`End session failed: ${e.message}`, 'error');
    } finally { setEnding(false); }
  };

  // Open: a live session for the same project is shown as is; otherwise Review starts or resumes one.
  const open = (t) => {
    const same = tref.current && tref.current.node === t.node && tref.current.project === t.project;
    if (same && (live || opening)) return;
    start(t, t.resume);
  };

  return { target, events, info, opening, openError, gone, pending, sending, ending, text, setText, queued,
    live, awaitingEcho, canSend, start, send, end, open };
}

function ReviewPanel({ review, state, onClose, openChat, onOpenDrawer }) {
  const { target, events, info, opening, openError, gone } = review;
  const [confirmEnd, setConfirmEnd] = useState(false);
  const [showEarlier, setShowEarlier] = useState(false);
  const panel = useRef();
  const body = useRef();
  const box = useRef();
  const again = useRef();
  useEscape(onClose, panel);
  const now = useNow(1000);

  const node = state?.nodes.find((x) => x.name === target.node);
  const p = node?.snapshot?.projects.find((x) => x.name === target.project);
  const open = p ? openQs(p).sort(byFocus) : [];
  const answeredEv = events.filter((e) => e.kind === 'answered');
  const answeredHere = new Map(answeredEv.map((e) => [e.id, e.choice]));
  const asked = askedId(events);
  const focus = asked ? open.find((q) => q.id === asked) || null : open[0] || null;
  const pills = p ? p.questions.filter((q) => !q.answered || answeredHere.has(q.id)).sort(byFocus) : [];
  const hasRows = events.some((e) => e.kind === 'text' || e.kind === 'user' || e.kind === 'tool');

  // Connecting comes from the chat state, not from whether a reply exists; an ended or failed chat keeps its view while
  // Continue or Retry runs, with the button busy.
  const phase = openError || gone || info.state === 'error' ? 'error'
    : info.state === 'ended' ? 'ended'
    : opening || info.state === 'starting' ? 'connecting'
    : 'live';
  const working = !gone && !openError && (info.state === 'working' || review.awaitingEcho) && phase === 'live';
  const lastWork = [...events].reverse().find((e) => e.kind === 'state' && e.state === 'working');
  const workSecs = lastWork ? Math.max(0, Math.round((now - Date.parse(lastWork.at)) / 1000)) : 0;
  const lastTurn = events.reduce((i, e, k) => (e.kind === 'turn' ? k : i), -1);
  const liveTools = working ? events.slice(lastTurn + 1).some((e) => e.kind === 'tool') : false;

  // Follow new rows unless the operator scrolled up to read.
  const pinned = useRef(true);
  useLayoutEffect(() => { if (pinned.current && body.current) body.current.scrollTop = body.current.scrollHeight; }, [events, review.pending, info.state, phase, review.queued.length]);
  const onScroll = () => { const b = body.current; pinned.current = b.scrollHeight - b.scrollTop - b.clientHeight < 40; };
  useEffect(() => {
    if (phase === 'error' || phase === 'ended') again.current?.focus();
    // The composer appears with the first reply; take the focus unless the operator moved it elsewhere on the page.
    else if (phase === 'live' && (document.activeElement === document.body || panel.current?.contains(document.activeElement))) box.current?.focus();
  }, [phase]);

  const onMd = mdLinkHandler('work/inbox/x.md', (path) => onOpenDrawer({ node: target.node, project: target.project, path, kind: path.includes('/adr/') ? 'adr' : path.includes('/inbox/') ? 'question' : 'ticket' }));
  const letters = focus ? focus.options.map(parseOption).filter((o) => o.letter) : [];
  const rec = letters.find((o) => o.recommended)?.letter || focus?.default;
  const chip = (v) => { review.send(v); box.current?.focus(); };
  // The composer only ever types: Enter sends, Shift+Enter adds a line. Chips are clicked.
  const onKey = (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); review.send(review.text, 'box'); }
  };
  const grow = (e) => {
    review.setText(e.target.value);
    e.target.style.height = 'auto';
    e.target.style.height = Math.min(e.target.scrollHeight, 180) + 'px';
  };
  useEffect(() => { if (!review.text && box.current) box.current.style.height = ''; }, [review.text]);

  const idx = focus ? pills.indexOf(focus) + 1 : 0;
  const after = focus ? pills.slice(idx).filter((q) => !q.answered).length : 0;
  const ticket = focus?.ticket ? p.tickets.find((t) => t.id === focus.ticket) : null;
  const rows = chatRows(events, showEarlier);
  const lastToolsKey = [...rows].reverse().find((r) => r.type === 'tools')?.key;

  const status = phase === 'connecting' || opening ? html`<span class="rv-status is-connecting"><span class="sdot"></span>connecting to ${target.node}…</span>`
    : phase === 'error' ? html`<span class="rv-status is-bad"><span class="sdot"></span>disconnected</span>`
    : phase === 'ended' ? html`<span class="rv-status is-ended"><span class="sdot"></span>ended</span>`
    : html`<span class="rv-status"><span class="sdot"></span>live on ${target.node}</span>`;

  const errDetail = gone ? 'The hub restarted and no longer has this chat.' : openError || info.detail || 'the session stopped';
  const errTitle = gone ? 'This chat is gone (the hub restarted)'
    : openError ? 'Could not open the review session'
    : /log ?in|logged|api key|oauth/i.test(errDetail) ? `The Claude login on ${target.node} failed`
    : /ssh|connect|pipe|exit 255|closed by remote/i.test(errDetail) ? `Connection to ${target.node} dropped`
    : `The review session on ${target.node} stopped`;
  const saved = answeredEv.map((e) => e.id);
  // One line without control characters: node text pasted into a terminal must not run as a command.
  const copyErr = async () => {
    try { await navigator.clipboard.writeText(String(errDetail).replace(/[\u0000-\u001f\u007f-\u009f\u2028\u2029]+/g, ' ').replace(/\s+/g, ' ').trim()); } catch { /* the text stays selectable */ }
  };

  const turns = events.filter((e) => e.kind === 'turn');
  const cost = turns.reduce((s, e) => s + (Number(e.cost_usd) || 0), 0);
  const endEv = [...events].reverse().find((e) => e.kind === 'state' && e.state === 'ended');
  const secs = endEv && events[0] ? (Date.parse(endEv.at) - Date.parse(events[0].at)) / 1000 : 0;
  const latest = new Map(answeredEv.map((e) => [e.id, e.choice]));
  const nextUp = allProjects(state).map(({ node: n, p: x }) => ({ n, x, open: openQs(x) }))
    .filter((g) => g.open.length && !(g.n.name === target.node && g.x.name === target.project))
    .sort((a, b) => Number(b.open.some((q) => q.gate)) - Number(a.open.some((q) => q.gate)))[0];
  const lastApplied = p?.questions.map((q) => q.applied).filter(Boolean).sort().pop();
  const nothing = phase === 'ended' && !answeredEv.length && !open.length;
  const gates = open.filter((q) => q.gate).length;

  return html`<aside class="rv" role="complementary" aria-label=${`Review chat for ${target.project}`} ref=${panel}>
    <header class="rv-head">
      <b>Review</b><span class="rv-project">${target.project}</span>${status}
      <span class="spacer"></span>
      ${review.live && !opening && html`<button class="rv-link" aria-expanded=${confirmEnd} onClick=${() => setConfirmEnd((v) => !v)} disabled=${review.ending}>${review.ending ? 'Ending…' : 'End session'}</button>`}
      <button class="rv-link" onClick=${onClose} aria-label="Close the panel; the session stays">Close <kbd>esc</kbd></button>
    </header>
    ${confirmEnd && review.live && html`<div class="rv-confirm"><${ConfirmBar}
      message=${html`End the review session? Answers written so far stay, and Review resumes it later.`}
      confirmLabel="End session" busyLabel="Ending…" onCancel=${() => setConfirmEnd(false)}
      onConfirm=${async () => { await review.end(); setConfirmEnd(false); }} /></div>`}
    ${(phase === 'live' || phase === 'error') && pills.length > 0 && html`<div class="rv-queue">
      <div class="rv-pills">
        ${pills.map((q) => html`<span key=${q.id} class=${'pill' + (q.answered ? ' is-done' : q === focus ? ' is-cur' : '')}>${q.id}${q.answered ? ` ${answeredHere.get(q.id) || q.choice}` : ''}</span>`)}
        <span class="rv-pos">${focus ? `${idx} of ${pills.length} · ${after ? `${after} after this` : 'last one'}` : `${plural(open.length, 'question')} left`}</span>
      </div>
      ${focus && html`<div class="rv-qline">
        <b>${ticket ? `${ticket.id} ${ticket.title}` : focus.context}</b>
        <span>lead applies ${focus.default} · ${since(focus.asked_at, now)}</span>
      </div>`}
    </div>`}
    <div class="rv-body" ref=${body} onScroll=${onScroll} aria-live="polite">
      ${phase === 'connecting' && !hasRows ? html`<div class="rv-center">
          <div class="rv-center-title"><span class="dots dots-lg" aria-hidden="true"><i></i><i></i><i></i></span>Opening the review session</div>
          <p>ssh ${node ? sshTarget(node) : target.node} · cairn-review skill · ${plural(open.length, 'open question')}${gates > 1 ? ', the gated ones first' : gates ? ', the gated one first' : ''}. First reply usually takes about 2 s.</p>
        </div>`
      : nothing ? html`<div class="rv-center">
          <div class="rv-center-title">Nothing to review in ${target.project}</div>
          <p>All questions are answered.${lastApplied ? ` The lead applied the last one ${ago(lastApplied, now)}.` : ''} Close with esc; Review starts a new session when a question comes in.</p>
        </div>`
      : html`<div class="rv-list">
        ${rows.map((r) => html`<${ChatRow} key=${r.key} row=${r} onMd=${onMd} p=${p} live=${liveTools && r.key === lastToolsKey}
          toggleEarlier=${() => setShowEarlier((v) => !v)} />`)}
        ${review.awaitingEcho && html`<div class="msg msg-user is-pending">${review.pending.text}</div>`}
        ${review.queued.map((m, i) => html`<div class="msg msg-user is-pending" key=${'q' + i}>${m}</div>`)}
        ${review.queued.length > 0 && html`<div class="msg-queued">${review.queued.length > 1 ? 'send in order when this turn finishes' : 'sends when this turn finishes'}</div>`}
        ${working && html`<div class="working"><span class="dots" aria-hidden="true"><i></i><i></i><i></i></span>Working<span aria-hidden="true"> · ${workSecs} s</span></div>`}
        ${phase === 'connecting' && html`<div class="working"><span class="dots" aria-hidden="true"><i></i><i></i><i></i></span>Opening the review session…</div>`}
        ${phase === 'error' && html`<div class="err-block" role="alert">
          <b>${errTitle}</b>
          ${!gone && html`<code>${errDetail}</code>`}
          <p>${saved.length ? `${saved.join(', ')} ${saved.length > 1 ? 'were' : 'was'} saved before the drop. ` : ''}${gone ? 'Review starts a new session at the first open question.' : 'Retry starts a new session; it asks the open questions again, gates first.'}</p>
          <div class="err-actions">
            <button class="btn btn-primary" ref=${again} disabled=${opening} onClick=${() => review.start(target, openError ? target.resume : false)}>${opening ? (gone ? 'Starting…' : 'Retrying…') : html`${gone ? 'Review' : 'Retry'}<span class="key">↵</span>`}</button>
            ${!gone && html`<button class="btn" onClick=${copyErr}>Copy error</button>`}
          </div>
        </div>`}
        ${phase === 'ended' && html`<div class="summary">
            <div class="summary-title">${!latest.size ? 'Nothing was answered.' : open.length ? `${latest.size} answered, ${plural(open.length, 'question')} still open.` : `All ${latest.size} answered.`}</div>
            ${latest.size > 0 && html`<div class="summary-grid">
              ${[...latest].map(([id, c]) => {
                const q = p?.questions.find((x) => x.id === id);
                const o = q?.options.map(parseOption).find((x) => x.letter === c);
                return html`<span class="mono">${id}</span><b>${c === 'other' ? '—' : c}</b><span>${o ? o.text : c === 'other' ? 'your own answer' : q?.context || ''}</span>`;
              })}
            </div>`}
          </div>
          <div class="ended-note">
            <b>Session ended <span class="mut" style="font-weight: 400">· ${endReason(info.state, info.detail)}</span></b>
            ${latest.size > 0 && html`<p>${p?.lead === 'alive'
              ? `The lead is running; it applies ${plural(latest.size, 'answer')} before its next ticket.`
              : `The lead restarts within 30 s and applies ${plural(latest.size, 'answer')}.`}</p>`}
            ${turns.length > 0 && html`<span class="stats">${fmtSecs(secs)} · $${cost.toFixed(2)} · ${plural(turns.length, 'turn')}</span>`}
            ${open.length > 0 && html`<button class="linkbtn" disabled=${opening} onClick=${() => review.start(target, true)}>${opening ? 'Resuming…' : 'Continue this session'}</button>`}
          </div>`}
      </div>`}
    </div>
    ${phase === 'ended' ? html`<footer class="rv-foot rv-endfoot">
        <p>${nextUp ? `${nextUp.x.name} has ${plural(nextUp.open.length, 'question')} waiting.` : 'Nothing else is waiting.'}</p>
        ${nextUp && html`<button class="btn" onClick=${() => openChat({ node: nextUp.n.name, project: nextUp.x.name, resume: wantsResume(chatOf(state, nextUp.n.name, nextUp.x.name)) })}>Review ${nextUp.x.name}</button>`}
        <button class="btn btn-primary" ref=${nextUp ? null : again} onClick=${onClose}>Close<span class="key">esc</span></button>
      </footer>`
    : phase === 'connecting' || phase === 'error' ? html`<footer class="rv-foot">
        <div class="composer-off">${phase === 'error'
          ? 'Reconnect to keep going. Nothing typed is lost.'
          : 'You can type once the session is ready.'}</div>
      </footer>`
    : html`<footer class="rv-foot">
        ${focus && html`<div class="rv-chips" role="group" aria-label=${`Quick replies for ${focus.id}`}>
          ${letters.map((o) => html`<button type="button" class=${'chip-btn' + (o.letter === rec ? ' is-rec' : '')} data-chip=${o.letter.toLowerCase()}
            disabled=${!review.canSend} title=${o.text} onClick=${() => chip(o.letter)}>${o.letter}${o.letter === rec && html`<small>rec</small>`}</button>`)}
          <span class="chip-sep" aria-hidden="true"></span>
          <button type="button" class="chip-btn" data-chip="y" disabled=${!review.canSend} title=${`Take the recommendation (${rec})`} onClick=${() => chip('y')}>y<small class="mut">= ${rec}</small></button>
          <button type="button" class="chip-btn is-word" disabled=${!review.canSend} title="Skip; the lead keeps the default" onClick=${() => chip('later')}>later</button>
        </div>`}
        <div class="composer">
          <textarea ref=${box} rows="1" maxlength="8000" aria-label="Message" value=${review.text} onInput=${grow} onKeyDown=${onKey}
            placeholder=${info.state === 'working' || review.awaitingEcho ? 'Type now; it sends when this turn finishes'
              : open.length ? 'Reply with a letter, y, later, or ask anything' : 'Ask about the project, or esc to close'}></textarea>
          <button class="send-key" aria-label="Send" disabled=${!review.text.trim()} onClick=${() => review.send(review.text, 'box')}>↵</button>
        </div>
        <div class="rv-hint"><span>↵ send</span><span>⇧↵ newline</span><span class="spacer"></span><span>esc closes, session stays</span></div>
      </footer>`}
  </aside>`;
}

/* ---------- toasts ---------- */

// A toast is a bold line plus an optional quieter one ("q11 answered A  wrote work/inbox/q11.answer.md · …").
function Toasts({ items, dismiss }) {
  return html`<div class="toasts" role="status" aria-live="polite">
    ${items.map((t) => html`<div key=${t.id} class=${'toast toast-' + t.tone}>
      <div class="toast-msg">${t.detail ? html`<b>${t.msg}</b><span class="toast-sub">${t.detail}</span>` : t.msg}</div>
      <button class="toast-x" onClick=${() => dismiss(t.id)} aria-label="Dismiss">×</button>
    </div>`)}
  </div>`;
}

/* ---------- keyboard ---------- */

// Esc closes the newest open form, drawer or panel (escStack); inside a field that belongs to something else it only leaves the field.
// j/k move between [data-nav] rows and / focuses the page filter. r opens Review for the focused row's project or this
// project page. c focuses the review composer when the panel is open and no Inbox question has the focus. The letter keys
// pick an answer only on the focused Inbox question (the row handles them); with nothing focused they go to the open row.
function useKeys({ openReview }) {
  const live = useRef(openReview);
  live.current = openReview;
  useEffect(() => {
    const onKey = (e) => {
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      const typing = e.target.closest?.('input, textarea, select, [contenteditable]');
      if (e.key === 'Escape') {
        const ms = document.querySelector('details.ms[open]');
        if (ms) { ms.open = false; ms.querySelector('summary')?.focus(); return; }
        // Chrome clears a search field on Esc without an input event, which would leave the filter applied.
        if (e.target.matches?.('input[type="search"]')) {
          e.preventDefault();
          if (e.target.value) { e.target.value = ''; e.target.dispatchEvent(new Event('input', { bubbles: true })); }
          else e.target.blur();
          return;
        }
        const top = escStack[escStack.length - 1];
        const outside = typing && top?.ref.current && !top.ref.current.contains(e.target);
        if (top && !outside) { e.preventDefault(); top.fn(); return; }
        if (typing) e.target.blur();
        return;
      }
      if (typing || e.defaultPrevented) return;
      const active = document.activeElement;
      const onRow = active?.closest?.('.qr');
      if (e.key === '/') {
        const f = document.querySelector('main [data-filter]');
        if (f) { e.preventDefault(); f.focus(); f.select(); }
        return;
      }
      if (e.key === 'j' || e.key === 'k') {
        if (document.querySelector('.drawer')) return;
        const items = [...document.querySelectorAll('main [data-nav]')].filter((el) => el.offsetParent !== null);
        if (!items.length) return;
        const cur = items.indexOf(active?.closest?.('[data-nav]'));
        const next = cur < 0 ? (e.key === 'j' ? 0 : items.length - 1) : Math.min(items.length - 1, Math.max(0, cur + (e.key === 'j' ? 1 : -1)));
        e.preventDefault();
        items[next].focus();
        items[next].scrollIntoView({ block: 'nearest' });
        return;
      }
      if (e.key === 'r') {
        if (document.querySelector('.drawer')) return;
        e.preventDefault();
        live.current(active?.closest?.('main [data-project]')?.dataset.project || null);
        return;
      }
      if (e.key === 'c' && !onRow) {
        const box = document.querySelector('.rv textarea');
        if (box) { e.preventDefault(); box.focus(); return; }
      }
      // Nothing focused: letters and Enter go to the open Inbox question, as if it had the focus.
      if ((active === document.body || !active) && (/^[a-z]$/.test(e.key) || e.key === 'Enter')) {
        const row = document.querySelector('main .qr.is-open');
        if (row) { e.preventDefault(); row.dispatchEvent(new CustomEvent('cairn-key', { detail: e.key })); }
      }
    };
    addEventListener('keydown', onKey);
    return () => removeEventListener('keydown', onKey);
  }, []);
}

/* ---------- app ---------- */

const VIEW_TITLES = { inbox: 'Inbox', projects: 'Projects', new: 'New project', nodes: 'Nodes', tokens: 'Tokens', settings: 'Settings' };

function App() {
  const route = useRoute();
  const now = useNow(5000);
  const [state, setState] = useState(null);
  const [fetchError, setFetchError] = useState(null);
  const [syncing, setSyncing] = useState(false);
  const [drawer, setDrawer] = useState(null);
  const [panelOpen, setPanelOpen] = useState(false);
  const [toasts, setToasts] = useState([]);
  const [notSignedIn, setNotSignedIn] = useState(signedOut);
  const lastFocus = useRef(null);
  useEffect(() => {
    signInListeners.add(setNotSignedIn);
    return () => signInListeners.delete(setNotSignedIn);
  }, []);

  const dismiss = useCallback((id) => setToasts((ts) => ts.filter((t) => t.id !== id)), []);
  const toast = useCallback((msg, tone = 'ok', detail = null) => {
    const id = Math.random().toString(36).slice(2);
    setToasts((ts) => [...ts.slice(-3), { id, msg, tone, detail }]);
    setTimeout(() => dismiss(id), tone === 'error' ? 10000 : 5000);
  }, [dismiss]);

  // Polls, syncs and post-action refreshes overlap: a state is applied only if no later-asked one was applied first,
  // so a poll sent before an answer cannot reopen the question when it lands after the post-answer refresh.
  const asked = useRef(0);
  const applied = useRef(0);
  const apply = (n, s) => {
    if (n < applied.current) return;
    applied.current = n;
    setState(s);
    setFetchError(null);
  };
  const refresh = useCallback(async () => {
    const n = ++asked.current;
    try {
      apply(n, await api.state());
    } catch (e) {
      if (n > applied.current) setFetchError(e.message);
    }
  }, []);
  const review = useReview(refresh, toast);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, POLL_MS);
    const vis = () => { if (document.visibilityState === 'visible') refresh(); };
    document.addEventListener('visibilitychange', vis);
    return () => { clearInterval(t); document.removeEventListener('visibilitychange', vis); };
  }, [refresh]);

  const sync = async () => {
    setSyncing(true);
    try {
      const s = await api.sync();
      // A sync's state is current when it returns, so it ranks with requests asked from then on.
      apply(++asked.current, s);
    } catch (e) {
      toast(`Sync failed: ${e.message}`, 'error');
    } finally { setSyncing(false); }
  };

  // The drawer and the review panel share the right edge: opening one closes the other. Closing the panel keeps the session.
  const openDrawer = useCallback((item) => {
    if (!drawer && !panelOpen) lastFocus.current = document.activeElement;
    setPanelOpen(false);
    setDrawer(item);
  }, [drawer, panelOpen]);
  const closeDrawer = useCallback(() => {
    setDrawer(null);
    lastFocus.current?.focus?.();
  }, []);
  const openChat = (target) => {
    if (!drawer && !panelOpen) lastFocus.current = document.activeElement;
    setDrawer(null);
    review.open(target);
    setPanelOpen(true);
    setTimeout(() => document.querySelector('.rv textarea')?.focus(), 0);
  };
  const closePanel = useCallback(() => {
    setPanelOpen(false);
    lastFocus.current?.focus?.();
  }, []);
  const openPath = useCallback((path, kind) => {
    setDrawer((d) => d && { node: d.node, project: d.project, path, kind });
  }, []);

  // r: the focused row's project (Inbox, Projects), else this project page's; nothing otherwise.
  const openReview = (key) => {
    const pick = key ? splitKey(key) : route.view === 'project' ? [route.node, route.project] : null;
    if (!pick) return;
    const node = state?.nodes.find((n) => n.name === pick[0]);
    if (!node?.ok) return;
    openChat({ node: pick[0], project: pick[1], resume: wantsResume(chatOf(state, pick[0], pick[1])) });
  };

  useEffect(() => { setDrawer(null); }, [route.view, route.node, route.project]);
  // A live session follows the operator across pages; an ended or failed one closes when they move to another project's page.
  useEffect(() => {
    const t = review.target;
    if (panelOpen && t && !review.live && !review.opening && route.view === 'project' && (route.node !== t.node || route.project !== t.project)) setPanelOpen(false);
  }, [route.view, route.node, route.project]);
  useKeys({ openReview });

  useEffect(() => {
    const open = allProjects(state).reduce((n, x) => n + openQs(x.p).length, 0);
    const where = route.view === 'project' ? route.project : VIEW_TITLES[route.view];
    document.title = open ? `(${open}) ${where} · Cairn` : `${where} · Cairn`;
  }, [state, route]);

  const unreachable = (state?.nodes || []).filter(isDown);
  const pageProps = { state, route, now, openDrawer, openChat, toast, refresh, review, panelOpen };

  return html`<div class=${'shell' + (panelOpen && review.target ? ' has-panel' : '')}>
    ${FIXTURE && html`<div class="fixture-banner" role="note"><b>Fixture mode: sample data, not your hub.</b> Nothing here is real and nothing you do reaches a node.
      <a href=${location.pathname}>Open the dashboard</a></div>`}
    <${Guard} retry=${state} render=${() => html`<${Sidebar} state=${state} route=${route} />`}
      fallback=${(e) => html`<aside class="side"><div class="side-inner"><p class="field-error">The sidebar could not be shown: ${errText(e)}</p></div></aside>`} />
    <div class="main">
      <header class="topbar">
        ${unreachable.map((n) => html`<span class="topbar-warn" key=${n.name}><span class="sdot" aria-hidden="true"></span>${n.name} unreachable</span>
          <span class="topbar-err" title=${n.error || ''}>${n.error || ''}</span>`)}
        <button class="sync-meta" onClick=${sync} disabled=${syncing || !state} title=${state ? `Hub synced ${clock(state.synced_at)}. Click to sync now.` : ''}>
          ${syncing ? 'Syncing…' : state ? `Synced ${ago(state.synced_at, now)}` : fetchError ? '' : 'Loading…'}
        </button>
      </header>
      ${notSignedIn && html`<div class="banner banner-error" role="alert">
        <b>This browser is not signed in to the hub.</b> Run <code>cairn open</code> in a terminal on the Mac: it opens the
        dashboard with a link that signs this browser in. Until then nothing here is current and every action is refused.
      </div>`}
      ${fetchError && !notSignedIn && html`<div class="banner banner-error" role="alert">
        <b>Can't load state from the hub:</b> ${fetchError}. ${state ? `Showing data from ${clock(state.synced_at)}; ` : ''}retrying every ${POLL_MS / 1000} s.
      </div>`}
      <main>
        ${!state
          ? (fetchError ? null : html`<div class="page"><div class="skeleton skeleton-page"><span></span><span></span><span></span><span></span></div></div>`)
          : html`<${Guard} key=${`${route.view}/${route.node || ''}/${route.project || ''}`} retry=${state}
            fallback=${(e) => html`<div class="page"><div class="banner banner-error" role="alert"><b>This page could not be shown:</b> ${errText(e)}. It is tried again at the next sync.</div></div>`}
            render=${() => (route.view === 'project' ? html`<${ProjectPage} key=${route.node + '/' + route.project} ...${pageProps} />`
              : route.view === 'projects' ? html`<${ProjectsPage} ...${pageProps} />`
              : route.view === 'new' ? html`<${NewProjectPage} key=${route.node || ''} ...${pageProps} />`
              : route.view === 'nodes' ? html`<${NodesPage} ...${pageProps} />`
              : route.view === 'tokens' ? html`<${TokensPage} ...${pageProps} />`
              : route.view === 'settings' ? html`<${SettingsPage} ...${pageProps} />`
              : html`<${Inbox} ...${pageProps} />`)} />`}
      </main>
    </div>
    ${drawer?.kind === 'dialog' && html`<${Guard} key="dialog" retry=${state} fallback=${(e) => html`<${PanelError} error=${e} onClose=${closeDrawer} />`}
      render=${() => html`<${DialogDrawer} item=${drawer} onClose=${closeDrawer} toast=${toast} refresh=${refresh} />`} />`}
    ${drawer && drawer.kind !== 'dialog' && html`<${Guard} key="drawer" retry=${state} fallback=${(e) => html`<${PanelError} error=${e} onClose=${closeDrawer} />`}
      render=${() => html`<${Drawer} item=${drawer} state=${state} onClose=${closeDrawer} onOpen=${openPath} now=${now} toast=${toast} refresh=${refresh} />`} />`}
    ${panelOpen && review.target && html`<${Guard} key=${review.target.node + '/' + review.target.project} retry=${state}
      fallback=${(e) => html`<${PanelError} error=${e} onClose=${closePanel} />`}
      render=${() => html`<${ReviewPanel} review=${review} state=${state} onClose=${closePanel} openChat=${openChat} onOpenDrawer=${openDrawer} />`} />`}
    <${Toasts} items=${toasts} dismiss=${dismiss} />
  </div>`;
}

render(html`<${App} />`, document.getElementById('app'));
