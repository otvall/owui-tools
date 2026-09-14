"""
title: Rich UI Demo
description: Interactive local demo card with a chart and a working button.
version: 1.0.0
required_open_webui_version: 0.11.1
"""

from fastapi.responses import HTMLResponse


class Tools:
    def __init__(self):
        self.citation = False

    async def show_rich_ui(self):
        """Display an interactive Rich UI demo widget with sample data and a button.

        Call this when the user asks to demonstrate Rich UI, a dashboard, or an
        interactive widget. The result renders directly in the conversation.
        """
        html = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Rich UI — интерактивная демонстрация</title>
<style>
*{box-sizing:border-box}html,body{margin:0;background:transparent}
body{padding:8px;font:15px/1.5 system-ui,-apple-system,sans-serif;color:#eef4ff}
main{max-width:740px;margin:auto;padding:24px;border:1px solid #334563;
border-radius:22px;background:linear-gradient(135deg,#132338,#1b1831)}
header{display:flex;justify-content:space-between;align-items:center;gap:12px}
.badge{font-size:11px;letter-spacing:.12em;color:#8ee8cb}
.version{font-size:12px;color:#bdcce0;border:1px solid #40506a;
border-radius:20px;padding:4px 10px;white-space:nowrap}
h1{font-size:24px;line-height:1.2;margin:12px 0 8px}
p{margin:0;color:#bdcce0}.stats{display:flex;gap:32px;margin:22px 0 14px}
.label{font-size:12px;color:#bdcce0}.value{font-size:28px;font-weight:700}
.chart{display:block;width:100%;height:150px;margin:8px 0}
.axis{stroke:#40506a;stroke-width:1}.bar{fill:#7c9aff}
.bar:last-of-type{fill:#76e0bd}svg text{fill:#bdcce0;font:12px system-ui}
footer{display:flex;align-items:center;gap:16px;flex-wrap:wrap;margin-top:16px}
button{border:0;border-radius:12px;background:#85edc9;color:#0f2530;
padding:12px 18px;font:600 14px system-ui;cursor:pointer}
button:hover{background:#b0ffe4}button:focus-visible{outline:3px solid #fff;outline-offset:3px}
#status{font-size:13px;color:#bdcce0}.note{font-size:11px;margin-top:14px;color:#97aac6}
@media(max-width:420px){main{padding:18px}h1{font-size:21px}.stats{gap:24px}}
</style>
</head>
<body>
<main id="card">
  <header><span class="badge">RICH UI · LIVE DEMO</span><span class="version">Open WebUI 0.11.1</span></header>
  <h1>Пульс демо-проекта</h1>
  <p>Демонстрационные заявки за рабочую неделю</p>
  <section class="stats" aria-label="Показатели">
    <div><div class="label">Всего заявок</div><div class="value" id="total">150</div></div>
    <div><div class="label">Нажатий кнопки</div><div class="value" id="count">0</div></div>
  </section>
  <svg class="chart" viewBox="0 0 500 150" role="img" aria-labelledby="chart-title">
    <title id="chart-title">Заявки: Пн 18, Вт 27, Ср 23, Чт 38, Пт 44</title>
    <line class="axis" x1="20" y1="120" x2="480" y2="120"/>
    <g id="bars"></g>
    <text x="65" y="144" text-anchor="middle">Пн</text>
    <text x="155" y="144" text-anchor="middle">Вт</text>
    <text x="245" y="144" text-anchor="middle">Ср</text>
    <text x="335" y="144" text-anchor="middle">Чт</text>
    <text x="425" y="144" text-anchor="middle">Пт</text>
  </svg>
  <footer><button id="update" type="button">Обновить данные +1</button>
    <span id="status" role="status" aria-live="polite">Готово к проверке</span></footer>
  <p class="note">Только демонстрационные данные · без CDN и сетевых запросов</p>
</main>
<script>
(() => {
  const base = [18, 27, 23, 38, 44];
  const days = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт'];
  let clicks = 0;
  const byId = id => document.getElementById(id);
  function sendHeight() {
    // v0.11.1 FullHeightIframe.svelte accepts this message from its own iframe.
    // Measure the card, avoiding a feedback loop with the iframe viewport height.
    parent.postMessage({type: 'iframe:height',
      height: Math.ceil(byId('card').getBoundingClientRect().height + 16)}, '*');
  }
  function render() {
    const values = base.map((value, i) => value + clicks * (i + 2));
    byId('total').textContent = values.reduce((a, b) => a + b, 0);
    byId('count').textContent = clicks;
    const max = Math.max(...values);
    byId('bars').replaceChildren();
    values.forEach((value, i) => {
      const height = value / max * 90;
      const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
      for (const [key, val] of Object.entries({x: 38 + i * 90,
        y: 120 - height, width: 54, height, rx: 7, class: 'bar'})) {
        rect.setAttribute(key, String(val));
      }
      const label = document.createElementNS('http://www.w3.org/2000/svg', 'text');
      label.setAttribute('x', String(65 + i * 90));
      label.setAttribute('y', String(112 - height));
      label.setAttribute('text-anchor', 'middle');
      label.textContent = value;
      byId('bars').append(rect, label);
    });
    byId('chart-title').textContent = 'Заявки: ' + values.map((v, i) => days[i] + ' ' + v).join(', ');
    byId('status').textContent = clicks ? 'Обновлено · нажатий: ' + clicks : 'Готово к проверке';
    requestAnimationFrame(sendHeight);
  }
  byId('update').addEventListener('click', () => { clicks += 1; render(); });
  new ResizeObserver(sendHeight).observe(byId('card'));
  window.addEventListener('load', sendHeight);
  render();
})();
</script>
</body>
</html>"""
        return HTMLResponse(
            content=html,
            headers={"Content-Disposition": "inline"},
        )
