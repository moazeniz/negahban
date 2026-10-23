<?php
// Starter page; a real Laravel project ships its own public/index.php.
if (parse_url($_SERVER['REQUEST_URI'] ?? '/', PHP_URL_PATH) === '{{health_path}}') {
    header('Content-Type: text/plain');
    echo 'ok';
    exit;
}
header('Content-Type: text/html; charset=utf-8');
echo '<!doctype html><html lang="fa" dir="rtl"><meta charset="utf-8"><title>{{app_name}}</title>'
   . '<h1>{{app_name}}</h1><p>قالب Laravel نگهبان روی لیارا اجرا شد. سورس پروژه‌ی Laravel خود را جایگزین این فایل کنید.</p></html>';
