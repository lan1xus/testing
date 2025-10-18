(function() {
  const messagesEl = document.getElementById('messages');
  const formEl = document.getElementById('chat-form');
  const inputEl = document.getElementById('chat-input');

  function appendMessage(text) {
    const el = document.createElement('div');
    el.className = 'message';
    el.textContent = text;
    messagesEl.appendChild(el);
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  let wsProtocol = (location.protocol === 'https:') ? 'wss' : 'ws';
  const ws = new WebSocket(`${wsProtocol}://${location.host}/ws`);

  ws.addEventListener('message', (event) => {
    appendMessage(event.data);
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
})();
