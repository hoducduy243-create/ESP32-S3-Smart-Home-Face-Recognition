"""Exercise the real dashboard script in installed headless Chrome, with fake APIs.

No production server, credentials or database are used. Run manually:
python cloud_server/tests/browser_samples.py
"""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile

from jinja2 import Environment, FileSystemLoader

SERVER = Path(__file__).resolve().parents[1]

BOOTSTRAP = r"""<script>
window.browserErrors = [];
window.addEventListener('error', e => browserErrors.push(e.message));
window.addEventListener('unhandledrejection', e => browserErrors.push(String(e.reason)));
window.apiCalls = [];
window.sampleUploads = [];
window.sampleCounts = new Map([['7', {unmasked: 1, masked: 1, legacy: 0}]]);
window.fetch = async (url, options = {}) => {
  apiCalls.push({url: String(url), method: options.method || 'GET'});
  let data = {};
  let status = 200;
  if (String(url).includes('/samples')) {
    const id = String(url).match(/\/(\d+)\/samples/)[1];
    const counts = sampleCounts.get(id) || {unmasked: 0, masked: 0, legacy: 0};
    if (options.method === 'POST') {
      const fields = options.body;
      const mode = fields.get('mode');
      if (!['masked', 'unmasked'].includes(mode) || fields.get('csrf_token') !== 'test-csrf'
          || fields.getAll('photos').length !== 1) throw new Error('Incorrect sample form');
      sampleUploads.push({mode, names: fields.getAll('photos').map(file => file.name)});
      counts[mode] += 1;
      sampleCounts.set(id, counts);
      data = {success: true, user_id: Number(id), mode, added: 1,
              ...counts, sample_count: counts.unmasked + counts.masked + counts.legacy};
    } else if (String(url).includes('/404/')) {
      data = {detail: 'Không tải được số mẫu'}; status = 503;
    } else data = {...counts, sample_count: counts.unmasked + counts.masked + counts.legacy};
  } else if (String(url).includes('/connection-logs')) data = {logs: []};
  else if (String(url).includes('/camera-status')) data = {master: {}, slave: {}};
  else data = {logs: [], total: 0, unlocked: 0, denied: 0};
  return {ok: status === 200, status, json: async () => data};
};
</script>"""

SCENARIO = r"""<script>
window.addEventListener('load', async () => {
  const check = (value, label) => { if (!value) throw new Error(label); };
  const tick = () => new Promise(resolve => setTimeout(resolve, 30));
  try {
    showView('users');
    const button = document.querySelector('[data-add-samples="7"]');
    button.click(); await tick();
    const dialog = document.getElementById('samples-dialog');
    const form = document.getElementById('samples-form-unmasked');
    const maskedForm = document.getElementById('samples-form-masked');
    check(dialog.open && dialog.matches(':modal'), 'Existing user: dialog did not open');
    check(form.action.endsWith('/api/users/7/samples'), 'Wrong existing user');
    check(document.getElementById('samples-description').textContent.includes('Test user'), 'Missing name');
    check(document.getElementById('samples-description').textContent.includes('2/20'), 'Missing summary');
    const selectPhoto = (target, name) => {
      const transfer = new DataTransfer();
      transfer.items.add(new File(['fake image'], name, {type: 'image/jpeg'}));
      target.elements.photos.files = transfer.files;
    };
    selectPhoto(form, 'plain.jpg');
    selectPhoto(maskedForm, 'mask.jpg');
    form.requestSubmit(); await tick();
    check(dialog.open, 'Dialog closed before the other group could be uploaded');
    check(apiCalls.some(c => c.method === 'POST' && c.url.endsWith('/api/users/7/samples')), 'No upload request');
    check(sampleUploads[0].mode === 'unmasked' && sampleUploads[0].names.join() === 'plain.jpg', 'Unmasked payload mixed groups');
    check(document.getElementById('samples-count-unmasked').textContent.startsWith('2 '), 'Unmasked count not updated');
    check(document.getElementById('samples-count-masked').textContent.startsWith('1 '), 'Masked count changed on unmasked upload');
    check(maskedForm.elements.photos.files[0].name === 'mask.jpg', 'Other selection was reset');
    check(form.elements.photos.files.length === 0, 'Uploaded selection was not cleared');
    maskedForm.requestSubmit(); await tick();
    check(sampleUploads[1].mode === 'masked' && sampleUploads[1].names.join() === 'mask.jpg', 'Masked payload mixed groups');
    check(document.getElementById('samples-count-unmasked').textContent.startsWith('2 '), 'Unmasked count changed on masked upload');
    check(document.getElementById('samples-count-masked').textContent.startsWith('2 '), 'Masked count not updated');
    check(!document.getElementById('samples-submit-unmasked').disabled, 'Submit stayed disabled');
    dialog.close(); await tick();

    // Same control must work inside the quick-management modal.
    openQuick('users'); button.click(); await tick();
    check(dialog.open && dialog.matches(':modal'), 'Quick management: dialog did not open');
    dialog.querySelector('[data-close-dialog]').click(); await tick();
    check(!dialog.open && document.getElementById('workspace-dialog').open, 'Parent management dialog lost');

    // A failed count request must leave the file picker available.
    button.dataset.addSamples = '404'; button.click(); await tick();
    check(dialog.open, 'Summary failure hid dialog');
    check(!document.getElementById('samples-alert').hidden, 'Summary failure not shown');
    dialog.close(); document.getElementById('workspace-dialog').close(); await tick();

    addUserRow({id: 8, name: 'New user', user_code: 'NEW', role: 'Member', avatar_path: null});
    document.querySelector('[data-add-samples="8"]').click(); await tick();
    check(dialog.open && form.action.endsWith('/api/users/8/samples'), 'Dynamic user: dialog did not open');
    check(browserErrors.length === 0, JSON.stringify(browserErrors));
    const report = document.createElement('pre'); report.id = 'browser-result';
    report.textContent = 'PASS: independent group files/counts/uploads, existing user, quick modal, API error, dynamic user';
    document.body.append(report);
  } catch (error) {
    const report = document.createElement('pre'); report.id = 'browser-result';
    report.textContent = 'FAIL: ' + error.stack + '\n' + JSON.stringify(browserErrors);
    document.body.append(report);
  }
});
</script>"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--chrome', default=r'C:\Program Files\Google\Chrome\Application\chrome.exe')
    args = parser.parse_args()
    env = Environment(loader=FileSystemLoader(SERVER / 'templates'), autoescape=True)
    html = env.get_template('index.html').render(
        users=[dict(id=7, name='Test user', user_code='TEST', role='Member', avatar_path='')],
        total_access=0, unlocked_count=0, denied_count=0, csrf_token='test-csrf',
        url_for=lambda _, path: (SERVER / 'static' / path).as_uri())
    html = html.replace('<head>', '<head>' + BOOTSTRAP).replace('</body>', SCENARIO + '</body>')
    with tempfile.TemporaryDirectory(prefix='samples-browser-') as directory:
        page = Path(directory) / 'dashboard.html'
        page.write_text(html, encoding='utf-8')
        process = subprocess.run([
            args.chrome, '--headless=new', '--no-first-run', '--no-default-browser-check',
            '--disable-extensions', '--disable-background-networking', '--disable-sync',
            '--allow-file-access-from-files', '--no-proxy-server',
            '--user-data-dir=' + str(Path(directory) / 'profile'),
            '--virtual-time-budget=4000', '--dump-dom', page.as_uri(),
        ], capture_output=True, encoding='utf-8', errors='replace', timeout=45,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        import re
        match = re.search(r'<pre id="browser-result">(.*?)</pre>', process.stdout, re.S)
        if not match:
            raise SystemExit('Browser did not finish test: ' + process.stderr[-1500:])
        print(match[1])
        if not match[1].startswith('PASS:'):
            raise SystemExit(1)


if __name__ == '__main__':
    main()
