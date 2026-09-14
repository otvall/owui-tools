"""Create an authenticated Open WebUI SQL-chart demonstration through its API."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import time
import uuid

import requests

root = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('sql_chart_tool', root / 'sql_chart_tool.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


async def check_tool():
    tool = module.Tools()
    result = await tool.get_demo_sql_data()
    assert len(result['rows']) == 7 and result['rows'][0]['online'] == 120
    response = await tool.plot_sql_chart(json.dumps(result['rows']), 'day', result['y_columns'])
    assert response.headers['content-disposition'] == 'inline'
    injected = await tool.plot_sql_chart('[{"x":"</script><img src=x>","y":1}]', 'x', ['y'])
    assert b'</script><img' not in injected.body
    for rows in ['[{"x":"a","y":true}]', '[{"x":"a","y":1},{"x":"a","y":2}]', '[{"x":"a","y":null}]']:
        assert 'error' in await tool.plot_sql_chart(rows, 'x', ['y'])
    gaps = await tool.plot_sql_chart('[{"x":"a","y":1},{"x":"b","y":null},{"x":"c","y":3}]', 'x', ['y'])
    assert gaps.status_code == 200
    (root / 'sql-demo-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print('Tool checks passed: SQL rows, inline HTML, escaping, invalid data, null gaps.', flush=True)


asyncio.run(check_tool())
base = os.environ.get('OWUI_URL', 'http://host.docker.internal:3000')
session = requests.Session()
login = json.loads((root / 'backups/local-login.json').read_text())
auth = session.post(base + '/api/v1/auths/signin', json=login, timeout=30)
auth.raise_for_status()
session.headers['Authorization'] = 'Bearer ' + auth.json()['token']
del login, auth


def api(method, path, payload=None):
    r = session.request(method, base + path, json=payload, timeout=60)
    if not r.ok:
        raise RuntimeError(f'{method} {path}: {r.status_code}: {r.text[:500]}')
    return r.json()


tool_id = 'sql_charts_demo'
form = {'id': tool_id, 'name': 'SQL Charts Demo',
        'content': (root / 'sql_chart_tool.py').read_text(),
        'meta': {'description': 'SQL → строки → несколько интерактивных кривых. Демо-данные.'},
        'access_grants': []}
existing = api('GET', '/api/v1/tools/')
if any(t['id'] == tool_id for t in existing):
    api('POST', f'/api/v1/tools/id/{tool_id}/update', form)
else:
    api('POST', '/api/v1/tools/create', form)
print('Tool installed through authenticated API.', flush=True)

model = 'openai/gpt-4.1-mini'
assert any(m['id'] == model for m in api('GET', '/api/models')['data'])
prompt = '''Покажи пример цепочки SQL → визуализация на демонстрационных данных.
1. Вызови get_demo_sql_data ровно один раз: это реальный SELECT по отдельной SQLite-базе с вымышленными продажами.
2. Передай все полученные rows без изменения чисел в plot_sql_chart: rows_json — JSON-массив строк, x_column="day", y_columns=["online","stores","partners"], title="Продажи по каналам (демо)", y_label="Выручка (тыс. руб.)". На одном графике нужны три кривые.
3. После вызова кратко объясни по-русски: данные демонстрационные, каждый элемент y_columns даёт отдельную кривую; названия в легенде скрывают и показывают кривые без повторного SQL или запроса к модели, а таблица под графиком показывает исходные строки. Не выводи HTML и не создавай другой график. Не вызывай другие инструменты.'''
uid, aid = str(uuid.uuid4()), str(uuid.uuid4())
now = int(time.time())
user_msg = {'id': uid, 'parentId': None, 'childrenIds': [aid], 'role': 'user',
            'content': prompt, 'timestamp': now, 'models': [model]}
assistant_msg = {'id': aid, 'parentId': uid, 'childrenIds': [], 'role': 'assistant',
                 'content': '', 'timestamp': now, 'model': model, 'done': False}
params = {'function_calling': 'native', 'max_tokens': 2000}
chat = api('POST', '/api/v1/chats/new', {'chat': {
    'title': 'SQL → три кривые · Rich UI', 'models': [model], 'params': params,
    'toolIds': [tool_id], 'history': {'currentId': aid, 'messages': {uid: user_msg, aid: assistant_msg}},
    'messages': [user_msg, assistant_msg], 'tags': [], 'files': [], 'timestamp': now * 1000}})
chat_id = chat['id']
(root / 'sql-demo-chat.json').write_text(json.dumps({'chat_id': chat_id, 'message_id': aid,
    'url': f'http://localhost:3000/c/{chat_id}'}, indent=2))
print(f'Chat created: http://localhost:3000/c/{chat_id}', flush=True)
payload = {'model': model, 'stream': True, 'messages': [{'role': 'user', 'content': prompt}],
           'params': params, 'tool_ids': [tool_id], 'chat_id': chat_id, 'id': aid,
           'user_message': user_msg, 'background_tasks': {'title_generation': False,
            'tags_generation': False, 'follow_up_generation': False}}
r = session.post(base + '/api/chat/completions', json=payload, stream=True, timeout=(30, 180))
r.raise_for_status()
lines = list(r.iter_lines())
print(f'Completion stream finished ({len(lines)} lines).', flush=True)
saved = api('GET', '/api/v1/chats/' + chat_id)
api('POST', '/api/v1/chats/' + chat_id, {'chat': {'title': 'SQL: три кривые (демо)', 'params': params, 'toolIds': [tool_id]}})
(root / 'sql-demo-chat-result.json').write_text(json.dumps(saved, ensure_ascii=False, indent=2))
msg = saved['chat']['history']['messages'].get(aid, {})
print('Assistant done:', msg.get('done'), 'saved fields:', list(msg), flush=True)
if msg.get('error'):
    print('Response error:', msg['error'], flush=True)
print('Serialized tool-call evidence:', {name: name in json.dumps(msg) for name in ['get_demo_sql_data', 'plot_sql_chart', 'iframe:height']}, flush=True)
