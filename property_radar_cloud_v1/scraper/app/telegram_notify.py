from __future__ import annotations
import html,requests

class TelegramNotify:
    def __init__(self,token,chat_id,panel_url=''):
        self.token=token or ''; self.chat_id=str(chat_id or ''); self.panel_url=(panel_url or '').rstrip('/')
        self.s=requests.Session()
    def ready(self): return bool(self.token and self.chat_id)
    def send(self,text,listing_url=None):
        if not self.ready(): return False
        buttons=[]
        row=[]
        if listing_url: row.append({'text':'🔗 Ogłoszenie','url':listing_url})
        if self.panel_url: row.append({'text':'🏡 Aplikacja','web_app':{'url':self.panel_url}})
        if row: buttons.append(row)
        payload={'chat_id':self.chat_id,'text':text,'parse_mode':'HTML','disable_web_page_preview':True}
        if buttons: payload['reply_markup']={'inline_keyboard':buttons}
        r=self.s.post(f'https://api.telegram.org/bot{self.token}/sendMessage',json=payload,timeout=30)
        r.raise_for_status(); return True

def esc(x): return html.escape(str(x or ''))
def money(v): return '?' if v is None else f"{v:,.0f} zł".replace(',',' ')
def area(v): return '?' if v is None else f"{v:,.0f} m²".replace(',',' ')
def ppm(v): return '?' if v is None else f"{v:.2f} zł/m²".replace('.',',')
def size_badge(v):
    if not v:return ''
    if v>=10000:return '👑 1 HA+'
    if v>=5000:return '🟪 5000+'
    if v>=3000:return '🟦 3000+'
    if v>=1500:return '🟩 1500+'
    return ''

def listing_message(r,kind='new'):
    tag='🆕 NOWA OFERTA' if kind=='new' else '💸 ZMIANA CENY'
    cat='DZIAŁKA' if r.get('category')=='plot' else 'GARAŻ'
    loc=r.get('area_locality') or r.get('location') or '?'
    dist='?' if r.get('distance_km') is None else f"{r['distance_km']:.1f} km"
    lines=[f'<b>{tag} — {cat}</b>',f'📍 <b>{esc(loc)}</b> • {dist}',f'💰 <b>{money(r.get("price"))}</b> • {ppm(r.get("price_m2"))}',f'📐 <b>{area(r.get("area_m2"))}</b> {size_badge(r.get("area_m2"))}']
    if r.get('category')=='plot':
        lines += [f'🏷 {esc(r.get("plot_type") or "nieustalona")} • 🏗 {esc(r.get("planning_status") or "nieustalone")}',f'🌲 prywatność <b>{r.get("privacy_score") or "?"}/10</b> • 📊 <b>{esc(r.get("deal_label") or "?")}</b>']
        if r.get('median_comparable'): lines.append(f'📈 mediana porównawcza: {ppm(r.get("median_comparable"))} ({r.get("comparable_count") or 0} ofert, {esc(r.get("comparison_quality") or "")})')
        if r.get('parcel_number'): lines.append(f'🗺 nr działki: <b>{esc(r["parcel_number"])}</b>')
    lines.append(f'☎️ <b>{esc(r.get("phone") or "brak / ukryty")}</b>')
    lines.append(f'🌐 {esc(r.get("source") or "?")}')
    return '\n'.join(lines)
