// Exercise the actual client's connection lifecycle without a running browser.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const vm = require('node:vm');

function client(search = '', preferences = new Map()) {
  const elements = new Map(), timers = new Map(), sockets = [], storage = new Map();
  let timerId = 0;
  function element(id) {
    if (!elements.has(id)) {
      const classes = new Set();
      elements.set(id, {id, value: '', hidden: false, disabled: false, textContent: '', open: false,
        style: {setProperty() {}}, dataset: {}, children: [],
        classList: {add: c => classes.add(c), remove: c => classes.delete(c), contains: c => classes.has(c),
          toggle: (c, on) => on ? classes.add(c) : classes.delete(c)},
        replaceChildren(...nodes) { this.children = nodes; }, append(...nodes) { this.children.push(...nodes); },
        removeChild(node) { this.children.splice(this.children.indexOf(node), 1); return node; },
        insertBefore(node, before) {
          const old = this.children.indexOf(node); if (old !== -1) this.children.splice(old, 1);
          const index = before === null ? this.children.length : this.children.indexOf(before);
          this.children.splice(index, 0, node); return node;
        },
        setAttribute() {}, addEventListener() {}, querySelector() { return null; }, focus() {}, close() { this.open = false; }});
    }
    return elements.get(id);
  }
  class Socket {
    static OPEN = 1;
    constructor(url) { this.url = url; this.readyState = 0; sockets.push(this); }
    close() { this.readyState = 3; }
    send() {}
    message(message) { this.onmessage({data: JSON.stringify(message)}); }
  }
  const location = {protocol: 'http:', host: 'localhost:8767', search};
  const context = vm.createContext({
    URLSearchParams, AbortSignal, WebSocket: Socket, location,
    history: {replaceState(_, __, url) { location.path = url; }},
    document: {getElementById: element, createElement: tag => element(`generated-${timerId++}-${tag}`),
      querySelector: element, querySelectorAll: () => [], body: element('body')},
    sessionStorage: {getItem: k => storage.get(k) || null, setItem: (k,v) => storage.set(k,v), removeItem: k => storage.delete(k)},
    localStorage: {getItem: k => preferences.get(k) || null, setItem: (k,v) => preferences.set(k,v)},
    setTimeout(fn, ms) { const id = ++timerId; timers.set(id, {fn,ms}); return id; },
    clearTimeout: id => timers.delete(id), setInterval() {},
    fetch: async () => ({ok: true, json: async () => ({strategies: [], default: 'rule_based'})}),
  });
  vm.runInContext(readFileSync(new URL('../static/app.js', `file://${__filename}`), 'utf8'), context);
  const run = js => vm.runInContext(js, context);
  element('name').value = 'Tester';
  return {run, element, timers, sockets, storage, preferences, location, context};
}

test('missing room resets a previous game, enables creation, clears stale URL and bad token', () => {
  const c = client();
  c.storage.set('levelup:HJQY9Q', 'expired');
  c.element('body').classList.add('in-game');
  c.element('table').classList.add('is-playing');
  c.run("roomCode='HJQY9Q'; state={phase:'playing',turn:0,seat:0,host:0,trick:[]}; handFingerprint='old'; shownResult='old'; connect()");
  const ws = c.sockets[0];
  ws.message({type: 'error', message: '房间不存在或已过期，请重新创建。'});
  ws.onclose({code: 4004});
  assert.equal(c.run('state'), null);
  assert.equal(c.location.path, '/');
  assert.equal(c.storage.has('levelup:HJQY9Q'), false);
  assert.equal(c.element('create').disabled, false);
  assert.equal(c.element('join').disabled, false);
  assert.equal(c.element('lobby').hidden, false);
  assert.equal(c.element('hand-section').hidden, true);
  assert.equal(c.element('body').classList.contains('in-game'), false);
  assert.equal(c.element('table').classList.contains('is-playing'), false);
  assert.equal(c.run('handFingerprint'), '');
  assert.match(c.element('room-error').textContent, /HJQY9Q.*完整邀请链接.*创建房间/);
  assert.equal(c.element('room-error').hidden, false);
});

test('an already-started room gives a persistent explanation and a usable lobby', () => {
  const c = client(); c.run("roomCode='ABCDEF'; connect()");
  c.sockets[0].message({type:'error', message:'牌局已经开始，只能由原玩家恢复连接。'});
  c.sockets[0].onclose({code:4003});
  assert.match(c.element('room-error').textContent, /牌局已经开始.*创建房间/);
  assert.equal(c.element('create').disabled, false);
});

test('welcome without a state still times out and preserves the recovery token', () => {
  const c = client(); c.run("roomCode='ABCDEF'; connect()");
  c.sockets[0].message({type:'welcome',token:'valid'});
  assert.equal(c.element('create').disabled, true);
  [...c.timers.values()].find(t => t.ms === 12000).fn();
  assert.equal(c.element('create').disabled, false);
  assert.equal(c.storage.get('levelup:ABCDEF'), 'valid');
  assert.match(c.element('room-error').textContent, /超时/);
});

test('connection failures stop retrying and return to the lobby', () => {
  const c = client(); c.run("roomCode='ABCDEF'; connect()");
  for (let n=0;n<3;n++) {
    c.sockets[n].onclose({code:1006});
    if (n<2) {
      const [id,timer] = [...c.timers].find(([,t]) => t.ms === 1800);
      c.timers.delete(id); timer.fn();
    }
  }
  assert.equal(c.sockets.length, 3);
  assert.equal(c.element('create').disabled, false);
  assert.match(c.element('room-error').textContent, /暂时无法连接/);
  assert.equal(c.timers.size, 0);
});

test('cancel ignores late socket events and leaves a working create flow', async () => {
  const c = client(); c.run("roomCode='ABCDEF'; connect()");
  const old = c.sockets[0]; c.element('cancel-connect').onclick();
  old.message({type:'welcome',token:'late'}); old.onerror(); old.onclose({code:1006});
  assert.equal(c.run('socket'), null);
  assert.equal(c.element('connection').textContent, '大厅');
  c.context.fetch = async () => ({ok: true, json: async () => ({room:'NEWABC',token:'new'})});
  await c.run('createRoom({preventDefault(){}})');
  assert.equal(c.sockets.length, 2);
  assert.match(c.sockets[1].url, /NEWABC$/);
});

test('cancelled HTTP creation cannot reconnect when its response arrives late', async () => {
  const c = client(); let resolve;
  c.context.fetch = () => new Promise(r => { resolve=r; });
  const pending = c.run('createRoom({preventDefault(){}})');
  c.element('cancel-connect').onclick();
  resolve({ok:true,json:async()=>({room:'LATEAB',token:'late'})});
  await pending;
  assert.equal(c.sockets.length, 0);
  assert.equal(c.element('create').disabled, false);
});

test('the slash-prefixed room query also prefills the room', () => {
  assert.equal(client('?/room=HJQY9Q').element('join-code').value, 'HJQY9Q');
});

function drawingClient() {
  const c = client();
  c.run("roomCode='DRAWXX';connect()");
  const ws = c.sockets[0]; ws.readyState = 1;
  const sent = []; ws.send = payload => sent.push(JSON.parse(payload));
  const card = (id, suit, rank) => ({id, suit, rank, label:String(rank), symbol:symbolsForTest[suit], group:suit});
  const symbolsForTest = {S:'♠', H:'♥', C:'♣', D:'♦'};
  let state = {type:'state',phase:'dealing',version:10,round:1,deal_id:1,seat:0,host:0,turn:1,dealer:0,
    level:'2',levels:['2','2'],trump:null,bid:null,bid_revision:0,bid_passed:[],deal_closing:false,
    dealt:5,deal_total:100,counts:[2,1,1,1],score:0,seconds_left:null,
    trick:[],last_trick:null,trick_number:1,result:null,events:[],
    players:[0,1,2,3].map(seat=>({seat,name:'Player'+seat,human:true,connected:true,auto:false})),
    hand:[card(20,'H',2),card(19,'S',3)]};
  const update = changes => { state = {...state, version:state.version+1, ...changes}; ws.message(state); };
  update({});
  return {c, sent, card, update};
}

test('drawing preserves selected trump cards across seat rotation, hand sorting and the final bidding window', () => {
  const {c, sent, card, update} = drawingClient();
  const selectedCard = c.element('hand').children[0]; selectedCard.onclick();
  update({turn:2, dealt:6, counts:[2,2,1,1]});
  assert.equal(c.run('selected.has(20)'), true);
  assert.equal(c.element('hand').children[0], selectedCard, 'another player drawing should not rebuild this hand');
  // A matching level card arrives, and sorting moves the original selected card.
  update({turn:1, dealt:9, hand:[card(19,'S',3),card(74,'H',2),card(20,'H',2)]});
  assert.equal(c.run('selected.has(20)'), true);
  assert.equal(c.element('hand').children[2], selectedCard, 'new own cards preserve the selected button identity');
  assert.equal(selectedCard.classList.contains('selected'), true);
  c.element('hand').children[1].onclick();
  update({turn:3,dealt:99});
  update({turn:0,dealt:100,deal_closing:true,deal_seconds_left:60});
  assert.equal(c.run('selected.size'), 2);
  assert.equal(c.element('play').disabled, false);
  c.element('play').onclick();
  assert.equal(sent[0].action, 'bid');
  assert.deepEqual(sent[0].ids.sort((a,b)=>a-b), [20,74]);
  assert.equal(sent[0].version, c.run('state.version'));
  assert.equal(sent[0].deal_id, 1);
});

test('drawing selection still clears for a new deal, phase, or seat, and playing selections clear on turn change', () => {
  for (const change of [{deal_id:2}, {phase:'burying'}, {seat:1}, {round:2}]) {
    const {c, update} = drawingClient();
    c.element('hand').children[0].onclick();
    update(change);
    assert.equal(c.run('selected.size'), 0, JSON.stringify(change));
  }
  const {c, card, update} = drawingClient();
  c.element('hand').children[0].onclick();
  update({hand:[card(19,'S',3)]});
  assert.equal(c.run('selected.size'), 0, 'removed cards cannot stay selected');
  update({phase:'playing',turn:0});
  c.element('hand').children[0].onclick();
  assert.equal(c.run('selected.size'), 1);
  update({turn:1});
  assert.equal(c.run('selected.size'), 0);
});

test('unlimited turns display no countdown while timed turns still do', () => {
  const c = client();
  c.run("state={phase:'playing',turn:0,seat:0,host:0,trick:[],seconds_left:null}; socket={readyState:1}; updateActions()");
  assert.equal(c.element('turn-message').textContent, '轮到你了 · 不限时');
  c.run('state.seconds_left=30; deadline=Date.now()+30000; updateActions()');
  assert.match(c.element('turn-message').textContent, /轮到你了 · 30 秒/);
});

test('不亮 appears after drawing, submits bid revision, and resets after a new bid', () => {
  const c = client();
  c.run("state={phase:'dealing',seat:0,host:0,turn:0,deal_id:7,bid_revision:1,bid_passed:[],deal_closing:false}; socket={readyState:1,send(data){globalThis.sent=JSON.parse(data)}}; updateActions()");
  assert.equal(c.element('pass-bid').hidden, true);
  c.run('state.deal_closing=true; dealDeadline=Date.now()+60000; updateActions()');
  assert.equal(c.element('pass-bid').hidden, false);
  assert.equal(c.element('pass-bid').disabled, false);
  assert.equal(c.element('clear').hidden, true);
  assert.match(c.element('turn-message').textContent, /60 秒/);
  c.element('pass-bid').onclick();
  assert.equal(c.run('sent.action'), 'pass');
  assert.equal(c.run('sent.bid_revision'), 1);
  assert.equal(c.run('sent.deal_id'), 7);
  c.run('pending=false;state.bid_passed=[0];updateActions()');
  assert.equal(c.element('pass-bid').textContent, '已确认');
  assert.equal(c.element('pass-bid').disabled, true);
  c.run('state.bid_passed=[];state.bid_revision=2;updateActions()');
  assert.equal(c.element('pass-bid').disabled, false);
  c.run("state.phase='burying';updateActions()");
  assert.equal(c.element('pass-bid').hidden, true);
  assert.equal(c.element('clear').hidden, false);
});

test('dealer gets 不改主 before receiving bottom, including first-round provisional dealer', () => {
  const c = client();
  c.run("state={phase:'dealing',round:1,dealer:0,seat:1,host:0,turn:0,bid:{seat:1},bid_passed:[],deal_closing:true};socket={readyState:1};updateActions()");
  assert.equal(c.element('pass-bid').textContent, '不改主');
  c.run('state.seat=0;updateActions()');
  assert.equal(c.element('pass-bid').textContent, '不亮');
  c.run('state.round=2;state.dealer=0;updateActions()');
  assert.equal(c.element('pass-bid').textContent, '不改主');
  c.run('state.bid_passed=[0];updateActions()');
  assert.equal(c.element('pass-bid').textContent, '已确认');
  assert.equal(c.element('pass-bid').disabled, true);
  c.run('state.round=1;state.bid=null;state.bid_passed=[];updateActions()');
  assert.equal(c.element('pass-bid').textContent, '不亮');
});

test('follow prompt uses the actual leader and effective suit, including level cards', () => {
  const c = client();
  c.run("state={level:'2',trump:'S',trick:[{seat:3,cards:[{suit:'H',rank:2}]}],hand:[{suit:'H',rank:13},{suit:'S',rank:4}]}");
  assert.equal(c.run('followPrompt()'), '请跟主牌 1 张');
  assert.equal(c.run("effectiveSuit({suit:'H',rank:2})"), 'T');
  assert.equal(c.run("effectiveSuit({suit:'H',rank:13})"), 'H');
  c.run("state.level='A';state.trump=null;state.trick[0].cards=[{suit:'C',rank:14}];state.hand=[{suit:'D',rank:14}]");
  assert.equal(c.run('followPrompt()'), '请跟主牌 1 张');
});

test('follow prompt explains a partial suit, a void, and a new lead after the last trick', () => {
  const c = client();
  c.run("state={level:'2',trump:'S',trick:[{seat:1,cards:[{suit:'H',rank:8},{suit:'H',rank:8}]}],hand:[{suit:'H',rank:7},{suit:'C',rank:4}]}");
  assert.equal(c.run('followPrompt()'), '跟♥红桃 1 张，补 1 张');
  c.run("state.hand=[{suit:'C',rank:4},{suit:'S',rank:9}]");
  assert.equal(c.run('followPrompt()'), '缺♥红桃，出 2 张');
  c.run('state.last_trick={plays:state.trick};state.trick=[]');
  assert.equal(c.run('followPrompt()'), '请领出新一墩');
});

test('previous trick persists separately after the next lead and updates only on completion', () => {
  const c = client();
  c.run(`state={phase:'playing',seat:0,dealer:0,turn:1,level:'2',trump:'S',counts:[24,24,24,24],
    players:[0,1,2,3].map(seat=>({seat,name:'Player'+seat})),trick_number:2,trick:[],
    last_trick:{number:1,points:15,winner:1,plays:[0,1,2,3].map(seat=>({seat,cards:[{id:seat,suit:'H',rank:10,label:'10',symbol:'♥'}]}))}};
    renderTable()`);
  const panel = () => c.element('trick-area').children.find(n => n.id === 'previous-trick');
  assert.equal(panel().children.length, 5);
  assert.equal(panel().children[0].textContent, '上墩 · 15 分');
  assert.match(panel().children[2].className, /won/);
  // All current player areas are empty until the next lead arrives.
  assert.equal(c.element('trick-area').children[0].children[1].children[0].textContent, '等待出牌');
  c.run("state.trick=[{seat:1,cards:[{id:9,suit:'D',rank:14,label:'A',symbol:'♦'}]}];state.turn=2;renderTable()");
  assert.equal(panel().children[0].textContent, '上墩 · 15 分');
  assert.equal(panel().children[1].children[1].children[0].children[0].textContent, '10');
  assert.match(c.element('table-caption').textContent, /第 2 墩.*Player1.*方块/);
  c.run('state.last_trick={number:2,points:0,winner:1,plays:state.trick};state.trick=[];state.trick_number=3;renderTable()');
  assert.equal(panel().children[0].textContent, '上墩 · 0 分');
  assert.equal(panel().children[1].children[1].children[0].children[0].textContent, 'A');
  c.run('state.last_trick=null;renderTable()');
  assert.equal(panel(), undefined);
});

test('agent reactions follow the play into the recap, while absent or long reactions render nothing', () => {
  const c = client();
  c.run(`state={phase:'playing',seat:0,dealer:0,turn:1,level:'2',trump:'S',counts:[24,25,25,25],
    players:[0,1,2,3].map(seat=>({seat,name:'Player'+seat})),trick_number:1,last_trick:null,
    trick:[{seat:0,reaction:'这分我收了',cards:[{id:9,suit:'D',rank:14,label:'A',symbol:'♦'}]}]};renderTable()`);
  // Walk the rendered table, not detached DOM nodes from earlier renders.
  function walk(node) { return [node, ...node.children.flatMap(walk)]; }
  const reactions = () => walk(c.element('trick-area')).filter(n => n.className === 'play-reaction');
  assert.deepEqual(reactions().map(n => n.textContent), ['这分我收了']);
  c.run('state.last_trick={number:1,points:0,winner:0,plays:state.trick};state.trick=[];state.trick_number=2;renderTable()');
  assert.deepEqual(reactions().map(n => n.textContent), ['这分我收了']);
  c.run("state.trick=[{seat:1,cards:state.last_trick.plays[0].cards}];renderTable()");
  assert.equal(reactions().length, 1); // Ordinary players do not get a bubble.
  c.run("state.last_trick.plays[0].reaction='一二三四五六七八九十';renderTable()");
  assert.equal(reactions().length, 0);
  c.run("delete state.last_trick.plays[0].reaction;renderTable()");
  assert.equal(reactions().length, 0);
  // Treat model text as text, never HTML.
  c.run("state.trick[0].reaction='<b>好</b>';renderTable()");
  assert.equal(reactions()[0].textContent, '<b>好</b>');
  assert.equal(reactions()[0].children.length, 0);
});

function playingClient(faces, options, lead = [{suit:'H',rank:7}, {suit:'H',rank:7}]) {
  const c = client();
  c.context.fixture = {phase:'playing',version:12,round:1,deal_id:1,seat:0,turn:0,host:0,dealer:0,
    level:'2',trump:'S',trick_number:3,seconds_left:null,
    players:[0,1,2,3].map(seat=>({seat,name:'Player'+seat,auto:false})),
    trick:lead.length ? [{seat:3,cards:lead}] : [],
    hand:faces.map(([suit,rank],id)=>({id,suit,rank,label:String(rank),symbol:{H:'♥',S:'♠',C:'♣',D:'♦'}[suit]})),
    play_options:options};
  c.run('state=fixture;globalThis.sent=[];socket={readyState:1,send(data){sent.push(JSON.parse(data))}};renderHand();updateActions()');
  return c;
}
const pairOptions = {count:2,pool:[0,1,2,3,4],required:[[0,1],[2,3]],forced:null};
const pairFaces = [['H',3],['H',3],['H',4],['H',4],['H',9],['C',9]];
function fireForced(c) {
  const timer = [...c.timers.values()].find(t => t.ms === 900);
  assert.ok(timer, 'forced play was scheduled');
  timer.fn();
}

test('legal pairs darken loose/off-suit cards and guide completion without trapping deselection', () => {
  const c = playingClient(pairFaces, pairOptions);
  const hand = c.element('hand').children;
  assert.deepEqual(hand.map(n=>n.disabled), [false,false,false,false,true,true]);
  assert.ok(hand[4].classList.contains('unplayable'));
  hand[4].onclick(); assert.equal(c.run('selected.size'), 0);
  hand[0].onclick();
  assert.deepEqual(hand.map(n=>n.disabled), [false,false,true,true,true,true]);
  assert.equal(c.element('play').disabled, true);
  hand[1].onclick(); assert.equal(c.element('play').disabled, false);
  hand[0].onclick(); assert.equal(c.element('play').disabled, true);
  c.element('clear').onclick();
  assert.deepEqual(c.element('hand').children.map(n=>n.disabled), [false,false,false,false,true,true]);
  assert.equal([...c.timers.values()].some(t=>t.ms===900), false, 'selection is not a globally forced play');
});

test('tractor selection keeps every valid run open and then requires its matching completion', () => {
  const c = playingClient([['H',3],['H',3],['H',4],['H',4],['H',5],['H',5],['H',8]],
    {count:4,pool:[0,1,2,3,4,5,6],required:[[0,1,2,3],[2,3,4,5]],forced:null},
    [{suit:'H',rank:9},{suit:'H',rank:9},{suit:'H',rank:10},{suit:'H',rank:10}]);
  const hand = c.element('hand').children;
  assert.deepEqual(hand.map(n=>n.disabled), [false,false,false,false,false,false,true]);
  hand[0].onclick();
  assert.deepEqual(hand.map(n=>n.disabled), [false,false,false,false,true,true,true]);
  for (const i of [1,2,3]) hand[i].onclick();
  assert.equal(c.element('play').disabled, false);
});

test('partial suit permits choosing a discard first but reserves space for the mandatory suit', () => {
  const c = playingClient([['H',3],['C',4],['D',8]],
    {count:2,pool:[0,1,2],required:[[0]],forced:null});
  const hand = c.element('hand').children;
  assert.deepEqual(hand.map(n=>n.disabled), [false,false,false]);
  hand[1].onclick();
  assert.deepEqual(hand.map(n=>n.disabled), [false,false,true]);
  assert.equal(c.element('play').disabled, true);
  hand[0].onclick(); assert.equal(c.element('play').disabled, false);
});

test('a lead stays in one effective suit, including off-suit level cards as trumps', () => {
  const c = playingClient([['H',2],['S',4],['H',3]],
    {count:null,pool:[0,1,2],required:[],forced:null}, []);
  const hand = c.element('hand').children;
  hand[0].onclick();
  assert.deepEqual(hand.map(n=>n.disabled), [false,false,true]);
  hand[1].onclick(); assert.equal(c.element('play').disabled, false);
  // Even externally provided incompatible selections cannot enable submit.
  c.run('selected=new Set([0,2]);updateActions()');
  assert.equal(c.element('play').disabled, true);
  assert.equal(hand[0].disabled, false); assert.equal(hand[2].disabled, false);
});

test('forced play sends once with the current version and never retries the same rejected move', () => {
  const c = playingClient([['H',3],['H',3],['C',4]],
    {count:2,pool:[0,1],required:[[0,1]],forced:[0,1]});
  assert.equal(c.element('auto-forced').checked, true);
  fireForced(c);
  assert.deepEqual(JSON.parse(c.run('JSON.stringify(sent)')), [{action:'play',version:12,deal_id:1,ids:[0,1]}]);
  c.run('pending=false;updateActions();updateActions()');
  assert.equal([...c.timers.values()].some(t=>t.ms===900), false);
  assert.equal(c.run('sent.length'), 1);
});

test('forced-play checkbox cancels pending automation, preserves manual play, and persists on reload', () => {
  const c = playingClient([['H',3]], {count:1,pool:[0],required:[[0]],forced:[0]}, [{suit:'H',rank:7}]);
  const staleTimer = [...c.timers.values()].find(t=>t.ms===900);
  c.element('auto-forced').checked = false; c.element('auto-forced').onchange();
  staleTimer.fn(); assert.equal(c.run('sent.length'), 0);
  assert.equal([...c.timers.values()].some(t=>t.ms===900), false);
  c.element('hand').children[0].onclick();
  assert.equal(c.element('play').disabled, false);
  assert.equal(client('', c.preferences).element('auto-forced').checked, false);
  c.element('auto-forced').checked = true; c.element('auto-forced').onchange();
  fireForced(c); assert.equal(c.run('sent.length'), 1);
});

test('forced-play timer is cancelled by turn changes, pending requests, disconnects and trustee mode', () => {
  for (const change of ['state.turn=1', 'pending=true', 'socket.readyState=3', 'state.players[0].auto=true', "state.phase='burying'"]) {
    const c = playingClient([['H',3]], {count:1,pool:[0],required:[[0]],forced:[0]}, [{suit:'H',rank:7}]);
    const timer = [...c.timers.values()].find(t=>t.ms===900);
    c.run(change+';updateActions()'); timer.fn();
    assert.equal(c.run('sent.length'), 0, change);
    assert.equal([...c.timers.values()].some(t=>t.ms===900), false, change);
  }
});

test('a new state invalidates the old timer and sends only the new forced play', () => {
  const c = playingClient([['H',3]], {count:1,pool:[0],required:[[0]],forced:[0]}, [{suit:'H',rank:7}]);
  const timer = [...c.timers.values()].find(t=>t.ms===900);
  c.run('state.version=13;updateActions()');
  timer.fn(); assert.equal(c.run('sent.length'), 0);
  fireForced(c);
  assert.equal(c.run('sent[0].version'), 13);
});

test('hand availability updates when only the turn changes', () => {
  const c = playingClient(pairFaces, pairOptions);
  c.run('state.turn=1;renderHand(false);updateActions()');
  assert.ok(c.element('hand').children.every(n=>n.disabled));
  c.run('state.turn=0;renderHand(false);updateActions()');
  assert.deepEqual(c.element('hand').children.map(n=>n.disabled), [false,false,false,false,true,true]);
});

test('search hints leave manual play enabled and never clear an outstanding play request', () => {
  const c = playingClient(pairFaces, pairOptions);
  c.run('connect();socket.readyState=1;selected=new Set([0,1]);pending=true;updateActions()');
  const ws = c.sockets[0];
  assert.equal(c.element('play').disabled, true);
  ws.message({type:'hint_pending',version:12});
  assert.equal(c.element('play').disabled, false);
  assert.equal(c.element('hint').disabled, true);
  c.element('play').onclick();
  assert.equal(c.run('pending'), true);
  ws.message({type:'hint',version:12,ids:[2,3],action:'play',score:1,reasons:[]});
  assert.equal(c.run('pending'), true);
  assert.deepEqual(JSON.parse(c.run('JSON.stringify([...selected])')), [0,1]);
});
