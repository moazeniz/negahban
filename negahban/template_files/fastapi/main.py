import os

from fastapi import FastAPI

app = FastAPI(title='{{app_name}}')


@app.get('/')
def index():
    return {'app': '{{app_name}}', 'env': os.getenv('APP_ENV', 'production'), 'message': 'سلام از لیارا! این برنامه با قالب FastAPI نگهبان ساخته شد.'}


@app.get('{{health_path}}')
def health():
    return {'status': 'ok'}
