<?php
// Liara healthCheck probe: healthy while Apache/PHP answers below 500.
$ctx = stream_context_create(['http' => ['timeout' => 4, 'ignore_errors' => true]]);
$body = @file_get_contents('http://127.0.0.1:{{port}}{{health_path}}', false, $ctx);
if ($body === false) {
    exit(1);
}
$code = (int) (explode(' ', $http_response_header[0] ?? 'HTTP/1.1 500')[1] ?? 500);
exit($code < 500 ? 0 : 1);
