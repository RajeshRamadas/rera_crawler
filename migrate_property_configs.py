"""
One-time migration script: populate property_configs text[] column with
canonical configuration labels extracted from uploaded_documents JSONB.

Run: python migrate_property_configs.py
"""
import psycopg2
import psycopg2.extras
import time
import re
from collections import defaultdict

DB_URI = "postgresql://rera_user:rera_password@localhost:5432/rera_db"

_PLOT_KEYS = {'Plot Type (Site Dimension in Mtr)', 'Plot Size', 'Plot Type'}
_CONFIG_KEYS = ['Unit Type', 'Type of Inventory', 'Plot Type (Site Dimension in Mtr)', 'Plot Size', 'Plot Type']
_SKIP_VALS = {'TOTAL', 'RESIDENTIAL', 'ODD', 'EVEN', 'ODD SITE', 'ODD SITES',
              'OTHER-ROOMS', 'OTHERS-ROOMS', 'EWS', '0.00', 'NA', 'N/A', ''}

def _normalize_config_label(raw: str, is_plot_key: bool = False):
    s = raw.strip()
    upper = s.upper()
    if is_plot_key or any(k in upper for k in ['PLOT SIZE', 'SITE DIMENSION', 'PLOTTED']):
        return 'Plots'
    if 'VILLA' in upper:
        return 'Villa'
    # BHK anywhere in string: "3BHK", "3.5BHK", "TYPE-H - 3BHK+3T", "3 BEDROOM", etc.
    m = re.search(r'(\d+(?:\.\d+)?)\s*(?:BHK|BED(?:ROOM)?)', upper)
    if m:
        try:
            n = float(m.group(1))
        except ValueError:
            n = 0
        if n < 1:
            return None
        n_int = int(n)
        if n_int >= 4:
            return '4 BHK+'
        if n_int == 0:
            return None
        return f'{n_int} BHK'
    if any(k in upper for k in ['SHOP', 'COMMERCIAL', 'OFFICE', 'CONVENTION', 'MULTIPLEX',
                                  'FOOD COURT', 'AMENITY', 'STORE ROOM']):
        return None
    return s or None

def extract_configs(uploaded_docs, project_type: str = ''):
    if not uploaded_docs:
        return ['Plots'] if project_type and 'plot' in project_type.lower() else []
    rows_by_id = defaultdict(dict)
    for item in uploaded_docs:
        header = item.get('financial_year', '').strip()
        row_id = item.get('document_type', '')
        val = item.get('file', '').strip()
        rows_by_id[row_id][header] = val
    configs = set()
    for row_id, rdata in rows_by_id.items():
        for key in _CONFIG_KEYS:
            raw = rdata.get(key, '').strip()
            if not raw:
                continue
            upper = raw.upper()
            if upper in _SKIP_VALS:
                continue
            try:
                float(upper)
                continue
            except ValueError:
                pass
            label = _normalize_config_label(raw, is_plot_key=(key in _PLOT_KEYS))
            if label:
                configs.add(label)
            break
    if not configs and project_type and 'plot' in project_type.lower():
        configs.add('Plots')
    order = {'1 BHK': 0, '2 BHK': 1, '3 BHK': 2, '4 BHK+': 3, 'Plots': 4, 'Villa': 5}
    return sorted(list(configs), key=lambda x: order.get(x, 99))


def main():
    conn = psycopg2.connect(DB_URI, cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = True

    with conn.cursor() as cur:
        print('Step 1: Ensure property_configs column exists...')
        cur.execute('ALTER TABLE rera_projects ADD COLUMN IF NOT EXISTS property_configs text[];')
        print('Step 2: Creating GIN index on property_configs...')
        cur.execute('CREATE INDEX IF NOT EXISTS idx_rera_property_configs ON rera_projects USING gin (property_configs);')
        print('  ✓ Index ready.')

        print('Step 3: Fetching all projects...')
        t0 = time.time()
        cur.execute("SELECT registration_no, data->'uploaded_documents' as docs, data->>'project_type' as ptype FROM rera_projects;")
        rows = cur.fetchall()
        print(f'  ✓ Fetched {len(rows)} rows in {time.time()-t0:.2f}s')

        print('Step 4: Extracting canonical configs...')
        t0 = time.time()
        updates = []
        stats = {}
        for r in rows:
            configs = extract_configs(r['docs'], r['ptype'] or '')
            updates.append((configs or None, r['registration_no']))
            for c in configs:
                stats[c] = stats.get(c, 0) + 1
        print(f'  ✓ Processed in {time.time()-t0:.2f}s')
        print('  Config distribution:', dict(sorted(stats.items(), key=lambda x: -x[1])))

        print(f'Step 5: Bulk-updating {len(updates)} rows...')
        t0 = time.time()
        psycopg2.extras.execute_batch(
            cur,
            'UPDATE rera_projects SET property_configs = %s WHERE registration_no = %s;',
            updates,
            page_size=500
        )
        print(f'  ✓ Updated in {time.time()-t0:.2f}s')

    conn.close()
    print('\n✅ Migration complete!')


if __name__ == '__main__':
    main()
