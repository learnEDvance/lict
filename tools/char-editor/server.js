const http = require('http');
const fs = require('fs');
const path = require('path');

const ROOT = __dirname;
const CHARS_DIR = path.join(ROOT, '..', '..', 'chars');

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.svg': 'image/svg+xml; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.txt': 'text/plain; charset=utf-8',
};

function sanitize(name) {
  let n = String(name || '').trim().replace(/[^A-Za-z0-9._-]/g, '_');
  n = n.replace(/^\.+/, '').replace(/\.svg$/i, '');
  return n.slice(0, 80);
}

http.createServer((req, res) => {
  const url = new URL(req.url, 'http://localhost');
  const p = url.pathname;

  if (p === '/save' && req.method === 'POST') {
    let body = '';
    req.on('data', (c) => (body += c));
    req.on('end', () => {
      fs.mkdirSync(CHARS_DIR, { recursive: true });
      try {
        const j = JSON.parse(body);
        const name = sanitize(j.name);
        if (!name) throw new Error('invalid name');
        const file = path.join(CHARS_DIR, name + '.svg');
        if (!file.startsWith(CHARS_DIR)) throw new Error('invalid path');
        fs.writeFileSync(file, j.svg);
        res.writeHead(200, { 'Content-Type': MIME['.json'] });
        res.end(JSON.stringify({ ok: true, file: name + '.svg' }));
      } catch (e) {
        res.writeHead(400, { 'Content-Type': MIME['.json'] });
        res.end(JSON.stringify({ ok: false, error: String((e && e.message) || e) }));
      }
    });
    return;
  }

  if (p === '/list') {
    fs.mkdirSync(CHARS_DIR, { recursive: true });
    const files = fs.readdirSync(CHARS_DIR).filter((f) => f.endsWith('.svg')).sort();
    res.writeHead(200, { 'Content-Type': MIME['.json'] });
    res.end(JSON.stringify(files));
    return;
  }

  if (p.startsWith('/chars/')) {
    const name = sanitize(p.slice('/chars/'.length));
    const file = path.join(CHARS_DIR, name + '.svg');
    if (!name || !file.startsWith(CHARS_DIR) || !fs.existsSync(file)) {
      res.writeHead(404, { 'Content-Type': 'text/plain' });
      res.end('not found');
      return;
    }
    res.writeHead(200, { 'Content-Type': MIME['.svg'] });
    fs.createReadStream(file).pipe(res);
    return;
  }

  let file;
  if (p === '/' || p === '/editor.html') {
    file = path.join(ROOT, 'editor.html');
  } else {
    file = path.join(ROOT, p);
  }
  if (!fs.existsSync(file) || !file.startsWith(ROOT)) {
    res.writeHead(404, { 'Content-Type': 'text/plain' });
    res.end('not found');
    return;
  }
  const ext = path.extname(file);
  res.writeHead(200, { 'Content-Type': MIME[ext] || 'application/octet-stream' });
  fs.createReadStream(file).pipe(res);
}).listen(3010, () => console.log('char editor: http://localhost:3010'));