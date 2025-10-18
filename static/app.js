(function() {
  const messagesEl = document.getElementById('messages');
  const formEl = document.getElementById('chat-form');
  const inputEl = document.getElementById('chat-input');

  const botStatusEl = document.getElementById('bot-status');
  const authStatusEl = document.getElementById('auth-status');
  const partyCountEl = document.getElementById('party-count');
  const partyMembersEl = document.getElementById('party-members');
  const partyPrivacyEl = document.getElementById('party-privacy');
  const partyPlaylistEl = document.getElementById('party-playlist');
  const partyInMatchEl = document.getElementById('party-inmatch');

  const loginBtn = document.getElementById('login-btn');
  const logoutBtn = document.getElementById('logout-btn');
  const verifyLink = document.getElementById('verify-link');
  const refreshBtn = document.getElementById('refresh-btn');
  const stopBotBtn = document.getElementById('stop-bot-btn');

  const cmdFilterEl = document.getElementById('cmd-filter');
  const cmdListEl = document.getElementById('cmd-list');

  let commands = [];
  let state = {
    bot: { online: false, ready: false, status: 'offline' },
    auth: { authenticated: false, pending: false, error: null, user: null, verification_uri_complete: null },
    party: { members: [], member_count: 0, privacy: 'private', playlist: null },
  };

  function renderStatus() {
    botStatusEl.textContent = state.bot.status || (state.bot.ready ? 'ready' : (state.bot.online ? 'online' : 'offline'));
    if (state.auth.authenticated && state.auth.user) {
      authStatusEl.textContent = `authenticated as ${state.auth.user.display_name}`;
      verifyLink.style.display = 'none';
    } else if (state.auth.pending && state.auth.verification_uri_complete) {
      authStatusEl.textContent = 'pending device code';
      verifyLink.href = state.auth.verification_uri_complete;
      verifyLink.style.display = 'inline-block';
    } else if (state.auth.error) {
      authStatusEl.textContent = `error: ${state.auth.error}`;
      verifyLink.style.display = 'none';
    } else {
      authStatusEl.textContent = 'not authenticated';
      verifyLink.style.display = 'none';
    }

    partyCountEl.textContent = state.party.member_count || 0;
    partyMembersEl.innerHTML = '';
    (state.party.members || []).forEach(m => {
      const li = document.createElement('li');
      li.textContent = `${m.display_name || m.id}${m.leader ? ' (leader)' : ''}`;
      partyMembersEl.appendChild(li);
    });
    partyPrivacyEl.textContent = state.party.privacy || '-';
    partyPlaylistEl.textContent = state.party.playlist || '-';
    partyInMatchEl.textContent = state.party.in_match ? 'true' : 'false';
  }

  function appendMessage(text) {
    const el = document.createElement('div');
    el.className = 'message';
    el.textContent = text;
    messagesEl.appendChild(el);
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function applyStatusDelta(delta) {
    if (delta.bot) state.bot = Object.assign({}, state.bot, delta.bot);
    if (delta.auth) state.auth = Object.assign({}, state.auth, delta.auth);
    if (delta.party) state.party = Object.assign({}, state.party, delta.party);
    renderStatus();
  }

  function applyStatusSnapshot(snapshot) {
    state = Object.assign({}, state, snapshot);
    renderStatus();
  }

  // WebSocket setup
  let wsProtocol = (location.protocol === 'https:') ? 'wss' : 'ws';
  const ws = new WebSocket(`${wsProtocol}://${location.host}/ws`);

  ws.addEventListener('message', (event) => {
    try {
      const msg = JSON.parse(event.data);
      if (msg.type === 'chat') {
        appendMessage(msg.text);
      } else if (msg.type === 'status') {
        if (msg.data && (msg.data.bot || msg.data.auth || msg.data.party)) {
          applyStatusDelta(msg.data);
        } else {
          applyStatusSnapshot(msg.data || {});
        }
      } else {
        appendMessage(event.data);
      }
    } catch (e) {
      appendMessage(event.data);
    }
  });

  ws.addEventListener('open', () => {
    appendMessage('Connected.');
  });

  ws.addEventListener('close', () => {
    appendMessage('Disconnected.');
  });

  formEl.addEventListener('submit', (e) => {
    e.preventDefault();
    const text = inputEl.value.trim();
    if (!text) return;
    try {
      ws.send(text);
    } catch (err) {
      console.error(err);
      appendMessage('Failed to send message.');
    }
    inputEl.value = '';
    inputEl.focus();
  });

  // Auth controls
  loginBtn.addEventListener('click', async () => {
    try {
      const res = await fetch('/auth/start');
      const data = await res.json();
      if (data.verification_uri_complete) {
        verifyLink.href = data.verification_uri_complete;
        verifyLink.style.display = 'inline-block';
        window.open(data.verification_uri_complete, '_blank');
      }
    } catch (e) {
      console.error(e);
    }
  });

  logoutBtn.addEventListener('click', async () => {
    try {
      await fetch('/auth/logout', { method: 'POST' });
    } catch (e) {
      console.error(e);
    }
  });

  refreshBtn.addEventListener('click', async () => {
    try {
      const res = await fetch('/auth/status');
      const data = await res.json();
      applyStatusDelta({ auth: data });
    } catch (e) {}
  });

  stopBotBtn.addEventListener('click', () => {
    try { ws.send('!stop'); } catch (e) { console.error(e); }
  });

  // Command help
  async function loadCommands() {
    try {
      const res = await fetch('/commands');
      const data = await res.json();
      commands = data.commands || [];
      renderCommandList('');
    } catch (e) {
      console.error(e);
    }
  }

  function renderCommandList(filter) {
    const q = (filter || '').toLowerCase();
    cmdListEl.innerHTML = '';
    commands.filter(c => !q || c.name.includes(q) || (c.aliases || []).some(a => a.includes(q))).forEach(c => {
      const li = document.createElement('li');
      const aliases = (c.aliases && c.aliases.length) ? ` (aliases: ${c.aliases.join(', ')})` : '';
      li.textContent = `!${c.name}${aliases} — ${c.help || ''}`;
      cmdListEl.appendChild(li);
    });
  }

  cmdFilterEl.addEventListener('input', () => renderCommandList(cmdFilterEl.value));

  // Initial fetches
  (async function init() {
    try {
      const res = await fetch('/auth/status');
      const data = await res.json();
      applyStatusDelta({ auth: data });
    } catch (e) {}
    await loadCommands();
  })();
})();
