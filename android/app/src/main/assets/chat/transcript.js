'use strict';

const md = window.markdownit({html: false, linkify: true, breaks: true});
const root = document.getElementById('transcript');

function route(action, fields = {}) {
    return '/action/' + action + '?' + new URLSearchParams(fields);
}

function fileLink(value) {
    try {
        if (/^file:\/\/\//i.test(value)) value = new URL(value).pathname;
        else if (/^file:\/\//i.test(value)) return null;
        value = decodeURIComponent(value);
    } catch (_) { return null; }
    const location = value.match(/(?:#L|:)(\d+)(?::\d+)?$/);
    const path = location ? value.slice(0, location.index) : value;
    if (!path.startsWith('/') || path.startsWith('//') || /[\u0000-\u001f]/.test(path)) return null;
    return {path, line: location ? location[1] : 1};
}

function websiteLink(href) {
    try {
        const url = new URL(href);
        return (url.protocol === 'https:' || url.protocol === 'http:') && url.origin !== location.origin;
    } catch (_) { return false; }
}

const validateLink = md.validateLink.bind(md);
md.validateLink = url => !!fileLink(url) || validateLink(url);
md.renderer.rules.link_open = (tokens, index, options, env, renderer) => {
    const token = tokens[index];
    const href = token.attrGet('href') || '';
    const file = fileLink(href);
    if (file) token.attrSet('href', route('file', file));
    else if (!websiteLink(href)) {
        token.attrs = token.attrs.filter(([key]) => key !== 'href');
    }
    return renderer.renderToken(tokens, index, options);
};

md.renderer.rules.image = (tokens, index) => md.utils.escapeHtml(tokens[index].content);
md.renderer.rules.table_open = () => '<div class="wide"><table>';
md.renderer.rules.table_close = () => '</table></div>';

md.block.ruler.before('fence', 'math_block', (state, first, last, silent) => {
    if (state.sCount[first] - state.blkIndent >= 4) return false;
    const start = state.bMarks[first] + state.tShift[first];
    const opening = state.src.slice(start, state.eMarks[first]).trim();
    if (opening !== '$$' && opening !== '\\[') return false;
    const closing = opening === '$$' ? '$$' : '\\]';
    for (let row = first + 1; row < last; row++) {
        const text = state.src.slice(state.bMarks[row], state.eMarks[row]).trim();
        if (text !== closing) continue;
        if (silent) return true;
        const token = state.push('math', '', 0);
        token.block = true;
        token.content = state.getLines(first + 1, row, state.blkIndent, false);
        token.meta = {display: true};
        state.line = row + 1;
        return true;
    }
    return false;
}, {alt: ['paragraph', 'blockquote', 'list']});

md.inline.ruler.before('escape', 'math', (state, silent) => {
    const start = state.pos;
    const pair = [['\\[', '\\]'], ['\\(', '\\)'], ['$$', '$$'], ['$', '$']]
        .find(([delimiter]) => state.src.startsWith(delimiter, start));
    if (!pair) return false;
    const [open, close] = pair;
    const display = open === '$$' || open === '\\[';
    const from = start + open.length;
    if (open === '$' && /\s/.test(state.src[from] || ' ')) return false;
    let end = state.src.indexOf(close, from);
    while (end >= 0 && state.src[end - 1] === '\\') end = state.src.indexOf(close, end + close.length);
    if (end <= from || end + close.length > state.posMax) return false;
    if (open === '$' && (/\s/.test(state.src[end - 1]) || /\d/.test(state.src[end + 1] || '')
            || state.src.slice(from, end).includes('\n'))) return false;
    if (!silent) {
        const token = state.push('math', '', 0);
        token.content = state.src.slice(from, end);
        token.meta = {display};
    }
    state.pos = end + close.length;
    return true;
});

md.renderer.rules.math = (tokens, index) => {
    const token = tokens[index];
    try {
        const math = katex.renderToString(token.content, {
            displayMode: token.meta.display, throwOnError: true, trust: false, maxExpand: 300, maxSize: 20
        });
        return token.meta.display ? '<span class="math-display">' + math + '</span>' : math;
    } catch (_) {
        return md.utils.escapeHtml(token.content);
    }
};

function linkBareFiles(element) {
    const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
    const changes = [];
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        if (node.parentElement.closest('a, pre, .katex')) continue;
        const regex = /(^|[\s(（【])((?:\/(?!\/))[^\s()<>"`，。；）】]+?:\d+(?::\d+)?)(?=$|[\s)\]，。；）】,.!?])/gu;
        const matches = [...node.textContent.matchAll(regex)];
        if (matches.length) changes.push([node, matches]);
    }
    for (const [node, matches] of changes) {
        const fragment = document.createDocumentFragment();
        const text = node.textContent;
        let offset = 0;
        for (const match of matches) {
            const file = fileLink(match[2]);
            if (!file) continue;
            const start = match.index + match[1].length;
            fragment.append(text.slice(offset, start));
            const name = file.path.split('/').pop() + ':' + file.line;
            const link = anchor(name, 'file', file);
            link.title = match[2];
            fragment.append(link);
            offset = start + match[2].length;
        }
        fragment.append(text.slice(offset));
        node.replaceWith(fragment);
    }
}

function element(tag, className, label) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (label != null) node.textContent = label;
    return node;
}

function anchor(label, action, fields) {
    const node = element('a', '', label);
    node.href = route(action, fields);
    return node;
}

function filesPanel(turn, open) {
    const details = element('details', 'files');
    details.dataset.turn = turn.id;
    details.open = open;
    details.append(element('summary', '', turn.editLabel));
    if (!turn.files?.length) {
        details.append(element('div', 'status', turn.files_edited ? '此轮缺少文件明细' : '此轮没有已记录的文件修改'));
        return details;
    }
    for (const file of turn.files) {
        const entry = element('div', 'file');
        const row = element('div', 'file-row');
        const title = anchor(file.path, 'file', {path: file.path, line: file.line || 1});
        title.className = 'file-name';
        row.append(title);
        if (file.added != null && file.removed != null) {
            row.append(element('span', 'added', '+' + file.added), element('span', 'removed', '−' + file.removed));
        } else {
            row.append(element('span', 'status', '行数未记录'));
        }
        entry.append(row);
        if (file.replayable && turn.editable) {
            const actions = element('div', 'actions');
            const fields = {turn: turn.id, path: file.path};
            actions.append(anchor('撤回修改', 'undo', fields), anchor('再次应用', 'redo', fields));
            entry.append(actions);
        } else {
            entry.append(element('div', 'status', '此修改缺少可重放的补丁'));
        }
        details.append(entry);
    }
    return details;
}

function renderTranscript(data) {
    const focused = document.activeElement;
    const scrollBefore = window.scrollY;
    const oldHeight = document.documentElement.scrollHeight;
    const nearBottom = oldHeight - (scrollBefore + window.innerHeight) < 72;
    const open = new Set([...root.querySelectorAll('details[open]')].map(node => node.dataset.turn));
    const fragment = document.createDocumentFragment();
    document.body.classList.toggle('claude', data.claude);
    for (const label of data.header || []) fragment.append(element('div', 'meta', label));
    if (data.notice) fragment.append(element('div', 'meta', data.notice));
    if (data.earlier) fragment.append(anchor(data.earlier, 'earlier'));
    for (const turn of data.turns || []) {
        const article = element('article');
        if (turn.state) article.append(element('div', 'meta', turn.state));
        for (const message of turn.messages) {
            if (message.timing) article.append(element('div', 'meta', message.timing));
            const bubble = element('section', 'message ' + (message.kind === 'user' ? 'user' : 'assistant'));
            const content = element('div');
            content.innerHTML = md.render(message.text);
            linkBareFiles(content);
            bubble.append(element('div', 'byline', message.label), content);
            article.append(bubble);
        }
        if (turn.timing) article.append(element('div', 'meta', turn.timing));
        if (turn.notice) article.append(element('div', 'meta', turn.notice));
        if (turn.editLabel) {
            const actions = element('div', 'turn-actions');
            actions.append(filesPanel(turn, open.has(turn.id)));
            if (turn.branch) actions.append(anchor(turn.branch, 'fork', {turn: turn.id}));
            article.append(actions);
            if (turn.quota) article.append(element('div', 'meta quota', turn.quota));
        }
        if (turn.ending) article.append(element('div', 'ending', turn.ending));
        fragment.append(article);
    }
    root.replaceChildren(fragment);
    if (data.prepend) window.scrollTo(0, scrollBefore + document.documentElement.scrollHeight - oldHeight);
    else if (nearBottom) window.scrollTo(0, document.documentElement.scrollHeight);
    else window.scrollTo(0, scrollBefore);
    if (focused?.getAttribute('href') && focused !== document.body) {
        const url = focused.getAttribute('href');
        const replacement = [...root.querySelectorAll('a')].find(link => link.getAttribute('href') === url);
        replacement?.focus({preventScroll: true});
    }
}
