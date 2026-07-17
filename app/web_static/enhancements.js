const enhancedTitles = {
  overview: ['Обзор', 'Состояние персонального ассистента'],
  chat: ['Диалоги', 'Общая история веб-чата'],
  max: ['MAX', 'Управление транспортом и исходящими сообщениями'],
  keys: ['API-ключи', 'Раздельные рабочие и административные ключи'],
  settings: ['Настройки AI', 'Провайдер, версия модели, бюджет и инструкция'],
  n8n: ['n8n', 'Управляемый вывод ответов в workflow'],
  stats: ['Статистика', 'Запросы, токены и задержка'],
  logs: ['Журнал', 'Безопасные события и аудит'],
};

setView = function(view) {
  state.view = view;
  $$('.view').forEach(element => element.classList.remove('active'));
  $(`#view-${view}`).classList.add('active');
  $$('.nav-item').forEach(element => {
    element.classList.toggle('active', element.dataset.view === view);
  });
  $('#page-title').textContent = enhancedTitles[view][0];
  $('#page-subtitle').textContent = enhancedTitles[view][1];
  if (view === 'chat') loadConversations();
  if (view === 'keys') loadKeys();
  if (view === 'settings') loadSettings();
  if (view === 'n8n') loadN8n();
  if (view === 'stats') loadStats();
  if (view === 'logs') loadLogs();
};

const originalLoadStatus = loadStatus;
loadStatus = async function() {
  await originalLoadStatus();
  if (state.status?.app_version) {
    $('#app-version').textContent = `v${state.status.app_version}`;
  }
};

async function loadAccount() {
  const value = $('#metric-balance');
  const detail = $('#metric-balance-detail');
  try {
    const account = await api('/api/provider/account');
    if (account.available) {
      value.textContent = `${Number(account.remaining_percent).toFixed(1)}%`;
      detail.textContent = `$${Number(account.remaining).toFixed(2)} из $${Number(account.budget).toFixed(2)} · запросы ${account.daily_requests.remaining_percent}%`;
    } else {
      value.textContent = `${account.daily_requests.remaining_percent}%`;
      detail.textContent = `Дневные запросы; баланс: ${account.reason}`;
    }
  } catch (error) {
    value.textContent = '—';
    detail.textContent = error.message;
  }
}

async function loadModels(selectedModel = null) {
  const select = $('#model-select');
  const current = selectedModel || select.value;
  select.disabled = true;
  try {
    const data = await api('/api/provider/models');
    select.textContent = '';
    data.items.forEach(model => {
      const option = document.createElement('option');
      option.value = model.id;
      const context = model.context_length ? ` · ${Number(model.context_length).toLocaleString('ru-RU')} ctx` : '';
      option.textContent = `${model.name}${context}`;
      select.append(option);
    });
    if (current && !data.items.some(model => model.id === current)) {
      const option = document.createElement('option');
      option.value = current;
      option.textContent = `${current} · текущая`;
      select.prepend(option);
    }
    select.value = current;
  } catch (error) {
    select.textContent = '';
    const option = document.createElement('option');
    option.value = current;
    option.textContent = current || 'Каталог недоступен';
    select.append(option);
    notice(error.message, true);
  } finally {
    select.disabled = false;
  }
}

loadSettings = async function() {
  try {
    const data = await api('/api/settings');
    const form = $('#settings-form');
    for (const [key, value] of Object.entries(data.settings)) {
      const element = form.elements.namedItem(key);
      if (element && key !== 'llm_model') element.value = value;
    }
    await loadModels(data.settings.llm_model);
  } catch (error) {
    notice(error.message, true);
  }
};

loadKeys = async function() {
  try {
    const keys = await api('/api/keys');
    const root = $('#key-cards');
    root.textContent = '';
    const providers = [
      ['openai', 'OpenAI project key', 'Ответы и список моделей'],
      ['openai-admin', 'OpenAI Admin key', 'Только официальная статистика расходов'],
      ['openrouter', 'OpenRouter', 'Дополнительный провайдер'],
    ];
    for (const [provider, titleText, description] of providers) {
      const card = document.createElement('article');
      card.className = 'panel';
      const title = document.createElement('h2');
      title.textContent = titleText;
      const descriptionNode = document.createElement('p');
      descriptionNode.textContent = description;
      const current = document.createElement('div');
      current.className = `key-state ${keys[provider].configured ? 'configured' : ''}`;
      current.textContent = keys[provider].configured
        ? `Настроен: ${keys[provider].masked}`
        : 'Ключ не добавлен';
      const form = document.createElement('form');
      form.className = 'stack';
      const input = document.createElement('input');
      input.type = 'password';
      input.autocomplete = 'off';
      input.placeholder = 'Новый API-ключ';
      const row = document.createElement('div');
      row.className = 'button-row';
      const save = document.createElement('button');
      save.className = 'button primary';
      save.textContent = 'Сохранить';
      const test = document.createElement('button');
      test.type = 'button';
      test.className = 'button';
      test.textContent = 'Проверить';
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'button danger-outline';
      remove.textContent = 'Удалить';
      row.append(save, test, remove);
      form.append(input, row);
      form.addEventListener('submit', async event => {
        event.preventDefault();
        try {
          await api(`/api/keys/${provider}`, {method: 'PUT', body: {key: input.value}});
          input.value = '';
          notice('Ключ сохранён');
          await loadKeys();
          await loadStatus();
          await loadAccount();
        } catch (error) {
          notice(error.message, true);
        }
      });
      test.onclick = async () => {
        try {
          const result = await api(`/api/keys/${provider}/test`, {method: 'POST'});
          notice(result.ok ? 'Подключение успешно' : 'Ключ не даёт нужного доступа', !result.ok);
          await loadAccount();
        } catch (error) {
          notice(error.message, true);
        }
      };
      remove.onclick = async () => {
        if (!confirm(`Удалить ${titleText}?`)) return;
        try {
          await api(`/api/keys/${provider}`, {method: 'DELETE'});
          notice('Ключ удалён');
          await loadKeys();
          await loadStatus();
        } catch (error) {
          notice(error.message, true);
        }
      };
      card.append(title, descriptionNode, current, form);
      root.append(card);
    }
  } catch (error) {
    notice(error.message, true);
  }
};

async function loadN8n() {
  try {
    const config = await api('/api/n8n');
    $('#n8n-enabled').checked = config.enabled;
    $('#n8n-url').value = '';
    $('#n8n-url').placeholder = config.url_masked || 'https://n8n.example/webhook/…';
    $('#n8n-token').value = '';
    const last = config.last?.state === 'ok' ? 'последняя доставка успешна' : `последнее состояние: ${config.last?.state || 'never'}`;
    $('#n8n-state').textContent = `${config.configured ? `Настроен ${config.url_masked}` : 'Webhook не настроен'} · ${last}`;
  } catch (error) {
    notice(error.message, true);
  }
}

async function saveN8n(event) {
  event.preventDefault();
  const payload = {enabled: $('#n8n-enabled').checked};
  if ($('#n8n-url').value.trim()) payload.url = $('#n8n-url').value.trim();
  if ($('#n8n-token').value.trim()) payload.token = $('#n8n-token').value.trim();
  try {
    await api('/api/n8n', {method: 'PUT', body: payload});
    notice('Настройки n8n сохранены');
    await loadN8n();
  } catch (error) {
    notice(error.message, true);
  }
}

async function testN8n() {
  try {
    await api('/api/n8n/test', {method: 'POST'});
    notice('Тестовое событие принято n8n');
    await loadN8n();
  } catch (error) {
    notice(error.message, true);
  }
}

$('#refresh-models').onclick = () => loadModels();
$('#n8n-form').onsubmit = saveN8n;
$('#test-n8n').onclick = testN8n;
$('#settings-form [name="llm_provider"]').onchange = event => {
  const defaults = {
    openai: {url: 'https://api.openai.com/v1', model: 'gpt-5.6-luna'},
    openrouter: {url: 'https://openrouter.ai/api/v1', model: 'openrouter/free'},
  }[event.target.value];
  $('#settings-form [name="llm_base_url"]').value = defaults.url;
  loadModels(defaults.model);
};

loadStatus();
loadAccount();
setInterval(loadAccount, 60000);
