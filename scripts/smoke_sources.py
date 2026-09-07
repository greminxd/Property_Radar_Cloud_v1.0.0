import json
from pathlib import Path
import sys
import requests


root = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root/'scraper'))
from app.scraper import Scraper

config = json.loads((root/'scraper/config.json').read_text(encoding='utf-8'))
results = []
for source in config['sources']:
    if source['name'] not in {'OLX','Otodom'}:
        continue
    url = source['search_urls'][0]
    try:
        response = requests.get(url,timeout=20,headers={'User-Agent':'PropertyRadar/1.6 public-source availability check'})
        result = {'source':source['name'],'http_status':response.status_code,'url':url,'bytes':len(response.content)}
        if response.status_code == 200:
            result['next_data_present'] = Scraper._next_data(response.text) is not None
            result['note'] = 'HTTP dostępny; ten test nie potwierdza kompletności ofert ani działania skanu produkcyjnego.'
        else:
            result['note'] = 'Nie ponawiano ani nie obchodzono odmowy dostępu. Wymagana weryfikacja dostępności źródła.'
        results.append(result)
    except requests.RequestException as error:
        results.append({'source':source['name'],'error':str(error)})
print(json.dumps(results,ensure_ascii=False,indent=2))
