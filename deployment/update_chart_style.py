"""Update the chart tool and restyle its saved demo embeds using the OWUI API."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import re
import requests

root = Path(__file__).resolve().parent
base = 'http://host.docker.internal:3000'
s = requests.Session()
r = s.post(base + '/api/v1/auths/signin', json=json.loads((root / 'backups/local-login.json').read_text()), timeout=30)
r.raise_for_status()
s.headers['Authorization'] = 'Bearer ' + r.json()['token']

def api(method, path, data=None):
    r = s.request(method, base + path, json=data, timeout=60)
    r.raise_for_status()
    return r.json()

old = api('GET', '/api/v1/tools/id/sql_charts_demo')
api('POST', '/api/v1/tools/id/sql_charts_demo/update', {
    'id': 'sql_charts_demo', 'name': old['name'],
    'content': (root / 'sql_chart_tool.py').read_text(),
    'meta': old['meta'], 'access_grants': old.get('access_grants', [])})
spec = importlib.util.spec_from_file_location('chart', root / 'sql_chart_tool.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
tool = module.Tools()
chat_id = '73a0038f-8482-4c92-92d4-0475da5925fa'
saved = api('GET', '/api/v1/chats/' + chat_id)
os.umask(0o077)
(root / 'backups/sql-chat-before-white.json').write_text(json.dumps(saved, ensure_ascii=False))
changed = {}
count = 0
for mid, message in saved['chat']['history']['messages'].items():
    updated = False
    for item in message.get('output', []):
        for i, embed in enumerate(item.get('embeds', [])):
            if not isinstance(embed, str) or 'id="chart-data"' not in embed:
                continue
            match = re.search(r'<script id="chart-data" type="application/json">(.*?)</script>', embed, re.S)
            data = json.loads(match.group(1))
            # Preserve original series names and labels exactly; only the template changes.
            rows = [{'__x__': label, **{series['name']: series['values'][k] for series in data['series']}} for k, label in enumerate(data['labels'])]
            response = asyncio.run(tool.plot_sql_chart(json.dumps(rows), '__x__', [series['name'] for series in data['series']], data['title'], data['yLabel']))
            assert response.status_code == 200
            item['embeds'][i] = response.body.decode()
            count += 1
            updated = True
    if updated:
        message['content'] = ''
        message['output'] = [item for item in message['output'] if item.get('type') != 'message']
        changed[mid] = message
assert count > 0
api('POST', '/api/v1/chats/' + chat_id, {'chat': {'history': {'messages': changed}}})
result = api('GET', '/api/v1/chats/' + chat_id)
for mid in changed:
    assert result['chat']['history']['messages'][mid]['output'] == changed[mid]['output']
print(f'Updated tool v1.1.0 and {count} saved chart embeds; API readback matches.')
