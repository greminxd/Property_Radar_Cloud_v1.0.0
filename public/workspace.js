const select = selector => document.querySelector(selector);
const all = selector => [...document.querySelectorAll(selector)];
const defaults = {metrics:true, scan:true, scanOpen:false, dark:false, layout:'grid'};
const sections = {metrics:'.summary-grid', scan:'#scanSection'};
const escape = value => String(value ?? '').replace(/[&<>"']/g, token => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[token]));
const read = (key, fallback) => {try {return JSON.parse(localStorage.getItem(key)) ?? fallback;} catch {return fallback;}};
const write = (key, value) => {try {localStorage.setItem(key, JSON.stringify(value));return true;} catch {return false;}};
const preferences = {...defaults, ...read('radar:layout:v1', {})};
let accountKey = 'radar:personal:anonymous', personal = {}, collection = 'all', selection = new Set(), records = [], refresh = () => {}, notify = () => {};
const keyOf = record => record.canonical_url;
const datum = record => personal[keyOf(record)] || {};
const number = (value, suffix) => value == null || !Number.isFinite(Number(value)) ? 'Nieustalone' : `${Number(value).toLocaleString('pl-PL', {maximumFractionDigits:1})} ${suffix}`;

export function locationEvidence(record) {
  const confidence = record.area_confidence || '';
  if (confidence.includes('egib-exact')) return 'EGiB · punkt geometrii działki';
  if (confidence.includes('location-geocode')) return 'Przybliżenie · środek miejscowości';
  if (/listing-geo|olx-api/.test(confidence)) return 'Punkt portalu · może być przybliżony';
  return 'Lokalizacja do weryfikacji';
}

function applyPreferences() {
  document.documentElement.dataset.theme = preferences.dark ? 'dark' : 'light';
  select('meta[name="theme-color"]').content = preferences.dark ? '#14241f' : '#f5f5ef';
  try {window.Telegram?.WebApp?.setHeaderColor?.(preferences.dark ? '#14241f' : '#f5f5ef');window.Telegram?.WebApp?.setBackgroundColor?.(preferences.dark ? '#14241f' : '#f5f5ef');} catch {}
  Object.entries(sections).forEach(([name, selector]) => {const node=select(selector);if(node)node.classList.toggle('hidden', !preferences[name]);});
  const lastScan=select('#lastScan');if(lastScan)lastScan.classList.toggle('hidden', !preferences.metrics);
  const scanSection=select('#scanSection');if(scanSection)scanSection.open = preferences.scanOpen;
  const cards=select('#cards');if(cards)cards.dataset.layout = preferences.layout;
  all('[data-section]').forEach(input => {input.checked = preferences[input.dataset.section];});
  const darkTheme=select('#darkTheme');if(darkTheme)darkTheme.checked = preferences.dark;
  all('[data-layout]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.layout === preferences.layout)));
}

function persistPersonal() {
  if (!write(accountKey, personal)) notify('Pamięć przeglądarki niedostępna — zmiany tylko do zamknięcia aplikacji.');
}

export function initializeWorkspace(user, render, toast) {
  accountKey = `radar:personal:v1:${user?.uid || user?.id || 'web'}`;
  personal = read(accountKey, {});
  if (!personal || typeof personal !== 'object' || Array.isArray(personal)) personal = {};
  refresh = render;
  notify = toast;
  applyPreferences();
}

export function matchesWorkspace(record) {
  const saved = datum(record);
  return collection === 'hidden' ? !!saved.hidden : !saved.hidden && (collection !== 'saved' || saved.saved);
}

export function decorateWorkspace(rows, visible) {
  records = rows;
  select('#savedCount').textContent = rows.filter(record => datum(record).saved && !datum(record).hidden).length;
  select('#hiddenCount').textContent = rows.filter(record => datum(record).hidden).length;
  const existing = new Set(rows.map(keyOf));
  selection = new Set([...selection].filter(key => existing.has(key)));
  all('#cards .card').forEach((element, index) => {
    const record = visible[index];
    if (!record) return;
    const personalData = datum(record);
    const controls = document.createElement('div');
    controls.className = 'personal-controls';
    controls.innerHTML = `<button class="save-btn ${personalData.saved?'saved':''}" aria-pressed="${!!personalData.saved}" aria-label="${personalData.saved?'Usuń z zapisanych':'Zapisz ofertę'}">${personalData.saved?'♥':'♡'}</button><button class="hide-btn">${personalData.hidden?'Przywróć':'Ukryj'}</button><label class="compare-check"><input type="checkbox" ${selection.has(keyOf(record))?'checked':''}> Porównaj</label>`;
    controls.querySelector('.save-btn').onclick = () => {personal[keyOf(record)] = {...personalData, saved:!personalData.saved};persistPersonal();refresh();};
    controls.querySelector('.hide-btn').onclick = () => {personal[keyOf(record)] = {...personalData, hidden:!personalData.hidden};persistPersonal();refresh();notify(personalData.hidden?'Oferta przywrócona':'Ukryta tylko u Ciebie. Przywrócisz ją w zakładce „Ukryte”.');};
    controls.querySelector('input').onchange = event => {
      const key = keyOf(record);
      if (event.target.checked && selection.size >= 3) {event.target.checked = false;notify('Porównaj maksymalnie 3 działki naraz.');return;}
      if (event.target.checked) selection.add(key); else selection.delete(key);
      updateTray();
    };
    element.querySelector('.card-body').append(controls);
  });
  updateTray();
}

export function decorateDetail(record) {
  const container = document.createElement('section');
  container.className = 'personal-note';
  container.innerHTML = `<p class="location-evidence">${escape(locationEvidence(record))} · odległość w linii prostej, nie dojazd.</p>${record.area_warning?`<p class="data-warning">${escape(record.area_warning)}</p>`:''}<label for="listingNote">Moja notatka <span>tylko na tym urządzeniu</span></label><textarea id="listingNote" maxlength="2000" placeholder="Dojazd, pytania do właściciela, wrażenia z oględzin…"></textarea>`;
  const textarea = container.querySelector('textarea');
  textarea.value = datum(record).note || '';
  textarea.onchange = () => {personal[keyOf(record)] = {...datum(record), note:textarea.value};persistPersonal();};
  select('#detailBody').append(container);
}

function updateTray() {
  select('#compareTray').classList.toggle('hidden', !selection.size);
  select('#compareCount').textContent = selection.size;
  select('#openCompare').disabled = selection.size < 2;
}

const workspaceSettings=select('#workspaceSettings');if(workspaceSettings)workspaceSettings.onclick = () => select('#workspaceDialog').showModal();
const closeWorkspace=select('[data-close-workspace]');if(closeWorkspace)closeWorkspace.onclick = () => select('#workspaceDialog').close();
all('[data-section]').forEach(input => input.onchange = () => {preferences[input.dataset.section] = input.checked;write('radar:layout:v1', preferences);applyPreferences();});
select('#darkTheme').onchange = event => {preferences.dark = event.target.checked;write('radar:layout:v1', preferences);applyPreferences();};
const scanSection=select('#scanSection');if(scanSection)scanSection.ontoggle = () => {preferences.scanOpen = scanSection.open;write('radar:layout:v1', preferences);};
const resetWorkspace=select('#resetWorkspace');if(resetWorkspace)resetWorkspace.onclick = () => {Object.keys(preferences).forEach(key=>delete preferences[key]);Object.assign(preferences, defaults);write('radar:layout:v1', preferences);applyPreferences();};
all('[data-layout]').forEach(button => button.onclick = () => {preferences.layout = button.dataset.layout;write('radar:layout:v1', preferences);applyPreferences();});
all('[data-collection]').forEach(button => button.onclick = () => {collection = button.dataset.collection;all('[data-collection]').forEach(item => {item.classList.toggle('active', item === button);item.setAttribute('aria-pressed', String(item === button));});refresh();});
select('#clearCompare').onclick = () => {selection.clear();refresh();};
select('#closeCompare').onclick = () => select('#compareDialog').close();
select('#openCompare').onclick = () => {
  const chosen = records.filter(record => selection.has(keyOf(record)));
  const fields = [
    ['Cena', record => number(record.price, 'zł')], ['Powierzchnia', record => number(record.area_m2, 'm²')],
    ['Cena / m²', record => number(record.price_m2, 'zł/m²')], ['Od Bieśnika', record => number(record.distance_km, 'km')],
    ['Lokalizacja', locationEvidence], ['Przeznaczenie', record => record.plot_type || 'Nieustalone'],
    ['Plan / WZ', record => record.planning_status || 'Nieustalone'], ['Moja notatka', record => datum(record).note || '—']
  ];
  select('#compareBody').innerHTML = `<div class="compare-scroll"><table><thead><tr><th scope="col">Parametr</th>${chosen.map(record => `<th scope="col">${escape(record.area_locality || record.location)}<small>${escape(record.title)}</small></th>`).join('')}</tr></thead><tbody>${fields.map(([label, value]) => `<tr><th scope="row">${label}</th>${chosen.map(record => `<td>${escape(value(record))}</td>`).join('')}</tr>`).join('')}</tbody></table></div><p class="muted">Dane deklarowane w ogłoszeniach. Zweryfikuj dokumenty i rzeczywiste granice przed zakupem.</p>`;
  select('#compareDialog').showModal();
};
applyPreferences();
