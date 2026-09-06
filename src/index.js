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
  // Forgiving parser for values pasted from Telegram/Cloudflare.
  // Accepts: 371510211, "371510211", user_id: 371510211 and CSV lists.
  const matches = String(value || '').match(/-?\d+/g) || [];
  return new Set(matches.map((x) => String(Number(x))));
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

async function fetchWithTimeout(url, options = {}, timeoutMs = 12000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort('timeout'), timeoutMs);
  try {
    return await fetch(url, { ...options, signal: controller.signal });
  } finally {
    clearTimeout(timer);
  }
}

async function telegramApi(env, method, payload) {
  if (!env.TELEGRAM_BOT_TOKEN) throw new Error('TELEGRAM_BOT_TOKEN missing');
  const r = await fetchWithTimeout(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(payload || {}),
  }, 12000);
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

function trustedRcn(r) {
  const med=Number(r?.rcn_median_ppm), n=Number(r?.rcn_count||0);
  const q=String(r?.rcn_quality||'').toLowerCase();
  const last=r?.rcn_last_date?new Date(r.rcn_last_date).getTime():null;
  const dateOk=last==null || (Number.isFinite(last) && last<=Date.now()+86400000);
  return Number.isFinite(med) && med>=0.5 && med<=3000 && n>=3 && dateOk && !q.startsWith('brak') && !q.startsWith('za mało');
}

function sizeBadge(v) {
  v = Number(v || 0);
  if (v >= 10000) return '👑 1 HA+';
  if (v >= 5000) return '🟪 5000+';
  if (v >= 3000) return '🟦 3000+';
  if (v >= 1500) return '🟩 1500+';
  return '';
}

function listingText(r, mode = 'new') {
  const loc = r.area_locality || r.location || '?';
  const dist = r.distance_km == null ? '?' : `${Number(r.distance_km).toFixed(1)} km`;
  const title = r.title || 'Działka bez tytułu';
  const lines = [`${mode === 'price' ? '📉' : '🆕'} <b>${escapeHtml(title)}</b>`, `📍 <b>${escapeHtml(loc)}</b> • ${dist}`];
  if (mode === 'price' && r.last_price_old != null && r.last_price_new != null) {
    lines.push(`💰 <s>${money(r.last_price_old)}</s> → <b>${money(r.last_price_new)}</b> (${Number(r.last_price_change_pct || 0).toFixed(1)}%)`);
  } else lines.push(`💰 <b>${money(r.price)}</b> • ${ppm(r.price_m2)}`);
  lines.push(`📐 <b>${area(r.area_m2)}</b> ${sizeBadge(r.area_m2)}`);
  if (r.published_at) lines.push(`🗓 Dodane: <b>${plDateOnly(r.published_at)}</b>`);
  else lines.push(`🗓 Data dodania: <b>nieustalona</b>`);
  if (r.plot_type) lines.push(`🏷 ${escapeHtml(r.plot_type)} • 🏗 ${escapeHtml(r.planning_status || 'nieustalone')}`);
  if (r.median_comparable) lines.push(`📢 Ogłoszenia: mediana <b>${ppm(r.median_comparable)}</b> • średnia ${ppm(r.market_mean_comparable)} (${r.comparable_count || 0})`);
  if (trustedRcn(r)) {
    lines.push(`🏛 RCN ${r.rcn_months || 24} mies.: mediana <b>${ppm(r.rcn_median_ppm)}</b> • średnia ${ppm(r.rcn_mean_ppm)} (${r.rcn_count || 0} trans., ≤${r.rcn_radius_km || '?'} km)`);
    if (r.rcn_last_date) lines.push(`🧾 Ostatnia transakcja: ${plDateOnly(r.rcn_last_date)} • ${ppm(r.rcn_last_ppm)}`);
  }
  if (r.phone) lines.push(`☎️ <b>${escapeHtml(r.phone)}</b>`);
  if (r.parcel_number) lines.push(`🗺 Nr działki: <b>${escapeHtml(r.parcel_number)}</b>`);
  if (Number(r.rcn_history_count||0)>0 && r.rcn_history_last_date) {
    const basis=String(r.rcn_history_match||'').includes('property-level')?'cena całej nieruchomości obejmującej działkę':'cena tej działki wg RCN';
    lines.push(`🧾 Historia RCN: <b>${plDateOnly(r.rcn_history_last_date)}</b> • ${money(r.rcn_history_last_price)} • ${ppm(r.rcn_history_last_ppm)} (${escapeHtml(basis)})`);
  }
  lines.push(`🌐 <b>${escapeHtml(r.source || '?')}</b>`);
  return lines.join('\n');
}


function escapeHtml(v) {
  return String(v ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]));
}

function mainMenu(origin, role = 'user') {
  const rows = [
    [{ text: '🏡 OTWÓRZ MINI APP', web_app: { url: origin } }],
    [{ text: '🆕 Nowe ogłoszenia', callback_data: 'list:new' }, { text: '📉 Zmiany cen', callback_data: 'list:price' }],
    [{ text: '📡 Status skanu', callback_data: 'status:short' }, { text: '🗃 Baza', callback_data: 'database' }],
  ];
  if (role === 'admin') {
    rows.push([{ text: '▶️ Skanuj teraz', callback_data: 'scan:run' }, { text: '⏹ Zatrzymaj skan', callback_data: 'scan:stop' }]);
    rows.push([{ text: '🧪 Diagnostyka', callback_data: 'diag' }]);
  }
  return { inline_keyboard: rows };
}


function plDateOnly(v) {
  if (!v) return '—';
  try { return new Intl.DateTimeFormat('pl-PL', { timeZone:'Europe/Warsaw', dateStyle:'short' }).format(new Date(v)); } catch { return String(v).slice(0,10); }
}

function plDate(v) {
  if (!v) return 'brak';
  try { return new Intl.DateTimeFormat('pl-PL', { timeZone: 'Europe/Warsaw', dateStyle: 'short', timeStyle: 'short' }).format(new Date(v)); }
  catch { return String(v); }
}

async function systemState(env) {
  try {
    const rows = (await env.DB.prepare(`SELECT key,value,updated_at FROM system_state`).all()).results || [];
    return Object.fromEntries(rows.map(x => [x.key, { value:x.value, updated_at:x.updated_at }]));
  } catch { return {}; }
}

async function databaseStats(env) {
  // One D1 batch instead of many serial round-trips. This matters for Telegram latency.
  const batch = await env.DB.batch([
    env.DB.prepare(`SELECT
      COUNT(*) total_rows,
      SUM(CASE WHEN active=1 THEN 1 ELSE 0 END) active_rows,
      SUM(CASE WHEN active=1 AND category='plot' THEN 1 ELSE 0 END) plots,
      SUM(CASE WHEN active=1 AND COALESCE(category,'')<>'plot' THEN 1 ELSE 0 END) active_nonplots,
      SUM(CASE WHEN active=0 OR source_status='archived' THEN 1 ELSE 0 END) archived,
      SUM(CASE WHEN active=1 AND category='plot' AND published_at IS NOT NULL AND julianday(published_at)>=julianday('now','-30 day') THEN 1 ELSE 0 END) published30,
      SUM(CASE WHEN active=1 AND category='plot' AND published_at IS NOT NULL AND julianday(published_at)>=julianday('now','-7 day') THEN 1 ELSE 0 END) published7,
      SUM(CASE WHEN active=1 AND category='plot' AND published_at IS NULL THEN 1 ELSE 0 END) unknown_date,
      SUM(CASE WHEN active=1 AND category='plot' AND phone IS NOT NULL AND phone<>'' THEN 1 ELSE 0 END) with_phone
    FROM listings`),
    env.DB.prepare(`SELECT AVG(price_m2) avg_ppm, MIN(price_m2) min_ppm, MAX(price_m2) max_ppm
      FROM listings WHERE active=1 AND category='plot' AND price_m2 BETWEEN 1 AND 5000`),
    env.DB.prepare(`SELECT price_m2 FROM listings
      WHERE active=1 AND category='plot' AND price_m2 BETWEEN 1 AND 5000 ORDER BY price_m2`),
    env.DB.prepare(`SELECT AVG(price_m2) avg_rcn, COUNT(*) rcn_count, MAX(transaction_date) rcn_last_date
      FROM rcn_transactions WHERE price_m2 BETWEEN 0.5 AND 3000
      AND julianday(transaction_date)>=julianday('now','-24 months') AND julianday(transaction_date)<=julianday('now','+1 day')`),
    env.DB.prepare(`SELECT price_m2 rcn_last_ppm, transaction_date, parcel_number FROM rcn_transactions
      WHERE price_m2 BETWEEN 0.5 AND 3000
      AND julianday(transaction_date)>=julianday('now','-24 months') AND julianday(transaction_date)<=julianday('now','+1 day')
      ORDER BY transaction_date DESC LIMIT 1`),
    env.DB.prepare(`SELECT price_m2 FROM rcn_transactions
      WHERE price_m2 BETWEEN 0.5 AND 3000
      AND julianday(transaction_date)>=julianday('now','-24 months') AND julianday(transaction_date)<=julianday('now','+1 day') ORDER BY price_m2`),
    env.DB.prepare(`SELECT COALESCE(NULLIF(area_locality,''),NULLIF(location,''),'?') name, COUNT(*) n
      FROM listings WHERE active=1 AND category='plot' GROUP BY name ORDER BY n DESC LIMIT 20`),
    env.DB.prepare(`SELECT * FROM scan_runs ORDER BY id DESC LIMIT 1`),
    env.DB.prepare(`SELECT COUNT(*) rcn_history_rows,
      COUNT(DISTINCT CASE WHEN parcel_id IS NOT NULL AND parcel_id<>'' THEN parcel_id END) rcn_identified_parcels
      FROM rcn_transactions WHERE julianday(transaction_date)>=julianday('now','-120 months') AND julianday(transaction_date)<=julianday('now','+1 day')`),
  ]);

  const first = (r) => (r?.results || [])[0] || {};
  const rows = (r) => r?.results || [];
  const total = first(batch[0]), price = first(batch[1]);
  const vals = rows(batch[2]).map(x=>Number(x.price_m2)).filter(Number.isFinite);
  let med = null;
  if (vals.length) { const m=Math.floor(vals.length/2); med=vals.length%2?vals[m]:(vals[m-1]+vals[m])/2; }

  const rcn = first(batch[3]), rcnLast = first(batch[4]);
  const rv = rows(batch[5]).map(x=>Number(x.price_m2)).filter(Number.isFinite);
  let rmed = null;
  if (rv.length) { const m=Math.floor(rv.length/2); rmed=rv.length%2?rv[m]:(rv[m-1]+rv[m])/2; }

  return {
    total_rows:Number(total.total_rows||0),
    active_rows:Number(total.active_rows||0),
    plots:Number(total.plots||0),
    active_nonplots:Number(total.active_nonplots||0),
    archived:Number(total.archived||0),
    published30:Number(total.published30||0),
    published7:Number(total.published7||0),
    unknown_date:Number(total.unknown_date||0),
    with_phone:Number(total.with_phone||0),
    ...price,
    median_ppm:med,
    rcn_median_ppm:rmed,
    rcn_mean_ppm:rcn?.avg_rcn||null,
    rcn_count:Number(rcn?.rcn_count||0),
    rcn_last_date:rcn?.rcn_last_date||null,
    rcn_last_ppm:rcnLast?.rcn_last_ppm||null,
    rcn_last_parcel:rcnLast?.parcel_number||null,
    localities:rows(batch[6]),
    last_scan:first(batch[7]),
    rcn_history_rows:Number(first(batch[8]).rcn_history_rows||0),
    rcn_identified_parcels:Number(first(batch[8]).rcn_identified_parcels||0),
  };
}

function parseDiag(last) {
  try { return JSON.parse(last?.diagnostics_json || '[]'); } catch { return []; }
}

function nextScanLabel() {
  const hour=Number(new Intl.DateTimeFormat('en-GB',{timeZone:'Europe/Warsaw',hour:'2-digit',hour12:false}).format(new Date()));
  const m=now.getMinutes(); if(hour<9 || (hour===9&&m<7))return 'dzisiaj 09:07'; if(hour<20 || (hour===20&&m<7))return 'dzisiaj 20:07'; return 'jutro 09:07';
}

async function botStatus(env) {
  const [db,st]=await Promise.all([databaseStats(env),systemState(env)]);const last=db.last_scan;const diags=parseDiag(last);
  let scanState=st.scan_status?.value||'idle';
  const started=st.scan_started_at?.value; if(scanState==='running'&&started){const age=(Date.now()-new Date(started).getTime())/60000;if(age>70)scanState='stale';}
  let progress={};
  try { progress=JSON.parse(st.scan_progress?.value||'{}')||{}; } catch {}
  return {...db,system:st,scan_state:scanState,scan_progress:progress,diagnostics:diags,next_scan:nextScanLabel()};
}

function statusShortText(s) {
  const state=String(s.scan_state||'idle');
  const running=['running','queued','cancelling'].includes(state), stale=state==='stale';
  const p=s.scan_progress||{}, done=Number(p.done_sources||0), total=Number(p.total_sources||0);
  const pct=total?Math.round(done/total*100):0;
  const icon=state==='running'?'🟡':state==='queued'?'🟠':state==='cancelling'?'🟣':state==='cancelled'?'⚫':stale?'🔴':'🟢';
  const label=state==='running'?'SKANUJE':state==='queued'?'W KOLEJCE':state==='cancelling'?'ZATRZYMYWANIE':state==='cancelled'?'ZATRZYMANY':stale?'ZAWIESZONY':'GOTOWY';
  const live=running&&total?`\n📊 Postęp: <b>${done}/${total} (${pct}%)</b> • rekordy live: ${p.accepted_records||0}`:'';
  return [`📡 <b>PROPERTY RADAR · STATUS</b>`,`${icon} Stan: <b>${label}</b>${live}`,
    `⚙️ Etap: <b>${escapeHtml(s.system?.scan_phase?.value||'—')}</b>`,
    `🔎 Ostatni skan: ${plDate(s.last_scan?.finished_at)}`,
    `🌐 Ostatnio źródła OK: ${s.last_scan?`${s.last_scan.healthy_sources||0}/${s.last_scan.total_sources||0}`:'—'}`,
    `⏰ Następny: <b>${s.next_scan}</b>`].join('\n');
}

function statusLongText(s) {
  const p=s.scan_progress||{}, src=Array.isArray(p.sources)?p.sources:[];
  const liveLines=src.map(x=>{
    const ico=x.status==='done'?(x.healthy?'✅':'⚠️'):x.status==='running'?'🔄':x.status==='cancelled'?'⏹':'▫️';
    const tail=x.status==='done'?` • ${x.records||0} rek.${x.elapsed_s!=null?` • ${Number(x.elapsed_s).toFixed(1)} s`:''}`:'';
    return `${ico} ${escapeHtml(x.name||'?')}${tail}`;
  });
  const bad=(s.diagnostics||[]).filter(x=>!x.healthy),good=(s.diagnostics||[]).filter(x=>x.healthy);
  const sourceLines=(s.diagnostics||[]).map(x=>`${x.healthy?'✅':'⚠️'} ${escapeHtml(x.source||'?')}: rekordy ${x.records??0}, linki ${x.discovered_links??0}, detail ${x.detail_pages_ok??0}${x.blocked?` • blokady ${x.blocked}`:''}${x.fatal?` • ${escapeHtml(x.fatal)}`:''}`);
  let locValidation={}; try{locValidation=JSON.parse(s.system?.location_validation?.value||'{}')||{};}catch{}
  let rcnState={}; try{rcnState=JSON.parse(s.system?.rcn_status?.value||'{}')||{};}catch{}
  let dbMaintenance={}; try{dbMaintenance=JSON.parse(s.system?.db_maintenance_last?.value||'{}')||{};}catch{}
  const locReasons=Object.entries(locValidation.reasons||{}).sort((a,b)=>Number(b[1])-Number(a[1])).slice(0,4).map(([k,v])=>`${escapeHtml(k)} ${v}`).join(' • ');
  return [`📋 <b>PEŁNY STATUS PROPERTY RADAR</b>`,``,
    `🤖 Stan: <b>${escapeHtml(s.scan_state||'idle')}</b>`,
    `⚙️ Faza: ${escapeHtml(s.system?.scan_phase?.value||'—')}`,
    `▶️ Start: ${plDate(s.system?.scan_started_at?.value)}`,
    p.total_sources?`📊 Live: <b>${p.done_sources||0}/${p.total_sources}</b> źródeł • pobrano ${p.downloaded_records||0} • przyjęto ${p.accepted_records||0}`:'',
    ...(liveLines.length?[``,`<b>POSTĘP ŹRÓDEŁ</b>`,...liveLines]:[]),
    ``,`✅ Ostatni zakończony: ${plDate(s.last_scan?.finished_at)}`,
    `🌐 Źródła OK: <b>${good.length}/${s.diagnostics?.length||0}</b> • błędne/niepewne: <b>${bad.length}</b>`,
    ...sourceLines,
    ``,`📦 Ostatni skan: pobrano ${s.last_scan?.downloaded_records??'—'} • przyjęto ${s.last_scan?.accepted_records??'—'} • nowe ${s.last_scan?.new_count??'—'} • zmiany cen ${s.last_scan?.price_change_count??'—'}`,
    `🚫 Odrzucone ${s.last_scan?.rejected_count??'—'} • wygaszone ${s.last_scan?.deactivated_count??'—'}`,
    locValidation.registry_version?`🧭 Walidacja lokalizacji: <b>${escapeHtml(locValidation.registry_version)}</b>${locReasons?` • ${locReasons}`:''}`:'',
    dbMaintenance.version?`🧹 Auto-porządki D1: sprawdzono ${dbMaintenance.checked_rows||0} • usunięto <b>${dbMaintenance.deleted_rows||0}</b>`:'',
    rcnState.state?`🏛 RCN: <b>${escapeHtml(rcnState.state)}</b>${rcnState.fetched!=null?` • pobrano ${rcnState.fetched}`:''}${rcnState.history_months?` • historia ${rcnState.history_months} mies.`:''}${rcnState.error?` • ${escapeHtml(rcnState.error)}`:''}`:'',
    ``,`⏰ Następny automatyczny: <b>${s.next_scan}</b>`,`🧯 Ostatni błąd: ${escapeHtml(s.system?.last_error?.value||'brak')}`].filter(Boolean).join('\n');
}

function databaseStatusText(s) {
  const loc=(s.localities||[]).map(x=>`${escapeHtml(x.name)}: <b>${x.n}</b>`).join(' • ')||'—';
  const legacy = Number(s.active_nonplots||0);
  return [`🗃 <b>STATUS BAZY</b>`,
    `📦 Wszystkie rekordy: <b>${s.total_rows||0}</b> • aktywne: <b>${s.active_rows||0}</b>`,
    `🏡 Aktywne działki: <b>${s.plots||0}</b>${legacy?` • poza nowym filtrem działek: ${legacy}`:''}`,
    `🕘 Dodane ≤7 dni: ${s.published7||0} • ≤30 dni: <b>${s.published30||0}</b>`,
    `❓ Aktywne działki bez daty publikacji: ${s.unknown_date||0} • archiwalne/nieaktywne: ${s.archived||0}`,
    `☎️ Z telefonem: ${s.with_phone||0}`,
    ``,
    `📢 <b>CENY Z OGŁOSZEŃ</b>`,
    `mediana: <b>${s.median_ppm==null?'—':ppm(s.median_ppm)}</b> • średnia: ${s.avg_ppm==null?'—':ppm(s.avg_ppm)}`,
    ``,
    `🏛 <b>REALNE TRANSAKCJE RCN — 24 mies.</b>`,
    `mediana: <b>${s.rcn_median_ppm==null?'—':ppm(s.rcn_median_ppm)}</b> • średnia: ${s.rcn_mean_ppm==null?'—':ppm(s.rcn_mean_ppm)}`,
    `transakcje benchmarkowe: ${s.rcn_count||0} • ostatnia: ${plDateOnly(s.rcn_last_date)}${s.rcn_last_ppm?` • ${ppm(s.rcn_last_ppm)}`:''}`,
    `archiwum RCN do historii działek: ${s.rcn_history_rows||0} rekordów • działki z ID EGiB: ${s.rcn_identified_parcels||0}`,
    ``,
    `📍 <b>AKTYWNE WG MIEJSCOWOŚCI</b>`,loc].join('\n');
}

async function listForBot(env, mode) {
  if(mode==='price') return (await env.DB.prepare(`SELECT * FROM listings WHERE active=1 AND category='plot' AND COALESCE(source_status,'active')<>'archived' AND last_meaningful_price_change_at IS NOT NULL AND julianday(last_meaningful_price_change_at)>=julianday('now','-30 days') ORDER BY last_meaningful_price_change_at DESC LIMIT 6`).all()).results||[];
  return (await env.DB.prepare(`SELECT * FROM listings WHERE active=1 AND category='plot' AND published_at IS NOT NULL AND julianday(published_at)>=julianday('now','-3 day') ORDER BY published_at DESC LIMIT 6`).all()).results||[];
}

function haversineKm(lat1,lon1,lat2,lon2){
  const R=6371.0088,toRad=x=>Number(x)*Math.PI/180;
  const p1=toRad(lat1),p2=toRad(lat2),dp=toRad(Number(lat2)-Number(lat1)),dl=toRad(Number(lon2)-Number(lon1));
  const a=Math.sin(dp/2)**2+Math.cos(p1)*Math.cos(p2)*Math.sin(dl/2)**2;return 2*R*Math.asin(Math.sqrt(a));
}

async function nearbyRcn(env,listing){
  if(!listing?.lat||!listing?.lon)return [];
  const rows=(await env.DB.prepare(`SELECT transaction_date,price,area_m2,price_m2,parcel_number,parcel_id,transaction_id,price_basis,mpzp,use_type,address,lat,lon FROM rcn_transactions WHERE lat IS NOT NULL AND lon IS NOT NULL AND price_m2 BETWEEN 0.5 AND 3000 AND julianday(transaction_date)>=julianday('now','-24 months') AND julianday(transaction_date)<=julianday('now','+1 day') ORDER BY transaction_date DESC LIMIT 2500`).all()).results||[];
  const targetArea=Number(listing.area_m2||0),limitRadius=Number(listing.rcn_radius_km||10);
  const filtered=rows.map(t=>({...t,distance_km:haversineKm(listing.lat,listing.lon,t.lat,t.lon)})).filter(t=>{
    if(t.distance_km>limitRadius)return false;
    if(targetArea&&t.area_m2){const ratio=Number(t.area_m2)/targetArea;if(ratio<0.5||ratio>2)return false;}
    return true;
  }).sort((a,b)=>new Date(b.transaction_date||0)-new Date(a.transaction_date||0));
  // Whole-property RCN transactions can be exposed once for each member parcel.
  // Show/count them only once in nearby comparables.
  const seen=new Set(),unique=[];
  for(const t of filtered){
    const k=String(t.price_basis||'')==='property'?(t.transaction_id||`${t.transaction_date}|${t.price}|${t.area_m2}`):(`parcel|${t.transaction_id||''}|${t.parcel_id||t.parcel_number||''}|${t.transaction_date}|${t.price}`);
    if(seen.has(k))continue;seen.add(k);unique.push(t);
  }
  return unique.slice(0,8);
}

function normParcel(v){const m=String(v||'').replace(/\s+/g,'').match(/(\d{1,7}(?:\/\d{1,7})?)/);return m?m[1]:'';}
function foldLoc(v){return String(v||'').toLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g,'').replace(/ł/g,'l');}

async function parcelHistoryRcn(env,listing){
  const pid=String(listing?.parcel_id||'').trim(),pn=normParcel(listing?.parcel_number);
  if(!pid&&!pn)return [];
  let rows=[];
  if(pid){
    rows=(await env.DB.prepare(`SELECT transaction_date,price,area_m2,price_m2,parcel_number,parcel_id,transaction_id,price_basis,mpzp,use_type,address,lat,lon
      FROM rcn_transactions WHERE parcel_id=? AND julianday(transaction_date)>=julianday('now','-120 months') AND julianday(transaction_date)<=julianday('now','+1 day') ORDER BY transaction_date DESC LIMIT 40`).bind(pid).all()).results||[];
    return rows.map(t=>({...t,history_match:String(t.price_basis||'')==='parcel'?'egib-id-exact':'egib-id-property-level'}));
  }
  rows=(await env.DB.prepare(`SELECT transaction_date,price,area_m2,price_m2,parcel_number,parcel_id,transaction_id,price_basis,mpzp,use_type,address,lat,lon
    FROM rcn_transactions WHERE parcel_number=? AND julianday(transaction_date)>=julianday('now','-120 months') AND julianday(transaction_date)<=julianday('now','+1 day') ORDER BY transaction_date DESC LIMIT 100`).bind(pn).all()).results||[];
  const loc=foldLoc(listing.area_locality||listing.location),targetArea=Number(listing.area_m2||0);
  return rows.filter(t=>{
    if(normParcel(t.parcel_number)!==pn)return false;
    const addr=foldLoc(t.address),locOk=!!(loc&&addr&&addr.includes(loc));
    let areaOk=false;if(targetArea&&t.area_m2){const ratio=Number(t.area_m2)/targetArea;areaOk=ratio>=0.70&&ratio<=1.35;}
    return locOk&&areaOk;
  }).map(t=>({...t,history_match:'parcel-number+locality+area'})).slice(0,20);
}

async function dispatchScan(env) {
  const repo = String(env.GITHUB_REPO || '').trim();
  if (!env.GITHUB_DISPATCH_TOKEN || !repo || repo.includes('PUT_')) {
    return {
      ok: false,
      code: 'github_dispatch_not_configured',
      http_status: 503,
      message: !env.GITHUB_DISPATCH_TOKEN
        ? 'Brak GITHUB_DISPATCH_TOKEN w Cloudflare Worker → Settings → Variables & Secrets.'
        : 'Brak poprawnego GITHUB_REPO w Cloudflare Worker.'
    };
  }

  const branch = String(env.GITHUB_BRANCH || 'master').trim() || 'master';
  const r = await fetchWithTimeout(`https://api.github.com/repos/${repo}/actions/workflows/scan.yml/dispatches`, {
    method: 'POST',
    headers: {
      'authorization': `Bearer ${env.GITHUB_DISPATCH_TOKEN}`,
      'accept': 'application/vnd.github+json',
      'x-github-api-version': '2022-11-28',
      'user-agent': 'property-radar-worker',
      'content-type': 'application/json',
    },
    body: JSON.stringify({ ref: branch }),
  }, 12000);

  const raw = await r.text();
  let body = null;
  if (raw) {
    try { body = JSON.parse(raw); } catch { body = raw; }
  }

  // GitHub historically returned 204. Newer API versions can return 200.
  if (r.ok) {
    try {
      await env.DB.batch([
        env.DB.prepare(`INSERT INTO system_state(key,value,updated_at) VALUES('scan_status','queued',datetime('now')) ON CONFLICT(key) DO UPDATE SET value='queued',updated_at=datetime('now')`),
        env.DB.prepare(`INSERT INTO system_state(key,value,updated_at) VALUES('scan_phase','oczekiwanie na GitHub Actions',datetime('now')) ON CONFLICT(key) DO UPDATE SET value='oczekiwanie na GitHub Actions',updated_at=datetime('now')`)
      ]);
    } catch(e) { console.warn('scan queue state',e?.message||e); }
    return {
      ok: true,
      status: r.status,
      workflow_run_id: body && typeof body === 'object' ? body.workflow_run_id || null : null,
      run_url: body && typeof body === 'object' ? body.html_url || body.run_url || null : null,
      message: 'Skan został uruchomiony na GitHub Actions.'
    };
  }

  const detail = body && typeof body === 'object' ? (body.message || JSON.stringify(body)) : String(body || '');
  return {
    ok: false,
    code: 'github_dispatch_failed',
    http_status: r.status,
    message: `GitHub API ${r.status}: ${detail.slice(0, 350)}`
  };
}


async function githubRequest(env, path, options={}) {
  const repo=String(env.GITHUB_REPO||'').trim();
  if(!env.GITHUB_DISPATCH_TOKEN||!repo||repo.includes('PUT_')) throw new Error('GitHub Actions nie jest skonfigurowane.');
  return await fetchWithTimeout(`https://api.github.com/repos/${repo}${path}`,{
    ...options,
    headers:{
      'authorization':`Bearer ${env.GITHUB_DISPATCH_TOKEN}`,
      'accept':'application/vnd.github+json',
      'x-github-api-version':'2022-11-28',
      'user-agent':'property-radar-worker',
      ...(options.headers||{})
    }
  },12000);
}

async function resolveActiveScanRun(env) {
  const st=await systemState(env);
  const saved=Number(st.scan_github_run_id?.value||0);
  if(Number.isFinite(saved)&&saved>0){
    try{
      const sr=await githubRequest(env,`/actions/runs/${saved}`);
      if(sr.ok){const sd=await sr.json();if(['queued','in_progress','waiting','requested','pending'].includes(sd.status))return saved;}
    }catch{}
  }
  const r=await githubRequest(env,'/actions/workflows/scan.yml/runs?per_page=20');
  if(!r.ok) throw new Error(`GitHub API ${r.status}`);
  const d=await r.json();
  const run=(d.workflow_runs||[]).find(x=>['queued','in_progress','waiting','requested','pending'].includes(x.status)&&x.event==='workflow_dispatch')
    ||(d.workflow_runs||[]).find(x=>['queued','in_progress','waiting','requested','pending'].includes(x.status));
  return run?.id||null;
}

async function stopScan(env) {
  const runId=await resolveActiveScanRun(env);
  if(!runId) return {ok:false,http_status:409,message:'Nie znalazłem aktywnego skanu do zatrzymania.'};
  await env.DB.prepare(`INSERT INTO system_state(key,value,updated_at) VALUES('scan_status','cancelling',datetime('now'))
    ON CONFLICT(key) DO UPDATE SET value='cancelling',updated_at=datetime('now')`).run();
  const r=await githubRequest(env,`/actions/runs/${runId}/cancel`,{method:'POST'});
  const raw=await r.text();
  if(!r.ok){
    return {ok:false,http_status:r.status,message:`GitHub nie anulował run ${runId}: ${raw.slice(0,260)}`};
  }
  await env.DB.batch([
    env.DB.prepare(`INSERT INTO system_state(key,value,updated_at) VALUES('scan_status','cancelled',datetime('now')) ON CONFLICT(key) DO UPDATE SET value='cancelled',updated_at=datetime('now')`),
    env.DB.prepare(`INSERT INTO system_state(key,value,updated_at) VALUES('scan_phase','zatrzymany ręcznie',datetime('now')) ON CONFLICT(key) DO UPDATE SET value='zatrzymany ręcznie',updated_at=datetime('now')`),
    env.DB.prepare(`INSERT INTO system_state(key,value,updated_at) VALUES('scan_github_run_id','',datetime('now')) ON CONFLICT(key) DO UPDATE SET value='',updated_at=datetime('now')`)
  ]);
  return {ok:true,run_id:runId,message:'Skan został zatrzymany.'};
}

async function handleTelegramUpdate(update, env, origin) {
  const callback=update?.callback_query;const msg=update?.message;
  const chatId=String(callback?.message?.chat?.id||msg?.chat?.id||'');const userId=String(callback?.from?.id||msg?.from?.id||'');const role=telegramRole(env,userId);
  async function send(text,markup=mainMenu(origin,role)){if(!chatId)return;await telegramApi(env,'sendMessage',{chat_id:chatId,text,parse_mode:'HTML',disable_web_page_preview:true,reply_markup:markup});}
  async function sendListing(r,mode='new'){
    if(!chatId)return;
    const markup={inline_keyboard:[[{text:'🔗 Ogłoszenie',url:r.canonical_url},{text:'🏡 Mini App',web_app:{url:origin}}]]};
    const text=listingText(r,mode);
    if(r.image_url){
      try{await telegramApi(env,'sendPhoto',{chat_id:chatId,photo:r.image_url,caption:text.slice(0,1000),parse_mode:'HTML',reply_markup:markup});return;}catch(e){console.warn('sendPhoto fallback',e?.message||e);}
    }
    await send(text,markup);
  }
  const text=(msg?.text||'').trim().toLowerCase();
  if(!callback&&text==='/id'){await send(`chat_id: <code>${escapeHtml(chatId)}</code>
user_id: <code>${escapeHtml(userId)}</code>`,{inline_keyboard:[]});return new Response('ok');}
  if(!role){await send(`⛔ Brak dostępu.
Twój user_id: <code>${escapeHtml(userId)}</code>`,{inline_keyboard:[]});return new Response('ok');}
  if(!callback && text==='/start'){
    // Force-refresh the per-chat menu button. setChatMenuButton requires an integer chat_id;
    // sending it as a string was silently caught before and left the stale trycloudflare URL in Telegram.
    const cid=Number(chatId);
    try {
      if(Number.isSafeInteger(cid)){
        await telegramApi(env,'setChatMenuButton',{chat_id:cid,menu_button:{type:'web_app',text:'🏡 Oferty',web_app:{url:origin}}});
      }
    } catch(e) { console.warn('setChatMenuButton repair failed', e?.message||e); }
    await send(`🏡 <b>PROPERTY RADAR</b>
Alerty tylko dla faktycznie nowych publikacji i istotnych zmian ceny.
Skan automatyczny: <b>09:07 / 20:07</b>.`,mainMenu(origin,role));
    return new Response('ok');
  }
  if(callback){
    // ACK and data lookup run in parallel; do not block status/menu rendering on a Telegram round-trip.
    const ack=telegramApi(env,'answerCallbackQuery',{callback_query_id:callback.id}).catch(()=>null);
    const done=async(p)=>{await Promise.all([ack,p]);return new Response('ok');};
    const data=callback.data||'';
    if(data==='status:short'){const s=await botStatus(env);return await done(send(statusShortText(s),{inline_keyboard:[[{text:'📋 Pełny status',callback_data:'status:long'}],[{text:'⬅️ Menu',callback_data:'menu'}]]}));}
    if(data==='status:long'){const s=await botStatus(env);return await done(send(statusLongText(s),{inline_keyboard:[[{text:'📊 Krótki status',callback_data:'status:short'}],[{text:'⬅️ Menu',callback_data:'menu'}]]}));}
    if(data==='database'){const d=await databaseStats(env);return await done(send(databaseStatusText(d)));}
    if(data==='menu'){return await done(send(`🏡 <b>PROPERTY RADAR</b>\nWybierz funkcję:`,mainMenu(origin,role)));}
    if(data==='scan:run'){
      if(role!=='admin') return await done(send('⛔ Ręczny skan tylko dla administratora.'));
      await ack;
      await send('🔄 <b>Zlecam skan…</b> GitHub Actions uruchomi go teraz lub ustawi w kolejce, jeśli poprzedni jeszcze pracuje.');
      const d=await dispatchScan(env);
      await send(d.ok?'✅ <b>Skan zlecony.</b> Nie ma limitu ręcznych uruchomień.':`⚠️ ${escapeHtml(d.message)}`,mainMenu(origin,role));
      return new Response('ok');
    }
    if(data==='scan:stop'){
      if(role!=='admin') return await done(send('⛔ Zatrzymanie skanu tylko dla administratora.'));
      await ack;
      await send('⏹ <b>Zatrzymuję aktywny skan…</b>');
      const d=await stopScan(env);
      await send(d.ok?`✅ <b>Skan zatrzymany.</b> Run: <code>${d.run_id}</code>`:`⚠️ ${escapeHtml(d.message)}`,mainMenu(origin,role));
      return new Response('ok');
    }
    if(data==='diag'){
      if(role!=='admin'){await send('⛔ Diagnostyka tylko dla administratora.');return new Response('ok');}
      let db='OK';try{await env.DB.prepare('SELECT 1').first();}catch(e){db='BŁĄD: '+String(e?.message||e)}
      const gh=!!(env.GITHUB_DISPATCH_TOKEN&&env.GITHUB_REPO&&!String(env.GITHUB_REPO).includes('PUT_'));
      await send(`🧪 <b>DIAGNOSTYKA</b>
D1: <b>${escapeHtml(db)}</b>
GitHub trigger: <b>${gh?'OK':'BRAK'}</b> • branch: <code>${escapeHtml(env.GITHUB_BRANCH||'master')}</code>
Webhook: <b>${env.TELEGRAM_WEBHOOK_SECRET?'OK':'BRAK'}</b>
Rola: <b>${escapeHtml(role)}</b>
user_id: <code>${escapeHtml(userId)}</code>`,{inline_keyboard:[[{text:'⬅️ Menu',callback_data:'menu'}]]});return new Response('ok');
    }
    if(data.startsWith('list:')){
      const mode=data.slice(5);const rows=await listForBot(env,mode);if(!rows.length){await send(mode==='price'?'Brak istotnych zmian cen.':'Brak faktycznie nowych ogłoszeń.');return new Response('ok');}
      for(const r of rows)await sendListing(r,mode==='price'?'price':'new');return new Response('ok');
    }
  }
  if(text==='/diag'){
    if(role!=='admin'){await send('⛔ Diagnostyka tylko dla administratora.');return new Response('ok');}
    let db='OK';try{await env.DB.prepare('SELECT 1').first();}catch(e){db='BŁĄD: '+String(e?.message||e)}
    await send(`🧪 <b>DIAGNOSTYKA</b>
D1: ${escapeHtml(db)}
GitHub trigger: ${env.GITHUB_DISPATCH_TOKEN?'OK':'BRAK'} • branch: <code>${escapeHtml(env.GITHUB_BRANCH||'master')}</code>
user_id: <code>${escapeHtml(userId)}</code>`);
  } else if(text==='/nowe'){
    const rows=await listForBot(env,'new');if(!rows.length)await send('Brak faktycznie nowych ogłoszeń.');for(const r of rows)await sendListing(r,'new');
  } else if(text==='/ceny'){
    const rows=await listForBot(env,'price');if(!rows.length)await send('Brak istotnych zmian cen.');for(const r of rows)await sendListing(r,'price');
  } else if(text==='/statuspelny') await send(statusLongText(await botStatus(env)));
  else if(text==='/status') await send(statusShortText(await botStatus(env)),{inline_keyboard:[[{text:'📋 Pełny status',callback_data:'status:long'}],[{text:'⬅️ Menu',callback_data:'menu'}]]});
  else if(text==='/baza') await send(databaseStatusText(await databaseStats(env)));
  else if(text==='/skanuj'&&role==='admin'){await send('🔄 <b>Zlecam skan…</b>');const d=await dispatchScan(env);await send(d.ok?'✅ Skan zlecony. Brak limitu ręcznych uruchomień.':`⚠️ ${escapeHtml(d.message)}`);}
  else if((text==='/stopscan'||text==='/stop')&&role==='admin'){await send('⏹ <b>Zatrzymuję skan…</b>');const d=await stopScan(env);await send(d.ok?`✅ Skan zatrzymany. Run: <code>${d.run_id}</code>`:`⚠️ ${escapeHtml(d.message)}`);}
  else await send(`🏡 <b>PROPERTY RADAR</b>
Alerty tylko dla faktycznie nowych publikacji i istotnych zmian ceny.
Skan automatyczny: <b>09:07 / 20:07</b>.`,mainMenu(origin,role));
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

  if (url.pathname === '/api/stats') return json(await databaseStats(env));
  if (url.pathname === '/api/status') return json(await botStatus(env));

  if (url.pathname === '/api/listings') {
    // Mini App is plots-only. Legacy houses/garages can remain in D1 for audit/history,
    // but they are never returned to the user-facing listing browser.
    const rows = await env.DB.prepare(`SELECT * FROM listings WHERE category='plot' AND COALESCE(source_status,'active')<>'invalid-parser' ORDER BY COALESCE(published_at,first_seen) DESC, id DESC LIMIT 2500`).all();
    return json({ listings: rows.results || [], stats: await databaseStats(env) });
  }

  const m = url.pathname.match(/^\/api\/listing\/(\d+)\/history$/);
  if (m) {
    const rows = await env.DB.prepare(`SELECT seen_at, price FROM price_history WHERE listing_id=? ORDER BY seen_at ASC`).bind(Number(m[1])).all();
    return json({ history: rows.results || [] });
  }
  const mr = url.pathname.match(/^\/api\/listing\/(\d+)\/rcn$/);
  if (mr) {
    const listing=await env.DB.prepare(`SELECT id,lat,lon,area_m2,area_locality,location,parcel_number,parcel_id,parcel_id_confidence,rcn_radius_km FROM listings WHERE id=?`).bind(Number(mr[1])).first();
    if(!listing)return json({error:'listing not found'},404);
    const [transactions,parcel_history]=await Promise.all([nearbyRcn(env,listing),parcelHistoryRcn(env,listing)]);
    return json({transactions,parcel_history});
  }

  if (url.pathname === '/api/scan' && req.method === 'POST') {
    if (user.role !== 'admin' && user.uid !== 'web') return json({ error: 'admin required' }, 403);
    const current=await botStatus(env);
    if(['running','queued','cancelling'].includes(String(current.scan_state||''))) return json({error:'Skan już jest aktywny.',state:current.scan_state},409);
    const d = await dispatchScan(env);
    return json(d, d.ok ? 200 : (d.http_status >= 400 && d.http_status <= 599 ? d.http_status : 502));
  }
  if (url.pathname === '/api/scan/stop' && req.method === 'POST') {
    if (user.role !== 'admin' && user.uid !== 'web') return json({ error: 'admin required' }, 403);
    const d=await stopScan(env);
    return json(d,d.ok?200:(d.http_status||502));
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
  async fetch(req, env, ctx) {
    const url = new URL(req.url);
    try {
      if (url.pathname.startsWith('/api/')) return await handleApi(req, env, url);

      if (url.pathname === '/telegram/webhook' && req.method === 'POST') {
        // Validate and read the tiny Telegram update before returning 200, then process the
        // plain object in waitUntil. This avoids keeping a Request body alive after response.
        const secret=req.headers.get('X-Telegram-Bot-Api-Secret-Token')||'';
        if(!env.TELEGRAM_WEBHOOK_SECRET||!timingSafeEqual(secret,env.TELEGRAM_WEBHOOK_SECRET)) {
          return new Response('forbidden',{status:403});
        }
        const update=await req.json().catch(()=>null);
        if(update) ctx.waitUntil(handleTelegramUpdate(update,env,url.origin).catch(e=>console.error('telegram background',e)));
        return new Response('ok',{status:200});
      }

      const asset = await env.ASSETS.fetch(req);
      return addSecurityHeaders(asset);
    } catch (e) {
      console.error(e);
      if (url.pathname.startsWith('/api/')) return json({ error: String(e?.message || e) }, 500);
      return new Response('Internal error', { status: 500 });
    }
  },
};
