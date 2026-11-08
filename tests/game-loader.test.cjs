// Run with: node --experimental-vm-modules --test tests/game-loader.test.cjs
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '..');
const timestampKey = 'game-data-last-updated-v2-cyggachievements';
const dataKey = 'game-data-cache-v2-cyggachievements';
const game = {
    appid: '730',
    info: { name: 'Test game', achievements: { WIN: { name: 'Winner' } } },
    achievements: { WIN: { earned: true, earned_time: 123 } }
};
const data = { last_updated: 123, games: [game] };
const response = (body, status = 200) => ({
    ok: status >= 200 && status < 300, status,
    json: async () => body,
    text: async () => typeof body === 'string' ? body : JSON.stringify(body)
});

async function setup(handler, storage = new Map()) {
    const requests = [];
    const elements = new Map();
    const localStorage = storage instanceof Map ? {
        get length() { return storage.size; },
        key: (index) => [...storage.keys()][index],
        getItem: (key) => storage.get(key) ?? null,
        setItem: (key, value) => storage.set(key, value),
        removeItem: (key) => storage.delete(key)
    } : storage;
    const document = { getElementById(id) {
        if (!elements.has(id)) elements.set(id, { style: {}, innerHTML: '' });
        return elements.get(id);
    } };
    const context = vm.createContext({
        console: { log() {}, warn() {}, error() {} },
        window: {
            location: { href: 'https://cyggachievements.github.io/achievement-viewer/' },
            addEventListener() {}
        },
        document, localStorage,
        fetch: async (url, options) => {
            requests.push({ url, options });
            if (url.endsWith('gamercard.html')) return response(null, 404);
            return handler(url, options);
        }
    });
    const utils = new vm.SourceTextModule(fs.readFileSync(path.join(root, 'js/utils.js'), 'utf8'), { context });
    await utils.link(() => { throw new Error('Unexpected utility import'); });
    await utils.evaluate();
    const loader = new vm.SourceTextModule(fs.readFileSync(path.join(root, 'js/GameLoader.js'), 'utf8'), {
        context, importModuleDynamically: () => utils
    });
    await loader.link(() => utils);
    await loader.evaluate();
    return { loader: loader.namespace, module: loader, context, requests, elements, document };
}

test('loads achievements without contacting an unavailable GitHub API', async () => {
    const env = await setup((url) => {
        if (url.startsWith('https://api.github.com/')) throw new TypeError('Failed to fetch');
        assert.ok(url.endsWith('game-data.json'));
        return response(data);
    });
    assert.equal(await env.loader.init(), true);
    assert.equal(env.loader.gamesData.size, 1);
    assert.equal(env.loader.gamesData.get('730').achievements[0].unlocked, true);
    assert.equal(env.requests.some(({ url }) => url.includes('api.github.com')), false);
});

test('fetches the full file after a partial timestamp response', async () => {
    const env = await setup((url, options) => options?.headers?.Range
        ? response('{"last_updated": 123, "games": [', 206)
        : response(data));
    assert.equal(await env.loader.init(), true);
    assert.equal(env.requests.filter(({ url }) => url.endsWith('game-data.json')).length, 2);
    assert.equal(env.loader.gamesData.size, 1);
});

test('accepts the legacy array format', async () => {
    const env = await setup(() => response([game]));
    assert.equal(await env.loader.init(), true);
    assert.equal(env.loader.gamesData.size, 1);
});

test('uses valid cached games when the server timestamp is unchanged', async () => {
    const env = await setup(() => response('{"last_updated": 123}', 206), new Map([
        [timestampKey, '123'], [dataKey, JSON.stringify([game])]
    ]));
    assert.equal(await env.loader.init(), true);
    assert.equal(env.loader.gamesData.size, 1);
    assert.equal(env.requests.filter(({ url }) => url.endsWith('game-data.json')).length, 1);
});

test('storage access restrictions do not prevent downloading games', async () => {
    const env = await setup(() => response(data), {
        get length() { throw new Error('Storage blocked'); },
        getItem() { throw new Error('Storage blocked'); },
        setItem() { throw new Error('Storage blocked'); }
    });
    assert.equal(await env.loader.init(), true);
    assert.equal(env.loader.gamesData.size, 1);
});

test('ignores malformed and non-array cache entries', async () => {
    for (const cached of ['{broken', '{}']) {
        const env = await setup(() => response(data), new Map([[timestampKey, '123'], [dataKey, cached]]));
        assert.equal(await env.loader.init(), true);
        assert.equal(env.loader.gamesData.size, 1);
    }
});

test('cache quota failures do not mark old cached games as current', async () => {
    const cache = new Map([[timestampKey, '122'], [dataKey, JSON.stringify([game])]]);
    const env = await setup(() => response(data), {
        length: 0,
        getItem: (key) => cache.get(key),
        setItem(key, value) {
            if (key === dataKey) {
                const error = new Error('Quota exceeded');
                error.name = 'QuotaExceededError';
                throw error;
            }
            cache.set(key, value);
        }
    });
    assert.equal(await env.loader.init(), true);
    assert.equal(cache.get(timestampKey), '122');
    assert.equal(env.loader.gamesData.size, 1);
});

test('falls back to user branch folders and supports .db achievement files', async () => {
    const env = await setup((url) => {
        if (url.includes('/contents/AppID')) {
            assert.ok(url.endsWith('?ref=user'));
            return response([{ type: 'dir', name: '730' }, { type: 'dir', name: '_appid_' }]);
        }
        if (url.endsWith('/730.db')) return response([{ apiname: 'WIN', achieved: 1, unlocktime: 123 }]);
        if (url.endsWith('/game-info.json')) return response(game.info);
        return response(null, 404);
    });
    assert.equal(await env.loader.init(), true);
    assert.equal(env.loader.gamesData.size, 1);
    assert.equal(env.loader.gamesData.get('730').achievements[0].unlocked, true);
});

test('reports failure when folders exist but no achievement files can be loaded', async () => {
    const env = await setup((url) => url.includes('/contents/AppID')
        ? response([{ type: 'dir', name: '730' }]) : response(null, 404));
    assert.equal(await env.loader.init(), false);
    assert.match(env.document.getElementById('results').innerHTML, /Could not load achievements/);
});

test('page keeps the loading error when both data sources fail', async () => {
    const env = await setup(() => { throw new TypeError('Failed to fetch'); });
    let renderCalls = 0;
    const exports = ['displayGames', 'showGameDetail', 'renderGameDetail', 'hideGameDetail',
        'setSortMode', 'setGridSortMode', 'handleDeepLink'];
    const renderer = new vm.SyntheticModule(exports, function () {
        for (const name of exports) this.setExport(name, () => { renderCalls++; });
    }, { context: env.context });
    const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
    const script = html.match(/<script type="module">([\s\S]*?)<\/script>/)[1];
    const page = new vm.SourceTextModule(script, { context: env.context });
    await page.link((specifier) => specifier.endsWith('GameLoader.js') ? env.module : renderer);
    await page.evaluate();
    await new Promise(setImmediate);
    assert.equal(env.loader.gamesData.size, 0);
    assert.equal(renderCalls, 0);
    assert.equal(env.document.getElementById('info').style.display, 'block');
    assert.match(env.document.getElementById('results').innerHTML, /Could not load achievements/);
});
