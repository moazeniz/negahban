from django.db import connection
from django.http import JsonResponse
from django.urls import path


def index(request):
    return JsonResponse({'app': '{{app_name}}', 'database': connection.vendor, 'message': 'سلام از لیارا! این برنامه با قالب Django نگهبان ساخته شد.'})


def health(request):
    return JsonResponse({'status': 'ok'})


urlpatterns = [path('', index), path('{{health_path}}'.strip('/') + '/', health)]
