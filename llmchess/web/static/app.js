/* llm-chess spectator client.
 *
 * Reads the arbiter's store through the local API and renders it live. It holds
 * no chess logic of its own: every position, clock and result shown here was
 * written by the arbiter, so what you see is the same thing the players see.
 */

const PIECES = {
  K: 'wK', Q: 'wQ', R: 'wR', B: 'wB', N: 'wN', P: 'wP',
  k: 'bK', q: 'bQ', r: 'bR', b: 'bB', n: 'bN', p: 'bP',
};

let state = null;
let socket = null;
let clockBase = null;   // {white_ms, black_ms, at, turn, running}
let currentGame = null;

const $ = (id) => document.getElementById(id);

/* ------------------------------------------------------------------ board */

function buildBoardChrome() {
  const files = ['a','b','c','d','e','f','g','h'];
  const ranks = ['8','7','6','5','4','3','2','1'];
  for (const id of ['files-top','files-bottom']) {
    $(id).innerHTML = files.map(f => `<span>${f}</span>`).join('');
  }
  for (const id of ['ranks-left','ranks-right']) {
    $(id).innerHTML = ranks.map(r => `<span>${r}</span>`).join('');
  }
  const board = $('board');
  board.innerHTML = '';
  for (let r = 0; r < 8; r++) {
    for (let f = 0; f < 8; f++) {
      const sq = document.createElement('div');
      sq.className = 'sq ' + ((r + f) % 2 === 0 ? 'light' : 'dark');
      sq.dataset.square = files[f] + ranks[r];
      board.appendChild(sq);
    }
  }
}

function renderPosition(pos) {
  const board = $('board');
  const squares = board.children;
  const rows = (pos.fen || '').split(' ')[0].split('/');
  const last = pos.last_move;

  let i = 0;
  for (let r = 0; r < 8; r++) {
    let f = 0;
    for (const ch of (rows[r] || '')) {
      if (/\d/.test(ch)) {
        for (let n = 0; n < Number(ch); n++) { setSquare(squares[i++], null, null, last, null); f++; }
      } else {
        setSquare(squares[i++], ch, null, last, null);
        f++;
      }
    }
    while (f < 8) { setSquare(squares[i++], null, null, last, null); f++; }
  }

  // highlight the king in check
  if (pos.is_check) {
    const target = pos.turn === 'white' ? 'K' : 'k';
    for (const sq of squares) {
      if (sq.dataset.piece === target) sq.classList.add('check');
    }
  }
}

function setSquare(el, pieceChar, files, last, isCheck) {
  el.className = 'sq ' + (el.classList.contains('light') ? 'light' : 'dark');
  el.innerHTML = '';
  el.dataset.piece = pieceChar || '';
  if (pieceChar) {
    const d = document.createElement('div');
    d.className = 'piece';
    d.style.backgroundImage = `url(/static/pieces/${PIECES[pieceChar]}.svg)`;
    el.appendChild(d);
  }
  if (last) {
    if (el.dataset.square === last.from || el.dataset.square === last.to) el.classList.add('last');
  }
}

/* ------------------------------------------------------------------- HUD */

function fmtClock(ms) {
  if (ms === null || ms === undefined) return '--:--';
  const neg = ms < 0;
  const total = Math.max(0, Math.round(ms / 1000));
  const m = Math.floor(total / 60);
  const s = total % 60;
  return (neg ? '-' : '') + `${m}:${String(s).padStart(2, '0')}`;
}

function clockClass(ms) {
  if (ms === null || ms === undefined) return '';
  if (ms < 10_000) return 'critical';
  if (ms < 60_000) return 'low';
  return '';
}

function renderClocks() {
  const clk = state && state.clock;
  const pos = state && state.position;
  for (const color of ['white', 'black']) {
    const el = document.querySelector(`.clock[data-clock="${color}"]`);
    if (!el) continue;
    if (!clk) { el.textContent = '--:--'; el.className = 'clock'; continue; }
    let ms = clk[`${color}_ms`];
    if (clk.running && pos && pos.turn === color) {
      ms -= (Date.now() - clk.at * 1000);
    }
    el.textContent = fmtClock(ms);
    el.className = 'clock ' + clockClass(ms);
  }
  for (const color of ['white', 'black']) {
    const strip = $(`strip-${color}`);
    if (strip) strip.classList.toggle('active', !!(state && state.position && state.position.turn === color && state.game.status !== 'finished'));
  }
}

function renderPlayers() {
  if (!state) return;
  const g = state.game;
  const w = $('strip-white'), b = $('strip-black');
  w.querySelector('.player-name').textContent = g.white_client;
  b.querySelector('.player-name').textContent = g.black_client;
  const turn = state.position ? state.position.turn : null;
  w.querySelector('.player-status').textContent = g.status === 'finished' ? '—' : (turn === 'white' ? 'to move' : 'waiting');
  b.querySelector('.player-status').textContent = g.status === 'finished' ? '—' : (turn === 'black' ? 'to move' : 'waiting');

  $('game-pill').textContent = g.id;
  const pill = $('status-pill');
  if (g.status === 'finished') {
    pill.textContent = `finished · ${g.result || '*'}${g.result_reason ? ' · ' + g.result_reason : ''}`;
    pill.className = 'pill ok';
  } else if (g.status === 'active') {
    pill.textContent = `in play · move ${Math.floor(g.ply / 2) + 1}${state.position && state.position.is_check ? ' · check' : ''}`;
    pill.className = 'pill live';
  } else {
    pill.textContent = 'waiting to start';
    pill.className = 'pill';
  }
  $('ply-hint').textContent = g.ply ? `${g.ply} plies` : 'no moves yet';
}

function renderCaptured() {
  const cap = state && state.position && state.position.captured;
  if (!cap) return;
  const order = { q: 0, r: 1, b: 2, n: 3, p: 4 };
  const sym = { q: 'Q', r: 'R', b: 'B', n: 'N', p: 'P' };
  const build = (list, color) => list
    .slice()
    .sort((a, b) => (order[a] ?? 9) - (order[b] ?? 9))
    .map(s => `<img src="/static/pieces/${color}${sym[s]}.svg" alt="${sym[s]}">`)
    .join('');
  $('cap-white').innerHTML = build(cap.white || [], 'b');   // white captured black's pieces
  $('cap-black').innerHTML = build(cap.black || [], 'w');
}

function renderMoves() {
  const list = $('movelist');
  if (!state || !state.moves.length) {
    list.innerHTML = '<li class="empty" style="display:block">no moves yet</li>';
    return;
  }
  const rows = [];
  for (let i = 0; i < state.moves.length; i += 2) {
    const w = state.moves[i];
    const b = state.moves[i + 1];
    const cls = (m) => `san by-${m.client}${i + (b ? 1 : 0) >= state.moves.length - 1 ? ' just' : ''}`;
    rows.push(`<li><span class="no">${Math.floor(i / 2) + 1}.</span>` +
      `<span class="${cls(w)}">${w.san}</span>` +
      (b ? `<span class="${cls(b)}">${b.san}</span>` : '<span></span>') + '</li>');
  }
  list.innerHTML = rows.join('');
  list.scrollTop = list.scrollHeight;
}

function renderAgents() {
  const box = $('agents');
  if (!state || !state.agents.length) { box.innerHTML = '<div class="empty">no agents registered</div>'; return; }
  box.innerHTML = state.agents.map(a => `
    <div class="agent-card">
      <div class="row1">
        <span class="name">${escapeHtml(a.client)}</span>
        <span class="color-tag">${a.color || 'spectator'}</span>
        <span class="stat ${a.status}">${a.status}</span>
      </div>
      <div class="meta">${a.session_id ? 'session ' + escapeHtml(a.session_id.slice(0, 18)) : 'no session yet'}${
        a.model ? ' · ' + escapeHtml(a.model) : ''}</div>
      ${a.last_error ? `<div class="err">${escapeHtml(a.last_error)}</div>` : ''}
    </div>`).join('');
}

function renderFeed(events) {
  const feed = $('feed');
  const items = (events || []).filter(e => e.kind !== 'agent_message');
  if (!items.length) { feed.innerHTML = '<div class="empty">nothing yet</div>'; return; }
  feed.innerHTML = items.slice(-80).map(e => `
    <div class="feed-item k-${e.kind}">
      <span class="ts">${timeOf(e.ts)}</span>
      <span class="txt">${escapeHtml(e.kind.replace(/_/g, ' '))}${e.client ? ' · ' + escapeHtml(e.client) : ''}${
        e.text ? ' — ' + escapeHtml(String(e.text).slice(0, 400)) : ''}</span>
    </div>`).join('');
  feed.scrollTop = feed.scrollHeight;
}

function renderTranscripts() {
  const box = $('transcripts');
  const clients = state ? [state.game.white_client, state.game.black_client] : [];
  const events = (state && state.events) || [];
  box.innerHTML = clients.map(c => {
    const msgs = events.filter(e => e.kind === 'agent_message' && e.client === c).slice(-12);
    const body = msgs.length
      ? msgs.map(m => `<div class="texchange"><div class="tmove">${escapeHtml((m.data && m.data.move) || '')}</div><pre>${escapeHtml(String(m.text || ''))}</pre></div>`).join('')
      : '<div class="texchange empty">nothing said yet</div>';
    const color = state && state.game.white_client === c ? 'white' : 'black';
    return `<div class="tcol"><div class="tcol-head"><span>${escapeHtml(c)}</span><span class="c">${color}</span></div><div class="tcol-body">${body}</div></div>`;
  }).join('');
}

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function timeOf(ts) {
  const d = new Date(ts * 1000);
  return d.toTimeString().slice(0, 8);
}

function render() {
  if (!state) return;
  renderPosition(state.position);
  renderPlayers();
  renderClocks();
  renderCaptured();
  renderMoves();
  renderAgents();
  renderFeed(state.events);
  renderTranscripts();
}

/* --------------------------------------------------------------- socket */

function connect(gameId) {
  if (socket) { try { socket.close(); } catch (e) { /* ignore */ } }
  currentGame = gameId;
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  socket = new WebSocket(`${proto}://${location.host}/ws/game/${gameId}`);
  socket.onopen = () => { $('link-dot').className = 'dot live'; };
  socket.onclose = () => { $('link-dot').className = 'dot dead'; setTimeout(bootstrap, 1500); };
  socket.onerror = () => { $('link-dot').className = 'dot dead'; };
  socket.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === 'state') { state = msg.state; render(); }
  };
}

async function bootstrap() {
  try {
    const r = await fetch('/api/active');
    const j = await r.json();
    if (j.game) { state = j; render(); connect(j.game.id); }
    else { $('status-pill').textContent = 'no active game'; $('link-dot').className = 'dot'; setTimeout(bootstrap, 2000); }
  } catch (e) {
    $('link-dot').className = 'dot dead';
    setTimeout(bootstrap, 1500);
  }
}

/* ------------------------------------------------------------------ UI */

$('btn-new').onclick = () => { $('modal').hidden = false; };
$('new-cancel').onclick = () => { $('modal').hidden = true; };
$('btn-pgn').onclick = () => {
  if (currentGame) window.open(`/api/game/${currentGame}/pgn`, '_blank');
};

$('new-start').onclick = async () => {
  const tc = $('new-tc').value;
  let initial_ms = null, increment_ms = 0;
  if (tc) { const [m, i] = tc.split('+').map(Number); initial_ms = m * 1000; increment_ms = i * 1000; }
  const body = {
    white: $('new-white').value,
    black: $('new-black').value,
    time_control: tc || null,
    initial_ms, increment_ms,
    autostart: $('new-autostart').checked,
  };
  $('new-start').disabled = true;
  try {
    const r = await fetch('/api/game', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const j = await r.json();
    $('modal').hidden = true;
    if (j.game_id) { currentGame = j.game_id; connect(j.game_id); }
  } catch (e) {
    alert('could not start game: ' + e);
  } finally {
    $('new-start').disabled = false;
  }
};

buildBoardChrome();
bootstrap();
setInterval(renderClocks, 200);   // clocks tick locally between server updates