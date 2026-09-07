import {createServer} from 'node:http';
import {readFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import {resolve, extname, sep} from 'node:path';

const root = fileURLToPath(new URL('../public/', import.meta.url));
const today = new Date();
const daysAgo = days => new Date(today.getTime() - days * 86400000).toISOString();
const listings = [
  {id:1, location:'Słona', title:'Miejsce na dom. Zieleń, cisza i otwarty widok.', price:149000, area_m2:1800, distance_km:3.2, source:'Otodom', plot_type:'budowlana', planning_status:'wydane WZ', published_at:daysAgo(1), area_confidence:'radius-verified:listing-geo'},
  {id:2, location:'Zdonia', title:'Działka pod lasem z dojazdem drogą gminną', price:189000, area_m2:2400, distance_km:2.1, source:'OLX', plot_type:'rolno-budowlana', planning_status:'MPZP', published_at:daysAgo(3), area_confidence:'radius-verified:olx-api'},
  {id:3, location:'Bieśnik', title:'Słoneczna parcela w spokojnym sąsiedztwie', price:125000, area_m2:1500, distance_km:0.8, source:'Adresowo', plot_type:'budowlana', planning_status:'nieustalone', published_at:daysAgo(5), area_confidence:'radius-verified:location-geocode'},
  {id:4, location:'Lusławice', title:'Przestrzeń na ogród i własny dom', price:215000, area_m2:3100, distance_km:5.8, source:'Morizon', plot_type:'budowlana', planning_status:'MPZP', published_at:daysAgo(12), area_confidence:'radius-verified:egib-exact'},
  {id:5, location:'Paleśnica', title:'Duża działka dla szukających spokoju', price:99000, area_m2:4500, distance_km:6.4, source:'Gratka', plot_type:'rolna', planning_status:'nieustalone', published_at:daysAgo(18), area_confidence:'radius-verified:listing-geo'},
  {id:6, location:'Zakliczyn', title:'Blisko centrum, z miejscem na własny plan', price:null, area_m2:1200, distance_km:4.1, source:'OLX', plot_type:'budowlana', planning_status:'wydane WZ', published_at:null, area_confidence:'radius-verified:olx-api'}
].map((record, index) => ({...record, active:1, category:'plot', source_status:'active', canonical_url:`https://example.com/oferta-demonstracyjna-${record.id}`, first_seen:daysAgo(index + 1), last_seen:daysAgo(0), updated_at:index%2===0?daysAgo(Math.max(0,index-1)):null, parcel_number:index%2===0?`${120+index}/${index+1}`:null, phone:index===1?'600 123 456':null, price_m2:record.price ? record.price / record.area_m2 : null, image_url:`/preview/terrain-${index%3}.svg`, description:'OFERTA DEMONSTRACYJNA — nie jest prawdziwym ogłoszeniem. Przykładowy opis służy wyłącznie do sprawdzenia nowego widoku, zapisywania, ukrywania, notatek i porównania. Dane należy zastąpić wynikami własnego skanu.'}));
const status = {scan_state:'idle',last_scan:{finished_at:daysAgo(0),status:'warning'},next_scan:'Podgląd — skany wyłączone',scan_control:{configured:false},scan_progress:{total_sources:3,done_sources:3,downloaded_records:18,accepted_records:6,sources:[{name:'Otodom',status:'done',healthy:true,records:8},{name:'OLX',status:'done',healthy:false,records:2,error:'Przykład: HTTP 429, skan częściowy. Oferty zachowane.'},{name:'Adresowo',status:'done',healthy:true,records:8}]}};
const stats = {plots:6,median_ppm:78.75,avg_ppm:71.29,published30:5,with_phone:0,blocked_urls:0,last_scan:{finished_at:daysAgo(0),healthy_sources:2,total_sources:3}};
const banner = '<div style="position:sticky;top:0;z-index:1000;background:#304c36;color:#fff;text-align:center;padding:7px 12px;font:11px system-ui">PODGLĄD · dane i ilustracje przykładowe · skanowanie oraz alerty wyłączone</div>';
const palette = [['#a3b584','#788e59','#c4d2a2'],['#97a07c','#596e4d','#c5caa6'],['#c4b589','#939b65','#e0d5af']];
function terrain(index) {
  const [base,deep,pale] = palette[index];
  return `<svg xmlns="http://www.w3.org/2000/svg" width="720" height="400" viewBox="0 0 720 400"><rect width="720" height="400" fill="${pale}"/><path d="M0 100L250 60 600 210 720 190V400H0Z" fill="${base}"/><path d="M0 280L240 180 490 330 720 240V400H0Z" fill="${deep}"/><path d="M490-20Q280 170 430 430" fill="none" stroke="${pale}" stroke-width="14"/><path d="M40 100L185 75 302 129 249 217 77 170Z" fill="#fff2" stroke="#f3f0d5" stroke-width="2" stroke-dasharray="7 5"/><text x="25" y="375" fill="#fff" font-family="sans-serif" font-size="15">ILUSTRACJA · DANE PRZYKŁADOWE</text></svg>`;
}
createServer(async (request, response) => {
  const path = new URL(request.url, 'http://127.0.0.1').pathname;
  response.setHeader('Cache-Control','no-store');
  const json = (data, code=200) => {response.writeHead(code, {'content-type':'application/json; charset=utf-8'});response.end(JSON.stringify(data));};
  if (request.method !== 'GET') return json({error:'Podgląd tylko do odczytu. Brak połączenia z produkcją.'},403);
  if (path === '/api/me') return json({user:{uid:'preview',role:'viewer'}});
  if (path === '/api/listings') return json({listings,stats});
  if (path === '/api/status') return json(status);
  if (/^\/api\/listing\/\d+\/history$/.test(path)) return json({history:[]});
  const imageMatch = path.match(/^\/api\/listing\/(\d+)\/image$/);
  if (imageMatch) {
    const listing=listings.find(item=>String(item.id)===imageMatch[1]);
    if(!listing?.image_url)return json({error:'image unavailable'},404);
    const match=String(listing.image_url).match(/terrain-(\d)\.svg$/);
    if(!match)return json({error:'image unavailable'},404);
    response.writeHead(200,{'content-type':'image/svg+xml','cache-control':'no-store'});
    return response.end(terrain(Number(match[1])));
  }
  if (/^\/preview\/terrain-[0-2]\.svg$/.test(path)) {response.writeHead(200,{'content-type':'image/svg+xml'});return response.end(terrain(Number(path.match(/(\d)\.svg$/)[1])));}
  if (path.startsWith('/api/')) return json({error:'Nieznany endpoint podglądu'},404);
  const file = resolve(root, '.' + (path === '/' ? '/index.html' : decodeURIComponent(path)));
  if (!file.startsWith(root.endsWith(sep)?root:root+sep)) return json({error:'Forbidden'},403);
  try {
    const extension = extname(file);
    let content = await readFile(file);
    if (extension === '.html') content = content.toString().replace('<body>','<body>'+banner).replace('<script src="https://telegram.org/js/telegram-web-app.js"></script>','');
    response.writeHead(200,{'content-type':({'.html':'text/html; charset=utf-8','.js':'text/javascript; charset=utf-8','.css':'text/css; charset=utf-8'})[extension] || 'application/octet-stream'});
    response.end(content);
  } catch {json({error:'Not found'},404);}
}).listen(4173,'127.0.0.1',()=>console.log('Podgląd: http://127.0.0.1:4173 — dane przykładowe, bez skanów i alertów.'));
