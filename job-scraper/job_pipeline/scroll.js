// Resolve the element that actually scrolls, not just the virtual list wrapper.
(card, options) => {
    const canScroll = el => el && el.clientHeight > 0 &&
        el.scrollHeight > el.clientHeight + 1 &&
        /auto|scroll|overlay/.test(getComputedStyle(el).overflowY);
    let root = null;
    const hint = options.container ? document.querySelector(options.container) : null;
    if (hint && hint.contains(card) && canScroll(hint)) root = hint;
    if (!root) {
        for (let el = card.parentElement; el; el = el.parentElement) {
            if (canScroll(el)) { root = el; break; }
        }
    }
    if (!root) root = document.scrollingElement;
    if (!root) throw new Error('No feed scrolling element found');
    const before = root.scrollTop;
    if (options.action === 'advance') {
        root.scrollTop = before + Math.max(1, Math.floor(root.clientHeight * 0.8));
    } else if (options.action === 'restore') {
        root.scrollTop = options.top;
    }
    return {top: root.scrollTop, before, height: root.clientHeight,
        total: root.scrollHeight, target: root === document.scrollingElement ?
        'document' : (root === hint ? 'configured panel' : 'card ancestor'),
        bottom: root.scrollTop + root.clientHeight >= root.scrollHeight - 2};
}
