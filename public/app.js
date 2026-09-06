const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const tg = window.Telegram?.WebApp;
let listings = [];
let stats = {};
let quick = '30d';

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
function rcnDeltaPct(r){
  if(!r.price_m2 || !r.rcn_median_ppm) return null;
  return ((+r.price_m2 - +r.rcn_median_ppm) / +r.rcn_median_ppm) * 100;
}
function marketDeltaPct(r){
  if(!r.price_m2 || !r.median_comparable) return null;
  return ((+r.price_m2 - +r.median_comparable) / +r.median_comparable) * 100;
}
function pctText(v){return v==null?'—':`${v>=0?'+':''}${v.toFixed(1).replace('.',',')}%`;}
function toast(s){const el=$('#toast');el.textContent=s;el.classList.add('show');setTimeout(()=>el.classList.remove('show'),2500)}

async function api(path, opts={}) {
  const r = await fetch(path, { credentials:'include', ...opts, headers:{'content-type':'application/json', ...(opts.headers||{})} });
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
  try { await api('/api/me'); showApp(); return load(); }
  catch(e){ if(e.status!==401) console.warn(e); }
  if (await telegramAutoLogin()) { showApp(); return load(); }
  $('#login').classList.remove('hidden');
}
function showApp(){ $('#login').classList.add('hidden'); $('#app').classList.remove('hidden'); }
$('#loginForm').addEventListener('submit', async (e)=>{
  e.preventDefault(); $('#loginError').textContent='';
  try { await api('/api/login',{method:'POST',body:JSON.stringify({password:$('#password').value})}); showApp(); await load(); }
  catch(e){ $('#loginError').textContent=e.message; }
});
$('#logoutBtn').addEventListener('click', async()=>{try{await api('/api/logout',{method:'POST',body:'{}'})}finally{location.reload()}});

async function load(){
  const data=await api('/api/listings'); listings=data.listings||[]; stats=data.stats||{};
  hydrateFilters(); renderStats(); syncStateToInputs(); render();
}

function renderStats(){
  $('#marketMedian').textContent=fmtPpm(stats.median_ppm);
  $('#marketMean').textContent=fmtPpm(stats.avg_ppm);
  $('#rcnMedian').textContent=fmtPpm(stats.rcn_median_ppm);
  $('#rcnMean').textContent=fmtPpm(stats.rcn_mean_ppm);
  $('#rcnMeta').textContent=`Transakcje: ${stats.rcn_count||0} • ostatnia: ${dateOnly(stats.rcn_last_date)}${stats.rcn_last_ppm?` • ${fmtPpm(stats.rcn_last_ppm)}`:''} • okno: 24 miesiące`;
  $('#statActive').textContent=stats.plots||0;
  $('#stat30').textContent=stats.published30||0;
  $('#statArchived').textContent=stats.archived||0;
  $('#statPhone').textContent=stats.with_phone||0;
  $('#lastScan').textContent=`Ostatni skan: ${dateTime(stats.last_scan?.finished_at)}${stats.last_scan?` • źródła OK ${stats.last_scan.healthy_sources||0}/${stats.last_scan.total_sources||0}`:''}`;
}

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
  const marketCount=r.comparable_count||0, rcnCount=r.rcn_count||0;
  return `<div class="analytics-grid">
    <div class="analytics-box asking"><span>📢 CENY Z OGŁOSZEŃ</span><b>${fmtPpm(r.median_comparable)}</b><small>mediana • średnia ${fmtPpm(r.market_mean_comparable)} • n=${marketCount} • ${esc(r.comparison_quality||'—')}</small></div>
    <div class="analytics-box rcn"><span>🏛 REALNE TRANSAKCJE RCN</span><b>${fmtPpm(r.rcn_median_ppm)}</b><small>mediana • średnia ${fmtPpm(r.rcn_mean_ppm)} • n=${rcnCount}${r.rcn_radius_km?` • ≤${esc(r.rcn_radius_km)} km`:''} • ${esc(r.rcn_quality||'—')}</small>${r.rcn_last_date?`<em>ostatnia: ${dateOnly(r.rcn_last_date)} • ${fmtPpm(r.rcn_last_ppm)}</em>`:''}</div>
  </div>`;
}
function card(r){
  const isPlot=r.category==='plot'; const distance=r.distance_km==null?'? km':`${(+r.distance_km).toFixed(1)} km`;
  const archived=isArchived(r);
  const phone=r.phone?`<a class="phone-btn" href="tel:${esc(r.phone)}">☎ ${esc(r.phone)}</a>`:`<div class="phone-btn disabled">☎ telefon brak / ukryty</div>`;
  const img=r.image_url?`<img src="${esc(r.image_url)}" loading="lazy" referrerpolicy="no-referrer" onerror="this.remove()">`:'';
  const badges=[
    archived?badge('ARCHIWALNA','warn'):'', badge(r.plot_type||'nieustalona','blue'), badge(r.planning_status||'nieustalone'),
    badge(sizeBadge(r.area_m2)),r.area_warning?badge('⚠ metraż','warn'):'',r.phone?badge('☎ telefon','good'):'',r.parcel_number?badge('🗺 nr działki','good'):'',
    r.last_meaningful_price_change_at?badge('📉 zmiana ceny','hot'):''
  ].join('');
  return `<article class="card ${archived?'archived-card':''}" data-id="${r.id}">
    <div class="photo">${img}<div class="photo-fallback">${isPlot?'🌱':'🚗'}</div></div>
    <div class="card-body"><div class="card-top"><div class="title">${esc(r.title||'(bez tytułu)')}</div><div class="distance">${distance}</div></div>
    <div class="location">📍 ${esc(loc(r))} · ${esc(r.source||'?')}</div>
    <div class="numbers"><div class="num"><span>Cena</span><b>${fmtMoney(r.price)}</b></div><div class="num"><span>Powierzchnia</span><b>${fmtArea(r.area_m2)}</b></div><div class="num"><span>Cena / m²</span><b>${fmtPpm(r.price_m2)}</b></div></div>
    <div class="badges">${badges}</div>
    ${isPlot?analyticsHtml(r):''}
    ${isPlot?`<div class="price-position">💡 ${esc(pricePosition(r))}</div>`:''}
    <div class="meta-line"><span>🗓 dodane na portalu: <b>${dateOnly(r.published_at)}</b></span>${r.updated_at?`<span>↻ aktualizacja: ${dateOnly(r.updated_at)}</span>`:''}<span>📡 Radar zobaczył: ${dateOnly(r.first_seen)}</span></div>
    ${archived&&r.archive_reason?`<div class="archive-note">⚠ ${esc(r.archive_reason)}</div>`:''}
    <div class="card-actions"><a class="open-btn" href="${esc(r.canonical_url)}" target="_blank" rel="noopener">Otwórz ogłoszenie</a>${phone}<button class="detail-btn" data-detail="${r.id}">Szczegóły</button></div>
    </div></article>`;
}
function presetLabel(){
  const ageLabel={7:'≤7 dni',30:'≤30 dni',90:'≤90 dni',all:'dowolna data',unknown:'bez daty'}[state.age]||state.age;
  const statusLabel={active:'aktywne',archived:'archiwalne',all:'wszystkie statusy'}[state.status];
  $('#activePreset').textContent=`• ${statusLabel} • publikacja ${ageLabel}`;
}
function render(){
  syncInputs(); const rows=sorted(listings.filter(match)); $('#resultCount').textContent=rows.length; $('#filterCount').textContent=activeFilterCount();presetLabel();
  $('#cards').innerHTML=rows.length?rows.map(card).join(''):`<div class="empty">Brak ofert dla tych filtrów.<br><span class="muted">Pamiętaj: domyślnie pokazujemy aktywne oferty opublikowane w ostatnich 30 dniach.</span></div>`;
  $$('[data-detail]').forEach(b=>b.addEventListener('click',()=>showDetail(+b.dataset.detail)));
}
async function showDetail(id){
  const r=listings.find(x=>+x.id===id); if(!r)return; let history=[],rcnTx=[];
  try{const [h,t]=await Promise.all([api(`/api/listing/${id}/history`),api(`/api/listing/${id}/rcn`)]);history=h.history||[];rcnTx=t.transactions||[]}catch{try{history=(await api(`/api/listing/${id}/history`)).history||[]}catch{}}
  const marketDelta=marketDeltaPct(r),rcnDelta=rcnDeltaPct(r);
  $('#detailBody').innerHTML=`<div class="detail-content"><h2>${esc(r.title||'Oferta')}</h2><div class="location">📍 ${esc(loc(r))} · ${r.distance_km==null?'?':(+r.distance_km).toFixed(1)} km · ${esc(r.source)}</div>
    <div class="detail-grid">
      ${[['Cena',fmtMoney(r.price)],['Powierzchnia',fmtArea(r.area_m2)],['Cena/m²',fmtPpm(r.price_m2)],['Status',isArchived(r)?'archiwalna / nieaktywna':'aktywna'],['Typ',r.plot_type||'nieustalona'],['Plan / WZ',r.planning_status||'nieustalone'],['Nr działki',r.parcel_number||'—'],['Telefon',r.phone||'brak / ukryty'],['Pierwotnie dodane',dateOnly(r.published_at)],['Ostatnia aktualizacja',dateOnly(r.updated_at)],['Radar pierwszy raz',dateTime(r.first_seen)],['Portal',r.source||'—']].map(([a,b])=>`<div class="detail-item"><span>${esc(a)}</span><b>${esc(b)}</b></div>`).join('')}
    </div>
    ${r.area_warning?`<div class="badge warn" style="margin-top:12px">${esc(r.area_warning)}</div>`:''}
    ${isArchived(r)&&r.archive_reason?`<div class="archive-note">⚠ ${esc(r.archive_reason)}</div>`:''}
    ${r.category==='plot'?`<h3 class="detail-section-title">Porównanie ceny</h3>${analyticsHtml(r)}<div class="comparison-summary"><b>Oferta vs ogłoszenia:</b> ${pctText(marketDelta)}<br><b>Oferta vs realne transakcje RCN:</b> ${pctText(rcnDelta)}</div>
      <div class="history rcn-history"><h3>Ostatnie porównywalne transakcje w okolicy</h3>${rcnTx.length?rcnTx.map(t=>`<div class="tx-row"><div><b>${dateOnly(t.transaction_date)}</b><span>${t.parcel_number?`dz. ${esc(t.parcel_number)} • `:''}${fmtArea(t.area_m2)} • ${(+t.distance_km).toFixed(1)} km</span></div><strong>${fmtPpm(t.price_m2)}</strong></div>`).join(''):'<div class="muted">Brak porównywalnych transakcji RCN dla tej lokalizacji/metrażu.</div>'}</div>`:''}
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
