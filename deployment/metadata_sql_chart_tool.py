"""
title: Metadata SQL Charts
description: SQL result rows stay in request metadata; the model passes only dataset ID and chart columns.
version: 1.0.0
required_open_webui_version: 0.11.1
"""

import json
import logging
import math
import sqlite3
from datetime import date
from uuid import uuid4

from fastapi.responses import HTMLResponse

NAMESPACE = 'metadata_sql_charts_v1'
MAX_ROWS = 2000
MAX_SERIES = 8
MAX_DATASETS = 4
log = logging.getLogger(__name__)


class ChartError(Exception):
    def __init__(self, code, message, action, **details):
        self.result = {'status': 'error', 'code': code, 'message': message,
                       'suggested_action': action, 'details': details,
                       'chart_created': False}


def fail(code, message, action, **details):
    raise ChartError(code, message, action, **details)


def store_sql_result(rows, columns, metadata, user, source='sql', is_demo=False):
    """SQL producer integration point. Never return rows or metadata to the LLM.

    rows: list of dicts. columns: [{name, type: date|number|category, unit?}].
    Call AFTER executing an authorized, bounded, read-only SQL query.
    Returned errors are safe summaries, not a dump of query results.
    """
    if not isinstance(metadata, dict):
        return {'status': 'error', 'code': 'METADATA_UNAVAILABLE',
                'message': 'Open WebUI не передал контекст запроса.',
                'suggested_action': 'Вызовите через серверный Tool Open WebUI с __metadata__.'}
    if not isinstance(user, dict) or not user.get('id'):
        return {'status': 'error', 'code': 'USER_CONTEXT_MISSING',
                'message': 'Нет контекста пользователя.', 'suggested_action': 'Передайте __user__ от Open WebUI.'}
    if not isinstance(rows, list) or len(rows) > MAX_ROWS:
        return {'status': 'error', 'code': 'TOO_MANY_ROWS',
                'message': f'Для этого примера нужно не более {MAX_ROWS} строк.',
                'suggested_action': 'Агрегируйте данные в SQL; не обрезайте выборку молча.',
                'details': {'max_rows': MAX_ROWS}}
    bucket = metadata.setdefault(NAMESPACE, {})
    if not isinstance(bucket, dict):
        return {'status': 'error', 'code': 'INVALID_METADATA',
                'message': 'Повреждён namespace набора данных.',
                'suggested_action': 'Исправьте серверный SQL Tool.'}
    if len(bucket) >= MAX_DATASETS:
        return {'status': 'error', 'code': 'DATASET_LIMIT',
                'message': 'Достигнут лимит наборов в одном запросе.',
                'suggested_action': 'Завершите запрос; при следующем заново получите нужную выборку.'}
    dataset_id = 'ds_' + uuid4().hex[:16]
    bucket[dataset_id] = {'schema_version': 1, 'owner_id': user['id'],
                          'source': source, 'is_demo': is_demo,
                          'columns': columns, 'rows': rows}
    return {'status': 'ready', 'dataset_id': dataset_id, 'row_count': len(rows),
            'columns': columns, 'is_demo': is_demo, 'scope': 'current_request_only',
            'next_action': 'Call plot_metadata_chart sequentially in this same request. Do not pass rows.'}


def validate_dataset(dataset, x_column, y_columns, user):
    if not isinstance(dataset, dict) or dataset.get('schema_version') != 1:
        fail('INVALID_SCHEMA', 'Неподдерживаемый формат набора.',
             'SQL Tool должен записать schema_version=1, columns и rows.')
    if not isinstance(user, dict) or not user.get('id') or dataset.get('owner_id') != user['id']:
        fail('ACCESS_DENIED', 'Набор недоступен этому пользователю.', 'Повторите SQL от текущего пользователя.')
    rows, columns = dataset.get('rows'), dataset.get('columns')
    if not isinstance(rows, list):
        fail('INVALID_ROWS', 'rows должен быть массивом объектов.', 'Исправьте адаптер SQL.')
    if not rows:
        fail('EMPTY_DATASET', 'SQL не вернул строк; график не создан.',
             'Проверьте период и фильтры. Не заменяйте отсутствие строк нулями.')
    if len(rows) > MAX_ROWS:
        fail('TOO_MANY_ROWS', 'Выборка слишком большая для inline-графика.',
             'Агрегируйте в SQL до нужной детализации.', row_count=len(rows), max_rows=MAX_ROWS)
    if not isinstance(columns, list) or not columns or len(columns) > 32:
        fail('INVALID_SCHEMA', 'Нужно описание 1–32 колонок.', 'Исправьте columns в SQL Tool.')
    schema = {}
    for col in columns:
        if not isinstance(col, dict) or not isinstance(col.get('name'), str) or not col['name']:
            fail('INVALID_SCHEMA', 'Колонка должна иметь строковое имя.', 'Исправьте columns в SQL Tool.')
        if col['name'] in schema or col.get('type') not in ('date', 'number', 'category'):
            fail('INVALID_SCHEMA', 'Имена колонок должны быть уникальны; тип: date, number или category.',
                 'Исправьте описание columns.')
        schema[col['name']] = col
    if not isinstance(x_column, str) or x_column not in schema:
        fail('COLUMN_NOT_FOUND', 'Колонка X не найдена.', 'Выберите X из available_columns.', available_columns=list(schema))
    if (not isinstance(y_columns, list) or not 1 <= len(y_columns) <= MAX_SERIES or
        any(not isinstance(c, str) for c in y_columns) or len(set(y_columns)) != len(y_columns)):
        fail('INVALID_SERIES', 'Укажите 1–8 разных названий колонок Y.', 'Исправьте y_columns.')
    for col in y_columns:
        if col not in schema:
            fail('COLUMN_NOT_FOUND', 'Колонка Y не найдена.', 'Выберите Y из available_columns.',
                 column=col, available_columns=list(schema))
        if col == x_column or schema[col]['type'] != 'number':
            fail('NON_NUMERIC_COLUMN', 'Колонка Y должна иметь тип number и отличаться от X.',
                 'Выберите числовую колонку.', column=col)
    units = [schema[c].get('unit') for c in y_columns]
    if any(not isinstance(u, str) or not u.strip() for u in units):
        fail('UNIT_REQUIRED', 'Для каждой колонки Y нужны единицы измерения.',
             'Укажите unit в описании SQL-колонок, например RUB или count.')
    if len(set(units)) != 1:
        fail('MIXED_UNITS', 'У выбранных кривых разные единицы измерения.',
             'Постройте отдельные графики или явно приведите значения к одним единицам в SQL.',
             columns=y_columns, units=units)
    labels, positions = [], []
    values = {c: [] for c in y_columns}
    xtype = schema[x_column]['type']
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            fail('INVALID_ROW', 'Строка должна быть объектом.', 'Исправьте адаптер SQL.', row_index=index)
        for col in [x_column] + y_columns:
            if col not in row:
                fail('MISSING_COLUMN_VALUE', 'В строке отсутствует ожидаемая колонка.',
                     'Верните все колонки; отсутствие измерения обозначьте null.', row_index=index, column=col)
        x = row[x_column]
        if xtype == 'date':
            try:
                if not isinstance(x, str) or date.fromisoformat(x).isoformat() != x:
                    raise ValueError()
                position = date.fromisoformat(x).toordinal()
            except (TypeError, ValueError):
                fail('INVALID_DATE', 'Ожидается дата YYYY-MM-DD.', 'Нормализуйте дату в адаптере SQL.',
                     row_index=index, column=x_column)
        elif xtype == 'number':
            if isinstance(x, bool) or not isinstance(x, (int, float)) or abs(x) > 1e15 or not math.isfinite(x):
                fail('INVALID_X', 'Числовая ось X содержит неподдерживаемое значение.',
                     'Верните конечные числа с абсолютным значением не более 1e15.', row_index=index, column=x_column)
            position = x
        else:
            if not isinstance(x, str) or not x or len(x) > 80:
                fail('INVALID_CATEGORY', 'Категория X должна быть непустой строкой до 80 символов.',
                     'Исправьте категорию в SQL.', row_index=index, column=x_column)
            position = index
        labels.append(str(x))
        positions.append(position)
        for col in y_columns:
            v = row[col]
            if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float))):
                fail('NON_NUMERIC_VALUE', 'В числовой колонке обнаружено не число.',
                     'Исправьте тип в SQL/адаптере: int или float; пропуск — null. Не заменяйте ошибку нулём.',
                     row_index=index, column=col, received_type=type(v).__name__)
            if v is not None and (abs(v) > 1e15 or not math.isfinite(v)):
                fail('INVALID_NUMBER', 'Число не является конечным или выходит за диапазон графика.',
                     'Уберите NaN/Infinity и приведите масштаб; максимум abs(value)=1e15.', row_index=index, column=col)
            values[col].append(v)
    if len(set(labels)) != len(labels) or len(set(positions)) != len(positions):
        fail('DUPLICATE_X', 'Несколько строк имеют одинаковый X.',
             'Сгруппируйте SQL по X; для нескольких кривых используйте отдельные колонки Y.')
    if xtype != 'category' and any(a >= b for a, b in zip(positions, positions[1:])):
        fail('UNSORTED_X', 'Даты или числа X не упорядочены по возрастанию.', 'Добавьте ORDER BY X ASC в SQL.')
    for col, vals in values.items():
        if all(v is None for v in vals):
            fail('EMPTY_SERIES', 'У кривой нет ни одной числовой точки.',
                 'Исключите эту колонку из y_columns или исправьте SQL.', column=col)
    return {'title': 'График', 'yLabel': units[0], 'labels': labels,
            'positions': positions, 'series': [{'name': c, 'values': values[c]} for c in y_columns]}


class Tools:
    def __init__(self):
        self.citation = False

    async def load_demo_sql_metadata(self, scenario: str = 'valid', __metadata__: dict = None, __user__: dict = None) -> dict:
        """Execute a fixed SELECT on fictional SQLite data and store rows in request metadata.

        :param scenario: valid for normal demo; invalid_numeric, empty or duplicate_x ONLY when testing data errors. Returns dataset_id and schema, never rows. Do not use for real business data. Await result before calling plot_metadata_chart.
        """
        if scenario not in ('valid', 'invalid_numeric', 'empty', 'duplicate_x'):
            return {'status': 'error', 'code': 'INVALID_SCENARIO', 'message': 'Неизвестный сценарий.',
                    'suggested_action': 'Используйте valid, invalid_numeric, empty или duplicate_x.'}
        try:
            with sqlite3.connect(':memory:') as db:
                db.execute('CREATE TABLE sales(day TEXT, channel TEXT, amount REAL)')
                data = [(120, 95, 55), (145, 102, 70), (132, 118, 62), (178, 110, 88),
                        (165, 138, 94), (210, 152, 105), (235, 160, 128)]
                db.executemany('INSERT INTO sales VALUES (?,?,?)', [
                    (f'2026-09-{i+1:02}', c, v) for i, vals in enumerate(data)
                    for c, v in zip(['online', 'stores', 'partners'], vals)])
                db.execute('PRAGMA query_only=ON')
                db.row_factory = sqlite3.Row
                rows = [dict(r) for r in db.execute('''SELECT day,
                    SUM(CASE WHEN channel='online' THEN amount ELSE 0 END) AS online,
                    SUM(CASE WHEN channel='stores' THEN amount ELSE 0 END) AS stores,
                    SUM(CASE WHEN channel='partners' THEN amount ELSE 0 END) AS partners
                    FROM sales GROUP BY day ORDER BY day ASC''')]
            if scenario == 'invalid_numeric':
                rows[2]['stores'] = 'not_a_number'
            elif scenario == 'empty':
                rows = []
            elif scenario == 'duplicate_x':
                rows.append(dict(rows[-1]))
            columns = [{'name': 'day', 'type': 'date'}] + [
                {'name': c, 'type': 'number', 'unit': 'тыс. руб.'} for c in ['online', 'stores', 'partners']]
            return store_sql_result(rows, columns, __metadata__, __user__, 'demo_sqlite', True)
        except Exception:
            log.exception('Demo SQL failed')
            return {'status': 'error', 'code': 'SQL_EXECUTION_FAILED',
                    'message': 'Не удалось выполнить демонстрационный SQL.',
                    'suggested_action': 'Сообщите об ошибке; проверьте серверный журнал.'}

    async def plot_metadata_chart(self, dataset_id: str, x_column: str, y_columns: list[str],
                                  __metadata__: dict = None, __user__: dict = None):
        """Render a white multi-line chart from rows stored in request metadata. Never pass raw rows.

        :param dataset_id: Exact ID returned by the SQL producer in THIS request. Expired IDs require rerunning SQL, not inventing data.
        :param x_column: Column of type date (YYYY-MM-DD), number, or category. Date/number values must be unique and sorted ascending.
        :param y_columns: 1–8 numeric column names from the producer schema, using identical units. Each column becomes a separate curve. null creates a gap. Errors return status, code, message, suggested_action and details without raw data.
        """
        try:
            if not isinstance(__metadata__, dict):
                fail('METADATA_UNAVAILABLE', 'Контекст запроса не передан.', 'Вызовите Tool через Open WebUI.')
            bucket = __metadata__.get(NAMESPACE)
            if not isinstance(dataset_id, str) or not isinstance(bucket, dict) or dataset_id not in bucket:
                fail('DATASET_NOT_FOUND', 'Набор данных отсутствует в текущем запросе.',
                     'Сначала выполните SQL Tool в этом же запросе, дождитесь его результата и используйте новый dataset_id.')
            payload = validate_dataset(bucket[dataset_id], x_column, y_columns, __user__)
            encoded = json.dumps(payload, ensure_ascii=True, allow_nan=False).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
            html = HTML_TEMPLATE.replace('__DATA__', encoded)
            return HTMLResponse(content=html, headers={'Content-Disposition': 'inline'}), {
                'status': 'success', 'code': 'CHART_READY', 'dataset_id': dataset_id,
                'row_count': len(payload['labels']), 'series_count': len(y_columns),
                'message': 'The complete interactive chart has ALREADY been embedded by this tool. You have NOT received the raw SQL rows. Do not generate HTML, code, charts, sample values or guessed numbers. No extra text is needed; if a text response is required, say only Готово. For an explicit error-recovery test, you may briefly name the previous error code.',
                'browser_render_verified': False}
        except ChartError as exc:
            return exc.result
        except Exception:
            log.exception('Metadata chart failed')
            return {'status': 'error', 'code': 'INTERNAL_RENDER_ERROR', 'chart_created': False,
                    'message': 'Внутренняя ошибка построения. Это не подтверждает ошибку в данных.',
                    'suggested_action': 'Не выдумывайте график; сообщите об ошибке и проверьте серверный журнал.'}


HTML_TEMPLATE = r'''<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SQL → интерактивный график</title>
<style>
*{box-sizing:border-box}body{margin:0;padding:8px;background:#fff;font:13px/1.5 system-ui;color:#374151}
main{padding:12px;background:#fff;max-width:950px;margin:auto}
#legend{display:flex;justify-content:center;flex-wrap:wrap;gap:8px;margin:0 0 8px}
button{color:#374151;background:transparent;border:0;padding:5px 10px;cursor:pointer;font:inherit}
button[aria-pressed=false]{opacity:.35;text-decoration:line-through}button:focus-visible{outline:2px solid #2563eb}
.dot{display:inline-block;width:18px;height:3px;vertical-align:middle;margin-right:7px}
svg{display:block;width:100%;height:auto;overflow:visible}svg text{fill:#6b7280;font:12px system-ui}
svg .point{cursor:crosshair}svg .point:focus{outline:none;stroke:#111827;stroke-width:3}
</style></head><body><main id="card">
<div id="legend" aria-label="Видимость кривых"></div>
<svg id="chart" viewBox="0 0 700 300" role="img" aria-label="Линейный график с общей шкалой"></svg>
</main><script id="chart-data" type="application/json">__DATA__</script>
<script>
(() => {
const d=JSON.parse(document.getElementById('chart-data').textContent), $=id=>document.getElementById(id);
const colors=['#2563eb','#059669','#d97706','#9333ea','#be123c','#0891b2','#4f46e5','#525252'];
const visible=d.series.map(()=>true), names={online:'Онлайн',stores:'Магазины',partners:'Партнёры'};
const label=s=>names[s.name]||s.name, fmt=v=>v===null?'нет данных':Number(v).toLocaleString('ru-RU',{maximumFractionDigits:3});
const all=d.series.flatMap(s=>s.values.filter(v=>v!==null));
let low=Math.min(0,...all), high=Math.max(0,...all); if(low===high){high=low+1;}
const pad=(high-low)*.1; high+=pad;if(low<0)low-=pad;
const X=i=>60+(d.labels.length===1?305:(d.positions[i]-d.positions[0])/(d.positions[d.positions.length-1]-d.positions[0])*610),Y=v=>245-(v-low)/(high-low)*205;
function node(tag,attrs={},text){const e=document.createElementNS('http://www.w3.org/2000/svg',tag);for(const[k,v]of Object.entries(attrs))e.setAttribute(k,v);if(text!==undefined)e.textContent=text;return e;}
function height(){parent.postMessage({type:'iframe:height',height:Math.ceil($('card').getBoundingClientRect().height+16)},'*');}
function draw(){
 const svg=$('chart');svg.replaceChildren();svg.setAttribute('aria-label',d.title);
 svg.append(node('text',{x:60,y:18},d.yLabel));
 for(let i=0;i<=4;i++){const v=low+(high-low)*i/4,y=Y(v);svg.append(node('line',{x1:60,y1:y,x2:670,y2:y,stroke:'#e5e7eb','stroke-dasharray':'3 4'}),node('text',{x:50,y:y+4,'text-anchor':'end'},fmt(v)));}
 const step=Math.max(1,Math.ceil(d.labels.length/7));
 d.labels.forEach((v,i)=>{if(i%step===0||i===d.labels.length-1)svg.append(node('text',{x:X(i),y:274,'text-anchor':'middle'},v));});
 d.series.forEach((s,j)=>{if(!visible[j])return;let path='',fresh=true;
 s.values.forEach((v,i)=>{if(v===null){fresh=true;return;}path+=(fresh?'M':'L')+X(i)+','+Y(v)+' ';fresh=false;});
 svg.append(node('path',{d:path,fill:'none',stroke:colors[j],'stroke-width':3,'stroke-linejoin':'round','data-series':s.name}));
 s.values.forEach((v,i)=>{if(v===null)return;const p=node('circle',{cx:X(i),cy:Y(v),r:4.5,fill:colors[j],class:'point',tabindex:0,'aria-label':d.labels[i]+' '+label(s)+' '+fmt(v)});p.append(node('title',{},d.labels[i]+' · '+label(s)+': '+fmt(v)));svg.append(p);});
 });
 requestAnimationFrame(height);
}
d.series.forEach((s,j)=>{const b=document.createElement('button');b.type='button';b.setAttribute('aria-pressed','true');b.setAttribute('aria-label',label(s));const dot=document.createElement('span');dot.className='dot';dot.style.background=colors[j];b.append(dot,document.createTextNode(label(s)));b.addEventListener('click',()=>{visible[j]=!visible[j];b.setAttribute('aria-pressed',String(visible[j]));draw();});$('legend').append(b);});
new ResizeObserver(height).observe($('card'));window.addEventListener('load',height);draw();
})();
</script></body></html>'''
