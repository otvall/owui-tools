"""Install the example Tool and OWUI Skill; test normal and recovery flows by API."""
import json
import os
from pathlib import Path
import time
from uuid import uuid4
import requests

root = Path(__file__).resolve().parent
base = 'http://host.docker.internal:3000'
session = requests.Session()
auth = session.post(base + '/api/v1/auths/signin',
                    json=json.loads((root / 'backups/local-login.json').read_text()), timeout=30)
auth.raise_for_status()
session.headers['Authorization'] = 'Bearer ' + auth.json()['token']
del auth


def api(method, path, payload=None):
    response = session.request(method, base + path, json=payload, timeout=120)
    response.raise_for_status()
    return response.json()


tool_id = 'metadata_sql_charts'
skill_id = 'metadata-sql-charts'
source = (root / 'metadata_sql_chart_tool.py').read_text()
tool = {'id': tool_id, 'name': 'Metadata SQL Charts', 'content': source,
        'meta': {'description': 'SQL через __metadata__: графики без передачи строк через модель; понятные ошибки данных.'},
        'access_grants': []}
known = api('GET', '/api/v1/tools/')
api('POST', f'/api/v1/tools/id/{tool_id}/update' if any(t['id'] == tool_id for t in known) else '/api/v1/tools/create', tool)
skill_text = (root / 'metadata-sql-charts/SKILL.md').read_text()
body = skill_text.split('---', 2)[2].strip()
skill = {'id': skill_id, 'name': skill_id,
         'description': 'SQL-графики через request metadata: формат колонок и строк, последовательные вызовы, обработка ошибок.',
         'content': body, 'meta': {'tags': ['sql', 'charts', 'metadata']}, 'is_active': True, 'access_grants': []}
known = api('GET', '/api/v1/skills/')
api('POST', f'/api/v1/skills/id/{skill_id}/update' if any(t['id'] == skill_id for t in known) else '/api/v1/skills/create', skill)
print('Installed Tool and active Skill via authenticated API.', flush=True)

model = 'openai/gpt-4.1-mini'
assert any(m['id'] == model for m in api('GET', '/api/models')['data'])
results = []
cases = [
    ('Metadata: SQL -> график', 'Прочитай скилл metadata-sql-charts. Покажи на демонстрационных данных три кривые online, stores, partners по day через metadata. Используй load_demo_sql_metadata с valid, затем plot_metadata_chart с полученным dataset_id. Все действия последовательно в этом запросе. Только график, без дополнительных пояснений.'),
    ('Metadata: ошибка и восстановление', 'Прочитай скилл metadata-sql-charts. Проведи демонстрационный тест ошибки данных: вызови load_demo_sql_metadata с invalid_numeric, затем plot_metadata_chart по day и online, stores, partners. После получения ошибки исправь именно тест: загрузи valid и построй график по новому dataset_id, все в этом же запросе. В конце одной фразой назови код ошибки и проблемную колонку. Это тестовые данные, не реальные продажи. Не вызывай инструменты параллельно.'),
]
for title, prompt in cases:
    uid, aid = str(uuid4()), str(uuid4())
    timestamp = int(time.time())
    user_message = {'id': uid, 'parentId': None, 'childrenIds': [aid], 'role': 'user', 'content': prompt,
                    'timestamp': timestamp, 'models': [model]}
    assistant = {'id': aid, 'parentId': uid, 'childrenIds': [], 'role': 'assistant', 'content': '',
                 'timestamp': timestamp, 'model': model, 'done': False}
    params = {'function_calling': 'native', 'max_tokens': 2000}
    created = api('POST', '/api/v1/chats/new', {'chat': {'title': title, 'models': [model],
        'params': params, 'toolIds': [tool_id], 'skillIds': [skill_id],
        'history': {'currentId': aid, 'messages': {uid: user_message, aid: assistant}},
        'messages': [user_message, assistant], 'files': [], 'tags': [], 'timestamp': timestamp * 1000}})
    cid = created['id']
    print('Running:', title, 'http://localhost:3000/c/' + cid, flush=True)
    response = session.post(base + '/api/chat/completions', json={
        'model': model, 'stream': True, 'parallel_tool_calls': False,
        'messages': [{'role': 'user', 'content': prompt}], 'params': params,
        'tool_ids': [tool_id], 'skill_ids': [skill_id], 'chat_id': cid, 'id': aid,
        'user_message': user_message, 'background_tasks': {'title_generation': False,
        'tags_generation': False, 'follow_up_generation': False}}, stream=True, timeout=(30,180))
    response.raise_for_status()
    for line in response.iter_lines():
        pass
    saved = api('GET', '/api/v1/chats/' + cid)
    api('POST', '/api/v1/chats/' + cid, {'chat': {'title': title, 'toolIds': [tool_id], 'skillIds': [skill_id], 'params': params}})
    message = saved['chat']['history']['messages'][aid]
    calls = [o for o in message.get('output', []) if o.get('type') == 'function_call']
    outputs = [o for o in message.get('output', []) if o.get('type') == 'function_call_output']
    print('Calls:', [(o.get('name'), o.get('arguments')) for o in calls], flush=True)
    # Each returned function output is kept compact; raw data only belongs in iframe embeds.
    for o in outputs:
        if any(c['call_id'] == o['call_id'] and c['name'] == 'load_demo_sql_metadata' for c in calls):
            assert not any(k in json.dumps(o.get('output', [])) for k in ['"rows":', 'not_a_number'])
    print('Result codes:', [code for code in ['CHART_READY','NON_NUMERIC_VALUE','DATASET_NOT_FOUND'] if code in json.dumps(outputs)], flush=True)
    results.append({'title': title, 'chat_id': cid, 'message_id': aid, 'url': 'http://localhost:3000/c/' + cid,
                    'calls': calls, 'outputs': outputs, 'content': message.get('content', '')})
    (root / 'metadata-chart-verification.json').write_text(json.dumps(results, ensure_ascii=False, indent=2))
    assert 'CHART_READY' in json.dumps(outputs), 'No successful chart: inspect verification artifact.'
    assert '<script' not in message.get('content', '') and '<canvas' not in message.get('content', '') and '```' not in message.get('content', ''), 'Model added an unwanted second chart.'
    if 'ошибка' in title:
        assert 'NON_NUMERIC_VALUE' in json.dumps(outputs)
print('Both end-to-end API flows passed.', flush=True)
