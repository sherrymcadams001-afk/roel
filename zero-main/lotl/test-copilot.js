const axios = require('axios');

const BASE_URL = 'http://localhost:3000';

async function testCopilot() {
    console.log('🧪 Testing Microsoft Copilot Endpoint...');
    
    // 1. Test Connection
    try {
        console.log('  Testing connection...');
        const res = await axios.get(`${BASE_URL}/test/copilot`);
        console.log('  ✅ Connection OK:', res.data);
    } catch (err) {
        console.error('  ❌ Connection Failed:', err.response ? err.response.data : err.message);
        // Don't exit - might just be that the server isn't running yet or browser issue
    }

    // 2. Send Prompt
    try {
        console.log('  Sending prompt: "Hello, are you Copilot?"');
        const res = await axios.post(`${BASE_URL}/copilot`, {
            prompt: "Hello, imply that you are Microsoft Copilot in your answer."
        }, { timeout: 120000 }); // 2 min timeout

        if (res.data.success) {
            console.log('  ✅ Prompt Success!');
            console.log('  🤖 Reply:', res.data.reply);
        } else {
            console.error('  ❌ Prompt Failed:', res.data);
        }
    } catch (err) {
        console.error('  ❌ Request Error:', err.response ? err.response.data : err.message);
    }
}

testCopilot().catch(err => console.error(err));
