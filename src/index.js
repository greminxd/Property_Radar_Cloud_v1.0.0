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
  if (r.phone) lines.push(`☎️ <b>${escapeHtml(r.phone)}</b>`);
  if (r.parcel_number) lines.push(`🗺 Nr działki: <b>${escapeHtml(r.parcel_number)}</b>`);
  lines.push(`🌐 <b>${escapeHtml(r.source || '?')}</b>`);
  return lines.join('\n');
}


function escapeHtml(v) {
  return String(v ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]));
}

function foldPublicText(v) {
  return String(v ?? '').toLowerCase().replace(/ł/g,'l').normalize('NFD').replace(/[\u0300-\u036f]/g,'');
}

const WORKER_MAINTENANCE_VERSION='1.5.3-location-category-cleanup-v1';
const KNOWN_BAD_LISTING_URLS={
  'https://www.olx.pl/d/oferta/dzialka-budowlana-20km-od-krakowa-CID3-ID1c8sfW.html':'wrong-zakliczyn-myslenice',
  'https://www.olx.pl/d/oferta/powierzchnia-300m2-CID3-ID1c2K6x.html':'rental-wrong-zakliczyn',
  'https://www.olx.pl/d/oferta/nowy-kolowrotek-samolla-ksn-8000-12-1-bb-karpiowy-surfcasting-1-sztuki-CID767-ID1ccuyw.html':'not-property',
  'https://www.olx.pl/d/oferta/nowy-kolowrotek-samolla-ksn-8000-12-1-bb-karpiowy-surfcasting-3-sztuki-CID767-ID1ccupp.html':'not-property',
  'https://www.olx.pl/d/oferta/3-pokoje-50-79-m-balkon-6-16-m2-przetronne-CID3-ID1caVYu.html':'not-plot-wroblowice-dolnoslaskie',
  'https://www.olx.pl/d/oferta/41-29-m-czystej-funkcjonalnosci-2-pok-41-29-m-balkon-6-16m-CID3-ID1caVYm.html':'not-plot-wroblowice-dolnoslaskie',
  'https://www.olx.pl/d/oferta/sprzedam-dzialke-budowlana-olszyny-k-szczytna-12-100-CID3-ID1c86Zt.html':'outside-area-olszyny-warminsko-mazurskie',
  'https://www.olx.pl/d/oferta/2-pokoje-41-29-m-balkon-6-16-m2-deweloperskie-blisko-wro-CID3-ID1caVYn.html':'not-plot-wroblowice-dolnoslaskie',
};

async function runWorkerMaintenance(env) {
  try {
    const current=await env.DB.prepare(`SELECT value FROM system_state WHERE key='worker_maintenance_version'`).first();
    if(String(current?.value||'')===WORKER_MAINTENANCE_VERSION) return;
  } catch {}
  await env.DB.prepare(`CREATE TABLE IF NOT EXISTS listing_blacklist (canonical_url TEXT PRIMARY KEY, reason TEXT, source TEXT, title TEXT, created_at TEXT NOT NULL)`).run();
  const urls=Object.keys(KNOWN_BAD_LISTING_URLS), now=new Date().toISOString();
  let rows=[];
  if(urls.length){
    const qs=urls.map(()=>'?').join(',');
    rows=(await env.DB.prepare(`SELECT id,canonical_url,source,title FROM listings WHERE canonical_url IN (${qs})`).bind(...urls).all()).results||[];
  }
  const stmts=[];
  for(const url of urls){
    const row=rows.find(x=>x.canonical_url===url);
    stmts.push(env.DB.prepare(`INSERT INTO listing_blacklist(canonical_url,reason,source,title,created_at) VALUES(?,?,?,?,?)
      ON CONFLICT(canonical_url) DO UPDATE SET reason=excluded.reason,source=excluded.source,title=excluded.title,created_at=excluded.created_at`)
      .bind(url,KNOWN_BAD_LISTING_URLS[url],row?.source||'OLX',row?.title||null,now));
  }
  const ids=rows.map(x=>Number(x.id)).filter(Number.isFinite);
  if(ids.length){
    const qs=ids.map(()=>'?').join(',');
    stmts.push(env.DB.prepare(`DELETE FROM price_history WHERE listing_id IN (${qs})`).bind(...ids));
  }
  if(urls.length){
    const qs=urls.map(()=>'?').join(',');
    stmts.push(env.DB.prepare(`DELETE FROM listings WHERE canonical_url IN (${qs})`).bind(...urls));
  }
  stmts.push(env.DB.prepare(`INSERT INTO system_state(key,value,updated_at) VALUES('worker_maintenance_version',?,datetime('now'))
    ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=datetime('now')`).bind(WORKER_MAINTENANCE_VERSION));
  stmts.push(env.DB.prepare(`INSERT INTO system_state(key,value,updated_at) VALUES('worker_maintenance_last',?,datetime('now'))
    ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=datetime('now')`).bind(JSON.stringify({version:WORKER_MAINTENANCE_VERSION,deleted_rows:rows.length,blocked_urls:urls.length,finished_at:now})));
  if(stmts.length) await env.DB.batch(stmts);
}

function rejectLegacyPublicListing(r) {
  const url=String(r?.canonical_url||'');
  if (KNOWN_BAD_LISTING_URLS[url]) return true;
  const title=foldPublicText(r?.title);
  const desc=foldPublicText(r?.description).slice(0,5000);
  const loc=foldPublicText(r?.area_locality||r?.location);
  const rentalTitle=/\b(do wynajecia|na wynajem|wynajme|wynajem|dzierzawa|do dzierzawy)\b/.test(title);
  const rentalDesc=/\b(oferta wynajmu|przedmiotem wynajmu|do wynajecia|na wynajem|cena wynajmu|czynsz)\b/.test(desc) ||
    /\b(?:zl|pln)\b.{0,18}\b(?:miesiecznie|za miesiac)\b/.test(desc);
  if (rentalTitle || rentalDesc) return true;
  // Second safety net for rows created by an older parser: obvious apartments are
  // never shown as plots even before a full scanner migration runs.
  if (/\b(liczba pokoi|rodzaj zabudowy|umeblowane|mieszkanie o powierzchni|salon z aneksem|sypialni)\b/.test(`${title} ${desc}`)) return true;
  // A foreign province in the stored location is a hard fail for this Małopolskie radar.
  if (/\b(dolnoslaskie|warminsko-mazurskie|mazowieckie|wielkopolskie|pomorskie|zachodniopomorskie|lubelskie|lubuskie|lodzkie|opolskie|podlaskie|podkarpackie|slaskie|swietokrzyskie|kujawsko-pomorskie)\b/.test(loc)) return true;
  const genericZakliczyn=/\bzakliczyn\b/.test(loc) && !/\b(zdonia|slona|biesnik|konczyska|olszowa|palesnica|luslawice|wesolow)\b/.test(loc);
  const wrongZakliczyn=/zakliczyn(?:ie)?\s*[\/,;()\-]*\s*(?:kolo|okolice|k\.?)\s+myslenic|(?:kolo|okolice|k\.?)\s+myslenic|powiat\s+myslenick|(?:gmina|gm\.)\s+siepraw/.test(`${title} ${desc}`);
  return genericZakliczyn && wrongZakliczyn;
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
  // Core status must not depend on the optional v1.4.8 blacklist migration.
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
    env.DB.prepare(`SELECT AVG(price_m2) avg_ppm, MIN(price_m2) min_ppm, MAX(price_m2) max_ppm FROM listings WHERE active=1 AND category='plot' AND price_m2 BETWEEN 1 AND 5000`),
    env.DB.prepare(`SELECT price_m2 FROM listings WHERE active=1 AND category='plot' AND price_m2 BETWEEN 1 AND 5000 ORDER BY price_m2`),
    env.DB.prepare(`SELECT COALESCE(NULLIF(area_locality,''),NULLIF(location,''),'?') name, COUNT(*) n FROM listings WHERE active=1 AND category='plot' GROUP BY name ORDER BY n DESC LIMIT 20`),
    env.DB.prepare(`SELECT * FROM scan_runs ORDER BY id DESC LIMIT 1`),
  ]);
  const first=(r)=>(r?.results||[])[0]||{}, rows=(r)=>r?.results||[];
  const total=first(batch[0]), price=first(batch[1]);
  const vals=rows(batch[2]).map(x=>Number(x.price_m2)).filter(Number.isFinite);
  let med=null;if(vals.length){const m=Math.floor(vals.length/2);med=vals.length%2?vals[m]:(vals[m-1]+vals[m])/2;}
  let blocked=0;
  try {
    const b=await env.DB.prepare(`SELECT COUNT(*) blocked FROM listing_blacklist`).first();
    blocked=Number(b?.blocked||0);
  } catch { blocked=0; }
  return {total_rows:Number(total.total_rows||0),active_rows:Number(total.active_rows||0),plots:Number(total.plots||0),active_nonplots:Number(total.active_nonplots||0),archived:Number(total.archived||0),published30:Number(total.published30||0),published7:Number(total.published7||0),unknown_date:Number(total.unknown_date||0),with_phone:Number(total.with_phone||0),...price,median_ppm:med,localities:rows(batch[3]),last_scan:first(batch[4]),blocked_urls:blocked};
}

function parseDiag(last) {
  try { return JSON.parse(last?.diagnostics_json || '[]'); } catch { return []; }
}

function nextScanLabel() {
  const now=new Date();
  const parts=new Intl.DateTimeFormat('en-GB',{timeZone:'Europe/Warsaw',hour:'2-digit',minute:'2-digit',hour12:false}).formatToParts(now);
  const hour=Number(parts.find(x=>x.type==='hour')?.value||0);
  const m=Number(parts.find(x=>x.type==='minute')?.value||0);
  if(hour<9 || (hour===9&&m<7))return 'dzisiaj 09:07';
  if(hour<20 || (hour===20&&m<7))return 'dzisiaj 20:07';
  return 'jutro 09:07';
}

async function botStatus(env) {
  let [db,st]=await Promise.all([databaseStats(env),systemState(env)]);
  // While a manual scan is queued/running, GitHub is the source of truth for the runner itself.
  // Syncing it here prevents the Mini App from being stuck on "queued" forever when Actions
  // fails before scraper/main.py has a chance to write to D1.
  try {
    const state=String(st.scan_status?.value||'idle');
    if(['queued','running','cancelling'].includes(state)) {
      const lastCheck=Date.parse(st.scan_github_checked_at?.value||'')||0;
      if(Date.now()-lastCheck>4500) {
        await syncGithubScanState(env,st);
        st=await systemState(env);
      }
    }
  } catch(e) { console.warn('github status sync',e?.message||e); }
  const last=db.last_scan;const diags=parseDiag(last);
  let scanState=st.scan_status?.value||'idle';
  const started=st.scan_started_at?.value; if(scanState==='running'&&started){const age=(Date.now()-new Date(started).getTime())/60000;if(age>70)scanState='stale';}
  let progress={};
  try { progress=JSON.parse(st.scan_progress?.value||'{}')||{}; } catch {}
  const scanControl={
    configured:!!(env.GITHUB_DISPATCH_TOKEN&&env.GITHUB_REPO&&!String(env.GITHUB_REPO).includes('PUT_')),
    repo:String(env.GITHUB_REPO||''),
    branch:String(st.scan_requested_branch?.value||env.GITHUB_BRANCH||'auto')
  };
  return {...db,system:st,scan_state:scanState,scan_progress:progress,diagnostics:diags,next_scan:nextScanLabel(),scan_control:scanControl};
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


function statusLiveText(s) {
  const p=s.scan_progress||{}, src=Array.isArray(p.sources)?p.sources:[];
  const total=Math.max(0,Number(p.total_sources||0)), done=Math.max(0,Number(p.done_sources||0));
  let pct=total?Math.round(done/total*100):0;
  if(String(s.scan_state||'')==='running' && total && done>=total) pct=99;
  const blocks=10, filled=Math.min(blocks,Math.round(pct/10));
  const bar='█'.repeat(filled)+'░'.repeat(blocks-filled);
  const lines=[`📡 <b>SKAN PROPERTY RADAR · LIVE</b>`,`<code>${bar}</code> <b>${pct}%</b>`,`🌐 Źródła: <b>${done}/${total}</b>  •  📥 pobrano: <b>${Number(p.downloaded_records||0)}</b>  •  ✅ przyjęto: <b>${Number(p.accepted_records||0)}</b>`];
  if(src.length){
    lines.push('');
    for(const x of src){
      const st=String(x.status||'');
      const ico=st==='done'?(x.healthy?'✅':'⚠️'):st==='running'?'🔄':st==='cancelled'?'⏹':'▫️';
      const tail=st==='done'?` · ${Number(x.records||0)}`:'';
      lines.push(`${ico} ${escapeHtml(x.name||'?')}${tail}`);
    }
  }
  lines.push('',`⚙️ ${escapeHtml(s.system?.scan_phase?.value||'Skan w toku')}`,'Dane z ukończonych źródeł są już widoczne w Mini App.');
  return lines.join('\n');
}
function statusShortOrLive(s){
  return ['running','queued','cancelling'].includes(String(s.scan_state||'')) ? statusLiveText(s) : statusShortText(s);
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
    ``,`☁️ Harmonogram: <b>Cloudflare Cron → GitHub Actions</b>`,
    `⏰ Następny automatyczny: <b>${s.next_scan}</b>`,
    s.system?.auto_scan_last_trigger?.value?`🕒 Ostatni trigger cron: ${plDate(s.system.auto_scan_last_trigger.value)} • ${escapeHtml(s.system?.auto_scan_last_result?.value||'—')}`:'',
    `🧯 Ostatni błąd: ${escapeHtml(s.system?.last_error?.value||s.system?.auto_scan_last_error?.value||'brak')}`].filter(Boolean).join('\n');
}

function databaseStatusText(s) {
  const loc=(s.localities||[]).map(x=>`${escapeHtml(x.name)}: <b>${x.n}</b>`).join(' • ')||'—';
  const legacy=Number(s.active_nonplots||0);
  return [`🗃 <b>STATUS BAZY</b>`,`📦 Wszystkie rekordy: <b>${s.total_rows||0}</b> • aktywne: <b>${s.active_rows||0}</b>`,`🏡 Aktywne działki: <b>${s.plots||0}</b>${legacy?` • poza filtrem: ${legacy}`:''}`,`🕘 Dodane ≤7 dni: ${s.published7||0} • ≤30 dni: <b>${s.published30||0}</b>`,`❓ Bez daty: ${s.unknown_date||0} • archiwalne/nieaktywne: ${s.archived||0}`,`☎️ Z telefonem: ${s.with_phone||0} • 🚫 zablokowane URL: ${s.blocked_urls||0}`,``,`📢 <b>CENY Z OGŁOSZEŃ / m²</b>`,`mediana: <b>${s.median_ppm==null?'—':ppm(s.median_ppm)}</b> • średnia: ${s.avg_ppm==null?'—':ppm(s.avg_ppm)}`,``,`📍 <b>AKTYWNE WG MIEJSCOWOŚCI</b>`,loc].join('\n');
}

async function listForBot(env, mode) {
  if(mode==='price') return (await env.DB.prepare(`SELECT * FROM listings WHERE active=1 AND category='plot' AND COALESCE(source_status,'active')<>'archived' AND last_meaningful_price_change_at IS NOT NULL AND julianday(last_meaningful_price_change_at)>=julianday('now','-30 days') ORDER BY last_meaningful_price_change_at DESC LIMIT 6`).all()).results||[];
  return (await env.DB.prepare(`SELECT * FROM listings WHERE active=1 AND category='plot' AND julianday(COALESCE(published_at,first_seen))>=julianday('now','-3 day') ORDER BY COALESCE(published_at,first_seen) DESC LIMIT 6`).all()).results||[];
}

async function setSystemStates(env, values) {
  const stmts=[];
  const sql=`INSERT INTO system_state(key,value,updated_at) VALUES(?,?,datetime('now'))
    ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=datetime('now')`;
  for(const [key,value] of Object.entries(values||{})) {
    stmts.push(env.DB.prepare(sql).bind(key,typeof value==='string'?value:JSON.stringify(value)));
  }
  if(stmts.length) await env.DB.batch(stmts);
}

async function resolveGithubBranch(env, repo) {
  const configured=String(env.GITHUB_BRANCH||'').trim();
  if(configured) return configured;
  try {
    const r=await fetchWithTimeout(`https://api.github.com/repos/${repo}`,{
      headers:{
        'authorization':`Bearer ${env.GITHUB_DISPATCH_TOKEN}`,
        'accept':'application/vnd.github+json',
        'x-github-api-version':'2022-11-28',
        'user-agent':'property-radar-worker'
      }
    },12000);
    if(r.ok){const d=await r.json();if(d?.default_branch)return String(d.default_branch);}
  } catch(e) { console.warn('default branch lookup',e?.message||e); }
  // Most new repositories use main. dispatchScan additionally retries master on ref errors.
  return 'main';
}

async function githubDispatchOnce(env,repo,branch) {
  const r=await fetchWithTimeout(`https://api.github.com/repos/${repo}/actions/workflows/scan.yml/dispatches`, {
    method:'POST',
    headers:{
      'authorization':`Bearer ${env.GITHUB_DISPATCH_TOKEN}`,
      'accept':'application/vnd.github+json',
      'x-github-api-version':'2022-11-28',
      'user-agent':'property-radar-worker',
      'content-type':'application/json'
    },
    body:JSON.stringify({ref:branch})
  },12000);
  const raw=await r.text();let body=null;
  if(raw){try{body=JSON.parse(raw)}catch{body=raw}}
  return {r,body};
}

async function dispatchScan(env) {
  const repo=String(env.GITHUB_REPO||'').trim();
  if(!env.GITHUB_DISPATCH_TOKEN||!repo||repo.includes('PUT_')) {
    return {
      ok:false,code:'github_dispatch_not_configured',http_status:503,
      message:!env.GITHUB_DISPATCH_TOKEN
        ? 'Brak GITHUB_DISPATCH_TOKEN w Cloudflare Worker → Settings → Variables & Secrets.'
        : 'Brak poprawnego GITHUB_REPO w Cloudflare Worker.'
    };
  }

  let branch=await resolveGithubBranch(env,repo);
  let {r,body}=await githubDispatchOnce(env,repo,branch);

  // A stale hard-coded branch was the most common reason the Mini App button did nothing.
  // If GitHub says the ref is invalid, try the other conventional branch automatically.
  if(!r.ok && [404,422].includes(r.status)) {
    const detail=body&&typeof body==='object'?(body.message||JSON.stringify(body)):String(body||'');
    if(/ref|branch/i.test(detail)) {
      const alt=branch==='main'?'master':'main';
      const retry=await githubDispatchOnce(env,repo,alt);
      if(retry.r.ok){branch=alt;r=retry.r;body=retry.body;}
    }
  }

  if(r.ok) {
    const requestedAt=new Date().toISOString();
    const bodyRunId=body&&typeof body==='object'?body.workflow_run_id||null:null;
    const bodyRunUrl=body&&typeof body==='object'?(body.html_url||body.run_url||''):'';
    try {
      await setSystemStates(env,{
        scan_status:'queued',
        scan_phase:'GitHub Actions: zlecono skan, czekam na runner',
        scan_requested_at:requestedAt,
        scan_requested_branch:branch,
        scan_github_run_id:bodyRunId?String(bodyRunId):'',
        scan_github_run_url:String(bodyRunUrl||''),
        scan_github_status:'queued',
        scan_github_conclusion:'',
        scan_progress:{version:2,status:'queued',run_id:bodyRunId,run_url:bodyRunUrl||null,started_at:null,heartbeat_at:requestedAt,total_sources:0,done_sources:0,downloaded_records:0,accepted_records:0,rejected_records:0,sources:[]}
      });
    } catch(e) { console.warn('scan queue state',e?.message||e); }
    return {
      ok:true,status:r.status,branch,
      workflow_run_id:body&&typeof body==='object'?body.workflow_run_id||null:null,
      run_url:body&&typeof body==='object'?body.html_url||body.run_url||null:null,
      message:`Skan został zlecony na GitHub Actions (${branch}).`
    };
  }

  const detail=body&&typeof body==='object'?(body.message||JSON.stringify(body)):String(body||'');
  return {ok:false,code:'github_dispatch_failed',http_status:r.status,message:`GitHub API ${r.status}: ${detail.slice(0,350)}`};
}

function warsawScheduledClock(ms) {
  const parts=new Intl.DateTimeFormat('en-GB',{timeZone:'Europe/Warsaw',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).formatToParts(new Date(ms));
  const get=(k)=>Number(parts.find(x=>x.type===k)?.value||0);
  return {year:get('year'),month:get('month'),day:get('day'),hour:get('hour'),minute:get('minute')};
}

async function runAutomaticScan(env, scheduledTime) {
  // Cloudflare cron is UTC. Wrangler fires four DST-safe candidate hours; this
  // local-time gate accepts only the intended 09:07 and 20:07 Europe/Warsaw slot.
  const scheduled=Number(scheduledTime||Date.now());
  const local=warsawScheduledClock(scheduled);
  if(local.minute!==7 || ![9,20].includes(local.hour)) return {ok:true,skipped:true,reason:'dst-candidate-not-target-local-time'};

  const st=await systemState(env);
  const active=String(st.scan_status?.value||'idle');
  if(['queued','running','cancelling'].includes(active)) {
    await setSystemStates(env,{auto_scan_last_trigger:new Date(scheduled).toISOString(),auto_scan_last_result:`skipped-active:${active}`});
    return {ok:true,skipped:true,reason:`scan-${active}`};
  }
  // Idempotency: retries or a manual scan started close to this slot must not create
  // a duplicate Action run.
  const lastRequest=Date.parse(st.scan_requested_at?.value||'')||0;
  let lastFinished=0;
  try {
    const row=await env.DB.prepare(`SELECT finished_at FROM scan_runs ORDER BY id DESC LIMIT 1`).first();
    lastFinished=Date.parse(row?.finished_at||'')||0;
  } catch {}
  if(Math.max(lastRequest,lastFinished)>Date.now()-70*60*1000) {
    await setSystemStates(env,{auto_scan_last_trigger:new Date(scheduled).toISOString(),auto_scan_last_result:'skipped-recent-scan'});
    return {ok:true,skipped:true,reason:'recent-scan'};
  }

  await setSystemStates(env,{auto_scan_last_trigger:new Date(scheduled).toISOString(),auto_scan_last_result:'dispatching'});
  const d=await dispatchScan(env);
  await setSystemStates(env,{
    auto_scan_last_result:d.ok?'dispatched':`error:${d.code||d.http_status||'unknown'}`,
    auto_scan_last_error:d.ok?'':String(d.message||'unknown error').slice(0,500)
  });
  return d;
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


async function syncGithubScanState(env, st=null) {
  st=st||await systemState(env);
  const current=String(st.scan_status?.value||'idle');
  if(!['queued','running','cancelling'].includes(current)) return;
  if(!env.GITHUB_DISPATCH_TOKEN||!env.GITHUB_REPO) return;
  let runId=Number(st.scan_github_run_id?.value||0);
  if(!runId) {
    try { runId=await resolveActiveScanRun(env); } catch(e) { console.warn('resolve run',e?.message||e); }
  }
  const now=new Date().toISOString();
  if(!runId) {
    const requested=Date.parse(st.scan_requested_at?.value||'')||0;
    const patch={scan_github_checked_at:now};
    if(requested&&Date.now()-requested>90000) {
      patch.scan_phase='GitHub Actions: nadal nie znaleziono uruchomionego workflow';
      patch.last_error='Skan został zlecony, ale przez ponad 90 s nie znaleziono workflow run. Sprawdź Actions, token i branch.';
    }
    await setSystemStates(env,patch);
    return;
  }
  const r=await githubRequest(env,`/actions/runs/${runId}`);
  if(!r.ok){await setSystemStates(env,{scan_github_checked_at:now});return;}
  const d=await r.json();
  const ghStatus=String(d.status||'');const conclusion=String(d.conclusion||'');
  const patch={scan_github_run_id:String(runId),scan_github_status:ghStatus,scan_github_conclusion:conclusion,scan_github_run_url:String(d.html_url||''),scan_github_checked_at:now};
  if(ghStatus==='queued'||ghStatus==='waiting'||ghStatus==='requested'||ghStatus==='pending') {
    if(current==='queued') patch.scan_phase='GitHub Actions: w kolejce';
  } else if(ghStatus==='in_progress') {
    // main.py will overwrite this with portal-level phases as soon as Python starts.
    if(current==='queued') patch.scan_status='running';
    const existingPhase=String(st.scan_phase?.value||'');
    if(!existingPhase||/GitHub Actions|czekam na runner/i.test(existingPhase)) patch.scan_phase='GitHub Actions: runner pracuje, uruchamiam skaner';
  } else if(ghStatus==='completed') {
    if(conclusion==='success') {
      // Normally scraper/main.py already changed the state to idle. This is a safety net.
      if(['queued','running','cancelling'].includes(current)) {patch.scan_status='idle';patch.scan_phase='gotowe';}
    } else if(conclusion==='cancelled') {
      patch.scan_status='cancelled';patch.scan_phase='zatrzymany';patch.last_error='';
    } else {
      patch.scan_status='error';patch.scan_phase=`GitHub Actions: ${conclusion||'błąd'}`;
      patch.last_error=`Workflow ${runId} zakończył się: ${conclusion||'unknown'}. Otwórz Actions / diagnostykę.`;
    }
  }
  await setSystemStates(env,patch);
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
    if(data==='status:short'){const s=await botStatus(env);return await done(send(statusShortOrLive(s),{inline_keyboard:[[{text:'📋 Pełny status',callback_data:'status:long'}],[{text:'⬅️ Menu',callback_data:'menu'}]]}));}
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
  else if(text==='/status'){const s=await botStatus(env);await send(statusShortOrLive(s),{inline_keyboard:[[{text:'📋 Pełny status',callback_data:'status:long'}],[{text:'⬅️ Menu',callback_data:'menu'}]]});}
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

  // v1.5.3 self-healing D1 cleanup: after deploying the Worker, simply opening
  // the authenticated Mini App removes confirmed legacy false positives immediately.
  if (['/api/me','/api/stats','/api/status','/api/listings'].includes(url.pathname)) {
    try { await runWorkerMaintenance(env); } catch(e) { console.warn('worker maintenance',e?.message||e); }
  }

  if (url.pathname === '/api/me') return json({ ok: true, user });

  if (url.pathname === '/api/stats') return json(await databaseStats(env));
  if (url.pathname === '/api/status') return json(await botStatus(env),200,{'Cache-Control':'no-store, no-cache, must-revalidate'});

  const imageMatch = url.pathname.match(/^\/api\/listing\/(\d+)\/image$/);
  if (imageMatch && req.method === 'GET') {
    const listing=await env.DB.prepare(`SELECT image_url,canonical_url FROM listings WHERE id=?`).bind(Number(imageMatch[1])).first();
    if(!listing?.image_url)return new Response('image unavailable',{status:404});
    let remote;
    try{remote=new URL(String(listing.image_url));}catch{return new Response('bad image url',{status:404});}
    if(!['http:','https:'].includes(remote.protocol))return new Response('bad image url',{status:404});
    const headers=new Headers({'Accept':'image/avif,image/webp,image/apng,image/*,*/*;q=0.8','User-Agent':'Mozilla/5.0 PropertyRadar/1.6.1'});
    try{if(listing.canonical_url)headers.set('Referer',String(listing.canonical_url));}catch{}
    let upstream;
    try{upstream=await fetch(remote.toString(),{headers,redirect:'follow'});}catch{return new Response('image fetch failed',{status:404});}
    if(!upstream.ok)return new Response('image fetch failed',{status:404});
    const contentType=upstream.headers.get('content-type')||'image/jpeg';
    if(!contentType.toLowerCase().startsWith('image/'))return new Response('not an image',{status:404});
    const outHeaders=new Headers({'Content-Type':contentType,'Cache-Control':'private, max-age=21600','X-Content-Type-Options':'nosniff'});
    const len=upstream.headers.get('content-length');if(len)outHeaders.set('Content-Length',len);
    return new Response(upstream.body,{status:200,headers:outHeaders});
  }

  if (url.pathname === '/api/listings') {
    // Mini App is plots-only. The feed must also work before the optional blacklist
    // migration has been run on an existing D1 database.
    let rows;
    try {
      rows = await env.DB.prepare(`SELECT * FROM listings WHERE category='plot' AND COALESCE(source_status,'active')<>'invalid-parser' AND NOT EXISTS (SELECT 1 FROM listing_blacklist b WHERE b.canonical_url=listings.canonical_url) ORDER BY COALESCE(published_at,first_seen) DESC, id DESC LIMIT 2500`).all();
    } catch {
      rows = await env.DB.prepare(`SELECT * FROM listings WHERE category='plot' AND COALESCE(source_status,'active')<>'invalid-parser' ORDER BY COALESCE(published_at,first_seen) DESC, id DESC LIMIT 2500`).all();
    }
    const visible=(rows.results || []).filter((r)=>!rejectLegacyPublicListing(r));
    return json({ listings: visible, stats: await databaseStats(env) },200,{'Cache-Control':'no-store, no-cache, must-revalidate'});
  }

  const m = url.pathname.match(/^\/api\/listing\/(\d+)\/history$/);
  if (m) {
    const rows = await env.DB.prepare(`SELECT seen_at, price FROM price_history WHERE listing_id=? ORDER BY seen_at ASC`).bind(Number(m[1])).all();
    return json({ history: rows.results || [] });
  }

  const rejectMatch = url.pathname.match(/^\/api\/listing\/(\d+)\/reject$/);
  if (rejectMatch && req.method === 'POST') {
    await env.DB.prepare(`CREATE TABLE IF NOT EXISTS listing_blacklist (canonical_url TEXT PRIMARY KEY, reason TEXT, source TEXT, title TEXT, created_at TEXT NOT NULL)`).run();
    if (user.role !== 'admin' && user.uid !== 'web') return json({ error:'admin required' },403);
    const id=Number(rejectMatch[1]);
    const listing=await env.DB.prepare(`SELECT id,canonical_url,source,title FROM listings WHERE id=?`).bind(id).first();
    if(!listing)return json({error:'listing not found'},404);
    const body=await req.json().catch(()=>({}));
    const reason=String(body.reason||'manual-invalid').slice(0,120);
    const now=new Date().toISOString();
    await env.DB.batch([
      env.DB.prepare(`INSERT INTO listing_blacklist(canonical_url,reason,source,title,created_at) VALUES(?,?,?,?,?)
        ON CONFLICT(canonical_url) DO UPDATE SET reason=excluded.reason,source=excluded.source,title=excluded.title,created_at=excluded.created_at`)
        .bind(listing.canonical_url,reason,listing.source,listing.title,now),
      env.DB.prepare(`DELETE FROM price_history WHERE listing_id=?`).bind(id),
      env.DB.prepare(`DELETE FROM listings WHERE id=?`).bind(id),
    ]);
    return json({ok:true,blocked_url:listing.canonical_url});
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
  async scheduled(controller, env, ctx) {
    // Automatic scans are triggered by Cloudflare, not by the user's PC and not by
    // GitHub's best-effort scheduler. GitHub Actions remains the scan executor.
    ctx.waitUntil(runAutomaticScan(env,controller.scheduledTime).catch(async e=>{
      console.error('automatic scan cron',e);
      try { await setSystemStates(env,{auto_scan_last_result:'error',auto_scan_last_error:String(e?.message||e).slice(0,500)}); } catch {}
      throw e;
    }));
  },
};
