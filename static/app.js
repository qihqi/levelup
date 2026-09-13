'use strict';
const $ = id => document.getElementById(id);
const compass = ['南', '东', '北', '西'];
const symbols = {S: '♠', H: '♥', C: '♣', D: '♦'};
const phases = {lobby: '等待入座', dealing: '摸牌 · 随时亮主', burying: '庄家扣底', playing: '出牌', round_end: '本局结束', match_end: '比赛结束'};
let state = null, socket = null, selected = new Set(), roomCode = '', token = '', connecting = false;
let manualClose = false, retryTimer = null, toastTimer = null, shownResult = '', pending = false, deadline = 0;
let hintPendingVersion = null;
let dealDeadline = 0, freshCards = new Set(), handFingerprint = '';
let connectTimer = null, reconnectAttempts = 0, requestGeneration = 0;
let forcedTimer = null, forcedTimerKey = '', forcedAttempt = '';
const FORCED_PLAY_DELAY = 900;
const safeStorage = {
  get(k) { try { return sessionStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { sessionStorage.setItem(k, v); } catch {} },
  remove(k) { try { sessionStorage.removeItem(k); } catch {} }
};
function text(tag, value, className = '') {
  const node = document.createElement(tag); node.textContent = value; node.className = className; return node;
}
function toast(message) {
  $('toast').textContent = message; $('toast').hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { $('toast').hidden = true; }, 4500);
}
function nickname() {
  const name = $('name').value.trim();
  if (!name) { $('name').focus(); toast('先填写你的昵称。'); return null; }
  safeStorage.set('levelup:name', name); return name;
}
function busy(value) {
  connecting = value; $('create').disabled = value; $('join').disabled = value;
  $('cancel-connect').hidden = !value;
}
function returnToLobby(message = '', failedRoom = roomCode, invalidateToken = false) {
  // Discard this socket before closing it: late messages cannot repaint the room.
  requestGeneration += 1;
  const previousSocket = socket; socket = null; manualClose = true;
  clearTimeout(retryTimer); clearTimeout(connectTimer); clearTimeout(toastTimer);
  cancelForcedPlay(); forcedAttempt = '';
  previousSocket?.close();
  if (invalidateToken && failedRoom) safeStorage.remove(`levelup:${failedRoom}`);
  state = null; roomCode = ''; token = ''; pending = false; hintPendingVersion = null; reconnectAttempts = 0;
  selected.clear(); freshCards.clear(); handFingerprint = ''; shownResult = '';
  deadline = 0; dealDeadline = 0; busy(false);
  if ($('info-dialog').open) $('info-dialog').close();
  if ($('result-dialog').open) $('result-dialog').close();
  if ($('previous-dialog').open) $('previous-dialog').close();
  document.body.classList.remove('in-game'); $('table').classList.remove('is-playing');
  $('seats').replaceChildren(); $('hand').replaceChildren(); $('waiting').replaceChildren();
  $('trick-area').replaceChildren();
  for (const id of ['waiting', 'trick-area', 'table-caption', 'hand-section', 'room-tools', 'ai-panel', 'ai-hint', 'mobile-status', 'info-button', 'toast']) $(id).hidden = true;
  $('lobby').hidden = false; $('room-error').hidden = !message; $('room-error').textContent = message;
  $('page-title').textContent = message ? '换一桌，随时开局。' : '好牌，和朋友一起打。';
  $('connection').textContent = '大厅'; $('connection').className = 'connection';
  $('join-code').value = failedRoom || '';
  $('round-label').textContent = '等待开局'; $('level-0').textContent = '2'; $('level-1').textContent = '2';
  $('score').textContent = '0'; $('score-fill').style.width = '0%';
  document.querySelector('.meter').setAttribute('aria-valuenow', '0');
  document.querySelector('.meter').setAttribute('aria-valuetext', '0 分');
  $('score-note').textContent = '拿到 80 分，换你上庄。';
  $('current-level').textContent = '2'; $('current-trump').textContent = '待亮主';
  $('events').replaceChildren(text('li', '创建新房间，或使用完整邀请链接加入。'));
  history.replaceState(null, '', '/');
}
function send(action, extra = {}) {
  if (!state || !socket || socket.readyState !== WebSocket.OPEN) return toast('正在连接，请稍候。');
  if (pending) return;
  pending = true;
  socket.send(JSON.stringify({action, version: state.version, deal_id: state.deal_id, ...extra}));
  updateActions();
}
async function createRoom(event) {
  event.preventDefault(); if (connecting) return;
  const name = nickname(); if (!name) return;
  const generation = ++requestGeneration;
  busy(true); reconnectAttempts = 0; $('room-error').hidden = true;
  try {
    const ai_strategy = $('create-strategy').value || undefined;
    const response = await fetch('/api/rooms', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({name, ai_strategy}), signal: AbortSignal.timeout(10000)});
    const result = await response.json();
    if (generation !== requestGeneration) return;
    if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : '创建失败，请重试。');
    token = result.token; roomCode = result.room;
    safeStorage.set(`levelup:${roomCode}`, token);
    connect();
  } catch (e) {
    if (generation !== requestGeneration) return;
    toast(e.name === 'TimeoutError' ? '创建房间超时，请重试。' : e.message); busy(false);
  }
}
function joinRoom() {
  if (connecting || !nickname()) return;
  const code = $('join-code').value.trim().toUpperCase();
  if (!/^[A-Z2-9]{6}$/.test(code)) return toast('请输入 6 位房间号。');
  roomCode = code; token = safeStorage.get(`levelup:${code}`) || ''; reconnectAttempts = 0;
  $('room-error').hidden = true; busy(true); connect();
}
function connect() {
  clearTimeout(retryTimer); clearTimeout(connectTimer); manualClose = false; busy(true);
  $('connection').textContent = '连接中…'; $('connection').className = 'connection';
  const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/${roomCode}`);
  let connectionError = '';
  socket = ws;
  connectTimer = setTimeout(() => {
    if (socket === ws) returnToLobby('连接房间超时。可以重新加入，或点击「创建房间」开始新游戏。');
  }, 12000);
  ws.onopen = () => { if (socket === ws) ws.send(JSON.stringify({name: $('name').value.trim() || safeStorage.get('levelup:name') || '牌友', token})); };
  ws.onmessage = event => {
    if (socket !== ws) return;
    const message = JSON.parse(event.data);
    if (message.type === 'welcome') {
      token = message.token; safeStorage.set(`levelup:${roomCode}`, token);
      history.replaceState(null, '', `/?room=${roomCode}`);
      $('connection').textContent = '已连接'; $('connection').className = 'connection online';
    } else if (message.type === 'state') {
      clearTimeout(connectTimer); reconnectAttempts = 0; busy(false);
      pending = false;
      if (hintPendingVersion !== message.version) hintPendingVersion = null;
      const previous = state;
      state = message; deadline = Date.now() + state.seconds_left * 1000;
      dealDeadline = Date.now() + (state.deal_seconds_left || 0) * 1000;
      const priorCards = new Set(previous?.hand.map(c => c.id) || []);
      freshCards = new Set(state.phase === 'dealing' && previous?.deal_id === state.deal_id
        ? state.hand.filter(c => !priorCards.has(c.id)).map(c => c.id) : []);
      const handChanged = state.hand.length !== previous?.hand.length || freshCards.size;
      if (handChanged || previous?.phase !== state.phase || JSON.stringify(previous?.bid) !== JSON.stringify(state.bid)) $('ai-hint').hidden = true;
      const owned = new Set(state.hand.map(c => c.id));
      selected = new Set([...selected].filter(id => owned.has(id)));
      // During dealing, turn identifies the next recipient, not who may bid.
      // Keep selections by physical card ID as new cards arrive and sort around them.
      if (previous && (previous.phase !== state.phase || previous.round !== state.round ||
          previous.deal_id !== state.deal_id || previous.seat !== state.seat ||
          (state.phase !== 'dealing' && (previous.turn !== state.turn || previous.trick_number !== state.trick_number)))) selected.clear();
      render();
    } else if (message.type === 'error') {
      connectionError = message.message;
      pending = false; hintPendingVersion = null; toast(message.message); updateActions();
    } else if (message.type === 'hint_pending') {
      pending = false; hintPendingVersion = message.version;
      toast('AI 正在思考出牌，仍可手动选牌和出牌。'); updateActions();
    } else if (message.type === 'hint') {
      const wasSearching = hintPendingVersion !== null;
      hintPendingVersion = null;
      if (wasSearching && pending) { updateActions(); return; }
      pending = false;
      if (message.version === state?.version || (state?.phase === 'dealing' && message.deal_id === state.deal_id)) {
        selected = new Set(message.ids); renderHand(); updateActions();
        renderHint(message);
        if (message.action === 'pass') toast(state.deal_closing ? `建议保持当前亮主，可点击「${bidPassLabel()}」确认。` : '建议暂不亮主，继续摸牌。');
      }
    }
  };
  ws.onclose = event => {
    if (socket !== ws || manualClose) return;
    clearTimeout(connectTimer); pending = false; updateActions();
    $('connection').textContent = '连接中断'; $('connection').className = 'connection';
    if ([4001, 4003, 4004, 1008].includes(event.code)) {
      const reason = event.code === 4004
        ? `房间 ${roomCode} 不在当前服务器或已过期。请使用原来的完整邀请链接（包括端口），或点击「创建房间」开始新游戏。`
        : event.code === 4001 ? '此座位已在另一连接恢复。你可以创建新房间，或重新加入。'
        : `${connectionError || '无法加入此房间。'} 你可以点击「创建房间」开始新游戏。`;
      returnToLobby(reason, roomCode, event.code === 4003 || event.code === 4004); return;
    }
    if (++reconnectAttempts >= 3) {
      returnToLobby('暂时无法连接房间。你可以重新加入，或点击「创建房间」开始新游戏。'); return;
    }
    busy(true);
    $('connection').textContent = '正在重连…'; retryTimer = setTimeout(connect, 1800);
  };
  ws.onerror = () => { if (socket === ws) $('connection').textContent = '连接失败，重试中…'; };
}
function effectiveSuit(card) {
  const level = {J: 11, Q: 12, K: 13, A: 14}[state?.level] || Number(state?.level);
  return card.suit === 'J' || card.rank === level || card.suit === state?.trump ? 'T' : card.suit;
}
function suitName(suit) { return {T: '主牌', S: '♠黑桃', H: '♥红桃', C: '♣梅花', D: '♦方块'}[suit]; }
function followPrompt() {
  if (!state?.trick.length) return '请领出新一墩';
  const lead = state.trick[0].cards, suit = effectiveSuit(lead[0]);
  const count = lead.length, available = state.hand.filter(c => effectiveSuit(c) === suit).length;
  if (!available) return `缺${suitName(suit)}，出 ${count} 张`;
  if (available < count) return `跟${suitName(suit)} ${available} 张，补 ${count - available} 张`;
  return `请跟${suitName(suit)} ${count} 张`;
}
function playGuidance() {
  const options = state?.play_options;
  if (!options || state.phase !== 'playing' || state.turn !== state.seat) return null;
  const pool = new Set(options.pool);
  if ([...selected].some(id => !pool.has(id))) return {allowed: new Set(), valid: false};
  if (options.count === null) {
    const suits = new Set(state.hand.filter(c => selected.has(c.id)).map(effectiveSuit));
    const allowed = new Set(state.hand.filter(c => !suits.size || (suits.size === 1 && suits.has(effectiveSuit(c)))).map(c => c.id));
    return {allowed, valid: selected.size > 0 && suits.size === 1};
  }
  const allowed = new Set();
  let valid = false;
  for (const required of options.required) {
    const combined = new Set([...required, ...selected]);
    if (combined.size > options.count) continue;
    // Any remaining slot can be filled from the pool. At capacity, only
    // the cards completing this witness can still be selected.
    for (const id of combined.size < options.count ? pool : combined) allowed.add(id);
    if (selected.size === options.count) valid = true;
  }
  return {allowed, valid};
}
function updateHandAvailability(guidance) {
  if (!state?.hand) return;
  const active = ['dealing', 'burying', 'playing'].includes(state.phase);
  const mine = state.phase === 'dealing' || state.turn === state.seat;
  const available = active && mine && socket?.readyState === WebSocket.OPEN && !pending;
  for (const [i, card] of state.hand.entries()) {
    const node = $('hand').children[i];
    if (!node) continue;
    const blocked = guidance && !guidance.allowed.has(card.id) && !selected.has(card.id);
    node.disabled = !available || Boolean(blocked);
    node.classList.toggle('unplayable', Boolean(blocked));
    node.classList.toggle('selected', selected.has(card.id));
    node.setAttribute('aria-pressed', selected.has(card.id));
    node.setAttribute('aria-label', `${effectiveSuit(card) === 'T' ? '主牌 ' : ''}${card.symbol} ${card.label}${blocked ? '，不能加入当前出牌' : selected.has(card.id) ? '，已选' : ''}`);
  }
}
function cancelForcedPlay() {
  clearTimeout(forcedTimer); forcedTimer = null; forcedTimerKey = '';
}
function forcedPlayKey() {
  if (!state || state.phase !== 'playing' || state.turn !== state.seat ||
      !state.play_options?.forced?.length || state.players?.[state.seat]?.auto ||
      !$('auto-forced').checked || pending || socket?.readyState !== WebSocket.OPEN) return '';
  return JSON.stringify([roomCode, state.deal_id, state.round, state.version, state.seat]);
}
function syncForcedPlay() {
  const key = forcedPlayKey();
  if (!key || key === forcedAttempt) { cancelForcedPlay(); return; }
  if (key === forcedTimerKey) return;
  cancelForcedPlay(); forcedTimerKey = key;
  forcedTimer = setTimeout(() => {
    if (forcedTimerKey !== key) return;
    if (forcedPlayKey() !== key) { cancelForcedPlay(); return; }
    cancelForcedPlay(); forcedAttempt = key;
    selected = new Set(state.play_options.forced);
    renderHand();
    send('play', {ids: [...selected]});
  }, FORCED_PLAY_DELAY);
}
function cardNode(card, mini = false, selectable = false) {
  const node = document.createElement(selectable ? 'button' : 'div');
  const isTrump = effectiveSuit(card) === 'T';
  node.className = `card suit-${card.suit}${mini ? ' mini' : ''}${card.suit === 'H' || card.rank === 16 ? ' red' : ''}${card.suit === 'J' ? ' joker' : ''}${isTrump ? ' trump' : ''}${selected.has(card.id) && selectable ? ' selected' : ''}`;
  node.title = `${isTrump ? '主牌 · ' : ''}${card.symbol} ${card.label}`;
  node.append(text('span', card.label, 'card-rank'));
  if (card.suit !== 'J') node.append(text('span', card.symbol, 'card-suit'));
  node.append(text('span', card.suit === 'J' ? '王' : card.symbol, 'card-center'));
  if (selectable) {
    node.type = 'button'; node.setAttribute('aria-pressed', selected.has(card.id));
    node.setAttribute('aria-label', `${card.group === 'T' ? '主牌 ' : ''}${card.symbol} ${card.label}${selected.has(card.id) ? '，已选' : ''}`);
    node.disabled = !['dealing', 'burying', 'playing'].includes(state.phase);
    if (freshCards.has(card.id)) node.classList.add('just-dealt');
    node.onclick = () => {
      if (node.disabled) return;
      $('ai-hint').hidden = true;
      if (selected.has(card.id)) selected.delete(card.id); else selected.add(card.id);
      node.classList.toggle('selected', selected.has(card.id)); node.setAttribute('aria-pressed', selected.has(card.id));
      updateActions();
    };
  }
  return node;
}
function nameOf(seat) { return state.players[seat].name; }
function teamOf(seat) { return seat % 2 === state.seat % 2 ? 'ally' : 'opponent'; }
function roleOf(seat) { return seat === state.seat ? '你' : teamOf(seat) === 'ally' ? '队友' : '对手'; }
function renderSeats() {
  const container = $('seats'); container.replaceChildren();
  const positions = ['south', 'east', 'north', 'west'];
  for (const player of state.players) {
    const relative = (player.seat - state.seat + 4) % 4;
    const active = state.turn === player.seat && ['dealing', 'burying', 'playing'].includes(state.phase) && !(state.phase === 'dealing' && state.dealt === 100);
    const seat = text('div', '', `seat ${positions[relative]} ${teamOf(player.seat)}${active ? ' active' : ''}`);
    seat.append(text('div', compass[player.seat], 'avatar'));
    const label = text('div', '');
    const name = text('div', `${player.name}${player.seat === state.seat ? ' · 你' : ''}`, 'seat-name');
    name.title = player.name;
    if (state.phase !== 'lobby' && !(state.phase === 'dealing' && state.round === 1) && player.seat === state.dealer) name.append(text('span', '庄', 'dealer-badge'));
    label.append(text('span', roleOf(player.seat), 'team-badge'), name);
    let status = state.phase === 'lobby' ? (player.human ? '已入座' : 'AI 补位') : `${state.counts[player.seat]} 张牌`;
    if (player.human && !player.connected) status += ' · 离线';
    else if (player.human && player.auto) status += ' · 托管';
    label.append(text('div', status, 'seat-info'));
    if (!player.human && state.phase === 'lobby') {
      const move = text('button', '坐这里', 'empty-seat'); move.onclick = () => send('seat', {seat: player.seat}); label.append(move);
    }
    seat.append(label); container.append(seat);
  }
}
function render() {
  const inGame = state.phase !== 'lobby';
  document.body.classList.toggle('in-game', inGame);
  $('info-button').hidden = !inGame; $('mobile-status').hidden = !inGame;
  $('mobile-status').replaceChildren(
    text('span', `打 ${state.level} · ${state.phase === 'dealing' && !state.bid ? '待亮主' : symbols[state.trump] || '无主'}`),
    text('span', `闲家 ${state.score} / 80 分`),
    text('span', '● 我方', 'ally-key'), text('span', '◆ 对手', 'opponent-key'));
  $('lobby').hidden = true; $('room-tools').hidden = false; $('room-code').textContent = roomCode;
  $('page-title').textContent = state.phase === 'lobby' ? '朋友就位，随时开局。' : `第 ${state.round} 局 · ${phases[state.phase]}`;
  $('round-label').textContent = state.round ? `第 ${state.round} 局` : '等待开局';
  $('level-0').textContent = state.levels[0]; $('level-1').textContent = state.levels[1];
  $('score').textContent = state.score; $('score-fill').style.width = `${Math.min(100, state.score / 80 * 100)}%`;
  document.querySelector('.meter').setAttribute('aria-valuenow', Math.min(80, state.score));
  document.querySelector('.meter').setAttribute('aria-valuetext', `${state.score} 分`);
  $('current-level').textContent = state.level;
  $('current-trump').textContent = state.phase === 'lobby' || (state.phase === 'dealing' && !state.bid) ? '待亮主' : symbols[state.trump] || '无主';
  $('score-note').textContent = state.phase === 'lobby' ? '拿到 80 分，换你上庄。' : `${state.dealer % 2 === 0 ? '南北' : '东西'}守庄 · ${state.dealer % 2 === 0 ? '东西' : '南北'}抓分`;
  $('events').replaceChildren(...state.events.slice().reverse().map(e => text('li', e)));
  if (state.phase === 'dealing' && state.round === 1) $('score-note').textContent = '首局抢庄 · 最后亮主者坐庄';
  renderSeats(); renderTable(); renderHand(false); renderAI(); updateActions();
  if (state.result) renderResult();
  if (state.result && shownResult !== `${state.round}:${state.phase}`) {
    shownResult = `${state.round}:${state.phase}`; $('result-dialog').showModal();
  } else if (!state.result && $('result-dialog').open) $('result-dialog').close();
}
function renderTable() {
  const waiting = $('waiting'); waiting.replaceChildren();
  const isPlay = state.phase === 'playing';
  $('table').classList.toggle('is-playing', isPlay);
  waiting.hidden = isPlay;
  $('trick-area').hidden = !isPlay;
  $('table-caption').hidden = !isPlay;
  if (state.phase === 'lobby') {
    waiting.append(text('span', `${state.players.filter(p => p.human).length} / 4 位真人已入座`, 'phase-badge'));
    waiting.append(text('h2', '对面的人，是你的队友。'));
    waiting.append(text('p', '复制房间邀请，等朋友一起入座。空位会由 AI 自动补齐。'));
    if (state.timer_options) {
      const controls = text('div', '', 'start-timer');
      const label = text('label', '操作时限'); label.htmlFor = 'start-timer';
      const select = document.createElement('select'); select.id = 'start-timer';
      select.setAttribute('aria-label', '开局操作时限'); fillTimers(select);
      select.onchange = () => send('timer', {seconds: select.value === 'none' ? null : Number(select.value)});
      controls.append(label, select); waiting.append(controls);
    }
    const button = text('button', state.host === state.seat ? '开始游戏' : '等待房主开始', 'primary');
    button.disabled = state.host !== state.seat; button.onclick = () => send('start'); waiting.append(button);
  } else if (state.phase === 'dealing') {
    waiting.append(text('span', `打 ${state.level} · 已摸 ${state.dealt} / ${state.deal_total} 张`, 'phase-badge'));
    waiting.append(text('h2', state.bid ? `${symbols[state.trump] || '无主'} 已亮` : '边摸牌，边亮主。'));
    const progress = document.createElement('progress'); progress.className = 'deal-progress';
    progress.max = state.deal_total; progress.value = state.dealt; progress.setAttribute('aria-label', '全桌摸牌进度');
    waiting.append(progress);
    if (state.bid) {
      const cards = text('div', '', 'bottom-cards'); cards.append(...state.bid.cards.map(c => cardNode(c, true))); waiting.append(cards);
      waiting.append(text('p', `${nameOf(state.bid.seat)} 亮主 · 仍可加固或反主`));
    } else waiting.append(text('p', `摸到级牌 ${state.level} 即可亮主，一对王可亮无主。`));
    waiting.append(text('p', state.deal_closing
      ? `底牌暂不发放，可亮主 / 反主或点击「${bidPassLabel()}」。\n${state.bid_passed?.length || 0} / 4 人已确认 · 全员确认或倒计时结束后定主、发底牌。`
      : '每 0.5 秒全桌摸一张 · 每人 25 张\n无需等轮次，选中手里的级牌随时亮主。', 'deal-message'));
  } else if (state.phase === 'burying') {
    waiting.append(text('span', `${symbols[state.trump] || '无主'} · 打 ${state.level}`, 'phase-badge'));
    waiting.append(text('h2', '八张底牌，藏好这一手。'));
    waiting.append(text('p', state.seat === state.dealer ? '你已收取 8 张底牌。请从 33 张手牌中选出 8 张扣底。' : `庄家 ${nameOf(state.dealer)} 正在扣底。`));
  } else if (isPlay) {
    const plays = state.trick;
    const area = $('trick-area'); area.replaceChildren();
    // Cards stay in consistent grid positions relative to the local player's seat.
    for (const offset of [2, 1, 3, 0]) {
      const seat = (state.seat + offset) % 4;
      const play = plays.find(p => p.seat === seat);
      const led = plays[0]?.seat === seat;
      const position = ['south', 'east', 'north', 'west'][offset];
      const box = text('div', '', `trick-play ${position} ${teamOf(seat)}${state.turn === seat ? ' active' : ''}`);
      box.setAttribute('aria-label', `${roleOf(seat)} ${nameOf(seat)}的出牌`);
      if (led) box.append(text('span', '首出', 'lead-tag'));
      const label = text('div', '', 'trick-player');
      label.append(text('span', roleOf(seat), 'team-badge'), text('span', nameOf(seat), 'trick-name'));
      box.append(label);
      const cards = text('div', '', 'played-cards');
      if (play) cards.append(...play.cards.map(c => cardNode(c, true)));
      else cards.append(text('span', state.turn === seat ? '正在出牌…' : '等待出牌', 'muted'));
      box.append(cards, text('div', `${compass[seat]}${seat === state.dealer ? ' · 庄' : ''} · 余 ${state.counts[seat]} 张`, 'play-meta')); area.append(box);
      appendReaction(box, play);
    }
    renderPreviousTrick(area);
    $('table-caption').textContent = state.trick.length ? `第 ${state.trick_number} 墩 · ${nameOf(state.trick[0].seat)} 首出${suitName(effectiveSuit(state.trick[0].cards[0]))}` : state.last_trick ? `第 ${state.trick_number} 墩 · 等待 ${nameOf(state.turn)} 领出` : '第 1 墩 · 庄家领出';
    if (state.ai_thinking) $('table-caption').textContent += ' · AI 正在思考…';
  } else {
    waiting.append(text('span', `闲家 ${state.score} 分`, 'phase-badge'));
    waiting.append(text('h2', state.phase === 'match_end' ? '这场升级，有了赢家。' : '一局落定，再来一局。'));
    const button = text('button', '查看结算', 'primary'); button.onclick = () => { renderResult(); $('result-dialog').showModal(); }; waiting.append(button);
  }
}
function renderPreviousTrick(area) {
  const last = state.last_trick;
  if (!last?.plays?.length) return;
  const panel = text('section', '', 'previous-trick'); panel.id = 'previous-trick';
  panel.setAttribute('aria-label', `上一墩，第 ${last.number} 墩出牌`);
  const title = text('button', `上墩 · ${last.points} 分`, 'previous-title');
  title.type = 'button'; title.setAttribute('aria-label', '放大查看上一墩');
  title.onclick = () => {
    $('previous-dialog-title').textContent = `第 ${last.number} 墩 · ${last.points} 分`;
    $('previous-detail').replaceChildren(...last.plays.map((play, index) => previousPlayNode(play, index, last, true)));
    $('previous-dialog').showModal();
  };
  panel.append(title);
  for (const [index, play] of last.plays.entries()) {
    panel.append(previousPlayNode(play, index, last));
  }
  area.append(panel);
}
function previousPlayNode(play, index, last, detail = false) {
  const won = play.seat === last.winner;
  const row = text('div', '', `previous-play ${teamOf(play.seat)}${won ? ' won' : ''}`);
  row.title = `${roleOf(play.seat)} ${nameOf(play.seat)}${index === 0 ? ' · 首出' : ''}${won ? ' · 赢墩' : ''}`;
  row.setAttribute('aria-label', row.title);
  row.append(text('span', `${compass[play.seat]}${detail ? ` · ${roleOf(play.seat)}` : ''}${won ? ' ✓' : ''}`, 'previous-seat'));
  const cards = text('div', '', 'previous-cards');
  cards.append(...play.cards.map(c => cardNode(c, true)));
  row.append(cards); appendReaction(row, play); return row;
}
function appendReaction(parent, play) {
  if (typeof play?.reaction !== 'string' || !play.reaction || [...play.reaction].length >= 10) return;
  const bubble = text('span', play.reaction, 'play-reaction');
  bubble.setAttribute('aria-label', `${nameOf(play.seat)}：${play.reaction}`);
  parent.append(bubble);
}
function renderHand(force = true) {
  const fingerprint = JSON.stringify([state.hand, state.phase, state.seat, state.dealer,
    state.phase === 'dealing' ? null : state.turn, state.level, state.trump,
    state.play_options, state.players[state.seat].auto]);
  if (!force && fingerprint === handFingerprint) return;
  handFingerprint = fingerprint;
  $('hand-section').hidden = state.phase === 'lobby';
  $('hand-count').textContent = `${state.hand.length} 张 · 金边为主牌`;
  $('hand-title').textContent = state.seat % 2 === state.dealer % 2 ? '我的手牌 · 守庄方' : '我的手牌 · 抓分方';
  if (state.phase === 'dealing') $('hand-title').textContent = state.deal_closing ? '我的手牌 · 最后亮主' : '我的手牌 · 摸牌中';
  const hand = $('hand');
  // Reconcile by physical card ID. Replacing the whole hand on every draw
  // detaches selected/focused buttons and can interrupt a click in progress.
  const existing = new Map([...hand.children].map(node => [node.dataset.cardId, node]));
  const nodes = state.hand.map((card, i) => {
    const id = String(card.id), view = JSON.stringify([card, effectiveSuit(card)]);
    let node = existing.get(id);
    if (!node || node.dataset.cardView !== view) node = cardNode(card, false, true);
    node.dataset.cardId = id; node.dataset.cardView = view;
    node.style.zIndex = i;
    node.classList.toggle('just-dealt', freshCards.has(card.id));
    return node;
  });
  const retained = new Set(nodes);
  for (const node of [...hand.children]) if (!retained.has(node)) hand.removeChild(node);
  for (const [i, node] of nodes.entries()) if (hand.children[i] !== node) hand.insertBefore(node, hand.children[i] || null);
  const own = state.players[state.seat];
  $('auto').textContent = own.auto ? '取消托管' : '开启托管'; $('auto').classList.toggle('enabled', own.auto);
}
function bidPassLabel() {
  // On the first round the final declarer becomes dealer only at settlement.
  const dealer = state.round === 1 ? state.bid?.seat : state.dealer;
  return dealer === state.seat ? '不改主' : '不亮';
}
function updateActions() {
  if (!state) return;
  const active = ['dealing', 'burying', 'playing'].includes(state.phase);
  const mine = active && (state.phase === 'dealing' || state.turn === state.seat);
  const available = socket?.readyState === WebSocket.OPEN && !pending;
  $('room-ai').disabled = !available || state.host !== state.seat || !['lobby', 'round_end', 'match_end'].includes(state.phase);
  $('room-timer').disabled = $('room-ai').disabled;
  if ($('start-timer')) $('start-timer').disabled = !available || state.host !== state.seat;
  const startButton = $('waiting').querySelector('button');
  if (startButton && state.phase === 'lobby') startButton.disabled = !available || state.host !== state.seat;
  $('turn-message').textContent = active ? (mine ? '轮到你了' : `等待 ${nameOf(state.turn)}`) : phases[state.phase];
  if (state.phase === 'dealing') $('turn-message').textContent = state.deal_closing
    ? `最后亮主 · ${Math.max(0, Math.ceil((dealDeadline - Date.now()) / 1000))} 秒`
    : '摸牌中 · 随时亮主';
  else if (mine) $('turn-message').textContent += state.seconds_left === null ? ' · 不限时' : ` · ${Math.max(0, Math.ceil((deadline - Date.now()) / 1000))} 秒`;
  $('selected-count').textContent = state.phase === 'playing' ? `${selected.size ? `已选 ${selected.size} · ` : ''}${followPrompt()}` : selected.size ? `已选 ${selected.size} 张${state.phase === 'burying' ? ' / 8' : ''}` : '点击手牌选择';
  $('play').textContent = state.phase === 'dealing' ? '亮主 / 反主' : state.phase === 'burying' ? '确认扣底' : '出牌';
  $('play').disabled = !mine || !available || !selected.size || (state.phase === 'burying' && selected.size !== 8);
  const guidance = playGuidance();
  if (guidance && !guidance.valid) $('play').disabled = true;
  updateHandAvailability(guidance);
  if (guidance) {
    $('selected-count').textContent += state.play_options.forced && $('auto-forced').checked
      ? ' · 唯一可出，自动出牌' : ' · 暗牌不可选';
  }
  const finalBid = state.phase === 'dealing' && state.deal_closing && Array.isArray(state.bid_passed);
  const passed = state.bid_passed?.includes(state.seat);
  $('pass-bid').hidden = !finalBid;
  $('pass-bid').textContent = passed ? '已确认' : bidPassLabel();
  $('pass-bid').disabled = !finalBid || !available || passed;
  $('clear').hidden = Boolean(finalBid);
  $('hint').disabled = !mine || !available || hintPendingVersion === state.version; $('clear').disabled = !selected.size;
  $('auto').disabled = !active || !available;
  syncForcedPlay();
}
function fillStrategies(select, strategies, chosen) {
  const visible = strategies.filter(strategy => !['basic', 'xgboost_play'].includes(strategy.id));
  select.replaceChildren(...visible.map(strategy => {
    const option = text('option', strategy.name); option.value = strategy.id; return option;
  }));
  select.value = chosen;
}
function renderAI() {
  $('ai-panel').hidden = false;
  fillStrategies($('room-ai'), state.ai_strategies || [], state.ai_strategy);
  $('ai-description').textContent = state.ai_strategies?.find(s => s.id === state.ai_strategy)?.description || '';
  $('timer-settings').hidden = !state.timer_options;
  if (state.timer_options) fillTimers($('room-timer'));
}
function fillTimers(select) {
  select.replaceChildren(...state.timer_options.map(seconds => {
    const label = seconds === null ? '不限时' : `出牌 ${seconds} 秒 / 扣底 ${Math.ceil(seconds * 1.5)} 秒`;
    const option = text('option', label); option.value = seconds === null ? 'none' : String(seconds); return option;
  }));
  select.value = state.turn_seconds === null ? 'none' : String(state.turn_seconds);
}
function renderHint(message) {
  const body = $('ai-hint-content'); body.replaceChildren();
  if (message.agent?.status === 'fallback') body.append(text('p', 'OpenAI 暂不可用，本次提示由记牌策略提供。', 'muted'));
  const options = message.alternatives || [];
  for (const [index, candidate] of options.entries()) {
    const section = text('div', '', 'hint-option');
    const cards = candidate.ids.map(id => state.hand.find(c => c.id === id)).filter(Boolean);
    const label = candidate.action === 'pass' ? '不亮主' : cards.map(c => `${c.suit === 'J' ? '' : c.symbol}${c.label}`).join(' ');
    section.append(text('strong', `${index === 0 ? '建议' : `备选 ${index}`}：${label}`));
    section.append(text('span', `评分 ${candidate.score.toFixed(1)}`, 'hint-score'));
    const reasons = text('ul', '');
    for (const reason of candidate.reasons) reasons.append(text('li', `${reason.label}：${reason.value >= 0 ? '+' : ''}${reason.value.toFixed(1)}`));
    section.append(reasons); body.append(section);
  }
  body.append(text('p', '评分用于比较本次候选动作，不代表获胜概率。缺门风险是基于公开信息的估计。', 'muted'));
  $('ai-hint').hidden = !options.length;
}
function renderResult() {
  const r = state.result; if (!r) return;
  const team = r.team === 0 ? '南北队' : '东西队';
  $('result-title').textContent = r.champion !== null ? `${team} 打过 A，赢得比赛！` : `${team}${r.gain ? `升 ${r.gain} 级` : '上庄'}`;
  const body = $('result-body'); body.replaceChildren();
  const score = text('div', String(r.score), 'result-score'); score.append(text('small', ' 闲家得分')); body.append(score);
  body.append(text('p', r.multiplier ? `抠底成功：底牌 ${r.bottom_points} 分 × ${r.multiplier} 倍 = ${r.bottom_points * r.multiplier} 分（已计入总分）。` : `庄家保底，底牌 ${r.bottom_points} 分未计入闲家。`));
  body.append(text('p', '本局底牌'));
  const cards = text('div', '', 'bottom-cards'); cards.append(...state.bottom.map(c => cardNode(c, true))); body.append(cards);
  if (r.champion === null) body.append(text('p', `下一局由 ${nameOf(r.next_dealer)} 坐庄。南北打 ${state.levels[0]}，东西打 ${state.levels[1]}。`));
  const actions = $('result-actions'); actions.replaceChildren();
  const button = text('button', state.host === state.seat ? (r.champion !== null ? '新比赛 · 从 2 开始' : '开始下一局') : '等待房主开始下一局', 'primary');
  button.disabled = state.host !== state.seat;
  button.onclick = () => { send(r.champion !== null ? 'restart' : 'next'); }; actions.append(button);
}
$('lobby-form').addEventListener('submit', createRoom);
$('join').onclick = joinRoom;
$('join-code').addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); joinRoom(); } });
$('clear').onclick = () => { selected.clear(); $('ai-hint').hidden = true; renderHand(); updateActions(); };
$('room-ai').onchange = () => send('ai_strategy', {strategy: $('room-ai').value});
$('room-timer').onchange = () => send('timer', {seconds: $('room-timer').value === 'none' ? null : Number($('room-timer').value)});
$('hint').onclick = () => send('hint');
$('pass-bid').onclick = () => send('pass', {bid_revision: state.bid_revision});
$('play').onclick = () => send({dealing: 'bid', burying: 'bury', playing: 'play'}[state.phase], {ids: [...selected]});
$('auto').onclick = () => send('auto');
$('auto-forced').checked = true;
try { $('auto-forced').checked = localStorage.getItem('levelup:auto-forced') !== 'false'; } catch {}
$('auto-forced').onchange = () => {
  try { localStorage.setItem('levelup:auto-forced', String($('auto-forced').checked)); } catch {}
  updateActions();
};
$('rules-button').onclick = () => $('rules-dialog').showModal();
$('info-button').onclick = () => {
  $('info-content').append(document.querySelector('.sidebar'));
  $('info-dialog').showModal();
};
$('info-dialog').addEventListener('close', () => document.querySelector('.workspace').append(document.querySelector('.sidebar')));
document.querySelectorAll('.close-dialog').forEach(button => { button.onclick = () => button.closest('dialog').close(); });
$('copy-room').onclick = async () => {
  const invite = `${location.origin}/?room=${roomCode}`;
  try { await navigator.clipboard.writeText(invite); toast('邀请链接已复制，朋友可以在开局前加入。'); }
  catch { toast(`房间号：${roomCode}，复制地址栏链接即可邀请。`); }
};
$('leave').onclick = () => returnToLobby();
$('cancel-connect').onclick = () => returnToLobby('已取消连接。可以重新加入，或创建新房间。');
$('name').value = safeStorage.get('levelup:name') || '';
fetch('/api/ai-strategies').then(response => {
  if (!response.ok) throw new Error('策略列表暂不可用');
  return response.json();
}).then(data => fillStrategies($('create-strategy'), data.strategies, data.default)).catch(() => {
  $('create-strategy').replaceChildren(text('option', '使用服务器默认策略'));
  $('create-strategy').firstChild.value = '';
});
const inviteParams = new URLSearchParams(location.search);
const invited = (inviteParams.get('room') || inviteParams.get('/room'))?.toUpperCase();
if (invited && /^[A-Z2-9]{6}$/.test(invited)) {
  $('join-code').value = invited;
  const saved = safeStorage.get(`levelup:${invited}`);
  if (saved) { roomCode = invited; token = saved; busy(true); connect(); }
}
setInterval(updateActions, 1000);
setInterval(() => { if (socket?.readyState === WebSocket.OPEN && !pending) socket.send(JSON.stringify({action: 'ping'})); }, 25000);
