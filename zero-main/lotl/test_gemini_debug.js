const puppeteer = require('puppeteer-core');

(async () => {
    try {
        const v = await (await fetch('http://127.0.0.1:9222/json/version')).json();
        const b = await puppeteer.connect({browserWSEndpoint: v.webSocketDebuggerUrl, defaultViewport: null});
        const t = (await b.targets()).find(t => t.url().includes('gemini.google.com') && t.type() === 'page');
        
        if (!t) { 
            console.log('No Gemini tab found'); 
            process.exit(1); 
        }
        
        const p = await t.page();
        await p.bringToFront();
        
        // Check what contenteditable elements exist
        const r = await p.evaluate(() => {
            const els = document.querySelectorAll('div[contenteditable="true"]');
            return Array.from(els).map(e => ({
                tag: e.tagName, 
                cls: e.className.slice(0,80), 
                visible: e.offsetWidth > 0,
                width: e.offsetWidth,
                height: e.offsetHeight
            }));
        });
        console.log('Contenteditable elements:', JSON.stringify(r, null, 2));
        
        // Try to type directly
        const typed = await p.evaluate(() => {
            const el = document.querySelector('div[contenteditable="true"]');
            if (!el) return 'NO ELEMENT';
            el.focus();
            el.innerText = 'HELLO TEST';
            return 'OK: ' + el.className;
        });
        console.log('Direct type:', typed);
        
        b.disconnect();
    } catch (e) {
        console.error('Error:', e.message);
    }
})();
