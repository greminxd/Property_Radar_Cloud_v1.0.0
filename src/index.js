const enc = new TextEncoder();
const dec = new TextDecoder();

function json(data, status = 200, extra = {}) {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      'content-type': 'application/json; charset=utf-8',
      'cache-control': 'no-store',
      ...extra,
    },
  });
}

function b64urlEncode(bytes) {
  let binary = '';
  bytes.forEach((b) => binary += String.fromCharCode(b));
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/g, '');
}

function b64urlDecode(s) {
  s = s.replace(/-/g, '+').replace(/_/g, '/');
  while (s.length % 4) s += '=';
  const bin = atob(s);
  return Uint8Array.from(bin, (c) => c.charCodeAt(0));
}

async function hmacBytes(keyBytes, message) {
  const key = await crypto.subtle.importKey(
    'raw', keyBytes, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']
  );
  return new Uint8Array(await crypto.subtle.sign('HMAC', key, enc.encode(message)));
}

async function hmacHex(keyBytes, message) {
  const sig = await hmacBytes(keyBytes, message);
  return [...sig].map((b) => b.toString(16).padStart(2, '0')).join('');
}

function timingSafeEqual(a, b) {
  if (typeof a !== 'string' || typeof b !== 'string' || a.length !== b.length) return false;
  let x = 0;
  for (let i = 0; i < a.length; i++) x |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return x === 0;
}

function parseCookies(req) {
  const out = {};
  const raw = req.headers.get('cookie') || '';
  for (const part of raw.split(';')) {
    const i = part.indexOf('=');
    if (i > 0) out[part.slice(0, i).trim()] = part.slice(i + 1).trim();
  }
  return out;
}

async function createSession(env, uid = 'web', role = 'admin') {
  const days = Math.max(1, Number(env.SESSION_DAYS || 30));
  const payloadObj = { uid: String(uid), role: String(role || 'user'), exp: Math.floor(Date.now() / 1000) + days * 86400 };
  const payload = b64urlEncode(enc.encode(JSON.stringify(payloadObj)));
  const sig = b64urlEncode(await hmacBytes(enc.encode(env.SESSION_SECRET), payload));
  return `${payload}.${sig}`;
}

async function verifySession(env, token) {
  if (!token || !env.SESSION_SECRET) return null;
  const [payload, sig] = token.split('.');
  if (!payload || !sig) return null;
  const expected = b64urlEncode(await hmacBytes(enc.encode(env.SESSION_SECRET), payload));
  if (!timingSafeEqual(expected, sig)) return null;
  try {
    const obj = JSON.parse(dec.decode(b64urlDecode(payload)));
    if (!obj.exp || obj.exp < Math.floor(Date.now() / 1000)) return null;
    return obj;
  } catch {
    return null;
  }
}

function sessionCookie(token, env) {
  const maxAge = Math.max(1, Number(env.SESSION_DAYS || 30)) * 86400;
  return `pr_session=${token}; Path=/; Max-Age=${maxAge}; HttpOnly; Secure; SameSite=None`;
}

function clearSessionCookie() {
  return 'pr_session=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=None';
}

async function authUser(req, env) {
  const token = parseCookies(req).pr_session;
  return await verifySession(env, token);
}

function parseIdList(value) {
  return new Set(String(value || '')
    .split(/[\s,;]+/)
    .map((x) => x.trim())
    .filter(Boolean));
}

function telegramRole(env, userId) {
  const id = String(userId || '');
  if (!id) return null;
  const admins = parseIdList(env.TELEGRAM_ADMINS);
  const users = parseIdList(env.TELEGRAM_USERS);
  // Backward compatibility with Cloud v1.0.0.
  const legacy = parseIdList(env.TELEGRAM_ALLOWED_USER_ID);
  if (admins.has(id)) return 'admin';
  if (users.has(id) || legacy.has(id)) return 'user';
  return null;
}

async function verifyTelegramInitData(initData, botToken, env) {
  if (!initData || !botToken) return null;
  const params = new URLSearchParams(initData);
  const givenHash = params.get('hash');
  if (!givenHash) return null;
  params.delete('hash');
  const authDate = Number(params.get('auth_date') || 0);
  if (!authDate || Math.abs(Math.floor(Date.now() / 1000) - authDate) > 86400) return null;

  const pairs = [...params.entries()].sort(([a], [b]) => a.localeCompare(b));
  const check = pairs.map(([k, v]) => `${k}=${v}`).join('\n');
  const secretKey = await hmacBytes(enc.encode('WebAppData'), botToken);
  const expected = await hmacHex(secretKey, check);
  if (!timingSafeEqual(expected, givenHash.toLowerCase())) return null;

  try {
    const user = JSON.parse(params.get('user') || '{}');
    if (!user.id) return null;
    const role = telegramRole(env, user.id);
    if (!role) return null;
    return { ...user, role };
  } catch {
    return null;
  }
}

async function telegramApi(env, method, payload) {
  if (!env.TELEGRAM_BOT_TOKEN) throw new Error('TELEGRAM_BOT_TOKEN missing');
  const r = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(payload || {}),
  });
  const data = await r.json();
  if (!data.ok) throw new Error(data.description || `Telegram ${method} failed`);
  return data.result;
}

function money(v) {
  if (v === null || v === undefined) return '?';
  return `${Math.round(Number(v)).toLocaleString('pl-PL')} zł`;
}

function area(v) {
  if (v === null || v === undefined) return '?';
  return `${Math.round(Number(v)).toLocaleString('pl-PL')} m²`;
}

function ppm(v) {
  if (v === null || v === undefined) return '?';
  return `${Number(v).toFixed(2).replace('.', ',')} zł/m²`;
}

function sizeBadge(v) {
  v = Number(v || 0);
  if (v >= 10000) return '👑 1 HA+';
  if (v >= 5000) return '🟪 5000+';
  if (v >= 3000) return '🟦 3000+';
  if (v >= 1500) return '🟩 1500+';
  return '';
}

function listingText(r) {
  const cat = r.category === 'garage' ? 'GARAŻ' : 'DZIAŁKA';
  const loc = r.area_locality || r.location || '?';
  const dist = r.distance_km == null ? '?' : `${Number(r.distance_km).toFixed(1)} km`;
  const lines = [
    `📌 <b>${cat}</b> — <b>${escapeHtml(loc)}</b> • ${dist}`,
    `💰 <b>${money(r.price)}</b> • ${ppm(r.price_m2)}`,
    `📐 <b>${area(r.area_m2)}</b> ${sizeBadge(r.area_m2)}`,
  ];
  if (r.category === 'plot') {
    lines.push(`🏷 ${escapeHtml(r.plot_type || 'nieustalona')} • 🏗 ${escapeHtml(r.planning_status || 'nieustalone')}`);
    lines.push(`🌲 prywatność <b>${r.privacy_score || '?'}/10</b> • 📊 ${escapeHtml(r.deal_label || '?')}`);
  }
  if (r.phone) lines.push(`☎️ <b>${escapeHtml(r.phone)}</b>`);
  if (r.parcel_number) lines.push(`🗺 nr działki: <b>${escapeHtml(r.parcel_number)}</b>`);
  return lines.join('\n');
}

function escapeHtml(v) {
  return String(v ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]));
}

function mainMenu(origin) {
  return {
    inline_keyboard: [
      [{ text: '🏡 OTWÓRZ APLIKACJĘ', web_app: { url: origin } }],
      [{ text: '🆕 Najnowsze', callback_data: 'list:new' }, { text: '🔥 Okazje', callback_data: 'list:deals' }],
      [{ text: '🌲 Prywatne', callback_data: 'list:private' }, { text: '📐 Duże 1500+', callback_data: 'list:large' }],
      [{ text: '🚗 Garaże', callback_data: 'list:garages' }, { text: '📊 Status', callback_data: 'status' }],
      [{ text: '🔄 Skanuj teraz', callback_data: 'scan' }],
    ],
  };
}

async function stats(env) {
  const total = await env.DB.prepare(`SELECT
      COUNT(*) total,
      SUM(CASE WHEN category='plot' THEN 1 ELSE 0 END) plots,
      SUM(CASE WHEN category='garage' THEN 1 ELSE 0 END) garages,
      SUM(CASE WHEN deal_label LIKE '%OKAZJA%' THEN 1 ELSE 0 END) deals,
      SUM(CASE WHEN privacy_score>=8 THEN 1 ELSE 0 END) private_count,
      SUM(CASE WHEN category='plot' AND area_m2>=1500 THEN 1 ELSE 0 END) large_count,
      SUM(CASE WHEN phone IS NOT NULL AND phone<>'' THEN 1 ELSE 0 END) with_phone
    FROM listings WHERE active=1`).first();
  const medRows = await env.DB.prepare(`SELECT price_m2 FROM listings WHERE active=1 AND category='plot' AND price_m2 BETWEEN 1 AND 2000 ORDER BY price_m2`).all();
  const vals = (medRows.results || []).map(x => Number(x.price_m2)).filter(Number.isFinite);
  let med = null;
  if (vals.length) {
    const m = Math.floor(vals.length / 2);
    med = vals.length % 2 ? vals[m] : (vals[m - 1] + vals[m]) / 2;
  }
  const last = await env.DB.prepare(`SELECT * FROM scan_runs ORDER BY id DESC LIMIT 1`).first();
  return { ...total, median_ppm: med, last_scan: last || null };
}

async function listForBot(env, mode) {
  let where = "active=1";
  let order = "first_seen DESC";
  if (mode === 'deals') { where += " AND deal_label LIKE '%OKAZJA%'"; order = 'price_m2 ASC'; }
  if (mode === 'private') { where += " AND category='plot' AND privacy_score>=8"; order = 'privacy_score DESC, first_seen DESC'; }
  if (mode === 'large') { where += " AND category='plot' AND area_m2>=1500"; order = 'area_m2 DESC'; }
  if (mode === 'garages') { where += " AND category='garage'"; order = 'first_seen DESC'; }
  return (await env.DB.prepare(`SELECT * FROM listings WHERE ${where} ORDER BY ${order} LIMIT 5`).all()).results || [];
}

async function dispatchScan(env) {
  if (!env.GITHUB_DISPATCH_TOKEN || !env.GITHUB_REPO || env.GITHUB_REPO.includes('PUT_')) {
    return { ok: false, message: 'Brak GITHUB_DISPATCH_TOKEN / GITHUB_REPO w Workerze.' };
  }
  const r = await fetch(`https://api.github.com/repos/${env.GITHUB_REPO}/actions/workflows/scan.yml/dispatches`, {
    method: 'POST',
    headers: {
      'authorization': `Bearer ${env.GITHUB_DISPATCH_TOKEN}`,
      'accept': 'application/vnd.github+json',
      'x-github-api-version': '2022-11-28',
      'user-agent': 'property-radar-worker',
      'content-type': 'application/json',
    },
    body: JSON.stringify({ ref: 'main' }),
  });
  if (r.status === 204) return { ok: true, message: 'Skan został uruchomiony na GitHub Actions.' };
  return { ok: false, message: `GitHub zwrócił ${r.status}: ${(await r.text()).slice(0, 250)}` };
}

async function handleTelegram(req, env) {
  const secret = req.headers.get('X-Telegram-Bot-Api-Secret-Token') || '';
  if (!env.TELEGRAM_WEBHOOK_SECRET || !timingSafeEqual(secret, env.TELEGRAM_WEBHOOK_SECRET)) {
    return new Response('forbidden', { status: 403 });
  }
  const update = await req.json();
  const callback = update.callback_query;
  const msg = update.message;
  const chatId = String(callback?.message?.chat?.id || msg?.chat?.id || '');
  const userId = String(callback?.from?.id || msg?.from?.id || '');
  const role = telegramRole(env, userId);
  const origin = new URL(req.url).origin;

  async function send(text, markup = mainMenu(origin)) {
    if (!chatId) return;
    await telegramApi(env, 'sendMessage', {
      chat_id: chatId,
      text,
      parse_mode: 'HTML',
      disable_web_page_preview: true,
      reply_markup: markup,
    });
  }

  const incomingText = (msg?.text || '').trim().toLowerCase();
  // /id is intentionally available before allow-listing so a new user can send
  // their Telegram user_id to the administrator.
  if (!callback && incomingText === '/id') {
    await send(`chat_id: <code>${escapeHtml(chatId)}</code>
user_id: <code>${escapeHtml(userId)}</code>`, { inline_keyboard: [] });
    return new Response('ok');
  }
  if (!role) return new Response('ok');

  if (callback) {
    try { await telegramApi(env, 'answerCallbackQuery', { callback_query_id: callback.id }); } catch {}
    const data = callback.data || '';
    if (data === 'status') {
      const s = await stats(env);
      const ls = s.last_scan;
      await send(`📊 <b>Property Radar</b>\n🟢 Aktywne: <b>${s.total || 0}</b>\n🌱 Działki: ${s.plots || 0} • 🚗 Garaże: ${s.garages || 0}\n🔥 Okazje: ${s.deals || 0} • 🌲 Prywatne 8+: ${s.private_count || 0}\n☎️ Z telefonem: ${s.with_phone || 0}\n📈 Mediana: ${s.median_ppm == null ? '—' : ppm(s.median_ppm)}\n🕒 Ostatni skan: ${escapeHtml(ls?.finished_at || 'brak')}`);
      return new Response('ok');
    }
    if (data === 'scan') {
      if (role !== 'admin') {
        await send('⛔ Tylko administrator może uruchomić skan.');
        return new Response('ok');
      }
      const d = await dispatchScan(env);
      await send(d.ok ? `🔄 ${escapeHtml(d.message)}` : `⚠️ ${escapeHtml(d.message)}`);
      return new Response('ok');
    }
    if (data.startsWith('list:')) {
      const mode = data.slice(5);
      const rows = await listForBot(env, mode);
      if (!rows.length) await send('Brak ofert dla tego filtra.');
      for (const r of rows) {
        await send(listingText(r), { inline_keyboard: [[{ text: '🔗 Ogłoszenie', url: r.canonical_url }, { text: '🏡 Aplikacja', web_app: { url: origin } }]] });
      }
      return new Response('ok');
    }
  }

  const text = incomingText;
  if (text === '/status') {
    const s = await stats(env);
    await send(`📊 <b>Property Radar</b>\nAktywne: <b>${s.total || 0}</b>\nDziałki: ${s.plots || 0} • Garaże: ${s.garages || 0}\nOkazje: ${s.deals || 0}\nMediana: ${s.median_ppm == null ? '—' : ppm(s.median_ppm)}`);
  } else if (text === '/skanuj') {
    if (role !== 'admin') {
      await send('⛔ Tylko administrator może uruchomić skan.');
    } else {
      const d = await dispatchScan(env);
      await send(d.ok ? `🔄 ${escapeHtml(d.message)}` : `⚠️ ${escapeHtml(d.message)}`);
    }
  } else {
    const s = await stats(env);
    await send(`🏡 <b>Property Radar</b>\nZdonia / Zakliczyn / Słona + bliskie okolice\n\n🟢 Aktywne: <b>${s.total || 0}</b>\n🌱 Działki: ${s.plots || 0} • 🚗 Garaże: ${s.garages || 0}\n🔥 Okazje: ${s.deals || 0} • 🌲 Prywatne 8+: ${s.private_count || 0}\n📐 Duże 1500+: ${s.large_count || 0}`);
  }
  return new Response('ok');
}

async function handleApi(req, env, url) {
  if (url.pathname === '/api/login' && req.method === 'POST') {
    if (!env.PANEL_PASSWORD || !env.SESSION_SECRET) return json({ error: 'Panel login not configured' }, 503);
    const body = await req.json().catch(() => ({}));
    if (!timingSafeEqual(String(body.password || ''), String(env.PANEL_PASSWORD))) return json({ error: 'Błędne hasło' }, 401);
    const token = await createSession(env, 'web');
    return json({ ok: true }, 200, { 'set-cookie': sessionCookie(token, env) });
  }

  if (url.pathname === '/api/auth/telegram' && req.method === 'POST') {
    const body = await req.json().catch(() => ({}));
    const user = await verifyTelegramInitData(body.initData || '', env.TELEGRAM_BOT_TOKEN, env);
    if (!user) return json({ error: 'Nieprawidłowe uwierzytelnienie Telegram' }, 401);
    const token = await createSession(env, `tg:${user.id}`, user.role);
    return json({ ok: true, user: { id: user.id, first_name: user.first_name || '', role: user.role } }, 200, { 'set-cookie': sessionCookie(token, env) });
  }

  if (url.pathname === '/api/logout' && req.method === 'POST') return json({ ok: true }, 200, { 'set-cookie': clearSessionCookie() });
  if (url.pathname === '/api/health') return json({ ok: true, service: 'property-radar' });

  const user = await authUser(req, env);
  if (!user) return json({ error: 'unauthorized' }, 401);

  if (url.pathname === '/api/me') return json({ ok: true, user });

  if (url.pathname === '/api/stats') return json(await stats(env));

  if (url.pathname === '/api/listings') {
    const rows = await env.DB.prepare(`SELECT * FROM listings WHERE active=1 ORDER BY first_seen DESC, id DESC LIMIT 1500`).all();
    return json({ listings: rows.results || [], stats: await stats(env) });
  }

  const m = url.pathname.match(/^\/api\/listing\/(\d+)\/history$/);
  if (m) {
    const rows = await env.DB.prepare(`SELECT seen_at, price FROM price_history WHERE listing_id=? ORDER BY seen_at ASC`).bind(Number(m[1])).all();
    return json({ history: rows.results || [] });
  }

  if (url.pathname === '/api/scan' && req.method === 'POST') {
    if (user.role !== 'admin' && user.uid !== 'web') return json({ error: 'admin required' }, 403);
    const d = await dispatchScan(env);
    return json(d, d.ok ? 200 : 503);
  }

  return json({ error: 'not found' }, 404);
}

function addSecurityHeaders(resp) {
  const h = new Headers(resp.headers);
  h.set('X-Content-Type-Options', 'nosniff');
  h.set('Referrer-Policy', 'no-referrer');
  h.set('Permissions-Policy', 'camera=(), microphone=(), geolocation=()');
  h.set('Cache-Control', resp.headers.get('content-type')?.includes('text/html') ? 'no-store' : (resp.headers.get('cache-control') || 'public, max-age=300'));
  return new Response(resp.body, { status: resp.status, statusText: resp.statusText, headers: h });
}

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    try {
      if (url.pathname.startsWith('/api/')) return await handleApi(req, env, url);
      if (url.pathname === '/telegram/webhook' && req.method === 'POST') return await handleTelegram(req, env);
      const asset = await env.ASSETS.fetch(req);
      return addSecurityHeaders(asset);
    } catch (e) {
      console.error(e);
      if (url.pathname.startsWith('/api/')) return json({ error: String(e?.message || e) }, 500);
      return new Response('Internal error', { status: 500 });
    }
  },
};
