const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const tg = window.Telegram?.WebApp;
let listings = [];
let stats = {};
let quick = 'all';

const state = {
  categories: new Set(), types: new Set(), planning: new Set(), localities: new Set(),
  minArea: '', maxArea: '', maxPrice: '', maxPpm: '', onlyPhone: false, onlyParcel: false,
};

const fmtMoney = (v) => v == null ? '—' : `${Math.round(+v).toLocaleString('pl-PL')} zł`;
const fmtArea = (v) => v == null ? '—' : `${Math.round(+v).toLocaleString('pl-PL')} m²`;
const fmtPpm = (v) => v == null ? '—' : `${(+v).toFixed(2).replace('.', ',')} zł/m²`;
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]));
const loc = (r) => r.area_locality || r.location || 'lokalizacja nieustalona';
const isDeal = (r) => (r.deal_label || '').includes('OKAZJA');
const sizeBadge = (a) => !a ? '' : a>=10000?'👑 1 HA+':a>=5000?'🟪 5000+':a>=3000?'🟦 3000+':a>=1500?'🟩 1500+':'';

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
$('#scanBtn').addEventListener('click', async()=>{try{const x=await api('/api/scan',{method:'POST',body:'{}'});toast(x.message||'Skan uruchomiony')}catch(e){toast(e.message)}});

async function load(){
  const data=await api('/api/listings'); listings=data.listings||[]; stats=data.stats||{}; hydrateFilters(); renderStats(); render();
}

function renderStats(){
  $('#marketMedian').textContent=stats.median_ppm==null?'—':fmtPpm(stats.median_ppm);
  $('#statTotal').textContent=stats.total||0; $('#statDeals').textContent=stats.deals||0; $('#statPrivate').textContent=stats.private_count||0; $('#statPhone').textContent=stats.with_phone||0;
  const ls=stats.last_scan?.finished_at; $('#lastScan').textContent=`Ostatni skan: ${ls?new Date(ls).toLocaleString('pl-PL'):'—'}`;
}

function choices(container, values, set, labels={}){
  const el=$(container); const old=new Set(set); el.innerHTML='';
  values.forEach(v=>{const id=`${container.slice(1)}-${btoa(unescape(encodeURIComponent(v))).replace(/=/g,'')}`;const label=document.createElement('label');label.className='choice';label.innerHTML=`<input type="checkbox" id="${id}" ${old.has(v)?'checked':''}><span>${esc(labels[v]||v)}</span>`;label.querySelector('input').addEventListener('change',ev=>ev.target.checked?set.add(v):set.delete(v));el.appendChild(label)});
}

function hydrateFilters(){
  const types=[...new Set(listings.filter(x=>x.category==='plot').map(x=>x.plot_type||'nieustalona'))].sort();
  const plans=[...new Set(listings.filter(x=>x.category==='plot').map(x=>x.planning_status||'nieustalone'))].sort();
  const places=[...new Set(listings.map(loc))].filter(x=>x!=='lokalizacja nieustalona').sort((a,b)=>a.localeCompare(b,'pl'));
  choices('#categoryFilters',['plot','garage'],state.categories,{plot:'Działki',garage:'Garaże'});
  choices('#typeFilters',types,state.types);
  choices('#planningFilters',plans,state.planning);
  choices('#localityFilters',places,state.localities);
}

function syncInputs(){
  state.minArea=$('#minArea').value;state.maxArea=$('#maxArea').value;state.maxPrice=$('#maxPrice').value;state.maxPpm=$('#maxPpm').value;state.onlyPhone=$('#onlyPhone').checked;state.onlyParcel=$('#onlyParcel').checked;
}

function activeFilterCount(){
  return state.categories.size+state.types.size+state.planning.size+state.localities.size+[state.minArea,state.maxArea,state.maxPrice,state.maxPpm,state.onlyPhone,state.onlyParcel].filter(Boolean).length;
}

function match(r){
  const q=$('#search').value.trim().toLowerCase();
  if(q && ![r.title,r.description,r.area_locality,r.location,r.parcel_number,r.phone,r.plot_type,r.planning_status,r.source].join(' ').toLowerCase().includes(q)) return false;
  if(state.categories.size&&!state.categories.has(r.category))return false;
  if(r.category==='plot'&&state.types.size&&!state.types.has(r.plot_type||'nieustalona'))return false;
  if(r.category==='plot'&&state.planning.size&&!state.planning.has(r.planning_status||'nieustalone'))return false;
  if(state.localities.size&&!state.localities.has(loc(r)))return false;
  if(state.minArea&&(+r.area_m2||0)<+state.minArea)return false;
  if(state.maxArea&&(+r.area_m2||Infinity)>+state.maxArea)return false;
  if(state.maxPrice&&(+r.price||Infinity)>+state.maxPrice)return false;
  if(state.maxPpm&&(+r.price_m2||Infinity)>+state.maxPpm)return false;
  if(state.onlyPhone&&!r.phone)return false;
  if(state.onlyParcel&&!r.parcel_number)return false;
  const ageH=(Date.now()-new Date(r.first_seen).getTime())/36e5;
  if(quick==='new'&&ageH>72)return false;
  if(quick==='deal'&&!isDeal(r))return false;
  if(quick==='private'&&!(r.category==='plot'&&+r.privacy_score>=8))return false;
  if(quick==='large'&&!(r.category==='plot'&&+r.area_m2>=1500))return false;
  if(quick==='garage'&&r.category!=='garage')return false;
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
    if(mode==='privacy')return num(b.privacy_score,-1)-num(a.privacy_score,-1);
    if(mode==='published')return new Date(b.published_at||0)-new Date(a.published_at||0);
    return new Date(b.first_seen)-new Date(a.first_seen);
  });
}

function badge(text,cls=''){return text?`<span class="badge ${cls}">${esc(text)}</span>`:''}

function card(r){
  const isPlot=r.category==='plot'; const distance=r.distance_km==null?'? km':`${(+r.distance_km).toFixed(1)} km`;
  const phone=r.phone?`<a class="phone-btn" href="tel:${esc(r.phone)}">☎ ${esc(r.phone)}</a>`:`<div class="phone-btn">☎ telefon brak / ukryty</div>`;
  const img=r.image_url?`<img src="${esc(r.image_url)}" loading="lazy" onerror="this.remove()">`:'';
  const badges=[
    badge(isPlot?(r.plot_type||'nieustalona'):'garaż','blue'),
    isPlot?badge(r.planning_status||'nieustalone'): '',
    isPlot&&isDeal(r)?badge(r.deal_label,'hot'): isPlot?badge(r.deal_label||''): '',
    isPlot&&r.privacy_score>=8?badge(`🌲 prywatność ${r.privacy_score}/10`,'good'):isPlot?badge(`🌲 ${r.privacy_score||'? '}/10`):'',
    badge(sizeBadge(r.area_m2)), r.area_warning?badge('⚠ metraż','warn'):'', r.phone?badge('☎ telefon','good'):'',
  ].join('');
  return `<article class="card" data-id="${r.id}">
    <div class="photo">${img}<div class="photo-fallback">${isPlot?'🌱':'🚗'}</div></div>
    <div class="card-body"><div class="card-top"><div class="title">${esc(r.title||'(bez tytułu)')}</div><div class="distance">${distance}</div></div>
    <div class="location">📍 ${esc(loc(r))} · ${esc(r.source||'?')}</div>
    <div class="numbers"><div class="num"><span>Cena</span><b>${fmtMoney(r.price)}</b></div><div class="num"><span>Powierzchnia</span><b>${fmtArea(r.area_m2)}</b></div><div class="num"><span>Cena / m²</span><b>${fmtPpm(r.price_m2)}</b></div></div>
    <div class="badges">${badges}</div>
    <div class="meta-line">${r.parcel_number?`<span>🗺 działka <b>${esc(r.parcel_number)}</b></span>`:''}<span>🕒 znalezione ${new Date(r.first_seen).toLocaleDateString('pl-PL')}</span>${r.published_at?`<span>opublikowano ${new Date(r.published_at).toLocaleDateString('pl-PL')}</span>`:(r.published_text?`<span>portal: ${esc(r.published_text)}</span>`:'')}</div>
    <div class="card-actions"><a class="open-btn" href="${esc(r.canonical_url)}" target="_blank" rel="noopener">Otwórz ogłoszenie</a>${phone}<button class="detail-btn" data-detail="${r.id}">Szczegóły</button></div>
    </div></article>`;
}

function render(){
  syncInputs(); const rows=sorted(listings.filter(match)); $('#resultCount').textContent=rows.length; $('#filterCount').textContent=activeFilterCount();
  $('#cards').innerHTML=rows.length?rows.map(card).join(''):`<div class="empty">Brak ofert dla tych filtrów.</div>`;
  $$('[data-detail]').forEach(b=>b.addEventListener('click',()=>showDetail(+b.dataset.detail)));
}

async function showDetail(id){
  const r=listings.find(x=>+x.id===id); if(!r)return; let history=[]; try{history=(await api(`/api/listing/${id}/history`)).history||[]}catch{}
  const reasons=(r.privacy_reasons||'').split('|').filter(Boolean);
  $('#detailBody').innerHTML=`<div class="detail-content"><h2>${esc(r.title||'Oferta')}</h2><div class="location">📍 ${esc(loc(r))} · ${r.distance_km==null?'?':(+r.distance_km).toFixed(1)} km · ${esc(r.source)}</div>
    <div class="detail-grid">
      ${[['Cena',fmtMoney(r.price)],['Powierzchnia',fmtArea(r.area_m2)],['Cena/m²',fmtPpm(r.price_m2)],['Typ',r.category==='garage'?'garaż':r.plot_type||'nieustalona'],['Plan / WZ',r.planning_status||'nieustalone'],['Nr działki',r.parcel_number||'—'],['Telefon',r.phone||'brak / ukryty'],['Prywatność',r.category==='plot'?`${r.privacy_score||'?'} / 10`:'n/d'],['Ocena ceny',r.deal_label||'—'],['Mediana porównawcza',r.median_comparable?fmtPpm(r.median_comparable):'—'],['Porównań',r.comparable_count||0],['Jakość porównania',r.comparison_quality||'—']].map(([a,b])=>`<div class="detail-item"><span>${esc(a)}</span><b>${esc(b)}</b></div>`).join('')}
    </div>
    ${r.area_warning?`<div class="badge warn" style="margin-top:12px">${esc(r.area_warning)}</div>`:''}
    ${reasons.length?`<div class="badges">${reasons.map(x=>badge(x,'good')).join('')}</div>`:''}
    <div class="description">${esc(r.description||'Brak opisu w parserze.')}</div>
    <div class="history"><h3>Historia ceny</h3>${history.length?history.map(x=>`<div class="history-row"><span>${new Date(x.seen_at).toLocaleString('pl-PL')}</span><b>${fmtMoney(x.price)}</b></div>`).join(''):'<div class="muted">Brak zmian ceny.</div>'}</div>
    <div class="card-actions"><a class="open-btn" href="${esc(r.canonical_url)}" target="_blank" rel="noopener">Otwórz źródło</a>${r.phone?`<a class="phone-btn" href="tel:${esc(r.phone)}">☎ ${esc(r.phone)}</a>`:''}</div></div>`;
  $('#detailDialog').showModal();
}

$('#detailClose').addEventListener('click',()=>$('#detailDialog').close());
$('#detailDialog').addEventListener('click',e=>{if(e.target===$('#detailDialog'))$('#detailDialog').close()});
$('#search').addEventListener('input',render); $('#sort').addEventListener('change',render);
$$('[data-quick]').forEach(b=>b.addEventListener('click',()=>{$$('[data-quick]').forEach(x=>x.classList.remove('active'));b.classList.add('active');quick=b.dataset.quick;render()}));

function openFilters(){hydrateFilters();$('#filters').classList.add('open');$('#backdrop').classList.remove('hidden')}
function closeFilters(){syncInputs();$('#filters').classList.remove('open');$('#backdrop').classList.add('hidden')}
$('#filtersBtn').addEventListener('click',openFilters);$('#filtersClose').addEventListener('click',closeFilters);$('#backdrop').addEventListener('click',closeFilters);$('#applyFilters').addEventListener('click',()=>{closeFilters();render()});
$('#clearFilters').addEventListener('click',()=>{state.categories.clear();state.types.clear();state.planning.clear();state.localities.clear();['minArea','maxArea','maxPrice','maxPpm'].forEach(x=>$('#'+x).value='');$('#onlyPhone').checked=false;$('#onlyParcel').checked=false;hydrateFilters();syncInputs();render()});

boot();
