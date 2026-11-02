import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const sourceUrl = new URL(
  '../../web/static/easel-icon-transparent.png', import.meta.url);
const publishedUrl = new URL(
  '../../web/frontend/public/static/easel-icon-transparent.png',
  import.meta.url);
const indexUrl = new URL('../../web/frontend/index.html', import.meta.url);
const sidebarUrl = new URL(
  '../../web/frontend/src/components/Sidebar.tsx', import.meta.url);
const chatPageUrl = new URL(
  '../../web/frontend/src/components/ChatPage.tsx', import.meta.url);

const sha256 = (bytes) => createHash('sha256').update(bytes).digest('hex');

function pngSize(bytes) {
  assert.equal(bytes.subarray(1, 4).toString('ascii'), 'PNG');
  assert.equal(bytes.subarray(4, 8).toString('ascii'), '\r\n\x1a\n');
  return { width: bytes.readUInt32BE(16), height: bytes.readUInt32BE(20) };
}

test('published brand asset is byte-identical to the original Easel image', () => {
  const source = readFileSync(sourceUrl);
  const published = readFileSync(publishedUrl);
  assert.equal(sha256(published), sha256(source));
  assert.equal(sha256(published),
    '71c260f5a28e82aced0a513186ebdcf43782b068633e3a7ca5f159d3e3606132');
  assert.deepEqual(pngSize(published), { width: 800, height: 800 });
});

test('index, sidebar and chat hero keep the packaged brand path', () => {
  const index = readFileSync(indexUrl, 'utf8');
  const sidebar = readFileSync(sidebarUrl, 'utf8');
  const chat = readFileSync(chatPageUrl, 'utf8');
  for (const text of [index, sidebar, chat]) {
    assert.match(text, /(?:\.\/|\/)static\/easel-icon-transparent\.png/);
  }
});
