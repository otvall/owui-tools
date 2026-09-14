import asyncio
import copy
import json
import re
import unittest

import metadata_sql_chart_tool as m


class MetadataChartTests(unittest.TestCase):
    def setUp(self):
        self.tool = m.Tools()
        self.meta = {}
        self.user = {'id': 'test-owner'}

    def load(self, scenario='valid'):
        return asyncio.run(self.tool.load_demo_sql_metadata(scenario, self.meta, self.user))

    def plot(self, dataset_id, x='day', ys=None, meta=None, user=None):
        return asyncio.run(self.tool.plot_metadata_chart(dataset_id, x, ys or ['online', 'stores', 'partners'],
                         self.meta if meta is None else meta, self.user if user is None else user))

    def test_success_without_raw_rows_in_model_result(self):
        summary = self.load()
        self.assertNotIn('rows', summary)
        response, context = self.plot(summary['dataset_id'])
        self.assertEqual(response.headers['content-disposition'], 'inline')
        self.assertEqual(context['code'], 'CHART_READY')
        self.assertNotIn('rows', context)
        payload = json.loads(re.search(r'application/json">(.*?)</script>', response.body.decode()).group(1))
        self.assertEqual(payload['series'][0]['values'], [120, 145, 132, 178, 165, 210, 235])

    def test_data_errors_and_recovery(self):
        for scenario, code in [('invalid_numeric','NON_NUMERIC_VALUE'), ('empty','EMPTY_DATASET'), ('duplicate_x','DUPLICATE_X')]:
            with self.subTest(scenario=scenario):
                result = self.plot(self.load(scenario)['dataset_id'])
                self.assertEqual(result['code'], code)
                self.assertFalse(result['chart_created'])
                self.assertTrue(result['suggested_action'])
                self.assertNotIn('not_a_number', json.dumps(result))
        good = self.plot(self.load()['dataset_id'])
        self.assertEqual(good[1]['code'], 'CHART_READY')

    def test_scope_and_owner(self):
        sid = self.load()['dataset_id']
        self.assertEqual(self.plot(sid, meta={})['code'], 'DATASET_NOT_FOUND')
        self.assertEqual(self.plot(sid, user={'id': 'someone-else'})['code'], 'ACCESS_DENIED')

    def test_column_and_unit_errors(self):
        sid = self.load()['dataset_id']
        self.assertEqual(self.plot(sid, ys=['missing'])['code'], 'COLUMN_NOT_FOUND')
        self.meta[m.NAMESPACE][sid]['columns'][2]['unit'] = 'count'
        self.assertEqual(self.plot(sid)['code'], 'MIXED_UNITS')

    def test_invalid_values_are_not_silently_coerced(self):
        sid = self.load()['dataset_id']
        rows = self.meta[m.NAMESPACE][sid]['rows']
        for val, code in [(True,'NON_NUMERIC_VALUE'), ('123','NON_NUMERIC_VALUE'), (float('nan'),'INVALID_NUMBER'), (float('inf'),'INVALID_NUMBER'), (10**500,'INVALID_NUMBER')]:
            rows[0]['online'] = val
            self.assertEqual(self.plot(sid)['code'], code)

    def test_order_and_dates(self):
        sid = self.load()['dataset_id']
        rows = self.meta[m.NAMESPACE][sid]['rows']
        rows.reverse()
        self.assertEqual(self.plot(sid)['code'], 'UNSORTED_X')
        rows[0]['day'] = '09/01/2026'
        self.assertEqual(self.plot(sid)['code'], 'INVALID_DATE')

    def test_null_and_empty_series(self):
        sid = self.load()['dataset_id']
        rows = self.meta[m.NAMESPACE][sid]['rows']
        rows[1]['stores'] = None
        self.assertEqual(self.plot(sid)[1]['code'], 'CHART_READY')
        for row in rows:
            row['stores'] = None
        self.assertEqual(self.plot(sid)['code'], 'EMPTY_SERIES')

    def test_limits(self):
        result = m.store_sql_result([{}] * (m.MAX_ROWS+1), [], self.meta, self.user)
        self.assertEqual(result['code'], 'TOO_MANY_ROWS')
        for _ in range(m.MAX_DATASETS):
            self.load()
        self.assertEqual(self.load()['code'], 'DATASET_LIMIT')

    def test_html_escaping_and_actual_date_spacing(self):
        sid = self.load()['dataset_id']
        ds = self.meta[m.NAMESPACE][sid]
        ds['rows'][6]['day'] = '2026-09-10'
        payload = m.validate_dataset(ds, 'day', ['online'], self.user)
        self.assertEqual(payload['positions'][-1] - payload['positions'][0], 9)
        ds['columns'][0]['type'] = 'category'
        ds['rows'][0]['day'] = '</script><img src=x onerror=alert(1)>'
        response, _ = self.plot(sid)
        self.assertNotIn(b'</script><img', response.body)


if __name__ == '__main__':
    unittest.main()
