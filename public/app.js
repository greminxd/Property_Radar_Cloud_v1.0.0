const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const tg = window.Telegram?.WebApp;
try {
  tg?.ready?.();
  tg?.expand?.();
  tg?.setHeaderColor?.('#f5f5f7');
  tg?.setBackgroundColor?.('#f5f5f7');
  tg?.setBottomBarColor?.('#f5f5f7');
} catch {}
let listings = [];
let stats = {};
let quick = '30d';
let me = null;
let scanStatus = null;
let scanPollTimer = null;
let listingsRefreshAt = 0;

const state = {
  categories: new Set(), types: new Set(), planning: new Set(), localities: new Set(), sources: new Set(),
  status: 'active', age: '30', minArea: '', maxArea: '', maxPrice: '', maxPpm: '', maxRcnPremium: '',
  onlyPhone: false, onlyParcel: false, onlyPriceChanges: false,
};

const fmtMoney = (v) => v == null ? '—' : `${Math.round(+v).toLocaleString('pl-PL')} zł`;
const fmtArea = (v) => v == null ? '—' : `${Math.round(+v).toLocaleString('pl-PL')} m²`;
const fmtPpm = (v) => v == null ? '—' : `${(+v).toFixed(2).replace('.', ',')} zł/m²`;
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]));
const loc = (r) => r.area_locality || r.location || 'lokalizacja nieustalona';
const sizeBadge = (a) => !a ? '' : a>=10000?'👑 1 HA+':a>=5000?'🟪 5000+':a>=3000?'🟦 3000+':a>=1500?'🟩 1500+':'';
const isArchived = (r) => +r.active !== 1 || String(r.source_status || '').toLowerCase() === 'archived';

function dateOnly(v){
  if(!v) return '—';
  try{return new Intl.DateTimeFormat('pl-PL',{dateStyle:'short',timeZone:'Europe/Warsaw'}).format(new Date(v));}catch{return String(v).slice(0,10)}
}
function dateTime(v){
  if(!v) return '—';
  try{return new Intl.DateTimeFormat('pl-PL',{dateStyle:'short',timeStyle:'short',timeZone:'Europe/Warsaw'}).format(new Date(v));}catch{return String(v)}
}
function publishedAgeDays(r){
  if(!r.published_at) return null;
  const t=new Date(r.published_at).getTime();
  if(!Number.isFinite(t)) return null;
  return (Date.now()-t)/86400000;
}
function trustedRcn(r){
  const med=Number(r?.rcn_median_ppm),n=Number(r?.rcn_count||0),q=String(r?.rcn_quality||'').toLowerCase();
  const last=r?.rcn_last_date?new Date(r.rcn_last_date).getTime():null;
  const dateOk=last==null || (Number.isFinite(last)&&last<=Date.now()+86400000);
  return Number.isFinite(med)&&med>=0.5&&med<=3000&&n>=3&&dateOk&&!q.startsWith('brak')&&!q.startsWith('za mało');
}
function rcnDeltaPct(r){
  if(!r.price_m2 || !trustedRcn(r)) return null;
  const d=((+r.price_m2 - +r.rcn_median_ppm) / +r.rcn_median_ppm) * 100;
  // Last-resort UI guard: unit/parser corruption must never be presented as a market insight.
  return Number.isFinite(d) && Math.abs(d)<=1500 ? d : null;
}
function marketDeltaPct(r){
  if(!r.price_m2 || !r.median_comparable) return null;
  return ((+r.price_m2 - +r.median_comparable) / +r.median_comparable) * 100;
}
function pctText(v){return v==null?'—':`${v>=0?'+':''}${v.toFixed(1).replace('.',',')}%`;}
function toast(s){const el=$('#toast');el.textContent=s;el.classList.add('show');setTimeout(()=>el.classList.remove('show'),2500)}

async function api(path, opts={}) {
  const isRead=!opts.method||String(opts.method).toUpperCase()==='GET';
  const url=isRead?`${path}${path.includes('?')?'&':'?'}_=${Date.now()}`:path;
  const r = await fetch(url, { credentials:'include', cache:'no-store', ...opts, headers:{'content-type':'application/json','cache-control':'no-cache', ...(opts.headers||{})} });
  let body = {};
  try { body = await r.json(); } catch {}
  if (!r.ok) throw Object.assign(new Error(body.error || `HTTP ${r.status}`), {status:r.status});
  return body;
}

async function telegramAutoLogin(){
  if (!tg?.initData) return false;
  try {
    tg.ready(); tg.expand();
    await api('/api/auth/telegram',{method:'POST',body:JSON.stringify({initData:tg.initData})});
    return true;
  } catch(e){ console.warn('Telegram auth failed',e); return false; }
}

async function boot(){
  try { const m=await api('/api/me'); me=m.user||null; showApp(); await load(); startScanPolling(); return; }
  catch(e){ if(e.status!==401) console.warn(e); }
  if (await telegramAutoLogin()) { const m=await api('/api/me');me=m.user||null;showApp();await load();startScanPolling();return; }
  $('#login').classList.remove('hidden');
}
function showApp(){
  $('#login').classList.add('hidden'); $('#app').classList.remove('hidden');
  const admin=me?.role==='admin'||me?.uid==='web';
  $('#scanNowBtn').classList.toggle('hidden',!admin);
  $('#stopScanBtn').classList.toggle('hidden',!admin);
}
$('#loginForm').addEventListener('submit', async (e)=>{
  e.preventDefault(); $('#loginError').textContent='';
  try { await api('/api/login',{method:'POST',body:JSON.stringify({password:$('#password').value})});me=(await api('/api/me')).user||null;showApp();await load();startScanPolling(); }
  catch(e){ $('#loginError').textContent=e.message; }
});
$('#logoutBtn').addEventListener('click', async()=>{try{await api('/api/logout',{method:'POST',body:'{}'})}finally{location.reload()}});

async function load(){
  const [data,status]=await Promise.all([api('/api/listings'),api('/api/status').catch(()=>null)]);
  listings=data.listings||[]; stats=data.stats||{}; scanStatus=status||scanStatus;
  hydrateFilters(); renderStats(); syncStateToInputs(); render(); renderScanStatus();
}

function renderStats(){
  $('#marketMedian').textContent=fmtPpm(stats.median_ppm);
  $('#marketMean').textContent=fmtPpm(stats.avg_ppm);
  const rcnGlobalOk=Number(stats.rcn_count||0)>=3 && Number(stats.rcn_median_ppm)>=0.5 && Number(stats.rcn_median_ppm)<=3000;
  $('#rcnMedian').textContent=rcnGlobalOk?fmtPpm(stats.rcn_median_ppm):'—';
  $('#rcnMean').textContent=rcnGlobalOk?fmtPpm(stats.rcn_mean_ppm):'—';
  $('#rcnMeta').textContent=`Benchmark 24 mies.: ${stats.rcn_count||0} • archiwum historii: ${stats.rcn_history_rows||0} • działki z ID EGiB: ${stats.rcn_identified_parcels||0} • ostatnia: ${dateOnly(stats.rcn_last_date)}${stats.rcn_last_ppm?` • ${fmtPpm(stats.rcn_last_ppm)}`:''}`;
  $('#statActive').textContent=stats.plots||0;
  $('#stat30').textContent=stats.published30||0;
  $('#statArchived').textContent=stats.archived||0;
  $('#statPhone').textContent=stats.with_phone||0;
  $('#lastScan').textContent=`Ostatni skan: ${dateTime(stats.last_scan?.finished_at)}${stats.last_scan?` • źródła OK ${stats.last_scan.healthy_sources||0}/${stats.last_scan.total_sources||0}`:''} • baza: ${stats.total_rows||0} rekordów / ${stats.plots||0} aktywnych działek`;
}


function scanStateInfo(state){
  const m={
    idle:['ready','GOTOWY','Radar gotowy'],
    running:['running','SKANUJE','Skan w toku'],
    queued:['queued','W KOLEJCE','Skan oczekuje'],
    cancelling:['queued','ZATRZYMYWANIE','Zatrzymywanie skanu'],
    cancelled:['cancelled','ZATRZYMANY','Skan zatrzymany'],
    error:['error','BŁĄD','Skan zakończony błędem'],
    stale:['error','STALE','Skan prawdopodobnie zawieszony']
  };
  return m[state]||['ready',String(state||'GOTOWY').toUpperCase(),'Radar'];
}
function renderScanStatus(){
  if(!scanStatus)return;
  const stateName=String(scanStatus.scan_state||'idle'), p=scanStatus.scan_progress||{};
  const [cls,label,headline]=scanStateInfo(stateName);
  const running=['running','queued','cancelling'].includes(stateName);
  const done=Number(p.done_sources||0), total=Number(p.total_sources||0);
  let pct=total?Math.min(100,Math.max(0,done/total*100)):(running?3:0);
  if(stateName==='running'&&total&&done>=total)pct=92; // portale gotowe, ale trwa RCN/scoring/finalizacja
  $('#scanCommand').classList.toggle('running',stateName==='running');
  $('#scanHeadline').textContent=headline;
  const badge=$('#scanStateBadge');badge.className=`scan-state-badge ${cls}`;badge.innerHTML=`<i></i> ${label}`;
  $('#scanPhase').textContent=scanStatus.system?.scan_phase?.value||'Oczekiwanie na następny skan.';
  $('#scanProgressBar').style.width=`${pct}%`;
  $('#scanProgressText').textContent=total?`${done} / ${total}`:'—';
  $('#scanDownloaded').textContent=Number(p.downloaded_records||0).toLocaleString('pl-PL');
  $('#scanAccepted').textContent=Number(p.accepted_records||0).toLocaleString('pl-PL');
  $('#scanNext').textContent=scanStatus.next_scan||'—';
  $('#scanStarted').textContent=running?`Start: ${dateTime(scanStatus.system?.scan_started_at?.value)}`:`Ostatni skan: ${dateTime(scanStatus.last_scan?.finished_at)}`;
  const src=Array.isArray(p.sources)?p.sources:[];
  $('#scanSources').innerHTML=src.length?src.map(x=>{
    const c=x.status==='done'?(x.healthy?'done':'warn'):x.status==='running'?'running':'';
    const extra=x.status==='done'&&x.records!=null?` · ${x.records}`:'';
    return `<span class="source-chip ${c}" title="${esc(x.error||'')}">${esc(x.name||'?')}${extra}</span>`;
  }).join(''):'<span class="muted">Szczegółowy postęp pojawi się podczas skanu.</span>';
  const admin=me?.role==='admin'||me?.uid==='web';
  $('#scanNowBtn').disabled=!admin||running;
  $('#stopScanBtn').disabled=!admin||!['running','queued'].includes(stateName);
}
async function pollScanStatus(forceListings=false){
  try{
    const previous=scanStatus?.scan_state;
    scanStatus=await api('/api/status');renderScanStatus();
    const running=['running','queued','cancelling'].includes(String(scanStatus.scan_state||''));
    const now=Date.now();
    // During a scan listings are refreshed repeatedly. New records from completed
    // portals appear in the Mini App before the whole run finishes.
    if(forceListings || (running && now-listingsRefreshAt>5000) || (previous==='running'&&!running)){
      listingsRefreshAt=now;
      const data=await api('/api/listings');listings=data.listings||[];stats=data.stats||{};
      hydrateFilters();renderStats();render();
    }
  }catch(e){console.warn('scan poll',e);}
}
function startScanPolling(){
  if(scanPollTimer)clearInterval(scanPollTimer);
  pollScanStatus();
  scanPollTimer=setInterval(()=>pollScanStatus(),2500);
}
async function startScanNow(){
  const b=$('#scanNowBtn');b.disabled=true;
  try{await api('/api/scan',{method:'POST',body:'{}'});toast('Skan zlecony');await pollScanStatus(true);}
  catch(e){toast(e.message);}
}
async function stopCurrentScan(){
  if(!confirm('Zatrzymać bieżący skan GitHub Actions? Dane zapisane do tej chwili zostaną w bazie.'))return;
  const b=$('#stopScanBtn');b.disabled=true;
  try{await api('/api/scan/stop',{method:'POST',body:'{}'});toast('Skan zatrzymany');await pollScanStatus(true);}
  catch(e){toast(e.message);}
}
$('#scanNowBtn').addEventListener('click',startScanNow);
$('#stopScanBtn').addEventListener('click',stopCurrentScan);
$('#refreshScanBtn').addEventListener('click',()=>pollScanStatus(true));

function choiceId(prefix,v){
  const raw=encodeURIComponent(v).replace(/%/g,''); return `${prefix}-${raw.slice(0,42)}`;
}
function choices(container, values, set, labels={}){
  const el=$(container); const old=new Set(set); el.innerHTML='';
  values.forEach(v=>{
    const id=choiceId(container.slice(1),v); const label=document.createElement('label');label.className='choice';
    label.innerHTML=`<input type="checkbox" id="${esc(id)}" ${old.has(v)?'checked':''}><span>${esc(labels[v]||v)}</span>`;
    label.querySelector('input').addEventListener('change',ev=>ev.target.checked?set.add(v):set.delete(v)); el.appendChild(label);
  });
}
function hydrateFilters(){
  const types=[...new Set(listings.filter(x=>x.category==='plot').map(x=>x.plot_type||'nieustalona'))].sort((a,b)=>a.localeCompare(b,'pl'));
  const plans=[...new Set(listings.filter(x=>x.category==='plot').map(x=>x.planning_status||'nieustalone'))].sort((a,b)=>a.localeCompare(b,'pl'));
  const places=[...new Set(listings.map(loc))].filter(x=>x!=='lokalizacja nieustalona').sort((a,b)=>a.localeCompare(b,'pl'));
  const sources=[...new Set(listings.map(x=>x.source).filter(Boolean))].sort((a,b)=>a.localeCompare(b,'pl'));
  choices('#categoryFilters',['plot'],state.categories,{plot:'Działki'});
  choices('#typeFilters',types,state.types); choices('#planningFilters',plans,state.planning);
  choices('#localityFilters',places,state.localities); choices('#sourceFilters',sources,state.sources);
}
function syncStateToInputs(){
  $('#statusFilter').value=state.status; $('#ageFilter').value=state.age;
  $('#minArea').value=state.minArea; $('#maxArea').value=state.maxArea; $('#maxPrice').value=state.maxPrice; $('#maxPpm').value=state.maxPpm; $('#maxRcnPremium').value=state.maxRcnPremium;
  $('#onlyPhone').checked=state.onlyPhone; $('#onlyParcel').checked=state.onlyParcel; $('#onlyPriceChanges').checked=state.onlyPriceChanges;
}
function syncInputs(){
  state.status=$('#statusFilter').value;state.age=$('#ageFilter').value;
  state.minArea=$('#minArea').value;state.maxArea=$('#maxArea').value;state.maxPrice=$('#maxPrice').value;state.maxPpm=$('#maxPpm').value;state.maxRcnPremium=$('#maxRcnPremium').value;
  state.onlyPhone=$('#onlyPhone').checked;state.onlyParcel=$('#onlyParcel').checked;state.onlyPriceChanges=$('#onlyPriceChanges').checked;
}
function activeFilterCount(){
  const defaultStatus=state.status==='active',defaultAge=state.age==='30';
  return state.categories.size+state.types.size+state.planning.size+state.localities.size+state.sources.size+
    [!defaultStatus,!defaultAge,state.minArea,state.maxArea,state.maxPrice,state.maxPpm,state.maxRcnPremium,state.onlyPhone,state.onlyParcel,state.onlyPriceChanges].filter(Boolean).length;
}
function ageMatches(r,mode){
  const age=publishedAgeDays(r);
  if(mode==='all') return true;
  if(mode==='unknown') return age==null;
  if(age==null) return false;
  return age>=-1 && age<=+mode;
}
function match(r){
  const q=$('#search').value.trim().toLowerCase();
  if(q && ![r.title,r.description,r.area_locality,r.location,r.parcel_number,r.phone,r.plot_type,r.planning_status,r.source,r.source_status].join(' ').toLowerCase().includes(q)) return false;
  if(state.status==='active' && isArchived(r))return false;
  if(state.status==='archived' && !isArchived(r))return false;
  if(!ageMatches(r,state.age))return false;
  if(state.categories.size&&!state.categories.has(r.category))return false;
  if(r.category==='plot'&&state.types.size&&!state.types.has(r.plot_type||'nieustalona'))return false;
  if(r.category==='plot'&&state.planning.size&&!state.planning.has(r.planning_status||'nieustalone'))return false;
  if(state.localities.size&&!state.localities.has(loc(r)))return false;
  if(state.sources.size&&!state.sources.has(r.source))return false;
  if(state.minArea&&(+r.area_m2||0)<+state.minArea)return false;
  if(state.maxArea&&(+r.area_m2||Infinity)>+state.maxArea)return false;
  if(state.maxPrice&&(+r.price||Infinity)>+state.maxPrice)return false;
  if(state.maxPpm&&(+r.price_m2||Infinity)>+state.maxPpm)return false;
  if(state.maxRcnPremium!==''){
    const d=rcnDeltaPct(r); if(d==null || d>+state.maxRcnPremium)return false;
  }
  if(state.onlyPhone&&!r.phone)return false;
  if(state.onlyParcel&&!r.parcel_number)return false;
  if(state.onlyPriceChanges&&!r.last_meaningful_price_change_at)return false;
  return true;
}
function sorted(rows){
  const mode=$('#sort').value; const num=(x,def)=>x==null?def:+x;
  return [...rows].sort((a,b)=>{
    if(mode==='distance')return num(a.distance_km,999)-num(b.distance_km,999);
    if(mode==='priceAsc')return num(a.price,Infinity)-num(b.price,Infinity);
    if(mode==='priceDesc')return num(b.price,-1)-num(a.price,-1);
    if(mode==='ppm')return num(a.price_m2,Infinity)-num(b.price_m2,Infinity);
    if(mode==='areaDesc')return num(b.area_m2,-1)-num(a.area_m2,-1);
    if(mode==='updated')return new Date(b.updated_at||0)-new Date(a.updated_at||0);
    if(mode==='rcnDelta')return num(rcnDeltaPct(a),Infinity)-num(rcnDeltaPct(b),Infinity);
    return new Date(b.published_at||0)-new Date(a.published_at||0);
  });
}
function badge(text,cls=''){return text?`<span class="badge ${cls}">${esc(text)}</span>`:''}
function pricePosition(r){
  const ask=marketDeltaPct(r),rcn=rcnDeltaPct(r);
  const pieces=[];
  if(ask!=null)pieces.push(`${pctText(ask)} vs ogłoszenia`);
  if(rcn!=null)pieces.push(`${pctText(rcn)} vs RCN`);
  return pieces.join(' • ')||'brak wystarczających porównań';
}
function analyticsHtml(r){
  const marketCount=r.comparable_count||0, rcnCount=r.rcn_count||0, rok=trustedRcn(r);
  return `<div class="analytics-grid">
    <div class="analytics-box asking"><span>📢 CENY Z OGŁOSZEŃ</span><b>${marketCount>=3?fmtPpm(r.median_comparable):'—'}</b><small>${marketCount>=3?`mediana • średnia ${fmtPpm(r.market_mean_comparable)} • n=${marketCount}`:`za mało porównywalnych ofert • n=${marketCount}`} • ${esc(r.comparison_quality||'—')}</small></div>
    <div class="analytics-box rcn"><span>🏛 REALNE TRANSAKCJE RCN</span><b>${rok?fmtPpm(r.rcn_median_ppm):'—'}</b><small>${rok?`mediana • średnia ${fmtPpm(r.rcn_mean_ppm)} • n=${rcnCount}`:`brak wiarygodnego benchmarku • n=${rcnCount}`}${r.rcn_radius_km?` • ≤${esc(r.rcn_radius_km)} km`:''} • ${esc(r.rcn_quality||'—')}</small>${rok&&r.rcn_last_date?`<em>ostatnia: ${dateOnly(r.rcn_last_date)} • ${fmtPpm(r.rcn_last_ppm)}</em>`:''}</div>
  </div>`;
}
function card(r){
  const archived=isArchived(r); const distance=r.distance_km==null?'—':`${(+r.distance_km).toFixed(1)} km`;
  const img=r.image_url?`<img src="${esc(r.image_url)}" loading="lazy" referrerpolicy="no-referrer" onerror="this.remove()">`:'';
  const market=marketDeltaPct(r), rcn=rcnDeltaPct(r);
  const tags=[r.plot_type&&r.plot_type!=='nieustalona'?r.plot_type:'',r.planning_status&&r.planning_status!=='nieustalone'?r.planning_status:'',r.parcel_number?`dz. ${r.parcel_number}`:'',Number(r.rcn_history_count||0)>0?'RCN historia':''].filter(Boolean);
  return `<article class="card offer-card ${archived?'archived-card':''}" data-id="${r.id}">
    <div class="photo offer-photo">${img}<div class="photo-fallback">🌱</div><span class="source-tag">${esc(r.source||'?')}</span>${archived?'<span class="archive-overlay">ARCHIWALNA</span>':''}</div>
    <div class="card-body offer-body">
      <div class="offer-kicker"><span>📍 ${esc(loc(r))}</span><span>${distance}</span></div>
      <div class="offer-title">${esc(r.title||'Działka')}</div>
      <div class="offer-price-row"><strong>${fmtMoney(r.price)}</strong><span>${fmtPpm(r.price_m2)}</span></div>
      <div class="offer-facts"><div><b>${fmtArea(r.area_m2)}</b><span>powierzchnia</span></div><div><b>${dateOnly(r.published_at)}</b><span>dodano</span></div>${r.phone?'<div><b>☎ dostępny</b><span>kontakt</span></div>':''}</div>
      ${tags.length?`<div class="offer-tags">${tags.map(x=>`<span>${esc(x)}</span>`).join('')}</div>`:''}
      <div class="offer-comparison"><span><small>vs ogłoszenia</small><b>${pctText(market)}</b></span><span class="rcn"><small>vs RCN</small><b>${pctText(rcn)}</b></span></div>
      ${Number(r.rcn_history_count||0)>0?`<div class="offer-history">🧾 Historia RCN • ${dateOnly(r.rcn_history_last_date)} • ${fmtMoney(r.rcn_history_last_price)}</div>`:''}
      <div class="offer-actions"><button class="detail-btn" data-detail="${r.id}">Szczegóły</button>${r.phone?`<a class="phone-btn" href="tel:${esc(r.phone)}">Zadzwoń</a>`:''}<a class="open-btn" href="${esc(r.canonical_url)}" target="_blank" rel="noopener">Otwórz</a></div>
    </div>
  </article>`;
}
function presetLabel(){
  const ageLabel={7:'≤7 dni',30:'≤30 dni',90:'≤90 dni',all:'dowolna data',unknown:'bez daty'}[state.age]||state.age;
  const statusLabel={active:'aktywne',archived:'archiwalne',all:'wszystkie statusy'}[state.status];
  $('#activePreset').textContent=`• ${statusLabel} • publikacja ${ageLabel}`;
}
function render(){
  syncInputs(); const rows=sorted(listings.filter(match)); $('#resultCount').textContent=rows.length; $('#filterCount').textContent=activeFilterCount();presetLabel();
  if(rows.length){
    $('#cards').innerHTML=rows.map(card).join('');
  } else {
    const activePlots=Number(stats.plots||0), unknown=Number(stats.unknown_date||0), total=Number(stats.total_rows||0);
    const canShowAll=listings.some(r=>!isArchived(r));
    $('#cards').innerHTML=`<div class="empty"><b>Brak działek dla bieżącego filtra.</b><br><span class="muted">Domyślnie pokazujemy aktywne oferty z datą publikacji z ostatnich 30 dni.<br>Baza D1: ${total} rekordów • aktywne działki: ${activePlots} • bez ustalonej daty publikacji: ${unknown}.</span>${canShowAll?`<br><button class="open-btn" id="emptyShowAll" type="button">Pokaż wszystkie aktywne działki z bazy</button>`:''}</div>`;
    const showAll=$('#emptyShowAll');
    if(showAll) showAll.addEventListener('click',()=>{state.status='active';state.age='all';quick='custom';syncStateToInputs();$$('[data-quick]').forEach(x=>x.classList.remove('active'));render();});
  }
  $$('[data-detail]').forEach(b=>b.addEventListener('click',()=>showDetail(+b.dataset.detail)));
}
async function showDetail(id){
  const r=listings.find(x=>+x.id===id); if(!r)return; let history=[],rcnTx=[],parcelHistory=[];
  try{const [h,t]=await Promise.all([api(`/api/listing/${id}/history`),api(`/api/listing/${id}/rcn`)]);history=h.history||[];rcnTx=t.transactions||[];parcelHistory=t.parcel_history||[]}catch{try{history=(await api(`/api/listing/${id}/history`)).history||[]}catch{}}
  const marketDelta=marketDeltaPct(r),rcnDelta=rcnDeltaPct(r);
  $('#detailBody').innerHTML=`<div class="detail-content"><h2>${esc(r.title||'Oferta')}</h2><div class="location">📍 ${esc(loc(r))} · ${r.distance_km==null?'?':(+r.distance_km).toFixed(1)} km · ${esc(r.source)}</div>
    <div class="detail-grid">
      ${[['Cena',fmtMoney(r.price)],['Powierzchnia',fmtArea(r.area_m2)],['Cena/m²',fmtPpm(r.price_m2)],['Status',isArchived(r)?'archiwalna / nieaktywna':'aktywna'],['Typ',r.plot_type||'nieustalona'],['Plan / WZ',r.planning_status||'nieustalone'],['Nr działki',r.parcel_number||'—'],['ID działki EGiB',r.parcel_id||'—'],['Telefon',r.phone||'brak / ukryty'],['Pierwotnie dodane',dateOnly(r.published_at)],['Ostatnia aktualizacja',dateOnly(r.updated_at)],['Radar pierwszy raz',dateTime(r.first_seen)],['Portal',r.source||'—']].map(([a,b])=>`<div class="detail-item"><span>${esc(a)}</span><b>${esc(b)}</b></div>`).join('')}
    </div>
    ${r.area_warning?`<div class="badge warn" style="margin-top:12px">${esc(r.area_warning)}</div>`:''}
    ${isArchived(r)&&r.archive_reason?`<div class="archive-note">⚠ ${esc(r.archive_reason)}</div>`:''}
    ${r.category==='plot'?`<h3 class="detail-section-title">Porównanie ceny za m²</h3>${analyticsHtml(r)}<div class="comparison-summary"><b>Cena/m² oferty vs mediana ogłoszeń:</b> ${pctText(marketDelta)} (${fmtPpm(r.price_m2)} vs ${fmtPpm(r.median_comparable)})<br><b>Cena/m² oferty vs mediana transakcji RCN:</b> ${pctText(rcnDelta)} (${fmtPpm(r.price_m2)} vs ${trustedRcn(r)?fmtPpm(r.rcn_median_ppm):'—'})</div>
      <div class="history rcn-history"><h3>🧾 Historia transakcyjna tej działki (RCN)</h3>${parcelHistory.length?parcelHistory.map(t=>{const whole=String(t.history_match||'').includes('property-level');return `<div class="tx-row"><div><b>${dateOnly(t.transaction_date)}</b><span>dz. ${esc(t.parcel_number||r.parcel_number||'?')} • ${fmtArea(t.area_m2)}${whole?' • ⚠ cena całej nieruchomości obejmującej działkę':' • cena działki'}</span></div><strong>${fmtMoney(t.price)}<small style="display:block">${fmtPpm(t.price_m2)}</small></strong></div>`}).join(''):(r.parcel_number?'<div class="muted">RCN nie zawiera wiarygodnie dopasowanej wcześniejszej transakcji tej działki.</div>':'<div class="muted">Brak numeru działki w ogłoszeniu — nie można bezpiecznie sprawdzić historii konkretnej parceli.</div>')}</div>
      <div class="history rcn-history"><h3>Porównywalne transakcje w okolicy</h3>${rcnTx.length?rcnTx.map(t=>`<div class="tx-row"><div><b>${dateOnly(t.transaction_date)}</b><span>${t.parcel_number?`dz. ${esc(t.parcel_number)} • `:''}${fmtArea(t.area_m2)} • ${(+t.distance_km).toFixed(1)} km</span></div><strong>${fmtPpm(t.price_m2)}</strong></div>`).join(''):'<div class="muted">Brak porównywalnych transakcji RCN dla tej lokalizacji/metrażu.</div>'}</div>`:''}
    <div class="description">${esc(r.description||'Brak opisu w parserze.')}</div>
    <div class="history"><h3>Historia ceny w Radarze</h3>${history.length?history.map(x=>`<div class="history-row"><span>${dateTime(x.seen_at)}</span><b>${fmtMoney(x.price)}</b></div>`).join(''):'<div class="muted">Brak zarejestrowanych zmian ceny.</div>'}</div>
    <div class="card-actions"><a class="open-btn" href="${esc(r.canonical_url)}" target="_blank" rel="noopener">Otwórz źródło</a>${r.phone?`<a class="phone-btn" href="tel:${esc(r.phone)}">☎ ${esc(r.phone)}</a>`:''}</div></div>`;
  $('#detailDialog').showModal();
}

$('#detailClose').addEventListener('click',()=>$('#detailDialog').close());
$('#detailDialog').addEventListener('click',e=>{if(e.target===$('#detailDialog'))$('#detailDialog').close()});
$('#search').addEventListener('input',render); $('#sort').addEventListener('change',render);
function selectQuick(name){
  quick=name; $$('[data-quick]').forEach(x=>x.classList.toggle('active',x.dataset.quick===name));
  if(name!=='reset'){state.minArea='';state.onlyPriceChanges=false;state.onlyPhone=false;}
  if(name==='7d'){state.status='active';state.age='7';}
  else if(name==='30d'){state.status='active';state.age='30';}
  else if(name==='drops'){state.status='active';state.age='all';state.onlyPriceChanges=true;}
  else if(name==='phone'){state.status='active';state.age='30';state.onlyPhone=true;}
  else if(name==='large'){state.status='active';state.age='30';state.minArea='1500';}
  else if(name==='all'){state.status='active';state.age='all';}
  else if(name==='reset'){resetFilters(); quick='30d';$$('[data-quick]').forEach(x=>x.classList.toggle('active',x.dataset.quick==='30d'));}
  syncStateToInputs(); render();
}
$$('[data-quick]').forEach(b=>b.addEventListener('click',()=>selectQuick(b.dataset.quick)));

function openFilters(){hydrateFilters();syncStateToInputs();$('#filters').classList.add('open');$('#backdrop').classList.remove('hidden')}
function closeFilters(){syncInputs();$('#filters').classList.remove('open');$('#backdrop').classList.add('hidden')}
$('#filtersBtn').addEventListener('click',openFilters);$('#filtersClose').addEventListener('click',closeFilters);$('#backdrop').addEventListener('click',closeFilters);$('#applyFilters').addEventListener('click',()=>{closeFilters();quick='custom';$$('[data-quick]').forEach(x=>x.classList.remove('active'));render()});
function resetFilters(){
  state.categories.clear();state.types.clear();state.planning.clear();state.localities.clear();state.sources.clear();state.status='active';state.age='30';state.minArea='';state.maxArea='';state.maxPrice='';state.maxPpm='';state.maxRcnPremium='';state.onlyPhone=false;state.onlyParcel=false;state.onlyPriceChanges=false;
  syncStateToInputs();hydrateFilters();
}
$('#clearFilters').addEventListener('click',()=>{resetFilters();quick='30d';$$('[data-quick]').forEach(x=>x.classList.toggle('active',x.dataset.quick==='30d'));render()});

boot();
