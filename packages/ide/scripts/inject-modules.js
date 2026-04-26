/**
 * Inject custom Polaris frontend modules into Theia's generated
 * src-gen/frontend/index.js AFTER `theia generate` but BEFORE webpack.
 */

const fs = require('fs');
const path = require('path');

const indexPath = path.resolve(__dirname, '..', 'src-gen', 'frontend', 'index.js');

if (!fs.existsSync(indexPath)) {
    console.error('src-gen/frontend/index.js not found — run `theia generate` first');
    process.exit(1);
}

let content = fs.readFileSync(indexPath, 'utf8');

// Theia generates either require() or import() calls depending on version.
// Match both patterns for the getting-started module as our anchor.
const requireAnchor = `require('@theia/getting-started/lib/browser/getting-started-frontend-module')`;
const importAnchor = `import('@theia/getting-started/lib/browser/getting-started-frontend-module')`;

const requireInjection = `        await load(container, require('../../lib/browser/polaris-frontend-module'));`;
const importInjection = `        await load(container, import('../../lib/browser/polaris-frontend-module'));`;

if (content.includes('polaris-frontend-module')) {
    console.log('inject-modules: already injected, skipping');
    process.exit(0);
}

let injected = false;

if (content.includes(requireAnchor)) {
    const line = content.split('\n').find(l => l.includes(requireAnchor));
    content = content.replace(line, line + '\n' + requireInjection);
    injected = true;
} else if (content.includes(importAnchor)) {
    const line = content.split('\n').find(l => l.includes(importAnchor));
    content = content.replace(line, line + '\n' + importInjection);
    injected = true;
}

if (!injected) {
    // Fallback: inject before "return container;"
    const fallback = 'return container;';
    if (content.includes(fallback)) {
        content = content.replace(fallback, requireInjection + '\n    ' + fallback);
        injected = true;
    }
}

if (injected) {
    fs.writeFileSync(indexPath, content, 'utf8');
    console.log('inject-modules: injected polaris-frontend-module');
} else {
    console.error('inject-modules: could not find injection anchor');
    process.exit(1);
}
