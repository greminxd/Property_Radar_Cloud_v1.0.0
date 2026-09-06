from __future__ import annotations
import html, requests

class TelegramNotify:
    def __init__(self,token,chat_ids,panel_url=''):
        self.token=token or ''
        raw=str(chat_ids or '')
        self.chat_ids=[x.strip() for x in raw.replace(';',',').split(',') if x.strip()]
        self.panel_url=(panel_url or '').rstrip('/')
        self.s=requests.Session()
        self.progress_messages={}
    def ready(self): return bool(self.token and self.chat_ids)

    def _buttons(self,listing_url=None):
        row=[]
        if listing_url: row.append({'text':'🔗 Ogłoszenie','url':listing_url})
        if self.panel_url: row.append({'text':'🏡 Mini App','web_app':{'url':self.panel_url}})
        return {'inline_keyboard':[row]} if row else None

    def _progress_markup(self):
        rows=[]
        if self.panel_url: rows.append([{'text':'🏡 Mini App','web_app':{'url':self.panel_url}},{'text':'📡 Status','callback_data':'status:short'}])
        rows.append([{'text':'⏹ Zatrzymaj skan','callback_data':'scan:stop'}])
        return {'inline_keyboard':rows}

    @staticmethod
    def _progress_text(progress, final=False):
        total=max(0,int(progress.get('total_sources') or 0)); done=max(0,int(progress.get('done_sources') or 0))
        pct=round(done/total*100) if total else (100 if final else 0)
        blocks=10; filled=min(blocks,round(pct/100*blocks))
        bar='█'*filled+'░'*(blocks-filled)
        src=[]
        for x in progress.get('sources') or []:
            st=x.get('status')
            ico='✅' if st=='done' and x.get('healthy') else '⚠️' if st=='done' else '🔄' if st=='running' else '▫️'
            tail=f" · {x.get('records',0)}" if st=='done' else ''
            src.append(f"{ico} {esc(x.get('name') or '?')}{tail}")
        title='✅ <b>SKAN ZAKOŃCZONY</b>' if final else '📡 <b>SKAN PROPERTY RADAR · LIVE</b>'
        lines=[title,f"<code>{bar}</code> <b>{pct}%</b>",f"🌐 Źródła: <b>{done}/{total}</b>  •  📥 pobrano: <b>{progress.get('downloaded_records',0)}</b>  •  ✅ przyjęto: <b>{progress.get('accepted_records',0)}</b>"]
        if src: lines+=['',*src]
        lines+=['', 'Dane z ukończonych źródeł są już widoczne w Mini App.' if not final else 'Mini App zawiera wynik zakończonego skanu.']
        return '\n'.join(lines)

    def start_progress(self,progress):
        if not self.ready(): return
        text=self._progress_text(progress,False); markup=self._progress_markup()
        for chat_id in self.chat_ids:
            try:
                r=self.s.post(f'https://api.telegram.org/bot{self.token}/sendMessage',json={
                    'chat_id':chat_id,'text':text,'parse_mode':'HTML','disable_web_page_preview':True,'reply_markup':markup
                },timeout=25)
                if r.ok:
                    data=r.json()
                    mid=(data.get('result') or {}).get('message_id')
                    if mid:self.progress_messages[str(chat_id)]=mid
            except Exception:
                pass

    def update_progress(self,progress,final=False):
        if not self.ready(): return
        text=self._progress_text(progress,final); markup=self._progress_markup()
        for chat_id in self.chat_ids:
            mid=self.progress_messages.get(str(chat_id))
            if not mid: continue
            try:
                r=self.s.post(f'https://api.telegram.org/bot{self.token}/editMessageText',json={
                    'chat_id':chat_id,'message_id':mid,'text':text,'parse_mode':'HTML',
                    'disable_web_page_preview':True,'reply_markup':markup
                },timeout=25)
                # "message is not modified" is harmless.
                if not r.ok and 'message is not modified' not in r.text.lower():
                    pass
            except Exception:
                pass

    def send(self,text,listing_url=None,image_url=None):
        if not self.ready(): return False
        markup=self._buttons(listing_url); sent=0; errors=[]
        for chat_id in self.chat_ids:
            try:
                # Photo alerts are much easier to scan. Telegram can fetch a public image URL directly.
                if image_url:
                    payload={'chat_id':chat_id,'photo':image_url,'caption':text[:1000],'parse_mode':'HTML'}
                    if markup:payload['reply_markup']=markup
                    r=self.s.post(f'https://api.telegram.org/bot{self.token}/sendPhoto',json=payload,timeout=30)
                    if r.ok:
                        sent+=1; continue
                payload={'chat_id':chat_id,'text':text,'parse_mode':'HTML','disable_web_page_preview':True}
                if markup:payload['reply_markup']=markup
                r=self.s.post(f'https://api.telegram.org/bot{self.token}/sendMessage',json=payload,timeout=30)
                r.raise_for_status(); sent+=1
            except Exception as e: errors.append(f'{chat_id}: {e}')
        if errors and not sent: raise RuntimeError('; '.join(errors))
        return bool(sent)

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

def _date_only(v):
    if not v:return 'nieustalona'
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(v).replace('Z','+00:00')).strftime('%d.%m.%Y')
    except:return str(v)[:10]

def listing_message(r,kind='new',old_price=None):
    loc=r.get('area_locality') or r.get('location') or '?'
    dist='?' if r.get('distance_km') is None else f"{r['distance_km']:.1f} km"
    if kind=='price':
        new=float(r.get('price') or 0); old=float(old_price or r.get('last_price_old') or 0)
        delta=new-old; pct=(delta/old*100) if old else 0
        tag='📉 REALNA ZMIANA CENY' if delta<0 else '📈 REALNA ZMIANA CENY'
    else:
        tag='🆕 NOWE OGŁOSZENIE'
    lines=[f'<b>{tag}</b>',f'🏡 <b>{esc(r.get("title") or "Działka")}</b>',f'📍 <b>{esc(loc)}</b> • {dist}']
    if kind=='price' and old_price:
        lines.append(f'💰 <s>{money(old_price)}</s> → <b>{money(r.get("price"))}</b> ({pct:+.1f}%)')
    else:
        lines.append(f'💰 <b>{money(r.get("price"))}</b> • {ppm(r.get("price_m2"))}')
    lines.append(f'📐 <b>{area(r.get("area_m2"))}</b> {size_badge(r.get("area_m2"))}')
    lines.append(f'🗓 Dodane: <b>{esc(_date_only(r.get("published_at")))}</b>')
    if r.get('plot_type'): lines.append(f'🏷 {esc(r.get("plot_type"))} • 🏗 {esc(r.get("planning_status") or "nieustalone")}')
    if r.get('median_comparable'):
        lines.append(f'📢 Ogłoszenia w porównaniu: mediana <b>{ppm(r.get("median_comparable"))}</b> • średnia {ppm(r.get("market_mean_comparable"))} ({r.get("comparable_count") or 0})')

    lines.append(f'🌐 {esc(r.get("source") or "?")}')
    return '\n'.join(lines)
