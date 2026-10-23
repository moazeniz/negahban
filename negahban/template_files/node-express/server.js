const express = require('express');

const app = express();
app.use(express.json());
app.get('/', (req, res) => res.json({ app: '{{app_name}}', message: 'سلام از لیارا! این برنامه با قالب Express نگهبان ساخته شد.' }));
app.get('{{health_path}}', (req, res) => res.json({ status: 'ok' }));

const port = Number(process.env.PORT || {{port}});
app.listen(port, '0.0.0.0', () => console.log(`listening on ${port}`));
