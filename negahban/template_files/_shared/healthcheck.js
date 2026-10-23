// Liara healthCheck probe: healthy while the server answers below 500.
const req = require('http').get('http://127.0.0.1:{{port}}{{health_path}}', r => process.exit(r.statusCode < 500 ? 0 : 1));
req.on('error', () => process.exit(1));
req.setTimeout(4000, () => process.exit(1));
