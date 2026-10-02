import { handler } from './handlers';

app.get('/things', handler);
app.post('/things', (req, res) => {});
