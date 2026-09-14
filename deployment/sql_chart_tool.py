"""
title: SQL Charts Demo
description: Plot SQL result rows as multiple interactive curves on one chart.
version: 1.1.0
required_open_webui_version: 0.11.1
"""

import json
import math
import sqlite3

from fastapi.responses import HTMLResponse


class Tools:
    def __init__(self):
        self.citation = False

    async def get_demo_sql_data(self) -> dict:
        """Execute a fixed SELECT on an isolated in-memory SQLite demo database.

        Returns fictional revenue in thousands of rubles for 7 days and 3 sales
        channels. Use ONLY for demonstrations, never describe it as real business data.
        Pass the returned rows unchanged to plot_sql_chart to visualize them.
        """
        values = [(120, 95, 55), (145, 102, 70), (132, 118, 62),
                  (178, 110, 88), (165, 138, 94), (210, 152, 105), (235, 160, 128)]
        with sqlite3.connect(':memory:') as db:
            db.execute('CREATE TABLE sales(day TEXT, channel TEXT, revenue REAL)')
            db.executemany('INSERT INTO sales VALUES (?,?,?)', [
                (f'2026-09-{i + 1:02}', channel, value)
                for i, row in enumerate(values)
                for channel, value in zip(['Онлайн', 'Магазины', 'Партнёры'], row)
            ])
            query = '''SELECT day,
  SUM(CASE WHEN channel = 'Онлайн' THEN revenue ELSE 0 END) AS online,
  SUM(CASE WHEN channel = 'Магазины' THEN revenue ELSE 0 END) AS stores,
  SUM(CASE WHEN channel = 'Партнёры' THEN revenue ELSE 0 END) AS partners
FROM sales GROUP BY day ORDER BY day'''
            db.execute('PRAGMA query_only=ON')
            db.row_factory = sqlite3.Row
            rows = [dict(row) for row in db.execute(query)]
        return {'source': 'Fictional in-memory SQLite demo, not production data',
                'sql': query, 'rows': rows, 'x_column': 'day',
                'y_columns': ['online', 'stores', 'partners'],
                'y_label': 'Выручка, тыс. ₽'}

    async def plot_sql_chart(
        self, rows_json: str, x_column: str, y_columns: list[str],
        title: str = 'График по результату SQL', y_label: str = 'Значение',
    ):
        """Render SQL result rows as an interactive multi-series line chart.

        :param rows_json: JSON array of row objects from SQL. Preserve actual values and order. Example: [{"day":"2026-09-01","online":120,"stores":95}]. SQL must sort the X column. Null Y values create gaps, not zeroes.
        :param x_column: Column for shared X labels, e.g. day. Labels must be unique; X positions are equally spaced categories.
        :param y_columns: Numeric columns to draw, one curve per column, e.g. ["online","stores","partners"]. All columns must use the same units.
        :param title: Descriptive chart title. Label fictional demo data as demonstration data.
        :param y_label: Shared Y axis label including units, e.g. Выручка, тыс. ₽.
        """
        try:
            if any(ord(c) < 32 for c in title + y_label):
                raise ValueError('Control characters in title/y_label. Retry using plain text and units RUB, without special symbols.')
            if len(rows_json) > 250_000:
                raise ValueError('Слишком большой JSON: максимум 250000 символов.')
            rows = json.loads(rows_json)
            if not isinstance(rows, list) or not 1 <= len(rows) <= 500:
                raise ValueError('Нужен массив из 1–500 строк; агрегируйте большие выборки в SQL.')
            if not 1 <= len(y_columns) <= 8 or len(set(y_columns)) != len(y_columns):
                raise ValueError('Укажите 1–8 разных числовых колонок.')
            if x_column in y_columns:
                raise ValueError('Колонка X не должна входить в Y.')
            labels, series = [], {key: [] for key in y_columns}
            for row in rows:
                if not isinstance(row, dict) or x_column not in row or row[x_column] is None:
                    raise ValueError('Каждая строка должна содержать непустую колонку X.')
                if not isinstance(row[x_column], (str, int, float)) or isinstance(row[x_column], bool):
                    raise ValueError('Значения X должны быть строками или числами.')
                labels.append(str(row[x_column]))
                for key in y_columns:
                    if key not in row:
                        raise ValueError(f'Отсутствует колонка {key}.')
                    value = row[key]
                    if value is not None and (isinstance(value, bool) or
                        not isinstance(value, (int, float)) or not math.isfinite(value)):
                        raise ValueError(f'В колонке {key} нужны конечные числа или null.')
                    series[key].append(value)
            if len(set(labels)) != len(labels):
                raise ValueError('Повторяющиеся значения X: сначала сгруппируйте данные в SQL.')
            if not any(v is not None for values in series.values() for v in values):
                raise ValueError('Нет числовых точек для графика.')
        except (ValueError, TypeError, OverflowError) as exc:
            return {'error': str(exc), 'action': 'Correct the arguments and call again.'}

        data = json.dumps({'title': title, 'yLabel': y_label, 'labels': labels,
                           'series': [{'name': key, 'values': series[key]} for key in y_columns]},
                          ensure_ascii=True, allow_nan=False).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
        html = r'''<!doctype html><html lang="ru"><head><meta charset="utf-8">
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
const X=i=>60+(d.labels.length===1?305:i/(d.labels.length-1)*610),Y=v=>245-(v-low)/(high-low)*205;
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
</script></body></html>'''.replace('__DATA__', data)
        return HTMLResponse(content=html, headers={'Content-Disposition': 'inline'})
